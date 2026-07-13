#!/usr/bin/env python3
"""Publish-readiness gate for the free buddhi-review skill (FREE-3).

This is the **fail-closed release gate** that runs before the free skill is
published to PyPI / the public repo. It replaces the old source-glob OSS-purity
check (which only read ``*.md``/``*.yml`` under ``skills/`` and missed root
package-data such as ``plan_profiles.yml``) with a scan of the **built
artifact** — the exact bytes a user ``pip install``s.

Two subcommands:

  * ``scan``    — build the sdist + wheel from a **clean** source (no stale
                  ``__pycache__/*.pyc``), unpack both, grep every shipped text
                  file for paid/internal product names, paid identifier names,
                  and publish-gate strings (author path / owner handle / private
                  registry), and assert the wheel ships **no compiled
                  extension** (``.so``/``.pyd``/``.c``/…). Exits non-zero on any
                  violation or if the build toolchain is missing — fail closed.
  * ``publish`` — copy **only** ``public/`` to a target tree and FAIL if any
                  above-``public/`` staging-root file (BACKPORT-PLAN.md,
                  BUILD-PLAN.md, FREE-SKILL-NOTES.md, the staging README, any
                  sibling ``buddhi*/`` design doc) would ship. The location
                  boundary is the real wall: the grep catches author/handle
                  leaks, not paid *design prose*.

The rule tables and the pure scan helpers are imported by the pytest suite
(``tests/test_oss_purity.py`` + ``tests/test_publish_gate.py``) so the gate has
exactly one definition. ``tools/`` and ``tests/`` are deliberately NOT part of
the scanned product surface: they are the gate's own scaffolding and enumerate
the forbidden vocabulary as assertion literals by design (and never reach the
installed wheel — only ``buddhi_review/`` does). The fast source-surface
pre-check (``tests/test_oss_purity.py``) separately covers ``tests/**/*.py``
and the root/docs markdown with a per-file scaffolding allowlist, so the
public-repo-only files get the same scan without tripping on the gate's own
vocabulary literals.
"""
from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import zipfile
from pathlib import Path

# ── Forbidden vocabulary ────────────────────────────────────────────────────────
# Paid product / internal names + the limitation-framed auto-rebase mechanism.
# These have NO legitimate free use and are matched as case-insensitive
# substrings anywhere in a shipped text file.
_FORBIDDEN_SUBSTR = (
    # paid product / internal surface (the pre-FREE-3 list)
    "Telegram", "Autopilot", "Cockpit", "self-heal",
    "auto-rebase", "--implementer-session", "keep this session open",
    # The paid "Buddhi Board" / work-dashboard may be killed at launch — never advertise it in free.
    "Buddhi Board", "work dashboard", "buddhi-board", "work-dashboard",
    # paid module / identifier names that have no legitimate free use (FREE-3 widen).
    # ``dashboard_server`` is covered by the ``dashboard_`` family stem below.
    "telegram_status_bot", "bot_quota", "oob_resolution",
)
# Paid identifier names that DO have a legitimate free *superstring* — matched as
# whole words so the free symbol is not a false positive:
#   review_loop        — free ships ``launch_review_loop`` / ``run_review_loop``;
#                        the bare module name ``review_loop`` is the paid monolith.
#   app1 / app2        — the paid reserved-cell labels (App1 autonomous-OOB,
#                        App2 in-place Stage-0); never a free identifier.
#   oob                — bare paid reference; the legitimate kernel-seam uses
#                        (``SignaledOOBSource``, ``oob_source``, ``can_observe_oob``)
#                        glue "oob" to a word char so ``\boob\b`` never matches
#                        them, and the prose "OOB source"/"Signaled-OOB" is
#                        scrubbed via _KERNEL_SEAM_ALLOWLIST before this runs.
#   mono               — the private reference tree's internal label; a bare
#                        "MONO" cross-reference in a comment is a private-tree
#                        leak. Whole-word so ``monotonic`` / ``monospace`` /
#                        "monolith" never trip (``_`` is a word char, so glued
#                        identifiers don't match either).
# (``dashboard_server`` / ``dashboard_refresh`` are covered by the ``dashboard_``
# family stem in _PAID_MODULE_NAMES below.)
_FORBIDDEN_WORD = (
    "review_loop", "app1", "app2", "oob", "mono",
)
# ``stage0`` / ``Stage-0`` are matched as substrings *after* the kernel-seam
# scrub. The only legitimate free uses — the ``buddhi.stage0.conditioning``
# import and the "Stage-0 condition one …" pass-through-verb prose — are removed
# by the allowlist, so a paid leak (``stage0_localizer``, "in-place Stage-0
# conditioning") survives the scrub and is caught. Substrings (not ``\b``-words)
# so glued forms like ``stage0_inplace`` cannot slip past a trailing ``_``.
_FORBIDDEN_SUBSTR_SCRUBBED = ("stage0", "stage-0")

# Paid-tier monolith module + namespace identifiers. The realistic open-core-split
# leak is a copy-paste of a private module (or its import) into a free
# ``buddhi_review/*.py`` — bare identifiers carry no give-away prose, so the
# per-name denylist must enumerate the paid module surface, not just a handful.
# Matched as case-insensitive substrings; every entry is verified absent from the
# free tree. ``dashboard_`` is a family stem (free is console-only and ships NO
# dashboard); the Telegram-daemon / budget / orchestration siblings are spelled in
# full because ``status_`` / ``usage`` / ``spawn`` legitimately occur in free code.
# ``version_info`` is deliberately NOT listed — it collides with stdlib
# ``sys.version_info``; the namespace + ``dashboard_`` rules cover that paid daemon
# module's realistic leak vector.
_PAID_MODULE_NAMES = (
    # the closed pro package namespace — any import of it is a leak. The unified
    # ``buddhi_review_pro`` / ``buddhi-review-pro`` is the current paid name; the
    # ``buddhi_pro`` / ``buddhikernel_pro`` (and dash) forms stay as catch-alls for
    # the earlier names. ONLY the full ``_pro`` / ``-pro`` token is listed — the
    # bare ``buddhi_review`` / ``buddhi-review`` is the FREE package/import name and
    # appears all over the free tree, so it must NEVER be a denylist term.
    "buddhi_pro", "buddhi-pro", "buddhikernel_pro", "buddhikernel-pro",
    "buddhi_review_pro", "buddhi-review-pro",
    # paid Work-dashboard family (stem) + paid Telegram-daemon read/IPC siblings
    "dashboard_", "status_data", "status_ipc",
    # paid budget / usage tracking
    "usage_cli", "usage_snapshot", "claude_usage", "loop_ledger",
    # paid / admin-only orchestration
    "parent_merge_watcher", "merge_conflict_resolver", "dispatch_bridge",
    "run_multi_repo", "_admin_log", "spawn-team",
)

# Publish-gate strings that must never appear in the public tree. ``snab`` is the
# operator's company handle (snab.cab / the snab-cab-* repos) — same category as
# the author path / owner handle. ``manasvi`` is the operator's bare username/handle
# (the macOS home-dir + GitHub-adjacent handle); listing it as well as the full
# ``/Users/manasvi`` path catches a bare reference that drops the ``/Users/`` prefix.
# Matched case-insensitively as a substring; the literal here lives in this gate's
# own scaffolding (``tools/``), which scan_tree() excludes, so it never self-trips.
_PUBLISH_GATE = ("/Users/manasvi", "manasvi", "m-s-21", "project-registry", "snab")

# High-confidence credential SHAPES (not fixed strings) that must never ship in any
# shipped text file. A Telegram bot token is ``<bot-id>:<secret>`` — 6–15 digits, a
# colon, then 34–40 url-safe chars. Matched on the lowercased, format-folded text
# (so a zero-width-char evasion still trips); the matched value is NEVER echoed
# into a hit message — only the shape is named.
_SECRET_PATTERNS = (
    ("Telegram bot token", re.compile(r"(?<!\d)\d{6,15}:[a-z0-9_-]{34,40}(?![a-z0-9_-])")),
)

# Public view of the forbidden vocabulary, for sibling guards + drift checks
# (``tests/test_oss_purity.py`` re-exports these; ``tests/test_verdict_parity.py``
# asserts its JSON-fixture guard stays a superset).
FORBIDDEN_TERMS = (
    _FORBIDDEN_SUBSTR + _PAID_MODULE_NAMES + _FORBIDDEN_SUBSTR_SCRUBBED + _FORBIDDEN_WORD
)
PUBLISH_GATE_TERMS = _PUBLISH_GATE

# Legitimate Apache-2.0 kernel-seam references the free skill ships. These name
# the *signaled* OOB source and the *pass-through* Stage-0 conditioning verb —
# the naive cells free legitimately implements — NOT the paid App1/App2 cells
# (autonomous OOB resolution / in-place localizer). Each token is scrubbed
# (case-insensitively) before the paid-identifier scan; the tokens are specific
# enough that no paid variant ("autonomous OOB", "in-place Stage-0 conditioning")
# is a substring of any of them, so a real leak is never masked.
_KERNEL_SEAM_ALLOWLIST = (
    "buddhi.stage0.conditioning",  # the Apache-2.0 kernel import (explicit allowlist per FREE-3)
    "stage-0 condition one",       # adapter/README prose: the pass-through conditioning verb
    "oob source",                  # the signaled-OOB seam's English name (README + seams.py)
    "signaled-oob",                # adapter docstring "Signaled-OOB only"
)

# ── No entitlement / licence-check logic anywhere in the free *code* ────────────
# The free skill performs NO licence/entitlement/lease check: such logic in
# readable source is both trivially patched out AND a blueprint of the paid
# architecture. Scanned over the package CODE surface only (``buddhi_review/``),
# never over docs/packaging where "MIT License" / a provider "entitlement" are
# legitimate English.
_ENTITLEMENT_SUBSTR = ("verify_lease", "keygen", "entitlement", "entitle", "ed25519")
_ENTITLEMENT_WORD = ("license", "licence", "lease")
# Benign references scrubbed before the entitlement-word scan: the project's own
# MIT licence naming, the git ``--force-with-lease`` force-push flag that the
# git-guardrail hook (FREE-2) documents as a thing it BLOCKS, and the two "licence"
# tokens in the sanctioned unclaimed-command upgrade notice (``cli.py``; approved
# verbatim 2026-07-12, exec-plan §B2a / §E item 9c). Those tokens are product COPY,
# not a lease/entitlement CHECK — scrubbing them here lets the notice ship while every
# real check symbol (``verify_lease`` / ``keygen`` / ``ed25519`` / …) stays caught by
# the un-scrubbed substring scan above.
_BENIGN_LICENSE = (
    "mit-licensed", "mit license", "licensed under", "license file",
    "license ::", "license = ", "force-with-lease",
    "buddhi licence", "get a licence",
)

# ── The ONE sanctioned license-ACQUISITION module (PRO-6 / §E.9(a)) ─────────────
# The setup wizard's first-run trial offer must ACQUIRE a license (Keygen open
# registration + trial creation + the private-index credential + the pip install of
# the pro wheel). That is acquisition, NOT enforcement, and it is confined to this
# ONE module — which is why it may name the acquisition vocabulary the rest of the
# tree may not. It is allowlisted from the entitlement scan AND from the two
# pro-package-name paid terms it must reference to install the wheel; it is STILL
# scanned for every OTHER paid surface (Cockpit/Telegram/dashboard/…), and
# tests/test_oss_purity.py additionally asserts it holds creation/registration/
# validation calls only — zero verification logic. Its test lives under tests/,
# already excluded by _is_scaffolding, so only the module itself is named here.
_ACQUISITION_ALLOWLIST = ("buddhi_review/pro_trial.py",)
# The ONLY paid-name terms the acquisition module may use — the pro package it pip-
# installs. Every OTHER paid name stays forbidden even here.
_ACQUISITION_PAID_TERMS = ("buddhi_review_pro", "buddhi-review-pro")

# Subtrees that are the gate's own scaffolding — excluded from the paid-name
# artifact scan. They enumerate the forbidden vocabulary as test/definition
# literals and never reach the installed wheel (only ``buddhi_review/`` does).
_SCAFFOLDING_TOP = ("tests", "tools")
# The package directory — the entitlement-logic scan and "is product code" both
# key off this prefix.
_PACKAGE_DIR = "buddhi_review"
# Compiled-extension / bytecode suffixes that must never ship in a pure-Python
# free wheel.
_COMPILED_SUFFIXES = (".so", ".pyd", ".c", ".pyx", ".dylib", ".dll", ".o", ".a", ".pyc")
# Benign binary asset suffixes the gate may ship without scanning. EMPTY today:
# the free skill is pure text + .sh + .yml, so any binary in the artifact is
# fail-closed. Adding a type here is a deliberate, reviewed decision.
_ALLOWED_BINARY_SUFFIXES: tuple[str, ...] = ()
# Source-tree cruft excluded from a clean checkout (mirrors the staging .gitignore).
_CLEAN_EXCLUDE = ("__pycache__", ".pytest_cache", "*.pyc", "*.egg-info", "build", "dist", ".DS_Store")


class BuildToolingUnavailable(RuntimeError):
    """Raised when ``python -m build`` cannot run (missing build/setuptools/wheel)."""


class BuildFailedError(RuntimeError):
    """Raised when the build command fails despite tooling being available."""


# ── Pure scanners (unit-testable, no I/O) ───────────────────────────────────────
def _normalize(text: str) -> str:
    """Fold a string to a canonical form before scanning so the most common
    "invisible" evasions can't hide a forbidden substring from the grep:
      * NFKC compatibility-folds fullwidth/ligature look-alikes to their ASCII
        base (e.g. fullwidth ``ｄ`` → ``d``);
      * format / zero-width chars (Unicode category ``Cf`` — ZWSP, ZWNJ, the BOM,
        the soft hyphen) are stripped, so ``b<ZWSP>uddhi_pro`` reads as one token;
      * non-spacing marks (Unicode category ``Mn`` — combining accents, variation
        selectors such as U+FE0F) are also stripped, blocking evasion via
        ``b️uddhi_pro`` (the variation selector survives NFKC but is ``Mn``).
    Pure homoglyph substitution (e.g. Cyrillic ``о`` for Latin ``o``) is NOT folded
    — that is a deliberate-insider vector outside the threat model, backstopped by
    the public/-only location boundary and the human publish-clean review.
    """
    if text.isascii():
        return text
    folded = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in folded if unicodedata.category(ch) not in ("Cf", "Mn"))


def scan_paid_and_publish(text: str, *, allow_acquisition: bool = False) -> list[str]:
    """Return the paid-name / publish-gate violations in ``text`` (one per hit kind).

    Applies the kernel-seam allowlist scrub first so the legitimate Apache-2.0
    signaled-OOB and pass-through-Stage-0 references are not false positives.

    ``allow_acquisition`` is set ONLY for the sanctioned acquisition module
    (:data:`_ACQUISITION_ALLOWLIST`): it scrubs the two pro-package-name terms that
    module must reference to pip-install the wheel, so every OTHER paid surface is
    still scanned there. Nothing else is relaxed.
    """
    lower = _normalize(text).lower()
    if allow_acquisition:
        for term in _ACQUISITION_PAID_TERMS:
            lower = lower.replace(term.lower(), " ")
    scrubbed = lower
    for benign in _KERNEL_SEAM_ALLOWLIST:
        scrubbed = scrubbed.replace(benign, " ")
    hits: list[str] = []
    for term in _FORBIDDEN_SUBSTR:
        if term.lower() in lower:
            hits.append(f"paid/limitation surface '{term}'")
    for term in _PAID_MODULE_NAMES:
        if term.lower() in lower:
            hits.append(f"paid monolith module '{term}'")
    for term in _FORBIDDEN_SUBSTR_SCRUBBED:
        if term in scrubbed:
            hits.append(f"paid identifier '{term}'")
    for term in _FORBIDDEN_WORD:
        if re.search(rf"\b{re.escape(term)}\b", scrubbed):
            hits.append(f"paid identifier '{term}'")
    for term in _PUBLISH_GATE:
        if term.lower() in lower:
            hits.append(f"publish-gate string '{term}'")
    for label, pat in _SECRET_PATTERNS:
        if pat.search(lower):
            # Name only the shape — never echo the matched credential.
            hits.append(f"likely secret ({label} shape) — value redacted")
    return hits


def scan_entitlement(text: str) -> list[str]:
    """Return entitlement/licence-check-logic violations in ``text`` (code only)."""
    lower = _normalize(text).lower()
    hits: list[str] = []
    for term in _ENTITLEMENT_SUBSTR:
        if term in lower:
            hits.append(f"entitlement symbol '{term}'")
    scrubbed = lower
    for benign in _BENIGN_LICENSE:
        scrubbed = scrubbed.replace(benign, " ")
    for term in _ENTITLEMENT_WORD:
        if re.search(rf"\b{term}\b", scrubbed):
            hits.append(f"entitlement symbol '{term}'")
    return hits


# ── File / artifact helpers ─────────────────────────────────────────────────────
def _read_text(path: Path) -> str | None:
    """Read ``path`` as text; return None for binary or non-UTF-8 files.

    Non-UTF-8 files are treated as undecodable so scan_tree() flags them
    fail-closed rather than silently substituting U+FFFD for non-UTF-8 bytes
    (which could mask a forbidden term encoded in a non-standard way).
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_scaffolding(rel: Path) -> bool:
    """True if ``rel`` (package-rooted) is gate scaffolding (tests/ or tools/)."""
    return len(rel.parts) > 0 and rel.parts[0] in _SCAFFOLDING_TOP


def _is_package_code(rel: Path) -> bool:
    """True if ``rel`` is product CODE inside the installed package (.py/.sh)."""
    return (
        len(rel.parts) > 0
        and rel.parts[0] == _PACKAGE_DIR
        and rel.suffix in (".py", ".sh")
    )


def _is_allowed_binary(rel: Path) -> bool:
    """True if a non-text shipped file is an explicitly-allowlisted benign asset.
    Empty allowlist today → every binary in the artifact is fail-closed."""
    return rel.suffix.lower() in _ALLOWED_BINARY_SUFFIXES


def scan_tree(root: Path) -> list[str]:
    """Scan every text file under ``root`` for violations.

    ``root`` is the *package-rooted* unpacked tree (so ``rel`` paths look like
    ``buddhi_review/…``, ``tests/…``, ``README.md``, ``buddhi_review/...METADATA``).
    Returns a list of ``"<rel>: <violation>"`` strings; empty means clean.
    """
    problems: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if _is_scaffolding(rel):
            continue  # the gate's own scaffolding enumerates the vocabulary by design
        text = _read_text(path)
        if text is None:
            # Binary / NUL-containing / undecodable shipped file. It cannot be
            # grepped, so a paid blob (entitlement logic, an obfuscated artifact,
            # a buddhi_pro source carrying a NUL byte) could ride it silently.
            # The free skill ships pure text + .sh/.yml today, so any binary in the
            # artifact is FAIL-CLOSED unless explicitly allowlisted as a benign asset.
            if not _is_allowed_binary(rel):
                problems.append(f"{rel}: binary/undecodable file shipped (cannot scan for leaks)")
            continue
        # The sanctioned acquisition module may name the pro package it installs +
        # the Keygen acquisition vocabulary; it is scanned for every OTHER paid
        # surface and its entitlement scan is skipped (it does acquisition, never
        # enforcement — asserted separately by test_oss_purity).
        acquisition = rel.as_posix() in _ACQUISITION_ALLOWLIST
        for hit in scan_paid_and_publish(text, allow_acquisition=acquisition):
            problems.append(f"{rel}: {hit}")
        if _is_package_code(rel) and not acquisition:
            for hit in scan_entitlement(text):
                problems.append(f"{rel}: {hit}")
    return problems


def compiled_extensions(names: list[str]) -> list[str]:
    """Return member names that are compiled extensions / bytecode."""
    return [n for n in names if any(s.lower() in _COMPILED_SUFFIXES for s in Path(n).suffixes)]


# ── Clean checkout + build ──────────────────────────────────────────────────────
def _git_tracked(public_dir: Path) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(public_dir), "ls-files", "--error-unmatch", "pyproject.toml"],
            capture_output=True, text=True,
        )
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def clean_checkout(public_dir: Path, dest: Path, *, prefer_git: bool = True) -> Path:
    """Materialise a clean copy of ``public_dir`` into ``dest`` (no stale .pyc).

    Prefers ``git archive`` of the committed tree (the strongest guarantee that
    nothing untracked — i.e. no ``__pycache__/*.pyc`` — enters the build). Falls
    back to a filtered copy of the working tree (used by the in-dev pytest run,
    which must see uncommitted changes), excluding the .gitignore cruft set.
    """
    if dest.exists():
        for child in dest.iterdir():
            shutil.rmtree(child) if (child.is_dir() and not child.is_symlink()) else child.unlink()
    else:
        dest.mkdir(parents=True)
    if prefer_git and _git_tracked(public_dir):
        try:
            # Check for uncommitted changes to warn the user
            dirty = subprocess.run(
                ["git", "-C", str(public_dir), "status", "--porcelain", "."],
                capture_output=True, text=True, errors="replace", check=True,
            ).stdout.strip()
            if dirty:
                print(
                    "WARNING: Working tree has uncommitted changes which will be IGNORED "
                    "by the git-archive scan. Use --from-worktree to scan uncommitted changes.",
                    file=sys.stderr,
                )
            # `git archive HEAD:<public-subtree>` → pristine committed bytes
            # (untracked + .gitignore'd __pycache__/*.pyc can never enter). Run
            # from the repo TOPLEVEL: from a subdir git applies the cwd prefix as
            # an implicit pathspec and the subtree peel returns nothing.
            top = subprocess.run(
                ["git", "-C", str(public_dir), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            rel = subprocess.run(
                ["git", "-C", str(public_dir), "rev-parse", "--show-prefix"],
                capture_output=True, text=True, check=True,
            ).stdout.strip().rstrip("/")
            spec = f"HEAD:{rel}" if rel else "HEAD"
            proc = subprocess.run(
                ["git", "-C", top, "archive", "--format=tar", spec],
                capture_output=True, check=True,
            )
            if not proc.stdout:
                raise subprocess.SubprocessError("empty git archive")
            with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
                if not tar.getmembers():
                    raise subprocess.SubprocessError("git archive has no members")
                _safe_extract(tar, dest)
            return dest
        except (OSError, subprocess.SubprocessError, tarfile.TarError, ValueError):
            # Fall through to the filtered copy — it also excludes the .pyc cruft.
            for child in dest.iterdir():
                shutil.rmtree(child) if (child.is_dir() and not child.is_symlink()) else child.unlink()
    # Filtered working-tree copy (used in-dev and as the git fallback). Excludes
    # the .gitignore cruft set, so stale __pycache__/*.pyc never enter the build.
    shutil.copytree(
        public_dir, dest, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(*_CLEAN_EXCLUDE),
        symlinks=True,  # preserve symlinks — don't dereference into target bytes
    )
    # Fail closed: a symlink inside public/ pointing outside the tree would embed
    # arbitrary host-file bytes if we let it through to the built artifact.
    _symlinks = [p for p in dest.rglob("*") if p.is_symlink()]
    if _symlinks:
        names = ", ".join(str(p.relative_to(dest)) for p in _symlinks[:5])
        raise ValueError(f"clean_checkout: symlinks not allowed in publish tree: {names}")
    return dest


def build_artifacts(src: Path, outdir: Path, *, isolation: bool = False) -> tuple[Path, Path]:
    """Build sdist + wheel of ``src`` into ``outdir``. Returns ``(wheel, sdist)``.

    ``isolation=False`` (default) uses ``--no-isolation`` so the build is
    network-free given pre-installed setuptools/wheel/build (the CI + dev path).
    Raises :class:`BuildToolingUnavailable` if the toolchain is absent.
    """
    if not isolation:
        _require_build_tooling()
    outdir.mkdir(parents=True, exist_ok=True)
    # Clear stale artifacts so we never pick up a wheel/sdist from a prior run.
    for stale in list(outdir.glob("*.whl")) + list(outdir.glob("*.tar.gz")):
        stale.unlink()
    cmd = [sys.executable, "-m", "build", "--outdir", str(outdir)]
    if not isolation:
        cmd.append("--no-isolation")
    cmd.append(str(src))
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise BuildFailedError(
            f"`python -m build` failed (rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        )
    wheels = sorted(outdir.glob("*.whl"))
    sdists = sorted(outdir.glob("*.tar.gz"))
    if not wheels or not sdists:
        raise BuildFailedError(
            f"build produced no wheel/sdist (wheels={wheels}, sdists={sdists})"
        )
    return wheels[0], sdists[0]


def _require_build_tooling() -> None:
    """Raise BuildToolingUnavailable unless build + setuptools + wheel are importable
    AND carry installed metadata (``python -m build --no-isolation`` needs both)."""
    import importlib.metadata as md
    import importlib.util as iu

    missing = [m for m in ("build", "setuptools", "wheel") if iu.find_spec(m) is None]
    if missing:
        raise BuildToolingUnavailable(f"missing build modules: {', '.join(missing)}")
    nometa = []
    for dist in ("setuptools", "wheel"):
        try:
            md.version(dist)
        except md.PackageNotFoundError:
            nometa.append(dist)
    if nometa:
        raise BuildToolingUnavailable(
            f"build modules import but lack installed metadata: {', '.join(nometa)} "
            f"(run `pip install -U {' '.join(nometa)} build`)"
        )


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract ``tar`` into ``dest``, refusing path-traversal members."""
    dest = dest.resolve()
    for member in tar.getmembers():
        if member.issym() or member.islnk() or member.ischr() or member.isblk() or member.isfifo():
            raise ValueError(f"unsafe tar member type: {member.name}")
        target = (dest / member.name).resolve()
        if not target.is_relative_to(dest):
            raise ValueError(f"unsafe tar member escapes dest: {member.name}")
    tar.extractall(dest)  # noqa: S202 — members validated above


def unpack_wheel(wheel: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(wheel) as zf:
        for name in zf.namelist():
            target = (dest / name).resolve()
            if not target.is_relative_to(dest_resolved):
                raise ValueError(f"unsafe zip member: {name}")
        zf.extractall(dest)
    return dest


def unpack_sdist(sdist: Path, dest: Path) -> Path:
    """Unpack the sdist; return the inner ``<name>-<version>/`` package-root dir."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(sdist) as tar:
        _safe_extract(tar, dest)
    inner = [p for p in dest.iterdir() if p.is_dir()]
    if len(inner) != 1:
        raise ValueError(
            f"unexpected sdist layout: expected exactly one top-level directory, "
            f"got {len(inner)}: {[p.name for p in inner]}"
        )
    return inner[0]


# ── Top-level gate ──────────────────────────────────────────────────────────────
class GateResult:
    def __init__(self) -> None:
        self.problems: list[str] = []
        self.wheel: Path | None = None
        self.sdist: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


def run_artifact_gate(public_dir: Path, *, isolation: bool = False,
                      prefer_git: bool = True, workdir: Path | None = None) -> GateResult:
    """Clean-build the artifact, unpack BOTH, scan every text file, and assert the
    wheel ships no compiled extension. Returns a :class:`GateResult`."""
    result = GateResult()
    tmp = workdir or Path(tempfile.mkdtemp(prefix="buddhi-publish-gate-"))
    src = clean_checkout(public_dir, tmp / "src", prefer_git=prefer_git)
    # The clean source itself must carry no compiled bytecode (gap #5).
    stale = [str(p.relative_to(src)) for p in src.rglob("*")
             if p.is_file() and any(s.lower() in _COMPILED_SUFFIXES for s in p.suffixes)]
    if stale:
        result.problems.append(f"clean source still carries compiled files: {stale}")
    wheel, sdist = build_artifacts(src, tmp / "dist", isolation=isolation)
    result.wheel, result.sdist = wheel, sdist

    # No compiled extensions / bytecode in the published wheel (gap, pure-Python).
    with zipfile.ZipFile(wheel) as zf:
        ext = compiled_extensions(zf.namelist())
    if ext:
        result.problems.append(f"wheel ships compiled extension(s): {ext}")

    # Scan the wheel (every text file — it is the installed product surface).
    wheel_tree = unpack_wheel(wheel, tmp / "wheel")
    result.problems += [f"[wheel] {p}" for p in scan_tree(wheel_tree)]

    # Scan the sdist (its package-rooted inner dir).
    sdist_tree = unpack_sdist(sdist, tmp / "sdist")
    result.problems += [f"[sdist] {p}" for p in scan_tree(sdist_tree)]
    return result


# ── Publish boundary (scope d) ──────────────────────────────────────────────────
# Names that live ABOVE public/ in the private staging root and must NEVER ship.
_ABOVE_PUBLIC_SENTINELS = (
    "BACKPORT-PLAN.md", "BUILD-PLAN.md", "FREE-SKILL-NOTES.md", "README.md", ".gitignore",
)


def _staging_above_public_staged(staging_root: Path) -> list[str]:
    """Return git-staged paths inside the staging tree that live ABOVE public/.

    The location boundary is the real wall: if the maintainer has staged any
    above-``public/`` staging-root file (a design doc, a plan, the private
    README) while publishing, that is exactly the leak this gate exists to stop.
    Scoped to the staging subtree so unrelated monorepo staging is ignored.
    Empty list when not a git repo.
    """
    # ``core.quotepath=false`` + ``-z`` (NUL-delimited) so a non-ASCII filename
    # (Cyrillic/CJK/accented/emoji — common in this i18n-heavy codebase) comes back
    # as a literal UTF-8 path. Git's DEFAULT C-quotes such paths
    # (``"…\321\200…"``), which would fail the ``startswith(prefix)`` test below and
    # silently let a staged private file slip the boundary.
    quiet = ["-c", "core.quotepath=false"]
    try:
        repo_prefix_bytes = subprocess.run(
            ["git", "-C", str(staging_root), *quiet, "rev-parse", "--show-prefix"],
            capture_output=True, check=True,
        ).stdout.strip()
        repo_prefix = repo_prefix_bytes.decode("utf-8", errors="surrogateescape")
        out_bytes = subprocess.run(
            ["git", "-C", str(staging_root), *quiet, "diff", "--cached", "-z", "--name-only"],
            capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    staged = [p.decode("utf-8", errors="surrogateescape") for p in out_bytes.split(b"\0") if p]
    # repo_prefix is staging_root relative to the repo root, e.g.
    # "buddhi-claude-code-staging/". Anything staged under it but NOT under
    # "<prefix>public/" is an above-public staging file.
    above = []
    public_prefix = f"{repo_prefix}public/"
    for path in staged:
        if repo_prefix and not path.startswith(repo_prefix):
            continue
        if not path.startswith(public_prefix):
            above.append(path)
    return above


def publish(staging_root: Path, target: Path | None, *, check: bool = False) -> GateResult:
    """Copy ONLY ``staging_root/public`` to ``target``, failing if anything above
    ``public/`` would ship. ``check=True`` is a dry run (no copy) for CI."""
    result = GateResult()
    public_dir = staging_root / "public"
    if not public_dir.is_dir():
        result.problems.append(f"no public/ tree under {staging_root}")
        return result

    # 1. Location boundary: no above-public staging file may be git-staged.
    for staged in _staging_above_public_staged(staging_root):
        result.problems.append(f"above-public staging file is staged for publish: {staged}")

    # 2. Build the publish set = public/ minus build cruft. By construction the
    #    paths sit under public/, but a SYMLINK inside public/ can point above it
    #    (its path is public/x, its target ../secret), and shutil.copy2 follows
    #    symlinks — so an above-public design doc could ride the copy under a
    #    public-relative name. Refuse ALL symlinks (fail-closed): even intra-public
    #    symlinks can be dangling, ambiguous, or platform-unpredictable on copy.
    public_root = public_dir.resolve()
    publish_set = []
    for p in public_dir.rglob("*"):
        rel = p.relative_to(public_dir)
        if ".." in rel.parts:
            result.problems.append(f"publish path escapes public/: {rel}")
            continue
        if p.is_symlink():
            result.problems.append(f"symlink not allowed in publish tree: {rel} -> {os.readlink(p)}")
            continue
        if p.is_file() and not _excluded_from_publish(rel):
            publish_set.append(p)

    if check or target is None:
        # Dry run: also confirm none of the sentinels are reachable as public/ files.
        for sentinel in _ABOVE_PUBLIC_SENTINELS:
            if sentinel not in ("README.md", ".gitignore") and (public_dir / sentinel).exists():
                result.problems.append(f"above-public sentinel leaked into public/ directory: {sentinel}")
        return result

    if not result.ok:
        return result

    # 3. Real copy: only public/ contents (prefix stripped), then assert the
    #    target carries none of the above-public sentinels and no build cruft.
    target.mkdir(parents=True, exist_ok=True)
    # Sync deletions: remove target files that are no longer in public/ so that
    # files deleted from the source don't persist across publish runs. Preserve VCS
    # metadata directories (.git/, .hg/, .svn/) so the target repo stays intact.
    _VCS_DIRS = {".git", ".hg", ".svn"}
    publish_rel = {src.relative_to(public_dir) for src in publish_set}
    for existing in sorted(target.rglob("*")):
        if not existing.is_file() and not existing.is_symlink():
            continue
        rel = existing.relative_to(target)
        if rel.parts and rel.parts[0] in _VCS_DIRS:
            continue
        if rel not in publish_rel:
            existing.unlink()
    for existing in sorted(target.rglob("*"), reverse=True):
        if not existing.is_dir():
            continue
        rel = existing.relative_to(target)
        if rel.parts and rel.parts[0] in _VCS_DIRS:
            continue
        try:
            if not any(existing.iterdir()):
                existing.rmdir()
        except OSError:
            pass
    for src in publish_set:
        rel = src.relative_to(public_dir)
        out = target / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
    for sentinel in _ABOVE_PUBLIC_SENTINELS:
        if (target / sentinel).exists() and sentinel not in ("README.md", ".gitignore"):
            # public/README.md and public/.gitignore are legitimate shipped files;
            # only the private staging copies above public/ must never appear here.
            result.problems.append(f"above-public sentinel leaked into target: {sentinel}")
    leaked_cruft = [str(p.relative_to(target)) for p in target.rglob("*")
                    if p.is_file() and _excluded_from_publish(p.relative_to(target))]
    if leaked_cruft:
        result.problems.append(f"build cruft leaked into target: {leaked_cruft}")
    return result


def _excluded_from_publish(rel: Path) -> bool:
    if any(part in ("__pycache__", ".pytest_cache") for part in rel.parts):
        return True
    if any(part.endswith(".egg-info") for part in rel.parts):
        return True
    if rel.parts and rel.parts[0] in ("build", "dist"):
        return True
    return rel.suffix == ".pyc" or rel.name == ".DS_Store"


# ── CLI ─────────────────────────────────────────────────────────────────────────
def _public_dir_default() -> Path:
    # tools/publish_gate.py → public/ is the parent of tools/.
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="clean-build the artifact and scan it (fail closed)")
    s.add_argument("--public-dir", type=Path, default=_public_dir_default())
    s.add_argument("--isolation", action="store_true",
                   help="use an isolated build env (needs network; default --no-isolation)")
    s.add_argument("--from-worktree", action="store_true",
                   help="build from a filtered working-tree copy instead of git archive")

    p = sub.add_parser("publish", help="copy only public/ to a target, failing on any above-public leak")
    p.add_argument("--staging-root", type=Path, default=_public_dir_default().parent)
    p.add_argument("--target", type=Path, default=None)
    p.add_argument("--check", action="store_true", help="dry run (no copy)")

    args = parser.parse_args(argv)

    if args.cmd == "scan":
        with tempfile.TemporaryDirectory(prefix="buddhi-publish-gate-") as tmpdir:
            try:
                result = run_artifact_gate(
                    args.public_dir, isolation=args.isolation, prefer_git=not args.from_worktree,
                    workdir=Path(tmpdir),
                )
            except BuildToolingUnavailable as exc:
                print(f"PUBLISH GATE: build toolchain unavailable — FAIL CLOSED\n{exc}", file=sys.stderr)
                return 2
            except BuildFailedError as exc:
                print(f"PUBLISH GATE: build failed — FAIL CLOSED\n{exc}", file=sys.stderr)
                return 1
            except Exception as exc:
                print(f"PUBLISH GATE: unexpected error — FAIL CLOSED\n{exc}", file=sys.stderr)
                return 3
            if result.ok:
                print(f"PUBLISH GATE: PASS — artifact is publish-clean "
                      f"(wheel={result.wheel.name}, sdist={result.sdist.name})")
                return 0
            print("PUBLISH GATE: FAIL — forbidden content in the built artifact:", file=sys.stderr)
            for prob in result.problems:
                print(f"  ✗ {prob}", file=sys.stderr)
            return 1

    if args.cmd == "publish":
        result = publish(args.staging_root, args.target, check=args.check)
        if result.ok:
            where = "(dry run)" if args.check or args.target is None else f"→ {args.target}"
            print(f"PUBLISH GATE: PASS — public/ boundary clean {where}")
            return 0
        print("PUBLISH GATE: FAIL — public/-only boundary violated:", file=sys.stderr)
        for prob in result.problems:
            print(f"  ✗ {prob}", file=sys.stderr)
        return 1

    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
