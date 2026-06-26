#!/usr/bin/env python3
"""PreToolUse guardrail — block the AGENT from hand-running history-rewriting /
state-discarding git commands via the Bash tool.

WHY THIS IS A HOOK, NOT A WRITTEN RULE. The create-pr / review-pr skills do the
git mechanics for you — the create-pr actuator branches, commits, and pushes in
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
import sys
import re
import shlex

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
        f"Blocked: manual `git {action}` via the Bash tool. Buddhi's create-pr / "
        f"review-pr flows handle the git mechanics for you — the create-pr "
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
        if blocked:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }}))
    except Exception:
        return 0  # fail OPEN
    return 0


if __name__ == "__main__":
    sys.exit(main())
