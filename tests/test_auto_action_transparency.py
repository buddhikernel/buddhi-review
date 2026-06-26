"""The ``⚙ [auto]`` autonomous-action transparency contract (build-spec §3.6).

Ported near-verbatim from the reference ``test_auto_action_transparency.py`` and
rewired onto :func:`buddhi_review.transparency.automation_notice`. Every action
the loop takes WITHOUT a per-action confirmation announces itself on this rail —
it is user-facing, always ships (never a strippable diagnostics layer), and is
greppable: the ``⚙`` gear and the literal ``[auto]`` tag appear on no other line.

Basic format + per-status glyph + non-tty-no-colour are pinned in
``test_notifier_transparency.py``; this file pins the rest of the contract:
stdout-not-stderr, greppability for EVERY status, the unknown-status fallback,
hint rendering, optional detail, and colour-strips-but-glyph-still-prints.
"""
from __future__ import annotations

import io
import re

import pytest

from buddhi_review.transparency import automation_notice

# Every status the loop emits, with its glyph (free uses "stop", not "block").
_STATUS_GLYPHS = {
    "do": "⚙",
    "done": "✓",
    "skip": "⊘",
    "fallback": "⚠",
    "stop": "✗",
}


class _FakeTTY(io.StringIO):
    """A stream that claims to be a terminal — so colour logic is exercised."""

    def isatty(self) -> bool:
        return True


def test_goes_to_stdout_not_stderr(capsys):
    automation_notice("squash-merge", "PR #1 clean", status="do")
    captured = capsys.readouterr()
    assert "[auto]" in captured.out
    assert captured.err == ""  # never the strippable stderr diagnostics channel


def test_greppable_tag_present_for_every_status(capsys):
    for status in _STATUS_GLYPHS:
        automation_notice("act", status=status)
    out = capsys.readouterr().out
    # one greppable [auto] line per status — `grep '\[auto\]'` returns the trail.
    assert out.count("[auto]") == len(_STATUS_GLYPHS)


def test_status_glyph_mapping():
    for status, glyph in _STATUS_GLYPHS.items():
        body = automation_notice("act", status=status)
        assert body.startswith(f"{glyph} [auto] act")


def test_unknown_status_falls_back_to_do():
    body = automation_notice("act", status="not-a-status")
    assert body.startswith("⚙ [auto] act")  # the intent glyph is the default


def test_hint_rendered_verbatim_in_parens():
    body = automation_notice("squash-merge", "left open", status="skip",
                             hint="enable: --auto-merge")
    # the hint names the governing flag verbatim, inside trailing parentheses.
    assert body == "⊘ [auto] squash-merge — left open   (enable: --auto-merge)"


def test_no_hint_no_parens():
    body = automation_notice("act", "detail only")
    assert "(" not in body and ")" not in body


def test_detail_optional_no_em_dash():
    body = automation_notice("act")
    assert "—" not in body  # no detail → no em-dash separator
    assert body == "⚙ [auto] act"


def test_gear_and_auto_tag_are_greppably_unique():
    body = automation_notice("re-request", "claude re-request failed", status="fallback")
    # the gear + [auto] tag lead the line and appear exactly once each, so a
    # single grep recovers the whole action trail with no false positives.
    assert re.match(r"^[⚙✓⊘⚠✗] \[auto\] ", body)
    assert body.count("[auto]") == 1


def test_colour_strips_under_no_color_but_glyph_still_prints(monkeypatch):
    tty = _FakeTTY()
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("BUDDHI_LOOP_NO_COLOR", raising=False)
    automation_notice("act", status="stop", stream=tty)
    assert "\033[" in tty.getvalue()  # a real TTY gets ANSI colour

    for var in ("NO_COLOR", "BUDDHI_LOOP_NO_COLOR"):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("BUDDHI_LOOP_NO_COLOR", raising=False)
        monkeypatch.setenv(var, "1")
        out = _FakeTTY()
        automation_notice("act", status="stop", stream=out)
        text = out.getvalue()
        assert "\033[" not in text       # colour stripped
        assert "✗ [auto] act" in text     # …but the glyph + tag still print


def test_returns_uncoloured_body_independent_of_terminal():
    # The returned body is always the uncoloured text (what callers/tests assert
    # on), regardless of whether the stream would have been coloured.
    tty = _FakeTTY()
    body = automation_notice("merge", "done", status="done", stream=tty)
    assert body == "✓ [auto] merge — done"
    assert "\033[" not in body
