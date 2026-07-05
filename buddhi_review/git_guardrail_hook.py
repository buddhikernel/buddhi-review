#!/usr/bin/env python3
"""PreToolUse guardrail — block the AGENT from hand-running history-rewriting /
state-discarding git commands via the Bash tool.

WHY THIS IS A HOOK, NOT A WRITTEN RULE. The open-pr / review-pr skills do the
git mechanics for you — the open-pr actuator branches, commits, and pushes in
its own subprocess (never through the agent's Bash tool), so this hook never sees
it. When the agent instead does the surgery BY HAND it goes off-script, burns
tokens, and risks the branch. Prose rules in a SKILL.md body are advisory and the
model can ignore them; a PreToolUse hook is enforced by the harness and cannot be.

WHAT IT BLOCKS (direct agent Bash calls only): ``git rebase`` / ``git merge`` /
``git reset --hard`` (or ``--keep`` / ``--merge``) / ``git cherry-pick`` /
force-push (``git push --force`` / ``-f`` / ``--force-with-lease`` / a ``+``
refspec). Recovery forms (``--abort`` / ``--quit`` / ``--skip``) are ALLOWED, and
so is everything else (``status`` / ``log`` / ``diff`` / ``add`` / ``commit`` /
plain ``push`` / ``fetch`` / ``checkout`` / ``worktree`` / ``revert`` / soft
reset). A bare ``git`` appearing as an ARGUMENT to another command (``echo``, a
quoted commit message) is not a git invocation and is ignored.

ESCAPE HATCH: prefix the command with ``BUDDHI_ALLOW_MANUAL_GIT=1`` for a
deliberate, visible one-off override.

Wiring: registered as a PreToolUse(Bash) hook in each shipped skill's SKILL.md
``hooks:`` frontmatter, invoked as ``python3 -m buddhi_review.git_guardrail_hook``
so it resolves wherever the package is installed. Reads the hook JSON on stdin,
emits a PreToolUse permission decision on stdout. Fails OPEN (allows) on any
malformed input or internal error — it is a productivity guard, never a
session-breaker. Pure stdlib; tested by ``tests/test_git_guardrail_hook.py``.
"""
import json
import os
import re
import shlex
import sys

try:
    from buddhi_review import session_worktrees
except Exception:  # pragma: no cover - hook must import even if the module is absent
    session_worktrees = None

OVERRIDE_TOKEN = "BUDDHI_ALLOW_MANUAL_GIT=1"

# git GLOBAL options that consume the FOLLOWING token, so the real subcommand is
# the token after the value (e.g. ``git -C /path rebase`` → subcommand=rebase).
_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}

# Shell separator chars; a token made only of these ends one command segment.
_SEP_CHARS = set("()<>;|&")

# Shell keywords/tokens that reset the command position.
_CMD_STARTERS = {"then", "else", "elif", "do", "{", "}"}

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_CMD_PREFIXES = {"sudo", "env", "time", "nohup", "command", "builtin", "exec", "nice", "setsid", "stdbuf"}

# Per-prefix value-taking flags: maps a prefix to the set of its flags that
# consume the NEXT token as an argument.  Prefixes absent from this table
# (time, nohup, command, builtin, setsid) have no value-taking flags, so their
# flags are consumed one-at-a-time.  Keeping this per-prefix prevents the bypass
# `time -p git rebase main`: `-p` is value-taking only for sudo (prompt string),
# not for time (POSIX-format flag, takes no argument).
_PREFIX_VALUE_FLAGS = {
    # -h (--help) and -e (--edit) do NOT consume the next token; including them
    # caused `sudo -h git rebase main` to swallow 'git' and bypass the guard.
    "sudo": {"-u", "--user", "-g", "--group", "-p", "--prompt", "-C", "-c"},
    # -C/--chdir consume the next token as a directory argument (BSD + GNU env).
    "env":  {"-u", "--unset", "-C", "--chdir"},
    "exec": {"-a"},
    # -n/--adjustment consume the next token as the priority adjustment value.
    "nice": {"-n", "--adjustment"},
    # -i/--input, -o/--output, -e/--error each consume the next token as the buffer mode.
    "stdbuf": {"-i", "--input", "-o", "--output", "-e", "--error"},
}


def _tokenize(command):
    """Tokenize a shell command, treating ``&& || ; | ( )`` as their own tokens
    so chained commands (``git add . && git rebase``) split cleanly. Falls back
    to a whitespace split if the lexer chokes (unbalanced quotes)."""
    # Normalize Windows line endings first to prevent \\\r\n surviving the next strip.
    command = command.replace("\r\n", "\n")
    # Handle backslash line continuations
    command = command.replace("\\\n", "")
    # Normalize backslashes to forward slashes before shlex so Windows paths
    # (e.g. C:\Git\bin\git.exe) are not swallowed by posix=True escape handling,
    # while preserving legitimate shell escapes (escaped spaces, quotes, operators).
    command = re.sub(r"\\(?![ \t\"'&|;()<>])", "/", command)
    # Replace newlines with semicolons so they act as command separators
    command = command.replace("\n", " ; ")
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        # punctuation_chars still separates adjacent shell operators here; keep
        # whitespace_split so ordinary shell words are returned without quotes.
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        sanitized = command.replace('"', "").replace("'", "")
        for sep in ("&&", "||", ";", "|", "(", ")"):
            sanitized = sanitized.replace(sep, f" {sep} ")
        return sanitized.split()


def _is_git(tok):
    name = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name == "git"


def _is_sep(tok):
    return bool(tok) and set(tok) <= _SEP_CHARS


def _reason(action):
    return (
        f"Blocked: manual `git {action}` via the Bash tool. Buddhi's open-pr / "
        f"review-pr flows handle the git mechanics for you — the open-pr "
        f"actuator branches, commits, and pushes. Never hand-rewrite history or "
        f"force-push: it risks the branch and burns tokens. For a deliberate "
        f"one-off, prefix the command with {OVERRIDE_TOKEN}."
    )


def _check_git(args):
    """Given the tokens AFTER a ``git`` executable (up to the next separator),
    return (blocked, reason)."""
    # Skip git global options (some consume the next token as their value).
    k = 0
    while k < len(args) and args[k].startswith("-"):
        k += 2 if args[k] in _VALUE_OPTS else 1
    if k >= len(args):
        return (False, "")
    sub = args[k]
    rest = args[k + 1:]
    if sub == "rebase":
        if any(a in ("--abort", "--quit", "--skip", "--help", "-h") for a in rest):
            return (False, "")
        return (True, _reason("rebase"))
    if sub == "merge":
        if any(a in ("--abort", "--quit", "--help", "-h") for a in rest):
            return (False, "")
        return (True, _reason("merge"))
    if sub == "reset":
        for a in rest:
            # --ha = --hard, --k = --keep, --mer = --merge; no --merge-base exists in git reset.
            if a.startswith("--ha") or a.startswith("--k") or a.startswith("--mer"):
                return (True, _reason(f"reset {a}"))
        return (False, "")
    if sub == "cherry-pick":
        if any(a in ("--abort", "--quit", "--skip", "--help", "-h") for a in rest):
            return (False, "")
        return (True, _reason("cherry-pick"))
    if sub == "push":
        for a in rest:
            if a.startswith("--for") or a.startswith("--mi") or (a.startswith("-") and not a.startswith("--") and "f" in a) or a.startswith("+"):
                return (True, _reason(f"push {a}"))
        return (False, "")
    return (False, "")


def decide(command):
    """Return (blocked: bool, reason: str). blocked=True → deny the Bash call.

    Only a ``git`` token in COMMAND position (start of a segment, after optional
    ``VAR=val`` env prefixes) is treated as a git invocation — a ``git`` that is
    an argument to another command is ignored."""
    if not command or re.match(r"\s*" + re.escape(OVERRIDE_TOKEN) + r"(?:\s|$)", command):
        return (False, "")
    toks = _tokenize(command)
    n = len(toks)
    i = 0
    at_cmd_start = True
    while i < n:
        tok = toks[i]
        if _is_sep(tok) or tok in _CMD_STARTERS:
            at_cmd_start = True
            i += 1
            continue
        if at_cmd_start and tok in _CMD_PREFIXES:
            _pfx_vals = _PREFIX_VALUE_FLAGS.get(tok, set())
            i += 1
            # Consume any flags (and their arguments) that follow the prefix so
            # that e.g. `sudo -u root git rebase` or `env -i git rebase` still
            # land `git` in command position.  Uses the per-prefix table so that
            # `time -p git rebase` is not a bypass (time's `-p` takes no argument).
            while i < n and toks[i].startswith("-"):
                if toks[i] in _pfx_vals:
                    i += 2
                else:
                    i += 1
            continue
        if at_cmd_start and _ENV_ASSIGN.match(tok):
            i += 1  # leading env assignment; still at command position
            continue
        if at_cmd_start and _is_git(tok):
            j = i + 1
            args = []
            while j < n and not _is_sep(toks[j]):
                args.append(toks[j])
                j += 1
            blocked, reason = _check_git(args)
            if blocked:
                return (True, reason)
            i = j
            continue
        at_cmd_start = False
        i += 1
    return (False, "")


# ── session → worktree capture (side-effect, NEVER affects the block decision) ──
# The standing "do your work in a NEW worktree" rule means the agent creates and
# operates on a worktree (B) while its shell $PWD stays at the spawn checkout (A).
# The /open-pr + /review-pr skills then can't tell which worktree the session is on
# and open the PR from the wrong place. We close that here: this hook already sees
# every git command + the session id, so on `git worktree add B` (creation) OR
# `git -C B …` on a worktree under .claude/worktrees (operation) we record
# session_id → B. The skills' worktree resolver reads it to auto-open the loop on B
# with no prompt. Strictly best-effort: every step is exception-swallowed and runs
# AFTER the deny decision, so a capture bug can neither block a command nor allow a
# blocked one.
_WORKTREE_SEGMENT = "/.claude/worktrees/"
# `git worktree add` options that consume the FOLLOWING token (so the bare
# positional that is the new worktree PATH is found after skipping them).
_ADD_VALUE_OPTS = {"-b", "-B", "--reason"}


def _git_invocations(command, base_cwd=None):
    """The ``(effective_cwd, arg-list)`` of every git command in COMMAND position
    — mirrors decide()'s walk so a ``git`` that is an argument to another command
    (echo, a commit message) is ignored. A leading ``cd <dir>`` / ``pushd <dir>``
    on the same line is tracked so a ``cd X && git worktree add <rel>`` resolves
    the worktree under X, not the shell's starting cwd (the recorded path was
    otherwise a phantom under the spawn checkout). ``base_cwd`` is the session/
    payload cwd (``data['cwd']``) — a fixed snapshot of the spawn checkout, NOT
    the live persistent shell cwd (which the hook payload does not report); when
    None the cwd component of each pair is None."""
    toks = _tokenize(command)
    n = len(toks)
    i = 0
    at_cmd_start = True
    cwd = os.path.abspath(os.path.expanduser(base_cwd)) if base_cwd else None
    cwd_stack = []
    out = []
    while i < n:
        tok = toks[i]
        if tok == "(" and cwd is not None:
            cwd_stack.append(cwd)
            at_cmd_start = True
            i += 1
            continue
        if tok == ")" and cwd_stack:
            cwd = cwd_stack.pop()
            at_cmd_start = True
            i += 1
            continue
        if _is_sep(tok) or tok in _CMD_STARTERS:
            at_cmd_start = True
            i += 1
            continue
        if at_cmd_start and tok in _CMD_PREFIXES:
            _pfx_vals = _PREFIX_VALUE_FLAGS.get(tok, set())
            i += 1
            while i < n and toks[i].startswith("-"):
                if toks[i] in _pfx_vals:
                    i += 2
                else:
                    i += 1
            continue
        if at_cmd_start and _ENV_ASSIGN.match(tok):
            i += 1
            continue
        if at_cmd_start and cwd is not None and tok in ("cd", "pushd"):
            # Track a directory change so a following `git worktree add <rel>`
            # resolves against the cd target. The first bare (non-flag) argument is
            # the directory; an arg-less / flag-only `cd` (→ $HOME) or `cd -` leaves
            # the tracked cwd unchanged (unresolvable here — conservative).
            j = i + 1
            newdir = None
            while j < n and not _is_sep(toks[j]):
                if not toks[j].startswith("-") and toks[j] != "-":
                    # Skip pushd stack-index refs like +N — not a directory name.
                    if tok == "pushd" and re.match(r'^\+\d+$', toks[j]):
                        j += 1
                        continue
                    newdir = toks[j]
                    break
                j += 1
            if newdir is not None:
                cwd = os.path.abspath(os.path.join(cwd, os.path.expanduser(newdir)))
            while i < n and not _is_sep(toks[i]):  # consume the whole cd segment
                i += 1
            continue
        if at_cmd_start and _is_git(tok):
            j = i + 1
            args = []
            while j < n and not _is_sep(toks[j]):
                args.append(toks[j])
                j += 1
            out.append((cwd, args))
            i = j
            continue
        at_cmd_start = False
        i += 1
    return out


def _under_worktrees(path):
    """True iff ``path`` lives under a ``.claude/worktrees`` directory."""
    norm = path.replace(os.sep, "/")
    return _WORKTREE_SEGMENT in (norm + "/")


def _add_target(rest):
    """The new-worktree PATH positional from the tokens after ``worktree add``
    (skipping value-taking options like ``-b <branch>``), or None."""
    k = 0
    while k < len(rest):
        t = rest[k]
        if t in _ADD_VALUE_OPTS:
            k += 2
            continue
        if t.startswith("-"):
            k += 1
            continue
        return t  # first bare positional is the worktree path
    return None


def _worktree_from_args(args, base_cwd):
    """The absolute worktree path a single git invocation targets, or None:
    the ``worktree add`` PATH (resolved against the git process cwd, incl. -C),
    or a ``-C <dir>`` that itself sits under .claude/worktrees. The result is
    gated on living under ``.claude/worktrees`` (the standing convention) so an
    off-tree ``worktree add /tmp/x`` never lands in the registry — belt-and-
    suspenders, since the resolver already ignores a recorded path that is not an
    actual candidate.

    PHANTOM GUARD: this hook resolves a RELATIVE path against ``base_cwd`` — the
    FIXED Claude Code session/project dir — but the git command actually runs in
    the persistent shell's cwd, which the hook payload does NOT report. So after
    the agent ``cd``s into another repo, a relative ``git -C .claude/worktrees/X``
    resolves against the WRONG base and yields a directory that does not exist. For
    an OPERATION on an existing worktree (``-C <wt>``, i.e. NOT a ``worktree add``)
    that directory MUST already be on disk, so a non-existent candidate is a
    cross-repo mis-resolution — drop it rather than record a path the resolver
    would later mis-target. A ``worktree add`` TARGET is about to be created and
    does NOT exist yet at PreToolUse time, so it cannot be disk-checked here; the
    resolver's live-worktree filter drops a phantom creation at selection time
    instead."""
    dash_c = None
    k = 0
    while k < len(args) and args[k].startswith("-"):
        if args[k] in _VALUE_OPTS:
            if args[k] == "-C" and k + 1 < len(args):
                dash_c = args[k + 1]
            k += 2
        else:
            k += 1
    sub = args[k] if k < len(args) else None
    rest = args[k + 1:] if k < len(args) else []
    git_cwd = base_cwd
    if dash_c:
        git_cwd = os.path.abspath(os.path.join(base_cwd, os.path.expanduser(dash_c)))
    candidate = None
    is_add = sub == "worktree" and rest and rest[0] == "add"
    if is_add:
        target = _add_target(rest[1:])
        if target:
            candidate = os.path.abspath(os.path.join(git_cwd, os.path.expanduser(target)))
    elif dash_c:
        candidate = git_cwd
    if not candidate or not _under_worktrees(candidate):
        return None
    # Phantom guard (see the docstring): a ``-C`` OPERATION targets a worktree
    # that must already exist; if the resolved path is not a real directory the
    # relative path was mis-resolved against the session cwd — drop it. A
    # ``worktree add`` creation target does not exist yet at PreToolUse time and
    # so is left for the resolver's live-worktree filter.
    if not is_add and not os.path.isdir(candidate):
        return None
    return candidate


def _maybe_register_worktree(data):
    """Record session_id → the worktree this command creates/operates on. Pure
    side-effect; swallows everything."""
    try:
        if session_worktrees is None:
            return
        session_id = data.get("session_id")
        if not session_id:
            return
        tool_input = data.get("tool_input")
        command = (tool_input.get("command", "") or "") if isinstance(tool_input, dict) else ""
        # Cheap gate: only the two command shapes that name a worktree.
        if "worktree" not in command and ".claude/worktrees" not in command:
            return
        base_cwd = data.get("cwd") or os.getcwd()
        found = None
        for cwd, args in _git_invocations(command, base_cwd):
            wt = _worktree_from_args(args, cwd or base_cwd)
            if wt:
                found = wt  # last one in the command wins
        if found:
            session_worktrees.register(session_id, found)
    except Exception:
        return


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # fail OPEN — never break the session on malformed hook input
    if not isinstance(data, dict):
        return 0  # fail OPEN — valid JSON but not an object (e.g. list, null)
    if data.get("tool_name") != "Bash":
        return 0
    try:
        tool_input = data.get("tool_input")
        if not isinstance(tool_input, dict):
            return 0
        command = tool_input.get("command", "") or ""
        blocked, reason = decide(command)
    except Exception:
        return 0  # fail OPEN
    # Best-effort session→worktree capture — AFTER the deny decision so it can
    # never change whether a command is blocked (its own helper also fails open).
    _maybe_register_worktree(data)
    if blocked:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
