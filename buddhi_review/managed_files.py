"""Versioned managed-file sync.

The setup wizard installs a small set of config files into a user's repo (the
Claude reviewer workflow, the label-gated CI workflow). Each ships with a
``# buddhi-managed-version: N`` marker so a later setup run can tell an OUTDATED
installed copy from an up-to-date one and offer the latest — instead of the old
"present by name = done" check that silently skips a stale file (the bug that
left buddhi-review's pre-guard ``claude-code-review.yml`` in place).

The marker is a COMMENT, not a YAML key, so it never changes how GitHub parses
the workflow; it is matched on raw text. This module is pure stdlib + pathlib
with NO subprocess — the wizard fetches the installed text and feeds it in — so
the version comparison stays trivially unit-testable.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

# A standalone comment line ``# buddhi-managed-version: <int>`` anywhere in a
# managed file. Case-insensitive, optional surrounding whitespace. The integer is
# monotonic — bump it whenever the SHIPPED file changes so an older installed copy
# is detected as out of date. A file with NO marker is a legacy (pre-versioning)
# copy and is treated as older than any versioned shipped file.
MANAGED_VERSION_RE = re.compile(
    r"(?im)^[ \t]*#[ \t]*buddhi-managed-version:[ \t]*(\d+)[ \t]*\r?$"
)

_REFERENCES_DIR = (
    Path(__file__).resolve().parent / "skills" / "review-pr" / "references"
)


def file_version(text: Optional[str]) -> Optional[int]:
    """The ``buddhi-managed-version`` integer carried by ``text``, or ``None`` when
    the marker is absent/unparseable (a legacy, pre-versioning file). Never raises."""
    if not text:
        return None
    m = MANAGED_VERSION_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def shipped_version(template_path: Path) -> Optional[int]:
    """The version marker carried by the BUNDLED template at ``template_path`` — the
    'latest' a user can be offered. ``None`` if the file is missing/unreadable or
    carries no marker."""
    try:
        return file_version(Path(template_path).read_text(encoding="utf-8"))
    except OSError:
        return None


def needs_update(installed: Optional[int], shipped: Optional[int]) -> bool:
    """Whether an installed copy should be offered an update.

    Only when we KNOW the shipped version AND the installed copy is either
    unversioned (legacy → treated as older) or a strictly lower version. An unknown
    shipped version → never offer (we can't claim 'newer'). Version numbers, not
    content hashes, gate the offer, so a user who merely CUSTOMISED their file is
    never mistaken for outdated unless its marker is genuinely lower."""
    if shipped is None:
        return False
    return installed is None or installed < shipped


# The files the setup wizard installs into a user's repo. Each entry binds the
# bundled template (the source of the latest version) to the destination path on
# the user's default branch. Adding a future managed config file = one entry.
MANAGED_FILES: List[Dict[str, object]] = [
    {
        "name": "claude-code-review.yml",
        "label": "Claude review workflow",
        "template": _REFERENCES_DIR / "claude-code-review.yml",
        "dest": ".github/workflows/claude-code-review.yml",
    },
    {
        "name": "tests-ready-for-ci.yml",
        "label": "label-gated CI workflow",
        "template": _REFERENCES_DIR / "tests-ready-for-ci.yml",
        "dest": ".github/workflows/tests-ready-for-ci.yml",
    },
]
