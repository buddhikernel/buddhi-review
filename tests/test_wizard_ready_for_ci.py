"""F4 — the label-gated-CI provision wire-up: the bundled ``tests-ready-for-ci.yml``
template, the CI-command detector + baker, the probe-before-install, the server-side
installer (reusing F2's ``_create_file_pr``), and its wiring into BOTH the per-repo
confirm flow and the full wizard when a repo opts into label-gated CI.

Bakes in the P7 UX lessons: #2 (an attention-grabbing "merge this PR — the gate is
INACTIVE until merged" warn callout), #4 (probe-before-install: no redundant second
PR when the gate is already on the default branch), #5 (the non-Claude reviewer
entry-point hints). Also guards the OSS-purity surface: the shipped template must be
publish-clean and use only standard runners.
"""
import base64
import io
import sys
import types
from pathlib import Path

import pytest

from buddhi_review import config, managed_files, wizard
from conftest import _yn_bridge

# The version the bundled ready-for-ci template currently ships at — an installed
# copy at this version is "up to date" and must NOT be offered an update.
_SHIPPED_RFC = managed_files.shipped_version(wizard._ready_for_ci_template_path())

# Re-use the publish-gate scanner the OSS-purity suite uses (one definition).
sys.path.insert(0, str(Path(wizard.__file__).resolve().parent.parent / "tools"))
import publish_gate as g  # noqa: E402

REPO = "octocat/Hello-World"
# Reviewer indices in wizard._REVIEWERS == ("copilot", "gemini", "codex", "claude").
COPILOT, GEMINI, CODEX, CLAUDE = 0, 1, 2, 3


def _R(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _recorder(router):
    calls = []

    def run(argv, cwd=None, timeout=30, input=None):
        calls.append({"argv": list(argv), "input": input})
        return router(list(argv), input)

    return run, calls


# ── The bundled template: shipped, clean, standard runners only ─────────────────────

def _template_path():
    return wizard._ready_for_ci_template_path()


def test_template_is_bundled_under_skills():
    """The template lives in the package's shipped references dir (covered by the
    ``skills/**/*`` package-data glob, so it rides the wheel)."""
    p = _template_path()
    assert p.exists(), "F4 must bundle tests-ready-for-ci.yml in the package"
    pkg = Path(wizard.__file__).resolve().parent
    assert p.resolve().is_relative_to(pkg / "skills"), "must sit under skills/ to ship"


def test_template_has_exactly_one_ci_command_marker():
    text = _template_path().read_text(encoding="utf-8")
    standalone = [ln for ln in text.splitlines() if ln.strip() == wizard._CI_COMMAND_MARKER]
    assert len(standalone) == 1, "the baker needs exactly one standalone marker line"


def test_template_uses_standard_runner_only():
    """Larger/self-hosted runners bill even on public repos — the bundled template
    must pin the free standard ``ubuntu-latest`` runner and nothing fancier."""
    text = _template_path().read_text(encoding="utf-8")
    runs_on = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("runs-on:")]
    assert runs_on == ["runs-on: ubuntu-latest"], runs_on
    low = text.lower()
    for fancy in ("buildjet", "larger", "namespace", "-cores", "self-hosted", "macos", "windows"):
        assert fancy not in low, f"non-standard runner hint '{fancy}' in the template"


def test_template_is_publish_clean():
    """Adversarial claim #1: the shipped template leaks no paid/private/MONO surface
    (it is scanned by the publish gate as installed product)."""
    text = _template_path().read_text(encoding="utf-8")
    assert g.scan_paid_and_publish(text) == []


def test_wizard_source_stays_publish_clean_after_f4():
    """The F4 wire-up code itself adds no paid/private string to the wizard source."""
    text = Path(wizard.__file__).read_text(encoding="utf-8")
    assert g.scan_paid_and_publish(text) == []
    assert g.scan_entitlement(text) == []


# ── _detect_ci_command — read-only stack detection ──────────────────────────────────

def test_detect_none_for_missing_or_empty(tmp_path):
    assert wizard._detect_ci_command(None) is None
    assert wizard._detect_ci_command(str(tmp_path / "nope")) is None
    assert wizard._detect_ci_command(str(tmp_path)) is None  # empty dir


def test_detect_makefile_ci_then_test(tmp_path):
    (tmp_path / "Makefile").write_text("ci:\n\techo hi\n", encoding="utf-8")
    assert wizard._detect_ci_command(str(tmp_path)) == "make ci"
    (tmp_path / "Makefile").write_text("test:\n\techo hi\n", encoding="utf-8")
    assert wizard._detect_ci_command(str(tmp_path)) == "make test"


@pytest.mark.parametrize("lockfile,expect", [
    ("package-lock.json", "npm ci && npm test"),
    ("yarn.lock", "yarn install && yarn test"),
    ("pnpm-lock.yaml", "pnpm install && pnpm test"),
    (None, "npm install && npm test"),
])
def test_detect_node_by_lockfile(tmp_path, lockfile, expect):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
    if lockfile:
        (tmp_path / lockfile).write_text("", encoding="utf-8")
    assert wizard._detect_ci_command(str(tmp_path)) == expect


def test_detect_node_skips_npm_placeholder_test(tmp_path):
    """npm's own ``"no test specified" … exit 1`` default must NOT be baked (it would
    recreate the very red gate the auto-wire kills)."""
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}',
        encoding="utf-8")
    assert wizard._detect_ci_command(str(tmp_path)) is None


def test_detect_python_requires_manifest_and_tests(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    # manifest but NO test evidence → fall through (pytest would red-gate an empty repo)
    assert wizard._detect_ci_command(str(tmp_path)) is None
    (tmp_path / "tests").mkdir()
    assert wizard._detect_ci_command(str(tmp_path)) == "python -m pip install -e . && python -m pytest"


def test_detect_go_and_rust(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    assert wizard._detect_ci_command(str(tmp_path)) == "go test ./..."
    (tmp_path / "go.mod").unlink()
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    assert wizard._detect_ci_command(str(tmp_path)) == "cargo test"


# ── _bake_ci_command — marker substitution preserving indentation ───────────────────

def test_bake_substitutes_marker_preserving_indent():
    text = _template_path().read_text(encoding="utf-8")
    baked = wizard._bake_ci_command(text, "make ci")
    assert wizard._CI_COMMAND_MARKER not in baked, "the literal marker must be gone"
    # The command lands at the marker line's original indentation (10 spaces here).
    assert "          make ci" in baked


def test_bake_multiline_command_keeps_each_line_indented():
    text = _template_path().read_text(encoding="utf-8")
    baked = wizard._bake_ci_command(text, "pip install -e .\npython -m pytest")
    assert "          pip install -e ." in baked
    assert "          python -m pytest" in baked
    assert wizard._CI_COMMAND_MARKER not in baked


# ── _probe_ready_for_ci_workflow — P7 #4 probe-before-install ───────────────────────

def _probe_router(*, present, content="base64==", rc=None):
    def router(argv, _inp):
        if argv[:2] == ["gh", "api"] and "tests-ready-for-ci.yml" in " ".join(argv):
            if rc is not None:
                return _R(returncode=rc, stdout="")
            return _R(returncode=0, stdout=content + "\n") if present else _R(returncode=1, stdout="")
        return _R()
    return router


def test_probe_true_when_present():
    run, calls = _recorder(_probe_router(present=True))
    assert wizard._probe_ready_for_ci_workflow(REPO, run) is True
    # it queries the workflow path on the default branch via a contents fetch
    api = next(c["argv"] for c in calls if c["argv"][:2] == ["gh", "api"])
    assert "contents/.github/workflows/tests-ready-for-ci.yml" in " ".join(api)


def test_probe_false_when_absent_or_null_or_no_repo():
    run, _ = _recorder(_probe_router(present=False))
    assert wizard._probe_ready_for_ci_workflow(REPO, run) is False
    run2, _ = _recorder(_probe_router(present=True, content="null"))
    assert wizard._probe_ready_for_ci_workflow(REPO, run2) is False
    run3, _ = _recorder(_probe_router(present=True))
    assert wizard._probe_ready_for_ci_workflow(None, run3) is False


# ── _offer_install_ready_for_ci ─────────────────────────────────────────────────────

def _install_router(*, present=False, version=None, default="main", head_sha="deadbeef",
                    pr_url="https://github.com/octocat/Hello-World/pull/9",
                    put_rc=0, pr_rc=0):
    # The content the contents-probe returns for an already-installed gate: a file
    # carrying the buddhi-managed-version marker (``version``), or — when ``version``
    # is None — a legacy UNMARKED copy that the version-check treats as outdated.
    if version is None:
        _installed_b64 = base64.b64encode(b"name: legacy ci\n").decode()
    else:
        _installed_b64 = base64.b64encode(
            f"# buddhi-managed-version: {version}\nname: ci\n".encode()).decode()

    def router(argv, _inp):
        joined = " ".join(argv)
        if argv[:3] == ["gh", "repo", "view"]:
            return _R(returncode=0, stdout=default + "\n")
        if argv[:2] == ["gh", "pr"]:
            return _R(returncode=pr_rc, stdout=(pr_url + "\n") if pr_rc == 0 else "")
        if argv[:2] == ["gh", "api"]:
            if "-X" in argv and "PUT" in argv:
                return _R(returncode=put_rc)
            if "tests-ready-for-ci.yml" in joined and "--jq" in argv and ".content" in argv:
                # the present-probe AND the version-check both read this content
                return _R(returncode=0, stdout=_installed_b64 + "\n") if present else _R(returncode=1, stdout="")
            if "/git/ref/heads/" in joined and "--jq" in argv:
                return _R(returncode=0, stdout=head_sha + "\n")
            if argv[2].endswith("/git/refs"):
                return _R(returncode=0)
        return _R()
    return router


def _offer(tmp_path, *, is_tty, router, make_detectable=True, input_answers=None):
    if make_detectable:
        (tmp_path / "Makefile").write_text("ci:\n\techo hi\n", encoding="utf-8")
    run, calls = _recorder(router)
    buf = io.StringIO()

    def in_fn(prompt=""):
        for k, v in (input_answers or {}).items():
            if k in prompt:
                return v
        return ""
    import contextlib
    with _tty(is_tty):
        result = wizard._offer_install_ready_for_ci(
            REPO, str(tmp_path), run=run, pal=wizard._Palette(False), stream=buf,
            input_fn=in_fn)
    return result, buf.getvalue(), calls


class _tty:
    def __init__(self, value):
        self.value = value
        self._orig = None
        self._orig_ss = None

    def __enter__(self):
        self._orig = wizard._is_tty
        wizard._is_tty = lambda: self.value
        if self.value:
            # On a forced TTY, _ask_yes_no routes through the module-level
            # single_select (which requires _read_key / a real TTY).  Replace it
            # with a bridge that reads the test's input_fn instead.
            self._orig_ss = wizard.single_select
            wizard.single_select = _yn_bridge
        return self

    def __exit__(self, *a):
        wizard._is_tty = self._orig
        if self.value:
            wizard.single_select = self._orig_ss


def test_offer_skips_when_already_present_and_current(tmp_path):
    """P7 #4 — a gate already on the default branch AT THE CURRENT VERSION opens NO
    redundant second PR."""
    result, out, calls = _offer(tmp_path, is_tty=True,
                                router=_install_router(present=True, version=_SHIPPED_RFC))
    assert result is None
    assert "already on the default branch" in out
    assert not any(c["argv"][:2] == ["gh", "pr"] for c in calls), "must not open a PR"


def test_offer_updates_when_present_but_outdated(tmp_path):
    """Versioned sync: a gate present on the default branch but at an OLDER version (or
    legacy unmarked) is offered an in-place UPDATE — a PR on the dedicated update
    branch, the CI command re-baked, and the muted git-revert reassurance shown."""
    result, out, calls = _offer(
        tmp_path, is_tty=True, router=_install_router(present=True, version=None),
        input_answers={"Install the label-gated CI workflow": "y"})
    assert result == "pr"
    # The update rides its OWN branch (distinct from the add branch) so the two can't
    # collide, and the PUT targets the ready-for-ci path.
    put = next(c["argv"] for c in calls if "-X" in c["argv"] and "PUT" in c["argv"])
    assert "branch=buddhi/update-ready-for-ci-workflow" in put
    pr = next(c["argv"] for c in calls if c["argv"][:2] == ["gh", "pr"])
    assert pr[pr.index("--head") + 1] == "buddhi/update-ready-for-ci-workflow"
    assert "older" in out and "revert the PR" in out


def test_offer_happy_path_opens_pr_with_baked_command(tmp_path):
    result, out, calls = _offer(
        tmp_path, is_tty=True, router=_install_router(),
        input_answers={"Install the label-gated CI workflow": "y"})  # accept; cmd blank → detected
    assert result == "pr"
    # The PUT writes the ready-for-ci path on the installer's OWN branch (not claude's).
    put = next(c["argv"] for c in calls if "-X" in c["argv"] and "PUT" in c["argv"])
    assert put[4] == "repos/octocat/Hello-World/contents/.github/workflows/tests-ready-for-ci.yml"
    assert "branch=buddhi/add-ready-for-ci-workflow" in put
    # The detected command ("make ci") is baked in; the literal marker never ships.
    content_arg = next(t for t in put if t.startswith("content="))
    decoded = base64.b64decode(content_arg.split("content=", 1)[1]).decode("utf-8")
    assert "make ci" in decoded
    assert wizard._CI_COMMAND_MARKER not in decoded
    # The PR opens base=default branch, head=the installer branch.
    pr = next(c["argv"] for c in calls if c["argv"][:2] == ["gh", "pr"])
    assert pr[pr.index("--base") + 1] == "main"
    assert pr[pr.index("--head") + 1] == "buddhi/add-ready-for-ci-workflow"
    # P7 #2 — a LOUD merge-me callout (warn glyph row), not a dim line.
    assert "MERGE THIS PR" in out and "INACTIVE" in out


def test_offer_uses_entered_command_over_detected(tmp_path):
    result, _, calls = _offer(
        tmp_path, is_tty=True, router=_install_router(),
        input_answers={"Install the label-gated CI workflow": "y",
                       "Command this gate runs at merge": "make verify"})
    assert result == "pr"
    put = next(c["argv"] for c in calls if "-X" in c["argv"] and "PUT" in c["argv"])
    decoded = base64.b64decode(
        next(t for t in put if t.startswith("content=")).split("content=", 1)[1]).decode()
    # Check the baked run: step at its indentation — "make ci" also appears in the
    # template's example COMMENTS, so match the substituted (10-space) command line.
    assert "          make verify" in decoded
    assert "          make ci" not in decoded


def test_offer_declined_opens_no_pr(tmp_path):
    result, out, calls = _offer(
        tmp_path, is_tty=True, router=_install_router(),
        input_answers={"Install the label-gated CI workflow": "n"})
    assert result is None
    assert not any(c["argv"][:2] == ["gh", "pr"] for c in calls)
    assert "config preference" in out


def test_offer_non_tty_with_detection_installs(tmp_path):
    result, out, calls = _offer(tmp_path, is_tty=False, router=_install_router())
    assert result == "pr"
    assert "auto-detected CI command" in out
    assert any(c["argv"][:2] == ["gh", "pr"] for c in calls)


def test_offer_non_tty_no_detection_skips(tmp_path):
    result, out, calls = _offer(tmp_path, is_tty=False, router=_install_router(),
                                make_detectable=False)
    assert result is None
    assert "no TTY" in out.lower() or "Skipping" in out
    assert not any(c["argv"][:2] == ["gh", "pr"] for c in calls)


def test_offer_tty_no_command_aborts(tmp_path):
    result, out, calls = _offer(
        tmp_path, is_tty=True, router=_install_router(), make_detectable=False,
        input_answers={"Install the label-gated CI workflow": "y"})  # accept but no cmd
    assert result is None
    assert "No CI command given" in out
    assert not any(c["argv"][:2] == ["gh", "pr"] for c in calls)


def test_offer_pr_failure_prints_manual_fallback(tmp_path):
    result, out, _ = _offer(
        tmp_path, is_tty=True, router=_install_router(pr_rc=1),
        input_answers={"Install the label-gated CI workflow": "y"})
    assert result is None
    assert "by hand" in out or "manually" in out


# ── Wire-up into the per-repo confirm flow + the full wizard ─────────────────────────

def _confirm_run(*, gh_auth=True, default="main", present=False, version=None):
    """A run() for confirm_repo_interactive driving a copilot-only fleet (no claude
    provisioning noise) + the ready-for-ci installer."""
    base = _install_router(present=present, version=version, default=default)
    calls = []

    def run(argv, cwd=None, timeout=30, input=None):
        calls.append(list(argv))
        if argv[:3] == ["gh", "auth", "status"]:
            return _R(returncode=0 if gh_auth else 1, stdout="")
        if argv[:2] == ["git", "-C"]:
            return _R(returncode=1, stdout="")  # cwd passed explicitly; no infer needed
        return base(argv, input)
    return run, calls


def _drive_confirm(tmp_path, run, *, lgc_on, reviewers={COPILOT}):
    (tmp_path / "Makefile").write_text("ci:\n\techo hi\n", encoding="utf-8")
    cfg_path = tmp_path / "config.yaml"
    ss_answers = {"GLOBAL default": 1, "Auto-merge default for": 0,
                  "Label-gated CI default for": 1 if lgc_on else 0,
                  "Confirm: enable label-gated CI": 1}

    def ss(prompt, options, *, preselect=0, **kw):
        for k, idx in ss_answers.items():
            if k in prompt:
                return idx
        return preselect

    def in_fn(prompt=""):
        for k, v in {"Copilot": "y", "Install the label-gated CI workflow": "y"}.items():
            if k in prompt:
                return v
        return ""
    buf = io.StringIO()
    with _tty(True):
        rc = wizard.confirm_repo_interactive(
            REPO, str(tmp_path), run=run, spawn_command=lambda *a, **k: None,
            getpass_fn=lambda *a: "", pal=wizard._Palette(False), stream=buf,
            cfg_path=cfg_path, multi_select=lambda *a, **k: set(reviewers),
            single_select=ss, input_fn=in_fn)
    return rc, buf.getvalue(), cfg_path


def test_confirm_lgc_on_installs_ready_for_ci(tmp_path):
    """The F4 wire-up: opting a repo into label-gated CI in the per-repo confirm runs
    the installer (a server-side PR for the ready-for-ci gate)."""
    run, calls = _confirm_run()
    rc, out, cfg_path = _drive_confirm(tmp_path, run, lgc_on=True)
    assert rc == 0
    assert config.label_gated_ci(config.load_config(cfg_path), REPO) is True
    # The installer opened a PR on its own branch, with the merge-me callout.
    assert any(c[:2] == ["gh", "pr"] for c in calls), "the ready-for-ci PR must be opened"
    assert "MERGE THIS PR" in out


def test_confirm_lgc_off_does_not_install(tmp_path):
    """Adversarial claim #2: provisioning fires only on a genuine opt-in — label-gated
    CI left OFF never touches the ready-for-ci workflow."""
    run, calls = _confirm_run()
    rc, _, cfg_path = _drive_confirm(tmp_path, run, lgc_on=False)
    assert rc == 0
    assert config.label_gated_ci(config.load_config(cfg_path), REPO) is False
    assert not any("tests-ready-for-ci.yml" in " ".join(c) for c in calls), \
        "no ready-for-ci provisioning when the opt-in is off"


def test_confirm_lgc_on_already_present_and_current_opens_no_pr(tmp_path):
    """Claim #2 (#4 probe): a re-run where the gate is already on the default branch AT
    THE CURRENT VERSION opts in but opens NO redundant second PR."""
    run, calls = _confirm_run(present=True, version=_SHIPPED_RFC)
    rc, out, _ = _drive_confirm(tmp_path, run, lgc_on=True)
    assert rc == 0
    assert not any(c[:2] == ["gh", "pr"] for c in calls)
    assert "already on the default branch" in out


# ── Reviewer provisioning DOES fire for the confirmed fleet (claim #2, other half) ──

def test_confirm_provisions_confirmed_reviewers(tmp_path):
    """The per-repo confirm provisions the fleet, not just records it: a confirmed
    Codex reviewer gets its app-install entry-point guidance (#5)."""
    run, _ = _confirm_run()
    rc, out, _ = _drive_confirm(tmp_path, run, lgc_on=False, reviewers={CODEX})
    assert rc == 0
    # F2/#5 guidance for the confirmed reviewer actually printed.
    assert "Connectors" in out and "GitHub" in out


# ── P7 #5 — the non-Claude reviewer entry-point hints ───────────────────────────────

def test_copilot_entry_point_hint_present(tmp_path):
    """P7 #5 — Copilot's path now tells the operator WHERE to enable Copilot review."""
    buf = io.StringIO()
    wizard.step_reviewers(
        REPO, str(tmp_path), {"gh_auth": True}, run=lambda *a, **k: _R(),
        spawn_command=lambda *a, **k: None, getpass_fn=lambda *a: "",
        pal=wizard._Palette(False), stream=buf, multi_select=lambda *a, **k: {COPILOT},
        input_fn=lambda *a: "")
    assert "Rules/Reviewers" in buf.getvalue()


def test_codex_and_gemini_entry_point_hints_present():
    """P7 #5 — Codex + Gemini entry-points (the lines already shipped; guard them)."""
    codex = " ".join(wizard._app_install_lines("codex", REPO))
    assert "Settings ▸ Connectors ▸ GitHub" in codex
    gemini = " ".join(wizard._app_install_lines("gemini", REPO))
    assert "gemini-code-assist" in gemini


# ── Adversarial claim #3 — no DUPLICATE Claude App guidance ─────────────────────────

def test_claude_app_guidance_not_duplicated(tmp_path):
    """Claim #3: F4 adds NO second Claude-App guide — the #5-shipped one fires once."""
    def run(argv, cwd=None, timeout=30, input=None):
        if argv[:2] == ["gh", "api"]:
            return _R(returncode=0, stdout="base64==")  # claude workflow present
        if argv[:3] == ["gh", "secret", "list"]:
            return _R(returncode=0, stdout="")
        return _R()
    buf = io.StringIO()
    with _tty(False):
        wizard.step_reviewers(
            REPO, str(tmp_path), {"gh_auth": False}, run=run,
            spawn_command=lambda *a, **k: None, getpass_fn=lambda *a: "",
            pal=wizard._Palette(False), stream=buf, multi_select=lambda *a, **k: {CLAUDE},
            input_fn=lambda *a: "")
    out = buf.getvalue()
    assert out.count("github.com/apps/claude") == 1, "Claude App guidance must appear exactly once"
