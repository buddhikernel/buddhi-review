"""The ``ESCALATED`` actuator — poll the console answer file for the human's answer.

The whole escalation rail runs in-process. The ask
was already delivered by ``ConsoleEscalation.deliver`` (panel + ``O_EXCL``
answer file); this module is the plain blocking read: poll
``ConsoleNotifier.read_answer`` (~2s) until the user types a number / free text
and saves, or the wait times out.

``sleep``/``clock`` are injectable so tests never sleep for real.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Dict, List, Optional

from buddhi_review.notifier import Ask, ConsoleNotifier, Notifier
from buddhi_review.seams import ConsoleEscalation


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


# 12h default answer window, overridable via BUDDHI_BIZQ_ANSWER_TIMEOUT.
ANSWER_TIMEOUT = _env_float("BUDDHI_BIZQ_ANSWER_TIMEOUT", 43200.0)
POLL_INTERVAL = 2.0


def wait_for_answer(
    notifier: Notifier,
    ask: Ask,
    *,
    timeout: Optional[float] = None,
    poll_interval: float = POLL_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Optional[str]:
    """Block until the answer file carries an answer, or ``timeout`` elapses.
    Returns the raw answer text (a number or free text), or None on timeout."""
    deadline = clock() + (ANSWER_TIMEOUT if timeout is None else timeout)
    while True:
        answer = notifier.read_answer(ask)
        if answer:
            return answer
        if clock() >= deadline:
            return None
        sleep(poll_interval)


def wait_for_delivered(
    escalation: ConsoleEscalation,
    *,
    timeout: Optional[float] = None,
    poll_interval: float = POLL_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, Optional[str]]:
    """Wait on every ask the escalation seam delivered this run, in delivery
    order. Returns ``{ask_id: answer-or-None}``; an answered ask's file is
    cleared so a later re-ask can recreate it with ``O_EXCL``."""
    notifier = escalation.notifier
    asks: List[Ask] = [
        a if isinstance(a, Ask) else ConsoleEscalation.to_channel_ask(a)
        for a in escalation.delivered
    ]
    answers: Dict[str, Optional[str]] = {}
    start_time = clock()
    total_timeout = ANSWER_TIMEOUT if timeout is None else timeout
    for ask in asks:
        elapsed = clock() - start_time
        remaining = max(0.0, total_timeout - elapsed)
        answer = wait_for_answer(
            notifier, ask, timeout=remaining, poll_interval=poll_interval,
            sleep=sleep, clock=clock,
        )
        answers[ask.id] = answer
        if answer and isinstance(notifier, ConsoleNotifier):
            notifier.clear(ask)
        if ask.id.startswith("fix-") and (answer or "").strip() == "3":
            break
    return answers
