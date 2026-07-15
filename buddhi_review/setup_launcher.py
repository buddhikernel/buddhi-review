#!/usr/bin/env python3
"""setup_launcher.py — open the setup wizard in a fresh terminal window.

The setup wizard (:mod:`buddhi_review.wizard`) is an interactive raw-mode TTY
program: arrow-key selectors, Space/Enter toggles, ``getpass`` secret prompts. An
AI coding agent driving the review loop through a non-interactive Bash tool CANNOT
answer those prompts — so handing the user a bare ``python3 -m buddhi_review
setup`` command would force them to quit their agent session to run it. This
launcher spawns the wizard in a NEW terminal window so the agent session stays
alive, degrading through a fallback chain that ends — on a headless / SSH host
with no window server — at printing the one-liner for the user to run by hand:

  macOS         → a generated ``.command`` file opened with ``open`` (Terminal)
  Linux (GNOME) → ``gnome-terminal -- bash -lc "…"``
  Linux (KDE)   → ``konsole -e bash -lc "…"``
  Linux (other) → ``x-terminal-emulator -e …``  →  ``xterm -e …``
  Windows       → a ``.bat`` launcher opened with ``cmd /c start cmd /k``
  headless      → print the command and tell the user to run it

The wizard runs as the installed module ``python3 -m buddhi_review setup``, so no
``cd`` into a checkout is needed. Pure stdlib; every external seam
(``platform.system``, ``shutil.which``, ``subprocess.Popen``, the command-file
writer, the output stream) is injectable so the spawn logic is unit-testable
without opening a window. Any args are forwarded verbatim to the wizard, so
``setup_launcher.py --repo owner/repo`` opens it in the per-repo confirm mode.

Run: python3 -m buddhi_review.setup_launcher [--repo owner/repo]
"""
from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The wizard runs as an installed module, not a script path.
_MODULE = "buddhi_review"
_SETUP_SUBCOMMAND = "setup"


def _python_bin():
    """The interpreter to run the wizard with — prefer the one running us."""
    return sys.executable or "python3"


def detect_strategy(system, which, environ=None):
    """Return the terminal-spawn strategy for this platform.

    ``system`` is a ``platform.system()``-style string; ``which`` is a
    ``shutil.which``-style callable; ``environ`` defaults to ``os.environ``.
    Returns one of: ``macos``, ``windows``, ``gnome-terminal``, ``konsole``,
    ``x-terminal-emulator``, ``xterm``, or ``print`` (the headless fallback — no
    GUI terminal reachable). An SSH session prints UNLESS it is a Linux session
    with a reachable window server (X11 forwarding / Wayland); every non-Linux
    remote session always prints. Pure so the decision is unit-testable.
    """
    environ = os.environ if environ is None else environ
    system = (system or "").lower()
    has_display = bool(environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"))
    is_ssh = any(environ.get(v) for v in ("SSH_CLIENT", "SSH_TTY", "SSH_CONNECTION"))
    # A remote (SSH) session can surface a window ONLY when a Linux session
    # forwards a window server: OpenSSH X11 forwarding uses a hostname prefix
    # (localhost:10.0), while a locally-inherited DISPLAY=:0 starts with ':' and
    # belongs to the remote desktop — spawning there opens a window the SSH user
    # cannot see. WAYLAND_DISPLAY has no forwarding convention, so accept as-is.
    _display = environ.get("DISPLAY") or ""
    display_reachable = bool(
        (_display and not _display.startswith(":"))  # X11-forwarded (has hostname)
        or environ.get("WAYLAND_DISPLAY")            # Wayland session
    )
    if is_ssh and not (system == "linux" and display_reachable):
        return "print"
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    if system == "linux":
        if not has_display:
            return "print"
        for term in ("gnome-terminal", "konsole", "x-terminal-emulator", "xterm"):
            if which(term):
                return term
        return "print"
    return "print"


def _posix_shell_command(python_bin, wizard_args=(), *, pythonpath=None):
    """The POSIX command that runs the wizard module, forwarding any extra args
    (e.g. ``--repo owner/repo`` for the per-repo confirm mode).

    ``pythonpath``, when set, is prefixed as ``PYTHONPATH=... <cmd>`` — this is the
    calling process's own ``PYTHONPATH`` (a plugin-only install's SKILL.md sets it to
    ``${CLAUDE_PLUGIN_DATA}/site:...`` before invoking this launcher). Without it, a
    spawned terminal window or a hand-run print-fallback command starts a process
    that does NOT inherit this one's environment, so ``import buddhi_review`` would
    fail for a plugin-only install that never ran a global ``pip install``.
    """
    cmd = f"{shlex.quote(str(python_bin))} -m {_MODULE} {_SETUP_SUBCOMMAND}"
    for arg in wizard_args:
        cmd += f" {shlex.quote(str(arg))}"
    if pythonpath:
        cmd = f"PYTHONPATH={shlex.quote(pythonpath)} {cmd}"
    return cmd


def _escape_cmd_chars(cmdline: str) -> str:
    """Escape cmd.exe special characters outside double quotes with ``^``.

    ``list2cmdline`` quotes args with whitespace but leaves cmd.exe
    metacharacters (``&|<>()^``) unquoted when the arg has none."""
    result = []
    in_quotes = False
    for char in cmdline:
        if char == '"':
            in_quotes = not in_quotes
            result.append(char)
        elif char in "&|<>()^" and not in_quotes:
            result.append("^" + char)
        else:
            result.append(char)
    return "".join(result)


def _windows_shell_command(python_bin, wizard_args=(), *, pythonpath=None):
    """The cmd.exe command equivalent — used for the print fallback on Windows.

    ``pythonpath`` mirrors ``_posix_shell_command``'s: prefixed as a ``set`` that
    scopes to the ``&&``-chained command so a plugin-only install still resolves in
    a freshly spawned window or a hand-run print-fallback command."""
    python_cmd = f'"{python_bin}" -m {_MODULE} {_SETUP_SUBCOMMAND}'
    if wizard_args:
        python_cmd += f" {_escape_cmd_chars(subprocess.list2cmdline(list(wizard_args)))}"
    if pythonpath:
        python_cmd = f'set "PYTHONPATH={pythonpath}"&& {python_cmd}'
    return python_cmd


def _keep_open(shell_cmd, label="setup wizard"):
    """Append an interactive shell so the window stays open after the command
    exits — the user needs to read the final output."""
    return f"{shell_cmd}; echo; echo '[{label} finished — close this window]'; exec bash"


def _write_command_file(_unused, shell_cmd, command_file_dir=None, *, name="buddhi-review-setup"):
    """Write a macOS ``.command`` launcher (``open``-able) and return its path:
    fixed name (overwritten each run), mode 0700, in a per-user private temp dir."""
    if command_file_dir:
        target_dir = Path(command_file_dir)
    else:
        uid = os.getuid() if hasattr(os, "getuid") else os.environ.get("USERNAME", "user")
        target_dir = Path(tempfile.gettempdir()) / f"setup-launcher-{uid}"
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not command_file_dir:
        # mkdir mode applies only on creation; enforce 0700 on a pre-existing dir
        # so the fixed filename is not reachable by other local users.
        try:
            os.chmod(target_dir, 0o700)
        except OSError:
            pass
    path = target_dir / f"{name}.command"
    body = (
        "#!/bin/bash\n"
        "# Auto-generated by setup_launcher.py — runs the buddhi-review setup wizard.\n"
        f"{shell_cmd}\n"
    )
    try:
        path.unlink()
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _print_fallback(stream, shell_cmd):
    """Headless last resort: tell the user how to run the wizard by hand."""
    print("Could not open a terminal window automatically (SSH session, "
          "no display server, or no supported terminal emulator found).", file=stream)
    print("Run the setup wizard yourself in an interactive terminal:", file=stream)
    print(f"\n    {shell_cmd}\n", file=stream)


def spawn_wizard(*, system=None, which=None, popen=None, environ=None,
                 python_bin=None, command_file_dir=None,
                 write_command_file=None, stream=None, wizard_args=None):
    """Open the setup wizard in a fresh terminal window.

    All external effects are injectable for testing (``system``, ``which``,
    ``popen``, ``environ``, ``python_bin``, ``command_file_dir``,
    ``write_command_file``, ``stream``, ``wizard_args``). Returns a dict
    ``{strategy, spawned: bool, command, command_file?}``. Never raises on a
    spawn failure — degrades to the print fallback and reports ``spawned=False``.
    """
    system = system if system is not None else platform.system()
    which = which or shutil.which
    popen = popen or subprocess.Popen
    environ = os.environ if environ is None else environ
    stream = stream or sys.stdout
    write_command_file = write_command_file or _write_command_file
    python_bin = python_bin or _python_bin()
    wizard_args = list(wizard_args or [])
    # Carry this process's own PYTHONPATH into the constructed command so a plugin-
    # only install (site dir on PYTHONPATH, not globally pip-installed) still
    # resolves `import buddhi_review` in a freshly spawned terminal or a hand-run
    # print-fallback command — neither inherits this process's environment.
    pythonpath = environ.get("PYTHONPATH") or None

    if (system or "").lower() == "windows":
        shell_cmd = _windows_shell_command(python_bin, wizard_args, pythonpath=pythonpath)
    else:
        shell_cmd = _posix_shell_command(python_bin, wizard_args, pythonpath=pythonpath)
    strategy = detect_strategy(system, which, environ)

    def _fallback():
        _print_fallback(stream, shell_cmd)
        return {"strategy": "print", "spawned": False, "command": shell_cmd}

    if strategy == "print":
        return _fallback()

    try:
        if strategy == "macos":
            # Keep the Terminal window open after the wizard exits so the user can
            # read the final summary (matches the Linux _keep_open branches below).
            cmd_file = write_command_file(None, _keep_open(shell_cmd, "setup wizard"), command_file_dir)
            popen(["open", str(cmd_file)])
            return {"strategy": "macos", "spawned": True, "command": shell_cmd,
                    "command_file": str(cmd_file)}
        if strategy == "gnome-terminal":
            popen(["gnome-terminal", "--", "bash", "-lc", _keep_open(shell_cmd, "setup wizard")])
        elif strategy in ("konsole", "x-terminal-emulator", "xterm"):
            popen([strategy, "-e", "bash", "-lc", _keep_open(shell_cmd, "setup wizard")])
        elif strategy == "windows":
            # A .bat launcher avoids the nested-double-quote misparse that
            # `cmd /k "<cmd with quotes>"` hits.
            uid = environ.get("USERNAME", "user")
            base_dir = Path(command_file_dir) if command_file_dir else Path(tempfile.gettempdir())
            target_dir = base_dir / f"setup-launcher-{uid}"
            target_dir.mkdir(parents=True, exist_ok=True)
            bat_path = target_dir / "buddhi-review-setup.bat"
            python_bin_escaped = str(python_bin).replace("%", "%%")
            args_cmd = ""
            if wizard_args:
                raw_args = subprocess.list2cmdline(list(wizard_args))
                args_cmd = f" {_escape_cmd_chars(raw_args)}".replace("%", "%%")
            invocation = f'"{python_bin_escaped}" -m {_MODULE} {_SETUP_SUBCOMMAND}{args_cmd}'
            bat_content = (
                "@echo off\n"
                "chcp 65001 >nul\n"
                f"{invocation}\n"
                "pause\n"
            )
            try:
                bat_path.unlink()
            except OSError:
                pass
            bat_path.write_text(bat_content, encoding="utf-8")
            popen(f'cmd /c start "" cmd /k "{bat_path}"')
        else:  # unreachable — detect_strategy only returns the cases above
            return _fallback()
    except (OSError, ValueError):
        return _fallback()
    return {"strategy": strategy, "spawned": True, "command": shell_cmd}


def _slugify(label):
    """``label`` → a safe filename slug: lowercase, non-alphanumerics → '-'."""
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return slug or "command"


def spawn_command(shell_command, *, label="command", system=None, which=None,
                  popen=None, environ=None, command_file_dir=None,
                  write_command_file=None, stream=None):
    """Open an arbitrary shell command in a fresh terminal window.

    Designed for an interactive CLI tool (e.g. ``claude setup-token``) that needs a
    real TTY and cannot run headlessly. The wizard's Claude step shells out to this
    to capture a ``CLAUDE_CODE_OAUTH_TOKEN``. All external effects are injectable;
    returns ``{strategy, spawned: bool, command, command_file?}`` and never raises.
    """
    system = system if system is not None else platform.system()
    which = which or shutil.which
    popen = popen or subprocess.Popen
    environ = os.environ if environ is None else environ
    stream = stream or sys.stdout
    write_command_file = write_command_file or _write_command_file

    strategy = detect_strategy(system, which, environ)
    slug = _slugify(label)
    cmd_name = f"buddhi-{slug}"

    def _fallback():
        _print_fallback(stream, shell_command)
        return {"strategy": "print", "spawned": False, "command": shell_command}

    if strategy == "print":
        return _fallback()

    try:
        if strategy == "macos":
            keep_cmd = _keep_open(shell_command, label)
            cmd_file = write_command_file(None, keep_cmd, command_file_dir, name=cmd_name)
            popen(["open", str(cmd_file)])
            return {"strategy": "macos", "spawned": True, "command": shell_command,
                    "command_file": str(cmd_file)}
        if strategy == "gnome-terminal":
            popen(["gnome-terminal", "--", "bash", "-lc", _keep_open(shell_command, label)])
        elif strategy in ("konsole", "x-terminal-emulator", "xterm"):
            popen([strategy, "-e", "bash", "-lc", _keep_open(shell_command, label)])
        elif strategy == "windows":
            uid = environ.get("USERNAME", "user")
            base = Path(command_file_dir) if command_file_dir else Path(tempfile.gettempdir())
            target_dir = base / f"buddhi-launcher-{uid}"
            target_dir.mkdir(parents=True, exist_ok=True)
            bat_path = target_dir / f"{cmd_name}.bat"
            try:
                bat_path.unlink()
            except OSError:
                pass
            bat_path.write_text(
                "@echo off\n"
                f"{shell_command}\n"
                "echo.\n"
                f"echo [{label} finished - you can close this window]\n"
                "pause\n",
                encoding="utf-8")
            popen(f'cmd /c start "" cmd /k "{bat_path}"')
        else:  # unreachable
            return _fallback()
    except (OSError, ValueError):
        return _fallback()
    return {"strategy": strategy, "spawned": True, "command": shell_command}


def main(argv=None):
    # Forward our own args verbatim to the wizard, so `launch-setup.sh --repo
    # owner/repo` opens the wizard in its per-repo confirm mode.
    argv = sys.argv[1:] if argv is None else list(argv)
    result = spawn_wizard(wizard_args=argv)
    if result["spawned"]:
        where = {"macos": "a new Terminal window"}.get(
            result["strategy"], f"a new terminal window ({result['strategy']})")
        print(f"Opened the buddhi-review setup wizard in {where}.")
        print("Complete the steps there — this session stays active. When the "
              "wizard finishes, re-run /review-pr or /open-pr.")
    # The headless fallback already printed instructions; exit 0 either way.
    return 0


if __name__ == "__main__":
    sys.exit(main())
