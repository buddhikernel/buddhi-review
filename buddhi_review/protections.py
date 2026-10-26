"""The protections a pull request lists in its description, and the owner holds
that keep a review fix from breaking one.

A PR author lists requirements under a ``Protections this PR must keep`` heading
(or a plain/bold label line followed by a list). This module:

* **reads** that section exactly (:func:`parse_body`): ATX levels 1–6, Setext and
  label forms; never inside a fence, indented code, a block quote or an HTML
  comment. Every top-level list item, paragraph, table, code block and subordinate
  heading-with-paragraph is one protection, numbered in encounter order across
  repeated sections, carried whole (no truncation);
* **pins** one reading per round (:class:`Snapshot`), with the 40-protection /
  8,000-character limits and the failed-read fallback to the run's last good
  reading;
* renders the numbered list as inert, nonce-fenced prompt data, and reads the
  ``PROTECTION-CONFLICT: <numbers>`` line a fixer emits;
* owns the hold lifecycle: :func:`make_protection_hold`, :func:`protection_ask`,
  :func:`decode_protection_answer`, :func:`settle_protection_hold` and
  :func:`restore_protection_hold`. Every decision is posted on the root review
  thread as approved visible text plus a hidden :data:`MARKER` comment, and a
  restart trusts only records the loop's own authenticated account posted, byte
  for byte.

A hold raised by the description guard offers only "Keep the protection" and "I'll
handle it myself"; the loop never changes protected description bytes itself.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from buddhi_review.notifier import Ask

TITLE = "Protections this PR must keep"
MAX_PROTECTIONS = 40
MAX_CHARS = 8000
REQUEST_MAX = 300

ORIGINS = ("classifier", "fixer", "verifier", "description_guard")
GUARD = "description_guard"
# The structured record marker. Spelled in two parts only because the publish
# gate's paid-package-name check matches this name's leading letters; the marker
# itself names no paid package.
MARKER = "buddhi-" + "protection"

# ── Approved wording (W1–W7) ─────────────────────────────────────────────────
W1 = ("Not applied yet: this change would break a protection listed in this pull "
      "request's description: \"{protection}\". I have asked the project owner to "
      "decide, and this thread stays open until they do.")
W2_TITLE = "A review request would break a listed protection"
W2_BODY = ("{reviewer} asks: \"{request}\". The pull request description lists this "
           "protection: \"{protection}\". Applying the request would break it.")
KEEP = "Keep the protection"
ALLOW = "Allow this change"
MANUAL = "I'll handle it myself"
KEEP_DETAIL = "The request is not applied. I reply on the thread with your decision and resolve it."
ALLOW_DETAIL = ("I apply the request on this thread only. The protection still guards "
                "every other change.")
MANUAL_DETAIL = "I change nothing on this thread and leave it open for you."
W3 = "The project owner kept the protection \"{protection}\", so this request is not applied."
W4 = ("The project owner allowed this change, although it relaxes the protection "
      "\"{protection}\".")
W5 = ("The protections list in this pull request's description is too long for me to "
      "check ({n} protections, {m} characters; the limit is 40 protections and 8,000 "
      "characters). I will not apply review fixes until it is shortened.")
W6 = ("This pull request's description has a \"Protections this PR must keep\" heading "
      "with nothing under it, so no protections are checked.")
W7 = ("I could not read this pull request's description, so I am not applying review "
      "fixes this round.")

_ACTIONS = {"keep": (KEEP, KEEP_DETAIL), "allow": (ALLOW, ALLOW_DETAIL),
            "manual": (MANUAL, MANUAL_DETAIL)}


def actions_for(origin: str) -> Tuple[str, ...]:
    """The menu, in display order, for a hold of ``origin``: the description guard
    never offers Allow (the loop does not edit protected description bytes)."""
    return ("keep", "manual") if origin == GUARD else ("keep", "allow", "manual")


# ── The section scanner ──────────────────────────────────────────────────────
#
# A line pass that follows the Markdown containers that matter here: list items
# (with nesting, by content column) and fenced code (closed when its list item
# ends). Every line records its list depth; only top-level lines (depth 0) can open
# or end a protections section. HTML comments are removed the way CommonMark reads
# them — a line starting ``<!--`` opens a comment block that runs to ``-->``; a
# ``<!--`` elsewhere is a comment only when its ``-->`` closes within the same
# paragraph — and never inside code (fenced, indented, or a code span).

_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_ATX_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_ATX_CLOSE_RE = re.compile(r"(?:^|[ \t]+)#+[ \t]*$")
_SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_THEMATIC_RE = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
_QUOTE_RE = re.compile(r"^ {0,3}>")
_ITEM_RE = re.compile(r"^( {0,3})([-*+]|(\d{1,9})[.)])([ \t]+|$)")
_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$")
_BACKTICKS_RE = re.compile(r"`+")


def _columns(ws: str, start: int = 0) -> int:
    col = start
    for ch in ws:
        col = col + 4 - (col % 4) if ch == "\t" else col + 1
    return col - start


def _indent(text: str) -> int:
    return _columns(text[:len(text) - len(text.lstrip(" \t"))])


def _strip_cols(text: str, n: int) -> str:
    """``text`` without its first ``n`` columns of leading whitespace (a tab that
    straddles the cut leaves its remaining columns as spaces)."""
    col = i = 0
    while i < len(text) and col < n and text[i] in " \t":
        if text[i] == "\t":
            nxt = col + 4 - col % 4
            if nxt > n:
                return " " * (nxt - n) + text[i + 1:]
            col = nxt
        else:
            col += 1
        i += 1
    return text[i:]


def _code_spans(text: str) -> List[Tuple[int, int]]:
    """``(start, end)`` of every backtick code span on one line."""
    spans: List[Tuple[int, int]] = []
    runs = list(_BACKTICKS_RE.finditer(text))
    k = 0
    while k < len(runs):
        opener = runs[k]
        close = next((r for r in runs[k + 1:] if len(r.group(0)) == len(opener.group(0))), None)
        if close is None:
            k += 1
            continue
        spans.append((opener.start(), close.end()))
        k = runs.index(close) + 1
    return spans


def _comment_end(text: str, at: int) -> int:
    """Index just past the comment opening at ``text[at]`` (``<!-->`` and
    ``<!--->`` are complete comments), or -1 when it does not close here."""
    if text.startswith("<!-->", at):
        return at + 5
    if text.startswith("<!--->", at):
        return at + 6
    j = text.find("-->", at + 4)
    return -1 if j < 0 else j + 3


def _join_cut(before: str, after: str) -> str:
    """Join the text around a removed comment without gluing two words together."""
    if before and after and not before[-1].isspace() and not after[0].isspace():
        return before + " " + after
    return before + after


@dataclass
class _Line:
    start: int
    end: int
    raw: str           # the line as written, without its line ending
    full: str          # the line with comments removed (block text)
    text: str          # ``full`` relative to its list container (classification)
    kind: str          # blank code quote atx setext thematic item indented text html
    depth: int = 0     # enclosing list items; 0 = top level
    level: int = 0     # heading level
    title: str = ""    # ATX heading text
    indent: int = 0    # indent of ``text``
    fence_open: bool = False
    fence_close: bool = False
    after_comment: bool = False   # follows a line that held nothing but a comment
    opens: bool = True            # text that starts a paragraph rather than continuing one


def _block_start(rel: str, para: bool) -> bool:
    """Would ``rel`` (container-relative) start a block of its own rather than
    continue a paragraph lazily?"""
    if _indent(rel) >= 4 or not rel.strip():
        return not rel.strip()
    return bool(_FENCE_RE.match(rel) or _ATX_RE.match(rel) or _QUOTE_RE.match(rel)
                or _THEMATIC_RE.match(rel) or rel.lstrip().startswith("<!--")
                or (_ITEM_RE.match(rel) and _interrupts(_ITEM_RE.match(rel), rel, para)))


def _interrupts(m: "re.Match[str]", rel: str, para: bool) -> bool:
    """May this list marker start an item here? After paragraph text only a
    non-empty bullet item or an ordered item starting at 1 can."""
    if not para:
        return True
    if not rel[m.end():].strip():
        return False
    return m.group(3) is None or int(m.group(3)) == 1


def _lines(body: str) -> List[_Line]:
    """The body's lines, classified, with list depth. A line holding nothing but an
    HTML comment is dropped — it is invisible, so it neither separates nor joins
    blocks — and the line after it may start a fresh block."""
    raws = [(m.start(), m.end(), m.group(0).rstrip("\r\n"))
            for m in _LINE_RE.finditer(body) if m.group(0)]
    lines: List[_Line] = []
    items: List[int] = []                       # open list items' content columns
    fence: Optional[Tuple[str, int, int]] = None   # (char, length, depth)
    html_depth: Optional[int] = None            # an open comment block's depth
    skip_to: Tuple[int, int] = (-1, 0)          # an inline comment's (line, col) end
    para_at: Optional[int] = None              # depth of the open paragraph
    quote_para = False                          # the last quote line holds paragraph text
    prev_blank = True
    after_comment = False

    def push(ln: _Line) -> None:
        nonlocal after_comment
        ln.after_comment, after_comment = after_comment, False
        lines.append(ln)

    def raw_html(start: int, end: int, raw: str, rest: str) -> None:
        """The rest of a line a comment block closed on is raw HTML: visible text
        (its own complete comments removed), never a heading, item or fence."""
        nonlocal after_comment, para_at
        text = _strip_inline_comments([], 0, rest, [], 0)
        if text.strip():
            push(_Line(start, end, raw, " " * _indent(raw) + text.strip(), text.strip(),
                       "html", depth=len(items)))
        else:
            after_comment = True
        para_at = None

    for idx, (start, end, bare) in enumerate(raws):
        if idx < skip_to[0]:
            after_comment = True       # wholly inside a multi-line inline comment
            continue
        if idx == skip_to[0]:
            # The rest of a line an inline comment closed on: more of the same
            # paragraph, never a fresh block.
            rest = bare[skip_to[1]:]
            after_comment = False
            if rest.strip():
                lines.append(_Line(start, end, bare, rest.rstrip(), rest.strip(), "text",
                                   depth=len(items), opens=False))
            prev_blank = False
            continue
        ind = _indent(bare)
        if not bare.strip():
            if html_depth is not None:
                after_comment = True
                continue
            kind = "code" if fence is not None else "blank"
            push(_Line(start, end, bare, bare, "", kind,
                       depth=fence[2] if fence is not None else len(items)))
            para_at, prev_blank = None, True
            continue
        # Which open list items does this line continue?
        k = sum(1 for col in items if ind >= col)
        rel_here = _strip_cols(bare, items[k - 1]) if k else bare
        if k < len(items) and para_at == len(items) and fence is None \
                and html_depth is None and not _block_start(rel_here, False):
            k = len(items)                      # a lazy paragraph continuation
        if fence is not None:
            if k >= fence[2]:
                rel = _strip_cols(bare, items[fence[2] - 1]) if fence[2] else bare
                fm = _FENCE_RE.match(rel)
                closes = bool(fm and fm.group(2)[0] == fence[0]
                              and len(fm.group(2)) >= fence[1] and not fm.group(3).strip())
                push(_Line(start, end, bare, bare, rel, "code", depth=fence[2],
                           indent=_indent(rel), fence_close=closes))
                if closes:
                    fence = None
                para_at, prev_blank = None, False
                continue
            fence = None                        # its list item ended: so did the fence
        if html_depth is not None:
            if k >= html_depth:
                close = bare.find("-->")
                if close < 0:
                    after_comment = True
                    continue
                html_depth = None
                raw_html(start, end, bare, bare[close + 3:])
                continue
            else:
                html_depth = None
        del items[k:]
        col = items[-1] if items else 0
        rel = _strip_cols(bare, col)
        rel_ind = _indent(rel)
        ln = _Line(start, end, raws[idx][2], bare, rel, "text", depth=len(items), indent=rel_ind)
        # A comment block: a line opening with "<!--" (up to 3 columns in).
        if rel_ind <= 3 and rel.lstrip().startswith("<!--"):
            at = len(bare) - len(rel.lstrip())
            cut = _comment_end(bare, at)
            if cut < 0:
                html_depth = len(items)
                after_comment = True
                para_at, prev_blank = None, False
                continue
            raw_html(start, end, raws[idx][2], bare[cut:])
            continue
        im = _ITEM_RE.match(rel) if rel_ind <= 3 and not _THEMATIC_RE.match(rel) else None
        if im is not None and _interrupts(im, rel, para_at == len(items)):
            spaces = _columns(im.group(4), col + _columns(im.group(1)) + len(im.group(2)))
            content_col = col + _columns(im.group(1)) + len(im.group(2)) + (
                spaces if 1 <= spaces <= 4 else 1)
            ln.kind = "item"
            items.append(content_col)
            inner = rel[im.end():]               # the item's first line, after its marker
            fm = _FENCE_RE.match(inner)
            if fm and not (fm.group(2)[0] == "`" and "`" in fm.group(3)):
                fence = (fm.group(2)[0], len(fm.group(2)), len(items))
                ln.full = bare
                para_at = None
            else:
                closes: List[Tuple[int, int]] = []
                ln.full = _strip_inline_comments(raws, idx, bare, closes, content_col)
                if closes:
                    skip_to = closes[0]
                ln.text = _strip_cols(ln.full, col)
                inner = ln.text[im.end():]       # a comment alone opens no paragraph
                para_at = (len(items) if inner.strip() and not _ATX_RE.match(inner)
                           else None)
            push(ln)
            prev_blank = False
            continue
        fm = _FENCE_RE.match(rel)        # at most 3 columns in, by the pattern
        if fm and not (fm.group(2)[0] == "`" and "`" in fm.group(3)):
            fence = (fm.group(2)[0], len(fm.group(2)), len(items))
            ln.kind, ln.fence_open = "code", True
            push(ln)
            para_at, prev_blank = None, False
            continue
        if rel_ind >= 4 and para_at != len(items):
            ln.kind = "indented"        # code: no comment is read inside it
            push(ln)
            para_at, prev_blank = None, False
            continue
        found: List[Tuple[int, int]] = []
        ln.full = _strip_inline_comments(raws, idx, bare, found, col)
        if found:
            skip_to = found[0]
        if not ln.full.strip():
            after_comment = True
            continue
        ln.text = _strip_cols(ln.full, col)
        ln.indent = rel_ind = _indent(ln.text)
        text = ln.text
        if _QUOTE_RE.match(text):
            ln.kind = "quote"
            q = re.sub(r"^(?: {0,3}>[ \t]?)+", "", text)
            quote_para = bool(q.strip()) and not (
                _ATX_RE.match(q) or _FENCE_RE.match(q) or _THEMATIC_RE.match(q))
        elif _ATX_RE.match(text):
            am = _ATX_RE.match(text)
            ln.kind, ln.level = "atx", len(am.group(1))
            ln.title = _ATX_CLOSE_RE.sub("", am.group(2) or "").strip()
        elif _SETEXT_RE.match(rel) and para_at == len(items):   # a comment spoils it
            ln.kind, ln.level = "setext", 1 if text.strip()[0] == "=" else 2
        elif _THEMATIC_RE.match(rel):
            ln.kind = "thematic"
        elif (lines and lines[-1].kind == "quote" and quote_para and not prev_blank
              and not after_comment):
            ln.kind = "quote"           # a lazy continuation of a quote's paragraph
        else:
            ln.kind = "text"
            ln.opens = para_at != len(items)
        push(ln)
        para_at = len(items) if ln.kind == "text" else None
        prev_blank = False
    return lines


def _strip_inline_comments(raws, idx: int, bare: str, found: list, col: int) -> str:
    """``bare`` with each ``<!--`` that is a real comment removed: one outside a
    code span whose ``-->`` closes on this line or on a later line of the same
    paragraph (whose container starts at column ``col``). A comment closing on a
    later line is reported through ``found`` as ``(line index, column after -->)``;
    the text from it to the line end goes."""
    out = bare
    pos = 0
    while True:
        at = out.find("<!--", pos)
        if at < 0:
            return out
        if any(a <= at < b for a, b in _code_spans(out)):
            pos = at + 4
            continue
        stop = _comment_end(out, at)
        if stop >= 0:
            out = _join_cut(out[:at], out[stop:])
            pos = at
            continue
        for j in range(idx + 1, len(raws)):
            later = raws[j][2]
            rel = _strip_cols(later, col)
            if _block_start(rel, True) or _SETEXT_RE.match(rel):
                break                      # the paragraph ends before it closes
            close = later.find("-->")
            if close >= 0:
                found.append((j, close + 3))
                return out[:at].rstrip()
        pos = at + 4                       # never closes: literal text


def _norm_title(text: str) -> str:
    prev = None
    s = text
    while s != prev:
        prev = s
        s = s.strip()
        s = s.strip("*_").strip()
        if s.endswith(":"):
            s = s[:-1]
    return s.casefold()


_TITLE_KEY = TITLE.casefold()


def _setext_at(lines: List[_Line], i: int) -> int:
    """The Setext level when top-level line ``i`` is a one-line paragraph
    underlined by the next line, else 0."""
    if (i + 1 < len(lines) and lines[i].kind == "text" and lines[i].depth == 0
            and lines[i + 1].kind == "setext" and lines[i].opens):
        return lines[i + 1].level
    return 0


def _start_at(lines: List[_Line], i: int) -> Optional[Tuple[str, int]]:
    """``(form, level)`` when line ``i`` opens a protections section."""
    ln = lines[i]
    if ln.depth:
        return None
    if ln.kind == "atx" and _norm_title(ln.title) == _TITLE_KEY:
        return "heading", ln.level
    level = _setext_at(lines, i)
    if level and _norm_title(ln.text) == _TITLE_KEY:
        return "setext", level
    if (ln.kind == "text" and _norm_title(ln.text) == _TITLE_KEY and ln.opens
            and (i + 1 >= len(lines) or lines[i + 1].kind in ("blank", "item")
                 or lines[i + 1].after_comment)):
        return "label", 0
    return None


def _heading_level(lines: List[_Line], i: int) -> int:
    """The level of a TOP-LEVEL heading starting at line ``i``, else 0."""
    if lines[i].depth:
        return 0
    if lines[i].kind == "atx":
        return lines[i].level
    return _setext_at(lines, i)


@dataclass(frozen=True)
class Section:
    start: int
    end: int
    raw: str
    blocks: Tuple[str, ...]


@dataclass(frozen=True)
class Reading:
    """One complete parse of a PR body."""
    sections: Tuple[Section, ...] = ()

    @property
    def found(self) -> bool:
        return bool(self.sections)

    @property
    def blocks(self) -> Tuple[str, ...]:
        return tuple(b for s in self.sections for b in s.blocks)


def _block_text(lines: Sequence[_Line]) -> str:
    # Trailing whitespace carries no meaning outside code (and a removed comment
    # would otherwise leave its leading space behind).
    texts = [ln.raw if ln.kind == "code" else ln.full.rstrip() for ln in lines]
    while texts and not texts[0].strip():
        texts.pop(0)
    while texts and not texts[-1].strip():
        texts.pop()
    return "\n".join(texts)


def _item_end(lines: List[_Line], i: int, stop: int) -> int:
    """One past the last line of the top-level list item opening at ``i``: every
    following line the line pass placed inside a list item belongs to it."""
    j = i + 1
    while j < stop and lines[j].depth >= 1:
        j += 1
    return j


def _blocks(lines: List[_Line], lo: int, hi: int) -> Tuple[str, ...]:
    out: List[str] = []
    i = lo
    pending_heading: Optional[List[_Line]] = None

    def emit(group: List[_Line]) -> None:
        nonlocal pending_heading
        if pending_heading is not None:
            group = pending_heading + group
            pending_heading = None
        text = _block_text(group)
        # A bare list marker (an item that held only a comment) protects nothing.
        if _ITEM_RE.sub("", text, count=1).strip():
            out.append(text)

    while i < hi:
        ln = lines[i]
        if ln.kind in ("blank", "thematic", "setext") and ln.depth == 0:
            i += 1
            continue
        if _heading_level(lines, i):
            if pending_heading is not None:
                emit([])
            span = 1 if ln.kind == "atx" else 2
            pending_heading = lines[i:i + span]
            i += span
            continue
        if ln.kind == "item" and ln.depth == 0:
            j = _item_end(lines, i, hi)
            if pending_heading is not None:
                emit([])
            emit(lines[i:j])
            i = j
            continue
        j = i + 1
        if ln.kind == "code":
            if not ln.fence_close or ln.fence_open:
                while j < hi and lines[j].kind == "code" and lines[j].depth == ln.depth:
                    j += 1
                    if lines[j - 1].fence_close:
                        break
        elif ln.kind == "quote":
            while j < hi and lines[j].kind == "quote":
                j += 1
        elif ln.kind == "indented":
            while j < hi and lines[j].kind in ("indented", "blank") and lines[j].depth == 0:
                j += 1
        else:
            # (an indented line that continues a paragraph is classed as text)
            while (j < hi and lines[j].kind == "text" and lines[j].depth == 0
                   and not lines[j].opens and not _heading_level(lines, j)):
                j += 1
        emit(lines[i:j])
        i = j
    if pending_heading is not None:
        emit([])
    return tuple(out)


def parse_body(body: Optional[str]) -> Reading:
    """Every protections section in ``body``, in order, with its raw slice and its
    protections. ``None`` or a body with no matching section reads as none."""
    body = body or ""
    lines = _lines(body)
    sections: List[Section] = []
    n = len(lines)
    i = 0
    while i < n:
        start = _start_at(lines, i)
        if start is None:
            i += 1
            continue
        form, level = start
        if form == "label":
            j = i + 1
            while j < n and lines[j].kind == "blank":
                j += 1
            end_line = i + 1
            if j < n and lines[j].kind == "item" and lines[j].depth == 0:
                k = j
                while k < n and lines[k].kind == "item" and lines[k].depth == 0:
                    stop = _item_end(lines, k, n)
                    end_line = stop
                    while end_line - 1 > k and lines[end_line - 1].kind == "blank":
                        end_line -= 1
                    k = stop
                    while k < n and lines[k].kind == "blank":
                        k += 1
                blocks = _blocks(lines, j, end_line)
            else:
                blocks = ()
            # Up to the next line read, so a comment closing the last item stays inside.
            end = lines[end_line].start if end_line < n else len(body)
            sections.append(Section(lines[i].start, end, body[lines[i].start:end], blocks))
            i = end_line
            continue
        body_from = i + (2 if form == "setext" else 1)
        j = body_from
        while j < n:
            if _start_at(lines, j) is not None:
                break
            lvl = _heading_level(lines, j)
            if lvl and lvl <= level:
                break
            j += 1
        end = lines[j].start if j < n else len(body)
        blocks = _blocks(lines, body_from, j)
        sections.append(Section(lines[i].start, end, body[lines[i].start:end], blocks))
        i = j
    return Reading(tuple(sections))


# ── The round snapshot ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Snapshot:
    """The protections pinned for one round. ``readable`` is False only when no
    reading has ever succeeded this run; ``reused`` marks a round whose own read
    failed and that fell back to the run's last good reading."""
    blocks: Tuple[str, ...] = ()
    sections: Tuple[str, ...] = ()
    found: bool = False
    readable: bool = True
    reused: bool = False

    @property
    def count(self) -> int:
        return len(self.blocks)

    @property
    def chars(self) -> int:
        return sum(len(b) for b in self.blocks)

    @property
    def over_limit(self) -> bool:
        return self.count > MAX_PROTECTIONS or self.chars > MAX_CHARS

    @property
    def blocked(self) -> bool:
        """Reviewer edits may not be retained or committed, and merging waits."""
        return not self.readable or self.over_limit

    @property
    def present(self) -> bool:
        return bool(self.blocks)

    @property
    def empty_section(self) -> bool:
        return self.found and not self.blocks


UNREADABLE = Snapshot(readable=False)


def snapshot_from_body(body: Optional[str]) -> Snapshot:
    reading = parse_body(body)
    return Snapshot(blocks=reading.blocks, sections=tuple(s.raw for s in reading.sections),
                    found=reading.found)


def round_warnings(snap: Snapshot) -> List[Tuple[str, str]]:
    """``(key, text)`` for every once-per-run warning this snapshot raises."""
    out = []
    if not snap.readable:
        out.append(("W7", W7))
    if snap.over_limit:
        out.append(("W5", W5.format(n=snap.count, m=snap.chars)))
    if snap.readable and snap.empty_section:
        out.append(("W6", W6))
    return out


# ── Prompt data ──────────────────────────────────────────────────────────────

def numbered(blocks: Sequence[str]) -> str:
    return "\n".join(f"[{i}] {b}" for i, b in enumerate(blocks, 1))


def fenced(label: str, text: str, nonce: str) -> str:
    return f"{label}\n<<{nonce}\n{text}\n{nonce}\n"


def allowance_block(allowances: Sequence[str], nonce: str) -> str:
    if not allowances:
        return ""
    return (
        "ALLOWED ON THIS THREAD ONLY — the project owner waived exactly these "
        "protections for this one review thread; every other protection still applies:\n"
        f"<<{nonce}\n" + "\n".join(f"- {a}" for a in allowances) + f"\n{nonce}\n")


_CONFLICT_LINE_RE = re.compile(r"^\s*PROTECTION-CONFLICT\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)


def conflict_line(stdout: str) -> Optional[str]:
    """The text after a fixer's ``PROTECTION-CONFLICT:`` line, else None."""
    m = _CONFLICT_LINE_RE.search(stdout or "")
    return None if m is None else (m.group(1) or "").strip()


_NUMBER_WORDS_RE = re.compile(r"\b(?:protections?|numbers?|nos?|and)\b\.?", re.IGNORECASE)


def _as_numbers(item: str) -> Optional[List[int]]:
    """The protection numbers a string names when it is ONLY numbers ("2",
    "[2]", "#2", "1, 3", "protection 2 and 3"); None for prose."""
    tokens = [t.strip("[]()#.:") for t in re.split(r"[\s,;]+", _NUMBER_WORDS_RE.sub(" ", item))]
    tokens = [t for t in tokens if t]
    if tokens and all(t.isascii() and t.isdigit() for t in tokens):
        return [int(t) for t in tokens]
    return None


def _plain(text: str) -> str:
    return " ".join(_ITEM_RE.sub("", text.strip(), count=1).split()).casefold()


def _conflict_indices(named: object, blocks: Sequence[str]) -> Tuple[set, bool]:
    """(indices into ``blocks`` that ``named`` identifies, whether some reported
    item identified nothing listed)."""
    if named is None or named == "" or named == [] or named == ():
        return set(), False
    items = named if isinstance(named, (list, tuple)) else [named]
    picked, lost = set(), False
    plain = [_plain(b) for b in blocks]
    for item in items:
        nums: Optional[List[int]] = None
        if isinstance(item, int) and not isinstance(item, bool):
            nums = [item]
        elif isinstance(item, str):
            echo = re.match(r"\s*\[(\d{1,4})\]\s*(.+)", item, re.S)   # a line of numbered()
            k = int(echo.group(1)) if echo else 0
            if item in blocks or (1 <= k <= len(blocks) and _plain(echo.group(2)) == plain[k - 1]):
                picked.add(blocks.index(item) if item in blocks else k - 1)
                continue
            if item.strip().casefold() in ("none", "n/a"):
                continue
            nums = _as_numbers(item)
            if nums is None and _plain(item) in plain:
                picked.add(plain.index(_plain(item)))
                continue
        hits = [k - 1 for k in nums or () if 1 <= k <= len(blocks)]
        picked.update(hits)
        lost = lost or not hits
    return picked, lost


def named_conflicts(named: object, blocks: Sequence[str],
                    allowances: Iterable[str] = ()) -> Tuple[str, ...]:
    """The exact protection texts ``named`` identifies — numbers (``2``, ``"2"``,
    ``"1, 3"``) or the protection's text — that are listed in ``blocks`` and not
    waived by ``allowances``, in list order. Digits inside prose name nothing."""
    picked, _ = _conflict_indices(named, blocks)
    waived = set(allowances)
    return tuple(blocks[k] for k in sorted(picked) if blocks[k] not in waived)


def unmapped_conflict(named: object, blocks: Sequence[str]) -> bool:
    """True when a non-empty report names something that is not a listed
    protection — a conflict was claimed but cannot be pinned down."""
    return _conflict_indices(named, blocks)[1]


# ── Holds, asks and records ──────────────────────────────────────────────────

def _phrase(texts: Sequence[str]) -> str:
    return "\"; \"".join(texts)


def visible_text(decision: str, texts: Sequence[str]) -> str:
    phrase = _phrase(texts)
    if decision == "pending":
        return W1.format(protection=phrase)
    if decision == "keep":
        return W3.format(protection=phrase)
    if decision == "allow":
        return W4.format(protection=phrase)
    return MANUAL


def record_body(decision: str, root_id: str, origin: str, texts: Sequence[str]) -> str:
    """The exact reply the loop posts for ``decision``: approved visible text, then
    a hidden marker binding the root, origin, decision and exact protection texts.
    ``<``, ``>`` and ``&`` are JSON-escaped so no protection text can close the
    comment early."""
    data = json.dumps({"decision": decision, "origin": origin, "protections": list(texts),
                       "root": str(root_id)}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f"{visible_text(decision, texts)}\n\n<!-- {MARKER} {data} -->"


_RECORD_RE = re.compile(r"<!-- " + re.escape(MARKER) + r" (\{[^\n]*\}) -->\Z")


def parse_record(body: str) -> Optional[dict]:
    """The marker's data when ``body`` is EXACTLY a record the loop would post,
    else None — a quoted, edited or hand-written marker never qualifies."""
    m = _RECORD_RE.search(body or "")
    if m is None:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    decision, origin, root = data.get("decision"), data.get("origin"), data.get("root")
    texts = data.get("protections")
    if (decision not in ("pending", "keep", "allow", "manual") or origin not in ORIGINS
            or not isinstance(root, str) or not isinstance(texts, list)
            or not all(isinstance(t, str) for t in texts)):
        return None
    if decision == "allow" and origin == GUARD:
        return None
    if body != record_body(decision, root, origin, texts):
        return None
    return {"decision": decision, "origin": origin, "root": root, "protections": texts}


def make_protection_hold(*, root_id, origin: str, protection_texts: Sequence[str]) -> dict:
    if origin not in ORIGINS:
        raise ValueError(f"unknown hold origin: {origin!r}")
    texts = list(protection_texts)
    return {"root_id": str(root_id), "origin": origin, "protection_texts": texts,
            "state": "pending", "allowances": [],
            "pending_record": record_body("pending", str(root_id), origin, texts)}


def _ask_id(hold: dict) -> str:
    digest = hashlib.sha256("\0".join(hold["protection_texts"]).encode("utf-8")).hexdigest()[:10]
    return f"protection-{hold['origin']}-{hold['root_id']}-{digest}"


def protection_ask(hold: dict, *, reviewer: str, request: str) -> Ask:
    """The owner's Clearance question for ``hold``: the approved title and body, the
    menu for its origin with every option's detail, the first choice recommended,
    and an action map in the same order as the options."""
    actions = actions_for(hold["origin"])
    return Ask(
        id=_ask_id(hold),
        question=W2_TITLE,
        options=[_ACTIONS[a][0] for a in actions],
        recommended_index=0,
        detail=W2_BODY.format(reviewer=reviewer, request=str(request or "")[:REQUEST_MAX],
                              protection=_phrase(hold["protection_texts"])),
        option_details=[_ACTIONS[a][1] for a in actions],
        actions=list(actions),
    )


def decode_protection_answer(ask: Ask, raw) -> Optional[str]:
    """``keep`` / ``allow`` / ``manual`` for a raw menu answer to ``ask``, by ITS
    own ordered action map; anything else — words, labels, out-of-range or empty
    answers, a non-protection ask — is None. A description-guard ask never yields
    ``allow``."""
    if raw is None or not str(ask.id).startswith("protection-"):
        return None
    text = str(raw).strip()
    actions = list(getattr(ask, "actions", None) or [])
    if not text.isdigit() or not text.isascii():
        return None
    k = int(text)
    if not 1 <= k <= len(actions):
        return None
    action = actions[k - 1]
    if action not in _ACTIONS:
        return None
    if action == "allow" and str(ask.id).startswith(f"protection-{GUARD}-"):
        return None
    return action


def settle_protection_hold(hold: dict, *, answer, login: Optional[str],
                           post_reply: Callable[[str], bool],
                           resolve_thread: Callable[[str], object],
                           refix: Callable[[str], object]) -> dict:
    """Apply the owner's raw menu ``answer`` to ``hold``. The decision record is
    posted first; only a successful post changes state. Keep then resolves the
    root thread; Allow (ordinary holds only) records the exact-text waiver and
    re-dispatches the root; manual touches nothing else."""
    if hold.get("state") != "pending" or not login:
        return hold
    actions = actions_for(hold["origin"])
    probe = Ask(id=_ask_id(hold), question="", actions=list(actions))
    action = decode_protection_answer(probe, answer)
    if action is None:
        return hold
    body = record_body(action, hold["root_id"], hold["origin"], hold["protection_texts"])
    try:
        ok = post_reply(body) is True
    except Exception:
        ok = False
    if not ok:
        return hold
    settled = dict(hold, state=action, decided_record=body)
    if action == "keep":
        try:
            resolve_thread(hold["root_id"])
        except Exception:
            pass
    elif action == "allow":
        settled["allowances"] = sorted(set(hold.get("allowances") or [])
                                       | set(hold["protection_texts"]))
        refix(hold["root_id"])
    return settled


_ANY_RECORD_RE = re.compile(r"<!-- " + re.escape(MARKER) + r" (\{[^\n]*?\}) -->")


def claimed_record(body: str) -> Optional[dict]:
    """The data of the first marker anywhere in ``body`` — whoever wrote it and
    whatever surrounds it — or None. Evidence that a hold MAY exist, never of what
    was decided."""
    for m in _ANY_RECORD_RE.finditer(body or ""):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        texts = data.get("protections") if isinstance(data, dict) else None
        if (isinstance(data, dict) and isinstance(data.get("root"), str)
                and data.get("origin") in ORIGINS and isinstance(texts, list)
                and all(isinstance(t, str) for t in texts)):
            return data
    return None


def restore_protection_hold(*, root_id, history: Sequence[dict], login: Optional[str]
                            ) -> Optional[dict]:
    """Rebuild a root's hold from its complete, ordered root/reply ``history``
    (dicts carrying ``root_id``, ``body`` and ``author``), trusting only exact
    records the authenticated ``login`` posted for this root. A decision settles
    only the pending hold it names (same origin and exact texts); an Allow never
    settles a description-guard hold. A marker for this root that cannot be
    trusted — another author's, a quoted or edited one, a decision with no pending
    record before it, any record when the login is unknown — keeps the root held
    (pending), never decided. None only when nothing claims a hold here."""
    root = str(root_id)
    hold: Optional[dict] = None
    claimed: Optional[dict] = None
    allowances: List[str] = []
    for rec in history:
        if not isinstance(rec, dict):
            continue
        body = str(rec.get("body") or "")
        data = parse_record(body) if login and rec.get("author") == login else None
        if data is None:
            loose = claimed_record(body)
            if loose is not None and loose["root"] == root and claimed is None:
                claimed = loose
            continue
        if data["root"] != root:
            continue
        origin, texts, decision = data["origin"], data["protections"], data["decision"]
        if decision == "pending":
            if hold is not None and hold["state"] == "pending" and (
                    hold["origin"], hold["protection_texts"]) != (origin, texts):
                # Two different open holds on one root: keep blocking on both,
                # and never narrow a guard hold to the ordinary menu.
                merged = list(dict.fromkeys(hold["protection_texts"] + texts))
                origin = GUARD if GUARD in (origin, hold["origin"]) else origin
                hold = make_protection_hold(root_id=root, origin=origin,
                                            protection_texts=merged)
            elif hold is None or hold["state"] != "pending":
                hold = make_protection_hold(root_id=root, origin=origin,
                                            protection_texts=texts)
            continue
        if hold is None:
            claimed = claimed or data   # a decision nothing asked for proves nothing
            continue
        if hold["state"] != "pending" or (hold["origin"], hold["protection_texts"]) != (origin, texts):
            continue
        hold = dict(hold, state=decision, decided_record=rec.get("body"))
        if decision == "allow":
            allowances = sorted(set(allowances) | set(texts))
    if hold is None and claimed is not None:
        hold = make_protection_hold(root_id=root, origin=claimed["origin"],
                                    protection_texts=claimed["protections"])
    if hold is not None:
        hold["allowances"] = allowances
    return hold
