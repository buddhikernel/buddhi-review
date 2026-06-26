"""Concrete implementations of the five kernel seams for the review skill.

The kernel exposes the seams as interfaces and never implements them. This module
supplies:

  * ``ReviewStore``       — Store: interrupt counters + the two-tier exclusion
                            lattice, with the review loop's 3 cause-buckets mapped
                            onto it (quota / pr-too-large → permanent;
                            errored → transient, retractable = the errored comeback).
  * ``ReviewRouter``      — Router: stakes-based effort recommendation.
  * ``ConsoleEscalation`` — Escalation transport: translates the kernel's
                            ``PreReasonedAsk`` into a channel-agnostic ``Ask`` and
                            delivers it via the **console** notifier.
  * ``SignaledOOBSource`` — OOB source: declares the substrate CAN observe a
                            *signaled* out-of-band resolution. It acts only on an
                            explicit signal; it never auto-detects a resolution.

The PolicyPack seam is :func:`buddhi_review.policy.review_policy_pack`.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Set

from buddhi.policy import GLOBAL_SCOPE
from buddhi.seams.router import RouterPick

from buddhi_review.notifier import Ask, ConsoleNotifier, Notifier


class ReviewStore:
    """Store seam: scope-keyed interrupt counters + the two-tier exclusion lattice.

    The review loop's three independent do-not-re-request causes map on as:
    ``quota`` and ``pr_too_large`` → the **permanent** tier (no comeback);
    ``errored`` → the **transient** tier (retractable — a later substantive comment
    brings the bot back). Causes never cross tiers.
    """

    def __init__(self) -> None:
        self._interrupts: Dict[str, int] = {}
        self._permanent: Set[str] = set()
        self._transient: Set[str] = set()

    # --- kernel Store interface -------------------------------------------------
    def interrupts_today(self, scope: str = GLOBAL_SCOPE) -> int:
        return self._interrupts.get(scope, 0)

    def record_interrupt(self, scope: str = GLOBAL_SCOPE) -> None:
        self._interrupts[scope] = self._interrupts.get(scope, 0) + 1

    def is_excluded(self, source: str) -> bool:
        return source in self._permanent or source in self._transient

    def exclude_permanent(self, source: str) -> None:
        self._permanent.add(source)

    def exclude_transient(self, source: str) -> None:
        self._transient.add(source)

    def retract_transient(self, source: str) -> None:
        self._transient.discard(source)  # never touches the permanent tier

    # --- review-loop cause buckets (semantic aliases) ---------------------------
    def exclude_quota(self, source: str) -> None:
        self.exclude_permanent(source)

    def exclude_pr_too_large(self, source: str) -> None:
        self.exclude_permanent(source)

    def exclude_errored(self, source: str) -> None:
        self.exclude_transient(source)

    def errored_comeback(self, source: str) -> None:
        """A bot that later posts a substantive comment comes back — but only from
        the transient (errored) tier; a true-quota/pr-too-large cap survives."""
        self.retract_transient(source)


class ReviewRouter:
    """Router seam: recommend effort from the item's stakes. The pack's effort
    taxonomy resolves the effort alias to a model; the user's Claude plan resolves
    the alias to a concrete model elsewhere."""

    def recommend(self, item) -> RouterPick:
        # Map stakes to a canonical tier ALIAS (opus / sonnet / haiku) — the values
        # the kernel + the user's plan profile resolve — paired with the role effort.
        stakes = getattr(item, "stakes", 0.0) if item is not None else 0.0
        if stakes >= 0.7:
            model, effort = "opus", "high"
        elif stakes >= 0.4:
            model, effort = "sonnet", "medium"
        else:
            model, effort = "haiku", "low"
        return RouterPick(model=model, effort=effort, rationale="stakes-based")


class ConsoleEscalation:
    """Escalation transport seam → the console answer-file channel.

    Records each delivered ask (so the loop can poll for the answer) and sends it to
    the notifier. The kernel holds only the interface; this is the adapter's transport.
    """

    def __init__(self, notifier: Optional[Notifier] = None) -> None:
        self.notifier: Notifier = notifier or ConsoleNotifier()
        self.delivered: List[Any] = []  # the kernel PreReasonedAsk objects, in order

    def deliver(self, ask) -> None:
        self.delivered.append(ask)
        self.notifier.send(self.to_channel_ask(ask))

    @staticmethod
    def to_channel_ask(ask) -> Ask:
        return Ask(
            id=ask.question.item_id,
            question=ask.question.question,
            options=[opt.label for opt in ask.options],
            recommended_index=ask.recommended_index,
            detail=ask.question.payload,
        )


class SignaledOOBSource:
    """OOB source seam: the **signaled** source.

    Declares the substrate CAN observe an out-of-band resolution (a human explicitly
    signals "handled" — the ``.continue`` flag / an explicitly resolved thread). The
    actual check is the adapter's ``detect_resolved`` via an injected predicate. It
    acts only on an explicit signal and **never** auto-detects a resolution; a
    contributor must never add autonomous detection here.
    """

    def __init__(self, signal: Optional[Callable[[object], bool]] = None) -> None:
        self._signal = signal or (lambda item: False)

    def can_observe_oob(self) -> bool:
        return True

    def is_signaled(self, item) -> bool:
        return bool(self._signal(item))
