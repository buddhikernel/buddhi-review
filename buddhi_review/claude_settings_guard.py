"""A pull request cannot run code through a committed ``.claude/settings.json``.

Every ``claude`` this package starts — the fixer and every model call — runs in
the user's checkout of the PR, and Claude Code loads ``.claude/settings.json`` and
``.claude/settings.local.json`` from its working directory. Several settings run
commands or load code: ``hooks`` (a ``SessionStart`` hook fires before any tool
call), ``env`` (``BASH_ENV``, ``NODE_OPTIONS``, …), ``apiKeyHelper``,
``statusLine`` and more. Left alone, a PR from an author the user does not
control could run code on the user's machine just by committing such a file.

:func:`window` wraps ONE ``claude`` spawn. For the spawn's duration each settings
file keeps a top-level key only when

* it is in :data:`INERT_KEYS` — display, notification and cosmetic preferences
  audited as unable to run anything. It is an ALLOWLIST: a key a future CLI
  release adds is held back until someone proves it inert; or
* its parsed JSON value equals the value the PR's BASE commit holds for the same
  key of the same file, AND every checkout file or directory the value names is
  byte-identical to base (:func:`named_paths`). A value that names the checkout
  root, climbs out of it, changes into a directory nobody can know in advance,
  or cannot be parsed, names more than can be checked, so it is held back.

Everything else is held back for the spawn and put back afterwards — the same
bytes, the same file mode, the same git index flags — after a normal exit, an
exception, a SIGTERM (the spawn's child is killed with it), a SIGKILL or a reboot:
the original is recorded first in a durable, owner-only journal
(:func:`state_dir`), which is replayed before the next spawn in that checkout and
at loop entry (:func:`recover`). When the spawn changed a file meanwhile, an
edit that is still one JSON object gets back the held keys it did not re-state,
whoever holds the original — git (a tracked file with no local edit) or only the
journal (an untracked file, a local edit). A deletion, a replacement or an edit
that is not one JSON object keeps the spawn's change, and when only the journal
holds the original, it is saved, owner-only, beside the journal, and named. An
original that is not one JSON object wins over the edit, and the dropped edit is
announced.

Each spawn also passes :data:`CLAUDE_ARGS`, so Claude Code loads no LOCAL
settings file at all — including the one it would otherwise read from the
repository's canonical git root, outside the checkout.

When a file cannot be made inert (a read-only ``.claude/``, a failed write, a
journal that cannot be written durably), the spawn does not happen:
:class:`SettingsGuardRefusal` — an ``OSError`` — is raised, which the fixer turns
into an escalation and a model call into its ordinary launch failure.

A checkout with neither a ``.claude`` entry nor a journal costs a few metadata
system calls: no subprocess and no write. Only one window per checkout is open at
a time, across processes too (an ``flock`` held from recovery to restore). Every
write goes through a directory descriptor, never through a symlink, into a temp
file that is then renamed over the target — so a settings path is never absent or
half-written, and nothing outside the checkout ever receives a write.

Pure stdlib. Which commit counts as "base" is decided by an injected resolver
(:func:`install_base_resolver`); with none installed, or when it cannot answer,
only :data:`INERT_KEYS` survive.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple

# Audited against the Claude Code settings schema: each key only changes what the
# CLI displays or how it notifies, except ``disabledMcpjsonServers`` (it can only
# switch servers OFF) and ``model`` (the spawn's explicit ``--model`` outranks
# it). ``disableAllHooks`` is deliberately absent: a PR-supplied ``true`` would
# switch off the base-trusted hooks this guard keeps live, so it must match base.
# So is ``cleanupPeriodDays``: it drives a sweep that deletes the user's
# transcripts and other history under ``~/.claude`` older than that many days.
INERT_KEYS = frozenset({
    "alwaysThinkingEnabled",
    "disabledMcpjsonServers",
    "includeCoAuthoredBy",
    "messageIdleNotifThresholdMs",
    "model",
    "preferredNotifChannel",
    "spinnerTipsEnabled",
    "syntaxHighlightingDisabled",
    "theme",
    "verbose",
})

CLAUDE_DIR = ".claude"
SETTINGS_FILES = ("settings.json", "settings.local.json")

# Every guarded ``claude`` spawn carries these arguments: load user and project
# settings, never LOCAL ones. Claude Code reads ``settings.local.json`` not only
# from the working directory but from the repository's canonical git root — for a
# linked worktree, the PRIMARY checkout, which this guard never touches and which
# can have another PR's commit checked out. A checkout's own local file carries no
# base-trusted key anyway (it has no base copy), so nothing a spawn needs is lost.
CLAUDE_ARGS = ("--setting-sources", "user,project")

# Where the journal and the per-checkout lock live (default: the shared buddhi
# cache dir). The test suite points it at a per-test temp dir.
STATE_DIR_ENV = "BUDDHI_SETTINGS_GUARD_DIR"
# Test containment only: the directory a falsy ``cwd`` resolves to instead of the
# process's working directory, so the suite never scrubs the contributor's own
# checkout. Unset in normal use.
FALLBACK_CWD_ENV = "BUDDHI_SETTINGS_GUARD_FALLBACK_CWD"

_JOURNAL_VERSION = 1
_DIR_LINK = "claude-dir"
_KINDS = (_DIR_LINK,) + SETTINGS_FILES
_GIT_TIMEOUT = 60
_INDEX_LOCK_RETRIES = 5
_MAX_LINK_HOPS = 8
_MAX_NESTING = 6
_MAX_JSON_DEPTH = 64
_MAX_WALK_FILES = 20_000
_MAX_WALK_BYTES = 256 * 1024 * 1024
_BOM = b"\xef\xbb\xbf"
_TMP_SUFFIX = ".guard-tmp"
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class SettingsGuardRefusal(OSError):
    """A checkout's ``.claude`` settings could not be made inert, so no ``claude``
    was started. An ``OSError`` so both callers' launch-failure paths apply
    unchanged: the fixer's ``fixer spawn failed: …`` escalation, and a model
    call's ``failed to launch claude`` error."""


class _Unrecoverable(Exception):
    """An interrupted earlier window's settings could not be put back."""


def _say(message: str) -> None:
    print(f"[settings-guard] {message}", file=sys.stderr, flush=True)


# ── the base-commit resolver ─────────────────────────────────────────────────────

# resolver(checkout) -> the full sha of the commit whose settings are trusted, or
# None when it cannot tell. It may carry a ``last_error`` string explaining a None.
BaseResolver = Callable[[str], Optional[str]]

_resolver: Optional[BaseResolver] = None
_unknown_base_noted: Set[str] = set()


def install_base_resolver(resolver: Optional[BaseResolver]) -> None:
    """Install the process-wide base resolver (``None`` uninstalls it)."""
    global _resolver
    _resolver = resolver
    _unknown_base_noted.clear()


def uninstall_base_resolver() -> None:
    install_base_resolver(None)


def _base_commit(checkout: str) -> Optional[str]:
    """The trusted commit for ``checkout``, or None — logged once per checkout
    (and again after a success) so a run that cannot resolve it says why, without
    a line for every spawn."""
    resolver = _resolver
    sha: Optional[str] = None
    if resolver is None:
        why = "no base resolver is installed"
    else:
        try:
            sha = resolver(checkout)
        except Exception as exc:  # a resolver bug must degrade, never crash a spawn
            sha, why = None, f"the base resolver failed: {exc}"
        else:
            why = str(getattr(resolver, "last_error", None) or "the base resolver found none")
    if isinstance(sha, str) and _SHA_RE.fullmatch(sha.strip()):
        _unknown_base_noted.discard(checkout)
        return sha.strip()
    if checkout not in _unknown_base_noted:
        _unknown_base_noted.add(checkout)
        _say(f"the PR's base commit is unknown ({why}); until it is known, claude "
             f"runs in {checkout} keep only display settings from .claude/")
    return None


# ── paths, state dir, locking ────────────────────────────────────────────────────

def state_dir() -> str:
    """The journal + lock directory: ``$BUDDHI_SETTINGS_GUARD_DIR`` when set, else
    ``~/.cache/buddhi/settings-guard``. It must survive a reboot (a journal is
    replayed after one), so it is never the system temp dir."""
    env = os.environ.get(STATE_DIR_ENV)
    if env:
        return os.path.expanduser(env)
    return os.path.expanduser("~/.cache/buddhi/settings-guard")


def _checkout(cwd: Optional[str]) -> str:
    """The directory whose settings the child loads. A falsy ``cwd`` means the
    child inherits this process's working directory, so that is what is guarded
    — resolved here, before anything is keyed, journaled or scrubbed."""
    if not cwd:
        fallback = os.environ.get(FALLBACK_CWD_ENV) if "PYTEST_CURRENT_TEST" in os.environ else None
        cwd = fallback or os.getcwd()
    return os.path.abspath(cwd)


def _canonical(checkout: str) -> str:
    """One name per checkout directory, however the caller spelled it: its real
    path, and on macOS the filesystem's own spelling of it (another letter case or
    Unicode normalisation names the same directory there)."""
    real = os.path.realpath(checkout)
    try:
        import fcntl
        getpath = fcntl.F_GETPATH
    except (ImportError, AttributeError):
        return real
    try:
        fd = os.open(real, os.O_RDONLY)
    except OSError:
        return real
    try:
        raw = fcntl.fcntl(fd, getpath, bytes(1024))
        return os.fsdecode(raw.split(b"\0", 1)[0]) or real
    except OSError:
        return real
    finally:
        os.close(fd)


def _key(checkout: str) -> str:
    return hashlib.sha256(os.fsencode(_canonical(checkout))).hexdigest()[:32]


def _journal_path(checkout: str) -> str:
    return os.path.join(state_dir(), _key(checkout) + ".json")


def _needs_attention(checkout: str) -> bool:
    """The hot-path test: metadata system calls only — no subprocess, no write."""
    return (os.path.lexists(os.path.join(checkout, CLAUDE_DIR))
            or os.path.lexists(_journal_path(checkout)))


def _private_state_dir() -> str:
    """The state dir, created 0o700 and verified to be a real directory owned by
    this user and closed to everyone else — the journal can hold the bytes of an
    untracked ``settings.local.json``, the usual home of local secrets."""
    d = state_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{d} is not a directory")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise OSError(f"{d} belongs to another user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(d, 0o700)
    return d


_process_lock = threading.RLock()
_depth: Dict[str, int] = {}


@contextlib.contextmanager
def _locked(checkout: str) -> Iterator[bool]:
    """Hold the checkout's lock; yields True when this thread already holds it (a
    re-entry, which must neither deadlock nor mistake its own live window for a
    crash). Across processes it is an ``flock`` the kernel frees on death."""
    key = _key(checkout)
    with _process_lock:
        if _depth.get(key):
            _depth[key] += 1
            try:
                yield True
            finally:
                _depth[key] -= 1
            return
        fd = None
        try:
            import fcntl
        except ImportError:  # no flock on this platform: in-process exclusion only
            fcntl = None
        if fcntl is not None:
            try:
                path = os.path.join(_private_state_dir(), key + ".lock")
                fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                if fd is not None:
                    os.close(fd)
                raise _refusal(checkout, f"cannot lock the checkout ({exc})")
        _depth[key] = 1
        try:
            yield False
        finally:
            _depth[key] = 0
            if fd is not None:
                os.close(fd)  # closing the descriptor releases the flock


def _refusal(checkout: str, reason: str) -> SettingsGuardRefusal:
    _say(f"not starting claude in {checkout}: {reason}")
    return SettingsGuardRefusal(f"the .claude settings in {checkout} could not be made safe: {reason}")


# ── small file helpers ───────────────────────────────────────────────────────────

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def _lstat(path: str) -> Optional[os.stat_result]:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _drain(fd: int) -> bytes:
    try:
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _read(path: str) -> bytes:
    """Read a regular file without following a symlink swapped in at ``path``."""
    return _drain(os.open(path, os.O_RDONLY | _NOFOLLOW))


def _read_regular(path: str) -> bytes:
    """:func:`_read`, refusing anything but a regular file (a FIFO would block)."""
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"{path} is not a regular file")
    return _drain(fd)


def _tmp_name(name: str, token: str) -> str:
    """The temp file a write of ``name`` goes through. ``token`` is random per
    window and recorded in its journal, so an interrupted write's leftover can be
    found and removed by its exact name — a name nobody could have predicted, so a
    PR can never have committed a file under it."""
    return f".{name}.{token}{_TMP_SUFFIX}"


class _Dir:
    """A directory held open by descriptor. Every operation below it is relative to
    that descriptor, and every directory under the checkout is opened without
    following a symlink — so a ``.claude`` swapped for a symlink (by a spawn, or
    after a crash) can never redirect a read or a write outside the checkout."""

    def __init__(self, fd: int):
        self.fd = fd

    @classmethod
    def open(cls, path: str, *, follow: bool = False) -> "_Dir":
        if os.open not in os.supports_dir_fd:
            raise OSError("this platform cannot open directories by descriptor")
        return cls(os.open(path, os.O_RDONLY | _DIRECTORY | (0 if follow else _NOFOLLOW)))

    def child(self, name: str) -> "_Dir":
        return _Dir(os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=self.fd))

    def __enter__(self) -> "_Dir":
        return self

    def __exit__(self, *exc) -> None:
        os.close(self.fd)

    def spelled(self, name: str) -> str:
        """The on-disk spelling of ``name``, matched case-folded (a case-insensitive
        filesystem serves ``.Claude/Settings.json`` to a reader of
        ``.claude/settings.json``); ``name`` itself when there is no such entry."""
        try:
            entries = os.listdir(self.fd)
        except OSError:
            return name
        if name in entries:
            return name
        folded = name.casefold()
        return next((e for e in sorted(entries) if e.casefold() == folded), name)

    def lstat(self, name: str) -> Optional[os.stat_result]:
        try:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except OSError:
            return None

    def read(self, name: str) -> bytes:
        return _drain(os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=self.fd))

    def readlink(self, name: str) -> str:
        return os.readlink(name, dir_fd=self.fd)

    def write(self, name: str, data: bytes, mode: int, token: str, *, full: bool = False) -> None:
        """Replace ``name`` with ``data`` at exactly ``mode``: written to a temp file
        in the same directory, synced, then renamed over it — so the path is never
        absent or half-written, a hard link at it never receives the bytes, and the
        bytes never exist under a mode wider than ``mode`` (the temp file is created
        at ``mode`` and ``fchmod``ed to its exact bits before a byte is written)."""
        tmp = _tmp_name(name, token)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
        fd = os.open(tmp, flags, mode & 0o777, dir_fd=self.fd)
        try:
            try:
                os.fchmod(fd, mode & 0o7777)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                _fsync(fd, full)
            finally:
                os.close(fd)
            os.replace(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=self.fd)
            raise
        self.sync(full)

    def symlink(self, target: str, name: str, token: str) -> None:
        tmp = _tmp_name(name, token)
        os.symlink(target, tmp, dir_fd=self.fd)
        try:
            os.replace(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=self.fd)
            raise
        self.sync()

    def unlink(self, name: str) -> None:
        os.unlink(name, dir_fd=self.fd)
        self.sync()


    def set_mode(self, name: str, mode: int) -> None:
        """Put ``mode`` back on a regular file without following a symlink."""
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=self.fd)
        try:
            if stat.S_IMODE(os.fstat(fd).st_mode) != mode & 0o7777:
                os.fchmod(fd, mode & 0o7777)
        finally:
            os.close(fd)

    def sync(self, full: bool = False) -> None:
        with contextlib.suppress(OSError):
            _fsync(self.fd, full)


def _fsync(fd: int, full: bool) -> None:
    """``fsync``; with ``full`` on macOS, ``F_FULLFSYNC``, which also flushes the
    drive's cache — the journal must be on disk before the scrub it covers."""
    if full:
        try:
            import fcntl
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except (ImportError, AttributeError, OSError):
            pass
    os.fsync(fd)


# ── settings JSON ────────────────────────────────────────────────────────────────

def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constants(name):
    raise ValueError(f"not JSON: {name}")


def _parse(data: bytes) -> Optional[dict]:
    """The settings object, or None unless the bytes are ONE strict JSON object
    (a leading UTF-8 BOM tolerated). Duplicate keys, ``NaN`` and friends count as
    unparseable: another parser could read them differently from this one. So
    does nesting deeper than :data:`_MAX_JSON_DEPTH` — a fixed limit, so the
    answer never depends on how deep the caller's own stack is."""
    if data.startswith(_BOM):
        data = data[len(_BOM):]
    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                         parse_constant=_no_constants)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or _depth_of(obj) > _MAX_JSON_DEPTH:
        return None
    return obj


def _depth_of(value) -> int:
    deepest, stack = 0, [(value, 1)]
    while stack:
        item, depth = stack.pop()
        deepest = max(deepest, depth)
        if deepest > _MAX_JSON_DEPTH:
            break
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
    return deepest


def _blank(data: bytes) -> bool:
    return not (data[len(_BOM):] if data.startswith(_BOM) else data).strip()


def _canon(value) -> str:
    """Type-strict, key-order-free form for "equals the base value": ``1``,
    ``1.0`` and ``true`` stay distinct."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _dump(obj: dict) -> bytes:
    text = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate from a ``\\ud800`` escape: keep it escaped
        return (json.dumps(obj, indent=2) + "\n").encode("utf-8")


# ── what a settings value names ──────────────────────────────────────────────────

_ROOT_VARS = frozenset({"CLAUDE_PROJECT_DIR", "PWD"})
# Directories a hook cannot know in advance: where the previous ``cd`` left.
_UNKNOWABLE_VARS = frozenset({"OLDPWD", "DIRSTACK"})
_ROOT_COMMANDS = frozenset({"pwd", "pwd -P", "pwd -L", "git rev-parse --show-toplevel"})
_BLANKS = " \t\r"
_COMMAND_ENDS = ";&|()\n"  # end a word AND a simple command
_CONTINUED = ([("end", "&&")], [("end", "||")], [("end", "|")])
_CD_COMMANDS = frozenset({"cd", "pushd", "popd"})
_STACK_ARG = re.compile(r"-|[+-][0-9]+")  # ``cd -``, ``pushd +1``, ``popd -0``
_MAX_CD_TARGETS = 16
_REDIRECTS = "<>"          # end a word
_NESTED = set(_BLANKS) | set(_COMMAND_ENDS) | set(_REDIRECTS) | set("'\"`")
_GLOB = frozenset("*?[{")
_PIECE_SEPARATORS = frozenset("=:,")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SYNTAX_WORDS = frozenset({"[", "[[", "]", "]]", "{", "}", "{}", "!"})
# Words after which the next word is still in command position: prefixes
# (``env python3 x.py``) and the shell's reserved words (``if …; then . ./x; fi``).
_PREFIX_COMMANDS = frozenset({"env", "exec", "command", "builtin", "nohup", "time", "nice",
                              "if", "then", "elif", "else", "do", "while", "until",
                              "fi", "done", "esac"})
# Package runners whose ``run`` runs a script (``uv run x.py``).
_RUNNERS = frozenset({"uv", "poetry", "pipenv", "pdm", "rye", "hatch", "bun", "deno"})
# Interpreters that import from the directory of the script they run
# (``sys.path[0]``, ``require('./x')``, ``require_relative``, ``FindBin``): that
# whole directory is a dependency, not only the script.
_SCRIPT_DIR_INTERPRETERS = re.compile(
    r"(python[0-9.]*|pypy[0-9.]*|node|nodejs|bun|deno|tsx|ts-node|ruby|perl)")
# Interpreter options after which the next word is code or a module name, not a
# script path, by interpreter family ("?" is an interpreter named by a variable or a
# runner); and options that take the next word as their argument.
_CODE_OPTIONS = {
    "python": frozenset({"-c", "-m"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "ruby": frozenset({"-e"}),
    "perl": frozenset({"-e", "-E"}),
}
_CODE_OPTIONS["?"] = frozenset().union(*_CODE_OPTIONS.values())
_PYTHON = re.compile(r"python[0-9.]*|pypy[0-9.]*")
# A cluster of Python's no-argument flags, optionally ending in an option that takes
# an argument: ``-c`` / ``-m`` / ``-W`` / ``-X``, its argument glued on (``-Wd``,
# ``-c'import x'``) or the next word (``-uW ignore``, ``-uc 'import x'``).
_PYTHON_FLAGS = re.compile(r"-([BbdEhiIOPqsSuvx]*)(?:([cmWX])(.*))?", re.S)
# A cluster of switches ending in an inline-code option, the code glued on or the
# next word (``ruby -we 'x'``, ``perl -ne'x'``, ``node -pe 'x'``).
_CODE_CLUSTERS = {
    "ruby": re.compile(r"-[acdlnpsSvwy]*e(.*)", re.S),
    "perl": re.compile(r"-[aclnpsStTuUvwWX0-9]*[eE](.*)", re.S),
    "node": re.compile(r"-pe()"),
}
# What a script path ends with: an operand of an interpreter, a module it preloads
# or a file its code names, of this shape, is run — its directory is a dependency.
_CODE_SUFFIXES = (".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx",
                  ".rb", ".pl", ".pm", ".sh", ".bash", ".php", ".lua")
# What Python imports from a directory on its path.
_PY_MODULE_SUFFIXES = (".py", ".pyc", ".pyo", ".pyw", ".so", ".pyd")
_PY_IMPORT = re.compile(r"(?:^|[\s;:])(?:import\s+([\w.]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w.]+(?:\s+as\s+\w+)?)*)"
                        r"|from\s+([\w.]+)\s+import)")
# Options that take the next word as their argument, by interpreter family
# (Python's come from :data:`_PYTHON_FLAGS`; its ``-I`` is a flag, as are Ruby's
# ``-W`` and Perl's ``-W`` / ``-X`` / ``-C``, and Perl's ``-M`` takes its module
# glued on: ``perl -M strict`` is an error).
_ARG_OPTIONS = {
    "python": frozenset(),
    "node": frozenset({"-r", "--require", "--import", "--loader", "--experimental-loader",
                       "-C", "--conditions"}),
    "ruby": frozenset({"-I", "-r", "-C", "-X"}),
    "perl": frozenset({"-I"}),
}
_ARG_OPTIONS["?"] = frozenset().union(*_ARG_OPTIONS.values())
# A string literal handed to a loader — Node's ``require``, Ruby's ``require`` /
# ``require_relative`` / ``load``, Perl's ``do`` / ``require``, Python's
# ``exec(open(…))`` — alone or joined onto an expression (``require(dir + '/x')``).
# It names code, whether or not it has a suffix.
_LOADER = re.compile(r"(?:\b(?:require_relative|require|load|do)\s*\(?|\bexec\s*\(\s*open\s*\()"
                     r"\s*(?:[^;()\n]*?[+.]\s*)??[rRbBuUfF]{0,2}(?=['\"`])")
# A string literal in interpreter code (``'x'``, ``"x"``, ``r'x'``, ``f"x"``, `` `x` ``).
_CODE_LITERAL = re.compile(r"""(['"`])((?:\\.|(?!\1).)*)\1""", re.S)
_BRACE_EXPANSION = re.compile(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")
_TILDE_STACK = re.compile(r"~[+-]?[0-9]*")

# Marker items standing for an expansion inside a word: the checkout root, the
# home directory, or an opaque value (``$FOO``, ``$1``) whose content is unknown.
_ROOT = ("root",)
_HOME = ("home",)


def _opaque(name: str) -> tuple:
    return ("opaque", name)


class _Unparseable(Exception):
    pass


def _expansion_end(s: str, i: int) -> int:
    """Index just past the ``$…`` / backtick expansion starting at ``s[i]``."""
    n = len(s)
    if s[i] == "`":
        j = i + 1
        while j < n:
            if s[j] == "\\":
                j += 2
                continue
            if s[j] == "`":
                return j + 1
            j += 1
        raise _Unparseable("unterminated backquote")
    if i + 1 >= n:
        return i + 1
    nxt = s[i + 1]
    if nxt in "({":
        close = ")" if nxt == "(" else "}"
        depth, j, quote = 0, i + 1, None
        while j < n:
            c = s[j]
            if quote:
                if c == "\\" and quote == '"':
                    j += 2
                    continue
                if c == quote:
                    quote = None
            elif c == "\\":
                j += 2
                continue
            elif c in "'\"":
                quote = c
            elif c == nxt:
                depth += 1
            elif c == close:
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        raise _Unparseable("unterminated expansion")
    m = _NAME_RE.match(s, i + 1)
    if m:
        return m.end()
    if nxt.isdigit() or nxt in "@*#?$!-":
        return i + 2
    return i + 1


def _split(command: str, *, globs: bool = True) -> List[List[tuple]]:
    """Split one shell command into words, as a shell would before expanding them:
    quotes are removed and adjacent quoted and unquoted parts join into ONE word
    (``"$CLAUDE_PROJECT_DIR"/x`` is one word). Each word is a list of items:
    ``("c", char, unquoted)`` for a literal character, or ``("x", text)`` for an
    expansion kept verbatim. A word holding only ``("end", op)`` marks where one
    simple command ends and the next begins, and how: ``op`` is ``;``, ``&&``,
    ``||``, ``|``, ``&``, ``(``, ``)``, ``;;`` or a newline (a newline right after
    ``&&``, ``||`` or ``|`` only continues the line). A word holding only
    ``("redir",)`` precedes a redirect's file or descriptor (``> out``, ``2>&1``,
    ``<<EOF``); a descriptor number before the operator is dropped. Raises
    :class:`_Unparseable`
    on what it cannot follow — an unbalanced quote, an unterminated expansion,
    ``$'…'`` quoting. Without ``globs``, no character counts as unquoted (text that
    no shell will glob)."""
    words: List[List[tuple]] = []
    word: List[tuple] = []
    started = False
    i, n = 0, len(command)

    def flush():
        nonlocal word, started
        if started:
            words.append(word)
        word, started = [], False

    while i < n:
        c = command[i]
        if c in _BLANKS:
            flush()
            i += 1
            continue
        if c in _REDIRECTS:
            if started and word and all(it[0] == "c" and it[1].isdigit() for it in word):
                word, started = [], False  # ``2>``: the descriptor, not an argument
            flush()
            i += 1
            while i < n and command[i] in "<>&|":  # ``>>``, ``<<<``, ``>&``, ``>|``
                i += 1
            words.append([("redir",)])
            continue
        if c in _COMMAND_ENDS:
            flush()
            op = command[i:i + 2] if command[i:i + 2] in (";;", "&&", "||") else c
            i += len(op)
            if not (op == "\n" and words and words[-1] in _CONTINUED):
                words.append([("end", op)])
            continue
        if c == "#" and not started:  # a comment runs to the end of the line
            j = command.find("\n", i)
            i = n if j < 0 else j
            continue
        started = True
        if c == "\\":
            if i + 1 >= n:
                raise _Unparseable("trailing backslash")
            if command[i + 1] != "\n":
                word.append(("c", command[i + 1], False))
            i += 2
        elif c == "'":
            j = command.find("'", i + 1)
            if j < 0:
                raise _Unparseable("unbalanced single quote")
            word.extend(("c", ch, False) for ch in command[i + 1:j])
            i = j + 1
        elif c == '"':
            i += 1
            while True:
                if i >= n:
                    raise _Unparseable("unbalanced double quote")
                ch = command[i]
                if ch == '"':
                    i += 1
                    break
                if ch == "\\" and i + 1 < n and command[i + 1] in '$`"\\\n':
                    if command[i + 1] != "\n":
                        word.append(("c", command[i + 1], False))
                    i += 2
                elif ch in "$`":
                    j = _expansion_end(command, i)
                    word.append(("x", command[i:j]) if j > i + 1 else ("c", ch, False))
                    i = j
                else:
                    word.append(("c", ch, False))
                    i += 1
        elif c == "$" and i + 1 < n and command[i + 1] == '"':
            i += 1  # bash's $"…" (a locale-translated string) is the same word as "…"
        elif c in "$`":
            if c == "$" and i + 1 < n and command[i + 1] == "'":
                raise _Unparseable("$'…' quoting")
            j = _expansion_end(command, i)
            word.append(("x", command[i:j]) if j > i + 1 else ("c", c, True))
            i = j
        else:
            word.append(("c", c, globs))
            i += 1
    flush()
    return words


def _classify_expansion(text: str) -> tuple:
    """``$CLAUDE_PROJECT_DIR`` / ``${PWD}`` / ``$(pwd)`` … → the root marker;
    ``$HOME`` → the home marker; any other plain variable or positional parameter
    → opaque; anything the guard cannot evaluate (a command substitution,
    ``${X:-…}``, arithmetic, ``$OLDPWD``) → :class:`_Unparseable`, because it could
    name any path at all."""
    if text.startswith("${"):
        name = text[2:-1]
        if not _NAME_RE.fullmatch(name):
            raise _Unparseable(f"cannot evaluate {text}")
    elif text.startswith("$(("):
        raise _Unparseable(f"cannot evaluate {text}")
    elif text.startswith("$(") or text.startswith("`"):
        inner = text[2:-1] if text.startswith("$(") else text[1:-1]
        if " ".join(inner.split()) in _ROOT_COMMANDS:
            return _ROOT
        raise _Unparseable(f"cannot evaluate {text}")
    else:
        name = text[1:]
        if not _NAME_RE.fullmatch(name):
            return _opaque(name)  # $1, $@, $? …
    if name in _ROOT_VARS:
        return _ROOT
    if name in _UNKNOWABLE_VARS:
        raise _Unparseable(f"cannot know {text}")
    return _HOME if name == "HOME" else _opaque(name)


def _text(items: List[tuple]) -> str:
    """The raw text of a word's items (literal characters and verbatim expansions)."""
    return "".join(it[1] for it in items)


def _chars(items: List[tuple]) -> Optional[str]:
    """The text of classified items when every one is a literal character."""
    if any(it[0] != "c" for it in items):
        return None
    return "".join(it[1] for it in items)


def _lead(items: List[tuple], n: int) -> str:
    """Up to ``n`` literal characters at the start, stopping at a marker."""
    out = []
    for it in items[:n]:
        if it[0] != "c":
            break
        out.append(it[1])
    return "".join(out)


def _literal(text: str) -> List[tuple]:
    return [("c", ch, False) for ch in text]


# A literal (quoted) ``$`` that would start an expansion if a shell re-read the
# word — as ``bash -c '$CLAUDE_PROJECT_DIR/x'`` makes one do.
_LATENT_EXPANSION = re.compile(r"\$[A-Za-z_{(]|`")


@dataclass
class _Named:
    """What a settings value names in the checkout."""
    paths: Set[str] = field(default_factory=set)       # exact files or directories
    trees: Set[str] = field(default_factory=set)       # directories a glob or variable reaches into
    bare: Set[str] = field(default_factory=set)        # relative words that are paths only if they exist
    bare_trees: Set[str] = field(default_factory=set)  # the same, reached into by a glob
    bare_root_scripts: Set[str] = field(default_factory=set)  # an interpreter's script at the root, if it exists
    commands: Set[Tuple[str, bool]] = field(default_factory=set)  # (path, bare) run directly: its shebang decides
    imports_root: bool = False                          # a Python program run with the root first on its path
    root_imports: Set[str] = field(default_factory=set)  # top-level names that program imports
    node: bool = False                                  # a Node-family program (or one of unknown family) runs
    unsafe: Optional[str] = None                       # names the root, escapes it, or is unparseable


def named_paths(value, checkout: Optional[str] = None, *, key: Optional[str] = None) -> _Named:
    """Everything a settings value names, for the dependency check.

    Every string in the value is split as a shell command (strings in positions
    that are never run — permission rules, hook matchers, hook types — are
    skipped by :func:`_command_strings`). A word names a checkout path when it
    starts with the project dir in any spelling (``"$CLAUDE_PROJECT_DIR"/x``,
    ``"${CLAUDE_PROJECT_DIR}"/x``, ``${CLAUDE_PROJECT_DIR}/x``,
    ``$CLAUDE_PROJECT_DIR/x``, ``"$CLAUDE_PROJECT_DIR/x"``, ``$PWD``, ``$(pwd)``,
    ``~+``), with ``./``, with an absolute path or a home-relative one that reaches
    the checkout, or is a relative word whose first component exists
    (``tools/check.py``) — each of those, and the pieces of a word split at ``=``,
    ``:`` and ``,`` (``--config=./x``, ``PYTHONPATH=lib:src``). A glob or a
    variable inside a path names the directory before it; a script an
    interpreter may run also names the script's directory, which it imports from,
    and a Python program run from the root (``-c``, ``-m``, stdin) depends on the
    modules there. A Node-family program (``node``, ``bun``, ``deno``, ``tsx``,
    ``ts-node``, or an interpreter of unknown family) also resolves a package name
    (``require('prettier')`` in its script, its ``-e`` code, ``-r``,
    ``NODE_OPTIONS``) through ``node_modules/`` and ``package.json`` in every
    directory above its code: :attr:`_Named.node` records that it runs, for the
    check. A ``cd`` only adds places a relative word is read from.
    The checkout root itself (a bare project-dir spelling, ``.``, ``./``, a
    root-level glob, an empty search-path element), a path that climbs out of the
    checkout, and anything the splitter cannot follow make the value unsafe
    outright. ``env`` values are also read literally, as the process gets them.

    ``key`` is the top-level settings key the value belongs to, which decides the
    positions :func:`_command_strings` skips. ``checkout`` resolves absolute and
    home-relative paths; existence is checked later."""
    named = _Named()
    try:
        for path, s in _command_strings(value, (key,) if key else ()):
            if len(path) == 2 and path[0] == "env":
                # An environment value is not run by a shell: read it literally
                # too, element by element, as the process that inherits it will.
                if _names_cwd(path[1], s):
                    named.unsafe = named.unsafe or "it names the checkout root"
                if path[1] == "NODE_OPTIONS":
                    named.node = True  # ``--require pkg``: resolved from the working directory
                for element in {s, *s.split(":")}:
                    if element:
                        _scan_literal(element, named, checkout)
                _scan_command(s, named, checkout, 0, strict=True, commands=False, scripts=True)
            else:
                _scan_command(s, named, checkout, 0, strict=True)
    except _Unparseable as exc:
        named.unsafe = f"it cannot be parsed ({exc})"
    except RecursionError:
        named.unsafe = "it is nested too deeply to check"
    except ValueError:  # a NUL or an unencodable character where a path should be
        named.unsafe = "it holds a path this guard cannot read"
    return named


def _command_strings(value, path: tuple) -> Iterator[Tuple[tuple, str]]:
    """Every string in ``value`` except those in positions Claude Code never runs:
    permission rules (``permissions.allow|deny|ask[]``, ``permissions.defaultMode``)
    are patterns it matches and ``permissions.additionalDirectories`` lists
    directories it may read; a hook's ``matcher`` / ``type`` / ``prompt`` are a
    regex, an enum and text for the model. Every other string — including in keys
    a future release adds — is treated as something that may run."""
    if isinstance(value, str):
        top = path[:1]
        if top == ("permissions",) and len(path) >= 2 and path[1] in (
                "allow", "deny", "ask", "defaultMode", "additionalDirectories"):
            return  # patterns it matches, and directories it may read: nothing runs
        if top == ("hooks",) and path and path[-1] in ("matcher", "type", "prompt"):
            return  # a regex, an enum, and a prompt hook's text for the model
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _command_strings(v, path + (k,))
    elif isinstance(value, list):
        for v in value:
            yield from _command_strings(v, path)


def _scan_command(command: str, named: _Named, checkout: Optional[str], depth: int,
                  *, strict: bool, commands: bool = True, globs: bool = True,
                  cwd: Optional["_Cwd"] = None, scripts: bool = False) -> None:
    """Scan a shell command's words. With ``commands``, each simple command is
    followed the way a shell reads it: the word in command position (after
    assignments, prefixes like ``env`` and reserved words like ``then``), the
    ``.`` builtin, an interpreter anywhere in the command and the script it runs
    (whose directory is a dependency too), a non-shell interpreter's ``-c`` code
    (read for its string literals), ``case`` and ``[[ … == … ]]`` patterns (not
    paths, though a command substitution in one still runs), and where a ``cd``
    may have left the shell (:class:`_Cwd`). ``cwd`` is that, inherited from the
    command a nested one is part of. With ``scripts``, every script-shaped word
    (:data:`_CODE_SUFFIXES`) is taken as a script whose directory it imports from
    (an ``env`` value such as ``NODE_OPTIONS=--require ./x.js``)."""
    if depth > _MAX_NESTING:
        raise _Unparseable("nested too deeply")
    try:
        words = _split(command, globs=globs)
    except _Unparseable:
        if strict:
            raise
        # A string inside a word (``sh -c '…'``'s argument, say) that is not valid
        # shell on its own: its path-looking pieces are still collected.
        words = []
        for piece in re.split(r"[\s;&|<>()'\"`]+", command):
            if piece:
                words.extend(_split(piece, globs=globs))
    st = _Command(commands, cwd)
    st.scripts = scripts

    def unsafe(why: str) -> None:
        named.unsafe = named.unsafe or why

    def cd_to(word: Optional[List[tuple]]) -> None:
        """Where the ``cd`` being read may take the shell (``None``: no operand)."""
        where = _cd_target(word, checkout, st.cwd.moved, st.command)
        if where[0] == "above":
            unsafe("it changes into a directory above the checkout")
        elif where[0] == "unknown":
            unsafe("it changes into a directory this guard cannot know")
        elif where[0] == "in":
            st.cwd.enter(where[1])
            norm = _lexical(where[1])
            if norm is not None:
                # ``cd`` resolves ``..`` by name, not through a symlink before it:
                # the directory it names that way is a place too.
                if norm in ("", ".") or norm == ".." or norm.startswith("../"):
                    unsafe("it names the checkout root" if norm in ("", ".")
                           else "it names a path outside the checkout")
                else:
                    st.cwd.enter(_literal(norm))
                    for place, bare in st.cwd.places(_literal(norm)):
                        _scan_word(place, named, checkout, False, bare)
        else:
            st.cwd.enter(None)

    def inline(word: List[tuple], module: bool) -> None:
        """An interpreter's inline code (``-c`` / ``-e`` …), or Python's ``-m`` module."""
        family = _family(st.interp)
        _scan_code(_text(word), named, checkout, depth, st.cwd, scripts=family in ("node", "?"))
        if family in ("python", "?") and not st.isolated:
            # ``-c`` / ``-m``: the checkout root is first on the program's path.
            named.imports_root = True
            code = _text(word)
            named.root_imports.update({code.split(".", 1)[0]} if module else _python_imports(code))
        # A code option this guard mistook (``perl -p x.pl``): the word may be the
        # script, so it is read as one too.
        for place, bare in st.cwd.places(word):
            _scan_whole(place, named, checkout, True, bare)

    def command_ended() -> None:
        if st.command in _CD_COMMANDS and not st.cd_operand:
            cd_to(None)
        if st.script_next and _family(st.interp) in ("python", "?") and not st.isolated:
            # No script: the program comes from stdin (``python3 - <<EOF``,
            # ``python3 <<< '…'``), with the checkout root first on its path.
            named.imports_root = True

    for word in words:
        if not word:  # ``""``: an empty argument names nothing
            if st.command in _CD_COMMANDS and not st.at_start:
                st.cd_operand = True  # ``cd ""`` stays where it is (or fails)
            continue
        if word[0][0] == "end":  # one simple command ended
            command_ended()
            st.boundary(word[0][1])
            continue
        if word[0][0] == "redir":
            st.redirect_next = True
            continue
        if st.redirect_next:
            # A redirect's file (``> out.log``, ``< in.txt``): a path, never an
            # operand or a command — or a here-string, the program a shell or an
            # interpreter reads (``bash <<< 'cd x && ./y'``).
            st.redirect_next = False
            literal = "".join(it[1] for it in word if it[0] == "c")
            nested = any(ch in _NESTED for ch in literal) or _LATENT_EXPANSION.search(literal)
            if nested:
                _scan_command(_text(word), named, checkout, depth + 1, strict=False, cwd=st.cwd)
            for place, bare in st.cwd.places(word):
                if nested:
                    _scan_whole(place, named, checkout, False, bare)
                else:
                    _scan_word(place, named, checkout, False, bare)
            continue
        text = _chars(word)
        name = (text or "").rsplit("/", 1)[-1]
        if st.case == "subject":
            st.case = "in"
        elif st.case == "in" and text == "in":
            st.case, st.pattern_seen = "patterns", False
            continue
        elif st.case == "patterns":
            if text == "esac":
                st.case = None
            else:
                st.pattern_seen = True
                _check_pattern(word)
            continue  # ``case "$f" in *.py)``: a pattern, not a path
        elif st.case == "arm" and st.at_start and text == "esac":
            st.case = None
            continue
        if text == "case" and st.at_start:
            st.case, st.at_start = "subject", False
            continue
        if text == "[[":
            st.test = True
            continue
        if text == "]]":
            st.test = False
            continue
        if st.test and text in ("==", "!=", "=~", "="):
            st.pattern_next = True
            continue
        if st.pattern_next:
            st.pattern_next = False
            _check_pattern(word)
            continue  # ``[[ $f == *.py ]]``: a pattern, not a path
        if text in _SYNTAX_WORDS:
            if text == "{}" and st.command in _CD_COMMANDS and not st.at_start:
                st.cd_operand = True  # ``xargs -I{} sh -c 'cd {} …'``
                unsafe("it changes into a directory this guard cannot know")
            continue  # ``[ -f x ]``, ``{ …; }``: shell syntax, not a glob of the root
        script: object = False
        operand = False  # the operand of a ``cd``, which takes effect after it is read
        if st.at_start:
            if text == ".":
                st.at_start = False
                continue  # the ``.`` builtin: the word after it is the file it reads
            if _is_assignment(word) or name in _PREFIX_COMMANDS:
                pass
            else:
                st.at_start = False
                st.command = name
                lead = _classify_expansion(word[0][1]) if word[0][0] == "x" else None
                if (lead is not None and lead[0] == "opaque") or _SCRIPT_DIR_INTERPRETERS.fullmatch(name):
                    st.script_next = True  # an interpreter, or one named by a variable
                    st.interp = name if lead is None else "?"
                elif lead is not None or "/" in (text or "") or (text or "").startswith(("~", ".")):
                    script = "command"      # a path run directly: its shebang decides
        elif st.command in _CD_COMMANDS:
            if text == "--" and not st.cd_operands:
                st.cd_operands = True
            elif st.cd_operands or text is None or not text.startswith("-") or _STACK_ARG.fullmatch(text):
                st.cd_operand = operand = True
        elif st.arg_next:
            # Under an interpreter named by a variable, the option may be one that
            # takes no argument (Python's ``-I``, Ruby's ``-W``): this word may be the
            # script.
            st.arg_next, script = False, st.arg_script
        elif st.code_next:
            module = st.code_next == "-m"
            st.code_next = False
            inline(word, module)
            continue
        elif text is not None and _SCRIPT_DIR_INTERPRETERS.fullmatch(name):
            st.script_next = True           # ``timeout 30 python3 x.py``, ``env -i node x.js``
            st.interp = name
        elif text == "run" and st.last in _RUNNERS:
            st.script_next = True           # ``uv run x.py``
            st.interp = "?"
        elif st.script_next and _lead(word, len(word)).startswith("-"):
            family = _family(st.interp)
            lead = _lead(word, len(word))   # the option, before any expansion glued to it
            flags = _PYTHON_FLAGS.fullmatch(lead) if family in ("python", "?") else None
            if family == "python" and flags and ("I" in flags.group(1) or "P" in flags.group(1)):
                st.isolated = True          # ``-I`` / ``-P``: the root is not on the path
            code = _code_option(lead, family, flags)
            if code is not None:
                st.script_next = False
                module, glued = code
                if text is None and glued is None:
                    glued = len(lead)       # ``-e"$X…"``: code glued on, starting with an expansion
                if glued is None:
                    st.code_next = "-m" if module else "-c"
                else:
                    inline(word[glued:], module)  # ``-c'import x'``, ``-we'require "x"'``
                    continue
            elif text is not None and (text in _ARG_OPTIONS[family] or (
                    flags and flags.group(2) in ("W", "X") and not flags.group(3))):
                st.arg_next, st.arg_script = True, family == "?"
        elif st.script_next:
            script, st.script_next = True, False
        if st.interp and _family(st.interp) in ("node", "?"):
            named.node = True  # it resolves package names above its code (:meth:`_Checker._node_lookups`)
        if script is False and (st.interp or st.scripts) and _script_shaped(word):
            # Any script-shaped operand of an interpreter may be what it runs or
            # preloads, whatever this guard makes of its options (``ruby -W x.rb``,
            # ``node -r ./preload.js x.js``).
            script = True
        st.last = name
        literal = "".join(it[1] for it in word if it[0] == "c")
        nested = (any(ch in _NESTED for ch in literal) or "\\_" in literal
                  or _LATENT_EXPANSION.search(literal))
        if nested:
            # A word holding a whole command (``bash -c "…"``, ``env -S 'a\_b'``), or a
            # quoted expansion an inner shell would expand: scan what it contains —
            # and the word itself as ONE path, since a quoted name may just contain a
            # space (``"lint hook.sh"``).
            _scan_command(_text(word).replace("\\_", " "), named, checkout, depth + 1,
                          strict=False, commands=not _is_assignment(word), cwd=st.cwd)
        for place, bare in st.cwd.places(word):
            if nested:
                _scan_whole(place, named, checkout, script, bare)
            else:
                _scan_word(place, named, checkout, script, bare)
        if operand:
            cd_to(word)
    command_ended()


def _check_pattern(word: List[tuple]) -> None:
    """A ``case`` or ``[[ … == … ]]`` pattern is matched, not opened — but the shell
    expands it first, so a command substitution in it runs."""
    for it in word:
        if it[0] == "x":
            _classify_expansion(it[1])


class _Command:
    """What :func:`_scan_command` knows about the simple command it is reading."""

    def __init__(self, commands: bool, cwd: Optional["_Cwd"] = None):
        self.commands = commands
        self.case: Optional[str] = None
        self.pattern_seen = False
        self.test = False
        self.scripts = False
        self.cwd = cwd.copy() if cwd else _Cwd()
        self._reset()

    def _reset(self) -> None:
        self.at_start = self.commands
        self.command = ""
        self.last = ""
        self.script_next = self.code_next = self.arg_next = self.pattern_next = False
        self.arg_script = False  # the option's argument may be the script
        self.redirect_next = False
        self.cd_operand = self.cd_operands = False  # an operand seen; ``--`` seen
        self.interp = ""        # the interpreter this simple command runs, if any
        self.isolated = False   # Python run with ``-I`` / ``-P``

    def boundary(self, op: str) -> None:
        self._reset()
        if self.case == "patterns":
            # ``a|b)``, ``(a)``, a line break before the pattern: still the pattern,
            # until the ``)`` after it.
            if op == ")" and self.pattern_seen:
                self.case = "arm"
            return
        if op == ";;" and self.case:
            self.case, self.pattern_seen = "patterns", False  # the next arm's pattern follows


class _Cwd:
    """Where a relative word may resolve. Until a ``cd``, only the checkout root.
    After one, the root is still possible — a ``cd`` may fail, be undone (a
    subshell ending, ``popd``, ``cd -``) or change nothing (in a pipeline, behind
    ``env``) — and so is every checkout directory any ``cd`` named (``targets``).
    A ``cd`` only ever adds places to check: nothing is skipped on a guess about
    where the shell is."""

    def __init__(self):
        self.moved = False
        self.targets: List[List[tuple]] = []

    def copy(self) -> "_Cwd":
        other = _Cwd()
        other.moved, other.targets = self.moved, list(self.targets)
        return other

    def enter(self, word: Optional[List[tuple]]) -> None:
        """A ``cd``, to the checkout directory ``word`` names (relative to any place
        the shell may be), or to one this guard need not follow (``None``)."""
        self.moved = True
        if word is None:
            return
        if _is_relative(word):
            new = [_literal("./") + word] + [t + [("c", "/", False)] + word for t in self.targets]
        else:
            new = [list(word)]
        self.targets += [t for t in new if t not in self.targets]
        if len(self.targets) > _MAX_CD_TARGETS:
            raise _Unparseable("it changes directory too often to follow")

    def places(self, word: List[tuple]) -> List[Tuple[List[tuple], bool]]:
        """``word`` as a path from each place it may be relative to, each with
        whether it counts only where it exists. A word that cannot climb is read
        from the root only, counting where it exists: inside a ``cd`` directory it
        stays inside it, and the ``cd``'s own operand named that directory, whose
        walk covers it. A word that can climb (:func:`_may_climb`) is read exactly
        from the root and from every ``cd`` directory."""
        if not self.moved or not _is_relative(word):
            return [(word, False)]
        if not _may_climb(word):
            return [(word, True)]
        return [(word, False)] + [(t + [("c", "/", False)] + word, False) for t in self.targets]


def _may_climb(word: List[tuple]) -> bool:
    """Whether a relative word may reach above where it starts: a ``..``
    component, a glob component that can match ``..`` (``.*``, ``.?``, ``[.].``),
    or an expansion, which could hold either."""
    for comp in _components(word):
        if any(it[0] != "c" for it in comp) or _chars(comp) == "..":
            return True
        if comp and comp[0][1] in ".[" and any(it[1] in _GLOB and it[2] for it in comp):
            return True
    return False


def _lexical(word: List[tuple]) -> Optional[str]:
    """A literal checkout path with a ``..`` in it, resolved by name as ``cd`` does
    (``link/../c`` is ``c``, whatever ``link`` points at); None otherwise."""
    items = word[1:] if word and word[0][0] == "x" else word
    text = _chars(items)
    if text is None or ".." not in text.split("/"):
        return None
    return os.path.normpath(text.lstrip("/") or ".")


def _cd_target(word: Optional[List[tuple]], checkout: Optional[str], moved: bool,
               command: str) -> tuple:
    """Where ``command word`` (``cd`` / ``pushd`` / ``popd``) may take the shell:
    ``("in", word)`` for a checkout directory, ``("back",)`` for a place it already
    was, ``("outside",)``, ``("above",)`` for a directory the checkout is in, or
    ``("unknown",)``."""
    if command == "popd" or (word is None and command == "pushd"):
        return ("back",)  # a directory from the stack
    if word is None:
        return _cd_target(_literal(os.environ.get("HOME", "~")), checkout, moved, command)
    text = _chars(word)
    if text is not None and _STACK_ARG.fullmatch(text):
        # ``cd -`` / ``pushd +1``: a directory this command line already visited —
        # or, before any, one inherited from the environment.
        return ("back",) if moved else ("unknown",)
    first = word[0]
    if first[0] == "x":
        try:
            lead = _classify_expansion(first[1])
        except _Unparseable:
            return ("unknown",)
        if lead is _ROOT:
            return ("in", list(word))
        if lead is not _HOME or _chars(word[1:]) is None:
            return ("unknown",)
        text = os.environ.get("HOME", "") + _chars(word[1:])
    elif _is_relative(word):
        return ("in", list(word))
    elif text is None:
        return ("unknown",)
    if text.startswith("~"):
        tilde = text.split("/", 1)[0]
        if tilde in ("~+", "~+0", "~0"):
            return ("in", list(word))
        if tilde != "~":
            return ("back",) if moved and tilde in ("~-", "~-0") else ("unknown",)
        text = os.path.expanduser(text)
    if not checkout or not text.startswith("/"):
        return ("unknown",)
    if _below_checkout(text, checkout) is not None:
        return ("in", _literal(text))
    target = os.path.realpath(text)
    if target == "/" or _canonical(checkout).startswith(target.rstrip("/") + "/"):
        return ("above",)
    return ("outside",)


def _is_relative(word: List[tuple]) -> bool:
    """A word naming a path relative to the current directory."""
    first = word[0]
    return first[0] == "c" and first[1] not in "/~-$@"


def _family(interp: str) -> str:
    if not interp or interp == "?":
        return "?"
    if _PYTHON.fullmatch(interp):
        return "python"
    return interp if interp in ("ruby", "perl") else "node"


def _code_option(text: str, family: str, flags) -> Optional[Tuple[bool, Optional[int]]]:
    """Whether an interpreter option word starts inline code (``-c``, ``-e`` …) or
    Python's ``-m``, alone or ending a cluster: ``(module, glued)``, where ``glued``
    is where code glued to the option starts (``-c'x'``), or None when the code is
    the next word. None when the word is not a code option."""
    if text in _CODE_OPTIONS[family]:
        return text == "-m", None
    if flags and flags.group(2) in ("c", "m"):
        return flags.group(2) == "m", (len(text) - len(flags.group(3))) if flags.group(3) else None
    for name, cluster in _CODE_CLUSTERS.items():
        m = cluster.fullmatch(text) if family in (name, "?") else None
        if m:
            return False, m.start(1) if m.group(1) else None
    return None


def _python_imports(code: str) -> Set[str]:
    """The top-level module names Python code imports by name."""
    names = set()
    for m in _PY_IMPORT.finditer(code):
        for module in (m.group(1) or m.group(2)).split(","):
            head = module.split()[0].split(".", 1)[0] if module.strip() else ""
            if head:
                names.add(head)
    return names


def _script_shaped(word: List[tuple]) -> bool:
    last = "".join(it[1] for it in word if it[0] == "c").rsplit("/", 1)[-1]
    return last.endswith(_CODE_SUFFIXES)


def _scan_code(code: str, named: _Named, checkout: Optional[str], depth: int,
               cwd: Optional[_Cwd] = None, scripts: bool = False) -> None:
    """A non-shell interpreter's ``-c`` / ``-e`` code: its string literals may name
    files (``open('tools/x.py')``), and one may be a whole command line a shell
    will run (``os.system('sh tools/x.sh')``), so each is read both ways. The
    rest is not shell and is not read as such. ``$`` expansions in a literal are
    the shell's, done before the interpreter runs. A literal a loader is handed
    (:data:`_LOADER`) is code whose directory is a dependency, with or without a
    suffix; with ``scripts`` (JavaScript, whose ``require('./x.js')`` loads x.js's
    own siblings), so is any script-shaped literal. A literal starting with ``/``
    may be joined onto the checkout's own path by the code, so it is read as a
    checkout path too."""
    loaded = {m.end() for m in _LOADER.finditer(code)}
    for m in _CODE_LITERAL.finditer(code):
        body = m.group(2)
        if not body:
            continue
        shaped = body.endswith(_CODE_SUFFIXES)
        # A loader's file without a suffix is a module (``require('./x')`` loads x.js).
        script = (shaped or "module") if m.start() in loaded else scripts and shaped
        if not ("$" in body or "`" in body or "~" in body):
            _scan_literal(body, named, checkout, script)
            for tail in {body, body.rsplit("}", 1)[-1]}:
                if tail.startswith("/"):
                    # ``process.env.CLAUDE_PROJECT_DIR + '/x.js'``, ``f"{root}/x.py"``:
                    # code may join the checkout's own path onto it, so it is a
                    # checkout path too, where one exists.
                    _scan_relative(_literal(tail.lstrip("/")), named, bare=True, script=script)
        elif script:
            # ``require('$CLAUDE_PROJECT_DIR/x.js')``: the shell expanded the path
            # before the interpreter ran, so it resolves as the shell's word would.
            _scan_word(_code_path(body), named, checkout, script)
        if script != "module":  # a module's path is not a command line
            _scan_command(body, named, checkout, depth + 1, strict=False, cwd=cwd, scripts=scripts)


def _code_path(text: str) -> List[tuple]:
    """A path in interpreter code, its shell expansions kept whole: literal
    characters, which no shell globs, and ``$…`` / backtick items."""
    items: List[tuple] = []
    i = 0
    while i < len(text):
        if text[i] in "$`":
            j = _expansion_end(text, i)
            items.append(("x", text[i:j]))
            i = j
        else:
            items.append(("c", text[i], False))
            i += 1
    return items


def _is_assignment(word: List[tuple]) -> bool:
    for i, it in enumerate(word):
        if it[0] == "c" and it[1] == "=":
            name = _chars(word[:i]) or ""
            return bool(_NAME_RE.fullmatch(name[:-1] if name.endswith("+") else name))
    return False


def _classify(word: List[tuple]) -> List[tuple]:
    return [_classify_expansion(it[1]) if it[0] == "x" else it for it in word]


def _scan_whole(word: List[tuple], named: _Named, checkout: Optional[str], script: object,
                if_present: bool = False) -> None:
    """Best effort: a quoted word as one path."""
    try:
        items = _classify(word)
        if any(it is _ROOT for it in items[1:]):
            return
        _scan_piece(items, named, checkout, script, if_present)
    except _Unparseable:
        pass


def _scan_word(word: List[tuple], named: _Named, checkout: Optional[str],
               script: object = False, if_present: bool = False) -> None:
    items = _classify(word)
    active = "".join(it[1] if it[0] == "c" and it[2] else "\0" for it in items)
    brace = _BRACE_EXPANSION.search(active)
    if brace is None:
        # A ``{`` that is not a brace expansion (``awk '{print $1}'``) is literal.
        items = [("c", it[1], False) if it[0] == "c" and it[1] in "{}" else it for it in items]
    elif "." in brace.group(0) or "/" in brace.group(0):
        named.unsafe = named.unsafe or "it expands to paths this guard cannot follow"
        return
    for i, it in enumerate(items):
        if it[0] == "c" and it[1] == "=":
            name = _chars(items[:i]) or ""
            append = name.endswith("+")
            name = name[:-1] if append else name
            value = items[i + 1:]
            # ``X+=v`` appends to X's current value, which may be empty.
            if _NAME_RE.fullmatch(name) and _names_cwd(name, ([_opaque(name)] + value) if append else value):
                named.unsafe = named.unsafe or "it names the checkout root"
            break
    pieces: List[List[tuple]] = [[]]
    for it in items:
        if it[0] == "c" and it[1] in _PIECE_SEPARATORS:
            pieces.append([])
        else:
            pieces[-1].append(it)
    candidates = [p for p in pieces if p]
    if len(pieces) > 1 and _chars(items) is not None:
        candidates.append(items)  # a path whose own name contains a separator
    for piece in candidates:
        _scan_piece(piece, named, checkout, script if len(candidates) == 1 else False, if_present)


def _scan_piece(piece: List[tuple], named: _Named, checkout: Optional[str],
                script: object = False, if_present: bool = False) -> None:
    """One path-shaped piece of a word. ``if_present``: the piece is read from the
    checkout root only because a ``cd`` may not have left it, so a relative name
    counts only where it exists there (``./x`` is ``x``) — though the root itself,
    or a climb out of it, is still unsafe."""
    if any(it is _ROOT for it in piece[1:]):
        raise _Unparseable("the project directory appears mid-word")
    first = piece[0]
    if first is _ROOT:
        rest = piece[1:]
        if rest and _lead(rest, 1) != "/":
            raise _Unparseable("the project directory is glued to other text")
        _scan_relative(rest, named, bare=False, script=script)
        return
    if first is _HOME:
        rest = _chars(piece[1:])
        home = os.environ.get("HOME")
        if rest is not None and home and (not rest or rest.startswith("/")):
            _scan_absolute(home + rest, named, checkout, script)
        return
    if first[0] == "opaque" or (first is _HOME and _chars(piece[1:]) is None):
        if any(_chars(c) == ".." for c in _components(piece[1:])):
            raise _Unparseable("it climbs out of a directory a variable names")
        return  # "$CLAUDE_FILE_PATHS", $1 …: not a checkout path this guard can name
    lead = _lead(piece, len(piece))
    if lead.startswith("~"):
        tilde = lead.split("/", 1)[0]
        if tilde in ("~+", "~+0", "~0"):  # the shell's $PWD
            _scan_relative(piece[len(tilde):], named, bare=False, script=script)
        elif tilde != "~" and _TILDE_STACK.fullmatch(tilde):
            raise _Unparseable(f"{tilde} names a directory from the directory stack")
        elif _chars(piece) is not None:
            _scan_absolute(os.path.expanduser(_chars(piece)), named, checkout, script)
        return
    if lead[:1] == "-":
        # ``-r./x``, ``-wI./lib``: a value glued to a short option, or to the last of
        # a cluster of them — every place the value could start is tried.
        k = 2
        while k < len(piece) and lead[k - 1:k].isalpha():
            _scan_piece(piece[k:], named, checkout, if_present=if_present)
            k += 1
        return
    if lead[:1] == "@":
        if len(piece) > 1:
            _scan_piece(piece[1:], named, checkout, if_present=if_present)  # ``@args.txt``
        return
    if lead[:1] == "/":
        if _chars(piece) is not None:
            _scan_absolute(_chars(piece), named, checkout, script)
        return
    whole = _chars(piece)
    if if_present:
        rest = piece
        while _lead(rest, 2) == "./":
            rest = rest[2:]
        if rest and _chars(rest) != "." and _lead(rest, 3)[:2] != "..":
            _scan_relative(rest, named, bare=True, script=script)
            return
    if (whole in (".", "..") or lead[:2] == "./" or lead[:3] == "../"
            or any(_chars(c) == ".." for c in _components(piece))):
        # Anchored, or climbing (``build/../configure``): exact, even where its
        # first component does not exist yet — a hook may create it.
        _scan_relative(piece, named, bare=False, script=script)
        return
    _scan_relative(piece, named, bare=True, script=script)


def _scan_literal(text: str, named: _Named, checkout: Optional[str], script: object = False) -> None:
    """A path taken literally, with no shell reading it (an ``env`` value): ``~``
    and ``$X`` are just characters of a relative name."""
    if text.startswith("/"):
        _scan_absolute(text, named, checkout, script)
    elif text in (".", "..") or text.startswith("./") or ".." in text.split("/"):
        _scan_relative(_literal(text), named, bare=False, script=script)
    else:
        _scan_relative(_literal(text), named, bare=True, script=script)


def _scan_absolute(text: str, named: _Named, checkout: Optional[str], script: object) -> None:
    if not checkout or not os.path.isabs(text):
        return
    below = _below_checkout(text, checkout)
    if below is not None:  # otherwise an absolute path outside the checkout
        _scan_relative(_literal(below), named, bare=False, script=script)


def _is_search_path(name: str) -> bool:
    """A variable holding a ``:``-separated list of directories to search."""
    return name.endswith(("PATH", "LIB", "DIRS")) or name.startswith(("LD_", "DYLD_"))


def _names_cwd(name: str, value) -> bool:
    """A search-path value (``PATH``, ``PYTHONPATH``, ``NODE_PATH`` …) with an element
    that is — or may be — empty: an empty element means the current directory,
    which for a hook is the checkout root. An element that is just a variable other
    than ``PATH`` itself counts as possibly empty (``PYTHONPATH=$PYTHONPATH:x`` with
    ``PYTHONPATH`` unset). Other variables (``RUST_LOG=a::b``, ``DISPLAY=:0``) are
    not search paths. ``value`` is a string or a word's classified items."""
    if not _is_search_path(name):
        return False
    if isinstance(value, str):
        elements = [_literal(e) if not re.fullmatch(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", e)
                    else [_opaque(e.strip("${}"))] for e in value.split(":")]
    else:
        elements = [[]]
        for it in value:
            if it[0] == "c" and it[1] == ":":
                elements.append([])
            else:
                elements[-1].append(it)
    if len(elements) > 1:
        return any(not e or (len(e) == 1 and e[0][0] == "opaque" and e[0][1] != "PATH")
                   for e in elements)
    return name.endswith("PATH") and not elements[0]


def _below_checkout(path: str, checkout: str) -> Optional[str]:
    """The part of absolute ``path`` below the checkout (``""`` for the checkout
    itself), or None when it is outside. Matched by name, then by where each of
    its leading directories really is — so an alias (another letter case on a
    case-insensitive filesystem, a symlink from outside into the checkout or one
    of its subdirectories) is still recognised."""
    for root in (os.path.abspath(checkout), os.path.realpath(checkout)):
        if path == root or path.startswith(root + "/"):
            return path[len(root):]
    target = _canonical(checkout)
    parts = path.split("/")
    for i in range(2, len(parts) + 1):
        prefix = "/".join(parts[:i])
        if not os.path.exists(prefix):
            return None
        canon = _canonical(prefix)
        if canon == target or canon.startswith(target + "/"):
            rest = "/".join(parts[i:])
            return canon[len(target):] + ("/" + rest if rest else "")
    return None


def _components(items: List[tuple]) -> List[List[tuple]]:
    components: List[List[tuple]] = [[]]
    for it in items:
        if it[0] == "c" and it[1] == "/":
            components.append([])
        else:
            components[-1].append(it)
    return components


def _scan_relative(items: List[tuple], named: _Named, *, bare: bool, script: object = False) -> None:
    """Resolve a checkout-relative path (``items`` may start with ``/`` or
    ``./``) into an exact path, or the directory a glob/variable reaches into. A
    ``script`` also names its directory."""
    components = _components(items)
    # The components as written: a ``..`` is resolved later, on disk, where it can be
    # checked that the component before it is a real directory and not a symlink.
    kept: List[str] = []
    depth = 0  # lexical depth, to spot the root itself and a path above it
    dynamic = None
    for idx, comp in enumerate(components):
        if not comp:
            continue
        if any(it[0] != "c" for it in comp):
            dynamic = "a variable"
        elif any(it[1] in _GLOB and it[2] for it in comp):
            dynamic = "a glob"
            if comp[0][1] in ".[" and idx < len(components) - 1:
                # ``.?`` / ``.*`` / ``[.].`` can match ``..`` itself
                named.unsafe = named.unsafe or "it climbs back out of a directory it expands"
                return
        if dynamic:
            if any(_chars(c) == ".." for c in components[idx + 1:]):
                named.unsafe = named.unsafe or "it climbs back out of a directory it expands"
                return
            break
        name = _chars(comp)
        if name == ".":
            continue
        depth += -1 if name == ".." else 1
        if depth < 0:
            named.unsafe = named.unsafe or "it names a path outside the checkout"
            return
        kept.append(name)
    if depth == 0:
        if bare and not dynamic and not kept:
            return
        named.unsafe = named.unsafe or (
            "it names the checkout root" if dynamic != "a variable"
            else "it names a checkout path built from a variable")
        return
    rel = "/".join(kept)
    if script != "module" or dynamic:
        # A module a loader names need not exist as written (``require('./x')``
        # loads ``x.js``): its directory, below, is what it depends on.
        if bare:
            (named.bare_trees if dynamic else named.bare).add(rel)
        else:
            (named.trees if dynamic else named.paths).add(rel)
    if (script == "command" or not script) and not dynamic:
        # Anything named may be run directly, wherever it stands (``timeout 30
        # ./check.py``): an interpreter its shebang names imports from its directory.
        named.commands.add((rel, bare))
    elif script and not dynamic:
        parent = kept[:-1]
        if depth > 1:
            (named.bare_trees if bare else named.trees).add("/".join(parent))
        elif bare:
            named.bare_root_scripts.update(
                [rel] + [rel + s for s in _CODE_SUFFIXES] if script == "module" else [rel])
        else:
            named.unsafe = named.unsafe or "its interpreter imports from the checkout root"


# ── is a named path byte-identical to base? ──────────────────────────────────────

@dataclass
class _Entry:
    mode: str
    kind: str
    sha: str


class _Base:
    """Read-only view of the base commit, relative to the checkout."""

    def __init__(self, checkout: str, commit: str):
        self.checkout = checkout
        self.commit = commit
        self.algo = "sha256" if len(commit) == 64 else "sha1"
        self._prefix: Optional[str] = None
        self._listings: Dict[str, Dict[str, _Entry]] = {}
        self._blobs: Dict[str, bytes] = {}

    def _git(self, *args: str) -> bytes:
        env = dict(os.environ, GIT_LITERAL_PATHSPECS="1", GIT_OPTIONAL_LOCKS="0")
        r = subprocess.run(["git", *args], cwd=self.checkout, capture_output=True,
                           timeout=_GIT_TIMEOUT, stdin=subprocess.DEVNULL, env=env)
        if r.returncode != 0:
            raise OSError(f"git {args[0]} failed: {os.fsdecode(r.stderr).strip()[:200]}")
        return r.stdout

    def prefix(self) -> str:
        if self._prefix is None:
            self._prefix = os.fsdecode(self._git("rev-parse", "--show-prefix")).strip()
        return self._prefix

    def _entries(self, *args: str) -> Dict[str, _Entry]:
        prefix = self.prefix()
        found: Dict[str, _Entry] = {}
        for rec in self._git("ls-tree", "-z", "--full-tree", *args).split(b"\0"):
            if not rec:
                continue
            meta, _, raw = rec.partition(b"\t")
            mode, kind, sha = os.fsdecode(meta).split()
            full = os.fsdecode(raw)
            if prefix and not full.startswith(prefix):
                continue
            found[full[len(prefix):]] = _Entry(mode, kind, sha)
        return found

    def listing(self, rel: str) -> Dict[str, _Entry]:
        """Every base entry on the way to ``rel``, at it, and below it — one
        ``git ls-tree -r -t`` — keyed by checkout-relative path."""
        if rel not in self._listings:
            target = self.prefix() + rel
            self._listings[rel] = self._entries(
                "-r", "-t", self.commit, *(("--", target) if target else ()))
        return self._listings[rel]

    def top(self) -> Set[str]:
        """The names directly in the checkout's directory at base."""
        if "\0top" not in self._listings:
            prefix = self.prefix()
            self._listings["\0top"] = self._entries(self.commit, *(("--", prefix) if prefix else ()))
        return set(self._listings["\0top"])

    def entry(self, rel: str) -> Optional[_Entry]:
        """The base entry at exactly ``rel`` (one non-recursive ``ls-tree``)."""
        key = "\0entry " + rel
        if key not in self._listings:
            self._listings[key] = self._entries(self.commit, "--", self.prefix() + rel)
        return self._listings[key].get(rel)

    def blob(self, sha: str) -> bytes:
        if sha not in self._blobs:
            self._blobs[sha] = self._git("cat-file", "blob", sha)
        return self._blobs[sha]

    def blob_id(self, data: bytes) -> str:
        h = hashlib.new(self.algo)
        h.update(b"blob %d\0" % len(data))
        h.update(data)
        return h.hexdigest()


_REGULAR_MODES = ("100644", "100755")


def _is_cache(path: str) -> bool:
    parts = path.split("/")
    return (parts[-1] == ".DS_Store"
            or (len(parts) > 1 and parts[-2] == "__pycache__" and parts[-1].endswith(".pyc")))


def _in_git_dir(rel: str) -> bool:
    parts = rel.split("/")
    return parts[0] == ".git" and ".." not in parts


class _Checker:
    """Decides whether every path a base-equal value names is byte-identical to
    base. Every failure to decide — a missing path, a git error, a walk that is
    too big — answers "not identical"."""

    def __init__(self, checkout: str, base: _Base):
        self.checkout = checkout
        self.base = base

    def unchanged(self, key: str, value) -> Tuple[bool, str]:
        named = named_paths(value, self.checkout, key=key)
        if named.unsafe:
            return False, named.unsafe
        try:
            def exists(rel: str) -> bool:
                # A relative word is a checkout path only when its first component
                # exists on disk or at base; otherwise it is a plain word (``npm``).
                first = rel.split("/", 1)[0]
                return (os.path.lexists(os.path.join(self.checkout, first))
                        or first in self.base.top())

            if any(exists(r) for r in named.bare_root_scripts):
                return False, "its interpreter imports from the checkout root"
            if named.imports_root:
                changed = self._root_imports(named.root_imports)
                if changed:
                    return False, (f"its interpreter imports from the checkout root, where "
                                   f"{changed} differs from the base branch")
            exact = named.paths | {r for r in named.bare if exists(r)}
            trees = named.trees | {r for r in named.bare_trees if exists(r)}
            node = named.node
            for rel, bare in named.commands:
                # A script run directly: an interpreter named by its shebang imports
                # from the script's directory, exactly as ``python3 x.py`` would.
                interp = self._shebang_imports(rel) if not bare or exists(rel) else ""
                if interp:
                    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
                    if not parent:
                        return False, "its interpreter imports from the checkout root"
                    trees.add(parent)
                    node = node or _family(interp) == "node"
            # The repository's own .git is not checkout content a PR can change —
            # unless the path climbs back out of it (``.git/../x.sh``).
            exact = sorted(r for r in exact if not _in_git_dir(r))
            trees = sorted(r for r in trees if not _in_git_dir(r))
            for rel in exact:
                if not self._same(rel, 0):
                    return False, f"it names {rel}, which differs from the base branch"
            for rel in trees:
                if not self._same(rel, 0):
                    return False, f"it reaches into {rel}/, which differs from the base branch"
            if node:
                if self.base.prefix():
                    return False, ("its Node program resolves packages through directories "
                                   "above the checkout, which this guard cannot compare")
                changed = self._node_lookups(exact, trees)
                if changed:
                    return False, (f"its Node program resolves packages through {changed}, "
                                   f"which differs from the base branch")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return False, f"its files could not be compared with the base branch ({exc})"
        return True, ""

    def _root_imports(self, names: Set[str]) -> Optional[str]:
        """What a Python program with the checkout root first on its path may import
        from it: every module file there, every package's ``__init__``, and the
        whole of each top-level name the program imports. The first that differs
        from base, or None."""
        for entry in sorted(os.listdir(self.checkout)):
            stem = entry.split(".", 1)[0]
            if entry == ".git" or not stem.isidentifier():
                continue
            if entry in names or stem in names:
                if not self._same(entry, 0):
                    return entry
            elif os.path.isdir(os.path.join(self.checkout, entry)):
                for inner in sorted(os.listdir(os.path.join(self.checkout, entry))):
                    if inner.startswith("__init__.") and not self._same(f"{entry}/{inner}", 0):
                        return f"{entry}/{inner}"
            elif entry.endswith(_PY_MODULE_SUFFIXES) and not self._same(entry, 0):
                return entry
        return None

    def _node_lookups(self, exact: List[str], trees: List[str]) -> Optional[str]:
        """Where a Node-family program resolves a package name its code requires
        (``require('prettier')``, ``-r pkg``, ``import 'pkg'``): ``node_modules/`` in
        every directory from the code's own up to the checkout root, and the nearest
        ``package.json`` (its ``imports``, and its ``exports`` under its own
        ``name``). The code is in, or run from, a directory the value names — a
        script's, a ``cd``'s, the root — so each ``node_modules`` and
        ``package.json`` beside one of the named paths' ancestors must be base's, or
        absent both here and at base. A directory a tree already covers is skipped
        (unless a ``..`` in it may lead elsewhere). The first that differs, or None."""
        dirs = {""}
        for rel in exact + trees:
            # As written (a ``..`` the kernel resolves, through a symlink too) and by name.
            for parts in {tuple(rel.split("/")), tuple(os.path.normpath(rel).split("/"))}:
                dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
        covered = tuple(t + "/" for t in trees)
        top = self.base.top()
        for d in sorted(dirs):
            if d and ".." not in d.split("/") and (d + "/").startswith(covered):
                continue
            for name in ("node_modules", "package.json"):
                rel = f"{d}/{name}" if d else name
                if os.path.lexists(os.path.join(self.checkout, rel)):
                    if not self._same(rel, 0):
                        return rel
                elif (name in top) if not d else (self.base.entry(rel) is not None):
                    return rel  # base has it; the checkout does not
        return None

    def _shebang_imports(self, rel: str) -> str:
        """The interpreter the file at ``rel`` names in a ``#!`` line (through ``env``
        or ``env -S`` too), when it imports from the script's directory; ``""``
        otherwise."""
        try:
            # Never waits for a writer: a FIFO a run left at a named path is not run.
            fd = os.open(os.path.join(self.checkout, rel),
                         os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
        except OSError:
            return ""
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return ""  # a directory (``'docs/'`` in -c code) is walked, not run
            head = os.read(fd, 256)
        finally:
            os.close(fd)
        if not head.startswith(b"#!"):
            return ""
        words = os.fsdecode(head[2:].split(b"\n", 1)[0]).split()
        names = [w.rsplit("/", 1)[-1] for w in words if not w.startswith("-")]
        if names and names[0] == "env":
            names = names[1:]
        return names[0] if names and _SCRIPT_DIR_INTERPRETERS.fullmatch(names[0]) else ""

    def _same(self, rel: str, hops: int) -> bool:
        return self._walk([], [p for p in rel.split("/") if p not in ("", ".")], hops)

    def _walk(self, done: List[str], rest: List[str], hops: int) -> bool:
        """Follow ``rest`` from the verified directory ``done`` the way the kernel
        will: component by component, on disk. A ``..`` is taken only after the
        component before it was verified to be a real directory (so it cannot
        climb out of a PR-planted symlink), and a symlink is followed only when base
        holds the identical symlink."""
        while rest:
            part, rest = rest[0], rest[1:]
            if part == "..":
                if not done:
                    return False  # above the checkout
                done = done[:-1]
                continue
            cur = "/".join(done + [part])
            st = _lstat(os.path.join(self.checkout, cur))
            if st is None:
                return False
            be = self.base.entry(cur)
            if stat.S_ISLNK(st.st_mode):
                return self._follow(cur, be, rest, hops)
            if rest:
                if not stat.S_ISDIR(st.st_mode) or be is None or be.kind != "tree":
                    return False
                done = done + [part]
                continue
            if stat.S_ISDIR(st.st_mode):
                return be is not None and be.kind == "tree" and self._same_dir(cur, hops)
            if stat.S_ISREG(st.st_mode):  # a glob "under" a file matches nothing but itself
                return (be is not None and be.kind == "blob" and be.mode in _REGULAR_MODES
                        and st.st_size <= _MAX_WALK_BYTES
                        and self.base.blob_id(_read(os.path.join(self.checkout, cur))) == be.sha)
            return False
        if not done:
            return False  # it resolves to the checkout root, which names the whole tree
        return self._same_dir("/".join(done), hops)

    def _follow(self, cur: str, be: Optional[_Entry], rest: List[str], hops: int) -> bool:
        """A symlink counts as unchanged only when base has the identical symlink —
        and then what it points at must be unchanged too."""
        if be is None or be.mode != "120000" or hops >= _MAX_LINK_HOPS:
            return False
        target = os.readlink(os.path.join(self.checkout, cur))
        if os.path.isabs(target) or self.base.blob(be.sha) != os.fsencode(target):
            return False
        here = cur.split("/")[:-1]
        return self._walk(here, [p for p in target.split("/") if p not in ("", ".")] + rest,
                          hops + 1)

    def _same_dir(self, rel: str, hops: int) -> bool:
        """A directory is unchanged when its COMPLETE on-disk file list — untracked
        and ignored files included, since a spawn can create one — and every
        file's bytes equal base, and every symlink in it is base's identical
        symlink to something itself unchanged."""
        below = rel + "/" if rel else ""
        want = {p: e for p, e in self.base.listing(rel).items()
                if p.startswith(below) and e.kind != "tree"}
        if any(e.kind != "blob" for e in want.values()):
            return False  # a submodule: nothing to compare its content with
        have = self._scan(rel)
        if have is None:
            return False
        extra = [p for p in have if p not in want]
        if extra and all(_is_cache(p) for p in extra):
            # ``__pycache__/*.pyc`` a hook's own run wrote, ``.DS_Store``: dropped
            # only when git does not track them (a PR could commit a crafted .pyc).
            tracked = self.tracked(rel)
            for p in extra:
                if p not in tracked:
                    del have[p]
        if set(have) != set(want):
            return False
        for path, (kind, detail) in sorted(have.items()):
            be = want[path]
            if kind == "file":
                if be.mode not in _REGULAR_MODES:
                    return False
                if self.base.blob_id(_read(os.path.join(self.checkout, path))) != be.sha:
                    return False
            elif not self._follow(path, be, [], hops):
                return False
        return True

    def tracked(self, rel: str) -> Set[str]:
        """The paths git's index tracks under ``rel``."""
        r = subprocess.run(["git", "ls-files", "-z", "--", rel or "."], cwd=self.checkout,
                           capture_output=True, timeout=_GIT_TIMEOUT, stdin=subprocess.DEVNULL,
                           env=dict(os.environ, GIT_LITERAL_PATHSPECS="1"))
        if r.returncode != 0:
            raise OSError("cannot read the git index")
        return {os.fsdecode(p) for p in r.stdout.split(b"\0") if p}

    def _scan(self, rel: str) -> Optional[Dict[str, Tuple[str, object]]]:
        found: Dict[str, Tuple[str, object]] = {}
        stack = [rel]
        total = 0
        while stack:
            d = stack.pop()
            try:
                entries = list(os.scandir(os.path.join(self.checkout, d) if d else self.checkout))
            except OSError:
                return None
            for e in entries:
                if not d and e.name == ".git":
                    continue  # the repository's own metadata is never part of a tree
                path = f"{d}/{e.name}" if d else e.name
                st = e.stat(follow_symlinks=False)
                if stat.S_ISDIR(st.st_mode):
                    stack.append(path)
                    continue
                if stat.S_ISREG(st.st_mode):
                    found[path] = ("file", st.st_size)
                    total += st.st_size
                elif stat.S_ISLNK(st.st_mode):
                    found[path] = ("link", None)
                else:
                    return None
                if len(found) > _MAX_WALK_FILES or total > _MAX_WALK_BYTES:
                    return None
        return found


# ── one scrub: plan, apply, put back ─────────────────────────────────────────────

@dataclass
class _Held:
    """One settings path held back for a window."""
    kind: str                 # "settings.json" | "settings.local.json" | "claude-dir"
    link: bool                # the entry is a symlink (removed for the window)
    original: bytes           # the file's bytes, or the symlink's target
    mode: int = 0
    written: Optional[bytes] = None
    tracked: bool = False     # the index has an entry for it
    git_holds: bool = False   # ... whose blob is exactly these original bytes
    index_sha: str = ""       # that entry's blob, as the guard found it
    dir_mode: int = 0o755     # the mode .claude had
    index_only: bool = False  # the content is back; only the index flag is still owed
    index_set: List[str] = field(default_factory=list)  # index paths THIS guard marked skip-worktree
    reasons: Dict[str, str] = field(default_factory=dict)  # why each key is held back (not journaled)

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "link": self.link,
            "original": base64.b64encode(self.original).decode("ascii"),
            "mode": self.mode,
            "written": None if self.written is None else base64.b64encode(self.written).decode("ascii"),
            "tracked": self.tracked,
            "git_holds": self.git_holds,
            "index_sha": self.index_sha,
            "dir_mode": self.dir_mode,
            "index_only": self.index_only,
            "index_set": list(self.index_set),
        }

    @classmethod
    def from_json(cls, rec) -> "_Held":
        if not isinstance(rec, dict) or rec.get("kind") not in _KINDS:
            raise ValueError("bad entry")
        link, mode, tracked = rec.get("link"), rec.get("mode"), rec.get("tracked")
        index_set, written = rec.get("index_set"), rec.get("written")
        flags = (link, tracked, rec.get("git_holds"), rec.get("index_only"))
        if not all(isinstance(f, bool) for f in flags):
            raise ValueError("bad entry")
        for m in (mode, rec.get("dir_mode")):
            if not isinstance(m, int) or not 0 <= m <= 0o7777:
                raise ValueError("bad entry")
        if not isinstance(rec.get("index_sha"), str):
            raise ValueError("bad entry")
        if not isinstance(index_set, list) or not all(isinstance(p, str) for p in index_set):
            raise ValueError("bad entry")
        if rec["kind"] == _DIR_LINK and not link:
            raise ValueError("bad entry")
        return cls(
            kind=rec["kind"], link=link,
            original=base64.b64decode(str(rec.get("original")), validate=True),
            mode=mode,
            written=None if written is None else base64.b64decode(str(written), validate=True),
            tracked=tracked, git_holds=rec["git_holds"], index_sha=rec["index_sha"],
            dir_mode=rec["dir_mode"], index_only=rec["index_only"], index_set=index_set,
        )


def _rel_of(checkout: str, held: _Held) -> str:
    """How messages name the held path: its on-disk spelling, found without ever
    listing a directory through a symlink."""
    try:
        with _Dir.open(checkout, follow=True) as root:
            d = root.spelled(CLAUDE_DIR)
            if held.kind == _DIR_LINK:
                return d
            try:
                with root.child(CLAUDE_DIR) as claude:
                    return f"{d}/{claude.spelled(held.kind)}"
            except OSError:
                return f"{d}/{held.kind}"
    except OSError:
        return CLAUDE_DIR if held.kind == _DIR_LINK else f"{CLAUDE_DIR}/{held.kind}"


def _plan(checkout: str) -> List[_Held]:
    """Decide, before anything is written, what each settings path must look like
    for the spawn. A file whose every key survives is not touched at all."""
    held: List[_Held] = []
    with _Dir.open(checkout, follow=True) as root:
        st = root.lstat(CLAUDE_DIR)
        if st is None:
            return []
        if stat.S_ISLNK(st.st_mode):
            # Never read or write through it: the link is removed for the window.
            return [_Held(kind=_DIR_LINK, link=True, original=os.fsencode(root.readlink(CLAUDE_DIR)))]
        if not stat.S_ISDIR(st.st_mode):
            return []
        spelled_dir = root.spelled(CLAUDE_DIR)
        checker: Optional[_Checker] = None
        base_known = True
        with root.child(CLAUDE_DIR) as claude:
            for name in SETTINGS_FILES:
                fst = claude.lstat(name)
                if fst is None:
                    continue
                if stat.S_ISLNK(fst.st_mode):
                    held.append(_Held(kind=name, link=True, dir_mode=stat.S_IMODE(st.st_mode),
                                      original=os.fsencode(claude.readlink(name))))
                    continue
                if not stat.S_ISREG(fst.st_mode):
                    continue
                data = claude.read(name)
                if _blank(data):
                    continue
                obj = _parse(data)
                if obj is not None and all(k in INERT_KEYS for k in obj):
                    continue
                kept: dict = {}
                reasons: Dict[str, str] = {}
                if obj is not None:
                    base_obj = None
                    if checker is None and base_known:
                        commit = _base_commit(checkout)
                        base_known = commit is not None
                        if commit is not None:
                            checker = _Checker(checkout, _Base(checkout, commit))
                    if checker is not None:
                        base_obj = _base_copy(checker.base, f"{spelled_dir}/{claude.spelled(name)}")
                    for k, v in obj.items():
                        if k in INERT_KEYS:
                            kept[k] = v
                        elif checker is None:
                            reasons[k] = "no base commit to compare with"
                        elif base_obj is None or k not in base_obj:
                            reasons[k] = "the base branch does not have it"
                        elif _canon(v) != _canon(base_obj[k]):
                            reasons[k] = "it differs from the base branch"
                        else:
                            same, why = checker.unchanged(k, v)
                            if same:
                                kept[k] = v
                            else:
                                reasons[k] = why
                    if len(kept) == len(obj):
                        continue
                held.append(_Held(kind=name, link=False, original=data,
                                  mode=stat.S_IMODE(fst.st_mode), written=_dump(kept),
                                  dir_mode=stat.S_IMODE(st.st_mode), reasons=reasons))
    return held


def _base_copy(base: _Base, rel: str) -> Optional[dict]:
    """The base commit's copy of the settings file at ``rel`` (the on-disk
    spelling), parsed — None when base has no regular file there, or it is not a
    JSON object."""
    try:
        be = base.entry(rel)
        if be is None or be.kind != "blob" or be.mode not in _REGULAR_MODES:
            return None
        return _parse(base.blob(be.sha))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _git_index(checkout: str, args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=checkout, capture_output=True,
                          timeout=_GIT_TIMEOUT, stdin=subprocess.DEVNULL)


def _index_entries(checkout: str, kinds: List[str]) -> Optional[Dict[str, List[Tuple[str, bool, str]]]]:
    """For each kind, the index entries that name it (case-folded): path, whether
    it is marked skip-worktree, and its blob. None when ``checkout`` is not in a
    git work tree. Raises ``OSError`` when the index cannot be read."""
    specs = []
    for kind in kinds:
        rel = CLAUDE_DIR if kind == _DIR_LINK else f"{CLAUDE_DIR}/{kind}"
        specs.append(f":(icase,literal){rel}")
    try:
        r = _git_index(checkout, ["ls-files", "-z", "-s", "-v", "--", *specs])
    except FileNotFoundError:
        return None  # no git at all: nothing is tracked
    except subprocess.SubprocessError as exc:
        raise OSError(f"cannot read the git index ({exc})")
    if r.returncode != 0:
        if b"not a git repository" in r.stderr:
            return None
        raise OSError(f"cannot read the git index ({os.fsdecode(r.stderr).strip()[:200]})")
    found: Dict[str, List[Tuple[str, bool, str]]] = {k: [] for k in kinds}
    for rec in r.stdout.split(b"\0"):
        if not rec:
            continue
        meta, _, raw = rec.partition(b"\t")
        fields = os.fsdecode(meta).split()
        path = os.fsdecode(raw)
        for kind in kinds:
            rel = CLAUDE_DIR if kind == _DIR_LINK else f"{CLAUDE_DIR}/{kind}"
            if path.casefold() == rel.casefold():
                found[kind].append((path, fields[0] in ("S", "s"), fields[2]))
    return found


def _blob_sha(data: bytes, like: str) -> str:
    """Git's blob id for ``data``, in the object format of the id ``like``."""
    h = hashlib.new("sha256" if len(like) == 64 else "sha1")
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def _update_index(checkout: str, flag: str, paths: List[str]) -> None:
    """``git update-index <flag> -- paths``, retried briefly while another git
    process holds the index lock."""
    last = ""
    for attempt in range(_INDEX_LOCK_RETRIES):
        try:
            r = _git_index(checkout, ["update-index", flag, "--", *paths])
        except (subprocess.SubprocessError, OSError) as exc:
            raise OSError(f"git update-index failed ({exc})")
        if r.returncode == 0:
            return
        last = os.fsdecode(r.stderr).strip()[:200]
        if "index.lock" not in last:
            break
        time.sleep(0.2 * (attempt + 1))
    raise OSError(f"git update-index {flag} failed ({last})")


def _write_journal(checkout: str, held: List[_Held], token: str) -> None:
    record = {
        "version": _JOURNAL_VERSION,
        "checkout": _canonical(checkout),
        "token": token,
        "entries": [h.to_json() for h in held],
    }
    with _Dir.open(_private_state_dir()) as state:
        state.write(os.path.basename(_journal_path(checkout)),
                    json.dumps(record).encode("utf-8"), 0o600, secrets.token_hex(8), full=True)


def _drop_journal(checkout: str) -> None:
    path = _journal_path(checkout)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    with contextlib.suppress(OSError), _Dir.open(os.path.dirname(path)) as state:
        state.sync()


def _read_journal(checkout: str) -> Tuple[List[_Held], str]:
    path = _journal_path(checkout)
    try:
        record = json.loads(_read_regular(path).decode("utf-8"))
        if not isinstance(record, dict) or record.get("version") != _JOURNAL_VERSION:
            raise ValueError("unknown journal version")
        if record.get("checkout") != _canonical(checkout):
            raise ValueError("the journal belongs to another checkout")
        token = record.get("token")
        entries = record.get("entries")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{16}", token):
            raise ValueError("bad journal")
        if not isinstance(entries, list):
            raise ValueError("bad journal")
        held = [_Held.from_json(rec) for rec in entries]
        if len({h.kind for h in held}) != len(held):
            raise ValueError("bad journal")
        return held, token
    except (OSError, ValueError, UnicodeDecodeError, TypeError, RecursionError) as exc:
        # This guard's own checks say what was wrong; any other error is named by
        # its type only, since its text could quote the journal's bytes.
        why = str(exc) if type(exc) is ValueError else type(exc).__name__
        raise _Unrecoverable(f"the journal {path} is unreadable ({why}); check "
                             f"{os.path.join(checkout, CLAUDE_DIR)} by hand, then delete it")


def _apply(checkout: str, held: List[_Held], token: str) -> None:
    """Journal first, then mark tracked entries skip-worktree, then write — and
    verify each write by reading it back. Any failure rolls back what was done and
    refuses the spawn."""
    try:
        index = _index_entries(checkout, [h.kind for h in held])
    except OSError as exc:
        raise _refusal(checkout, str(exc))
    for h in held:
        entries = (index or {}).get(h.kind, [])
        shas = {sha for _, _, sha in entries}
        h.tracked = bool(entries)
        h.index_sha = shas.pop() if len(shas) == 1 else ""
        h.git_holds = bool(h.index_sha) and _blob_sha(h.original, h.index_sha) == h.index_sha
        h.index_set = [p for p, skip, _ in entries if not skip]
    try:
        _write_journal(checkout, held, token)
    except OSError as exc:
        raise _refusal(checkout, f"cannot record the original settings durably ({exc})")
    try:
        marks = [p for h in held for p in h.index_set]
        if marks:
            _update_index(checkout, "--skip-worktree", marks)
        with _Dir.open(checkout, follow=True) as root:
            for h in held:
                rel = _rel_of(checkout, h)
                if h.kind == _DIR_LINK:
                    _remove_link(root, CLAUDE_DIR, rel)
                    continue
                # Opened without following: a .claude swapped for a symlink since the
                # plan fails here instead of being written through.
                with root.child(CLAUDE_DIR) as claude:
                    name = claude.spelled(h.kind)
                    if h.link:
                        _remove_link(claude, name, rel)
                        continue
                    try:
                        claude.write(name, h.written, h.mode, token)
                    except OSError as exc:
                        raise OSError(f"cannot rewrite {rel} ({exc.strerror or exc})")
                    st = claude.lstat(name)
                    if st is None or not stat.S_ISREG(st.st_mode) or claude.read(name) != h.written:
                        raise OSError(f"{rel} did not read back as written")
    except OSError as exc:
        problems, unresolved, _ = _put_back(checkout, held, token)
        _settle_journal(checkout, unresolved, token)
        raise _refusal(checkout, str(exc))
    for h in held:
        _note_held(checkout, h)


def _remove_link(d: _Dir, name: str, rel: str) -> None:
    st = d.lstat(name)
    if st is not None and stat.S_ISLNK(st.st_mode):
        d.unlink(name)
    if d.lstat(name) is not None:
        raise OSError(f"{rel} is still present")


def _settle_journal(checkout: str, unresolved: List[_Held], token: str) -> None:
    """Drop the journal when everything was put back; otherwise keep exactly the
    entries still owed, so a later recovery retries only those."""
    if not unresolved:
        try:
            _drop_journal(checkout)
        except OSError as exc:
            # Everything is back; replaying this journal later changes nothing.
            _say(f"the .claude settings in {checkout} are back, but the journal "
                 f"{_journal_path(checkout)} could not be removed ({exc.strerror or exc}); "
                 f"the next claude run there replays it first")
        return
    with contextlib.suppress(OSError):
        _write_journal(checkout, unresolved, token)


_held_noted: Set[Tuple[str, str, Tuple[str, ...]]] = set()
_NOT_AN_OBJECT = "(the whole file: it is not one JSON object)"


def _held_keys(h: _Held) -> List[str]:
    """The keys the guard held back, read off what it wrote."""
    original = _parse(h.original)
    if original is None:
        return [_NOT_AN_OBJECT]
    visible = _parse(h.written or b"") or {}
    return [k for k in original if k not in visible]


def _note_held(checkout: str, h: _Held) -> None:
    rel = _rel_of(checkout, h)
    keys = ("(symlink)",) if h.link else tuple(
        f"{k} ({h.reasons[k]})" if k in h.reasons else k for k in _held_keys(h))
    marker = (checkout, rel, keys)
    if marker in _held_noted:
        return
    _held_noted.add(marker)
    if h.link:
        _say(f"{rel}: this symlink is removed while claude runs in {checkout}, and put back after")
    else:
        _say(f"{rel}: holding back {'; '.join(keys)} while claude runs in {checkout}")


def _put_back(checkout: str, held: List[_Held], token: str
              ) -> Tuple[List[str], List[_Held], Dict[str, str]]:
    """Restore every held path, then the index flags this guard set. Returns the
    problems, the entries still owed, and what happened to each entry by kind
    (``"restored"``, ``"unchanged"``, ``"merged"``, ``"kept"``, ``"saved"``)."""
    say = _say
    problems: List[str] = []
    unresolved: List[_Held] = []
    done: List[_Held] = []
    outcomes: Dict[str, str] = {}
    try:
        index = _index_entries(checkout, [h.kind for h in held])
    except OSError:
        index = None
    try:
        root = _Dir.open(checkout, follow=True)
    except OSError as exc:
        return [f"{checkout}: {exc.strerror or exc}"], list(held), outcomes
    with root:
        for h in held:
            rel = _rel_of(checkout, h)
            try:
                if h.index_only:
                    outcomes[h.kind] = "restored"
                elif index is not None and _index_moved(h, index):
                    # The index no longer holds what it held when the guard held this
                    # file back (a branch switch, a recreated worktree, a ``git rm``, a
                    # pull that now tracks it): this is not that file any more.
                    outcomes[h.kind] = _put_back_moved(checkout, root, h, rel, token,
                                                       index.get(h.kind, []), say)
                elif h.kind == _DIR_LINK:
                    outcomes[h.kind] = _put_back_link(root, CLAUDE_DIR, h, rel, token, say)
                elif h.link:
                    outcomes[h.kind] = _put_back_inner_link(root, h, rel, token, say)
                else:
                    outcomes[h.kind] = _put_back_file(checkout, root, h, rel, token, say)
                done.append(h)
            except OSError as exc:
                problems.append(f"{rel}: {exc.strerror or exc}")
                unresolved.append(h)
    marked = {p for h in done for p in h.index_set}
    if marked:
        try:
            if index is None:
                raise OSError("cannot read the git index")
            clear = sorted({p for entries in index.values() for p, skip, _ in entries
                            if skip and p in marked})
            if clear:
                _update_index(checkout, "--no-skip-worktree", clear)
        except OSError as exc:
            problems.append(f"the git index: {exc}")
            for h in done:
                if h.index_set:
                    h.index_only = True  # the content is settled: only the flag is owed
                    unresolved.append(h)
    return problems, unresolved, outcomes


def _index_moved(h: _Held, index: Dict[str, List[Tuple[str, bool, str]]]) -> bool:
    now = {sha for _, _, sha in index.get(h.kind, [])}
    if h.tracked:
        return bool(h.index_sha) and now != {h.index_sha}
    return bool(now)  # untracked when held back, tracked now


def _put_back_moved(checkout: str, root: _Dir, h: _Held, rel: str, token: str,
                    entries: List[Tuple[str, bool, str]], say) -> str:
    """The index moved under a held path. The original is never written into a
    path git now tracks differently, where the next ``git add -A`` would stage it,
    and it is saved whenever it may exist nowhere else — a staged edit ``git reset``
    unstaged, say. What the guard itself wrote never stays: a file still holding
    the guard's bytes gets back what git now holds, or — when git no longer tracks
    it (``git rm --cached``) — the original, exactly what was on disk before."""
    shas = {sha for _, _, sha in entries}
    how = (f"git no longer tracks {rel}" if not shas
           else f"git now tracks {rel}" if not h.tracked
           else f"git now holds a different {rel}")
    if h.kind == _DIR_LINK or h.link:
        if h.git_holds:
            say(f"{rel}: {how}, so the settings held back from the earlier one were not put back")
            return "kept"
        saved = _save_original(checkout, h, token)
        say(f"{rel}: {how}, so the settings held back from the earlier one were not put back; "
            f"its original was saved to {saved}")
        return "saved"
    cst = root.lstat(CLAUDE_DIR)
    claude = root.child(CLAUDE_DIR) if cst is not None and stat.S_ISDIR(cst.st_mode) else None
    try:
        name = claude.spelled(h.kind) if claude else h.kind
        st = claude.lstat(name) if claude else None
        current = claude.read(name) if st is not None and stat.S_ISREG(st.st_mode) else None
        now: Optional[bytes] = None
        if current is not None and current == h.written:
            if not shas and not h.git_holds:
                _narrow_dir(claude, h.dir_mode)
            if not shas:
                now = h.original  # untracked now: exactly what was on disk before
            elif len(shas) == 1:
                blob = _git_index(checkout, ["cat-file", "blob", next(iter(shas))])
                if blob.returncode == 0:
                    now = blob.stdout
            if now is not None:
                claude.write(name, now, h.mode, token, full=not h.git_holds and not shas)
        if now is None and current == h.original:
            return "unchanged"
        if now == h.original:
            return "restored"
        saved = _save_original(checkout, h, token)
        if now is not None:
            say(f"{rel}: {how}, so the file now matches it; its earlier bytes were saved "
                f"to {saved}")
        else:
            say(f"{rel}: {how}, so the settings held back from the earlier one were not put "
                f"back; its earlier bytes were saved to {saved}")
        return "saved"
    finally:
        if claude is not None:
            os.close(claude.fd)


def _put_back_safely(checkout: str, held: List[_Held], token: str
                     ) -> Tuple[List[str], List[_Held], Dict[str, str]]:
    """:func:`_put_back`, where even an unexpected error becomes a reported problem
    (the journal keeps everything) rather than ending the run. Only the error's
    type is reported: its text could quote settings bytes."""
    try:
        return _put_back(checkout, held, token)
    except Exception as exc:  # a guard bug must never take the review run down with it
        return [f"an unexpected {type(exc).__name__}"], list(held), {}


def _put_back_file(checkout: str, root: _Dir, h: _Held, rel: str, token: str, say) -> str:
    """Put the original back.

    Found exactly as the guard left it, the file gets its original bytes back.
    Otherwise a spawn changed it:

    * it left an edited file whose bytes are one JSON object: the edit is kept,
      and each key the guard held back that the spawn did not re-state is merged
      back in — read off the bytes the guard wrote, so a key the spawn could see
      and deleted stays deleted;
    * the original is not one JSON object: there is nothing to merge into, so the
      original's bytes win and the dropped edit is announced;
    * the spawn deleted or replaced the file, removed ``.claude``, or left bytes
      that are not one JSON object: there is no edited object to merge into, so
      the spawn's change stays. When git holds the original (a tracked file with
      no local edit), git can always bring it back — and held keys resurrected
      into a file its author removed would ride the next commit. When only the
      journal holds it (an untracked file, or a local edit such as a
      ``--skip-worktree`` override), the original is saved, owner-only, beside
      the journal, and named.

    Whatever is written goes through a temp file at the file's original mode.
    Bytes only the journal holds go back only into a ``.claude`` no wider than it
    was, flushed all the way before the journal is dropped. Nothing is ever
    written through a ``.claude`` that is no longer a real directory."""
    cst = root.lstat(CLAUDE_DIR)
    claude = root.child(CLAUDE_DIR) if cst is not None and stat.S_ISDIR(cst.st_mode) else None
    try:
        name = claude.spelled(h.kind) if claude else h.kind
        st = claude.lstat(name) if claude else None
        current = claude.read(name) if st is not None and stat.S_ISREG(st.st_mode) else None
        if current is not None and current == h.original:
            if st.st_nlink > 1:
                # A hard link made while settings were held back: replace it rather
                # than change the mode of a file outside the checkout.
                claude.write(name, h.original, h.mode, token)
            else:
                claude.set_mode(name, h.mode)
            return "unchanged"
        if current is not None and current == h.written:
            _narrow_dir(claude, h.dir_mode)
            # Bytes only the journal holds are flushed all the way before the journal
            # that holds them is dropped.
            claude.write(name, h.original, h.mode, token, full=not h.git_holds)
            return "restored"
        unmergeable = current is not None and _parse(h.original) is not None and (
            _blank(current) or _parse(current) is None)
        if current is None or (unmergeable and not h.git_holds):
            if not h.git_holds:
                saved = _save_original(checkout, h, token)
                say(f"{rel}: the file was changed while settings were held back from it; "
                    f"nothing was merged back into it, and its original, which exists "
                    f"nowhere else, was saved to {saved}")
                return "saved"
            what = (f"{CLAUDE_DIR} was removed" if cst is None
                    else f"{CLAUDE_DIR} was replaced" if claude is None
                    else "the file was deleted" if st is None else "the file was replaced")
            say(f"{rel}: {what} while settings were held back from it; they "
                f"({', '.join(_held_keys(h))}) were not put back")
            return "kept"
        if not h.git_holds:
            _narrow_dir(claude, h.dir_mode)
        data, note = _merge(h, current, rel)
        claude.write(name, data, h.mode, token, full=not h.git_holds)
        if note:
            say(note)
        return "merged"
    finally:
        if claude is not None:
            os.close(claude.fd)


def _narrow_dir(d: _Dir, mode: int) -> None:
    """Take back any access a spawn granted on ``.claude`` since the guard recorded
    its mode — a file relying on its directory for privacy is written back only
    into a directory no wider than it was. Never widens."""
    current = stat.S_IMODE(os.fstat(d.fd).st_mode)
    wanted = current & (mode | 0o700)
    if wanted != current:
        os.fchmod(d.fd, wanted)


def _save_original(checkout: str, h: _Held, token: str) -> str:
    """Keep what may be the only copy of a held path's original bytes, saved
    owner-only in the state dir; returns where. Raises ``OSError`` — and the
    journal keeps it — when it cannot be saved."""
    name = f"{_key(checkout)}.{token}.{h.kind}"
    with _Dir.open(_private_state_dir()) as state:
        state.write(name, h.original, 0o600, token, full=True)
    return os.path.join(state_dir(), name)


def _merge(h: _Held, current: bytes, rel: str) -> Tuple[bytes, Optional[str]]:
    """A spawn edited a held file (for a file git holds, an empty file reads as
    ``{}``). Its keys win — a key it re-stated is its own — and the keys the guard
    held back that it did not re-state are merged back, read off the bytes the
    guard actually wrote, so a key the spawn could see and deleted stays deleted.
    An original that is not one JSON object has nothing to merge into: its bytes
    win, and the dropped edit is announced. An edit that is not one JSON object is
    kept as it is, without the held keys (git still has them)."""
    original = _parse(h.original)
    if original is None:
        return h.original, (f"{rel}: the edit made to this file while settings were held "
                            f"back could not be merged with the original, which is not one "
                            f"JSON object, so the original was restored and the edit dropped")
    edited = {} if _blank(current) else _parse(current)
    if edited is None:
        return current, (f"{rel}: kept the edit made to this file while settings were held "
                         f"back; it is not one JSON object, so "
                         f"{', '.join(_held_keys(h))} were not put back")
    back = [k for k in _held_keys(h) if k not in edited]
    merged = dict(edited)
    for k in back:
        merged[k] = original[k]
    if _canon(merged) == _canon(original):
        return h.original, None
    return _dump(merged), (f"{rel}: kept the edit made to this file while settings were "
                           f"held back" + (f", and put back {', '.join(back)}" if back else ""))


def _put_back_link(d: _Dir, name: str, h: _Held, rel: str, token: str, say) -> str:
    target = os.fsdecode(h.original)
    st = d.lstat(name)
    if st is None:
        d.symlink(target, name, token)
        return "restored"
    if stat.S_ISLNK(st.st_mode) and d.readlink(name) == target:
        return "unchanged"
    say(f"{rel}: a new {rel} was created while the symlink to {target} was removed, so "
        f"the symlink was not put back")
    return "kept"


def _put_back_inner_link(root: _Dir, h: _Held, rel: str, token: str, say) -> str:
    cst = root.lstat(CLAUDE_DIR)
    if cst is None or not stat.S_ISDIR(cst.st_mode):
        say(f"{rel}: {CLAUDE_DIR} was removed while the symlink to "
            f"{os.fsdecode(h.original)} was removed, so the symlink was not put back")
        return "kept"
    with root.child(CLAUDE_DIR) as claude:
        return _put_back_link(claude, claude.spelled(h.kind), h, rel, token, say)


def _sweep_temps(checkout: str, token: Optional[str]) -> None:
    """Remove temp files an interrupted window left behind: exactly the names its
    token produces (in the checkout root and in ``.claude``, never through a
    symlink), and this checkout's journal temps in the state dir. A leftover could
    otherwise hold a local secret, untracked, for the next ``git add -A``."""
    with contextlib.suppress(OSError), _Dir.open(state_dir()) as d:
        for n in _journal_temps(checkout):
            with contextlib.suppress(OSError):
                d.unlink(n)
    if token is None:
        return

    def sweep(d: _Dir, names) -> None:
        for n in names:
            st = d.lstat(_tmp_name(n, token))
            if st is not None and not stat.S_ISDIR(st.st_mode):
                with contextlib.suppress(OSError):
                    d.unlink(_tmp_name(n, token))

    with contextlib.suppress(OSError), _Dir.open(checkout, follow=True) as root:
        sweep(root, {CLAUDE_DIR, root.spelled(CLAUDE_DIR)})
        with root.child(CLAUDE_DIR) as claude:
            sweep(claude, set(SETTINGS_FILES) | {claude.spelled(n) for n in SETTINGS_FILES})


def _journal_temps(checkout: str) -> List[str]:
    """This checkout's temp files in the state dir — what a kill mid-write of its
    journal, or of a saved original, leaves."""
    kinds = "|".join(re.escape(k) for k in (*SETTINGS_FILES, _DIR_LINK))
    pattern = re.compile(r"\." + _key(checkout) + r"\.(json|[0-9a-f]{16}\.(" + kinds + r"))"
                         r"\.[0-9a-f]{16}" + re.escape(_TMP_SUFFIX))
    try:
        return [n for n in os.listdir(state_dir()) if pattern.fullmatch(n)]
    except OSError:
        return []


_orphans_noted: Set[str] = set()


def _note_orphaned_journals() -> None:
    """A journal whose checkout no longer exists (moved or deleted after a crash)
    holds the only copy of what was held back there: name it, once per run, rather
    than leave it stranded unseen."""
    d = state_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if n in _orphans_noted or not re.fullmatch(r"[0-9a-f]{32}\.json", n):
            continue
        path = os.path.join(d, n)
        try:
            gone = json.loads(_read_regular(path).decode("utf-8")).get("checkout")
        except Exception:  # an unreadable journal is reported when its own checkout runs
            continue
        if isinstance(gone, str) and not os.path.lexists(gone):
            _orphans_noted.add(n)
            _say(f"{path} holds .claude settings held back from {gone}, which no longer "
                 f"exists; delete it once you no longer need them")


def _recover_locked(checkout: str) -> bool:
    """Replay a journal an interrupted window left (the caller holds the lock, so
    it is never another live window's). True when there was one to replay."""
    if not os.path.lexists(_journal_path(checkout)):
        _sweep_temps(checkout, None)  # a kill mid-journal-write leaves only a temp
        return False
    held, token = _read_journal(checkout)
    _sweep_temps(checkout, token)
    problems, unresolved, outcomes = _put_back_safely(checkout, held, token)
    _settle_journal(checkout, unresolved, token)
    if problems:
        raise _Unrecoverable(f"{'; '.join(problems)}; the journal is {_journal_path(checkout)}")
    for h in held:
        if outcomes.get(h.kind) == "restored":
            _say(f"restored {_rel_of(checkout, h)} in {checkout} after an interrupted claude run")
    return True


@contextlib.contextmanager
def _sigterm_restores() -> Iterator[None]:
    """While settings are held back, a SIGTERM becomes an exception: the spawn's
    ``subprocess.run`` then kills its ``claude`` child, and the window's restore
    runs — rather than this process dying with the child still running, where a
    restarted loop would put the PR's settings back underneath it. Main thread
    only, and only while nobody else handles SIGTERM."""
    installed = False
    if threading.current_thread() is threading.main_thread():
        with contextlib.suppress(ValueError, OSError, AttributeError):
            if signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
                signal.signal(signal.SIGTERM, _exit_on_sigterm)
                installed = True
    try:
        yield
    finally:
        if installed:
            with contextlib.suppress(ValueError, OSError):
                if signal.getsignal(signal.SIGTERM) is _exit_on_sigterm:
                    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def _exit_on_sigterm(signum, frame):
    raise SystemExit(128 + signum)


@contextlib.contextmanager
def _sigterm_deferred() -> Iterator[None]:
    """Hold a SIGTERM until the restore inside finishes."""
    previous = None
    with contextlib.suppress(ValueError, OSError, AttributeError):
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        yield
    finally:
        if previous is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)


# ── public entry points ──────────────────────────────────────────────────────────

@contextlib.contextmanager
def window(cwd: Optional[str]) -> Iterator[None]:
    """Guard ONE ``claude`` spawn started in ``cwd`` (a falsy ``cwd`` is the
    process's own working directory, which the child inherits). Raises
    :class:`SettingsGuardRefusal` — and the spawn must not happen — when the
    settings there cannot be made inert."""
    try:
        checkout = _checkout(cwd)
    except OSError as exc:
        raise _refusal("the current directory", f"it is unavailable ({exc})")
    if not _needs_attention(checkout):
        yield
        return
    with _locked(checkout) as reentered:
        if reentered:
            yield  # this thread's own window on this checkout is already open
            return
        try:
            _recover_locked(checkout)
        except (_Unrecoverable, OSError) as exc:
            raise _refusal(checkout, f"settings an earlier claude run left held back "
                                     f"could not be put back ({exc})")
        try:
            held = _plan(checkout)
        except OSError as exc:
            raise _refusal(checkout, f"cannot read its .claude settings ({exc})")
        except Exception as exc:  # a guard bug refuses the spawn; it never ends the run
            raise _refusal(checkout, f"its .claude settings could not be checked "
                                     f"(an unexpected {type(exc).__name__})")
        if not held:
            yield
            return
        token = secrets.token_hex(8)
        try:
            _apply(checkout, held, token)
        except SettingsGuardRefusal:
            raise
        except Exception as exc:
            _settle_journal(checkout, _put_back_safely(checkout, held, token)[1], token)
            raise _refusal(checkout, f"its .claude settings could not be held back "
                                     f"(an unexpected {type(exc).__name__})")
        with _sigterm_restores():
            try:
                yield
            finally:
                with _sigterm_deferred():
                    problems, unresolved, _ = _put_back_safely(checkout, held, token)
                    _settle_journal(checkout, unresolved, token)
                if problems and all(h.index_only for h in unresolved):
                    _say(f"the .claude settings in {checkout} are back, but git still marks "
                         f"them skip-worktree ({'; '.join(problems)}); that is cleared before "
                         f"the next claude run there")
                elif problems:
                    _say(f"could not restore the .claude settings in {checkout} "
                         f"({'; '.join(problems)}); they stay held back, and claude will "
                         f"not start there until they are restored (the journal is "
                         f"{_journal_path(checkout)})")


def recover(cwd: Optional[str]) -> bool:
    """Put back settings an interrupted earlier run left held back in ``cwd``'s
    checkout (the loop-entry replay). Never raises: False when it could not, and
    the next spawn there then refuses to start until it can."""
    try:
        checkout = _checkout(cwd)
    except OSError:
        return False
    _note_orphaned_journals()
    if not os.path.lexists(_journal_path(checkout)) and not _journal_temps(checkout):
        return True
    try:
        with _locked(checkout) as reentered:
            if not reentered:
                _recover_locked(checkout)
        return True
    except (_Unrecoverable, OSError) as exc:
        _say(f"could not restore the .claude settings in {checkout} ({exc}); claude "
             f"will not start there until they are restored")
        return False
