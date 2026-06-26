"""The setup wizard — step gating (free vs locked teaser) + config keys written."""
import io
import types
from pathlib import Path

import pytest

from buddhi_review import wizard

# Keys that must NEVER appear in a free config (they are paid surface).
_PAID_KEYS = (
    "telegram_bot_token", "telegram_chat_id", "github_account_plan",
    "github_billing_token", "dashboard_refresh_interval", "budget_throttle",
    "claude_credit_reserve", "github_review_minutes",
)
_FREE_KEYS = {"plan", "active_reviewers", "auto_on_open", "notifications", "repo", "cwd"}


# ── Pure helpers ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expect_ver,expect_ok", [
    ("gh version 2.90.0 (2026-01-01)", (2, 90), True),
    ("gh version 2.87.0 (2025-12-01)", (2, 87), True),
    ("gh version 2.86.9 (2025-11-01)", (2, 86), False),
    ("gh version 3.0.0", (3, 0), True),
    ("not a version", None, False),
])
def test_gh_version_ok(text, expect_ver, expect_ok):
    ver, ok = wizard.gh_version_ok(text)
    assert ver == expect_ver
    assert ok == expect_ok


def test_recommend_plan():
    assert wizard.recommend_plan({"opus": True, "sonnet": True}) == "max-5x"
    assert wizard.recommend_plan({"opus": False, "sonnet": True}) == "pro"
    assert wizard.recommend_plan({"opus": False, "sonnet": False}) == wizard.config.DEFAULT_PLAN


@pytest.mark.parametrize("url,expect", [
    ("git@github.com:acme/widgets.git", "acme/widgets"),
    ("https://github.com/acme/widgets.git", "acme/widgets"),
    ("https://github.com/acme/widgets", "acme/widgets"),
    ("ssh://git@github.com/acme/widgets.git", "acme/widgets"),
])
def test_infer_repo(url, expect):
    def run(argv, cwd=None, timeout=30, input=None):
        return types.SimpleNamespace(returncode=0, stdout=url + "\n")
    assert wizard.infer_repo(run) == expect


def test_build_config_only_free_keys():
    cfg = wizard.build_config("pro", "acme/widgets", "/tmp/x",
                              ["copilot", "claude"], {"copilot": True, "claude": False})
    assert set(cfg.keys()) <= _FREE_KEYS
    for k in _PAID_KEYS:
        assert k not in cfg
    assert cfg["plan"] == "pro"
    assert cfg["notifications"] == "console"
    assert cfg["active_reviewers"] == ["copilot", "claude"]
    assert cfg["auto_on_open"] == {"copilot": True, "claude": False}


def test_merge_preserving_keeps_unknown_keys():
    existing = {"repos": {"acme/widgets": {"active_reviewers": ["claude"]}}, "plan": "old"}
    new = {"plan": "max-5x", "notifications": "console"}
    merged = wizard.merge_preserving(existing, new)
    assert merged["repos"] == {"acme/widgets": {"active_reviewers": ["claude"]}}  # preserved
    assert merged["plan"] == "max-5x"  # overlaid


# ── Full run with injected seams ───────────────────────────────────────────────────

def _fake_run_factory(*, workflow_present, gh_authed=True):
    def fake_run(argv, cwd=None, timeout=30, input=None):
        R = types.SimpleNamespace
        if argv[:2] == ["gh", "--version"]:
            return R(returncode=0, stdout="gh version 2.90.0 (2026-01-01)")
        if argv[:3] == ["gh", "auth", "status"]:
            return R(returncode=0 if gh_authed else 1, stdout="")
        if argv[:2] == ["git", "-C"] and "remote" in argv:
            return R(returncode=0, stdout="git@github.com:acme/widgets.git\n")
        if argv[:2] == ["git", "-C"] and "rev-parse" in argv:
            return R(returncode=0, stdout="")  # cwd toplevel filled by tmp below
        if argv and (argv[0] == "claude" or str(argv[0]).endswith("claude")):
            return R(returncode=0, stdout="pong")
        if argv[:2] == ["gh", "api"]:
            return R(returncode=0 if workflow_present else 1,
                     stdout="base64==" if workflow_present else "")
        if argv[:3] == ["gh", "secret", "list"]:
            return R(returncode=0, stdout="")
        return R(returncode=0, stdout="")
    return fake_run


def _drive(monkeypatch, tmp_path, *, workflow_present, suppress_upsell=False, answers="default"):
    monkeypatch.delenv("BUDDHI_NO_UPSELL", raising=False)
    if suppress_upsell:
        monkeypatch.setenv("BUDDHI_NO_UPSELL", "1")
    cfg_path = tmp_path / "config.yaml"
    buf = io.StringIO()
    run = _fake_run_factory(workflow_present=workflow_present)

    # cwd toplevel → the temp dir, so the workflow write lands there.
    def run_with_cwd(argv, cwd=None, timeout=30, input=None):
        if argv[:2] == ["git", "-C"] and "rev-parse" in argv and "--show-toplevel" in argv:
            return types.SimpleNamespace(returncode=0, stdout=str(tmp_path) + "\n")
        return run(argv, cwd=cwd, timeout=timeout, input=input)

    def ss(prompt, options, *, preselect=0, **kw):
        return preselect

    def ms(prompt, options, *, preselected=None, **kw):
        return set(range(len(options)))  # all four reviewers

    rc = wizard.run(
        config_path=cfg_path, run=run_with_cwd, which=lambda x: "/bin/claude" if x == "claude" else None,
        single_select=ss, multi_select=ms, getpass_fn=lambda *a: "",
        spawn_command=lambda *a, **k: {"spawned": False}, input_fn=lambda *a: "",
        stream=buf)
    return rc, buf.getvalue(), cfg_path


def _load_yaml(path):
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_run_writes_only_free_keys(monkeypatch, tmp_path):
    rc, out, cfg_path = _drive(monkeypatch, tmp_path, workflow_present=True)
    assert rc == 0
    cfg = _load_yaml(cfg_path)
    assert set(cfg.keys()) <= _FREE_KEYS
    for k in _PAID_KEYS:
        assert k not in cfg
    assert cfg["notifications"] == "console"
    assert cfg["active_reviewers"] == list(wizard._REVIEWERS)
    # Pin all four spec-required auto_on_open defaults (claude summoned, the three
    # GitHub-App bots auto-review) so a default flip is caught.
    assert cfg["auto_on_open"] == {"copilot": True, "gemini": True, "codex": True, "claude": False}
    assert cfg["repo"] == "acme/widgets"


def test_run_renders_locked_teasers(monkeypatch, tmp_path):
    _, out, _ = _drive(monkeypatch, tmp_path, workflow_present=True)
    # Steps 4 & 6 render the locked budget + monitoring nudges (single-benefit).
    assert wizard._BUDGETS_TEASER in out
    assert wizard._MONITORING_TEASER in out
    assert "🔒" in out


def test_locked_teasers_suppressed_by_env(monkeypatch, tmp_path):
    _, out, _ = _drive(monkeypatch, tmp_path, workflow_present=True, suppress_upsell=True)
    assert wizard._BUDGETS_TEASER not in out
    assert wizard._MONITORING_TEASER not in out


def test_pro_soon_teaser_at_done(monkeypatch):
    """The 'Pro coming soon' nudge renders at the done step and is suppressible."""
    monkeypatch.delenv("BUDDHI_NO_UPSELL", raising=False)
    pal = wizard._Palette(False)
    buf = io.StringIO()
    wizard.step_done(Path("/tmp/x.yaml"), pal=pal, stream=buf)
    assert wizard._PRO_SOON_TEASER in buf.getvalue()
    monkeypatch.setenv("BUDDHI_NO_UPSELL", "1")
    buf2 = io.StringIO()
    wizard.step_done(Path("/tmp/x.yaml"), pal=pal, stream=buf2)
    assert wizard._PRO_SOON_TEASER not in buf2.getvalue()


def test_claude_workflow_written_when_absent(monkeypatch, tmp_path):
    rc, out, _ = _drive(monkeypatch, tmp_path, workflow_present=False)
    assert rc == 0
    dest = tmp_path / ".github" / "workflows" / "claude-code-review.yml"
    assert dest.exists(), "the bundled workflow template should be written when absent"
    # The load-bearing sentinel survives the copy.
    assert "No issues found." in dest.read_text(encoding="utf-8")
    # The one-line static Actions note is shown (no live usage read).
    assert "Actions minutes" in out


def test_set_claude_secret_non_tty_defers(monkeypatch, tmp_path):
    # Non-TTY: never spawn a token window; emit a deferred note instead.
    monkeypatch.setattr(wizard, "_is_tty", lambda: False)
    pal = wizard._Palette(False)
    buf = io.StringIO()

    def run(argv, cwd=None, timeout=30, input=None):
        if argv[:3] == ["gh", "secret", "list"]:
            return types.SimpleNamespace(returncode=0, stdout="")
        return types.SimpleNamespace(returncode=0, stdout="")

    status = wizard._set_claude_secret("acme/widgets", run=run, spawn_command=lambda *a, **k: None,
                                       getpass_fn=lambda *a: "", pal=pal, stream=buf)
    assert status == "deferred"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in buf.getvalue()


def test_claude_secret_checked_even_when_workflow_present(monkeypatch, tmp_path):
    """A repo with the workflow committed but no CLAUDE_CODE_OAUTH_TOKEN is still
    non-functional for Claude review — the secret check must run regardless of
    workflow presence (build-spec §8 step 5 / reviewer-setup.md)."""
    monkeypatch.setattr(wizard, "_is_tty", lambda: False)  # defers after the list check
    calls = []

    def run(argv, cwd=None, timeout=30, input=None):
        calls.append(list(argv))
        R = types.SimpleNamespace
        if argv[:2] == ["gh", "api"]:
            return R(returncode=0, stdout="base64==")  # workflow PRESENT
        if argv[:3] == ["gh", "secret", "list"]:
            return R(returncode=0, stdout="")  # token MISSING
        return R(returncode=0, stdout="")

    pal = wizard._Palette(False)
    buf = io.StringIO()
    enabled, _ = wizard.step_reviewers(
        "acme/widgets", str(tmp_path), {"gh_auth": True}, run=run,
        spawn_command=lambda *a, **k: None, getpass_fn=lambda *a: "",
        pal=pal, stream=buf, multi_select=lambda *a, **k: {3}, input_fn=lambda *a: "")
    assert enabled == ["claude"]
    assert any(c[:3] == ["gh", "secret", "list"] for c in calls), \
        "the secret check must run even when the Claude workflow is already present"


def test_no_paid_surface_strings_in_wizard_source():
    """OSS purity: the wizard names no paid mechanism. The locked teasers cite a
    benefit (e.g. 'Mobile push'), never the paid product or channel by name."""
    text = Path(wizard.__file__).read_text(encoding="utf-8")
    for forbidden in ("Telegram", "Autopilot", "auto-rebase", "self-heal", "force-push", "Cockpit"):
        assert forbidden not in text, f"{forbidden} leaked into the free wizard source"
