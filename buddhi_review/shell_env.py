"""shell_env.py — the single sanctioned writer for Buddhi-managed shell-rc exports.

A secret the operator prefers to keep in the environment — per the project
convention that secrets live in the env, not in ``config.yaml`` — is persisted as
an ``export NAME=value`` line inside ONE marked, idempotent block in the user's
shell rc file. The setup wizard is the only caller today: when ``gh`` is not
authenticated it offers to persist a ``GH_TOKEN`` (the GitHub token escape hatch
for the Copilot reviewer) here rather than in ``config.yaml``.

The block is written atomically (temp file + ``os.replace``) at mode ``0600``, and
existing managed exports are read back and re-emitted on every write so a second
write never clobbers the first. Pure stdlib; this module never RETURNS a secret
value to a caller — it takes a value IN (:func:`upsert`) and reports presence OUT
(:func:`present`).
"""
from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path
from typing import Iterable, Optional, Tuple

# The block marker. A neutral label — it names no notification channel or paid
# surface; it only marks the lines this module owns so a re-write replaces them
# in place rather than appending a duplicate block.
MARKER = "# Added by buddhi-review setup"

# Canonical env names this module WRITES, in their stable on-disk order. Only the
# GitHub token escape hatch for the Copilot reviewer (an alternative to
# ``gh auth login``). No notification or budget credentials live here.
GH_TOKEN_NAME = "GH_TOKEN"
WRITE_ORDER = (GH_TOKEN_NAME,)

# Export-line prefixes whose lines belong to the managed block (recognised for
# stripping + reading), across the posix (``export``) and fish (``set -gx``) forms.
# Anchored to the EXACT variable name (trailing ``=`` for posix, trailing space for
# fish) so the predicate cannot bleed into a user's ``GH_TOKEN``-prefixed sibling
# (e.g. ``GH_TOKEN_BACKUP``).
_MANAGED_PREFIXES = (
    "export GH_TOKEN=", "set -gx GH_TOKEN ",
)


# ── Target resolution ───────────────────────────────────────────────────────────

def _on_windows() -> bool:
    """True on Windows. A tiny seam so tests can exercise the ``setx`` path WITHOUT
    monkeypatching ``os.name`` (which would flip pathlib to WindowsPath)."""
    return os.name == "nt"


def target(rc_path: Optional[str] = None) -> Tuple[Path, str]:
    """Resolve ``(rc_file_path, syntax)`` for a write. ``syntax`` ∈
    {``posix``, ``fish``, ``windows``}.

    Priority: an explicit ``rc_path`` arg → the ``BUDDHI_SHELL_RC`` env override
    (an escape hatch for headless contexts + tests; treated as posix) → ``$SHELL``
    (zsh→~/.zshenv, bash→~/.bash_profile, fish→fish conf.d, else ~/.profile).
    """
    if rc_path:
        return Path(rc_path), "posix"
    override = os.environ.get("BUDDHI_SHELL_RC")
    if override:
        return Path(override), "posix"
    if _on_windows():
        # Windows has no sourced rc file. The canonical per-user persistence is
        # ``setx``, which writes the user environment inherited by NEW processes.
        return Path("Windows user environment (setx)"), "windows"
    name = os.path.basename(os.environ.get("SHELL", ""))
    home = Path(os.path.expanduser("~"))
    if name == "zsh":
        return home / ".zshenv", "posix"
    if name == "bash":
        return home / ".bash_profile", "posix"
    if name == "fish":
        return home / ".config" / "fish" / "conf.d" / "buddhi.fish", "fish"
    return home / ".profile", "posix"


# ── Export-line formatting ──────────────────────────────────────────────────────

def _fish_quote(value: str) -> str:
    """Quote a value for fish: double-quote and escape ``\\``, ``$``, ``"``.
    ``shlex.quote`` produces POSIX single-quote style fish cannot parse."""
    escaped = value.replace("\\", "\\\\").replace("$", "\\$").replace('"', '\\"')
    return f'"{escaped}"'


def format_export(name: str, value: str, syntax: str) -> str:
    """A single canonical export line for the chosen shell syntax."""
    if syntax == "fish":
        return f"set -gx {name} {_fish_quote(value)}"
    return f"export {name}={shlex.quote(value)}"


def export_lines(syntax: str, items: Iterable[Tuple[str, str]]) -> list:
    """Export lines for an ordered list of ``(name, value)`` pairs."""
    return [format_export(name, value, syntax) for name, value in items]


# ── Block parsing ───────────────────────────────────────────────────────────────

def _split_managed(content: str) -> Tuple[str, list]:
    """Split rc-file ``content`` into ``(content_without_managed_block,
    [captured managed export-line strings])``. The marker line + its preceding
    blank separator are dropped, managed export lines under the marker are
    captured (removed from the output), and the block ends at the first
    non-managed line (a trailing blank there is also dropped)."""
    if MARKER not in content:
        return content, []
    out: list = []
    captured: list = []
    skip = False
    for line in content.splitlines(keepends=True):
        if line.rstrip("\r\n") == MARKER:
            if out and out[-1] in ("\n", "\r\n"):
                out.pop()
            skip = True
            continue
        if skip:
            trimmed = line.strip()
            if trimmed.startswith(_MANAGED_PREFIXES):
                captured.append(line)
                continue
            skip = False
            if trimmed == "":
                continue
        out.append(line)
    return "".join(out), captured


def strip_block(content: str) -> str:
    """Remove the managed export block (idempotent re-write helper)."""
    return _split_managed(content)[0]


def _parse_export(line: str, syntax: str) -> Tuple[Optional[str], str]:
    """Best-effort ``(name, value)`` for a managed export line. Used only to
    PRESERVE an existing value across a merge — never surfaced to a caller."""
    s = line.strip()
    if syntax == "fish":
        try:
            toks = shlex.split(s)
        except ValueError:
            return None, ""
        if len(toks) >= 2 and toks[0] == "set":
            idx = 1
            while idx < len(toks) and toks[idx].startswith("-"):
                idx += 1
            if idx < len(toks):
                return toks[idx], toks[idx + 1] if idx + 1 < len(toks) else ""
        return None, ""
    if s.startswith("export "):
        s = s[len("export "):]
    name, eq, rest = s.partition("=")
    if not eq:
        return None, ""
    try:
        vals = shlex.split(rest)
    except ValueError:
        vals = [rest]
    return name.strip(), (vals[0] if vals else "")


def read_managed(content: str, syntax: str = "posix") -> dict:
    """``{name: value}`` for every managed export currently in the block."""
    _, captured = _split_managed(content)
    out: dict = {}
    for line in captured:
        name, val = _parse_export(line, syntax)
        if name:
            out[name] = val
    return out


def present(rc_path: Optional[str] = None) -> set:
    """The set of managed export NAMES currently defined in the rc block (NOT
    their values). Best-effort: an unreadable/absent file → empty set."""
    path, syntax = target(rc_path)
    try:
        content = path.read_text(encoding="utf-8", errors="surrogateescape") if path.exists() else ""
    except OSError:
        content = ""
    return set(read_managed(content, syntax).keys())


# ── Atomic write + upsert ───────────────────────────────────────────────────────

def _atomic_write(path: Path, content: str) -> bool:
    """Write ``content`` to ``path`` atomically (temp file + ``os.replace``), 0600.
    Never truncates the live rc file on a mid-write failure."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_str = tempfile.mkstemp(
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".buddhi.tmp",
        )
        tmp = Path(tmp_str)
        try:
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
            with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
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
            os.chmod(path, 0o600)  # the file may hold a secret
        except OSError:
            pass
        return True
    except OSError:
        return False


def _upsert_windows(mapping: dict, also_env: bool, *, runner=None) -> Tuple[bool, Path]:
    """Persist managed vars on Windows via ``setx`` (the per-user environment,
    picked up by NEW processes — there is no sourced rc file). An empty/None value
    clears the var. With ``also_env`` the change mirrors into this process's
    ``os.environ``. The setx runner is injectable for tests."""
    import subprocess
    run = runner or (lambda args: subprocess.run(args, capture_output=True, text=True))
    ok = True
    for name, value in mapping.items():
        val = "" if value is None else str(value)
        try:
            res = run(["setx", name, val])
            if getattr(res, "returncode", 0) != 0:
                ok = False
        except Exception:
            ok = False
    if ok and also_env:
        for name, value in mapping.items():
            if value is None or value == "":
                os.environ.pop(name, None)
            else:
                os.environ[name] = str(value)
    return ok, Path("Windows user environment (setx)")


def upsert(
    mapping: dict,
    *,
    rc_path: Optional[str] = None,
    syntax: Optional[str] = None,
    also_env: bool = False,
    setx_runner=None,
) -> Tuple[bool, Path]:
    """Merge ``mapping`` (NAME→value) into the managed rc block atomically.

    A value of ``None`` or ``""`` REMOVES that export. Existing managed exports
    not named in ``mapping`` are PRESERVED (read from the rc file, re-emitted).
    Names outside :data:`WRITE_ORDER` are ignored. With ``also_env=True`` the
    change mirrors into ``os.environ`` of THIS process. Returns ``(ok, rc_path)``.
    """
    mapping = {k: v for k, v in mapping.items() if k in WRITE_ORDER}
    if rc_path is None:
        path, syntax = target()
    else:
        path = Path(rc_path)
        syntax = syntax or "posix"
    if syntax == "windows":
        return _upsert_windows(mapping, also_env, runner=setx_runner)
    try:
        existing = path.read_text(encoding="utf-8", errors="surrogateescape") if path.exists() else ""
    except OSError:
        existing = ""
    stripped, _ = _split_managed(existing)
    merged = read_managed(existing, syntax)
    for name, value in mapping.items():
        if value is None or value == "":
            merged.pop(name, None)
        else:
            # shlex.quote() preserves literal \n/\r inside single quotes (valid shell),
            # but _split_managed parses line-by-line and treats the continuation lines
            # as non-managed content, corrupting the rc file. Reject bad values early.
            sval = str(value)
            if any(c in sval for c in ("\n", "\r", "\x00")):
                raise ValueError(
                    f"Secret value for {name!r} contains a newline or NUL character "
                    f"and cannot be written to the rc file. Strip the value before calling upsert()."
                )
            merged[name] = sval
    ordered = [(n, merged[n]) for n in WRITE_ORDER if n in merged]
    if ordered:
        block = "\n".join(["", MARKER, *export_lines(syntax, ordered), ""]) + "\n"
    else:
        block = ""
    ok = _atomic_write(path, stripped + block)
    if ok and also_env:
        for name, value in mapping.items():
            if value is None or value == "":
                os.environ.pop(name, None)
            else:
                os.environ[name] = str(value)
    return ok, path
