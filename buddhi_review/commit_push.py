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

Cold-worktree hardening. A worktree whose gate has never run is COLD: the first
gate invocation installs dependencies and builds, dropping ``node_modules/`` /
``target/`` / ``build/`` / coverage trees into it. Two guards keep that from
poisoning the round:

  * the fix commit never sweeps a runner artifact in (:data:`_RUNNER_DROPPING_DIRS`,
    held back by a FIXED count-independent glob pathspec set — a tree with
    thousands of untracked ``node_modules`` files costs ONE pathspec, never one
    per file, so git's argv is never blown; a fixer's edit to a TRACKED file
    under such a dir is still committed, recovered per-file);
  * one runner's suite can be given its own ceiling
    (``BUDDHI_TEST_GATE_TIMEOUT_SECS_<RUNNER>``) without inflating every other
    runner's — unset is byte-identical to the global-only behaviour.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
import stat
import subprocess
import sys
from typing import (
    AbstractSet, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple,
)

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

# Runner droppings: dependency-install / build / cache / coverage DIRS a test or
# build runner drops into a COLD worktree, matched as a path SEGMENT at any depth
# (so a JS workspace's ``packages/*/node_modules`` is caught too). Held back only
# when UNTRACKED; a fixer's edit to a TRACKED file under one still lands (it is
# recovered per-file after the glob exclude in :func:`_stage_all`).
#
# These names are UNAMBIGUOUS — nothing but a runner output tree is ever called
# `node_modules` or `.pytest_cache` — so the segment matches at ANY position,
# including the LAST one. That final-segment match is load-bearing: a monorepo's
# tracked `node_modules` SYMLINK, replaced by a real install, is itself the path
# whose deletion has to be recovered.
_RUNNER_DROPPING_DIRS = frozenset({
    "node_modules",       # npm / yarn / pnpm / bun install
    "__pycache__",        # CPython bytecode cache
    ".pytest_cache",      # pytest's cache plugin
    ".gradle",            # Gradle project-local cache
    "_build",             # mix (Elixir) build tree
    "htmlcov",            # coverage.py HTML report
    ".nyc_output",        # nyc raw coverage
    ".tox", ".nox",       # tox / nox environment trees
})

# Runner-output dirs whose name is also a perfectly ordinary FILE name — `scripts/
# build`, a Go/Make repo's `deps` or `coverage` entrypoint script. Matched ONLY
# when a child segment follows (`build/classes/Main.class`), never as the last
# segment, so a plain file that happens to be *called* `build` is committed like
# any other source file instead of being silently withheld from the fix commit
# (which the tripwire would then also hide, leaving the round to report `pushed`
# with the fix missing). Exactly the shape the dotnet dirs below already use.
# :func:`_runner_globs` keeps the two layers in step by emitting only the
# `**/<d>/**` contents glob for these — never the bare `**/<d>`, which git matches
# against a file of that name just as readily as against the directory.
#
# Within a runner dir the depth stays unrestricted: Gradle/CMake output has no
# fixed child segment to key on, and a source-`build/`-vs-output-`build/` split is
# not expressible in the git-pathspec exclude layer that does the real holding-
# back. A TRACKED file under a source `build/` still lands via the per-file
# recovery, and a NEW file the fixer added DIRECTLY BESIDE committed source in one
# of these dirs is recovered the same way (`_tracked_source_dirs` — the repo having
# committed content in that exact directory is the evidence that it is source, not
# runner output). Only a NEW file under a directory holding no committed content at
# all — every real Gradle/CMake/cargo output tree — stays out.
_RUNNER_DROPPING_PARENT_DIRS = frozenset({
    "build",              # Gradle / CMake / generic build output
    "target",             # cargo (and Maven) build output
    "deps",               # mix (Elixir) dependency tree
    "coverage",           # JS coverage output (nyc / jest / vitest)
})

# dotnet build output dirs. A bare ``bin``/``obj`` segment collides with real
# source layouts (a Rust ``src/bin/`` multi-binary tree, a repo's own ``bin/``
# scripts dir), so these count as droppings ONLY when immediately followed by a
# build-config segment (``bin/Debug/…``, ``obj/Release/…``).
_RUNNER_DROPPING_DOTNET_DIRS = frozenset({"bin", "obj"})
_RUNNER_DROPPING_DOTNET_CONFIGS = frozenset({"Debug", "Release"})

# Coverage-artifact FILE basenames excluded the same way, at any depth.
_RUNNER_DROPPING_FILES = frozenset({
    ".coverage",          # coverage.py data file
    "coverage.xml",       # coverage.py / pytest-cov XML report
    "lcov.info",          # lcov / JS coverage report
})

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


def _is_hard_runner_dropping(segs: Sequence[str]) -> bool:
    """The UNAMBIGUOUS half of :func:`_is_runner_dropping`, split out so the
    ambiguous half can be overridden without ever weakening this one: an
    :data:`_RUNNER_DROPPING_DIRS` segment (nothing but a runner tree is called
    ``node_modules`` or ``.pytest_cache``), a dotnet ``bin/{Debug,Release}`` /
    ``obj/{Debug,Release}`` build-output dir, or an :data:`_RUNNER_DROPPING_FILES`
    coverage file. No repo evidence can make one of these source, so a
    ``build/node_modules/x.js`` inside the repo's own committed ``build/`` stays an
    artifact."""
    if any(s in _RUNNER_DROPPING_DIRS for s in segs):
        return True
    for i, s in enumerate(segs[:-1]):
        if (s in _RUNNER_DROPPING_DOTNET_DIRS
                and segs[i + 1] in _RUNNER_DROPPING_DOTNET_CONFIGS):
            return True
    return segs[-1] in _RUNNER_DROPPING_FILES


def _is_runner_dropping(path: str, *,
                        source_dirs: AbstractSet[str] = frozenset()) -> bool:
    """True iff ``path`` is — or lies under — a :data:`_RUNNER_DROPPING_DIRS`
    directory, lies UNDER a :data:`_RUNNER_DROPPING_PARENT_DIRS` directory (those
    names double as ordinary file names, so they count only with a child segment
    after them — a plain ``scripts/build`` script is NOT an artifact — or when the
    path is itself a porcelain DIRECTORY entry, a trailing ``/``, which says
    "directory" just as conclusively as a child segment does), lies under a
    dotnet ``bin/{Debug,Release}`` / ``obj/{Debug,Release}`` build-output dir, or is
    a :data:`_RUNNER_DROPPING_FILES` coverage file, by path SEGMENT at any depth.
    Kept exactly in step with :func:`_runner_exclude_pathspecs` (what ``git add``
    actually holds back) so the held-back set, the tracked-file recovery, and the
    clean-tree tripwire never drift from the real staging behaviour.

    ``source_dirs`` (from :func:`_tracked_source_dirs`) is the ONE thing that can
    overturn a match, and only the AMBIGUOUS ``build``/``target``/``deps``/
    ``coverage`` one: a directory that DIRECTLY holds files committed in HEAD is
    source by the repo's own evidence, so a fixer's new ``build/new_rule.py``
    sitting beside a committed ``build/existing.py`` is a real edit, not runner
    output. Without
    that escape the guard held such a file out of the fix commit AND hid it from
    the clean-tree tripwire, so the round reported ``pushed`` with the fix missing.
    Callers that cannot consult the repo (or are asking what the FIXED glob layer
    holds back, which knows nothing of tracked-ness) pass nothing and get the
    unchanged path-only answer. The override never applies to a porcelain
    directory entry (a trailing ``/`` — an untracked nested git repo), which must
    keep being held back rather than staged as a stray gitlink.

    ``/`` is the ONLY separator recognised, deliberately: ``git status``'s
    porcelain emits ``/``-separated paths on every platform (Windows included)
    and :func:`_runner_globs` emits ``/``-only globs, so treating a literal
    backslash as a separator could never help a real path — it would only make
    this predicate claim a file (basename ``bin\\Debug\\App.dll``) that the git
    exclude layer cannot match, which would report the file as held back while it
    rode into the commit."""
    raw = path or ""
    norm = raw.strip("/")
    if not norm:
        return False
    segs = norm.split("/")
    if _is_hard_runner_dropping(segs):
        return True
    # ``segs[:-1]`` only — a name that is also a plausible FILE name counts as a
    # runner dir solely when something lives under it. The ONE exception is a
    # porcelain DIRECTORY entry (trailing ``/``), where the LAST segment counts
    # too: ``--untracked-files=all`` expands every untracked directory EXCEPT a
    # nested git repo, which it collapses to a single ``?? deps/`` record, so a
    # build step that clones straight into an ambiguous root leaves the artifact
    # name as the final segment with no child to key on. The trailing slash is
    # itself the disambiguation that child segment was standing in for — the entry
    # is a DIRECTORY, never the ``scripts/build`` FILE this rule protects — and
    # without the exception the whole checkout rides into the customer's PR as a
    # mode-160000 gitlink (nothing else flags it, so ``_stage_all`` takes its bare
    # ``git add -A`` shortcut).
    ambiguous = segs if raw.endswith("/") else segs[:-1]
    if not any(s in _RUNNER_DROPPING_PARENT_DIRS for s in ambiguous):
        return False
    return not (source_dirs and not raw.endswith("/")
                and "/".join(segs[:-1]) in source_dirs)


def _runner_globs() -> List[str]:
    """The raw repo-root-relative glob bodies for every RUNNER dropping (an
    unambiguous dir at any depth + its contents, a file-name-shaped dir's CONTENTS
    only, dotnet ``bin``/``obj`` only under a build-config child, coverage files),
    sorted for a deterministic order. Wrapped in the right pathspec magic by
    :func:`_runner_exclude_pathspecs` (exclude, for ``git add``) and by
    :func:`_stage_all`'s reset (include, to un-stage pre-staged runner content).

    A git pathspec matches PATHS, not types, so a bare ``**/build`` excludes the
    ``scripts/build`` shell script exactly as readily as the ``build/`` output
    tree. :data:`_RUNNER_DROPPING_PARENT_DIRS` therefore gets ONLY the ``/**``
    contents form — mirroring :func:`_is_runner_dropping`'s ``segs[:-1]`` test, so
    the predicate and the exclude layer agree on every path. That contents form
    also covers the collapsed nested-repo entry (``?? deps/``) the predicate's
    trailing-slash exception classifies: git's traversal drops the whole
    would-be-gitlink directory rather than staging it, verified against ``git add
    -A -- :/ :(top,exclude,glob)**/deps/**``."""
    globs: List[str] = []
    for d in sorted(_RUNNER_DROPPING_DIRS):
        globs.append(f"**/{d}")
        globs.append(f"**/{d}/**")
    for d in sorted(_RUNNER_DROPPING_PARENT_DIRS):
        globs.append(f"**/{d}/**")
    for d in sorted(_RUNNER_DROPPING_DOTNET_DIRS):
        for c in sorted(_RUNNER_DROPPING_DOTNET_CONFIGS):
            globs.append(f"**/{d}/{c}")
            globs.append(f"**/{d}/{c}/**")
    for f in sorted(_RUNNER_DROPPING_FILES):
        globs.append(f"**/{f}")
    return globs


def _runner_exclude_pathspecs() -> List[str]:
    """``git add`` exclude pathspecs for every RUNNER dropping — a FIXED,
    count-INDEPENDENT dir/file glob set (never per-file), so a cold worktree with
    thousands of untracked ``node_modules`` files is held back by ONE pathspec,
    not thousands (which would blow git's argv). ``:(top,exclude,glob)`` anchors
    to the repo root and lets ``**`` span path components, matching
    :func:`_stage_all`'s ``:/`` scope; where a bare dir form is emitted it covers
    the dir entry itself (a tracked ``node_modules`` symlink), the ``/**`` twin its
    contents."""
    return [f":(top,exclude,glob){g}" for g in _runner_globs()]


def _rerun_limit() -> int:
    try:
        return max(0, int(os.environ.get("BUDDHI_TEST_FAILURE_RERUNS",
                                         str(_RERUN_LIMIT_DEFAULT))))
    except (TypeError, ValueError):
        return _RERUN_LIMIT_DEFAULT


# Per-runner override for the gate timeout:
# BUDDHI_TEST_GATE_TIMEOUT_SECS_<RUNNER> (the `test_runner` constant upper-cased,
# every non-alphanumeric character → "_": _GRADLE, _CARGO, _NODE_TEST, …) gives ONE
# runner's suite its own ceiling, so a slow JVM/native suite can get more time
# without inflating pytest's. Resolved per gate call; with NO per-runner var set,
# every path uses the global BUDDHI_TEST_GATE_TIMEOUT_SECS / default below —
# byte-identical to the behaviour before this override existed.
_PER_RUNNER_TIMEOUT_ENV_PREFIX = "BUDDHI_TEST_GATE_TIMEOUT_SECS_"


def _per_runner_timeout_configured() -> bool:
    """True iff ANY per-runner timeout override is present in the environment —
    the cheap guard that keeps the unset path byte-identical (no pre-run
    ``detect_runner`` call is made unless an override could actually apply)."""
    return any(k.startswith(_PER_RUNNER_TIMEOUT_ENV_PREFIX) for k in os.environ)


def _test_gate_timeout(runner: Optional[str] = None) -> int:
    """The pre-push test-gate timeout (seconds) for ``runner``:
    ``BUDDHI_TEST_GATE_TIMEOUT_SECS_<RUNNER>`` when set (clamped to a 1s minimum;
    blank/unparseable is ignored → the global), else the global
    ``BUDDHI_TEST_GATE_TIMEOUT_SECS`` when set to a positive int, else
    :data:`_TEST_TIMEOUT_DEFAULT`. A non-positive or unparseable global falls back
    to the default (an infinite/zero gate is meaningless). ``runner`` is a
    :mod:`test_runner` constant (``"pytest"``, ``"gradle"``, …); ``None`` / an
    unknown runner / no override reads the global exactly as before."""
    if runner:
        key = _PER_RUNNER_TIMEOUT_ENV_PREFIX + re.sub(
            r"[^A-Za-z0-9]", "_", str(runner)).upper()
        raw = os.environ.get(key)
        if raw not in (None, ""):
            try:
                return max(1, int(raw))
            except (TypeError, ValueError):
                pass
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
        timeout_secs = _test_gate_timeout()
        if _per_runner_timeout_configured():
            # Resolve the runner BEFORE the run ONLY when a per-runner override
            # exists — the unset path keeps today's exact call order (detection
            # stays post-run); `detect_runner` is read-only, so the extra call is
            # side-effect-free. The post-run detection below is unchanged.
            try:
                timeout_secs = _test_gate_timeout(
                    test_runner.detect_runner(cwd, cmd).runner)
            except Exception:  # noqa: BLE001 — a detection bug must never break the gate
                pass
        proc = run(cmd, cwd=cwd, timeout=timeout_secs)
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
    # freshly-added) editor/backup dropping — and a NEW untracked RUNNER artifact a
    # cold-worktree gate run dropped — unstaged; that's not a lost fixer edit, so it
    # must not trip this "edits are not on the PR" alarm. A tracked dropping's own
    # modification/deletion IS staged and committed like any other change (see
    # :func:`_new_to_head`), so it never shows up here as residue in the first
    # place. The held-back set is read from the guard's own single source
    # (:func:`_held_back_new_artifacts`) so the two can never drift. A
    # genuinely-lost non-dropping edit still fires the tripwire.
    entries = list(_iter_porcelain_z(getattr(r, "stdout", "") or ""))
    held_back = _held_back_new_artifacts(
        entries, source_dirs=_tracked_source_dirs(cwd, entries, run=run))
    residue = [path for _xy, path in entries if path not in held_back]
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
#
# The contract is SPLIT. The per-file editor/backup and paired-delete holds are
# best-effort and FAIL-OPEN: they are derived from :func:`_status_entries`, which
# returns ``[]`` on a failed scan — indistinguishable from "nothing to hold
# back" — so an errored probe just means no per-file excludes are applied. The
# runner-artifact holds are FAIL-CLOSED: the FIXED glob set
# (:func:`_runner_exclude_pathspecs`) is applied to every ``git add`` regardless
# of what the scan returned, so a failed probe can never fall back to a bare
# ``git add -A`` that sweeps an untracked ``node_modules``/``target`` tree into
# the commit. And the guard can now block a commit outright: when a reset it
# depends on (un-staging a rename dropping, an editor exclude, or a prestaged
# runner artifact) itself fails, ``_stage_all`` returns that non-zero result
# instead of falling through, and :func:`commit_and_push` turns it into
# ``"error"`` rather than shipping a partially-staged commit.
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


def _dirty_beyond_held_back(porcelain_z: str, cwd: Optional[str] = None, *,
                            run: Run = _default_run) -> bool:
    """True iff ``git status --porcelain -z --untracked-files=all`` output shows an
    uncommitted change BEYOND the NEW artifacts the staging guard deliberately
    withheld from the fix commit (:func:`_held_back_new_artifacts`) — the
    rebase-precondition twin of :func:`_assert_clean_after_commit`'s residue
    filter, reading the same porcelain form through the same predicate so the two
    can never disagree about what counts as residue.

    Only an UNTRACKED (``??``) held-back path is tolerated. An untracked file never
    blocks ``git rebase``; anything carrying an index or tracked-worktree delta —
    including the held-back half of a risky-delete / moved-into-a-runner-dir pair,
    deliberately absent from that set — would make the rebase itself fail, so it
    still counts as dirty.

    ``cwd`` lets the same tracked-source-dir question be asked here as at staging
    time (:func:`_tracked_source_dirs`); without it the two predicates would part
    ways, and an uncommitted new file beside committed source in a ``build/`` would
    read as a withheld artifact and let a rebase run over it."""
    entries = list(_iter_porcelain_z(porcelain_z or ""))
    source_dirs = _tracked_source_dirs(cwd, entries, run=run) if cwd else frozenset()
    held_back = _held_back_new_artifacts(entries, source_dirs=source_dirs)
    return any(xy != "??" or path not in held_back for xy, path in entries)


def _held_back_new_artifacts(
    entries: Sequence[Tuple[str, str]], *,
    source_dirs: AbstractSet[str] = frozenset(),
) -> Set[str]:
    """The NEW-to-HEAD paths :func:`_stage_all` deliberately holds out of the fix
    commit: an editor/backup dropping (:func:`_is_dropping`) and — for a cold
    worktree — an untracked RUNNER artifact (:func:`_is_runner_dropping`:
    ``node_modules/``, ``target/``, coverage output, …).

    This is the guard's OWN single-source list of what it withholds, so the
    clean-tree tripwire (:func:`_assert_clean_after_commit`) can never lag the
    guard's staging behaviour: a round whose only worktree residue is a cold-gate
    artifact reads as a legitimate no-op, not as a fixer's lost edits. The
    risky-delete pair (:func:`_risky_delete_pairs`) is deliberately NOT in this
    set — that deletion IS a lost real-file change and must keep firing the
    tripwire.

    ``source_dirs`` (:func:`_tracked_source_dirs`) must be the SAME answer
    :func:`_stage_all` staged by, or this set over-claims: a new file beside
    committed source in a ``build/`` is recovered into the commit, so calling it
    held-back would hide a genuinely lost edit from the tripwire. Callers pass what
    the repo says; the empty default is the path-only reading."""
    return {path for xy, path in entries
            if (_is_dropping(path)
                or _is_runner_dropping(path, source_dirs=source_dirs))
            and _new_to_head(xy)}


# Byte budget for ONE git invocation's pathspec arguments. Every PER-FILE pathspec
# list the staging guard builds (the un-stage resets, the tracked-runner recovery)
# is bounded by the worktree's file COUNT, so a big enough tree — a vendored
# ``node_modules`` of 20k tracked files that a runner just reinstalled — would
# otherwise exceed the OS ``ARG_MAX`` and raise ``OSError: [Errno 7] Argument list
# too long`` straight out of the round. 100 KB is an order of magnitude under the
# smallest ``ARG_MAX`` in practice (Linux/macOS are ≥ 256 KB), so the batching is
# invisible in the common case (one call) and merely issues a few more calls on a
# huge tree. The FIXED glob-exclude set is count-independent and never batched.
_PATHSPEC_ARGV_BUDGET = 100_000


def _run_batched(
    run: Run, argv_prefix: Sequence[str], pathspecs: Sequence[str], *, cwd: str,
) -> Optional["subprocess.CompletedProcess[str]"]:
    """Run ``argv_prefix + <pathspecs>`` in as many invocations as it takes to keep
    every argv well under the OS ``ARG_MAX`` (:data:`_PATHSPEC_ARGV_BUDGET`),
    stopping at the first non-zero result and returning it. An empty
    ``pathspecs`` runs nothing and returns ``None``.

    An ``OSError`` from the spawn itself (``E2BIG`` on a pathological path, a
    missing git) is converted into a non-zero :class:`~subprocess.CompletedProcess`
    rather than propagated: the staging guard's contract is that its caller reads a
    return code, so a crash here would kill the round instead of handing back
    cleanly."""
    last: Optional["subprocess.CompletedProcess[str]"] = None
    batch: List[str] = []
    size = 0

    def _flush() -> bool:
        nonlocal last, batch, size
        if not batch:
            return True
        argv = [*argv_prefix, *batch]
        try:
            last = run(argv, cwd=cwd)
        except OSError as exc:
            last = subprocess.CompletedProcess(
                args=argv, returncode=1, stdout="",
                stderr=f"git invocation failed: {exc}")
        batch, size = [], 0
        return getattr(last, "returncode", 1) == 0

    for spec in pathspecs:
        # BYTES, not characters: the kernel counts the encoded argv, so a tree of
        # 4-byte-UTF-8 paths would otherwise be measured at a quarter of its real
        # size and sail past the budget straight into ``E2BIG``.
        cost = len(spec.encode("utf-8", "surrogateescape")) + 1
        if batch and size + cost > _PATHSPEC_ARGV_BUDGET:
            if not _flush():
                return last
        batch.append(spec)
        size += cost
    _flush()
    return last


def _run_batched_stdout(
    run: Run, argv_prefix: Sequence[str], args: Sequence[str], *, cwd: str,
) -> Optional[str]:
    """The READ-ONLY twin of :func:`_run_batched`: run ``argv_prefix + <args>`` in
    the same ``ARG_MAX``-bounded batches and return the CONCATENATED stdout of every
    invocation, in argument order. Returns ``None`` the moment any invocation fails
    or exits non-zero, so a PARTIAL read is never mistaken for a complete one by a
    caller that is about to draw a conclusion from it."""
    out: List[str] = []
    batch: List[str] = []
    size = 0

    def _flush() -> bool:
        nonlocal batch, size
        if not batch:
            return True
        try:
            r = run([*argv_prefix, *batch], cwd=cwd)
        except OSError:
            return False
        if getattr(r, "returncode", 1) != 0:
            return False
        out.append(getattr(r, "stdout", "") or "")
        batch, size = [], 0
        return True

    for arg in args:
        cost = len(arg.encode("utf-8", "surrogateescape")) + 1
        if batch and size + cost > _PATHSPEC_ARGV_BUDGET:
            if not _flush():
                return None
        batch.append(arg)
        size += cost
    return "".join(out) if _flush() else None


def _ambiguous_runner_parents(
    entries: Sequence[Tuple[str, str]],
) -> Dict[str, str]:
    """``{immediate parent dir -> outermost ambiguous-dir prefix}`` for every NEW
    path in ``entries`` that is runner-classified ONLY by the ambiguous
    :data:`_RUNNER_DROPPING_PARENT_DIRS` rule — the candidates
    :func:`_tracked_source_dirs` has to ask HEAD about, and the ONLY paths a
    ``source_dirs`` answer can rescue.

    The prefix is the LOOKUP SCOPE, not the answer: one ``git ls-tree`` over
    ``build`` enumerates every committed file below it, so a tree with a thousand
    output subdirectories still costs one pathspec. NEW-only because a TRACKED file
    under a runner dir already reaches the commit via
    :func:`_stage_all`'s per-file recovery; a porcelain DIRECTORY entry (trailing
    ``/`` — an untracked nested repo, the one thing ``--untracked-files=all`` does
    not expand) is skipped so it can never be staged as a stray gitlink."""
    parents: Dict[str, str] = {}
    for xy, path in entries:
        raw = path or ""
        if raw.endswith("/") or not _new_to_head(xy):
            continue
        segs = raw.strip("/").split("/")
        if len(segs) < 2 or _is_hard_runner_dropping(segs):
            continue
        for i, seg in enumerate(segs[:-1]):
            if seg in _RUNNER_DROPPING_PARENT_DIRS:
                parents.setdefault("/".join(segs[:-1]), "/".join(segs[: i + 1]))
                break
    return parents


def _tracked_source_dirs(
    cwd: str, entries: Sequence[Tuple[str, str]], *, run: Run = _default_run,
) -> Set[str]:
    """Of the ambiguous runner-dir parents in ``entries``
    (:func:`_ambiguous_runner_parents`), those that DIRECTLY hold at least one file
    COMMITTED IN HEAD — the repo's own evidence that it keeps SOURCE in that exact
    directory, so a NEW file the fixer put there is an edit to commit, not runner
    output to withhold (see :func:`_is_runner_dropping`'s ``source_dirs``).

    DIRECTLY is the whole discriminator, and it is what keeps the escape hatch from
    swallowing real build output: a repo that commits ``build/scripts/deploy.sh``
    proves ``build/scripts`` is source, NOT ``build`` — so Gradle's fresh
    ``build/classes/Main.class`` (whose parent holds nothing committed) is still
    held back.

    ``ls-tree HEAD``, never ``ls-files``, is LOAD-BEARING: the INDEX is writable by
    the very actors this guard defends against, so a fixer's ``git add -N
    build/artifact.js`` (or a plain ``git add -A`` over a cold tree) would
    manufacture its own proof that ``build/`` is a source dir and walk the whole
    artifact subtree into the PR. HEAD is written only by a commit, so the evidence
    always predates the round.

    Best-effort by design: any git/OS failure — including the unborn HEAD of a
    repo with no commits — returns an empty set, which is exactly the pre-existing
    (hold-everything-back) behaviour."""
    parents = _ambiguous_runner_parents(entries)
    if not parents:
        return set()
    try:
        listing = _run_batched_stdout(
            run, ["git", "ls-tree", "-r", "--full-tree", "--name-only", "-z",
                  "HEAD", "--"],
            [f":(top,literal){d}" for d in sorted(set(parents.values()))], cwd=cwd)
    except (subprocess.SubprocessError, OSError, UnicodeDecodeError, ValueError):
        return set()
    if listing is None:
        return set()
    # ``rpartition`` (not a segment rebuild) so a committed FILE named ``build``
    # yields ``""`` — it is emphatically not evidence that a ``build/`` DIRECTORY
    # holds source; that shape is the runner-replaced-a-build-script case whose
    # whole subtree must keep being held back.
    holding = {p.rpartition("/")[0] for p in listing.split("\0") if p}
    # An EXACT-parent intersection, never an ancestor walk — deliberately, and it is
    # the reason a new file in a NEW subdirectory of a source ``build/``
    # (``build/sub/new.py`` beside a committed ``build/existing.py``) is still held
    # back. Recovering descendants would need only ONE committed file anywhere in
    # ``build/`` to declare the WHOLE subtree source, so every Gradle/CMake output
    # dir under it (``build/classes/…``, ``build/libs/…``) would ride into the
    # customer's PR — the exact leak this guard exists to stop, and a far worse
    # failure than withholding a file the operator is told about. Nothing
    # distinguishes a fixer's new ``build/sub/new.py`` from Gradle's fresh
    # ``build/classes/Main.class``: both are new files under a directory holding no
    # committed content, so the ambiguity resolves toward holding back.
    return set(parents) & holding


def _renamed_into_runner_sources(
    cwd: str, entries: Sequence[Tuple[str, str]], *, run: Run = _default_run,
    source_dirs: AbstractSet[str] = frozenset(),
) -> Set[str]:
    """Deleted, still-tracked source paths from ``entries`` whose exact content is
    sitting in a NEW runner-classified path (:func:`_is_runner_dropping`) the glob
    exclude is about to hold back — an UNSTAGED rename INTO a runner dir (``mv
    src/old.py build/new.py``).

    ``source_dirs`` (:func:`_tracked_source_dirs`) is threaded through to that
    predicate, so a move INSIDE the repo's own committed ``build/`` (``mv
    build/old.sh build/new.sh``) is not a candidate at all: that destination is
    recovered into the commit as ordinary source, making the move an ordinary
    rename rather than a pair to withhold and hand to a human.

    Git does NO rename detection here: the destination has no index entry, so
    porcelain reports a plain ``D <src>`` + ``?? <dest>`` pair, not the ``R``
    record :func:`_stage_all`'s staged-rename decomposition keys on. BOTH deletion
    columns count as that source half: a bare ``mv`` leaves the deletion unstaged
    (`` D``), while a fixer that followed it with ``git add -u`` (or ``git rm``)
    leaves the identical pair with the deletion already in the INDEX (``D ``) —
    same move, same loss, and reading only the worktree column would let the
    ``add -u`` shape through. The staged half's content lives in HEAD rather than
    the index (a staged deletion has no ``ls-files`` entry at all), so its blob is
    read with ``ls-tree HEAD``; the unstaged half keeps its ``ls-files`` read,
    whose index blob is the pre-deletion content. Left alone,
    the ``git add -A`` stages the source's DELETION while the exclude holds its
    replacement back, and :func:`_held_back_new_artifacts` then hides that
    destination from the clean-tree tripwire — so the pushed PR silently drops the
    file with nothing to show for it. ``_stage_all`` holds the source back too,
    giving the pair the SAME two-half protection a backup-then-replace move gets
    from :func:`_risky_delete_pairs`: both halves stay uncommitted residue, and the
    tripwire fires on the still-deleted source so a human resolves it.

    Paired by CONTENT IDENTITY — the destination's blob hash equals the source's
    INDEX blob hash — never by name: a move that also renames the file carries no
    name relationship to key on, so a name-based pair would be a guess, not a fact
    (cf. :func:`_backup_source`). An EMPTY deleted blob is never paired at all, for
    the same reason: every empty file in existence hashes to one sha, so matching it
    is not evidence of a move — a deleted ``__init__.py`` beside any 0-byte runner
    artifact would otherwise read as one. Cost is bounded: the probe runs at all only when
    the tree holds BOTH a worktree deletion and a new runner artifact, and only
    candidates whose SIZE already matches a deleted blob are hashed — a cold
    ``node_modules`` of thousands of files is stat'd, never read. Best-effort: any
    git/OS failure returns an empty set, leaving the guard exactly as it was.

    A SYMLINK destination (``mv src/link build/link``) is paired the same way but
    through a second, separate route, because ``git hash-object`` FOLLOWS a symlink
    — handed ``build/link`` it returns the hash of whatever the link POINTS AT, so
    routing symlinks through the regular probe would not merely miss the pair, it
    could invent one. Git stores a symlink as a blob holding the link TARGET, so
    the honest comparison is ``os.readlink`` against the deleted entry's own index
    blob (``git cat-file -p``), restricted to index entries whose MODE is
    ``120000``. That restriction makes the symlink route purely additive: it can
    only find pairs the regular route structurally cannot, never re-pair or unpair
    anything the regular route already decided. ``lstat``'s ``st_size`` for a
    symlink IS the target's byte length, so the same size gate bounds this route
    too."""
    # The two shapes the SAME move arrives in, split by where the deleted content
    # can still be read from. WORKTREE column ``D``: the deletion is unstaged, so
    # the path keeps an index entry holding its pre-deletion blob. INDEX column
    # ``D`` (a fixer's ``git add -u`` / ``git rm`` after the move): the index entry
    # is GONE, so ``ls-files`` returns nothing for it and only HEAD still has the
    # blob. An unmerged ``DD`` stays on the worktree route it has always taken.
    worktree_deleted = sorted({path for xy, path in entries
                               if len(xy) > 1 and xy[1] == "D" and not _new_to_head(xy)})
    staged_deleted = sorted({path for xy, path in entries
                             if len(xy) > 1 and xy[0] == "D" and xy[1] != "D"
                             and not _new_to_head(xy)})
    candidates = [path for xy, path in entries
                  if _is_runner_dropping(path, source_dirs=source_dirs)
                  and _new_to_head(xy)]
    if not (worktree_deleted or staged_deleted) or not candidates:
        return set()
    try:
        # Porcelain paths are repo-root-relative while ``cwd`` may be a subdirectory
        # (hence ``:/`` on the add), so the filesystem probes below resolve against
        # the top level, never against ``cwd``.
        top = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
        root = (getattr(top, "stdout", "") or "").strip()
        if getattr(top, "returncode", 1) != 0 or not root:
            return set()
        blobs: Dict[str, str] = {}          # deleted path -> its pre-deletion blob sha
        modes: Dict[str, str] = {}          # deleted path -> its recorded file mode
        if worktree_deleted:
            # ``<mode> <sha> <stage>\t<path>`` — the index still holds the blob.
            # ``--full-name``: like ``ls-tree`` below, ``ls-files`` prints paths
            # relative to ``cwd`` unless told otherwise, while ``worktree_deleted``
            # (and ``blobs``/``modes``, keyed off it) come from porcelain's
            # repo-root-relative paths.
            listing = _run_batched_stdout(
                run, ["git", "ls-files", "-s", "-z", "--full-name", "--"],
                [f":(top,literal){p}" for p in worktree_deleted], cwd=cwd)
            if listing is None:
                return set()
            for rec in listing.split("\0"):
                meta, sep, path = rec.partition("\t")
                fields = meta.split()
                if sep and path and len(fields) >= 2:
                    blobs[path] = fields[1]
                    modes[path] = fields[0]
        blob_sizes: Dict[str, int] = {}     # blob sha -> its byte length
        if staged_deleted:
            # ``<mode> <type> <sha> <size>\t<path>`` — a DIFFERENT field order from
            # ``ls-files`` above, and the only place the staged-away content is
            # still readable. ``blob`` only: a pathspec can never name a tree here
            # (every path came from a porcelain FILE record), and checking the type
            # keeps a tree's sha from being mistaken for file content if one ever
            # did. ``--full-tree`` for the same cwd-relative-output reason as the
            # ``ls-files`` call above. ``-l`` adds the SIZE field git already has on
            # hand, so this one call also answers the size lookup below for these
            # blobs — instead of one extra ``cat-file -s`` spawn per staged deletion.
            listing = _run_batched_stdout(
                run, ["git", "ls-tree", "-l", "--full-tree", "-z", "HEAD", "--"],
                [f":(top,literal){p}" for p in staged_deleted], cwd=cwd)
            if listing is None:
                return set()
            for rec in listing.split("\0"):
                meta, sep, path = rec.partition("\t")
                fields = meta.split()
                if sep and path and len(fields) >= 4 and fields[1] == "blob":
                    blobs[path] = fields[2]
                    modes[path] = fields[0]
                    try:
                        blob_sizes[fields[2]] = int(fields[3])
                    except ValueError:
                        pass
        # Only the WORKTREE-deletion blobs (from ``ls-files`` above, which carries no
        # size field) still need a spawn here — the STAGED-deletion blobs already got
        # their size from ``ls-tree -l`` above, at no extra process.
        for sha in sorted(set(blobs.values()) - set(blob_sizes)):
            r = run(["git", "cat-file", "-s", sha], cwd=cwd)
            if getattr(r, "returncode", 1) != 0:
                continue
            try:
                blob_sizes[sha] = int((getattr(r, "stdout", "") or "").strip())
            except ValueError:
                continue
        # A 0-byte blob is NO identity at all: EVERY empty file hashes to the same
        # sha (``e69de29…``), so admitting that size would let any empty untracked
        # artifact (cargo's 0-byte ``target/debug/.cargo-lock``, an empty file an
        # npm package ships) "match" an unrelated deleted empty source (a package's
        # ``__init__.py``, ``py.typed``, ``.gitkeep``) and hold that deletion out of
        # the fix commit. Content identity is a FACT only for a non-empty blob;
        # pairing on the degenerate one is exactly the guess this function refuses
        # to make, so the size gate never admits it.
        sizes: Set[int] = {n for n in blob_sizes.values() if n}
        if not sizes:
            return set()
        # The SIZE gate is what keeps this cheap: a size mismatch rules a candidate
        # out without reading a byte of it, so only the handful that could actually
        # BE the moved file is hashed. ``lstat`` (not ``stat``) so a symlink is
        # judged as itself rather than as whatever it points at — and so the two
        # kinds can be told apart and sent down the probe that fits each.
        probe: List[str] = []
        links: List[str] = []
        for path in candidates:
            try:
                info = os.lstat(os.path.join(root, path))
            except OSError:
                continue
            if info.st_size not in sizes:
                continue
            if stat.S_ISREG(info.st_mode):
                probe.append(os.path.join(root, path))
            elif stat.S_ISLNK(info.st_mode):
                links.append(os.path.join(root, path))
        if not probe and not links:
            return set()
        destinations: Set[str] = set()
        if probe:
            hashed = _run_batched_stdout(
                run, ["git", "hash-object", "--"], probe, cwd=cwd)
            if hashed is None:
                return set()
            destinations = set(hashed.split())
        # EXACT blob identity, and a rename that also EDITS the file (``mv
        # src/old.py build/new.py`` followed by a rewrite) is a KNOWN, ACCEPTED
        # miss rather than an oversight — do not "fix" it with a similarity /
        # rename-detection match. Every candidate here is a path inside a runner
        # OUTPUT dir, and runner output is DERIVED FROM the repo's own source: a
        # transpiled ``build/app.js``, a ``coverage/``/``htmlcov/`` report that
        # embeds each measured source file verbatim, a ``target/`` copy. "Closely
        # resembles a deleted source file" is therefore the NORM among candidates,
        # not evidence of a move, so a similarity floor would pair ORDINARY
        # deletions with ORDINARY build output — holding a real deletion out of the
        # fix commit and firing a manual-resolution ``stop`` on cold rounds that
        # merely deleted a file. That is the mirror-image loss, not a smaller one.
        # Git's own rename detection is unavailable for the same structural reason
        # the pair needs finding at all: the destination has NO index entry (hence
        # the ``D``+``??`` porcelain shape instead of an ``R`` record), and
        # manufacturing one with ``git add -N`` would both pollute the index this
        # guard exists to defend and defeat the stat-never-read cost bound
        # (``test_the_move_probe_never_hashes_a_size_mismatched_tree``) — a cold
        # ``node_modules`` would have to be READ, not stat'd, to be scored.
        # The STAGED form of the very same move already lands WHOLE and needs
        # nothing here: ``git mv src/old.py build/new.py`` + an edit arrives as a
        # single ``RM`` record, which :func:`_stage_all`'s tracked-runner per-file
        # recovery commits with both halves.
        matched = {src for src, sha in blobs.items() if sha in destinations}
        if links:
            matched |= _symlink_renamed_sources(
                links, blobs, modes, blob_sizes, cwd=cwd, run=run)
        return matched
    except (subprocess.SubprocessError, OSError, UnicodeDecodeError, ValueError):
        return set()


# A git index/tree entry mode of ``120000`` is a SYMLINK; its blob holds the link
# TARGET, not any file's contents.
_GIT_SYMLINK_MODE = "120000"


def _symlink_renamed_sources(
    links: Sequence[str], blobs: Dict[str, str], modes: Dict[str, str],
    blob_sizes: Dict[str, int], *, cwd: str, run: Run = _default_run,
) -> Set[str]:
    """The SYMLINK half of :func:`_renamed_into_runner_sources`: of its deleted
    tracked paths (``blobs``: path -> index blob sha, ``modes``: path -> index
    mode), those recorded as symlinks whose link TARGET is exactly what one of the
    size-matched new symlink destinations ``links`` (absolute paths) points at.

    Separate from the regular-file route because ``git hash-object`` dereferences a
    symlink — it would compare the wrong bytes entirely. Both sides are read as the
    link target instead: ``os.readlink`` on the destination, ``git cat-file -p`` on
    the source's index blob (a symlink blob is the target string, no trailing
    newline). Only ``120000`` sources are considered, so a deleted regular FILE
    whose contents happen to spell a path is never mistaken for a moved symlink.

    ``cat-file -p`` is reached only for a blob whose byte length equals some
    destination link's target length, keeping the "never read a big blob" bound;
    an unreadable or non-text blob is skipped, not fatal."""
    targets: Set[str] = set()
    for abs_path in links:
        try:
            targets.add(os.readlink(abs_path))
        except OSError:
            continue
    if not targets:
        return set()
    target_sizes = {len(t.encode("utf-8", "surrogateescape")) for t in targets}
    contents: Dict[str, Optional[str]] = {}
    matched: Set[str] = set()
    for src, sha in blobs.items():
        if (modes.get(src) != _GIT_SYMLINK_MODE
                or blob_sizes.get(sha) not in target_sizes):
            continue
        if sha not in contents:
            try:
                r = run(["git", "cat-file", "-p", sha], cwd=cwd)
                contents[sha] = ((getattr(r, "stdout", "") or "")
                                 if getattr(r, "returncode", 1) == 0 else None)
            except (subprocess.SubprocessError, OSError, UnicodeDecodeError):
                contents[sha] = None
        if contents[sha] in targets:
            matched.add(src)
    return matched


def _fmt_droppings(paths: Sequence[str], limit: int = 6) -> str:
    """A compact, bounded rendering of the excluded paths for the log line."""
    shown = ", ".join(paths[:limit])
    extra = len(paths) - limit
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def _stage_all(
    cwd: str, *, run: Run = _default_run,
    notice: Callable[..., str] = automation_notice,
) -> "subprocess.CompletedProcess[str]":
    """``git add -A`` for the round's commit, minus editor/backup droppings, any
    deleted source file paired with one (:func:`_risky_delete_pairs`) or moved into
    a runner dir by an unstaged rename (:func:`_renamed_into_runner_sources`), and
    — for a cold worktree — every UNTRACKED runner artifact (:func:`_is_runner_dropping`:
    ``node_modules/``, ``target/``, ``build/``, ``bin/Debug/``, coverage output,
    …). Runner artifacts are held back by a FIXED, count-INDEPENDENT glob pathspec
    set (:func:`_runner_exclude_pathspecs`), so a tree with thousands of untracked
    ``node_modules`` files never blows git's argv; a fixer's edit to a TRACKED file
    under such a dir is recovered per-file and still committed, as is a NEW file the
    fixer added beside committed source in an ambiguously-named ``build``/``target``/
    ``deps``/``coverage`` dir (:func:`_tracked_source_dirs` — the glob is
    source-blind, so those need the same per-file recovery).

    The FIXED runner glob set is applied UNCONDITIONALLY — the guard must stay
    fail-CLOSED for artifacts even when the porcelain scan came back empty because
    it FAILED (see the comment above the exclude assembly), and with nothing
    actually matching them the add is behaviourally a plain ``git add -A``. It
    stages the whole worktree (``:/`` — the top of the tree, so the
    scope matches a bare ``git add -A`` regardless of ``cwd``) with one
    ``:(top,exclude,literal)`` pathspec per excluded path — a repo-root-anchored
    LITERAL exact-path exclude (not a glob), aligning with the repo-root-relative
    porcelain paths, matching at any depth, and never mis-firing on a legitimate
    file — and emits an ``[auto]`` line naming what was kept out. Returns the ``git
    add`` result unchanged so the caller's return-code check is untouched."""
    entries = _status_entries(cwd, run=run)
    # A staged rename whose DESTINATION is a dropping — a fixer that did
    # ``git mv src.py src.py.bak`` (or ``mv src.py src.py.bak && git add -A``, which
    # Git records the same way) — arrives as a SINGLE ``R  src.py.bak`` porcelain
    # record, not the ``?? src.py.bak`` the dropping scan below keys on. Its staged
    # column is ``R`` (already-in-HEAD per :func:`_new_to_head`), so it would sail
    # straight past the scan and commit the ``.bak`` — the exact sweep this guard
    # exists to stop. Un-stage those renames FIRST: that decomposes each back into
    # its ``D src.py`` (the vanished real file) + ``?? src.py.bak`` (the new backup)
    # halves, which the dropping scan and :func:`_risky_delete_pairs` below then
    # handle exactly like a hand-rolled backup-then-replace — holding BOTH the backup
    # and its now-orphaned source out of the commit. Only rename destinations that
    # are themselves droppings are touched; a legitimate rename stays staged.
    rename_droppings = sorted({
        path for xy, path in entries
        if (xy[0] in "RC" or xy[1] in "RC") and _is_dropping(path)
    })
    if rename_droppings:
        reset_result = _run_batched(run, ["git", "reset", "-q", "--"],
                     [f":(top,literal){p}" for p in rename_droppings], cwd=cwd)
        if reset_result is not None and getattr(reset_result, "returncode", 1) != 0:
            return reset_result
        entries = _status_entries(cwd, run=run)
    droppings = [path for xy, path in entries if _is_dropping(path) and _new_to_head(xy)]
    risky = _risky_delete_pairs(entries)
    # Which ambiguously-named dirs (``build``/``target``/``deps``/``coverage``) this
    # repo keeps SOURCE in, asked of the index ONCE and threaded through every
    # runner classification below, so the staging decision, the notice, the move
    # probe and the tripwire all read the same answer.
    source_dirs = _tracked_source_dirs(cwd, entries, run=run)
    # An UNSTAGED rename INTO a runner dir (``mv src/old.py build/new.py``) arrives
    # as ``D src/old.py`` + ``?? build/new.py`` — no ``R`` record, so the staged-
    # rename decomposition above never sees it. The runner glob below holds the
    # DESTINATION back, so staging the source's deletion on its own would drop the
    # file from the PR outright. Hold the source back too (:func:`_renamed_into_
    # runner_sources`), the same two-half protection a backup-then-replace pair
    # gets. Paths already held back as a risky-delete pair are dropped here so the
    # operator gets ONE notice about them, not two.
    moved_into_runner = _renamed_into_runner_sources(
        cwd, entries, run=run, source_dirs=source_dirs) - risky
    excluded = droppings + sorted(risky | moved_into_runner)  # per-file holds

    # ── Runner droppings (cold-worktree hardening) ───────────────────────────────
    # A cold worktree's gate run drops dependency-install / build / cache /
    # coverage trees (``node_modules/``, ``target/``, ``build/``, ``bin/Debug/``,
    # …). They are held back by a FIXED dir/file GLOB set
    # (:func:`_runner_exclude_pathspecs`) applied to the SAME ``git add`` as the
    # editor excludes — count-INDEPENDENT, so a tree with thousands of untracked
    # ``node_modules`` files costs ONE pathspec, not thousands (which would blow
    # git's argv if enumerated per-file the way editor droppings are). Only
    # UNTRACKED runner artifacts are held back:
    #   • ``untracked_runner`` — new, held back (for the LOG only; membership is
    #     decided by the glob exclude, not this list, so its size never reaches an
    #     argv).
    #   • ``tracked_runner``   — a fixer's edit to a TRACKED file under a runner
    #     dir (a vendored tree, a source ``build/``) that is still UNSTAGED
    #     (``xy[1] != " "``); the glob excludes it, so it is RECOVERED per-file
    #     below. That list is bounded by the worktree's file count, so it is issued
    #     in ARG_MAX-bounded batches (:func:`_run_batched`) — a runner reinstalling
    #     a 20k-file VENDORED tree would otherwise blow git's argv. Two exclusions
    #     are load-bearing: a path already held back as a risky-delete pair (staging
    #     it would commit the very deletion the guard just told the operator it was
    #     withholding), and a change the fixer ALREADY staged (``xy[1] == " "``).
    #     The latter needs no recovery — ``git add`` never un-stages, so it survives
    #     the exclude untouched — and recovering it would be fatal: a staged
    #     DELETION (``D ``, a fixer's own ``git rm``) has no index entry and no
    #     worktree file, so its pathspec matches nothing and ``git add`` exits 128,
    #     turning the whole round into an ``error`` that never ships the fix.
    #   • ``prestaged_runner`` — NEW runner droppings already carrying an index
    #     entry (``xy != "??"``: a fixer's ``git add -A``, or an intent-to-add
    #     ``git add -N`` stub, whose ``  A`` staged column is a SPACE yet is still an
    #     index entry the recovery would happily fill in with full content). Each is
    #     un-staged BY ITS OWN LITERAL PATH so it cannot ride the commit — an
    #     exclude alone never un-stages. Deliberately NOT an include-glob reset: a
    #     glob also decomposes a staged RENAME touching a runner dir, and the
    #     per-file recovery below only ever sees porcelain's rename DESTINATION — the
    #     origin's deletion would silently never reach the commit, resurrecting the
    #     renamed-away file.
    #   • ``source_children`` — a NEW file the fixer added DIRECTLY BESIDE committed
    #     source in an ambiguously-named dir (``build/new_rule.py`` next to a
    #     committed ``build/existing.py``). The FIXED glob layer knows nothing of
    #     tracked-ness, so ``**/build/**`` holds it back exactly like real output;
    #     it is RECOVERED per-file below with a plain ``git add`` (``-u`` cannot —
    #     the path is untracked). Left out, the guard withheld the fixer's own new
    #     file, the tripwire hid it, and the round reported ``pushed`` with the fix
    #     missing. Editor droppings and both held-back pair halves are subtracted:
    #     a ``build/src.py.bak`` must stay out however source-y its directory is.
    runner_excludes = _runner_exclude_pathspecs()
    untracked_runner = [path for xy, path in entries
                        if _is_runner_dropping(path, source_dirs=source_dirs)
                        and _new_to_head(xy)]
    # NO ``source_dirs`` here, deliberately: this list is "what the GLOB held back
    # that must be put back", and the glob is source-blind. Narrowing it would
    # strand a tracked ``build/keep.txt`` edit — excluded by the glob, then never
    # recovered — which is the very loss this whole guard exists to prevent.
    tracked_runner = sorted({path for xy, path in entries
                             if _is_runner_dropping(path) and not _new_to_head(xy)
                             and xy[1] != " "}
                            - risky - moved_into_runner)
    prestaged_runner = sorted({
        path for xy, path in entries
        if _is_runner_dropping(path, source_dirs=source_dirs)
        and _new_to_head(xy) and xy != "??"})
    source_children = sorted(
        {path for xy, path in entries
         if _new_to_head(xy) and _is_runner_dropping(path)
         and not _is_runner_dropping(path, source_dirs=source_dirs)}
        - set(excluded))

    # There is deliberately NO "nothing to hold back → bare ``git add -A``"
    # short-circuit here. Every list above is derived from :func:`_status_entries`,
    # which is best-effort and returns ``[]`` on ANY read failure — a non-UTF-8
    # filename in a freshly-installed ``node_modules/`` (``-z`` emits path bytes
    # verbatim and ``_default_run`` decodes strictly), or a ``git status`` timeout
    # while enumerating thousands of untracked artifact files. "All four lists are
    # empty" therefore means EITHER "nothing to withhold" OR "the scan failed", and
    # the two are indistinguishable from here — so a bare ``git add -A`` on that
    # branch would sweep the whole cold ``node_modules/``/``target/`` tree into the
    # customer's PR precisely when the guard is needed most. Falling through keeps
    # it fail-CLOSED at zero cost: the runner glob set is FIXED, count-independent
    # and < 4 KB of argv (``test_runner_exclude_pathspec_set_is_fixed_and_small``),
    # ``resets`` is empty, ``excludes == runner_excludes``, and the per-file
    # ``source_children`` recovery below still runs — which a short-circuiting
    # ``return`` would skip, silently dropping the fixer's own new file in an
    # ambiguously-named source dir. On a genuinely clean repo the resulting
    # ``git add -A -- :/ <fixed globs>`` is behaviourally a plain ``git add -A``.
    #
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
    resets = [f":(top,literal){p}" for p in excluded + prestaged_runner]
    if resets:
        reset_result = _run_batched(run, ["git", "reset", "-q", "--"], resets, cwd=cwd)
        if reset_result is not None and getattr(reset_result, "returncode", 1) != 0:
            return reset_result
    # The runner glob excludes are ALWAYS applied from here on (even with no editor
    # excludes), so an untracked ``node_modules`` never rides in; when nothing
    # matches them the add is behaviourally a plain ``git add -A``.
    per_file_excludes = [f":(top,exclude,literal){p}" for p in excluded]
    excludes = per_file_excludes + runner_excludes
    # This ``add`` is the one argv that cannot be split — every exclude must be in
    # the SAME pathspec set, or a later invocation re-adds what an earlier one held
    # out. Its per-file half is bounded by the worktree's file count, so on a tree
    # with tens of thousands of editor droppings it approaches the OS ``ARG_MAX``.
    # When it would, fall back to an equivalent two-step: add with only the FIXED
    # runner globs, then un-stage the per-file paths in bounded batches. The end
    # state is identical — a reset returns each path to its HEAD state in the index,
    # exactly what excluding it from the add achieves — and both halves are then
    # argv-safe. Below the budget nothing changes, so the common path keeps its
    # single call.
    if sum(len(s.encode("utf-8", "surrogateescape")) + 1
           for s in excludes) > _PATHSPEC_ARGV_BUDGET:
        try:
            add = run(["git", "add", "-A", "--", ":/", *runner_excludes], cwd=cwd)
        except OSError as exc:
            return subprocess.CompletedProcess(
                args=["git", "add", "-A"], returncode=1, stdout="",
                stderr=f"git invocation failed: {exc}")
        if getattr(add, "returncode", 1) == 0 and excluded:
            undo = _run_batched(run, ["git", "reset", "-q", "--"],
                                [f":(top,literal){p}" for p in excluded], cwd=cwd)
            if undo is not None and getattr(undo, "returncode", 1) != 0:
                return undo
    else:
        try:
            add = run(["git", "add", "-A", "--", ":/", *excludes], cwd=cwd)
        except OSError as exc:
            return subprocess.CompletedProcess(
                args=["git", "add", "-A"], returncode=1, stdout="",
                stderr=f"git invocation failed: {exc}")
    # Recover a fixer's edit to a TRACKED file under a runner dir that the glob just
    # held out. ``-u`` (not ``-A``) is LOAD-BEARING: it stages a tracked path's
    # modification/deletion and NEVER adds an untracked file. With ``-A``, a tracked
    # entry the runner replaced with a directory — a ``build`` SCRIPT overwritten by a
    # ``build/`` output tree, a vendored ``node_modules`` SYMLINK replaced by a real
    # install — becomes a directory pathspec that recursively stages the whole
    # untracked artifact tree the exclude had just held back (proven to reach a
    # remote). ``-u`` records that path's deletion and nothing else.
    if getattr(add, "returncode", 1) == 0 and tracked_runner:
        recovered = _run_batched(
            run, ["git", "add", "-u", "--"],
            [f":(top,literal){p}" for p in tracked_runner], cwd=cwd)
        if recovered is not None:
            add = recovered
    # Recover the fixer's NEW files added beside committed source in an
    # ambiguously-named dir. A plain ``git add`` (no ``-u``, which only ever touches
    # tracked paths, and no ``-A``, whose directory recursion is what leaked an
    # artifact subtree before): every path here came from ``--untracked-files=all``
    # porcelain as an individual FILE, and a porcelain DIRECTORY entry is excluded
    # upstream in :func:`_is_runner_dropping`, so no pathspec here can name a tree.
    if getattr(add, "returncode", 1) == 0 and source_children:
        kept = _run_batched(
            run, ["git", "add", "--"],
            [f":(top,literal){p}" for p in source_children], cwd=cwd)
        if kept is not None:
            add = kept
    if getattr(add, "returncode", 1) == 0:
        if droppings:
            notice("stage",
                   f"excluded {len(droppings)} editor/backup dropping(s) from the fix "
                   f"commit: {_fmt_droppings(droppings)}",
                   status="skip")
        if untracked_runner:
            notice("stage",
                   f"excluded {len(untracked_runner)} runner artifact(s) "
                   f"(node_modules/target/build/coverage/…) from the fix commit: "
                   f"{_fmt_droppings(sorted(untracked_runner))}",
                   status="skip")
        if risky:
            sorted_risky = sorted(risky)
            notice("stage",
                   f"held back {len(risky)} deleted source file(s) paired with an "
                   f"excluded backup dropping — looks like an in-place rewrite that "
                   f"failed mid-way (moved to a backup, never recreated); resolve "
                   f"manually: {_fmt_droppings(sorted_risky)}",
                   status="stop")
        if moved_into_runner:
            sorted_moved = sorted(moved_into_runner)
            notice("stage",
                   f"held back {len(moved_into_runner)} deleted source file(s) whose "
                   f"content was moved INTO a runner artifact dir "
                   f"(node_modules/target/build/coverage/…) — that destination is NOT "
                   f"committed, so committing the deletion alone would drop the file "
                   f"from the PR; resolve manually: {_fmt_droppings(sorted_moved)}",
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
    # Is there anything to do at all? Read through the SAME ``-z
    # --untracked-files=all`` form and the SAME residue predicate
    # (:func:`_dirty_beyond_held_back`) as the clean-tree tripwire
    # (:func:`_assert_clean_after_commit`) and the rebase precondition
    # (:func:`exit_rebase`), so all three agree on what counts as an uncommitted
    # change. A bare ``.strip()`` over raw porcelain masked this only while
    # ``git add -A`` swept runner output into the commit: now that a cold worktree's
    # untracked ``node_modules/`` / ``target/`` / coverage tree is held back
    # PERMANENTLY (:func:`_held_back_new_artifacts`), porcelain reports it as
    # uncommitted on EVERY round of every JS/Rust/JVM/Python repo — so a genuinely
    # no-op round (a fixer that reported ``fixed`` but changed nothing on disk)
    # would fall through to the minutes-long test gate, and a suite that is red for
    # pre-existing reasons would escalate and let the operator's "stop" end a run
    # that changed nothing.
    try:
        status = run(["git", "status", "--porcelain", "-z", "--untracked-files=all"],
                     cwd=cwd)
    # ``UnicodeDecodeError`` (a ValueError, so NOT covered by the other two) is
    # reachable only because of the ``-z`` above: plain porcelain C-quotes a
    # non-UTF-8 path into pure ASCII, ``-z`` emits those bytes verbatim and
    # ``_default_run`` decodes strictly. Same tuple :func:`_status_entries` uses for
    # this exact command shape.
    except (subprocess.SubprocessError, UnicodeDecodeError, OSError):
        return "error"
    if status.returncode != 0:
        return "error"
    if not _dirty_beyond_held_back(status.stdout or "", cwd, run=run):
        # Nothing BEYOND the held-back set, but the held-back set itself may
        # include a fixer-authored file :func:`_tracked_source_dirs` could not
        # tell apart from real runner output (an ambiguous dir with no directly
        # committed sibling). ``_stage_all`` would notice() this same set on any
        # round it actually runs on; mirror that here so a round that never
        # reaches ``_stage_all`` still names what it left uncommitted.
        entries = list(_iter_porcelain_z(status.stdout or ""))
        source_dirs = _tracked_source_dirs(cwd, entries, run=run)
        held_back = _held_back_new_artifacts(entries, source_dirs=source_dirs)
        if held_back:
            notice("stage",
                   f"excluded {len(held_back)} artifact(s)/dropping(s) from this "
                   f"no-op round (nothing else changed): "
                   f"{_fmt_droppings(sorted(held_back))}",
                   status="skip")
        return "nothing"

    # Shift-left advisory: name a fixer-introduced syntax error in the round's
    # changed files in milliseconds, before the (possibly minutes-long) test gate.
    # ADVISORY ONLY — it never blocks the commit/push, and runs even when the gate
    # is disabled so the off-mode bypass can't defeat it.
    # It gets its OWN read, in the LINE-based porcelain form, rather than reusing
    # the ``status`` above: :func:`_changed_paths_from_porcelain` parses lines, which
    # the NUL-separated ``-z`` output has none of, and ``--untracked-files=all``
    # would hand the checker every individual file of a cold worktree's untracked
    # ``node_modules`` tree in place of the single collapsed ``node_modules/``
    # directory entry its isfile filter drops for free. Best-effort: any failure
    # yields an empty porcelain, i.e. no advisory — never a blocked commit.
    try:
        line_status = run(["git", "status", "--porcelain"], cwd=cwd)
        porcelain = (getattr(line_status, "stdout", "") or ""
                     if getattr(line_status, "returncode", 1) == 0 else "")
    except (subprocess.SubprocessError, UnicodeDecodeError, OSError):
        porcelain = ""
    _advisory_syntax_precheck(cwd, porcelain, notice=notice)

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
    #    poisoned/unverifiable state we never rebase or force-push. The ONE
    #    tolerated residue is what the staging guard deliberately withheld from the
    #    fix commit (:func:`_held_back_new_artifacts` — a cold worktree's untracked
    #    ``node_modules/`` / ``target/`` / coverage tree, an editor dropping). A
    #    cold-worktree round ENDS in exactly that state by design, so reading it as
    #    "dirty" would silently skip the base rebase on every such PR and strand it
    #    behind its base — while :func:`_assert_clean_after_commit`, reading the
    #    same worktree through the same predicate, calls it legitimate.
    #    See :func:`_dirty_beyond_held_back` for exactly what stays intolerable.
    #    ``-z --untracked-files=all`` matches :func:`_assert_clean_after_commit`
    #    byte for byte, so the two can never disagree about what is residue: plain
    #    porcelain C-quotes a non-ASCII name, which would read as unrecognised
    #    residue and skip a rebase that is in fact fine.
    try:
        st = run(["git", "status", "--porcelain", "-z", "--untracked-files=all"],
                 cwd=cwd)
    # ``UnicodeDecodeError`` (a ValueError, so NOT covered by the other two) is
    # reachable only because of the ``-z`` above: plain porcelain C-quotes a
    # non-UTF-8 path into pure ASCII, ``-z`` emits those bytes verbatim and
    # ``_default_run`` decodes strictly. Same tuple :func:`_status_entries` uses
    # for this exact command shape.
    except (subprocess.SubprocessError, UnicodeDecodeError, OSError) as exc:
        return "skipped", f"could not read the worktree state ({exc}) — not rebasing"
    if getattr(st, "returncode", 1) != 0:
        return "skipped", "could not read the worktree state — not rebasing"
    if _dirty_beyond_held_back(getattr(st, "stdout", "") or "", cwd, run=run):
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
    #     Read through the SAME filter as the step-2 precondition: the held-back
    #     runner artifacts we entered with are still sitting there afterwards, so
    #     counting them as dirty here would roll every cold-worktree rebase straight
    #     back and report ``error`` for a rebase that in fact succeeded.
    try:
        st2 = run(["git", "status", "--porcelain", "-z", "--untracked-files=all"],
                  cwd=cwd)
        dirty_after = (getattr(st2, "returncode", 1) != 0
                       or _dirty_beyond_held_back(
                           getattr(st2, "stdout", "") or "", cwd, run=run))
    # ``UnicodeDecodeError`` belongs here for the same ``-z`` reason as the step-2
    # read, and it matters MORE: this read runs AFTER a successful rebase, so an
    # escaping exception would skip the ``dirty_after`` → :func:`_restore_branch`
    # path entirely and leave the branch rebased locally but never force-pushed —
    # a silent local/remote divergence reported only as an "unexpected error".
    except (subprocess.SubprocessError, UnicodeDecodeError, OSError):
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
