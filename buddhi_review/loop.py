"""The review orchestration — classify each comment, let the kernel decide its
disposition, and act.

This module is the kernel-driven core of the review→fix→verify loop. For each PR
comment it: classifies (``claude -p`` seam) → maps the label to a kernel ``RawItem``
→ ``run_embedded`` (the seven decisions) → reads the kernel's disposition. The
*coarse* decision (fix / ask-a-human / skip / defer-under-budget) is the kernel's;
this module turns it into the substrate action.

This module ships the full classify → kernel-decide → disposition pipeline, the
console escalation, and the transparency layer. The substrate actuators that hang
off each disposition — the real ``gh`` comment fetch, the snapshot/rollback fix
apply, the answer-poll round loop, the squash-merge — are each a clearly-named
seam here so wiring them in does not disturb the kernel core.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from buddhi.closure import (
    CONVERGED,
    DENIED,
    DISCARDED,
    ESCALATED,
    INVALID_ASK,
    MODEL_HANDLED,
)
from buddhi.adapter import Budget

from buddhi_review.adapter import ReviewAdapter
from buddhi_review.classify import Classification, classify_comment
from buddhi_review.mapping import raw_item_for
from buddhi_review.protections import named_conflicts, unmapped_conflict

# Kernel status → the review disposition the loop acts on. The contract is
# "same label + same disposition", so this map is load-bearing.
DISPOSITION = {
    MODEL_HANDLED: "fix",            # dispatch the fixer
    CONVERGED: "already-resolved",   # nothing to do
    DISCARDED: "skip",               # OUTDATED / INVALID — do not act
    ESCALATED: "escalate",           # asked a human via the console channel
    INVALID_ASK: "escalate",         # malformed question still surfaces to a human
    DENIED: "defer",                 # interrupt budget spent — defer, do not drop
}


@dataclass
class Comment:
    id: str
    text: str
    source: str = "reviewer"
    path: Optional[str] = None       # file path from pulls/<pr>/comments, if present
    diff_hunk: Optional[str] = None  # diff context from pulls/<pr>/comments, if present
    created_at: Optional[str] = None  # ISO-8601 stamp — drives the errored comeback
    updated_at: Optional[str] = None  # ISO-8601 edit stamp; the errored-comeback
    # candidate is updated_at-then-created_at, so an EDITED substantive comment can
    # prove recovery by its edit time (the recorded error stamp stays created_at).
    from_issue_channel: bool = False  # True for issues/<pr>/comments (the PR
    # conversation timeline). Per the claude-code-review.yml contract the loop
    # only fixes INLINE findings; the issue channel is scanned for the clean
    # sentinel + signals only, never returned as an actionable finding.
    in_reply_to: Optional[str] = None  # a thread reply's ROOT comment id

    @property
    def root_id(self) -> str:
        """The review thread's root comment — what a protections hold is keyed on."""
        return self.in_reply_to or self.id


# The disposition of a comment whose request conflicts with a listed protection:
# it is held for the project owner, never routed through the kernel's discard.
PROTECTION_HOLD = "protection-hold"


@dataclass
class CommentResult:
    comment_id: str
    classification: Classification
    kernel_status: str
    disposition: str
    # The round's pinned protections (complete, numbered in order) this comment
    # was classified against, and every later fixer / verifier call for it reads.
    protections: Tuple[str, ...] = ()
    # The exact protection texts the classifier said the request would break,
    # validated against ``protections`` and the root's owner allowances.
    protection_conflicts: Tuple[str, ...] = ()
    # The exact protection texts the project owner waived for this root's thread.
    allowances: Tuple[str, ...] = ()


def process_comment(
    comment: Comment,
    *,
    adapter: ReviewAdapter,
    classify_runner: Callable[[str], str],
    budget: Budget,
    classify_retries: int = 1,
    prior_attempt: Optional[str] = None,
    protections: Sequence[str] = (),
    allowances: Sequence[str] = (),
) -> CommentResult:
    # path/diff_hunk (captured by ingest) give the classifier the "file or module
    # the comment touches" its escalation criteria tell it to consult; they ride
    # inside build_prompt's inert nonce fence (untrusted PR-payload content).
    protections = tuple(protections)
    classification = classify_comment(
        comment.text, runner=classify_runner, retries=classify_retries,
        path=comment.path, diff_hunk=comment.diff_hunk, prior_attempt=prior_attempt,
        protections=protections, allowances=tuple(allowances),
    )
    conflicts = named_conflicts(classification.protection_conflicts, protections,
                                allowances) if protections else ()
    if protections and not conflicts and unmapped_conflict(
            classification.protection_conflicts, protections):
        # A conflict was claimed but names nothing listed: hold on every protection
        # not already waived, rather than let the claim fall through unseen.
        conflicts = tuple(p for p in protections if p not in set(allowances))
    if conflicts:
        # Held for the owner BEFORE the kernel sees the label: an INVALID /
        # OUTDATED label must not discard a request that would break a protection.
        return CommentResult(
            comment_id=comment.id, classification=classification,
            kernel_status=PROTECTION_HOLD, disposition=PROTECTION_HOLD,
            protections=protections, protection_conflicts=conflicts,
            allowances=tuple(allowances))
    raw = raw_item_for(
        comment.id,
        classification.label,
        severity=classification.severity,
        source=comment.source,
        text=comment.text,
    )
    outcome = adapter.run_embedded(raw, budget)
    return CommentResult(
        comment_id=comment.id,
        classification=classification,
        kernel_status=outcome.status,
        disposition=DISPOSITION.get(outcome.status, "escalate"),
        protections=protections,
        allowances=tuple(allowances),
    )


def held_result(comment: Comment, protections: Sequence[str] = ()) -> CommentResult:
    """The result for a comment whose thread already carries an owner hold: it is
    not classified again and never dispatched; it stays the owner's question."""
    return CommentResult(
        comment_id=comment.id,
        classification=Classification(label="BUSINESS_QUESTION",
                                      reason="held for the project owner"),
        kernel_status=PROTECTION_HOLD, disposition=PROTECTION_HOLD,
        protections=tuple(protections))


def process_comments(
    comments: Sequence[Comment],
    *,
    adapter: Optional[ReviewAdapter] = None,
    classify_runner: Callable[[str], str],
    max_rounds: int = 10,
    classify_retries: int = 1,
    prior_attempts: Optional[Dict[str, str]] = None,
    protections: Sequence[str] = (),
    allowances: Optional[Dict[str, Sequence[str]]] = None,
    held_ids: Iterable[str] = (),
) -> List[CommentResult]:
    """Classify + decide every comment in one round. Returns one result per comment.

    A shared ``adapter`` (hence a shared kernel Store) means the bounded interrupt
    budget paces escalations across the whole batch, exactly as the kernel intends.

    ``protections`` is the round's one pinned list, given to every classifier call;
    ``allowances`` maps a comment id to the texts its thread's owner waived; a
    comment in ``held_ids`` already belongs to an owner hold and is not classified.
    """
    adapter = adapter or ReviewAdapter()
    budget = Budget(rounds_remaining=max_rounds, max_rounds=max_rounds)
    held = set(held_ids)
    return [
        held_result(c, protections) if c.id in held else process_comment(
            c, adapter=adapter, classify_runner=classify_runner,
            budget=budget, classify_retries=classify_retries,
            prior_attempt=(prior_attempts or {}).get(c.id),
            protections=protections, allowances=(allowances or {}).get(c.id, ()),
        )
        for c in comments
    ]
