"""The setup wizard — step gating (free vs locked teaser) + config keys written."""
import io
import types
from pathlib import Path

import pytest

from buddhi_review import wizard
from conftest import _yn_bridge


# Keys that must NEVER appear in a free config (they are paid surface).
_PAID_KEYS = (
    "telegram_bot_token", "telegram_chat_id", "github_account_plan",
    "github_billing_token", "dashboard_refresh_interval", "budget_throttle",
    "claude_credit_reserve", "github_review_minutes",
)
_FREE_KEYS = {"plan", "active_reviewers", "auto_on_open", "notifications", "repo", "cwd",
              "repos"}


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
    # F1 fail-closed install gate: _drive runs NON-interactively (no TTY), so the
    # wizard cannot obtain an explicit "this reviewer is installed" confirmation for
    # ANY bot — every reviewer is dropped from both the fleet and auto_on_open. The
    # key regression this pins: no app bot is ever recorded with auto_on_open=True
    # from a non-interactive run (which would make the loop merge a zero-review PR).
    assert cfg["active_reviewers"] == []
    assert cfg["auto_on_open"] == {}
    assert cfg["repo"] == "acme/widgets"


def test_full_run_confirms_the_bound_repo(monkeypatch, tmp_path):
    """The full wizard records repos[<bound repo>] (presence == confirmed) in
    ADDITION to the top-level global default, so the per-repo gate (F5/F6) sees the
    repo as confirmed. _drive's injected single_select returns the preselect, so the
    per-repo auto-merge + label-gated-CI default to off."""
    from buddhi_review import config
    rc, _, cfg_path = _drive(monkeypatch, tmp_path, workflow_present=True)
    assert rc == 0
    cfg = _load_yaml(cfg_path)
    # The bound repo (inferred as acme/widgets) has a confirmed per-repo entry — its
    # presence marks the repo confirmed even though the non-interactive run enabled
    # NO reviewers (the F1 fail-closed gate has no TTY to confirm any install).
    assert config.repo_entry(cfg, "acme/widgets") is not None
    assert config.active_reviewers(cfg, "acme/widgets") == ()
    # Off-by-default per-repo settings round-trip through the F1 readers.
    assert config.repo_entry(cfg, "acme/widgets")["auto_merge"] is False
    assert config.label_gated_ci(cfg, "acme/widgets") is False
    # The top-level global default still exists alongside the per-repo entry.
    assert config.has_global_default(cfg) is True


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
    workflow presence (build-spec §8 step 5 / reviewer-setup.md). The secret walk
    runs BEFORE the F1 install gate, so it still fires even though this
    non-interactive run then drops Claude for lack of a TTY to confirm its App."""
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
    assert enabled == []  # fail-closed: no TTY to confirm the Claude App install
    assert any(c[:3] == ["gh", "secret", "list"] for c in calls), \
        "the secret check must run even when the Claude workflow is already present"


# ── the pasted-secret TTY reader (getpass truncates + FLUSHES a wrapped paste) ────


def _char_stream(text):
    """A (read_char, more_pending) pair over `text`. more_pending() is True while any
    unread char remains — so a newline that ISN'T the last char is a WRAP and the final
    newline SUBMITS, mirroring how a paste burst sits in the tty buffer."""
    i = {"n": 0}

    def read_char():
        if i["n"] >= len(text):
            return ""
        ch = text[i["n"]]
        i["n"] += 1
        return ch

    return read_char, (lambda: i["n"] < len(text))


def test_assemble_pasted_secret_rejoins_wrapped_multiline():
    read_char, more = _char_stream("sk-ant-oat01-A\n part-B\n")
    assert wizard._assemble_pasted_secret(read_char, more) == "sk-ant-oat01-Apart-B"


def test_assemble_pasted_secret_single_line_submits_on_enter():
    read_char, more = _char_stream("sk-ant-oat01-solo\n")
    assert wizard._assemble_pasted_secret(read_char, more) == "sk-ant-oat01-solo"


def test_assemble_pasted_secret_masks_each_captured_char():
    read_char, more = _char_stream("abc\n")
    masked = []
    out = wizard._assemble_pasted_secret(read_char, more, echo=masked.append)
    assert out == "abc" and masked == ["a", "b", "c"]


def test_read_hidden_tty_returns_none_without_tty(monkeypatch):
    monkeypatch.setattr(wizard, "_is_tty", lambda: False)
    assert wizard._read_hidden_tty("p: ") is None


def test_read_pasted_secret_uses_tty_reader_when_present(monkeypatch):
    """On a TTY the value comes from the self-managed reader — getpass is NEVER called
    (it truncates at the first line AND flushes the wrapped rest)."""
    monkeypatch.setattr(wizard, "_read_hidden_tty", lambda prompt: "sk-ant-oat01-full")

    def _boom(_p):
        raise AssertionError("getpass must not be called on a TTY")

    assert wizard._read_pasted_secret("p: ", _boom) == "sk-ant-oat01-full"


def test_read_pasted_secret_falls_back_to_getpass_off_tty(monkeypatch):
    """Off a TTY the reader returns None → getpass_fn is used, whitespace stripped."""
    monkeypatch.setattr(wizard, "_read_hidden_tty", lambda prompt: None)
    assert wizard._read_pasted_secret("p: ", lambda p: "  sk-ant-oat01-clean  ") == \
        "sk-ant-oat01-clean"


def test_claude_multiline_paste_is_reconstructed_and_stored(monkeypatch):
    """End-to-end: a token pasted wrapped across lines is read WHOLE by the TTY reader,
    validated, and stored as its true single-line form (the reported bug)."""
    clean = "sk-ant-oat01-AAAAccccDDDDeeeeFFFF__gggg-HHHH"
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "_gh_secret_exists", lambda *a, **k: None)  # not present
    monkeypatch.setattr(wizard, "_ask_yes_no", lambda *a, **k: True)        # consent granted
    monkeypatch.setattr(wizard, "_read_hidden_tty", lambda prompt: clean)
    stored = {}

    def _store(repo, name, token, **k):
        stored["t"] = token
        return (True, "", "repo")

    monkeypatch.setattr(wizard, "_set_secret_scoped", _store)
    seen = {}

    def _validate(tok, **k):
        seen["validated"] = tok
        return ("valid", "")

    status = wizard._set_claude_secret(
        "acme/widgets",
        run=lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=""),
        spawn_command=lambda *a, **k: {"spawned": True},
        getpass_fn=lambda *a, **k: "SHOULD_NOT_BE_USED",
        pal=wizard._Palette(False), stream=io.StringIO(),
        owner_type="User", validate_fn=_validate)
    assert status == "set"
    assert seen["validated"] == clean     # validated the reconstructed token
    assert stored["t"] == clean           # stored the reconstructed token


def test_claude_inconclusive_verdict_is_not_stored(monkeypatch):
    """An inconclusive ('unknown') verdict must NOT store the token or reach the
    org-scope prompt — an unverified token is never written to GitHub."""
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "_gh_secret_exists", lambda *a, **k: None)  # not present
    monkeypatch.setattr(wizard, "_ask_yes_no", lambda *a, **k: True)        # consent granted
    stored = []

    def _store(repo, name, token, **k):
        stored.append(token)
        return (True, "", "repo")

    monkeypatch.setattr(wizard, "_set_secret_scoped", _store)
    status = wizard._set_claude_secret(
        "buddhikernel/repo",
        run=lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=""),
        spawn_command=lambda *a, **k: {"spawned": True},
        getpass_fn=lambda *a, **k: "sk-maybe",
        pal=wizard._Palette(False), stream=io.StringIO(),
        owner_type="User", validate_fn=lambda tok, **k: ("unknown", "inconclusive"))
    assert status == "failed"          # gave up without saving
    assert stored == []                # nothing written to GitHub


def test_step_reviewers_guides_claude_github_app_install(tmp_path):
    """The Claude path must PROMINENTLY guide installing github.com/apps/claude —
    the workflow + token alone 401 and post nothing (the buddhi-review PR #3
    silent-Claude failure). Without this guidance a user wires the workflow + token
    and is surprised when claude[bot] never posts."""
    def run(argv, cwd=None, timeout=30, input=None):
        if argv[:2] == ["gh", "api"]:
            return types.SimpleNamespace(returncode=0, stdout="base64==")  # workflow present
        return types.SimpleNamespace(returncode=0, stdout="")

    pal = wizard._Palette(False)
    buf = io.StringIO()
    wizard.step_reviewers(
        "acme/widgets", str(tmp_path), {"gh_auth": False}, run=run,
        spawn_command=lambda *a, **k: None, getpass_fn=lambda *a: "",
        pal=pal, stream=buf, multi_select=lambda *a, **k: {3}, input_fn=lambda *a: "")
    out = buf.getvalue()
    assert "github.com/apps/claude" in out, "must guide the Claude GitHub App install"
    assert "401" in out, "must explain the silent-failure symptom"


def test_app_install_lines_name_each_app():
    claude = " ".join(wizard._app_install_lines("claude", "o/r"))
    assert "github.com/apps/claude" in claude and "401" in claude
    assert "Connectors" in " ".join(wizard._app_install_lines("codex", "o/r"))
    assert "gemini-code-assist" in " ".join(wizard._app_install_lines("gemini", "o/r"))


def test_no_paid_surface_strings_in_wizard_source():
    """OSS purity: the wizard names no paid mechanism. The locked teasers cite a
    benefit (e.g. 'Mobile push'), never the paid product or channel by name."""
    text = Path(wizard.__file__).read_text(encoding="utf-8")
    for forbidden in ("Telegram", "Autopilot", "auto-rebase", "self-heal", "force-push", "Cockpit"):
        assert forbidden not in text, f"{forbidden} leaked into the free wizard source"


# ── F1: fail-closed reviewer install-confirmation gate ──────────────────────────────
#
# Selecting a reviewer in the multi-select is only INTENT; the vendor GitHub-App /
# Copilot installs happen in the GitHub UI and setup cannot verify them by API. The
# gate makes setup require an explicit "this reviewer is installed" confirmation
# before a reviewer is recorded as an expected (auto-)reviewer. An unconfirmed bot is
# dropped from BOTH the enabled fleet and auto_on_open — otherwise the loop waits
# forever on a review that never comes, or merges a PR that got zero reviews.

def _router(answers, default=""):
    """A prompt-substring → reply router for input_fn (first matching key wins)."""
    def fn(prompt=""):
        for key, val in answers.items():
            if key in prompt:
                return val
        return default
    return fn


def _reviewers_run(*, workflow_present=True, secret_present=True):
    """A run() seam for step_reviewers: the Claude workflow + secret states are
    controllable; every other call is a benign rc=0."""
    def run(argv, cwd=None, timeout=30, input=None):
        R = types.SimpleNamespace
        if argv[:2] == ["gh", "api"]:
            return R(returncode=0 if workflow_present else 1,
                     stdout="base64==" if workflow_present else "")
        if argv[:3] == ["gh", "secret", "list"]:
            return R(returncode=0,
                     stdout="CLAUDE_CODE_OAUTH_TOKEN" if secret_present else "")
        return R(returncode=0, stdout="")
    return run


# The gate is now a labeled single_select (preselect=No): option 1 = Yes (enable),
# option 0 / preselect = No (disable). These helpers drive that channel.
def _ss_yes(*a, **k):
    return 1


def _ss_no(prompt, options, *, preselect=0, **kw):
    return preselect


def _ss_gate_yes(prompt, options, *, preselect=0, **kw):
    """Confirm the install gate (option 1 = Yes); every OTHER select — including the
    G3 auto-on-open labeled select — takes its preselect (auto-on-open → Yes)."""
    return 1 if "ready to review PRs" in prompt else preselect


def _run_step_reviewers(*, bots, tmp_path, single_select=_ss_no, input_fn=None,
                        gh_auth=True, repo="acme/widgets"):
    """Drive step_reviewers for a chosen reviewer subset → (enabled, auto_on_open, output).
    ``single_select`` drives BOTH the install-confirmation gate and the per-bot
    auto-on-open labeled select (route by prompt substring to answer them apart)."""
    idxs = {wizard._REVIEWERS.index(b) for b in bots}
    buf = io.StringIO()
    enabled, aoo = wizard.step_reviewers(
        repo, str(tmp_path), {"gh_auth": gh_auth}, run=_reviewers_run(),
        spawn_command=lambda *a, **k: None, getpass_fn=lambda *a: "",
        pal=wizard._Palette(False), stream=buf,
        multi_select=lambda *a, **k: idxs, single_select=single_select,
        input_fn=input_fn or (lambda *a: ""))
    return enabled, aoo, buf.getvalue()


def test_confirm_reviewer_installed_truth_table(monkeypatch):
    """The gate helper in isolation. The decision hinges on _is_tty (a module global):
    no TTY → always False, no matter the selection. With a TTY, ONLY option 1 (Yes) is
    True; the preselect (option 0 = No) is False."""
    buf = io.StringIO()
    pal = wizard._Palette(False)
    blank = lambda *a: ""
    monkeypatch.setattr(wizard, "_is_tty", lambda: False)
    # No TTY: even a single_select that would return Yes is never consulted → False.
    assert wizard._confirm_reviewer_installed(
        "copilot", "o/r", single_select=_ss_yes, pal=pal, stream=buf, input_fn=blank) is False

    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    assert wizard._confirm_reviewer_installed(
        "codex", "o/r", single_select=_ss_no, pal=pal, stream=buf, input_fn=blank) is False
    assert wizard._confirm_reviewer_installed(
        "gemini", "o/r", single_select=lambda *a, **k: 0, pal=pal, stream=buf, input_fn=blank) is False
    assert wizard._confirm_reviewer_installed(
        "claude", "o/r", single_select=_ss_yes, pal=pal, stream=buf, input_fn=blank) is True
    assert wizard._confirm_reviewer_installed(
        "copilot", None, single_select=_ss_yes, pal=pal, stream=buf, input_fn=blank) is True


def test_confirm_gate_prompt_and_options_are_canonical(monkeypatch):
    """The labeled-select gate renders the canonical question + both option labels +
    consequence details (byte-for-byte from setup-ux-parity.md)."""
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    seen = {}

    def ss(prompt, options, *, preselect=0, **kw):
        seen["prompt"] = prompt
        seen["options"] = list(options)
        seen["preselect"] = preselect
        return preselect

    wizard._confirm_reviewer_installed("codex", "acme/widgets", single_select=ss,
                                       pal=wizard._Palette(False), stream=io.StringIO(),
                                       input_fn=lambda *a: "")
    assert "Confirm the 'codex' reviewer is installed on acme/widgets and ready to review PRs?" in seen["prompt"]
    assert seen["preselect"] == 0       # preselect = No (fail-closed)
    labels = [lbl for lbl, _ in seen["options"]]
    details = [d for _, d in seen["options"]]
    assert labels[0] == "No / not sure — leave it disabled"
    assert details[0] == "the loop won't wait on a reviewer that can't respond"
    assert labels[1] == "Yes — codex is installed and will review PRs on acme/widgets"
    assert details[1] == "the loop treats it as an expected reviewer"


@pytest.mark.parametrize("bot", list(wizard._REVIEWERS))
def test_install_gate_disables_each_reviewer_without_a_tty(monkeypatch, tmp_path, bot):
    """Fail-closed for ALL FOUR reviewers — including Copilot, whose path is an
    info-row hint (no app-install panel). With no TTY to confirm, a single_select that
    would say Yes is IGNORED and the bot is dropped from both the fleet and auto_on_open."""
    monkeypatch.setattr(wizard, "_is_tty", lambda: False)
    enabled, aoo, out = _run_step_reviewers(
        bots=[bot], single_select=_ss_yes, input_fn=lambda *a: "y", tmp_path=tmp_path)
    assert enabled == []
    assert aoo == {}                       # nothing recorded at all …
    assert True not in aoo.values()        # … so certainly no auto_on_open=True
    assert "left DISABLED" in out


@pytest.mark.parametrize("bot", list(wizard._REVIEWERS))
def test_install_gate_fails_closed_on_preselect_no_even_with_a_tty(monkeypatch, tmp_path, bot):
    """Even WITH a TTY, anything short of an explicit Yes fails closed: the gate
    preselects No, so accepting the default (the single_select returns its preselect)
    leaves the reviewer disabled."""
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "_set_claude_secret", lambda *a, **k: "present")
    monkeypatch.setattr(wizard, "_offer_update_managed_file", lambda *a, **k: None)
    enabled, aoo, _ = _run_step_reviewers(bots=[bot], single_select=_ss_no, tmp_path=tmp_path)
    assert enabled == []
    assert True not in aoo.values()


def test_install_gate_enables_app_reviewers_on_explicit_yes(monkeypatch, tmp_path):
    """Explicit Yes (with a TTY) keeps the reviewer. The three GitHub-App reviewers go
    through the same gate; on confirm they are enabled and their auto_on_open default
    (True — they auto-review on PR open) is captured via the labeled select's
    preselect."""
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    enabled, aoo, _ = _run_step_reviewers(
        bots=["copilot", "gemini", "codex"], single_select=_ss_gate_yes, tmp_path=tmp_path)
    assert enabled == ["copilot", "gemini", "codex"]
    assert aoo == {"copilot": True, "gemini": True, "codex": True}


def test_install_gate_enables_claude_on_explicit_yes(monkeypatch, tmp_path):
    """Claude is gated too (its GitHub App is the third requirement). On an explicit
    Yes it is enabled, with auto_on_open=False — it is mention-driven, never asked."""
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "_set_claude_secret", lambda *a, **k: "present")
    monkeypatch.setattr(wizard, "_offer_update_managed_file", lambda *a, **k: None)
    enabled, aoo, _ = _run_step_reviewers(
        bots=["claude"], single_select=_ss_yes, tmp_path=tmp_path)
    assert enabled == ["claude"]
    assert aoo == {"claude": False}


def test_install_gate_partial_confirmation_drops_only_the_declined(monkeypatch, tmp_path):
    """A mixed answer enables only the confirmed reviewers: declining Gemini (its gate
    prompt carries the lowercase bot id) drops it from BOTH the fleet and auto_on_open
    while Copilot/Codex stay."""
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)

    def ss(prompt, options, *, preselect=0, **kw):
        if "ready to review PRs" in prompt:       # the install gate …
            return 0 if "'gemini'" in prompt else 1   # … decline gemini, confirm the rest
        return preselect                          # auto-on-open keeps its Yes default

    enabled, aoo, _ = _run_step_reviewers(
        bots=["copilot", "gemini", "codex"], single_select=ss, tmp_path=tmp_path)
    assert enabled == ["copilot", "codex"]
    assert "gemini" not in aoo
    assert aoo == {"copilot": True, "codex": True}


# ── G3: the per-bot auto-on-open question is a labeled select, not a bare [Y/n] ─────
#
# The question wording stays OSS's canonical "Does {Bot} auto-review when a PR is
# opened?"; the OPTIONS carry the canonical labels + consequence details
# (byte-for-byte from setup-ux-parity.md `per_reviewer_auto_on_open_prompt`).

def test_auto_on_open_prompt_and_options_are_canonical(monkeypatch, tmp_path):
    """The auto-on-open capture renders the canonical labeled select — question +
    both option labels + consequence details — with Yes preselected. What mutation
    this catches: reverting to the bare [Y/n] prompt (no options rendered), editing
    a label, or flipping the preselect to No."""
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    captured = []

    def ss(prompt, options, *, preselect=0, **kw):
        captured.append((prompt, list(options), preselect, kw.get("shortcuts")))
        return 1 if "ready to review PRs" in prompt else preselect

    _, aoo, out = _run_step_reviewers(bots=["copilot"], single_select=ss,
                                      tmp_path=tmp_path)
    aoo_calls = [c for c in captured if "auto-review when a PR is opened" in c[0]]
    assert len(aoo_calls) == 1, "the auto-on-open question must ride the labeled select"
    prompt, options, preselect, shortcuts = aoo_calls[0]
    assert "Does Copilot auto-review when a PR is opened?" in prompt
    assert preselect == 0                    # default = Yes (auto-review)
    assert shortcuts == {"y": 0, "n": 1}     # the old prompt's keystrokes still work
    labels = [lbl for lbl, _ in options]
    details = [d for _, d in options]
    assert labels[0] == "Yes — reviews automatically when a PR is opened"
    assert details[0] == "the loop won't re-summon it in round 1 (avoids a duplicate review)"
    assert labels[1] == "No — must be triggered/requested to review"
    assert details[1] == "the loop posts its round-1 request so its review still arrives"
    assert "[Y/n]" not in out                # the bare text prompt is gone
    assert aoo == {"copilot": True}          # accepting the preselect records Yes


def test_auto_on_open_no_option_records_false(monkeypatch, tmp_path):
    """Choosing option 1 (No — must be triggered/requested) records
    auto_on_open=False, so the loop will summon the bot in round 1."""
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)

    def ss(prompt, options, *, preselect=0, **kw):
        if "auto-review when a PR is opened" in prompt:
            return 1                          # No — must be triggered/requested
        return 1 if "ready to review PRs" in prompt else preselect

    _, aoo, _ = _run_step_reviewers(bots=["codex"], single_select=ss, tmp_path=tmp_path)
    assert aoo == {"codex": False}


def test_auto_on_open_non_tty_keeps_the_yes_default(monkeypatch):
    """Off a TTY the labeled select falls back to the numbered prompt, and a blank
    or EOF answer resolves to the SAME default the old bare [Y/n] prompt had: Yes.
    (In the full step a non-TTY run never reaches this question — the F1 gate drops
    every unconfirmed reviewer first — so the helper is asserted directly.)"""
    monkeypatch.setattr(wizard, "_is_tty", lambda: False)

    def eof(*a):
        raise EOFError

    for input_fn in (lambda *a: "", eof):
        buf = io.StringIO()
        assert wizard._ask_auto_on_open(
            "copilot", single_select=wizard.single_select,
            pal=wizard._Palette(False), stream=buf, input_fn=input_fn) is True
        out = buf.getvalue()
        assert "Does Copilot auto-review when a PR is opened?" in out
        assert "Yes — reviews automatically when a PR is opened" in out
        assert "[Y/n]" not in out


def test_auto_on_open_tty_keystroke_shortcuts_match_the_old_prompt(monkeypatch):
    """On a real TTY the old bare prompt answered to a single 'y'/'n' keystroke; the
    labeled select keeps those shortcuts, so 'n' still records auto_on_open=False
    (and 'y' True) instead of falling through to the Yes preselect. What mutation
    this catches: dropping the shortcuts kwarg, which would turn a muscle-memory 'n'
    into a silent Yes — the loop would then never summon a bot that never auto-posts."""
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    for key, expect in (("y", True), ("n", False)):
        monkeypatch.setattr(wizard, "_read_key", lambda k=key: k)
        assert wizard._ask_auto_on_open(
            "copilot", single_select=wizard.single_select,
            pal=wizard._Palette(False), stream=io.StringIO(),
            input_fn=lambda *a: "") is expect


def test_auto_on_open_claude_never_asked(monkeypatch, tmp_path):
    """Claude stays mention-driven: auto_on_open['claude'] is recorded False and the
    auto-on-open select never renders for it."""
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "_set_claude_secret", lambda *a, **k: "present")
    monkeypatch.setattr(wizard, "_offer_update_managed_file", lambda *a, **k: None)
    captured = []

    def ss(prompt, options, *, preselect=0, **kw):
        captured.append(prompt)
        return 1 if "ready to review PRs" in prompt else preselect

    enabled, aoo, _ = _run_step_reviewers(bots=["claude"], single_select=ss,
                                          tmp_path=tmp_path)
    assert enabled == ["claude"]
    assert aoo == {"claude": False}
    assert not any("auto-review when a PR is opened" in p for p in captured)


# ── F1: launch-into-first-review offer at Done ──────────────────────────────────────

def test_offer_first_review_prints_command_on_yes(monkeypatch):
    """At Done the wizard offers to start the first review; an explicit Yes prints the
    EXACT launch command (with the repo) so the user goes straight into a review."""
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "single_select", _yn_bridge)
    buf = io.StringIO()
    wizard._offer_first_review("acme/widgets", pal=wizard._Palette(False), stream=buf,
                               input_fn=lambda *a: "y")
    assert "/review-pr <pr-number> acme/widgets" in buf.getvalue()


def test_offer_first_review_silent_on_decline_or_non_tty(monkeypatch):
    """Decline or no TTY → nothing printed and nothing launched (setup already
    succeeded; the offer is a convenience, never a gate)."""
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "single_select", _yn_bridge)
    buf = io.StringIO()
    wizard._offer_first_review("acme/widgets", pal=wizard._Palette(False), stream=buf,
                               input_fn=lambda *a: "")
    assert "/review-pr" not in buf.getvalue()
    monkeypatch.setattr(wizard, "_is_tty", lambda: False)
    buf2 = io.StringIO()
    wizard._offer_first_review("acme/widgets", pal=wizard._Palette(False), stream=buf2,
                               input_fn=lambda *a: "y")
    assert buf2.getvalue() == ""


def test_full_run_offers_first_review_at_done(monkeypatch, tmp_path):
    """The full wizard wires the offer in after step_done: a TTY run that says yes to
    'Review an open PR now?' prints the launch command."""
    monkeypatch.delenv("BUDDHI_NO_UPSELL", raising=False)
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "single_select", _yn_bridge)
    # Neuter the Claude secret / update sub-prompts so only the prompts under test run.
    monkeypatch.setattr(wizard, "_set_claude_secret", lambda *a, **k: "present")
    monkeypatch.setattr(wizard, "_offer_update_managed_file", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_offer_gh_token", lambda *a, **k: None)
    cfg_path = tmp_path / "config.yaml"
    buf = io.StringIO()
    run = _fake_run_factory(workflow_present=True)

    def run_with_cwd(argv, cwd=None, timeout=30, input=None):
        if argv[:2] == ["git", "-C"] and "rev-parse" in argv and "--show-toplevel" in argv:
            return types.SimpleNamespace(returncode=0, stdout=str(tmp_path) + "\n")
        return run(argv, cwd=cwd, timeout=timeout, input=input)

    rc = wizard.run(
        config_path=cfg_path, run=run_with_cwd,
        which=lambda x: "/bin/claude" if x == "claude" else None,
        single_select=lambda prompt, options, *, preselect=0, **kw: preselect,
        multi_select=lambda *a, **k: set(),     # no reviewers → keep the run short
        getpass_fn=lambda *a: "", spawn_command=lambda *a, **k: {"spawned": False},
        input_fn=_router({"Review an open PR now": "y"}), stream=buf)
    assert rc == 0
    assert "/review-pr <pr-number> acme/widgets" in buf.getvalue()


# ── F1: non-interactive end-to-end smoke (the return-value-plumbing regression) ──────

def test_non_interactive_full_run_persists_no_auto_reviewer(monkeypatch, tmp_path):
    """End-to-end regression: a fully NON-INTERACTIVE wizard run (EOF on every prompt,
    no TTY) must persist NO app bot with auto_on_open=True — even though the user
    'selected' all four reviewers in the multi-select. Recording an unconfirmed app
    reviewer as an auto-reviewer is exactly what let the loop merge a zero-review PR;
    this asserts the gate's drop reaches the WRITTEN config, not just the return value."""
    monkeypatch.delenv("BUDDHI_NO_UPSELL", raising=False)
    cfg_path = tmp_path / "config.yaml"
    buf = io.StringIO()
    run = _fake_run_factory(workflow_present=True)

    def run_with_cwd(argv, cwd=None, timeout=30, input=None):
        if argv[:2] == ["git", "-C"] and "rev-parse" in argv and "--show-toplevel" in argv:
            return types.SimpleNamespace(returncode=0, stdout=str(tmp_path) + "\n")
        return run(argv, cwd=cwd, timeout=timeout, input=input)

    def eof(*a):
        raise EOFError

    rc = wizard.run(
        config_path=cfg_path, run=run_with_cwd,
        which=lambda x: "/bin/claude" if x == "claude" else None,
        single_select=lambda prompt, options, *, preselect=0, **kw: preselect,
        multi_select=lambda *a, **k: set(range(len(wizard._REVIEWERS))),  # "select" all four
        getpass_fn=lambda *a: "", spawn_command=lambda *a, **k: {"spawned": False},
        input_fn=eof, stream=buf)
    assert rc == 0
    cfg = _load_yaml(cfg_path)
    # Inspect the top-level AND every per-repo auto_on_open block: no bot is True.
    blocks = [cfg.get("auto_on_open") or {}]
    for entry in (cfg.get("repos") or {}).values():
        blocks.append(entry.get("auto_on_open") or {})
    for block in blocks:
        assert True not in block.values(), f"a non-interactive run recorded an auto-reviewer: {block}"
    # Nothing could be confirmed without a TTY, so the persisted fleet is empty.
    assert cfg.get("active_reviewers") == []


# ── Canonical shared-step wording (setup-ux-parity.md) renders verbatim ──────────────

def test_canonical_shared_step_strings_render_verbatim():
    """Spot-check a sample of canonical shared-step strings — doctor rows (with the
    composed install URLs), the reviewer-fleet prerequisite warning + per-bot option
    details, the app-install guidance, and the done-summary rows — render byte-for-byte."""
    pal = wizard._Palette(False)

    # Doctor: progress lines + install URLs composed into the rows.
    buf = io.StringIO()
    wizard.step_doctor(run=lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=""),
                       which=lambda x: None, pal=pal, stream=buf)
    d = buf.getvalue()
    assert "Checking the tools the loop depends on." in d
    assert "Checking Claude CLI…" in d and "Probing reachable model tiers" not in d  # no claude → not probed
    assert "Claude CLI not found — install it to run reviews/fixes: https://claude.com/claude-code" in d
    assert "gh CLI not found — install GitHub CLI (https://cli.github.com), then run `gh auth login`" in d
    assert "Fix the ⚠ items above, then re-run /review-pr setup." in d  # warns present → footer

    # Reviewer-fleet intro warning + per-bot option details (rendered in step_reviewers).
    buf2 = io.StringIO()
    wizard.step_reviewers("acme/widgets", "/tmp/x", {"gh_auth": True},
                          run=lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=""),
                          spawn_command=lambda *a, **k: None, getpass_fn=lambda *a: "",
                          pal=pal, stream=buf2, multi_select=lambda *a, **k: set(),
                          input_fn=lambda *a: "")
    r = buf2.getvalue()
    assert ("EVERY reviewer you enable must already have its vendor GitHub app + plan "
            "installed on this repo, with its trigger configured and working — otherwise "
            "the round-1 request may have no effect (the per-bot setup steps follow).") in r

    # App-install guidance: Codex plan prereq + trigger, Gemini canonical trigger, Claude 401 line.
    codex = " ".join(wizard._app_install_lines("codex", "acme/widgets"))
    assert codex == ("A ChatGPT plan that includes Codex is required. Install the OpenAI "
                     "Codex app via Codex ▸ Settings ▸ Connectors ▸ GitHub and grant it "
                     "access to `acme/widgets`. It then replies to '@codex review' on a PR.")
    gemini = " ".join(wizard._app_install_lines("gemini", "acme/widgets"))
    assert gemini == ("Install the Gemini Code Assist GitHub App (github.com/apps/gemini-code-assist) "
                      "and grant it access to `acme/widgets`. It then replies to '/gemini review' on a PR.")
    claude = " ".join(wizard._app_install_lines("claude", "acme/widgets"))
    assert "fails with 401 (\"Claude Code is not installed on this repository\")" in claude

    # Done summary: labeled auto/summon split + auto-merge / label-gated descriptions,
    # and the inferred-repo fallback row.
    buf3 = io.StringIO()
    wizard.step_summary("pro", None, ["copilot", "claude"],
                        {"copilot": True, "claude": False}, pal=pal, stream=buf3,
                        auto_merge=True, label_gated_ci=False)
    s = buf3.getvalue()
    assert "auto: copilot · summon round 1: claude" in s
    assert "on — clean PRs squash-merge" in s
    assert "off — CI runs on every push" in s
    assert "(inferred at runtime from the git remote)" in s
