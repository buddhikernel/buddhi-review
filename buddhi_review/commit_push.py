"""Per-round commit + push with the test-before-push gate.

After a round's fixes are applied, the worktree is committed and pushed so the
re-requested reviewers see the new code. The gate runs the suite and on red asks
the human via the console channel — it never edits or reverts your tests. The
red-gate console ask offers three operator answers:

  1. **Push as-is** — bypass the gate this round and push the red tree.
  2. **Stop the run** — hand over for manual review (the default).
  3. **I've fixed it — re-run the gate & continue** — the operator edited the
     worktree at the host; commit any pending edits, re-run the FULL gate, and
     push + continue ONLY when it is green, asking again (never pushing) while
     it stays red. The gate is the sole arbiter — the operator's claim is
     re-verified, never trusted. It never auto-edits or reverts a test.

Test command resolution: env ``BUDDHI_TEST_COMMAND`` → the per-repo
``repos[<repo>].test_command`` → the global ``test_command`` → auto-detect (a
``tests/`` dir → ``python3 -m pytest tests/ -q``) → no gate (emits a ``⊘ [auto]``
notice so the skip is never silent). A configured command STRING carrying shell
syntax runs via ``bash -lc``; a bare command runs as a plain argv
(:func:`_split_test_command`). ``--test-failure-mode off`` skips the gate the
same loud way.

The red-gate panel is a self-contained escalation: it shows the MEANINGFUL slice
of the captured pytest output (the ``short test summary info`` / ``FAILURES``
block — never screens of leading ``...... [ NN%]`` progress dots) followed by the
clearly-labelled action options, so the operator reads what failed and what their
choices are in one block. The chosen lines are reproduced byte-for-byte (a blank
line inserted before each pytest section rule). The push addresses the branch by
its OWN name (``<remote> HEAD:refs/heads/<branch>``) so a mismatched-named or
dangling upstream can't fail it, falling back to a bare push for a detached HEAD
or an upstream-less worktree; either way the push stays non-force.

The console panels and phase-break spacing honour ``NO_COLOR`` /
``BUDDHI_LOOP_NO_COLOR`` (the same env names the rest of the pipeline honours).
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
import subprocess
import sys
from typing import Callable, Iterator, List, Optional, Sequence, Set, Tuple

from buddhi_review import config, lang_syntax, merge, test_runner
from buddhi_review.notifier import Ask, ConsoleNotifier, Notifier
from buddhi_review.transparency import automation_notice, _colour_enabled

_GIT_TIMEOUT = 120
# Hard timeout (seconds) on the pre-push test-gate subprocess. Env-overridable via
# BUDDHI_TEST_GATE_TIMEOUT_SECS; a non-positive / unparseable value uses the default.
_TEST_TIMEOUT_DEFAULT = 600

# Cap on consecutive "I've fixed it — re-run" turns before the loop hands over.
# Each turn blocks on a human answer, so this is a termination safety net (a
# non-interactive answer source can never spin the gate forever), not a budget.
_RERUN_LIMIT_DEFAULT = 3

# How many of the captured pytest tail's real lines the red-gate panel keeps.
_PYTEST_TAIL_LINES = 200

# How many lines of the MEANINGFUL failure slice the escalation message shows.
_FAILURE_EXCERPT_LINES = 24

# A pytest section / sub-section rule: a title wrapped in runs of `=`, `-`, `!`
# or `_` — `=== FAILURES ===`, `--- Captured stdout call ---`, `___ test_x ___`,
# `!!! Interrupted: 1 error during collection !!!`. The `_` FAILURES sub-section
# header is the only rule with an unbounded (test-id) title and pytest clamps its
# side fill to a single `_` on long ids, so that branch alone accepts `_+`; the
# fixed short `=`/`-`/`!` titles keep the `{3,}` guard so prose like `- foo -`
# and the rule-less `-- Docs: …` footer never match.
_PYTEST_SECTION_RE = re.compile(
    r"^\s*(?:(?:={3,}|-{3,}|!{3,})\s+\S.*\s+(?:={3,}|-{3,}|!{3,})"
    r"|_+\s+\S.*\s+_+)\s*$")

# A pure pytest progress line: a run of status chars (`. F E s x X`, plus spaces)
# optionally closed by a `[ NN%]` marker, or a bare `[ NN%]`. On a byte-capped
# tail these dominate the HEAD and are useless in an escalation — the failure
# detail (the FAILURES / short-test-summary section) lives near the END.
_PYTEST_PROGRESS_RE = re.compile(r"^[.FExsX\s]+(?:\[\s*\d+%\])?\s*$")

# The leading class headline `run_test_gate` prepends on a `compile_error` /
# `env_error` red (`_gate_class_headline`). Recognized so `failure_excerpt` can
# pull it out of the truncatable tail and always reattach it — it is control
# text naming WHY the gate is red, not part of the captured run output, and
# must survive the excerpt regardless of where the real failure detail falls.
_GATE_HEADLINE_RE = re.compile(r"^\[local-tests\] ✗ gate RED — ")

# Editor/backup droppings a fixer can leave in the worktree mid-round — a stray
# ``foo.bak`` from an in-place rewrite, an emacs ``.#lock`` lock file, a vim
# ``.swp``/``.swo`` swap, a macOS ``.DS_Store``. The per-round commit's ``git add
# -A`` must never sweep these into the PR (it happened once in the reference
# pipeline and two such files reached a repo's ``main``). Matched on the whole
# BASENAME, never a substring, so a real source file named ``bakery.py`` is safe.
_DROPPING_GLOBS: Tuple[str, ...] = (
    "*.bak", "*~", "*.orig", ".#*", "*.swp", "*.swo", ".DS_Store",
)

# The subset of `_DROPPING_GLOBS` whose backup name is DETERMINISTICALLY the
# source path plus a fixed suffix — `src.py` -> `src.py.bak`/`src.py~`/
# `src.py.orig`. `.DS_Store`, `.#*` (emacs lock) and `*.swp`/`*.swo` (vim swap,
# dot-prefixed) carry no such source-name relationship, so pairing those to a
# "source" would be a guess, not a fact (see `_backup_source`).
_BACKUP_SUFFIXES: Tuple[str, ...] = (".bak", "~", ".orig")

Run = Callable[..., "subprocess.CompletedProcess[str]"]


def _default_run(argv: Sequence[str], *, cwd: Optional[str] = None,
                 timeout: int = _GIT_TIMEOUT) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout,
        stdin=subprocess.DEVNULL, cwd=cwd,
    )


def _is_dropping(path: str) -> bool:
    """True iff ``path``'s basename matches a known editor/backup dropping glob
    (:data:`_DROPPING_GLOBS`). A trailing ``/`` (a collapsed untracked dir in
    porcelain) is stripped first so it is judged by its directory name, not ``""``."""
    base = os.path.basename(path.rstrip("/"))
    return any(fnmatch.fnmatchcase(base, g) for g in _DROPPING_GLOBS)


def _rerun_limit() -> int:
    try:
        return max(0, int(os.environ.get("BUDDHI_TEST_FAILURE_RERUNS",
                                         str(_RERUN_LIMIT_DEFAULT))))
    except (TypeError, ValueError):
        return _RERUN_LIMIT_DEFAULT


def _test_gate_timeout() -> int:
    """The pre-push test-gate timeout (seconds): ``BUDDHI_TEST_GATE_TIMEOUT_SECS``
    when set to a positive int, else :data:`_TEST_TIMEOUT_DEFAULT`. A non-positive
    or unparseable value falls back to the default (an infinite/zero gate is
    meaningless)."""
    try:
        value = int(os.environ.get("BUDDHI_TEST_GATE_TIMEOUT_SECS", ""))
    except (TypeError, ValueError):
        return _TEST_TIMEOUT_DEFAULT
    return value if value > 0 else _TEST_TIMEOUT_DEFAULT


# Shell metacharacters that force a command to run via `bash -lc` instead of a bare
# `shlex.split` argv. `shlex.split("npm ci && npm test")` yields a literal `"&&"`
# token and execvp would hand it to npm as an argument, so a command carrying ANY
# shell syntax must go to a shell: control operators (``&`` ``|`` ``;`` — covers
# ``&&`` / ``||`` / ``|`` / ``;`` / background ``&``), redirection (``<`` ``>`` —
# covers ``2>`` / ``>>`` / heredoc ``<<``), expansion (``$`` and backticks — ``$VAR``
# / ``$(…)`` / ``${…}`` / ``` `…` ```), subshell grouping (``(`` ``)``), and a
# newline. Quotes are NOT here (shlex handles quoting), and glob chars
# (``*`` ``?`` ``[`` ``]`` ``{`` ``}``) are deliberately NOT here: JS/Go/pytest
# runners expand their own path globs, so passing the literal pattern via execvp is
# what they expect. See `_command_needs_shell`.
_SHELL_METACHARS = frozenset("&|;<>()$`\n")


def _command_needs_shell(cmd: str) -> bool:
    """True when the test-command STRING `cmd` needs a shell (``bash -lc "<cmd>"``)
    rather than a bare `shlex.split` argv, because it uses shell syntax execvp
    cannot honour: a control operator (``&&`` / ``||`` / ``|`` / ``;`` / background
    ``&``), redirection (``>`` / ``<`` / ``2>`` / heredoc), expansion (``$VAR`` /
    ``$(…)`` / backticks), a subshell, a newline, a leading ``VAR=val`` environment
    prefix, or a ``cd <dir>`` step. A bare command (``npx vitest run``,
    ``go test ./...``) has none of these and runs directly — glob patterns are left
    literal because test runners expand their own. Over-matching a quoted operator
    (e.g. ``go test -run 'A|B'``) is harmless: ``bash -lc`` re-parses the quotes to
    the same argv `shlex` would produce."""
    s = (cmd or "").strip()
    if not s:
        return False
    if any(ch in _SHELL_METACHARS for ch in s):
        return True
    if re.match(r"[A-Za-z_][A-Za-z0-9_]*=", s):            # leading VAR=val prefix
        return True
    if re.search(r"(?:^|\s)cd\s", s):                     # a `cd <dir>` step
        return True
    return False


def _split_test_command(cmd: str) -> list:
    """Turn a test-command STRING into the argv the gate executes. A command with
    shell syntax (`_command_needs_shell`) is wrapped as ``["bash", "-lc", cmd]`` so
    the shell interprets the operators / env-prefix / ``cd``; a bare command splits
    to argv with `shlex` and runs directly (execvp, never ``shell=True``). A
    malformed bare command (an unbalanced quote → `shlex.split` ``ValueError``) also
    falls back to ``bash -lc`` so this NEVER raises — the malformed command then
    surfaces as a clean RED gate (a captured nonzero bash exit) instead of an
    uncaught traceback that would crash the loop (`run_test_gate`'s never-raises
    contract). Centralised here so the reference implementation and (via the same
    shape) this package agree on the rule that turns a configured command into an
    argv."""
    s = (cmd or "").strip()
    if _command_needs_shell(s):
        return ["bash", "-lc", s]
    try:
        return shlex.split(s)
    except ValueError:
        return ["bash", "-lc", s]


def resolve_test_command(cwd: str, repo: Optional[str] = None) -> Optional[List[str]]:
    """The command the pre-push test gate runs, as an argv list — or ``None`` for
    no gate (the caller's loud skip).

    Resolution (first non-blank source wins): env ``BUDDHI_TEST_COMMAND`` (the
    whole command); else the per-repo ``repos[<repo>].test_command``; else the
    top-level global ``test_command`` (both via
    :func:`buddhi_review.config.test_command`); else auto-detect — a ``tests/``
    dir means the pytest default ``python3 -m pytest tests/ -q``, nothing
    detectable means ``None``. A configured command STRING is turned into argv by
    `_split_test_command` (shell-operator commands → ``bash -lc``, bare commands →
    ``shlex.split``). A blank/whitespace value at any source falls through, so a
    config that predates the ``test_command`` key is byte-for-byte unchanged.
    ``repo=None`` reads env → global → auto-detect."""
    raw: Optional[str] = os.environ.get("BUDDHI_TEST_COMMAND")
    if not (raw and str(raw).strip()):
        cfg = config.load_config() if config.config_path().exists() else {}
        raw = config.test_command(cfg, repo)
    if raw and str(raw).strip():
        return _split_test_command(str(raw))
    if os.path.isdir(os.path.join(cwd, "tests")):
        return ["python3", "-m", "pytest", "tests/", "-q"]
    return None


def _changed_paths_from_porcelain(porcelain: str) -> List[str]:
    """The changed file paths from ``git status --porcelain`` output — the post-arrow
    name for a rename. Deleted / missing paths fall away at the caller's isfile
    filter. Best-effort: a path git quoted for odd characters keeps its quotes and
    simply fails the isfile check (no false alarm)."""
    paths: List[str] = []
    for line in (porcelain or "").splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        entry = line[3:]
        if ("R" in status or "C" in status) and " -> " in entry:  # rename / copy: "old -> new"
            entry = entry.split(" -> ", 1)[1]
        entry = entry.strip().strip('"')
        if entry:
            paths.append(entry)
    return paths


def _advisory_syntax_precheck(
    cwd: str, porcelain: str, *,
    notice: Callable[..., str] = automation_notice,
) -> Optional[str]:
    """Shift-left ADVISORY: a fast, language-keyed syntax check of the round's changed
    files (+ each embedded ``*_JS``-in-Python constant) BEFORE the (possibly
    minutes-long) test gate, so a fixer-introduced syntax error is named by
    file+line in milliseconds. It NEVER blocks the commit/push — it only informs,
    and it runs whether or not the test gate runs, so the ``off`` mode can't defeat
    it. A checker whose tool is absent SKIPS (never a false alarm); the whole pass is
    best-effort and never raises. Returns the advisory text it printed (or None)."""
    try:
        rels = _changed_paths_from_porcelain(porcelain)
        abspaths = [p for p in (os.path.join(cwd, rel) for rel in rels)
                    if os.path.isfile(p)]
        broken = lang_syntax.first_error(
            lang_syntax.check_paths(abspaths, repo_root=cwd))
    except Exception:
        return None
    if broken is None:
        return None
    return notice(
        "syntax pre-check",
        (f"{broken.lang} syntax error in {broken.path}"
         + (f" — {broken.detail}" if broken.detail else "")),
        status="do",
        hint="advisory — never blocks the commit")


def format_pytest_tail(tail: str, limit: int = _PYTEST_TAIL_LINES) -> List[str]:
    """Prepare a captured pytest tail for the red-gate panel: keep the last
    ``limit`` real lines and insert ONE blank line before each pytest section /
    sub-section rule so ``pytest -q``'s back-to-back sections don't run together.

    Presentation only — the captured lines themselves are reproduced byte-for-
    byte (never reformatted), and the inserted blank separators don't count
    toward ``limit``. An empty/None tail renders the literal placeholder."""
    if limit <= 0:
        return []
    out: List[str] = []
    prev_blank = True  # never open with a blank separator
    # splitlines() is intentional: handles \r\n, \r, and other line endings
    # correctly. `tail` is already fully in memory at this point, so there is no
    # OOM risk from splitting it; rsplit('\n', limit) would leave stray \r chars
    # on Windows-style output and is not a drop-in replacement.
    for line in (tail or "(no output captured)").splitlines()[-limit:]:
        if not prev_blank and _PYTEST_SECTION_RE.match(line):
            out.append("")
        out.append(line)
        prev_blank = not line.strip()
    return out


def failure_excerpt(tail: Optional[str], max_lines: int = _FAILURE_EXCERPT_LINES) -> str:
    """The MEANINGFUL slice of a captured ``pytest -q`` tail for the escalation
    message: the ``short test summary info`` section (it names every failed test
    + its error), else the first ``=== FAILURES/ERRORS ===`` block, else the END
    of the tail — NEVER the leading progress dots (``...... [ 68%]``), which on a
    byte-capped tail are all the head holds. Pure progress lines are dropped and
    the result is capped to ``max_lines`` with a truncation note so nothing drops
    silently. A leading ``run_test_gate`` class headline (``_GATE_HEADLINE_RE``,
    e.g. ``compile_error``/``env_error``) is pulled out first and always
    reattached as its own line — it names WHY the gate is red and must survive
    even when the real failure detail is long enough to fill the whole cap.
    Pure/testable; the red-gate panel renders this (via ``format_pytest_tail``)
    so the operator reads what FAILED, not screens of dots."""
    # Clamp: both capping branches keep ``max_lines - 1`` real lines + a one-line
    # truncation note, so a value below 2 would slice to ``[:0]`` (note-only,
    # content lost) or hit the ``[-0:]`` whole-list slice. The escalation always
    # wants at least one real line plus the note (mirrors ``format_pytest_tail``'s
    # own ``limit <= 0`` guard).
    max_lines = max(2, max_lines)
    raw = tail or ""
    headline, _, rest = raw.partition("\n")
    if _GATE_HEADLINE_RE.match(headline):
        raw = rest
    else:
        headline = ""
    lines = raw.splitlines()
    meaningful = [ln for ln in lines if not _PYTEST_PROGRESS_RE.match(ln)]
    if not meaningful:
        body = "(no failure detail captured)"
        return f"{headline}\n{body}" if headline else body

    def _find(pred: Callable[[str], bool]) -> Optional[int]:
        return next((i for i, ln in enumerate(meaningful) if pred(ln)), None)

    start = _find(lambda ln: "short test summary info" in ln)
    if start is None:
        start = _find(lambda ln: bool(_PYTEST_SECTION_RE.match(ln))
                      and ("FAILURES" in ln or "ERRORS" in ln))
    if start is not None:
        sect = meaningful[start:]
        if len(sect) > max_lines:
            kept = sect[:max_lines - 1]
            extra = len(sect) - len(kept)  # count AFTER slicing — the real drop
            sect = kept + [
                f"… (+{extra} more line(s) — re-run the test suite for the full output)"]
    else:
        # No FAILURES / summary marker in the captured tail → show the END (errors
        # live there, never the leading dots), noting any omitted head so nothing
        # drops silently.
        sect = meaningful[-(max_lines - 1):]
        omitted = len(meaningful) - len(sect)
        if omitted > 0:
            sect = [f"… (+{omitted} earlier line(s) omitted — re-run the test "
                    f"suite for the full output)"] + sect
    body = "\n".join(sect)
    return f"{headline}\n{body}" if headline else body


def _print_red_gate_panel(
    lines: List[str], *,
    options: Optional[Sequence[str]] = None,
    recommended_index: int = 0,
) -> None:
    """Print the escalate-only red-gate panel to stdout: a header, the failure
    excerpt bracketed by rules, and (when given) the clearly-labelled action
    options so the panel is a self-contained escalation — the operator reads WHAT
    failed and WHAT their choices are in one block. NO_COLOR /
    BUDDHI_LOOP_NO_COLOR / a non-TTY stream strip the colour; the glyph, text and
    option labels always print."""
    use_colour = _colour_enabled(sys.stdout)
    red = "\033[31m" if use_colour else ""
    reset = "\033[0m" if use_colour else ""
    rule = "─" * 72
    print(flush=True)
    print(f"{red}[local-tests] ✗ test gate RED — turbulence (failing tests), not pushing this round.{reset}",
          flush=True)
    print(rule, flush=True)
    for line in lines:
        print(f"  {line}" if line else "", flush=True)
    print(rule, flush=True)
    if options:
        print("  Turbulence (failing tests) — How to proceed (answer in the file linked below):", flush=True)
        for i, opt in enumerate(options, 1):
            star = "  (recommended)" if (i - 1) == recommended_index else ""
            print(f"    {i}. {opt}{star}", flush=True)
        print(rule, flush=True)


def _emit_no_tests_skip(runner_label: str) -> None:
    """Print the unmistakable no-tests SKIP notice: the resolved gate command RAN
    but the classifier (:func:`test_runner.classify`) found ZERO tests, so the gate
    is NOT red — it verified nothing, the same "no gate" posture as an undetectable
    suite. Loud on purpose ('zero coverage, not green') so a green push is never
    mistaken for a real pass. Never blocks the push (the caller returns
    ``skipped``)."""
    print(f"[local-tests] no tests detected for {runner_label} — gate SKIPPED "
          f"(zero coverage, not green)")


def _gate_class_headline(klass: str, runner_label: str) -> str:
    """A one-line RED-gate headline naming a ``compile_error`` / ``env_error`` class
    so the operator sees WHY the gate is red (the build / collection step failed, or
    the runner / a dependency is missing) rather than a bare nonzero exit. Returns
    ``""`` for ``passed`` / ``no_tests`` / ``failed`` / ``timeout`` — their tail is
    unchanged, so a plain failed-gate display stays byte-identical to before F2."""
    if klass == test_runner.COMPILE_ERROR:
        return (f"[local-tests] ✗ gate RED — compile_error ({runner_label}): the "
                f"build / collection step failed BEFORE any test ran. No "
                f"failing-test ids are extracted from a compile error.")
    if klass == test_runner.ENV_ERROR:
        return (f"[local-tests] ✗ gate RED — env_error ({runner_label}): the test "
                f"runner or a dependency is missing / the command could not run. No "
                f"failing-test ids are extracted from an env error.")
    return ""


def run_test_gate(
    cwd: str, *, repo: Optional[str] = None, run: Run = _default_run,
    notice: Callable[..., str] = automation_notice,
) -> Tuple[str, str]:
    """Returns ``(status, output_tail)`` with status ``green`` / ``red`` /
    ``skipped``. The resolved command's runner is detected (:mod:`test_runner`) and
    its exit CLASSIFIED into one of the six outcome classes, so a silent-exit-0
    runner (jasmine / Karma / go / VSTest / gtest / swift / cargo) that ran ZERO
    tests never false-greens AND pytest's exit 5 never false-reds (F2):

      * ``passed``                     → ``green``.
      * ``no_tests``                   → ``skipped``: the command ran but found no
        tests, so it verified nothing — the same loud "no gate" posture as an
        undetectable suite (:func:`_emit_no_tests_skip`), NEVER red.
      * ``compile_error`` / ``env_error`` → ``red`` with the CLASS named in a
        one-line tail headline (:func:`_gate_class_headline`).
      * ``failed`` / ``timeout``       → ``red`` (tail byte-identical to before F2).

    ``output_tail`` is the full combined stdout+stderr (the caller caps + formats
    it for display) for ``green``/``red`` — but ALWAYS ``""`` for ``skipped``
    (no detectable suite, or a `no_tests` classification), regardless of what the
    command printed: a skip verified nothing, so there is no failure detail to
    show and callers must not infer one from the tail. ``repo`` (``owner/repo``)
    scopes the per-repo ``test_command`` resolution; ``None`` reads env → global
    → auto-detect. A ``skipped`` on no detectable suite stays loud, never silent."""
    try:
        cmd = resolve_test_command(cwd, repo)
        if cmd is None:
            notice("test-gate", "no test suite detected — pushing unverified",
                   status="skip", hint="set BUDDHI_TEST_COMMAND to enable the gate")
            return "skipped", ""
        print(f"[local-tests] running {' '.join(cmd)} before push …", flush=True)
        proc = run(cmd, cwd=cwd, timeout=_test_gate_timeout())
    except subprocess.TimeoutExpired as exc:
        # A real timeout kills the process before `run()` returns, so it never
        # reaches the `classify()` call below. Route it through the SAME
        # classifier for consistency with the six-outcome contract; the tail
        # stays byte-identical either way since `_gate_class_headline` adds no
        # headline for TIMEOUT (matching a plain `failed`, both "before F2").
        info = test_runner.detect_runner(cwd, cmd)
        klass = test_runner.classify(info.runner, None, "", "", timed_out=True)
        headline = _gate_class_headline(klass, info.runner)
        tail = f"test command failed to run: {exc}"
        return "red", (f"{headline}\n{tail}" if headline else tail)
    except OSError as exc:
        # The command never spawned at all (missing runner binary, permission
        # denied, …) — that IS env_error by definition, so classify it directly
        # rather than feeding the exception text through `classify()`'s
        # stdout/stderr marker scan (built for a completed process's captured
        # output, not a Python exception string).
        info = test_runner.detect_runner(cwd, cmd)
        headline = _gate_class_headline(test_runner.ENV_ERROR, info.runner)
        tail = f"test command failed to run: {exc}"
        return "red", (f"{headline}\n{tail}" if headline else tail)
    except ValueError as exc:
        return "red", f"test command failed to run: {exc}"
    tail = (proc.stdout or "") + "\n" + (proc.stderr or "")
    # Detect the runner behind the resolved command and classify its outcome (F2):
    # a zero-test run of a silent-exit-0 runner classifies `no_tests` (SKIP, not a
    # false-green) and pytest exit 5 classifies `no_tests` (SKIP, not a false-red);
    # a compile / env failure is named apart from a genuine test failure. No triage
    # (failing-id extraction / scoped re-run) — this free skill's gate has none.
    info = test_runner.detect_runner(cwd, cmd)
    klass = test_runner.classify(info.runner, proc.returncode, tail, "", False)
    if klass == test_runner.NO_TESTS:
        _emit_no_tests_skip(info.runner)
        return "skipped", ""
    if klass == test_runner.PASSED:
        return "green", tail
    headline = _gate_class_headline(klass, info.runner)
    return "red", (f"{headline}\n{tail}" if headline else tail)


def _assert_clean_after_commit(
    cwd: Optional[str], *, run: Run = _default_run,
    notice: Callable[..., str] = automation_notice,
) -> None:
    """Tripwire: after a round's fixes are committed (``git add -A``) and pushed,
    the worktree MUST be clean. Residue means a fixer wrote files the commit did
    not capture — edits that never reached the PR. Surface it loudly with a
    ``⚠ [auto] fix-residue tripwire`` notice. Best-effort: any error is swallowed
    and the loop is never failed by this check."""
    if not cwd:
        return
    try:
        # ``-z`` for VERBATIM paths (matching :func:`_detect_droppings` — plain
        # porcelain C-quotes/escapes a path holding a quote, space-arrow, tab, or
        # non-ASCII byte, and renders a rename as a single ``old -> new`` line,
        # either of which would make a dropping name evade the filter below) and
        # ``--untracked-files=all`` so a wholly-untracked directory holding only a
        # dropping is enumerated as its individual file (``dir/foo.bak``) rather than
        # collapsed to a ``dir/`` entry whose basename would evade the dropping
        # filter below — matching :func:`_detect_droppings` so the two stay coherent.
        r = run(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=cwd)
    except Exception:
        return
    if getattr(r, "returncode", 1) != 0:
        return
    # The sweep guard (:func:`_stage_all`) deliberately leaves a NEW (untracked or
    # freshly-added) editor/backup dropping unstaged — that's not a lost fixer edit,
    # so it must not trip this "edits are not on the PR" alarm. A tracked dropping's
    # own modification/deletion IS staged and committed like any other change (see
    # :func:`_new_to_head`), so it never shows up here as residue in the first
    # place. A genuinely-lost non-dropping edit still fires the tripwire.
    residue = [path for xy, path in _iter_porcelain_z(getattr(r, "stdout", "") or "")
               if not (_is_dropping(path) and _new_to_head(xy))]
    if not residue:
        return
    shown = ", ".join(residue[:8])
    more = f" (+{len(residue) - 8} more)" if len(residue) > 8 else ""
    notice(
        "fix-residue tripwire",
        f"{len(residue)} uncommitted file(s) remained in the worktree AFTER "
        f"commit+push ({shown}{more}) — a fixer wrote outside the committed set; "
        f"those edits are NOT on the PR.",
        status="fallback", hint="clean-tree tripwire")


def _resolve_push_target(
    cwd: Optional[str], *, run: Run = _default_run,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the EXPLICIT push target ``(remote, branch)`` for the branch
    checked out in ``cwd`` — so a caller can push ``<remote> HEAD:refs/heads/<branch>``,
    addressing the branch by its OWN name and bypassing ``push.default`` and the
    tracking config entirely. A branch created off ``origin/<other>`` (that other
    branch later deleted) keeps that wrong/gone name as its upstream merge ref;
    under ``push.default=simple`` a bare ``git push`` then refuses (exit 128 —
    "upstream branch … does not match the name of your current branch") even
    though the branch is perfectly pushable by its own name.

    Returns ``(None, None)`` — the caller falls back to a bare ``git push`` —
    when EITHER HEAD is detached / the branch can't be resolved, OR the branch
    has NO configured upstream remote (``branch.<b>.remote`` unset). The
    no-upstream guard is LOAD-BEARING, not a convenience default: a NAMED branch
    with no upstream must keep failing a bare push loudly rather than have a
    remote synthesised for it (that would land a stray branch on the wrong repo
    and let the loop falsely conclude the round's fixes shipped).

    When an upstream IS configured, Git's push-remote precedence is honoured:
    ``branch.<b>.pushRemote`` overrides ``remote.pushDefault`` overrides
    ``branch.<b>.remote`` (the fetch remote) — so a fork workflow that pulls from
    one remote and pushes to another targets the right one. The ``(None, None)``
    guard still fires only on a missing ``branch.<b>.remote``, never on a missing
    ``pushRemote`` / ``pushDefault``."""
    def _cfg(key: str) -> Tuple[int, str]:
        # All result-attribute access stays inside the try so an odd run-seam
        # result (a stub, a non-CompletedProcess) degrades to the bare-push
        # fail-safe rather than raising up into the push path.
        try:
            r = run(["git", "config", "--get", key], cwd=cwd)
            return getattr(r, "returncode", 1), (getattr(r, "stdout", "") or "").strip()
        except Exception:
            return 1, ""

    try:
        br = run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=cwd)
        branch = (getattr(br, "stdout", "") or "").strip()
    except Exception:
        return None, None
    if not branch:
        return None, None  # detached HEAD / symbolic-ref failure
    rc, fetch_remote = _cfg(f"branch.{branch}.remote")
    if rc != 0 or not fetch_remote:
        return None, None  # no upstream remote → keep the bare-push fail-safe
    for key in (f"branch.{branch}.pushRemote", "remote.pushDefault"):
        prc, push_remote = _cfg(key)
        if prc == 0 and push_remote:
            return push_remote, branch
    return fetch_remote, branch


def _push_argv(cwd: Optional[str], *, run: Run = _default_run) -> List[str]:
    """The ``git push`` argv for the branch checked out in ``cwd``: explicit
    ``<remote> HEAD:refs/heads/<branch>`` when an upstream remote is configured
    (immune to a mismatched/dangling upstream), else a bare ``git push`` (a
    detached HEAD or no upstream remote — the documented fail-safe; see
    :func:`_resolve_push_target`). Either way the push stays NON-force: a
    stale/diverged tip is still REJECTED as non-fast-forward, so only HOW it
    pushes changes, never WHEN."""
    remote, branch = _resolve_push_target(cwd, run=run)
    if branch and remote:
        return ["git", "push", remote, f"HEAD:refs/heads/{branch}"]
    return ["git", "push"]


def _diagnose_commit_failure(
    cwd: str, head_before: Optional[str], *, run: Run = _default_run,
    notice: Callable[..., str] = automation_notice,
) -> None:
    """Diagnose a non-zero ``git commit``. When HEAD did NOT move and the tree is
    still dirty, the commit was almost certainly REJECTED by a local pre-commit
    hook — surface that with a distinct, actionable message instead of the bare
    generic error. Best-effort (any probe error is swallowed); the caller's return
    contract is unchanged (still ``"error"``)."""
    try:
        head_after = _git_rev_parse(cwd, "HEAD", run=run)
        st = run(["git", "status", "--porcelain"], cwd=cwd)
        still_dirty = bool((getattr(st, "stdout", "") or "").strip())
    except Exception:
        return
    if head_before == head_after and still_dirty:
        notice(
            "commit",
            "commit likely rejected by a pre-commit hook (or another git-level "
            "failure) — HEAD did not move and the worktree is still dirty, so this "
            "round's fixes are NOT committed. Check git output above for the cause, "
            "fix it (or bypass the hook), then re-run.",
            status="stop", hint="a local pre-commit hook or git-level failure blocked the commit")


# ── Fix-commit sweep guard ───────────────────────────────────────────────────────
# The per-round commit stages with ``git add -A``, which would sweep any editor/
# backup dropping (:data:`_DROPPING_GLOBS`) a fixer left behind into the PR. This
# guard keeps them out of staging while leaving every legitimate change untouched.
# It is best-effort and FAIL-OPEN: a status probe that errors just falls back to
# the plain ``git add -A`` so the guard can never block a commit.
#
# One paired case needs more than a plain exclude: a fixer/editor doing an
# in-place rewrite via backup-then-replace (move ``src.py`` to ``src.py.bak``,
# write a fresh ``src.py``) that fails BEFORE recreating ``src.py`` leaves the
# worktree showing ``D src.py`` (deleted, tracked) plus ``?? src.py.bak`` (new,
# untracked). Excluding only the backup would still let the plain ``git add -A``
# stage and commit ``src.py``'s deletion — landing a real-file deletion on the PR
# while its only surviving content sits in the excluded, uncommitted backup. See
# :func:`_risky_delete_pairs`, which holds the deleted source out of staging too.


def _new_to_head(xy: str) -> bool:
    """True iff a porcelain XY status code means the path has NO blob in HEAD yet
    — brand-new (untracked, ``??``) or freshly introduced in either porcelain
    column (``A ``/`` A``/``AM``/…). A tracked dropping that is merely modified
    or DELETED (``M``/``D``/``R``/…) already exists in HEAD, and the sweep guard
    must never hold that change back — Git is supposed to record it (in
    particular, a fixer's deletion of a previously-committed dropping must reach
    the commit, not be silently kept alive)."""
    return xy == "??" or "A" in xy[:2]


def _iter_porcelain_z(stdout: str) -> Iterator[Tuple[str, str]]:
    """Yield ``(xy, path)`` for each entry of ``git status --porcelain -z
    --untracked-files=all`` output, verbatim (no C-quoting/escaping — porcelain
    otherwise mangles any name holding a quote, space-arrow (``a -> b``), tab, or
    non-ASCII byte) and with a rename/copy record's ORIGIN field (the field right
    after an ``R``/``C`` entry) consumed so it is never misread as its own path."""
    fields = (stdout or "").split("\0")
    i = 0
    while i < len(fields):
        rec = fields[i]
        if len(rec) < 4:  # trailing empty field / malformed entry
            i += 1
            continue
        xy, path = rec[:2], rec[3:]  # "XY <path>" (no quoting under -z)
        if xy[0] in "RC" or xy[1] in "RC":
            i += 1  # a rename/copy — the ORIGIN path is the next field; skip it
        yield xy, path
        i += 1


def _status_entries(cwd: str, *, run: Run = _default_run) -> List[Tuple[str, str]]:
    """One ``git status --porcelain -z --untracked-files=all`` scan, parsed via
    :func:`_iter_porcelain_z` — the shared substrate for :func:`_detect_droppings`
    and :func:`_risky_delete_pairs` so both read the exact same worktree snapshot
    and can't drift out of sync with each other.

    ``--untracked-files=all`` is essential: without it a dropping inside a brand-new
    (otherwise-untracked) directory is hidden under a collapsed ``dir/`` porcelain
    entry and would slip past both callers' scans, riding the ``git add -A`` into
    the commit. Best-effort: any error or a non-zero status → ``[]`` (each caller
    then falls back to its own safe default)."""
    try:
        st = run(["git", "status", "--porcelain", "-z", "--untracked-files=all"],
                 cwd=cwd)
    except (subprocess.SubprocessError, UnicodeDecodeError, OSError):
        return []
    if getattr(st, "returncode", 1) != 0:
        return []
    return list(_iter_porcelain_z(getattr(st, "stdout", "") or ""))


def _detect_droppings(cwd: str, *, run: Run = _default_run) -> List[str]:
    """Repo-relative paths in ``cwd``'s worktree that are NEW editor/backup
    droppings (:func:`_is_dropping`) with no HEAD history (:func:`_new_to_head`)
    — the only ones ``_stage_all`` may safely hold out of the commit. A dropping
    that was already tracked (its own modification or deletion) is Git's to
    record like any other change and is deliberately excluded here, so it stages
    and commits normally instead of being silently kept alive."""
    entries = _status_entries(cwd, run=run)
    return [path for xy, path in entries if _is_dropping(path) and _new_to_head(xy)]


def _backup_source(path: str) -> Optional[str]:
    """The pre-backup path a suffix-style backup dropping (:data:`_BACKUP_SUFFIXES`)
    was made FROM — ``src.py.bak`` -> ``src.py`` — or ``None`` when ``path``
    carries none of those suffixes (including every other :data:`_DROPPING_GLOBS`
    pattern, which has no deterministic source-name relationship to pair)."""
    for suf in _BACKUP_SUFFIXES:
        if path.endswith(suf) and len(path) > len(suf):
            return path[: -len(suf)]
    return None


def _risky_delete_pairs(entries: Sequence[Tuple[str, str]]) -> Set[str]:
    """Deleted, non-dropping source paths from ``entries`` (already-parsed
    ``(xy, path)`` porcelain pairs) that are paired with a fresh backup dropping
    sitting beside them — a fixer/editor's backup-then-replace rewrite (move
    ``src.py`` to ``src.py.bak``) that failed before recreating ``src.py``.

    Excluding only the backup (the ordinary dropping path) would still let
    ``git add -A`` stage and commit the real file's deletion while its only
    surviving content sits in the excluded, uncommitted backup — a silent data
    loss. ``_stage_all`` holds these paths out of staging exactly like the
    backup they are paired with, so both remain uncommitted residue for a human
    to resolve rather than landing a lossy deletion."""
    entries = list(entries)
    deleted = {path for xy, path in entries if "D" in xy[:2] and not _is_dropping(path)}
    if not deleted:
        return set()
    backups = (path for xy, path in entries if _is_dropping(path) and _new_to_head(xy))
    sources = {src for b in backups if (src := _backup_source(b)) is not None}
    return deleted & sources


def _fmt_droppings(paths: Sequence[str], limit: int = 6) -> str:
    """A compact, bounded rendering of the excluded paths for the log line."""
    shown = ", ".join(paths[:limit])
    extra = len(paths) - limit
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def _stage_all(
    cwd: str, *, run: Run = _default_run,
    notice: Callable[..., str] = automation_notice,
) -> "subprocess.CompletedProcess[str]":
    """``git add -A`` for the round's commit, minus editor/backup droppings and
    any deleted source file paired with one (:func:`_risky_delete_pairs`).

    With nothing to exclude this is byte-identical to a plain ``git add -A``.
    Otherwise it stages the whole worktree (``:/`` — the top of the tree, so the
    scope matches a bare ``git add -A`` regardless of ``cwd``) with one
    ``:(top,exclude,literal)`` pathspec per excluded path — a repo-root-anchored
    LITERAL exact-path exclude (not a glob), aligning with the repo-root-relative
    porcelain paths, matching at any depth, and never mis-firing on a legitimate
    file — and emits an ``[auto]`` line naming what was kept out. Returns the ``git
    add`` result unchanged so the caller's return-code check is untouched."""
    entries = _status_entries(cwd, run=run)
    droppings = [path for xy, path in entries if _is_dropping(path) and _new_to_head(xy)]
    risky = _risky_delete_pairs(entries)
    excluded = droppings + sorted(risky)
    if not excluded:
        return run(["git", "add", "-A"], cwd=cwd)
    # An exclude pathspec only tells ``git add`` NOT to (re-)add a path — it never
    # UNstages one already in the index. A dropping a fixer had itself ``git add``-ed
    # (or a source deletion a fixer had itself staged) would otherwise survive the
    # exclude and ride the commit (and be falsely logged as excluded). Un-stage each
    # first so it is guaranteed out of the index; a reset on an unstaged path is a
    # harmless no-op, so the common (untracked) case is unaffected. ``git reset --
    # <pathspec>`` parses its arguments as pathspecs (not raw paths), so a path name
    # carrying glob metacharacters (``[``/``]``/``*``/``?``) could otherwise unstage
    # the WRONG path (or miss its own); wrap each in ``:(top,literal)`` — an exact,
    # repo-root-anchored match — mirroring the ``:(top,exclude,literal)`` form
    # already used for the ``git add`` exclude below.
    resets = [f":(top,literal){p}" for p in excluded]
    run(["git", "reset", "-q", "--", *resets], cwd=cwd)
    excludes = [f":(top,exclude,literal){p}" for p in excluded]
    add = run(["git", "add", "-A", "--", ":/", *excludes], cwd=cwd)
    if getattr(add, "returncode", 1) == 0:
        if droppings:
            notice("stage",
                   f"excluded {len(droppings)} editor/backup dropping(s) from the fix "
                   f"commit: {_fmt_droppings(droppings)}",
                   status="skip")
        if risky:
            sorted_risky = sorted(risky)
            notice("stage",
                   f"held back {len(risky)} deleted source file(s) paired with an "
                   f"excluded backup dropping — looks like an in-place rewrite that "
                   f"failed mid-way (moved to a backup, never recreated); resolve "
                   f"manually: {_fmt_droppings(sorted_risky)}",
                   status="stop")
    return add


def commit_and_push(
    cwd: str,
    *,
    message: str,
    repo: Optional[str] = None,
    run: Run = _default_run,
    notifier: Optional[Notifier] = None,
    answer_wait: Optional[Callable[[Notifier, Ask], Optional[str]]] = None,
    test_gate: bool = True,
    notice: Callable[..., str] = automation_notice,
) -> str:
    """Commit every working-tree change and push. Returns ``pushed`` /
    ``nothing`` (no changes) / ``stopped`` (human chose stop on a red gate, the
    gate timed out unanswered, or the re-run limit was reached) / ``error``.

    The red-gate ask has three answers — 1 = push as-is, 2 = stop,
    3 = "I've fixed it — re-run the gate & continue". Answer 3 commits any
    pending worktree edits, re-runs the FULL gate, and pushes + continues ONLY
    when green (asking again, never pushing, while red); the gate is the sole
    arbiter. ``answer_wait`` is the
    :func:`buddhi_review.escalation_wait.wait_for_answer` seam. ``repo``
    (``owner/repo``) scopes the gate's per-repo ``test_command`` resolution."""
    status = run(["git", "status", "--porcelain"], cwd=cwd)
    if status.returncode != 0:
        return "error"
    if not (status.stdout or "").strip():
        return "nothing"

    # Shift-left advisory: name a fixer-introduced syntax error in the round's
    # changed files in milliseconds, before the (possibly minutes-long) test gate.
    # ADVISORY ONLY — it never blocks the commit/push, and runs even when the gate
    # is disabled so the off-mode bypass can't defeat it.
    _advisory_syntax_precheck(cwd, status.stdout or "", notice=notice)

    if test_gate:
        reruns = 0
        while True:
            print(flush=True)  # phase break — the test gate is its own block
            gate, tail = run_test_gate(cwd, repo=repo, run=run, notice=notice)
            if gate != "red":
                break  # green / skipped → fall through to commit + push
            notifier = notifier or ConsoleNotifier()
            # Show the MEANINGFUL failure slice (the short-test-summary / FAILURES
            # block), not screens of leading `...... [ NN%]` progress dots — the
            # operator must read what actually broke to decide how to proceed.
            formatted = format_pytest_tail(failure_excerpt(tail))
            options = [
                "Push as-is (bypass the gate this round)",
                "Stop the run",
                "I've fixed it — re-run the gate & continue",
            ]
            _print_red_gate_panel(formatted, options=options, recommended_index=1)
            ask = Ask(
                id="test-gate",
                question="The local test gate is RED after this round's fixes — "
                         "how should it proceed?",
                options=options,
                recommended_index=1,
                detail="\n".join(formatted),
            )
            notifier.send(ask)
            answer = answer_wait(notifier, ask) if answer_wait else None
            ans = (answer or "").strip()
            if ans == "1":
                notice("test-gate", "red gate bypassed by operator answer",
                       status="fallback")
                break
            if ans == "3":
                if reruns >= _rerun_limit():
                    notice("test-gate", f"re-run limit reached ({reruns}) — "
                           "stopping (the gate is the sole arbiter)",
                           status="stop")
                    return "stopped"
                reruns += 1
                notice("test-gate", "operator reports a manual fix — committing "
                       "pending edits and re-running the FULL gate (pushes only if "
                       "green)", status="do")
                # Commit the operator's edits (with the round's fixes) so a green
                # re-run has them to push. Best-effort: a clean tree, an already-
                # committed tree, or a rejected commit just means nothing new
                # lands here and the re-run gate is the arbiter (the final push
                # ships whatever commit is present).
                if _stage_all(cwd, run=run, notice=notice).returncode == 0:
                    run(["git", "commit", "-m", message], cwd=cwd)
                continue
            # "2" / None / anything else → stop (the default).
            notice("test-gate", "red gate — stopping (no auto test-edit, "
                   "no revert)", status="stop")
            return "stopped"
    else:
        notice("test-gate", "gate disabled for this run", status="skip",
               hint="re-enable: --test-failure-mode escalate")

    print(flush=True)  # phase break — the commit step is its own block
    if _stage_all(cwd, run=run, notice=notice).returncode != 0:
        return "error"
    # Commit only when something is staged. The "I've fixed it" path — or an
    # operator who committed their own host-side fix — may have already captured
    # the tree; a no-op `git commit` exits nonzero and must NOT be misread as an
    # error (the push below still ships the existing commit).
    committed_now = False
    if run(["git", "diff", "--cached", "--quiet"], cwd=cwd).returncode != 0:
        head_before = _git_rev_parse(cwd, "HEAD", run=run)
        if run(["git", "commit", "-m", message], cwd=cwd).returncode != 0:
            _diagnose_commit_failure(cwd, head_before, run=run, notice=notice)
            return "error"
        committed_now = True
    # Droppings-only round: `_stage_all` above excluded every changed path (all
    # editor/backup droppings), so nothing was staged/committed here — and if HEAD
    # already matches the upstream tracking ref, the push below would ship nothing
    # new either. That is NO progress, not "pushed" (a genuine host-side commit the
    # operator made before this call still has something ahead of upstream and
    # takes the push path below as before).
    if not committed_now and _push_is_noop(cwd, run=run):
        return "nothing"
    print(flush=True)  # phase break — the push is its own block
    # Push by EXPLICIT refspec (via _push_argv) so a mismatched-named or dangling
    # upstream can't fail the push; falls back to a bare push for a detached HEAD
    # or an upstream-less worktree (the documented fail-safe). Still non-force: a
    # stale/diverged tip is REJECTED as non-fast-forward exactly as a bare push.
    proc = run(_push_argv(cwd, run=run), cwd=cwd)
    if proc.returncode != 0:
        notice("push", f"git push failed: {(proc.stderr or '').strip()[:200]}", status="fallback")
        return "error"
    # Clean-tree tripwire: the worktree must be clean after commit+push.
    _assert_clean_after_commit(cwd, run=run, notice=notice)
    return "pushed"


# ── Exit-rebase: rebase a hand-back PR onto latest base + --force-with-lease ─────
# A NEW capability and a deliberate, narrowly-scoped extension of this skill's
# "never rebases, force-pushes" stance (see merge.py): the per-round push above
# stays strictly non-force, and the squash-merge never rebases. The ONLY place a
# force-push is ever performed is here — and only on a manual-landing hand-back,
# only on the loop's OWN feature branch, only with --force-with-lease (never a
# bare -f), and only when the rebase is clean. There is no conflict resolver: the
# behaviour is the most conservative one possible — a clean rebase proceeds, but
# the FIRST sign of a conflict is escalated WITH a diagnosis (the conflicted
# files + the manual steps), never resolved; a best-effort restore to the
# pre-rebase state is attempted. It never leaves a half-rebased branch or
# silently swallows a conflict.


def _push_is_noop(cwd: Optional[str], *, run: Run = _default_run) -> bool:
    """True iff local ``HEAD`` already equals its upstream tracking ref (``@{u}``)
    — a push from here would ship nothing new. Any ambiguity (no upstream
    configured, detached HEAD, resolution error) → False, the conservative
    default that just lets the normal push path run as the fail-safe."""
    head = _git_rev_parse(cwd, "HEAD", run=run)
    upstream = _git_rev_parse(cwd, "@{u}", run=run)
    if head is None or upstream is None:
        return False
    return head == upstream


def _git_rev_parse(cwd: Optional[str], ref: str, *, run: Run = _default_run) -> Optional[str]:
    """The SHA ``ref`` resolves to, or None if it does not resolve / on any error.
    Uses ``--verify --quiet`` so a missing ref is a clean None, never a raise."""
    try:
        r = run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=cwd)
    except (subprocess.SubprocessError, OSError):
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    out = (getattr(r, "stdout", "") or "").strip()
    return out or None


def _rebase_conflicted_files(cwd: Optional[str], *, run: Run = _default_run) -> List[str]:
    """The unmerged (conflicted) paths during an in-progress rebase, via
    ``git diff --name-only --diff-filter=U``. Best-effort: any error → ``[]``."""
    try:
        r = run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=cwd)
    except (subprocess.SubprocessError, OSError):
        return []
    if getattr(r, "returncode", 1) != 0:
        return []
    return [ln.strip() for ln in (getattr(r, "stdout", "") or "").splitlines() if ln.strip()]


def _restore_branch(cwd: Optional[str], sha: str, *, run: Run = _default_run) -> None:
    """Best-effort restoration of the branch to exactly ``sha`` after an aborted rebase.

    Aborts any in-progress rebase first (``git rebase --abort`` — a no-op exit is
    ignored), then VERIFIES HEAD is the snapshot SHA and, only if it drifted,
    hard-resets to it. Belt-and-suspenders so a conflict can never leave a
    half-rebased branch behind. Every step swallows its own error, so restoration
    is not guaranteed if git commands themselves fail."""
    try:
        run(["git", "rebase", "--abort"], cwd=cwd)
    except (subprocess.SubprocessError, OSError):
        pass
    if _git_rev_parse(cwd, "HEAD", run=run) != sha:
        try:
            run(["git", "reset", "--hard", sha], cwd=cwd)
        except (subprocess.SubprocessError, OSError):
            pass


def exit_rebase(
    cwd: str,
    *,
    base: str,
    repo: Optional[str] = None,
    run: Run = _default_run,
    notice: Callable[..., str] = automation_notice,
) -> Tuple[str, str]:
    """Rebase the loop's OWN feature branch onto the latest ``base`` and
    ``git push --force-with-lease`` it, so a hand-back PR can be merged cleanly.

    See the module-level note above for the safety stance. Returns
    ``(status, detail)``:

      * ``"rebased"``  — clean rebase + ``--force-with-lease`` push succeeded.
      * ``"current"``  — already on top of ``base``; no-op (no gratuitous push).
      * ``"conflict"`` — a rebase conflict; ABORTED and a best-effort restore to
                         the pre-rebase SHA attempted (``_restore_branch`` swallows
                         errors, so restore is not guaranteed if git commands fail).
                         ``detail`` names the conflicted files and the manual rebase
                         steps.
      * ``"skipped"``  — a precondition was not met (dirty worktree, an
                         unresolvable push target, a failed fetch / base lookup).
                         ``detail`` says why; nothing was changed.
      * ``"error"``    — an unexpected git failure; a best-effort restore to the
                         pre-rebase SHA was attempted where needed. ``detail`` says
                         what to do by hand.

    The worktree MUST be clean (a dirty / poisoned worktree → ``"skipped"``), the
    push target MUST resolve to this branch's own name (reusing
    :func:`_resolve_push_target`), and the force-push is always
    ``--force-with-lease`` against the remote tip we last fetched, so a remote
    that advanced under us is rejected rather than clobbered. ``run`` is the same
    injectable git seam the rest of this module uses.

    ``repo`` (the PR's base ``owner/repo``, threaded from the round driver) is how
    the base remote stays ALIGNED with the behind-drift check
    (:func:`buddhi_review.merge._branch_is_behind_base`): both resolve the base
    remote by matching ``repo`` to a configured remote's URL FIRST (via
    :func:`buddhi_review.merge._remote_for_repo`), so a fork PR based on
    ``upstream/main`` is rebased onto ``upstream`` — not the fork's stale
    ``origin`` copy. Without it, the drift check could classify a PR as behind
    ``upstream/main`` while this path rebased ``origin/main`` (or reported
    "current"), leaving a behind+red PR stuck. When ``repo`` is absent or matches
    no configured remote, the resolution falls back to ``branch.<base>.remote``
    then the push remote, exactly as before."""
    # 1. Resolve THIS branch's own push remote + name. Detached HEAD / no
    #    upstream → skip (we will not synthesise a target for a force-push).
    remote, branch = _resolve_push_target(cwd, run=run)
    if not (remote and branch):
        return "skipped", ("could not resolve this branch's push target "
                           "(detached HEAD or no upstream) — not rebasing")

    # 2. The worktree MUST be clean — a rebase needs it, and a dirty tree is a
    #    poisoned/unverifiable state we never rebase or force-push.
    try:
        st = run(["git", "status", "--porcelain"], cwd=cwd)
    except (subprocess.SubprocessError, OSError) as exc:
        return "skipped", f"could not read the worktree state ({exc}) — not rebasing"
    if getattr(st, "returncode", 1) != 0:
        return "skipped", "could not read the worktree state — not rebasing"
    if (getattr(st, "stdout", "") or "").strip():
        return "skipped", "the worktree has uncommitted changes — not rebasing"

    # 3. Snapshot the pre-rebase SHA so any failure restores the branch exactly.
    head = _git_rev_parse(cwd, "HEAD", run=run)
    if not head:
        return "skipped", "could not resolve HEAD — not rebasing"

    # 4. Fetch the push remote so our own remote tip is current for the
    #    --force-with-lease check below.
    try:
        fr = run(["git", "fetch", remote], cwd=cwd)
    except (subprocess.SubprocessError, OSError) as exc:
        return "skipped", f"could not fetch from {remote} ({exc}) — not rebasing"
    if getattr(fr, "returncode", 1) != 0:
        return "skipped", f"could not fetch from {remote} — not rebasing"

    # Derive the base remote separately from the push remote.  In a fork setup
    # (push → origin/fork, PR base → upstream/main) the push remote and the
    # remote that hosts the base branch are different.
    #
    # The resolution order MUST match the behind-drift check
    # (merge._base_remote → _remote_for_repo), or the two paths disagree: the
    # drift check can classify a fork PR as behind ``upstream/main`` (via the
    # repo→remote-URL match) while this path, falling back to the push remote,
    # rebases ``origin/main`` (the fork's stale base copy) or reports "current" —
    # leaving a behind+red PR stuck. So resolve ``repo`` (the PR's base repo)
    # against a configured remote's URL FIRST — authoritative regardless of
    # branch.<base>.remote — then fall back to git config branch.<base>.remote,
    # then to the push remote (the typical non-fork deployment, where the push
    # remote doubles as the base remote).
    def _base_remote_cfg() -> str:
        if repo:
            matched = merge._remote_for_repo(repo, cwd=cwd, run=run)
            if matched:
                return matched
        try:
            r = run(["git", "config", "--get", f"branch.{base}.remote"], cwd=cwd)
            val = (getattr(r, "stdout", "") or "").strip()
            if getattr(r, "returncode", 1) == 0 and val:
                return val
        except (subprocess.SubprocessError, OSError):
            pass
        return remote

    base_remote = _base_remote_cfg()
    if base_remote != remote:
        try:
            bfr = run(["git", "fetch", base_remote], cwd=cwd)
            if getattr(bfr, "returncode", 1) != 0:
                base_remote = remote  # fall back; base_ref resolve may still fail below
        except (subprocess.SubprocessError, OSError):
            base_remote = remote

    base_ref = f"{base_remote}/{base}"
    base_sha = _git_rev_parse(cwd, base_ref, run=run)
    if not base_sha:
        return "skipped", f"could not resolve {base_ref} after fetch — not rebasing"

    # 5. Already on top of base (base is an ancestor of HEAD) → no-op. Never
    #    force-push a branch that is already current.
    try:
        anc = run(["git", "merge-base", "--is-ancestor", base_sha, "HEAD"], cwd=cwd)
        already_current = getattr(anc, "returncode", 1) == 0
    except (subprocess.SubprocessError, OSError):
        already_current = False
    if already_current:
        return "current", ""

    notice("exit-rebase",
           f"rebasing this branch onto the latest {base_ref} so the PR can be "
           f"merged cleanly", status="do",
           hint="manual-landing rebase (force-with-lease, own branch only)")

    # 5c. Detect a rebase already in progress — git rebase would reject this
    #     with a non-zero exit and no conflicted files, which would otherwise
    #     be misclassified as 'conflict'.
    try:
        gd = run(["git", "rev-parse", "--git-dir"], cwd=cwd)
        git_dir = (getattr(gd, "stdout", "") or "").strip()
        if git_dir:
            git_dir_abs = (git_dir if os.path.isabs(git_dir)
                           else os.path.join(cwd, git_dir))
            if (os.path.isdir(os.path.join(git_dir_abs, "rebase-merge"))
                    or os.path.isdir(os.path.join(git_dir_abs, "rebase-apply"))):
                return "error", ("a rebase is already in progress in this worktree "
                                 "— resolve or abort it first: `git rebase --abort`")
    except (subprocess.SubprocessError, OSError):
        pass

    # 6. Rebase onto the latest base.
    try:
        rb = run(["git", "rebase", base_ref], cwd=cwd)
    except (subprocess.SubprocessError, OSError) as exc:
        _restore_branch(cwd, head, run=run)
        return "error", (f"the rebase command failed to run ({exc}) — the branch "
                         f"was restored to its pre-rebase state")
    if getattr(rb, "returncode", 1) != 0:
        # Non-zero exit: check for actual unmerged files to distinguish a real
        # merge conflict from other git failures (e.g., unexpected error states).
        # Only return 'conflict' when conflicted files are present; otherwise
        # return 'error' to avoid a misleading diagnosis and wrong status routing.
        conflicted = _rebase_conflicted_files(cwd, run=run)
        _restore_branch(cwd, head, run=run)
        if not conflicted:
            return "error", (
                f"the rebase onto {base_ref} failed with a non-zero exit but no "
                f"conflicted files were found (not a merge conflict). The branch "
                f"was restored; rebase by hand: "
                f"`git fetch {base_remote} {base} && git rebase {base_ref}`.")
        files = ", ".join(conflicted)
        return "conflict", (
            f"the rebase onto {base_ref} hit a conflict in {files}; a restore to "
            f"the pre-rebase state was attempted. Rebase it by hand: "
            f"`git fetch {base_remote} {base} && git rebase {base_ref}`, resolve the "
            f"conflict, then `git push --force-with-lease` — verify your branch state before proceeding.")

    # 6b. A clean rebase leaves a clean tree. If anything is dirty (should never
    #     happen on a zero exit), restore and bail rather than force-push a mess.
    try:
        st2 = run(["git", "status", "--porcelain"], cwd=cwd)
        dirty_after = bool((getattr(st2, "stdout", "") or "").strip())
    except (subprocess.SubprocessError, OSError):
        dirty_after = True
    if dirty_after:
        _restore_branch(cwd, head, run=run)
        return "error", ("the rebase reported success but left an unexpected dirty "
                         "tree — the branch was restored; rebase by hand")

    # 7. Force-with-lease push the OWN branch by its own name. The --force-with-lease
    #    arg asserts the remote is still at the tip we just fetched, so a remote that
    #    advanced under us is REJECTED (never clobbered). Never a bare -f / --force.
    expect = _git_rev_parse(cwd, f"{remote}/{branch}", run=run)
    lease_arg = (f"--force-with-lease=refs/heads/{branch}:{expect}"
                 if expect else "--force-with-lease")
    push = ["git", "push", lease_arg, remote, f"HEAD:refs/heads/{branch}"]
    try:
        pp = run(push, cwd=cwd)
    except (subprocess.SubprocessError, OSError) as exc:
        return "error", (f"rebased locally but the --force-with-lease push failed "
                         f"to run ({exc}) — push it by hand")
    if getattr(pp, "returncode", 1) != 0:
        detail = (getattr(pp, "stderr", "") or getattr(pp, "stdout", "") or "").strip()[:200]
        return "error", (f"rebased locally but the --force-with-lease push was "
                         f"rejected: {detail} — push it by hand")
    return "rebased", ""
