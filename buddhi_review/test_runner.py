"""test_runner.py — runner detection + outcome classification for the polyglot
test gate (P2). The SOLE owner of the "what runner is behind the gate command, and
what did its exit mean?" question.

Two responsibilities, and deliberately NO triage layer (no failing-id extraction,
no scoped re-run — this free skill's gate escalates on red, nothing more):

  detect_runner(cwd, resolved_cmd) -> RunnerInfo
      Identify the runner behind the gate command, from (in priority order) the
      resolved command's argv[0]/shape, then repo markers. An `npm/yarn/pnpm/bun`
      package-script (`npm test`, `<mgr> run <name>`) is UNWRAPPED (P10) through
      `package.json` `scripts` to the real runner, so a standard `npm test` repo
      stays Tier-A. A `bash -lc "<cmd>"` wrapper is read back out of the shell STRING
      (`_runner_from_shell_string`): a string naming exactly ONE recognized runner —
      possibly behind a `cd`, an env prefix, or a preceding `&&` step — resolves to
      that runner (`source="shell"`, but NEVER scopable, since a shell string is not
      an argv we can append an exact-subset filter to); a string naming NO recognized
      runner (`make test`) or TWO-plus distinct ones (`pytest && npx jest`) stays an
      opaque wrapper. An unrecognized argv (`./run-tests.sh`, `make`, `nx`, `bazel`),
      a multi-tool package script (`tsc && jest`), or a multi-project run (`pnpm -r
      test`, `npm test -w pkg`) is likewise an opaque wrapper -> runner=UNKNOWN
      (Tier-B honest degrade); a `tox.ini [tox]` / `noxfile.py` forces tox/nox (a
      Tier-B wrapper) over the tox.ini->pytest signal. Pure / read-only (no network,
      no installs); the ONLY subprocess it runs is a bounded `cargo nextest
      --version` probe to tell nextest from stable cargo, which the design permits.

  classify(runner, exit_code, stdout, stderr, timed_out) -> str
      Map one runner invocation's outcome to exactly one of the six classes
      {passed, failed, no_tests, compile_error, env_error, timeout}. This is the
      layer that makes the gate CORRECT: a silent-exit-0 runner (jasmine / Karma /
      go / VSTest / gtest / swift / cargo) must NOT false-green on zero tests, and
      pytest's exit 5 must NOT false-red. `compile_error` / `env_error` are distinct
      from `failed` so the gate can name the class in its RED headline.

Why this is its own module: keeping it pure stdlib (no project imports) lets the
commit/push gate import it directly. The gate maps the returned class to its
(status, tail) contract.

The per-runner exit-code / marker facts below were cross-checked against each
runner's own docs/source; where a runner is silent-exit-0 on zero tests the marker
STRING is parsed, because the exit code alone would false-green.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple, Optional


# ── Outcome classes — the six-class contract P2 owns ─────────────────────────────
PASSED = "passed"
FAILED = "failed"
NO_TESTS = "no_tests"
COMPILE_ERROR = "compile_error"
ENV_ERROR = "env_error"
TIMEOUT = "timeout"

#: Every class `classify` can return. A gate-wiring test pins this so a stray new
#: class can never leak into the gate without a matching mapping.
OUTCOMES = frozenset({PASSED, FAILED, NO_TESTS, COMPILE_ERROR, ENV_ERROR, TIMEOUT})

#: The two RED classes that carry no per-test outcome at all: the run never got as
#: far as executing tests, so there is nothing test-level to report and the gate
#: names the class itself in its RED headline (`compile error` / `missing
#: dependency`). `failed`/`timeout` red too, but those DID reach the tests.
NON_TRIAGE_RED = frozenset({COMPILE_ERROR, ENV_ERROR})


# ── Runner identifiers ───────────────────────────────────────────────────────────
PYTEST = "pytest"
UNITTEST = "unittest"
DJANGO = "django"
TOX = "tox"
NOX = "nox"
JEST = "jest"
VITEST = "vitest"
MOCHA = "mocha"
JASMINE = "jasmine"
KARMA = "karma"
NODE_TEST = "node_test"
AVA = "ava"
BUN = "bun"
DENO = "deno"
GO = "go"
CARGO = "cargo"
NEXTEST = "nextest"
MAVEN = "maven"
GRADLE = "gradle"
MIX = "mix"
DOTNET = "dotnet"
RSPEC = "rspec"
MINITEST = "minitest"
PHPUNIT = "phpunit"
PEST = "pest"
CTEST = "ctest"
GTEST = "gtest"
CATCH2 = "catch2"
SWIFT = "swift"
DART = "dart"
FLUTTER = "flutter"
UNKNOWN = "unknown"

#: The complement of the runners that report per-test identities an exact-subset
#: re-run could address (a runner is scopable only when it was recognized from argv).
#: Nothing here consumes `scopable`; it is recorded as a property of the runner, and
#: the Tier-B wrappers / whole-suite-only runners are deliberately excluded from it.
_NON_SCOPABLE = frozenset({
    UNKNOWN, TOX, NOX, CARGO, JASMINE, KARMA, AVA, NODE_TEST, MINITEST,
    # Tier-C runtimes: detected and gated, but they report one whole-suite result,
    # so they are never advertised as scopable.
    BUN, DENO,
})


class RunnerInfo(NamedTuple):
    """The identified runner behind a gate command.

    runner       — one of the runner-id constants above, or UNKNOWN for an opaque
                   wrapper (`bash -lc`, `./run-tests.sh`, `make`) we cannot see
                   through.
    scopable     — best-effort hint: is this a recognized runner whose tests could
                   be addressed individually, rather than only as a whole suite?
                   Recorded by detection as a property of the runner; the gate here
                   runs the whole suite either way. UNKNOWN and the Tier-B wrappers
                   are never scopable.
    source       — how it was identified, for logging/tests. A runner FOUND is
                   tagged by where: `"argv"` (recognized at argv[0]), `"shell"` (read
                   out of a `bash -lc` string), `"npm-script"` (unwrapped through a
                   `package.json` script, P10), or `"marker:<file>"` (repo-marker
                   fallback). An opaque UNKNOWN carries WHY it stayed opaque:
                   `"wrapper:shell"`, `"wrapper:unrecognized"`,
                   `"wrapper:multi-project"`, or `"wrapper:npm-script"` (with
                   `-lifecycle` / `-shell` / `-expansion` suffixes for the specific
                   package-script degrade). `"none"` = no command AND no marker.
                   The set is OPEN — new detection paths add values, so match by
                   prefix (`source.startswith("wrapper:")`), never exact membership.
    resolved_cmd — the EXPANDED argv that actually invokes the runner, when it
                   differs from the argv this RunnerInfo was detected from — e.g.
                   an npm/yarn/pnpm/bun package-script unwrap (P10) exposes
                   `["cross-env", "SPECIAL=1", "pytest", "-c", "custom.ini"]` behind
                   a `["npm", "test"]` gate. None when the detected-from argv
                   already IS the invoking command (argv-detected, marker-detected),
                   which the caller then uses as-is. Anything that needs to re-derive
                   how the runner is actually invoked reads THIS rather than the raw
                   gate argv, so it inherits the script's own env / launcher / config
                   flags instead of a bare reconstruction that bypasses them.
    script_name  — for a package-script unwrap (P10), the name of the `scripts.<name>`
                   entry the runner was found behind — the TERMINAL one when the
                   script re-indirects (`"test": "npm run test:ci"` → `"test:ci"`),
                   since that is the one npm reports as `npm_lifecycle_event` to the
                   runner it finally spawns. None for every non-package-script
                   detection. Consumed by `npm_script_env`.
    package_manager — for a package-script unwrap (P10), the `_PKG_MANAGERS` entry
                   (`"npm"`/`"yarn"`/`"pnpm"`/`"bun"`) the gate command actually
                   invoked — the HEAD-of-chain one for a re-indirection, the
                   OPPOSITE end from `script_name`, because the head manager's
                   process-tree-wide variables (INIT_CWD) survive an inner
                   re-indirect while the per-child lifecycle variables are re-set
                   by each manager. None for every non-package-script detection.
                   Consumed by `npm_script_env`, since not every manager provides
                   the same npm-compatibility environment variables (bun does not
                   set `INIT_CWD`).
    """
    runner: str
    scopable: bool
    source: str
    resolved_cmd: Optional[list[str]] = None
    script_name: Optional[str] = None
    package_manager: Optional[str] = None


def _mk(runner: str, source: str, resolved_cmd: Optional[list[str]] = None,
        script_name: Optional[str] = None,
        package_manager: Optional[str] = None) -> RunnerInfo:
    return RunnerInfo(runner=runner,
                      scopable=(runner not in _NON_SCOPABLE),
                      source=source,
                      resolved_cmd=resolved_cmd,
                      package_manager=package_manager,
                      script_name=script_name)


# ── Detection: argv shape ────────────────────────────────────────────────────────

#: Launcher prefixes that wrap a real runner (`npx vitest`, `bundle exec rspec`,
#: `poetry run pytest`). Stripped so the token AFTER them is examined. `npm`/`yarn`/
#: `pnpm` are deliberately NOT here: `npm test` runs a package SCRIPT (unwrapping
#: it is P10), so it stays opaque; only `npx` (direct-binary) is a launcher.
_LAUNCHERS = ("npx", "bunx", "pnpx")
_LAUNCHER_PAIRS = (("bundle", "exec"), ("poetry", "run"), ("uv", "run"),
                   ("pipenv", "run"), ("rye", "run"), ("pdm", "run"))

#: npm's documented `-y`/`--yes` flag suppresses npx's install-confirmation prompt
#: (`npx -y <pkg>` / `npx --yes <pkg>`) — it precedes the runner token, not the
#: runner itself, so it must be dropped alongside the launcher.
_NPX_NONINTERACTIVE_FLAGS = ("-y", "--yes")

#: Env-setting launchers that wrap a real runner (`cross-env NODE_ENV=test jest`,
#: `env FOO=1 pytest`). Stripped — along with any leading `VAR=value` shell
#: assignments — so the underlying runner token is exposed. `cross-env-shell` /
#: `dotenv` / `env-cmd` take a shell string or config flags of their own and are
#: deliberately NOT here (they stay opaque → Tier-B).
_ENV_LAUNCHERS = ("env", "cross-env")


def _basename_token(tok: str) -> str:
    """The trailing path component of an argv token, minus a trailing `.exe`,
    lowercased — so `./vendor/bin/phpunit`, `node_modules/.bin/jest.CMD` and
    `/usr/bin/pytest` all reduce to the runner name."""
    base = os.path.basename((tok or "").strip().strip('"').strip("'").replace("\\", "/"))
    if base.lower().endswith(".exe"):
        base = base[:-4]
    if base.lower().endswith(".cmd"):
        base = base[:-4]
    return base.lower()


def _strip_launchers(argv: list) -> list:
    """Drop a leading launcher prefix (`npx`, `bundle exec`, `poetry run`, …) so the
    real runner token comes first. Returns a possibly-shorter copy; never mutates."""
    toks = list(argv)
    changed = True
    while changed and toks:
        changed = False
        head = _basename_token(toks[0])
        if head in _LAUNCHERS:
            toks = toks[1:]
            changed = True
            while toks and _basename_token(toks[0]) in _NPX_NONINTERACTIVE_FLAGS:
                toks = toks[1:]
            continue
        if len(toks) >= 2:
            for a, b in _LAUNCHER_PAIRS:
                if head == a and _basename_token(toks[1]) == b:
                    toks = toks[2:]
                    changed = True
                    break
    return toks


#: A bare shell `VAR=value` assignment token (`PYTHONPATH=.`, `NODE_ENV=test`) — NOT
#: itself an executable, unlike `env`/`cross-env`. Shared with `_py_launcher_prefix`,
#: which must recognize the SAME shape to know when a reconstructed launcher needs an
#: `env` executable prepended before it is safe to `subprocess.run` without a shell.
_BARE_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _strip_env_prefix(toks: list) -> list:
    """Drop a leading env-setting prefix — an `env`/`cross-env` launcher and/or
    consecutive `VAR=value` shell assignments — so `cross-env NODE_ENV=test jest` and
    `NODE_ENV=1 pytest` expose the real runner token. Returns a copy; never mutates."""
    out = list(toks)
    changed = True
    while changed and out:
        changed = False
        first = str(out[0])
        if _basename_token(first) in _ENV_LAUNCHERS:
            out = out[1:]
            changed = True
            continue
        if _BARE_ENV_ASSIGNMENT_RE.match(first):
            out = out[1:]
            changed = True
    return out


#: Python interpreter options that CONSUME the following token as their argument
#: (`-X dev`, `-W ignore`) — case-sensitive since `-x` (skip the `#!` line) is a
#: distinct, argument-less flag from `-X` (set an implementation option).
_PY_OPTS_WITH_ARG = ("-X", "-W")


def _skip_python_interpreter_opts(toks: list) -> int:
    """Index of the first token after `python` that is not a leading interpreter
    option (`-I`, `-X dev`, `-O`, …), so `-m <module>` / `manage.py` is found even
    behind `python -I -m pytest` / `python -X dev manage.py test`."""
    i = 1
    while i < len(toks):
        tok = str(toks[i])
        if not tok.startswith("-") or tok == "-m":
            break
        i += 1
        if tok in _PY_OPTS_WITH_ARG:
            i += 1
    return i


def _strip_launchers_and_env(toks: list) -> list:
    """Drop BOTH a launcher prefix (`npx`, `bundle exec`, …) and an env prefix
    (`cross-env`, `FOO=1`, …), interleaved in either order (`cross-env uv run
    pytest`), until neither strips anything further. Returns a possibly-shorter
    copy; never mutates. The shared core of `_runner_from_argv` (detection) and
    `_py_launcher_prefix` (rerun reconstruction) — both must agree on exactly how
    many leading tokens are launcher/env noise, or a scoped rerun ends up invoking
    the env-setter itself (`cross-env <test-id> …`) instead of the real runner."""
    out = list(toks)
    changed = True
    while changed:
        changed = False
        stripped_launchers = _strip_launchers(out)
        if len(stripped_launchers) < len(out):
            out = stripped_launchers
            changed = True
        stripped_env = _strip_env_prefix(out)
        if len(stripped_env) < len(out):
            out = stripped_env
            changed = True
    return out


def _runner_from_argv(argv: list) -> Optional[str]:
    """Identify a runner from a bare (non-shell-wrapped) argv, or None when argv[0]
    is not a recognized runner. Handles the `python -m <mod>` form, multi-token
    shapes (`cargo nextest run`, `go test`, `ng test`), launcher prefixes, and a
    leading env prefix (`cross-env … jest`)."""
    if not argv:
        return None
    toks = _strip_launchers_and_env(argv)
    if not toks:
        return None
    head = _basename_token(toks[0])

    # `python -m <module>` / `python manage.py test` — skip leading interpreter
    # options first (`python -I -m pytest`, `python -X dev manage.py test`) so `-m`
    # /`manage.py` is recognized regardless of how many precede it.
    if head in ("python", "python2", "python3", "py"):
        idx = _skip_python_interpreter_opts(toks)

        # `python -m <module>` — pytest / unittest / nox / tox / … run as a module.
        if idx < len(toks) - 1 and _basename_token(toks[idx]) == "-m":
            mod = _basename_token(toks[idx + 1])
            return {
                "pytest": PYTEST, "unittest": UNITTEST, "nox": NOX, "tox": TOX,
                "nose2": UNITTEST, "mypy": None,  # nose2 is unittest-based
            }.get(mod)

        # `python manage.py test` — Django's unittest runner.
        if idx < len(toks) and _basename_token(toks[idx]) == "manage.py":
            return DJANGO if "test" in [str(t).lower() for t in toks[idx + 1:]] else None

    # `./manage.py test` — Django's unittest runner, no interpreter prefix.
    if head == "manage.py":
        return DJANGO if "test" in [str(t).lower() for t in toks[1:]] else None

    # Cargo: `cargo nextest run …` (Tier-A) vs `cargo test …` (Tier-B).
    if head == "cargo" and len(toks) >= 2:
        sub = _basename_token(toks[1])
        if sub == "nextest":
            return NEXTEST
        if sub == "test":
            return CARGO
        return None

    # `go test …`
    if head == "go":
        return GO if len(toks) >= 2 and _basename_token(toks[1]) == "test" else None

    # `dotnet test …`
    if head == "dotnet":
        return DOTNET if len(toks) >= 2 and _basename_token(toks[1]) == "test" else None

    # `mix test …`
    if head == "mix":
        return MIX if "test" in [str(t).lower() for t in toks[1:]] else None

    # `swift test …`
    if head == "swift":
        return SWIFT if len(toks) >= 2 and _basename_token(toks[1]) == "test" else None

    # `dart test …` / `flutter test …`
    if head == "dart":
        return DART if "test" in [str(t).lower() for t in toks[1:]] else None
    if head == "flutter":
        return FLUTTER if "test" in [str(t).lower() for t in toks[1:]] else None

    # `ng test …` (Angular → Karma)
    if head == "ng":
        return KARMA if "test" in [str(t).lower() for t in toks[1:]] else None

    # `node --test …`  (node:test built-in runner)
    if head in ("node", "nodejs"):
        return NODE_TEST if any(str(t) in ("--test", "--test-only") for t in toks[1:]) else None

    # `bun test …` — Bun's built-in test runner (Tier-C). `bun run <script>` is a
    # package-script wrap resolved by the unwrap layer, not here; `bun test` is the
    # runner itself.
    if head == "bun":
        return BUN if len(toks) >= 2 and _basename_token(toks[1]) == "test" else None

    # `deno test …` — Deno's built-in test runner (Tier-C). `deno task <name>` runs a
    # deno.json task (opaque) and is deliberately NOT matched here.
    if head == "deno":
        return DENO if len(toks) >= 2 and _basename_token(toks[1]) == "test" else None

    # Maven / Gradle wrappers.
    if head in ("mvn", "mvnw"):
        return MAVEN
    if head in ("gradle", "gradlew"):
        return GRADLE

    # Direct-binary runners (basename match).
    direct = {
        "pytest": PYTEST, "py.test": PYTEST,
        "vitest": VITEST, "jest": JEST, "mocha": MOCHA, "jasmine": JASMINE,
        "karma": KARMA, "ava": AVA,
        "tox": TOX, "nox": NOX,
        "rspec": RSPEC, "phpunit": PHPUNIT, "pest": PEST,
        "ctest": CTEST, "nextest": NEXTEST,
    }
    if head in direct:
        return direct[head]

    return None


def _is_shell_wrapper(argv: list) -> bool:
    """True when argv is a `bash -lc "<cmd>"` / `sh -c "<cmd>"` form (P1's
    `_command_needs_shell` commands are executed this way): the runner is not argv[0]
    — it is hidden inside the shell STRING (`_shell_string` reads it back out)."""
    if len(argv) >= 2:
        head = _basename_token(argv[0])
        if head in ("bash", "sh", "zsh", "dash", "ksh") and any(
                str(a) in ("-c", "-lc", "-lic", "-ic") for a in argv[1:2]):
            return True
    return False


def _shell_string(argv: list) -> str:
    """The COMMAND STRING a shell wrapper carries: `bash -lc "<cmd>"` → `<cmd>`.
    `_is_shell_wrapper` pins the `-c` flag at argv[1], so the string is argv[2]."""
    return str(argv[2]) if len(argv) >= 3 else ""


#: shlex's `punctuation_chars` set — the shell control operators it tokenizes apart
#: from words (`&&` `||` `;` `|` `(` `)` `<` `>` and combinations like `>&`).
_SHELL_PUNCT = frozenset("();<>|&")


def _shell_segments(cmd: str) -> list:
    """A wrapped shell string split into the individual COMMANDS it runs, each as an
    argv-style token list: `cd sub && npx jest` → `[["cd","sub"], ["npx","jest"]]`.

    Tokenized with `shlex` in punctuation mode, which is quote-AWARE: a quoted
    operator (`go test -run 'A|B'` — the over-match `_command_needs_shell` documents,
    which needs no shell at all) keeps `A|B` as ONE token instead of splitting the
    command in half, while an unspaced real operator (`pytest|tee log`) still splits.
    An unbalanced quote raises `ValueError` → no segments → the caller stays opaque."""
    lex = shlex.shlex(cmd or "", posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        toks = list(lex)
    except ValueError:                  # unbalanced quote — untokenizable; stay opaque
        return []
    segments, cur = [], []
    for tok in toks:
        if tok and all(ch in _SHELL_PUNCT for ch in tok):   # an operator ends a command
            if cur:
                segments.append(cur)
                cur = []
            continue
        cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


def _runner_from_shell_string(cmd: str) -> Optional[str]:
    """Best-effort: the runner behind a `bash -lc "<cmd>"` wrapper, read out of the
    shell STRING. None when the string names no runner we recognize, or names more
    than one.

    This exists because P1's `_command_needs_shell` wraps a LOT of ordinary gate
    commands into `bash -lc` — everything carrying a shell operator, a `cd <dir>` step
    or a `VAR=val` prefix (`cd frontend && npx jasmine`, `CI=1 go test ./...`,
    `npm ci && npx jest`). Reading argv[0] alone calls all of those UNKNOWN, which
    hands them to `_classify_generic` (rc==0 → passed) and hence false-GREENS a
    zero-test run of a silent-exit-0 runner — the exact guarantee this module exists
    to keep. Each command in the string is checked, so the runner is found behind a
    `cd`, an env prefix, or a preceding `npm ci` step.

    Two or more DISTINCT runners (`pytest && npx jest`) → None: the wrapper's exit
    code and output are then a MIX of two runners' conventions, which no single
    per-runner classifier can read soundly, so it stays an opaque wrapper (Tier-B),
    exactly as before. `_classify_generic` still holds the no-false-green line there."""
    found = []
    for seg in _shell_segments(cmd):
        r = _runner_from_argv(_strip_env_prefix(seg))
        if r is not None and r not in found:
            found.append(r)
    return found[0] if len(found) == 1 else None


# ── Detection: npm / yarn / pnpm / bun package-script unwrap (P10) ────────────────
#
# `npm test` / `yarn test` / `pnpm test` (and `<mgr> run test`) run a package.json
# SCRIPT, not a runner binary — so from argv alone they are opaque. This layer reads
# `package.json` `scripts.<name>` and resolves THROUGH it to the real runner, keeping
# a standard `npm test` repo Tier-A. Only an unrecognized single tool, a multi-tool
# chain (`jest && eslint`), a shell expansion the unwrap cannot reproduce (`pytest
# -c $CONF`), or a multi-project/workspace run (`pnpm -r test`, `npm test -w pkg`)
# degrades to a Tier-B UNKNOWN wrapper.

_PKG_MANAGERS = ("npm", "yarn", "pnpm", "bun")
_TEST_ALIASES = ("test", "t", "tst")
_RUN_SUBCMDS = ("run", "run-script")

#: Workspace / recursive flags that make a package-script run span multiple packages
#: or cwds — opaque, never unwrapped (honest whole-suite degrade). Scoped PER
#: MANAGER: a flag shape real for one manager is not necessarily a flag at all for
#: another — pnpm's `-r`/`--recursive` is not a yarn flag (`yarn run -h=1` lists
#: `-T`/`--top-level`, not `-r`), so a bare `-r` in a yarn invocation is a token
#: yarn FORWARDS verbatim to the underlying script (`yarn test -r setup.js` forwards
#: Mocha's `-r`/`--require`, confirmed via mocha's own `-r, --require <module>`
#: option) — treating it as a universal workspace flag would misclassify a plain
#: single-runner script as an opaque multi-project wrapper and lose that runner's
#: classifier. npm's `-w`/`--workspace`/`--workspaces`/`--ws`/`--prefix` (npm
#: 11.4.2, confirmed via `npm help workspaces` + `npm help run-script`). Yarn
#: Berry's `-T`/`--top-level` (confirmed via `yarn run -h=1`: "Check the root
#: workspace for scripts and/or binaries instead of the current one") and `--cwd`
#: (yarn 1.22.22) — each changes WHICH package.json is read, so resolving
#: `scripts.<name>` from `cwd` would bind to the wrong script. pnpm's
#: `-r`/`--recursive`/`--filter`/`-C`/`--dir`/`-w`/`--workspace-root` (pnpm
#: 9.15.9). bun's `--cwd` and `--filter`/`-F` (`bun run --filter <pat> <script>` /
#: `bun run -F=<pat> <script>` / `bun -F<pat> run <script>` — confirmed against
#: real Bun 1.3.12: `bun run --help` lists `-F, --filter=<val>`, and all three
#: joined/space forms filter-ran the matching workspace packages, not just cwd's —
#: run the script in every MATCHING workspace package — Bun's "Filter" docs — so a
#: root-script unwrap would classify the whole run by ONE package's runner and let
#: a scoped re-run silently skip the others' suites). Each `--cwd`/`--dir`/
#: `--prefix` reads `<dir>/package.json` instead of the gate's `cwd`, confirmed
#: empirically — resolving `scripts.<name>` from `cwd` would then bind to the
#: WRONG package's script.
_NPM_WORKSPACE_FLAGS = frozenset({"-w", "--workspace", "--workspaces", "--ws", "--prefix"})
_YARN_WORKSPACE_FLAGS = frozenset({"-T", "--top-level", "--cwd"})
_PNPM_WORKSPACE_FLAGS = frozenset({
    "-r", "--recursive", "--filter", "-C", "--dir", "-w", "--workspace-root",
})
_BUN_WORKSPACE_FLAGS = frozenset({"--cwd", "--filter", "-F"})
_MANAGER_WORKSPACE_FLAGS = {
    "npm": _NPM_WORKSPACE_FLAGS,
    "yarn": _YARN_WORKSPACE_FLAGS,
    "pnpm": _PNPM_WORKSPACE_FLAGS,
    "bun": _BUN_WORKSPACE_FLAGS,
}

#: A token that is ENTIRELY a shell control operator (`&&`, `||`, `|`, `;`, `&`,
#: `(`/`)`, `<`/`>`) — emitted as its own token by shlex with `punctuation_chars`.
#: A shell metacharacter INSIDE a quoted argument (`--testPathPattern='(a|b)'`) is
#: part of a normal word token and never fullmatches this.
_OPERATOR_TOKEN_RE = re.compile(r"[|&;()<>]+")


def _script_chains_tools(script: str) -> bool:
    """True when a `scripts.<name>` value composes MULTIPLE tools or needs a subshell
    — chaining (`&&`/`||`/`|`/`;`), a background/`&`, a subshell `(…)`/`$(…)`, a
    redirection, or a backtick command — so the captured output is NOT one recognized
    runner's and a per-runner classifier + scoped triage would be unsafe → Tier-B.

    A shell metacharacter INSIDE a quoted argument (`jest --testPathPattern='(a|b)'`,
    `jest -t 'x|y'`) is NOT composition: shlex honours the quotes, so the `|`/`(`/`)`
    stay inside one word token and are not flagged. A plain `VAR=val runner` env
    prefix is likewise not composition (it is stripped by `_strip_env_prefix`)."""
    # A newline (or CR) in a package.json script value is a shell command separator
    # (`"test": "jest\neslint"` runs both) — shlex treats it as whitespace, so guard
    # it explicitly before tokenizing.
    if "\n" in script or "\r" in script:
        return True
    try:
        lex = shlex.shlex(script, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return True                       # unbalanced quotes → opaque, needs a shell
    if any(_OPERATOR_TOKEN_RE.fullmatch(t) for t in tokens):
        return True
    # Backtick and `$(…)` command substitution run a subcommand and need a shell —
    # raw-scan for BOTH. The punctuation-token split above catches only an UNQUOTED
    # `(`/`)`; a double-quoted `$(…)` would otherwise slip through, since POSIX double
    # quotes do NOT suppress command substitution. Deliberately conservative: a
    # single-quoted LITERAL `$(`/backtick is over-flagged to Tier-B — the safe
    # (whole-suite degrade) side. A bare `$VAR` expansion is NOT flagged here: it
    # parameterizes the SAME runner (like a stripped `VAR=val` env prefix), it does
    # not run a second command — `_script_needs_shell_expansion` degrades it, for
    # the unrelated reason that `shlex.split` cannot expand it.
    return "`" in script or "$(" in script


#: Where a `$` actually INTRODUCES a shell expansion: a name (`$JOBS`), a brace
#: (`${JOBS}`), a subshell (`$(nproc)`), or a special/positional parameter (`$1`,
#: `$@`, `$*`, `$#`, `$?`, `$$`, `$!`, `$-`). A `$` followed by anything else — a
#: quote, whitespace, a regex metacharacter — is literal to the shell too, so the
#: common trailing-anchor arg (`jest -t 'renders$'`,
#: `jest --testPathPattern='\.test\.js$'`) keeps its Tier-A unwrap.
_SHELL_EXPANSION_RE = re.compile(r"\$[A-Za-z_{(0-9@*#?!$-]")


def _script_needs_shell_expansion(script: str) -> bool:
    """True when a `scripts.<name>` value carries a shell EXPANSION (`$VAR`,
    `${VAR}`, `$(cmd)`, `$1`/`$@`/…) → Tier-B.

    The package manager runs a script through a SHELL, which expands these before
    the runner ever starts. `_resolve_package_script` reads the script with
    `shlex.split`, which does NOT expand, so each such token survives LITERAL into
    `resolved_cmd` — and the scoped re-run executes that argv with no shell
    (`subprocess.run(list(cmd))`, `_run_local_pytest_scoped`). The mismatch is
    silent rather than loud:

      `"test": "MODE=$CI_MODE pytest"` — the gate runs under `MODE=strict`; the
          re-run sets `MODE` to the literal `$CI_MODE`, so a conftest branching on
          it takes a DIFFERENT branch and still exits 0.
      `"test": "pytest $EXTRA_FLAGS"` — `$EXTRA_FLAGS=--runxfail` under the gate;
          the literal `$EXTRA_FLAGS` matches no flag `_pytest_behavior_flag_args`
          knows, so the re-run DROPS it and runs under different xfail semantics —
          the exact shape `_PYTEST_BEHAVIOR_BOOL_FLAGS` exists to preserve.

    Either way "passes in isolation" would answer about a different environment or
    config than the failing gate, and that answer authorizes the pollution-widening
    in `_failing_tests_pass_in_isolation`. Stay opaque (honest whole-suite degrade)
    rather than expand it here — reproducing the package manager's expansion needs
    that same script-shell, which would also execute any `$(…)` inside it.

    Only `$` expansions are flagged. A tilde (`pytest -c ~/ci.ini`) or an unquoted
    glob (`pytest tests/*.py`) survives literal too, but neither produces this
    silent wrong-but-valid run: a glob sits in the target-path slot the scoped
    re-run replaces with ids anyway, and an unexpanded `~` names a path that does
    not exist, so the re-run fails LOUDLY and stays conservative (no widening).

    Raw-scanned, so a single-quoted LITERAL `'$VAR'` — which the shell would not
    expand either — is over-flagged to Tier-B: the safe side, the same deliberate
    trade-off as `_script_chains_tools`'s `$(`/backtick scan."""
    return bool(_SHELL_EXPANSION_RE.search(script or ""))


def _is_workspace_flag(tok: str, head: str) -> bool:
    """Whether `tok` is a workspace/recursive flag belonging to package manager
    `head` specifically — see `_MANAGER_WORKSPACE_FLAGS` for the per-manager
    citations. A flag shape that only exists for a DIFFERENT manager is not
    matched: it is either meaningless for `head` or, for yarn/pnpm/bun, a token
    `head` forwards verbatim to the underlying script."""
    t = str(tok)
    if t in _MANAGER_WORKSPACE_FLAGS.get(head, frozenset()):
        return True
    if head == "npm":
        return (t.startswith("--workspace=") or t.startswith("--workspace-")
                or t.startswith("--prefix="))
    if head == "yarn":
        return t.startswith("--cwd=")
    if head == "pnpm":
        return (t.startswith("--filter") or t.startswith("--dir=")
                or t.startswith("--cwd="))
    if head == "bun":
        return (t.startswith("--cwd=") or t.startswith("--filter=")
                or t.startswith("-F"))
    return False


#: Per-manager flags that consume a following value TOKEN (pnpm's `--filter <pattern>`,
#: `-C <dir>`, `--dir <dir>`, `--loglevel <level>`, `--resume-from <package>`,
#: `--changed-files-ignore-pattern <pattern>`, `--test-pattern <pattern>`; bun's
#: `--filter <pattern>`, `-F <pattern>`, `--cwd <dir>`, `--shell <bun|system>`,
#: `--env-file <path>`, `--elide-lines <n>`, `-r`/`--preload <module>`, `--require
#: <module>`, `--import <module>`, `--install <mode>`, `-d`/`--define <k:v>`) rather
#: than an `=`-joined or concatenated form — the pair is skipped together so the
#: value doesn't prematurely end `_run_flag_prefix_len`'s scan (nor get mistaken for
#: the script positional). Confirmed on pnpm 10.33.0 / Bun 1.3.12: each accepts the
#: SPACE-separated form and consumes the next token as its MANDATORY value, so a
#: value that happens to sit before a later `--filter` (`pnpm run --loglevel warn
#: --filter=a test`, `pnpm run --changed-files-ignore-pattern '**/README.md'
#: --filter=a test`, `bun run --install fallback --filter=a test`) no longer ends the
#: scan early and misroutes a filtered workspace run to a single scopable script
#: (#540; `--loglevel`/`--resume-from` (pnpm) and `--install`/`-d`/`--define` (bun)
#: added P10b round-3, 2026-07-20; `--changed-files-ignore-pattern`/`--test-pattern`
#: (pnpm) added P10b round-4 — `pnpm run --help` lists both under Filtering options
#: as `<pattern>`-taking flags distinct from `--filter`, and `bun run --help`
#: documents `--install=<val>` / `-d, --define=<val>` as MANDATORY-value flags
#: (unlike the optional-value flags below), all confirmed to swallow the following
#: token the same way as the flags already listed above); `--conditions`/`--port`/
#: `--drop` (bun) added P10b round-5, 2026-07-21 — bun's own arg-parser table
#: (`src/runtime/cli/Arguments.rs`) declares each as `<STR>` (or `<STR>...` for
#: repeatable `--conditions`/`--drop`) with NO trailing `?`, the same mandatory-value
#: shape as `--define`/`--install` above, so `bun run --conditions test --filter='*'
#: test` was consuming `test` as `--conditions`'s value then still ending the scan on
#: it as a phantom script positional, letting the real `--filter` after it slip past
#: unrecognized. `--title` (bun) added P10b round-6, 2026-07-21 — confirmed on Bun
#: 1.3.12 (`bun run --help`) and empirically (`bun run --title ci ci` sets the
#: process title to `ci` and still runs the `ci` script, i.e. the value is consumed
#: as a MANDATORY space-separated token, not `=`-joined only) that `bun run --title
#: ci --filter=a test` runs the `test` script scoped to workspace `a`; before this
#: `--title` wasn't in `_BUN_VALUE_FLAGS`, so the scan stopped at `ci` as a phantom
#: script positional, letting the real `--filter=a` after it slip past unrecognized.
#: `--jsx-import-source` (bun) added P10b round-7, 2026-07-21 — confirmed on Bun
#: 1.3.12 (`bun run --help` lists `--jsx-import-source=<val>` as a `run`-level
#: flag, distinct from `bun build`'s identically-named bundler flag) and
#: empirically (`bun run --jsx-import-source someval test` runs the `test`
#: script, i.e. `someval` is consumed as a MANDATORY space-separated token) that
#: `bun run --jsx-import-source test --filter=a test` was misrouted: before this,
#: `--jsx-import-source` wasn't in `_BUN_VALUE_FLAGS`, so the scan stopped at its
#: value token (`test`) as a phantom script positional, letting the real
#: `--filter=a` after it slip past unrecognized and returning a scopable=False
#: root-script target instead of the workspace-`a`-scoped run.
#: Deliberately EXCLUDED are bun's optional-value flags
#: (`--inspect`/`--inspect-wait`/`--inspect-brk`): the SAME arg-parser table declares
#: these `<STR>?` — the trailing `?` marks an OPTIONAL value that is only accepted
#: `=`-joined, never as a following bare token (confirmed: `bun run --inspect test`
#: runs the `test` script), so listing them here would wrongly swallow the script
#: positional.
_PNPM_VALUE_FLAGS = frozenset({"--filter", "-C", "--dir", "--loglevel", "--resume-from",
                               "--changed-files-ignore-pattern", "--test-pattern"})
_BUN_VALUE_FLAGS = frozenset({"--filter", "-F", "--cwd", "--shell", "--env-file",
                              "--elide-lines", "-r", "--preload", "--require",
                              "--import", "--install", "-d", "--define",
                              "--conditions", "--port", "--drop", "--title",
                              "--jsx-import-source"})


def _run_flag_prefix_len(own: list, value_flags: frozenset) -> int:
    """How many leading tokens of `own` a `run`-subcommand manager (pnpm or bun)
    parses as ITS OWN flags — the boundary past which its workspace-flag scan must
    NOT look. The region covers leading flags, ONE `run`/`run-script` subcommand
    token, and the flags between it and the SCRIPT positional: both accept their
    command flags on either side of `run` (`pnpm run -r test` ≡ `pnpm -r run test`,
    both recursive; `bun --filter=* run test` ≡ `bun run --filter=* test`), so a
    scan that stopped at the `run` SUBCOMMAND let a workspace flag after it slip
    through to a Tier-A root-script unwrap (#540 round-5 regression, audited
    2026-07-18). `value_flags` are the manager's OWN value-consuming flags
    (`--filter <pat>`, `-C <dir>`, `--cwd <dir>`), whose following value token is
    skipped WITH the flag so it never ends the region as a phantom script positional.

    The SCRIPT positional still ends the region. Unlike yarn's clipanion-based
    parser, which recognizes its named flags (`-T`) anywhere on the command line —
    even trailing the script name (confirmed by
    `test_yarn_top_level_flag_degrades_to_multi_project`) — both managers' own docs
    (`pnpm run --help`: `Usage: pnpm run <command> [<args>...]`; `bun run --help`:
    `Usage: bun run [flags] <file or script>`) and empirical behavior (`pnpm test
    --runxfail` / `bun run test --filter unit` run the script WITH the trailing
    token) show everything from the script name onward is that script's own
    argument, never re-parsed as a manager flag. So `pnpm test -r fE` / `bun run
    test --filter unit` run the `test` script WITH the trailing flag — the runner's
    own `-r`/`--filter`, not the manager's `--recursive`/workspace filter — and
    scanning past the script name would misclassify that forwarded runner flag as a
    workspace/recursive run."""
    idx = 0
    seen_run = False
    while idx < len(own):
        tok = own[idx]
        if not tok.startswith("-"):
            if seen_run or tok not in _RUN_SUBCMDS:
                break                    # the script positional ends the region
            seen_run = True              # skip the one run/run-script subcommand
            idx += 1
            continue
        idx += 1
        if tok in value_flags and idx < len(own):
            idx += 1                     # consume the flag's value too
    return idx


def _next_nonflag_pos(toks: list, start: int, value_flags: frozenset = frozenset()) -> Optional[int]:
    """First index at or after `start` that is NOT one of `toks`'s own flags — the
    script-name positional. `value_flags` are the manager's OWN space-separated
    value-consuming flags (see `_PNPM_VALUE_FLAGS`/`_BUN_VALUE_FLAGS`): the pair is
    skipped together so the value token (`warn` in `--loglevel warn`) is never
    mistaken for the script name (#540 round-6)."""
    j = start
    while j < len(toks):
        t = str(toks[j])
        if t and not t.startswith("-"):
            return j
        if t in value_flags and j + 1 < len(toks):
            j += 2
        else:
            j += 1
    return None


def _read_package_scripts(base: str) -> dict:
    """The `scripts` object from `package.json`, or {} on absence/malformed JSON.
    A full JSON parse (not a substring scan) so a script VALUE is read exactly.
    RecursionError included: a pathologically nested document overflows the C
    scanner's recursion limit with RecursionError, not ValueError — it must take
    the same documented Tier-B degrade, never escape through `detect_runner`."""
    raw = _read_text(base or ".", "package.json")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return {}
    if not isinstance(data, dict):
        return {}
    scripts = data.get("scripts")
    return scripts if isinstance(scripts, dict) else {}


def _package_script_target(argv: list) -> Optional[tuple]:
    """If argv invokes a package.json script via npm/yarn/pnpm/bun, return
    (script_name, multi_project, forwarded_args, package_manager). Else None (argv is
    not a package-script form).

    package_manager is the `_PKG_MANAGERS` entry (`"npm"`/`"yarn"`/`"pnpm"`/`"bun"`)
    that invoked it — callers needing to know which npm-compatibility environment
    variables the manager actually provides (e.g. bun does not set `INIT_CWD`; see
    `npm_script_env`) key off this instead of assuming npm's behavior for all four.

    multi_project=True → a workspace/recursive run (`pnpm -r test`, `npm test -w pkg`,
    `pnpm --filter x test`) spanning multiple packages/cwds; the caller must NOT
    unwrap it (opaque → Tier-B). `bun test` returns None here: it is bun's OWN runner
    (handled by `_runner_from_argv`); only `bun run <script>` is a package script.

    forwarded_args are the tokens the package manager passes VERBATIM to the invoked
    script/runner. npm requires a literal `--` separator to forward anything (`npm
    run <command> [-- <args>]`, confirmed via `npm help run-script`) — without it,
    npm consumes/parses the trailing tokens itself rather than forwarding them.
    Yarn, pnpm, and bun forward everything after the script name AS-IS, no `--`
    needed (confirmed via `pnpm run --help`: `pnpm run <command> [<args>...]`, and
    empirically: `pnpm test --runxfail` / `yarn test --runxfail` / `bun run test
    --runxfail` all run the script WITH `--runxfail`); dropping those args would
    silently change a scoped rerun's pass/fail semantics from the full gate's.
    `[]` when there is nothing to forward."""
    if not argv:
        return None
    head = _basename_token(argv[0])
    if head not in _PKG_MANAGERS:
        return None
    rest = [str(t) for t in argv[1:]]
    # A literal `--` separator ends the package manager's own flags — anything after
    # it is forwarded verbatim to the underlying script/runner (`npm test -- --filter
    # x` runs the test script WITH `--filter x`, it is not an npm workspace flag), so
    # workspace-flag AND script-name detection must not scan past it.
    dd = rest.index("--") if "--" in rest else None
    own = rest[:dd] if dd is not None else rest
    forwarded = rest[dd + 1:] if dd is not None else []
    # pnpm AND bun forward everything after the SCRIPT positional verbatim to the
    # invoked script (see `_run_flag_prefix_len` — its region spans the flags on
    # either side of a `run` subcommand) — scanning past the script name would treat
    # a forwarded runner flag (pytest's `-r fE` in `pnpm test -r fE`, or `--filter
    # unit` in `bun run test --filter unit`) as the manager's own workspace/recursive
    # flag. Both empirically forward the trailing token (`bun run test --filter a`
    # runs the `test` script WITH `--filter a`, per `bun run --help`'s `[flags]
    # <script>` order — confirmed on Bun 1.3.12). Other managers keep the whole-`own`
    # scan: yarn's named flags are recognized anywhere (even trailing the script
    # name), and npm never forwards without a literal `--` (already excluded via
    # `own`/`forwarded`).
    if head == "pnpm":
        workspace_scan = own[:_run_flag_prefix_len(own, _PNPM_VALUE_FLAGS)]
    elif head == "bun":
        workspace_scan = own[:_run_flag_prefix_len(own, _BUN_VALUE_FLAGS)]
    else:
        workspace_scan = own
    if any(_is_workspace_flag(t, head) for t in workspace_scan):
        return ("", True, forwarded, head)
    # Skip leading global option flags (`npm --silent run test`).
    i = 0
    while i < len(own) and own[i].startswith("-"):
        i += 1
    if i >= len(own):
        return None
    sub = own[i].lower()
    # Yarn/pnpm/bun forward trailing `own` tokens (past the script name) with no `--`
    # needed — npm does not, it requires the literal `--` handled via `forwarded`
    # above. Only applies when no literal `--` was present at all: once one appears,
    # everything past it is already captured in `forwarded`.
    implicit_forward = head != "npm" and dd is None
    if head == "bun":
        # bun: only `bun run <script>` wraps a package script; `bun test` is bun's
        # built-in runner (resolved by _runner_from_argv, not here).
        if sub in _RUN_SUBCMDS:
            pos = _next_nonflag_pos(own, i + 1, _BUN_VALUE_FLAGS)
            if pos is None:
                return None
            tail = own[pos + 1:] if implicit_forward else []
            return (own[pos], False, tail + forwarded, head)
        return None
    if sub in _RUN_SUBCMDS:
        pos = _next_nonflag_pos(own, i + 1, _PNPM_VALUE_FLAGS if head == "pnpm" else frozenset())
        if pos is None:
            return None
        tail = own[pos + 1:] if implicit_forward else []
        return (own[pos], False, tail + forwarded, head)
    if sub in _TEST_ALIASES:
        # npm and pnpm document `t`/`tst` as ALIASES for `test` (confirmed via their
        # CLI docs). Yarn does not: `yarn run -h=1` documents `yarn run <scriptName>
        # ...`, and yarn also permits omitting `run` — either way it resolves the
        # LITERAL script name, not an alias table. So for yarn, `t`/`tst` must look
        # up `scripts.t`/`scripts.tst` themselves, not fall through to `scripts.test`
        # (which may exist but be an unrelated script). bun never reaches this line
        # (it early-returns above), so only npm/pnpm alias `t`/`tst` to `test` here.
        name = sub if (head == "yarn" and sub != "test") else "test"
        tail = own[i + 1:] if implicit_forward else []
        return (name, False, tail + forwarded, head)
    return None


def _resolve_package_script(cwd: Optional[str], argv: list,
                            _depth: int = 0, _seen: frozenset = frozenset()) -> Optional[RunnerInfo]:
    """Unwrap an npm/yarn/pnpm/bun package-script invocation to the runner behind it.

    Returns None when argv is NOT a package-script form (the caller continues to
    `_runner_from_argv`). When it IS one, always returns a RunnerInfo:
      - a recognized single runner  → that runner (Tier-A, source "npm-script");
      - a multi-project/workspace    → UNKNOWN (source "wrapper:multi-project");
      - a script carrying a shell expansion the unwrap cannot reproduce (`pytest
        -c $CONF` — see `_script_needs_shell_expansion`) → UNKNOWN (Tier-B);
      - a multi-tool / opaque / missing / cyclic / runaway script → UNKNOWN (Tier-B).
    Resolves one level of script re-indirection (`"test": "npm run test:ci"`), bounded
    by a depth cap + a visited-name set so `"test": "npm test"` can never loop. A
    leading env prefix (`cross-env NODE_ENV=test npm test`) is stripped before the
    package-manager match and reattached to `resolved_cmd`, so an env-prefixed
    invocation still unwraps instead of degrading to Tier-B. Forwarded args — after a
    literal `--` for npm (`npm test -- --runxfail`, per `npm run <command> [--
    <args>]`), or trailing the script name with no `--` needed for yarn/pnpm/bun
    (`yarn test --runxfail`, `pnpm test --runxfail`) — are likewise reattached to
    `resolved_cmd`, since dropping them would silently change the scoped rerun's
    pass/fail semantics from the full gate's.

    A `pre<name>`/`post<name>` lifecycle hook (npm — and, depending on config,
    yarn/pnpm — runs these automatically around the named script; see
    https://docs.npmjs.com/cli/v11/using-npm/scripts#pre--post-scripts) makes the
    combined output NOT attributable to `name`'s runner alone, so its presence
    degrades this to Tier-B (UNKNOWN) rather than advertising a single-runner
    unwrap a scoped rerun would then silently bypass."""
    stripped_argv = _strip_env_prefix(argv)
    target = _package_script_target(stripped_argv)
    if target is None:
        return None
    name, multi, forwarded, manager = target
    if multi:
        return _mk(UNKNOWN, "wrapper:multi-project")
    if _depth >= 4 or name in _seen:
        return _mk(UNKNOWN, "wrapper:npm-script")
    base = cwd or "."
    scripts = _read_package_scripts(base)
    script = scripts.get(name)
    if not isinstance(script, str) or not script.strip():
        return _mk(UNKNOWN, "wrapper:npm-script")
    if any(isinstance(scripts.get(hook + name), str) and scripts[hook + name].strip()
           for hook in ("pre", "post")):
        # A lifecycle hook runs ALONGSIDE `name` (npm always; yarn/pnpm
        # conditionally) — the gate's output is then a mix of the hook's and the
        # target script's, which no single runner's classifier can safely parse,
        # and a scoped rerun of ONLY the target script would skip the hook the
        # full gate actually ran under. Stay opaque rather than misattribute it.
        return _mk(UNKNOWN, "wrapper:npm-script-lifecycle")
    if _script_chains_tools(script):
        return _mk(UNKNOWN, "wrapper:npm-script-shell")   # chains tools → Tier-B
    if _script_needs_shell_expansion(script):
        # The package manager's shell expands `$VAR` before the runner starts;
        # `shlex.split` below does not, so unwrapping would hand the scoped re-run
        # a literal `$VAR` and let it verify under a different env/config than the
        # gate that failed.
        return _mk(UNKNOWN, "wrapper:npm-script-expansion")
    try:
        sub_argv = shlex.split(script, posix=True)
    except ValueError:
        return _mk(UNKNOWN, "wrapper:npm-script")
    if not sub_argv:
        return _mk(UNKNOWN, "wrapper:npm-script")
    env_prefix = argv[:len(argv) - len(stripped_argv)] if len(stripped_argv) < len(argv) else None
    # The script may itself re-indirect through another package script. Thread the
    # outer forwarded `-- <args>` INTO the nested invocation instead of blindly
    # re-appending them to the fully-resolved runner. The package manager appends them
    # to the re-indirect command as raw trailing tokens (`npm run test:ci <args>`), so
    # whether they reach the terminal runner is decided by the NESTED command's OWN
    # forwarding rule: `npm run <other>` forwards NOTHING without its own `--` (verified
    # against npm 11.4.2 — `npm test -- --x` re-indirecting via `npm run inner` runs the
    # inner runner with argv `[]`), whereas a yarn/pnpm/bun re-indirect forwards them
    # bare. Appending unconditionally produced a `resolved_cmd` (`jest --x`) that the
    # gate never actually ran (`jest`), which would skew a scoped rerun.
    nested_argv = sub_argv + forwarded if forwarded else sub_argv
    nested = _resolve_package_script(base, nested_argv, _depth + 1, _seen | {name})
    if nested is not None:
        if env_prefix and nested.resolved_cmd is not None:
            nested = nested._replace(resolved_cmd=env_prefix + nested.resolved_cmd)
        if nested.package_manager is not None:
            # A mixed chain (`npm test` → `"test": "bun run inner"` → the runner)
            # reports the HEAD-of-chain manager — the one the gate command actually
            # invoked. `npm_script_env` keys the INIT_CWD omission off this: npm at
            # the head sets INIT_CWD on its child and bun (which never sets it)
            # passes it through to the runner, so keying off the TERMINAL manager
            # would drop a variable the gate's child really saw. The lifecycle
            # variables stay keyed to the TERMINAL script (`script_name`): each
            # manager overwrites those for its own child, so the innermost wins.
            #
            # The MIRROR chain (`bun run test` → `"test": "npm run inner"`) is ALSO
            # correct keyed on the HEAD, NOT on "every manager in the chain is bun":
            # bun runs an inner `npm`/`pnpm`/`yarn run` through its OWN script runner
            # (auto-aliased — verified on Bun 1.3.12: the terminal runner's
            # `npm_config_user_agent` stays `bun/…` and INIT_CWD is UNSET, even under
            # `--shell=system`), so a bun-HEADED chain never invokes a real
            # INIT_CWD-setting manager and its child sees none — exactly what a
            # head=bun omission reproduces. Keying instead off "any non-bun manager
            # present" would WRONGLY synthesize INIT_CWD for that child (the textual
            # `npm` never runs), reopening the fidelity gap it meant to close.
            nested = nested._replace(package_manager=manager)
        return nested
    r = _runner_from_argv(sub_argv)
    if r is not None:
        # Carry the EXPANDED sub_argv forward as `resolved_cmd` — the caller binds
        # the adapter (and builds a scoped re-run) against THIS, not the original
        # `npm test` argv, so the script's own env/launcher/config flags survive, AND
        # this invocation's own forwarded `-- <args>` tail so a behavior-changing flag
        # (`--runxfail`) isn't silently dropped from a scoped rerun.
        resolved = sub_argv + forwarded if forwarded else sub_argv
        if env_prefix:
            resolved = env_prefix + resolved
        return _mk(r, "npm-script", resolved_cmd=resolved, script_name=name,
                   package_manager=manager)
    return _mk(UNKNOWN, "wrapper:npm-script")


def npm_script_env(base: str, info: Optional[RunnerInfo]) -> dict:
    """The script-visible environment variables npm/yarn/pnpm/bun inject into a
    `scripts.<name>` child that we can reconstruct EXACTLY, without npm — for a
    P10 package-script unwrap whose scoped re-run bypasses the package manager
    (https://docs.npmjs.com/cli/v11/using-npm/scripts#environment):

      npm_lifecycle_event   the script name npm is running (`test`, `test:ci`)
      npm_lifecycle_script  that script's raw command string
      npm_package_json      absolute path of the package.json it came from
      INIT_CWD              the directory npm was invoked in — the gate's own cwd,
                            which is exactly the `cwd` a scoped re-run passes.
                            OMITTED for a bun-invoked unwrap: bun does not set
                            `INIT_CWD` on a `bun run` child (checked against Bun
                            1.2.14 — the lifecycle/package variables are present,
                            `INIT_CWD` is not), so synthesizing it would hand the
                            scoped re-run a variable the full gate's child never
                            saw. Keyed on the HEAD-of-chain manager (the one the
                            gate command actually invoked): in a mixed chain
                            (`npm test` → `"test": "bun run inner"`) npm sets
                            INIT_CWD at the head and bun passes it through, so
                            the runner's child really does see it

    A conftest / jest setup file / runner config that branches on one of these
    (`process.env.npm_lifecycle_event === "test:ci"`) would otherwise take a
    DIFFERENT branch under the scoped re-run than under the full gate, turning the
    isolation probe's answer into a statement about a different environment.

    Deliberately partial, and only over variables with a single faithful value.
    `npm_config_*` (and the wider `npm_package_*` set) are OMITTED — NOT because they
    are purely npm's own knobs: a gate command CAN carry a CLI flag that becomes one
    (`npm test --mode=strict` exposes `npm_config_mode=strict`; even an arbitrary
    `--foo=bar` becomes `npm_config_foo=bar` — verified against npm 11.4.2), so a
    value here can be a user input a conftest could read. They are omitted because we
    cannot reproduce them FAITHFULLY: the npmrc-cascade-derived values are
    unrecoverable without npm, and partially reproducing npm's CLI→config
    normalization (`--silent`→`loglevel=silent`, camelCase, `--no-`, boolean-vs-valued,
    short flags) would substitute a FABRICATED environment for a merely incomplete one
    — the worse failure. The residual gap (a scoped re-run seeing different
    `npm_config_*` than the full gate) is an ACCEPTED, bounded limitation: it bites
    only a runner/conftest that branches on a CLI-supplied `npm_config_*`. Degrading
    every config-flag-carrying invocation to whole-suite instead would forfeit the far
    more common HARMLESS case — `npm --silent run test`, `npm test --runxfail` — which
    npm consumes as config the runner ignores, and which stay deliberately Tier-A.
    Returns {} for anything that is not a single-runner package-script unwrap, or when
    package.json is unreadable. Never raises."""
    if info is None or info.source != "npm-script" or not info.script_name:
        return {}
    base = base or "."
    try:
        pkg_json = os.path.abspath(os.path.join(base, "package.json"))
        init_cwd = os.path.abspath(base)
    except (OSError, ValueError):
        return {}
    if not _exists(base, "package.json"):
        return {}
    env = {"npm_lifecycle_event": info.script_name,
           "npm_package_json": pkg_json}
    if info.package_manager != "bun":
        env["INIT_CWD"] = init_cwd
    script = _read_package_scripts(base).get(info.script_name)
    if isinstance(script, str):
        env["npm_lifecycle_script"] = script
    return env


def npm_bin_path_dirs(base: str) -> list[str]:
    """Ancestor `node_modules/.bin` directories from `base` up to the filesystem
    root, nearest first — the SAME directories npm/yarn/pnpm prepend to `PATH`
    while running a `scripts.<name>` entry (https://docs.npmjs.com/cli/v11/
    using-npm/scripts#path). A P10 package-script unwrap's `resolved_cmd` (e.g.
    `["cross-env", "SPECIAL=1", "pytest"]` behind `npm test`) can name a binary
    that exists ONLY in one of these directories; executing it directly via
    `subprocess.run` — bypassing npm entirely, as a scoped rerun does — would
    otherwise raise FileNotFoundError even though the full `npm test` gate
    passed. Missing/unreadable directories are skipped; a `base` with no
    `node_modules` anywhere in its ancestry returns []. Never raises."""
    dirs = []
    try:
        cur = os.path.abspath(base or ".")
    except (OSError, ValueError):
        return dirs
    seen = set()
    while cur not in seen:
        seen.add(cur)
        candidate = os.path.join(cur, "node_modules", ".bin")
        if _exists(cur, "node_modules", ".bin") and os.path.isdir(candidate):
            dirs.append(candidate)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return dirs


# ── Detection: repo markers ──────────────────────────────────────────────────────

def _exists(base: str, *rel) -> bool:
    try:
        return os.path.exists(os.path.join(base, *rel))
    except (OSError, ValueError):
        return False


def _read_text(base: str, *rel) -> str:
    try:
        with open(os.path.join(base, *rel), "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except (OSError, ValueError):
        return ""


def _glob(base: str, pattern: str) -> list:
    try:
        return sorted(str(p) for p in Path(base).glob(pattern))
    except (OSError, ValueError):
        return []


# Directories pruned from the recursive .NET project-file walk — dependency and
# build output that is never itself a test project and can be huge.
_LARGE_DIR_NAMES = frozenset({
    ".git", "node_modules", "bin", "obj", "venv", ".venv", "target", "build",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".gradle", ".mvn",
})


@lru_cache(maxsize=16)
def _nextest_available(base: str) -> bool:
    """True when cargo-nextest is usable in this repo. Prefers the read-only
    `.config/nextest.toml` marker; falls back to a bounded `cargo nextest --version`
    probe (the one subprocess detection is allowed to run). Any failure -> False, so
    a repo without nextest degrades to stable `cargo test`."""
    if _exists(base, ".config", "nextest.toml"):
        return True
    try:
        proc = subprocess.run(
            ["cargo", "nextest", "--version"],
            cwd=base, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=2)
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 — FileNotFoundError / timeout / anything → no
        return False


def _runner_from_markers(cwd: Optional[str]) -> RunnerInfo:
    """Identify the repo's runner from read-only marker files. Applied only when
    argv did not decide (e.g. detection called with no command). The tox/nox guard
    runs FIRST so a repo that drives pytest through tox is reported as the tox
    wrapper, not pytest."""
    base = cwd or "."

    # ── Guard: tox / nox wrappers force Tier-B (override the tox.ini→pytest signal).
    if _exists(base, "noxfile.py"):
        return _mk(NOX, "marker:noxfile.py")
    if _exists(base, "tox.ini") and re.search(r"(?m)^\[tox\]", _read_text(base, "tox.ini")):
        return _mk(TOX, "marker:tox.ini")

    # ── Python: pytest.
    if _exists(base, "pytest.ini") or _exists(base, "conftest.py"):
        return _mk(PYTEST, "marker:pytest")
    if re.search(r"(?m)^\[tool\.pytest\.ini_options\]", _read_text(base, "pyproject.toml")):
        return _mk(PYTEST, "marker:pyproject")
    if re.search(r"(?m)^\[tool:pytest\]", _read_text(base, "setup.cfg")):
        return _mk(PYTEST, "marker:setup.cfg")

    # ── JS/TS: read package.json + config files.
    js = _runner_from_js(base)
    if js is not None:
        return js

    # ── Bun / Deno: their own built-in test runners (Tier-C — often no test-dep in
    # package.json). Checked AFTER the JS runner-config/dep signals so a bun/deno repo
    # that actually drives vitest/jest is reported as that runner, not the runtime. A
    # lockfile / runtime-config marker is the signal.
    if _exists(base, "bunfig.toml") or _exists(base, "bun.lockb") or _exists(base, "bun.lock"):
        return _mk(BUN, "marker:bun")
    if (_exists(base, "deno.json") or _exists(base, "deno.jsonc")
            or _exists(base, "deno.lock")):
        return _mk(DENO, "marker:deno")

    # ── Go / Rust / Elixir.
    if _exists(base, "go.mod"):
        return _mk(GO, "marker:go.mod")
    if _exists(base, "Cargo.toml"):
        return _mk(NEXTEST if _nextest_available(base) else CARGO, "marker:Cargo.toml")
    if _exists(base, "mix.exs"):
        return _mk(MIX, "marker:mix.exs")

    # ── JVM.
    if _exists(base, "pom.xml"):
        return _mk(MAVEN, "marker:pom.xml")
    if (_exists(base, "build.gradle") or _exists(base, "build.gradle.kts")
            or _exists(base, "settings.gradle") or _exists(base, "settings.gradle.kts")):
        return _mk(GRADLE, "marker:build.gradle")

    # ── Ruby / PHP.
    if _exists(base, ".rspec") or (_exists(base, "Gemfile") and _exists(base, "spec")):
        return _mk(RSPEC, "marker:rspec")
    if _exists(base, "Gemfile") and _exists(base, "test"):
        return _mk(MINITEST, "marker:minitest")
    if _exists(base, "tests", "Pest.php"):
        return _mk(PEST, "marker:pest")
    if _exists(base, "phpunit.xml") or _exists(base, "phpunit.xml.dist"):
        return _mk(PHPUNIT, "marker:phpunit")

    # ── .NET. Walk (not Path.glob("**/*.csproj")) so we can prune large
    # dependency/build dirs in-place instead of traversing them fully first.
    proj_files = []
    try:
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d.lower() not in _LARGE_DIR_NAMES]
            for file in files:
                if file.endswith(".csproj") or file.endswith(".fsproj"):
                    proj_files.append(os.path.join(root, file))
                    if len(proj_files) >= 20:
                        break
            if len(proj_files) >= 20:
                break
    except (OSError, ValueError):
        pass
    for proj in proj_files:
        try:
            with open(proj, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
        except (OSError, ValueError):
            continue
        if re.search(r"Microsoft\.NET\.Test\.Sdk|xunit|nunit|MSTest|IsTestProject", txt, re.I):
            return _mk(DOTNET, "marker:csproj")

    # ── Native / mobile.
    if re.search(r"\benable_testing\s*\(", _read_text(base, "CMakeLists.txt")):
        return _mk(CTEST, "marker:CMakeLists.txt")
    if re.search(r"\.testTarget\b|isTest:\s*true", _read_text(base, "Package.swift")):
        return _mk(SWIFT, "marker:Package.swift")
    if _exists(base, "pubspec.yaml"):
        pub = _read_text(base, "pubspec.yaml")
        return _mk(FLUTTER if re.search(r"(?m)^\s*flutter\s*:", pub) else DART, "marker:pubspec.yaml")

    # ── Django (unittest runner) — a late fallback, after pytest markers.
    if _exists(base, "manage.py"):
        return _mk(DJANGO, "marker:manage.py")

    return _mk(UNKNOWN, "none")


def _runner_from_js(base: str) -> Optional[RunnerInfo]:
    """Detect a JS/TS runner from config files + package.json dev/deps. Returns None
    when there is no JS project or the runner is indeterminate (an npm-script-only
    `package.json` — unwrapping `scripts.test` is P10). Config files are checked
    before package.json because a present config file is the stronger signal."""
    # Config-file markers (the strong signal).
    if _glob(base, "vitest.config.*") or _glob(base, "vitest.workspace.*"):
        return _mk(VITEST, "marker:vitest.config")
    if _glob(base, "jest.config.*"):
        return _mk(JEST, "marker:jest.config")
    if (_exists(base, "karma.conf.js") or _exists(base, "karma.conf.ts")
            or _exists(base, "angular.json")):
        return _mk(KARMA, "marker:karma")
    if (_exists(base, ".mocharc.js") or _exists(base, ".mocharc.json")
            or _exists(base, ".mocharc.yml") or _exists(base, ".mocharc.cjs")):
        return _mk(MOCHA, "marker:.mocharc")
    if (_exists(base, "jasmine.json") or _exists(base, "spec", "support", "jasmine.json")):
        return _mk(JASMINE, "marker:jasmine.json")

    pkg = _read_text(base, "package.json")
    if not pkg:
        return None
    # package.json present: read declared dev/deps + an inline `"jest": {}` /
    # `"ava": {}` config block. Substring scans on the raw text are enough to pick
    # the runner without a full JSON parse (and never raise on malformed JSON).
    def _dep(name):
        return re.search(r'"' + re.escape(name) + r'"\s*:', pkg) is not None
    if _dep("vitest"):
        return _mk(VITEST, "marker:package.json")
    if _dep("jest"):
        return _mk(JEST, "marker:package.json")
    if _dep("karma") or _dep("@angular/cli"):
        return _mk(KARMA, "marker:package.json")
    if _dep("mocha"):
        return _mk(MOCHA, "marker:package.json")
    if _dep("jasmine") or _dep("jasmine-core"):
        return _mk(JASMINE, "marker:package.json")
    if _dep("ava"):
        return _mk(AVA, "marker:package.json")
    # No recognized runner dep: its `scripts.test` may still run a real runner
    # (`"test": "vitest run"`) or a Tier-C runtime (`"test": "bun test"`). Unwrap it
    # (P10) so a standard `npm test` repo stays Tier-A in marker-only detection too. A
    # multi-tool / opaque / missing script resolves to UNKNOWN → return None so the
    # caller continues to the bun/deno markers, then UNKNOWN.
    unwrapped = _resolve_package_script(base, ["npm", "run", "test"])
    if unwrapped is not None and unwrapped.runner != UNKNOWN:
        return unwrapped
    return None


def detect_runner(cwd: Optional[str], resolved_cmd: Optional[list[str] | str]) -> RunnerInfo:
    """Identify the runner behind the resolved gate command.

    Priority: the command's argv[0]/shape first, then repo markers. A `bash -lc`
    shell wrapper or a non-runner argv[0] (`./run-tests.sh`, `make`, a custom
    binary) is an opaque wrapper we cannot see through → UNKNOWN (Tier-B). Marker
    detection is reached only when NO command pins the runner (argv empty/None) —
    the gate always passes a command, but detection is reusable for a marker-only
    query (auto-detect, F2). Read-only apart from the bounded nextest probe.
    """
    if isinstance(resolved_cmd, str):
        # A bare string would otherwise be iterated character-by-character below.
        resolved_cmd = [resolved_cmd]
    argv = [str(a) for a in (resolved_cmd or []) if a is not None]
    if _is_shell_wrapper(argv):
        # The runner is inside the shell string, not at argv[0] — read it back out
        # (`_runner_from_shell_string`). NEVER scopable, even when found: a shell
        # string is not an argv we can append an exact-subset filter to (the runner
        # may sit behind a `cd`, mid-pipeline, or after a `&&` step), which is P1's
        # "scopable iff argv AND recognized" rule. A string naming no recognized
        # runner (`npm test`, `make test`) or two of them stays a Tier-B opaque
        # wrapper, exactly as before.
        r = _runner_from_shell_string(_shell_string(argv))
        if r is not None:
            return RunnerInfo(runner=r, scopable=False, source="shell")
        return _mk(UNKNOWN, "wrapper:shell")
    # npm/yarn/pnpm/bun package-script → unwrap through package.json `scripts` to the
    # real runner (P10). A recognized single runner keeps the repo Tier-A; a
    # multi-tool / multi-project / opaque script degrades to a Tier-B UNKNOWN wrapper.
    unwrapped = _resolve_package_script(cwd, argv)
    if unwrapped is not None:
        return unwrapped
    r = _runner_from_argv(argv)
    if r is not None:
        return _mk(r, "argv")
    if argv:
        # A real but unrecognized argv[0] (`./run-tests.sh`, `make check`, a custom
        # binary). We cannot know what it runs → opaque wrapper. Markers do NOT
        # override this (the script may run anything), matching P1's rule that an
        # unrecognized argv is un-scopable Tier-B.
        return _mk(UNKNOWN, "wrapper:unrecognized")
    # No command given — fall back to repo markers (with the tox/nox guard).
    return _runner_from_markers(cwd)


# ── Classification ───────────────────────────────────────────────────────────────

#: Universal, HIGH-CONFIDENCE env-error markers — the runner/toolchain itself is
#: missing (not a test's own assertion). Kept deliberately narrow so a per-runner
#: compile/collection error (e.g. pytest's own `ModuleNotFoundError` at collection,
#: which is exit 2 = compile_error) is NOT swallowed as env. Applied only on a
#: NONZERO exit (a clean exit is never an env error).
_ENV_MARKERS_UNIVERSAL = re.compile(
    r"command not found"                                     # bash/zsh: <cmd>: command not found
    r"|^(?:\S*/)?(?:sh|dash|ash|bash|zsh|ksh):\s*(?:\d+:\s*)?[\w./+-]+:\s*not found\s*$"
                                                # sh/dash: [sh: 1: ]<cmd>: not found — requires the shell-name
                                                # prefix so an in-message "<token>: not found" from a test's
                                                # own assertion (e.g. "AssertionError: config: not found")
                                                # isn't misread as a shell diagnostic
    r"|is not recognized as an internal or external command"  # Windows cmd
    r"|is not recognized as the name of a cmdlet"              # Windows PowerShell
    r"|no such file or directory[^\n]*[\s'\"/]"
    r"(?:python|node|cargo|go|mvn|gradle|dotnet|mix|ruby|php|swift|dart|flutter|pytest|jest|vitest|mocha|jasmine|karma|rspec|phpunit|pest)[0-9.]*['\"]?\s*$"
                                                # ENOENT on a MISSING runner: the exe must be the
                                                # basename of the missing path (a `/`/quote/space
                                                # boundary before it, end-of-line after an optional
                                                # version suffix). Anchoring like the shell-name rule
                                                # above stops a `.*<substr>` match on an in-message
                                                # path — e.g. `…/logo.png` (`go`) or a venv
                                                # `…/python3.11/site-packages/foo.py` (`python`) —
                                                # from mislabeling a real test failure as env_error.
    r"|error: no such command"          # cargo subcommand (e.g. nextest) not installed
    r"|executable file not found",      # Go/exec ENOENT
    re.I | re.M,
)


def _combine(stdout, stderr) -> str:
    a = stdout if isinstance(stdout, str) else (stdout.decode("utf-8", "ignore") if isinstance(stdout, (bytes, bytearray)) else "")
    b = stderr if isinstance(stderr, str) else (stderr.decode("utf-8", "ignore") if isinstance(stderr, (bytes, bytearray)) else "")
    return (a or "") + "\n" + (b or "")


def classify(runner, exit_code, stdout="", stderr="", timed_out=False) -> str:
    """Classify ONE runner invocation into exactly one of OUTCOMES.

    `exit_code` may be None (e.g. the process was killed) → treated as a failure
    unless `timed_out`. `stdout`/`stderr` are combined for marker scanning (the gate
    already merges them). This is a PURE function — no filesystem, no subprocess."""
    if timed_out:
        return TIMEOUT
    out = _combine(stdout, stderr)
    try:
        rc = int(exit_code) if exit_code is not None else 1
    except (TypeError, ValueError):
        rc = 1

    # Universal env-error pre-check (nonzero only). Exit 127 is command-not-found on
    # POSIX; the marker set catches the rest. A per-runner compile/collection error
    # is NOT matched here (those markers are runner-specific and handled below).
    if rc != 0 and (rc == 127 or _ENV_MARKERS_UNIVERSAL.search(out)):
        return ENV_ERROR

    fn = _CLASSIFIERS.get(runner, _classify_generic)
    return fn(rc, out)


# ---- helpers shared across per-runner classifiers -------------------------------

def _has(out: str, *patterns) -> bool:
    """True when any regex pattern matches (case-insensitive)."""
    return any(re.search(p, out, re.I) for p in patterns)


#: The go per-package RESULT line that proves tests ACTUALLY RAN: a column-0 `ok`
#: WITHOUT a zero-test annotation. A bare `^ok\s` is NOT run evidence — `go test -run
#: Nomatch ./...` (every test filtered out) and a package with no `_test.go` file BOTH
#: exit 0 and print a column-0 `ok  pkg 0.5s [no tests to run]` / `[no test files]`
#: line (verified on go1.26.5), so reading those as "tests ran" false-GREENS a run
#: that executed nothing. Shared by `_RAN_TESTS_MARKERS_ANY` and `_classify_go` — one
#: source of truth, because the two MUST agree on what counts as evidence. The
#: `(?!\s+\|)` excludes deno's OWN column-0 summary (`ok | 0 passed | 0 failed
#: (0ms)`), which is not a go package result — a go `ok` line is always `ok <pkg>
#: <time>`, never a `|`. The exclusion spans the whitespace ON PURPOSE: a bare
#: `\s+(?!\|)` fails here, because its greedy `\s+` backtracks to a single space
#: when the lookahead trips, so `ok  |…` (two spaces before the pipe) slips through
#: and false-greens a padded deno summary. Anchoring the pipe test to the whole
#: whitespace run catches any-width padding, so a zero-test deno run reached
#: through an opaque wrapper cannot read as go run-evidence and defeat its own
#: `_NO_TESTS_MARKERS_ANY` entry.
_GO_OK_RAN = r"^ok(?!\s+\|)\s+(?![^\n]*\[no tests? (?:to run|files)\])"

#: Zero-test markers UNIONED across every per-runner classifier below — the only
#: signal `_classify_generic` has when the runner is opaque. Not every runner is
#: string-detectable (pytest signals an empty run by exit code 5 ALONE, so a wrapper
#: hiding pytest keeps the pre-F2 posture), but every silent-exit-0 runner is: those
#: are precisely the ones that would otherwise false-green.
_NO_TESTS_MARKERS_ANY = re.compile(
    r"\[no tests? (?:to run|files)\]|testing: warning: no tests to run"  # go
    r"|^Ran 0 tests\b"                                                   # unittest/django/bun
    r"|No tests? (?:found|ran|executed|to run)\b"                        # jest/phpunit/dart/maven…
    r"|No test (?:files|suite) found|Couldn't find any files to test"    # vitest/mocha/ava
    r"|No specs found|\b0 specs,\s*0 failures"                           # jasmine
    r"|Executed 0 of \d+|TOTAL:\s*0\s+SUCCESS"                           # karma
    r"|^#\s*tests\s+0\b|^1\.\.0\b"                                       # node:test (TAP)
    r"|running 0 tests"                                                  # cargo
    r"|Tests run:\s*0\b"                                                 # maven/surefire
    r"|No tests found for given includes"                                # gradle
    r"|No test is available|no test matches the specified selection criteria"  # dotnet/VSTest
    r"|\b0 tests,\s*0 failures|There are no tests to run"                # mix
    r"|\b0 examples,\s*0 failures"                                       # rspec
    r"|\b0 runs,\s*0 assertions"                                         # minitest
    r"|No tests were found|0 tests from 0 test (?:suites|cases)"         # gtest/ctest
    r"|\[  PASSED  \] 0 tests|Running 0 tests"                           # gtest banners
    r"|Executed 0 tests|Test run with 0 tests|No matching test cases were run"  # swift
    r"|Found no tests"                                                   # dart
    r"|\b0 passed\s*\|\s*0 failed\b",                                    # deno
    re.I | re.M,
)

#: RUN-EVIDENCE markers: proof that tests ACTUALLY EXECUTED — a nonzero count, or a
#: per-test verdict. The counterweight to `_NO_TESTS_MARKERS_ANY`: an opaque wrapper
#: often drives SEVERAL suites (`npm test` → lint + unit), so one empty suite's marker
#: must never mask a sibling suite that really ran. Same guard shape the go / dotnet /
#: swift / cargo classifiers already use against their own zero-count trailers.
_RAN_TESTS_MARKERS_ANY = re.compile(
    _GO_OK_RAN + r"|^---\s+(?:PASS|FAIL)"                        # go / TAP
    r"|\b[1-9]\d* (?:passed|failed|passing|failing|error)"       # pytest/vitest/mocha…
    r"|\bRan [1-9]\d* tests?\b"                                  # unittest/django
    r"|Tests:\s*[1-9]"                                           # jest / phpunit summary
    r"|\b[1-9]\d* specs?,|Executed [1-9]\d* of"                  # jasmine / karma
    r"|^#\s*(?:pass|fail)\s+[1-9]"                               # node:test (TAP)
    r"|running [1-9]\d* tests?"                                  # cargo
    r"|Tests run:\s*[1-9]"                                       # maven/surefire
    r"|(?:Total|Passed|Failed):\s*[1-9]"                         # dotnet/VSTest
    r"|\b[1-9]\d* tests?,\s*\d+ failures?"                       # mix
    r"|\b[1-9]\d* examples?,|\b[1-9]\d* runs?,"                  # rspec / minitest
    r"|\[  (?:PASSED|FAILED)  \] [1-9]"                          # gtest
    r"|Executed [1-9]\d* tests?|Test run with [1-9]\d* tests?"   # swift
    r"|All tests passed|OK \([1-9]",                             # dart / phpunit
    re.I | re.M,
)


def _classify_generic(rc: int, out: str) -> str:
    """Opaque-wrapper / unknown-runner fallback (`npm test`, `make check`,
    `./run-tests.sh`, a `bash -lc` string naming no runner we recognize).

    rc nonzero → failed. rc 0 → passed, UNLESS the output carries a zero-test marker
    from SOME known runner and NO evidence that anything ran — then no_tests. That
    last branch is what stops the wrapper path from re-opening the false-green this
    module exists to close: `npm test` is the single most common gate command there
    is, argv[0] alone can never see the jasmine / Karma / go / VSTest / gtest behind
    it, and every one of those EXITS 0 on an empty run. Both halves are required —
    the marker alone would mislabel a multi-suite wrapper whose FIRST suite is empty
    and whose second really ran (the go `[no test files]`-beside-a-passing-package
    shape), so a nonzero count anywhere keeps the run `passed`.

    Erring toward no_tests here is deliberate and fail-SAFE: the gate maps no_tests to
    a loud "zero coverage, not green" SKIP that never blocks the push, whereas the
    error it replaces — reporting a zero-test run as a verified GREEN — is the one this
    module must never make. env/timeout are already handled by `classify`."""
    if rc != 0:
        return FAILED
    if _NO_TESTS_MARKERS_ANY.search(out) and not _RAN_TESTS_MARKERS_ANY.search(out):
        return NO_TESTS
    return PASSED


# ---- Python ---------------------------------------------------------------------

def _classify_pytest(rc: int, out: str) -> str:
    # `python -m pytest` when pytest is not installed exits 1 with this exact string
    # (the RUNNER missing — distinct from a test's own import error, which is a
    # collection error = exit 2 = compile_error below).
    if rc == 1 and _has(out, r"No module named ['\"]?pytest\b"):
        return ENV_ERROR
    if rc == 0:
        return PASSED
    if rc == 5:                 # ExitCode.NO_TESTS_COLLECTED
        return NO_TESTS
    if rc == 1:                 # ExitCode.TESTS_FAILED
        return FAILED
    if rc == 2:                 # collection/import error (INTERRUPTED)
        return COMPILE_ERROR
    if rc in (3, 4):            # INTERNAL_ERROR / USAGE_ERROR — tooling/config, not a test
        return ENV_ERROR
    return FAILED


def _classify_unittest(rc: int, out: str) -> str:
    # Python >= 3.12: `python -m unittest` exits 5 on zero tests. Python <= 3.11:
    # exits 0 but prints "Ran 0 tests" — parse it or it false-greens. Gated on the
    # absence of a real "Ran N tests" (nonzero) summary anywhere in the output so a
    # suite that actually ran and failed — but whose OWN output happens to contain
    # the "Ran 0 tests" string (e.g. a test asserting on captured subprocess text)
    # — can't be masked as an empty run. Same run-evidence guard shape as the
    # go/cargo/maven/dotnet classifiers.
    if rc == 5:
        return NO_TESTS
    if _has(out, r"(?m)^Ran 0 tests\b", r"\bRan 0 tests in\b") and not _has(
            out, r"\bRan [1-9]\d* tests?\b"):
        return NO_TESTS
    if rc == 0:
        return PASSED
    # A bare `ImportError`/`ModuleNotFoundError` before any test = load failure.
    if _has(out, r"\bImportError\b", r"\bModuleNotFoundError\b", r"\bSyntaxError\b") and not _has(out, r"(?m)^(FAIL|ERROR):"):
        return COMPILE_ERROR
    return FAILED


def _classify_django(rc: int, out: str) -> str:
    # `manage.py test` drives unittest; zero tests → "Ran 0 tests". Gated on the
    # absence of a real "Ran N tests" (nonzero) summary — same run-evidence guard the
    # unittest classifier above uses — so a suite that actually ran and failed, but
    # whose own output happens to echo "Ran 0 tests" (e.g. a test asserting on
    # captured subprocess/management-command text), isn't masked as an empty run.
    if _has(out, r"\bRan 0 tests\b") and not _has(out, r"\bRan [1-9]\d* tests?\b"):
        return NO_TESTS
    if rc == 0:
        return PASSED
    if _has(out, r"\bImportError\b", r"\bModuleNotFoundError\b", r"\bSyntaxError\b",
            r"\bAppRegistryNotReady\b") and not _has(out, r"(?m)^(FAIL|ERROR):"):
        return COMPILE_ERROR
    return FAILED


# ---- JavaScript / TypeScript ----------------------------------------------------

def _js_env(out: str) -> bool:
    """JS-specific env markers: node_modules / a dependency not installed. Kept out
    of the universal set because `Cannot find module` is the exact signature of a
    missing dependency AND (rarely) a test's own bad require — env_error still reds
    the gate, so erring toward env here is safe and satisfies the
    missing-node_modules → env_error contract."""
    return _has(out, r"Cannot find module", r"Cannot find package",
                r"Failed to resolve (?:import|entry)", r"ERR_MODULE_NOT_FOUND",
                r"node_modules.*not found", r"Please install .* to use",
                # webpack (Angular/Karma `ng test`): "Module not found: Error: Can't
                # resolve 'X'"; esbuild (Vite/vitest, Angular v16+): "Could not
                # resolve 'X'" — both are canonical missing-node_modules signatures.
                r"Module not found", r"Can't resolve", r"Could not resolve")


def _classify_jest(rc: int, out: str) -> str:
    if rc != 0 and _js_env(out):
        return ENV_ERROR
    # jest exits 1 on "No tests found" (conflated with a failure) — parse it. With
    # --json the signal is numTotalTests==0. Gated on the absence of a real "Tests: N
    # …" summary (nonzero) so a genuinely-run suite whose OWN output happens to
    # contain the marker text (a snapshot/assertion string, e.g.) can't be masked as
    # an empty run — same run-evidence guard shape as the go/cargo/maven/dotnet
    # classifiers above.
    if _has(out, r"No tests found", r'"numTotalTests"\s*:\s*0\b',
            r"No tests found, exiting with code") and not _has(out, r"Tests:\s*[1-9]"):
        return NO_TESTS
    if rc == 0:
        return PASSED
    # A TS/transform compile error surfaces before tests run.
    if _has(out, r"error TS\d+", r"SyntaxError", r"Test suite failed to run.*(?:SyntaxError|Cannot use import)"):
        return COMPILE_ERROR
    return FAILED


def _classify_vitest(rc: int, out: str) -> str:
    if rc != 0 and _js_env(out):
        return ENV_ERROR
    # Gated on the absence of a real nonzero "Test Files"/"Tests" summary — same
    # run-evidence guard shape the jest classifier above uses — so a suite that
    # actually ran and failed, but whose own output happens to echo a
    # "No test files found"-style string, isn't masked as an empty run.
    if _has(out, r"No test files found", r"No test suite found",
            r"include:.*no test files", r'"numTotalTests"\s*:\s*0\b') and not _has(
            out, r"Test Files\s+[1-9]\d*\s*(?:passed|failed)",
            r"Tests\s+[1-9]\d*\s*(?:passed|failed)"):
        return NO_TESTS
    if rc == 0:
        return PASSED
    if _has(out, r"error TS\d+", r"Failed to load", r"SyntaxError"):
        return COMPILE_ERROR
    return FAILED


def _classify_mocha(rc: int, out: str) -> str:
    # mocha's exit code == the number of failing tests; 0 = all pass. "No test files
    # found" (exit >0) = no tests. Gated on the absence of a real "N passing"/"N
    # failing" summary — same run-evidence guard shape as the jest/vitest/jasmine
    # classifiers above — so a suite that actually ran (e.g. a CLI/error-path test
    # whose own captured output happens to echo the no-test marker text) isn't
    # masked as an empty run.
    if rc != 0 and _js_env(out):
        return ENV_ERROR
    if _has(out, r"No test files found", r"Error: No test files found",
            r"cannot resolve path.*spec") and not _has(
            out, r"\b[1-9]\d* passing\b", r"\b[1-9]\d* failing\b"):
        return NO_TESTS
    # mocha exits 0 on a run with zero runnable tests (empty suite / a --grep
    # filter matching nothing) unless --fail-zero is passed, printing "0 passing"
    # with none of the "No test files found"-style markers above. Gated on the
    # absence of a real nonzero "N passing" summary — same run-evidence guard
    # shape as the jest/vitest classifiers — so an actually-run suite isn't
    # misread.
    if rc == 0 and _has(out, r"\b0 passing\b") and not _has(out, r"\b[1-9]\d* passing\b"):
        return NO_TESTS
    if rc == 0:
        return PASSED
    if _has(out, r"error TS\d+", r"SyntaxError"):
        return COMPILE_ERROR
    return FAILED


def _classify_jasmine(rc: int, out: str) -> str:
    if rc != 0 and _js_env(out):
        return ENV_ERROR
    # jasmine EXITS 0 on "No specs found" — the canonical silent-green class. The
    # marker MUST be parsed or a zero-test run false-greens. TWO guards scope it to a
    # benign empty run:
    #   * rc == 0 — a NONZERO exit is NEVER a benign empty run (the same invariant
    #     `_classify_dotnet` documents in its own branch). jasmine v3+ deliberately
    #     exits nonzero when it finds no specs BECAUSE its maintainers treat that as an
    #     error, and a broken/misconfigured run (a bad `spec_dir`, a helper that threw
    #     before loading) also exits nonzero while printing the same marker — reporting
    #     any of those as a green SKIP lets the push proceed unverified.
    #   * no real "N specs, M failures" summary (nonzero N) — so a genuinely-run suite
    #     whose OWN failure output quotes that marker text (a spec asserting on a CLI's
    #     "No specs found" message) can't be misread as an empty run. Same run-evidence
    #     guard shape as the jest classifier above.
    if rc == 0 and _has(out, r"No specs found", r"Incomplete: No specs found",
                        r"\b0 specs,\s*0 failures") and not _has(out, r"\b[1-9]\d* specs?,"):
        return NO_TESTS
    if rc == 0:
        return PASSED
    return FAILED


def _classify_karma(rc: int, out: str) -> str:
    # A missing dependency (webpack "Module not found" / esbuild) → env_error.
    if rc != 0 and _js_env(out):
        return ENV_ERROR
    # A BROKEN run reports "Executed 0 of N" too — a TS/bundle compile error, or a
    # browser that DISCONNECTED / ERRORed mid-run. That is NOT a clean zero-spec run,
    # so name it compile_error (RED) — it must never be masked as a green no-tests
    # SKIP (the exact silent-green a test gate must catch). Checked BEFORE the
    # zero-spec marker below, which is scoped to the SUCCESS outcome.
    if _has(out, r"error TS\d+", r"Compilation( of the )?.*failed", r"Cannot determine",
            r"Executed 0 of \d+ \(?(?:ERROR|DISCONNECTED)", r"\bDISCONNECTED\b"):
        return COMPILE_ERROR
    # Karma / Angular `ng test`: the "Executed N of M" summary is the ONLY trustworthy
    # signal — the "SUCCESS" token prints even at zero tests, and the exit code flips
    # (default failOnEmptyTestSuite=true → exit 1 on empty; opt-out → exit 0 silent
    # green). no_tests ⟺ executed count (group 1) == 0, REGARDLESS of exit code, so we
    # catch BOTH the default false-red and the opt-out false-green.
    if _has(out, r"No specs found", r"Executed 0 of \d+", r"TOTAL:\s*0\s+SUCCESS"):
        return NO_TESTS
    if rc == 0:
        return PASSED
    return FAILED


def _classify_node_test(rc: int, out: str) -> str:
    # node:test emits TAP. Zero tests → "# tests 0" plan.
    if rc != 0 and _js_env(out):
        return ENV_ERROR
    if _has(out, r"(?m)^#\s*tests\s+0\b", r"(?m)^1\.\.0\b"):
        return NO_TESTS
    if rc == 0:
        return PASSED
    return FAILED


def _classify_ava(rc: int, out: str) -> str:
    if rc != 0 and _js_env(out):
        return ENV_ERROR
    if _has(out, r"Couldn't find any files to test", r"No test files"):
        return NO_TESTS
    if rc == 0:
        return PASSED
    return FAILED


# ---- Bun / Deno (Tier-C runtimes) -----------------------------------------------

def _classify_bun(rc: int, out: str) -> str:
    # `bun test` is Bun's built-in runner (JS). A missing dependency → env.
    if rc != 0 and _js_env(out):
        return ENV_ERROR
    # Bun prints "Ran N tests across M files"; zero tests → no_tests. Parsed FIRST
    # (regardless of exit code) so neither a silent exit 0 nor a "No tests found!"
    # nonzero exit false-classifies. A bare "0 pass" is NOT used as a marker — a run
    # with "0 pass / 3 fail" is a real FAILURE, not an empty run. Gated on the absence
    # of a real nonzero "Ran N tests" summary — same run-evidence guard shape as the
    # jest/vitest/mocha classifiers above — so a genuinely-run suite whose own output
    # happens to echo the marker text isn't masked as an empty run.
    if _has(out, r"Ran 0 tests\b", r"error: No tests found!?") and not _has(
            out, r"Ran [1-9]\d* tests?\b"):
        return NO_TESTS
    if rc == 0:
        return PASSED
    # A parse error from Bun's own transpiler runs no tests (Bun does not type-check).
    if _has(out, r"\bSyntaxError\b",
            r"(?m)^error:\s+(?:Expected|Unexpected|Cannot parse|Parse error)"):
        return COMPILE_ERROR
    return FAILED


def _classify_deno(rc: int, out: str) -> str:
    # `deno test` type-checks + runs. A missing/unresolvable module or a failed remote
    # fetch is an environment error, not a test failure.
    if rc != 0 and _has(out, r"error: Module not found",
                        r"error: (?:Cannot|Could not) resolve",
                        r"error: Relative import path",
                        r"Cannot find module"):
        return ENV_ERROR
    # deno test EXITS 1 with "No test modules found" — a false-RED no_tests. A
    # zero-count summary (a filter that matched nothing) is also no_tests. Parsed
    # FIRST so the nonzero exit does not mask it as a failure. Gated on the absence
    # of a real nonzero "N passed"/"N failed" summary — same run-evidence guard
    # shape as the jest/vitest/mocha classifiers above — so a genuinely-run suite
    # whose own output happens to echo the marker text isn't masked as an empty run.
    if _has(out, r"No test modules found", r"\b0 passed\s*\|\s*0 failed\b") and not _has(
            out, r"\b[1-9]\d* passed\b", r"\b[1-9]\d* failed\b"):
        return NO_TESTS
    if rc == 0:
        return PASSED
    # A type-check / parse error before any test ran.
    if _has(out, r"TS\d+ \[ERROR\]", r"error: Type checking failed",
            r"The module's source code could not be parsed"):
        return COMPILE_ERROR
    return FAILED


# ---- Go / Rust ------------------------------------------------------------------

def _go_json_stream(out: str) -> bool:
    """True when `out` is test2json's OWN record stream (`go test -json`).

    Under -json every plain-text line the go tool would print (`=== RUN`, `--- PASS:`,
    `ok  pkg`, `FAIL  pkg`, `PASS`) is RE-ESCAPED into an `Output` string, so NONE of
    them sit at column 0 (verified on go1.26.5). A test that merely PRINTS a captured
    -json fixture runs in TEXT mode, so its OWN run decoration IS at column 0 — which is
    what disqualifies it here.

    The per-package RESULT line (`ok\tpkg` / `FAIL\tpkg`) is the load-bearing half: the
    go tool prints it for EVERY package in text mode, whereas the per-test decoration
    (`=== RUN` / `--- FAIL:`) is absent whenever the harness dies before or around
    `m.Run` — a `TestMain` that calls `os.Exit(1)` during setup prints no test markers at
    all, so the run decoration alone cannot prove text mode (verified on go1.26.5).
    """
    return not _has(out, r"(?m)^(?:=== (?:RUN|PAUSE|CONT)\b|--- (?:PASS|FAIL|SKIP):"
                         r"|ok\s|FAIL\b|PASS\r?$)")


#: A `go test -v` per-test REGION: `=== RUN/PAUSE/CONT/NAME <test>` opens it, and the
#: test's OWN stdout — which go echoes VERBATIM, at column 0 — runs until the test's
#: column-0 verdict (a SUBTEST verdict is indented, so a column-0 `--- PASS/FAIL/SKIP:`
#: is the top-level test's own). The package RESULT lines (`ok`/`FAIL`/`PASS`/`?`) also
#: close a region, defensively: a panicking test never prints a verdict, and leaving the
#: region open would swallow the REST of a `./...` run — hiding a real build failure (a
#: false NEGATIVE, the worse direction).
_GO_V_OPEN_RE = re.compile(r"^=== (?:RUN|PAUSE|CONT|NAME)\b")
_GO_V_CLOSE_RE = re.compile(r"^(?:---\s+(?:PASS|FAIL|SKIP|BENCH):|ok\s|FAIL\b|PASS\b|\?\s)")


def _go_tool_lines(out: str) -> str:
    """`out` with every `go test -v` TEST-BODY region stripped — the lines the go TOOL
    itself printed, not the ones a TEST printed.

    `go test -v` echoes a passing test's stdout verbatim at column 0 (verified on
    go1.26.5), so a tooling/snapshot test that prints CAPTURED build-failure text — a
    `# <pkg>` header AND a `file.go:line:col:` diagnostic, exactly what a `go build`
    transcript looks like — otherwise trips the compile_error predicate below and REDs
    a suite that PASSED (rc 0, every test green).

    Stripping the regions is what makes that predicate safe WITHOUT gating it on the
    exit code or on run-evidence absence — both of which would cost real coverage: go
    builds every package BEFORE running any test binary, so a genuine build failure's
    `# <pkg>` header is printed OUTSIDE any test region (verified go1.26.5: it is line
    1 of a `go test -v ./...` whose sibling package passes), and it therefore survives
    this strip. The predicate stays EXIT-CODE-INDEPENDENT — an exit-0 build failure
    (golang/go#64286) beside a PASSING sibling package still REDs, which an `rc != 0`
    or "no run evidence anywhere" gate would each have silently let through."""
    kept, in_test = [], False
    for line in (out or "").splitlines():
        if _GO_V_OPEN_RE.match(line):
            in_test = True
            kept.append(line)
            continue
        if in_test:
            if not _GO_V_CLOSE_RE.match(line):
                continue                    # a TEST's own stdout — not the go tool's
            in_test = False
        kept.append(line)
    return "\n".join(kept)


def _classify_go(rc: int, out: str) -> str:
    # A build failure and a test failure BOTH exit nonzero (the exact numeric code is
    # version/context-dependent — sources disagree 1 vs 2), so distinguish WHICH red a
    # red run is by markers, NOT by the exit code — but a text marker may only RE-classify
    # an ALREADY-red run (rc != 0), never turn a GREEN one red: `go test -v` echoes a
    # passing test's own stdout verbatim (verified on go1.26.5), so a tooling/snapshot
    # test that prints captured go output (`FAIL\tpkg [setup failed]`) exits 0 and an
    # unconditional marker check false-RED'd it. Every REAL build/setup failure exits
    # NONZERO (verified on go1.26.5: `[build failed]` rc=1, `[setup failed]` rc=1), and the
    # exit-0 golang/go#64286 case emits NO such marker at all — it is caught by the
    # header + column-0 diagnostic predicate below, which stays exit-code-independent.
    # `[setup failed]` is the LOAD-time counterpart of `[build failed]` (a bad import
    # path, an x.go/x_test.go package-name mismatch): go annotates the package that way
    # and may emit NO `file.go:line:col:` diagnostic at all, so the header+column-0
    # predicate below cannot see it.
    if rc != 0 and _has(out, r"\[build failed\]", r"\[setup failed\]",
                        r"build constraints exclude all Go files",
                        r"cannot find package", r"no required module provides package"):
        return COMPILE_ERROR
    # `"Action":"build-fail"` is the `go test -json` (Go 1.24+) equivalent: under -json,
    # build/setup diagnostics arrive as JSON `build-output` records, so the `# <pkg>`
    # header and the diagnostic are no longer at column 0 and that predicate misses them.
    # It stays EXIT-CODE-INDEPENDENT so a #64286-style exit-0 build failure under -json
    # still REDs. Two guards keep a test's own stdout out: test2json RE-ESCAPES it into an
    # `Output` string, so a test printing `{"Action":"build-fail"}` lands as
    # `\"Action\":\"build-fail\"` (the record must therefore be an UNESCAPED, column-0 JSON
    # object), and a test printing the record in TEXT mode leaves the go tool's own column-0
    # decoration behind — which `_go_json_stream` rejects. That second guard keys on the
    # per-package RESULT line (`ok`/`FAIL`), not just `=== RUN`/`--- FAIL:`: a harness whose
    # `TestMain` os.Exit()s before `m.Run` prints NO test markers, so a run-marker-only
    # predicate read its printed fixture as a real -json stream and RED'd a plain harness
    # FAILURE as compile_error.
    # The documented `FailedBuild` field (set on the package `fail` event) needs NO separate
    # alternative: cmd/go sets cfg.BuildJSON — which installs the JSONPrinter that emits
    # `build-fail` — and calls json.SetFailedBuild in the SAME else-branch of the SAME
    # `gotestjsonbuildtext` GODEBUG check at all three call sites (testflag.go:361,
    # test.go:1028/1507), so both signals landed together in Go 1.24 and can never appear
    # apart (verified go1.24.0 + go1.26.5 across broken-pkg / broken-dep / `[setup failed]` /
    # go#64286); the legacy `=1` mode emits NEITHER and falls through to the text predicates.
    # A FailedBuild-only stream would RED even so: go writes `FAIL\tpkg [build failed]` into
    # the SAME converter, so the `[build failed]` check above matches it inside `Output`.
    if _go_json_stream(out) and _has(out, r'(?m)^\{[^\n]*"Action":\s*"build-fail"'):
        return COMPILE_ERROR
    # A real compile/build error is reported by BOTH signals that a test's own output
    # never combines, NOT by run-evidence absence (which a passing SIBLING package
    # defeats in a `go test ./...` run):
    #   (a) `go build`/`go test` prints a `# <import-path>` header (`^#\s+\S`) before
    #       that package's diagnostics — a passing package never gets one, and neither a
    #       t.Log/t.Errorf decoration NOR a `fmt.Println` in a test body emits it;
    #   (b) the compiler locates the error at the START of a line with a full
    #       `file.go:line:COLUMN:` (round-6 18851642 widened `\.go:\d+:\d+:`→col-optional
    #       AND matched anywhere, which misrouted decorated failures + false-RED
    #       position-logging tests).
    # Requiring the header AND a column-0 diagnostic means neither a sibling package's
    # run-evidence, the overall exit code, NOR a test that merely prints/logs a
    # `file.go:line:col` string can mask or fake it — so golang/go#64286 (a test-less
    # package's build error may print NO per-package `[build failed]` annotation, so its
    # RED can't be inferred from that marker) still REDs beside a passing sibling, while
    # a decorated failure, a passing `-v` run, and a linter/codegen test that dumps a
    # diagnostic string all lack the `# pkg` header and fall through to FAILED / PASSED.
    # The path class is `[^\s:]+` (NO spaces): the diagnostic is the compiler's own
    # column-0 token and go emits it as a RELATIVE path (`./pkg/main.go:5:9:`), never a
    # spaced one — so a class that allowed spaces (round-5 a2ee13d408's `[^\n:]+`, on the
    # FALSE "go emits spaced paths" premise) let a `#`-header run false-RED when a
    # column-0 `.go:N:N:` string appeared in prose (`See handler.go:42:1:` on a PASSING
    # doc/lint run) AND misroute a failing test whose captured build path contained a
    # space (`/home/ci/My Project/broken/main.go:5:9:`) into compile_error. Keep it space-
    # excluding; do NOT re-widen.
    # Both halves are matched against the go TOOL's own lines (`_go_tool_lines`), NOT the
    # raw output: under `-v` go echoes a PASSING test's stdout verbatim at column 0, so a
    # tooling/snapshot test printing a captured `go build` transcript emits BOTH halves at
    # column 0 on an rc==0 all-green run and false-RED the gate. Scoping the SAME two
    # regexes to the tool's lines fixes that while keeping the predicate exit-code-
    # independent (see `_go_tool_lines`) — do NOT instead gate it on `rc != 0` or on
    # run-evidence absence: either would drop the exit-0 build failure beside a passing
    # sibling that this predicate exists to catch.
    tool_out = _go_tool_lines(out)
    if _has(tool_out, r"(?m)^#\s+\S") and _has(
            tool_out, r"(?m)^(?:[a-zA-Z]:)?[^\s:]+\.go:\d+:\d+:"):
        return COMPILE_ERROR
    # go test exits 0 for BOTH all-pass AND a zero-test run — parse the marker, and
    # call it no_tests only when NOTHING actually ran. RUN EVIDENCE is an UNANNOTATED
    # column-0 `ok` line (`_GO_OK_RAN`) or a `--- PASS`, and deliberately NOT a bare
    # `^ok\s` / `^PASS\b`: `go test -run Nomatch ./...` filters every test out yet still
    # exits 0 printing `ok  pkg 0.5s [no tests to run]` at column 0 (a `_test.go`-less
    # package prints `[no test files]`), and the binary prints a column-0 `PASS` even at
    # zero tests — so a blind `^ok`/`^PASS` read those zero-test runs as evidence and
    # false-GREENED them (verified go1.26.5). The evidence scan stays WHOLE-OUTPUT so a
    # `go test ./...` where a SIBLING package really ran stays GREEN beside an empty one.
    if rc == 0:
        if _has(out, r"\[no test files\]", r"no test files", r"\[no tests to run\]",
                r"testing: warning: no tests to run") and not _has(
                out, "(?m)" + _GO_OK_RAN, r"(?m)^---\s+PASS"):
            return NO_TESTS
        return PASSED
    return FAILED


def _classify_cargo(rc: int, out: str) -> str:
    # cargo test: exit 101 is BOTH a compile failure AND a test failure. A TRUE
    # compile failure never runs a test binary, so it has NO "test result:" summary
    # line. rustc diagnostics ("error[E…]", "could not compile") legitimately appear
    # in TEST output too — trybuild/compiletest UI tests and snapshot tests assert on
    # diagnostic strings — so a diagnostic substring alone is NOT proof of a compile
    # failure. Require the diagnostic markers AND the absence of any "test result:"
    # line; otherwise the crate compiled and the tests ran (pass/fail by exit code).
    # This also prevents a passing `--nocapture` run that echoes a diagnostic from
    # false-reding the gate.
    if not _has(out, r"test result:") and _has(
            out, r"error\[E\d+\]", r"could not compile", r"error: could not compile",
            r"aborting due to \d+ previous error"):
        return COMPILE_ERROR
    if rc == 0:
        # cargo runs MANY binaries (each crate's unit tests, every integration file,
        # Doc-tests), each printing its own "running N tests" line. A genuinely empty
        # run has a "running 0 tests" line AND no "running [1-9]… tests" line anywhere
        # (0 executed across every binary + doctests). Keying on the "running" count —
        # not "test result: ok." — is the robust discriminator.
        if _has(out, r"running 0 tests") and not re.search(r"running [1-9]\d* tests?", out):
            return NO_TESTS
        return PASSED
    return FAILED


def _classify_nextest(rc: int, out: str) -> str:
    # cargo-nextest uses DISTINCT exit codes: 4 = no tests to run, 100 = test
    # failures, 101 = a build/other error.
    if rc == 0:
        return PASSED
    if rc == 4:
        return NO_TESTS
    if rc == 100:
        return FAILED
    if rc == 101:
        return COMPILE_ERROR
    if _has(out, r"error\[E\d+\]", r"could not compile"):
        return COMPILE_ERROR
    return FAILED


# ---- JVM ------------------------------------------------------------------------

def _classify_maven(rc: int, out: str) -> str:
    if _has(out, r"COMPILATION ERROR", r"Compilation failure"):
        return COMPILE_ERROR
    if rc == 0:
        # Surefire prints "Tests run: 0" per module when nothing ran.
        if _has(out, r"Tests run:\s*0\b", r"No tests to run") and not re.search(
                r"Tests run:\s*[1-9]", out):
            return NO_TESTS
        return PASSED
    if _has(out, r"BUILD FAILURE") and not _has(out, r"There (?:are|were) test failures",
                                                r"Failed tests:", r"Tests run:.*Failures: [1-9]"):
        # A BUILD FAILURE with no test-failure summary is a build/config error.
        return COMPILE_ERROR
    return FAILED


# Leading camelCase VERBS of Gradle / Android-Gradle-Plugin task names that are
# NOT test-execution tasks even though the name ends in "…Test" / "…AndroidTest": the
# AGP unit-test variant generates COMPILE / PACKAGE / PROCESS support tasks whose names
# end in "Test" (`javaPreCompileDebugUnitTest`, `packageDebugUnitTestForUnitTest`,
# `packageDebugAndroidTest`). A genuine Test-TYPE execution task never leads with one of
# these verbs — `test`, `integrationTest`, `connectedDebugAndroidTest`, a user's
# `fooTest` don't. The leading word is matched EXACTLY (a camelCase boundary), so a user
# task like `checkoutFlowTest` (leading word "checkout", not "check") is still a test task.
_GRADLE_NON_TEST_TASK_VERBS = frozenset({
    "compile", "process", "package", "merge", "generate", "bundle", "assemble",
    "java", "kapt", "ksp", "dex", "map", "extract", "transform", "strip",
    "desugar", "lint", "jacoco", "sync", "pre", "collect", "write", "create",
    "parse", "optimize", "verify", "install", "copy", "prepare", "bind",
})


def _is_gradle_test_execution_task(name: str) -> bool:
    """True when a Gradle task NAME (the last ``:`` segment) is a Test-TYPE EXECUTION
    task — the only task whose status is test-run evidence.

    Matches `test` / `integrationTest` / a user `fooTest` / AGP `testDebugUnitTest` /
    `connectedDebugAndroidTest` — and their plural forms (`tests`, `integrationTests`,
    `unitTests`): task names are user-defined and not required to end in singular
    "Test". Rejects the COMPILE / RESOURCE / LIFECYCLE tasks Gradle prints around the
    real one — including the AGP support tasks that merely END in "Test"
    (`javaPreCompileDebugUnitTest`, `packageDebugUnitTestForUnitTest`) — by their
    leading verb, and `compileTestJava` / `testClasses` / `processTestResources` by
    their non-"Test"/"Tests" suffix. No AGP support task name ends in the plural
    "Tests", so admitting that suffix does not re-admit them. Keying the no-tests
    decision on a broad `\\S*[Tt]est\\S*` (or even `endswith("Test")` alone) name-match
    is what shipped a FALSE-GREEN: those support tasks execute with a bare header, so
    their `ran`-evidence discards the genuine `:testDebugUnitTest NO-SOURCE` and a
    zero-test Android module greens.

    Known blind spot, NOT fixable from console text alone: a `Test`-TYPE task
    registered under a name with NO lexical relation to "test" at all (Groovy
    `tasks.register("integration", Test)`) is invisible to this — and any —
    name-based rule, so a zero-source `:integration NO-SOURCE` run (if it is the
    only detected execution task) falls through `_classify_gradle` to PASSED. Gradle's
    default (non-`--info`) console never prints a task's TYPE, only its NAME and
    STATUS, so distinguishing an arbitrary-named Test task from an arbitrary-named
    Copy/JavaCompile/etc. task that also prints NO-SOURCE needs task-registration
    info this pure text classifier does not have. Broadening the match to catch it
    (e.g. treating any unrecognized NO-SOURCE task name as evidence) would re-admit
    the COMPILE/PACKAGE/PROCESS support tasks above — reintroducing the exact
    false-green this function exists to prevent — so it is left unhandled rather
    than guessed at."""
    if name in ("test", "tests"):
        return True
    if not (name.endswith("Test") or name.endswith("Tests")):
        return False
    m = re.match(r"[a-z]+", name)          # the leading camelCase word
    return (m.group(0) if m else "") not in _GRADLE_NON_TEST_TASK_VERBS


def _classify_gradle(rc: int, out: str) -> str:
    # A bare "error: " is too broad — any test that logs that literal substring on an
    # otherwise-green build would false-red. Anchor to the javac/scalac diagnostic
    # shape (`File.java:N: error:`); Kotlin failures don't use this shape at all
    # (kotlinc emits "e: file: (line, col): …") and are still caught by the Task
    # FAILED / Compilation failed markers above.
    if _has(out, r"Compilation failed", r"> Task :compile\w* FAILED",
            r"(?m)^\s*(?:[a-zA-Z]:)?[^:\n]+\.(?:java|kt|scala):\d+:\s*error:"):
        return COMPILE_ERROR
    if rc == 0:
        # Zero-test evidence comes from the test EXECUTION task's own status line and
        # nothing else (see `_is_gradle_test_execution_task`). Gradle prints a chain of
        # COMPILE / RESOURCE / LIFECYCLE tasks around the real one, and a broad name-match
        # sweeps them in — which is exactly what shipped a FALSE-GREEN. A zero-test
        # project prints:
        #     > Task :compileTestJava NO-SOURCE
        #     > Task :processTestResources NO-SOURCE
        #     > Task :testClasses UP-TO-DATE          ← a LIFECYCLE task (no actions):
        #     > Task :test NO-SOURCE                    UP-TO-DATE, NEVER NO-SOURCE
        # and an Android module additionally prints support tasks that END in "Test" and
        # execute with a bare header (`javaPreCompileDebugUnitTest`,
        # `packageDebugUnitTestForUnitTest`). Both defeat a name-match rule — the first by
        # never being NO-SOURCE, the second by looking like a task that "ran". Only the
        # `:test` / `:integrationTest` / `:testDebugUnitTest` EXECUTION task's own status
        # counts.
        # Evidence is read from the persisted `> Task :<name> <STATUS>` lines. The gate
        # captures output with a pipe (non-TTY), so Gradle's default `--console=auto`
        # resolves to PLAIN and prints exactly these lines — verified against real Gradle
        # 8.11.1. (Inherent blind spot, NOT reachable on the default path: a user who
        # forces `org.gradle.console=rich` gets output that persists NO `> Task` status
        # line at all, so a zero-test project is unclassifiable from console text and
        # falls through to PASSED. Catching that needs `build/test-results/**/*.xml`,
        # outside this pure function; do NOT fake it with an ANSI strip.)
        ran, statuses = False, []
        for task, status in re.findall(
                r"(?m)^> Task (:\S+)(?:[ \t]+(\S[^\n]*?))?[ \t]*$", out):
            if not _is_gradle_test_execution_task(task.rsplit(":", 1)[-1]):
                continue                       # not an execution task — no evidence
            st = (status or "").strip().upper()
            statuses.append(st)
            if st not in ("NO-SOURCE", "UP-TO-DATE", "SKIPPED"):
                ran = True                     # no status / FROM-CACHE → suite had tests
        # A real run anywhere WINS: `gradle check` / `gradle test integrationTest` can
        # have one empty execution task beside a sibling that really ran, and that is a
        # verified pass. Otherwise, NO_TESTS requires EVERY detected execution task to be
        # zero-execution — NO-SOURCE (no test source) or SKIPPED (task skipped, e.g. an
        # `onlyIf` predicate / `-x`; its actions never ran, so zero tests executed and
        # there is NO prior-pass evidence). A `:test NO-SOURCE` beside a `:integrationTest
        # SKIPPED` executed NOTHING, so reporting PASSED would false-green an untested
        # build — exactly the class this module exists to catch. UP-TO-DATE is EXCLUDED:
        # it means Gradle's up-to-date check ran, which only happens for a task that HAS
        # source (a sourceless task short-circuits straight to NO-SOURCE) — so a
        # NO-SOURCE `:test` beside an UP-TO-DATE `:integrationTest` is a real,
        # previously-verified suite that stays PASSED.
        if ran:
            return PASSED
        if statuses and all(st in ("NO-SOURCE", "SKIPPED") for st in statuses):
            return NO_TESTS
        if _has(out, r"No tests found for given includes"):
            return NO_TESTS
        return PASSED
    # Gradle `--tests <pattern>` with no match FAILS exit 1 ("No tests found for
    # given includes"). P2 leaves that as a plain failure — P6 handles the nuance.
    return FAILED


# ---- .NET / Elixir --------------------------------------------------------------

def _classify_dotnet(rc: int, out: str) -> str:
    # A build error precedes the test run. `Build FAILED` is the dotnet CLI's own
    # summary banner — a green run can never legitimately print it, so it stays
    # unconditional. The `error CS\d+` / `error MSB\d+` diagnostic substrings are
    # rc-gated: a green run (rc 0) can legitimately emit that literal text as test
    # OUTPUT (e.g. a Roslyn analyzer/source-generator test asserting on the exact
    # compiler diagnostic string) and must never false-red — same invariant as the
    # abort branch below.
    if _has(out, r"Build FAILED"):
        return COMPILE_ERROR
    if rc != 0 and _has(out, r"error CS\d+", r"error MSB\d+"):
        return COMPILE_ERROR
    # Microsoft Testing Platform (opt-in): 8 = no tests, 2 = test failure.
    if rc == 8:
        return NO_TESTS
    # A VSTest ABORT (test-host crash / run aborted) is a real failure, not an empty
    # run — and it emits NO per-test failure lines, so in a multi-project solution it
    # can land beside a sibling empty project's "No test is available…" marker and slip
    # through the no-failure-evidence gate below into NO_TESTS (microsoft/vstest#2952).
    # Kept rc-gated so a test that merely PRINTS an abort string on a green run (rc 0)
    # can never false-red.
    if rc != 0 and _has(out, r"Test Run Aborted", r"active test run was aborted",
                        r"Testhost process exited", r"test host process crashed"):
        return FAILED
    # Classic VSTest EXITS 0 on zero tests (a silent false-green) — so honor the
    # no-tests markers ONLY at rc==0. A NONZERO exit whose ONLY signal is a no-tests
    # marker is NOT a benign empty run: a repo with no tests exits 0 by DEFAULT (stays
    # NO_TESTS green-skip below), so nonzero-exit-with-only-a-no-tests-marker arises
    # ONLY via opt-in <TreatNoTestsAsError>true</TreatNoTestsAsError> (the dev chose
    # fail-on-empty) or a BROKEN test discovery (missing adapter / TFM mismatch /
    # unloadable dll) — both are real errors, so they must fall through to FAILED, not
    # be masked green (round-1 1bd706113d dropped this rc==0 gate → red→green masking).
    # The reliable markers are "No test is available in <assembly>" (nothing discovered)
    # and "no test matches the specified selection criteria" (a filter matched
    # nothing) — always safe regardless of project count. The "Total: 0" / "Passed: 0"
    # summary markers are per-PROJECT lines: `dotnet test` on a multi-project solution
    # prints one summary line per project, so a solution with one empty project and one
    # passing project emits BOTH "Total: 0" (the empty project) and "Total: 3" (the
    # project with real tests) at rc==0. Gate those two markers on the absence of a
    # nonzero count ANYWHERE in the output — same guard shape as _classify_swift below —
    # so a passing multi-project run can never be masked NO_TESTS by a sibling empty
    # project's zero-count trailer.
    if rc == 0 and _has(out, r"No test is available",
                        r"no test matches the specified selection criteria"):
        return NO_TESTS
    if rc == 0 and _has(out, r"Total:\s*0\b",
                        r"Passed!\s*-\s*Failed:\s*0,\s*Passed:\s*0") and not _has(
            out, r"Total:\s*[1-9]\d*\b", r"Passed!\s*-\s*Failed:\s*\d+,\s*Passed:\s*[1-9]"):
        return NO_TESTS
    if rc == 0:
        return PASSED
    return FAILED


def _classify_mix(rc: int, out: str) -> str:
    # Elixir: exit 2 = test failures; exit 1 = a compile error OR zero tests — parse
    # the "N tests, M failures" summary.
    if _has(out, r"\(CompileError\)", r"== Compilation error", r"could not compile"):
        return COMPILE_ERROR
    if _has(out, r"\b0 tests,\s*0 failures", r"There are no tests to run"):
        return NO_TESTS
    if rc == 0:
        return PASSED
    if rc == 1:
        # rc 1 with a normal "N tests, M failures" line but no compile marker: a
        # non-zero exit from Elixir's runner is a failure; a bare compile with no
        # summary is compile_error.
        if _has(out, r"\d+ tests?,\s*\d+ failures?"):
            return FAILED
        return COMPILE_ERROR
    return FAILED


# ---- Ruby / PHP -----------------------------------------------------------------

def _classify_rspec(rc: int, out: str) -> str:
    # A spec-load error (outside examples) is a compile-class error.
    if _has(out, r"An error occurred while loading", r"cannot load such file",
            r'"errors_outside_of_examples_count"\s*:\s*[1-9]'):
        return COMPILE_ERROR
    if _has(out, r"\b0 examples,\s*0 failures", r'"example_count"\s*:\s*0\b'):
        return NO_TESTS
    if rc == 0:
        return PASSED
    return FAILED


def _classify_phpunit(rc: int, out: str) -> str:
    if _has(out, r"PHP (?:Parse|Fatal) error", r"Class .* not found",
            r"Cannot open file"):
        return COMPILE_ERROR
    if _has(out, r"No tests executed!", r"No tests found"):
        return NO_TESTS
    if rc == 0:
        return PASSED
    return FAILED


def _classify_minitest(rc: int, out: str) -> str:
    if _has(out, r"\b0 runs,\s*0 assertions", r"0 tests,\s*0 assertions"):
        return NO_TESTS
    if _has(out, r"(?:LoadError|SyntaxError|NameError):", r"cannot load such file"):
        return COMPILE_ERROR
    if rc == 0:
        return PASSED
    return FAILED


# ---- Native / mobile ------------------------------------------------------------

def _classify_catch2(rc: int, out: str) -> str:
    # Catch2 (>= v3): 42 = tests failed, 2 = no tests ran, 4 = all skipped.
    if rc == 0:
        return PASSED
    if rc == 2:
        return NO_TESTS
    if rc == 4:
        return NO_TESTS           # all skipped — nothing actually executed
    if rc == 42:
        return FAILED
    if _has(out, r"No test cases matched", r"No tests ran"):
        return NO_TESTS
    return FAILED


def _classify_gtest(rc: int, out: str) -> str:
    # GoogleTest EXITS 0 when zero tests match a filter unless
    # --gtest_fail_if_no_test_selected is set — parse the "0 tests" banner. Gated on
    # the absence of a real "[  PASSED/FAILED  ] N tests" summary (nonzero N) so a
    # suite that actually ran and failed — but whose OWN output happens to echo a
    # zero-tests marker (e.g. a wrapper/snapshot test quoting another empty run) —
    # can't be masked as an empty run. Same run-evidence guard shape as the
    # jest/vitest/mocha/jasmine classifiers above.
    if _has(out, r"No tests were found", r"0 tests from 0 test (?:suites|cases)",
            r"\[  PASSED  \] 0 tests", r"Running 0 tests") and not _has(
            out, r"\[  (?:PASSED|FAILED)  \] [1-9]"):
        return NO_TESTS
    if _has(out, r"\[  FAILED  \]") and rc == 0:
        # defensive: a FAILED banner with rc 0 shouldn't happen, but never green it.
        return FAILED
    if rc == 0:
        return PASSED
    return FAILED


def _classify_ctest(rc: int, out: str) -> str:
    # ctest EXITS 0 on "No tests were found!!!" unless --no-tests=error. Parse it.
    if _has(out, r"No tests were found!!!", r"No tests were found",
            r"Total Test time.*\n?\s*0 tests"):
        return NO_TESTS
    if rc == 0:
        return PASSED
    if _has(out, r"Errors while running CTest", r"Failed to compile", r"Build error"):
        return COMPILE_ERROR
    return FAILED


def _classify_swift(rc: int, out: str) -> str:
    # A run in which tests actually executed ALWAYS prints a suite/execution banner.
    # Its presence gates the two "nothing ran" branches below, so a real FAILURE whose
    # assertion diff merely QUOTES a no-tests marker (a tool snapshot-testing its own
    # no-tests copy) cannot short-circuit to a green no_tests / compile SKIP. The count
    # banners tolerate the SINGULAR ("Executed 1 test" / "Test run with 1 test") — the
    # `s?` — else a one-test run yields no banner and false-greens; the glyph line
    # (`◇`/`✔`/`✘` "Test … started/passed/failed") is Swift Testing's per-test recorder.
    has_run_banner = _has(out, r"(?m)^\s*Test Suite\b", r"Executed \d+ tests?\b",
                          r"Test run with \d+ tests?\b",
                          r"(?m)^\s*[◇✔✘✓✗]\s+Test\b")
    # WHOLE-RUN "nothing to run" markers — a package with no test target (SwiftPM
    # `testsNotFound`, emitted as an rc=1 `error: no tests found; create a target`) or
    # a global filter that matched nothing. A genuine no-target / no-match run has NO
    # banner, so gating on its absence is safe AND honors these at ANY exit code
    # (they can't co-occur with a real failure) — checked BEFORE the compile branch,
    # whose broad `^error:` marker would else mis-swallow the "error: no tests found".
    if not has_run_banner and _has(out, r"No matching test cases were run",
                                   r"no tests found; create a target"):
        return NO_TESTS
    # A compile/build error before any test suite ran prints NO summary line at all.
    if (rc != 0 and not has_run_banner
            and _has(out, r"\.swift:\d+(?::\d+)?: error:", r"error: no such module",
                     r"(?m)^\s*error:", r"error: build failed")):
        return COMPILE_ERROR
    if rc == 0:
        # PER-SUITE / swift-testing COUNT markers, gated on rc==0 (a real failure exits
        # nonzero — the P2b defect-3 false-green). A zero-count marker means no_tests
        # ONLY when NOTHING ran in EITHER framework: `swift test` runs XCTest AND
        # swift-testing and co-emits the UNUSED framework's "0 tests" trailer beside the
        # used one, so an XCTest-only or swift-testing-only PASS (the default
        # `swift package init` template) must NOT be mislabeled no_tests when a NONZERO
        # count is present anywhere.
        if _has(out, r"Executed 0 tests", r"Test run with 0 tests") and not _has(
                out, r"Executed [1-9]\d* tests?", r"Test run with [1-9]\d* tests?"):
            return NO_TESTS
        return PASSED
    return FAILED


def _classify_dart(rc: int, out: str) -> str:
    # dart/flutter test exit 1 for BOTH a test failure AND "No tests ran" — parse
    # the marker. Gated on the absence of a real pass/fail summary ("All tests
    # passed"/"Some tests failed"/a nonzero "+N"/"-N" run counter) so a suite that
    # actually ran and failed — but whose OWN output happens to echo a no-tests
    # marker (e.g. a test asserting on captured subprocess text) — can't be masked
    # as an empty run. Same run-evidence guard shape as the jest/vitest/mocha/
    # jasmine classifiers above.
    if _has(out, r"No tests ran", r"No tests match", r"Found no tests") and not _has(
            out, r"All tests passed", r"Some tests failed",
            r"\+[1-9]\d*(?:\s+-\d+)?:"):
        return NO_TESTS
    if rc == 0:
        return PASSED
    if _has(out, r"Error: .*\.dart:\d+", r"Compilation failed", r"(?m)^Error when reading"):
        return COMPILE_ERROR
    return FAILED


# ---- dispatch table -------------------------------------------------------------

_CLASSIFIERS = {
    PYTEST: _classify_pytest,
    UNITTEST: _classify_unittest,
    DJANGO: _classify_django,
    JEST: _classify_jest,
    VITEST: _classify_vitest,
    MOCHA: _classify_mocha,
    JASMINE: _classify_jasmine,
    KARMA: _classify_karma,
    NODE_TEST: _classify_node_test,
    AVA: _classify_ava,
    BUN: _classify_bun,
    DENO: _classify_deno,
    GO: _classify_go,
    CARGO: _classify_cargo,
    NEXTEST: _classify_nextest,
    MAVEN: _classify_maven,
    GRADLE: _classify_gradle,
    MIX: _classify_mix,
    DOTNET: _classify_dotnet,
    RSPEC: _classify_rspec,
    MINITEST: _classify_minitest,
    PHPUNIT: _classify_phpunit,
    PEST: _classify_phpunit,      # pest wraps phpunit → identical exit/marker rules
    CTEST: _classify_ctest,
    GTEST: _classify_gtest,
    CATCH2: _classify_catch2,
    SWIFT: _classify_swift,
    DART: _classify_dart,
    FLUTTER: _classify_dart,      # flutter test shares dart's exit/marker semantics
    # TOX / NOX / UNKNOWN → _classify_generic (opaque Tier-B wrappers).
}


# ---- module boundary ------------------------------------------------------------
# Detection + classification is this module's ENTIRE surface, and the byte-parity
# envelope shared across the product trees ends at this line. The failure-triage
# layer other trees carry below it (failing-id extraction, scoped re-run) is
# deliberately absent here: this free skill's gate escalates on red, nothing more.
