"""F2 — the HEAD-AWARE (substantive-strict) never-merge-unreviewed gate.

The clean-exit SAFETY gate no longer asks only "did an expected reviewer EVER
review this PR?" (the name-based ``reviewed_ever`` set) but "did one review the
COMMIT being merged?". A review of an OLDER head no longer satisfies the gate once
a substantive fix lands on top; only the merged head itself — or a tail of this
run's cosmetic-only fixes after a reviewed head — passes. This SUBSUMES D3 (stale
approval), D4 (all-cosmetic / already-fixed tail), and D5 (review-body defer).

The four invariants under test:
  1. SUBSTANTIVE-STRICT — pass iff the merged head was reviewed OR every commit
     since the last-reviewed head is a loop-authored cosmetic fix; unknown
     provenance = substantive = block; empty reviewed set = block.
  2. FAIL-CLOSED — the merged head + boundary come from LOCAL git; a None head /
     boundary / review fetch, or an ancestor check that errors → BLOCK. The
     name-based path never grants a pass (only picks the reason string).
  3. PINNED MERGE + RE-GATE — merge with ``--match-head-commit`` the verified head;
     a content push landing AFTER the gate advances the boundary and re-blocks.
  4. SHA-LESS SIGNAL ANCHORING — a sha-less clean signal anchors to the round head
     ONLY for a bot with no real per-commit review; a stale real review is never
     synthesized onto the current head.

Both the pure helpers (``_genuine_review_shas_by_bot`` / ``_head_reviewed_blocks_
merge``) and the wired gate (through the real ``RoundDriver._clean_exit`` + merge
path) are exercised, network-free, with a commit-DAG gh fake.
"""
import subprocess

import pytest

from buddhi_review import detectors, round_driver
from buddhi_review.round_driver import (
    RoundDriver,
    _genuine_review_shas_by_bot,
    _head_reviewed_blocks_merge,
)
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.seams import ConsoleEscalation
from test_round_driver import FakeClock, FakeNotifier, label_runner

CLAUDE_ONLY = {"active_reviewers": ["claude"], "auto_on_open": {"claude": False}}
# A linear history c0 → c1 → c2 → c3 (each the child of the last).
ORDER = {"c0": 0, "c1": 1, "c2": 2, "c3": 3}


def _CP(rc, out=""):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr="")


class HeadGh:
    """A gh/git fake modelling a linear commit history (``order``: sha → index).

    ``git rev-parse HEAD`` yields the next entry of ``rev_parse_seq`` (last one
    repeated), so a test can move the local head between the gate and the merge.
    ``git merge-base --is-ancestor a b`` is rc 0 iff BOTH shas are known and
    ``order[a] <= order[b]`` (fail-closed on an unknown sha). Every gh spawn is
    recorded; ``gh pr merge`` / ``gh pr view`` answer so the clean-exit merge path
    runs to a landed (or aborted) merge."""

    def __init__(self, rev_parse_seq, order=ORDER):
        self._seq = list(rev_parse_seq)
        self._i = 0
        self.order = order
        self.calls = []

    def _head(self):
        i = min(self._i, len(self._seq) - 1)
        self._i += 1
        return self._seq[i]

    def __call__(self, argv, *, cwd=None, timeout=None):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "HEAD":
            return _CP(0, self._head() + "\n")
        if argv[:2] == ["git", "merge-base"]:
            a, b = argv[-2], argv[-1]
            ok = a in self.order and b in self.order and self.order[a] <= self.order[b]
            return _CP(0 if ok else 1)
        out = " M x.py\n" if argv[:3] == ["git", "status", "--porcelain"] else ""
        return _CP(0, out)

    def matching(self, *needles):
        return [c for c in self.calls if all(any(n in a for a in c) for n in needles)]


def _review(bot, sha, *, body="please fix this", state="COMMENTED"):
    return {"user": {"login": f"{bot}[bot]"}, "commit_id": sha, "body": body,
            "state": state}


def _inline(bot, sha):
    return {"user": {"login": f"{bot}[bot]"}, "original_commit_id": sha}


def _gate_driver(*, fleet, reviewed_ever, rev_parse_seq, last_substantive,
                 reviews=(), inline=(), clean_signal=None, order=ORDER,
                 rr_none=False, auto_merge=True, ancestor_error=False):
    """A RoundDriver with its head-aware-gate state set directly, so the gate can be
    exercised at ``_clean_exit`` without driving a full round loop. ``reviews`` /
    ``inline`` are RAW dict payloads; ``clean_signal`` seeds ``_clean_signal_head``."""
    gh = HeadGh(rev_parse_seq, order=order)
    if ancestor_error:
        base = gh.__call__

        def erroring(argv, *, cwd=None, timeout=None):
            if list(argv)[:2] == ["git", "merge-base"]:
                raise subprocess.SubprocessError("merge-base boom")
            return base(argv, cwd=cwd, timeout=timeout)
        gh_run = erroring
        gh_run.calls = gh.calls  # type: ignore[attr-defined]
        gh_run.matching = gh.matching  # type: ignore[attr-defined]
    else:
        gh_run = gh
    driver = RoundDriver(
        "7", repo="o/r", cwd="/nonexistent", cfg=CLAUDE_ONLY,
        adapter=ReviewAdapter(escalation=ConsoleEscalation(notifier=FakeNotifier())),
        classify_runner=label_runner("INVALID"),
        fetch=lambda pr, repo=None, cwd=None: [],
        reactions_fetch=lambda pr, repo=None, cwd=None: [],
        reviews_fetch=lambda pr, repo=None, cwd=None: list(reviews) if reviews is not None else None,
        inline_fetch=lambda pr, repo=None, cwd=None: list(inline) if inline is not None else None,
        threads_fetch=lambda pr, repo=None, cwd=None: [],
        resolve_thread=lambda thread_id, cwd=None: True,
        gh_run=gh_run, clock=FakeClock(), sleep=lambda s: None,
        notice=lambda *a, **k: "", auto_merge=auto_merge, rr_none=rr_none,
        preflight=False,
    )
    driver._run_start_fleet = set(fleet)
    driver.reviewed_ever = set(reviewed_ever)
    driver._process_start_head = rev_parse_seq[0]
    driver._last_substantive_head = last_substantive
    driver._clean_signal_head = dict(clean_signal or {})
    return driver, gh


def _merged(driver):
    return driver._clean_exit(1).merged


# ===========================================================================
# Pure helpers
# ===========================================================================

def test_pure_shas_credits_review_and_inline_by_commit():
    shas = _genuine_review_shas_by_bot(
        [_review("claude", "c1", body="fix this")],
        [_inline("codex", "c2")])
    assert shas == {"claude": {"c1"}, "codex": {"c2"}}


def test_pure_shas_placeholder_body_is_not_credited():
    # A quota / too-large / errored body is a RESPONSE, not a review of its commit.
    for body in ["I've exhausted my quota, try again in 5 hours.",
                 "This pull request is too large to review.",
                 "The review run failed; please try again."]:
        shas = _genuine_review_shas_by_bot([_review("claude", "c1", body=body)], [])
        assert shas == {}, body


def test_pure_shas_empty_body_only_counts_when_approved():
    assert _genuine_review_shas_by_bot([_review("claude", "c1", body="", state="COMMENTED")], []) == {}
    assert _genuine_review_shas_by_bot(
        [_review("claude", "c1", body="", state="APPROVED")], []) == {"claude": {"c1"}}


def test_pure_block_rule_substantive_strict():
    anc = lambda a, b: ORDER[a] <= ORDER[b]
    # merged head reviewed → pass (case 1)
    assert _head_reviewed_blocks_merge(True, {"claude"}, {"claude": {"c1"}}, "c1", "c1", anc) is False
    # reviewed head in the cosmetic tail range [boundary, merged] → pass (case 2)
    assert _head_reviewed_blocks_merge(True, {"claude"}, {"claude": {"c1"}}, "c2", "c1", anc) is False
    # reviewed an OLDER head, boundary advanced past it → block (D3)
    assert _head_reviewed_blocks_merge(True, {"claude"}, {"claude": {"c1"}}, "c2", "c2", anc) is True
    # empty reviewed set → block
    assert _head_reviewed_blocks_merge(True, {"claude"}, {}, "c1", "c1", anc) is True
    # empty fleet (--rr-none) → no block (the deliberate lift)
    assert _head_reviewed_blocks_merge(True, set(), {}, "c1", "c1", anc) is False


def test_pure_block_rule_fails_closed_on_ancestor_error():
    def boom(a, b):
        raise RuntimeError("ancestry unresolved")
    # c1 != merged c2, so the range test calls is_ancestor, which raises → the
    # helper must not swallow a pass. (The driver's _is_ancestor catches + returns
    # False; here we assert the helper propagates rather than silently passing.)
    with pytest.raises(RuntimeError):
        _head_reviewed_blocks_merge(True, {"claude"}, {"claude": {"c1"}}, "c2", "c1", boom)


# ===========================================================================
# 1–3. Substantive-strict at the wired gate
# ===========================================================================

def test_review_of_current_merged_head_merges():
    # (scenario 2) A review anchored to the CURRENT merged head passes.
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"}, rev_parse_seq=["c1"],
        last_substantive="c1", reviews=[_review("claude", "c1")])
    assert _merged(driver) is True
    assert gh.matching("gh", "merge", "--squash")


def test_stale_review_blocks_after_a_substantive_fix():
    # (scenario 1 / D3) A review anchored to an OLD head (c1) does NOT satisfy the
    # gate once a substantive fix (c2) sits on top — boundary advanced to c2.
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"}, rev_parse_seq=["c2"],
        last_substantive="c2", reviews=[_review("claude", "c1")])
    assert _merged(driver) is False
    assert gh.matching("gh", "merge", "--squash") == []


def test_all_cosmetic_tail_after_reviewed_head_merges():
    # (scenario 3 / D4) The reviewed head c1 is the last substantive head; c2/c3
    # after it are this run's cosmetic-only fixes → the merged head c3 auto-merges.
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"}, rev_parse_seq=["c3"],
        last_substantive="c1", reviews=[_review("claude", "c1")])
    assert _merged(driver) is True
    assert gh.matching("gh", "merge", "--squash")


def test_a_substantive_fix_inside_the_tail_blocks():
    # The complement of the cosmetic tail: the boundary is c2 (a substantive fix
    # landed at c2), the only review is of c1 (< boundary) → block.
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"}, rev_parse_seq=["c3"],
        last_substantive="c2", reviews=[_review("claude", "c1")])
    assert _merged(driver) is False


# ===========================================================================
# 4. Crash-restart skipped-already-fixed (D4) + review-body defer (D5)
# ===========================================================================

def test_crash_restart_already_fixed_finding_blocks():
    # (D4) A prior crashed run pushed a substantive fix; its commits sit at/before
    # the process-start head (unknown provenance = substantive). The reviewer's only
    # review is its ORIGINAL finding at c0, but the boundary is the restart head c1.
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"}, rev_parse_seq=["c1"],
        last_substantive="c1", inline=[_inline("claude", "c0")])
    assert _merged(driver) is False


def test_review_body_defer_without_re_review_blocks():
    # (D5) The reviewer's latest review body defers ("needs another look") on c0,
    # then a substantive fix landed (c1). The deferred review anchors to c0 only;
    # boundary is c1 → the merge is blocked until a re-review of c1.
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"}, rev_parse_seq=["c1"],
        last_substantive="c1",
        reviews=[_review("claude", "c0", body="Deferring — needs another look.")])
    assert _merged(driver) is False


# ===========================================================================
# 5. Fail-closed edges
# ===========================================================================

def test_empty_reviewed_set_blocks():
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever=set(), rev_parse_seq=["c1"],
        last_substantive="c1", reviews=[], inline=[])
    assert _merged(driver) is False
    assert gh.matching("gh", "merge") == []


def test_none_local_head_blocks_gate_unverified(capsys):
    # rev-parse HEAD is unreadable → merged_head None → gate-unverified block.
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"}, rev_parse_seq=[""],
        last_substantive="c1", reviews=[_review("claude", "c1")])
    assert _merged(driver) is False
    assert "GATE UNVERIFIED" in capsys.readouterr().out


def test_none_review_fetch_blocks_gate_unverified():
    # The raw review fetch failed (None, distinct from []) → fail-closed block.
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"}, rev_parse_seq=["c1"],
        last_substantive="c1", reviews=None, inline=[])
    assert _merged(driver) is False


def test_ancestor_error_blocks():
    # A review at c1 that WOULD satisfy the cosmetic-tail range [c0, c2] via two real
    # ancestor checks: with merge-base erroring, _is_ancestor returns False for both,
    # so the range test can never pass and the gate BLOCKS (fail-closed). (Without
    # the error the same setup merges — asserted by test_all_cosmetic_tail…)
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"}, rev_parse_seq=["c2"],
        last_substantive="c0", reviews=[_review("claude", "c1")], ancestor_error=True)
    assert _merged(driver) is False


def test_name_based_reviewed_ever_never_grants_a_pass():
    # The bot IS in reviewed_ever (the old name-based gate would merge), but its only
    # review is of a stale head c0 while the boundary is c2 → the head-aware gate
    # BLOCKS. reviewed_ever membership is necessary, never sufficient.
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"}, rev_parse_seq=["c2"],
        last_substantive="c2", reviews=[_review("claude", "c0")])
    assert _merged(driver) is False


# ===========================================================================
# 6. --rr-none / zero fleet lifts the block
# ===========================================================================

def test_rr_none_empty_fleet_merges_without_a_pin():
    # --rr-none: no reviewers expected. The gate's empty-fleet lift passes and the
    # merge proceeds (no reviewer to pin to → no --match-head-commit).
    driver, gh = _gate_driver(
        fleet=set(), reviewed_ever=set(), rev_parse_seq=["c1"],
        last_substantive="c1", reviews=[], inline=[], rr_none=True)
    assert _merged(driver) is True
    assert gh.matching("gh", "merge", "--squash")
    assert gh.matching("--match-head-commit") == []


# ===========================================================================
# 7. Sha-less signal anchoring (invariant 4)
# ===========================================================================

def test_shaless_signal_anchors_for_a_bot_with_no_real_review():
    # A sha-less approval (codex +1 / claude sentinel) carries no commit_id; it is
    # credited at the round head it reviewed (c1) since the bot has no real review.
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"}, rev_parse_seq=["c1"],
        last_substantive="c1", reviews=[], inline=[], clean_signal={"claude": "c1"})
    assert _merged(driver) is True


def test_stale_real_review_is_not_synthesized_onto_current_head():
    # The bot has a STALE real review (c0) AND a sha-less anchor pointing at the
    # current head c2. The anchor must NOT be applied (bot HAS a real review), so its
    # stale c0 alone stands and, with the boundary at c2, the gate BLOCKS.
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"}, rev_parse_seq=["c2"],
        last_substantive="c2", reviews=[_review("claude", "c0")],
        clean_signal={"claude": "c2"})
    assert _merged(driver) is False


def test_shaless_anchor_of_current_head_merges_multi_bot():
    # Two-bot fleet: claude has a stale real review (c0), codex a sha-less +1 at the
    # current head c1. codex (no real review) IS anchored at c1 → the gate passes on
    # codex even though claude's review is stale.
    cfg_fleet = {"claude", "codex"}
    driver, gh = _gate_driver(
        fleet=cfg_fleet, reviewed_ever=cfg_fleet, rev_parse_seq=["c1"],
        last_substantive="c1", reviews=[_review("claude", "c0")],
        clean_signal={"codex": "c1"})
    assert _merged(driver) is True


# ===========================================================================
# 8. Pinned merge + post-gate re-gate (invariant 3)
# ===========================================================================

def test_merge_call_carries_match_head_commit():
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"}, rev_parse_seq=["c1"],
        last_substantive="c1", reviews=[_review("claude", "c1")])
    assert _merged(driver) is True
    pinned = gh.matching("gh", "merge", "--match-head-commit")
    assert pinned and "c1" in pinned[0]


def test_post_gate_content_push_advances_boundary_and_reblocks():
    # The gate signs off on c1 (reviewed). Between the gate and the merge a content
    # push lands (rev-parse now yields c2) → the boundary advances to c2, the gate
    # re-runs, and c2 is unreviewed → BLOCK, no merge.
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"},
        rev_parse_seq=["c1", "c2"],   # gate sees c1; the post-gate re-read sees c2
        last_substantive="c1", reviews=[_review("claude", "c1")])
    assert _merged(driver) is False
    assert gh.matching("gh", "merge", "--squash") == []


def test_regate_pins_the_head_it_read_not_a_later_reread():
    # Correct-by-construction guard: the post-gate re-gate advances the boundary to
    # the head it READ (c2) and re-gates against that SAME sha — it must NOT re-read
    # a head that moved again (c3). Here c1 and c2 are both reviewed, c3 is not; a
    # re-reading gate would advance boundary→c2 then judge merged=c3 as a cosmetic
    # tail after c2 and merge the UNREVIEWED c3. The fixed gate pins the reviewed c2.
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"},
        rev_parse_seq=["c1", "c2", "c3"],
        last_substantive="c1", reviews=[_review("claude", "c1"), _review("claude", "c2")])
    assert _merged(driver) is True
    pinned = gh.matching("gh", "merge", "--match-head-commit")
    assert pinned and "c2" in pinned[0] and "c3" not in pinned[0]


def test_post_gate_no_move_merges_pinned():
    # No post-gate move: rev-parse keeps yielding c1, so the re-gate is a no-op and
    # the merge lands pinned to c1.
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"},
        rev_parse_seq=["c1"], last_substantive="c1", reviews=[_review("claude", "c1")])
    assert _merged(driver) is True
    assert gh.matching("gh", "merge", "--match-head-commit")


# ===========================================================================
# Adversarial: a placeholder review is never credited at the wired gate
# ===========================================================================

def test_placeholder_review_at_merged_head_still_blocks():
    # The bot's ONLY review at the merged head is a quota placeholder → not a review
    # of that commit → the gate blocks even though the bot is in reviewed_ever.
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"}, rev_parse_seq=["c1"],
        last_substantive="c1",
        reviews=[_review("claude", "c1", body="Rate limit exceeded; try again later.")])
    assert _merged(driver) is False
