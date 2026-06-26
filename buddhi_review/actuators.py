"""Turn the kernel's disposition into the substrate action.

``loop.process_comment`` ends at a decision (``fix`` / ``escalate`` / ``skip`` /
``defer`` / ``already-resolved``); this module acts on it. The split keeps
``loop.py`` kernel-pure: the actuators own subprocesses and files, the loop owns
the decision pipeline.

* ``fix``       → :func:`buddhi_review.fix_apply.apply_fix` (snapshot/rollback +
                  the safety floor). After a bounded retry, a transient failure
                  ESCALATES rather than retrying on another model.
* ``escalate``  → already delivered by the kernel through ``ConsoleEscalation``
                  during ``run_embedded``; nothing to re-deliver here. The round
                  driver waits via :mod:`buddhi_review.escalation_wait`.
* ``skip`` / ``already-resolved`` → no action.
* ``defer``     → the kernel's interrupt budget said "not now"; surfaced in the
                  result, never silently dropped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from buddhi_review import plan_profile
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.fix_apply import FixOutcome, apply_fix
from buddhi_review.loop import Comment, CommentResult
from buddhi_review.notifier import Ask

# fix-apply seam: (comment, result) -> FixOutcome. Injectable for tests; the CLI
# binds the real apply_fix with the worktree cwd + verify mode.
FixDispatch = Callable[[Comment, CommentResult], FixOutcome]


@dataclass
class ActionResult:
    comment_id: str
    disposition: str
    final: str  # fixed | skipped-invalid | skipped | escalated | deferred | already-resolved
    detail: str = ""
    # Carried up from FixOutcome: a fix whose rollback could not be proven clean
    # poisons the shared worktree. The round driver halts before pushing on this,
    # regardless of ``final`` — so it is plumbed for EVERY fix disposition.
    rollback_failed: bool = False


def default_fix_dispatch(
    *,
    cwd: str,
    plan: Optional[str] = None,
    verify_runner: Optional[Callable[[str], str]] = None,
    verify_mode: str = "auto",
) -> FixDispatch:
    def _dispatch(comment: Comment, result: CommentResult) -> FixOutcome:
        # result.classification (aliased to `c`) is guaranteed to be a valid
        # Classification object when disposition is "fix", and its attributes
        # like model, effort, and reason always have default string values
        # (never None) as per the Classification dataclass definition.
        c = result.classification
        return apply_fix(
            comment.text,
            cwd=cwd,
            # The classifier emits a tier ALIAS; resolve it through the user's
            # plan at the dispatch boundary (pro: opus → sonnet).
            # `plan` is passed explicitly so resolution never re-reads config.yaml
            # per fix (which would re-warn a config-less user every comment).
            model=plan_profile.tier_model(c.model, plan),
            effort=c.effort,
            reason=c.reason,
            diff_hunk=comment.diff_hunk or "",
            commented_files=[comment.path] if comment.path else (),
            verify_runner=verify_runner,
            verify_mode=verify_mode,
        )

    return _dispatch


def act_on_result(
    comment: Comment,
    result: CommentResult,
    *,
    adapter: ReviewAdapter,
    fix_dispatch: Optional[FixDispatch] = None,
) -> ActionResult:
    d = result.disposition
    if d == "fix":
        if fix_dispatch is None:
            return ActionResult(comment.id, d, "deferred", "no fixer wired (decision-only run)")
        outcome = fix_dispatch(comment, result)
        rb = outcome.rollback_failed
        if outcome.status == "applied":
            return ActionResult(comment.id, d, "fixed", outcome.detail, rollback_failed=rb)
        if outcome.status in ("skipped", "rejected"):
            return ActionResult(comment.id, d, "skipped-invalid", outcome.detail, rollback_failed=rb)
        # transient-failed → escalate rather than retrying on another model
        ask = Ask(
            id=f"fix-{comment.id}",
            question=f"The automated fix for comment {comment.id} failed — how should it be handled?",
            options=[
                "Apply the change manually (the loop moves on)",
                "Skip this comment",
                "Stop the run",
            ],
            recommended_index=0,
            detail=outcome.detail,
        )
        adapter.escalation.delivered.append(ask)
        adapter.escalation.notifier.send(ask)
        return ActionResult(comment.id, d, "escalated", outcome.detail, rollback_failed=rb)
    if d == "escalate":
        # The kernel delivered the pre-reasoned ask via ConsoleEscalation in
        # run_embedded; the answer-poll happens at round level (escalation_wait).
        return ActionResult(comment.id, d, "escalated")
    if d == "skip":
        return ActionResult(comment.id, d, "skipped")
    if d == "defer":
        return ActionResult(comment.id, d, "deferred", "interrupt budget spent — deferred, not dropped")
    return ActionResult(comment.id, d, d)
