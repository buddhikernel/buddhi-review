"""Map a classified review comment onto a kernel ``RawItem``.

The design split that makes this a *kernel* rebuild and not a reimplementation:
the **kernel decides the coarse disposition** (act / ask-a-human / skip / stop
under budget) from the item's fields; the **review label** (carried in ``meta``)
tells the adapter *how* to act once the kernel says "act". So this function
translates each label into the item fields that steer the kernel to the intended
branch:

  OUTDATED / INVALID                          → ``out_of_scope`` → DISCARDED (skip)
  SUBSTANTIVE / COSMETIC                       → high model_confidence, substantive
                                                 change → MODEL_HANDLED (dispatch fixer)
  BUSINESS_QUESTION / PR_DESCRIPTION /
  CLASSIFICATION_FAILED                        → low model_confidence → HUMAN route
                                                 → ESCALATED (console ask) [or DENIED
                                                 if the interrupt budget is spent]
"""
from __future__ import annotations

from buddhi.stage0.conditioning import RawItem

from buddhi_review.classify import DISCARD_LABELS, FIX_LABELS

_SEVERITY_STAKES = {"critical": 0.9, "high": 0.7, "medium": 0.5, "low": 0.3}


def raw_item_for(
    comment_id: str,
    label: str,
    *,
    severity: str = "medium",
    source: str = "reviewer",
    text: str = "",
) -> RawItem:
    stakes = _SEVERITY_STAKES.get(severity, 0.5)
    payload = text or label
    if label in DISCARD_LABELS:
        return RawItem(
            id=comment_id,
            payload=payload,
            source=source,
            meta={"out_of_scope": True, "review_label": label},
        )
    if label in FIX_LABELS:
        # The model can handle it: high confidence + a substantive change → MODEL_HANDLED.
        return RawItem(
            id=comment_id,
            payload=payload,
            source=source,
            stakes=stakes,
            model_confidence=0.9,
            changes=("substantive",),
            meta={"review_label": label},
        )
    # BUSINESS_QUESTION / PR_DESCRIPTION / CLASSIFICATION_FAILED → ask a human.
    return RawItem(
        id=comment_id,
        payload=payload,
        source=source,
        stakes=max(stakes, 0.5),
        model_confidence=0.1,
        changes=("substantive",),
        meta={"review_label": label},
    )
