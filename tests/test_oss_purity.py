"""Fast source-surface OSS-purity guard (the no-build pre-check).

The free skill's source AND its shipped docs must describe ONLY free behavior.
This scans the package source tree (``buddhi_review/**`` — every ``*.py``,
``skills/**/*.{md,yml}`` and ``*.sh``) — PLUS the public-repo-only surface the
artifact gate never sees (``tests/**/*.py``, ``tests/**/*.md``, the root-level
``*.md``, ``docs/**/*.md``, the hand-authored README chart ``docs/**/*.svg``,
and the public CI workflows ``.github/workflows/*.{yml,yaml}``) — for:

  * paid PRODUCT / INTERNAL names and paid IDENTIFIER names that have no
    legitimate free use (Telegram, Autopilot, Cockpit, self-heal, the paid
    auto-rebase mechanism framed as a free limitation, the private reference
    tree's internal label ``mono``, and the paid module / reserved-cell
    identifiers ``review_loop`` / ``dashboard_server`` /
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


def _acquisition_allowlisted(path: Path) -> bool:
    """True for the ONE sanctioned license-acquisition module (PRO-6 §E.9(a)),
    which may name the pro package it installs + the Keygen acquisition vocabulary
    the rest of the tree may not."""
    return path.relative_to(_PKG.parent).as_posix() in g._ACQUISITION_ALLOWLIST


@pytest.mark.parametrize("path", _shipped_files(), ids=lambda p: str(p.name))
def test_no_paid_or_private_surface(path):
    hits = g.scan_paid_and_publish(
        path.read_text(encoding="utf-8"),
        allow_acquisition=_acquisition_allowlisted(path))
    assert hits == [], f"{path.name}: {hits}"


def test_at_least_the_new_modules_are_scanned():
    names = {p.name for p in _shipped_files()}
    for expected in ("wizard.py", "open_pr.py", "shell_env.py", "setup_launcher.py",
                     "backends.py", "git_guardrail_hook.py", "plan_profile.py"):
        assert expected in names


# ── Public-repo-only surface: tests/, root & docs markdown, README-chart SVGs,
#    and the public CI workflow YAML ──────────────────────────────────────────────
# These files ship in the PUBLIC repo (never the wheel), so the artifact gate
# never scans them — historically a blind spot: a private-tree reference in a
# test comment, a root doc, a hand-drawn SVG's ``<text>``/``<!-- comment -->``,
# or a workflow ``name:``/``run:`` line sailed through. Scanned with the same
# paid/publish scanner. The entitlement scan stays package-code-only by design:
# tests and docs legitimately say "MIT License".
_REPO = _PKG.parent
_TESTS = Path(__file__).resolve().parent

# Files that enumerate the forbidden vocabulary BY DESIGN — scanner fixtures and
# negative assertions (``assert "manasvi" not in text``). This is the same
# scaffolding carve-out the artifact gate applies to tests/ + tools/ wholesale,
# but per-file, so every OTHER file stays scanned and a NEW file is scanned by
# default. Entries are REPO-RELATIVE POSIX paths (not bare basenames), so the
# exemption pins to the exact fixture: a future same-named file at a different
# path (e.g. ``tests/subdir/test_wizard.py``) is still scanned. Every entry must
# actually trip the scanner (honesty guard below): a scan-clean or renamed entry
# must be removed, so the allowlist can never quietly exempt an ordinary file.
_VOCAB_SCAFFOLDING = frozenset({
    "tests/test_oss_purity.py",              # this guard: docstring + re-exported tables
    "tests/test_publish_gate.py",            # scanner unit fixtures spell the vocabulary
    "tests/test_verdict_parity.py",          # fixture guard mirrors the tables (superset)
    "tests/test_claude_workflow_parity.py",  # asserts author/handle absent from the workflow
    "tests/test_open_pr.py",                 # asserts the paid rebase notice never prints
    "tests/test_escalation_triggers.py",     # asserts no autonomy-dial vocabulary leaks in
    "tests/test_notifier_transparency.py",   # asserts a paid channel value coerces to console
    "tests/test_shell_env.py",               # asserts paid env keys never reach the env
    "tests/test_wizard.py",                  # asserts wizard output ships no paid name
    "tests/test_pro_trial.py",               # the acquisition module's test names the pro package it installs
})


def _collect_public_repo_files(repo_root: Path, tests_dir: Path):
    """Enumerate the public-repo-only scan surface under ``repo_root`` (parametrized
    so the self-tests can point it at a synthetic tree). Excludes the exact
    repo-relative paths in _VOCAB_SCAFFOLDING; a glob over a missing dir is empty,
    never an error."""
    candidates = list(tests_dir.rglob("*.py"))
    candidates += tests_dir.rglob("*.md")
    candidates += repo_root.glob("*.md")
    candidates += (repo_root / "docs").rglob("*.md")
    candidates += (repo_root / "docs").rglob("*.svg")
    candidates += (repo_root / ".github" / "workflows").glob("*.yml")
    candidates += (repo_root / ".github" / "workflows").glob("*.yaml")
    return sorted(
        p for p in candidates
        if p.relative_to(repo_root).as_posix() not in _VOCAB_SCAFFOLDING
    )


def _public_repo_files():
    return _collect_public_repo_files(_REPO, _TESTS)


@pytest.mark.parametrize("path", _public_repo_files(),
                         ids=lambda p: str(p.relative_to(_REPO)))
def test_no_paid_or_private_surface_in_tests_and_docs(path):
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        pytest.fail(f"{path.relative_to(_REPO)}: not valid UTF-8")
    hits = g.scan_paid_and_publish(text)
    assert hits == [], f"{path.relative_to(_REPO)}: {hits}"


def test_public_repo_surface_is_actually_covered():
    """The globs must keep picking the surface up — an empty/mis-rooted glob
    would silently skip the whole widened scan."""
    files = _public_repo_files()
    names = {p.name for p in files}
    for expected in ("conftest.py", "test_wizard_ready_for_ci.py",
                     "README.md", "GETTING_STARTED.md", "ARCHITECTURE.md"):
        assert expected in names
    # The two hardening surfaces (defense-in-depth) must be picked up too.
    assert any(p.suffix == ".svg" for p in files), "docs SVG surface not scanned"
    assert any(p.parent.name == "workflows" and p.suffix in (".yml", ".yaml")
               for p in files), "public workflow YAML surface not scanned"


@pytest.mark.parametrize("rel", sorted(_VOCAB_SCAFFOLDING))
def test_vocab_scaffolding_allowlist_is_honest(rel):
    path = _REPO / rel
    assert path.exists(), (
        f"stale _VOCAB_SCAFFOLDING entry '{rel}' — the file is gone or renamed; "
        f"remove (or update) the entry")
    hits = g.scan_paid_and_publish(path.read_text(encoding="utf-8"))
    assert hits != [], (
        f"'{rel}' is scan-clean — it no longer earns its exemption; remove it "
        f"from _VOCAB_SCAFFOLDING so it is scanned like every other file")


# ── Self-tests for the widened enumeration + relative-path exemption ─────────────
def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_svg_and_workflow_surfaces_are_scanned(tmp_path):
    """A planted forbidden word in a docs SVG and a public workflow YAML is picked
    up by the enumeration and caught by the scanner — these ship in the public
    repo but never in the wheel, so only this guard covers them."""
    svg = _write(tmp_path / "docs" / "assets" / "x.svg",
                 '<svg xmlns="http://www.w3.org/2000/svg">'
                 '<!-- Autopilot --><text>chart</text></svg>')
    wf = _write(tmp_path / ".github" / "workflows" / "leak.yml",
                "name: ci\njobs:\n  t:\n    steps:\n      - run: echo bot_quota\n")
    collected = _collect_public_repo_files(tmp_path, tmp_path / "tests")
    rels = {p.relative_to(tmp_path).as_posix() for p in collected}
    assert "docs/assets/x.svg" in rels, "docs SVG not enumerated"
    assert ".github/workflows/leak.yml" in rels, "workflow YAML not enumerated"
    assert g.scan_paid_and_publish(svg.read_text(encoding="utf-8")), "SVG leak not caught"
    assert g.scan_paid_and_publish(wf.read_text(encoding="utf-8")), "workflow leak not caught"


def test_allowlist_pins_to_relative_path_not_basename(tmp_path):
    """The fixture exemption pins to the exact repo-relative path: a file sharing
    an allowlisted BASENAME at a different path is NOT exempted (it is scanned)."""
    tests = tmp_path / "tests"
    _write(tests / "test_wizard.py", "x = 1\n")             # allowlisted exact path
    _write(tests / "subdir" / "test_wizard.py", "x = 1\n")  # same basename, other path
    rels = {p.relative_to(tmp_path).as_posix()
            for p in _collect_public_repo_files(tmp_path, tests)}
    assert "tests/test_wizard.py" not in rels, "exact-path fixture must stay exempted"
    assert "tests/subdir/test_wizard.py" in rels, (
        "a same-basename file at a different path must be scanned, not exempted")


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
    if _acquisition_allowlisted(path):
        pytest.skip("pro_trial.py is the sanctioned acquisition module — it may name "
                    "the Keygen acquisition vocabulary; the creation-only guard below "
                    "asserts it holds ZERO enforcement logic")
    hits = g.scan_entitlement(path.read_text(encoding="utf-8"))
    assert hits == [], f"{path.name}: {hits}"


# ── The acquisition allowlist is scoped, not a hole: acquisition ONLY, never
#    enforcement ──────────────────────────────────────────────────────────────────
# pro_trial.py is allowed the Keygen acquisition vocabulary so the wizard can mint a
# trial + install the wheel — but it must contain creation / registration /
# validation calls ONLY, and ZERO entitlement-ENFORCEMENT logic (no signed-machine-
# file verify, no runtime lease check, no signature math). The compiled wheel checks
# itself (§E.1/§E.3); a lease check in the readable free tree is both trivially
# patched AND a blueprint of the paid architecture. These greppable markers pin that.
_ENFORCEMENT_MARKERS = (
    "verify_lease", "require_lease", "ed25519", "machine_file", "machine-file",
    "machine file", "parse_and_verify", "heartbeat", "anti_rollback",
    "last_seen_online", "signed machine",
)
_ACQUISITION_MARKERS = ("register_user", "create_trial_license", "validate_key")


def test_acquisition_module_is_creation_only():
    """The allowlisted acquisition module holds creation/registration/validation
    calls only, and NO entitlement-enforcement logic."""
    allowlisted = [p for p in _CODE_FILES if _acquisition_allowlisted(p)]
    assert allowlisted, "the acquisition allowlist names a module that is not shipped"
    for path in allowlisted:
        text = path.read_text(encoding="utf-8").lower()
        present = [m for m in _ENFORCEMENT_MARKERS if m in text]
        assert present == [], f"{path.name}: enforcement logic leaked in: {present}"
        assert any(m in text for m in _ACQUISITION_MARKERS), (
            f"{path.name}: no acquisition markers found — is this still the "
            f"acquisition module?")
