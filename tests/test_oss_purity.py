"""Fast source-surface OSS-purity guard (the no-build pre-check).

The free skill's source AND its shipped docs must describe ONLY free behavior.
This scans the package source tree (``buddhi_review/**`` — every ``*.py``,
``skills/**/*.{md,yml}`` and ``*.sh``) for:

  * paid PRODUCT / INTERNAL names and paid IDENTIFIER names that have no
    legitimate free use (Telegram, Autopilot, Cockpit, self-heal, the paid
    auto-rebase mechanism framed as a free limitation, and the paid module /
    reserved-cell identifiers ``review_loop`` / ``dashboard_server`` /
    ``telegram_status_bot`` / ``bot_quota`` / ``oob_resolution`` / ``App1`` /
    ``App2`` / ``Stage-0`` / ``stage0``);
  * publish-gate strings (author path, owner handle, internal registry);
  * entitlement / licence-check LOGIC in the package code (no ``verify_lease`` /
    ``keygen`` / ``license`` symbol — the free skill never checks a lease).

The rule tables and the scanners live in ``tools/publish_gate.py`` (one
definition, shared with the authoritative built-artifact gate in
``tests/test_publish_gate.py``). The legitimate Apache-2.0 kernel-seam
references the free skill ships — the *signaled* OOB source and the
*pass-through* Stage-0 conditioning verb (incl. ``buddhi.stage0.conditioning``)
— are allowlisted there; the paid App1/App2 cells are not.

NOTE: ``force-push`` is deliberately NOT forbidden — ``merge.py`` legitimately
documents the free squash-merge as one that "never force-pushes". The git
``--force-with-lease`` flag the guardrail hook (FREE-2) documents as a thing it
BLOCKS is likewise allowlisted out of the entitlement ``lease`` scan.
"""
import sys
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent / "buddhi_review"
sys.path.insert(0, str(_PKG.parent / "tools"))

import publish_gate as g  # noqa: E402

# Re-exported for sibling guards (tests/test_verdict_parity.py asserts its JSON
# fixture publish-clean list stays a superset of these).
_FORBIDDEN = g.FORBIDDEN_TERMS
_PUBLISH_GATE = g.PUBLISH_GATE_TERMS


def _shipped_files():
    files = list(_PKG.rglob("*.py"))
    files += list((_PKG / "skills").rglob("*.md"))
    files += list((_PKG / "skills").rglob("*.yml"))
    files += list(_PKG.rglob("*.sh"))
    return files


@pytest.mark.parametrize("path", _shipped_files(), ids=lambda p: str(p.name))
def test_no_paid_or_private_surface(path):
    hits = g.scan_paid_and_publish(path.read_text(encoding="utf-8"))
    assert hits == [], f"{path.name}: {hits}"


def test_at_least_the_new_modules_are_scanned():
    names = {p.name for p in _shipped_files()}
    for expected in ("wizard.py", "create_pr.py", "shell_env.py", "setup_launcher.py",
                     "backends.py", "git_guardrail_hook.py", "plan_profile.py"):
        assert expected in names


# ── No entitlement / licence-check logic anywhere in the free code ─────────────────
# The free skill performs NO licence/entitlement/lease check (exec-plan §E.1): such
# logic in readable source is both trivially patched out AND a blueprint of the paid
# architecture. The front-door seam asks a backend only a generic yes/no
# (``is_active()``) — never why. This guard locks that invariant over the package
# CODE surface (``*.py`` / ``*.sh``); docs legitimately mention "MIT License" and a
# provider "entitlement", so the scan never runs over them.
_CODE_FILES = [p for p in _shipped_files() if p.suffix in (".py", ".sh")]


@pytest.mark.parametrize("path", _CODE_FILES, ids=lambda p: str(p.name))
def test_no_entitlement_logic_symbols(path):
    hits = g.scan_entitlement(path.read_text(encoding="utf-8"))
    assert hits == [], f"{path.name}: {hits}"
