"""Launch preflight gates + per-repo round-1 summon + the unconfigured-repo
round-table status.

Four behaviours, all console-only:

* ``enforce_repo_confirmation_gate`` — refuse to launch on a repo with no
  confirmed reviewer fleet AND no global default to fall back to (loud banner +
  ``✗ [auto]`` notice + ``exit(2)``); an unconfirmed repo WITH a global default
  proceeds on it with a ``⚠ [auto]`` fallback notice; a confirmed repo / no repo
  is a silent no-op. Bypass ``BUDDHI_ALLOW_UNCONFIRMED_REPO=1``.
* ``refuse_primary_checkout`` — refuse the repo's PRIMARY checkout while it sits
  on the PR head branch (fixers need a dedicated worktree). Bypass
  ``BUDDHI_ALLOW_PRIMARY_CHECKOUT=1``. Any uncertainty proceeds (never hard-block).
* round-1 summon consumes ``auto_on_open`` resolved PER-REPO.
* an idle reviewer on an unconfigured repo renders ``not configured (repo)``.

Neither gate touches any non-console channel — pinned by a source check.
"""
from __future__ import annotations

import inspect
import io
import subprocess
from contextlib import redirect_stdout

from buddhi_review import cli, commit_push, detectors, round_driver
from buddhi_review.round_driver import RoundDriver, _display_width

REPO = "octocat/Hello-World"


# ── recorders / fakes ────────────────────────────────────────────────────────
class NoticeRec:
    """Captures every automation_notice() call as (action, status, hint)."""

    def __init__(self):
        self.calls = []

    def __call__(self, action, detail="", *, status="do", hint=None, **kw):
        self.calls.append({"action": action, "detail": detail,
                           "status": status, "hint": hint})
        return f"{action} — {detail}"

    def statuses(self):
        return [c["status"] for c in self.calls]


class ExitRec:
    """A non-raising exit double so the gate's post-exit return is reachable."""

    def __init__(self):
        self.code = None

    def __call__(self, code):
        self.code = code


class GhRec:
    """Records every git/gh spawn the driver makes (returns success)."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, *, cwd=None, timeout=None):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def comment_bodies(self):
        out = []
        for c in self.calls:
            if c[:3] == ["gh", "pr", "comment"] and "--body" in c:
                out.append(c[c.index("--body") + 1])
        return out


def _make_run(*, worktrees=None, toplevel=None, head=None, current=None,
              fail=()):
    """A fake git/gh runner keyed on argv. ``fail`` is a set of step names
    ('worktree', 'toplevel', 'branch', 'head') that should return non-zero."""

    def cp(stdout="", rc=0):
        return subprocess.CompletedProcess(["x"], rc, stdout=stdout, stderr="")

    def run(argv, *, cwd=None, timeout=None):
        if argv[:3] == ["git", "worktree", "list"]:
            if "worktree" in fail:
                return cp(rc=1)
            return cp("".join(f"worktree {w}\n" for w in (worktrees or [])))
        if argv[:2] == ["git", "rev-parse"] and "--show-toplevel" in argv:
            return cp((toplevel + "\n") if toplevel else "", 1 if "toplevel" in fail or not toplevel else 0)
        if argv[:2] == ["git", "rev-parse"] and "--abbrev-ref" in argv:
            return cp((current + "\n") if current else "", 1 if "branch" in fail or not current else 0)
        if argv[:3] == ["gh", "pr", "view"]:
            return cp((head + "\n") if head else "", 1 if "head" in fail or not head else 0)
        return cp()

    return run


# ── enforce_repo_confirmation_gate ───────────────────────────────────────────
class TestRepoConfirmationGate:
    def test_confirmed_repo_is_silent_noop(self, monkeypatch):
        monkeypatch.delenv("BUDDHI_ALLOW_UNCONFIRMED_REPO", raising=False)
        cfg = {"repos": {"octocat/hello-world": {"active_reviewers": ["claude"]}}}
        rec, ex = NoticeRec(), ExitRec()
        buf = io.StringIO()
        with redirect_stdout(buf):
            round_driver.enforce_repo_confirmation_gate(
                REPO, cfg, exit_fn=ex, notice=rec)
        assert ex.code is None
        assert rec.calls == []
        assert buf.getvalue() == ""

    def test_unconfirmed_with_global_default_falls_back(self, monkeypatch):
        monkeypatch.delenv("BUDDHI_ALLOW_UNCONFIRMED_REPO", raising=False)
        cfg = {"active_reviewers": ["copilot", "claude"]}  # global default, no repos entry
        rec, ex = NoticeRec(), ExitRec()
        round_driver.enforce_repo_confirmation_gate(REPO, cfg, exit_fn=ex, notice=rec)
        assert ex.code is None                       # not refused
        assert rec.statuses() == ["fallback"]        # one ⚠ fallback notice
        c = rec.calls[0]
        assert c["action"] == "repo-config fallback"
        assert "the global default" in c["detail"]
        assert "copilot, claude" in c["detail"]      # names the fallback fleet

    def test_unconfirmed_no_default_refuses_and_exits(self, monkeypatch):
        monkeypatch.delenv("BUDDHI_ALLOW_UNCONFIRMED_REPO", raising=False)
        monkeypatch.setenv("NO_COLOR", "1")
        rec, ex = NoticeRec(), ExitRec()
        buf = io.StringIO()
        with redirect_stdout(buf):
            round_driver.enforce_repo_confirmation_gate(REPO, {}, exit_fn=ex,
                                                        notice=rec)
        out = buf.getvalue()
        assert ex.code == 2                          # sys.exit(2) equivalent
        assert "REPO NOT CONFIRMED" in out and REPO in out
        assert rec.statuses() == ["stop"]            # ✗ stop notice, no fallback after
        assert rec.calls[0]["action"] == "repo-config gate"
        assert "BUDDHI_ALLOW_UNCONFIRMED_REPO" in (rec.calls[0]["hint"] or "")

    def test_bypass_runs_on_builtin_defaults(self, monkeypatch):
        monkeypatch.setenv("BUDDHI_ALLOW_UNCONFIRMED_REPO", "1")
        rec, ex = NoticeRec(), ExitRec()
        round_driver.enforce_repo_confirmation_gate(REPO, {}, exit_fn=ex, notice=rec)
        assert ex.code is None                       # bypass → not refused
        assert rec.statuses() == ["fallback"]
        assert "built-in defaults" in rec.calls[0]["detail"]

    def test_repo_none_is_noop(self):
        rec, ex = NoticeRec(), ExitRec()
        round_driver.enforce_repo_confirmation_gate(None, {}, exit_fn=ex, notice=rec)
        assert ex.code is None and rec.calls == []

    def test_probe_error_fails_open(self, monkeypatch):
        # A resolver that raises must NOT crash the gate (it returns, no exit).
        monkeypatch.setattr(round_driver, "repo_entry",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        rec, ex = NoticeRec(), ExitRec()
        round_driver.enforce_repo_confirmation_gate(REPO, {}, exit_fn=ex, notice=rec)
        assert ex.code is None and rec.calls == []


# ── refuse_primary_checkout ──────────────────────────────────────────────────
class TestRefusePrimaryCheckout:
    def test_linked_worktree_proceeds(self, monkeypatch):
        monkeypatch.delenv("BUDDHI_ALLOW_PRIMARY_CHECKOUT", raising=False)
        run = _make_run(worktrees=["/repo"], toplevel="/repo/.claude/worktrees/wt",
                        head="feat/x", current="feat/x")
        rec = NoticeRec()
        assert round_driver.refuse_primary_checkout(
            "7", "o/r", "/repo/.claude/worktrees/wt", run=run, notice=rec) is None
        assert rec.calls == []

    def test_primary_on_pr_branch_refused(self, monkeypatch):
        monkeypatch.delenv("BUDDHI_ALLOW_PRIMARY_CHECKOUT", raising=False)
        monkeypatch.setenv("NO_COLOR", "1")
        run = _make_run(worktrees=["/repo"], toplevel="/repo",
                        head="feat/x", current="feat/x")
        rec = NoticeRec()
        buf = io.StringIO()
        with redirect_stdout(buf):
            reason = round_driver.refuse_primary_checkout(
                "7", "o/r", "/repo", run=run, notice=rec)
        assert reason and "PRIMARY checkout" in reason
        assert "BUDDHI_ALLOW_PRIMARY_CHECKOUT=1" in reason
        assert "PRIMARY CHECKOUT" in buf.getvalue()
        assert rec.statuses() == ["stop"]

    def test_primary_but_off_pr_branch_proceeds(self, monkeypatch):
        # cwd IS the primary checkout but sits on a different branch than the PR
        # head → not the dangerous case → proceed.
        monkeypatch.delenv("BUDDHI_ALLOW_PRIMARY_CHECKOUT", raising=False)
        run = _make_run(worktrees=["/repo"], toplevel="/repo",
                        head="feat/x", current="main")
        rec = NoticeRec()
        assert round_driver.refuse_primary_checkout(
            "7", "o/r", "/repo", run=run, notice=rec) is None
        assert rec.calls == []

    def test_unknown_head_proceeds(self, monkeypatch):
        # gh pr view fails (offline / no PR) → head unknown → proceed.
        monkeypatch.delenv("BUDDHI_ALLOW_PRIMARY_CHECKOUT", raising=False)
        run = _make_run(worktrees=["/repo"], toplevel="/repo",
                        head=None, current="feat/x")
        assert round_driver.refuse_primary_checkout(
            "7", "o/r", "/repo", run=run) is None

    def test_worktree_probe_error_proceeds(self, monkeypatch):
        monkeypatch.delenv("BUDDHI_ALLOW_PRIMARY_CHECKOUT", raising=False)
        run = _make_run(fail={"worktree"})
        assert round_driver.refuse_primary_checkout(
            "7", "o/r", "/repo", run=run) is None

    def test_detached_head_proceeds(self, monkeypatch):
        # Primary checkout, head known, but the branch probe yields nothing
        # (detached HEAD) → the docstring promises fail-open → proceed.
        monkeypatch.delenv("BUDDHI_ALLOW_PRIMARY_CHECKOUT", raising=False)
        run = _make_run(worktrees=["/repo"], toplevel="/repo",
                        head="feat/x", current=None)
        assert round_driver.refuse_primary_checkout(
            "7", "o/r", "/repo", run=run) is None

    def test_env_bypass_proceeds(self, monkeypatch):
        monkeypatch.setenv("BUDDHI_ALLOW_PRIMARY_CHECKOUT", "1")
        # Even a clear primary-on-head case must proceed; run must not be consulted.
        def boom(*a, **k):
            raise AssertionError("bypass set → must not probe git")
        assert round_driver.refuse_primary_checkout(
            "7", "o/r", "/repo", run=boom) is None


class TestIsPrimaryCheckoutRealGit:
    """Network-free real-git temp-repo coverage for the primary detector."""

    @staticmethod
    def _git(args, cwd):
        subprocess.run(["git", *args], cwd=str(cwd), check=True,
                       capture_output=True, text=True)

    def _make_repo(self, p):
        p.mkdir(parents=True, exist_ok=True)
        self._git(["init", "-b", "main"], p)
        self._git(["config", "user.email", "t@t"], p)
        self._git(["config", "user.name", "t"], p)
        (p / "f.txt").write_text("x\n")
        self._git(["add", "-A"], p)
        self._git(["commit", "-m", "init"], p)

    def test_primary_linked_and_plain(self, tmp_path):
        run = commit_push._default_run
        repo = tmp_path / "repo"
        self._make_repo(repo)
        assert round_driver._is_primary_checkout(str(repo), run) is True
        # A subdirectory of the primary checkout still resolves as primary
        # (toplevel comparison, not a raw path compare) — a loop launched from a
        # nested cwd in a primary-on-PR-branch must still be refused.
        sub = repo / "pkg"
        sub.mkdir()
        assert round_driver._is_primary_checkout(str(sub), run) is True
        wt = tmp_path / "wt"
        self._git(["worktree", "add", "-b", "feat/x", str(wt)], repo)
        assert round_driver._is_primary_checkout(str(wt), run) is False
        plain = tmp_path / "plain"
        plain.mkdir()
        assert round_driver._is_primary_checkout(str(plain), run) is False


# ── console-only invariant ───────────────────────────────────────────────────
def test_gates_emit_to_the_console_only():
    # Both gates speak only through stdout (print) and the injected notice
    # callback — never stderr or any other stream.
    for fn in (round_driver.enforce_repo_confirmation_gate,
               round_driver.refuse_primary_checkout,
               round_driver._print_refusal_banner):
        src = inspect.getsource(fn)
        assert "stderr" not in src
        assert "file=" not in src


# ── refusal marker reaches stdout (the launcher's cross-process grep contract) ──
# launch-review.sh greps the detached run-loop's log for
# round_driver.REFUSED_TO_LAUNCH_MARKER. These pin that the PRODUCTION emit path
# (the default automation_notice → stdout, captured into the log) actually carries
# the marker for BOTH gates — so the launcher's poll can find it.
class TestRefusalMarkerReachesStdout:
    def test_marker_constant_matches_launcher_grep_literal(self):
        # The launcher greps the bare phrase; the constant is its single source.
        assert round_driver.REFUSED_TO_LAUNCH_MARKER == "refused to launch"

    def test_primary_checkout_refusal_emits_marker_via_default_notice(self, monkeypatch):
        monkeypatch.delenv("BUDDHI_ALLOW_PRIMARY_CHECKOUT", raising=False)
        monkeypatch.setenv("NO_COLOR", "1")
        run = _make_run(worktrees=["/repo"], toplevel="/repo",
                        head="feat/x", current="feat/x")
        buf = io.StringIO()
        with redirect_stdout(buf):  # default notice = the real automation_notice
            round_driver.refuse_primary_checkout("7", "o/r", "/repo", run=run)
        assert round_driver.REFUSED_TO_LAUNCH_MARKER in buf.getvalue()

    def test_repo_confirmation_refusal_emits_marker_via_default_notice(self, monkeypatch):
        monkeypatch.delenv("BUDDHI_ALLOW_UNCONFIRMED_REPO", raising=False)
        monkeypatch.setenv("NO_COLOR", "1")
        ex = ExitRec()
        buf = io.StringIO()
        with redirect_stdout(buf):  # default notice = the real automation_notice
            round_driver.enforce_repo_confirmation_gate(REPO, {}, exit_fn=ex)
        assert ex.code == 2
        assert round_driver.REFUSED_TO_LAUNCH_MARKER in buf.getvalue()


def test_banner_emits_colour_when_enabled(monkeypatch):
    monkeypatch.setattr(round_driver, "_colour_enabled", lambda s: True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        round_driver._print_refusal_banner("TITLE", "body text here")
    out = buf.getvalue()
    assert "\033[31m" in out and "TITLE" in out and "body text here" in out


# ── cli wiring of the two gates into the detached engine (run-loop) ──────────
# The gates live in the in-process engine `_run_loop` (what launch-review.sh
# detaches); the `review-pr` front door just routes to the backend dispatcher.
def _review_args(**over):
    a = cli.build_parser().parse_args(["run-loop", "7", "--repo", "o/r", "--cwd", "/x"])
    for k, v in over.items():
        setattr(a, k, v)
    return a


class _FakeOutcome:
    status = "clean"
    rounds = 1
    merged = False


class TestCliGateWiring:
    def test_primary_refusal_returns_2_and_skips_the_loop(self, monkeypatch):
        seen = {}

        def fake_refuse(pr, repo, cwd, **k):
            seen["args"] = (pr, repo, cwd)
            return "REFUSED"   # truthy reason → caller aborts with rc 2

        monkeypatch.setattr(cli.round_driver, "refuse_primary_checkout", fake_refuse)
        monkeypatch.setattr(cli.round_driver, "enforce_repo_confirmation_gate",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not gate after a refusal")))
        monkeypatch.setattr(cli.round_driver, "RoundDriver",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not build the driver after a refusal")))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli._run_loop(_review_args())
        assert rc == 2
        assert seen["args"] == ("7", "o/r", "/x")   # (pr, repo, cwd), in that order

    def test_proceed_calls_both_gates_in_order_then_runs(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cli.round_driver, "refuse_primary_checkout",
                            lambda pr, repo, cwd, **k: calls.append(("refuse", pr, repo, cwd)) or None)
        monkeypatch.setattr(cli.round_driver, "enforce_repo_confirmation_gate",
                            lambda repo, cfg, **k: calls.append(("enforce", repo)))

        class FakeDriver:
            def __init__(self, *a, **k):
                pass

            def run(self):
                return _FakeOutcome()

        monkeypatch.setattr(cli.round_driver, "RoundDriver", FakeDriver)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli._run_loop(_review_args())
        assert rc == 0
        # the primary-checkout gate runs FIRST, then the repo-confirmation gate
        assert [c[0] for c in calls] == ["refuse", "enforce"]
        assert calls[0] == ("refuse", "7", "o/r", "/x")
        assert calls[1] == ("enforce", "o/r")


# ── per-repo round-1 summon ──────────────────────────────────────────────────
def _driver(cfg, *, repo=REPO, gh=None):
    rec = gh or GhRec()
    d = RoundDriver("7", repo=repo, cwd="/x", cfg=cfg, gh_run=rec,
                    classify_runner=lambda p: "{}", clean_llm=None)
    return d, rec


class TestPerRepoRound1Summon:
    def test_round1_consumes_per_repo_auto_on_open(self):
        # Global says claude is mention-driven (would summon); the per-repo block
        # says it auto-reviews on open (must NOT be summoned in round 1).
        cfg = {
            "auto_on_open": {"claude": False},
            "repos": {"octocat/hello-world": {
                "active_reviewers": ["claude"],
                "auto_on_open": {"claude": True}}},
        }
        d, rec = _driver(cfg)
        d._summon(1, ["claude"])
        assert rec.comment_bodies() == []            # per-repo True → not summoned

    def test_round1_other_repo_falls_back_to_global(self):
        cfg = {
            "auto_on_open": {"claude": False},
            "repos": {"octocat/hello-world": {
                "active_reviewers": ["claude"],
                "auto_on_open": {"claude": True}}},
        }
        d, rec = _driver(cfg, repo="other/repo")     # no per-repo entry
        d._summon(1, ["claude"])
        assert "@claude review" in rec.comment_bodies()  # global False → summoned

    def test_rounds_two_plus_resummon_even_auto_on_open(self):
        cfg = {"repos": {"octocat/hello-world": {
            "active_reviewers": ["claude"],
            "auto_on_open": {"claude": True}}}}
        d, rec = _driver(cfg)
        d._summon(2, ["claude"])                     # rounds ≥2 re-request all
        assert "@claude review" in rec.comment_bodies()


# ── per-reviewer repo-availability round-table status ────────────────────────
def _gh_claude(present, *, error=False):
    """A gh_run fake for the Claude-workflow presence probe.

    ``present=True``  → the Contents-API GET returns a non-empty (base64) body;
    ``present=False`` → a 404 (workflow absent on the default branch);
    ``error=True``    → the runner raises (the fail-closed path). Every non-probe
    gh/git call returns a plain success so an unrelated driver spawn is a no-op."""
    def run(argv, *, cwd=None, timeout=None):
        if error:
            raise OSError("gh unavailable")
        is_contents = argv[:2] == ["gh", "api"] and any("contents/" in a for a in argv)
        if is_contents:
            if present:
                return subprocess.CompletedProcess(argv, 0, stdout="YWJjZA==\n", stderr="")
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Not Found")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    return run


def _status_driver(cfg, *, repo=REPO, gh=None, notice=None):
    kw = {}
    if gh is not None:
        kw["gh_run"] = gh
    if notice is not None:
        kw["notice"] = notice
    return RoundDriver("7", repo=repo, cwd="/x", cfg=cfg,
                       classify_runner=lambda p: "{}", clean_llm=None, **kw)


class TestUnconfiguredRepoStatus:
    # Badge a reviewer that posted no review by what the loop can RELIABLY detect
    # with its user token. Only Claude is detectable (a Contents-API GET of its
    # workflow), so an ABSENT claude-code-review.yml renders "Not configured (repo)
    # 🔧". Copilot/Codex/Gemini are never per-reviewer-detectable, so their silence
    # is honest — "No review posted 🔇", NEVER a "Not configured" the loop can't
    # verify. The probe runs once before round 1 (_populate_repo_gate).

    def test_absent_claude_workflow_excludes_claude(self):
        # (a) absent workflow → Claude "Not configured (repo) 🔧".
        d = _status_driver({}, repo="o/r", gh=_gh_claude(present=False))
        assert d._repo_unconfigured is True
        d._populate_repo_gate()
        assert "claude" in d._repo_gate_excluded
        assert d._bot_status_text("claude") == "Not configured (repo) 🔧"

    def test_idle_bot_on_unconfigured_repo(self):
        # Same as above via the render path: an idle Claude on an unregistered
        # repo whose workflow is absent reads the distinct repo-gate badge.
        d = _status_driver({}, repo="o/r", gh=_gh_claude(present=False))
        assert d._repo_unconfigured is True
        d._populate_repo_gate()
        assert d._bot_status_text("claude") == "Not configured (repo) 🔧"

    def test_present_claude_workflow_does_not_exclude_claude(self):
        # (b) present workflow → Claude NOT excluded → honest silence, never the
        # repo-gate badge, even on an otherwise-unregistered repo.
        d = _status_driver({}, repo="o/r", gh=_gh_claude(present=True))
        d._populate_repo_gate()
        assert "claude" not in d._repo_gate_excluded
        assert d._bot_status_text("claude") == "No review posted 🔇"

    def test_probe_gh_error_fails_closed_excludes_claude(self):
        # (c) probe gh-error → fail-closed: an unverifiable Claude is treated as
        # absent (excluded), never assumed present.
        d = _status_driver({}, repo="o/r", gh=_gh_claude(present=True, error=True))
        d._populate_repo_gate()
        assert "claude" in d._repo_gate_excluded
        assert d._bot_status_text("claude") == "Not configured (repo) 🔧"

    def test_unregistered_repo_non_claude_reviewers_render_honest_silence(self):
        # (d) On a totally-unregistered repo, Copilot/Codex/Gemini render honest
        # silence — NOT "Not configured" — because none of the three is
        # per-reviewer-detectable with the loop's user token. Only Claude (absent)
        # earns the repo-gate badge. The one-time operator notice fires on
        # construction.
        rec = NoticeRec()
        d = _status_driver({}, repo="o/r", gh=_gh_claude(present=False), notice=rec)
        d._populate_repo_gate()
        for bot in ("copilot", "codex", "gemini"):
            assert bot not in d._repo_gate_excluded
            assert d._bot_status_text(bot) == "No review posted 🔇"
            assert d._bot_status_text(bot) != round_driver._STATUS_NOT_CONFIGURED
        assert d._bot_status_text("claude") == "Not configured (repo) 🔧"
        # the one-time operator cue fired once on driver construction
        assert [c["action"] for c in rec.calls] == ["repo not registered"]
        assert rec.statuses() == ["fallback"]

    def test_probe_is_monotonic_for_the_run(self):
        # The gate is populated once and never re-evaluated: a second probe call
        # (even with a now-"present" runner) does not clear the exclusion.
        d = _status_driver({}, repo="o/r", gh=_gh_claude(present=False))
        d._populate_repo_gate()
        assert d._repo_gate_excluded == {"claude"}
        d.gh_run = _gh_claude(present=True)           # workflow "appears" mid-run
        d._populate_repo_gate()                        # no-op — already probed
        assert d._repo_gate_excluded == {"claude"}

    def test_global_default_present_is_no_review_posted(self):
        # A configured repo (global default) is not repo-unconfigured; with the
        # probe unrun, Claude is not excluded → honest silence.
        d = _status_driver({"active_reviewers": ["claude"]}, repo="o/r")
        assert d._repo_unconfigured is False
        assert d._bot_status_text("claude") == "No review posted 🔇"

    def test_confirmed_repo_is_no_review_posted(self):
        cfg = {"repos": {"o/r": {"active_reviewers": ["claude"]}}}
        d = _status_driver(cfg, repo="o/r")
        assert d._repo_unconfigured is False
        assert d._bot_status_text("claude") == "No review posted 🔇"

    def test_repo_none_is_not_unconfigured(self):
        d = _status_driver({}, repo=None)
        assert d._repo_unconfigured is False
        assert d._bot_status_text("claude") == "No review posted 🔇"

    def test_repo_none_present_workflow_is_honest_silence(self):
        # repo=None is a supported loop mode (gh infers {owner}/{repo} from cwd, as
        # the loop's other gh calls do). A PRESENT Claude workflow there reads
        # present → honest silence, NEVER a false "Not configured (repo)".
        d = _status_driver({}, repo=None, gh=_gh_claude(present=True))
        d._populate_repo_gate()
        assert "claude" not in d._repo_gate_excluded
        assert d._bot_status_text("claude") == "No review posted 🔇"

    def test_repo_none_absent_workflow_is_genuinely_detected(self):
        # repo=None + an absent workflow is genuinely detected via the placeholder
        # (gh 404) → the honest repo-gate badge, not a false silence.
        d = _status_driver({}, repo=None, gh=_gh_claude(present=False))
        d._populate_repo_gate()
        assert d._bot_status_text("claude") == "Not configured (repo) 🔧"

    def test_seen_bot_is_active_not_excluded(self):
        # Activity this round outranks the repo-gate: even an excluded Claude that
        # engaged reads "Active ✅".
        d = _status_driver({}, repo="o/r", gh=_gh_claude(present=False))
        d._populate_repo_gate()
        d._bot_state("claude").last_seen = 1.0
        assert d._bot_status_text("claude") == "Active ✅"

    def test_lifecycle_status_outranks_repo_gate(self):
        # A real terminal signal (quota) must win over the repo-gate badge —
        # the gate only displaces the idle "No review posted" label.
        d = _status_driver({}, repo="o/r", gh=_gh_claude(present=False))
        d._populate_repo_gate()
        d.store.exclude_quota("claude")
        d._bot_state("claude").signal = detectors.SIGNAL_QUOTA
        assert d._bot_status_text("claude") == "Quota exhausted ⚠️"

    def test_status_renders_in_table_and_box_stays_rectangular(self):
        d = _status_driver({}, repo="o/r", gh=_gh_claude(present=False))
        d._populate_repo_gate()                        # claude workflow absent
        buf = io.StringIO()
        with redirect_stdout(buf):
            d._render_round(1, [], [])
        out = buf.getvalue()
        # Full label, not truncated (the cell strips the decorative 🔧 emoji).
        assert "Not configured (repo)" in out
        box = [ln for ln in out.splitlines() if ln and ln[0] in "┌├└│"]
        widths = {_display_width(ln) for ln in box}
        assert len(widths) == 1, f"box not rectangular: {widths}"
