"""Autonomous-action transparency — the ``⚙ [auto]`` marker.

Every action the loop takes WITHOUT a per-action confirmation announces itself on
**STDOUT** via :func:`automation_notice`. It is user-facing and **always ships**
(never a strippable diagnostics layer) — on a public skill or an unfamiliar repo
these must always be visible. It is greppable: the ``⚙`` gear and the literal
``[auto]`` tag appear on no other log line, so ``grep '\\[auto\\]'`` returns the
full action trail. The leading glyph alone signals intent.

Status → glyph:
  ``do`` ⚙ intent/doing · ``done`` ✓ · ``skip`` ⊘ skipped-by-config ·
  ``fallback`` ⚠ recoverable fallback · ``stop`` ✗ hard stop / handed back.

Colour auto-strips under ``NO_COLOR`` / ``BUDDHI_LOOP_NO_COLOR`` (same env names the
rest of the pipeline honours); the glyph still prints. ``hint=`` names the governing
flag verbatim so the reader knows how to turn the behaviour off.
"""
from __future__ import annotations

import os
import sys
from typing import Optional, TextIO

# status -> (glyph, ANSI colour code)
_STATUS = {
    "do": ("⚙", "36"),        # ⚙ cyan   — intent / doing
    "done": ("✓", "32"),      # ✓ green  — completed
    "skip": ("⊘", "33"),      # ⊘ yellow — skipped by config
    "fallback": ("⚠", "33"),  # ⚠ yellow — recoverable fallback
    "stop": ("✗", "31"),      # ✗ red    — hard stop / handed back
}


def _colour_enabled(stream: TextIO) -> bool:
    if "NO_COLOR" in os.environ or "BUDDHI_LOOP_NO_COLOR" in os.environ:
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def automation_notice(
    action: str,
    detail: str = "",
    *,
    status: str = "do",
    hint: Optional[str] = None,
    stream: Optional[TextIO] = None,
) -> str:
    """Emit one ``⚙ [auto]`` line to stdout and return its uncoloured text.

    The returned string (glyph + ``[auto]`` + action + detail + hint, no colour) is
    what tests assert on, so the contract is pinned independent of the terminal.
    """
    glyph, colour = _STATUS.get(status, _STATUS["do"])
    body = f"{glyph} [auto] {action}"
    if detail:
        body += f" — {detail}"
    if hint:
        body += f"   ({hint})"
    out = stream if stream is not None else sys.stdout
    line = f"\033[{colour}m{body}\033[0m" if _colour_enabled(out) else body
    print(line, file=out)
    return body
