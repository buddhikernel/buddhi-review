"""Skill content-transform + provenance seam.

When the package installs its bundled skills into a user's Claude Code config, the
raw ``SKILL.md`` / reference files are passed through an ordered chain of *content
transforms* before being written to disk, and the post-transform bytes are hashed
so a later integrity check can tell an untouched managed file from a user-edited
one. This module owns that seam:

  * :func:`package_version` — the single-sourced package version string.
  * an ordered, **extensible** transform registry (:func:`register_transform` /
    :func:`apply_transforms`) — a later module can add a transform by *calling*
    :func:`register_transform` at import time; it never needs to edit this file.
  * one transform shipped today — :data:`VERSION_STAMP_KEY` records the producing
    version into a skill's YAML frontmatter (idempotent).
  * :func:`content_hash` — SHA-256 of the POST-transform text.

The transforms are pure string→string functions, so they are unit-testable in
isolation and carry no filesystem or network side effects. The actual install /
write step is intentionally NOT here.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Union

# A transform is a pure function of the file text plus a context mapping (the
# install step's per-file metadata, e.g. an explicit ``version``).
TransformFn = Callable[[str, Mapping[str, Any]], str]

# Frontmatter key the version-stamp transform writes. Claude Code's skill loader
# tolerates unknown frontmatter keys, so recording provenance here is safe.
VERSION_STAMP_KEY = "x-buddhi-version"

# Default ordering slot for a registered transform. Lower runs earlier. The
# built-in version stamp sits at this default; a later transform picks a smaller
# or larger number to run before or after it without editing this module.
DEFAULT_ORDER = 100


def package_version() -> str:
    """Return the package's single-sourced version string.

    Reads ``buddhi_review.__version__`` (a plain top-level string literal) lazily,
    so importing this module never pulls in the package's heavier optional
    dependencies.
    """
    from buddhi_review import __version__

    return __version__


@dataclass(frozen=True)
class Transform:
    """One registered content transform: a name, an ordering key, and the function."""

    name: str
    order: int
    fn: TransformFn


# Insertion-ordered registry. ``registered_transforms()`` returns a view sorted by
# ``order`` (stable, so equal orders keep registration order).
_REGISTRY: list[Transform] = []


def register_transform(
    fn: Optional[TransformFn] = None,
    *,
    name: Optional[str] = None,
    order: int = DEFAULT_ORDER,
) -> Any:
    """Register a content transform. Usable three ways, so a later module can add a
    transform WITHOUT editing this file:

      * bare decorator        — ``@register_transform``
      * configured decorator  — ``@register_transform(name="foo", order=50)``
      * direct call           — ``register_transform(foo, name="foo", order=50)``

    Returns the wrapped function (so the decorator forms leave the name bound).
    """

    def _decorate(func: TransformFn) -> TransformFn:
        resolved = name or getattr(func, "__name__", None) or "transform"
        _REGISTRY.append(Transform(name=resolved, order=order, fn=func))
        return func

    return _decorate(fn) if fn is not None else _decorate


def unregister_transform(target: Union[str, Transform]) -> bool:
    """Remove every registered transform whose name matches ``target`` (a name or a
    :class:`Transform`). Returns True if anything was removed. Mainly for tests."""
    wanted = target.name if isinstance(target, Transform) else target
    before = len(_REGISTRY)
    _REGISTRY[:] = [t for t in _REGISTRY if t.name != wanted]
    return len(_REGISTRY) != before


def registered_transforms() -> tuple[Transform, ...]:
    """Return the registry as a tuple ordered by ``order`` (then registration order)."""
    return tuple(sorted(_REGISTRY, key=lambda t: t.order))


def apply_transforms(text: str, *, ctx: Optional[Mapping[str, Any]] = None) -> str:
    """Thread ``text`` through every registered transform in order and return the
    result. ``ctx`` carries per-file metadata each transform may consult (e.g.
    ``{"version": "1.2.3"}`` to pin the stamped version)."""
    context: Mapping[str, Any] = dict(ctx) if ctx else {}
    for transform in registered_transforms():
        text = transform.fn(text, context)
    return text


def content_hash(text: str) -> str:
    """Return a SHA-256 hex digest of ``text``, invariant to line-ending style.

    Callers hash the **post-transform** text (``apply_transforms(...)``): hashing
    after the version stamp is applied is what lets a later integrity check
    recognise a freshly-installed managed file instead of flagging the stamp as a
    user modification.

    Line endings are normalised (CRLF / lone CR → LF) before hashing, so a pure
    EOL conversion of a managed file — e.g. an editor rewriting CRLF→LF with no
    content change — does NOT read as a modification. Only EOL bytes are collapsed;
    every other byte is hashed as-is, so a genuine content edit is always caught.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ── The one transform shipped today: stamp the producing version ────────────────
def _stamp_version(text: str, ctx: Mapping[str, Any]) -> str:
    """Record ``x-buddhi-version: <ver>`` in the leading YAML frontmatter block.

    Idempotent: re-running with the same version yields byte-identical text, and an
    existing stamp is updated in place (never duplicated). Content without a leading
    ``---`` frontmatter block (e.g. a reference ``.yml``) is returned unchanged.

    EOL- and BOM-preserving: the inserted line matches the file's existing line
    ending (CRLF vs LF) so the post-transform bytes never carry a mixed-EOL line
    that a later normalisation could flip — which would otherwise make a stamped
    file falsely look modified to :func:`content_hash`. A leading UTF-8 BOM is
    carried through and the frontmatter beneath it is still stamped.
    """
    version = str(ctx.get("version") or package_version())
    # A leading UTF-8 BOM (U+FEFF) is split off so the frontmatter beneath it is
    # still detected, then re-prepended unchanged. ``chr(0xFEFF)`` keeps this source
    # pure-ASCII and reviewable — no invisible BOM literal in the file.
    bom_char = chr(0xFEFF)
    bom, payload = (bom_char, text[1:]) if text.startswith(bom_char) else ("", text)
    lines = payload.split("\n")
    if not lines or lines[0].strip() != "---":
        return text  # no frontmatter to stamp (return the original, BOM intact)

    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        return text  # unterminated frontmatter — leave it untouched

    # ``split("\n")`` leaves a trailing ``\r`` on each line of a CRLF file; mirror it
    # on the inserted/updated stamp line so the whole block stays single-EOL.
    eol = "\r" if lines[0].endswith("\r") else ""
    stamp_line = f"{VERSION_STAMP_KEY}: {version}{eol}"
    key_re = re.compile(rf"^\s*{re.escape(VERSION_STAMP_KEY)}\s*:", re.IGNORECASE)
    body = lines[1:close]
    for idx, line in enumerate(body):
        if key_re.match(line):
            body[idx] = stamp_line
            break
    else:
        body.append(stamp_line)

    return bom + "\n".join([lines[0], *body, *lines[close:]])


register_transform(_stamp_version, name="version-stamp", order=DEFAULT_ORDER)
