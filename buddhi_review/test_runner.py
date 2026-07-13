"""test_runner.py — runner detection + outcome classification for the polyglot
test gate (P2). The SOLE owner of the "what runner is behind the gate command, and
what did its exit mean?" question.

Two responsibilities, and deliberately NO triage layer (no failing-id extraction,
no scoped re-run — this free skill's gate escalates on red, nothing more):

  detect_runner(cwd, resolved_cmd) -> RunnerInfo
      Identify the runner behind the gate command, from (in priority order) the
      resolved command's argv[0]/shape, the STRING inside a `bash -lc` wrapper, then
      repo markers. The gate wraps every command carrying shell syntax in `bash -lc`
      (`commit_push._split_test_command`: a `cd <dir>` step, a `VAR=val` prefix, an
      `&&` chain), so the runner is read back OUT of that string — else the most
      ordinary gate commands there are would all be opaque. An unrecognized argv
      (`npm test`, `./run-tests.sh`, `make`) or a shell string naming no runner we
      know stays an opaque wrapper -> runner=UNKNOWN (and `classify` still holds the
      no-false-green line there); a `tox.ini [tox]` / `noxfile.py` forces tox/nox (a
      Tier-B wrapper) over the tox.ini->pytest signal. Pure / read-only (no network,
      no installs); the ONLY subprocess it runs is a bounded `cargo nextest --version`
      probe to tell nextest from stable cargo, which the design explicitly permits.

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

import os
import re
import shlex
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple, Optional, Union


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

#: The classes that RED the gate but must NEVER be fed to failing-id parsing (P3
#: reads this to route them away from triage). `failed`/`timeout` red too, but they
#: MAY carry ids.
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

#: Recognized runners for which a per-runner test id can be extracted AND an
#: exact-subset re-run is feasible (P1's "scopable iff argv AND recognized" rule).
#: A best-effort forward hint for P3 — P2 does not itself consume `scopable`. The
#: Tier-B wrappers/whole-suite-degrade runners are deliberately excluded.
_NON_SCOPABLE = frozenset({
    UNKNOWN, TOX, NOX, CARGO, JASMINE, KARMA, AVA, NODE_TEST, MINITEST,
})


class RunnerInfo(NamedTuple):
    """The identified runner behind a gate command.

    runner   — one of the runner-id constants above, or UNKNOWN for an opaque
               wrapper (`npm test`, `./run-tests.sh`, `make`, a `bash -lc` string
               naming no runner we recognize) we cannot see through.
    scopable — best-effort hint: is this a recognized, scoped-rerun-capable runner?
               (P1's rule; consumed by P3, not P2.) UNKNOWN, the Tier-B wrappers and
               ANY shell-wrapped runner (source "shell") are never scopable.
    source   — how it was identified ("argv", "shell" — read out of a `bash -lc`
               string, "wrapper:shell", "wrapper:unrecognized", "marker:<file>",
               "none") — for logging/tests.
    """
    runner: str
    scopable: bool
    source: str


def _mk(runner: str, source: str) -> RunnerInfo:
    return RunnerInfo(runner=runner,
                      scopable=(runner not in _NON_SCOPABLE),
                      source=source)


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


#: Interpreter options that consume a SEPARATE following argument (`-X dev`,
#: `-W ignore`) — case-sensitive since `-x` (skip the `#!` line) is a distinct,
#: argument-less flag from `-X` (set an implementation option).
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


def _runner_from_argv(argv: list) -> Optional[str]:
    """Identify a runner from a bare (non-shell-wrapped) argv, or None when argv[0]
    is not a recognized runner. Handles the `python -m <mod>` form, multi-token
    shapes (`cargo nextest run`, `go test`, `ng test`), and launcher prefixes."""
    if not argv:
        return None
    toks = _strip_launchers(argv)
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

#: A leading `VAR=val` environment prefix on a command (`CI=1 npx jest`). Mirrors
#: `commit_push._command_needs_shell`'s own env-prefix rule — which is one of the
#: reasons a command gets wrapped in `bash -lc` in the first place.
_ENV_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


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


def _strip_env_prefix(toks: list) -> list:
    """Drop a leading `VAR=val` env prefix (`CI=1 NODE_ENV=test npx jest`) so the
    runner token comes first. Returns a possibly-shorter copy; never mutates."""
    i = 0
    while i < len(toks) and _ENV_PREFIX_RE.match(str(toks[i])):
        i += 1
    return toks[i:]


def _runner_from_shell_string(cmd: str) -> Optional[str]:
    """Best-effort: the runner behind a `bash -lc "<cmd>"` wrapper, read out of the
    shell STRING. None when the string names no runner we recognize, or names more
    than one.

    This exists because `commit_push._split_test_command` wraps a LOT of ordinary gate
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
    # A package.json with none of the above: the test command is an opaque npm
    # script (P10 unwraps it). Not indeterminate JS → return None so the caller
    # falls through / reports UNKNOWN.
    return None


def detect_runner(cwd: Optional[str], resolved_cmd: Optional[Union[list[str], str]]) -> RunnerInfo:
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
#: source of truth, because the two MUST agree on what counts as evidence.
_GO_OK_RAN = r"^ok\s+(?![^\n]*\[no tests? (?:to run|files)\])"

#: Zero-test markers UNIONED across every per-runner classifier below — the only
#: signal `_classify_generic` has when the runner is opaque. Not every runner is
#: string-detectable (pytest signals an empty run by exit code 5 ALONE, so a wrapper
#: hiding pytest keeps the pre-F2 posture), but every silent-exit-0 runner is: those
#: are precisely the ones that would otherwise false-green.
_NO_TESTS_MARKERS_ANY = re.compile(
    r"\[no tests? (?:to run|files)\]|testing: warning: no tests to run"  # go
    r"|^Ran 0 tests\b"                                                   # unittest/django
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
    r"|Found no tests",                                                  # dart
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
    a loud "zero coverage, not green" SKIP that never blocks the push
    (`commit_push._emit_no_tests_skip`), whereas the error it replaces — reporting a
    zero-test run as a verified GREEN — is the one this module must never make.
    env/timeout are already handled by `classify`."""
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
    # go/cargo/maven/dotnet classifiers above.
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
    # absence of a real "Ran N tests" (nonzero) summary — same run-evidence guard
    # the unittest classifier above uses — so a suite that actually ran and failed,
    # but whose own output happens to echo "Ran 0 tests" (e.g. a test asserting on
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
    if rc == 0:
        return PASSED
    if _has(out, r"error TS\d+", r"SyntaxError"):
        return COMPILE_ERROR
    return FAILED


def _classify_jasmine(rc: int, out: str) -> str:
    # jasmine EXITS 0 on "No specs found" — the canonical silent-green class. The
    # marker MUST be parsed or a zero-test run false-greens. Gated on the absence of a
    # real "N specs, M failures" summary (nonzero N) so a genuinely-run suite whose OWN
    # failure output happens to quote that marker text (e.g. a spec asserting on a
    # CLI's own "No specs found" message) can't be misread as an empty run — same
    # run-evidence guard shape as the jest classifier above.
    if _has(out, r"No specs found", r"Incomplete: No specs found",
            r"\b0 specs,\s*0 failures") and not _has(out, r"\b[1-9]\d* specs?,"):
        return NO_TESTS
    if rc != 0 and _js_env(out):
        return ENV_ERROR
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
    # column-0 `ok` line (`_GO_OK_RAN`) or a `--- PASS`, and deliberately NOT:
    #   * a bare `^ok\s` — `go test -run Nomatch ./...` filters every test out yet
    #     still exits 0 printing `ok  pkg 0.5s [no tests to run]` at column 0 (and a
    #     `_test.go`-less package prints `[no test files]`), so a blind `^ok` read
    #     those zero-test runs as evidence and false-GREENED them (verified go1.26.5);
    #   * a bare `^PASS\b` — the test binary prints a column-0 `PASS` under `-v` even
    #     when zero tests ran (`testing: warning: no tests to run` / `PASS` / `ok pkg
    #     [no tests to run]`, verified go1.26.5), so `PASS` proves only that the binary
    #     did not fail, NOT that anything executed.
    # The evidence check stays a WHOLE-OUTPUT scan so a package that genuinely ran
    # keeps a `go test ./...` run GREEN when a SIBLING package is empty (the common
    # `?  pkg [no test files]` beside `ok  pkg 0.2s` shape).
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
        # NO-SOURCE is a generic Gradle task-outcome marker, not test-specific — it
        # also prints for e.g. `> Task :processResources NO-SOURCE` on any project
        # with no src/main/resources, even when the test task ran and passed. Anchor
        # to test-NAMED tasks' own status lines, and require EVERY matched test task
        # to be NO-SOURCE: a multi-task invocation (`gradle check`, `gradle test
        # integrationTest`) can have one empty test task print NO-SOURCE beside a
        # sibling that actually ran — normal Gradle console output never prints a
        # "BUILD SUCCESSFUL ... N test" count to use as counter-evidence, so keying
        # off a single NO-SOURCE line would false the gate into NO_TESTS despite real
        # tests executing.
        test_task_statuses = re.findall(
            r"(?m)^> Task :\S*[Tt]est\S*(?:[ \t]+(\S[^\n]*))?[ \t]*$", out)
        if test_task_statuses:
            if all((status or "").strip().upper() == "NO-SOURCE"
                    for status in test_task_statuses):
                return NO_TESTS
            return PASSED
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
