"""The per-repo test-command wizard step + its config readers.

``wizard.step_repo_test_command`` is the setup-UX twin of the reference wizard's
step of the same name (shared-step wording is word-for-word identical). It offers
the auto-detected CI command as a shown default, PRESERVES a persisted command on
a bare Enter, clears one on an explicit ``none``/``default``, and never prompts
off a TTY.

The value it returns is persisted to ``repos[<repo>].test_command`` and read back
by :func:`buddhi_review.config.repo_test_command` (explicit per-repo only) and
:func:`buddhi_review.config.test_command` (per-repo → global), which is what
:func:`buddhi_review.commit_push.resolve_test_command` consumes.
"""
import io

import pytest

from buddhi_review import config, wizard


REPO = "octocat/Hello-World"


def _drive(current, *, tty=True, entered="", cwd=None, detected=None, monkeypatch=None):
    """Run the step with an injected input_fn / TTY / detector. Returns
    ``(result, output)``."""
    buf = io.StringIO()
    monkeypatch.setattr(wizard, "_is_tty", lambda: tty)
    if detected is not None:
        monkeypatch.setattr(wizard, "_detect_ci_command", lambda _cwd: detected)
    prompts = []

    def input_fn(prompt=""):
        prompts.append(prompt)
        return entered

    result = wizard.step_repo_test_command(
        REPO, current, cwd, pal=wizard._Palette(False), stream=buf,
        input_fn=input_fn)
    return result, buf.getvalue(), prompts


# ── The step's contract ────────────────────────────────────────────────────────

def test_non_tty_never_prompts_and_keeps_the_persisted_value(monkeypatch):
    """Off a TTY the step returns the persisted value unchanged and asks nothing.

    MUTATION: delete the `if not _is_tty(): return current` branch and this fails
    (input_fn would be called).
    """
    def _boom(prompt=""):
        pytest.fail("a non-TTY step must not prompt")

    buf = io.StringIO()
    monkeypatch.setattr(wizard, "_is_tty", lambda: False)
    result = wizard.step_repo_test_command(
        REPO, "npx vitest run", None, pal=wizard._Palette(False), stream=buf,
        input_fn=_boom)
    assert result == "npx vitest run"


def test_non_tty_unset_repo_stays_unset(monkeypatch):
    monkeypatch.setattr(wizard, "_is_tty", lambda: False)
    result = wizard.step_repo_test_command(
        REPO, None, None, pal=wizard._Palette(False), stream=io.StringIO(),
        input_fn=lambda p="": pytest.fail("must not prompt"))
    assert result is None


def test_blank_preserves_an_existing_command(monkeypatch):
    """A bare Enter never wipes a configured command.

    MUTATION: change the final `return entered or current` to `return entered` and
    this fails.
    """
    result, _, prompts = _drive("npx vitest run", entered="", monkeypatch=monkeypatch)
    assert result == "npx vitest run"
    assert "[npx vitest run]" in prompts[0]      # the current value is the shown default


def test_blank_on_an_unset_repo_does_not_adopt_the_detection(monkeypatch):
    """The detected command is SHOWN, never silently adopted: accepting it requires
    typing it. A blank answer leaves the repo unset (the auto-detect default).

    MUTATION: `return entered or detected or current` and this fails — the repo
    would persist a command the user never chose.
    """
    result, _, prompts = _drive(None, entered="", detected="npm ci && npm test",
                                monkeypatch=monkeypatch)
    assert result is None
    assert "detected: npm ci && npm test" in prompts[0]


def test_typed_command_is_returned(monkeypatch):
    result, _, _ = _drive(None, entered="go test ./...", monkeypatch=monkeypatch)
    assert result == "go test ./..."


def test_typed_command_overrides_an_existing_one(monkeypatch):
    result, _, _ = _drive("npx vitest run", entered="go test ./...",
                          monkeypatch=monkeypatch)
    assert result == "go test ./..."


@pytest.mark.parametrize("word", ["none", "default", "NONE", "Default"])
def test_none_or_default_clears_an_existing_command(word, monkeypatch):
    """Typing `none`/`default` (case-insensitively) explicitly unsets the command.

    MUTATION: drop the `if entered.lower() in ("none", "default")` branch and this
    fails — "none" would be persisted as a literal command.
    """
    result, _, _ = _drive("npx vitest run", entered=word, monkeypatch=monkeypatch)
    assert result is None


def test_eof_is_treated_as_blank(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "_detect_ci_command", lambda _c: None)

    def _eof(prompt=""):
        raise EOFError

    result = wizard.step_repo_test_command(
        REPO, "go test ./...", None, pal=wizard._Palette(False), stream=buf,
        input_fn=_eof)
    assert result == "go test ./..."


def test_no_detection_prompt_names_the_pytest_default(monkeypatch):
    _, _, prompts = _drive(None, entered="", detected=None, monkeypatch=monkeypatch)
    assert "blank = pytest default" in prompts[0]


def test_panel_states_the_shell_operator_behaviour(monkeypatch):
    """The panel tells the operator that a `&&`/`|`/`;` command runs via bash -lc —
    the executor rule this step's value feeds. Shared-step wording (parity)."""
    _, out, _ = _drive(None, entered="", monkeypatch=monkeypatch)
    assert "Per-repo — Test command" in out
    assert "A command with && / | / ; runs via `bash -lc`." in out


# ── The config readers the step writes into ────────────────────────────────────

def test_repo_test_command_reads_only_the_explicit_per_repo_value():
    cfg = {"test_command": "go test ./...",
           "repos": {"octocat/hello-world": {"test_command": "npx vitest run"}}}
    assert config.repo_test_command(cfg, REPO) == "npx vitest run"
    # the global NEVER leaks into the per-repo reader (the wizard's shown default)
    assert config.repo_test_command(cfg, "other/repo") is None


@pytest.mark.parametrize("value", [None, "", "   "])
def test_repo_test_command_treats_blank_as_unset(value):
    cfg = {"repos": {"octocat/hello-world": {"test_command": value}}}
    assert config.repo_test_command(cfg, REPO) is None


def test_test_command_resolves_per_repo_then_global():
    cfg = {"test_command": "go test ./...",
           "repos": {"octocat/hello-world": {"test_command": "npx vitest run"}}}
    assert config.test_command(cfg, REPO) == "npx vitest run"
    assert config.test_command(cfg, "other/repo") == "go test ./..."
    assert config.test_command(cfg) == "go test ./..."


def test_test_command_blank_per_repo_falls_through_to_global():
    """Unlike label_gated_ci (key presence shadows), a BLANK per-repo test_command
    falls through to the global — a config predating the key is unchanged."""
    cfg = {"test_command": "go test ./...",
           "repos": {"octocat/hello-world": {"test_command": "  "}}}
    assert config.test_command(cfg, REPO) == "go test ./..."


def test_test_command_absent_everywhere_is_none():
    assert config.test_command({}, REPO) is None
    assert config.test_command({"repos": {"octocat/hello-world": {}}}, REPO) is None
