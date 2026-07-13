"""netrc_writer.py — a generic, merge-preserving writer for a single ``~/.netrc``
machine entry.

A ``machine <host>`` stanza (with its ``login`` / ``password``) is upserted into
the user's ``~/.netrc`` so a credential-authenticated tool (``pip`` against a
private index, ``curl``, …) can find it. The write is:

  * **merge-preserving** — every OTHER stanza and comment is kept byte-for-byte;
    only the target host's stanza is replaced (or appended when absent);
  * **idempotent** — re-running with the same host replaces our stanza in place
    rather than appending a duplicate;
  * **atomic + 0600** — a temp file + ``os.replace`` (a ``~/.netrc`` MUST be 0600
    or most tools refuse to read it), so a mid-write crash never truncates the
    live file.

This module is deliberately GENERIC and credential-agnostic: the host / login /
password are all arguments, so it names no specific service and carries no
service-specific vocabulary. The only caller today passes the private-index host.

Malformed input is handled conservatively: if the target host's stanza cannot be
located cleanly (e.g. it shares a physical line with another stanza), the existing
file is NOT rewritten — our stanza is appended and the return value reports
``"appended-unparsed"`` so the caller can surface that the file needs a human look.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple

# netrc top-level keywords that begin a new stanza. A line whose first token is one
# of these ends the previous ``machine`` stanza.
_TOP_KEYWORDS = ("machine", "default", "macdef")


def default_path() -> Path:
    """The ``~/.netrc`` path, overridable via ``BUDDHI_NETRC`` (tests / headless)."""
    override = os.environ.get("BUDDHI_NETRC")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".netrc"


def _format_stanza(host: str, login: str, password: str) -> str:
    """Our canonical multi-line stanza for one machine entry."""
    return f"machine {host}\n  login {login}\n  password {password}\n"


def _line_starts_stanza(line: str) -> Optional[str]:
    """The top-level keyword a line begins with (``machine`` / ``default`` /
    ``macdef``), or None. Used to bound stanzas."""
    toks = line.split()
    return toks[0] if toks and toks[0] in _TOP_KEYWORDS else None


def _line_is_our_machine(line: str, host: str) -> bool:
    toks = line.split()
    return len(toks) >= 2 and toks[0] == "machine" and toks[1] == host


def _line_is_tangled(line: str) -> bool:
    """True when a physical line packs MORE than one stanza keyword (e.g.
    ``machine a login x machine b login y``). We cannot splice such a line without
    risking another host's data, so we refuse to rewrite it."""
    toks = line.split()
    return sum(1 for t in toks if t in _TOP_KEYWORDS) > 1


def _replace_or_append(content: str, host: str, login: str, password: str) -> Tuple[str, str]:
    """Return ``(new_content, action)`` where action ∈
    {``created``, ``updated``, ``appended-unparsed``}. Preserves every non-target
    line byte-for-byte."""
    stanza = _format_stanza(host, login, password)
    if not content.strip():
        return stanza, "created"

    lines = content.splitlines(keepends=True)
    # Refuse to splice a physical line that packs more than one stanza (another
    # host's credentials could ride it) — append instead and report it.
    if any(_line_is_our_machine(l, host) and _line_is_tangled(l) for l in lines):
        sep = "" if content.endswith("\n") else "\n"
        return content + sep + stanza, "appended-unparsed"

    # Locate EVERY (untangled) stanza for this host. netrc resolves duplicate
    # `machine` entries to the LAST one in the file, so replacing only the first
    # match would leave a later stale duplicate as the one that actually wins —
    # collapse all matching stanzas into a single upserted one at the first
    # occurrence's position instead.
    spans = []
    i = 0
    while i < len(lines):
        if _line_is_our_machine(lines[i], host):
            start = i
            end = start + 1
            while end < len(lines) and _line_starts_stanza(lines[end]) is None:
                end += 1
            spans.append((start, end))
            i = end
        else:
            i += 1

    if not spans:
        sep = "" if content.endswith("\n") else "\n"
        return content + sep + stanza, "created"

    first_start = spans[0][0]
    pieces = ["".join(lines[:first_start]), stanza]
    prev_end = spans[0][1]
    for s, e in spans[1:]:
        pieces.append("".join(lines[prev_end:s]))
        prev_end = e
    pieces.append("".join(lines[prev_end:]))
    new_content = "".join(pieces)
    return new_content, "updated"


def _atomic_write_0600(path: Path, content: str) -> bool:
    """Write ``content`` to ``path`` atomically at mode 0600 (temp + ``os.replace``).
    Never truncates the live file on a mid-write failure. Returns True on success."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_str = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        tmp = Path(tmp_str)
        try:
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = None
                f.write(content)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
        except BaseException:
            if fd is not None:
                os.close(fd)
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


def upsert(host: str, login: str, password: str, *, path: Optional[Path] = None) -> Tuple[bool, str]:
    """Upsert the ``machine <host>`` stanza into ``~/.netrc`` (merge-preserving,
    idempotent, atomic, 0600). Returns ``(ok, action)`` where ``action`` ∈
    {``created``, ``updated``, ``appended-unparsed``, ``read-error``}; ``ok`` is
    False on a write error OR when an existing file can't be read/decoded — we
    never treat an unreadable file as empty, since that would overwrite it and
    destroy every other stanza it holds (the live file is then left untouched)."""
    target = path or default_path()
    try:
        content = target.read_text(encoding="utf-8") if target.exists() else ""
    except (OSError, UnicodeDecodeError):
        return False, "read-error"
    new_content, action = _replace_or_append(content, host, login, password)
    ok = _atomic_write_0600(target, new_content)
    return ok, action
