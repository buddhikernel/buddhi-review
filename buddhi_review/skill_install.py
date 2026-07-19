"""Provenance-safe installer for the bundled Claude Code skills.

This is the write step the :mod:`buddhi_review.skill_provenance` seam deliberately
leaves out. It copies the package's bundled ``skills/`` tree into the user's Claude
Code config, threading every file THROUGH the seam so the version stamp is applied on
write and the recorded hash is over the POST-transform bytes. A JSON *sidecar* records,
per destination file, the producing version and that post-transform hash — the memory a
later run reads to tell an untouched managed file from a user-edited one.

Design invariants (why this module is careful):

  * **The safe default never clobbers.** A destination that does not hash-match anything
    we recorded — a user edit, a foreign file, or a symlink — is a CONFLICT and is left
    exactly as-is. ``force=True`` is the ONLY way to overwrite one, and it moves the
    existing file to a ``.bak-<ts>`` sidecar first. There is no interactive prompt path:
    ``force`` is the sole signal, so a non-interactive re-sync (the upgrade path) that
    passes ``force=False`` is always safe, in a TTY or a pipe alike.
  * **Per file, not per directory.** State is decided and acted on one file at a time; a
    conflict on one file never blocks updating the safe files beside it, and a whole
    skill directory is never moved aside (that would touch conflicts).
  * **Atomic per-file writes.** Each written file goes to a temp file in the destination
    directory and is ``os.replace``-d into place, so a reader never sees a half-written
    file and a mid-run failure leaves the previous file intact.
  * **Symlinks are never written through.** A destination that is a symlink (or lives
    under a symlinked skill directory) is a CONFLICT; ``force`` replaces the *link*
    itself after backing it up, never its target.
  * **The recorded hash round-trips.** We hash ``apply_transforms(<raw bundled source>)``
    — the same bytes we write — so a freshly installed file reads back as NOOP on the
    very next run, and it stays correct when F3b registers a second transform (we always
    transform the pristine bundled source, never re-transform on-disk content).

Trust model: the sidecar is a TRUSTED local provenance store. "A file we recorded" means
"its absolute path is present in the sidecar with a matching hash" — a file whose on-disk
hash equals a recorded hash is treated as ours (UPDATE-safe, no backup). Editing the
sidecar to forge such a match requires write access to the user's own config directory,
which already permits editing the installed skills directly, so it crosses no trust
boundary; accidental corruption is safe (a bad hash or unparseable JSON reads as empty →
every existing file becomes a CONFLICT and is preserved). We deliberately do NOT sign or
otherwise authenticate the sidecar, and UPDATE does not back up (it is the ordinary
per-upgrade path; a ``.bak`` on every version bump would be noise, not safety).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from buddhi_review.skill_provenance import apply_transforms, content_hash, package_version

# ── Well-known locations ──────────────────────────────────────────────────────────

# Legacy skill directory names the current tree no longer bundles but an older manual
# install snippet may have left behind. ``open-pr`` was once called ``create-pr``; a
# stale ``create-pr`` directory is cleaned up by ``--uninstall`` (see the uninstall path
# for why plain uninstall only reports it — there is no provenance to prove it unmodified).
LEGACY_SKILL_NAMES: Tuple[str, ...] = ("create-pr",)

# The sidecar is a single global file (honouring ``XDG_CONFIG_HOME``); records are keyed
# by ABSOLUTE destination path, so two different ``CLAUDE_CONFIG_DIR`` roots never collide
# in it. Bump ``_SIDECAR_SCHEMA`` only on an incompatible layout change.
_SIDECAR_SCHEMA = 1


class SkillInstallError(RuntimeError):
    """A run-level failure that stops the whole install (e.g. the bundled source tree is
    missing). Per-file errors do NOT raise — they are recorded as ``error`` outcomes so
    the safe files in the same run still complete."""


# ── Result types (pure data; the CLI does the printing) ───────────────────────────

# Per-file actions the installer reports. INSTALL/UPDATE/NOOP/CONFLICT are the install
# verbs; REMOVED/absent-noop the uninstall verbs; ERROR a per-file failure.
INSTALL = "install"
UPDATE = "update"
NOOP = "noop"
CONFLICT = "conflict"
REMOVED = "removed"
ERROR = "error"


@dataclass(frozen=True)
class FileOutcome:
    """What happened (or, under ``--dry-run``, what would happen) to one destination."""

    skill: str
    rel: str          # path relative to the skill dir, e.g. "references/env-vars.md"
    path: Path        # absolute destination path
    action: str       # one of the verbs above
    detail: str = ""  # backup path, conflict reason, error text …


@dataclass(frozen=True)
class InstallSummary:
    target_root: Path
    sidecar_path: Path
    dry_run: bool
    uninstall: bool
    forced: bool
    files: Tuple[FileOutcome, ...] = field(default_factory=tuple)

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for f in self.files:
            out[f.action] = out.get(f.action, 0) + 1
        return out

    @property
    def had_error(self) -> bool:
        return any(f.action == ERROR for f in self.files)


# ── Path resolution ───────────────────────────────────────────────────────────────

def bundled_skills_root() -> Path:
    """The package's bundled ``skills/`` tree — works editable OR wheel-installed."""
    import buddhi_review

    return Path(buddhi_review.__file__).resolve().parent / "skills"


def target_root() -> Path:
    """Where skills install: ``$CLAUDE_CONFIG_DIR/skills`` if that env var is set, else
    ``~/.claude/skills``. Resolved ONCE per run so every sidecar key shares one root."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(override) if override else Path.home() / ".claude"
    return base / "skills"


def sidecar_path() -> Path:
    """The global provenance sidecar: ``$XDG_CONFIG_HOME/buddhi/installed-skills.json``
    if that env var is set, else ``~/.config/buddhi/installed-skills.json``."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "buddhi" / "installed-skills.json"


# ── Source discovery ──────────────────────────────────────────────────────────────

def _skill_dirs(src_root: Path) -> List[str]:
    """The bundled skill names (immediate sub-directories of the source tree), sorted."""
    if not src_root.is_dir():
        return []
    return sorted(p.name for p in src_root.iterdir() if p.is_dir())


def _skill_files(skill_src: Path) -> List[Path]:
    """Every regular file under one bundled skill dir (recursive), sorted."""
    return sorted(p for p in skill_src.rglob("*") if p.is_file())


# ── Sidecar I/O ───────────────────────────────────────────────────────────────────

def _load_sidecar(path: Path) -> Dict[str, dict]:
    """Return the ``{abs_path: {"version", "hash"}}`` records. A missing OR corrupt
    sidecar reads as empty — which is SAFE: with no record every existing file is a
    CONFLICT and is left untouched, never clobbered on a bad-parse."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    # Keep only well-formed records.
    return {
        k: v for k, v in files.items()
        if isinstance(v, dict) and isinstance(v.get("hash"), str)
    }


def _write_sidecar(path: Path, records: Dict[str, dict]) -> None:
    """Atomically persist the records. Parent dirs are created; the sidecar file itself
    is written via temp + ``os.replace`` so a crash never leaves it half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": _SIDECAR_SCHEMA, "files": records}
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".installed-skills-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        _silent_unlink(tmp)
        raise


# ── Low-level filesystem helpers ──────────────────────────────────────────────────

def _timestamp() -> str:
    """Backup suffix stamp, ``YYYYmmdd-HHMMSS``. A module-level function so tests can
    pin it; uniqueness within the same second is handled by :func:`_backup`."""
    return time.strftime("%Y%m%d-%H%M%S")


def _silent_unlink(p) -> None:
    try:
        os.unlink(p)
    except OSError:
        pass


def _hash_on_disk(dest: Path) -> Optional[str]:
    """Post-transform-comparable hash of the on-disk file, or ``None`` if it cannot be
    read as UTF-8 text (a binary/foreign file → treated as not-ours → CONFLICT)."""
    try:
        return content_hash(dest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _under_symlinked_dir(dest: Path, root: Path) -> bool:
    """True if any directory BETWEEN ``root`` and ``dest`` (the skill dir or a nested dir)
    is a symlink. ``root`` itself is not checked — relocating the whole skills root via a
    symlink is a legitimate user choice; a symlink at/under the skill level is the
    clobber-through-a-link vector we refuse."""
    cur = dest.parent
    while cur != root and root in cur.parents:
        try:
            if cur.is_symlink():
                return True
        except OSError:
            return True
        cur = cur.parent
    return False


def _atomic_write(dest: Path, text: str) -> None:
    """Write ``text`` to ``dest`` via a temp file in the same directory + ``os.replace``.

    ``newline=""`` disables newline translation so the bytes on disk are EXACTLY the
    post-transform text — the on-disk read then hashes back to the recorded hash. On any
    failure the temp file is removed and the previous ``dest`` is left intact.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".buddhi-skill-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, dest)
    except BaseException:
        _silent_unlink(tmp)
        raise


def _backup(path: Path) -> Path:
    """Move ``path`` (a file, symlink, or directory) aside to ``<name>.bak-<ts>``, adding
    a ``-N`` suffix if that name is taken this second. ``os.replace`` moves a symlink as
    the link itself (never its target)."""
    ts = _timestamp()
    bak = path.with_name(f"{path.name}.bak-{ts}")
    i = 1
    while os.path.lexists(bak):
        bak = path.with_name(f"{path.name}.bak-{ts}-{i}")
        i += 1
    os.replace(path, bak)
    return bak


def _prune_empty_dir(d: Path, stop: Path) -> None:
    """Remove ``d`` and any now-empty ancestors up to (not including) ``stop``."""
    cur = d
    while cur != stop and stop in cur.parents:
        try:
            cur.rmdir()  # only succeeds when empty
        except OSError:
            return
        cur = cur.parent


# ── The install / update path ─────────────────────────────────────────────────────

def _decide_action(dest: Path, root: Path, h_cur: str, h_rec: Optional[str]) -> str:
    """The 3-way state for one destination (see the module docstring)."""
    if dest.is_symlink() or _under_symlinked_dir(dest, root):
        return CONFLICT
    if not dest.exists():
        return INSTALL
    if dest.is_dir():
        return CONFLICT  # a directory where a file belongs — never ours to overwrite
    h_disk = _hash_on_disk(dest)
    if h_disk is None:
        return CONFLICT  # unreadable / non-text → foreign
    if h_disk == h_cur:
        return NOOP
    if h_rec is not None and h_disk == h_rec:
        return UPDATE
    return CONFLICT


def _install(
    *, src_root: Path, root: Path, sc_path: Path, records: Dict[str, dict],
    force: bool, dry_run: bool,
) -> List[FileOutcome]:
    version = package_version()
    outcomes: List[FileOutcome] = []
    changed = False

    for skill in _skill_dirs(src_root):
        skill_src = src_root / skill
        for src_file in _skill_files(skill_src):
            rel = src_file.relative_to(skill_src).as_posix()
            dest = root / skill / Path(rel)
            key = str(dest)

            try:
                raw = src_file.read_text(encoding="utf-8")
            except OSError as exc:
                outcomes.append(FileOutcome(skill, rel, dest, ERROR, f"unreadable source: {exc}"))
                continue
            written = apply_transforms(raw, ctx={"version": version})
            h_cur = content_hash(written)
            h_rec = (records.get(key) or {}).get("hash")

            action = _decide_action(dest, root, h_cur, h_rec)
            detail = ""

            # A CONFLICT caused by a symlinked ANCESTOR directory can never be resolved by
            # writing — that would follow the link and clobber files inside its target (and
            # ``--force``'s per-file backup would land INSIDE that target, not replace the
            # link). Refuse in every mode, force or not: the user removes the symlinked
            # directory and re-runs. (A symlinked destination FILE, by contrast, is safely
            # replaced below — its link is backed up and a fresh file written in its place.)
            if action == CONFLICT and _under_symlinked_dir(dest, root):
                outcomes.append(FileOutcome(
                    skill, rel, dest, CONFLICT,
                    "a parent directory is a symlink — refusing to write through it; "
                    "remove the symlinked directory and re-run"))
                continue

            if action == CONFLICT and not force:
                detail = ("symlink — left untouched" if dest.is_symlink()
                          else "user-modified or foreign — left untouched "
                               "(pass --force to overwrite, backing it up first)")
                outcomes.append(FileOutcome(skill, rel, dest, CONFLICT, detail))
                continue

            if dry_run:
                # Report the would-be action; a forced conflict is reported as UPDATE
                # (that is what --force turns it into) with a note.
                shown = UPDATE if action == CONFLICT else action
                if action == CONFLICT:
                    detail = "would overwrite (--force), backing up the existing file first"
                outcomes.append(FileOutcome(skill, rel, dest, shown, detail))
                continue

            try:
                if action == NOOP:
                    pass  # bytes on disk already match; only the record is refreshed below
                else:
                    if action == CONFLICT:  # forced: back the existing file/symlink up first
                        bak = _backup(dest)
                        detail = f"overwrote conflict; backup at {bak.name}"
                    _atomic_write(dest, written)
                new_rec = {"version": version, "hash": h_cur}
                if records.get(key) != new_rec:  # avoid rewriting an unchanged sidecar
                    records[key] = new_rec
                    changed = True
                shown = UPDATE if action == CONFLICT else action
                outcomes.append(FileOutcome(skill, rel, dest, shown, detail))
            except OSError as exc:
                outcomes.append(FileOutcome(skill, rel, dest, ERROR, str(exc)))

    if changed and not dry_run:
        _write_sidecar(sc_path, records)
    return outcomes


# ── The uninstall path ────────────────────────────────────────────────────────────

def _uninstall(
    *, src_root: Path, root: Path, sc_path: Path, records: Dict[str, dict],
    force: bool, dry_run: bool,
) -> List[FileOutcome]:
    outcomes: List[FileOutcome] = []
    changed = False
    touched_dirs: set = set()

    def skill_of(dest: Path) -> str:
        try:
            return dest.relative_to(root).parts[0]
        except (ValueError, IndexError):
            return "?"

    # 1) Our recorded files under THIS run's target root.
    for key in [k for k in records if _is_under(Path(k), root)]:
        dest = Path(key)
        skill = skill_of(dest)
        rel = _rel_within_skill(dest, root)
        rec_hash = records[key].get("hash")

        # Never unlink or back up THROUGH a symlinked ancestor directory — that would
        # follow the link and delete/move files inside its target. Left untouched in every
        # mode (even --force); the user removes the symlinked directory manually.
        if _under_symlinked_dir(dest, root):
            outcomes.append(FileOutcome(
                skill, rel, dest, CONFLICT,
                "a parent directory is a symlink — left untouched "
                "(remove the symlinked directory manually)"))
            continue

        if dest.is_symlink():
            if force and not dry_run:
                bak = _backup(dest)
                del records[key]; changed = True
                touched_dirs.add(dest.parent)
                outcomes.append(FileOutcome(skill, rel, dest, REMOVED, f"symlink backed up to {bak.name}"))
            elif force and dry_run:
                outcomes.append(FileOutcome(skill, rel, dest, REMOVED, "would back up + remove symlink"))
            else:
                outcomes.append(FileOutcome(skill, rel, dest, CONFLICT, "symlink — left (pass --force to remove)"))
            continue

        if not dest.exists():
            # Already gone — just forget it (no file to remove).
            if not dry_run:
                del records[key]; changed = True
            outcomes.append(FileOutcome(skill, rel, dest, NOOP, "already absent"))
            continue

        h_disk = _hash_on_disk(dest)
        ours_unmodified = h_disk is not None and rec_hash is not None and h_disk == rec_hash
        if ours_unmodified or force:
            if dry_run:
                detail = "would remove" + ("" if ours_unmodified else " (--force; backing up modified file first)")
                outcomes.append(FileOutcome(skill, rel, dest, REMOVED, detail))
                continue
            try:
                detail = ""
                if not ours_unmodified:  # forced removal of a modified/foreign file → back it up
                    bak = _backup(dest)
                    detail = f"modified — backed up to {bak.name}"
                else:
                    dest.unlink()
                del records[key]; changed = True
                touched_dirs.add(dest.parent)
                outcomes.append(FileOutcome(skill, rel, dest, REMOVED, detail))
            except OSError as exc:
                outcomes.append(FileOutcome(skill, rel, dest, ERROR, str(exc)))
        else:
            outcomes.append(FileOutcome(skill, rel, dest, CONFLICT,
                                        "modified or foreign — left (pass --force to remove)"))

    # 2) Stale legacy skill dirs (e.g. create-pr) left by the old manual snippet. These
    #    carry NO provenance, so plain uninstall cannot prove them unmodified and LEAVES
    #    them (reporting how to remove); --force backs the whole dir up and removes it.
    for legacy in LEGACY_SKILL_NAMES:
        ldir = root / legacy
        if not (ldir.exists() or ldir.is_symlink()):
            continue
        kind = "symlink" if ldir.is_symlink() else "directory"
        if force:
            if dry_run:
                outcomes.append(FileOutcome(legacy, "", ldir, REMOVED, f"would back up + remove legacy {kind}"))
            else:
                try:
                    bak = _backup(ldir)
                    outcomes.append(FileOutcome(legacy, "", ldir, REMOVED, f"legacy {kind} backed up to {bak.name}"))
                except OSError as exc:
                    outcomes.append(FileOutcome(legacy, "", ldir, ERROR, str(exc)))
        else:
            outcomes.append(FileOutcome(legacy, "", ldir, CONFLICT,
                                        f"legacy {kind}, no provenance — left (pass --force to remove)"))

    # 3) Prune skill dirs emptied by the removals above.
    if not dry_run:
        for d in sorted(touched_dirs, key=lambda p: len(p.parts), reverse=True):
            _prune_empty_dir(d, root)

    if changed and not dry_run:
        _write_sidecar(sc_path, records)
    return outcomes


def _is_under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _rel_within_skill(dest: Path, root: Path) -> str:
    try:
        parts = dest.relative_to(root).parts
        return Path(*parts[1:]).as_posix() if len(parts) > 1 else dest.name
    except ValueError:
        return dest.name


# ── Public entry point ────────────────────────────────────────────────────────────

def install_skills(*, force: bool = False, dry_run: bool = False,
                   uninstall: bool = False) -> InstallSummary:
    """Install (or update / uninstall) the bundled skills, provenance-safe.

    ``force``     — overwrite CONFLICT files (each backed up to ``.bak-<ts>`` first).
                    The ONLY clobber signal; without it a CONFLICT is always preserved,
                    in a TTY or a pipe alike. F3a's re-sync calls with ``force=False``.
    ``dry_run``   — compute and report every per-file action but write NOTHING (no file,
                    no ``.bak``, no sidecar change, no directory creation).
    ``uninstall`` — remove our files (ours-unmodified, or any file under ``--force``) and
                    prune their sidecar records; a stale legacy skill dir is handled too.

    Returns an :class:`InstallSummary`. Raises :class:`SkillInstallError` only on a
    run-level failure (the bundled source tree is missing); per-file errors are recorded
    as ``error`` outcomes so the rest of the run still completes.
    """
    src_root = bundled_skills_root()
    root = target_root()
    sc_path = sidecar_path()

    if not uninstall and not _skill_dirs(src_root):
        raise SkillInstallError(
            f"no bundled skills found at {src_root} — is buddhi-review installed correctly?"
        )

    records = _load_sidecar(sc_path)
    if uninstall:
        files = _uninstall(src_root=src_root, root=root, sc_path=sc_path,
                           records=records, force=force, dry_run=dry_run)
    else:
        files = _install(src_root=src_root, root=root, sc_path=sc_path,
                         records=records, force=force, dry_run=dry_run)

    return InstallSummary(
        target_root=root, sidecar_path=sc_path, dry_run=dry_run,
        uninstall=uninstall, forced=force, files=tuple(files),
    )
