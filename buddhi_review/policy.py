"""The review-domain ``PolicyPack`` — the single policy contract the kernel reads.

This pack tunes the kernel's seven decisions for PR review: the discard predicate
(out-of-scope comments), the effort taxonomy (low/medium/high → haiku/sonnet/opus
aliases, resolved to concrete models per the user's plan elsewhere), the judgment
threshold (model-vs-human routing), the validity rule (a business question needs a
payload), the ask phrasings, and the bounded interrupt budget that paces human
escalations. Nothing domain-specific lives in the kernel; it all enters here.

:data:`ESCALATION_CRITERIA` is the prose face of the step-4 judgment policy: the
single statement of *when* a review comment needs the project owner rather than an
automatic fix. The classifier (:func:`buddhi_review.classify.build_prompt`) weaves
it in as trusted instruction, so the "fix it / ask the owner" rule is authored in
exactly one place.
"""
from __future__ import annotations

from buddhi.policy import (
    AskPolicy,
    BudgetKnobs,
    ConvergenceHeuristics,
    EffortTaxonomy,
    JudgmentPolicy,
    PolicyPack,
)
from buddhi.stage0.conditioning import Item


def _out_of_scope(item: Item) -> bool:
    """Step-1 discard predicate: comments the classifier marked OUTDATED / INVALID
    are stamped ``out_of_scope`` in meta (see :mod:`buddhi_review.mapping`)."""
    return bool(item.meta.get("out_of_scope", False))


def _question_has_payload(question) -> bool:
    """Step-5 validity rule: a business question with an empty payload is malformed."""
    return bool(question.payload.strip())


# ── Escalation criteria: WHEN a comment needs the owner (step-4 judgment) ──────
# The prose companion to ``JudgmentPolicy`` below. The classifier weaves this in
# as TRUSTED instruction text (above its security fence) so the one decision —
# resolve the comment automatically vs. ask the project owner — is stated once.
#
# A comment that genuinely needs the owner's judgment is ALWAYS surfaced to them
# (labelled BUSINESS_QUESTION, with the reason attached), never resolved silently.
# This text only sharpens WHICH comments clear that bar; it never trades an
# escalation away for speed. The three triggers are each doc-gated — a comment
# escalates only when the repository's own docs and conventions do not already
# settle it.
ESCALATION_CRITERIA = (
    "ESCALATION — when to ask the project owner instead of resolving it yourself:\n"
    "Label a comment BUSINESS_QUESTION only when its correct resolution is NOT "
    "settled by the code plus the project's own documentation, conventions, and "
    "PR context — because resolving it would impose a judgment that is the owner's "
    "to make. Use BUSINESS_QUESTION when the comment matches ANY of these three "
    "triggers AND the available docs / context do NOT clearly settle it:\n"
    "  1. STRATEGY / NON-TECHNICAL — it concerns what the product should do, a "
    "business rule or policy, scope, priorities, or another non-engineering matter "
    "that the docs or PR description don't already answer.\n"
    "  2. AMBIGUOUS TECHNICAL — it raises a technical point with MORE THAN ONE "
    "genuinely defensible answer (an alternative architecture, data model, or API "
    "shape, or a trade-off between competing technical goods where choosing one "
    "forecloses the other at a cost the owner should weigh) that nothing in the "
    "context or docs resolves. A technical point with one obviously-correct answer "
    "any competent engineer would pick — including a routine optimization or "
    "refactor that merely names a trade-off such as memory-vs-speed or "
    "clarity-vs-performance — is NOT this; it is SUBSTANTIVE or COSMETIC.\n"
    "  3. CONTENT / DESIGN STYLE — it is a taste call about USER-FACING content "
    "(wording, tone, messaging, copy) or visual / UX design (layout, hierarchy, "
    "interaction) that would change the product's voice, message, or user "
    "experience in a way the owner should own, and the docs / design system / "
    "conventions don't dictate one choice. Guidance about how the CODE itself "
    "reads — variable or function naming, formatting, readability, refactoring — "
    "is NOT this; that is COSMETIC. A low-stakes clarity, typo, or tone tweak with "
    "one obvious improvement is COSMETIC or SUBSTANTIVE, not this.\n"
    "You are running inside the repository: when a comment hinges on a documented "
    "convention, rule, or design decision, consult the obvious sources (README, "
    "the contributor guide, any design or conventions doc, and the file or module "
    "the comment touches) BEFORE concluding the answer is undocumented. If they "
    "settle the choice, it is NOT a business question — pick the technical label "
    "and let the loop apply the documented answer. A doc settles the comment only "
    "when it directly and unambiguously dictates this exact choice; a "
    "loosely-related or merely-similar passage does NOT settle a strategy, taste, "
    "or architecture fork — when in doubt whether the doc truly decides it, "
    "escalate.\n"
    "Default conservatively toward NOT a business question: when a comment is a "
    "plain coding matter with one clearly-correct resolution, prefer a technical "
    "label so the loop fixes it rather than interrupting the owner. But do NOT "
    "collapse a genuine multiple-defensible-answers, strategy, or content/design "
    "taste call into a technical label merely because it is actionable — if "
    "auto-picking one resolution would silently make a product, architectural, or "
    "taste decision that is the owner's and the docs don't settle it, it is a "
    "BUSINESS_QUESTION; escalate it.\n"
)


def review_policy_pack(daily_interrupt_budget: int = 25) -> PolicyPack:
    """The concrete review pack.

    The interrupt-budget pacing is NEUTRALIZED here (kernel untouched): every
    genuine business question — a comment whose correct resolution is the owner's
    to make — must reach the owner, never be silently deferred because an earlier
    ask "spent the budget". The kernel's graduated ask bar and its high-stakes
    bypass are both flattened to zero (``base = cap = high_stakes_threshold =
    0.0``), so :func:`buddhi.decisions.aggregate_budget.aggregate_budget` always
    ADMITs a valid ask regardless of how many were admitted before. The
    ``daily_interrupt_budget`` parameter is retained for API stability but no
    longer gates any ask (the flat-zero bar is spend-independent). The unrelated
    exclusion lattice (an excluded reviewer's source) is untouched — only the
    budget pacing is removed."""
    return PolicyPack(
        name="buddhi-review",
        version="1",
        discard_predicates=(_out_of_scope,),
        effort_taxonomy=EffortTaxonomy(
            levels=("low", "medium", "high"),
            ceiling="high",
            model_by_effort={"low": "haiku", "medium": "sonnet", "high": "opus"},
        ),
        convergence=ConvergenceHeuristics(),
        judgment=JudgmentPolicy(business_question_threshold=0.6),
        validity_rules=(_question_has_payload,),
        ask=AskPolicy(
            option_phrasings=(
                "Apply the suggested change",
                "Skip — the suggestion is not valid here",
                "Defer — this needs your judgment",
            ),
            recommended_index=0,
            min_options=2,
            max_options=4,
        ),
        budget=BudgetKnobs(
            # A flat-zero bar (base == cap) never rises with spend, so the
            # graduated ask bar admits every ask; a zero high-stakes threshold
            # additionally makes every ask take the high-stakes bypass. Together:
            # no valid business question is ever deferred for budget. The ceiling
            # is kept > 0 (the kernel requires >= 1) but is inert under this bar.
            daily_interrupt_budget=max(1, daily_interrupt_budget),
            base=0.0,
            cap=0.0,
            high_stakes_threshold=0.0,
        ),
    )
