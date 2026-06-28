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

from buddhi_review import merge
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
        out = " M x.py\n" if argv[:3] == ["git", "status", "--porcelain"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


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
    # all-skipped is NOT green (no real check ran) — keep waiting
    assert merge._ci_verdict([{"bucket": "skipping"}]) == "pending"
    # a pass beside a skip IS green (the skip is neutral)
    assert merge._ci_verdict([{"bucket": "pass"}, {"bucket": "skipping"}]) == "green"
    # state fallback when bucket is missing
    assert merge._ci_verdict([{"state": "SUCCESS"}]) == "green"
    assert merge._ci_verdict([{"state": "FAILURE"}]) == "red"


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
