"""Manual-landing exit-rebase wiring in the round driver.

Covers the routing (which hand-back buckets rebase, which are skipped) and the
HONEST post-rebase outcome message (Bucket A "ready to merge" vs Bucket B
"merge at your discretion — <reason>"). The git work itself is unit-tested in
``test_exit_rebase.py``; here ``commit_push.exit_rebase`` is stubbed so the
focus is the driver's decision + phrasing.
"""
import subprocess

import pytest

from buddhi_review import commit_push, round_driver
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.round_driver import RoundDriver, RoundTimes, RunOutcome
from buddhi_review.seams import ConsoleEscalation


class NoticeRec:
    def __init__(self):
        self.calls = []  # (action, detail, status)

    def __call__(self, action, detail="", *, status="do", hint=None):
        self.calls.append((action, detail, status))
        return ""

    def landing(self):
        return [c for c in self.calls if c[0] == "manual-landing"]


def gh_run(base="main", head=None, current_branch=None):
    """A fake git/gh seam: answers baseRefName with ``base``, headRefName with
    ``head``, and ``git rev-parse --abbrev-ref HEAD`` with ``current_branch``.
    Pass empty string for ``base`` to simulate an unresolvable base.
    Everything else rc=0/empty."""
    def run(argv, *, cwd=None, timeout=None):
        joined = " ".join(argv)
        if "baseRefName" in joined and base:
            out = base + "\n"
        elif "headRefName" in joined and head:
            out = head + "\n"
        elif "rev-parse" in joined and "--abbrev-ref" in joined and current_branch:
            out = current_branch + "\n"
        else:
            out = ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
    return run


def make_driver(notice, *, gh=None, cfg=None, **kw):
    return RoundDriver(
        "7", repo="o/r", cwd="/nonexistent", cfg=cfg if cfg is not None else {},
        adapter=ReviewAdapter(escalation=ConsoleEscalation()),
        classify_runner=lambda p: "INVALID",
        gh_run=gh or gh_run(), notice=notice, **kw,
    )


@pytest.fixture
def stub_rebase(monkeypatch):
    """Stub ``commit_push.exit_rebase`` to a chosen (status, detail), recording
    every call. Returns a setter the test uses to pick the return value."""
    box = {"ret": ("rebased", ""), "calls": []}

    def fake(cwd, *, base, run, notice):
        box["calls"].append({"cwd": cwd, "base": base})
        return box["ret"]

    monkeypatch.setattr(round_driver.commit_push, "exit_rebase", fake)
    return box


# ── Routing: who rebases, who is skipped ────────────────────────────────────────
def test_merged_run_does_not_rebase(stub_rebase):
    nr = NoticeRec()
    d = make_driver(nr)
    d._maybe_exit_rebase(RunOutcome("clean", 1, merged=True))
    assert stub_rebase["calls"] == []        # a merged PR is never rebased
    assert nr.landing() == []


def test_push_disabled_does_not_rebase(stub_rebase):
    nr = NoticeRec()
    d = make_driver(nr, push=False)
    d._maybe_exit_rebase(RunOutcome("clean", 1, merged=False))
    assert stub_rebase["calls"] == []        # pushing off → never force-push


def test_bucket_c_rebase_skip_is_not_rebased(stub_rebase):
    nr = NoticeRec()
    d = make_driver(nr)
    d._maybe_exit_rebase(RunOutcome("needs-human", 1, False, rebase_skip=True))
    assert stub_rebase["calls"] == []        # poisoned / push-failed → skipped
    landing = nr.landing()
    assert landing and landing[-1][2] == "skip"
    assert "unverifiable" in landing[-1][1]


def test_branch_mismatch_skips_rebase(stub_rebase):
    nr = NoticeRec()
    # Worktree is on "main" but the PR head is "feature/pr-7" → must skip.
    d = make_driver(nr, gh=gh_run(base="main", head="feature/pr-7",
                                  current_branch="main"))
    d._maybe_exit_rebase(RunOutcome("clean", 1, merged=False))
    assert stub_rebase["calls"] == []    # mis-pointed worktree → never rebased
    landing = nr.landing()
    assert landing and landing[-1][2] == "skip"
    assert "feature/pr-7" in landing[-1][1]


def test_unresolvable_base_is_not_rebased(stub_rebase):
    nr = NoticeRec()
    d = make_driver(nr, gh=gh_run(base=""))   # baseRefName lookup fails
    d._maybe_exit_rebase(RunOutcome("clean", 1, merged=False))
    assert stub_rebase["calls"] == []
    landing = nr.landing()
    assert landing and landing[-1][2] == "skip"
    assert "base branch" in landing[-1][1]


def test_handback_calls_exit_rebase_with_resolved_base(stub_rebase):
    nr = NoticeRec()
    d = make_driver(nr, gh=gh_run(base="develop"))
    d._run_start_fleet = {"claude"}
    d.reviewed_ever = {"claude"}
    d._maybe_exit_rebase(RunOutcome("clean", 2, merged=False))
    assert len(stub_rebase["calls"]) == 1
    assert stub_rebase["calls"][0]["base"] == "develop"


# ── Honest post-rebase message: Bucket A (ready) vs Bucket B (discretion) ────────
def test_ready_to_merge_when_a_real_review_happened(stub_rebase):
    stub_rebase["ret"] = ("rebased", "")
    nr = NoticeRec()
    d = make_driver(nr)
    d._run_start_fleet = {"claude"}
    d.reviewed_ever = {"claude"}            # a genuine review happened
    d._maybe_exit_rebase(RunOutcome("clean", 1, merged=False))
    line = nr.landing()[-1]
    assert line[2] == "done"
    assert "ready to merge" in line[1]


def test_no_review_is_not_labelled_ready(stub_rebase):
    stub_rebase["ret"] = ("rebased", "")
    nr = NoticeRec()
    d = make_driver(nr)
    d._run_start_fleet = {"claude", "copilot"}
    d.reviewed_ever = set()                 # nobody actually reviewed
    d._maybe_exit_rebase(RunOutcome("clean", 1, merged=False))
    line = nr.landing()[-1]
    assert line[2] == "fallback"
    assert "ready to merge" not in line[1]
    assert "no expected reviewer actually reviewed" in line[1]
    assert "merge at your discretion" in line[1]


def test_empty_fleet_is_not_labelled_ready(stub_rebase):
    stub_rebase["ret"] = ("rebased", "")
    nr = NoticeRec()
    d = make_driver(nr)
    d._run_start_fleet = set()              # no reviewers configured
    d._maybe_exit_rebase(RunOutcome("clean", 1, merged=False))
    line = nr.landing()[-1]
    assert line[2] == "fallback"
    assert "no reviewers are configured" in line[1]
    assert "ready to merge" not in line[1]


def test_ci_red_is_not_labelled_ready(stub_rebase):
    stub_rebase["ret"] = ("rebased", "")
    nr = NoticeRec()
    d = make_driver(nr)
    d._run_start_fleet = {"claude"}
    d.reviewed_ever = {"claude"}
    d._premerge_ci_red = True              # CI was red at the clean-exit gate
    d._maybe_exit_rebase(RunOutcome("clean", 1, merged=False))
    line = nr.landing()[-1]
    assert line[2] == "fallback"
    assert "pre-merge CI is red" in line[1]
    assert "ready to merge" not in line[1]


def test_max_rounds_is_not_labelled_ready(stub_rebase):
    stub_rebase["ret"] = ("rebased", "")
    nr = NoticeRec()
    d = make_driver(nr)
    d._run_start_fleet = {"claude"}
    d.reviewed_ever = {"claude"}
    d._maybe_exit_rebase(RunOutcome("max-rounds", 5, merged=False))
    line = nr.landing()[-1]
    assert line[2] == "fallback"
    assert "round budget" in line[1]
    assert "ready to merge" not in line[1]


def test_stopped_is_not_labelled_ready(stub_rebase):
    stub_rebase["ret"] = ("rebased", "")
    nr = NoticeRec()
    d = make_driver(nr)
    d._maybe_exit_rebase(RunOutcome("stopped", 1, merged=False))
    line = nr.landing()[-1]
    assert line[2] == "fallback"
    assert "stopped before a clean finish" in line[1]


def test_needs_human_is_not_labelled_ready(stub_rebase):
    stub_rebase["ret"] = ("rebased", "")
    nr = NoticeRec()
    d = make_driver(nr)
    d._maybe_exit_rebase(RunOutcome("needs-human", 1, merged=False))
    line = nr.landing()[-1]
    assert line[2] == "fallback"
    assert "needs you" in line[1]


# ── Non-rebased outcomes surface the right console status ────────────────────────
def test_conflict_message_carries_diagnosis(stub_rebase):
    stub_rebase["ret"] = ("conflict", "the rebase onto origin/main hit a conflict "
                          "in shared.py; ... git rebase ...")
    nr = NoticeRec()
    d = make_driver(nr)
    d._maybe_exit_rebase(RunOutcome("clean", 1, merged=False))
    line = nr.landing()[-1]
    assert line[2] == "stop"
    assert "could not be rebased" in line[1]
    assert "shared.py" in line[1]


def test_already_current_message(stub_rebase):
    stub_rebase["ret"] = ("current", "")
    nr = NoticeRec()
    d = make_driver(nr)
    d._run_start_fleet = {"claude"}
    d.reviewed_ever = {"claude"}
    d._maybe_exit_rebase(RunOutcome("clean", 1, merged=False))
    line = nr.landing()[-1]
    assert line[2] == "skip"
    assert "already up to date" in line[1]


def test_skipped_message_surfaces_reason(stub_rebase):
    stub_rebase["ret"] = ("skipped", "the worktree has uncommitted changes — not rebasing")
    nr = NoticeRec()
    d = make_driver(nr)
    d._maybe_exit_rebase(RunOutcome("clean", 1, merged=False))
    line = nr.landing()[-1]
    assert line[2] == "skip"
    assert "left as-is" in line[1]
    assert "uncommitted" in line[1]


def test_error_message_surfaces_reason(stub_rebase):
    stub_rebase["ret"] = ("error", "rebased locally but the --force-with-lease push "
                          "was rejected: stale info — push it by hand")
    nr = NoticeRec()
    d = make_driver(nr)
    d._maybe_exit_rebase(RunOutcome("clean", 1, merged=False))
    line = nr.landing()[-1]
    assert line[2] == "fallback"
    assert "push it by hand" in line[1]


def test_unexpected_error_never_loses_the_outcome(monkeypatch):
    # A bug in the rebase path must degrade to a note, never crash the hand-back.
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(round_driver.commit_push, "exit_rebase", boom)
    nr = NoticeRec()
    d = make_driver(nr)
    d._maybe_exit_rebase(RunOutcome("clean", 1, merged=False))  # must not raise
    line = nr.landing()[-1]
    assert line[2] == "fallback"
    assert "by hand" in line[1]


# ── Wired through run(): a clean hand-back triggers the exit-rebase ──────────────
def test_run_triggers_exit_rebase_on_clean_handback(stub_rebase):
    nr = NoticeRec()
    # No reviewers configured → first round is an immediate clean exit; auto_merge
    # is off, so the loop hands the PR back (not merged) and the exit-rebase fires.
    d = make_driver(nr, cfg={"active_reviewers": []}, gh=gh_run(base="main"))
    outcome = d.run()
    assert outcome.status == "clean" and outcome.merged is False
    assert len(stub_rebase["calls"]) == 1
    assert stub_rebase["calls"][0]["base"] == "main"
