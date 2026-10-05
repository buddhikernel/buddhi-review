"""Config — ``~/.config/buddhi/config.yaml``.

Keys: ``plan``, ``active_reviewers``, ``auto_on_open``, ``label_gated_ci``,
``auto_merge``, ``repos``, ``notifications`` (always ``console``), ``repo``, ``cwd``.
Setup writes the top-level ``auto_merge`` and ``label_gated_ci`` as ``false`` for a
separately installed backend, which asks for an explicit per-repo choice whenever
they are present. This package never reads the top-level ``auto_merge``, and reads
the top-level ``label_gated_ci`` only as the fallback for a repo with no value of
its own. The notifier writes to the console. :func:`set_repo_keys` is the per-repo writer (deep-merge
into ``repos[<repo>]``, atomic, sibling-preserving).

The file lives at the canonical Buddhi config location. A settings file left at
the legacy ``~/.config/review-loop/config.yaml`` is merged into it the first time
:func:`config_path` resolves the location (:func:`migrate_legacy_config`), and
every read-modify-write of the file runs under ONE inter-process lock
(:func:`config_lock`).

Reviewer availability is **per-repo** — Copilot/Gemini/Codex are GitHub Apps
installed per repo and ``claude[bot]`` needs its workflow in each repo — so the
fleet + the ``auto_on_open`` facts resolve per ``owner/repo`` through the
``repos:`` map. Resolution order diverges slightly by function:

* :func:`active_reviewers` — CONFIRMED ``repos[<repo>]`` entry that **carries**
  ``active_reviewers`` (even if malformed) wins; a valid list is returned as-is,
  a malformed value falls to ``DEFAULT_REVIEWERS`` **without** consulting the
  top-level global default. When the repo has no entry, or has one that lacks
  the key, the top-level ``active_reviewers`` list is used; absent that, the
  built-in four-bot set.

* :func:`auto_on_open` — CONFIRMED ``repos[<repo>]`` entry that **carries**
  ``auto_on_open`` shadows the top-level block entirely; a valid dict is
  looked up per-bot, a malformed value falls straight to ``DEFAULT_AUTO_ON_OPEN``
  (skipping the global ``auto_on_open`` block). When no per-repo key is present,
  the top-level block is used; absent that, ``DEFAULT_AUTO_ON_OPEN``.

Passing ``repo=None`` reads the global default, so a caller that does not
specify a repo gets the global-default fleet.

Absent config → defaults + a one-line log warning, never an error (the onboarding
gate prompts setup instead of degrading silently).
"""
from __future__ import annotations

import contextlib
import errno
import os
import stat
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:  # PyYAML is a hard dep of the package; guard only so import never explodes.
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

try:  # POSIX advisory locks; absent on Windows, where only the in-process lock applies.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

DEFAULT_PLAN = "max-5x"
DEFAULT_REVIEWERS: Tuple[str, ...] = ("copilot", "gemini", "codex", "claude")
# Whether a bot posts a review automatically when a PR is opened — a fact the loop
# cannot infer, so it is config. Default: the three GitHub-App bots auto-comment;
# claude is summoned in round 1.
DEFAULT_AUTO_ON_OPEN: Dict[str, bool] = {
    "copilot": True,
    "gemini": True,
    "codex": True,
    "claude": False,
}
# Whether a "ready-for-ci" label gate guards the merge — a pre-merge CI gate the
# loop attaches + polls. Default OFF (opt-in per repo / globally); mirrors the
# reference loop's default-off ``label_gated_ci``.
DEFAULT_LABEL_GATED_CI = False


def canonical_config_path() -> Path:
    """The canonical Buddhi config location, ``~/.config/buddhi/config.yaml``.
    Deliberately NOT ``$XDG_CONFIG_HOME``-aware: every reader of this file resolves
    it from the home directory alone, so honouring XDG here would split the
    settings across two files again."""
    return Path.home() / ".config" / "buddhi" / "config.yaml"


def legacy_config_path() -> Path:
    """Where earlier releases kept the config: ``~/.config/review-loop/config.yaml``.
    Read only by :func:`migrate_legacy_config`, never by a resolver."""
    return Path.home() / ".config" / "review-loop" / "config.yaml"


def config_path() -> Path:
    """The ONE config resolver: ``BUDDHI_CONFIG`` when set (no migration — the
    override names its own file), else the canonical Buddhi config location.

    A legacy file is migrated HERE, before the path is handed out, so no read,
    write or ``.exists()`` check anywhere in the package can see the canonical
    path before the legacy settings have reached it. The migration is idempotent,
    costs two ``lstat`` calls once done, and fails open (it never raises)."""
    override = os.environ.get("BUDDHI_CONFIG")
    if override:
        return Path(override)
    path = canonical_config_path()
    migrate_legacy_config(path)
    return path


# ── The config lock ─────────────────────────────────────────────────────────────
# ONE inter-process lock serialises every read-modify-write of a config file and
# the legacy migration. ``os.replace`` already prevents a torn file, but it cannot
# stop two concurrent read-modify-writes from losing one of the two updates; the
# lock can. It is an advisory ``flock`` on a lock file BESIDE the config file, so
# the lock file's own lifetime never races the config's atomic replace. It is held
# for the length of one read-modify-write, never across an interactive prompt.
#
# The wait is bounded so a stuck holder never hangs a launch, but a waiter that
# gives up does NOT then write: the holder may be mid read-modify-write, and an
# unlocked write in that window is overwritten by the holder's stale snapshot. A
# lock file that cannot be opened is treated the same way: an open failure does not
# prove there is no holder. So
# :func:`config_lock` yields whether the caller may modify the file, every writer
# (``write_config``, :func:`set_repo_keys`, the migration) refuses on ``False``, and
# read-only callers carry on.

LOCK_TIMEOUT_S = 10.0
_LOCK_POLL_S = 0.02
# The only ``flock`` errors that mean "this file system has no inter-process lock",
# so there is no holder to race and writers go on. Every other error fails closed.
_FLOCK_UNSUPPORTED_ERRNOS = frozenset(
    getattr(errno, name) for name in ("ENOTSUP", "EOPNOTSUPP", "ENOSYS")
    if hasattr(errno, name))
_lock_guard = threading.Lock()
# lock-file path -> [in-process RLock, re-entry depth, flock fd or None, may write]
_lock_state: Dict[str, List[Any]] = {}


def config_lock_path(path: Path) -> Path:
    """The lock file for the config at ``path``: ``<path>.lock``, beside it."""
    path = Path(path)
    return path.with_name(path.name + ".lock")


def _flock_acquire(lock_file: Path, timeout: float, *,
                   display_timeout: Optional[float] = None) -> Tuple[Optional[int], bool]:
    """Open ``lock_file`` (0600) and take an exclusive ``flock`` on it, waiting up
    to ``timeout`` seconds. Returns ``(fd, may_write)``:

    * ``(fd, True)`` — the lock is held.
    * ``(None, True)`` — no inter-process lock exists here at all: no ``fcntl``
      (non-POSIX), or a file system that reports ``flock`` as unsupported (ENOTSUP,
      EOPNOTSUPP, ENOSYS). There is no holder to race, so writers behave as they
      did before the lock existed.
    * ``(None, False)`` — the lock could not be taken, so writers modify nothing.
      Three causes, each with its own stderr line: another process kept the lock past
      the timeout (a stuck process must never hang a launch, so the caller goes on,
      but a live holder may be mid read-modify-write); the lock file could not be
      opened or created; or ``flock`` failed for any other reason (e.g. ENOLCK), which
      proves nothing about holders. ``EINTR`` is retried within the same deadline. A failed open does NOT prove there is no holder — the lock
      file's permissions can change while another process still holds its ``flock``,
      and the directory can still allow the atomic replace that would clobber the
      holder's update — so it refuses rather than guesses."""
    if fcntl is None:
        return None, True
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        print(f"Warning: Could not open the config lock file {lock_file} ({exc}). The "
              f"config was not changed. Fix the permissions on that file or its folder, "
              f"then run the command again.",
              file=sys.stderr)
        return None, False
    deadline = time.monotonic() + timeout
    may_write = True
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd, True
            except OSError as exc:
                if exc.errno in _FLOCK_UNSUPPORTED_ERRNOS:
                    break  # this file system has no flock at all: no holder to race
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES,
                                     errno.EINTR):
                    # Any other failure (ENOLCK: lock resources exhausted, ...) did
                    # not take the lock, and does not prove nobody holds it, so it
                    # fails closed like a timeout. EINTR falls through to the
                    # deadline check below, so an interruption can never extend or
                    # bypass the bounded wait.
                    print(f"Warning: Could not take the config lock {lock_file} ({exc}). "
                          f"The config was not changed. Run the command again.",
                          file=sys.stderr)
                    may_write = False
                    break
            if time.monotonic() >= deadline:
                print(f"Warning: Another process has held the config lock {lock_file} for "
                      f"more than {int(timeout if display_timeout is None else display_timeout)} "
                      f"seconds. The config was not changed. Run "
                      f"the command again once that process has finished.",
                      file=sys.stderr)
                may_write = False
                break
            time.sleep(_LOCK_POLL_S)
    except BaseException:  # an interrupt while waiting must not leak the fd
        os.close(fd)
        raise
    os.close(fd)
    return None, may_write


@contextlib.contextmanager
def config_lock(path: Path, *, timeout: Optional[float] = None) -> Iterator[bool]:
    """Hold the ONE inter-process lock for the config file at ``path`` (the lock
    file is :func:`config_lock_path`). Re-entrant within a thread, so a locked
    caller may call another locked writer (``set_repo_keys`` → ``write_config``)
    without deadlocking itself; other threads of this process wait on the
    in-process lock, and other processes on the ``flock``.

    Yields whether the caller may modify the file (see :func:`_flock_acquire`):
    ``False`` when another thread or process kept the lock past the timeout, or when
    the lock file could not be opened (each reported on stderr in its own words). A
    writer then writes nothing and reports failure; a read-only caller may carry on.
    The same deadline covers both lock layers. A re-entry yields the outermost
    acquisition's answer without waiting again, so a caller that wraps several
    writers in one hold needs no check of its own: each writer refuses on ``False``.

    Always resolve ``path`` BEFORE entering: :func:`config_path` may run the
    migration, which takes this same lock."""
    key = os.path.abspath(str(config_lock_path(path)))
    with _lock_guard:
        state = _lock_state.setdefault(key, [threading.RLock(), 0, None, True])
    rlock = state[0]
    wait = LOCK_TIMEOUT_S if timeout is None else max(0.0, timeout)
    deadline = time.monotonic() + wait
    if not rlock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        print(f"Warning: Another thread has held the config lock {Path(key)} for more "
              f"than {int(wait)} seconds. The config was not changed. Run the command "
              f"again once that thread has finished.",
              file=sys.stderr)
        yield False
        return
    try:
        if state[1] == 0:
            state[2], state[3] = _flock_acquire(
                Path(key), max(0.0, deadline - time.monotonic()), display_timeout=wait)
        state[1] += 1
        try:
            yield state[3]
        finally:
            state[1] -= 1
            if state[1] == 0:
                fd, state[2] = state[2], None
                if fd is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    os.close(fd)
    finally:
        rlock.release()


# ── Legacy migration ────────────────────────────────────────────────────────────
# Earlier releases kept the config at ~/.config/review-loop/config.yaml. The first
# time the location is resolved, that file's settings are merged into the
# canonical file (a byte-for-byte copy when there is no canonical file yet), and
# only THEN is the legacy file renamed, beside itself, to
# ``config.yaml.migrated-<UTC timestamp>`` — kept, never deleted — so every later
# call is two failed ``lstat`` calls (the legacy name and the record below). Until the canonical file holds the settings
# the legacy name stays in place, so a concurrent process sees it and queues on the
# lock rather than reading a canonical file that does not have them yet.
#
# Before replacing the canonical file, a pending record preserves the legacy
# snapshot, the pre-migration baseline, and the intended canonical contents. It is
# marked completed only after the replacement lands. A pending replacement is
# retried after failure or interruption, while an edit made directly to the
# canonical file after that attempt establishes a new baseline. The completed
# record stays until the legacy name is retired; it prevents the same bytes from
# being merged repeatedly after a refused rename, while a later legacy edit merges
# only what changed. A record whose legacy file was already retired (a run stopped
# after the rename) is dropped, never used.
#
# Just before the rename the record also names the backup the legacy file is
# retired to. A process of an earlier release (it takes no lock) may rewrite the
# legacy file after it was read here, so the backup can hold a newer write that is
# merged only after the rename. Until that catch-up merge lands the record stays,
# so when it fails or the run stops, a later resolve — which no longer finds the
# legacy name — still sees the record and merges the newer write from the backup.
# The legacy name is therefore retired only once that record is written; until
# then it stays, and the move is reported and retried like a refused rename.
# It reads only the backup: a legacy file created since is never read for it or
# overwritten, and is migrated on its own afterwards.
#
# The record also keeps the baseline every conflict was judged against: the
# canonical file's timestamp before the migration wrote it, and the canonical file's
# identity just after. The migration's own write is never the user's, so while the
# canonical file is still that write, a later resolve judges "newer" against the
# same baseline; once anything else has rewritten it, against that newer write.
#
# Nothing else in either folder is touched. Messages go to stderr only (the
# ``status`` verb's stdout is JSON).

_reported: set = set()
# Canonical paths whose migration this process deferred because another process
# kept the config lock past the timeout. Every ``load_config()`` resolves the path,
# so retrying would wait the full timeout again on each one; the next process
# migrates instead.
_deferred: set = set()
# Per-repo keys whose readers treat a PRESENT null as a value (it shadows the global
# default), so in a merge a null there is a setting, not an absence.
_NULL_IS_A_VALUE_PER_REPO = ("active_reviewers", "auto_on_open", "label_gated_ci")


def _note_once(message: str) -> None:
    """Print one migration line to stderr, once per process. A problem that
    persists (an unreadable legacy file) is reported on every run, but not once
    per resolve inside the same run."""
    if message in _reported:
        return
    _reported.add(message)
    print(message, file=sys.stderr)


def _lexists(path: Path) -> bool:
    """Whether ``path`` exists (a dangling symlink counts). A missing component or
    a non-directory parent is "absent"; any other ``OSError`` (a parent without
    the search bit) propagates, because it is not evidence of absence."""
    try:
        os.lstat(str(path))
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _identity(st: os.stat_result) -> Tuple[int, int, int, int]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _stat_identity(path: Path) -> Optional[Tuple[int, int, int, int]]:
    """``path``'s identity, following a symlink as a read does; ``None`` on error."""
    try:
        return _identity(os.stat(str(path)))
    except OSError:
        return None


def _same_entry(a: Path, b: Path) -> bool:
    """One directory entry reached by two paths (a symlinked or bind-mounted
    folder on the way)."""
    try:
        return a.name == b.name and os.path.samefile(str(a.parent), str(b.parent))
    except OSError:
        return False


def _twin(legacy: Path, canonical: Path) -> str:
    """How the two paths relate: ``"distinct"``; ``"same"`` — the legacy path
    reaches the canonical ENTRY (a symlinked legacy folder, or a legacy symlink
    chain through the canonical path), so there is one file and nothing to move;
    or ``"linked"`` — the canonical path is a symlink or a hard link to the legacy
    file, so its first write would split them into a stale twin."""
    if _same_entry(legacy, canonical):
        return "same"
    hop, hops = legacy, 0
    try:
        while os.path.islink(str(hop)) and hops < 40:
            target = Path(os.readlink(str(hop)))
            hop = target if target.is_absolute() else hop.parent / target
            hops += 1
            if _same_entry(hop, canonical):
                return "same"
        if not os.path.samefile(str(legacy), str(canonical)):
            return "distinct"
        st = os.lstat(str(canonical))
    except OSError:
        return "distinct"
    return "linked" if stat.S_ISLNK(st.st_mode) or st.st_nlink > 1 else "same"


def _read_config_file(path: Path) -> Tuple[Optional[Dict[str, Any]], bytes, Optional[str],
                                           Optional[Tuple[int, int, int, int]]]:
    """Read ``path`` as a YAML mapping for the migration: ``(data, raw, None,
    identity)`` on success (an empty document is ``{}``), else ``(None, b"",
    reason, None)``. ``reason`` is always one of the approved user-facing words:
    ``PyYAML is not installed``, ``not a regular file``, ``not valid YAML``,
    ``not a YAML mapping`` or ``unreadable`` (any ``OSError``). ``identity`` (device, inode, size, mtime) is taken from the
    open file, so it describes exactly the bytes read. Opened non-blocking and
    checked to be a regular file BEFORE reading, so a FIFO at the path can never
    hang the resolver."""
    if yaml is None:
        return None, b"", "PyYAML is not installed", None
    try:
        fd: Optional[int] = os.open(str(path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        return None, b"", "unreadable", None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, b"", "not a regular file", None
        with os.fdopen(fd, "rb") as fh:
            fd = None
            raw = fh.read()
    except OSError as exc:
        return None, b"", "unreadable", None
    finally:
        if fd is not None:
            os.close(fd)
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return None, b"", "not valid YAML", None
    if data is None:
        return {}, raw, None, _identity(st)
    if not isinstance(data, dict):
        return None, b"", "not a YAML mapping", None
    return data, raw, None, _identity(st)


def _same(a: Any, b: Any) -> bool:
    """Type-strict equality: ``1`` and ``true`` are different settings (readers
    accept only a real bool), though Python's ``==`` calls them equal."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _holds_nothing(key: Any, value: Any) -> bool:
    """A top-level value every reader treats as absent: ``None``, a ``plan`` that is
    not a non-empty string, a ``repos`` block that is not a mapping, a
    ``known_repos`` that is not a list, an ``active_reviewers`` that is not a list,
    an ``auto_on_open`` that is not a mapping, a ``label_gated_ci`` that is not a
    bool. Such a value never overrides a real setting on the other side of a merge."""
    if value is None:
        return True
    if key == "plan":  # plan() and plan_profile.active_plan() fall back to the default
        return not (isinstance(value, str) and value)
    if key == "test_command":  # test_command() treats a falsy or blank command as unset
        return not (value and str(value).strip())
    if key == "repos":
        return not isinstance(value, dict)
    if key in ("known_repos", "active_reviewers"):  # active_reviewers(): non-list -> built-in fleet
        return not isinstance(value, list)
    if key == "auto_on_open":  # auto_on_open() reads only a mapping
        return not isinstance(value, dict)
    if key == "label_gated_ci":  # label_gated_ci() accepts only a real bool
        return not isinstance(value, bool)
    return False


def merge_config_files(canonical: Dict[str, Any], legacy: Dict[str, Any], *,
                       legacy_wins: bool) -> Tuple[Dict[str, Any], List[str]]:
    """Merge the legacy config into the canonical one, losing no setting that only
    one side holds. Returns ``(merged, conflicts)`` — ``conflicts`` names every
    key path whose two values differed (``plan``, ``repos.<repo>.<key>``).

    * ``repos`` merges per repo (matched case-insensitively against the canonical
      file's entries, keeping the canonical spelling) and, within a repo, per key.
    * A list-valued ``known_repos`` is the union: canonical order, then each legacy
      item the canonical list lacks.
    * Any other key held by one side only is kept; two equal values are one value
      (compared type-strictly); a value every reader treats as absent yields to the
      other side (a top-level ``None``, a ``plan`` that is not a non-empty string, a
      non-mapping ``repos``, a non-list ``known_repos`` or ``active_reviewers``, a
      non-mapping ``auto_on_open``, a non-bool ``label_gated_ci``, a non-mapping repo entry,
      and a per-repo ``None`` for a key whose reader treats null as absent).
    * A CONFLICT — the same key (a top-level value such as ``plan``,
      ``active_reviewers``, ``auto_on_open``, ``label_gated_ci`` or
      ``test_command``, or one key of one repo) with two different values — takes
      the value from the file modified more recently (``legacy_wins``), and the
      canonical value on a tie. The newer write is the best available proxy for
      what the user intended most recently; the losing legacy value survives in
      the ``.migrated-<ts>`` backup.

    Neither input is mutated."""
    merged: Dict[str, Any] = dict(canonical)
    conflicts: List[str] = []
    for key, lval in legacy.items():
        if key not in merged:
            merged[key] = lval
            continue
        cval = merged[key]
        if _holds_nothing(key, cval):
            if not _holds_nothing(key, lval):
                merged[key] = lval
            continue
        if _holds_nothing(key, lval):
            continue
        if key == "repos":  # never short-circuited: the order of the entries matters
            merged[key] = _merge_repos(cval, lval, legacy_wins=legacy_wins,
                                       conflicts=conflicts)
        elif _same(cval, lval):
            continue
        elif key == "known_repos":
            union = list(cval)
            for item in lval:
                if not any(_same(item, have) for have in union):
                    union.append(item)
            merged[key] = union
        else:
            conflicts.append(str(key))
            if legacy_wins:
                merged[key] = lval
    return merged, conflicts


def _merge_repos(canonical: Dict[Any, Any], legacy: Dict[Any, Any], *, legacy_wins: bool,
                 conflicts: List[str]) -> Dict[Any, Any]:
    """The per-repo, per-key half of :func:`merge_config_files`. Readers use the
    FIRST mapping entry that matches a repo case-insensitively, so a legacy entry
    merges into that canonical entry; a legacy entry that is not a mapping, or a
    second, differently-cased legacy entry for a repo already merged, is kept
    under its own spelling exactly as the legacy file held it — appended, so it
    never lands ahead of the entry readers use."""
    out: Dict[Any, Any] = dict(canonical)
    by_norm: Dict[str, Any] = {}
    for ckey, centry in canonical.items():
        norm = norm_repo(ckey)
        if norm is not None and isinstance(centry, dict):
            by_norm.setdefault(norm, ckey)
    merged_into: set = set()
    for lkey, lentry in legacy.items():
        norm = norm_repo(lkey)
        ckey = by_norm.get(norm) if norm is not None and isinstance(lentry, dict) else None
        if ckey is None or ckey in merged_into:
            if lkey not in out:
                out[lkey] = lentry
            elif isinstance(lentry, dict) and not isinstance(out[lkey], dict):
                out.pop(lkey)
                out[lkey] = lentry
            continue
        merged_into.add(ckey)
        centry = out[ckey]
        if _same(centry, lentry):
            continue
        entry = dict(centry)
        for k, v in lentry.items():
            if k not in entry or _repo_value_is_unset(k, entry[k]):
                if not _repo_value_is_unset(k, v) or k not in entry:
                    entry[k] = v
                continue
            if _repo_value_is_unset(k, v) or _same(entry[k], v):
                continue
            conflicts.append(f"repos.{ckey}.{k}")
            if legacy_wins:
                entry[k] = v
        out[ckey] = entry
    return out


def _repo_value_is_unset(key: Any, value: Any) -> bool:
    """A per-repo value its reader treats as absent: ``None`` (except for the keys
    whose readers let a present null shadow the global), and a blank
    ``test_command`` (blank, or falsy such as ``false`` / ``0`` / ``[]``, as
    :func:`test_command` reads it)."""
    if key == "test_command":
        return not (value and str(value).strip())
    return value is None and key not in _NULL_IS_A_VALUE_PER_REPO


def _first_entries(repos: Dict[Any, Any]) -> Dict[str, Dict[str, Any]]:
    """Each repo's entry as readers find it (:func:`repo_entry`): the FIRST mapping
    whose key matches the repo case-insensitively, keyed by the normalised name."""
    first: Dict[str, Dict[str, Any]] = {}
    for rkey, rval in repos.items():
        norm = norm_repo(rkey)
        if norm is not None and isinstance(rval, dict):
            first.setdefault(norm, rval)
    return first


def _changed_since(base: Dict[str, Any], now: Dict[str, Any]) -> Dict[str, Any]:
    """What an edit changed or added, going from ``base`` to ``now`` — per top-level
    key, and per repo per key under ``repos``. A removal is not a change to carry
    over: the canonical file keeps what it has.

    A repo is compared as readers find it in each file — its FIRST mapping entry,
    matched case-insensitively — so an edit that only changes the case of a repo's
    key changes nothing. A later entry for the same repo is never carried over: no
    reader uses it, and alone in the change it would be merged into the entry
    readers do use."""
    out: Dict[str, Any] = {}
    for key, value in now.items():
        old = base.get(key)
        if key == "repos" and isinstance(value, dict) and isinstance(old, dict):
            was = _first_entries(old)
            seen: set = set()
            repos: Dict[Any, Any] = {}
            for rkey, rval in value.items():
                norm = norm_repo(rkey) if isinstance(rval, dict) else None
                if norm is not None:
                    if norm in seen:
                        continue  # behind an earlier entry for the same repo
                    seen.add(norm)
                rold = was.get(norm) if norm is not None else old.get(rkey)
                if isinstance(rval, dict) and isinstance(rold, dict):
                    diff = {k: v for k, v in rval.items() if k not in rold or not _same(rold[k], v)}
                    if diff:
                        repos[rkey] = diff
                elif rkey not in old or not _same(rold, rval):
                    repos[rkey] = rval
            if repos:
                out[key] = repos
        elif key not in base or not _same(old, value):
            out[key] = value
    return out


def _backup_name(legacy: Path) -> Path:
    """``config.yaml.migrated-<UTC timestamp>`` beside ``legacy`` — never an
    existing name, so no earlier backup is ever overwritten."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"{legacy.name}.migrated-{stamp}"
    candidate, n = legacy.with_name(base), 1
    while _lexists(candidate):
        candidate, n = legacy.with_name(f"{base}-{n}"), n + 1
    return candidate


def _write_bytes_atomic(path: Path, raw: bytes) -> bool:
    """Write ``raw`` to ``path`` atomically (temp file + ``os.replace``) at 0600."""
    try:
        _replace_with_bytes(path, raw)
        return True
    except OSError:
        return False


def _replace_with_bytes(path: Path, raw: bytes) -> None:
    """:func:`_write_bytes_atomic`, raising the ``OSError`` instead. A failure
    leaves the file at ``path`` as it was."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fd = None
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# The merge record — see the section comment above.
def _record_path(canonical: Path) -> Path:
    return canonical.with_name(canonical.name + ".legacy-merged")


def _record_exists(canonical: Path) -> bool:
    """Whether a merge record may sit beside ``canonical``. A probe error counts as
    present so the locked reader reports it instead of discarding recovery state."""
    try:
        return _lexists(_record_path(canonical))
    except OSError:
        return True


_UNREADABLE_RECORD = object()


def _read_record(canonical: Path) -> Any:
    path = _record_path(canonical)
    data, _, why, _ = _read_config_file(path)
    if data is None and why == "unreadable":
        try:
            if _lexists(path):
                return _UNREADABLE_RECORD
        except OSError:
            return _UNREADABLE_RECORD
    if not data:
        return None
    ident, conflicts, merged_legacy = (data.get("identity"), data.get("conflicts"),
                                       data.get("legacy"))
    if not (isinstance(ident, list) and isinstance(conflicts, list)
            and isinstance(merged_legacy, dict)):
        return None
    backup = data.get("backup")
    state = data.get("state", "completed")
    if state not in ("pending", "completed"):
        return None
    mtime, written = data.get("canonical_before"), data.get("canonical_written")
    pending_source = data.get("canonical_pending")
    if not (isinstance(pending_source, list) and len(pending_source) == 4
            and all(type(n) is int for n in pending_source)):
        pending_source = None
    pending_target = data.get("canonical_target")
    if not isinstance(pending_target, dict):
        pending_target = None
    baseline = None
    if "canonical_before" in data and (mtime is None or type(mtime) is int):
        if state == "pending":
            baseline = (mtime, None)
        elif isinstance(written, list) and len(written) == 4 \
                and all(type(n) is int for n in written):
            baseline = (mtime, tuple(written))
    return {"identity": tuple(ident), "conflicts": [str(c) for c in conflicts],
            "legacy": merged_legacy, "backup": backup if isinstance(backup, str) else None,
            "baseline": baseline, "state": state,
            "pending_source": tuple(pending_source) if pending_source is not None else None,
            "pending_target": pending_target}


def _write_record(canonical: Path, ident: Tuple[int, ...], conflicts: List[str],
                  merged_legacy: Dict[str, Any], before: Dict[str, Optional[int]],
                  backup: Optional[str] = None, *, state: str = "completed",
                  target: Optional[Dict[str, Any]] = None) -> None:
    """Write pending or completed recovery state atomically. Raises ``OSError``
    when it cannot be written; any record already there is then left as it was."""
    body: Dict[str, Any] = {"identity": list(ident), "conflicts": list(conflicts),
                            "legacy": merged_legacy, "state": state}
    if backup is not None:
        body["backup"] = backup
    if state == "pending":
        source = _stat_identity(canonical)
        if source is not None:
            body["canonical_pending"] = list(source)
        if target is not None:
            body["canonical_target"] = target
    # The baseline the merge was judged against, and the canonical file this run
    # left: a later resolve reuses the baseline only while the file is still that.
    written = (_stat_identity(canonical)
               if state == "completed" and "mtime" in before else None)
    if "mtime" in before:
        body["canonical_before"] = before["mtime"]
    if written is not None:
        body["canonical_written"] = list(written)
    _replace_with_bytes(_record_path(canonical),
                        yaml.safe_dump(body, sort_keys=False).encode("utf-8"))


def _drop_record(canonical: Path) -> None:
    try:
        os.unlink(str(_record_path(canonical)))
    except OSError:
        pass


def _pending_backup(legacy: Path, record: Optional[Dict[str, Any]]) -> Optional[Path]:
    """The backup a record names, unless it is known to be gone: the legacy file was
    retired to it and a write that landed in it may not have been merged yet. Only
    a ``.migrated-`` name beside ``legacy`` is accepted. A backup whose probe fails
    (an I/O error, say) is returned too: that is no evidence it is gone, so
    :func:`_catch_up` reports it as unreadable and the record is kept for a retry."""
    name = record.get("backup") if record is not None else None
    if not name or os.sep in name or (os.altsep and os.altsep in name) \
            or not name.startswith(f"{legacy.name}.migrated-"):
        return None
    path = legacy.with_name(name)
    try:
        return path if _lexists(path) else None
    except OSError:
        return path


def _restrict_backup(backup: Path) -> None:
    """Make ``backup`` 0600, as the canonical file is: it holds the same settings.
    Only a regular file with no other name is changed — never a symlink's target or
    a hard link's other names."""
    try:
        st = os.lstat(str(backup))
        if stat.S_ISREG(st.st_mode) and st.st_nlink == 1:  # never a link's other name
            os.chmod(str(backup), 0o600)
    except OSError:
        pass


def _catch_up(canonical: Path, legacy: Path, backup: Path, ident: Tuple[int, ...],
              base: Dict[str, Any], before: Dict[str, Optional[int]],
              conflicts: List[str]) -> Any:
    """Merge what a write by an earlier release changed in the legacy file after it
    was read (``ident``, ``base``) and before it was renamed to ``backup``. Nothing
    else writes to the backup name. Returns ``None`` when the backup still holds the
    file that was read, or is gone (nothing to catch up); a reported failure outcome
    string when it cannot be read (both callers then keep the record, so a later
    resolve retries once the backup is readable); else what
    :func:`_merge_into_canonical` returns."""
    try:
        if _identity(os.stat(str(backup))) == tuple(ident):
            return None
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return _unreadable(backup, canonical, "unreadable")
    later, later_raw, why, later_ident = _read_config_file(backup)
    if later is None or later_ident is None:
        if not _lexists(backup):
            return None
        return _unreadable(backup, canonical, why or "unreadable")
    return _merge_into_canonical(canonical, legacy, later, later_raw, later_ident,
                                 before, base, backup=backup.name,
                                 pending_record=(ident, conflicts, base))


def _report_moved(legacy: Path, canonical: Path, backup: Path, conflicts: List[str]) -> None:
    verb = "were merged into" if conflicts else "are now in"
    _note_once(f"Config moved: Settings from {legacy} {verb} {canonical}. The old file was "
               f"kept as {backup}. {_conflict_sentence(conflicts)}")


def _already_retired(legacy: Path, ident: Tuple[int, ...]) -> bool:
    """True when a ``.migrated-`` backup beside ``legacy`` is the very file the
    record describes (a rename keeps the device, inode, size and mtime)."""
    prefix = f"{legacy.name}.migrated-"
    try:
        names = [n for n in os.listdir(str(legacy.parent)) if n.startswith(prefix)]
        return any(_identity(os.stat(str(legacy.parent / n))) == ident for n in names)
    except OSError:
        return False


def _conflict_summary(conflicts: List[str]) -> str:
    shown = ", ".join(conflicts[:5])
    more = len(conflicts) - 5
    return shown + (f" and {more} more" if more > 0 else "")


def _conflict_sentence(conflicts: List[str]) -> str:
    if not conflicts:
        return "No settings conflicted."
    return (f"{len(conflicts)} setting(s) conflicted. The value from the more recently "
            f"modified file was kept for: {_conflict_summary(conflicts)}.")


def migrate_legacy_config(canonical: Optional[Path] = None, *,
                          legacy: Optional[Path] = None) -> str:
    """Move the legacy config's settings into the canonical file. Returns the
    outcome: ``"absent"`` (nothing to migrate — the cheap common case),
    ``"migrated"``, ``"unreadable"`` (legacy left untouched), ``"deferred"``
    (another process kept the config lock past the timeout: both files are left
    untouched, and this process does not try again) or ``"error"``.

    Never raises an ``Exception`` and never blocks the caller beyond the lock's
    bounded wait: any failure is ONE stderr line and the caller carries on with the
    canonical path. Runs under :func:`config_lock` so it cannot interleave with a
    concurrent migration or writer, and never runs without it."""
    canonical = Path(canonical) if canonical is not None else canonical_config_path()
    legacy = Path(legacy) if legacy is not None else legacy_config_path()
    try:
        try:
            legacy_present = _lexists(legacy)
        except OSError as exc:
            return _unreadable(legacy, canonical, "unreadable")
        if not legacy_present and not _record_exists(canonical):
            return "absent"  # done: no legacy name, and no unfinished catch-up recorded
        if legacy_present and _twin(legacy, canonical) == "same":
            return "absent"  # one directory entry: there is nothing to move
        deferred_key = os.path.abspath(str(canonical))
        if deferred_key in _deferred:
            return "deferred"
        if legacy_present:
            # An unreadable legacy file is reported without taking the lock or
            # creating anything; a readable one is read again under the lock before
            # it is merged.
            data, _, why, _ = _read_config_file(legacy)
            if data is None:
                if _lexists(legacy):
                    return _unreadable(legacy, canonical, why or "unreadable")
                if not _record_exists(canonical):
                    return "absent"  # a concurrent process moved it between the two looks
        with config_lock(canonical) as may_write:
            if not may_write:
                # The holder may be mid read-modify-write of the canonical file: a
                # merge written now would be overwritten by its stale snapshot. The
                # lock's own stderr line already reported the wait.
                _deferred.add(deferred_key)
                return "deferred"
            return _migrate_locked(canonical, legacy)
    except Exception as exc:  # fail open: a migration error never blocks a launch
        _note_once(f"Warning: Could not move settings from {legacy} to {canonical} ({exc}). "
                   f"Settings are read from {canonical}.")
        return "error"


def _unreadable(legacy: Path, canonical: Path, reason: str) -> str:
    _note_once(f"Warning: Could not read the old config file {legacy} ({reason}). It was "
               f"left unchanged. Settings are read from {canonical}.")
    return "unreadable"


def _write_failed(legacy: Path, canonical: Path) -> str:
    _note_once(f"Warning: Could not move settings from {legacy} to {canonical} because "
               f"writing the file failed. Both files were left unchanged. Settings are "
               f"read from {canonical}.")
    return "error"


def _write_failed_after_merge(legacy: Path, canonical: Path) -> str:
    _note_once(f"Warning: Settings from {legacy} were merged into {canonical}, but the "
               f"recovery record could not be completed. The old file was left "
               f"unchanged and the merge will be retried.")
    return "error"


def _migrate_locked(canonical: Path, legacy: Path) -> str:
    # The canonical file's timestamp before this migration writes it: "newer" is
    # always judged against the file as the user left it. None = it holds nothing of
    # its own, so the legacy side wins every conflict.
    before: Dict[str, Optional[int]] = {}
    record = _read_record(canonical) if _lexists(canonical) else None
    if record is _UNREADABLE_RECORD:
        return _unreadable(_record_path(canonical), canonical, "unreadable")
    if record is not None and record["baseline"] is not None:
        # An earlier run that did not finish wrote the canonical file. While the file
        # is still that write, its baseline stands: judged against the migration's own
        # write, a legacy change made before it would lose to the value it replaced.
        mtime, written = record["baseline"]
        reuse = _stat_identity(canonical) == written
        if record["state"] == "pending":
            current_identity = _stat_identity(canonical)
            reuse = current_identity == record["pending_source"]
            if not reuse and record["pending_target"] is not None:
                current, _, _, _ = _read_config_file(canonical)
                reuse = current is not None and _same(current, record["pending_target"])
        if reuse:
            before["mtime"] = mtime
    caught_up = False
    pending = _pending_backup(legacy, record)
    if pending is not None and record is not None:
        # An earlier run retired the legacy file to this backup but did not merge a
        # write that landed in it before the rename: merge it now, from the backup.
        # That run may also have stopped before it made the backup 0600.
        _restrict_backup(pending)
        more = _catch_up(canonical, legacy, pending, record["identity"], record["legacy"],
                         before, record["conflicts"])
        if isinstance(more, str):
            return more  # reported; the record still names the backup, so it is retried
        _drop_record(canonical)
        record = None
        if more is not None:
            _report_moved(legacy, canonical, pending, more)
            caught_up = True
    done = "migrated" if caught_up else "absent"
    # Re-check under the lock: a concurrent process may have finished the move.
    if not _lexists(legacy):
        _drop_record(canonical)  # no legacy file is left for a record to describe
        return done
    twin = _twin(legacy, canonical)
    if twin == "same":
        return done
    legacy_data, raw, why, ident = _read_config_file(legacy)
    if legacy_data is None or ident is None:
        if not _lexists(legacy):
            return done
        return _unreadable(legacy, canonical, why or "unreadable")
    if record is not None and _already_retired(legacy, record["identity"]):
        _drop_record(canonical)  # left by a run stopped after the rename: it is done
        record = None
    if twin == "linked":
        # The canonical path links to the legacy file: give it a regular file of its
        # own (same bytes) before the legacy name is retired below.
        before["mtime"] = None
        try:
            _write_record(canonical, ident, [], legacy_data, before, state="pending",
                          target=legacy_data)
        except OSError:
            return _write_failed(legacy, canonical)
        if not _write_bytes_atomic(canonical, raw):
            return _write_failed(legacy, canonical)
        try:
            _write_record(canonical, ident, [], legacy_data, before)
        except OSError:
            return _write_failed_after_merge(legacy, canonical)
        conflicts: List[str] = []
    elif record is not None and record["state"] == "completed" \
            and record["identity"] == ident:
        conflicts = record["conflicts"]  # merged already, by a run that did not finish
    else:
        # A pending record does not vouch for the canonical replacement: retry the
        # whole snapshot even when the legacy file itself has not changed.
        base = (record["legacy"] if record is not None
                and record["state"] == "completed" else None)
        merged = _merge_into_canonical(canonical, legacy, legacy_data, raw, ident, before, base)
        if isinstance(merged, str):
            return merged
        conflicts = merged
    # The canonical file holds the settings: retire the legacy name. The record names
    # the backup first, so a newer write the rename carries into it is retried by a
    # later resolve until the catch-up below has merged it. Only that record leads a
    # resolve to the backup once the legacy name is gone, so when it cannot be
    # written the name is NOT retired: the legacy file keeps any newer write, the
    # record already there (and its baseline) is left as it was, and a later resolve
    # tries again — reported exactly as a refused rename.
    backup = _backup_name(legacy)
    try:
        _write_record(canonical, ident, conflicts, legacy_data, before, backup=backup.name)
        os.rename(str(legacy), str(backup))
    except OSError as exc:
        read_back = _read_record(canonical)
        recorded = (read_back is not _UNREADABLE_RECORD
                    and (read_back or {}).get("identity") == ident)
        _note_once(f"Warning: Settings from {legacy} were merged into {canonical}, but the "
                   f"old file could not be renamed ({exc.strerror or exc}). "
                   f"{_conflict_sentence(conflicts)} "
                   + ("It will not be merged again unless it changes." if recorded
                      else "It will be merged again on the next run."))
        return "error"
    # 0600 before the catch-up, so a catch-up that fails or is stopped never leaves
    # the backup readable by others.
    _restrict_backup(backup)
    # A process of an earlier release (it takes no lock) may have rewritten the legacy
    # file after it was read here, so the moved file holds that newer write.
    more = _catch_up(canonical, legacy, backup, ident, legacy_data, before, conflicts)
    if isinstance(more, str):
        return more  # reported; the record still names the backup, so it is retried
    if more:
        conflicts = conflicts + [c for c in more if c not in conflicts]
    _drop_record(canonical)
    _report_moved(legacy, canonical, backup, conflicts)
    return "migrated"


def _merge_into_canonical(canonical: Path, legacy: Path, legacy_data: Dict[str, Any],
                          raw: bytes, ident: Tuple[int, int, int, int],
                          before: Dict[str, Optional[int]],
                          base: Optional[Dict[str, Any]], *,
                          backup: Optional[str] = None,
                          pending_record: Optional[Tuple[Tuple[int, ...], List[str],
                                                         Dict[str, Any]]] = None) -> Any:
    """Bring the legacy settings into ``canonical``, then record the merge.
    ``base`` — the legacy settings an earlier, unfinished run already merged — limits
    the merge to what changed since. Returns the conflict list, or a failure
    outcome string after reporting it (nothing then moved)."""
    if not _lexists(canonical):
        # Nothing to merge with: the canonical file becomes a byte-for-byte copy of
        # the legacy one, comments included (and an empty file stays empty).
        before.setdefault("mtime", None)
        try:
            _write_record(canonical, ident, [], legacy_data, before, backup=backup,
                          state="pending", target=legacy_data)
        except OSError:
            return _write_failed(legacy, canonical)
        if not _write_bytes_atomic(canonical, raw):
            return _write_failed(legacy, canonical)
        try:
            _write_record(canonical, ident, [], legacy_data, before, backup=backup)
        except OSError:
            return _write_failed_after_merge(legacy, canonical)
        return []
    canonical_data, _, why, cident = _read_config_file(canonical)
    if canonical_data is None or cident is None:
        _note_once(f"Warning: Could not move settings from {legacy} to {canonical} because "
                   f"{canonical.name} could not be read: {why}. Both files were left "
                   f"unchanged. Settings are read from {canonical}.")
        return "error"
    # Newer modification time wins a conflict; a tie goes to the canonical file.
    canonical_mtime = before.setdefault("mtime", cident[3])
    legacy_wins = canonical_mtime is None or ident[3] > canonical_mtime
    incoming = _changed_since(base, legacy_data) if base is not None else legacy_data
    merged, conflicts = merge_config_files(canonical_data, incoming, legacy_wins=legacy_wins)
    if not _same(merged, canonical_data):
        # The pending record preserves the user's pre-migration timestamp before
        # replacing the canonical file. If the replacement or the completed-record
        # write is interrupted, the next run retries against that same baseline.
        pending_ident, pending_conflicts, pending_legacy = (
            pending_record if pending_record is not None
            else (ident, conflicts, legacy_data))
        try:
            _write_record(canonical, pending_ident, pending_conflicts, pending_legacy,
                          before, backup=backup, state="pending", target=merged)
        except OSError:
            return _write_failed(legacy, canonical)
        # Deferred import: wizard imports config at module load (config is the
        # lower layer), exactly as set_repo_keys does.
        from buddhi_review.wizard import write_config
        if not write_config(merged, canonical):
            return _write_failed(legacy, canonical)
    try:
        _write_record(canonical, ident, conflicts, legacy_data, before, backup=backup)
    except OSError:
        return _write_failed_after_merge(legacy, canonical)
    return conflicts


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    p = path or config_path()
    if yaml is None:
        return {}
    try:
        present = p.exists()
    except OSError as e:  # e.g. the config folder lacks the search bit
        print(f"Warning: Could not load or parse config file {p}: {e}", file=sys.stderr)
        return {}
    if not present:
        print(f"Warning: Config file not found at {p}. Using default settings.", file=sys.stderr)
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        print(f"Warning: Could not load or parse config file {p}: {e}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def load_config_checked(path: Optional[Path] = None) -> Tuple[Dict[str, Any], bool]:
    """Like :func:`load_config`, but also reports whether the read genuinely
    succeeded. ``load_config`` folds "absent", "corrupt", and "malformed" into the
    same ``{}`` so most callers never have to handle an exception; a caller that
    must never mistake "config unreadable" for "config says no" (a fail-closed
    opt-in check) uses this instead. Today the one caller,
    ``wizard._attach_ready_for_ci``, only reaches the unreadable-vs-absent branch
    when its own ``opted_in`` parameter is ``None`` — a state no current
    production path leaves it in — so this distinction is defense-in-depth for
    that caller's direct/future use, not an active guarantee on any user path yet.

    Returns ``(cfg, ok)``. ``ok`` is False only when ``path`` EXISTS (or cannot even
    be probed) but could not be read or parsed into a dict (PyYAML missing, an ``OSError``/``UnicodeDecodeError``,
    a YAML syntax error, or a document that isn't a mapping) — a genuinely absent
    file is ``({}, True)``, since there is nothing to fail to read."""
    p = path or config_path()
    try:
        if not p.exists():
            return {}, True
    except OSError:  # it cannot even be probed, so it cannot be read
        return {}, False
    if yaml is None:
        return {}, False
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}, False
    # No ``or {}`` normalisation before the isinstance check: it would rewrite every
    # FALSY non-mapping document (``[]``, ``false``, ``0``) to ``{}`` and report it as
    # readable, which is the "garbage config read as 'says no'" outcome this helper
    # exists to prevent. ``None`` (empty file / explicit ``null``) is the one legitimately
    # absent-content case, so it alone maps to ``({}, True)``.
    if data is None:
        return {}, True
    return (data, True) if isinstance(data, dict) else ({}, False)


def plan(cfg: Dict[str, Any]) -> str:
    v = cfg.get("plan")
    return v if isinstance(v, str) and v else DEFAULT_PLAN


# ── Per-repo reviewer resolution (the ``repos:`` map) ───────────────────────────

def norm_repo(repo: Optional[str]) -> Optional[str]:
    """Normalise a repo identifier to the lowercased ``owner/repo`` key used in
    the ``repos:`` map, or ``None``. GitHub repo slugs are case-insensitive, so
    lowercasing lets a ``gh``-inferred ``Owner/Repo`` match a stored
    ``owner/repo`` key."""
    if not repo:
        return None
    key = str(repo).strip().lower()
    return key or None


def repos_map(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The ``repos:`` block — a map keyed ``owner/repo`` of per-repo reviewer
    config ``{active_reviewers, auto_on_open}``. ``{}`` when absent or not a map."""
    v = cfg.get("repos")
    return v if isinstance(v, dict) else {}


def repo_entry(cfg: Dict[str, Any], repo: Optional[str]) -> Optional[Dict[str, Any]]:
    """The ``repos[<repo>]`` mapping for ``repo`` (case-insensitive), or ``None``
    when the repo has no confirmed entry. A non-dict entry is ignored. The mere
    PRESENCE of the entry is the per-repo confirmation marker — an explicit empty
    fleet still counts as confirmed."""
    key = norm_repo(repo)
    if key is None:
        return None
    for k, v in repos_map(cfg).items():
        if str(k).strip().lower() == key and isinstance(v, dict):
            return v
    return None


def has_global_default(cfg: Dict[str, Any]) -> bool:
    """True when a global-default reviewer fleet is set — a top-level
    ``active_reviewers`` list. An unconfirmed repo may fall back to the global
    default only when this holds; without it the loop's gate fails closed."""
    return isinstance(cfg.get("active_reviewers"), list)


def active_reviewers(cfg: Dict[str, Any], repo: Optional[str] = None) -> Tuple[str, ...]:
    """The enabled reviewer fleet. A CONFIRMED repo's per-repo ``active_reviewers``
    (a ``repos[<repo>]`` entry that carries the key) wins; otherwise the top-level
    ``active_reviewers`` (the global default); otherwise the built-in four-bot set.
    An explicit empty list (the user confirmed "no bots for this repo") is honoured
    as-is. ``repo=None`` reads the global default."""
    entry = repo_entry(cfg, repo)
    if entry is not None and "active_reviewers" in entry:
        v = entry.get("active_reviewers")
    else:
        v = cfg.get("active_reviewers")
    if isinstance(v, list):
        return tuple(str(x) for x in v)
    return DEFAULT_REVIEWERS


def auto_on_open(cfg: Dict[str, Any], bot: str, repo: Optional[str] = None) -> bool:
    """Whether ``bot`` posts a review automatically when a PR is opened. A
    CONFIRMED repo's per-repo ``auto_on_open`` block (a ``repos[<repo>]`` entry
    that carries the key) wins; otherwise the top-level ``auto_on_open`` block;
    otherwise ``DEFAULT_AUTO_ON_OPEN`` (claude → False, the GitHub-App reviewers →
    True). The presence of a per-repo ``auto_on_open`` key shadows the top-level
    block even when malformed (then every bot falls to the per-bot default).
    ``repo=None`` reads the top-level block."""
    entry = repo_entry(cfg, repo)
    if entry is not None and "auto_on_open" in entry:
        m = entry.get("auto_on_open")
    else:
        m = cfg.get("auto_on_open")
    if isinstance(m, dict) and bot in m:
        return bool(m[bot])
    return DEFAULT_AUTO_ON_OPEN.get(bot, True)


def label_gated_ci(cfg: Dict[str, Any], repo: Optional[str] = None) -> bool:
    """Whether a "ready-for-ci" label gate guards the merge for ``repo``. A
    CONFIRMED repo's per-repo ``label_gated_ci`` (a ``repos[<repo>]`` entry that
    carries the key) wins; otherwise the top-level global ``label_gated_ci``;
    otherwise ``DEFAULT_LABEL_GATED_CI`` (off). Mirrors the
    :func:`active_reviewers` resolution order. The presence of a per-repo
    ``label_gated_ci`` key shadows the global flag even when malformed (a non-bool
    value falls to the default, never the global). ``repo=None`` reads the global
    flag."""
    entry = repo_entry(cfg, repo)
    if entry is not None and "label_gated_ci" in entry:
        v = entry.get("label_gated_ci")
    else:
        v = cfg.get("label_gated_ci")
    return v if isinstance(v, bool) else DEFAULT_LABEL_GATED_CI


def establish_global_defaults(cfg: Dict[str, Any], *, auto_merge: Optional[bool] = None,
                              label_gated_ci: Optional[bool] = None) -> Dict[str, Any]:
    """Return a copy of ``cfg`` in which a top-level ``auto_merge`` /
    ``label_gated_ci`` EXISTS for each setting the setup wizard asked (its
    argument is not ``None``). This package does not act on them: a separately
    installed backend checks that they are present and then asks for an explicit
    per-repo choice. Here, :func:`auto_merge` never reads the top-level key, and
    :func:`label_gated_ci` reads it only as its fallback, where ``False`` is the same
    as the built-in default. The value written is always ``False``
    — the fail-safe — never the bound repo's own answer, so nothing a repo did not
    choose for itself can be inherited (the bound repo keeps its answer under
    ``repos[<repo>]``). A top-level value already present, set by hand, is left as
    it is: the global exists, and repos keep inheriting it. Every other key is
    kept."""
    out = dict(cfg)
    for key, asked in (("auto_merge", auto_merge), ("label_gated_ci", label_gated_ci)):
        if asked is not None and out.get(key) is None:
            out[key] = False
    return out


def auto_merge(cfg: Dict[str, Any], repo: Optional[str] = None) -> bool:
    """Whether a clean pass auto-merges (squash-merge + delete branch) the PR for
    ``repo``. A CONFIRMED repo's per-repo ``auto_merge`` (a ``repos[<repo>]`` entry
    with a **bool** value) enables it; otherwise OFF (fail-closed). Unlike
    :func:`label_gated_ci` there is deliberately NO global-default tier — the merge
    is opt-in PER REPO (the wizard's ``step_repo_auto_merge`` is the only writer of
    the per-repo value), so this consults ONLY ``repos[<repo>]``. Setup also writes a
    top-level ``auto_merge: false`` for a separately installed backend; this function
    ignores it, so editing it changes nothing here. A non-bool
    per-repo value falls to OFF, never on. ``repo=None`` → OFF. The per-run
    ``--auto-merge`` / ``--no-auto-merge`` flag, when explicitly set, always wins
    over this (resolved at the CLI: flag > this config value > off)."""
    entry = repo_entry(cfg, repo)
    v = entry.get("auto_merge") if entry is not None else None
    return v if isinstance(v, bool) else False


def test_command(cfg: Dict[str, Any], repo: Optional[str] = None) -> Optional[str]:
    """The configured test-gate command STRING for ``repo``: a CONFIRMED repo's
    non-blank per-repo ``test_command`` (a ``repos[<repo>]`` entry carrying the
    key) wins; otherwise the non-blank top-level global ``test_command``;
    otherwise ``None`` (the gate then auto-detects). Unlike
    :func:`label_gated_ci`, a blank / ``None`` per-repo value FALLS THROUGH to
    the global rather than shadowing it — a config that predates this key is
    byte-for-byte unchanged. ``repo=None`` reads the global value. The env
    override (``BUDDHI_TEST_COMMAND``) is the caller's concern
    (:func:`buddhi_review.commit_push.resolve_test_command`), not config's."""
    entry = repo_entry(cfg, repo)
    raw = entry.get("test_command") if entry is not None else None
    if not (raw and str(raw).strip()):
        raw = cfg.get("test_command")
    if not (raw and str(raw).strip()):
        return None
    return str(raw)


def repo_test_command(cfg: Dict[str, Any], repo: Optional[str]) -> Optional[str]:
    """The EXPLICIT per-repo ``test_command`` STRING for ``repo`` (a
    ``repos[<repo>]`` entry carrying a non-blank ``test_command``), else ``None``.
    Unlike :func:`test_command` this reads ONLY the persisted per-repo value —
    never the global — so the wizard can show and PRESERVE a repo's own
    configured command without folding in the global default."""
    entry = repo_entry(cfg, repo)
    if not isinstance(entry, dict):
        return None
    raw = entry.get("test_command")
    return str(raw) if raw is not None and str(raw).strip() else None


# ── Per-repo writer (the ``repos:`` map) ────────────────────────────────────────

def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``base`` with ``overlay`` recursively layered on: nested
    dicts merge key-by-key, every other value (lists included) replaces wholesale,
    and an overlay value of ``None`` REMOVES the key. ``None`` is the per-repo
    TRISTATE — "no explicit value, inherit the default" — so removing the key is
    what persists that intent (an absent key is exactly what
    :func:`label_gated_ci` / :func:`test_command` read as *inherit*, and what the
    reference wizard writes by simply omitting the key when its value is ``None``).
    It is also the wizard's explicit-clear signal for a per-repo ``test_command``,
    so a caller can drop a persisted key through the same writer that sets one.
    Keys present in ``base`` but absent from ``overlay`` are preserved."""
    out = dict(base)
    for k, v in overlay.items():
        cur = out.get(k)
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(cur, dict):
            out[k] = _deep_merge(cur, v)
        else:
            out[k] = v
    return out


def _prune_stale_auto_on_open(entry: Dict[str, Any]) -> None:
    """Drop ``auto_on_open`` flags for bots no longer in ``entry``'s
    ``active_reviewers``. :func:`_deep_merge` replaces the ``active_reviewers``
    list wholesale but merges the ``auto_on_open`` dict key-by-key, so a setup
    re-run that drops a reviewer would otherwise leave that bot's stale flag
    behind. Prune only when the merged entry carries a **list**
    ``active_reviewers`` — never against an absent or malformed fleet, which would
    wipe every flag. Membership is the exact per-bot string match the
    :func:`auto_on_open` reader uses, so a flag survives iff a current reviewer
    would still look it up. Mutates ``entry`` in place, and only when a key is
    actually dropped (a no-op leaves the merged block, and its identity, intact)."""
    fleet = entry.get("active_reviewers")
    block = entry.get("auto_on_open")
    if not isinstance(fleet, list) or not isinstance(block, dict):
        return
    live = {str(b) for b in fleet}
    pruned = {bot: flag for bot, flag in block.items() if bot in live}
    if len(pruned) != len(block):
        entry["auto_on_open"] = pruned


def set_repo_keys(repo: str, keys: Dict[str, Any], path: Optional[Path] = None) -> bool:
    """Deep-merge ``keys`` into ``cfg["repos"][norm_repo(repo)]`` and persist the
    config atomically, leaving sibling repos and every unknown key intact.

    This is the per-repo CONFIRMATION writer: it records a repo's
    ``active_reviewers`` / ``auto_on_open`` / ``label_gated_ci`` /
    ``test_command`` and, by creating the ``repos[<repo>]`` entry, marks the repo
    confirmed (:func:`repo_entry`'s presence marker). An existing entry is
    updated in place under a case-insensitive match, so re-confirming a repo
    never spawns a duplicate sibling key. A ``None`` value in ``keys`` REMOVES
    that key from the entry (see :func:`_deep_merge` — the wizard's explicit
    "none"/"default" clear), never persists a null. After the merge,
    ``auto_on_open`` is pruned to the resulting ``active_reviewers`` (see
    :func:`_prune_stale_auto_on_open`) so a re-run that drops a reviewer cannot
    leave that bot's stale flag behind. Returns ``False`` (writing nothing) for
    an unusable repo / non-dict ``keys``, when another process kept the config
    lock past its timeout (:func:`config_lock`), or when the atomic write fails."""
    key = norm_repo(repo)
    if key is None or not isinstance(keys, dict):
        return False
    p = path or config_path()
    # Reuse the wizard's single atomic, merge-preserving writer. Deferred import:
    # wizard imports config at module load, so a top-level import here would be
    # circular — config is the lower layer.
    from buddhi_review.wizard import write_config
    # The read and the write are one locked unit, so a concurrent writer's update
    # can never be read-then-overwritten away — and without the lock there is no
    # write at all, for the same reason.
    with config_lock(p) as may_write:
        if not may_write:
            return False
        cfg = load_config(p) if p.exists() else {}
        repos = cfg.get("repos")
        repos = dict(repos) if isinstance(repos, dict) else {}
        # Update the existing entry in place under a case-insensitive match so the
        # same repo never gains a second, differently-cased sibling key.
        target = next((k for k in repos if str(k).strip().lower() == key), key)
        base = repos.get(target)
        merged = _deep_merge(base if isinstance(base, dict) else {}, keys)
        # _deep_merge replaces the active_reviewers LIST wholesale but merges the
        # auto_on_open DICT key-by-key, so a re-run that drops a reviewer would leave
        # its stale auto_on_open flag behind. Prune the merged block back to the
        # resulting fleet (no-op when the entry has no list active_reviewers).
        _prune_stale_auto_on_open(merged)
        repos[target] = merged
        cfg = dict(cfg)
        cfg["repos"] = repos
        return write_config(cfg, p)


def notifier_channel(cfg: Dict[str, Any]) -> str:
    """Notifications are delivered to the console. This is the only channel this
    package ships, regardless of what a hand-edited config sets."""
    return "console"
