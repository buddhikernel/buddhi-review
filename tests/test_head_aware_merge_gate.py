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
  4. SHA-LESS SIGNAL ANCHORING — a sha-less clean signal (approval reaction /
     issue-channel sentinel / restored verdict) is anchored to the round head ONLY
     when it is provably FRESH: it POST-DATES that head's commit, or its freshness
     is structurally proven (the reaction baseline, the polish tip-guard). An
     undated signal is UNANCHORABLE. Freshness — not "does this bot also have an
     earlier real review" — is the discriminator; anchors are then setdefault-UNIONed
     with the raw per-commit shas, so a findings bot's later clean sign-off still
     counts (the loop's mainline path).

Both the pure helpers (``_genuine_review_shas_by_bot`` / ``_head_reviewed_blocks_
merge``) and the wired gate (through the real ``RoundDriver._clean_exit`` + merge
path) are exercised, network-free, with a commit-DAG gh fake.
"""
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from buddhi_review import detectors, round_driver
from buddhi_review.round_driver import (
    RoundDriver,
    RoundTimes,
    _genuine_review_shas_by_bot,
    _head_reviewed_blocks_merge,
)
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.fix_apply import FixOutcome
from buddhi_review.loop import Comment
from buddhi_review.seams import ConsoleEscalation
from test_round_driver import FakeClock, FakeNotifier, label_runner

CLAUDE_ONLY = {"active_reviewers": ["claude"], "auto_on_open": {"claude": False}}
# A linear history c0 → c1 → c2 → c3 (each the child of the last).
ORDER = {"c0": 0, "c1": 1, "c2": 2, "c3": 3}


def _CP(rc, out=""):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr="")


def _ct(sha, order=None):
    """The modelled committer date of ``sha`` — successive hours along c0→c3, so a
    signal stamped ``_ct("c0")`` provably PRE-dates the commit ``c1``. This is the
    freshness cutoff a sha-less clean signal must post-date to be anchored."""
    n = (order or ORDER).get(sha, 0)
    return f"2026-01-01T{n:02d}:00:00+00:00"


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
        if argv[:3] == ["git", "show", "-s"]:
            # The committer date of the requested sha — the F2 freshness cutoff.
            ref = argv[-1]
            return _CP(0, _ct(self._seq[-1] if ref == "HEAD" else ref, self.order) + "\n")
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


def test_pure_shas_non_string_body_does_not_crash_and_is_not_credited():
    # A truthy non-string body (malformed payload / test seam) must not reach the
    # is_placeholder_review_body regex — it fails closed to "not genuine" instead
    # of raising, unless the state is an explicit APPROVED.
    assert _genuine_review_shas_by_bot(
        [_review("claude", "c1", body={"not": "a string"}, state="COMMENTED")], []) == {}
    assert _genuine_review_shas_by_bot(
        [_review("claude", "c1", body=12345, state="APPROVED")], []) == {"claude": {"c1"}}


def test_pure_shas_non_string_inline_anchor_is_not_credited():
    # A non-string commit anchor (malformed payload / test seam) must not be added
    # to the reviewed-sha set — it would otherwise flow into ancestry / subprocess
    # argv building downstream.
    assert _genuine_review_shas_by_bot([], [{"user": {"login": "codex[bot]"},
                                              "original_commit_id": 12345}]) == {}
    assert _genuine_review_shas_by_bot([], [{"user": {"login": "codex[bot]"},
                                              "original_commit_id": None,
                                              "commit_id": None}]) == {}


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


def _anchoring_driver(*, head, reviews=(), inline=(), fleet={"claude"}):
    """A driver positioned at ``head`` with its round-review head + freshness cutoff
    set exactly as the round loop sets them, so ``_anchor_clean_signal`` can be
    exercised for real (rather than hand-seeding ``_clean_signal_head``)."""
    driver, gh = _gate_driver(
        fleet=fleet, reviewed_ever=set(fleet), rev_parse_seq=[head],
        last_substantive=head, reviews=reviews, inline=inline)
    driver._round_review_head = head
    driver._round_review_head_time = _ct(head)
    return driver, gh


def test_stale_shaless_sentinel_is_not_anchored_and_blocks():
    # CRITICAL 1. The ONLY artifact is a claude issue-channel "No issues found."
    # written when the head was c0; the head is now c2. The sentinet is returned by
    # NEITHER raw fetch, so nothing else can credit it — if it were anchored to c2 it
    # would merge a commit nobody reviewed. It must NOT anchor.
    driver, gh = _anchoring_driver(head="c2", reviews=[], inline=[])
    driver._anchor_clean_signal("claude", stamp=_ct("c0"))   # posted against c0
    assert driver._clean_signal_head == {}                   # stale → unanchorable
    assert _merged(driver) is False
    assert gh.matching("gh", "merge", "--squash") == []


def test_undated_shaless_signal_is_unanchorable_and_blocks():
    # CRITICAL 1, reaction vector: a bare +1 / restored LGTM with no usable
    # timestamp cannot be proven to have reviewed ANY head → never credited.
    driver, gh = _anchoring_driver(head="c2", reviews=[], inline=[])
    driver._anchor_clean_signal("claude", stamp=None)
    assert driver._clean_signal_head == {}
    assert _merged(driver) is False


def test_missing_head_commit_time_makes_signals_unanchorable():
    # The cutoff itself is unreadable → freshness cannot be established → fail-closed.
    driver, gh = _anchoring_driver(head="c2", reviews=[], inline=[])
    driver._round_review_head_time = None
    driver._anchor_clean_signal("claude", stamp=_ct("c2"))
    assert driver._clean_signal_head == {}
    assert _merged(driver) is False


def test_fresh_shaless_signal_anchors_even_when_the_bot_has_a_real_review():
    # CRITICAL 2 — the loop's MAINLINE path. Every actionable claude finding is an
    # INLINE comment, so claude always carries a real (now stale) sha at c0; its
    # round-2 clean sign-off arrives sha-less on the ISSUE channel against the
    # post-fix head c1. The FRESH sign-off must anchor — union, not exclusion — or
    # the loop hands back every PR that ever had a finding.
    driver, gh = _anchoring_driver(head="c1", reviews=[], inline=[_inline("claude", "c0")])
    driver._anchor_clean_signal("claude", stamp=_ct("c1"))   # reviewed the new head
    assert driver._clean_signal_head == {"claude": "c1"}
    assert _merged(driver) is True
    pinned = gh.matching("gh", "merge", "--match-head-commit")
    assert pinned and "c1" in pinned[0]


def test_proven_fresh_signal_anchors_without_a_timestamp():
    # A structurally-proven-fresh signal (reaction baseline / polish tip-guard)
    # anchors even with no usable timestamp — that is what the proof is for.
    driver, gh = _anchoring_driver(head="c1", reviews=[], inline=[])
    driver._anchor_clean_signal("claude", stamp=None, proven_fresh=True)
    assert driver._clean_signal_head == {"claude": "c1"}
    assert _merged(driver) is True


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
# CRITICAL 2 end-to-end — the mainline round loop, with a genuinely MOVING head
# ===========================================================================

class MainlineGh:
    """A gh/git fake whose tip really advances H0 → H1 on the round's fix push, with
    per-head committer dates. Proves the ROUND LOOP wires the round-review head and
    its freshness cutoff correctly — a gate-level test cannot."""

    def __init__(self, head_times=None):
        self.head = "H0"
        self.pushed = False
        self.calls = []
        # sha -> committer date. Default is one hour per head; a test working on a
        # seconds timeline (e.g. the register-delay window) passes its own.
        self.head_times = head_times or {}

    @staticmethod
    def _n(h):
        return int(h[1:]) if h[:1] == "H" and h[1:].isdigit() else -1

    def __call__(self, argv, *, cwd=None, timeout=None):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "HEAD":
            return _CP(0, self.head + "\n")
        if argv[:3] == ["git", "show", "-s"]:
            ref = argv[-1]
            ref = self.head if ref == "HEAD" else ref
            if ref in self.head_times:
                return _CP(0, self.head_times[ref] + "\n")
            return _CP(0, f"2026-01-01T{max(self._n(ref), 0):02d}:00:00+00:00\n")
        if argv[:2] == ["git", "merge-base"]:
            return _CP(0 if self._n(argv[-2]) <= self._n(argv[-1]) else 1)
        if argv[:3] == ["git", "diff", "--cached"]:
            return _CP(0 if self.pushed else 1)      # staged work until we commit
        if argv[:2] == ["git", "push"]:
            self.head, self.pushed = "H1", True      # the fix push advances the tip
            return _CP(0)
        if argv[:3] == ["git", "status", "--porcelain"]:
            return _CP(0, "" if self.pushed else " M x.py\n")
        return _CP(0)

    def matching(self, *needles):
        return [c for c in self.calls if all(any(n in a for a in c) for n in needles)]


def test_mainline_findings_then_fresh_signoff_merges_end_to_end():
    # THE LOOP'S MAINLINE, driven through the real round loop:
    #   round 1 — claude posts an INLINE finding against H0 (so it carries a real,
    #             soon-to-be-stale sha) → the fix is applied and PUSHED → tip H1,
    #             and the substantive boundary advances to H1;
    #   round 2 — claude re-reviews H1 and signs off SHA-LESSLY on the issue channel.
    # The sign-off post-dates H1's commit, so it anchors to H1 and the PR merges,
    # pinned to H1. Under the pre-rework guard ("drop the anchor when the bot has any
    # real review") claude was always excluded and this handed back — the regression
    # that made the loop reject nearly every PR that ever had a finding.
    finding = Comment(id="f1", text="this null check is missing", source="claude[bot]",
                      path="x.py", diff_hunk="@@ -1 +1 @@",
                      created_at="2026-01-01T00:30:00+00:00")     # reviewed H0
    signoff = Comment(id="s1", text="No issues found.", source="claude[bot]",
                      from_issue_channel=True,
                      created_at="2026-01-01T02:00:00+00:00")     # reviewed H1
    timeline = [(0, finding), (90, signoff)]
    gh = MainlineGh()
    clock = FakeClock()

    def fetch(pr, repo=None, cwd=None):
        return [c for t, c in timeline if t <= clock.t]

    driver = RoundDriver(
        "7", repo="o/r", cwd="/nonexistent", cfg=CLAUDE_ONLY,
        adapter=ReviewAdapter(escalation=ConsoleEscalation(notifier=FakeNotifier())),
        classify_runner=label_runner("SUBSTANTIVE"),
        fix_dispatch=lambda c, r: FixOutcome(status="applied"),
        fetch=fetch, reactions_fetch=lambda pr, repo=None, cwd=None: [],
        # The finding is INLINE (anchored to the head it was posted against, H0); the
        # sign-off is issue-channel, so it appears in NEITHER raw fetch — exactly as
        # in production, which is why it must ride the sha-less anchor.
        reviews_fetch=lambda pr, repo=None, cwd=None: [],
        inline_fetch=lambda pr, repo=None, cwd=None: (
            [_inline("claude", "H0")] if clock.t >= 0 else []),
        threads_fetch=lambda pr, repo=None, cwd=None: [],
        resolve_thread=lambda thread_id, cwd=None: True,
        gh_run=gh, clock=clock, sleep=clock.sleep, notice=lambda *a, **k: "",
        # The summon happens at 01:30 — after H1's commit (01:00) and before the
        # sign-off (02:00), so the sign-off is a genuine response to the re-request.
        wall_clock=lambda: datetime(2026, 1, 1, 1, 30, tzinfo=timezone.utc),
        times=RoundTimes(quiescence=60, poll_interval=30, min_bot_wait=420,
                         idle_timeout=900, max_wait_total=1800, register_delay=0),
        answer_waiter=lambda esc, **k: {},
        auto_merge=True, preflight=False, push=True, test_gate=False, max_rounds=3,
    )
    outcome = driver.run()

    assert gh.head == "H1"                                  # the fix push advanced it
    assert driver._last_substantive_head == "H1"            # boundary followed it
    assert driver._clean_signal_head == {"claude": "H1"}    # fresh sign-off anchored
    assert outcome.merged is True
    pinned = gh.matching("gh", "merge", "--match-head-commit")
    assert pinned and "H1" in pinned[0]


def test_summon_floor_is_the_trigger_instant_not_after_the_register_delay():
    # REGRESSION (freshness floor placement). _summon ends by sleeping out
    # register_delay, so capturing the floor AFTER it would put the cutoff a full
    # register_delay LATER than the moment reviewers were actually asked — refusing an
    # anchor to every sha-less sign-off that lands in that window and handing back a
    # fully-reviewed PR. The floor must be the TRIGGER instant. Driven through the real
    # round loop with a REAL register_delay and a wall clock slaved to the loop's own
    # clock; a constant injected clock cannot observe this at all.
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    gh = MainlineGh()
    clock = FakeClock()
    driver = RoundDriver(
        "7", repo="o/r", cwd="/nonexistent", cfg=CLAUDE_ONLY,
        adapter=ReviewAdapter(escalation=ConsoleEscalation(notifier=FakeNotifier())),
        classify_runner=label_runner("INVALID"),
        fetch=lambda pr, repo=None, cwd=None: [],
        reactions_fetch=lambda pr, repo=None, cwd=None: [],
        reviews_fetch=lambda pr, repo=None, cwd=None: [],
        inline_fetch=lambda pr, repo=None, cwd=None: [],
        threads_fetch=lambda pr, repo=None, cwd=None: [],
        resolve_thread=lambda thread_id, cwd=None: True,
        gh_run=gh, clock=clock, sleep=clock.sleep, notice=lambda *a, **k: "",
        wall_clock=lambda: base + timedelta(seconds=clock.t),
        times=RoundTimes(quiescence=15, poll_interval=5, min_bot_wait=0,
                         idle_timeout=30, max_wait_total=300, register_delay=60),
        answer_waiter=lambda esc, **k: {},
        auto_merge=False, preflight=False, push=False, test_gate=False, max_rounds=1,
    )
    driver.run()
    # Round 1 begins at t=0, so the trigger instant is base. Capturing the floor after
    # _summon would make this base+60s and silently veto a whole register_delay of
    # genuine sign-offs.
    assert driver._round_summon_time == base


def test_a_signoff_written_before_the_summon_is_refused():
    # MUTATION GUARD for the summon floor itself. Deleting
    # `cutoff = max(cutoff, self._round_summon_time)` must not leave the suite green:
    # this sign-off POST-dates the head's commit (so the committer-date bar alone
    # admits it) but PRE-dates the trigger, so it cannot be a response to it and was
    # computed against the previous head. Only the floor refuses it.
    base = datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc)
    driver, gh = _anchoring_driver(head="c1", reviews=[], inline=[])
    driver._round_review_head_time = base.isoformat()               # head committed 05:00
    driver._round_summon_time = base + timedelta(minutes=20)        # trigger      05:20
    driver._anchor_clean_signal(
        "claude", stamp=(base + timedelta(minutes=10)).isoformat())  # sign-off    05:10
    assert driver._clean_signal_head == {}
    assert _merged(driver) is False


def test_a_structurally_proven_signal_is_not_vetoed_by_a_trailing_stamp():
    # A reaction whose id was absent from the pre-summon baseline demonstrably arrived
    # after the trigger — a clock-free ORDERING fact. The date test compares GitHub's
    # clock against ours, so skew can make that same reaction's stamp look older than
    # our floor. The ordering proof must win, or the identical +1 would be ACCEPTED
    # with no timestamp and REFUSED with one — more evidence, worse outcome.
    base = datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc)
    driver, gh = _anchoring_driver(head="c1", reviews=[], inline=[])
    driver._round_review_head_time = base.isoformat()
    driver._round_summon_time = base + timedelta(minutes=20)
    trailing = (base + timedelta(minutes=10)).isoformat()   # behind our local floor
    driver._anchor_clean_signal("claude", stamp=trailing, proven_fresh=True)
    assert driver._clean_signal_head == {"claude": "c1"}
    # …and the same stamp WITHOUT the structural proof is still refused.
    driver._clean_signal_head.clear()
    driver._anchor_clean_signal("claude", stamp=trailing)
    assert driver._clean_signal_head == {}


def test_a_signoff_between_the_trigger_and_the_delay_end_still_anchors():
    # The unit consequence of the above: with the floor at the trigger, a sign-off
    # written 30s after the trigger — i.e. still inside the register delay — clears the
    # cutoff and anchors. With the floor at trigger+60 it would be refused.
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    driver, gh = _anchoring_driver(head="c1", reviews=[], inline=[])
    driver._round_review_head_time = base.isoformat()      # c1 was committed, then…
    driver._round_summon_time = base                       # …reviewers were triggered
    driver._anchor_clean_signal("claude", stamp=(base + timedelta(seconds=30)).isoformat())
    assert driver._clean_signal_head == {"claude": "c1"}
    # …while one written BEFORE the trigger is still refused.
    driver._clean_signal_head.clear()
    driver._anchor_clean_signal("claude", stamp=(base - timedelta(seconds=30)).isoformat())
    assert driver._clean_signal_head == {}


# ===========================================================================
# MAJOR 3 — the ATTENDED (auto-merge off, TTY) merge is gated too
# ===========================================================================

def _attended(driver, monkeypatch, answer="yes"):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: answer)
    return driver._clean_exit(1).merged


def test_attended_merge_warns_on_unreviewed_head_and_pins(monkeypatch, capsys):
    # auto-merge OFF + a TTY: the human is prompted, but must be TOLD the head is
    # unreviewed BEFORE answering, and the merge must be PINNED to the verified head
    # so an unreviewed push cannot land under their "yes".
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"}, rev_parse_seq=["c2"],
        last_substantive="c2", reviews=[_review("claude", "c0")], auto_merge=False)
    merged = _attended(driver, monkeypatch)
    out = capsys.readouterr().out
    assert "[unreviewed-head]" in out          # surfaced BEFORE the prompt
    assert merged is True                      # the human's call is still honoured
    pinned = gh.matching("gh", "merge", "--match-head-commit")
    assert pinned and "c2" in pinned[0]        # …but pinned to the gate's head


def test_attended_merge_on_a_reviewed_head_is_silent_and_pinned(monkeypatch, capsys):
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"}, rev_parse_seq=["c1"],
        last_substantive="c1", reviews=[_review("claude", "c1")], auto_merge=False)
    merged = _attended(driver, monkeypatch)
    out = capsys.readouterr().out
    assert "[unreviewed-head]" not in out and "[gate-unverified]" not in out
    assert merged is True
    assert gh.matching("gh", "merge", "--match-head-commit")


# ===========================================================================
# MAJOR 4 — quoted placeholder vocabulary is documentary, not a placeholder
# ===========================================================================

def test_quoted_error_vocabulary_is_not_a_placeholder():
    quoting = ("The test asserts `Review run failed.` yields the errored signal; "
               "LGTM otherwise.")
    assert detectors.is_placeholder_review_body(quoting) is False
    # …and a real self-report still is.
    assert detectors.is_placeholder_review_body(
        "The review run failed; please try again.") is True


def test_review_quoting_error_copy_keeps_its_sha_and_merges():
    # A genuine review whose PROSE cites placeholder copy (exactly what Buddhi's own
    # PRs contain) must keep its commit credit, not be over-blocked into a handback.
    body = "The test asserts `Review run failed.` yields the errored signal; LGTM."
    assert _genuine_review_shas_by_bot(
        [_review("claude", "c1", body=body)], []) == {"claude": {"c1"}}
    driver, gh = _gate_driver(
        fleet={"claude"}, reviewed_ever={"claude"}, rev_parse_seq=["c1"],
        last_substantive="c1", reviews=[_review("claude", "c1", body=body)])
    assert _merged(driver) is True


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
