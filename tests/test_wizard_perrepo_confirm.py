"""F3 — the wizard's per-repo confirmation flow.

``wizard.confirm_repo_interactive`` is the lightweight ``setup --repo <owner/repo>``
mode (parity with the reference wizard's ``confirm_repo_interactive``): it confirms
ONE repo's reviewer fleet + ``auto_on_open`` + ``auto_merge`` + label-gated CI,
optionally promotes the fleet to the global default, and persists ``repos[<repo>]``
through :func:`buddhi_review.config.set_repo_keys`. Every answer round-trips back
through the F1 readers (:func:`config.active_reviewers` / :func:`config.auto_on_open`
/ :func:`config.label_gated_ci` / :func:`config.repo_entry`).
"""
import io
import types

import pytest

from buddhi_review import config, wizard
from conftest import _yn_bridge


REPO = "octocat/Hello-World"
# Reviewer indices in wizard._REVIEWERS == ("copilot", "gemini", "codex", "claude").
COPILOT, GEMINI, CODEX, CLAUDE = 0, 1, 2, 3


@pytest.fixture(autouse=True)
def _interactive(monkeypatch):
    """The per-repo confirm flow is an interactive TTY program; force a TTY so the
    F1 fail-closed install-confirmation gate can obtain its explicit Yes. Without
    this the gate (correctly) drops every reviewer for lack of a TTY to confirm on."""
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    # On a forced TTY, _ask_yes_no routes through the module-level single_select
    # (which requires _read_key / a real TTY).  Replace it with a bridge that reads
    # the test's input_fn instead, so yes/no questions are driveable from tests.
    monkeypatch.setattr(wizard, "single_select", _yn_bridge)
    # Default behaviour = flag OFF (auto-promote first-run, never ask). Clear any
    # ambient BUDDHI_ASK_GLOBAL_DEFAULT so the default-path tests are deterministic;
    # the flag-path tests opt in with monkeypatch.setenv.
    monkeypatch.delenv("BUDDHI_ASK_GLOBAL_DEFAULT", raising=False)


# ── Injected seams ─────────────────────────────────────────────────────────────────

def _ss_router(answers, captured=None):
    """A single_select that routes by prompt substring → option index; captures
    (prompt, options) when ``captured`` is given; falls back to ``preselect``."""
    def ss(prompt, options, *, preselect=0, **kw):
        if captured is not None:
            captured.append((prompt, list(options)))
        for key, idx in answers.items():
            if key in prompt:
                return idx
        return preselect
    return ss


def _in_router(answers, default=""):
    """An input_fn that routes a yes/no prompt by substring → raw reply."""
    def fn(prompt=""):
        for key, val in answers.items():
            if key in prompt:
                return val
        return default
    return fn


def _run_factory(*, gh_auth=False, remote=None, toplevel=None):
    def run(argv, cwd=None, timeout=30, input=None):
        R = types.SimpleNamespace
        if argv[:3] == ["gh", "auth", "status"]:
            return R(returncode=0 if gh_auth else 1, stdout="")
        if argv[:2] == ["git", "-C"] and "remote" in argv:
            return R(returncode=0 if remote else 1, stdout=((remote + "\n") if remote else ""))
        if argv[:2] == ["git", "-C"] and "rev-parse" in argv:
            return R(returncode=0 if toplevel else 1, stdout=((toplevel + "\n") if toplevel else ""))
        if argv[:2] == ["gh", "api"]:
            return R(returncode=1, stdout="")          # no Claude workflow present
        if argv[:3] == ["gh", "secret", "list"]:
            return R(returncode=0, stdout="")
        return R(returncode=0, stdout="")
    return run


def _confirm(cfg_path, *, repo=REPO, reviewers, ss_answers=None, yn_answers=None,
             gh_auth=False, captured=None, multi_select=None):
    """Drive confirm_repo_interactive with scripted selectors. ``reviewers`` is a set
    of indices into wizard._REVIEWERS. Returns ``(rc, output)``."""
    buf = io.StringIO()
    ms = multi_select or (lambda *a, **k: set(reviewers))
    # Auto-confirm the F1 install gate by default — these tests drive a SUCCESSFUL
    # confirmation. The gate is a labeled single_select ("… ready to review PRs?",
    # option 1 = Yes), and since G3 the per-bot auto-on-open question is a labeled
    # select on the SAME channel ("Does {Bot} auto-review …", 0 = Yes / 1 = No;
    # unrouted it takes its Yes preselect). A test wanting a gate drop overrides
    # "ready to review PRs".
    ss = {"ready to review PRs": 1, **(ss_answers or {})}
    rc = wizard.confirm_repo_interactive(
        repo, "/work/checkout",
        run=_run_factory(gh_auth=gh_auth), spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "", pal=wizard._Palette(False), stream=buf,
        cfg_path=cfg_path, multi_select=ms,
        single_select=_ss_router(ss, captured),
        input_fn=_in_router(yn_answers or {}))
    return rc, buf.getvalue()


def _read(path):
    return config.load_config(path)


# ── Per-repo write round-trips through set_repo_keys + the F1 readers ───────────────

def test_confirm_writes_per_repo_entry_and_round_trips(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    rc, _ = _confirm(
        cfg_path, reviewers={COPILOT},
        ss_answers={"GLOBAL default": 1,            # No — only this repo
                    "Auto-merge default for": 1,    # On
                    "Label-gated CI default for": 1,  # On
                    "Confirm: enable label-gated CI": 1,  # Yes
                    "Does Copilot auto-review": 0})  # copilot auto-on-open True
    assert rc == 0
    cfg = _read(cfg_path)
    # The repo is CONFIRMED (the entry exists) and every key round-trips via F1.
    assert config.repo_entry(cfg, REPO) is not None
    assert config.active_reviewers(cfg, REPO) == ("copilot",)
    assert config.auto_on_open(cfg, "copilot", REPO) is True
    assert config.label_gated_ci(cfg, REPO) is True
    assert config.repo_entry(cfg, REPO)["auto_merge"] is True


def test_confirm_records_multiselect_and_per_bot_auto_on_open(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    rc, _ = _confirm(
        cfg_path, reviewers={COPILOT, CODEX},
        ss_answers={"GLOBAL default": 1, "Auto-merge default for": 0,
                    "Label-gated CI default for": 0,
                    # per-bot auto_on_open via the G3 labeled select (0=Yes, 1=No)
                    "Does Copilot auto-review": 0, "Does Codex auto-review": 1})
    assert rc == 0
    cfg = _read(cfg_path)
    assert config.active_reviewers(cfg, REPO) == ("copilot", "codex")
    assert config.auto_on_open(cfg, "copilot", REPO) is True
    assert config.auto_on_open(cfg, "codex", REPO) is False
    assert config.label_gated_ci(cfg, REPO) is False
    assert config.repo_entry(cfg, REPO)["auto_merge"] is False


# ── The label-gated-CI question ────────────────────────────────────────────────────

def test_label_gated_ci_opt_in_requires_the_second_confirm(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    # "On" at the first prompt but DECLINE the explicit confirmation → stays off.
    rc, _ = _confirm(
        cfg_path, reviewers={COPILOT},
        ss_answers={"GLOBAL default": 1, "Auto-merge default for": 0,
                    "Label-gated CI default for": 1,        # On …
                    "Confirm: enable label-gated CI": 0})   # … but No at the confirm
    assert rc == 0
    assert config.label_gated_ci(_read(cfg_path), REPO) is False


def test_label_gated_ci_off_skips_the_confirm(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    captured = []
    rc, _ = _confirm(
        cfg_path, reviewers={COPILOT},
        ss_answers={"GLOBAL default": 1, "Auto-merge default for": 0,
                    "Label-gated CI default for": 0},  # Off
        captured=captured)
    assert rc == 0
    assert config.label_gated_ci(_read(cfg_path), REPO) is False
    # The explicit second confirm is NEVER shown when the answer is Off.
    assert not any("Confirm: enable label-gated CI" in p for p, _ in captured)


# ── Global-default: DEFAULT behaviour — auto-promote first-run, never ask ───────────

def _seed_global_default(cfg_path, fleet):
    config.set_repo_keys("seed/seed", {"active_reviewers": list(fleet)}, cfg_path)
    cfg = config.load_config(cfg_path)
    cfg["active_reviewers"] = list(fleet)
    from buddhi_review.wizard import write_config
    write_config(cfg, cfg_path)


def test_first_run_auto_promotes_and_never_asks(tmp_path):
    """First system-wide setup (no global default yet): the confirmed fleet is
    auto-promoted to the global default WITHOUT a prompt — so cross-repo runs have a
    fall-back and this first repo doubles as the global default."""
    cfg_path = tmp_path / "config.yaml"
    captured = []
    rc, out = _confirm(
        cfg_path, reviewers={COPILOT, CODEX},
        ss_answers={"Auto-merge default for": 0, "Label-gated CI default for": 0},
        captured=captured)
    assert rc == 0
    cfg = _read(cfg_path)
    # Auto-promoted: the top-level fleet is now the global default …
    assert config.has_global_default(cfg) is True
    assert cfg["active_reviewers"] == ["copilot", "codex"]
    # … and the same fleet is written as this repo's confirmed entry.
    assert config.repo_entry(cfg, REPO) is not None
    assert "set as global default" in out
    # The promotion question was NEVER shown.
    assert not any("GLOBAL default" in p for p, _ in captured)


def test_subsequent_run_leaves_established_default_and_never_asks(tmp_path):
    """With a global default already set, confirming a NEW repo neither re-promotes
    nor asks — the established default is left untouched; only the repo entry lands."""
    cfg_path = tmp_path / "config.yaml"
    _seed_global_default(cfg_path, ["copilot", "claude"])
    captured = []
    rc, out = _confirm(
        cfg_path, repo="acme/widgets", reviewers={CODEX},
        ss_answers={"Auto-merge default for": 0, "Label-gated CI default for": 0,
                    "Does Codex auto-review": 1},
        captured=captured)
    assert rc == 0
    cfg = _read(cfg_path)
    # The established global default is unchanged.
    assert cfg["active_reviewers"] == ["copilot", "claude"]
    # The new repo IS confirmed, but was NOT promoted.
    assert config.repo_entry(cfg, "acme/widgets") is not None
    assert "set as global default" not in out
    assert not any("GLOBAL default" in p for p, _ in captured)


# ── Flag restores the interactive promotion prompt (BUDDHI_ASK_GLOBAL_DEFAULT) ──────

def test_flag_restores_promotion_prompt_and_promotes_on_yes(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDHI_ASK_GLOBAL_DEFAULT", "1")
    cfg_path = tmp_path / "config.yaml"
    captured = []
    rc, out = _confirm(
        cfg_path, reviewers={COPILOT, CODEX},
        ss_answers={"GLOBAL default": 0,            # Yes — promote
                    "Auto-merge default for": 0, "Label-gated CI default for": 0},
        captured=captured)
    assert rc == 0
    cfg = _read(cfg_path)
    assert config.has_global_default(cfg) is True
    assert cfg["active_reviewers"] == ["copilot", "codex"]
    assert config.repo_entry(cfg, REPO) is not None
    assert "set as global default" in out
    # Under the flag the prompt WAS shown.
    assert any("GLOBAL default" in p for p, _ in captured)


def test_flag_prompt_no_leaves_default_unset(tmp_path, monkeypatch):
    """Flag ON + user answers 'No' → the repo is confirmed but NOT promoted (the
    old behaviour, faithfully restored)."""
    monkeypatch.setenv("BUDDHI_ASK_GLOBAL_DEFAULT", "1")
    cfg_path = tmp_path / "config.yaml"
    rc, out = _confirm(
        cfg_path, reviewers={COPILOT},
        ss_answers={"GLOBAL default": 1, "Auto-merge default for": 0,
                    "Label-gated CI default for": 0})
    assert rc == 0
    cfg = _read(cfg_path)
    assert config.has_global_default(cfg) is False     # no top-level fleet written
    assert config.repo_entry(cfg, REPO) is not None     # but the repo IS confirmed
    assert "set as global default" not in out


def test_flag_global_default_question_names_its_subject(tmp_path, monkeypatch):
    """P7 #3 (flag path): the restored prompt names the concrete fleet AND which
    reviewers auto-post on PR open — no bare 'these'."""
    monkeypatch.setenv("BUDDHI_ASK_GLOBAL_DEFAULT", "1")
    cfg_path = tmp_path / "config.yaml"
    captured = []
    _confirm(
        cfg_path, reviewers={COPILOT, CODEX},
        ss_answers={"GLOBAL default": 1, "Auto-merge default for": 0,
                    "Label-gated CI default for": 0,
                    # copilot auto-posts on open, codex does not (G3 labeled select)
                    "Does Copilot auto-review": 0, "Does Codex auto-review": 1},
        captured=captured)
    gd = [(p, opts) for p, opts in captured if "GLOBAL default" in p]
    assert gd, "the global-default question must be asked under the flag"
    prompt, opts = gd[0]
    # The fleet is named in the prompt (the question's subject).
    assert "copilot, codex" in prompt
    # The option detail names which reviewers auto-post on open (copilot, not codex).
    details = " ".join(d for _, d in opts)
    assert "auto-posts on PR open: copilot" in details


# ── Empty-fleet wipe guard (flag path only — never silently wipe a global default) ──

def test_flag_empty_fleet_promotion_keeps_existing_global_default_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDHI_ASK_GLOBAL_DEFAULT", "1")
    cfg_path = tmp_path / "config.yaml"
    _seed_global_default(cfg_path, ["copilot", "claude"])
    rc, out = _confirm(
        cfg_path, reviewers=set(),                  # the user deselected every reviewer
        ss_answers={"GLOBAL default": 0,            # Yes — promote (empty fleet) …
                    "Clear your global default": 0,  # … but KEEP the existing default
                    "Auto-merge default for": 0, "Label-gated CI default for": 0})
    assert rc == 0
    cfg = _read(cfg_path)
    # The existing global default survives untouched.
    assert cfg["active_reviewers"] == ["copilot", "claude"]
    # This repo's entry is still written, with the (empty) confirmed fleet.
    assert config.active_reviewers(cfg, REPO) == ()
    assert "Keeping your existing global default" in out


def test_flag_empty_fleet_promotion_clears_global_default_when_chosen(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDHI_ASK_GLOBAL_DEFAULT", "1")
    cfg_path = tmp_path / "config.yaml"
    _seed_global_default(cfg_path, ["copilot", "claude"])
    rc, _ = _confirm(
        cfg_path, reviewers=set(),
        ss_answers={"GLOBAL default": 0,
                    "Clear your global default": 1,  # explicitly clear it
                    "Auto-merge default for": 0, "Label-gated CI default for": 0})
    assert rc == 0
    assert _read(cfg_path)["active_reviewers"] == []


# ── Seeding from the global default ─────────────────────────────────────────────────

def test_reviewer_fleet_is_seeded_from_the_global_default(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    _seed_global_default(cfg_path, ["copilot", "claude"])
    seen = {}

    def capturing_ms(prompt, options, *, preselected=None, **kw):
        seen["preselected"] = preselected
        return {COPILOT}

    _confirm(cfg_path, reviewers={COPILOT}, multi_select=capturing_ms,
             ss_answers={"GLOBAL default": 1, "Auto-merge default for": 0,
                         "Label-gated CI default for": 0})
    # The global default ["copilot", "claude"] preselects indices {0, 3}.
    assert seen["preselected"] == {COPILOT, CLAUDE}


# ── Repo resolution (infer / cannot-infer) ─────────────────────────────────────────

def test_confirm_returns_2_when_no_repo_and_none_inferable(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    buf = io.StringIO()
    rc = wizard.confirm_repo_interactive(
        "", None, run=_run_factory(remote=None, toplevel=None),
        spawn_command=lambda *a, **k: None, getpass_fn=lambda *a: "",
        pal=wizard._Palette(False), stream=buf, cfg_path=cfg_path,
        multi_select=lambda *a, **k: set(), single_select=_ss_router({}),
        input_fn=_in_router({}))
    assert rc == 2
    assert not cfg_path.exists()
    assert "none could be inferred" in buf.getvalue()


def test_confirm_infers_repo_from_git_remote(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    buf = io.StringIO()
    run = _run_factory(remote="git@github.com:acme/widgets.git", toplevel="/work/x")
    rc = wizard.confirm_repo_interactive(
        "", None, run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "", pal=wizard._Palette(False), stream=buf,
        cfg_path=cfg_path, multi_select=lambda *a, **k: {COPILOT},
        single_select=_ss_router({"ready to review PRs": 1, "GLOBAL default": 1,
                                  "Auto-merge default for": 0,
                                  "Label-gated CI default for": 0,
                                  "Does Copilot auto-review": 0}),
        input_fn=_in_router({}))
    assert rc == 0
    assert config.repo_entry(_read(cfg_path), "acme/widgets") is not None


# ── Sibling preservation through the per-repo write ─────────────────────────────────

def test_confirm_leaves_sibling_repo_and_unknown_keys_intact(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    config.set_repo_keys("acme/widgets", {"active_reviewers": ["gemini"]}, cfg_path)
    cfg = config.load_config(cfg_path)
    cfg["a_hand_added_key"] = {"keep": "me"}
    from buddhi_review.wizard import write_config
    write_config(cfg, cfg_path)

    rc, _ = _confirm(
        cfg_path, reviewers={COPILOT},
        ss_answers={"GLOBAL default": 1, "Auto-merge default for": 0,
                    "Label-gated CI default for": 0,
                    "Does Copilot auto-review": 0})
    assert rc == 0
    cfg = _read(cfg_path)
    assert config.repo_entry(cfg, "acme/widgets") == {"active_reviewers": ["gemini"]}
    assert cfg["a_hand_added_key"] == {"keep": "me"}


# ── Parity with the reference wizard's confirm_repo_interactive ─────────────────────

def test_confirm_repo_interactive_full_parity(tmp_path):
    """One end-to-end pass mirroring the reference wizard: reviewer multiSelect +
    per-bot auto_on_open + global-default promotion + per-repo auto_merge + label-gated
    CI, all persisted under repos[<repo>] and read back through the F1 readers, plus
    the read-back rows the operator sees."""
    cfg_path = tmp_path / "config.yaml"
    rc, out = _confirm(
        cfg_path, reviewers={COPILOT, CODEX},
        ss_answers={"GLOBAL default": 0,                 # promote to global default
                    "Auto-merge default for": 1,         # auto-merge on
                    "Label-gated CI default for": 1,     # label-gated CI on …
                    "Confirm: enable label-gated CI": 1,  # … confirmed
                    # per-bot auto_on_open via the G3 labeled select (0=Yes, 1=No)
                    "Does Copilot auto-review": 0, "Does Codex auto-review": 1})
    assert rc == 0
    cfg = _read(cfg_path)
    # Per-repo persistence (every key via an F1 reader).
    assert config.active_reviewers(cfg, REPO) == ("copilot", "codex")
    assert config.auto_on_open(cfg, "copilot", REPO) is True
    assert config.auto_on_open(cfg, "codex", REPO) is False
    assert config.repo_entry(cfg, REPO)["auto_merge"] is True
    assert config.label_gated_ci(cfg, REPO) is True
    # Promotion established the global default too.
    assert config.has_global_default(cfg) is True
    # The operator-facing read-back rows.
    assert "Confirmed reviewers for octocat/Hello-World: copilot, codex" in out
    assert "Auto-merge for octocat/Hello-World: on" in out
    assert "Label-gated CI for octocat/Hello-World: on" in out
    assert "/review-pr Hello-World <pr-number>" in out
