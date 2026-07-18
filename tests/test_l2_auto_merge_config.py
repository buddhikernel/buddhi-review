"""L2 — the review loop honors the wizard's per-repo ``auto_merge`` config.

Before L2 the ``--auto-merge`` flag defaulted to a concrete ``False``, so an UNSET
flag arrived as ``False`` and ``repos[<repo>].auto_merge`` was unreachable — every
operator's per-repo auto-merge was silently always off. L2 tri-states the flag
(``None`` = unset) and resolves the effective value with a strict, fail-closed
precedence:

    explicit --auto-merge / --no-auto-merge  >  repos[<repo>].auto_merge  >  off

There is deliberately NO global-default tier (the merge is opt-in per repo), so a
genuinely-unset run with no per-repo config never auto-merges. The resolution runs
at BOTH loop entry points — the ``review-pr`` front door (so the resolved bool
reaches a separately-installed PRO backend via the argv seam) and the ``run-loop``
engine (so a directly-invoked engine still honors the config).
"""
from __future__ import annotations

import pytest

from buddhi_review import backends, cli, config, open_pr

REPO = "octocat/Hello-World"


def _parse(argv):
    return cli.build_parser().parse_args(argv)


# ── config.auto_merge — the data-layer resolver (pure, no I/O) ─────────────────

def test_auto_merge_off_without_repo_or_config():
    assert config.auto_merge({}) is False
    assert config.auto_merge({}, None) is False
    assert config.auto_merge({}, REPO) is False  # no repos entry


def test_auto_merge_per_repo_true_and_false():
    assert config.auto_merge({"repos": {REPO: {"auto_merge": True}}}, REPO) is True
    assert config.auto_merge({"repos": {REPO: {"auto_merge": False}}}, REPO) is False


@pytest.mark.parametrize("bad", ["true", 1, 0, [], {}, None])
def test_auto_merge_malformed_per_repo_value_falls_closed_off(bad):
    # A non-bool per-repo value never turns the merge ON — it falls to off.
    # (``1``/``0`` are ints, not bools, so they are malformed here too.)
    assert config.auto_merge({"repos": {REPO: {"auto_merge": bad}}}, REPO) is False


def test_auto_merge_entry_without_key_is_off():
    # A confirmed repo (entry present) that carries no auto_merge key → off.
    cfg = {"repos": {REPO: {"active_reviewers": ["claude"]}}}
    assert config.auto_merge(cfg, REPO) is False


def test_auto_merge_has_no_global_tier():
    # A top-level ``auto_merge`` is NOT a global default — it is ignored entirely.
    # Only ``repos[<repo>].auto_merge`` can turn the merge on.
    cfg = {"auto_merge": True, "repos": {REPO: {"active_reviewers": ["claude"]}}}
    assert config.auto_merge(cfg, REPO) is False
    assert config.auto_merge(cfg, "other/repo") is False
    assert config.auto_merge(cfg, None) is False


def test_auto_merge_case_insensitive_repo():
    cfg = {"repos": {"octocat/hello-world": {"auto_merge": True}}}
    assert config.auto_merge(cfg, "OCTOCAT/Hello-World") is True


def test_auto_merge_unconfirmed_repo_does_not_borrow_another_repos_flag():
    cfg = {"repos": {REPO: {"auto_merge": True}}}
    assert config.auto_merge(cfg, "someone/else") is False  # per-repo, never leaks


# ── cli parser — the tri-state default ────────────────────────────────────────

@pytest.mark.parametrize("command", ["review-pr", "run-loop"])
def test_flag_is_tri_state(command):
    assert _parse([command, "7"]).auto_merge is None            # unset → None
    assert _parse([command, "7", "--auto-merge"]).auto_merge is True
    assert _parse([command, "7", "--no-auto-merge"]).auto_merge is False


def test_unset_default_is_not_false():
    # Regression guard for the exact L2 bug: an unset flag must be None (so config
    # is reachable), never a concrete False (which shadows the config forever).
    assert _parse(["review-pr", "7"]).auto_merge is not False


# ── _effective_auto_merge — the shared precedence resolver (the matrix) ────────

_ON_CFG = {"repos": {REPO: {"auto_merge": True}}}
_OFF_CFG = {"repos": {REPO: {"auto_merge": False}}}
_NO_CFG: dict = {}


def _args(*extra):
    return _parse(["review-pr", "7", "--repo", REPO, *extra])


def test_precedence_explicit_on_wins_over_any_config():
    for cfg in (_ON_CFG, _OFF_CFG, _NO_CFG):
        assert cli._effective_auto_merge(_args("--auto-merge"), cfg) is True


def test_precedence_explicit_off_wins_over_config_on():
    # The adversarial guard: an explicit --no-auto-merge is NEVER overridden by a
    # config that says on.
    assert cli._effective_auto_merge(_args("--no-auto-merge"), _ON_CFG) is False
    assert cli._effective_auto_merge(_args("--no-auto-merge"), _OFF_CFG) is False
    assert cli._effective_auto_merge(_args("--no-auto-merge"), _NO_CFG) is False


def test_precedence_unset_falls_back_to_per_repo_config():
    assert cli._effective_auto_merge(_args(), _ON_CFG) is True
    assert cli._effective_auto_merge(_args(), _OFF_CFG) is False


def test_precedence_unset_and_no_config_is_off_fail_closed():
    # The fail-closed floor: no flag, no per-repo config → never auto-merge.
    assert cli._effective_auto_merge(_args(), _NO_CFG) is False


def test_precedence_unset_repo_none_is_off():
    # A run with no --repo and no config resolves to off, never on.
    ns = _parse(["review-pr", "7"])
    assert cli._effective_auto_merge(ns, _ON_CFG) is False  # config keyed on a repo we didn't name
    assert cli._effective_auto_merge(ns, _NO_CFG) is False


def test_effective_always_returns_concrete_bool():
    # The resolver never returns None: the loop/back-ends receive a definite bool.
    for cfg in (_ON_CFG, _OFF_CFG, _NO_CFG):
        for extra in ((), ("--auto-merge",), ("--no-auto-merge",)):
            assert cli._effective_auto_merge(_args(*extra), cfg) in (True, False)


# ── review-pr front door — the resolved bool reaches the backend hand-off ──────

def _patch_front_door(monkeypatch, cfg, captured):
    """Silence the update banner, pin the config, and capture the auto_merge kwarg
    handed to launch_review_loop (the PRO-backend hand-off)."""
    monkeypatch.setattr(cli.update_banner, "maybe_emit_update_banner",
                        lambda **k: None)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)

    def _capture(pr, repo, cwd, **opts):
        captured["auto_merge"] = opts.get("auto_merge")
        return 0

    monkeypatch.setattr(cli, "launch_review_loop", _capture)


def test_review_pr_unset_resolves_config_on_to_true(monkeypatch):
    cap = {}
    _patch_front_door(monkeypatch, _ON_CFG, cap)
    assert cli._review_pr(_args()) == 0
    assert cap["auto_merge"] is True  # reaches the (PRO) backend as a resolved bool


def test_review_pr_unset_no_config_resolves_to_false(monkeypatch):
    cap = {}
    _patch_front_door(monkeypatch, _NO_CFG, cap)
    assert cli._review_pr(_args()) == 0
    assert cap["auto_merge"] is False  # fail-closed, still a concrete bool (not None)


def test_review_pr_explicit_off_overrides_config_on(monkeypatch):
    cap = {}
    _patch_front_door(monkeypatch, _ON_CFG, cap)
    assert cli._review_pr(_args("--no-auto-merge")) == 0
    assert cap["auto_merge"] is False


def test_review_pr_explicit_flag_skips_config_load(monkeypatch):
    # An explicit --auto-merge/--no-auto-merge never needs the config file, so
    # load_config() must not be called (no I/O, no missing-config warning).
    calls = []
    cap = {}
    _patch_front_door(monkeypatch, _ON_CFG, cap)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: calls.append(1) or _ON_CFG)
    assert cli._review_pr(_args("--no-auto-merge")) == 0
    assert not calls
    assert cap["auto_merge"] is False


def test_review_pr_no_repo_skips_config_load(monkeypatch):
    # An unset flag with no --repo can never match a per-repo config entry, so
    # load_config() must not be called either.
    calls = []
    cap = {}
    monkeypatch.setattr(cli.update_banner, "maybe_emit_update_banner", lambda **k: None)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: calls.append(1) or _ON_CFG)
    monkeypatch.setattr(cli, "launch_review_loop",
                        lambda pr, repo, cwd, **opts: cap.__setitem__("auto_merge", opts.get("auto_merge")) or 0)
    ns = _parse(["review-pr", "7"])
    assert cli._review_pr(ns) == 0
    assert not calls
    assert cap["auto_merge"] is False


# ── backends argv seam — the resolved value flows through, no stray flag ───────

def _run_front_door_capture_argv(monkeypatch, cfg, extra=()):
    """Drive _review_pr through the REAL FreeBackend launch and capture the argv
    forwarded to the detached run-loop (via _detached_run)."""
    rec = []
    monkeypatch.setattr(cli.update_banner, "maybe_emit_update_banner",
                        lambda **k: None)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(backends, "discover_backends",
                        lambda **k: [backends.FreeBackend()])
    monkeypatch.setattr(backends, "_detached_run",
                        lambda cmd, *a, **kw: rec.append(cmd))
    rc = cli._review_pr(_args(*extra))
    assert rc == 0 and rec, "the launcher argv was not captured"
    return rec[0]


def test_argv_seam_carries_resolved_on_no_stray_no_flag(monkeypatch):
    # unset flag + per-repo config on → the argv carries --auto-merge and NEVER a
    # stray --no-auto-merge (which would have silenced the config).
    argv = _run_front_door_capture_argv(monkeypatch, _ON_CFG)
    assert "--auto-merge" in argv
    assert "--no-auto-merge" not in argv


def test_argv_seam_unset_no_config_is_explicit_off(monkeypatch):
    # unset flag + no config → explicit --no-auto-merge in the argv (fail-closed:
    # the backend is told off, never left to guess).
    argv = _run_front_door_capture_argv(monkeypatch, _NO_CFG)
    assert "--no-auto-merge" in argv
    assert "--auto-merge" not in [t for t in argv if t == "--auto-merge"]


def test_loop_argv_omits_flag_only_on_genuine_none():
    # The seam itself still appends NOTHING for a genuinely-None opt (the belt the
    # front door no longer relies on, but which must not regress).
    assert "--auto-merge" not in backends._loop_argv("7", "o/r", "/x", {"auto_merge": None})
    assert "--no-auto-merge" not in backends._loop_argv("7", "o/r", "/x", {"auto_merge": None})
    assert "--auto-merge" in backends._loop_argv("7", "o/r", "/x", {"auto_merge": True})
    assert "--no-auto-merge" in backends._loop_argv("7", "o/r", "/x", {"auto_merge": False})


# ── run-loop engine — the resolved value reaches RoundDriver ───────────────────

class _FakeOutcome:
    status = "clean"
    rounds = 1
    merged = True


class _FakeDriver:
    last_auto_merge = None

    def __init__(self, *a, **kw):
        _FakeDriver.last_auto_merge = kw.get("auto_merge")

    def run(self):
        return _FakeOutcome()


def _patch_run_loop(monkeypatch, cfg):
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(cli, "ConsoleNotifier",
                        lambda *a, **k: type("N", (), {"startup_log": lambda self: None})())
    monkeypatch.setattr(cli.round_driver, "refuse_primary_checkout",
                        lambda *a, **k: False)
    monkeypatch.setattr(cli.round_driver, "enforce_repo_confirmation_gate",
                        lambda *a, **k: None)
    monkeypatch.setattr(cli.round_driver, "RoundDriver", _FakeDriver)
    monkeypatch.setattr(cli.upsell, "maybe_emit_run_end_nudge", lambda *a, **k: None)


def _run_loop_args(*extra):
    # --max-rounds 3 skips the gh diff-size fetch (no network in the test).
    return _parse(["run-loop", "7", "--repo", REPO, "--max-rounds", "3", *extra])


def test_run_loop_unset_resolves_config_on(monkeypatch):
    _patch_run_loop(monkeypatch, _ON_CFG)
    cli._run_loop(_run_loop_args())
    assert _FakeDriver.last_auto_merge is True


def test_run_loop_unset_no_config_is_off(monkeypatch):
    _patch_run_loop(monkeypatch, _NO_CFG)
    cli._run_loop(_run_loop_args())
    assert _FakeDriver.last_auto_merge is False


def test_run_loop_explicit_off_overrides_config_on(monkeypatch):
    _patch_run_loop(monkeypatch, _ON_CFG)
    cli._run_loop(_run_loop_args("--no-auto-merge"))
    assert _FakeDriver.last_auto_merge is False


def test_run_loop_explicit_on_with_no_config(monkeypatch):
    _patch_run_loop(monkeypatch, _NO_CFG)
    cli._run_loop(_run_loop_args("--auto-merge"))
    assert _FakeDriver.last_auto_merge is True


# ── open-pr's _dispatch_launch — the resolved bool reaches this hand-off too ────
# open-pr has no --auto-merge flag of its own, so there is no tri-state to
# layer on top here — just the repos[<repo>].auto_merge config lookup.

def _patch_dispatch_launch(monkeypatch, cfg, captured):
    # _dispatch_launch imports launch_review_loop locally from buddhi_review.backends
    # on every call, so the patch target is the backends module, not open_pr.
    monkeypatch.setattr(open_pr, "load_config", lambda *a, **k: cfg)

    def _capture(pr, repo, cwd, **opts):
        captured["auto_merge"] = opts.get("auto_merge")
        return 0

    monkeypatch.setattr(backends, "launch_review_loop", _capture)


def test_dispatch_launch_unset_resolves_config_on_to_true(monkeypatch):
    cap = {}
    _patch_dispatch_launch(monkeypatch, _ON_CFG, cap)
    open_pr._dispatch_launch("7", REPO, "/work", None)
    assert cap["auto_merge"] is True  # reaches the (PRO) backend as a resolved bool


def test_dispatch_launch_no_config_resolves_to_false(monkeypatch):
    cap = {}
    _patch_dispatch_launch(monkeypatch, _NO_CFG, cap)
    open_pr._dispatch_launch("7", REPO, "/work", None)
    assert cap["auto_merge"] is False  # fail-closed, still a concrete bool (not None)


def test_dispatch_launch_argv_seam_carries_resolved_on(monkeypatch):
    # Drive through the REAL FreeBackend launch and confirm the resolved bool
    # reaches the detached run-loop argv — not just the direct kwarg capture.
    rec = []
    monkeypatch.setattr(open_pr, "load_config", lambda *a, **k: _ON_CFG)
    monkeypatch.setattr(backends, "discover_backends",
                        lambda **k: [backends.FreeBackend()])
    monkeypatch.setattr(backends, "_detached_run",
                        lambda cmd, *a, **kw: rec.append(cmd))
    open_pr._dispatch_launch("7", REPO, "/work", None)
    assert rec, "the launcher argv was not captured"
    assert "--auto-merge" in rec[0]
    assert "--no-auto-merge" not in rec[0]
