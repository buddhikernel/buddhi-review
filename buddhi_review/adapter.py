"""The four-verb adapter — re-homes the kernel onto the GitHub PR-review substrate.

Binding B: the adapter is the *caller* that drives the kernel; the kernel calls
back through the five seams. The adapter supplies substrate I/O (read comments from
``gh``, escalate via the console channel, observe a signaled resolution); the kernel
supplies the decision.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from buddhi.adapter import Adapter, Budget, DecisionItem, PolicyResult
from buddhi.closure import evaluate_item
from buddhi.stage0.conditioning import condition

from buddhi_review.policy import review_policy_pack
from buddhi_review.seams import (
    ConsoleEscalation,
    ReviewRouter,
    ReviewStore,
    SignaledOOBSource,
)


class ReviewAdapter(Adapter):
    """Wires the review seams to satisfy ``buddhi.adapter.Adapter`` (four verbs)."""

    def __init__(
        self,
        *,
        pack=None,
        store: Optional[ReviewStore] = None,
        router: Optional[ReviewRouter] = None,
        escalation: Optional[ConsoleEscalation] = None,
        oob_source: Optional[SignaledOOBSource] = None,
        ingest_source: Optional[Callable[[], Iterable[DecisionItem]]] = None,
        daily_interrupt_budget: int = 25,
    ) -> None:
        self.pack = pack or review_policy_pack(daily_interrupt_budget)
        self.store = store or ReviewStore()
        self.router = router or ReviewRouter()
        self.escalation = escalation or ConsoleEscalation()
        self.oob_source = oob_source or SignaledOOBSource()
        self._ingest_source = ingest_source or (lambda: ())

    def ingest(self) -> Iterable[DecisionItem]:
        """Yield the substrate's raw item stream (``DecisionItem`` == ``RawItem``).

        Substrate-specific reading of the PR's reviewer comments. Injected so the
        adapter is testable without ``gh``; the CLI wires the real ``gh`` source.
        """
        return tuple(self._ingest_source())

    def run_embedded(self, item: DecisionItem, budget: Budget) -> PolicyResult:
        """Stage-0 condition one raw item, then run it through the seven decisions.

        When the outcome is an escalation the kernel delivers the pre-reasoned ask
        through the Escalation seam during this call (the loop then polls for the
        human's answer); the adapter does not re-deliver it.
        """
        typed = condition([item], pack=self.pack)[0]
        return evaluate_item(
            item=typed,
            pack=self.pack,
            router=self.router,
            store=self.store,
            escalation=self.escalation,
            oob_source=self.oob_source,
            budget=budget,
        )

    def escalate_async(self, ask) -> None:
        """Deliver a pre-reasoned ask via the Escalation seam (console channel)."""
        self.escalation.deliver(ask)

    def detect_resolved(self, item: DecisionItem) -> bool:
        """Signaled-OOB only: True iff the substrate can observe a resolution AND a
        human signalled this item as handled. It never auto-detects a resolution."""
        if not self.oob_source.can_observe_oob():
            return False
        return self.oob_source.is_signaled(item)
