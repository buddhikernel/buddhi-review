"""The interactive setup wizard — ``python3 -m buddhi_review setup``.

Walks the user through the free configuration surface and writes
``~/.config/review-loop/config.yaml`` (the path :mod:`buddhi_review.config`
reads). The ordered flow mirrors the reference wizard, with the two budget /
monitoring steps shown as single-line locked upgrade teasers (they persist
nothing):

  1. Tooling doctor      — Claude CLI, ``gh`` ≥ 2.87 + auth, reachable model tiers
  2. Plan selection      — drives ``plan_profiles.yml`` role→model resolution
  3. Repo binding        — infer ``owner/repo`` + toplevel from ``git remote``
  4. Provider budgets    — locked teaser (paid); persists nothing
  5. Reviewer fleet      — multi-select + validate each + capture ``auto_on_open``
  6. Live monitoring     — locked teaser (paid); persists nothing
  7. Summary + done      — read-back + the launch hint

Only the free keys are persisted: ``plan``, ``repo``, ``cwd``,
``active_reviewers``, ``auto_on_open``, ``notifications: console``. A shell-rc
secret (the Copilot ``GH_TOKEN`` escape hatch) is written via
:mod:`buddhi_review.shell_env`, never into config. The Claude reviewer path can
provision the workflow server-side — when the checkout is on a feature branch it
opens a ``gh``-api PR that lands ``claude-code-review.yml`` on the default branch
(an issue_comment workflow runs ONLY from there) without touching the local tree,
falling back to a local copy — and set the ``CLAUDE_CODE_OAUTH_TOKEN`` secret. The
secret is scoped repo-only by default (least blast radius); on an org-owned repo
an explicit, separately-confirmed opt-in can scope it org-wide but visible to this
repo alone, gated on org-admin with a repo-scope fallback. When a repo opts into
label-gated CI, the wizard likewise offers to install the bundled
``tests-ready-for-ci.yml`` gate on the default branch (its CI command detected from
the checkout and confirmed), via the same server-side PR path — so the opt-in
provisions a real gate, not just a recorded preference.

The wizard is an interactive raw-mode TTY program; the ``/review-pr setup`` skill
step opens it in a fresh window via :mod:`buddhi_review.setup_launcher`. Every
external effect (subprocess runner, the selectors, prompts, the spawn helper, the
output stream) is injectable, so the step-gating logic is unit-testable without a
real terminal.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:  # PyYAML is a hard dep of the package; guard so import never explodes.
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from buddhi_review import (config, detectors, managed_files, plan_profile,
                           setup_launcher, shell_env, upsell)
from buddhi_review.transparency import _colour_enabled

_GH_MIN = (2, 87)
_REVIEWERS = ("copilot", "gemini", "codex", "claude")
_GITHUB_APP_BOTS = ("copilot", "gemini", "codex")  # claude is workflow/mention-driven
_MODEL_TIERS = ("opus", "sonnet", "haiku")

# Each locked teaser is a single-line, single-BENEFIT contextual upgrade nudge
# (exec-plan §E: the one permitted paid reference — a benefit, NEVER a paid product
# name or mechanism enumeration), suppressible with BUDDHI_NO_UPSELL. The paid web
# work-tracking surface is deliberately NOT named or advertised here (it may be cut).
_BUDGETS_TEASER = "Budget controls — paid tier"
_MONITORING_TEASER = "Live run monitoring — paid tier"
# A single-line, suppressible benefit-only nudge (exec-plan §E: benefit only,
# no paid product name or mechanism details).
_PRO_SOON_TEASER = "More automation is coming soon — stay tuned."

# A one-line static note (NOT a live usage read) about the Claude review workflow's
# GitHub Actions cost.
_ACTIONS_NOTE = ("Note: the Claude review workflow runs on GitHub Actions and uses "
                 "your repo's Actions minutes (private repos have a free monthly "
                 "allowance; public repos are free) — see github.com/settings/billing.")

# A clear, single-purpose Claude re-check prompt. Names the workflow file
# explicitly and SPLITS the two independent facts (the workflow is committed on
# the default branch · the CLAUDE_CODE_OAUTH_TOKEN secret is set) instead of
# mashing them into one run-on question, so a user who just merged the install PR
# or set the secret in another window can re-confirm in one keypress.
_CLAUDE_RECHECK_PROMPT = (
    "Has the workflow file claude-code-review.yml been committed on the default "
    "branch (and the CLAUDE_CODE_OAUTH_TOKEN secret set)? Shall I confirm now?")

# Claude's confirm-install gate is declined / un-confirmed: the disabled row keeps
# OSS's clean lead and adds why it matters (the 401 / silent-post symptom), so the
# Claude case reads differently from the generic GitHub-App reviewers' row.
_CLAUDE_DISABLED_ROW = (
    "Claude not confirmed installed — left DISABLED (re-run setup once it is "
    "installed; without it the workflow 401s and claude[bot] posts nothing).")


# ── Colour + output ──────────────────────────────────────────────────────────────

class _Palette:
    def __init__(self, enabled: bool):
        if enabled:
            self.RESET, self.BOLD, self.DIM = "\033[0m", "\033[1m", "\033[2m"
            self.CYAN, self.GREEN, self.RED, self.YELLOW, self.GREY = (
                "\033[36m", "\033[32m", "\033[31m", "\033[33m", "\033[90m")
        else:
            self.RESET = self.BOLD = self.DIM = ""
            self.CYAN = self.GREEN = self.RED = self.YELLOW = self.GREY = ""


def _upsell_suppressed() -> bool:
    """``BUDDHI_NO_UPSELL`` truthy → suppress the locked-teaser upgrade nudges
    (honours the OSS upsell-suppression contract). Default: teasers render. The
    check lives in ``upsell`` so the wizard teasers and the in-run nudge share one
    suppression switch."""
    return upsell.upsell_suppressed()


def _ask_global_default() -> bool:
    """``BUDDHI_ASK_GLOBAL_DEFAULT`` truthy → restore the interactive "also set this
    fleet as your GLOBAL default?" prompt in the per-repo confirm flow. Default
    (unset/falsy): no prompt — the FIRST system-wide setup (no global default yet)
    auto-promotes its fleet to the global default so cross-repo runs have a
    fall-back, and every later per-repo confirm leaves the established default
    untouched. The flag lets the promotion question be brought back wholesale."""
    return os.environ.get("BUDDHI_ASK_GLOBAL_DEFAULT", "").strip().lower() in (
        "1", "true", "yes")


# ── Interactive TTY input primitives ──────────────────────────────────────────────

def _is_tty() -> bool:
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (ImportError, ValueError, OSError, AttributeError):
        return False


def _assemble_pasted_secret(read_char, more_pending, echo=None) -> str:
    """Assemble a possibly-multi-line pasted secret char-by-char. A token copied from a
    narrow terminal window WRAPS across lines, so its clipboard carries newlines: each
    such newline is a WRAP (detected because more input is still buffered) — it is
    dropped and reading continues; a newline with nothing more pending is the user's
    SUBMIT (Enter). Returns the joined chars with ALL whitespace stripped (a real token
    has none). The `read_char` / `more_pending` seams keep this unit-testable without a
    real TTY; `echo` (optional) masks each captured char so the field visibly fills."""
    chars = []
    while True:
        ch = read_char()
        if ch == "":                       # EOF / read error
            break
        if ch in ("\n", "\r"):
            if more_pending():
                continue                   # a newline INSIDE the paste (a wrap)
            break                          # the user's submit Enter
        chars.append(ch)
        if echo is not None:
            echo(ch)
    return "".join("".join(chars).split())


def _read_hidden_tty(prompt: str) -> Optional[str]:
    """Read a hidden secret from the TTY in a SELF-MANAGED no-echo session, capturing a
    wrapped MULTI-LINE paste intact. Deliberately NOT getpass: getpass returns only the
    first line AND its TCSAFLUSH restore DISCARDS the wrapped continuation before it can
    be read, so a token copied from a narrow window is silently truncated (confirmed via
    a pty). This reads the line + its wrapped continuation in ONE session and restores
    WITHOUT flushing (TCSADRAIN). Bracketed paste is turned off so the paste arrives as
    raw text (no ``ESC[200~`` framing to swallow); each captured char echoes a masked
    '•' so the paste visibly registers. Returns the assembled token, or None when stdin
    is not a usable TTY (the caller then falls back to getpass)."""
    if not _is_tty():
        return None
    try:
        import select
        import termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except Exception:
        return None
    # Tell the user what to do (they didn't know Enter submits); the value still masks
    # to • as it pastes, so they also see the field register.
    sys.stdout.write("  (paste the value, then press Enter)\n")
    sys.stdout.write(prompt)
    sys.stdout.write("\x1b[?2004l")        # disable bracketed paste → raw paste text
    sys.stdout.flush()

    def _mask(_ch):
        sys.stdout.write("•")
        sys.stdout.flush()

    def _read_char():
        try:
            return os.read(fd, 1).decode("utf-8", "replace")
        except OSError:
            return ""

    def _more_pending():
        try:
            return bool(select.select([fd], [], [], 0.1)[0])
        except Exception:
            return False

    try:
        new = termios.tcgetattr(fd)
        new[3] = new[3] & ~(termios.ECHO | termios.ICANON)   # lflags: no echo, char-at-a-time
        new[6][termios.VMIN] = 1
        new[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, new)          # NOW — never TCSAFLUSH
        return _assemble_pasted_secret(_read_char, _more_pending, echo=_mask)
    except Exception:
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)     # restore, NO flush
        except Exception:
            pass
        sys.stdout.write("\x1b[?2004h")   # re-enable bracketed paste (mirror the disable above)
        sys.stdout.write("\n")
        sys.stdout.flush()


def _read_pasted_secret(prompt: str, getpass_fn: Callable) -> str:
    """Read a hidden secret that may arrive as a MULTI-LINE paste (a token copied from a
    narrow console window wraps across lines). On a TTY, read via :func:`_read_hidden_tty`
    — getpass CANNOT be used here: it truncates at the first line AND flushes the rest.
    Off a TTY (tests, pipes), fall back to the injected ``getpass_fn`` (whitespace
    stripped). Either way the result is the token's true single-line form."""
    got = _read_hidden_tty(prompt)
    if got is not None:
        return got
    return "".join((getpass_fn(prompt) or "").split())


def _read_key() -> str:
    """A single keypress in raw mode → one of: ``up``, ``down``, ``space``,
    ``enter``, ``esc``, or the literal character."""
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        b = os.read(fd, 1)
        if not b:
            raise EOFError
        if b == b"\x1b":  # escape sequence (arrow keys)
            import select
            rlist, _, _ = select.select([fd], [], [], 0.05)
            if rlist:
                seq = os.read(fd, 2)
                return {b"[A": "up", b"[B": "down", b"OA": "up", b"OB": "down"}.get(seq, "esc")
            return "esc"
        if b in (b"\r", b"\n"):
            return "enter"
        if b == b" ":
            return "space"
        if b == b"\x03":  # Ctrl-C
            raise KeyboardInterrupt
        return b.decode("utf-8", errors="ignore")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _render_choices(options: Sequence[Tuple[str, str]], cursor: int, selected, radio: bool,
                    pal: _Palette, stream) -> None:
    for i, (label, detail) in enumerate(options):
        mark = ("◉" if i in selected else "◯") if radio else ("[x]" if i in selected else "[ ]")
        pointer = f"{pal.CYAN}❯{pal.RESET}" if i == cursor else " "
        line = f" {pointer} {mark} {pal.BOLD}{label}{pal.RESET}"
        if detail:
            line += f"  {pal.DIM}{detail}{pal.RESET}"
        print(line, file=stream)


def _clear_choices(n: int, stream) -> None:
    stream.write(f"\033[{n}A")
    for _ in range(n):
        stream.write("\033[2K\033[1B")
    stream.write(f"\033[{n}A")
    stream.flush()


def _numbered_select(prompt: str, options: Sequence[Tuple[str, str]], preselect: int,
                     pal: _Palette, stream, input_fn) -> int:
    print(prompt, file=stream)
    for i, (label, detail) in enumerate(options, 1):
        extra = f"  ({detail})" if detail else ""
        print(f"  {i}. {label}{extra}", file=stream)
    try:
        raw = input_fn(f"  Choose [1-{len(options)}] (default {preselect + 1}): ").strip()
    except EOFError:
        return preselect
    if not raw:
        return preselect
    try:
        idx = int(raw) - 1
    except ValueError:
        return preselect
    return idx if 0 <= idx < len(options) else preselect


def single_select(prompt: str, options: Sequence[Tuple[str, str]], *, preselect: int = 0,
                  pal: Optional[_Palette] = None, stream=None, input_fn=input,
                  shortcuts: Optional[dict] = None) -> int:
    """A radio selector → the chosen index. Raw-mode arrows on a TTY; a numbered
    prompt otherwise. Emits a trailing blank line so the answered question is set off
    from whatever the wizard prints next (consistent vertical rhythm).
    ``shortcuts`` maps a literal keystroke (e.g. ``'y'``) to the index it should
    select immediately, bypassing the arrow loop."""
    stream = stream or sys.stdout
    pal = pal or _Palette(_colour_enabled(stream))
    if not options:
        return preselect
    if not _is_tty():
        idx = _numbered_select(prompt, options, preselect, pal, stream, input_fn)
        print("", file=stream)
        return idx
    cursor = max(0, min(preselect, len(options) - 1))
    print(prompt, file=stream)
    _render_choices(options, cursor, {cursor}, True, pal, stream)
    while True:
        try:
            key = _read_key()
        except EOFError:
            print("", file=stream)
            return cursor
        if key == "up":
            cursor = (cursor - 1) % len(options)
        elif key == "down":
            cursor = (cursor + 1) % len(options)
        elif key == "enter":
            print("", file=stream)
            return cursor
        elif shortcuts and key.lower() in shortcuts:
            cursor = shortcuts[key.lower()]
            _clear_choices(len(options), stream)
            _render_choices(options, cursor, {cursor}, True, pal, stream)
            print("", file=stream)
            return cursor
        else:
            continue
        _clear_choices(len(options), stream)
        _render_choices(options, cursor, {cursor}, True, pal, stream)


def _numbered_multiselect(prompt: str, options: Sequence[Tuple[str, str]], preselected: set,
                          pal: _Palette, stream, input_fn) -> set:
    print(prompt, file=stream)
    for i, (label, detail) in enumerate(options, 1):
        mark = "x" if i - 1 in preselected else " "
        extra = f"  ({detail})" if detail else ""
        print(f"  [{mark}] {i}. {label}{extra}", file=stream)
    try:
        raw = input_fn("  Enter numbers to toggle (comma-separated), or blank to accept: ").strip()
    except EOFError:
        return set(preselected)
    chosen = set(preselected)
    if raw:
        for tok in raw.replace(",", " ").split():
            try:
                idx = int(tok) - 1
            except ValueError:
                continue
            if 0 <= idx < len(options):
                chosen.symmetric_difference_update({idx})
    return chosen


def multi_select(prompt: str, options: Sequence[Tuple[str, str]], *, preselected=None,
                 pal: Optional[_Palette] = None, stream=None, input_fn=input) -> set:
    """A checkbox selector → the set of chosen indices. Raw-mode (↑/↓ move, Space
    toggles, Enter confirms) on a TTY; a numbered toggle prompt otherwise."""
    stream = stream or sys.stdout
    pal = pal or _Palette(_colour_enabled(stream))
    selected = set(range(len(options))) if preselected is None else set(preselected)
    if not options:
        return selected
    if not _is_tty():
        chosen = _numbered_multiselect(prompt, options, selected, pal, stream, input_fn)
        print("", file=stream)
        return chosen
    cursor = 0
    print(prompt, file=stream)
    print(f"  {pal.DIM}↑/↓ move · Space toggle · Enter confirm{pal.RESET}", file=stream)
    _render_choices(options, cursor, selected, False, pal, stream)
    while True:
        try:
            key = _read_key()
        except EOFError:
            print("", file=stream)
            return selected
        if key == "up":
            cursor = (cursor - 1) % len(options)
        elif key == "down":
            cursor = (cursor + 1) % len(options)
        elif key == "space":
            selected.symmetric_difference_update({cursor})
        elif key == "enter":
            print("", file=stream)
            return selected
        else:
            continue
        _clear_choices(len(options), stream)
        _render_choices(options, cursor, selected, False, pal, stream)


def _ask_yes_no(prompt: str, *, default: bool, input_fn=input, stream=None,
                pal: Optional[_Palette] = None,
                single_select_fn: Optional[Callable] = None) -> bool:
    """A Yes/No question. On a real TTY it renders as the SAME arrow radio selector
    as every other prompt (↑/↓ + Enter over Yes / No) — one consistent pattern, never
    a bare ``[Y/n]`` text line. Off a TTY (pipes / CI) it falls back to the plain text
    prompt, exactly like the other selectors' numbered fallback. Either way it leaves
    a trailing blank line so consecutive prompts breathe. ``default`` pre-selects
    Yes (True) or No (False)."""
    stream = stream or sys.stdout
    if _is_tty():
        _ss = single_select_fn if single_select_fn is not None else single_select
        idx = _ss(prompt, [("Yes", ""), ("No", "")],
                  preselect=0 if default else 1,
                  pal=pal, stream=stream, input_fn=input_fn,
                  shortcuts={"y": 0, "n": 1})
        return idx == 0
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        raw = input_fn(f"  {prompt} {suffix}: ").strip().lower()
    except EOFError:
        raw = ""
    print("", file=stream)
    if not raw:
        return default
    return raw in ("y", "yes")


# ── Panels ─────────────────────────────────────────────────────────────────────

def _panel(title: str, lines: Sequence[str], pal: _Palette, stream) -> None:
    print(f"\n{pal.BOLD}── {title} ──{pal.RESET}", file=stream)
    for ln in lines:
        print(f"  {ln}", file=stream)


def _row(status: str, text: str, pal: _Palette, stream) -> None:
    glyph = {"ok": f"{pal.GREEN}✓{pal.RESET}", "warn": f"{pal.YELLOW}⚠{pal.RESET}",
             "bad": f"{pal.RED}✗{pal.RESET}", "step": f"{pal.CYAN}▸{pal.RESET}",
             "info": f"{pal.GREY}·{pal.RESET}"}.get(status, " ")
    print(f"  {glyph} {text}", file=stream)


def _note(text: str, pal: _Palette, stream) -> None:
    """A dim, un-glyphed aside at panel indent.
    For explanation/context that should recede behind the glyph rows."""
    print(f"  {pal.GREY}{text}{pal.RESET}", file=stream)


def _kv(label: str, value: str, pal: _Palette, stream) -> None:
    print(f"  {label:<18} {pal.BOLD}{value}{pal.RESET}", file=stream)


def _teaser(text: str, pal: _Palette, stream) -> None:
    """Render a single-line locked upgrade teaser (suppressed by BUDDHI_NO_UPSELL)."""
    if _upsell_suppressed():
        return
    print(f"  {pal.GREY}🔒 {text}{pal.RESET}", file=stream)


# ── Subprocess seam ──────────────────────────────────────────────────────────────

def _default_run(argv, *, timeout=30, input=None, env=None):
    # ``env`` threads an isolated credential environment for the token validator
    # (the candidate CLAUDE_CODE_OAUTH_TOKEN + a stripped set of higher-precedence
    # creds). It defaults to None → subprocess inherits os.environ, so every
    # existing call site is unaffected.
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          input=input, env=env)


def _run_ok(run, argv, *, timeout=15) -> Tuple[bool, str]:
    """``(returncode == 0, stdout)`` for ``argv``; any exception → ``(False, "")``."""
    try:
        r = run(argv, timeout=timeout)
    except Exception:
        return False, ""
    return getattr(r, "returncode", 1) == 0, (getattr(r, "stdout", "") or "")


# ── Candidate-token validation (verify BEFORE storing the secret) ──────────────────
# Credentials that OUTRANK CLAUDE_CODE_OAUTH_TOKEN (#5) in Claude Code's auth
# precedence. They MUST be stripped from the validation subprocess env, else the
# candidate token is never exercised — Claude authenticates via one of these (or, if
# all absent, the keychain at #6) and a BAD pasted token passes as a FALSE POSITIVE.
# ANTHROPIC_API_KEY / _AUTH_TOKEN are #2–#3; the Bedrock/Vertex/Foundry switches (#1)
# reroute auth to a cloud provider entirely. The #4 credential — ``apiKeyHelper``, a
# script in settings.json — is NOT an env var and cannot be popped here; it is
# neutralised by pointing CLAUDE_CONFIG_DIR at an empty dir + running from it (below).
_HIGHER_PRECEDENCE_CREDS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
# Token-VALUE-rejection signature from a DIRECT `claude -p ping` (a mis-pasted /
# expired / wrong-account CLAUDE_CODE_OAUTH_TOKEN). Unlike the repo-run signature
# (detectors.AUTH_FAILED_RE, which excludes a bare "401" to avoid the
# App-not-installed confound), a bare 401 from a direct CLI ping unambiguously means
# the pasted token is bad — there is no App-not-installed case on a local ping — so a
# bare 401 IS matched here. An ambiguous/blank failure is NOT matched: it must read
# "unknown", never "invalid".
_TOKEN_INVALID_RE = re.compile(
    r"\b401\b|unauthorized|invalid bearer|invalid.*token|expired|"
    r"authentication_error|authentication_failed",
    re.IGNORECASE,
)


def _validate_claude_token(token, *, run, which=shutil.which) -> Tuple[str, str]:
    """Tri-state validation of a candidate ``CLAUDE_CODE_OAUTH_TOKEN`` via a headless
    ``claude -p ping --model haiku`` in an ISOLATED subprocess env. Returns
    ``(state, detail)`` with ``state`` in {"valid", "invalid", "unknown"}; ``detail``
    is a short human message that NEVER contains the token (``""`` when there is none
    to add). The token is passed ONLY via ``env`` — never on argv or in a log line.

    Why this EXACT mechanism (load-bearing — do not re-derive):
      * No ``claude`` on PATH → ``"unknown"``: we cannot test, so never block setup.
      * ``-p ping --model haiku`` is the cheapest real round-trip. NEVER ``--bare``:
        bare mode IGNORES CLAUDE_CODE_OAUTH_TOKEN and would test the operator's local
        login → a false positive (a bad token reads valid).
      * Env-credential isolation: the child env copies ``os.environ``, SETS
        CLAUDE_CODE_OAUTH_TOKEN to the candidate, and POPS every higher-precedence
        credential (:data:`_HIGHER_PRECEDENCE_CREDS`), so a bad value 401s here
        instead of silently passing via a higher cred or the keychain.
      * Settings-FILE isolation closes the one credential that is NOT an env var:
        ``apiKeyHelper`` (a settings.json script) ranks ABOVE the OAuth token (#4),
        so popping env creds can't reach it. Point CLAUDE_CONFIG_DIR at a fresh EMPTY
        dir (no settings.json → no user ``apiKeyHelper``) AND run FROM that dir (no
        project ``.claude/settings.json`` is discovered). ``--settings '{}'`` does
        NOT work — it MERGES, it does not replace. A managed/enterprise
        ``apiKeyHelper`` at precedence #1 is intentionally un-overridable per the
        docs, so on such a machine the result is advisory.
      * returncode 0 (with isolation intact) → ``"valid"``. Non-zero WITH an auth
        signature → ``"invalid"``. Exception / timeout, isolation failure, or
        non-zero WITHOUT a signature → ``"unknown"`` (an ambiguous failure is NEVER
        called invalid).

    The OAuth token minted by ``claude setup-token`` IS valid for this path — do NOT
    switch to ANTHROPIC_API_KEY. No public token-introspection endpoint exists, so a
    tiny model round-trip is the only reliable check."""
    # Defence in depth: a token copied from a wrapped terminal can carry internal
    # whitespace (newline + indent); a real sk-ant-oat token has none, so strip ALL
    # whitespace before the round-trip regardless of how the caller cleaned it.
    token = "".join((token or "").split())
    claude_bin = which("claude")
    if not claude_bin:
        return ("unknown", "Claude CLI not found — the token couldn't be tested.")
    env = dict(os.environ)
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    for _cred in _HIGHER_PRECEDENCE_CREDS:
        env.pop(_cred, None)
    # Settings-FILE isolation, achieved via the process cwd (the run seam does not
    # thread cwd): chdir into the empty CLAUDE_CONFIG_DIR so the spawned `claude`
    # inherits it and discovers neither a user nor a project settings.json. Restore
    # the original cwd in `finally`, before removing the tempdir.
    cfg_dir = None
    prev_cwd = None
    isolated = False
    argv = [claude_bin, "--model", "haiku", "--permission-mode", "bypassPermissions",
            "--strict-mcp-config", "--no-session-persistence", "-p", "ping"]
    try:
        try:
            # realpath so the value matches os.getcwd() after chdir — on macOS
            # mkdtemp returns /var/… but the cwd resolves to /private/var/….
            cfg_dir = os.path.realpath(tempfile.mkdtemp(prefix="buddhi-toktest-"))
            env["CLAUDE_CONFIG_DIR"] = cfg_dir
            prev_cwd = os.getcwd()
            os.chdir(cfg_dir)
            isolated = True
        except Exception:
            isolated = False  # couldn't isolate settings; env-cred pops still apply
        try:
            r = run(argv, timeout=25, env=env)
        except Exception as exc:
            return ("unknown", f"The test couldn't run ({type(exc).__name__}).")
        if getattr(r, "returncode", 1) == 0:
            # An apiKeyHelper could have authenticated a BAD token if settings
            # isolation failed, so a pass without it is untrustworthy → "unknown".
            if not isolated:
                return ("unknown", "Settings isolation failed — result untrustworthy.")
            return ("valid", "The token authenticated.")
        blob = (getattr(r, "stdout", "") or "") + "\n" + (getattr(r, "stderr", "") or "")
        if _TOKEN_INVALID_RE.search(blob):
            if not isolated:
                return ("unknown", "Settings isolation failed — result untrustworthy.")
            return ("invalid", "The token was rejected (authentication failed).")
        return ("unknown", "The test was inconclusive (no clear authentication error).")
    finally:
        if prev_cwd is not None:
            try:
                os.chdir(prev_cwd)
            except Exception:
                pass
        if cfg_dir:
            shutil.rmtree(cfg_dir, ignore_errors=True)


# ── Pure helpers (unit-tested directly) ───────────────────────────────────────────

def gh_version_ok(version_str: str) -> Tuple[Optional[Tuple[int, int]], bool]:
    """Parse ``gh version X.Y …`` → ``((major, minor), >= 2.87)``. Unparseable →
    ``(None, False)``."""
    m = re.search(r"gh version (\d+)\.(\d+)", version_str or "")
    if not m:
        return None, False
    ver = (int(m.group(1)), int(m.group(2)))
    return ver, ver >= _GH_MIN


def recommend_plan(tiers: Dict[str, bool]) -> str:
    """Recommend a plan from the reachable model tiers: Opus → ``max-5x``;
    Sonnet only → ``pro``; neither → the default plan."""
    if tiers.get("opus"):
        return "max-5x" if "max-5x" in plan_profile.known_plans() else config.DEFAULT_PLAN
    if tiers.get("sonnet"):
        return "pro" if "pro" in plan_profile.known_plans() else config.DEFAULT_PLAN
    return config.DEFAULT_PLAN


def infer_repo(run, cwd: Optional[str] = None) -> Optional[str]:
    """``owner/repo`` parsed from the cwd's ``git remote get-url origin``, or None."""
    try:
        r = run(["git", "-C", cwd or ".", "remote", "get-url", "origin"], timeout=10)
    except Exception:
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    url = (getattr(r, "stdout", "") or "").strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def repo_toplevel(run, cwd: Optional[str] = None) -> Optional[str]:
    try:
        r = run(["git", "-C", cwd or ".", "rev-parse", "--show-toplevel"], timeout=10)
    except Exception:
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    return (getattr(r, "stdout", "") or "").strip() or None


# ── GitHub-fact probes (the provisioning engine reads these) ──────────────────────

def _owner_type(owner: Optional[str], *, run) -> Optional[str]:
    """Whether a repo's ``owner`` is a GitHub Organization or a personal User
    account, via ``gh api users/<owner> --jq .type`` (the Users API reports both
    kinds and returns "Organization" for org logins). ``owner`` may be a bare login
    or an ``owner/repo`` slug — the repo part is dropped. Returns
    ``"Organization"`` / ``"User"``, or ``None`` when the lookup can't run (no
    owner, a malformed login, a gh error, or an unexpected value); callers treat
    ``None`` as "not an org" and stay on the safe repo-scoped path."""
    if not owner:
        return None
    login = str(owner).split("/", 1)[0].strip()
    if not login or not re.match(r"^[A-Za-z0-9_-]+$", login):
        return None
    ok, out = _run_ok(run, ["gh", "api", f"users/{login}", "--jq", ".type"])
    if not ok:
        return None
    t = out.strip()
    return t if t in ("Organization", "User") else None


def _default_branch(repo: Optional[str], *, run) -> Optional[str]:
    """The repo's default branch name (the ONLY branch an issue_comment workflow
    runs from), via ``gh repo view``. ``None`` when it can't be read."""
    if not repo:
        return None
    ok, out = _run_ok(run, ["gh", "repo", "view", repo, "--json", "defaultBranchRef",
                            "--jq", ".defaultBranchRef.name"])
    if not ok:
        return None
    return out.strip() or None


def _current_branch(cwd: Optional[str], *, run) -> Optional[str]:
    """The checked-out branch name in ``cwd``, or ``None`` (no cwd, not a repo,
    detached HEAD, or git unavailable). A detached HEAD reports 'HEAD' from
    rev-parse — treated as unknown since it is not a branch the workflow could live
    on."""
    if not cwd:
        return None
    ok, out = _run_ok(run, ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"])
    if not ok:
        return None
    b = out.strip()
    return b if (b and b != "HEAD") else None


def build_config(plan: str, repo: Optional[str], cwd: Optional[str],
                 active_reviewers: Sequence[str], auto_on_open: Dict[str, bool]) -> Dict[str, Any]:
    """The free config dict — ONLY the free keys. No budget / monitoring / push-channel
    key is ever emitted (those live nowhere in the free config)."""
    cfg: Dict[str, Any] = {
        "plan": plan,
        "active_reviewers": list(active_reviewers),
        "auto_on_open": {b: bool(v) for b, v in auto_on_open.items()},
        "notifications": "console",
    }
    if repo:
        cfg["repo"] = repo
    if cwd:
        cfg["cwd"] = cwd
    return cfg


def merge_preserving(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay ``new`` onto ``existing``, preserving any unknown existing keys
    (e.g. a hand-added ``repos:`` block). Free retires no key."""
    merged = dict(existing or {})
    merged.update(new)
    return merged


def write_config(cfg: Dict[str, Any], path: Path) -> bool:
    """Atomically write ``cfg`` to ``path`` (temp file + ``os.replace``) at 0600."""
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is a hard dep
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_str = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        tmp = Path(tmp_str)
        try:
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = None  # os.fdopen took ownership; don't close in the except block
                yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(path))
        except BaseException:
            if fd is not None:
                os.close(fd)
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


# ── Steps ─────────────────────────────────────────────────────────────────────

def step_doctor(*, run, which, pal, stream) -> Dict[str, Any]:
    """Step 1 — tooling doctor. Reports each check; never hard-exits (setup stays
    useful even if a tool is missing — the loop's own preflight catches it)."""
    _panel("Step 1 — Tooling doctor", ["Checking the tools the loop depends on."], pal, stream)
    _row("step", "Checking Claude CLI…", pal, stream)
    claude_bin = which("claude")
    if claude_bin:
        _row("ok", f"Claude CLI found ({claude_bin})", pal, stream)
    else:
        _row("warn", "Claude CLI not found — install it to run reviews/fixes: "
                     "https://claude.com/claude-code", pal, stream)

    _row("step", f"Checking gh CLI (≥ {_GH_MIN[0]}.{_GH_MIN[1]})…", pal, stream)
    gh_present, gh_out = _run_ok(run, ["gh", "--version"])
    ver, ok_ver = gh_version_ok(gh_out)
    if not gh_present:
        _row("warn", "gh CLI not found — install GitHub CLI (https://cli.github.com), "
                     "then run `gh auth login`", pal, stream)
    elif ok_ver:
        _row("ok", f"gh {ver[0]}.{ver[1]} (≥ {_GH_MIN[0]}.{_GH_MIN[1]})", pal, stream)
    else:
        shown = f"{ver[0]}.{ver[1]}" if ver else "unknown"
        _row("warn", f"gh {shown} is below {_GH_MIN[0]}.{_GH_MIN[1]} — Copilot review "
                     "needs the newer gh; upgrade: https://cli.github.com", pal, stream)

    gh_auth, _ = _run_ok(run, ["gh", "auth", "status"]) if gh_present else (False, "")
    _row("ok" if gh_auth else "warn",
         "gh authenticated" if gh_auth else "gh not authenticated — run `gh auth login`",
         pal, stream)

    tiers: Dict[str, bool] = {}
    if claude_bin:
        _row("step", "Probing reachable model tiers…", pal, stream)
        for tier in _MODEL_TIERS:
            ok, _ = _run_ok(run, [claude_bin, "--model", tier, "--permission-mode",
                                  "bypassPermissions", "--strict-mcp-config", "-p", "ping"],
                            timeout=60)
            tiers[tier] = ok
            # Per-tier rows tell the user which tiers aren't on their plan (a single
            # summary row would hide that).
            _row("ok" if ok else "warn",
                 tier if ok else f"{tier} — unreachable (plan limit, auth, or network)", pal, stream)

    # Fix-and-rerun footer when a tooling check (Claude CLI, or gh presence / version /
    # auth) warned. OSS never hard-exits — it points the user at the ⚠ rows instead.
    if not (claude_bin and gh_present and ok_ver and gh_auth):
        _row("warn", "Fix the ⚠ items above, then re-run /review-pr setup.", pal, stream)

    return {"claude_bin": claude_bin, "gh_ok": bool(gh_present and ok_ver),
            "gh_auth": gh_auth, "tiers": tiers}


def step_plan(doctor: Dict[str, Any], *, pal, stream, single_select=single_select,
              input_fn=input) -> str:
    """Step 2 — plan selection."""
    plans = plan_profile.known_plans()
    options: List[Tuple[str, str]] = []
    for name in plans:
        # Show the model that hard calls resolve to under this plan (informational).
        model = plan_profile.model_for("fix-substantive", plan=name)
        options.append((name, f"fixes run {model}"))
    rec = recommend_plan(doctor.get("tiers", {}))
    preselect = plans.index(rec) if rec in plans else 0
    _panel("Step 2 — Claude plan",
           ["Drives which model each role runs, resolved from your Claude plan "
            "(Pro uses Sonnet; Max and up use Opus for the hard calls)."],
           pal, stream)
    idx = single_select("  Which Claude plan are you on?", options, preselect=preselect,
                        pal=pal, stream=stream, input_fn=input_fn)
    return plans[idx]


def step_repo(preset_repo: Optional[str], *, run, pal, stream, input_fn=input) -> Tuple[Optional[str], Optional[str]]:
    """Step 3 — repo binding."""
    _panel("Step 3 — Repo binding", ["Which repository should the loop review?"], pal, stream)
    cwd = repo_toplevel(run)
    inferred = infer_repo(run) if not preset_repo else None
    repo = preset_repo or inferred
    if repo:
        if not preset_repo and inferred:
            _row("info", f"Inferred from git remote: {inferred}", pal, stream)
        use = _ask_yes_no(f"Use {repo}?", default=True, input_fn=input_fn, stream=stream)
        if not use:
            try:
                manual = input_fn("  Enter owner/repo (or blank to skip): ").strip()
            except (EOFError, KeyboardInterrupt):
                manual = ""
            repo = manual or None
    else:
        try:
            repo = input_fn("  Could not infer the repo. Enter owner/repo (or blank to skip): ").strip() or None
        except (EOFError, KeyboardInterrupt):
            repo = None
    if repo:
        _row("ok", f"Bound to {repo}", pal, stream)
    else:
        _row("info", "No repo bound — it is inferred at runtime from the cwd's git remote", pal, stream)
    return repo, cwd


def step_budgets_locked(*, pal, stream) -> None:
    """Step 4 — provider budgets (paid). A locked teaser; persists nothing."""
    _panel("Step 4 — Provider budgets", [], pal, stream)
    _teaser(_BUDGETS_TEASER, pal, stream)


def _probe_claude_workflow(repo: Optional[str], run) -> Tuple[bool, str]:
    """``(workflow present on the default branch, note)`` via ``gh api``."""
    if not repo:
        return False, "no repo bound — cannot check for the Claude workflow"
    ok, out = _run_ok(run, ["gh", "api",
                            f"repos/{repo}/contents/.github/workflows/claude-code-review.yml",
                            "--jq", ".content"])
    if ok and out.strip():
        return True, "claude-code-review.yml present on the default branch"
    return False, "claude-code-review.yml missing (offer to add it)"


def _workflow_template_path() -> Path:
    return Path(__file__).parent / "skills" / "review-pr" / "references" / "claude-code-review.yml"


def _write_workflow_template(cwd: Optional[str]) -> Optional[Path]:
    """Copy the bundled ``claude-code-review.yml`` into ``<cwd>/.github/workflows/``.
    Returns the written path, or None on failure / missing template."""
    template = _workflow_template_path()
    if not (cwd and template.exists()):
        return None
    try:
        dest_dir = Path(cwd) / ".github" / "workflows"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "claude-code-review.yml"
        shutil.copyfile(template, dest)
        return dest
    except OSError:
        return None


def _create_file_pr(repo: str, default_branch: str, path: str, content: str,
                    message: str, title: str, branch: str, *,
                    recovery_path: Optional[str] = None,
                    body: Optional[str] = None, run) -> Tuple[bool, str]:
    """Open a PR that CREATES OR UPDATES an arbitrary file (``path``, text
    ``content``) on the repo's DEFAULT branch entirely server-side — no local
    checkout is touched, so the user's feature branch and working tree are left
    exactly as they were:

      1. read the default branch's head SHA,
      2. create a fresh ref ``branch`` off it (reusing it if a partial earlier run
         already created the branch),
      3. PUT ``content`` to ``path`` on that ref (one commit, message ``message``),
         supplying the existing blob SHA when ``path`` is already present so an
         UPDATE doesn't 422,
      4. open a PR titled ``title`` (base=default, head=branch) with body ``body``.

    Generic by design — it installs the Claude review workflow and the
    ``ready-for-ci`` template, and UPDATES either to a newer bundled version. Because
    the branch is cut from the default branch (which already carries any existing
    file), the existing blob SHA is probed UNCONDITIONALLY before the PUT — absent
    (a fresh create) it is a plain add, present it is an in-place update.
    ``recovery_path`` (defaults to ``path``) is the path probed for that blob SHA;
    ``body`` overrides the default PR body. Returns ``(ok, detail)`` — ``detail`` is
    the PR URL on success, else a short reason. The PUT needs a token with the
    ``workflow`` scope; without it GitHub rejects the write and the caller falls back
    to a local copy."""
    path = str(path).lstrip("/")
    recovery_path = str(recovery_path or path).lstrip("/")
    enc_path = urllib.parse.quote(path, safe="/")
    enc_recovery = urllib.parse.quote(recovery_path, safe="/")
    enc_branch = urllib.parse.quote(branch, safe="")
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    pr_body = body or (f"Adds `{path}` to the default branch. "
                       "(Opened by the Buddhi setup wizard.)")
    try:
        head = run(["gh", "api", f"repos/{repo}/git/ref/heads/{default_branch}",
                    "--jq", ".object.sha"], timeout=20)
        if getattr(head, "returncode", 1) != 0 or not (getattr(head, "stdout", "") or "").strip():
            return (False, "couldn't read the default branch head")
        sha = head.stdout.strip()
        ref = run(["gh", "api", f"repos/{repo}/git/refs",
                   "-f", f"ref=refs/heads/{branch}", "-f", f"sha={sha}"], timeout=20)
        if getattr(ref, "returncode", 1) != 0:
            # Branch creation failed — it may already exist from a partial earlier
            # run. Reuse it (so recovery needs no manual cleanup) but ONLY after
            # confirming it really exists; otherwise the create genuinely failed.
            probe = run(["gh", "api", f"repos/{repo}/git/ref/heads/{enc_branch}"], timeout=15)
            if getattr(probe, "returncode", 1) != 0:
                return (False, f"couldn't create branch '{branch}'")
        # The Contents API requires the existing blob SHA to UPDATE a file (a PUT
        # over one 422s without it). The branch is cut from the default branch, so
        # if `recovery_path` is already there it carries the SHA we need; probe for it
        # unconditionally — absent (a fresh CREATE) → no SHA, a plain add.
        sha_args: List[str] = []
        file_probe = run(["gh", "api",
                          f"repos/{repo}/contents/{enc_recovery}?ref={enc_branch}",
                          "--jq", ".sha"], timeout=15)
        if getattr(file_probe, "returncode", 1) == 0 and (file_probe.stdout or "").strip():
            sha_args = ["-f", f"sha={file_probe.stdout.strip()}"]
        put = run(["gh", "api", "-X", "PUT",
                   f"repos/{repo}/contents/{enc_path}",
                   "-f", f"message={message}",
                   "-f", f"content={b64}", "-f", f"branch={branch}"] + sha_args, timeout=20)
        if getattr(put, "returncode", 1) != 0:
            return (False, "couldn't write the file "
                           "(for workflow paths the gh token may need the 'workflow' scope)")
        pr = run(["gh", "pr", "create", "--repo", repo,
                  "--base", default_branch, "--head", branch,
                  "--title", title,
                  "--body", pr_body], timeout=30)
    except Exception as exc:
        return (False, type(exc).__name__)
    if getattr(pr, "returncode", 1) != 0:
        # gh pr create fails when a PR for this branch ALREADY exists (a prior setup
        # run opened it) — the branch push above still succeeds, so this is not a real
        # failure. Detect the existing PR and return it, mirroring create-pr.sh's
        # idempotent gh-pr-view fallback, instead of a scary "opening the PR failed".
        try:
            existing = run(["gh", "pr", "view", branch, "--repo", repo,
                            "--json", "url", "--jq", ".url"], timeout=20)
            ex_url = (getattr(existing, "stdout", "") or "").strip()
            if getattr(existing, "returncode", 1) == 0 and ex_url:
                return (True, ex_url)
        except Exception:
            pass
        return (False, "branch pushed but opening the PR failed")
    out = (getattr(pr, "stdout", "") or "").strip()
    url = out.splitlines()[-1] if out else "(PR opened)"
    return (True, url)


# The muted reassurance shown after an update PR — the old file is never lost.
_REVERT_NOTE = ("Your previous version is preserved in the PR's git history — "
                "revert the PR to roll back.")


def _installed_managed_file_text(repo: Optional[str], dest_path: str, run) -> Optional[str]:
    """Decoded text of a managed file as it currently exists on ``repo``'s default
    branch, or ``None`` if absent / unreadable. Read so the wizard can compare the
    INSTALLED copy's ``buddhi-managed-version`` marker against the bundled template
    and offer an update when it is older. Best-effort — never raises."""
    if not repo:
        return None
    ok, out = _run_ok(run, ["gh", "api", f"repos/{repo}/contents/{dest_path}",
                            "--jq", ".content"])
    if not ok or not out.strip():
        return None
    try:
        # The Contents API returns base64 (with embedded newlines gh strips via --jq).
        return base64.b64decode(out).decode("utf-8", "replace")
    except Exception:
        return None


def _offer_update_managed_file(repo: str, default: Optional[str], spec: Dict[str, object],
                               installed_text: Optional[str], *, run, pal, stream,
                               input_fn=input) -> Optional[str]:
    """Offer a server-side PR that updates a managed file IN PLACE when the installed
    copy is older than the bundled template (verbatim overwrite — for files with NO
    per-install baking, i.e. the Claude workflow; ``ready-for-ci`` re-bakes its CI
    command via its own installer). The old version stays in the update PR's git
    history. Returns ``'pr'`` (update PR opened) or ``None`` (already current /
    unknown / declined / non-TTY / no default branch / write failed)."""
    template = spec["template"]  # type: ignore[index]
    name = str(spec["name"])
    shipped = managed_files.shipped_version(template)  # type: ignore[arg-type]
    installed_v = managed_files.file_version(installed_text)
    if not managed_files.needs_update(installed_v, shipped):
        return None
    if not default:
        return None
    cur = "unversioned" if installed_v is None else f"v{installed_v}"
    _row("warn", f"{name} — your installed copy ({cur}) is older than the bundled "
                 f"v{shipped}. Updating it brings the latest fixes (e.g. the "
                 "auth-failure guard that turns a silent 401 red).", pal, stream)
    if not _is_tty():
        _row("info", f"Re-run setup in a terminal to update {name} to v{shipped}.",
             pal, stream)
        return None
    if not _ask_yes_no(f"Open a PR to update {name} to v{shipped} on '{default}'?",
                       default=True, input_fn=input_fn, stream=stream):
        return None
    try:
        content = Path(template).read_text(encoding="utf-8")  # type: ignore[arg-type]
    except OSError as exc:
        _row("warn", f"Couldn't read the bundled {name}: {exc}", pal, stream)
        return None
    dest = str(spec["dest"])
    slug = name.replace(".yml", "").replace(".", "-")
    ok, detail = _create_file_pr(
        repo, default, dest, content,
        f"Update {name} to v{shipped} (Buddhi setup)",   # message
        f"Update {name} to v{shipped}",                  # title
        f"buddhi/update-{slug}-v{shipped}",              # branch
        body=(f"Updates `{dest}` to Buddhi managed version v{shipped}. "
              f"(Opened by the Buddhi setup wizard.)\n\n{_REVERT_NOTE}"),
        run=run)
    if ok:
        _row("ok", f"Opened a PR to update {name}: {detail}", pal, stream)
        print(f"  {pal.GREY}{_REVERT_NOTE}{pal.RESET}", file=stream)
        _row("info", f"Merge that PR to put {name} v{shipped} on '{default}'.", pal, stream)
        return "pr"
    _row("warn", f"Couldn't open the update PR automatically ({detail}). You can copy "
                 f"the bundled {name} in by hand.", pal, stream)
    return None


def _offer_install_claude_workflow(repo: str, cwd: Optional[str], *, run, pal, stream,
                                   input_fn=input) -> Optional[str]:
    """Offer to install ``claude-code-review.yml`` when it is absent from the
    default branch. An issue_comment workflow runs ONLY from the default branch, so
    a silent write into a feature checkout never enables reviews. On a feature
    branch this offers the SERVER-SIDE route first — a ``gh``-api PR that lands the
    workflow on the default branch on a fresh branch, leaving the local checkout
    untouched — and falls back to a local copy only if that fails or is declined.
    Returns ``'pr'`` (a PR was opened), ``True`` (written into the local checkout),
    or ``None`` (nothing done)."""
    template = _workflow_template_path()
    if not template.exists():
        return None
    try:
        template_text: Optional[str] = template.read_text(encoding="utf-8")
    except OSError:
        template_text = None

    cur = _current_branch(cwd, run=run)
    default = _default_branch(repo, run=run)
    on_feature = bool(cur and default and cur != default)

    if on_feature and template_text is not None and _is_tty():
        _row("warn", f"This checkout is on '{cur}', not the default branch "
                     f"'{default}'. GitHub runs the review workflow only from the "
                     f"default branch, so committing it to '{cur}' won't enable "
                     f"Claude reviews until it lands on '{default}'.", pal, stream)
        if _ask_yes_no(f"Open a PR that adds the workflow to '{default}' on a fresh "
                       "branch instead?", default=True, input_fn=input_fn, stream=stream):
            ok, detail = _create_file_pr(
                repo, default,
                ".github/workflows/claude-code-review.yml",       # path
                template_text,                                    # content
                "Add Claude code-review workflow (Buddhi setup)",  # message
                "Add Claude code-review workflow",                # title
                "buddhi/add-claude-review-workflow",              # branch
                run=run)
            if ok:
                _row("ok", f"Opened a PR to add the workflow: {detail}", pal, stream)
                print(f"  {pal.GREY}{_ACTIONS_NOTE}{pal.RESET}", file=stream)
                _row("info", f"Merge that PR to put the workflow on '{default}' (the "
                             "default branch), then set the CLAUDE_CODE_OAUTH_TOKEN "
                             "secret below.", pal, stream)
                return "pr"
            _row("warn", f"Could not open the PR automatically ({detail}). Falling "
                         "back to a local copy you can land yourself.", pal, stream)
            # fall through to the local-write offer

    # Local-copy fallback (the historical behavior).
    local_file = Path(cwd) / ".github" / "workflows" / "claude-code-review.yml" if cwd else None
    if local_file and local_file.exists():
        _row("info", f"Claude — local workflow file already exists at {local_file}. "
                     "Commit and push it to the default branch to activate.", pal, stream)
        return True
    if cwd and _ask_yes_no("Write the bundled claude-code-review.yml into "
                           ".github/workflows/ now?", default=True, input_fn=input_fn,
                           stream=stream):
        dest = _write_workflow_template(cwd)
        if dest:
            _row("ok", f"Wrote {dest} — commit + push it, then merge into '{default}' "
                       "to enable Claude reviews.", pal, stream)
            print(f"  {pal.GREY}{_ACTIONS_NOTE}{pal.RESET}", file=stream)
            return True
        _row("warn", "Could not write the workflow template", pal, stream)
    return None


# ── Label-gated CI: the ready-for-ci workflow installer (F4) ───────────────────────

# The placeholder line the bundled ``tests-ready-for-ci.yml`` carries in its single
# ``run:`` step. The wizard substitutes the repo's detected+confirmed CI command for
# it at install time, so the gate runs a REAL command instead of a failing
# placeholder the user must hand-edit.
_CI_COMMAND_MARKER = "__BUDDHI_CI_COMMAND__"


def _ready_for_ci_template_path() -> Path:
    return Path(__file__).parent / "skills" / "review-pr" / "references" / "tests-ready-for-ci.yml"


def _probe_ready_for_ci_workflow(repo: Optional[str], run) -> bool:
    """Whether the generic label gate ``tests-ready-for-ci.yml`` is already on the
    repo's DEFAULT branch — ONE ``gh api …/contents`` fetch, mirroring
    :func:`_probe_claude_workflow`. Returns ``True`` (present), ``False`` (absent,
    unreadable, no repo, or the fetch failed). This is the P7 #4 probe-before-install:
    it SKIPS re-offering the install PR when the gate is already there; without it a
    re-run opens a redundant second identical PR every time."""
    if not repo:
        return False
    ok, out = _run_ok(run, ["gh", "api",
                            f"repos/{repo}/contents/.github/workflows/tests-ready-for-ci.yml",
                            "--jq", ".content"])
    stripped = out.strip()
    return bool(ok and stripped and stripped != "null")


def _pyproject_test_extra(pyproject_text: str) -> bool:
    """Whether ``pyproject_text`` declares a ``test`` extra under
    ``[project.optional-dependencies]``. A line-oriented text scan, in the same
    read-only style as the rest of the detector (the stdlib has no TOML parser
    on every supported Python, and a malformed file must degrade to False, never
    raise)."""
    in_section = False
    for ln in pyproject_text.splitlines():
        s = ln.strip()
        if s.startswith("["):
            header = s.split("#")[0].strip().replace(" ", "").replace("\t", "")
            in_section = header == "[project.optional-dependencies]"
            continue
        if in_section and re.match(r"""^(?:test|"test"|'test')\s*=""", s):
            return True
    return False


def _detect_ci_command(cwd: Optional[str]) -> Optional[str]:
    """Best-effort detect a repo's CI/test command from its LOCAL checkout, so the
    label gate is auto-wired with a real command instead of a failing placeholder.
    Returns a shell command string, or ``None`` when nothing recognisable is found
    (the caller then asks, pre-filling this as the default). Only READS files — it
    never executes them, and never assumes a stack the repo does not actually show."""
    if not cwd:
        return None
    root = Path(cwd)
    if not root.is_dir():
        return None

    def _reads(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        except OSError:
            return ""

    # A Makefile ``ci:``/``test:`` target — honour the project's own entry point first.
    mk = ""
    for name in ("Makefile", "makefile", "GNUmakefile"):
        if (root / name).is_file():
            mk = _reads(root / name)
            break
    if re.search(r"(?m)^ci\s*:(?!=)", mk):
        return "make ci"
    if re.search(r"(?m)^test\s*:(?!=)", mk):
        return "make test"
    # Node — a package.json with a REAL ``test`` script. SKIP npm's "no test
    # specified" default (it itself ``exit 1``s); baking that would re-create the
    # very red gate this auto-wire exists to kill.
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else None
        test_script = scripts.get("test") if isinstance(scripts, dict) else None
        if (isinstance(test_script, str) and test_script.strip()
                and "no test specified" not in test_script and "exit 1" not in test_script):
            if (root / "yarn.lock").is_file():
                return "yarn install && yarn test"
            if (root / "pnpm-lock.yaml").is_file():
                return "pnpm install && pnpm test"
            return "npm ci && npm test" if (root / "package-lock.json").is_file() else "npm install && npm test"
    # Python — a package manifest AND real evidence of a test suite (a tests dir or
    # a pytest config). Without that evidence ``pytest`` exits non-zero on a repo with
    # no tests yet → a red gate, so fall through to ASK rather than bake a guess.
    has_manifest = any((root / n).is_file() for n in ("pyproject.toml", "setup.py", "setup.cfg"))
    has_tests = (
        (root / "tests").is_dir() or (root / "test").is_dir()
        or (root / "conftest.py").is_file() or (root / "pytest.ini").is_file()
        or (root / "tox.ini").is_file()
        or "[tool:pytest]" in _reads(root / "setup.cfg")
        or "[tool.pytest" in _reads(root / "pyproject.toml")
    )
    if has_manifest and has_tests:
        # A declared `test` extra means a plain `pip install -e .` gate cannot
        # even import pytest — the baked command must install the extra or every
        # gated run dies in seconds on `No module named pytest`.
        if _pyproject_test_extra(_reads(root / "pyproject.toml")):
            return "python -m pip install -e '.[test]' && python -m pytest"
        return "python -m pip install -e . && python -m pytest"
    # Go / Rust.
    if (root / "go.mod").is_file():
        return "go test ./..."
    if (root / "Cargo.toml").is_file():
        return "cargo test"
    return None


def _bake_ci_command(content: str, command: str) -> str:
    """Substitute ``command`` for the ``_CI_COMMAND_MARKER`` line in the label-gated
    CI template, preserving the YAML ``run: |`` block indentation so a multi-line
    command stays valid. Returns the wired template text."""
    out: List[str] = []
    for ln in content.splitlines(keepends=True):
        if ln.strip() == _CI_COMMAND_MARKER:
            indent = ln[: len(ln) - len(ln.lstrip())]
            ending = "\r\n" if ln.endswith("\r\n") else "\n"
            for cmd_line in (command.splitlines() or [command]):
                out.append(f"{indent}{cmd_line}{ending}")
        else:
            out.append(ln)
    return "".join(out)


# Shell text that cannot be safely collapsed onto one `&&`-joined line: ANY
# standalone shell control-flow keyword (an `if` block's commands cannot be
# `&&`-chained without repositioning its `then`/`fi`; a bare `fi`/`done` closer
# would become the broken `… && fi`), a function-body `{` opener at end of
# line, or a heredoc. Matching any keyword ANYWHERE in a joined line is
# deliberately aggressive: a false positive merely falls back to detection,
# while a miss bakes a gate that syntax-errors (or worse, silently weakens).
# A SINGLE-line command never joins, so it is exempt (handled by the caller).
_UNJOINABLE_SHELL_RE = re.compile(
    r"(?:^|[\s;&|])(?:if|then|elif|else|fi|for|while|until|do|done|case|esac|"
    r"select)\b"
    r"|(?:\{$)|<<"
)

# An unquoted trailing shell comment (`#` at a word start — after whitespace OR
# an operator, since `;`/`&`/`|` end a word: bash reads `echo x;# note` as a
# comment too). Joining anything AFTER such a line would land inside the
# comment — `pip install x  # editable && pytest` silently never runs pytest,
# turning the gate vacuously green — so any NON-FINAL line carrying one makes
# the whole chain unextractable. A WORD-glued `#` (e.g. pip's `…#egg=name` URL
# fragment or `${var#prefix}`) is not a comment and stays fine.
_TRAILING_COMMENT_RE = re.compile(r"(?:^|[\s;&|()])#")


def _ends_in_continuation(s: str) -> bool:
    """Whether ``s`` ends in a REAL backslash line-continuation: an ODD run of
    trailing backslashes. An even run is escaped literal backslashes — the line
    is complete, and whatever follows is a separate command that must be
    ``&&``-joined, never folded in as arguments (`echo built \\\\` + `false`
    folded would run `false` as echo arguments — a vacuously green gate)."""
    return (len(s) - len(s.rstrip("\\"))) % 2 == 1


def _join_shell_lines(lines: Sequence[str]) -> Optional[str]:
    """Collapse the (stripped, non-empty, non-comment) lines of one ``run:``
    block into a single shell command chain. Consecutive lines are joined with
    `` && ``; a line already ending in a connector (``&&``/``||``/``|``/``;``/
    ``&``) or starting with one is joined with a space, and a trailing backslash
    continuation is folded into its next line — so an already-chained or wrapped
    command round-trips without doubled operators. Returns ``None`` when a line
    carries something a one-line `` && `` chain cannot express: control flow, a
    heredoc, or an unquoted trailing comment on any line that is not the last
    (everything joined after it would be swallowed by the comment). A SINGLE
    line needs no joining at all, so it round-trips verbatim — preserving is
    always safe when nothing is transformed."""
    if len(lines) == 1:
        return lines[0]
    out = ""
    for i, ln in enumerate(lines):
        if _UNJOINABLE_SHELL_RE.search(ln):
            return None
        if i < len(lines) - 1 and _TRAILING_COMMENT_RE.search(ln):
            return None
        if not out:
            out = ln
        elif _ends_in_continuation(out):
            out = out[:-1].rstrip() + " " + ln
        elif out.endswith(("&&", "||", "|", ";", "&")) or ln.startswith(("&&", "||", "|")):
            out += " " + ln
        else:
            out += " && " + ln
    return out or None


def _installed_ci_command(installed_text: Optional[str]) -> Optional[str]:
    """The CI command the INSTALLED label gate actually runs — extracted from
    the workflow text the version check already fetched — so the UPDATE path can
    re-bake the user's real command instead of flattening it back to the generic
    detection. (Live incident, 2026-07-01: a versioned update re-baked the
    auto-detected ``pip install -e .`` over a hand-wired
    ``pip install -e '.[test]' …`` gate — every gated run then failed in seconds
    on ``No module named pytest``, and the gate's extra scan step silently
    vanished.)

    The gate job's steps are scanned in order and every ``run:`` value is
    collected (``uses:`` steps such as checkout are skipped). Each step's value
    is ONE opaque command string, stripped; its internal newlines — and, across
    steps, the step boundaries — are joined with `` && `` (the baked template
    has a single command slot). Blank, comment-only, and never-baked
    ``_CI_COMMAND_MARKER`` lines are dropped. Returns ``None`` when nothing is
    extractable: unparseable / non-mapping YAML, no ``ci`` (or single) job, no
    ``run:`` content, or shell control flow that cannot be collapsed onto one
    line — the caller then falls back to detection (TTY) or skips the update
    (non-TTY) instead of re-baking blind."""
    if not installed_text or yaml is None:
        return None
    try:
        doc = yaml.safe_load(installed_text)
    except Exception:
        # Not just yaml.YAMLError: pathological nesting raises RecursionError
        # out of PyYAML's recursive composer, and this best-effort reader of
        # repo-fetched content must degrade to None, never kill the wizard.
        return None
    if not isinstance(doc, dict):
        return None
    # Workflow-level execution context that we cannot carry into the stock template:
    # `env` at the workflow scope injects vars that GitHub applies to every job/step;
    # `defaults.run.working-directory` / `defaults.run.shell` at the workflow level
    # silently change the directory or shell for ALL run steps.  Baking the command
    # without these would produce a gate running in the wrong directory, wrong shell,
    # or missing env vars — fail closed, same as the job-level guards below.
    if doc.get("env"):
        return None
    _wf_defaults_run = doc.get("defaults") or {}
    if isinstance(_wf_defaults_run, dict):
        _wf_defaults_run = _wf_defaults_run.get("run") or {}
    else:
        _wf_defaults_run = {}
    if _wf_defaults_run.get("working-directory") or _wf_defaults_run.get("shell"):
        return None
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        return None
    # The bundled template's job id is `ci`; prefer it, tolerate a renamed single
    # job. Several jobs with no `ci` is ambiguous (jobs run on separate runners —
    # joining their commands would fabricate a serial chain that never existed).
    job = jobs.get("ci") if "ci" in jobs else (
        next(iter(jobs.values())) if len(jobs) == 1 else None)
    if not isinstance(job, dict):
        return None
    steps = job.get("steps")
    if not isinstance(steps, list):
        return None
    # Job-level runner: the stock template pins `runs-on: ubuntu-latest`. A gate
    # customized to a different runner (macos-latest, windows-latest, self-hosted, a
    # `${{ matrix.* }}` expression, or a labels list) carries commands that assume that
    # runner — Xcode, PowerShell, self-hosted tooling — so rebaking them onto
    # ubuntu-latest would run them where those tools do not exist. Fail closed unless
    # the runner is the stock ubuntu-latest (or absent, accepted for robustness — user
    # workflows may omit the key). A single-element list (`[ubuntu-latest]`) is
    # normalized; anything else — another scalar, a multi-label list, or a matrix
    # expression — is treated as non-stock.
    _runs_on = job.get("runs-on")
    if isinstance(_runs_on, list) and len(_runs_on) == 1:
        _runs_on = _runs_on[0]
    if _runs_on is not None and _runs_on != "ubuntu-latest":
        return None
    # Job-level execution context that we cannot carry into the stock template:
    # `defaults.run.working-directory` / `defaults.run.shell` silently change the
    # directory or shell for every run step; `env` at the job level injects vars the
    # commands may rely on.  Baking the bare command text without these would produce
    # a gate that runs from the wrong directory, wrong shell, or missing env vars.
    job_env = job.get("env")
    if job_env:
        return None
    # Job-level service containers (e.g. postgres, redis) are unprovisionable in
    # the stock template's single-step shell; commands that rely on them would fail
    # silently, so treat the gate as unextractable.
    if job.get("services"):
        return None
    # A container-baked job runs inside a custom image with tools/libraries that the
    # stock ubuntu-latest template does not provide; dropping the container key would
    # cause the preserved command to fail or behave differently.
    if job.get("container"):
        return None
    _job_defaults_run = job.get("defaults") or {}
    if isinstance(_job_defaults_run, dict):
        _job_defaults_run = _job_defaults_run.get("run") or {}
    else:
        _job_defaults_run = {}
    if _job_defaults_run.get("working-directory") or _job_defaults_run.get("shell"):
        return None
    commands: List[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        if "run" not in step:
            uses_val = step.get("uses", "")
            # Only the checkout action is safe to skip — any other `uses:` step
            # (e.g. setup-uv, setup-node) provides environment the run commands
            # depend on; baking the run commands without it builds a broken gate.
            if not isinstance(uses_val, str) or not uses_val.startswith("actions/checkout"):
                return None
            # A checkout step with `with:` inputs (submodules, lfs, fetch-depth, path,
            # etc.) configures the tree in ways the stock template's plain checkout
            # cannot reproduce; the preserved command would then run against a different
            # or incomplete tree.
            if step.get("with"):
                return None
            continue
        # Step-level context fields that we cannot preserve in the template's single
        # command slot: working-directory, shell, and env all affect how or where the
        # command runs, and the stock template carries none of them.
        if step.get("working-directory") or step.get("shell") or step.get("env"):
            return None
        run_val = step["run"]
        if not isinstance(run_val, str):
            # A non-string `run:` (YAML parsed `run: false` into a boolean; the
            # Actions runner would still execute it as text). Dropping the step
            # would silently WEAKEN the chain — unextractable instead.
            return None
        lines: List[str] = []
        for raw_ln in run_val.strip().splitlines():
            ln = raw_ln.strip()
            if ln and not ln.startswith("#") and ln != _CI_COMMAND_MARKER:
                if ln.endswith("\\") and not raw_ln.rstrip("\r\n").endswith("\\"):
                    # Backslash followed by trailing WHITESPACE: bash escapes
                    # the whitespace and the line is COMPLETE — but stripping
                    # just erased the distinction the continuation check keys
                    # on, so folding here would glue two commands. Unextractable.
                    return None
                lines.append(ln)
            elif lines and _ends_in_continuation(lines[-1]):
                # A dropped line (blank / comment / marker) right after a
                # backslash continuation: in real shell the continuation binds
                # to THIS line, not to whatever kept line follows — folding
                # across the gap would glue two commands with NO operator
                # (`echo running gate \` + `# note` + `exit 1` must not become
                # the always-green `echo running gate exit 1`). Unextractable.
                return None
        if not lines:
            continue
        joined = _join_shell_lines(lines)
        if joined is None:
            return None  # control flow in one step poisons the whole chain
        commands.append(joined)
    if not commands:
        return None
    # A non-final step ending in a dangling continuation would fold the NEXT
    # step's command into itself as arguments — unexpressible on one line.
    if any(_ends_in_continuation(c) for c in commands[:-1]):
        return None
    # Multiple run steps cannot be safely joined: GitHub Actions runs each `run:`
    # step in its own shell process, so `cd`/`export` side-effects from step N
    # do NOT persist to step N+1 — joining them on one `&&`-chain would recreate
    # a different (and wrong) execution environment.
    if len(commands) > 1:
        return None
    return _join_shell_lines(commands)


def _offer_install_ready_for_ci(repo: str, cwd: Optional[str], *, run, pal, stream,
                                input_fn=input) -> Optional[str]:
    """After a repo opts INTO label-gated CI, install the generic
    ``tests-ready-for-ci.yml`` label gate on the repo's DEFAULT branch via a
    server-side PR (reusing :func:`_create_file_pr` with its OWN branch + path,
    distinct from the Claude installer), leaving the local checkout untouched. This
    is the missing mechanism the per-repo opt-in needs: the merge automation only
    reaps the Actions-minute saving when a workflow on the BASE branch actually gates
    on the ``ready-for-ci`` label — without this template the opt-in is inert.

    The gate's CI command is WIRED FOR the user — detected from the local checkout
    (:func:`_detect_ci_command`), confirmed on a TTY with that detection as the
    default, and baked into the single ``run:`` step — so the installed workflow runs
    a REAL command, never a failing ``exit 1`` placeholder the user has to hand-edit.
    On an UPDATE of an already-installed gate the default flips to the command the
    installed workflow actually runs (:func:`_installed_ci_command`), falling back to
    detection only when nothing is extractable — and a non-TTY update either
    preserves that command verbatim or skips with a warning, NEVER re-bakes the
    generic detection over a hand-wired gate (the 2026-07-01 incident).

    P7 #4 (probe-before-install): if the gate is already on the default branch, it
    says so and opens NO redundant second PR. P7 #2 (merge-me callout): on a
    successful PR it prints a PROMINENT warn row — not a dim line — that the gate
    stays INACTIVE until the PR is merged. Returns ``'pr'`` (a PR was opened) or
    ``None`` (already present / no template / declined / no command resolved / write
    failed); on any failure it prints the manual fallback."""
    # P7 #4 — probe first; never open a redundant second PR for a gate already there.
    # Version-aware: when the installed gate is OLDER than the bundled template, fall
    # through to the bake-and-PR flow as an UPDATE (re-wiring the CI command) instead
    # of skipping; a current / unknown installed copy is left untouched.
    is_update = False
    installed_cmd: Optional[str] = None
    if _probe_ready_for_ci_workflow(repo, run):
        _spec = next((s for s in managed_files.MANAGED_FILES
                      if s["name"] == "tests-ready-for-ci.yml"), None)
        _shipped_v = managed_files.shipped_version(_spec["template"]) if _spec else None
        _installed_text = _installed_managed_file_text(
            repo, ".github/workflows/tests-ready-for-ci.yml", run)
        _installed_v = managed_files.file_version(_installed_text)
        if not managed_files.needs_update(_installed_v, _shipped_v):
            _row("ok", "Label-gated CI workflow already on the default branch "
                       "(up to date)", pal, stream)
            return None
        _cur = "unversioned" if _installed_v is None else f"v{_installed_v}"
        _row("warn", f"Label-gated CI workflow on the default branch is older "
                     f"({_cur}) than the bundled v{_shipped_v} — offering an update.",
             pal, stream)
        is_update = True
        # An update must PRESERVE the command the installed gate actually runs
        # (reusing the text the version check just fetched) — re-detecting from
        # the checkout flattens a hand-wired gate back to the generic guess.
        installed_cmd = _installed_ci_command(_installed_text)
    template = _ready_for_ci_template_path()
    if not template.exists():
        _row("warn", f"Bundled label-gated CI template not found: {template}", pal, stream)
        return None
    try:
        content = template.read_text(encoding="utf-8")
    except OSError as exc:
        _row("warn", f"Couldn't read the label-gated CI template: {exc}", pal, stream)
        return None
    # The marker MUST be exactly one standalone line — ``_bake_ci_command`` replaces
    # a line whose strip() equals the marker, so a 0/2+/embedded marker would either
    # leak the literal token or wire nothing.
    marker_lines = [ln for ln in content.splitlines() if ln.strip() == _CI_COMMAND_MARKER]
    if len(marker_lines) != 1:
        _row("warn", f"Label-gated CI template must carry exactly one "
                     f"{_CI_COMMAND_MARKER} marker line (found {len(marker_lines)}) — "
                     "can't auto-wire the CI command. Update the bundled template.", pal, stream)
        return None
    # Resolve the REAL CI command BEFORE opening the PR so the gate never ships a
    # failing placeholder. On an UPDATE the installed gate's own command is the
    # default; the generic detection is only the fresh-install default and the
    # fallback when the installed copy yields nothing extractable.
    detected = _detect_ci_command(cwd)
    default_cmd = (installed_cmd or detected) if is_update else detected
    if not _is_tty():
        if is_update and not installed_cmd:
            _row("warn", "Skipping the label-gated CI workflow update — the installed "
                         "gate's CI command couldn't be read, and re-baking an "
                         "auto-detected command off-TTY could silently drop what your "
                         "gate really runs. Re-run setup in a terminal to confirm the "
                         "command.", pal, stream)
            return None
        if not default_cmd:
            _row("warn", "Skipping label-gated CI workflow install — no TTY to capture a CI "
                         "command and none auto-detected.", pal, stream)
            _note("Re-run setup in a terminal, or copy the bundled tests-ready-for-ci.yml in by "
                  "hand and set its CI command.", pal, stream)
            return None
        ci_command = default_cmd
        if is_update:
            _row("info", f"No TTY — preserving the installed gate's CI command: "
                         f"{ci_command}", pal, stream)
        else:
            _row("info", f"No TTY — wiring the auto-detected CI command: {ci_command}",
                 pal, stream)
    else:
        if not _ask_yes_no("Install the label-gated CI workflow "
                           "(.github/workflows/tests-ready-for-ci.yml) on the default "
                           "branch via a PR?", default=True, input_fn=input_fn, stream=stream):
            _row("info", "Skipped. Label-gated CI stays a config preference until a "
                         "workflow on the default branch gates on the `ready-for-ci` "
                         "label.", pal, stream)
            return None
        # Wire the command FOR the user — a default they accept or edit (the
        # installed gate's own command on an update, the detected one on a fresh
        # install); never a workflow file they hand-edit afterwards.
        suffix = (f" [{default_cmd}]" if default_cmd
                  else " (e.g. `make ci`, `npm test`, `python -m pytest`)")
        try:
            entered = input_fn(f"  Command this gate runs at merge (your "
                               f"tests/lint/build){suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            entered = ""
        ci_command = entered or default_cmd
        if not ci_command:
            _row("warn", "No CI command given — not installing a gate that would run "
                         "nothing. Re-run setup and enter your test command.", pal, stream)
            return None
    content = _bake_ci_command(content, ci_command)
    default = _default_branch(repo, run=run)
    if not default:
        _row("warn", "Couldn't determine the default branch — install the template "
                     "manually.", pal, stream)
        return None
    ok, detail = _create_file_pr(
        repo, default,
        ".github/workflows/tests-ready-for-ci.yml",        # path
        content,                                           # content (CI command baked in)
        ("Update label-gated CI workflow (Buddhi setup)" if is_update
         else "Add label-gated CI workflow (Buddhi setup)"),       # message
        ("Update label-gated CI workflow" if is_update
         else "Add label-gated CI workflow"),                      # title
        ("buddhi/update-ready-for-ci-workflow" if is_update
         else "buddhi/add-ready-for-ci-workflow"),                 # branch (its own, != claude)
        body=((f"Updates `.github/workflows/tests-ready-for-ci.yml` to the latest "
               f"bundled version (CI command re-wired). (Opened by the Buddhi setup "
               f"wizard.)\n\n{_REVERT_NOTE}") if is_update else None),
        run=run)
    if ok:
        _verb = "update" if is_update else "add"
        _row("ok", f"Opened a PR to {_verb} the label gate (CI command: {ci_command}): "
                   f"{detail}", pal, stream)
        if is_update:
            print(f"  {pal.GREY}{_REVERT_NOTE}{pal.RESET}", file=stream)
        # P7 #2 — a loud, attention-grabbing callout (a warn row, not a dim line):
        # the gate does NOTHING until this PR merges onto the default branch.
        _row("warn", f"MERGE THIS PR to activate the gate — it stays INACTIVE until it "
                     f"is reviewed and merged onto '{default}' (the default branch). "
                     f"Until then the merge automation attaches `ready-for-ci` but no "
                     f"workflow listens for it, so CI is never gated.", pal, stream)
        return "pr"
    _row("warn", f"Couldn't open the PR automatically ({detail}). Copy the bundled "
                 "tests-ready-for-ci.yml into .github/workflows/ on your default "
                 "branch by hand.", pal, stream)
    return None


def _gh_secret_exists(repo: str, name: str, *, org: Optional[str] = None, run) -> Optional[bool]:
    """Whether ``name`` is among the Actions secrets visible to ``repo``. Checks the
    repo's own secrets (``gh secret list --repo``); when ``repo``'s owner is an
    Organization it ALSO checks the org-level secrets actually shared with ``repo``
    via ``repos/{repo}/actions/organization-secrets`` (NOT ``gh secret list --org``,
    which lists ALL org secrets regardless of per-repo visibility — a
    ``selected``-visibility org secret absent from this repo's share list would
    false-positive and skip prompting). Returns ``True`` / ``False``, or ``None``
    when NEITHER check could run (so the caller offers to set it rather than
    silently assume it is present). ``org`` is auto-resolved from the owner when not
    given; pass it explicitly to avoid a repeat owner-type lookup."""
    if not repo:
        return None
    if org is None and "/" in repo:
        owner = repo.split("/", 1)[0]
        if _owner_type(owner, run=run) == "Organization":
            org = owner

    def _scan(args) -> Tuple[bool, bool]:
        # (ran, found). `gh secret list` prints "NAME\tUPDATED" per line; match the
        # first whitespace-delimited token exactly so a name that is a substring of
        # another can't false-positive.
        ok, out = _run_ok(run, ["gh", "secret", "list"] + args)
        if not ok:
            return (False, False)
        for ln in out.splitlines():
            tok = ln.split()
            if tok and tok[0].upper() == name.upper():
                return (True, True)
        return (True, False)

    ran_repo, found = _scan(["--repo", repo])
    if found:
        return True
    if org:
        # Repo-scoped org-secrets endpoint: only the org secrets actually shared
        # with THIS repo, not the org-wide list.
        ok, out = _run_ok(run, ["gh", "api", "--paginate",
                                f"repos/{repo}/actions/organization-secrets",
                                "--jq", ".secrets[].name"])
        if ok:
            org_names = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if name.upper() in (on.upper() for on in org_names):
                return True
            # Org check ran and missed it; if the repo check itself failed we still
            # can't rule out a repo-level secret — say unknown.
            return False if ran_repo else None
        return None  # org API failed; status unknown
    return False if ran_repo else None


def _org_admin(org: Optional[str], *, run) -> Optional[bool]:
    """Whether the authenticated gh user is an ADMIN (owner) of ``org``, via the
    org-membership API (``role`` is "admin" for owners, "member" otherwise). Only an
    org admin can create org-level Actions secrets, so the org-scope opt-in is gated
    on this. Returns ``True`` / ``False``, or ``None`` when the check can't run (no
    org, gh error, or the user isn't a member) — callers treat anything but ``True``
    as 'not confirmed' and stay repo-scoped."""
    if not org or not re.match(r"^[A-Za-z0-9_-]+$", org):
        return None
    ok, out = _run_ok(run, ["gh", "api", f"user/memberships/orgs/{org}", "--jq", ".role"])
    if not ok:
        return None
    return out.strip() == "admin"


def _set_gh_secret(repo: str, name: str, value: str, *, org: Optional[str] = None,
                   run) -> Tuple[bool, str]:
    """Set the Actions secret ``name`` to ``value`` by piping the value into
    ``gh secret set`` on stdin (the ``--body`` flag reads stdin when omitted, so the
    secret never appears on argv or in the process list).

    Scope: by default the secret is set on ``repo`` (``--repo``, the least blast
    radius). When ``org`` is given it is set org-wide with ``--visibility selected``,
    and the current repo is UNIONED into the existing selected-repo list (fetched
    first) so repos already sharing the secret keep access — gh ``--repos`` REPLACES
    the list, it does not append. Returns ``(ok, detail)``."""
    if not (repo and value):
        return (False, "missing repo or value")
    if org:
        # --visibility selected requires --repos; the org's repo is named without the
        # owner (gh resolves it within the org). Union the current repo into the
        # existing selected list so omitting it can't silently revoke other repos.
        repo_name = repo.split("/", 1)[-1]
        repos_to_set = [repo_name]
        ok, out = _run_ok(run, ["gh", "api", "--paginate",
                                f"orgs/{org}/actions/secrets/{name}/repositories",
                                "--jq", ".repositories[].name"])
        if ok:
            existing = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if existing:
                repos_to_set = existing if repo_name in existing else existing + [repo_name]
        argv = ["gh", "secret", "set", name, "--org", org,
                "--visibility", "selected", "--repos", ",".join(repos_to_set)]
    else:
        argv = ["gh", "secret", "set", name, "--repo", repo]
    try:
        r = run(argv, input=value, timeout=20)
    except Exception as exc:
        return (False, type(exc).__name__)
    if getattr(r, "returncode", 1) != 0:
        return (False, (getattr(r, "stderr", "") or "").strip() or "gh secret set failed")
    return (True, "secret set")


def _set_secret_scoped(repo: str, name: str, value: str, *, prefer_org: bool = False,
                       run) -> Tuple[bool, str, str]:
    """Set the Actions secret ``name`` to ``value``, choosing the scope SAFELY.

    Default → repo scope (``--repo``, the least blast radius). This seam NEVER goes
    org-wide on its own; ``prefer_org`` is the caller's EXPLICIT, separately-confirmed
    opt-in (offered only when ``_owner_type`` reports an Organization). Even with
    ``prefer_org`` it goes org-wide ONLY after confirming the gh user is an org admin
    (``_org_admin``); on a non-admin org, a permission denial, or ANY org-set failure
    it FALLS BACK to repo scope — the flow never fails because of an org attempt. The
    org secret is scoped to this repo only (``--visibility selected``), so it is not
    exposed across the whole org. Returns ``(ok, detail, scope)`` where ``scope`` is
    the scope actually used ("org" or "repo")."""
    if prefer_org and repo and "/" in repo:
        org = repo.split("/", 1)[0]
        if _org_admin(org, run=run) is True:
            ok, detail = _set_gh_secret(repo, name, value, org=org, run=run)
            if ok:
                return (ok, detail, "org")
            # org set failed (permission/denial/other) → fall back to repo scope
        # non-admin, unknown role, or failed org set → repo-scope fallback
    ok, detail = _set_gh_secret(repo, name, value, run=run)
    return (ok, detail, "repo")


def _set_claude_secret(repo: str, *, run, spawn_command, getpass_fn, pal, stream,
                       single_select=single_select, input_fn=input,
                       owner_type: Optional[str] = None,
                       validate_fn=_validate_claude_token, auth_probe=None) -> str:
    """Walk the user through setting ``CLAUDE_CODE_OAUTH_TOKEN`` — minting + storing
    it, validating it BEFORE the store, and RE-MINTING a stored one that has gone
    bad. Returns a short status string.

    Dual-credential (FREE): either credential satisfies the workflow — the
    pay-as-you-go ``ANTHROPIC_API_KEY`` or the subscription
    ``CLAUDE_CODE_OAUTH_TOKEN`` — and the bundled template accepts both, so the
    prompt is SKIPPED when EITHER is already present. The existence check is
    org-aware: an org-set secret won't appear in ``gh secret list --repo``.

    Part A — verify-before-store (``validate_fn``, defaulting to
    :func:`_validate_claude_token`, injectable for tests). A freshly-pasted token is
    validated by a real, isolated ``claude -p ping`` BEFORE it is saved: ``"valid"``
    → store; ``"invalid"`` → warn + re-prompt (bounded ~3, then give up without
    storing — an invalid token NEVER reaches ``gh secret set``); ``"unknown"`` →
    warn + exit without storing (unverified tokens are never written to GitHub,
    even transiently). The token value is never echoed or logged.

    Part B — re-mint a stored-but-broken token (``auth_probe``, defaulting to
    :func:`detectors.latest_run_token_auth_failed`). GitHub Actions secrets are
    write-only, so a stored token can't be read to test it; the only evidence is the
    runtime signal. When the OAuth secret already EXISTS (and no working
    ANTHROPIC_API_KEY backs it), this probes ``repo``'s latest claude-code-review run
    for a token-401. A 401 → the stored token is broken → enter the re-mint flow
    (Part A's paste+validate+store, framed as a re-mint). Clean / no run / couldn't
    tell → keep the skip (``"present"``) — a working or UNKNOWN token is NEVER
    blind-re-minted. The probe is best-effort and never raises.

    Safe scoping: the new secret is set on THIS repo only by default (least blast
    radius). When ``repo``'s owner is an Organization an EXPLICIT, separately-
    confirmed opt-in to scope it org-wide (visible to this repo alone) is offered
    and routed through ``_set_secret_scoped``, which enforces the org-admin check
    and the repo-scope fallback — so this never widens exposure without consent."""
    name = "CLAUDE_CODE_OAUTH_TOKEN"
    if owner_type is None and "/" in repo:
        owner_type = _owner_type(repo, run=run)
    org = repo.split("/", 1)[0] if (owner_type == "Organization" and "/" in repo) else None
    # Skip the prompt only when one credential is CONFIRMED present (True). An
    # unknown result (None) is NOT treated as present — we offer to set it.
    claude_present = _gh_secret_exists(repo, name, org=org, run=run) is True
    anthropic_present = _gh_secret_exists(repo, "ANTHROPIC_API_KEY", org=org, run=run) is True
    remint = False
    if claude_present or anthropic_present:
        # Part B — re-mint detection. Probe ONLY when the OAuth token is the sole
        # credential:
        # ANTHROPIC_API_KEY satisfies the workflow on its own and is NOT the token
        # that 401s here, so a working pay-as-you-go key backing the repo keeps the
        # skip. A token-401 on the latest review run → the stored OAuth token is
        # broken → fall through to the re-mint flow. Clean / no run / couldn't-tell →
        # keep the skip (never blind-re-mint a working or unknown token). The probe
        # is best-effort and never raises.
        if claude_present and not anthropic_present:
            probe = auth_probe or detectors.latest_run_token_auth_failed
            try:
                remint = bool(probe(repo, run=run))
            except Exception:
                remint = False
        if not remint:
            return "present"
    def _manual(status="deferred"):
        _row("step", "Re-mint the reviewer token by hand:" if remint
                     else "Set the reviewer token by hand:", pal, stream)
        print("    claude setup-token        # prints a long-lived OAuth token", file=stream)
        print(f"    gh secret set {name} --repo {repo}    # paste that token", file=stream)
        return status

    if remint:
        # The stored token is failing live — say so plainly before the offer.
        _row("warn", f"Claude's reviews are failing on {repo} — its {name} looks "
                     "invalid or expired.", pal, stream)
        _note("That secret is write-only, so it can't be repaired — only replaced; "
              "let's re-mint it.", pal, stream)

    if not _is_tty():
        return _manual()
    # CONSENT GATE: the mint spawns a terminal + expects a paste — never launch it
    # without an explicit yes. Declining routes to the by-hand instructions.
    if remint:
        _q = f"Re-mint {name} for {repo} now via `claude setup-token`?"
    else:
        _q = f"{name} is not set on {repo}. Set it now via `claude setup-token`?"
    if not _ask_yes_no(_q, default=False, input_fn=input_fn, stream=stream,
                       pal=pal, single_select_fn=single_select):
        return _manual("skipped")

    # Open `claude setup-token` in a fresh window so its OAuth flow gets a real TTY
    # (it cannot run headlessly), then key the note off whether the spawn succeeded.
    try:
        res = spawn_command("claude setup-token", label="claude-setup-token", stream=stream)
        if isinstance(res, dict) and res.get("spawned"):
            _note("A terminal opened running `claude setup-token` — finish login there, "
                  "copy the printed token, then paste it below.", pal, stream)
        else:
            _note("Run `claude setup-token` in another terminal, then paste the printed "
                  "token below.", pal, stream)
    except Exception:
        _note("Run `claude setup-token` in another terminal, then paste the printed "
              "token below.", pal, stream)

    # Part A — paste → verify-before-store, with bounded re-prompts on a rejected
    # token. The token is stored ONLY once it authenticates ("valid"); "unknown"
    # (inconclusive — no claude binary, timeout, unrecognized error) exits without
    # storing. An "invalid" token never reaches the writer. The token value is never
    # echoed or logged.
    validate = validate_fn or _validate_claude_token
    token = ""
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            # A token copied from a wrapped / small terminal window carries an internal
            # newline (+ indent space) — getpass returns only its first line, so read via
            # the multi-line-aware helper that drains the wrapped continuation and rejoins
            # the pieces into the real single-line token (a real sk-ant-oat token has none).
            token = _read_pasted_secret(
                f"  Paste the {name} (input hidden), or blank to skip: ", getpass_fn)
        except (EOFError, KeyboardInterrupt):
            token = ""
        if not token:
            return _manual("skipped")
        _row("step", "Testing the token…", pal, stream)
        try:
            state, detail = validate(token, run=run)
        except Exception as exc:
            # A validator that RAISES (vs. the default, which catches its own errors
            # and returns "unknown") is a contract violation / bug, not a transient
            # blip — fail SAFE: never store on it.
            _row("warn", f"The token check crashed ({type(exc).__name__}) — not saving "
                         f"{name} on GitHub.", pal, stream)
            return _manual("failed")
        if state == "valid":
            break
        if state == "unknown":
            # A "valid" verdict is the ONLY thing that stores — an UNVERIFIED token is
            # never written to GitHub. "unknown" = the test couldn't confirm the token
            # either way (an unrecognized error, a timeout, or no CLI). Do NOT store it
            # and do NOT ask where it should live; surface the reason + the by-hand path.
            _row("warn", f"Couldn't verify this token — not saving an unverified {name} "
                         "on GitHub.", pal, stream)
            if detail:
                _note(str(detail), pal, stream)
            return _manual("failed")  # inconclusive → nothing verified to store
        # "invalid": the token didn't authenticate.
        _row("warn", "That token didn't authenticate — it may be mis-pasted, expired, "
                     "or for the wrong account.", pal, stream)
        if detail:
            _note(str(detail), pal, stream)
        if attempt < max_attempts:
            _note("Copy the token again from the `claude setup-token` window, "
                  "then paste it below.", pal, stream)
            continue
        _row("warn", f"Still not authenticating after {max_attempts} tries — not "
                     f"saving {name} on GitHub.", pal, stream)
        return _manual("failed")
    # Scope SAFELY: repo-only by default. On an org-owned repo, offer the explicit
    # org-wide opt-in (still visible to this repo alone); _set_secret_scoped checks
    # org-admin and falls back to repo scope on any denial.
    prefer_org = False
    if org and _is_tty():
        idx = single_select(
            f"{org} is a GitHub organization. Where should {name} live?",
            [("This repository only (recommended)",
              "least exposure — a repo secret on this repo alone"),
             (f"Org-wide, scoped to {repo.split('/')[-1]} only",
              "an org secret visible only to this repo (needs org-admin; "
              "falls back to repo scope otherwise)")],
            preselect=0, pal=pal, stream=stream, input_fn=input_fn)
        prefer_org = (idx == 1)
    ok, detail, scope = _set_secret_scoped(repo, name, token, prefer_org=prefer_org, run=run)
    if ok:
        _row("ok", f"{name} set on {repo} ({scope} scope).", pal, stream)
        return "set"
    _row("warn", f"Could not set {name} ({detail}) — set it by hand with "
                 f"`gh secret set {name} --repo {repo}`.", pal, stream)
    return "failed"


def _probe_gh_token(token, *, run) -> Tuple[bool, str, str]:
    """Verify a pasted GitHub token with a real ``gh api user`` round-trip, the token
    FORCED into the child env (``GH_TOKEN``) so it authenticates as the PASTED token
    even while ``gh`` itself is logged out (this hatch only runs when gh is
    unauthenticated). Returns ``(ok, login, error)``: ``ok`` is True only on exit 0
    with a non-empty login. The token is passed ONLY via ``env`` — never on argv or
    in a log line. ``GH_TOKEN`` outranks ``GITHUB_TOKEN`` in gh's own precedence, so
    forcing it guarantees the probe tests the pasted value, not an ambient cred."""
    env = {**os.environ, shell_env.GH_TOKEN_NAME: token}
    try:
        r = run(["gh", "api", "user", "--jq", ".login"], env=env)
    except Exception as exc:  # network / spawn failure → not verifiable → fail closed
        return False, "", (str(exc) or exc.__class__.__name__)
    login = (getattr(r, "stdout", "") or "").strip()
    if getattr(r, "returncode", 1) == 0 and login:
        return True, login, ""
    err = ((getattr(r, "stderr", "") or "").strip()
           or (getattr(r, "stdout", "") or "").strip())
    err = err.splitlines()[0] if err else ""  # keep the surfaced error to one line
    return False, "", err


def _offer_gh_token(*, run, getpass_fn, pal, stream, input_fn=input) -> None:
    """When gh is unauthenticated, offer the GH_TOKEN env escape hatch (persisted
    via shell_env, never config). A freshly-pasted token is VERIFIED by a real
    ``gh api user`` call BEFORE it is saved (see :func:`_probe_gh_token`); a wrong /
    expired / mis-pasted token is therefore never written to the shell rc, where it
    would silently shadow a later ``gh auth login`` and break every gh call. Because
    the value lands in the rc (not a re-mintable GitHub secret), this path fails
    CLOSED — an unverifiable token is NOT stored. The user may instead run
    ``gh auth login``. The token value is never echoed or logged."""
    if not _is_tty():
        return
    if not _ask_yes_no("gh is not authenticated. Persist a GH_TOKEN now? "
                       "(otherwise run `gh auth login` yourself)",
                       default=False, input_fn=input_fn, stream=stream):
        return
    max_attempts = 2  # one paste + one bounded re-prompt, then skip (never loop)
    for attempt in range(1, max_attempts + 1):
        try:
            # Multi-line-aware read: a token copied from a wrapped/narrow terminal wraps
            # across lines (getpass would keep only the first) — drain the continuation
            # and strip all whitespace (a real token contains none).
            token = _read_pasted_secret(
                "  Paste a GitHub token (input hidden), or blank to skip: ", getpass_fn)
        except (EOFError, KeyboardInterrupt):
            token = ""
        if not token:
            return
        _row("step", "Testing the token …", pal, stream)
        ok_probe, login, err = _probe_gh_token(token, run=run)
        if ok_probe:
            ok, path = shell_env.upsert({shell_env.GH_TOKEN_NAME: token}, also_env=True)
            if ok:
                who = f" (authenticated as {login})" if login else ""
                _row("ok", f"GH_TOKEN written to {path}{who} — open a new shell to "
                           f"pick it up", pal, stream)
            else:
                _row("warn", "Could not persist GH_TOKEN — set it in your shell rc by hand",
                     pal, stream)
            return
        # Probe failed → surface the REAL error briefly + guidance, and do NOT store.
        detail = f": {err}" if err else ""
        _row("warn", f"That token didn't authenticate{detail}", pal, stream)
        if attempt < max_attempts:
            _row("info", "Check it has not expired and has `repo`/`read:org` scope, then "
                         "paste it again — or run `gh auth login` instead.", pal, stream)
            continue
        _row("warn", "Not persisting GH_TOKEN. Run `gh auth login`, or set a verified "
                     "token in your shell rc by hand.", pal, stream)
        return


def _app_install_lines(bot: str, repo: Optional[str]) -> List[str]:
    """The GitHub-UI steps to install an app-backed reviewer on ``repo``. The vendor
    apps can't be installed via API, so setup GUIDES the install. Claude needs the
    ``github.com/apps/claude`` App too — its workflow + token alone 401 and post
    nothing (the silent-Claude failure: a workflow run that 401s with 'Claude Code
    is not installed on this repository')."""
    where = f"`{repo}`" if repo else "this repo"
    if bot == "claude":
        return [
            f"Install the Claude GitHub App (github.com/apps/claude), then grant it access to {where}.",
            "Without it the claude-code-review run fails with 401 (\"Claude Code is not "
            "installed on this repository\") and claude[bot] posts NOTHING — the "
            "workflow + token alone are NOT enough.",
        ]
    if bot == "codex":
        return ["A ChatGPT plan that includes Codex is required.",
                "Install the OpenAI Codex app via Codex ▸ Settings ▸ Connectors ▸ "
                f"GitHub and grant it access to {where}.",
                "It then replies to '@codex review' on a PR."]
    if bot == "gemini":
        return ["Install the Gemini Code Assist GitHub App (github.com/apps/gemini-code-assist) "
                f"and grant it access to {where}.",
                "It then replies to '/gemini review' on a PR."]
    return [f"Install {bot}'s GitHub app and grant it access to {where}."]


def _guide_app_install(bot: str, repo: Optional[str], *, pal, stream) -> None:
    """Print a PROMINENT (bold-headed panel) app-install guide so the steps never
    get lost among the other ✓/⚠ rows. Used for the vendor GitHub-App reviewers and
    — equally REQUIRED, not optional — the Claude GitHub App."""
    required = " (REQUIRED)" if bot == "claude" else ""
    _panel(f"{bot.capitalize()} GitHub App{required} — install it + grant repo access",
           _app_install_lines(bot, repo), pal, stream)


def _confirm_reviewer_installed(bot: str, repo: Optional[str], *, single_select, pal, stream,
                                input_fn) -> bool:
    """Fail-closed gate: did the user CONFIRM ``bot`` is actually installed on this
    repo and ready to review? Selecting a reviewer in the multi-select signals only
    INTENT — the vendor app / Copilot installs happen in the GitHub UI and setup
    cannot verify them through an API. Recording an UN-installed reviewer as an
    expected auto-reviewer is the dangerous case: the loop then waits forever for a
    review that never arrives, or — with auto-merge on — squash-merges a PR that got
    ZERO reviews. So this preselects **No**: only an explicit Yes keeps the reviewer.
    A decline, a blank answer, or a NON-INTERACTIVE run (no TTY to answer) all leave
    the reviewer disabled, and the caller drops the bot from BOTH the enabled fleet
    and ``auto_on_open`` — so it can never default to auto-review.

    The decision deliberately hinges on :func:`_is_tty` (a module global, not a DI
    param): without a TTY there is no way to obtain the explicit confirmation, so the
    only safe answer is No. The question + labeled options carry the CONSEQUENCE of
    each choice so the user understands why an unconfirmed reviewer is left out."""
    if not _is_tty():
        return False
    where = f" on {repo}" if repo else ""
    repo_name = repo or "this repo"
    idx = single_select(
        f"  Confirm the '{bot}' reviewer is installed{where} and ready to review PRs?",
        [("No / not sure — leave it disabled",
          "the loop won't wait on a reviewer that can't respond"),
         (f"Yes — {bot} is installed and will review PRs on {repo_name}",
          "the loop treats it as an expected reviewer")],
        preselect=0, pal=pal, stream=stream, input_fn=input_fn)
    return idx == 1


def _ask_auto_on_open(bot: str, *, single_select, pal, stream, input_fn) -> bool:
    """Does ``bot`` post a review by itself when a PR is opened? — the one fact the
    loop cannot deduce from "is the app installed". A labeled select whose options
    carry the CONSEQUENCE of each answer (the same pattern as the install gate):
    Yes → the loop leaves the bot alone in round 1 (a summon would duplicate the
    review it already posts); No → the loop summons it in round 1 so its review
    still arrives.

    Preselects Yes — the GitHub-App reviewers auto-review on open out of the box —
    and a non-interactive run (the numbered fallback; a blank or EOF answer)
    resolves to that same Yes, exactly as the previous bare Yes/No prompt did. The
    previous prompt's y/n keystroke shortcuts are kept too (y → Yes, n → No), so
    the muscle-memory answer still records the same value."""
    idx = single_select(
        f"  Does {bot.capitalize()} auto-review when a PR is opened?",
        [("Yes — reviews automatically when a PR is opened",
          "the loop won't re-summon it in round 1 (avoids a duplicate review)"),
         ("No — must be triggered/requested to review",
          "the loop posts its round-1 request so its review still arrives")],
        preselect=0, pal=pal, stream=stream, input_fn=input_fn,
        shortcuts={"y": 0, "n": 1})
    return idx == 0


def step_reviewers(repo: Optional[str], cwd: Optional[str], doctor: Dict[str, Any], *,
                   run, spawn_command, getpass_fn, pal, stream,
                   multi_select=multi_select, single_select=single_select,
                   input_fn=input, seed: Optional[Sequence[str]] = None
                   ) -> Tuple[List[str], Dict[str, bool]]:
    """Step 5 — reviewer fleet: multi-select, validate each, capture auto_on_open.
    ``seed`` (when given — e.g. the per-repo confirm mode passes the global default)
    is the set of reviewers to PRESELECT; ``None`` preselects all four (the full
    wizard's first-run default)."""
    _panel("Step 5 — Reviewer fleet", [
        "Enable only the reviewers you have set up on this repo.",
        "EVERY reviewer you enable must already have its vendor GitHub app + plan "
        "installed on this repo, with its trigger configured and working — otherwise "
        "the round-1 request may have no effect (the per-bot setup steps follow).",
    ], pal, stream)
    labels = {
        "copilot": ("Copilot", "needs gh ≥ 2.87 + Copilot Pro/Enterprise"),
        "gemini": ("Gemini", "needs the Gemini Code Assist GitHub app on the repo"),
        "codex": ("Codex", "needs the OpenAI Codex GitHub app + a ChatGPT plan"),
        "claude": ("Claude", "needs claude-code-review.yml + CLAUDE_CODE_OAUTH_TOKEN secret"),
    }
    options = [labels[b] for b in _REVIEWERS]
    if seed is None:
        preselected = None
    else:
        seed_set = {str(b).strip().lower() for b in seed}
        preselected = {i for i, b in enumerate(_REVIEWERS) if b in seed_set}
    chosen_idx = multi_select("  Which reviewers should run?", options, preselected=preselected,
                              pal=pal, stream=stream, input_fn=input_fn)
    enabled = [_REVIEWERS[i] for i in sorted(chosen_idx)]
    if not enabled:
        _row("warn", "No reviewers selected — the loop will have nothing to fan out to", pal, stream)

    auto_on_open: Dict[str, bool] = {}
    confirmed: List[str] = []
    for bot in enabled:
        if bot == "copilot":
            if doctor.get("gh_auth"):
                _row("ok", "Copilot — gh authenticated", pal, stream)
            else:
                _row("warn", "Copilot needs gh authenticated (and a paid Copilot plan)", pal, stream)
                _offer_gh_token(run=run, getpass_fn=getpass_fn, pal=pal, stream=stream, input_fn=input_fn)
            # P7 #5 — the entry-point hint so the operator knows WHERE Copilot code
            # review is turned on (there is no API to install it, like the app bots).
            _row("info", "Enable Copilot code review for this repo: repo (or org) ▸ "
                         "Settings ▸ Rules/Reviewers (requires a paid Copilot plan).",
                 pal, stream)
        elif bot in ("gemini", "codex"):
            # The vendor app can't be installed via API — GUIDE the install
            # prominently (legible, not a dim aside) so it isn't missed.
            _guide_app_install(bot, repo, pal=pal, stream=stream)
        elif bot == "claude":
            # Claude review needs TWO independent things: the workflow on the
            # default branch AND the CLAUDE_CODE_OAUTH_TOKEN repo secret. Check both
            # — a repo with the workflow committed but no token is still
            # non-functional, so the secret walkthrough runs regardless of whether
            # the workflow is already present.
            present, note = _probe_claude_workflow(repo, run)
            _row("ok" if present else "warn", f"Claude — {note}", pal, stream)
            if repo:
                if not present:
                    _offer_install_claude_workflow(repo, cwd, run=run, pal=pal,
                                                   stream=stream, input_fn=input_fn)
                else:
                    # Present — but maybe an OLDER version. Offer an in-place update
                    # when the installed copy's `buddhi-managed-version` marker is
                    # behind the bundled template; this is what brings the auth-
                    # failure guard to a repo whose workflow predates it (the
                    # buddhi-review pre-guard copy that silently 401'd green).
                    _spec = next((s for s in managed_files.MANAGED_FILES
                                  if s["name"] == "claude-code-review.yml"), None)
                    if _spec is not None:
                        _installed = _installed_managed_file_text(
                            repo, str(_spec["dest"]), run)
                        _offer_update_managed_file(
                            repo, _default_branch(repo, run=run), _spec, _installed,
                            run=run, pal=pal, stream=stream, input_fn=input_fn)
                if doctor.get("gh_auth"):
                    _set_claude_secret(repo, run=run, spawn_command=spawn_command,
                                       getpass_fn=getpass_fn, pal=pal, stream=stream,
                                       single_select=single_select, input_fn=input_fn)
                # P7 #1: a clear, single-purpose re-check — it names the workflow
                # file and splits the two facts (workflow committed · secret set)
                # so a user who just merged the install PR / set the secret can
                # re-confirm without re-running the wizard.
                if not present and _is_tty() and _ask_yes_no(
                        _CLAUDE_RECHECK_PROMPT, default=False,
                        input_fn=input_fn, stream=stream):
                    present2, note2 = _probe_claude_workflow(repo, run)
                    _row("ok" if present2 else "warn", f"Claude — {note2}", pal, stream)
            # The THIRD requirement, beyond the workflow + token: the Claude GitHub
            # App. Without it the workflow 401s ("Claude Code is not installed on
            # this repository") and posts nothing — guide it prominently so it's
            # never missed (the buddhi-review PR #3 silent-Claude failure).
            _guide_app_install("claude", repo, pal=pal, stream=stream)

        # Fail-closed install confirmation. Selecting a reviewer above is only INTENT;
        # the vendor app / Copilot installs happen in the GitHub UI and can't be
        # verified by API. A reviewer the user does NOT explicitly confirm as installed
        # is dropped from BOTH the enabled fleet and auto_on_open — otherwise the loop
        # would wait forever for a review that never comes, or (auto-merge on) merge a
        # PR that got ZERO reviews. A non-interactive run can't confirm → also dropped.
        if not _confirm_reviewer_installed(bot, repo, single_select=single_select, pal=pal,
                                           stream=stream, input_fn=input_fn):
            disabled_row = _CLAUDE_DISABLED_ROW if bot == "claude" else (
                f"{bot.capitalize()} not confirmed installed — left DISABLED "
                "(re-run setup once it is installed on this repo).")
            _row("warn", disabled_row, pal, stream)
            continue
        confirmed.append(bot)

        # auto_on_open: Claude is mention-driven (never auto-reviews on open); the
        # GitHub-App bots are asked via the labeled select (preselect Yes).
        if bot == "claude":
            auto_on_open["claude"] = False
        else:
            auto_on_open[bot] = _ask_auto_on_open(bot, single_select=single_select,
                                                  pal=pal, stream=stream,
                                                  input_fn=input_fn)
    return confirmed, auto_on_open


def step_monitoring_locked(*, pal, stream) -> None:
    """Step 6 — live monitoring (paid). A locked teaser; persists nothing."""
    _panel("Step 6 — Live monitoring", [], pal, stream)
    _teaser(_MONITORING_TEASER, pal, stream)


# ── Per-repo confirmation steps (auto_merge · label-gated CI · global default) ─────

def step_repo_auto_merge(repo: str, current_default: bool, *, pal, stream,
                         single_select=single_select, input_fn=input) -> bool:
    """Ask the auto-merge default for THIS repo specifically. ``current_default``
    (the global / inherited auto-merge bool) preselects the matching option so a
    bare Enter confirms it. Returns the per-repo choice. Mirrors the reference
    wizard's ``step_repo_auto_merge``."""
    _panel("Per-repo — Auto-merge", [
        f"Repo: {pal.BOLD}{repo}{pal.RESET}",
        "When a loop exits cleanly (all reviewers satisfied), Buddhi can squash-merge "
        "the PR and delete its branch automatically — or leave it open for you.",
    ], pal, stream)
    idx = single_select(
        f"  Auto-merge default for {repo}?",
        [("Off", "you merge manually after a clean loop exit"),
         ("On", "clean exit → squash-merge + branch delete")],
        preselect=1 if current_default else 0, pal=pal, stream=stream, input_fn=input_fn)
    return idx == 1


def step_repo_label_gated_ci(repo: str, current_default: bool, *, pal, stream,
                             single_select=single_select, input_fn=input) -> bool:
    """Opt THIS repo into label-gated pre-merge CI, CONSEQUENCE-CONFIRMED.
    ``current_default`` (the global / inherited bool) preselects the first prompt so
    a bare Enter confirms the inherited setting. Turning it ON requires an explicit
    SECOND confirmation (preselected "No") so enabling is a deliberate act, never a
    stray Enter — the value F5's pre-merge gate consumes via
    ``config.label_gated_ci(cfg, repo)``. Mirrors the reference wizard's
    ``step_repo_label_gated_ci``. (Detecting + installing the ``ready-for-ci``
    workflow that makes the opt-in a real saving is F4's job, not this step's.)"""
    _panel("Per-repo — Label-gated CI", [
        f"Repo: {pal.BOLD}{repo}{pal.RESET}",
        "Label-gated CI saves Actions minutes: instead of CI running on every push, "
        "Buddhi adds the `ready-for-ci` label right before merge so CI runs ONCE.",
        "",
        "CONSEQUENCE of turning this ON for this repo:",
        "  • CI will NOT run on your manual / intermediate pushes to a PR.",
        "  • A required status check that gates on the label BLOCKS a human merge",
        "    until the label is added.",
    ], pal, stream)
    idx = single_select(
        f"  Label-gated CI default for {repo}?",
        [("Off", "CI runs on every push (Buddhi never defers it to the label)"),
         ("On", "CI runs only when the `ready-for-ci` label is added at merge")],
        preselect=1 if current_default else 0, pal=pal, stream=stream, input_fn=input_fn)
    if idx != 1:
        return False
    confirm = single_select(
        "  Confirm: enable label-gated CI for this repo? CI will NOT run on manual "
        "pushes — only when the `ready-for-ci` label is added at merge.",
        [("No — leave it off", "CI keeps running on every push"),
         ("Yes — enable label-gated CI", "I understand CI defers to the merge label")],
        preselect=0, pal=pal, stream=stream, input_fn=input_fn)
    return confirm == 1


def step_set_global_default(repo: str, has_existing_default: bool,
                            reviewers: Sequence[str], auto_on_open: Dict[str, bool], *,
                            pal, stream, single_select=single_select,
                            input_fn=input) -> bool:
    """Ask whether the just-confirmed fleet should ALSO become the GLOBAL default
    (the fall-back fleet for repos with no confirmed entry). Returns a bool.

    Preselect YES when no global default exists yet (a default lets runs on other
    repos proceed without a hard stop); otherwise NO, so a single-repo confirm does
    not silently rewrite an established default.

    P7 #3: the prompt NAMES its subject — the concrete reviewer fleet and which of
    those reviewers auto-post on PR open — so the question has an explicit antecedent
    (no bare "these"). The user sees exactly which reviewers would become the
    cross-repo default and which auto-review on open."""
    fleet = ", ".join(reviewers) if reviewers else "an empty fleet"
    auto_names = [b for b in reviewers if auto_on_open.get(b)]
    auto_desc = (f"auto-posts on PR open: {', '.join(auto_names)}"
                 if auto_names else "none auto-post on open; all are summoned")
    idx = single_select(
        f"  Also set this reviewer fleet ({fleet}) and its auto-post-on-PR-open "
        f"settings as your GLOBAL default (not just {repo})?",
        [(f"Yes — also use {fleet} as my global default",
          f"repos with no confirmed fleet fall back to this ({auto_desc})"),
         ("No — only for this repo", "leaves the global default unchanged")],
        preselect=1 if has_existing_default else 0, pal=pal, stream=stream,
        input_fn=input_fn)
    return idx == 0


def step_summary(plan: str, repo: Optional[str], reviewers: Sequence[str],
                 auto_on_open: Dict[str, bool], *, pal, stream,
                 auto_merge: Optional[bool] = None,
                 label_gated_ci: Optional[bool] = None) -> None:
    """Step 7a — read-back. ``auto_merge`` / ``label_gated_ci`` are the bound repo's
    confirmed per-repo settings; each is shown only when supplied (the full wizard
    confirms a bound repo, so it passes them)."""
    _panel("Step 7 — What's active", [], pal, stream)
    _kv("Claude plan", plan, pal, stream)
    _kv("Repo", repo or "(inferred at runtime from the git remote)", pal, stream)
    _kv("Reviewers", ", ".join(reviewers) or "(none)", pal, stream)
    # Labeled auto / summon split reads clearer than a compact bot:auto|summon token.
    auto_names = [b for b, v in auto_on_open.items() if v]
    summon_names = [b for b, v in auto_on_open.items() if not v]
    parts = []
    if auto_names:
        parts.append(f"auto: {', '.join(auto_names)}")
    if summon_names:
        parts.append(f"summon round 1: {', '.join(summon_names)}")
    _kv("Auto-on-open", " · ".join(parts) or "(none)", pal, stream)
    if auto_merge is not None:
        _kv("Auto-merge", "on — clean PRs squash-merge" if auto_merge
            else "off — you merge manually", pal, stream)
    if label_gated_ci is not None:
        _kv("Label-gated CI", "on — CI deferred to the merge label" if label_gated_ci
            else "off — CI runs on every push", pal, stream)
    _kv("Notifications", "console", pal, stream)


def step_done(path: Path, *, pal, stream) -> None:
    """Step 7b — done + launch hint."""
    _panel("Step 7 — Done", [
        f"Config written : {path}",
        "Re-run setup   : /review-pr setup",
        "Review a PR    : /review-pr <pr-number>   (omit to auto-select)",
        "Create a PR    : /open-pr",
    ], pal, stream)
    _teaser(_PRO_SOON_TEASER, pal, stream)


def _offer_first_review(repo: Optional[str], *, pal, stream, input_fn=input) -> None:
    """After setup completes, offer to go straight into the first review. On an
    explicit Yes, print the EXACT launch command so the user runs a review without
    hunting for the entry point. Fail-soft: a non-interactive run or a decline is a
    no-op (nothing is printed and nothing is launched — setup already succeeded)."""
    if not _is_tty():
        return
    if not _ask_yes_no("Review an open PR now?", default=False, input_fn=input_fn, stream=stream):
        return
    target = f" {repo}" if repo else ""
    _row("step", f"Launch it:  /review-pr <pr-number>{target}   "
                 "(omit the number to auto-select an open PR)", pal, stream)


# ── Per-repo confirm mode (parity with the reference wizard) ───────────────────────

def _write_global_default(reviewers: Sequence[str], auto_on_open: Dict[str, bool],
                          path: Path) -> bool:
    """Persist the top-level (global-default) reviewer fleet + ``auto_on_open``,
    leaving every other key — sibling ``repos`` entries included — intact. The
    global default is the fall-back fleet for repos with no confirmed entry
    (:func:`buddhi_review.config.has_global_default`)."""
    existing = config.load_config(path) if path.exists() else {}
    cfg = dict(existing)
    cfg["active_reviewers"] = list(reviewers)
    cfg["auto_on_open"] = {b: bool(v) for b, v in auto_on_open.items()}
    return write_config(cfg, path)


def _repo_auto_merge_default(cfg: Dict[str, Any], repo: Optional[str]) -> bool:
    """The bound repo's previously-confirmed ``auto_merge`` (a per-repo entry key),
    else off. There is no global ``auto_merge`` reader (the loop takes auto-merge
    from a per-run flag), so this only consults ``repos[<repo>]``."""
    entry = config.repo_entry(cfg, repo) or {}
    v = entry.get("auto_merge")
    return v if isinstance(v, bool) else False


def confirm_repo_interactive(repo: Optional[str], cwd: Optional[str], *,
                             run, spawn_command, getpass_fn, pal, stream, cfg_path: Path,
                             multi_select=multi_select, single_select=single_select,
                             input_fn=input) -> int:
    """The lightweight ``setup --repo <owner/repo>`` mode — parity with the reference
    wizard's ``confirm_repo_interactive``: confirm ONE repo's reviewer fleet +
    ``auto_on_open`` + ``auto_merge`` + label-gated CI (seeded from the global
    default), optionally promote the fleet to the GLOBAL default, and write
    ``repos[<repo>]`` through :func:`buddhi_review.config.set_repo_keys`. Skips the
    full wizard's plan / budget / monitoring steps. Returns an exit code (0 ok ·
    1 write failure · 2 no repo)."""
    repo = (repo or "").strip().rstrip("/")
    if not repo:
        repo = infer_repo(run) or ""
        if not repo:
            _row("warn", "No repo given and none could be inferred from the git "
                         "remote. Run from inside the repo checkout or pass "
                         "--repo owner/repo.", pal, stream)
            return 2
    if cwd is None:
        cwd = repo_toplevel(run) or os.getcwd()

    # A light tooling probe — only `gh auth`, which the reviewer step needs for the
    # Copilot + Claude-secret paths. The full doctor's per-tier model pings aren't
    # worth their latency in a single-repo confirm.
    gh_auth, _ = _run_ok(run, ["gh", "auth", "status"])
    doctor: Dict[str, Any] = {"gh_auth": gh_auth}

    _panel("Confirm reviewers", [
        f"Repo: {pal.BOLD}{repo}{pal.RESET}",
        "Confirm which reviewers are set up on THIS repo and which auto-review when",
        "a PR is opened. Seeded from your global default.",
    ], pal, stream)

    existing = config.load_config(cfg_path) if cfg_path.exists() else {}
    has_gd = config.has_global_default(existing)
    seed = list(config.active_reviewers(existing)) if has_gd else None

    reviewers, auto_on_open = step_reviewers(
        repo, cwd, doctor, run=run, spawn_command=spawn_command, getpass_fn=getpass_fn,
        pal=pal, stream=stream, multi_select=multi_select, single_select=single_select,
        input_fn=input_fn, seed=seed)

    if _ask_global_default():
        # BUDDHI_ASK_GLOBAL_DEFAULT restores the interactive promotion prompt.
        set_gd = step_set_global_default(repo, has_gd, reviewers, auto_on_open, pal=pal,
                                         stream=stream, single_select=single_select,
                                         input_fn=input_fn)
        # Guard the global-default WIPE: confirming a repo with an EMPTY fleet (e.g. the
        # user deselected every reviewer) must not silently clear an existing non-empty
        # global default. Ask, defaulting to KEEP — this repo's entry is still written.
        if set_gd and not reviewers and has_gd:
            current = list(config.active_reviewers(existing))
            if current:
                _row("warn", f"Your global default fleet is {', '.join(current)}. Setting "
                             "an EMPTY fleet as the global default clears it for every repo "
                             "with no confirmed reviewers.", pal, stream)
                keep = single_select(
                    "  Clear your global default reviewer fleet?",
                    [("No — keep my current global default",
                      f"leaves {', '.join(current)} in place; still writes this repo's entry"),
                     ("Yes — clear it",
                      "every repo with no confirmed fleet then has no reviewers")],
                    preselect=0, pal=pal, stream=stream, input_fn=input_fn)
                if keep == 0:
                    set_gd = False
                    _row("info", "Keeping your existing global default; writing only this "
                                 "repo's entry.", pal, stream)
    else:
        # Default: no prompt. The FIRST system-wide setup (no global default yet)
        # auto-promotes its fleet to the global default so cross-repo runs have a
        # fall-back; every later per-repo confirm leaves the established default
        # untouched. set_gd stays False when a global default already exists, so the
        # empty-fleet wipe can never fire in this branch. Require a non-empty fleet
        # so a first run with zero confirmed reviewers doesn't establish a vacuous
        # global default that silently suppresses reviewers on every other repo.
        set_gd = not has_gd and bool(reviewers)

    am = step_repo_auto_merge(repo, _repo_auto_merge_default(existing, repo), pal=pal,
                              stream=stream, single_select=single_select, input_fn=input_fn)
    lgc = step_repo_label_gated_ci(repo, config.label_gated_ci(existing, repo), pal=pal,
                                   stream=stream, single_select=single_select,
                                   input_fn=input_fn)
    # F4 — opting in is only a real saving if a workflow on the default branch
    # actually gates CI on the `ready-for-ci` label; install that mechanism (a
    # server-side PR with the CI command detected + confirmed). The probe-before-
    # install (P7 #4) skips a redundant PR when the gate is already present.
    if lgc:
        _offer_install_ready_for_ci(repo, cwd, run=run, pal=pal, stream=stream,
                                    input_fn=input_fn)

    ok = config.set_repo_keys(repo, {
        "active_reviewers": list(reviewers),
        "auto_on_open": {b: bool(v) for b, v in auto_on_open.items()},
        "auto_merge": bool(am),
        "label_gated_ci": bool(lgc),
    }, cfg_path)
    if ok and set_gd:
        ok = _write_global_default(reviewers, auto_on_open, cfg_path) and ok
    if not ok:
        _row("bad", f"Could not write {cfg_path} — check the path's permissions",
             pal, stream)
        return 1

    # Read everything back through the F1 readers so the summary reflects what
    # actually persisted, not just what we intended to write.
    cfg = config.load_config(cfg_path)
    final_reviewers = config.active_reviewers(cfg, repo)
    entry = config.repo_entry(cfg, repo) or {}
    _row("ok", f"Confirmed reviewers for {repo}: "
               f"{', '.join(final_reviewers) if final_reviewers else '(none)'}"
               + ("  ·  set as global default" if set_gd else ""), pal, stream)
    _row("ok", f"Auto-merge for {repo}: {'on' if entry.get('auto_merge') else 'off'}",
         pal, stream)
    _row("ok", f"Label-gated CI for {repo}: "
               f"{'on' if config.label_gated_ci(cfg, repo) else 'off'}", pal, stream)
    _row("info", f"Everyday usage: /review-pr {repo.split('/')[-1]} <pr-number>",
         pal, stream)
    return 0


# ── Orchestration ──────────────────────────────────────────────────────────────

def run(*, argv: Optional[Sequence[str]] = None, config_path: Optional[Path] = None,
        run: Optional[Callable] = None, which: Optional[Callable] = None,
        single_select: Callable = single_select, multi_select: Callable = multi_select,
        getpass_fn: Optional[Callable] = None, spawn_command: Optional[Callable] = None,
        input_fn: Callable = input, stream=None) -> int:
    """Run the full wizard. Every external effect is injectable for testing. Writes
    the free config and returns an exit code (0 on success)."""
    stream = stream or sys.stdout
    run = run or _default_run
    which = which or shutil.which
    getpass_fn = getpass_fn or _default_getpass
    spawn_command = spawn_command or setup_launcher.spawn_command
    cfg_path = config_path or config.config_path()
    pal = _Palette(_colour_enabled(stream))

    preset_repo = None
    args = list(argv or [])
    if "--repo" in args:
        i = args.index("--repo")
        if i + 1 < len(args):
            preset_repo = args[i + 1]

    try:
        # `setup --repo <owner/repo>` → the lightweight per-repo confirm mode (parity
        # with the reference wizard); the no-arg path runs the full wizard below.
        if preset_repo:
            print(f"{pal.BOLD}buddhi-review — confirm reviewers for a repo{pal.RESET}",
                  file=stream)
            return confirm_repo_interactive(
                preset_repo, None, run=run, spawn_command=spawn_command,
                getpass_fn=getpass_fn, pal=pal, stream=stream, cfg_path=cfg_path,
                multi_select=multi_select, single_select=single_select, input_fn=input_fn)

        print(f"{pal.BOLD}buddhi-review setup{pal.RESET}", file=stream)

        doctor = step_doctor(run=run, which=which, pal=pal, stream=stream)
        plan = step_plan(doctor, pal=pal, stream=stream, single_select=single_select, input_fn=input_fn)
        repo, cwd = step_repo(preset_repo, run=run, pal=pal, stream=stream, input_fn=input_fn)
        step_budgets_locked(pal=pal, stream=stream)  # paid teaser
        reviewers, auto_on_open = step_reviewers(
            repo, cwd, doctor, run=run, spawn_command=spawn_command, getpass_fn=getpass_fn,
            pal=pal, stream=stream, multi_select=multi_select, single_select=single_select,
            input_fn=input_fn)
        step_monitoring_locked(pal=pal, stream=stream)  # paid teaser

        existing = config.load_config(cfg_path) if cfg_path.exists() else {}
        # Per-repo confirmation for the bound repo: ask the auto-merge + label-gated
        # CI defaults so repos[<repo>] records a genuine per-repo opt-in.
        repo_auto_merge = repo_label_gated_ci = None
        if repo:
            repo_auto_merge = step_repo_auto_merge(
                repo, _repo_auto_merge_default(existing, repo), pal=pal, stream=stream,
                single_select=single_select, input_fn=input_fn)
            repo_label_gated_ci = step_repo_label_gated_ci(
                repo, config.label_gated_ci(existing, repo), pal=pal, stream=stream,
                single_select=single_select, input_fn=input_fn)
            # F4 — provision the ready-for-ci gate when the bound repo opts in
            # (same server-side install + probe-before-install as the confirm flow).
            if repo_label_gated_ci:
                _offer_install_ready_for_ci(repo, cwd, run=run, pal=pal, stream=stream,
                                            input_fn=input_fn)

        new_cfg = build_config(plan, repo, cwd, reviewers, auto_on_open)
        merged = merge_preserving(existing, new_cfg)
        ok = write_config(merged, cfg_path)
        # Record the bound repo's per-repo entry (presence == confirmed). The
        # top-level fleet written above is ALSO the global default — non-disruptive;
        # the full wizard always establishes one.
        if ok and repo:
            config.set_repo_keys(repo, {
                "active_reviewers": list(reviewers),
                "auto_on_open": {b: bool(v) for b, v in auto_on_open.items()},
                "auto_merge": bool(repo_auto_merge),
                "label_gated_ci": bool(repo_label_gated_ci),
            }, cfg_path)

        step_summary(plan, repo, reviewers, auto_on_open, pal=pal, stream=stream,
                     auto_merge=repo_auto_merge, label_gated_ci=repo_label_gated_ci)
        if ok:
            step_done(cfg_path, pal=pal, stream=stream)
            _offer_first_review(repo, pal=pal, stream=stream, input_fn=input_fn)
            return 0
        _row("bad", f"Could not write {cfg_path} — check the path's permissions", pal, stream)
        return 1
    except KeyboardInterrupt:
        print(f"\n{pal.RED}Setup aborted.{pal.RESET}", file=stream)
        return 1


def _default_getpass(prompt: str) -> str:
    import getpass
    try:
        return getpass.getpass(prompt)
    except Exception:
        return ""
