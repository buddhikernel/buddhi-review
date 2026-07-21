"""F5 — the FREE pre-merge gate (SAFETY: never merge unreviewed + label-gated CI)
plus the console persistent-silent-reviewer warning.

Three bundled behaviors, all driven through the real :class:`RoundDriver` round
loop on a fake clock with no network (the proven ``test_round_driver`` harness is
reused). Plus direct unit coverage of ``merge``'s CI-verdict collapse + poll.

  1. SAFETY gate (parity of the reference loop's never-merge-unreviewed backstop):
       * a clean round where NO expected reviewer reviewed → no merge (hand back);
       * REGRESSION: a fleet that approves clean with zero comments → STILL merges;
       * a by-design empty fleet → quiet no-auto-merge (no alarm, no merge);
       * a quota/error PLACEHOLDER is a response, not a review → still blocks.
  2. Label-gated CI: when enabled, attach ``ready-for-ci`` + poll ``gh pr checks``
     to green BEFORE merge; red / never-settling / absent checks block the merge.
  3. Console silent-reviewer warning: a configured reviewer silent across the run
     → a loud console note pointing at the wizard/config; a reviewer that
     responded any round → no note.
"""
import json
import subprocess

import pytest

from buddhi_review import gh_ingest, merge, round_driver
from buddhi_review.loop import Comment
from test_round_driver import CLAUDE_ONLY, GhRecorder, label_runner, make_driver
from buddhi_review.actuators import FixDispatch
from buddhi_review.fix_apply import FixOutcome


# ---------------------------------------------------------------------------
# gh stubs for the label-gated CI path
# ---------------------------------------------------------------------------

class CiGh(GhRecorder):
    """A GhRecorder that answers ``gh pr checks --json …`` with a canned rows
    payload (everything else stays rc=0 / empty, like the base recorder)."""

    def __init__(self, checks_rows):
        super().__init__()
        self._rows = checks_rows

    def __call__(self, argv, *, cwd=None, timeout=None):
        self.calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "checks"]:
            payload = "" if self._rows is None else json.dumps(self._rows)
            return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")
        return self._reply(argv)


LABEL_CI = {"active_reviewers": ["claude"], "auto_on_open": {"claude": False},
            "label_gated_ci": True}


# ===========================================================================
# 1. SAFETY gate — never merge unreviewed
# ===========================================================================

def test_zero_review_round_does_not_merge():
    # A fresh PR whose configured reviewer never posts: the round is "clean" (no
    # actionable comments) but NOTHING reviewed it → the loop must REFUSE to merge
    # and hand back, even with --auto-merge on. (The buddhi-review PR #1 incident.)
    driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY, auto_merge=True)
    outcome = driver.run()
    assert outcome.status == "clean"
    assert outcome.merged is False                  # blocked
    assert gh.matching("gh", "merge", "--squash") == []  # never merged


def test_zero_review_block_is_loud(capsys):
    driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY, auto_merge=True)
    driver.run()
    out = capsys.readouterr().out
    assert "NOT MERGING" in out and "NO REVIEW" in out
    assert "claude" in out


def test_clean_approval_with_zero_comments_still_merges():
    # REGRESSION (the critical no-false-positive case): a fleet that approves
    # clean with ZERO actionable comments has genuinely reviewed → it STILL
    # merges. A clean approval IS a review.
    cfg = {"active_reviewers": ["claude", "copilot"],
           "auto_on_open": {"claude": False, "copilot": True}}
    timeline = [
        (0, Comment(id="a", text="No issues found.", source="claude[bot]")),
        (0, Comment(id="b", text="LGTM, no issues found.", source="copilot[bot]")),
    ]
    driver, clock, gh = make_driver(timeline, cfg=cfg, auto_merge=True)
    outcome = driver.run()
    assert outcome.status == "clean" and outcome.merged is True
    assert gh.matching("gh", "merge", "--squash")


def test_actionable_comment_counts_as_review_then_merges():
    # A substantive comment (fixed), then a clean round → the bot genuinely
    # reviewed → merge proceeds.
    timeline = [
        (0, Comment(id="a", text="this null check is missing", source="claude[bot]")),
        (90, Comment(id="b", text="No issues found.", source="claude[bot]")),
    ]
    fix: FixDispatch = lambda c, r: FixOutcome(status="applied")
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        auto_merge=True, answer_waiter=lambda esc, **k: {})
    outcome = driver.run()
    assert outcome.status == "clean" and outcome.merged is True
    assert gh.matching("gh", "merge", "--squash")


def test_by_design_empty_fleet_quiet_no_merge(capsys):
    # An explicit empty fleet ("no bots for this repo") → quiet no-auto-merge:
    # nothing is merged, and NO loud handback fires (there is nothing to gate on).
    cfg = {"active_reviewers": []}
    driver, clock, gh = make_driver([], cfg=cfg, auto_merge=True)
    outcome = driver.run()
    assert outcome.status == "clean" and outcome.merged is False
    assert gh.matching("gh", "merge", "--squash") == []
    out = capsys.readouterr().out
    assert "NOT MERGING" not in out      # no loud block
    assert "REVIEWER SILENT" not in out  # no silent-reviewer alarm either


def test_quota_placeholder_is_not_a_review_blocks_merge():
    # A bot whose ONLY output is a quota placeholder has RESPONDED but not
    # REVIEWED — the placeholder must not satisfy the safety gate, so the merge
    # is still blocked (P1 trap 2).
    timeline = [(0, Comment(id="a", text="Rate limit exceeded for this model.",
                            source="claude[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY, auto_merge=True)
    outcome = driver.run()
    assert driver.store.is_excluded("claude")
    assert outcome.merged is False
    assert gh.matching("gh", "merge", "--squash") == []


# ===========================================================================
# 2. Label-gated CI — attach + poll to green before merge
# ===========================================================================

def test_label_ci_attaches_polls_green_then_merges():
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    gh = CiGh([{"name": "tests", "bucket": "pass", "state": "SUCCESS"}])
    driver, clock, _ = make_driver(timeline, cfg=LABEL_CI, gh=gh, auto_merge=True)
    outcome = driver.run()
    assert outcome.merged is True
    assert gh.matching("--add-label", "ready-for-ci")   # label attached
    assert gh.matching("pr", "checks")                  # CI polled
    assert gh.matching("gh", "merge", "--squash")       # then merged


def test_label_ci_red_blocks_merge():
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    gh = CiGh([{"name": "tests", "bucket": "fail", "state": "FAILURE"}])
    driver, clock, _ = make_driver(timeline, cfg=LABEL_CI, gh=gh, auto_merge=True)
    outcome = driver.run()
    assert outcome.merged is False
    assert gh.matching("--add-label", "ready-for-ci")   # attached
    assert gh.matching("pr", "checks")                  # polled
    assert gh.matching("gh", "merge", "--squash") == []  # red → never merged


def test_label_ci_absent_checks_never_false_greens():
    # No checks ever register (empty rollup). The gate must NOT read absence as
    # green: it keeps polling (bounded) and ultimately blocks the merge.
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    gh = CiGh([])  # always empty
    driver, clock, _ = make_driver(timeline, cfg=LABEL_CI, gh=gh, auto_merge=True)
    outcome = driver.run()
    assert outcome.merged is False
    assert len(gh.matching("pr", "checks")) >= 2        # kept polling, didn't bail green
    assert gh.matching("gh", "merge", "--squash") == []


def test_label_ci_pending_then_green_settles_and_merges():
    # First poll pending, later polls green — the gate waits, then merges.
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]

    class FlakyGh(CiGh):
        def __init__(self):
            super().__init__(None)
            self._poll = 0
        def __call__(self, argv, *, cwd=None, timeout=None):
            if argv[:3] == ["gh", "pr", "checks"]:
                self._poll += 1
                rows = ([{"name": "tests", "bucket": "pending", "state": ""}]
                        if self._poll < 3
                        else [{"name": "tests", "bucket": "pass", "state": "SUCCESS"}])
                self.calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(rows), stderr="")
            return super().__call__(argv, cwd=cwd, timeout=timeout)

    gh = FlakyGh()
    driver, clock, _ = make_driver(timeline, cfg=LABEL_CI, gh=gh, auto_merge=True)
    outcome = driver.run()
    assert outcome.merged is True
    assert len(gh.matching("pr", "checks")) >= 3
    assert gh.matching("gh", "merge", "--squash")


def test_label_ci_off_skips_attach_and_merges_normally():
    # label_gated_ci default OFF → no label attach, no CI poll, normal merge.
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY, auto_merge=True)
    outcome = driver.run()
    assert outcome.merged is True
    assert gh.matching("--add-label", "ready-for-ci") == []
    assert gh.matching("pr", "checks") == []


# ===========================================================================
# 3. Thread-resolution gate — one gate ahead of BOTH pre-merge paths
# ===========================================================================

def test_thread_gate_blocks_before_label_ci_fork():
    # An unresolved human thread must stop the merge BEFORE the label-gated CI
    # path even runs — proving the one thread gate sits ahead of the CI fork.
    from test_round_driver import FakeThreads

    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    ft = FakeThreads([gh_ingest.ReviewThread(
        id="PRRT_human", is_resolved=False, root_comment_id="human99")])
    gh = CiGh([{"name": "tests", "bucket": "pass", "state": "SUCCESS"}])
    driver, clock, _ = make_driver(
        timeline, cfg=LABEL_CI, gh=gh, auto_merge=True,
        threads_fetch=ft.fetch, resolve_thread=ft.resolve)
    outcome = driver.run()
    assert outcome.merged is False
    assert gh.matching("gh", "merge", "--squash") == []      # never merged
    assert gh.matching("--add-label", "ready-for-ci") == []  # CI fork never entered
    assert gh.matching("pr", "checks") == []
    assert ft.resolved == []                                 # human thread untouched


def test_thread_gate_passes_then_label_ci_runs_and_merges():
    # With the thread gate satisfied (own thread resolved) AND CI green, both
    # pre-merge gates pass and the PR merges.
    from test_round_driver import FakeThreads

    timeline = [(0, Comment(id="a", text="this variable is unused",
                            source="claude[bot]", path="app.py"))]
    ft = FakeThreads([gh_ingest.ReviewThread(
        id="PRRT_1", is_resolved=False, root_comment_id="a")])
    gh = CiGh([{"name": "tests", "bucket": "pass", "state": "SUCCESS"}])
    driver, clock, _ = make_driver(
        timeline, cfg=LABEL_CI, gh=gh, auto_merge=True,
        threads_fetch=ft.fetch, resolve_thread=ft.resolve)
    outcome = driver.run()
    assert outcome.merged is True
    assert ft.resolved == ["PRRT_1"]                    # own thread resolved first
    assert gh.matching("--add-label", "ready-for-ci")   # then the CI fork ran
    assert gh.matching("gh", "merge", "--squash")       # then merged


# --- direct merge-module unit coverage of the verdict collapse + poll ----------

def test_ci_verdict_collapse():
    assert merge._ci_verdict(None) == "pending"
    assert merge._ci_verdict([]) == "pending"            # absent → never green
    assert merge._ci_verdict([{"bucket": "pass"}]) == "green"
    assert merge._ci_verdict([{"bucket": "fail"}]) == "red"
    assert merge._ci_verdict([{"bucket": "cancel"}]) == "red"
    # any failing check is red even alongside a pass / pending
    assert merge._ci_verdict([{"bucket": "pass"}, {"bucket": "fail"}]) == "red"
    assert merge._ci_verdict([{"bucket": "pass"}, {"bucket": "pending"}]) == "pending"
    # a NON-EMPTY all-skipped rollup IS green — CI ran, every job was intentionally
    # skipped; that must not wedge the merge forever. (An ABSENT rollup — [] above —
    # still stays 'pending' and never merges.)
    assert merge._ci_verdict([{"bucket": "skipping"}]) == "green"
    assert merge._ci_verdict([{"bucket": "skipping"}, {"bucket": "skipping"}]) == "green"
    # a skip beside a still-pending check is NOT green — a real check is in flight
    assert merge._ci_verdict([{"bucket": "skipping"}, {"bucket": "pending"}]) == "pending"
    # a skip beside a failure is red (fail wins)
    assert merge._ci_verdict([{"bucket": "skipping"}, {"bucket": "fail"}]) == "red"
    # a pass beside a skip IS green (the skip is neutral)
    assert merge._ci_verdict([{"bucket": "pass"}, {"bucket": "skipping"}]) == "green"
    # state fallback when bucket is missing
    assert merge._ci_verdict([{"state": "SUCCESS"}]) == "green"
    assert merge._ci_verdict([{"state": "FAILURE"}]) == "red"
    assert merge._ci_verdict([{"state": "SKIPPED"}]) == "green"   # state-only skip
    # a non-empty rollup of only unrecognizable rows never false-greens
    assert merge._ci_verdict([{"foo": "bar"}]) == "pending"


def test_wait_for_ci_green_blocks_when_label_attach_fails():
    # A failed `gh pr edit --add-label` means the label-gated workflow never
    # fires → the gate must block (return False), never merge blind.
    def run(argv, *, cwd=None, timeout=None):
        rc = 1 if argv[:3] == ["gh", "pr", "edit"] else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="boom")
    ok = merge.wait_for_ci_green("7", repo="o/r", run=run, sleep=lambda s: None,
                                 settle_secs=0, attempts=3, interval=0)
    assert ok is False


def test_wait_for_ci_green_timeout_is_bounded():
    # Perpetually-pending checks must terminate (no deadlock) and block.
    calls = {"n": 0}
    def run(argv, *, cwd=None, timeout=None):
        if argv[:3] == ["gh", "pr", "checks"]:
            calls["n"] += 1
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps([{"bucket": "pending"}]), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    ok = merge.wait_for_ci_green("7", repo="o/r", run=run, sleep=lambda s: None,
                                 settle_secs=0, attempts=5, interval=0)
    assert ok is False
    assert calls["n"] == 5   # polled exactly `attempts` times, then gave up


# ===========================================================================
# 3. Console persistent-silent-reviewer warning
# ===========================================================================

def test_auto_on_open_reviewer_silent_all_run_emits_loud_note(capsys):
    # An auto_on_open=true reviewer the loop never summons (it should review on
    # open) that never posts → loud note even though it was never @-mentioned.
    cfg = {"active_reviewers": ["gemini"], "auto_on_open": {"gemini": True}}
    driver, clock, gh = make_driver([], cfg=cfg)
    driver.run()
    out = capsys.readouterr().out
    assert "REVIEWER SILENT" in out
    assert "gemini" in out
    assert "wizard" in out or "active_reviewers" in out   # CTA → wizard/config


def test_summoned_reviewer_silent_emits_loud_note(capsys):
    # An auto_on_open=false reviewer the loop DID summon that never posts → note.
    driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
    driver.run()
    out = capsys.readouterr().out
    assert "REVIEWER SILENT" in out and "claude" in out


def test_reviewer_that_responded_any_round_gets_no_note(capsys):
    # A clean approval is a response → the reviewer is NOT flagged silent.
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY)
    driver.run()
    out = capsys.readouterr().out
    assert "REVIEWER SILENT" not in out


def test_quota_responder_gets_no_silent_note(capsys):
    # A reviewer that posted only a quota placeholder RESPONDED (it is excluded
    # with a known reason) → no "wastefully silent" note (it is not silent, and
    # the cause is a known one, not a setup gap).
    timeline = [(0, Comment(id="a", text="Rate limit exceeded.", source="claude[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY)
    driver.run()
    out = capsys.readouterr().out
    assert "REVIEWER SILENT" not in out


def test_mixed_fleet_one_silent_one_reviews_merges_and_notes_the_silent(capsys):
    # claude reviews clean (so the PR merges), gemini stays silent → gemini gets
    # a note, claude does not, and the merge proceeds (an expected reviewer DID
    # review).
    cfg = {"active_reviewers": ["claude", "gemini"],
           "auto_on_open": {"claude": False, "gemini": True}}
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=cfg, auto_merge=True)
    outcome = driver.run()
    out = capsys.readouterr().out
    assert outcome.merged is True                    # claude's review let it merge
    assert gh.matching("gh", "merge", "--squash")
    assert "REVIEWER SILENT" in out and "gemini" in out
    # claude reviewed → it must not be named in a silent banner
    assert "'claude' never responded" not in out


def test_failed_summon_does_not_flag_silent(capsys):
    # An auto_on_open=false reviewer whose summon FAILS (gh non-zero) was never
    # actually requested → it must NOT be flagged silent (the PR-300 trap: never
    # penalize a bot the loop could not summon).
    class SummonFailGh(GhRecorder):
        def __call__(self, argv, *, cwd=None, timeout=None):
            self.calls.append(list(argv))
            # fail the @claude review summon comment; everything else rc=0
            rc = 1 if (argv[:3] == ["gh", "pr", "comment"]) else 0
            return subprocess.CompletedProcess(argv, rc, stdout="", stderr="no perms")

    gh = SummonFailGh()
    driver, clock, _ = make_driver([], cfg=CLAUDE_ONLY, gh=gh)
    driver.run()
    out = capsys.readouterr().out
    assert "REVIEWER SILENT" not in out   # never requested → not penalized


# ===========================================================================
# 4. #41 — label-add transient-blip retry
# ===========================================================================

class _LabelEditGh(CiGh):
    """A CiGh whose ``gh pr edit --add-label`` returns non-zero for the first
    ``fail_first`` attempts, then rc=0. Records every edit call."""

    def __init__(self, checks_rows, fail_first):
        super().__init__(checks_rows)
        self._fail_first = fail_first
        self.edits = 0

    def __call__(self, argv, *, cwd=None, timeout=None):
        if argv[:3] == ["gh", "pr", "edit"]:
            self.edits += 1
            self.calls.append(list(argv))
            rc = 1 if self.edits <= self._fail_first else 0
            return subprocess.CompletedProcess(argv, rc, stdout="", stderr="blip")
        return super().__call__(argv, cwd=cwd, timeout=timeout)


def test_label_add_retries_transient_blip_then_greens_and_merges():
    # First two `gh pr edit --add-label` attempts fail (a transient blip), the
    # third succeeds → the label attaches, CI polls green, the PR merges.
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    gh = _LabelEditGh([{"name": "tests", "bucket": "pass", "state": "SUCCESS"}], fail_first=2)
    driver, clock, _ = make_driver(timeline, cfg=LABEL_CI, gh=gh, auto_merge=True)
    outcome = driver.run()
    assert outcome.merged is True
    assert gh.edits == 3                                 # retried past the two blips
    assert gh.matching("gh", "merge", "--squash")


def test_label_add_all_attempts_fail_blocks_merge():
    # Every add attempt fails → the gate gives up after exactly LABEL_ADD_ATTEMPTS
    # tries and blocks the merge (the label-gated workflow would never fire).
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    gh = _LabelEditGh([{"name": "tests", "bucket": "pass"}], fail_first=99)
    driver, clock, _ = make_driver(timeline, cfg=LABEL_CI, gh=gh, auto_merge=True)
    outcome = driver.run()
    assert outcome.merged is False
    assert gh.edits == merge.LABEL_ADD_ATTEMPTS           # bounded — exactly N tries
    assert gh.matching("gh", "merge", "--squash") == []


def test_attach_ready_for_ci_backoff_uses_injected_sleep():
    # The retry backoff goes through the injected sleep seam (fake clock), never a
    # real time.sleep — two failed attempts sleep 2s then 4s.
    slept = []
    calls = {"n": 0}
    def run(argv, *, cwd=None, timeout=None):
        if argv[:3] == ["gh", "pr", "edit"]:
            calls["n"] += 1
            rc = 0 if calls["n"] >= 3 else 1
            return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    ok = merge._attach_ready_for_ci("7", repo="o/r", cwd=None, run=run,
                                    sleep=slept.append)
    assert ok is True
    assert slept == [2.0, 4.0]   # backoff_s * attempt for the two failed tries


# ===========================================================================
# 5. #42 — a NON-EMPTY all-skipped rollup greens (label CI path)
# ===========================================================================

def test_label_ci_all_skipped_rollup_greens_and_merges():
    # CI ran and every job was intentionally skipped (path filters / conditional
    # matrix) → a non-empty all-skipped rollup counts as GREEN, so the merge
    # proceeds rather than waiting forever.
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    gh = CiGh([{"name": "tests", "bucket": "skipping", "state": "SKIPPED"},
               {"name": "lint", "bucket": "skipping", "state": "SKIPPED"}])
    driver, clock, _ = make_driver(timeline, cfg=LABEL_CI, gh=gh, auto_merge=True)
    outcome = driver.run()
    assert outcome.merged is True
    assert gh.matching("gh", "merge", "--squash")


# ===========================================================================
# 6. #43 / #44 — general pre-merge mergeability gate (NON-label auto-merge path)
# ===========================================================================

def _mergeable_gh(view_payload, *, checks_seq=None, view_seq=None):
    """A gh fake: answers `gh pr view --json mergeable,…` with ``view_payload``
    (or successive entries from ``view_seq`` if provided, last repeated), and
    (optionally) `gh pr checks` from ``checks_seq`` (one rows-list per poll,
    last repeated). Everything else rc=0/empty."""
    box = {"poll": 0, "view_poll": 0}

    def run(argv, *, cwd=None, timeout=None):
        joined = " ".join(argv)
        if argv[:3] == ["gh", "pr", "view"] and "mergeable" in joined:
            if view_seq is not None:
                i = min(box["view_poll"], len(view_seq) - 1)
                box["view_poll"] += 1
                payload = view_seq[i]
            else:
                payload = view_payload
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(payload), stderr="")
        if argv[:3] == ["gh", "pr", "checks"] and checks_seq is not None:
            i = min(box["poll"], len(checks_seq) - 1)
            box["poll"] += 1
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(checks_seq[i]), stderr="")
        return GhRecorder._reply(argv)
    return run


class _RecordingRun(GhRecorder):
    """Wrap a plain run() so `.matching` still works for merge-call assertions."""
    def __init__(self, run):
        super().__init__()
        self._run = run

    def __call__(self, argv, *, cwd=None, timeout=None):
        self.calls.append(list(argv))
        return self._run(argv, cwd=cwd, timeout=timeout)


def test_nonlabel_mergeable_pr_merges():
    # A clean-reviewed non-label PR that GitHub reports mergeable → merges.
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    gh = _RecordingRun(_mergeable_gh(
        {"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
         "statusCheckRollup": [], "isDraft": False}))
    driver, clock, _ = make_driver(timeline, cfg=CLAUDE_ONLY, gh=gh, auto_merge=True)
    outcome = driver.run()
    assert outcome.merged is True
    assert gh.matching("gh", "merge", "--squash")


def test_nonlabel_conflict_blocks_merge():
    # GitHub reports merge conflicts → the merge is blocked, the PR handed back.
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    gh = _RecordingRun(_mergeable_gh(
        {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY",
         "statusCheckRollup": [], "isDraft": False}))
    driver, clock, _ = make_driver(timeline, cfg=CLAUDE_ONLY, gh=gh, auto_merge=True)
    outcome = driver.run()
    assert outcome.merged is False
    assert gh.matching("gh", "merge", "--squash") == []


def test_nonlabel_failing_check_blocks_merge():
    # A failing rollup check (non-required, so mergeStateStatus stays CLEAN) still
    # blocks the merge — and flags _premerge_ci_red for the honest hand-back.
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    gh = _RecordingRun(_mergeable_gh(
        {"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
         "statusCheckRollup": [{"__typename": "CheckRun", "name": "tests",
                                "conclusion": "FAILURE"}], "isDraft": False}))
    driver, clock, _ = make_driver(timeline, cfg=CLAUDE_ONLY, gh=gh, auto_merge=True)
    outcome = driver.run()
    assert outcome.merged is False
    assert driver._premerge_ci_red is True
    assert gh.matching("gh", "merge", "--squash") == []


def test_nonlabel_pending_check_settles_green_then_merges():
    # check_pr_mergeable reports pending checks on the last fix push; the gate
    # waits them out (#44) and merges once CI settles green. The re-check after
    # settle uses a clean view so check_pr_mergeable also returns ok.
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    view_pending = {"mergeable": "UNKNOWN", "mergeStateStatus": "UNSTABLE",
                    "statusCheckRollup": [{"__typename": "CheckRun", "name": "tests",
                                           "status": "IN_PROGRESS", "conclusion": None}],
                    "isDraft": False}
    view_clean = {"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                  "statusCheckRollup": [], "isDraft": False}
    checks = [[{"name": "tests", "bucket": "pending", "state": ""}],
              [{"name": "tests", "bucket": "pending", "state": ""}],
              [{"name": "tests", "bucket": "pass", "state": "SUCCESS"}]]
    gh = _RecordingRun(_mergeable_gh(None, checks_seq=checks,
                                     view_seq=[view_pending, view_clean]))
    driver, clock, _ = make_driver(timeline, cfg=CLAUDE_ONLY, gh=gh, auto_merge=True)
    outcome = driver.run()
    assert outcome.merged is True
    assert len(gh.matching("pr", "checks")) >= 3     # waited out the pending polls
    assert gh.matching("gh", "merge", "--squash")


def test_nonlabel_pending_check_never_settles_blocks_merge():
    # Checks stay pending forever → the settle wait is bounded and blocks the merge.
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    view = {"mergeable": "UNKNOWN", "mergeStateStatus": "UNSTABLE",
            "statusCheckRollup": [{"__typename": "CheckRun", "name": "tests",
                                   "status": "IN_PROGRESS", "conclusion": None}],
            "isDraft": False}
    checks = [[{"name": "tests", "bucket": "pending", "state": ""}]]  # always pending
    gh = _RecordingRun(_mergeable_gh(view, checks_seq=checks))
    driver, clock, _ = make_driver(timeline, cfg=CLAUDE_ONLY, gh=gh, auto_merge=True)
    outcome = driver.run()
    assert outcome.merged is False
    assert gh.matching("gh", "merge", "--squash") == []


def test_nonlabel_pending_settles_green_but_blocked_blocks_merge():
    # Regression for the BLOCKED-after-settle hole: initial check_pr_mergeable sees
    # pending checks (BLOCKED is evaluated after pending in check_pr_mergeable, so
    # the initial call short-circuits before reaching the BLOCKED check). CI settles
    # green via wait_for_ci_settle, but the PR is still BLOCKED (missing required
    # reviews). The re-check after settle must detect BLOCKED and block the merge.
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    view_pending = {"mergeable": "UNKNOWN", "mergeStateStatus": "UNSTABLE",
                    "statusCheckRollup": [{"__typename": "CheckRun", "name": "tests",
                                           "status": "IN_PROGRESS", "conclusion": None}],
                    "isDraft": False}
    view_blocked = {"mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED",
                    "statusCheckRollup": [], "isDraft": False}
    checks = [[{"name": "tests", "bucket": "pass", "state": "SUCCESS"}]]
    gh = _RecordingRun(_mergeable_gh(None, checks_seq=checks,
                                     view_seq=[view_pending, view_blocked]))
    driver, clock, _ = make_driver(timeline, cfg=CLAUDE_ONLY, gh=gh, auto_merge=True)
    outcome = driver.run()
    assert outcome.merged is False
    assert gh.matching("gh", "merge", "--squash") == []


def test_nonlabel_gate_is_fail_soft_on_gh_error():
    # An errored/unreadable `gh pr view` must NOT block a mergeable-looking PR —
    # check_pr_mergeable returns mergeable, so the merge proceeds (gh pr merge
    # stays the authoritative final check).
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]

    class _ViewErrGh(GhRecorder):
        def __call__(self, argv, *, cwd=None, timeout=None):
            self.calls.append(list(argv))
            joined = " ".join(argv)
            if argv[:3] == ["gh", "pr", "view"] and "mergeable" in joined:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="gh boom")
            return self._reply(argv)

    gh = _ViewErrGh()
    driver, clock, _ = make_driver(timeline, cfg=CLAUDE_ONLY, gh=gh, auto_merge=True)
    outcome = driver.run()
    assert outcome.merged is True                    # never wedged on a transient blip
    assert gh.matching("gh", "merge", "--squash")


# --- direct merge-module unit coverage of check_pr_mergeable + wait_for_ci_settle -

def _view_run(payload):
    return lambda argv, *, cwd=None, timeout=None: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps(payload), stderr="")


def test_check_pr_mergeable_verdicts():
    assert merge.check_pr_mergeable("7", run=_view_run({})) == (True, "")
    assert merge.check_pr_mergeable("7", run=_view_run({"isDraft": True}))[0] is False
    ok, reason = merge.check_pr_mergeable("7", run=_view_run({"mergeable": "CONFLICTING"}))
    assert ok is False and "conflict" in reason
    ok, reason = merge.check_pr_mergeable("7", run=_view_run({"mergeStateStatus": "DIRTY"}))
    assert ok is False and "conflict" in reason
    ok, reason = merge.check_pr_mergeable("7", run=_view_run({"mergeStateStatus": "BEHIND"}))
    assert ok is False and "rebase" in reason
    ok, reason = merge.check_pr_mergeable("7", run=_view_run({"mergeStateStatus": "BLOCKED"}))
    assert ok is False and "protection" in reason
    ok, reason = merge.check_pr_mergeable(
        "7", run=_view_run({"statusCheckRollup": [{"conclusion": "FAILURE", "name": "t"}]}))
    assert ok is False and "failing" in reason and "t" in reason
    ok, reason = merge.check_pr_mergeable(
        "7", run=_view_run({"statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": None}]}))
    assert ok is False and merge.reason_is_pending_checks(reason)
    # all-skipped rollup is mergeable (matches the green verdict)
    assert merge.check_pr_mergeable(
        "7", run=_view_run({"statusCheckRollup": [{"conclusion": "SKIPPED", "name": "x"}]})) == (True, "")


def test_check_pr_mergeable_fail_soft():
    def boom(argv, *, cwd=None, timeout=None):
        raise OSError("gh gone")
    assert merge.check_pr_mergeable("7", run=boom) == (True, "")

    def rc1(argv, *, cwd=None, timeout=None):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="x")
    assert merge.check_pr_mergeable("7", run=rc1) == (True, "")

    def garbage(argv, *, cwd=None, timeout=None):
        return subprocess.CompletedProcess(argv, 0, stdout="not json", stderr="")
    assert merge.check_pr_mergeable("7", run=garbage) == (True, "")


def test_wait_for_ci_settle_green_red_and_bounded_timeout():
    def rows_gh(seq):
        box = {"i": 0}
        def run(argv, *, cwd=None, timeout=None):
            if argv[:3] == ["gh", "pr", "checks"]:
                i = min(box["i"], len(seq) - 1)
                box["i"] += 1
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(seq[i]), stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return run, box
    green = [{"bucket": "pass"}]
    pend = [{"bucket": "pending"}]
    red = [{"bucket": "fail"}]
    noticer = lambda *a, **k: ""  # noqa: E731
    run, _ = rows_gh([pend, pend, green])
    assert merge.wait_for_ci_settle("7", run=run, sleep=lambda s: None,
                                    notice=noticer, attempts=5, interval=0) is True
    run, _ = rows_gh([pend, red])
    assert merge.wait_for_ci_settle("7", run=run, sleep=lambda s: None,
                                    notice=noticer, attempts=5, interval=0) is False
    run, box = rows_gh([pend])   # perpetually pending
    assert merge.wait_for_ci_settle("7", run=run, sleep=lambda s: None,
                                    notice=noticer, attempts=4, interval=0) is False
    assert box["i"] == 4         # bounded — polled exactly `attempts` times


# ===========================================================================
# 7. #g9a — @claude-never-reviewed NOTIFICATION (never a block)
# ===========================================================================

class _ClaudeSummonFailGh(GhRecorder):
    """Fail ONLY the `@claude review` trigger comment; everything else rc=0."""
    def __call__(self, argv, *, cwd=None, timeout=None):
        self.calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "comment"] and any("@claude" in a for a in argv):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no perms")
        return self._reply(argv)


_CLAUDE_AND_COPILOT = {"active_reviewers": ["claude", "copilot"],
                       "auto_on_open": {"claude": False, "copilot": True}}


def test_claude_trigger_failed_but_other_reviewed_notifies_and_still_merges(capsys):
    # claude's @-summon fails (primary reviewer never sees the code), copilot
    # reviews clean → a LOUD notification fires, but the run still MERGES (the
    # generalized SAFETY gate is satisfied — a reviewer did review). #g9a is a
    # notification, never a block.
    timeline = [(0, Comment(id="cp", text="No issues found.", source="copilot[bot]"))]
    gh = _ClaudeSummonFailGh()
    driver, clock, _ = make_driver(timeline, cfg=_CLAUDE_AND_COPILOT, gh=gh, auto_merge=True)
    outcome = driver.run()
    out = capsys.readouterr().out
    assert driver._claude_trigger_failed is True
    assert "PRIMARY REVIEWER SKIPPED" in out and "claude" in out
    assert outcome.merged is True                    # notification, not a block
    assert gh.matching("gh", "merge", "--squash")


def test_no_claude_banner_when_claude_reviewed(capsys):
    # claude's summon lands and it reviews clean → no "primary reviewer skipped"
    # banner (the flag is cleared on a successful summon and claude is in
    # reviewed_ever anyway).
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY, auto_merge=True)
    driver.run()
    out = capsys.readouterr().out
    assert "PRIMARY REVIEWER SKIPPED" not in out
    assert driver._claude_trigger_failed is False


def test_no_claude_banner_when_nobody_reviewed(capsys):
    # claude's summon fails AND no other reviewer reviews → the SAFETY gate blocks
    # (case b), and the #g9a banner does NOT fire (nothing reviewed it — that is
    # the generalized block's job, not this notification).
    gh = _ClaudeSummonFailGh()
    driver, clock, _ = make_driver([], cfg=_CLAUDE_AND_COPILOT, gh=gh, auto_merge=True)
    outcome = driver.run()
    out = capsys.readouterr().out
    assert "PRIMARY REVIEWER SKIPPED" not in out    # no other reviewer → not this note
    assert "NOT MERGING" in out and "NO REVIEW" in out  # the SAFETY block instead
    assert outcome.merged is False


def test_safety_gate_still_blocks_case_b_unaffected_by_g9a():
    # Explicit regression: a non-empty fleet with ZERO real reviews still BLOCKS
    # the merge — the #g9a work must not weaken the never-merge-unreviewed gate.
    driver, clock, gh = make_driver([], cfg=_CLAUDE_AND_COPILOT, auto_merge=True)
    outcome = driver.run()
    assert driver._run_start_fleet == {"claude", "copilot"}
    assert not (driver._run_start_fleet & driver.reviewed_ever)   # nobody reviewed
    assert outcome.merged is False
    assert gh.matching("gh", "merge", "--squash") == []


# ===========================================================================
# 8. #g9b — interactive TTY merge prompt (auto-merge OFF)
# ===========================================================================

class _FakeStdin:
    def __init__(self, tty):
        self._tty = tty
    def isatty(self):
        return self._tty


def test_tty_prompt_yes_merges(monkeypatch):
    # auto-merge OFF + a terminal + "yes" → after the mergeability check, merge.
    monkeypatch.setattr(round_driver.sys, "stdin", _FakeStdin(True))
    monkeypatch.setattr("builtins.input", lambda *a, **k: "yes")
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY, auto_merge=False)
    outcome = driver.run()
    assert outcome.merged is True
    assert gh.matching("gh", "merge", "--squash")


def test_tty_prompt_no_does_not_merge(monkeypatch):
    monkeypatch.setattr(round_driver.sys, "stdin", _FakeStdin(True))
    monkeypatch.setattr("builtins.input", lambda *a, **k: "no")
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY, auto_merge=False)
    outcome = driver.run()
    assert outcome.merged is False
    assert gh.matching("gh", "merge", "--squash") == []


def test_headless_never_prompts_and_never_calls_input(monkeypatch):
    # stdin NOT a terminal → no prompt, and input() is never called (a nohup/CI run
    # must never block on a read). The PR is left open.
    monkeypatch.setattr(round_driver.sys, "stdin", _FakeStdin(False))
    def _boom(*a, **k):
        raise AssertionError("input() must not be called in a headless run")
    monkeypatch.setattr("builtins.input", _boom)
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY, auto_merge=False)
    outcome = driver.run()
    assert outcome.merged is False
    assert gh.matching("gh", "merge", "--squash") == []


def test_tty_prompt_blocked_when_not_mergeable(monkeypatch):
    # A terminal + "yes", but GitHub reports the PR non-mergeable → the merge is
    # NOT fired (the mergeability check gates the interactive path too).
    monkeypatch.setattr(round_driver.sys, "stdin", _FakeStdin(True))
    monkeypatch.setattr("builtins.input", lambda *a, **k: "yes")
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    gh = _RecordingRun(_mergeable_gh(
        {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY", "isDraft": False}))
    driver, clock, _ = make_driver(timeline, cfg=CLAUDE_ONLY, gh=gh, auto_merge=False)
    outcome = driver.run()
    assert outcome.merged is False
    assert gh.matching("gh", "merge", "--squash") == []


def test_tty_prompt_never_offers_merge_of_an_unreviewed_pr(monkeypatch):
    # The never-merge-unreviewed backstop applies to the interactive path too: a
    # configured fleet that stayed silent must NOT be offered an interactive merge,
    # even attended and even on a would-be "yes" — input() is never called and the
    # PR is left open (the old auto-merge-OFF behavior).
    monkeypatch.setattr(round_driver.sys, "stdin", _FakeStdin(True))
    def _boom(*a, **k):
        raise AssertionError("must not prompt to merge an unreviewed PR")
    monkeypatch.setattr("builtins.input", _boom)
    # claude is configured but never reviews (no comment on the timeline) → the
    # round is clean with zero real reviews (case b).
    driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY, auto_merge=False)
    outcome = driver.run()
    assert not (driver._run_start_fleet & driver.reviewed_ever)   # nobody reviewed
    assert outcome.merged is False
    assert gh.matching("gh", "merge", "--squash") == []
