"""The ``upgrade`` command + the ``updaters`` entry-point seam (F3a).

This command runs a package manager against the user's OWN Python environment, so the
suite is written as a safety proof first and a feature test second. Nothing here ever
installs, execs, or touches a real environment: the distribution metadata, ``sys.prefix``
/ ``sys.base_prefix``, the stdlib dir the PEP 668 marker lives beside, ``shutil.which``,
the read-only git probe, the step runner, and ``os.execv`` are ALL injected.

The load-bearing assertions:

  * the full detection matrix — editable / pipx / uv tool / venv / OS-managed /
    uncertain — each pinned to the action it chooses;
  * an OS-managed or unidentifiable interpreter executes NOTHING (the step runner is
    asserted never to have been called), and safety outranks precedence: an editable
    install in an OS-managed interpreter is still notify-only;
  * a virtual environment stays a safe target even when the interpreter it was built
    from carries the marker (refusing that would break the common Debian-family case);
  * an editable install never resolves to a PyPI upgrade — that would detach the user's
    own checkout from the package they import;
  * pip is always invoked as ``<python> -m pip``, never the ``pip`` console script;
  * the re-exec into the skill re-sync performs ZERO buddhi_review imports after the
    upgrade subprocess returns (enforced with a real import recorder on ``sys.meta_path``,
    because the package root resolves its public names lazily);
  * a broken third-party entry point is skipped rather than fatal, and no installed
    updater can talk the command past its own safety gate.
"""
import argparse
import ast
import io
import json
import sys
from pathlib import Path

import pytest

from buddhi_review import cli, update_banner, updaters

_PUBLIC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PUBLIC / "tools"))

import publish_gate as g  # noqa: E402

_PY = "/opt/py/bin/python3"


# ── Injected environment doubles ────────────────────────────────────────────────────

class FakeDist:
    """An ``importlib.metadata`` distribution stand-in: only ``direct_url.json`` matters."""

    def __init__(self, direct_url=None):
        self._direct_url = direct_url

    def read_text(self, name):
        return self._direct_url if name == "direct_url.json" else None


def _editable_direct_url(source_dir) -> str:
    return json.dumps({"url": Path(source_dir).as_uri(), "dir_info": {"editable": True}})


def _clean_git(argv, cwd):
    """A clean checkout on a named branch."""
    if "rev-parse" in argv:
        return 0, "true\n"
    if "status" in argv:
        return 0, ""
    if "symbolic-ref" in argv:
        return 0, "refs/heads/main\n"
    return 0, ""


def _all_present(binary):
    return f"/usr/bin/{binary}"


def _nothing_present(binary):
    return None


def _venv(tmp_path):
    """A (prefix, base_prefix) pair that reads as a virtual environment."""
    prefix = tmp_path / "venv"
    prefix.mkdir(exist_ok=True)
    base = tmp_path / "base"
    base.mkdir(exist_ok=True)
    return str(prefix), str(base)


def _stdlib(tmp_path, *, managed: bool) -> str:
    """A stdlib dir, with or without the PEP 668 ``EXTERNALLY-MANAGED`` marker."""
    d = tmp_path / ("stdlib-managed" if managed else "stdlib-plain")
    d.mkdir(exist_ok=True)
    if managed:
        (d / updaters.EXTERNALLY_MANAGED).write_text("[externally-managed]\n")
    return str(d)


def _detect(tmp_path, *, dist=None, prefix=None, base_prefix=None, managed=False,
            which=_all_present, git=_clean_git, **kw):
    """detect_install_method with every seam defaulted to a safe venv target."""
    if prefix is None or base_prefix is None:
        v_prefix, v_base = _venv(tmp_path)
        prefix = prefix if prefix is not None else v_prefix
        base_prefix = base_prefix if base_prefix is not None else v_base
    return updaters.detect_install_method(
        dist_fn=lambda: dist if dist is not None else FakeDist(),
        prefix=prefix, base_prefix=base_prefix,
        stdlib_dir=_stdlib(tmp_path, managed=managed),
        python=_PY, which=which, git=git, **kw)


# ── The detection matrix ────────────────────────────────────────────────────────────

def test_editable_pulls_then_reinstalls_in_place(tmp_path):
    src = tmp_path / "checkout"
    src.mkdir()
    plan = _detect(tmp_path, dist=FakeDist(_editable_direct_url(src)))
    assert plan.method == updaters.EDITABLE and plan.notify_only is False
    assert plan.source_dir == str(src)
    assert [s.argv for s in plan.steps] == [
        ("git", "-C", str(src), "pull"),
        (_PY, "-m", "pip", "install", "-e", str(src)),
    ]
    # A failed pull leaves the checkout untouched → the soft outcome, not a broken upgrade.
    assert plan.steps[0].soft_fail is True and plan.steps[1].soft_fail is False


def test_editable_never_resolves_to_a_pypi_upgrade(tmp_path):
    """The whole point of detecting editable: upgrading it from PyPI would silently
    detach the user's own checkout from the package they import."""
    src = tmp_path / "checkout"
    src.mkdir()
    plan = _detect(tmp_path, dist=FakeDist(_editable_direct_url(src)))
    for step in plan.steps:
        assert "-U" not in step.argv and "--upgrade" not in step.argv
        # The only mention of the dist name would be a PyPI target; the editable
        # re-install names the local directory instead.
        assert updaters.DIST_NAME not in step.argv
    assert any(step.argv[-2:] == ("-e", str(src)) for step in plan.steps)


def test_pipx_upgrades_through_pipx(tmp_path):
    prefix, base = _venv(tmp_path)
    (Path(prefix) / "pipx_metadata.json").write_text("{}")
    plan = _detect(tmp_path, prefix=prefix, base_prefix=base)
    assert plan.method == updaters.PIPX and plan.notify_only is False
    assert [s.argv for s in plan.steps] == [("pipx", "upgrade", "buddhi-review")]


def test_uv_tool_upgrades_through_uv(tmp_path):
    prefix, base = _venv(tmp_path)
    (Path(prefix) / "uv-receipt.toml").write_text("")
    plan = _detect(tmp_path, prefix=prefix, base_prefix=base)
    assert plan.method == updaters.UV_TOOL and plan.notify_only is False
    assert [s.argv for s in plan.steps] == [("uv", "tool", "upgrade", "buddhi-review")]


def test_uv_tool_recognised_from_the_layout_without_a_receipt(tmp_path):
    prefix = tmp_path / "share" / "uv" / "tools" / "buddhi-review"
    prefix.mkdir(parents=True)
    plan = _detect(tmp_path, prefix=str(prefix), base_prefix=str(tmp_path / "base"))
    assert plan.method == updaters.UV_TOOL and plan.notify_only is False


@pytest.mark.parametrize("prefix_parts", [
    ("home", "tools", "uv", "venv"),      # a plain venv kept under a "tools" dir
    ("srv", "uv", "tools-backup", "env"),  # "tools-backup" is not the uv tools dir
    ("opt", "tools", "venv"),
])
def test_a_plain_venv_is_not_mistaken_for_a_uv_tool(tmp_path, prefix_parts):
    """A loose "the path mentions uv and tools somewhere" test would route an ordinary
    virtualenv to ``uv tool upgrade``, which at best fails and at worst upgrades a
    DIFFERENT, uv-managed copy while leaving the environment in use untouched."""
    prefix = tmp_path.joinpath(*prefix_parts)
    prefix.mkdir(parents=True)
    plan = _detect(tmp_path, prefix=str(prefix), base_prefix=str(tmp_path / "base"))
    assert plan.method == updaters.VENV
    assert all(step.argv[0] != "uv" for step in plan.steps)


def test_venv_upgrades_with_python_dash_m_pip(tmp_path):
    plan = _detect(tmp_path)
    assert plan.method == updaters.VENV and plan.notify_only is False
    assert [s.argv for s in plan.steps] == [
        (_PY, "-m", "pip", "install", "-U", "buddhi-review")]


def test_pip_is_never_the_console_script(tmp_path):
    """``pip install`` must run as ``<python> -m pip``: the console script is locked on
    Windows while it is running, so pip cannot replace its own executable."""
    for plan in (_detect(tmp_path),
                 _detect(tmp_path, dist=FakeDist(_editable_direct_url(tmp_path)))):
        for step in plan.steps:
            if "pip" in step.argv:
                assert step.argv[0] == _PY and step.argv[1:3] == ("-m", "pip")
                assert step.argv[0] != "pip"


def test_os_managed_interpreter_is_notify_only(tmp_path):
    plan = _detect(tmp_path, prefix=str(tmp_path / "usr"),
                   base_prefix=str(tmp_path / "usr"), managed=True)
    assert plan.method == updaters.SYSTEM
    assert plan.notify_only is True and plan.steps == ()
    assert "package manager" in plan.reason
    assert any("pip" in line for line in plan.manual)


def test_unclassifiable_interpreter_is_notify_only(tmp_path):
    """No venv and NO marker: we cannot positively call it a safe owned target, so it
    gets exactly the same treatment as a known-unsafe one."""
    plan = _detect(tmp_path, prefix=str(tmp_path / "usr"),
                   base_prefix=str(tmp_path / "usr"), managed=False)
    assert plan.method == updaters.UNCERTAIN
    assert plan.notify_only is True and plan.steps == ()


def test_safety_outranks_precedence_for_an_editable_install(tmp_path):
    """Detection precedence puts editable first, but precedence is an ORDER, not an
    override of the gate: an editable install in an OS-managed interpreter is still
    notify-only, and the manual guidance still names the editable commands."""
    src = tmp_path / "checkout"
    src.mkdir()
    plan = _detect(tmp_path, dist=FakeDist(_editable_direct_url(src)),
                   prefix=str(tmp_path / "usr"), base_prefix=str(tmp_path / "usr"),
                   managed=True)
    assert plan.method == updaters.EDITABLE
    assert plan.notify_only is True and plan.steps == ()
    assert any("git" in line and "pull" in line for line in plan.manual)


def test_a_venv_built_from_an_os_managed_python_is_still_safe(tmp_path):
    """The marker is asked about the TARGET we would write to, never the base. A venv
    on a Debian-family host is the common case — refusing it would be wrong."""
    plan = _detect(tmp_path, managed=True)  # marker present, but prefix != base_prefix
    assert plan.method == updaters.VENV and plan.notify_only is False


def test_unreadable_metadata_is_uncertain_and_notify_only(tmp_path):
    def _boom():
        raise ValueError("no metadata")

    prefix, base = _venv(tmp_path)
    plan = updaters.detect_install_method(
        dist_fn=_boom, prefix=prefix, base_prefix=base,
        stdlib_dir=_stdlib(tmp_path, managed=False), python=_PY,
        which=_all_present, git=_clean_git)
    assert plan.method == updaters.UNCERTAIN
    assert plan.notify_only is True and plan.steps == ()


@pytest.mark.parametrize("marker,binary", [("pipx_metadata.json", "pipx"),
                                           ("uv-receipt.toml", "uv")])
def test_a_missing_tool_binary_is_notify_only(tmp_path, marker, binary):
    prefix, base = _venv(tmp_path)
    (Path(prefix) / marker).write_text("{}")
    plan = _detect(tmp_path, prefix=prefix, base_prefix=base,
                   which=lambda b: None if b == binary else f"/usr/bin/{b}")
    assert plan.notify_only is True and plan.steps == ()
    assert binary in plan.reason


def test_editable_without_git_on_path_is_notify_only(tmp_path):
    src = tmp_path / "checkout"
    src.mkdir()
    plan = _detect(tmp_path, dist=FakeDist(_editable_direct_url(src)),
                   which=_nothing_present)
    assert plan.method == updaters.EDITABLE and plan.notify_only is True
    assert "git" in plan.reason


def test_editable_with_a_missing_source_tree_is_notify_only(tmp_path):
    gone = tmp_path / "gone"
    plan = _detect(tmp_path, dist=FakeDist(_editable_direct_url(gone)))
    assert plan.method == updaters.EDITABLE and plan.notify_only is True
    assert "missing" in plan.reason


@pytest.mark.parametrize("git_fn,needle", [
    (lambda argv, cwd: (128, "") if "rev-parse" in argv else _clean_git(argv, cwd),
     "not a git repository"),
    (lambda argv, cwd: (0, " M x.py\n") if "status" in argv else _clean_git(argv, cwd),
     "uncommitted changes"),
    (lambda argv, cwd: (1, "") if "symbolic-ref" in argv else _clean_git(argv, cwd),
     "detached"),
])
def test_editable_edge_cases_never_force(tmp_path, git_fn, needle):
    """A checkout we cannot safely advance is reported, never stashed, reset, or forced."""
    src = tmp_path / "checkout"
    src.mkdir()
    plan = _detect(tmp_path, dist=FakeDist(_editable_direct_url(src)), git=git_fn)
    assert plan.method == updaters.EDITABLE
    assert plan.notify_only is True and plan.steps == ()
    assert needle in plan.reason


@pytest.mark.parametrize("editable", [True, 1, "true", "True"])
def test_a_non_boolean_editable_marker_still_reads_as_editable(tmp_path, editable):
    """The two failure directions are not symmetric: reading a REAL editable install as
    non-editable sends it to PyPI and detaches the user's checkout, while the reverse
    only re-installs it from its own directory. So the truthiness test is a little
    wider than the spec, deliberately."""
    src = tmp_path / "checkout"
    src.mkdir()
    dist = FakeDist(json.dumps({"url": src.as_uri(), "dir_info": {"editable": editable}}))
    plan = _detect(tmp_path, dist=dist)
    assert plan.method == updaters.EDITABLE
    assert all("-U" not in step.argv for step in plan.steps)


@pytest.mark.parametrize("editable", [False, 0, "false", None])
def test_a_falsy_editable_marker_is_not_editable(tmp_path, editable):
    src = tmp_path / "checkout"
    src.mkdir()
    dist = FakeDist(json.dumps({"url": src.as_uri(), "dir_info": {"editable": editable}}))
    assert _detect(tmp_path, dist=dist).method == updaters.VENV


@pytest.mark.parametrize("marker,binary", [("pipx_metadata.json", "pipx"),
                                           ("uv-receipt.toml", "uv")])
def test_an_empty_which_result_counts_as_missing(tmp_path, marker, binary):
    """A lookup that answers with an empty string has not found a runnable binary; a
    plain ``is None`` test would let an empty argv[0] through to the runner."""
    prefix, base = _venv(tmp_path)
    (Path(prefix) / marker).write_text("{}")
    plan = _detect(tmp_path, prefix=prefix, base_prefix=base, which=lambda b: "")
    assert plan.notify_only is True and plan.steps == ()


def test_an_empty_which_result_blocks_the_editable_path(tmp_path):
    src = tmp_path / "checkout"
    src.mkdir()
    plan = _detect(tmp_path, dist=FakeDist(_editable_direct_url(src)), which=lambda b: "")
    assert plan.method == updaters.EDITABLE and plan.notify_only is True


def test_a_non_editable_direct_url_is_not_treated_as_editable(tmp_path):
    """``direct_url.json`` also exists for a plain VCS/local install; only an explicit
    ``dir_info.editable == true`` counts."""
    dist = FakeDist(json.dumps({"url": "https://example.invalid/x.whl"}))
    plan = _detect(tmp_path, dist=dist)
    assert plan.method == updaters.VENV


def test_malformed_direct_url_is_not_treated_as_editable(tmp_path):
    plan = _detect(tmp_path, dist=FakeDist("{not json"))
    assert plan.method == updaters.VENV


# ── The updaters seam ───────────────────────────────────────────────────────────────

class FakeUpdater:
    def __init__(self, *, active=True, priority=0, name="fake", rec=None,
                 outcome=None, boom=False):
        self._active = active
        self.priority = priority
        self.name = name
        self.rec = rec if rec is not None else []
        self._outcome = outcome
        self._boom = boom

    def is_active(self):
        return self._active

    def run_update(self, plan, **opts):
        self.rec.append(plan)
        if self._boom:
            raise RuntimeError("boom")
        return self._outcome if self._outcome is not None else updaters.UpdateOutcome(0, True)


def test_free_updater_satisfies_the_protocol():
    free = updaters.FreeUpdater()
    assert isinstance(free, updaters.Updater)
    assert free.is_active() is True
    assert free.name == "free" and free.priority == 0


def test_discovery_always_includes_the_builtin_updater():
    found = updaters.discover_updaters(entry_points_fn=lambda group: [])
    assert any(isinstance(u, updaters.FreeUpdater) for u in found)


def test_discovery_surfaces_a_registered_updater():
    class _EP:
        def load(self):
            return FakeUpdater

    found = updaters.discover_updaters(entry_points_fn=lambda group: [_EP()])
    names = {getattr(u, "name", None) for u in found}
    assert names == {"fake", "free"}


def test_a_broken_entry_point_is_skipped_not_fatal():
    class _BadEP:
        def load(self):
            raise ImportError("no such module")

    found = updaters.discover_updaters(entry_points_fn=lambda group: [_BadEP()])
    assert [getattr(u, "name", None) for u in found] == ["free"]


def test_an_entry_point_of_the_wrong_shape_is_skipped():
    class _ShapelessEP:
        def load(self):
            return object()

    found = updaters.discover_updaters(entry_points_fn=lambda group: [_ShapelessEP()])
    assert [getattr(u, "name", None) for u in found] == ["free"]


def test_discovery_does_not_duplicate_the_builtin_when_registered():
    class _FreeEP:
        def load(self):
            return updaters.FreeUpdater

    found = updaters.discover_updaters(entry_points_fn=lambda group: [_FreeEP()])
    assert sum(1 for u in found if getattr(u, "name", None) == "free") == 1


def test_a_higher_priority_active_updater_wins():
    free = updaters.FreeUpdater()
    low = FakeUpdater(priority=1, name="low")
    high = FakeUpdater(priority=50, name="high")
    assert updaters.select_updater([free, low, high]) is high


def test_an_inactive_updater_is_skipped():
    free = updaters.FreeUpdater()
    off = FakeUpdater(active=False, priority=99, name="off")
    assert updaters.select_updater([off, free]) is free


def test_an_updater_that_raises_from_is_active_is_inactive():
    class _Boom:
        name = "boom"
        priority = 99

        def is_active(self):
            raise RuntimeError("boom")

        def run_update(self, plan, **opts):  # pragma: no cover - never selected
            raise AssertionError

    free = updaters.FreeUpdater()
    assert updaters.select_updater([_Boom(), free]) is free


def test_a_name_property_that_raises_never_breaks_discovery():
    """``getattr(obj, "name", default)`` only swallows AttributeError, so a hostile or
    buggy ``name`` property would otherwise take the command down inside discovery."""
    class _HostileName:
        priority = 0

        @property
        def name(self):
            raise RuntimeError("boom")

        def is_active(self):
            return False

        def run_update(self, plan, **opts):  # pragma: no cover - never active
            raise AssertionError

    class _EP:
        def load(self):
            return _HostileName

    found = updaters.discover_updaters(entry_points_fn=lambda group: [_EP()])
    assert any(isinstance(u, updaters.FreeUpdater) for u in found)


def test_a_hostile_repr_never_escapes_the_failure_handler():
    class _Hostile:
        priority = 5

        @property
        def name(self):
            raise RuntimeError("no name for you")

        def is_active(self):
            return True

        def run_update(self, plan, **opts):
            raise RuntimeError("boom")

    plan = updaters.UpgradePlan(method=updaters.VENV, notify_only=False, reason="")
    outcome = updaters.perform_update(_Hostile(), plan)   # must not raise
    assert outcome.returncode == 1 and outcome.upgraded is False


def test_a_non_int_priority_never_breaks_ordering():
    free = updaters.FreeUpdater()
    junk = FakeUpdater(priority="not-a-number", name="junk")
    assert updaters.select_updater([junk, free]) in (junk, free)  # no exception


def test_perform_update_reports_a_raising_updater_without_re_running():
    """A partially-applied upgrade must never be followed by a second installer, so a
    raising updater is a clean non-zero failure — not a silent free-updater retry."""
    plan = updaters.UpgradePlan(method=updaters.VENV, notify_only=False, reason="")
    outcome = updaters.perform_update(FakeUpdater(boom=True), plan)
    assert outcome.returncode == 1 and outcome.upgraded is False
    assert "failed" in (outcome.message or "")


@pytest.mark.parametrize("returned,expect_rc,expect_upgraded", [
    (0, 0, True), (3, 3, False), (None, 0, True),
])
def test_a_plain_return_value_is_coerced(returned, expect_rc, expect_upgraded):
    plan = updaters.UpgradePlan(method=updaters.VENV, notify_only=False, reason="")
    outcome = updaters.perform_update(FakeUpdater(outcome=returned), plan)
    # ``outcome=None`` means the double falls back to its own default, so exercise the
    # None-coercion directly as well.
    if returned is None:
        outcome = updaters._coerce_outcome(None)
    assert outcome.returncode == expect_rc and outcome.upgraded is expect_upgraded


# ── FreeUpdater step execution ──────────────────────────────────────────────────────

class Runner:
    def __init__(self, codes=None):
        self.calls = []
        self._codes = list(codes or [])

    def __call__(self, argv, cwd=None):
        self.calls.append((tuple(argv), cwd))
        return self._codes.pop(0) if self._codes else 0


def _venv_plan():
    return updaters.UpgradePlan(
        method=updaters.VENV, notify_only=False, reason="venv",
        steps=(updaters.Step((_PY, "-m", "pip", "install", "-U", "buddhi-review")),),
        manual=("manual line",))


def _editable_plan(src="/src"):
    return updaters.UpgradePlan(
        method=updaters.EDITABLE, notify_only=False, reason="editable",
        steps=(updaters.Step(("git", "-C", src, "pull"), cwd=src, soft_fail=True),
               updaters.Step((_PY, "-m", "pip", "install", "-e", src), cwd=src)),
        manual=(f"git -C {src} pull", f"{_PY} -m pip install -e {src}"), source_dir=src)


def test_free_updater_runs_every_step_in_order():
    runner = Runner()
    outcome = updaters.FreeUpdater().run_update(_editable_plan(), runner=runner)
    assert outcome.returncode == 0 and outcome.upgraded is True
    assert [c[0][0] for c in runner.calls] == ["git", _PY]


def test_free_updater_stops_at_the_first_hard_failure():
    runner = Runner(codes=[0, 7])
    outcome = updaters.FreeUpdater().run_update(_editable_plan(), runner=runner)
    assert outcome.returncode == 7 and outcome.upgraded is False
    assert len(runner.calls) == 2


def test_a_failed_git_pull_changes_nothing_and_does_not_reinstall():
    runner = Runner(codes=[1])
    outcome = updaters.FreeUpdater().run_update(_editable_plan(), runner=runner)
    assert outcome.returncode == 0 and outcome.upgraded is False  # → no skill re-sync
    assert len(runner.calls) == 1  # the pip re-install never ran


def test_free_updater_runs_nothing_for_a_notify_only_plan():
    """Defence in depth: even handed a notify-only plan that somehow carries steps, the
    updater runs nothing — ``notify_only`` alone is enough to stop it."""
    runner = Runner()
    plan = updaters.UpgradePlan(
        method=updaters.SYSTEM, notify_only=True, reason="no",
        steps=(updaters.Step(("pip", "install", "-U", "buddhi-review")),))
    outcome = updaters.FreeUpdater().run_update(plan, runner=runner)
    assert runner.calls == [] and outcome.upgraded is False


def test_a_notify_only_plan_can_never_carry_steps():
    """The plan builder strips steps from a notify-only verdict, so a future branch that
    sets both cannot hand an executable command to anything downstream."""
    plan = updaters._plan(updaters.SYSTEM, notify_only=True, reason="no",
                          steps=(updaters.Step(("pip", "install", "-U", "x")),))
    assert plan.steps == ()


# ── The command ─────────────────────────────────────────────────────────────────────

class Execer:
    def __init__(self, on_call=None):
        self.calls = []
        self._on_call = on_call

    def __call__(self, path, argv):
        self.calls.append((path, list(argv)))
        if self._on_call:
            self._on_call()


def _args(**kw):
    ns = argparse.Namespace(check=False, dry_run=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _run_upgrade(monkeypatch, plan, *, args=None, runner=None, execer=None,
                 latest_fn=None):
    monkeypatch.setattr(updaters, "detect_install_method", lambda **kw: plan)
    out, err = io.StringIO(), io.StringIO()
    rc = cli._upgrade(args or _args(), runner=runner, execer=execer,
                      latest_fn=latest_fn, out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("method", [updaters.SYSTEM, updaters.UNCERTAIN])
def test_notify_only_executes_nothing_and_exits_success(monkeypatch, method):
    plan = updaters.UpgradePlan(
        method=method, notify_only=True, reason="not a safe target",
        # Steps deliberately present: the command must key off notify_only alone, so a
        # plan that carries both still executes nothing.
        steps=(updaters.Step(("pip", "install", "-U", "buddhi-review")),),
        manual=("python3 -m pip install -U buddhi-review", "buddhi-review install-skills"))
    runner, execer = Runner(), Execer()
    rc, out, _err = _run_upgrade(monkeypatch, plan, runner=runner, execer=execer)
    assert rc == 0
    assert runner.calls == [] and execer.calls == []   # nothing was run, nothing exec'd
    assert "python3 -m pip install -U buddhi-review" in out
    assert "buddhi-review install-skills" in out


def test_dry_run_executes_nothing(monkeypatch):
    runner, execer = Runner(), Execer()
    rc, out, _err = _run_upgrade(monkeypatch, _venv_plan(), args=_args(dry_run=True),
                                 runner=runner, execer=execer)
    assert rc == 0 and runner.calls == [] and execer.calls == []
    assert "[dry-run]" in out
    assert "-m pip install -U buddhi-review" in out          # the exact command
    assert "-m buddhi_review.cli install-skills" in out      # and the re-sync that follows


def test_check_performs_no_upgrade(monkeypatch):
    monkeypatch.delenv("BUDDHI_NO_UPDATE_CHECK", raising=False)
    runner, execer = Runner(), Execer()
    rc, out, _err = _run_upgrade(monkeypatch, _venv_plan(), args=_args(check=True),
                                 runner=runner, execer=execer,
                                 latest_fn=lambda: "99.0.0")
    assert rc == 0 and runner.calls == [] and execer.calls == []
    assert "99.0.0" in out and "buddhi-review upgrade" in out


def test_check_speaks_up_when_already_current(monkeypatch):
    """Unlike the deliberately-muted launch banner, an explicit --check always reports
    a verdict — silence would read as a failure."""
    monkeypatch.delenv("BUDDHI_NO_UPDATE_CHECK", raising=False)
    rc, out, _err = _run_upgrade(monkeypatch, _venv_plan(), args=_args(check=True),
                                 latest_fn=lambda: "0.0.1")
    assert rc == 0 and "is current" in out


def test_check_distinguishes_offline_from_up_to_date(monkeypatch):
    monkeypatch.delenv("BUDDHI_NO_UPDATE_CHECK", raising=False)
    rc, out, _err = _run_upgrade(monkeypatch, _venv_plan(), args=_args(check=True),
                                 latest_fn=lambda: None)
    assert rc == 0 and "Could not determine" in out and "is current" not in out


def test_check_wins_over_dry_run(monkeypatch):
    monkeypatch.delenv("BUDDHI_NO_UPDATE_CHECK", raising=False)
    runner, execer = Runner(), Execer()
    rc, out, _err = _run_upgrade(monkeypatch, _venv_plan(),
                                 args=_args(check=True, dry_run=True),
                                 runner=runner, execer=execer, latest_fn=lambda: "99.0.0")
    assert rc == 0 and "[dry-run]" not in out
    assert runner.calls == [] and execer.calls == []


def test_check_honours_the_no_update_check_knob(monkeypatch):
    monkeypatch.setenv("BUDDHI_NO_UPDATE_CHECK", "1")
    called = []
    rc, out, _err = _run_upgrade(monkeypatch, _venv_plan(), args=_args(check=True),
                                 latest_fn=lambda: called.append(1) or "99.0.0")
    assert rc == 0 and called == []  # the opt-out really does skip the network
    assert "BUDDHI_NO_UPDATE_CHECK" in out


def test_a_successful_upgrade_re_execs_into_the_skill_resync(monkeypatch):
    runner, execer = Runner(), Execer()
    rc, out, _err = _run_upgrade(monkeypatch, _venv_plan(), runner=runner, execer=execer)
    assert rc == 0
    assert [c[0] for c in runner.calls] == [
        (_PY, "-m", "pip", "install", "-U", "buddhi-review")]
    assert execer.calls == [
        (sys.executable, [sys.executable, "-m", "buddhi_review.cli", "install-skills"])]
    assert "Re-syncing" in out


def test_an_already_current_install_still_re_syncs(monkeypatch):
    """pip exits 0 whether or not the version moved, and the re-sync is the documented
    repair path — so it runs either way, and the output says so."""
    runner, execer = Runner(codes=[0]), Execer()
    rc, out, _err = _run_upgrade(monkeypatch, _venv_plan(), runner=runner, execer=execer)
    assert rc == 0 and len(execer.calls) == 1
    assert "already-current" in out


def test_a_failed_upgrade_exits_non_zero_and_does_not_re_sync(monkeypatch):
    runner, execer = Runner(codes=[4]), Execer()
    rc, _out, err = _run_upgrade(monkeypatch, _venv_plan(), runner=runner, execer=execer)
    assert rc == 4 and execer.calls == []      # no skill re-sync after a failed upgrade
    assert "failed" in err


def test_a_failed_git_pull_prints_the_manual_commands_and_does_not_re_sync(monkeypatch):
    runner, execer = Runner(codes=[1]), Execer()
    rc, out, _err = _run_upgrade(monkeypatch, _editable_plan(), runner=runner, execer=execer)
    assert rc == 0 and execer.calls == []
    assert "git -C /src pull" in out


def test_a_failed_exec_names_the_command_and_exits_non_zero(monkeypatch):
    """The upgrade already landed, so silently continuing would leave stale skills on
    disk with no sign anything went wrong."""
    def _boom():
        raise OSError("exec failed")

    runner, execer = Runner(), Execer(on_call=_boom)
    rc, _out, err = _run_upgrade(monkeypatch, _venv_plan(), runner=runner, execer=execer)
    assert rc == 1
    assert "-m buddhi_review.cli install-skills" in err


def _package_bound_names(tree):
    """Every name in ``cli.py`` that is bound to something imported from this package —
    module aliases and re-exported symbols alike. Referencing any of them after the
    upgrade risks resolving against replaced-on-disk bytes."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "buddhi_review":
                names.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] == "buddhi_review":
                    names.add(a.asname or a.name.split(".")[0])
    return names


def test_the_post_upgrade_tail_never_touches_the_package():
    """THE TORN-STATE GUARD, checked statically — and this is the authoritative one.

    Once the upgrade has replaced this package's files, the running process holds a
    half-stale module graph, and the package root resolves its public names through a
    lazy PEP-562 ``__getattr__``. So between the upgrade returning and the exec there
    must be no import of this package AND no reference to any name bound from it —
    including a plain attribute read on an already-imported sibling, which no runtime
    import hook can see. Reading the source is the only check that covers all of it."""
    src = (_PUBLIC / "buddhi_review" / "cli.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = _package_bound_names(tree)
    # Sanity: the guard is looking at real names, not an empty set.
    assert {"updaters", "update_banner", "__version__"} <= forbidden

    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_upgrade")
    idx = next(i for i, st in enumerate(fn.body)
               if any(isinstance(c, ast.Attribute) and c.attr == "perform_update"
                      for c in ast.walk(st)))
    tail = fn.body[idx + 1:]
    # A refactor that moved the exec out of the tail would make this test vacuous.
    assert any(isinstance(n, ast.Name) and n.id == "execv"
               for st in tail for n in ast.walk(st)), "the tail no longer re-execs"

    offenders = []
    for st in tail:
        for node in ast.walk(st):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                offenders.append(f"line {node.lineno}: import")
            elif isinstance(node, ast.Name) and node.id in forbidden:
                offenders.append(f"line {node.lineno}: reads {node.id!r}")
    assert offenders == [], f"post-upgrade tail touches the package: {offenders}"


class _ImportRecorder:
    """Records every buddhi_review module IMPORT while armed.

    A ``sys.modules`` hit bypasses ``sys.meta_path`` entirely, so this only fires on a
    genuinely NEW import. That makes it a useful RUNTIME confirmation but a weak guard
    on its own — hence the static check above; this one adds the observation that a real
    call really does perform no fresh import."""

    def __init__(self):
        self.armed = False
        self.seen = []

    def find_spec(self, fullname, path=None, target=None):
        if self.armed and fullname.split(".")[0] == "buddhi_review":
            self.seen.append(fullname)
        return None  # never claim the module; the real finders still handle it


# The handler modules cli.py imports lazily — the ones a careless tail would newly
# import. Evicted before the probe so this test cannot go quiet just because a sibling
# test module happened to import one of them first.
_LAZY_HANDLER_MODULES = (
    "buddhi_review.skill_install", "buddhi_review.open_pr", "buddhi_review.wizard",
    "buddhi_review.rebase_gate", "buddhi_review.git_guardrail_hook",
)


def test_no_package_import_happens_between_the_upgrade_and_the_re_exec(monkeypatch):
    """The runtime half of the torn-state guard: a real ``_upgrade`` call performs no
    fresh package import after the upgrade subprocess returns."""
    for name in _LAZY_HANDLER_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    recorder = _ImportRecorder()
    monkeypatch.setattr(sys, "meta_path", [recorder] + list(sys.meta_path))
    seen_at_exec = []

    def _arm(argv, cwd=None):
        recorder.armed = True          # the upgrade "replaces the package" here
        return 0

    def _at_exec():
        seen_at_exec.extend(recorder.seen)
        recorder.armed = False

    execer = Execer(on_call=_at_exec)
    rc, _out, _err = _run_upgrade(monkeypatch, _venv_plan(), runner=_arm, execer=execer)
    recorder.armed = False
    assert rc == 0 and len(execer.calls) == 1
    assert seen_at_exec == [], f"imported after the upgrade: {seen_at_exec}"


def test_the_re_exec_target_is_a_minimal_install_skills(monkeypatch):
    execer = Execer()
    _run_upgrade(monkeypatch, _venv_plan(), runner=Runner(), execer=execer)
    _path, argv = execer.calls[0]
    # A bare install-skills: no --force, so a user's edited skill is never clobbered.
    assert argv[1:] == ["-m", "buddhi_review.cli", "install-skills"]
    assert "--force" not in argv


def test_an_installed_updater_cannot_bypass_the_safety_gate(monkeypatch):
    """The gate lives in the command, ahead of discovery: a notify-only verdict is
    reached before any updater is selected, so a high-priority installed updater never
    even sees the call."""
    hijacker = FakeUpdater(priority=999, name="hijacker")
    monkeypatch.setattr(updaters, "discover_updaters", lambda **kw: [hijacker])
    plan = updaters.UpgradePlan(method=updaters.SYSTEM, notify_only=True,
                                reason="OS-managed", manual=("do it yourself",))
    runner, execer = Runner(), Execer()
    rc, _out, _err = _run_upgrade(monkeypatch, plan, runner=runner, execer=execer)
    assert rc == 0 and hijacker.rec == [] and runner.calls == [] and execer.calls == []


def test_a_selected_updater_receives_the_gated_plan(monkeypatch):
    winner = FakeUpdater(priority=10, name="winner")
    monkeypatch.setattr(updaters, "discover_updaters", lambda **kw: [winner])
    plan = _venv_plan()
    execer = Execer()
    rc, _out, _err = _run_upgrade(monkeypatch, plan, runner=Runner(), execer=execer)
    assert rc == 0 and winner.rec == [plan] and len(execer.calls) == 1


def test_a_broken_installed_entry_point_never_breaks_the_command(monkeypatch):
    class _BadEP:
        def load(self):
            raise ImportError("nope")

    monkeypatch.setattr(updaters, "_iter_entry_points", lambda group: [_BadEP()])
    runner, execer = Runner(), Execer()
    rc, _out, _err = _run_upgrade(monkeypatch, _venv_plan(), runner=runner, execer=execer)
    assert rc == 0 and len(runner.calls) == 1   # the built-in updater still ran it


# ── Wiring ──────────────────────────────────────────────────────────────────────────

def test_upgrade_is_a_known_free_command():
    parser = cli.build_parser()
    assert "upgrade" in cli._known_commands(parser)


def test_upgrade_flags_default_to_read_nothing():
    args = cli.build_parser().parse_args(["upgrade"])
    assert args.command == "upgrade" and args.check is False and args.dry_run is False


def test_main_dispatches_upgrade(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_upgrade", lambda args: seen.append(args) or 0)
    assert cli.main(["upgrade", "--dry-run"]) == 0
    assert seen and seen[0].dry_run is True


def test_the_builtin_updater_is_registered_in_the_project_metadata():
    text = (_PUBLIC / "pyproject.toml").read_text(encoding="utf-8")
    assert '[project.entry-points."buddhi_review.updaters"]' in text
    assert 'free = "buddhi_review.updaters:FreeUpdater"' in text
    assert updaters.UPDATERS_GROUP == "buddhi_review.updaters"


# ── Deliverable 4: the banner names the real command ────────────────────────────────

def test_the_banner_names_the_upgrade_command_not_a_method_blind_pip():
    line = update_banner.format_banner(buddhi_latest="0.3.0", current="0.2.1")
    assert "buddhi-review upgrade" in line
    assert "pip install" not in line       # a method-blind pip line could damage an install
    assert "\n" not in line                # still exactly one muted line


def test_latest_release_is_fail_open(tmp_path):
    def _boom():
        raise RuntimeError("offline")

    assert update_banner.latest_release(
        now=1_000_000.0, state_path=tmp_path / "u.json", fetcher=_boom,
        ttl_seconds=3600.0) is None


def test_latest_release_reads_the_shared_cache(tmp_path):
    state = tmp_path / "u.json"
    state.write_text(json.dumps({"checked_at": 10 ** 12, "latest": "9.9.9"}))
    assert update_banner.latest_release(
        now=1_000_000.0, state_path=state, fetcher=lambda: None,
        ttl_seconds=3600.0) == "9.9.9"


# ── OSS purity ──────────────────────────────────────────────────────────────────────

def test_the_new_module_is_publish_clean():
    src = (_PUBLIC / "buddhi_review" / "updaters.py").read_text(encoding="utf-8")
    assert g.scan_paid_and_publish(src) == []
    assert g.scan_entitlement(src) == []


#: Every module the seam is allowed to import — all standard library. Naming ANY other
#: distribution would be exactly the coupling the entry-points group exists to avoid, so
#: this list is the capability-neutrality guard: a new import has to be justified here.
_ALLOWED_IMPORT_ROOTS = {
    "__future__", "dataclasses", "importlib", "json", "os", "pathlib", "shlex",
    "shutil", "subprocess", "sys", "sysconfig", "typing", "urllib",
}


def test_the_seam_names_a_group_never_another_package():
    """Capability-neutral by construction: the entry-points GROUP string is the entire
    coupling to anything outside this package."""
    src = (_PUBLIC / "buddhi_review" / "updaters.py").read_text(encoding="utf-8")
    roots = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= _ALLOWED_IMPORT_ROOTS, f"unexpected import: {roots - _ALLOWED_IMPORT_ROOTS}"
    assert updaters.UPDATERS_GROUP == "buddhi_review.updaters"
