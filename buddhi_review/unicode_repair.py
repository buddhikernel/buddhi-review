"""Deterministic "dangerous Unicode" repair for source files.

LLM fixers routinely leak Unicode that LOOKS like ASCII but breaks the parser:
smart quotes used as string delimiters, a non-breaking space where indentation
should be, an invisible zero-width char inside an identifier, a leading BOM, a
line/paragraph separator. These are MECHANICAL to repair -- no model needed. This
module maps ONLY the codepoints whose repair is unambiguous AS A CODE CHARACTER;
everything else (em-dash, ellipsis, homoglyph identifiers, bidi controls, combining
marks, fullwidth ASCII) is left untouched, because "fixing" those would require
guessing intent and could corrupt legitimate prose / i18n / security-relevant text.

Pure: no git, no subprocess, no model. The CALLER is responsible for applying this
ONLY to a file that already FAILS a syntax check, re-verifying that the substitution
actually resolved the failure, and rolling back otherwise. This module just does the
character substitution and reports what it changed -- the SAFETY comes from the
caller's gating, so the mapping here stays a blunt, predictable table.

The taxonomy lists codepoints as explicit HEX integers built into lookup strings via
chr() -- on purpose: most of these chars are invisible or ASCII-lookalikes, so a
literal glyph in the source would be unreadable and corruption-prone, and an escape
in a string literal is hard to eyeball. The integer table is pure-ASCII + verifiable.
"""
from __future__ import annotations

import os
import tempfile
from typing import List, NamedTuple, Optional, Tuple


class Replacement(NamedTuple):
    codepoint: str        # e.g. "U+201C"
    char: str             # the offending character
    repl: Optional[str]   # ASCII replacement; "" = delete; None = FLAG-ONLY (unsafe to auto-fix)
    cls: str              # smart_double|smart_single|nbsp|zero_width|bom|line_sep|bidi


def _chars(*cps: int) -> str:
    return "".join(chr(c) for c in cps)


# -- SAFE-to-auto-fix taxonomy (built from explicit codepoints) -----------------
_SMART_DOUBLE = _chars(0x201C, 0x201D, 0x201E, 0x201F)   # left/right/low/high double
_SMART_SINGLE = _chars(0x2018, 0x2019, 0x201A, 0x201B)   # left/right/low/high single
_SPACES = _chars(0x00A0, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006,
                 0x2007, 0x2008, 0x2009, 0x200A, 0x202F, 0x205F, 0x3000)  # NBSP + exotic
_ZERO_WIDTH = _chars(0x200B, 0x200C, 0x200D, 0x2060)     # ZWSP ZWNJ ZWJ word-joiner
_LINE_SEP = _chars(0x2028, 0x2029)                       # line / paragraph separator
_BOM = chr(0xFEFF)                                       # BOM / zero-width no-break space

SAFE_UNICODE = {}  # char -> (replacement, class)
for _c in _SMART_DOUBLE:
    SAFE_UNICODE[_c] = ('"', "smart_double")
for _c in _SMART_SINGLE:
    SAFE_UNICODE[_c] = ("'", "smart_single")
for _c in _SPACES:
    SAFE_UNICODE[_c] = (" ", "nbsp")
for _c in _ZERO_WIDTH:
    SAFE_UNICODE[_c] = ("", "zero_width")
for _c in _LINE_SEP:
    SAFE_UNICODE[_c] = ("\n", "line_sep")
# U+FEFF is handled positionally in normalize_text: a leading one is a BOM (delete),
# a non-leading one is a zero-width no-break joiner (delete, class zero_width).

# -- UNSAFE classes -- surfaced by scan_text for escalation, NEVER auto-rewritten.
# Bidi / Trojan-source controls are a SECURITY signal, not a typo: stripping them can
# hide an attack or change intended RTL rendering. (Homoglyphs / fullwidth / combining
# marks are also unsafe but have no deterministic 1:1 map, so we do not enumerate
# them -- they simply pass through untouched.)
_BIDI = _chars(0x202A, 0x202B, 0x202C, 0x202D, 0x202E,   # LRE RLE PDF LRO RLO
               0x2066, 0x2067, 0x2068, 0x2069,           # LRI RLI FSI PDI
               0x200E, 0x200F)                           # LRM RLM


def _cp(ch: str) -> str:
    return "U+%04X" % ord(ch)


def normalize_text(text: str) -> Tuple[str, List[Replacement]]:
    """Return ``(normalized_text, [Replacement, ...])``. Remaps ONLY the SAFE_UNICODE
    codepoints; a LEADING U+FEFF is dropped as a BOM, a non-leading U+FEFF as a
    zero-width char. Every other character (em-dash, homoglyph, bidi, ...) is left
    exactly as-is. Idempotent."""
    if not text:
        return text, []
    out: List[str] = []
    reps: List[Replacement] = []
    for i, ch in enumerate(text):
        if ch == _BOM:
            reps.append(Replacement("U+FEFF", ch, "", "bom" if i == 0 else "zero_width"))
            continue  # delete
        sub = SAFE_UNICODE.get(ch)
        if sub is None:
            out.append(ch)
            continue
        repl, cls = sub
        reps.append(Replacement(_cp(ch), ch, repl, cls))
        out.append(repl)
    return "".join(out), reps


def scan_text(text: str) -> List[Replacement]:
    """Detect-only: every SAFE_UNICODE occurrence (with its replacement) PLUS the
    UNSAFE bidi-control class (``repl=None``) so a caller can flag/escalate rather
    than silently rewrite. Never mutates."""
    reps: List[Replacement] = []
    for i, ch in enumerate(text or ""):
        if ch == _BOM:
            reps.append(Replacement("U+FEFF", ch, "", "bom" if i == 0 else "zero_width"))
        elif ch in SAFE_UNICODE:
            repl, cls = SAFE_UNICODE[ch]
            reps.append(Replacement(_cp(ch), ch, repl, cls))
        elif ch in _BIDI:
            reps.append(Replacement(_cp(ch), ch, None, "bidi"))
    return reps


def has_dangerous_unicode(text: str) -> bool:
    """True iff `text` contains at least one SAFE-fixable dangerous codepoint."""
    return bool(normalize_text(text)[1])


def _normalize_selected_lines(text: str, line_numbers) -> Tuple[str, List[Replacement]]:
    """Normalize ONLY the given 1-based line numbers (the fixer's changed lines);
    every other line is left byte-identical. Splits on ``\\n`` ONLY (git's line model,
    so the numbers align with a diff and CRLF is preserved -- a trailing ``\\r`` rides
    along untouched). Other lines keep their legit typographic glyphs."""
    targets = set(line_numbers or ())
    if not targets:
        return text, []
    parts = text.split("\n")
    reps: List[Replacement] = []
    for i in range(len(parts)):
        if (i + 1) in targets:
            new_line, line_reps = normalize_text(parts[i])
            if line_reps:
                parts[i] = new_line
                reps.extend(line_reps)
    return "\n".join(parts), reps


def normalize_code_file(path: str, *, only_lines=None) -> List[Replacement]:
    """Read `path` (utf-8 / surrogateescape, EOLs preserved), normalize the SAFE
    classes, and write it back ATOMICALLY only if something changed. With
    ``only_lines`` (a set of 1-based line numbers) normalize ONLY those lines -- the
    fixer's changed lines -- leaving pre-existing typographic glyphs elsewhere intact
    (the surgical scope that keeps the cleanup from corrupting a clean sibling block).
    A symlink is skipped (never replaced by a regular file). Returns the Replacements
    applied (``[]`` = no change, no write). Best-effort: ``[]`` on any IO error."""
    try:
        if os.path.islink(path):
            return []
        with open(path, "r", encoding="utf-8", errors="surrogateescape",
                  newline="") as f:   # newline="" → CRLF/LF byte-preserved
            text = f.read()
    except OSError:
        return []
    if only_lines is None:
        new, reps = normalize_text(text)
    else:
        new, reps = _normalize_selected_lines(text, only_lines)
    if not reps or new == text:
        return []
    if not overwrite_atomic(path, new):
        return []
    return reps


def overwrite_atomic(path: str, text: str) -> bool:
    """Write `text` to `path` ATOMICALLY (a temp file in the same dir + os.replace),
    preserving the file's mode, with utf-8 / surrogateescape and EOLs verbatim
    (``newline=""``). A symlink is skipped (never replaced by a regular file). Returns
    True on a successful write, False on any IO error or a symlink. Best-effort and
    never raises."""
    try:
        if os.path.islink(path):
            return False
        d = os.path.dirname(path) or "."
        original_mode = os.stat(path).st_mode
        fd, tmp = tempfile.mkstemp(prefix=".urepair-", suffix=".tmp", dir=d)
        try:
            try:
                with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape",
                               newline="") as f:
                    fd = -1  # fdopen took ownership; don't double-close
                    f.write(text)
            finally:
                if fd != -1:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            os.chmod(tmp, original_mode)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
    except OSError:
        return False
    return True


# -- position-level editing (for a verifier-driven minimal cleanup) -------------
# normalize_code_file rewrites EVERY SAFE codepoint on the selected lines. When the
# caller needs to keep ONLY the substitutions that are load-bearing for a fix (so a
# legitimate smart quote that merely happens to sit on a changed line is preserved),
# it enumerates the candidate edits, then applies the minimal necessary subset.

def selected_line_edits(text: str, line_numbers) -> List[Tuple[int, str]]:
    """Every SAFE dangerous codepoint on the given 1-based lines, as
    ``[(absolute_char_index, replacement), ...]`` (``""`` = delete). Lines are split
    on ``\\n`` ONLY (git's line model; a trailing ``\\r`` is an ordinary char and is
    never an edit, so CRLF is preserved). The caller applies any subset via
    :func:`apply_edits`. UNSAFE classes (bidi, em-dash, homoglyph, ...) are never
    enumerated, exactly as :func:`normalize_text`."""
    targets = set(line_numbers or ())
    if not targets or not text:
        return []
    edits: List[Tuple[int, str]] = []
    idx = 0
    for lineno, line in enumerate(text.split("\n"), start=1):
        if lineno in targets:
            for ch in line:
                if ch == _BOM:
                    edits.append((idx, ""))
                else:
                    sub = SAFE_UNICODE.get(ch)
                    if sub is not None:
                        edits.append((idx, sub[0]))
                idx += 1
        else:
            idx += len(line)
        idx += 1   # the "\n" the split consumed (the trailing one past EOF is unused)
    return edits


def apply_edits(text: str, edits) -> str:
    """Apply ``[(absolute_char_index, replacement), ...]`` to `text`: each index's
    single character is replaced by its replacement string (``""`` deletes). Indices
    refer to the ORIGINAL text; edits are applied right-to-left so a deletion never
    shifts an earlier index. Edits at out-of-range indices are ignored."""
    if not edits:
        return text
    out = list(text)
    n = len(out)
    for idx, repl in sorted(edits, key=lambda e: e[0], reverse=True):
        if 0 <= idx < n:
            out[idx:idx + 1] = repl
    return "".join(out)
