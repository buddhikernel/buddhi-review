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


# ── _installed_ci_command — workflow-level context guards ───────────────────────────

def _wf(body: str) -> str:
    """Wrap a YAML body in a minimal workflow skeleton."""
    return f"name: ci\non: [push]\n{body}"


def test_installed_ci_command_basic_extraction():
    text = _wf("jobs:\n  ci:\n    runs-on: ubuntu-latest\n    steps:\n"
               "      - uses: actions/checkout@v4\n"
               "      - run: python -m pytest\n")
    assert wizard._installed_ci_command(text) == "python -m pytest"


def test_installed_ci_command_workflow_level_env_returns_none():
    """A top-level ``env:`` block applies to every step; the stock template cannot
    carry it, so the command must be treated as unextractable."""
    text = _wf("env:\n  DB_URL: postgres://localhost/test\n"
               "jobs:\n  ci:\n    runs-on: ubuntu-latest\n    steps:\n"
               "      - uses: actions/checkout@v4\n"
               "      - run: python -m pytest\n")
    assert wizard._installed_ci_command(text) is None


def test_installed_ci_command_workflow_level_defaults_run_wd_returns_none():
    """A workflow-level ``defaults.run.working-directory`` silently changes every
    run step's directory; baking the command without it would run in the wrong dir."""
    text = _wf("defaults:\n  run:\n    working-directory: src\n"
               "jobs:\n  ci:\n    runs-on: ubuntu-latest\n    steps:\n"
               "      - uses: actions/checkout@v4\n"
               "      - run: python -m pytest\n")
    assert wizard._installed_ci_command(text) is None


def test_installed_ci_command_workflow_level_defaults_run_shell_returns_none():
    """A workflow-level ``defaults.run.shell`` changes the shell for every run step."""
    text = _wf("defaults:\n  run:\n    shell: bash\n"
               "jobs:\n  ci:\n    runs-on: ubuntu-latest\n    steps:\n"
               "      - uses: actions/checkout@v4\n"
               "      - run: python -m pytest\n")
    assert wizard._installed_ci_command(text) is None


def test_installed_ci_command_job_level_env_still_returns_none():
    """Job-level env guard must still fire even when workflow-level env is absent."""
    text = _wf("jobs:\n  ci:\n    runs-on: ubuntu-latest\n"
               "    env:\n      PYTHONPATH: src\n    steps:\n"
               "      - uses: actions/checkout@v4\n"
               "      - run: python -m pytest\n")
    assert wizard._installed_ci_command(text) is None


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


# ═════════════════════════════════════════════════════════════════════════════
# R2 — the update path PRESERVES the installed gate's CI command, and the
# Python detection is `.[test]`-aware.
#
# Live incident (2026-07-01): the versioned-update offer re-baked the generic
# detected command (`pip install -e .`) over a hand-wired
# `pip install -e '.[test]' …` gate — every gated run then failed in seconds on
# `No module named pytest`, and the gate's extra scan step silently vanished.
# ═════════════════════════════════════════════════════════════════════════════

# The live incident's shape: a real, customised, single-run-line gate (legacy
# unmarked, so the version check offers an update).
_CUSTOM_CMD = ("pip install -e '.[test]' build setuptools wheel && "
               "python -m pytest -q && python tools/publish_gate.py scan")
_CUSTOM_GATE = f"""\
name: Ready-for-CI
on:
  pull_request:
    types: [labeled, synchronize]
jobs:
  ci:
    if: contains(github.event.pull_request.labels.*.name, 'ready-for-ci')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: CI
        run: |
          {_CUSTOM_CMD}
"""


def _gate(steps_yaml: str, job: str = "ci") -> str:
    return f"jobs:\n  {job}:\n    runs-on: ubuntu-latest\n    steps:\n{steps_yaml}"


# ── _installed_ci_command — extraction off the fetched installed text ────────────────

def test_extract_verbatim_single_run_line():
    assert wizard._installed_ci_command(_CUSTOM_GATE) == _CUSTOM_CMD


def test_extract_multi_step_bails_on_step_isolation():
    """Multi-step gates cannot be safely joined: each `run:` step in GitHub
    Actions is a new shell, so `cd`/`export` in step N do NOT persist to
    step N+1. Joining as `&&` would produce different (wrong) shell state."""
    text = _gate("      - uses: actions/checkout@v4\n"
                 "      - name: Install\n"
                 "        run: pip install -e '.[test]'\n"
                 "      - name: Test\n"
                 "        run: python -m pytest -q\n"
                 "      - name: Gate\n"
                 "        run: python tools/publish_gate.py scan\n")
    assert wizard._installed_ci_command(text) is None


def test_extract_multiline_block_joins_lines_and_drops_comments():
    text = _gate("      - name: CI\n"
                 "        run: |\n"
                 "          # install first\n"
                 "          pip install -e '.[test]'\n"
                 "\n"
                 "          python -m pytest -q\n")
    assert wizard._installed_ci_command(text) == (
        "pip install -e '.[test]' && python -m pytest -q")


def test_extract_no_doubled_operator_when_lines_already_chain():
    text = _gate("      - name: CI\n"
                 "        run: |\n"
                 "          pip install -e . &&\n"
                 "          python -m pytest\n")
    assert wizard._installed_ci_command(text) == "pip install -e . && python -m pytest"


def test_extract_folds_backslash_continuations():
    text = _gate("      - name: CI\n"
                 "        run: |\n"
                 "          pip install -e . \\\n"
                 "            --no-build-isolation\n"
                 "          python -m pytest\n")
    assert wizard._installed_ci_command(text) == (
        "pip install -e . --no-build-isolation && python -m pytest")


def test_extract_bails_on_trailing_comment_before_more_commands():
    """`pip install x  # editable` joined before `pytest` would comment pytest
    OUT — a vacuously green gate. Unextractable, never a weaker chain."""
    text = _gate("      - name: CI\n"
                 "        run: |\n"
                 "          pip install -e .  # editable install\n"
                 "          python -m pytest\n")
    assert wizard._installed_ci_command(text) is None
    # …but a trailing comment on the LAST line is harmless and preserved.
    solo = _gate("      - name: CI\n"
                 "        run: |\n"
                 "          python -m pytest  # the whole suite\n")
    assert wizard._installed_ci_command(solo) == "python -m pytest  # the whole suite"
    # …and a glued '#' (pip URL fragment) is not a comment at all.
    frag = _gate("      - name: CI\n"
                 "        run: |\n"
                 "          pip install git+https://e.io/r.git#egg=x\n"
                 "          python -m pytest\n")
    assert wizard._installed_ci_command(frag) == (
        "pip install git+https://e.io/r.git#egg=x && python -m pytest")


def test_extract_single_line_round_trips_verbatim_without_join_rules():
    """One line = no transformation = always safe to preserve, even when it
    contains syntax the JOIN rules would refuse (a here-string's `<<<`)."""
    text = _gate("      - name: CI\n"
                 '        run: python check.py <<< "$CFG"\n')
    assert wizard._installed_ci_command(text) == 'python check.py <<< "$CFG"'


def test_extract_bails_when_a_step_dangles_a_continuation_before_another():
    text = _gate("      - name: Install\n"
                 "        run: pip install -e . \\\n"
                 "      - name: Test\n"
                 "        run: python -m pytest\n")
    assert wizard._installed_ci_command(text) is None


def test_extract_background_ampersand_joins_without_doubling():
    text = _gate("      - name: CI\n"
                 "        run: |\n"
                 "          server --port 1234 &\n"
                 "          python -m pytest\n")
    assert wizard._installed_ci_command(text) == (
        "server --port 1234 & python -m pytest")


def test_extract_bails_on_function_block_opener():
    text = _gate("      - name: CI\n"
                 "        run: |\n"
                 "          run_all() {\n"
                 "            pytest\n"
                 "          }\n"
                 "          run_all\n")
    assert wizard._installed_ci_command(text) is None


def test_extract_bails_on_shell_control_flow_and_heredocs():
    loop_text = _gate("      - name: CI\n"
                      "        run: |\n"
                      "          if [ -f Makefile ]; then\n"
                      "            make ci\n"
                      "          fi\n")
    assert wizard._installed_ci_command(loop_text) is None
    heredoc = _gate("      - name: CI\n"
                    "        run: |\n"
                    "          cat > cfg <<EOT\n"
                    "          x=1\n"
                    "          EOT\n")
    assert wizard._installed_ci_command(heredoc) is None


def test_extract_bails_on_control_flow_in_any_line_position():
    """Adversarial (verify panel): keywords NOT at end-of-line must bail too —
    `… && fi` / `&& then pytest` are syntax errors, never a bakeable chain."""
    opener_with_cmd = _gate("      - name: CI\n"
                            "        run: |\n"
                            "          if true; then python -m pytest -q\n"
                            "          fi\n")
    assert wizard._installed_ci_command(opener_with_cmd) is None
    keyword_at_start = _gate("      - name: CI\n"
                             "        run: |\n"
                             "          if test -f x\n"
                             "          then pytest\n"
                             "          else echo no\n"
                             "          fi\n")
    assert wizard._installed_ci_command(keyword_at_start) is None
    # The multi-STEP shape: opener and closer as separate single-line steps —
    # the single-line verbatim shortcut must not smuggle them past the check.
    split_steps = _gate("      - name: Open\n"
                        "        run: 'if [ -f Makefile ]; then make ci'\n"
                        "      - name: Close\n"
                        "        run: fi\n")
    assert wizard._installed_ci_command(split_steps) is None
    # …while a COMPLETE one-liner block in a single step stays preserved
    # verbatim (no join happens, nothing can break).
    one_liner = _gate("      - name: CI\n"
                      "        run: 'if [ -f Makefile ]; then make ci; fi'\n")
    assert wizard._installed_ci_command(one_liner) == (
        "if [ -f Makefile ]; then make ci; fi")


def test_extract_double_backslash_is_a_literal_not_a_continuation():
    """Adversarial (verify panel, round 2): `echo built \\\\` ends in an ESCAPED
    backslash — the line is complete. Folding `false` in as echo arguments
    would make the gate vacuously green; it must be `&&`-joined instead."""
    text = _gate("      - name: CI\n"
                 "        run: |\n"
                 "          echo built \\\\\n"
                 "          false\n")
    assert wizard._installed_ci_command(text) == "echo built \\\\ && false"
    glued = _gate("      - name: CI\n"
                  "        run: |\n"
                  "          echo tag=v1\\\\\n"
                  "          false\n")
    assert wizard._installed_ci_command(glued) == "echo tag=v1\\\\ && false"
    # …and a dropped comment after an ESCAPED backslash is harmless (the line
    # was complete), while after a real continuation it still bails.
    escaped_then_comment = _gate("      - name: CI\n"
                                 "        run: |\n"
                                 "          echo built \\\\\n"
                                 "          # note\n"
                                 "          false\n")
    assert wizard._installed_ci_command(escaped_then_comment) == (
        "echo built \\\\ && false")


def test_extract_bails_on_backslash_then_trailing_whitespace():
    """Adversarial (verify panel, round 3): `echo built \\ ` (trailing SPACE
    after the backslash) is a COMPLETE line in bash — the backslash escapes the
    space, not the newline. Stripping erases that distinction, so folding would
    glue `false` in as echo arguments (vacuously green). Unextractable."""
    space = _gate("      - name: CI\n"
                  "        run: |\n"
                  "          echo built \\ \n"
                  "          false\n")
    assert wizard._installed_ci_command(space) is None
    tab = _gate("      - name: CI\n"
                "        run: |\n"
                "          echo built \\\t\n"
                "          false\n")
    assert wizard._installed_ci_command(tab) is None


def test_extract_never_raises_on_pathological_yaml():
    """Adversarial (verify panel, round 3): PyYAML raises RecursionError (not a
    YAMLError) on deep nesting — the best-effort reader must return None, never
    kill the wizard with a traceback."""
    assert wizard._installed_ci_command("jobs: " + "[" * 20000 + "]" * 20000) is None


def test_extract_bails_on_operator_glued_comments():
    """Adversarial (verify panel, round 2): bash starts a comment at any WORD
    start — `echo x;# note` is commented after the `;` — so joining `&& false`
    after it would be swallowed. Word-glued `#` (pip's `#egg=`) stays fine."""
    semi = _gate("      - name: CI\n"
                 "        run: |\n"
                 "          echo x;# editable install note\n"
                 "          false\n")
    assert wizard._installed_ci_command(semi) is None
    amp = _gate("      - name: CI\n"
                "        run: |\n"
                "          echo x &#background note\n"
                "          false\n")
    assert wizard._installed_ci_command(amp) is None
    cross_step = _gate("      - name: A\n"
                       "        run: 'echo x;# note'\n"
                       "      - name: B\n"
                       "        run: 'python -m pytest'\n")
    assert wizard._installed_ci_command(cross_step) is None


def test_extract_bails_on_dropped_line_after_backslash_continuation():
    """Adversarial (verify panel): `echo running gate \\` + a comment + `exit 1`
    must NOT extract as the always-green `echo running gate exit 1` — the shell
    binds the continuation to the COMMENT line, not to `exit 1`."""
    comment_gap = _gate("      - name: CI\n"
                        "        run: |\n"
                        "          echo running gate \\\n"
                        "          # note to self\n"
                        "          exit 1\n")
    assert wizard._installed_ci_command(comment_gap) is None
    blank_gap = _gate("      - name: CI\n"
                      "        run: |\n"
                      "          echo running gate \\\n"
                      "\n"
                      "          exit 1\n")
    assert wizard._installed_ci_command(blank_gap) is None


def test_extract_none_for_placeholder_marker_only():
    """A never-baked copy (the marker still in the run slot) has nothing to
    preserve — extraction must say so, not return the literal marker."""
    text = _gate("      - name: CI\n"
                 "        run: |\n"
                 f"          {wizard._CI_COMMAND_MARKER}\n")
    assert wizard._installed_ci_command(text) is None
    # The bundled template itself is exactly this shape.
    assert wizard._installed_ci_command(
        _template_path().read_text(encoding="utf-8")) is None


def test_extract_none_for_unparseable_or_shapeless_yaml():
    assert wizard._installed_ci_command(None) is None
    assert wizard._installed_ci_command("") is None
    assert wizard._installed_ci_command("jobs:\n  ci: [unclosed") is None  # YAML error
    assert wizard._installed_ci_command("- just\n- a list\n") is None      # non-mapping
    assert wizard._installed_ci_command("name: x\n") is None               # no jobs
    assert wizard._installed_ci_command("jobs: {}\n") is None              # empty jobs
    # steps present but uses:-only (no run anywhere)
    assert wizard._installed_ci_command(
        _gate("      - uses: actions/checkout@v4\n")) is None
    # a non-string run value (YAML `run: 42` / `run: false` parse as scalars the
    # Actions runner would still execute as text) → unextractable, never a
    # silently shortened chain, and never a crash
    assert wizard._installed_ci_command(
        _gate("      - name: CI\n        run: 42\n")) is None
    assert wizard._installed_ci_command(
        _gate("      - name: A\n        run: 'echo ok'\n"
              "      - name: B\n        run: false\n")) is None


def test_extract_none_for_context_dependent_steps():
    """Steps or jobs with working-directory / shell / env context fields must be
    rejected — we cannot carry that context into the stock template's single slot."""
    # Step-level working-directory
    wd_step = _gate("      - name: CI\n"
                    "        run: npm test\n"
                    "        working-directory: frontend\n")
    assert wizard._installed_ci_command(wd_step) is None

    # Step-level shell override
    shell_step = _gate("      - name: CI\n"
                       "        run: make ci\n"
                       "        shell: bash\n")
    assert wizard._installed_ci_command(shell_step) is None

    # Step-level env
    env_step = _gate("      - name: CI\n"
                     "        run: npm test\n"
                     "        env:\n"
                     "          NODE_ENV: test\n")
    assert wizard._installed_ci_command(env_step) is None

    # Job-level defaults.run.working-directory
    wd_defaults = (
        "on: [push]\n"
        "jobs:\n"
        "  ci:\n"
        "    runs-on: ubuntu-latest\n"
        "    defaults:\n"
        "      run:\n"
        "        working-directory: frontend\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - run: npm test\n"
    )
    assert wizard._installed_ci_command(wd_defaults) is None

    # Job-level defaults.run.shell
    shell_defaults = (
        "on: [push]\n"
        "jobs:\n"
        "  ci:\n"
        "    runs-on: ubuntu-latest\n"
        "    defaults:\n"
        "      run:\n"
        "        shell: bash\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - run: make ci\n"
    )
    assert wizard._installed_ci_command(shell_defaults) is None

    # Job-level env
    job_env = (
        "on: [push]\n"
        "jobs:\n"
        "  ci:\n"
        "    runs-on: ubuntu-latest\n"
        "    env:\n"
        "      DB_URL: postgres://localhost/test\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - run: make test\n"
    )
    assert wizard._installed_ci_command(job_env) is None


def test_extract_prefers_ci_job_and_tolerates_renamed_single_job():
    renamed = _gate("      - name: CI\n        run: make verify\n", job="tests")
    assert wizard._installed_ci_command(renamed) == "make verify"
    two_jobs = (renamed + "  extra:\n    runs-on: ubuntu-latest\n    steps:\n"
                          "      - name: Other\n        run: make other\n")
    # several jobs, none named `ci` → ambiguous → not extractable
    assert wizard._installed_ci_command(two_jobs) is None
    with_ci = (_gate("      - name: CI\n        run: make ci-cmd\n")
               + "  lint:\n    runs-on: ubuntu-latest\n    steps:\n"
                 "      - name: Lint\n        run: make lint\n")
    assert wizard._installed_ci_command(with_ci) == "make ci-cmd"


# ── the update flow preserves the installed command ──────────────────────────────────

def _content_router(installed_yaml, *, default="main", put_rc=0, pr_rc=0,
                    pr_url="https://github.com/octocat/Hello-World/pull/9"):
    """_install_router, but the installed gate's CONTENT is caller-provided."""
    _b64 = base64.b64encode(installed_yaml.encode()).decode()

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
                return _R(returncode=0, stdout=_b64 + "\n")
            if "/git/ref/heads/" in joined and "--jq" in argv:
                return _R(returncode=0, stdout="deadbeef\n")
            if argv[2].endswith("/git/refs"):
                return _R(returncode=0)
        return _R()
    return router


def _offer_recording(tmp_path, *, is_tty, router, input_answers=None):
    """Like _offer, but records every prompt input_fn saw (to pin the default)."""
    (tmp_path / "Makefile").write_text("ci:\n\techo hi\n", encoding="utf-8")
    run, calls = _recorder(router)
    buf = io.StringIO()
    prompts = []

    def in_fn(prompt=""):
        prompts.append(prompt)
        for k, v in (input_answers or {}).items():
            if k in prompt:
                return v
        return ""
    with _tty(is_tty):
        result = wizard._offer_install_ready_for_ci(
            REPO, str(tmp_path), run=run, pal=wizard._Palette(False), stream=buf,
            input_fn=in_fn)
    return result, buf.getvalue(), calls, prompts


def _baked_put_content(calls):
    put = next(c["argv"] for c in calls if "-X" in c["argv"] and "PUT" in c["argv"])
    return base64.b64decode(
        next(t for t in put if t.startswith("content=")).split("content=", 1)[1]).decode()


def test_update_tty_defaults_to_the_installed_command_not_detection(tmp_path):
    """The regression the incident exposed: a Makefile in the checkout means
    detection says `make ci` — but the update's question must default to the
    command the INSTALLED gate actually runs, and a blank accept bakes THAT."""
    result, out, calls, prompts = _offer_recording(
        tmp_path, is_tty=True, router=_content_router(_CUSTOM_GATE),
        input_answers={"Install the label-gated CI workflow": "y"})
    assert result == "pr"
    cmd_prompt = next(p for p in prompts if "Command this gate runs" in p)
    assert f"[{_CUSTOM_CMD}]" in cmd_prompt, "the default must be the installed command"
    baked = _baked_put_content(calls)
    assert f"          {_CUSTOM_CMD}" in baked
    assert "          make ci\n" not in baked, "detection must not clobber the gate"
    assert wizard._CI_COMMAND_MARKER not in baked


def test_update_tty_entered_command_still_wins(tmp_path):
    result, _, calls, _ = _offer_recording(
        tmp_path, is_tty=True, router=_content_router(_CUSTOM_GATE),
        input_answers={"Install the label-gated CI workflow": "y",
                       "Command this gate runs at merge": "make verify"})
    assert result == "pr"
    assert "          make verify" in _baked_put_content(calls)


def test_update_tty_placeholder_only_falls_back_to_detection(tmp_path):
    """A never-baked installed copy has nothing to preserve — the question falls
    back to the detected default exactly as before."""
    never_baked = _gate("      - name: CI\n"
                        "        run: |\n"
                        f"          {wizard._CI_COMMAND_MARKER}\n")
    result, _, calls, prompts = _offer_recording(
        tmp_path, is_tty=True, router=_content_router(never_baked),
        input_answers={"Install the label-gated CI workflow": "y"})
    assert result == "pr"
    cmd_prompt = next(p for p in prompts if "Command this gate runs" in p)
    assert "[make ci]" in cmd_prompt
    assert "          make ci" in _baked_put_content(calls)


def test_update_non_tty_preserves_the_installed_command_verbatim(tmp_path):
    result, out, calls, _ = _offer_recording(
        tmp_path, is_tty=False, router=_content_router(_CUSTOM_GATE))
    assert result == "pr"
    assert "preserving the installed gate's CI command" in out
    assert "auto-detected CI command" not in out, "never the generic re-bake on update"
    assert f"          {_CUSTOM_CMD}" in _baked_put_content(calls)


def test_update_non_tty_unextractable_warns_and_skips(tmp_path):
    """Off-TTY with nothing extractable the update must NOT re-bake blind — it
    skips with a warn row and opens no PR (detection was available: Makefile)."""
    result, out, calls, _ = _offer_recording(
        tmp_path, is_tty=False, router=_content_router("name: legacy ci\n"))
    assert result is None
    assert "couldn't be read" in out and "terminal" in out
    assert not any("PUT" in c["argv"] for c in calls if "-X" in c["argv"])
    assert not any(c["argv"][:2] == ["gh", "pr"] for c in calls)


def test_fresh_install_flow_unchanged_by_r2(tmp_path):
    """A fresh INSTALL (gate absent) keeps the detection-driven flow: the TTY
    default is the detected command and non-TTY wires it with the same wording."""
    result, _, _, prompts = _offer_recording(
        tmp_path, is_tty=True, router=_install_router(),
        input_answers={"Install the label-gated CI workflow": "y"})
    assert result == "pr"
    assert "[make ci]" in next(p for p in prompts if "Command this gate runs" in p)
    result2, out2, _, _ = _offer_recording(tmp_path, is_tty=False,
                                           router=_install_router())
    assert result2 == "pr"
    assert "auto-detected CI command" in out2


# ── `.[test]`-aware Python detection ─────────────────────────────────────────────────

def test_detect_python_installs_declared_test_extra(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='x'\n\n[project.optional-dependencies]\n"
        'test = ["pytest>=7"]\n', encoding="utf-8")
    (tmp_path / "tests").mkdir()
    assert wizard._detect_ci_command(str(tmp_path)) == (
        "python -m pip install -e '.[test]' && python -m pytest")


def test_detect_python_without_test_extra_unchanged(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='x'\n\n[project.optional-dependencies]\n"
        'dev = ["ruff"]\n', encoding="utf-8")
    (tmp_path / "tests").mkdir()
    assert wizard._detect_ci_command(str(tmp_path)) == (
        "python -m pip install -e . && python -m pytest")


def test_pyproject_test_extra_scoped_to_its_section():
    # a `test =` key under ANOTHER table must not count
    assert wizard._pyproject_test_extra(
        "[tool.other]\ntest = ['x']\n") is False
    # quoted key + spaced table header both count
    assert wizard._pyproject_test_extra(
        '[ project.optional-dependencies ]\n"test" = ["pytest"]\n') is True
    assert wizard._pyproject_test_extra("") is False
