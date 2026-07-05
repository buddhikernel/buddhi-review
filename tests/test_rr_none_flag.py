"""Tests for --rr-none — summon nobody, resolve existing comments, merge.

--rr-none is the operator's explicit "expect ZERO reviewers this run" request:
no summon, no re-request, no waiting. The comments already on the PR are still
fixed and resolved, then the run merges on a clean exit even with zero reviews —
the one explicit lift of the never-merge-unreviewed backstop (distinct from a
configured-but-silent fleet, which still blocks).
"""
from __future__ import annotations

import pytest

from buddhi_review import backends, cli
from buddhi_review.loop import Comment

from test_round_driver import CLAUDE_ONLY, label_runner, make_driver


# ── cli argparse: parsing + mutual exclusion ─────────────────────────────────
def _parse(argv):
    return cli.build_parser().parse_args(argv)


def test_rr_none_defaults_false():
    assert _parse(["review-pr", "7"]).rr_none is False


def test_rr_none_sets_flag():
    assert _parse(["review-pr", "7", "--rr-none"]).rr_none is True


@pytest.mark.parametrize("other", ["--rr", "--rr-active"])
def test_rr_none_mutually_exclusive(other):
    # --rr / --rr-active / --rr-none share one mutually-exclusive group.
    with pytest.raises(SystemExit):
        _parse(["review-pr", "7", "--rr-none", other])


# ── backends: the flag reaches the detached run-loop argv ────────────────────
def test_loop_argv_emits_rr_none():
    assert "--rr-none" in backends._loop_argv("7", "o/r", "/x", {"rr_none": True})


def test_loop_argv_omits_rr_none_by_default():
    assert "--rr-none" not in backends._loop_argv("7", "o/r", "/x", {})


# ── round driver: zero fleet, fix existing comments, merge ───────────────────
def test_expected_bots_empty_under_rr_none():
    driver, _, _ = make_driver([], cfg=CLAUDE_ONLY, rr_none=True)
    assert driver.expected_bots() == []
    # Without the flag the same fleet is non-empty (guards against a no-op test).
    baseline, _, _ = make_driver([], cfg=CLAUDE_ONLY)
    assert baseline.expected_bots() == ["claude"]


def test_rr_none_merges_with_zero_comments_and_no_wait():
    # Zero comments + zero reviews, yet --rr-none merges on a clean exit — and
    # without waiting out any reviewer silence window (clock stays at 0).
    driver, clock, gh = make_driver(
        [], cfg=CLAUDE_ONLY, auto_merge=True, rr_none=True,
        answer_waiter=lambda esc, **k: {})
    outcome = driver.run()
    assert outcome.status == "clean" and outcome.merged is True
    assert gh.matching("gh", "merge", "--squash")   # merged with zero reviews
    assert clock.t < 60                             # no reviewer-silence wait


def test_rr_none_still_processes_existing_comments_then_merges():
    # The loop must NOT short-circuit on the empty fleet: an existing comment is
    # still ingested + acted on, THEN the run merges.
    timeline = [(0, Comment(id="c1", text="a leftover review comment",
                            source="claude[bot]"))]
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, auto_merge=True, rr_none=True,
        classify=label_runner("INVALID"), answer_waiter=lambda esc, **k: {})
    outcome = driver.run()
    assert driver.actions, "existing comment was not processed under --rr-none"
    assert outcome.status == "clean" and outcome.merged is True
    assert gh.matching("gh", "merge", "--squash")


def test_rr_none_does_not_block_merge_when_nobody_reviewed():
    # The never-merge-unreviewed backstop (which blocks a fleet that was expected
    # but stayed silent) must be LIFTED under --rr-none: reviewed_ever is empty
    # yet the PR still merges.
    driver, clock, gh = make_driver(
        [], cfg=CLAUDE_ONLY, auto_merge=True, rr_none=True,
        answer_waiter=lambda esc, **k: {})
    outcome = driver.run()
    assert not driver.reviewed_ever          # nobody reviewed
    assert outcome.merged is True            # ...yet it merged
