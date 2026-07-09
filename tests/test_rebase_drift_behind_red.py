"""Regression tests for the behind+red rebase-drift catch-22.

The bug: the loop decides "does this PR need a rebase?" from GitHub's
single-valued ``mergeStateStatus``, via :func:`merge.check_pr_mergeable` (whose
``(ok, reason)`` the round driver routes on). ``mergeStateStatus`` can hold only
ONE value; when a PR is BOTH behind its base AND has a failing check, GitHub
reports UNSTABLE/BLOCKED (the red check), which MASKS the BEHIND state. So the
gate returned "checks failing: ..." (not "base branch ahead"),
``_premerge_gate_ok`` flagged it CI-red, and the branch was NEVER routed to the
exit-rebase path — even though rebasing onto the fixed base would clear the red.
Any PR that is simultaneously behind-base and red hit this catch-22 (e.g. a fix
lands on main to clear a CI failure; every open PR is now behind AND red, and
the loop refused to rebase them onto the fix).

The fix adds :func:`merge._branch_is_behind_base` — a GIT behind-count that is
independent of ``mergeStateStatus`` — and consults it inside
:func:`merge.check_pr_mergeable`'s FAILING branch so a behind+red PR reports the
DRIFT reason ("base branch ahead — needs rebase/update") instead of "checks
failing", which routes it to the existing exit-rebase path (the reason no longer
starts with "checks failing", so ``_premerge_gate_ok`` does not flag it CI-red).

This file pins three properties:

1. Behavioral — ``_branch_is_behind_base`` returns True only for a verified
   positive behind-count, and FAILS SAFE (returns False) on every git error
   (fetch/rev-list non-zero, missing git, timeout, non-int output), so an
   unverifiable behind-check never triggers a blind force-push.
2. End-to-end — ``check_pr_mergeable`` returns the DRIFT reason for a behind+red
   PR, "checks failing" for a red-but-current PR, and never runs the git
   behind-check for a green PR or a PR already reported BEHIND by GitHub.
3. Structural — the failing branch consults ``_branch_is_behind_base`` and can
   return the DRIFT reason BEFORE the plain "checks failing" return.
"""
from __future__ import annotations

import inspect
import json
import subprocess

from buddhi_review import merge

_DRIFT = "base branch ahead — needs rebase/update"


# ---- test doubles ----------------------------------------------------------

def _cp(returncode=0, stdout=""):
    """A CompletedProcess-like stand-in for a ``run`` seam result."""
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr="")


class _FakeRun:
    """Records calls and returns a queued CompletedProcess per git subcommand.

    ``raises`` (if set) is raised on the FIRST call, modelling a ``run`` seam
    that blows up (timeout / missing binary) before returning.
    """

    def __init__(self, fetch=None, revlist=None, raises=None):
        self.calls = []
        self._fetch = fetch if fetch is not None else _cp(0, "")
        self._revlist = revlist if revlist is not None else _cp(0, "0")
        self._raises = raises

    def __call__(self, argv, *, cwd=None, timeout=None):
        self.calls.append((list(argv), {"cwd": cwd}))
        if self._raises is not None:
            raise self._raises
        if "fetch" in argv:
            return self._fetch
        if "rev-list" in argv:
            return self._revlist
        raise AssertionError(f"unexpected git call: {argv}")


# ---- 1. Behavioral: _branch_is_behind_base ---------------------------------

class TestBranchIsBehindBase:
    def test_behind_count_positive_is_true(self):
        """A verified behind-count >= 1 is the only True case."""
        fake = _FakeRun(revlist=_cp(0, "3\n"))
        assert merge._branch_is_behind_base("main", cwd="/x", run=fake) is True

    def test_behind_count_zero_is_false(self):
        """Up-to-date (behind-count 0) is not drift."""
        fake = _FakeRun(revlist=_cp(0, "0\n"))
        assert merge._branch_is_behind_base("main", cwd="/x", run=fake) is False

    def test_fetch_nonzero_returns_false(self):
        """A failed fetch is unverifiable -> fail safe (no blind force-push)."""
        fake = _FakeRun(fetch=_cp(1, ""))
        assert merge._branch_is_behind_base("main", cwd="/x", run=fake) is False

    def test_revlist_nonzero_returns_false(self):
        """A failed rev-list is unverifiable -> fail safe."""
        fake = _FakeRun(revlist=_cp(1, ""))
        assert merge._branch_is_behind_base("main", cwd="/x", run=fake) is False

    def test_timeout_returns_false(self):
        """A git timeout is a transient failure -> fail safe."""
        fake = _FakeRun(raises=subprocess.TimeoutExpired(cmd=["git"], timeout=120))
        assert merge._branch_is_behind_base("main", cwd="/x", run=fake) is False

    def test_missing_git_returns_false(self):
        """Missing git binary (FileNotFoundError) -> fail safe."""
        fake = _FakeRun(raises=FileNotFoundError("git"))
        assert merge._branch_is_behind_base("main", cwd="/x", run=fake) is False

    def test_non_int_stdout_returns_false(self):
        """Garbage (non-int) rev-list output must not raise -> fail safe."""
        fake = _FakeRun(revlist=_cp(0, "not-a-number"))
        assert merge._branch_is_behind_base("main", cwd="/x", run=fake) is False

    def test_fetch_failure_short_circuits_before_revlist(self):
        """On a fetch failure the behind-count is never queried."""
        fake = _FakeRun(fetch=_cp(1, ""))
        merge._branch_is_behind_base("main", cwd="/x", run=fake)
        assert not any("rev-list" in argv for argv, _kw in fake.calls)

    def test_issues_expected_git_commands(self):
        """The signal is git-only: fetch origin <base>, then behind-count. The
        checkout is selected via the injected run seam's ``cwd`` (OSS pattern),
        not a ``git -C`` prefix."""
        fake = _FakeRun(revlist=_cp(0, "2"))
        merge._branch_is_behind_base("main", cwd="/repo", run=fake)
        argvs = [argv for argv, _kw in fake.calls]
        cwds = [kw["cwd"] for _argv, kw in fake.calls]
        assert argvs[0] == ["git", "fetch", "origin", "main"]
        assert argvs[1] == ["git", "rev-list", "--count", "HEAD..origin/main"]
        assert cwds == ["/repo", "/repo"]   # both git calls run in the worktree


# ---- 2. End-to-end: check_pr_mergeable routes behind+red to DRIFT -----------

def _gate_run(view_payload, *, behind="0", fetch_rc=0):
    """A fake ``run`` seam for :func:`merge.check_pr_mergeable`: answers the
    ``gh pr view`` query with ``view_payload``, ``git fetch`` with rc=``fetch_rc``,
    and ``git rev-list --count`` with ``behind`` on stdout. Records every argv on
    ``.calls`` so a test can assert whether the git behind-check even ran."""
    calls = []

    def run(argv, *, cwd=None, timeout=None):
        calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(view_payload), stderr="")
        if argv[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(argv, fetch_rc, stdout="", stderr="")
        if argv[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(argv, 0, stdout=str(behind), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    run.calls = calls
    return run


class TestCheckPrMergeableBehindRed:
    def test_behind_and_red_reports_drift_not_checks_failing(self):
        """The core fix: a PR that is BOTH behind base AND red reports the DRIFT
        reason (which routes to exit-rebase) rather than "checks failing" (which
        would flag it CI-red and never rebase)."""
        run = _gate_run(
            {"statusCheckRollup": [{"conclusion": "FAILURE", "name": "tests"}],
             "baseRefName": "main"}, behind="2")
        ok, reason = merge.check_pr_mergeable("7", cwd="/repo", run=run)
        assert ok is False
        assert reason == _DRIFT
        assert not reason.startswith("checks failing")   # routes to rebase, not CI-red
        assert ["git", "fetch", "origin", "main"] in run.calls  # behind-check ran

    def test_red_but_current_reports_checks_failing(self):
        """A genuinely-red PR that is NOT behind (behind-count 0) still reports
        "checks failing" — the git behind-check ran and found no drift."""
        run = _gate_run(
            {"statusCheckRollup": [{"conclusion": "FAILURE", "name": "tests"}],
             "baseRefName": "main"}, behind="0")
        ok, reason = merge.check_pr_mergeable("7", cwd="/repo", run=run)
        assert ok is False
        assert reason == "checks failing: tests"
        assert ["git", "fetch", "origin", "main"] in run.calls  # behind-check ran

    def test_behind_check_error_falls_through_to_checks_failing(self):
        """FAIL-SAFE: an unverifiable behind-check (git fetch non-zero) must NOT
        claim drift — it falls through to today's "checks failing", so no blind
        force-push happens on an unverifiable signal."""
        run = _gate_run(
            {"statusCheckRollup": [{"conclusion": "FAILURE", "name": "tests"}],
             "baseRefName": "main"}, fetch_rc=1)
        ok, reason = merge.check_pr_mergeable("7", cwd="/repo", run=run)
        assert ok is False
        assert reason == "checks failing: tests"

    def test_green_behind_pr_never_runs_the_behind_check(self):
        """behind + GREEN: check_pr_mergeable returns (True, "") before the
        failing branch, so the git behind-check is NEVER reached — a behind but
        green PR is left for GitHub to squash-merge (no rebase)."""
        run = _gate_run(
            {"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
             "statusCheckRollup": [], "isDraft": False, "baseRefName": "main"},
            behind="5")   # git WOULD say behind, but the check must not run
        ok, reason = merge.check_pr_mergeable("7", cwd="/repo", run=run)
        assert (ok, reason) == (True, "")
        assert not any(a[:2] == ["git", "fetch"] for a in run.calls)  # no behind-check

    def test_github_reported_behind_returns_drift_without_git(self):
        """No double-handling: an up-to-date-branch-protection repo where GitHub
        reports mergeStateStatus==BEHIND is caught EARLIER and already returns the
        drift reason — the new git behind-check is never reached."""
        run = _gate_run(
            {"mergeStateStatus": "BEHIND", "baseRefName": "main"}, behind="5")
        ok, reason = merge.check_pr_mergeable("7", cwd="/repo", run=run)
        assert (ok, reason) == (False, _DRIFT)
        assert not any(a[:2] == ["git", "fetch"] for a in run.calls)  # never reached


# ---- 3. Structural: the failing branch routes behind to DRIFT --------------

_GATE_SRC = inspect.getsource(merge.check_pr_mergeable)


def _failing_branch_source():
    start = _GATE_SRC.find("if failing:")
    assert start != -1, "failing branch not found in check_pr_mergeable"
    end = _GATE_SRC.find("if pending:", start)
    assert end != -1, "could not bound the failing branch (no following pending branch)"
    return _GATE_SRC[start:end]


class TestGateRoutesBehindRedToDrift:
    def test_behind_helper_exists_at_module_level(self):
        """Module-level so it can be unit-tested without an end-to-end mock."""
        assert hasattr(merge, "_branch_is_behind_base")
        assert callable(merge._branch_is_behind_base)

    def test_failing_branch_consults_behind_helper(self):
        """The failing branch must consult the CI-independent behind-check."""
        src = _failing_branch_source()
        assert "_branch_is_behind_base(" in src, (
            "check_pr_mergeable's failing branch must consult "
            "_branch_is_behind_base so a behind+red PR is routed to the "
            "exit-rebase path instead of reporting 'checks failing'."
        )

    def test_failing_branch_returns_drift_before_checks_failing(self):
        """A behind+red PR must report the DRIFT reason, which the exit-rebase
        path owns, BEFORE falling through to the plain 'checks failing' return."""
        src = _failing_branch_source()
        drift_pos = src.find(f'"{_DRIFT}"')
        checks_pos = src.find('f"checks failing:')
        assert drift_pos != -1, (
            f"failing branch must be able to return the drift reason '{_DRIFT}'"
        )
        assert checks_pos != -1, "failing branch must still carry the 'checks failing' return"
        assert drift_pos < checks_pos, (
            "the behind-base DRIFT return must precede the 'checks failing' "
            "return so a behind+red PR is rebased first (the exit-rebase path "
            "owns base-branch drift) rather than handed back as a plain failure."
        )
