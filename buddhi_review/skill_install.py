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
    exactly as-is. The one exception is *adoption*: a file byte-equal to the PRISTINE
    bundled source (the raw, unstamped bytes the pre-F2 README's ``cp -R`` snippet left
    behind) is provably unmodified, so it is restamped and recorded rather than flagged. ``force=True`` is the ONLY way to overwrite one, and it moves the
    existing file to a ``.bak-<ts>`` sidecar first. There is no interactive prompt path:
    ``force`` is the sole signal, so a non-interactive re-sync (the upgrade path) that
    passes ``force=False`` is always safe, in a TTY or a pipe alike.
  * **One verdict per file per run.** The no-longer-bundled prune only ever considers a
    destination the install loop did NOT handle. A record that names a currently bundled
    file — under any spelling: a non-canonical path, a case variant on a case-insensitive
    filesystem, a hard link — is skipped there, so no run can both keep a file and delete
    it.
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
# stale ``create-pr`` directory is reported (and, with ``--force``, backed up + removed)
# by BOTH plain install and ``--uninstall`` via :func:`_handle_legacy_dirs` — there is no
# provenance to prove one unmodified, so neither path clobbers it without ``--force``.
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
# verbs; REMOVED/absent-noop the uninstall verbs (REMOVED/NOOP also appear during a
# plain install's no-longer-bundled prune, see ``_install``); ERROR a per-file failure.
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
    base = Path(override).expanduser().resolve() if override else Path.home() / ".claude"
    return base / "skills"


def sidecar_path() -> Path:
    """The global provenance sidecar: ``$XDG_CONFIG_HOME/buddhi/installed-skills.json``
    if that env var is set, else ``~/.config/buddhi/installed-skills.json``."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser().resolve() if xdg else Path.home() / ".config"
    return base / "buddhi" / "installed-skills.json"


# ── Source discovery ──────────────────────────────────────────────────────────────

def _skill_dirs(src_root: Path) -> List[str]:
    """The bundled skill names (immediate sub-directories of the source tree), sorted.

    ``is_dir()`` follows symlinks, so a symlinked entry is excluded explicitly rather
    than trusted as a real skill directory — the bundled tree ships from a package
    archive and must never be walked through a link to content outside it."""
    if not src_root.is_dir():
        return []
    return sorted(p.name for p in src_root.iterdir() if not p.is_symlink() and p.is_dir())


def _skill_files(skill_src: Path) -> List[Path]:
    """Every regular file under one bundled skill dir (recursive), sorted.

    ``is_file()`` follows symlinks; excluded for the same provenance reason as
    :func:`_skill_dirs` above."""
    return sorted(p for p in skill_src.rglob("*") if not p.is_symlink() and p.is_file())


# ── Sidecar I/O ───────────────────────────────────────────────────────────────────

def _load_sidecar(path: Path) -> Dict[str, dict]:
    """Return the ``{abs_path: {"version", "hash"}}`` records. A missing OR corrupt
    sidecar reads as empty — which is SAFE: with no record every existing file is a
    CONFLICT and is left untouched, never clobbered on a bad-parse. A ``schema`` that
    is present but does not match ``_SIDECAR_SCHEMA`` is a future incompatible layout
    bump — read as empty too, rather than misinterpreting its records under today's
    shape.

    The existence check lives INSIDE the guard on purpose: ``Path.exists()`` itself
    raises ``PermissionError`` when the config directory lacks the search bit (a
    ``chmod 000 ~/.config/buddhi``), and an uncaught raise there would abort the whole
    install with a bare traceback and no summary. This is a pre-write read, so failing
    safe (empty → every existing file is a CONFLICT) can never clobber."""
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # RecursionError: json.loads hits the interpreter's limit on deeply nested input,
        # and it is neither an OSError nor a ValueError — without it a structurally corrupt
        # sidecar still aborts the run with the traceback this guard exists to prevent.
        return {}
    if not isinstance(data, dict):
        return {}
    schema = data.get("schema")
    if schema is not None and schema != _SIDECAR_SCHEMA:
        return {}
    files = data.get("files")
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
            fd = None  # os.fdopen took ownership; don't double-close below
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        _silent_unlink(tmp)
        raise


def _flush_sidecar(sc_path: Path, records: Dict[str, dict]) -> List[FileOutcome]:
    """Call :func:`_write_sidecar`, turning a failure (unwritable/occupied config dir)
    into a per-run ``ERROR`` outcome instead of an uncaught exception. By the time this
    runs the skill files are already written, so a sidecar failure must be reported, not
    raised — an uncaught exception here would skip the CLI's summary entirely and leave
    the user with a bare traceback and no indication their install has no provenance."""
    try:
        _write_sidecar(sc_path, records)
        return []
    except OSError as exc:
        return [FileOutcome(
            "(provenance)", sc_path.name, sc_path, ERROR,
            f"install/uninstall succeeded but the provenance sidecar failed to write "
            f"— re-run to retry recording it: {exc}")]


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
            fd = None  # os.fdopen took ownership; don't double-close below
            fh.write(text)
        os.replace(tmp, dest)
    except BaseException:
        if fd is not None:
            os.close(fd)
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


def _backup_outside_root(path: Path, root: Path) -> Path:
    """Like :func:`_backup`, but lands the backup as a sibling of ``root`` itself (e.g.
    ``~/.claude/create-pr.bak-<ts>`` rather than ``~/.claude/skills/create-pr.bak-<ts>``).
    A legacy skill DIRECTORY backed up in place with :func:`_backup` would still be a
    top-level directory containing an unmodified ``SKILL.md`` under the skills root —
    Claude Code discovers skills by scanning that root, so the stale skill would remain
    selectable under its backup name. Moving one level up removes it from that scan while
    keeping the same recoverable, collision-avoiding, no-clobber semantics as
    :func:`_backup`. Regular per-file conflict backups stay in place via :func:`_backup` —
    those are individual files beside an untouched ``SKILL.md``, not a duplicate of it."""
    ts = _timestamp()
    bak = root.parent / f"{path.name}.bak-{ts}"
    i = 1
    while os.path.lexists(bak):
        bak = root.parent / f"{path.name}.bak-{ts}-{i}"
        i += 1
    os.replace(path, bak)
    return bak


def _handle_legacy_dirs(*, root: Path, force: bool, dry_run: bool) -> List[FileOutcome]:
    """Report (or, with ``--force``, back up + remove) stale legacy skill dirs (e.g.
    ``create-pr``) left by the old manual install snippet. These carry NO provenance, so
    the safe default cannot prove one unmodified and LEAVES it (reporting how to remove
    it); ``--force`` backs the whole dir up and removes it — same never-clobber-without-
    force convention as a regular file conflict. Shared by install (so a plain upgrade
    surfaces the stale dir instead of only ``--uninstall`` ever mentioning it) and
    uninstall."""
    outcomes: List[FileOutcome] = []
    for legacy in LEGACY_SKILL_NAMES:
        ldir = root / legacy
        if not (ldir.exists() or ldir.is_symlink()):
            continue
        kind = "symlink" if ldir.is_symlink() else "directory"
        if force:
            if dry_run:
                outcomes.append(FileOutcome(legacy, "", ldir, REMOVED,
                                            f"would back up (outside skills root) + remove legacy {kind}"))
            else:
                try:
                    bak = _backup_outside_root(ldir, root)
                    outcomes.append(FileOutcome(legacy, "", ldir, REMOVED,
                                                f"legacy {kind} backed up to {bak}"))
                except OSError as exc:
                    outcomes.append(FileOutcome(legacy, "", ldir, ERROR, str(exc)))
        else:
            outcomes.append(FileOutcome(legacy, "", ldir, CONFLICT,
                                        f"legacy {kind}, no provenance — left (pass --force to remove)"))
    return outcomes


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

def _decide_action(dest: Path, root: Path, h_cur: str, h_rec: Optional[str],
                   h_raw: Optional[str] = None) -> str:
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
    # ADOPTION: the pre-F2 README told users to ``cp -R`` the bundled skills straight into
    # ~/.claude/skills, which leaves the RAW (pre-transform, unstamped) bundled bytes on
    # disk and no sidecar record at all. Such a file matches neither ``h_cur`` (stamped)
    # nor ``h_rec`` (absent), so without this branch the documented upgrade — a plain
    # ``install-skills`` — would report it as a conflict and never take it under
    # provenance unless the user found ``--force``. Byte-equality with the pristine
    # bundled source PROVES the file is unmodified, so restamping it is not a clobber:
    # only the version-stamp line changes. A legacy copy of an OLDER release whose content
    # has since changed matches nothing provable and stays a CONFLICT, by design.
    if h_raw is not None and h_disk == h_raw:
        return UPDATE
    return CONFLICT


def _install(
    *, src_root: Path, root: Path, sc_path: Path, records: Dict[str, dict],
    force: bool, dry_run: bool,
) -> List[FileOutcome]:
    version = package_version()
    outcomes: List[FileOutcome] = []
    changed = False
    # Destinations the loop below handles, in the SAME normalized form the prune derives
    # its targets in (see the prune's skip check for why raw strings are not comparable).
    current_dests: set = set()

    for skill in _skill_dirs(src_root):
        skill_src = src_root / skill
        for src_file in _skill_files(skill_src):
            rel = src_file.relative_to(skill_src).as_posix()
            dest = root / skill / Path(rel)
            key = str(dest)
            current_dests.add(os.path.normpath(key))

            try:
                raw = src_file.read_text(encoding="utf-8")
            except OSError as exc:
                outcomes.append(FileOutcome(skill, rel, dest, ERROR, f"unreadable source: {exc}"))
                continue
            written = apply_transforms(raw, ctx={"version": version})
            h_cur = content_hash(written)
            h_rec = (records.get(key) or {}).get("hash")
            # Hash of the UNtransformed bundled source — the fingerprint of a legacy
            # manual ``cp -R`` copy; see the adoption branch in :func:`_decide_action`.
            h_raw = content_hash(raw)

            action = _decide_action(dest, root, h_cur, h_rec, h_raw)
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

            bak = None
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
                if bak is None:
                    outcomes.append(FileOutcome(skill, rel, dest, ERROR, str(exc)))
                else:
                    # The backup succeeded but the replacement write then failed (e.g.
                    # ENOSPC) — restore the backed-up original so a failed forced
                    # overwrite never leaves the destination missing, matching the
                    # never-clobber-without-a-recoverable-copy guarantee.
                    try:
                        os.replace(bak, dest)
                        outcomes.append(FileOutcome(
                            skill, rel, dest, ERROR,
                            f"write failed, original restored from backup: {exc}"))
                    except OSError as restore_exc:
                        outcomes.append(FileOutcome(
                            skill, rel, dest, ERROR,
                            f"write failed AND restoring backup {bak.name} failed — "
                            f"original is at {bak.name}: {exc}; {restore_exc}"))

    # A stale legacy skill dir (e.g. create-pr) is never part of the currently bundled
    # tree, so the loop above never sees it. Report it here too — not just under
    # ``--uninstall`` — so the documented upgrade path (a plain re-run of install-skills)
    # actually surfaces it instead of leaving it installed and invocable forever.
    outcomes.extend(_handle_legacy_dirs(root=root, force=force, dry_run=dry_run))

    # A destination recorded under THIS root that the loop above never touched has a
    # source that is no longer part of the bundled tree (a file removed, or a whole
    # skill renamed/dropped upstream — the loop only ever walks what IS still bundled).
    # Without pruning these, the documented "just re-run install-skills to upgrade" flow
    # would never actually refresh what an upgrade removed upstream, unlike the old
    # ``rm -rf``/``cp -R`` snippet this command replaced. Only an ours-unmodified match
    # is pruned automatically; a modified/foreign file is left as a CONFLICT — the same
    # never-clobber default as every other path in this module — and ``--force`` removes
    # it, backed up first, exactly like a regular uninstall.
    #
    # Second half of the "never prune a live file" guard. Lexical normalization alone is
    # not enough: on a case-INSENSITIVE filesystem (APFS/HFS+ by default, and NTFS)
    # ``…/OPEN-PR/SKILL.md`` is a different STRING but the very same file, and a hard link
    # gives a live file a second name that no amount of normalizing reveals. So we also
    # match on filesystem identity. ``os.lstat`` (never ``stat``) so a record that is a
    # SYMLINK is not matched by its target's identity — a symlink keeps its own, symlink-
    # safe handling below (backed up, never written or unlinked through).
    # ``ValueError`` is caught alongside ``OSError`` in both lstat calls below: a sidecar
    # key is untrusted input and a raw ``os.lstat`` — unlike ``Path.exists()`` /
    # ``Path.is_symlink()``, which swallow both — raises ValueError on an embedded NUL and
    # UnicodeEncodeError (a ValueError) on a lone surrogate. Letting either escape would
    # abort the whole run, even ``--dry-run``, with a bare traceback and no summary.
    current_ids: set = set()
    for d in current_dests:
        try:
            st = os.lstat(d)
        except (OSError, ValueError):
            continue  # not written this run (dry-run, or a per-file error above)
        current_ids.add((st.st_dev, st.st_ino))

    touched_dirs: set = set()
    for key in list(records):
        dest = _normalized_dest_if_under(key, root)
        if dest is None:
            continue  # a different config root's record; not ours this run
        # NORMALIZE BEFORE THE SKIP. The prune acts on the NORMALIZED path, so the
        # "is this still bundled?" test has to be made in the same form: a record whose
        # key is a non-canonical spelling of a live file (``…/open-pr/./SKILL.md``, a
        # doubled slash, a ``x/../`` hop) survives a raw string compare, normalizes back
        # onto that live file, matches its recorded hash, and would be unlinked here with
        # no backup — while the loop above kept the very same file. One run, two
        # contradictory actions, and a currently-bundled skill silently deleted.
        if str(dest) in current_dests:
            continue
        try:
            st = os.lstat(dest)
        except (OSError, ValueError):
            st = None  # unreadable, absent, or an unusable key — fall through, never raise
        if st is not None and (st.st_dev, st.st_ino) in current_ids:
            continue  # a case-variant / hard-linked alias of a file we just installed
        skill = _skill_of(dest, root)
        rel = _rel_within_skill(dest, root)
        rec_hash = records[key].get("hash")

        if _under_symlinked_dir(dest, root):
            outcomes.append(FileOutcome(
                skill, rel, dest, CONFLICT,
                "no longer bundled; a parent directory is a symlink — left untouched "
                "(remove the symlinked directory manually)"))
            continue

        if dest.is_symlink():
            if force and not dry_run:
                bak = _backup(dest)
                del records[key]; changed = True
                touched_dirs.add(dest.parent)
                outcomes.append(FileOutcome(
                    skill, rel, dest, REMOVED, f"no longer bundled; symlink backed up to {bak.name}"))
            elif force and dry_run:
                outcomes.append(FileOutcome(
                    skill, rel, dest, REMOVED, "no longer bundled; would back up + remove symlink"))
            else:
                outcomes.append(FileOutcome(
                    skill, rel, dest, CONFLICT,
                    "no longer bundled; symlink — left (pass --force to remove)"))
            continue

        # Records only ever name FILES. A key naming a directory is corruption, and acting
        # on it would move a whole directory — under ``--force`` that is ``_backup``-ing a
        # live skill dir (with the user's own files in it) to a ``.bak-<ts>`` sibling that
        # Claude Code still scans. Same rule as ``_decide_action``'s "a directory where a
        # file belongs", and it keeps the module's per-file-not-per-directory invariant.
        if dest.is_dir():
            # The record can only ever have named a FILE, so a directory here proves the
            # record itself is dead (e.g. upstream renamed ``notes.md`` to ``notes/``).
            # Retire it, or the same CONFLICT line repeats on every future upgrade with
            # no way — not even ``--force`` — for the user to clear it.
            if not dry_run:
                del records[key]; changed = True
            outcomes.append(FileOutcome(
                skill, rel, dest, CONFLICT,
                "no longer bundled; the record names a directory, not a file — "
                "left untouched (stale record dropped)"))
            continue

        if not dest.exists():
            if not dry_run:
                del records[key]; changed = True
            outcomes.append(FileOutcome(skill, rel, dest, NOOP, "no longer bundled; already absent"))
            continue

        h_disk = _hash_on_disk(dest)
        ours_unmodified = h_disk is not None and rec_hash is not None and h_disk == rec_hash
        if ours_unmodified or force:
            if dry_run:
                detail = "no longer bundled; would remove" + (
                    "" if ours_unmodified else " (--force; backing up modified file first)")
                outcomes.append(FileOutcome(skill, rel, dest, REMOVED, detail))
                continue
            try:
                if ours_unmodified:
                    dest.unlink()
                    detail = "no longer bundled"
                else:
                    bak = _backup(dest)
                    detail = f"no longer bundled; modified — backed up to {bak.name}"
                del records[key]; changed = True
                touched_dirs.add(dest.parent)
                outcomes.append(FileOutcome(skill, rel, dest, REMOVED, detail))
            except OSError as exc:
                outcomes.append(FileOutcome(skill, rel, dest, ERROR, str(exc)))
        else:
            outcomes.append(FileOutcome(
                skill, rel, dest, CONFLICT,
                "no longer bundled; modified or foreign — left (pass --force to remove)"))

    if not dry_run:
        for d in sorted(touched_dirs, key=lambda p: len(p.parts), reverse=True):
            _prune_empty_dir(d, root)

    if changed and not dry_run:
        outcomes.extend(_flush_sidecar(sc_path, records))
    return outcomes


# ── The uninstall path ────────────────────────────────────────────────────────────

def _uninstall(
    *, src_root: Path, root: Path, sc_path: Path, records: Dict[str, dict],
    force: bool, dry_run: bool,
) -> List[FileOutcome]:
    outcomes: List[FileOutcome] = []
    changed = False
    touched_dirs: set = set()

    # 1) Our recorded files under THIS run's target root. Each key is normalized (but
    # never symlink-resolved) via :func:`_normalized_dest_if_under` before the
    # containment check and before deriving skill/rel — see that function's docstring
    # for why a raw, un-normalized key is not safe to trust here.
    for key in list(records):
        dest = _normalized_dest_if_under(key, root)
        if dest is None:
            continue
        skill = _skill_of(dest, root)
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

        # A record naming a directory is corruption; never move a whole directory aside
        # (see the identical guard in :func:`_install`'s prune).
        if dest.is_dir():
            if not dry_run:
                del records[key]; changed = True
            outcomes.append(FileOutcome(
                skill, rel, dest, CONFLICT,
                "the record names a directory, not a file — left untouched "
                "(stale record dropped)"))
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

    # 2) Stale legacy skill dirs (e.g. create-pr) left by the old manual snippet.
    outcomes.extend(_handle_legacy_dirs(root=root, force=force, dry_run=dry_run))

    # 3) Prune skill dirs emptied by the removals above.
    if not dry_run:
        for d in sorted(touched_dirs, key=lambda p: len(p.parts), reverse=True):
            _prune_empty_dir(d, root)

    if changed and not dry_run:
        outcomes.extend(_flush_sidecar(sc_path, records))
    return outcomes


def _rel_within_skill(dest: Path, root: Path) -> str:
    try:
        parts = dest.relative_to(root).parts
        return Path(*parts[1:]).as_posix() if len(parts) > 1 else dest.name
    except ValueError:
        return dest.name


def _skill_of(dest: Path, root: Path) -> str:
    """The top-level skill name for a destination under ``root`` (e.g. ``"open-pr"``)."""
    try:
        return dest.relative_to(root).parts[0]
    except (ValueError, IndexError):
        return "?"


def _normalized_dest_if_under(key: str, root: Path) -> Optional[Path]:
    """A sidecar record ``key`` is untrusted: a corrupted or hand-edited sidecar could
    contain ``..`` segments that pass a literal-string-parents check on the RAW path
    while the path actually resolves outside ``root`` on disk. Normalize lexically
    (``os.path.normpath`` — never ``Path.resolve()``, which would follow symlinks and
    defeat ``_under_symlinked_dir``'s later check) before the containment test, and hand
    back that SAME normalized path for every subsequent use — never the raw key.

    Containment is STRICT (``root in dest.parents``): a key equal to the skills root
    itself is not actionable. A real skill file always lives at least two components
    below the root, so nothing legitimate is lost — while a key of exactly ``root``
    would otherwise let ``--force`` back up the ENTIRE skills tree in one move.

    Returns ``None`` if the normalized path is not strictly under ``root`` (a different
    config root's record, the root itself, or a traversal attempt)."""
    dest = Path(os.path.normpath(key))
    return dest if root in dest.parents else None


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
