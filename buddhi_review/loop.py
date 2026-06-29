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
from typing import Callable, List, Optional, Sequence

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
    from_issue_channel: bool = False  # True for issues/<pr>/comments (the PR
    # conversation timeline). Per the claude-code-review.yml contract the loop
    # only fixes INLINE findings; the issue channel is scanned for the clean
    # sentinel + signals only, never returned as an actionable finding.


@dataclass
class CommentResult:
    comment_id: str
    classification: Classification
    kernel_status: str
    disposition: str


def process_comment(
    comment: Comment,
    *,
    adapter: ReviewAdapter,
    classify_runner: Callable[[str], str],
    budget: Budget,
    classify_retries: int = 1,
) -> CommentResult:
    # path/diff_hunk (captured by ingest) give the classifier the "file or module
    # the comment touches" its escalation criteria tell it to consult; they ride
    # inside build_prompt's inert nonce fence (untrusted PR-payload content).
    classification = classify_comment(
        comment.text, runner=classify_runner, retries=classify_retries,
        path=comment.path, diff_hunk=comment.diff_hunk,
    )
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
    )


def process_comments(
    comments: Sequence[Comment],
    *,
    adapter: Optional[ReviewAdapter] = None,
    classify_runner: Callable[[str], str],
    max_rounds: int = 10,
    classify_retries: int = 1,
) -> List[CommentResult]:
    """Classify + decide every comment in one round. Returns one result per comment.

    A shared ``adapter`` (hence a shared kernel Store) means the bounded interrupt
    budget paces escalations across the whole batch, exactly as the kernel intends.
    """
    adapter = adapter or ReviewAdapter()
    budget = Budget(rounds_remaining=max_rounds, max_rounds=max_rounds)
    return [
        process_comment(
            c, adapter=adapter, classify_runner=classify_runner,
            budget=budget, classify_retries=classify_retries,
        )
        for c in comments
    ]
