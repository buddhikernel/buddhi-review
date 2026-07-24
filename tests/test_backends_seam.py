"""The front-door seam: backend discovery, selection, and the launch dispatcher.

Proves the delegation path with a TEST DOUBLE — a fake backend whose ``is_active()``
is True — and asserts both ``/review-pr`` and ``/open-pr`` route to it, and fall
back to the free engine when no active backend is registered. No real loop spawns:
the launcher subprocess is replaced with a recording stub.
"""
import inspect
import io
import subprocess
import types
from pathlib import Path

import pytest

import buddhi_review
from buddhi_review import backends, cli, open_pr

_LAUNCHER = Path(buddhi_review.__file__).parent / "launch-review.sh"


# ── A recording test double ──────────────────────────────────────────────────────

class FakeBackend:
    def __init__(self, *, active=True, priority=0, name="fake", rec=None):
        self._active = active
        self.priority = priority
        self.name = name
        self.rec = rec if rec is not None else []

    def is_active(self):
        return self._active

    def run_review_loop(self, pr, repo, cwd, **opts):
        self.rec.append((pr, repo, cwd))
        return 0


class BrokenBackend:
    name = "broken"
    priority = 99

    def is_active(self):  # pragma: no cover - selection should never reach this
        raise RuntimeError("boom")

    def run_review_loop(self, pr, repo, cwd, **opts):  # pragma: no cover
        raise RuntimeError("boom")


# ── Protocol shape ────────────────────────────────────────────────────────────────

def test_free_backend_satisfies_the_protocol():
    free = backends.FreeBackend()
    assert isinstance(free, backends.Backend)
    assert free.is_active() is True
    assert free.name == "free" and free.priority == 0


def test_backend_protocol_is_exported_from_package():
    assert buddhi_review.Backend is backends.Backend
    assert buddhi_review.launch_review_loop is backends.launch_review_loop
    assert buddhi_review.discover_backends is backends.discover_backends


# ── discover_backends ─────────────────────────────────────────────────────────────

def test_discover_always_includes_free_with_no_entry_points():
    found = backends.discover_backends(entry_points_fn=lambda group: [])
    assert any(getattr(b, "name", None) == "free" for b in found)
    assert any(isinstance(b, backends.FreeBackend) for b in found)


def test_discover_surfaces_an_entry_point_backend():
    class _EP:
        def load(self):
            return FakeBackend  # a Backend class

    found = backends.discover_backends(entry_points_fn=lambda group: [_EP()])
    names = [getattr(b, "name", None) for b in found]
    assert "fake" in names and "free" in names  # both present


def test_discover_skips_a_broken_entry_point():
    class _BadEP:
        def load(self):
            raise ImportError("no such module")

    # A broken third-party plugin must never crash discovery; free still appears.
    found = backends.discover_backends(entry_points_fn=lambda group: [_BadEP()])
    assert [getattr(b, "name", None) for b in found] == ["free"]


def test_discover_does_not_duplicate_free_when_registered():
    class _FreeEP:
        def load(self):
            return backends.FreeBackend

    found = backends.discover_backends(entry_points_fn=lambda group: [_FreeEP()])
    assert sum(1 for b in found if getattr(b, "name", None) == "free") == 1


def test_a_hostile_name_cannot_make_a_broken_entry_point_fatal():
    """The de-duplication test at the end of discovery sits OUTSIDE the per-entry-point
    ``try``, so reading (or comparing) a third-party ``name`` there must stay on built-in
    behaviour. Otherwise a backend whose ``name`` raises makes a broken plugin FATAL —
    the one thing discovery promises can never happen."""
    class _HostileName:
        def __eq__(self, other):
            raise RuntimeError("hostile __eq__")

        __hash__ = None

    class _RaisingProperty(FakeBackend):
        @property
        def name(self):
            raise RuntimeError("hostile property")

    class _ValueEP:
        def load(self):
            b = FakeBackend(name="x")
            b.name = _HostileName()
            return b

    class _PropertyEP:
        def load(self):
            return _RaisingProperty()

    for ep in (_ValueEP(), _PropertyEP()):
        found = backends.discover_backends(entry_points_fn=lambda group: [ep])
        assert any(isinstance(b, backends.FreeBackend) for b in found)


# ── select_backend ────────────────────────────────────────────────────────────────

def test_select_prefers_highest_priority_active():
    free = backends.FreeBackend()
    low = FakeBackend(active=True, priority=1, name="low")
    high = FakeBackend(active=True, priority=50, name="high")
    assert backends.select_backend([free, low, high]) is high


def test_select_skips_inactive_and_falls_to_free():
    free = backends.FreeBackend()
    inactive = FakeBackend(active=False, priority=99, name="off")
    assert backends.select_backend([inactive, free]) is free


def test_select_treats_an_erroring_backend_as_inactive():
    free = backends.FreeBackend()
    # A backend that throws from is_active() must not be chosen, nor crash selection.
    assert backends.select_backend([BrokenBackend(), free]) is free


def test_select_returns_free_even_with_no_candidates():
    assert isinstance(backends.select_backend([]), backends.FreeBackend)


# ── launch_review_loop dispatcher ──────────────────────────────────────────────────

def test_dispatcher_routes_to_active_backend():
    rec = []
    fake = FakeBackend(active=True, priority=100, rec=rec)
    rc = backends.launch_review_loop("7", "o/r", "/x",
                                     backends=[fake, backends.FreeBackend()])
    assert rc == 0
    assert rec == [("7", "o/r", "/x")]  # the active backend handled it; free did not


def test_dispatcher_falls_back_to_free_when_none_active(monkeypatch):
    rec = []
    monkeypatch.setattr(backends, "_detached_run", lambda cmd, *a, **kw: rec.append(cmd))
    rc = backends.launch_review_loop("7", "o/r", "/x",
                                     backends=[FakeBackend(active=False),
                                               backends.FreeBackend()])
    assert rc == 0
    assert rec and rec[0][:3] == ["bash", str(_LAUNCHER), "7"]  # free engine launched


# ── FreeBackend launch behavior (flag forwarding + missing launcher) ───────────────

def test_free_backend_forwards_flags_to_run_loop():
    rec = []
    free = backends.FreeBackend()
    rc = free.run_review_loop(
        "7", "o/r", "/x", err=io.StringIO(),
        runner=lambda cmd: rec.append(cmd), launcher=str(_LAUNCHER),
        auto_merge=True, max_rounds=3, rr=True,
        verify_fixes="always", test_failure_mode="off")
    assert rc == 0
    argv = rec[0]
    assert argv[:3] == ["bash", str(_LAUNCHER), "7"]
    assert argv[argv.index("--repo") + 1] == "o/r"
    assert argv[argv.index("--cwd") + 1] == "/x"
    assert "--auto-merge" in argv
    assert argv[argv.index("--max-rounds") + 1] == "3"
    assert "--rr" in argv
    assert argv[argv.index("--verify-fixes") + 1] == "always"
    assert argv[argv.index("--test-failure-mode") + 1] == "off"


def test_free_backend_no_auto_merge_flag():
    rec = []
    backends.FreeBackend().run_review_loop(
        "9", "o/r", "/x", runner=lambda cmd: rec.append(cmd),
        launcher=str(_LAUNCHER), auto_merge=False)
    assert "--no-auto-merge" in rec[0]


def test_free_backend_missing_launcher_returns_1(tmp_path):
    err = io.StringIO()
    rc = backends.launch_free_loop("7", "o/r", "/x", err=err,
                                   launcher=str(tmp_path / "nope.sh"))
    assert rc == 1
    msg = err.getvalue()
    assert "launch the loop manually" in msg and "run-loop" in msg


def test_free_backend_runner_exception_is_caught(tmp_path):
    err = io.StringIO()

    def boom(cmd):
        raise OSError("cannot spawn")

    rc = backends.launch_free_loop("7", "o/r", "/x", err=err, runner=boom,
                                   launcher=str(_LAUNCHER))
    assert rc == 1
    assert "could not launch the review loop" in err.getvalue()


def test_launcher_refusal_exit_code_propagates_cleanly(tmp_path):
    # When the launcher RAN and exited non-zero (a startup-gate refusal: exit 2),
    # it already printed its own in-session refusal panel. launch_free_loop must
    # propagate that exit code so the front door reflects the refusal — and must
    # NOT add the misleading generic "could not launch … run it manually" line.
    err = io.StringIO()

    def refused(cmd):
        raise subprocess.CalledProcessError(2, cmd)

    rc = backends.launch_free_loop("7", "o/r", "/x", err=err, runner=refused,
                                   launcher=str(_LAUNCHER))
    assert rc == 2
    assert "could not launch the review loop" not in err.getvalue()


def test_detached_run_uses_check_true_so_refusals_propagate():
    # The launcher's `exit 2` surfaces as a clean front-door exit 2 ONLY because
    # _detached_run runs subprocess.run(..., check=True): that raises
    # CalledProcessError, which launch_free_loop catches and propagates. The
    # injected-runner tests above can't exercise this link, so pin it directly —
    # a flip to check=False would swallow the refusal (front-door return 0) with
    # the rest of the suite still green.
    assert "check=True" in inspect.getsource(backends._detached_run)


# ── /review-pr routes through the dispatcher ───────────────────────────────────────

def test_review_pr_routes_to_active_backend(monkeypatch):
    rec = []
    monkeypatch.setattr(
        backends, "discover_backends",
        lambda **k: [FakeBackend(active=True, priority=100, rec=rec),
                     backends.FreeBackend()])
    args = cli.build_parser().parse_args(["review-pr", "7", "--repo", "o/r", "--cwd", "/x"])
    rc = cli._review_pr(args)
    assert rc == 0
    assert rec == [("7", "o/r", "/x")]


def test_review_pr_falls_back_to_free(monkeypatch):
    rec = []
    monkeypatch.setattr(backends, "discover_backends",
                        lambda **k: [backends.FreeBackend()])
    monkeypatch.setattr(backends, "_detached_run", lambda cmd, *a, **kw: rec.append(cmd))
    args = cli.build_parser().parse_args(
        ["review-pr", "7", "--repo", "o/r", "--cwd", "/x", "--rr"])
    rc = cli._review_pr(args)
    assert rc == 0
    assert rec and rec[0][:3] == ["bash", str(_LAUNCHER), "7"]
    assert "--rr" in rec[0]  # the front door's flags reached the engine launch


# ── /open-pr routes through the dispatcher ───────────────────────────────────────

def _gh_run(argv, cwd=None, timeout=60, input=None):
    if argv[:3] == ["gh", "pr", "create"]:
        return types.SimpleNamespace(
            returncode=0, stdout="https://github.com/acme/widgets/pull/7\n", stderr="")
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def test_open_pr_default_launch_routes_to_active_backend(monkeypatch):
    rec = []
    monkeypatch.setattr(backends, "discover_backends",
                        lambda **k: [FakeBackend(active=True, priority=9, rec=rec)])
    out, err = io.StringIO(), io.StringIO()
    url = open_pr.create_and_launch(
        "acme/widgets", "/work", "main", "feat/x", title="t", body="b",
        run=_gh_run, launch=open_pr._dispatch_launch, out=out, err=err)
    assert url == "https://github.com/acme/widgets/pull/7"
    assert rec == [("7", "acme/widgets", "/work")]  # routed through the dispatcher
    assert out.getvalue().strip().splitlines()[-1] == url  # PR URL still last on stdout


def test_open_pr_default_launch_falls_back_to_free(monkeypatch):
    rec = []
    monkeypatch.setattr(backends, "discover_backends",
                        lambda **k: [backends.FreeBackend()])
    monkeypatch.setattr(backends, "_detached_run", lambda cmd, *a, **kw: rec.append(cmd))
    out, err = io.StringIO(), io.StringIO()
    url = open_pr.create_and_launch(
        "acme/widgets", "/work", "main", "feat/x", title="t", body="b",
        run=_gh_run, launch=open_pr._dispatch_launch, out=out, err=err)
    assert url == "https://github.com/acme/widgets/pull/7"
    assert rec and rec[0][:3] == ["bash", str(_LAUNCHER), "7"]  # free engine launched


def test_open_pr_no_loop_never_launches(monkeypatch):
    called = []
    monkeypatch.setattr(backends, "discover_backends",
                        lambda **k: [FakeBackend(active=True, rec=called)])
    out, err = io.StringIO(), io.StringIO()
    open_pr.create_and_launch(
        "acme/widgets", "/work", "main", "feat/x", title="t", body="b",
        run=_gh_run, launch=open_pr._dispatch_launch, out=out, err=err, no_loop=True)
    assert called == []  # --no-loop skips the dispatcher entirely
