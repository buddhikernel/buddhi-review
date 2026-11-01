"""The protections a pull request lists in its description, and the owner holds
that keep a review fix from breaking one.

A PR author lists requirements under a ``Protections this PR must keep`` heading
(or a plain/bold label line followed by a list). This module:

* **reads** that section exactly (:func:`parse_body`) from the tree of one pinned
  parser, cmark-gfm 0.29.0.gfm.13 (bundled in multimark 0.4.0) with GitHub's
  extensions: ATX levels 1–6, Setext and label forms, at the top level of the
  document only — never inside a fence, indented code, a block quote, a list or raw
  HTML. Every top-level list item, paragraph, table, code block and subordinate
  heading-with-paragraph is one protection, numbered in encounter order across
  repeated sections, carried whole (no truncation). A body it cannot read raises
  :class:`ProtectionReadError`, never an empty reading;
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
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from buddhi_review.notifier import Ask

TITLE = "Protections this PR must keep"
MAX_PROTECTIONS = 40
MAX_CHARS = 8000
REQUEST_MAX = 300

ORIGINS = ("classifier", "fixer", "verifier", "description_guard")
GUARD = "description_guard"
# The structured record marker, hidden in each decision the loop posts. Written
# in two parts so that a text scan of this repository for another name with the
# same leading letters never matches this line.
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


# ── The section reader ───────────────────────────────────────────────────────
#
# The body's structure comes from one pinned parser: cmark-gfm 0.29.0.gfm.13 as
# bundled in multimark 0.4.0, with GitHub's five extensions on. Its tree is copied
# into plain dicts while the parser is open, and every section, protection and
# boundary is then cut from the body's own text by the tree's line numbers — never
# from rendered output, and never from a second scanner. A body the parser cannot
# read, or whose tree does not map onto the body's lines, raises
# :class:`ProtectionReadError`; it never reads as an empty one.

_EXTENSIONS = ("table", "strikethrough", "autolink", "tagfilter", "tasklist")
_MULTIMARK = ("0.4.0", "0.29.0.gfm.13")    # the package, and the cmark-gfm it bundles
# Only CR, LF and CRLF end a line, as for the parser (never str.splitlines()).
_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$")
_LINE_END_RE = re.compile(r"\r\n|\r|\n")
_MARKER_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s*")
_ITEM_RE = re.compile(r"^( {0,3})([-*+]|(\d{1,9})[.)])([ \t]+|$)")


class ProtectionReadError(Exception):
    """The body's protections could not be read. ``reason`` (never shown to anyone)
    is ``dependency-unavailable``, ``parse-failed`` or ``source-map-invalid``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_multimark = None


def _load_multimark():
    """The pinned parser package, imported on first use. Any other build, or none,
    is ``dependency-unavailable``."""
    global _multimark
    if _multimark is None:
        try:
            import multimark
            pinned = ((multimark.__version__, multimark.cmark_version()) == _MULTIMARK
                      and set(_EXTENSIONS) <= multimark.VALID_EXTENSIONS)
        except Exception as exc:
            raise ProtectionReadError("dependency-unavailable") from exc
        if not pinned:
            raise ProtectionReadError("dependency-unavailable")
        _multimark = multimark
    return _multimark


def _gfm_tree(body: str, *, extensions: Sequence[str]) -> dict:
    """The parser's tree for ``body`` as plain dicts: ``kind``, 1-based ``start`` and
    ``end`` lines, ``literal`` where the parser has one, ``level`` for a heading, and
    ``children`` when there are any. Copied while the parser is open, so no node
    outlives it."""
    multimark = _load_multimark()
    with multimark.Parser(extensions=list(extensions)) as parser:
        parser.feed(body)
        return _owned(parser.finish())


def _owned(root) -> dict:
    tree: dict = {}
    stack = [(root, tree)]
    while stack:
        node, out = stack.pop()
        out.update(kind=node.type_string, start=node.start_line, end=node.end_line)
        literal = node.literal
        if literal is not None:
            out["literal"] = literal
        if out["kind"] == "heading":
            out["level"] = node.heading_level
        children = []
        child = node.first_child
        while child is not None:
            children.append((child, {}))
            child = child.next
        if children:
            out["children"] = [copy for _, copy in children]
            stack.extend(children)
    return tree


def _check_shape(tree: object) -> None:
    """A document of well-typed nodes, or ``parse-failed``."""
    if not isinstance(tree, dict) or tree.get("kind") != "document":
        raise ProtectionReadError("parse-failed")
    stack = [tree]
    while stack:
        node = stack.pop()
        if not (isinstance(node, dict) and isinstance(node.get("kind"), str)
                and type(node.get("start")) is int and type(node.get("end")) is int
                and isinstance(node.get("literal", ""), str)
                and (node["kind"] != "text" or "literal" in node)
                and (node["kind"] != "heading" or type(node.get("level")) is int)
                and isinstance(node.get("children", []), list)):
            raise ProtectionReadError("parse-failed")
        stack.extend(node.get("children", []))


def _norm_title(text: str) -> str:
    s = text.strip()
    return (s[:-1] if s.endswith(":") else s).strip().casefold()


_TITLE_KEY = TITLE.casefold()


def _is_comment(node: dict) -> bool:
    # An HTML block's literal keeps the up to three spaces the block is indented by.
    return (node["kind"] in ("html_inline", "html_block")
            and node.get("literal", "").lstrip(" ").startswith("<!--"))


# What may stand before a title's first text run on its line (indentation, a BOM, a
# heading's opening #s, emphasis, a removed comment), between two of its runs
# (spacing, a line break or a backslash one, emphasis, a removed comment), and after
# its last run: the same, closing #s too, to the end of its text; then a Setext
# heading's underline and the one line after it that the parser's end can take in.
_TITLE_LEAD = "(?:^|(?<=[\r\n]))[ \t﻿#*_\x00]*"
_TITLE_SPACE = " \t\r\n*_\x00"
_TITLE_BREAK = "\\\\(?=[\r\n])"
_TITLE_GAP = "(?:[" + _TITLE_SPACE + "]|" + _TITLE_BREAK + ")*"
_EOL = "(?:" + _LINE_END_RE.pattern + ")"
_TITLE_END = ("(?:[#" + _TITLE_SPACE + "]|" + _TITLE_BREAK + ")*"
              "(?:" + _EOL + " {0,3}(?:=+|-+)[ \t]*(?:" + _EOL + "[^\r\n]*)?)?"
              + _EOL + "?\\Z")


def _title(node: dict) -> Optional[Tuple[str, List[str], List[str]]]:
    """A heading's or paragraph's text with its emphasis unwrapped and its comments
    taken out, the text runs it is made of, and its comments; None when it holds
    anything else (a link, code, an image, struck-through or raw HTML text)."""
    parts: List[Optional[str]] = []    # None where a comment was
    runs: List[str] = []
    comments: List[str] = []
    stack = list(reversed(node.get("children", [])))
    while stack:
        n = stack.pop()
        if n["kind"] in ("softbreak", "linebreak"):
            parts.append("\n")
        elif n["kind"] == "text":
            parts.append(n["literal"])
            runs.append(n["literal"])
        elif n["kind"] in ("emph", "strong"):
            stack.extend(reversed(n.get("children", [])))
        elif _is_comment(n):
            parts.append(None)
            comments.append(n["literal"])
        else:
            return None
    # A removed comment leaves what _gap leaves: a space only where the text on
    # either side of it would otherwise glue two words together.
    following, after = [], ""
    for part in reversed(parts):
        following.append(after)
        after = part or after
    text = ""
    for part, after in zip(parts, reversed(following)):
        text += _gap(text, after) if part is None else part
    return text, runs, comments


def _gap(before: str, after: str) -> str:
    """What stands in for a removed one-line comment: a space only where the text
    around it would otherwise glue two words together."""
    return " " if before and after and not before[-1].isspace() and not after[0].isspace() else ""


def _comment_pattern(literal: str) -> "re.Pattern[str]":
    """The comment that ``literal`` opens, as written in the body: an HTML block's
    comment ends at its first ``-->``; a line after the first may sit behind the
    container's indentation or quote markers, which the parser drops, and the parser
    reads a NUL as U+FFFD."""
    end = literal.find("-->", 2)
    comment = (literal if end < 0 else literal[:end + 3]).rstrip("\r\n")
    return re.compile(r"(?:\r\n|\r|\n)[ \t>]*".join(
        re.escape(part.lstrip(" \t")).replace("�", "[\x00�]")
        for part in _LINE_END_RE.split(comment)))


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


def parse_body(body: Optional[str]) -> Reading:
    """Every protections section in ``body``, in order, with its raw slice and its
    protections. ``None`` or an empty body reads as none; a body that cannot be read
    raises :class:`ProtectionReadError`."""
    body = body or ""
    if not body:
        return Reading()
    try:
        tree = _gfm_tree(body, extensions=_EXTENSIONS)
    except ProtectionReadError:
        raise
    except Exception as exc:
        raise ProtectionReadError("parse-failed") from exc
    _check_shape(tree)
    return _read(body, tree)


def _read(body: str, tree: dict) -> Reading:
    """The sections ``tree`` places in ``body``. A heading or label paragraph at the
    top level of the document opens one; a heading's runs to the next top-level
    heading of the same or a higher rank, or the next section; a label's takes the
    list that follows it, past comment-only blocks."""
    lines = _LINE_RE.findall(body)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    nodes = tree.get("children", [])

    def span(node: dict) -> Tuple[int, int]:
        first, last = node["start"], node["end"]
        if node["kind"] == "html_block":
            # The parser can end an HTML block before its closing line; its literal
            # holds every line the block took.
            last = first - 1 + len(_LINE_RE.findall(node.get("literal", "")))
        if not 1 <= first <= last <= len(lines):
            raise ProtectionReadError("source-map-invalid")
        return offsets[first - 1], offsets[last]

    if any(n["start"] > len(lines) for n in nodes):
        raise ProtectionReadError("source-map-invalid")
    # The parser gives the paragraph it splits off the head of a table no line at
    # all (and the table that paragraph's first line), so a reading that needs that
    # paragraph fails; every reading that reaches the table needs it first. (Only
    # top-level nodes and list items are placed by line; comments by their text.)
    starts = [offsets[n["start"] - 1] if n["start"] >= 1 else None for n in nodes]
    placed = [s for s in starts if s is not None]
    if placed != sorted(placed):
        raise ProtectionReadError("source-map-invalid")

    def start(i: int) -> int:
        """Where top-level node ``i`` starts, for a reading that needs it."""
        if i == len(nodes):
            return len(body)
        if starts[i] is None:
            raise ProtectionReadError("source-map-invalid")
        return starts[i]

    def declares(i: int) -> bool:
        node = nodes[i]
        title = _title(node) if node["kind"] in ("heading", "paragraph") else None
        if title is None or _norm_title(title[0]) != _TITLE_KEY:
            return False
        # Every text run must be the body's own characters, in order, in the title's
        # own lines and outside its comments, with nothing between them but spacing,
        # emphasis or a comment, and the last must end the node's text: an entity, an
        # escape or a reference definition that renders as the title does not make one.
        hi = span(node)[1]
        if i + 1 < len(nodes) and starts[i + 1] is not None:
            # A Setext heading's end can take in the next block's first line.
            hi = min(hi, starts[i + 1])
        source = body[start(i):hi]
        for comment in title[2]:
            source = _comment_pattern(comment).sub(lambda m: "\x00" * len(m.group()), source)
        pattern = _TITLE_LEAD + _TITLE_GAP.join(re.escape(run) for run in title[1]) + _TITLE_END
        return re.search(pattern, source) is not None

    opens = [declares(i) for i in range(len(nodes))]

    def text(node: dict, limit: int) -> str:
        """``node``'s protection: its lines, up to ``limit`` where the next sibling
        starts (a Setext heading's end can reach into it), with its comments taken
        out and any line that held nothing but a comment dropped."""
        lo, hi = span(node)
        source = body[lo:min(hi, limit)]
        comments = []
        stack = [node]
        while stack:
            n = stack.pop()
            if _is_comment(n):
                comments.append(n)
            else:
                stack.extend(reversed(n.get("children", [])))
        # The parser's line for an inline node lags behind after a code span, raw
        # HTML, a link title or a backslash break that runs over a line, so a comment
        # is found by its text alone: in order, and only where every copy of that text
        # in the block is one of its comments (never code or escaped text).
        patterns = [_comment_pattern(comment["literal"]) for comment in comments]
        wanted = Counter(p.pattern for p in patterns)
        if any(len(re.findall(p, source)) != n for p, n in wanted.items()):
            raise ProtectionReadError("source-map-invalid")
        cuts: List[Tuple[int, int]] = []
        for pattern in patterns:
            m = pattern.search(source, cuts[-1][1] if cuts else 0)
            if m is None:
                raise ProtectionReadError("source-map-invalid")
            cuts.append(m.span())
        out, last = "", 0
        for start, end in cuts:
            out += source[last:start]
            breaks = "".join(_LINE_END_RE.findall(source[start:end]))
            out += breaks or _gap(out, source[end:])
            last = end
        out += source[last:]
        # The cuts keep every line break, so line i of ``out`` is line i of ``source``.
        bounds = [m.span() for m in _LINE_RE.finditer(source)]
        chunks = [chunk.rstrip("\r\n") for chunk in _LINE_RE.findall(out)]
        if node["kind"] != "code_block":
            # Trailing whitespace carries no meaning outside code.
            chunks = [chunk.rstrip() for chunk in chunks]
        chunks = [chunk for (a, b), chunk in zip(bounds, chunks) if chunk.strip()
                  or not any(start < b and end > a for start, end in cuts)]
        while chunks and not chunks[0].strip():
            chunks.pop(0)
        while chunks and not chunks[-1].strip():
            chunks.pop()
        return "\n".join(chunks)

    def blocks(first: int, stop: int, end: int) -> Tuple[str, ...]:
        # Every top-level list item is one protection, and every other block; a
        # subordinate heading joins the block after it. A block ends where the next
        # one starts, and the last where the section ends.
        def limit(i: int) -> int:
            return end if i + 1 == stop else start(i + 1)

        out: List[str] = []
        pending: Optional[str] = None
        for i in range(first, stop):
            node = nodes[i]
            if node["kind"] == "thematic_break":
                continue
            if node["kind"] == "heading":
                if pending:
                    out.append(pending)
                pending = text(node, limit(i))
                continue
            if node["kind"] == "list":
                if pending:
                    out.append(pending)
                    pending = None
                items = node.get("children", [])
                for k, item in enumerate(items):
                    value = text(item, span(items[k + 1])[0] if k + 1 < len(items) else limit(i))
                    # A bare list marker (an item that held only a comment) protects nothing.
                    if _MARKER_RE.sub("", value).strip():
                        out.append(value)
                continue
            value = text(node, limit(i))
            if value:
                if pending:
                    value = pending + "\n" + value
                    pending = None
                out.append(value)
        if pending:
            out.append(pending)
        return tuple(out)

    sections: List[Section] = []
    for i, node in enumerate(nodes):
        if not opens[i]:
            continue
        lo = start(i)
        if node["kind"] == "heading":
            stop = next((j for j in range(i + 1, len(nodes)) if opens[j] or (
                nodes[j]["kind"] == "heading" and nodes[j]["level"] <= node["level"])), len(nodes))
            hi = start(stop)
            found = blocks(i + 1, stop, hi)
        else:
            j = i + 1
            while j < len(nodes) and nodes[j]["kind"] == "html_block" and _is_comment(nodes[j]):
                j += 1
            if j < len(nodes) and nodes[j]["kind"] == "list":
                span(nodes[j])
                # The list's end can take in the blank line after it; the label's
                # slice ends with the list's last written line, or with the last line
                # of a code or raw HTML block in it, whose blank lines are its own (a
                # fenced block's text starts after its fence; an indented one may so
                # keep one blank line more).
                floor = nodes[j]["start"]
                stack = list(nodes[j].get("children", []))
                while stack:
                    n = stack.pop()
                    if n["kind"] in ("code_block", "html_block"):
                        if n["start"] < 1:
                            raise ProtectionReadError("source-map-invalid")
                        floor = max(floor, n["start"] - 1 + len(_LINE_RE.findall(n.get("literal", "")))
                                    + (1 if n["kind"] == "code_block" else 0))
                    stack.extend(n.get("children", []))
                last = nodes[j]["end"]
                while last > floor and not lines[last - 1].strip(" \t\r\n"):
                    last -= 1
                hi = offsets[last]
                found = blocks(j, j + 1, hi)
            else:
                hi = span(node)[1]
                found = ()
        sections.append(Section(lo, hi, body[lo:hi], found))
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


def record_body(decision: str, root_id: str, origin: str, texts: Sequence[str],
                source: Optional[str] = None) -> str:
    """The exact reply the loop posts for ``decision``: approved visible text, then
    a hidden marker binding the root, origin, decision and exact protection texts —
    and, for a held request, the id of the comment that made it (``source``).
    ``<``, ``>`` and ``&`` are JSON-escaped so no protection text can close the
    comment early."""
    fields = {"decision": decision, "origin": origin, "protections": list(texts),
              "root": str(root_id)}
    if source is not None:
        fields["source"] = str(source)
    data = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
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
    texts, source = data.get("protections"), data.get("source")
    if (decision not in ("pending", "keep", "allow", "manual") or origin not in ORIGINS
            or not isinstance(root, str) or not isinstance(texts, list)
            or not all(isinstance(t, str) for t in texts)):
        return None
    if decision == "allow" and origin == GUARD:
        return None
    # Exactness also rules out a source that is not a string: it would re-render.
    if body != record_body(decision, root, origin, texts, source):
        return None
    return {"decision": decision, "origin": origin, "root": root, "protections": texts,
            "source": source}


def make_protection_hold(*, root_id, origin: str, protection_texts: Sequence[str]) -> dict:
    """A pending hold; ``sources`` lists the comments whose requests it holds, and
    ``kept`` the requests an earlier Keep on this root refused (see :func:`kept_after`)."""
    if origin not in ORIGINS:
        raise ValueError(f"unknown hold origin: {origin!r}")
    texts = list(protection_texts)
    return {"root_id": str(root_id), "origin": origin, "protection_texts": texts,
            "state": "pending", "allowances": [], "sources": [], "kept": [],
            "pending_record": record_body("pending", str(root_id), origin, texts)}


def kept_after(hold: dict) -> List[str]:
    """Every request a Keep on ``hold``'s root has refused so far: the ones an earlier
    Keep refused (``kept``) and, when ``hold`` itself is kept, the requests it holds
    (the root, for a hold naming none). A later hold on the same root inherits this
    list whatever its own state: a Keep refusal outlives a reopened thread, so a later
    request's hold — or its Allow — never releases a request the owner refused."""
    kept = [str(k) for k in hold.get("kept") or []]
    if hold.get("state") == "keep":
        kept += [str(s) for s in hold.get("sources") or [hold["root_id"]]]
    return list(dict.fromkeys(kept))


def _ask_id(hold: dict) -> str:
    # The request that opened the hold (its first source) is part of the id, so a
    # hold opened after an earlier one on the same root was settled — same origin,
    # same texts — is a NEW question, never de-duplicated as the settled one. A
    # request joining a still-open hold keeps that first source, so the same hold
    # keeps one id; a restart rebuilds the same sources, hence the same id.
    # The hold's kind is part of the id too: an inline comment's id and a review
    # body's id are separate GitHub id spaces that may coincide (the driver keys
    # their holds apart for the same reason), so each kind gets its own question.
    # The driver sets ``inline`` before asking; an inline hold's id is unchanged.
    sources = hold.get("sources") or []
    opener = str(sources[0]) if sources else ""
    kind = [] if hold.get("inline", True) else ["review"]
    digest = hashlib.sha256("\0".join(kind + [opener] + list(hold["protection_texts"]))
                            .encode("utf-8")).hexdigest()[:10]
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
    settled["kept"] = kept_after(settled)
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
    (pending), never decided, with no trusted source. ``sources`` collects the
    requests the open pending records name. None only when nothing claims a hold
    here."""
    root = str(root_id)
    hold: Optional[dict] = None
    claimed: Optional[dict] = None
    allowances: List[str] = []
    kept: List[str] = []    # every request a Keep refused, across successive holds
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
            source = [data["source"]] if data["source"] is not None else []
            if hold is not None and hold["state"] == "pending":
                if (hold["origin"], hold["protection_texts"]) != (origin, texts):
                    # Two different open holds on one root: keep blocking on both,
                    # and never narrow a guard hold to the ordinary menu.
                    merged = list(dict.fromkeys(hold["protection_texts"] + texts))
                    sources = hold["sources"]
                    hold = make_protection_hold(
                        root_id=root, origin=GUARD if GUARD in (origin, hold["origin"])
                        else origin, protection_texts=merged)
                    hold["sources"] = sources
                hold["sources"] = list(dict.fromkeys(hold["sources"] + source))
            else:
                if hold is not None:
                    kept = kept_after(dict(hold, kept=kept))
                # ``allowances`` deliberately carries over: the live driver copies an
                # earlier Allow onto a fresh hold on the same root too, so a restart
                # must neither revoke nor widen it. A waived text is never re-held
                # (conflicts on it are dropped), and a pending hold blocks regardless.
                hold = make_protection_hold(root_id=root, origin=origin, protection_texts=texts)
                hold["sources"] = source
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
        hold["kept"] = kept_after(dict(hold, kept=kept))
    return hold
