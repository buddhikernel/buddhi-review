"""F2 — runner detection + the no-tests/exit/compile/env classifier for the free
skill's pre-push test gate.

``buddhi_review/test_runner.py`` is the SOLE owner of "what runner is behind the
gate command, and what did its exit mean?" These tests pin:

  1. The classifier as a TABLE — every runner, at least {pass, fail, no-tests} (plus
     compile/env where the runner has them), asserting the exact class. The
     silent-exit-0 set (jasmine / Karma / go / VSTest / gtest / swift / cargo) is
     called out explicitly — a zero-test run of any of them must classify `no_tests`,
     never `passed`, because that false-green is the whole reason the classifier
     exists. Plus pytest exit 5, the unittest ≤3.11-vs-≥3.12 split, and env markers.
  2. Detection — argv shapes + marker-file fixtures per runner (tmp dirs, no real
     toolchains), including the tox.ini/noxfile force-guard, the runner read back out
     of a `bash -lc` STRING (the gate wraps every `cd …` / `VAR=… ` / `&&` command
     that way, so argv[0] alone would call them all opaque), and the genuinely opaque
     wrapper (`npm test`, `make`) → UNKNOWN — which the generic classifier still
     refuses to false-green on a zero-test run.
  3. Gate wiring in ``commit_push.run_test_gate`` — `no_tests` SKIPs (never red) with
     the loud notice; `env_error` / `compile_error` RED with the class named in the
     tail; pytest exit 5 is a SKIP, not a red gate; and the free skill's
     no-detectable-suite "no gate" SKIP posture is unchanged by the classifier wiring.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from buddhi_review import commit_push
from buddhi_review import test_runner as tr


def _silent(*a, **k):
    return ""


# ── REAL Gradle console output ───────────────────────────────────────────────────
# Verbatim `gradle test` / `gradle check` output, lifecycle tasks INCLUDED. Gradle
# always prints the compile / resource / lifecycle tasks around the `:test` EXECUTION
# task, and their statuses say NOTHING about whether tests ran:
#
#   > Task :compileTestJava NO-SOURCE      ← compile task: no test sources to compile
#   > Task :processTestResources NO-SOURCE ← resource task
#   > Task :testClasses UP-TO-DATE         ← LIFECYCLE task: it has no actions, so it is
#                                            UP-TO-DATE — it is NEVER "NO-SOURCE"
#   > Task :test NO-SOURCE                 ← the EXECUTION task — the ONLY zero-test evidence
#
# A classifier keyed on a `\S*[Tt]est\S*` NAME-match sees all four and, requiring every
# match to be NO-SOURCE, is defeated by `testClasses UP-TO-DATE` — so the empty project
# falls through to `passed` and the gate goes GREEN on ZERO tests. That is the exact
# false-green these fixtures exist to pin. Every Gradle fixture here is real output; a
# 1–3 line synthetic (written to the regex instead of to Gradle) is what let it ship.

_GRADLE_ZERO_TEST = """> Task :compileJava UP-TO-DATE
> Task :processResources NO-SOURCE
> Task :classes UP-TO-DATE
> Task :compileTestJava NO-SOURCE
> Task :processTestResources NO-SOURCE
> Task :testClasses UP-TO-DATE
> Task :test NO-SOURCE

BUILD SUCCESSFUL in 1s
3 actionable tasks: 3 up-to-date"""

_GRADLE_PASSING = """> Task :compileJava
> Task :processResources NO-SOURCE
> Task :classes
> Task :compileTestJava
> Task :processTestResources NO-SOURCE
> Task :testClasses
> Task :test

BUILD SUCCESSFUL in 3s
4 actionable tasks: 4 executed"""

# `gradle check`: the unit-test task is empty (NO-SOURCE) but the integrationTest
# sibling REALLY RAN — a verified pass, never an empty run.
_GRADLE_CHECK_MIXED = """> Task :compileJava UP-TO-DATE
> Task :classes UP-TO-DATE
> Task :compileTestJava NO-SOURCE
> Task :processTestResources NO-SOURCE
> Task :testClasses UP-TO-DATE
> Task :test NO-SOURCE
> Task :compileIntegrationTestJava
> Task :integrationTestClasses
> Task :integrationTest
> Task :check

BUILD SUCCESSFUL in 6s
5 actionable tasks: 3 executed, 2 up-to-date"""

# A multi-module build where EVERY module's execution task is empty.
_GRADLE_MULTI_MODULE_ZERO = """> Task :app:compileTestJava NO-SOURCE
> Task :app:processTestResources NO-SOURCE
> Task :app:testClasses UP-TO-DATE
> Task :app:test NO-SOURCE
> Task :lib:compileTestJava NO-SOURCE
> Task :lib:processTestResources NO-SOURCE
> Task :lib:testClasses UP-TO-DATE
> Task :lib:test NO-SOURCE

BUILD SUCCESSFUL in 2s
6 actionable tasks: 6 up-to-date"""

# `gradle test integrationTest`: the unit-test task is empty (NO-SOURCE) but the
# integrationTest sibling is UP-TO-DATE, not NO-SOURCE. Gradle's own up-to-date check
# only runs for a task that HAS source — a sourceless task short-circuits straight to
# NO-SOURCE instead — so UP-TO-DATE here proves the suite exists and was previously
# verified. A rule that requires EVERY execution task to be NO-SOURCE before returning
# NO_TESTS must not fire here; it also must not need the UP-TO-DATE task to look like
# "it ran" to reach that answer.
_GRADLE_NO_SOURCE_PLUS_UP_TO_DATE_SIBLING = """> Task :compileJava UP-TO-DATE
> Task :classes UP-TO-DATE
> Task :compileTestJava NO-SOURCE
> Task :processTestResources NO-SOURCE
> Task :testClasses UP-TO-DATE
> Task :test NO-SOURCE
> Task :compileIntegrationTestJava UP-TO-DATE
> Task :integrationTestClasses UP-TO-DATE
> Task :integrationTest UP-TO-DATE

BUILD SUCCESSFUL in 500ms
3 actionable tasks: 3 up-to-date"""

# The suite EXISTS and is up-to-date (nothing changed since the last green run).
_GRADLE_UP_TO_DATE = """> Task :compileJava UP-TO-DATE
> Task :processResources NO-SOURCE
> Task :classes UP-TO-DATE
> Task :compileTestJava UP-TO-DATE
> Task :processTestResources NO-SOURCE
> Task :testClasses UP-TO-DATE
> Task :test UP-TO-DATE

BUILD SUCCESSFUL in 800ms
4 actionable tasks: 4 up-to-date"""

# REAL Android-Gradle-Plugin (AGP) output. The unit-test variant generates SUPPORT
# tasks whose names ALSO end in "Test" and execute with a bare header
# (`javaPreCompileDebugUnitTest`, `packageDebugUnitTestForUnitTest`) — an `endswith
# ("Test")` name-match reads them as "a task ran" and discards the genuine
# `:app:testDebugUnitTest NO-SOURCE`, greening a ZERO-TEST Android module. Only the
# `:testDebugUnitTest` EXECUTION task's own status is evidence. (Task lines here are
# verbatim shapes from real Android CI logs.)
_GRADLE_ANDROID_ZERO_TEST = """> Task :app:preDebugUnitTestBuild UP-TO-DATE
> Task :app:processDebugUnitTestJavaRes NO-SOURCE
> Task :app:javaPreCompileDebugUnitTest
> Task :app:packageDebugUnitTestForUnitTest
> Task :app:compileDebugUnitTestKotlin NO-SOURCE
> Task :app:compileDebugUnitTestJavaWithJavac NO-SOURCE
> Task :app:testDebugUnitTest NO-SOURCE

BUILD SUCCESSFUL in 47s
42 actionable tasks: 5 executed, 37 up-to-date"""

_GRADLE_ANDROID_PASSING = """> Task :app:preDebugUnitTestBuild UP-TO-DATE
> Task :app:javaPreCompileDebugUnitTest
> Task :app:compileDebugUnitTestJavaWithJavac
> Task :app:testDebugUnitTest

BUILD SUCCESSFUL in 12s
44 actionable tasks: 12 executed, 32 up-to-date"""

_GRADLE_FAILING = """> Task :compileJava
> Task :classes
> Task :compileTestJava
> Task :testClasses
> Task :test FAILED

DemoTest > addsTwoNumbers() FAILED
    org.opentest4j.AssertionFailedError: expected: <3> but was: <4>

2 tests completed, 1 failed

FAILURE: Build failed with an exception.

* What went wrong:
Execution failed for task ':test'.
> There were failing tests.

BUILD FAILED in 4s"""

_GRADLE_COMPILE_ERROR = """> Task :compileJava FAILED

FAILURE: Build failed with an exception.

* What went wrong:
Execution failed for task ':compileJava'.
> Compilation failed; see the compiler error output for details.

BUILD FAILED in 2s"""


# ── The classifier table ─────────────────────────────────────────────────────────

# (id, runner, exit_code, output, expected_class). Output strings are the real
# marker text each runner emits — verified against each runner's docs/source.
_CLASSIFY_CASES = [
    # ---- pytest ----
    ("pytest-pass", tr.PYTEST, 0, "459 passed in 3.2s", tr.PASSED),
    ("pytest-fail", tr.PYTEST, 1, "FAILED tests/test_x.py::test_y - assert", tr.FAILED),
    ("pytest-no-tests-exit5", tr.PYTEST, 5, "no tests ran in 0.01s", tr.NO_TESTS),
    ("pytest-collection-error-exit2", tr.PYTEST, 2, "ERROR tests/test_x.py - ImportError", tr.COMPILE_ERROR),
    ("pytest-runner-missing", tr.PYTEST, 1, "/usr/bin/python3: No module named pytest", tr.ENV_ERROR),
    ("pytest-usage-error-exit4", tr.PYTEST, 4, "ERROR: file or directory not found: tests/", tr.ENV_ERROR),

    # ---- unittest (version split) ----
    ("unittest-pass", tr.UNITTEST, 0, "Ran 12 tests in 0.1s\n\nOK", tr.PASSED),
    ("unittest-fail", tr.UNITTEST, 1, "Ran 3 tests\n\nFAILED (failures=1)", tr.FAILED),
    ("unittest-no-tests-312", tr.UNITTEST, 5, "\n----\nNO TESTS RAN", tr.NO_TESTS),
    ("unittest-no-tests-311", tr.UNITTEST, 0, "\n----\nRan 0 tests in 0.000s\n\nOK", tr.NO_TESTS),
    ("unittest-load-error", tr.UNITTEST, 1, "ImportError: cannot import name 'foo'", tr.COMPILE_ERROR),
    # A real, failing run whose OWN output happens to echo "Ran 0 tests in ..." (e.g.
    # a test asserting on captured subprocess text) must NOT be masked as an empty
    # run — the genuine "Ran 1 test" summary is real-run evidence.
    ("unittest-fail-with-embedded-zero-marker", tr.UNITTEST, 1,
     "test_subprocess_output (t.T) ... FAIL\nAssertionError: expected 'Ran 0 tests in 0.000s'\n\nRan 1 test in 0.01s\n\nFAILED (failures=1)",
     tr.FAILED),

    # ---- django (unittest runner) ----
    ("django-pass", tr.DJANGO, 0, "Ran 8 tests in 1.0s\nOK", tr.PASSED),
    ("django-no-tests", tr.DJANGO, 0, "Ran 0 tests in 0.000s\nOK", tr.NO_TESTS),
    ("django-fail", tr.DJANGO, 1, "Ran 2 tests\nFAILED (failures=1)", tr.FAILED),
    # A real, failing run whose OWN output happens to echo "Ran 0 tests" (e.g. a
    # test asserting on captured subprocess/management-command text) must NOT be
    # masked as an empty run — the genuine "Ran 1 test" summary is real-run evidence.
    ("django-fail-with-embedded-zero-marker", tr.DJANGO, 1,
     "test_subcommand_output (t.T) ... FAIL\nAssertionError: expected 'Ran 0 tests in 0.000s'\n\nRan 1 test in 0.01s\n\nFAILED (failures=1)",
     tr.FAILED),

    # ---- jest ----
    ("jest-pass", tr.JEST, 0, "Tests: 5 passed, 5 total", tr.PASSED),
    ("jest-fail", tr.JEST, 1, "Tests: 1 failed, 4 passed", tr.FAILED),
    ("jest-no-tests", tr.JEST, 1, "No tests found, exiting with code 1", tr.NO_TESTS),
    ("jest-no-tests-json", tr.JEST, 1, '{"numTotalTests":0,"numPassedTests":0}', tr.NO_TESTS),
    ("jest-missing-deps", tr.JEST, 1, "Cannot find module 'react' from 'src/App.test.js'", tr.ENV_ERROR),
    # A real, failing suite whose OWN assertion/snapshot text happens to contain "No
    # tests found" must NOT be masked as an empty run — the genuine "Tests: 1 failed,
    # 1 total" summary is real-run evidence.
    ("jest-fail-with-embedded-no-tests-string", tr.JEST, 1,
     "FAIL src/empty.test.js\n  ✕ renders empty state\n    Expected: \"No tests found\"\n\nTests: 1 failed, 1 total",
     tr.FAILED),

    # ---- vitest ----
    ("vitest-pass", tr.VITEST, 0, "Test Files  3 passed (3)", tr.PASSED),
    ("vitest-fail", tr.VITEST, 1, "FAIL  src/a.test.ts > adds", tr.FAILED),
    ("vitest-no-tests", tr.VITEST, 1, "No test files found, exiting with code 1", tr.NO_TESTS),
    ("vitest-missing-deps", tr.VITEST, 1, "Failed to resolve import 'vite'", tr.ENV_ERROR),
    ("vitest-esbuild-missing-dep", tr.VITEST, 1, "X [ERROR] Could not resolve 'react'\n\n    src/App.tsx:1:18:", tr.ENV_ERROR),
    # A real, failing suite whose OWN assertion text happens to contain "No test
    # files found" must NOT be masked as an empty run — the genuine "Test Files  1
    # failed" / "Tests  1 failed" summary is real-run evidence.
    ("vitest-fail-with-embedded-no-tests-string", tr.VITEST, 1,
     "FAIL src/empty.test.ts\n  ✕ renders empty state\n    Expected: \"No test files found\"\n\n"
     " Test Files  1 failed (1)\n      Tests  1 failed (1)",
     tr.FAILED),

    # ---- mocha ----
    ("mocha-pass", tr.MOCHA, 0, "  5 passing (20ms)", tr.PASSED),
    ("mocha-fail", tr.MOCHA, 2, "  3 passing\n  2 failing", tr.FAILED),
    ("mocha-no-tests", tr.MOCHA, 1, "Error: No test files found", tr.NO_TESTS),
    # rc==0 zero-runnable-test run (empty suite / --grep matching nothing) — mocha
    # prints only "0 passing" with none of the "No test files found" markers above,
    # and exits 0 unless --fail-zero is passed. Verified empirically against a real
    # mocha install: `mocha --grep nonexistent` and an empty `describe()` block both
    # exit 0 printing "\n\n  0 passing (1ms)\n".
    ("mocha-zero-runnable-rc0", tr.MOCHA, 0, "\n\n  0 passing (1ms)\n", tr.NO_TESTS),

    # ---- jasmine — the no-specs marker is RC-GATED (a nonzero exit is NEVER a benign
    #      empty run; same invariant _classify_dotnet documents in its own branch).
    #      jasmine v2 exits 0 on no specs (the silent-green class → no_tests SKIP);
    #      v3 exits 1 and v4+ exits 2 BECAUSE jasmine itself treats an empty run as an
    #      ERROR — so those RED, and a broken run (bad spec_dir / a helper that threw)
    #      printing the same marker at a nonzero exit REDs too, instead of skipping the
    #      gate and letting the push proceed unverified. ----
    ("jasmine-pass", tr.JASMINE, 0, "5 specs, 0 failures", tr.PASSED),
    ("jasmine-fail", tr.JASMINE, 3, "5 specs, 2 failures", tr.FAILED),
    ("jasmine-no-specs-v2-exit0", tr.JASMINE, 0, "Started\n\nNo specs found\nFinished", tr.NO_TESTS),
    ("jasmine-no-specs-v3-exit1-is-failed", tr.JASMINE, 1, "Started\nNo specs found\nIncomplete: No specs found", tr.FAILED),
    ("jasmine-no-specs-v4-exit2-is-failed", tr.JASMINE, 2, "Started\nNo specs found\nIncomplete: No specs found", tr.FAILED),

    # ---- Karma / ng test: "Executed 0 of N" marker wins over exit code. Default
    #      failOnEmptyTestSuite=true → empty suite EXITS 1 (false-red risk); the
    #      opt-out → exit 0 (false-green). Both must classify no_tests. ----
    ("karma-pass", tr.KARMA, 0, "Executed 12 of 12 SUCCESS", tr.PASSED),
    ("karma-fail", tr.KARMA, 1, "Executed 12 of 12 (1 FAILED)", tr.FAILED),
    ("karma-no-specs-default-exit1", tr.KARMA, 1, "Executed 0 of 0 SUCCESS", tr.NO_TESTS),
    ("karma-no-specs-optout-exit0", tr.KARMA, 0, "Executed 0 of 0 SUCCESS", tr.NO_TESTS),
    ("karma-webpack-missing-dep", tr.KARMA, 1, "ERROR in ./src/app.component.ts\nModule not found: Error: Can't resolve '@angular/material' in '/app/src'", tr.ENV_ERROR),
    # A BROKEN karma run reports "Executed 0 of N" but must NOT be masked as a green
    # no-tests SKIP: a TS compile error and a browser DISCONNECT are RED.
    ("karma-ts-compile-error-not-skip", tr.KARMA, 1, "ERROR in src/app.ts\nerror TS2304: Cannot find name 'Foo'.\nExecuted 0 of 0 ERROR", tr.COMPILE_ERROR),
    ("karma-browser-disconnect-not-skip", tr.KARMA, 1, "Chrome ... DISCONNECTED\nExecuted 0 of 12 DISCONNECTED (1 min)", tr.COMPILE_ERROR),

    # ---- node:test / ava ----
    ("node-test-pass", tr.NODE_TEST, 0, "# tests 4\n# pass 4", tr.PASSED),
    ("node-test-no-tests", tr.NODE_TEST, 0, "# tests 0\n# pass 0", tr.NO_TESTS),
    ("ava-pass", tr.AVA, 0, "3 tests passed", tr.PASSED),
    ("ava-no-tests", tr.AVA, 1, "Couldn't find any files to test", tr.NO_TESTS),

    # ---- bun (P10 Tier-C) — marker-first "Ran N tests across M files" ----
    ("bun-pass", tr.BUN, 0, " 3 pass\n 0 fail\nRan 3 tests across 1 files. [8.00ms]", tr.PASSED),
    ("bun-fail", tr.BUN, 1, " 2 pass\n 1 fail\nRan 3 tests across 1 files. [8.00ms]", tr.FAILED),
    ("bun-no-tests", tr.BUN, 1, "Ran 0 tests across 0 files. [1.00ms]", tr.NO_TESTS),
    # A run with "0 pass" but real failures is a FAILURE, never masked as no_tests.
    ("bun-zero-pass-with-fails-is-fail", tr.BUN, 1,
     " 0 pass\n 3 fail\nRan 3 tests across 1 files.", tr.FAILED),
    ("bun-missing-dep", tr.BUN, 1, 'error: Cannot find module "react" from "/app/x.test.ts"', tr.ENV_ERROR),
    ("bun-parse-error", tr.BUN, 1, "1 | const x = ;\n              ^\nerror: Unexpected )", tr.COMPILE_ERROR),

    # ---- deno (P10 Tier-C) — EXITS 1 on "No test modules found" (false-red) ----
    ("deno-pass", tr.DENO, 0, "ok | 3 passed | 0 failed (10ms)", tr.PASSED),
    ("deno-fail", tr.DENO, 1, "FAILED | 2 passed | 1 failed (10ms)", tr.FAILED),
    ("deno-no-tests-exit1", tr.DENO, 1, "error: No test modules found", tr.NO_TESTS),
    ("deno-filter-zero", tr.DENO, 0, "ok | 0 passed | 0 failed (2ms)", tr.NO_TESTS),
    ("deno-type-error", tr.DENO, 1, "TS2345 [ERROR]: Argument of type 'x'.\nerror: Type checking failed", tr.COMPILE_ERROR),
    ("deno-missing-module", tr.DENO, 1, 'error: Module not found "file:///app/dep.ts".', tr.ENV_ERROR),

    # ---- go (SILENT EXIT 0 on no test files) ----
    ("go-pass", tr.GO, 0, "ok  \texample.com/a\t0.3s", tr.PASSED),
    ("go-fail", tr.GO, 1, "--- FAIL: TestX\nFAIL\texample.com/a", tr.FAILED),
    ("go-no-test-files", tr.GO, 0, "?   \texample.com/a\t[no test files]", tr.NO_TESTS),
    ("go-build-failed", tr.GO, 1, "# example.com/a\n./a.go:3:1: syntax error\nFAIL\texample.com/a [build failed]", tr.COMPILE_ERROR),
    # golang/go#64286: a test-less package's build error prints NO "[build failed]"
    # annotation and the run EXITS 0 beside a passing sibling — the `# <pkg>` header +
    # column-0 `file.go:N:N:` predicate is exit-code-INDEPENDENT and still REDs it.
    ("go-64286-exit0-build-fail", tr.GO, 0, "# example.com/a\n./a.go:5:9: undefined: Foo\nok  \texample.com/b\t0.2s", tr.COMPILE_ERROR),
    # "[setup failed]" is the LOAD-time counterpart of "[build failed]" (bad import path
    # / package-name mismatch) and may emit NO file:line diagnostic at all.
    ("go-setup-failed", tr.GO, 1, "FAIL\texample.com/a [setup failed]\nexample.com/a: no non-test Go files in /src/a", tr.COMPILE_ERROR),
    # `go test -json` (Go 1.24+): the build diagnostic arrives as a column-0 JSON
    # "build-fail" record, so the text header/diagnostic predicate cannot see it.
    ("go-json-build-fail", tr.GO, 1, '{"Time":"2026-01-01T00:00:00Z","Action":"build-fail","Package":"example.com/a"}', tr.COMPILE_ERROR),
    # A DECORATED test failure (t.Errorf prints `x_test.go:12:`) is a FAILURE — it has no
    # `# <pkg>` header, so it must never be misrouted to compile_error.
    ("go-decorated-fail", tr.GO, 1, "=== RUN   TestX\n    x_test.go:12: got 1 want 2\n--- FAIL: TestX (0.00s)\nFAIL\nFAIL\texample.com/a\t0.1s", tr.FAILED),
    # A PASSING `go test -v` echoes the test's own t.Log verbatim — a logged
    # `handler.go:42:1:` string must NOT false-RED it.
    ("go-pass-v-with-tlog", tr.GO, 0, "=== RUN   TestX\n    x_test.go:12: checked handler.go:42:1:\n--- PASS: TestX (0.00s)\nPASS\nok  \texample.com/a\t0.1s", tr.PASSED),
    # The `[^\s:]+` (space-EXCLUDING) path class: a column-0 PROSE line whose `.go:N:N:`
    # follows a space ("See handler.go:42:1:") is not a compiler diagnostic, so a passing
    # doc/lint run carrying a `#` heading must stay green. (A `[^\n:]+` class false-RED it.)
    ("go-pass-hash-header-prose-diag", tr.GO, 0, "# Coverage report\nSee handler.go:42:1: for details\nok  \texample.com/a\t0.1s", tr.PASSED),
    # A test PRINTING a captured -json build-fail fixture runs in TEXT mode, so the go
    # tool's own column-0 decoration is present → not a real -json stream → stays FAILED.
    ("go-prints-json-fixture-textmode", tr.GO, 1, '=== RUN   TestSnap\n{"Action":"build-fail","Package":"x"}\n--- FAIL: TestSnap\nFAIL\texample.com/a', tr.FAILED),
    # The toolchain markers are rc!=0-gated: a GREEN snapshot test that echoes a captured
    # "[setup failed]" string must not false-RED.
    ("go-pass-prints-setup-failed-string", tr.GO, 0, "=== RUN   TestSnap\n    snap_test.go:9: FAIL\tpkg [setup failed]\n--- PASS: TestSnap\nPASS\nok  \texample.com/a", tr.PASSED),
    # `go test -run <no-match>` FILTERS every test out, yet still EXITS 0 printing a
    # column-0 `ok  pkg 0.5s [no tests to run]` per package (real go1.26.5 output). An
    # `ok`-line check that ignored the annotation read that as "tests ran" and reported
    # a verified GREEN for a run that executed NOTHING — the exact silent-exit-0
    # false-green this classifier exists to catch.
    ("go-run-filter-no-match-exit0", tr.GO, 0,
     "ok  \texample.com/a\t0.521s [no tests to run]\nok  \texample.com/b\t0.879s [no tests to run]", tr.NO_TESTS),
    # …and under `-v` the same zero-test run ALSO prints a bare column-0 `PASS` (the test
    # binary saying "I did not fail"), which is likewise NOT proof that anything ran.
    ("go-run-filter-no-match-verbose", tr.GO, 0,
     "testing: warning: no tests to run\nPASS\nok  \texample.com/a\t0.369s [no tests to run]\n?   \texample.com/c\t[no test files]", tr.NO_TESTS),
    # But an UNANNOTATED `ok` line IS run evidence: one package really ran while a
    # sibling was filtered to zero → the run verified something → stays GREEN.
    ("go-mixed-one-ran-one-filtered", tr.GO, 0,
     "ok  \texample.com/a\t0.1s\nok  \texample.com/b\t0.2s [no tests to run]", tr.PASSED),
    # `go test -v` echoes a PASSING test's stdout VERBATIM at column 0, so a tooling /
    # snapshot test asserting on captured `go build` output prints BOTH halves of the
    # compile predicate (`# <pkg>` header AND a column-0 `file.go:N:N:`) on an all-green
    # rc==0 run. Those lines are the TEST's, not the go tool's — scoping the predicate to
    # the tool's own lines keeps this green while `go-64286-exit0-build-fail` above (whose
    # header the TOOL printed, outside any `=== RUN` region) still REDs at the SAME rc 0.
    ("go-pass-prints-captured-build-failure", tr.GO, 0,
     "=== RUN   TestParsesBuildFailure\n# example.com/other/broken\n./main.go:5:9: undefined: undefinedSymbol\n--- PASS: TestParsesBuildFailure (0.00s)\nPASS\nok  \texample.com/tooling\t0.4s", tr.PASSED),
    # A REAL build failure under -v still REDs: the go tool builds every package BEFORE
    # running any test binary, so its `# <pkg>` header lands OUTSIDE the `=== RUN` region
    # even when a sibling package's tests run and pass right after it.
    ("go-real-build-fail-v-with-passing-sibling", tr.GO, 1,
     "# example.com/broken\nbroken/b.go:3:23: undefined: undefinedSymbol\nFAIL\texample.com/broken [build failed]\n=== RUN   TestAdd\n--- PASS: TestAdd (0.00s)\nPASS\nok  \texample.com/a\t0.354s\nFAIL", tr.COMPILE_ERROR),

    # ---- cargo (stable — 101 is BOTH fail and compile) ----
    ("cargo-pass", tr.CARGO, 0, "test result: ok. 5 passed; 0 failed", tr.PASSED),
    ("cargo-fail-101", tr.CARGO, 101, "test result: FAILED. 4 passed; 1 failed", tr.FAILED),
    ("cargo-compile-101", tr.CARGO, 101, "error[E0308]: mismatched types\nerror: could not compile `x`", tr.COMPILE_ERROR),
    ("cargo-no-tests", tr.CARGO, 0, "running 0 tests\ntest result: ok. 0 passed; 0 failed", tr.NO_TESTS),
    # A cargo TEST failure (exit 101) whose output carries rustc diagnostics — a
    # trybuild / compiletest UI-test mismatch — is a FAILURE, not compile_error: the
    # "test result:" summary proves the test binary compiled + ran.
    ("cargo-trybuild-testfail", tr.CARGO, 101, "running 1 test\ntest ui ... FAILED\nEXPECTED:\nerror[E0308]: mismatched types\ntest result: FAILED. 0 passed; 1 failed\nerror: test failed", tr.FAILED),
    # A PASSING cargo run (exit 0) with --nocapture echoing a diagnostic string must
    # NOT false-red — "test result: ok." is present, so it is passed.
    ("cargo-pass-nocapture-diag", tr.CARGO, 0, "running 1 test\ntest diag::renders ... ok\n---- diag::renders stdout ----\nrendered: error[E0308]: mismatched types\ntest result: ok. 1 passed; 0 failed", tr.PASSED),

    # ---- cargo-nextest (distinct codes) ----
    ("nextest-pass", tr.NEXTEST, 0, "Summary 5 tests run: 5 passed", tr.PASSED),
    ("nextest-no-tests-4", tr.NEXTEST, 4, "", tr.NO_TESTS),
    ("nextest-fail-100", tr.NEXTEST, 100, "Summary 5 tests run: 4 passed, 1 failed", tr.FAILED),
    ("nextest-compile-101", tr.NEXTEST, 101, "error: could not compile", tr.COMPILE_ERROR),

    # ---- maven / gradle ----
    ("maven-pass", tr.MAVEN, 0, "Tests run: 12, Failures: 0\nBUILD SUCCESS", tr.PASSED),
    ("maven-fail", tr.MAVEN, 1, "Tests run: 5, Failures: 1\nBUILD FAILURE", tr.FAILED),
    ("maven-no-tests", tr.MAVEN, 0, "Tests run: 0, Failures: 0\nBUILD SUCCESS", tr.NO_TESTS),
    ("maven-compile", tr.MAVEN, 1, "COMPILATION ERROR\nBUILD FAILURE", tr.COMPILE_ERROR),
    # Gradle fixtures are REAL `gradle test` / `gradle check` console output — INCLUDING
    # the lifecycle tasks Gradle always emits around the execution task
    # (`compileTestJava`, `processTestResources`, `testClasses`). A synthetic 1–3 line
    # fixture is what let the FALSE-GREEN below ship: written to the regex, not to
    # Gradle. See _GRADLE_* above and TestGradleZeroTestIsNeverGreen.
    ("gradle-pass", tr.GRADLE, 0, _GRADLE_PASSING, tr.PASSED),
    ("gradle-zero-test-is-no-tests", tr.GRADLE, 0, _GRADLE_ZERO_TEST, tr.NO_TESTS),
    ("gradle-check-empty-plus-sibling-that-ran", tr.GRADLE, 0, _GRADLE_CHECK_MIXED, tr.PASSED),
    # `:test NO-SOURCE` beside `:integrationTest UP-TO-DATE` — UP-TO-DATE implies the
    # sibling suite HAS source (a sourceless task would print NO-SOURCE, not
    # UP-TO-DATE), so this must stay a pass, not fall to NO_TESTS just because no
    # execution task looked like it "ran" this invocation.
    ("gradle-no-source-plus-up-to-date-sibling-is-pass", tr.GRADLE, 0,
     _GRADLE_NO_SOURCE_PLUS_UP_TO_DATE_SIBLING, tr.PASSED),
    ("gradle-multi-module-zero-test", tr.GRADLE, 0, _GRADLE_MULTI_MODULE_ZERO, tr.NO_TESTS),
    # Android (AGP): the support tasks `javaPreCompileDebugUnitTest` /
    # `packageDebugUnitTestForUnitTest` END in "Test" and run with a bare header, but
    # only `:app:testDebugUnitTest NO-SOURCE` is zero-test evidence.
    ("gradle-android-zero-test-is-no-tests", tr.GRADLE, 0, _GRADLE_ANDROID_ZERO_TEST, tr.NO_TESTS),
    ("gradle-android-passing-run", tr.GRADLE, 0, _GRADLE_ANDROID_PASSING, tr.PASSED),
    # `> Task :test UP-TO-DATE` — the suite EXISTS and was up-to-date. UP-TO-DATE is
    # NON-evidence (it neither ran now nor says the suite is empty) → a green build.
    ("gradle-test-up-to-date-is-pass", tr.GRADLE, 0, _GRADLE_UP_TO_DATE, tr.PASSED),
    ("gradle-fail", tr.GRADLE, 1, _GRADLE_FAILING, tr.FAILED),
    ("gradle-compile", tr.GRADLE, 1, _GRADLE_COMPILE_ERROR, tr.COMPILE_ERROR),

    # ---- dotnet (VSTest SILENT EXIT 0 + MTP) ----
    ("dotnet-pass", tr.DOTNET, 0, "Passed!  - Failed: 0, Passed: 12, Total: 12", tr.PASSED),
    ("dotnet-fail", tr.DOTNET, 1, "Failed!  - Failed: 1, Passed: 11", tr.FAILED),
    ("dotnet-vstest-no-tests-exit0", tr.DOTNET, 0, "No test is available in App.Tests.dll. Make sure that test discoverer & executors are registered.", tr.NO_TESTS),
    ("dotnet-vstest-filter-no-match", tr.DOTNET, 0, "...but no test matches the specified selection criteria.", tr.NO_TESTS),
    ("dotnet-mtp-no-tests-8", tr.DOTNET, 8, "", tr.NO_TESTS),
    ("dotnet-build-fail", tr.DOTNET, 1, "Build FAILED.\nProgram.cs(3,1): error CS1002", tr.COMPILE_ERROR),
    # `error CS\d+` / `error MSB\d+` are rc-gated: a green run (rc 0) that merely
    # PRINTS that literal text as test output (e.g. a Roslyn analyzer test asserting
    # on the exact diagnostic string) must never false-red.
    ("dotnet-green-printing-error-cs-string", tr.DOTNET, 0,
     "Passed!  - Failed: 0, Passed: 1, Total: 1\n  expected diagnostic: error CS1002", tr.PASSED),
    # The no-tests markers are rc==0-GATED. A repo with no tests exits 0 by DEFAULT, so a
    # NONZERO exit whose ONLY signal is a no-tests marker is never a benign empty run —
    # it is opt-in <TreatNoTestsAsError> or a BROKEN discovery (missing adapter / TFM
    # mismatch / unloadable dll). Both are real errors → FAILED, never masked green.
    ("dotnet-rc-nonzero-only-no-tests-is-failed", tr.DOTNET, 1, "No test is available in App.Tests.dll. Make sure that test discoverer & executors are registered.", tr.FAILED),
    ("dotnet-rc0-no-tests-still-no-tests", tr.DOTNET, 0, "No test is available in App.Tests.dll.", tr.NO_TESTS),
    # A VSTest ABORT emits no per-test failure lines and can land beside a sibling empty
    # project's no-tests marker (microsoft/vstest#2952) — it must RED, not skip.
    ("dotnet-abort-is-failed", tr.DOTNET, 1, "Test Run Aborted.\nNo test is available in App.Tests.dll.", tr.FAILED),
    ("dotnet-testhost-crash-is-failed", tr.DOTNET, 1, "Testhost process exited with error: ...\nno test matches the specified selection criteria", tr.FAILED),
    # The abort branch is rc-gated: a GREEN run that merely PRINTS an abort string stays green.
    ("dotnet-green-printing-abort-string", tr.DOTNET, 0, "Passed!  - Failed: 0, Passed: 3, Total: 3\n  log: 'Test Run Aborted' handled", tr.PASSED),

    # ---- mix (elixir) ----
    ("mix-pass", tr.MIX, 0, "5 tests, 0 failures", tr.PASSED),
    ("mix-fail-2", tr.MIX, 2, "5 tests, 2 failures", tr.FAILED),
    ("mix-no-tests-1", tr.MIX, 1, "0 tests, 0 failures", tr.NO_TESTS),
    ("mix-compile-1", tr.MIX, 1, "== Compilation error in file test/x_test.exs ==", tr.COMPILE_ERROR),

    # ---- rspec / minitest ----
    ("rspec-pass", tr.RSPEC, 0, "5 examples, 0 failures", tr.PASSED),
    ("rspec-fail", tr.RSPEC, 1, "5 examples, 1 failure", tr.FAILED),
    ("rspec-no-tests", tr.RSPEC, 0, "0 examples, 0 failures", tr.NO_TESTS),
    ("rspec-load-error", tr.RSPEC, 1, "An error occurred while loading ./spec/x_spec.rb", tr.COMPILE_ERROR),
    ("minitest-pass", tr.MINITEST, 0, "5 runs, 12 assertions, 0 failures, 0 errors", tr.PASSED),
    ("minitest-no-tests", tr.MINITEST, 0, "0 runs, 0 assertions, 0 failures, 0 errors", tr.NO_TESTS),
    ("minitest-fail", tr.MINITEST, 1, "5 runs, 8 assertions, 1 failures, 0 errors", tr.FAILED),

    # ---- phpunit / pest ----
    ("phpunit-pass", tr.PHPUNIT, 0, "OK (12 tests, 34 assertions)", tr.PASSED),
    ("phpunit-fail", tr.PHPUNIT, 1, "FAILURES!\nTests: 5, Assertions: 8, Failures: 1", tr.FAILED),
    ("phpunit-no-tests", tr.PHPUNIT, 0, "No tests executed!", tr.NO_TESTS),
    ("phpunit-fatal", tr.PHPUNIT, 2, "PHP Fatal error:  Class 'Foo' not found", tr.COMPILE_ERROR),
    ("pest-pass", tr.PEST, 0, "Tests:  12 passed", tr.PASSED),
    # Pest does NOT inherit phpunit's no-tests shape: zero tests → exit 1 (NON-zero) +
    # " INFO  No tests found." — the marker-first branch classifies it no_tests anyway.
    ("pest-no-tests", tr.PEST, 1, "  INFO  No tests found.", tr.NO_TESTS),

    # ---- ctest / gtest / catch2 (SILENT EXIT 0) ----
    ("ctest-pass", tr.CTEST, 0, "100% tests passed, 0 tests failed out of 10", tr.PASSED),
    ("ctest-fail", tr.CTEST, 8, "50% tests passed, 5 tests failed out of 10", tr.FAILED),
    ("ctest-no-tests-exit0", tr.CTEST, 0, "No tests were found!!!", tr.NO_TESTS),
    ("gtest-pass", tr.GTEST, 0, "[==========] 10 tests from 2 test suites ran.\n[  PASSED  ] 10 tests.", tr.PASSED),
    ("gtest-fail", tr.GTEST, 1, "[  FAILED  ] 1 test, listed below:", tr.FAILED),
    ("gtest-no-tests-exit0", tr.GTEST, 0, "[==========] 0 tests from 0 test suites ran.", tr.NO_TESTS),
    ("catch2-pass", tr.CATCH2, 0, "All tests passed (12 assertions in 3 test cases)", tr.PASSED),
    ("catch2-fail-42", tr.CATCH2, 42, "test cases: 3 | 2 passed | 1 failed", tr.FAILED),
    ("catch2-no-tests-2", tr.CATCH2, 2, "No test cases matched", tr.NO_TESTS),
    ("catch2-all-skipped-4", tr.CATCH2, 4, "", tr.NO_TESTS),

    # ---- swift (SILENT EXIT 0) ----
    ("swift-pass", tr.SWIFT, 0, "Test Suite 'All tests' passed.\nExecuted 5 tests", tr.PASSED),
    ("swift-fail", tr.SWIFT, 1, "Test Suite 'All tests' failed.\nExecuted 5 tests, with 1 failure", tr.FAILED),
    ("swift-no-tests-exit0", tr.SWIFT, 0, "Test Suite 'All tests' passed.\nExecuted 0 tests, with 0 failures", tr.NO_TESTS),
    ("swift-testing-no-tests", tr.SWIFT, 0, "Test run with 0 tests passed after 0.001 seconds.", tr.NO_TESTS),
    ("swift-filter-no-match", tr.SWIFT, 0, "No matching test cases were run", tr.NO_TESTS),
    # The count banners tolerate the SINGULAR ("Executed 1 test" / "Test run with 1 test")
    # — without the `s?` a ONE-test run yields no banner and false-greens as no_tests.
    ("swift-one-test-count-banner", tr.SWIFT, 0, "Test Suite 'All tests' passed.\nExecuted 1 test, with 0 failures", tr.PASSED),
    ("swift-testing-one-test", tr.SWIFT, 0, "Test run with 1 test passed after 0.1 seconds.", tr.PASSED),
    # `swift test` runs XCTest AND swift-testing and co-emits the UNUSED framework's
    # "0 tests" trailer beside the used one — a NONZERO count anywhere means it ran.
    ("swift-xctest-pass-with-zero-trailer", tr.SWIFT, 0, "Test Suite 'All tests' passed.\nExecuted 5 tests, with 0 failures\nTest run with 0 tests passed after 0.001 seconds.", tr.PASSED),
    # A real FAILURE whose assertion diff merely QUOTES a no-tests marker must not
    # short-circuit to a green no_tests SKIP — the run banner gates those branches.
    ("swift-fail-quoting-no-tests-marker", tr.SWIFT, 1, "Test Suite 'All tests' failed.\nExecuted 3 tests, with 1 failure\n  XCTAssertEqual failed: expected \"No matching test cases were run\"", tr.FAILED),
    # SwiftPM `testsNotFound` — a package with no test target (rc=1, no banner).
    ("swift-no-target-rc1", tr.SWIFT, 1, "error: no tests found; create a target", tr.NO_TESTS),

    # ---- dart / flutter (exit 1 for BOTH fail and no-tests) ----
    ("dart-pass", tr.DART, 0, "All tests passed!", tr.PASSED),
    ("dart-fail", tr.DART, 1, "Some tests failed.", tr.FAILED),
    ("dart-no-tests", tr.DART, 1, "No tests ran.", tr.NO_TESTS),
    ("flutter-no-tests", tr.FLUTTER, 1, "No tests ran.", tr.NO_TESTS),
    # A real failure whose captured output merely QUOTES a no-tests marker (e.g. a
    # test asserting on captured subprocess text) must not short-circuit to a green
    # no_tests SKIP — the "Some tests failed" summary gates the zero-tests branch.
    ("dart-fail-quoting-no-tests-marker", tr.DART, 1, "expected: 'No tests ran.'\nSome tests failed.", tr.FAILED),

    # deno's zero-count summary (`deno test --filter nomatch` exits 0) reached through
    # an OPAQUE wrapper must classify no_tests — and its column-0 `ok |` head must NOT
    # read as a go `ok <pkg>` result line (run evidence), which would defeat the
    # marker and false-green the empty run.
    ("generic-f2-wrapper-deno-zero", tr.UNKNOWN, 0,
     "ok | 0 passed | 0 failed (2ms)", tr.NO_TESTS),
    # …including any-width padding before the pipe: the go run-evidence guard must
    # exclude the deno head across a whitespace run, not just a single space (a bare
    # `\s+(?!\|)` backtracks and lets a two-space `ok  |…` slip through as go evidence).
    ("generic-f2-wrapper-deno-zero-padded", tr.UNKNOWN, 0,
     "ok  | 0 passed | 0 failed (2ms)", tr.NO_TESTS),
    ("generic-f2-wrapper-deno-zero-padded-wide", tr.UNKNOWN, 0,
     "ok   |  0 passed  |  0 failed (2ms)", tr.NO_TESTS),
    # …while a deno run that really RAN beside a zero-count sibling keeps PASSED.
    ("generic-f2-wrapper-deno-zero-then-ran", tr.UNKNOWN, 0,
     "ok | 0 passed | 0 failed (1ms)\nok | 4 passed | 0 failed (9ms)", tr.PASSED),

    # ---- universal env / timeout ----
    ("cmd-not-found-127", tr.VITEST, 127, "vitest: command not found", tr.ENV_ERROR),
    ("unknown-wrapper-pass", tr.UNKNOWN, 0, "whatever", tr.PASSED),
    ("unknown-wrapper-fail", tr.UNKNOWN, 1, "whatever", tr.FAILED),
]


@pytest.mark.parametrize("case_id,runner,rc,out,want",
                         _CLASSIFY_CASES, ids=[c[0] for c in _CLASSIFY_CASES])
def test_classify_table(case_id, runner, rc, out, want):
    assert tr.classify(runner, rc, out) == want, (
        f"{case_id}: classify({runner!r}, {rc}, …) should be {want!r}")


class TestClassifierContract:
    def test_every_class_in_outcomes(self):
        # No case in the table produces a class outside the six-class contract.
        for _cid, runner, rc, out, want in _CLASSIFY_CASES:
            assert want in tr.OUTCOMES
            assert tr.classify(runner, rc, out) in tr.OUTCOMES

    def test_timeout_wins_over_exit_code(self):
        # A gate TimeoutExpired → timeout regardless of any (stale) exit code.
        assert tr.classify(tr.PYTEST, 0, "459 passed", timed_out=True) == tr.TIMEOUT
        assert tr.classify(tr.GO, None, "", timed_out=True) == tr.TIMEOUT

    def test_none_exit_code_is_not_a_crash(self):
        # A killed process (exit_code None) must classify, never raise.
        assert tr.classify(tr.PYTEST, None, "boom") in tr.OUTCOMES

    def test_unknown_runner_falls_to_generic(self):
        assert tr.classify("some-runner-we-never-heard-of", 0, "") == tr.PASSED
        assert tr.classify("some-runner-we-never-heard-of", 3, "x") == tr.FAILED

    def test_non_triage_red_is_compile_and_env(self):
        assert tr.NON_TRIAGE_RED == frozenset({tr.COMPILE_ERROR, tr.ENV_ERROR})


class TestEnvMarkerBoundary:
    """The universal env pre-check must catch a MISSING runner/toolchain, but must
    NOT swallow a genuine test failure whose message merely mentions 'not found'."""

    @pytest.mark.parametrize("rc,out", [
        (127, "vitest: command not found"),
        (127, "sh: 1: vitest: not found"),
        (127, "go: not found"),
        (101, "error: no such command: nextest"),
        # a MISSING interpreter/runner as the basename of the ENOENT path is env
        (1, "No such file or directory: '/usr/bin/python3'"),
        (1, "Error: spawn node ENOENT\n  No such file or directory: '/usr/local/bin/node'"),
        (1, "execvp: No such file or directory: cargo"),
    ])
    def test_shell_command_not_found_is_env(self, rc, out):
        assert tr.classify(tr.VITEST, rc, out) == tr.ENV_ERROR

    @pytest.mark.parametrize("runner,rc,out", [
        (tr.GO, 1, "--- FAIL: TestX\n    config not found here\nFAIL\tpkg"),
        (tr.PYTEST, 1, "FAILED tests/x.py::t - AssertionError: user not found"),
        (tr.JEST, 1, "expect(received).toBe(expected)\n  key not found in map"),
        # "no such file or directory" with the runner name only as a MID-PATH or
        # MID-WORD substring is a real test failure, not env — the anchor must reject it.
        (tr.PYTEST, 1, "FileNotFoundError: [Errno 2] No such file or directory: '/tmp/logo.png'"),
        (tr.PYTEST, 1, "FileNotFoundError: [Errno 2] No such file or directory: "
                       "'/home/u/.venv/lib/python3.11/site-packages/fixtures/data.bin'"),
        (tr.JEST, 1, "ENOENT: no such file or directory, open '/app/node_modules/x/f.json'"),
    ])
    def test_mid_line_not_found_stays_failed(self, runner, rc, out):
        # A "not found" phrase inside a normal assertion message is a TEST failure,
        # not an environment error — over-classification would mislabel the outcome.
        assert tr.classify(runner, rc, out) == tr.FAILED


class TestSilentExitZeroNeverGreens:
    """The false-green class the classifier exists to prevent: a runner that EXITS 0
    on zero tests must classify `no_tests`, NEVER `passed`."""

    SILENT_ZERO = [
        (tr.JASMINE, "Started\nNo specs found\nFinished"),
        (tr.KARMA, "Executed 0 of 0 SUCCESS"),
        (tr.GO, "?   \tpkg\t[no test files]"),
        (tr.DOTNET, "Passed!  - Failed: 0, Passed: 0, Total: 0"),
        (tr.GTEST, "[==========] 0 tests from 0 test suites ran."),
        (tr.CTEST, "No tests were found!!!"),
        (tr.SWIFT, "Executed 0 tests, with 0 failures"),
        (tr.CARGO, "running 0 tests\ntest result: ok. 0 passed; 0 failed"),
        (tr.NODE_TEST, "# tests 0\n# pass 0"),
    ]

    @pytest.mark.parametrize("runner,out", SILENT_ZERO,
                             ids=[r for r, _ in SILENT_ZERO])
    def test_exit_zero_zero_tests_is_no_tests_not_passed(self, runner, out):
        got = tr.classify(runner, 0, out)
        assert got == tr.NO_TESTS, f"{runner} exit-0 zero-test run false-greened as {got}"


class TestGoBuildFailureDiscrimination:
    """go: a BUILD failure and a TEST failure both exit nonzero, and golang/go#64286
    exits ZERO. The discriminators are (a) rc!=0-gated toolchain markers, (b) the
    exit-code-independent `# <pkg>` header AND column-0 `file.go:N:N:` predicate, and
    (c) the `go test -json` `"Action":"build-fail"` record. A test's OWN output must
    never fake any of them."""

    def test_exit0_build_fail_still_reds(self):
        # go#64286: no "[build failed]" annotation, exit 0, a passing sibling package —
        # the header+column-0 predicate is exit-code-independent, so it still REDs.
        out = "# example.com/a\n./a.go:5:9: undefined: Foo\nok  \texample.com/b\t0.2s"
        assert tr.classify(tr.GO, 0, out) == tr.COMPILE_ERROR

    def test_setup_failed_is_compile_error(self):
        assert tr.classify(tr.GO, 1, "FAIL\tpkg [setup failed]") == tr.COMPILE_ERROR

    def test_json_build_fail_record_is_compile_error(self):
        out = '{"Action":"build-fail","Package":"example.com/a"}'
        assert tr.classify(tr.GO, 1, out) == tr.COMPILE_ERROR

    def test_json_stream_predicate_rejects_text_mode(self):
        # A TEXT-mode run carries the go tool's own column-0 decoration (`ok`/`FAIL`/
        # `=== RUN`), so it is NOT a -json stream — a printed fixture cannot fake one.
        assert tr._go_json_stream('{"Action":"build-fail"}') is True
        assert tr._go_json_stream('=== RUN TestX\n{"Action":"build-fail"}\nFAIL\tpkg') is False

    @pytest.mark.parametrize("rc,out", [
        # a decorated FAILURE (t.Errorf prints file:line) — no `# pkg` header
        (1, "=== RUN   TestX\n    x_test.go:12: got 1 want 2\n--- FAIL: TestX\nFAIL\tpkg"),
        # a PASSING -v run echoing a t.Log that mentions a file:line:col
        (0, "=== RUN   TestX\n    x_test.go:9: checked handler.go:42:1:\n--- PASS: TestX\nok  \tpkg"),
        # a PASSING run with a `#` heading + a PROSE `.go:N:N:` (space before it → the
        # space-EXCLUDING `[^\s:]+` class must not match)
        (0, "# Coverage report\nSee handler.go:42:1: for details\nok  \tpkg"),
        # a GREEN snapshot test echoing a captured "[setup failed]" string (rc==0 gate)
        (0, "=== RUN   TestSnap\n    s_test.go:9: FAIL\tpkg [setup failed]\n--- PASS: TestSnap\nok  \tpkg"),
    ])
    def test_a_tests_own_output_never_fakes_a_build_failure(self, rc, out):
        assert tr.classify(tr.GO, rc, out) != tr.COMPILE_ERROR


class TestDotnetNoTestsIsExitGated:
    """dotnet: a repo with no tests exits 0 by DEFAULT, so a NONZERO exit whose only
    signal is a no-tests marker is opt-in `TreatNoTestsAsError` or a BROKEN discovery —
    both real errors. It must fall through to FAILED, never be masked as a green skip."""

    def test_rc0_no_tests_marker_is_no_tests(self):
        assert tr.classify(tr.DOTNET, 0, "No test is available in App.Tests.dll.") == tr.NO_TESTS

    def test_rc_nonzero_only_no_tests_marker_is_failed(self):
        assert tr.classify(tr.DOTNET, 1, "No test is available in App.Tests.dll.") == tr.FAILED
        assert tr.classify(
            tr.DOTNET, 1, "no test matches the specified selection criteria") == tr.FAILED

    def test_abort_beside_an_empty_sibling_is_failed_not_skipped(self):
        # microsoft/vstest#2952: an abort emits NO per-test failure line and can land
        # beside a sibling empty project's marker — it must RED, not green-skip.
        out = "Test Run Aborted.\nNo test is available in Other.Tests.dll."
        assert tr.classify(tr.DOTNET, 1, out) == tr.FAILED

    def test_mtp_exit8_is_still_no_tests(self):
        assert tr.classify(tr.DOTNET, 8, "") == tr.NO_TESTS

    def test_green_run_printing_an_abort_string_stays_green(self):
        out = "Passed!  - Failed: 0, Passed: 3, Total: 3\n  log: 'Test Run Aborted' handled"
        assert tr.classify(tr.DOTNET, 0, out) == tr.PASSED

    def test_empty_project_beside_a_passing_project_is_passed_not_skipped(self):
        # `dotnet test` on a multi-project solution prints one summary line PER
        # project, so an empty project's "Total: 0" / "Passed: 0" trailer can appear
        # beside a sibling project's real "Total: 3" at rc==0 — must not mask green.
        out = (
            "Passed!  - Failed: 0, Passed: 0, Skipped: 0, Total: 0, Duration: 1 ms - Empty.Tests.dll\n"
            "Passed!  - Failed: 0, Passed: 3, Skipped: 0, Total: 3, Duration: 9 ms - App.Tests.dll"
        )
        assert tr.classify(tr.DOTNET, 0, out) == tr.PASSED

    def test_all_projects_empty_is_still_no_tests(self):
        out = "Passed!  - Failed: 0, Passed: 0, Skipped: 0, Total: 0, Duration: 1 ms - Empty.Tests.dll"
        assert tr.classify(tr.DOTNET, 0, out) == tr.NO_TESTS


class TestSwiftRunBannerGate:
    """swift: the run banner (`Test Suite`, `Executed N test(s)`, `Test run with N
    test(s)`, the swift-testing glyph recorder) proves tests actually executed. It gates
    the "nothing ran" branches, and the SINGULAR form must be tolerated or a ONE-test run
    yields no banner and false-greens as no_tests."""

    @pytest.mark.parametrize("out", [
        "Test Suite 'All tests' passed.\nExecuted 1 test, with 0 failures",
        "Test run with 1 test passed after 0.1 seconds.",
        "◇ Test example() started.\n✔ Test example() passed after 0.1 seconds.",
    ])
    def test_one_test_run_has_a_banner(self, out):
        assert tr.classify(tr.SWIFT, 0, out) == tr.PASSED

    def test_zero_trailer_beside_a_nonzero_count_is_a_pass(self):
        # swift test co-emits the UNUSED framework's "0 tests" trailer beside the used one.
        out = ("Test Suite 'All tests' passed.\nExecuted 5 tests, with 0 failures\n"
               "Test run with 0 tests passed after 0.001 seconds.")
        assert tr.classify(tr.SWIFT, 0, out) == tr.PASSED

    def test_failure_quoting_a_no_tests_marker_stays_failed(self):
        out = ("Test Suite 'All tests' failed.\nExecuted 3 tests, with 1 failure\n"
               '  XCTAssertEqual failed: expected "No matching test cases were run"')
        assert tr.classify(tr.SWIFT, 1, out) == tr.FAILED

    def test_no_target_is_no_tests_at_any_exit(self):
        assert tr.classify(tr.SWIFT, 1, "error: no tests found; create a target") == tr.NO_TESTS


class TestGradleZeroTestIsNeverGreen:
    """THE false-green this module exists to prevent: a Gradle project with ZERO TESTS
    must classify `no_tests` (the gate SKIPs, loudly), never `passed`.

    Zero-test evidence is the `:test` / `:integrationTest` EXECUTION task's own status
    line and NOTHING else. Gradle always prints the compile / resource / LIFECYCLE tasks
    around it, and a `\\S*[Tt]est\\S*` name-match sweeps them all in:

        > Task :compileTestJava NO-SOURCE       ← compile task
        > Task :processTestResources NO-SOURCE  ← resource task
        > Task :testClasses UP-TO-DATE          ← LIFECYCLE task — no actions, so
                                                  UP-TO-DATE, NEVER "NO-SOURCE"
        > Task :test NO-SOURCE                  ← the EXECUTION task

    An "EVERY test-named task is NO-SOURCE" rule is therefore defeated by
    `testClasses UP-TO-DATE` — the no-tests branch never fires and the empty project
    falls through to `passed`. Both directions are pinned here, on REAL output.
    """

    def test_zero_test_project_is_no_tests_not_passed(self):
        got = tr.classify(tr.GRADLE, 0, _GRADLE_ZERO_TEST)
        assert got == tr.NO_TESTS, f"zero-test Gradle project false-greened as {got}"

    def test_the_lifecycle_task_cannot_defeat_the_no_tests_branch(self):
        # The regression in one line: `testClasses` is UP-TO-DATE (never NO-SOURCE), so
        # any all()-over-test-NAMED-tasks rule reads False here and returns `passed`.
        assert "> Task :testClasses UP-TO-DATE" in _GRADLE_ZERO_TEST
        assert "> Task :test NO-SOURCE" in _GRADLE_ZERO_TEST
        assert tr.classify(tr.GRADLE, 0, _GRADLE_ZERO_TEST) == tr.NO_TESTS

    @pytest.mark.parametrize("name", [
        "compileTestJava", "processTestResources", "testClasses",
        "compileIntegrationTestJava", "integrationTestClasses", "testFixturesJar",
    ])
    def test_a_non_execution_test_named_task_is_never_zero_test_evidence(self, name):
        # NO-SOURCE on a compile/resource/lifecycle task says NOTHING about whether the
        # suite ran — only the execution task does. On its own it must not skip the gate.
        out = f"> Task :{name} NO-SOURCE\n> Task :test\n\nBUILD SUCCESSFUL in 2s"
        assert tr.classify(tr.GRADLE, 0, out) == tr.PASSED

    def test_real_passing_run_is_passed(self):
        assert tr.classify(tr.GRADLE, 0, _GRADLE_PASSING) == tr.PASSED

    def test_up_to_date_execution_task_is_non_evidence(self):
        # UP-TO-DATE proves a previous run's result is still valid → a pass, not a skip.
        assert tr.classify(tr.GRADLE, 0, _GRADLE_UP_TO_DATE) == tr.PASSED

    def test_skipped_execution_task_alone_is_zero_execution(self):
        # SKIPPED (`onlyIf {false}` / `-x`) executed NOTHING — with no sibling that
        # really ran, the run verified nothing and must classify no_tests, never green.
        assert tr.classify(
            tr.GRADLE, 0, "> Task :test SKIPPED\n\nBUILD SUCCESSFUL in 1s") == tr.NO_TESTS

    def test_check_run_with_one_empty_task_and_a_sibling_that_ran_is_passed(self):
        # A real run ANYWHERE wins — `gradle check` may have an empty `:test` beside an
        # `:integrationTest` that really executed. That is a verified pass, not an empty run.
        assert tr.classify(tr.GRADLE, 0, _GRADLE_CHECK_MIXED) == tr.PASSED

    def test_no_source_beside_an_up_to_date_sibling_is_passed_not_skipped(self):
        # UP-TO-DATE, unlike a fresh execution, doesn't set `ran` — but it still proves
        # the sibling suite has source (NO-SOURCE would have fired instead if it didn't).
        # A NO_TESTS rule keyed on "every execution task is NO-SOURCE" must not fire here.
        assert "> Task :test NO-SOURCE" in _GRADLE_NO_SOURCE_PLUS_UP_TO_DATE_SIBLING
        assert "> Task :integrationTest UP-TO-DATE" in _GRADLE_NO_SOURCE_PLUS_UP_TO_DATE_SIBLING
        got = tr.classify(tr.GRADLE, 0, _GRADLE_NO_SOURCE_PLUS_UP_TO_DATE_SIBLING)
        assert got == tr.PASSED, f"NO-SOURCE beside an UP-TO-DATE sibling false-skipped as {got}"

    def test_multi_module_all_execution_tasks_empty_is_no_tests(self):
        assert tr.classify(tr.GRADLE, 0, _GRADLE_MULTI_MODULE_ZERO) == tr.NO_TESTS

    def test_multi_module_one_module_ran_is_passed(self):
        out = ("> Task :app:testClasses UP-TO-DATE\n> Task :app:test NO-SOURCE\n"
               "> Task :lib:testClasses\n> Task :lib:test\n\nBUILD SUCCESSFUL in 3s")
        assert tr.classify(tr.GRADLE, 0, out) == tr.PASSED

    def test_failing_and_compile_error_are_unchanged(self):
        assert tr.classify(tr.GRADLE, 1, _GRADLE_FAILING) == tr.FAILED
        assert tr.classify(tr.GRADLE, 1, _GRADLE_COMPILE_ERROR) == tr.COMPILE_ERROR

    def test_android_agp_zero_test_module_is_no_tests_not_passed(self):
        # AGP prints support tasks that END in "Test" and run with a bare header
        # (javaPreCompileDebugUnitTest, packageDebugUnitTestForUnitTest). An
        # `endswith("Test")` name-match reads them as "a task ran" and discards the
        # genuine `:app:testDebugUnitTest NO-SOURCE` — greening a ZERO-TEST module.
        got = tr.classify(tr.GRADLE, 0, _GRADLE_ANDROID_ZERO_TEST)
        assert got == tr.NO_TESTS, f"zero-test Android module false-greened as {got}"

    def test_android_agp_passing_run_is_passed(self):
        assert tr.classify(tr.GRADLE, 0, _GRADLE_ANDROID_PASSING) == tr.PASSED

    @pytest.mark.parametrize("name,is_exec", [
        # genuine Test-TYPE EXECUTION tasks
        ("test", True), ("integrationTest", True), ("functionalTest", True),
        ("myServiceTest", True), ("testDebugUnitTest", True),
        ("testReleaseUnitTest", True), ("connectedDebugAndroidTest", True),
        ("checkoutFlowTest", True),   # leading word "checkout", NOT the verb "check"
        # NON-execution support / lifecycle tasks (some END in "Test")
        ("javaPreCompileDebugUnitTest", False), ("packageDebugUnitTestForUnitTest", False),
        ("javaPreCompileDebugAndroidTest", False), ("packageDebugAndroidTest", False),
        ("compileTestJava", False), ("compileTestKotlin", False),
        ("testClasses", False), ("processTestResources", False),
        ("processDebugUnitTestJavaRes", False), ("preDebugUnitTestBuild", False),
    ])
    def test_execution_task_predicate(self, name, is_exec):
        assert tr._is_gradle_test_execution_task(name) is is_exec


class TestJasmineNoSpecsIsExitGated:
    """jasmine: a NONZERO exit is NEVER a benign empty run — the same invariant
    `_classify_dotnet` documents in its own branch. jasmine v3+ exits nonzero on an
    empty run BECAUSE jasmine treats that as an error, and a broken run (a bad
    `spec_dir`, a helper that threw before loading) prints the same marker at a nonzero
    exit. Reporting either as a green SKIP lets the push proceed unverified."""

    def test_rc0_no_specs_is_no_tests(self):
        assert tr.classify(tr.JASMINE, 0, "Started\n\nNo specs found\nFinished") == tr.NO_TESTS

    @pytest.mark.parametrize("rc", [1, 2, 3])
    def test_nonzero_exit_with_only_a_no_specs_marker_is_failed(self, rc):
        got = tr.classify(tr.JASMINE, rc, "No specs found")
        assert got == tr.FAILED, f"nonzero jasmine run green-skipped as {got}"

    def test_nonzero_incomplete_no_specs_is_failed(self):
        out = "Started\nNo specs found\nIncomplete: No specs found"
        assert tr.classify(tr.JASMINE, 2, out) == tr.FAILED

    def test_real_spec_failure_stays_failed(self):
        assert tr.classify(tr.JASMINE, 3, "5 specs, 2 failures") == tr.FAILED

    def test_passing_run_stays_passed(self):
        assert tr.classify(tr.JASMINE, 0, "5 specs, 0 failures") == tr.PASSED

    def test_missing_dependency_is_still_env_error(self):
        # The env branch runs BEFORE the marker branch — a nonzero run whose real cause
        # is a missing dep is named env_error, not a bare failure.
        assert tr.classify(
            tr.JASMINE, 1, "Cannot find module 'jasmine-core'") == tr.ENV_ERROR


# ── Detection — argv ─────────────────────────────────────────────────────────────

_ARGV_CASES = [
    (["python3", "-m", "pytest", "tests/", "-q"], tr.PYTEST),
    (["pytest", "tests/"], tr.PYTEST),
    (["py.test"], tr.PYTEST),
    (["python", "-m", "unittest", "discover"], tr.UNITTEST),
    (["python", "manage.py", "test"], tr.DJANGO),
    (["./manage.py", "test", "app"], tr.DJANGO),
    (["python3", "-m", "nox"], tr.NOX),
    (["tox"], tr.TOX),
    (["python", "-I", "-m", "pytest", "tests/"], tr.PYTEST),
    (["python", "-X", "dev", "-m", "pytest"], tr.PYTEST),
    (["python", "-I", "manage.py", "test"], tr.DJANGO),
    (["npx", "vitest", "run"], tr.VITEST),
    (["npx", "jest"], tr.JEST),
    (["node_modules/.bin/jest"], tr.JEST),
    (["mocha", "test/"], tr.MOCHA),
    (["jasmine"], tr.JASMINE),
    (["ng", "test"], tr.KARMA),
    (["node", "--test"], tr.NODE_TEST),
    (["npx", "ava"], tr.AVA),
    (["bun", "test"], tr.BUN),
    (["bun", "test", "./src"], tr.BUN),
    (["deno", "test"], tr.DENO),
    (["deno", "test", "--allow-read", "tests/"], tr.DENO),
    (["go", "test", "./..."], tr.GO),
    (["cargo", "test"], tr.CARGO),
    (["cargo", "nextest", "run"], tr.NEXTEST),
    (["mvn", "test"], tr.MAVEN),
    (["./mvnw", "verify"], tr.MAVEN),
    (["gradle", "test"], tr.GRADLE),
    (["./gradlew", "test"], tr.GRADLE),
    (["mix", "test"], tr.MIX),
    (["dotnet", "test"], tr.DOTNET),
    (["bundle", "exec", "rspec"], tr.RSPEC),
    (["rspec", "spec/"], tr.RSPEC),
    (["./vendor/bin/phpunit"], tr.PHPUNIT),
    (["vendor/bin/pest"], tr.PEST),
    (["ctest", "--output-on-failure"], tr.CTEST),
    (["swift", "test"], tr.SWIFT),
    (["dart", "test"], tr.DART),
    (["flutter", "test"], tr.FLUTTER),
]


@pytest.mark.parametrize("argv,want", _ARGV_CASES,
                         ids=[" ".join(a) for a, _ in _ARGV_CASES])
def test_detect_from_argv(argv, want):
    info = tr.detect_runner(".", argv)
    assert info.runner == want
    assert info.source == "argv"


class TestWrapperDetection:
    @pytest.mark.parametrize("argv", [
        # A shell string naming NO recognized runner (`npm test`, `make test`), or a
        # preceding install step whose runner is likewise unrecognized.
        ["bash", "-lc", "npm ci && npm test"],
        ["sh", "-c", "make test"],
        ["bash", "-lc", "./run-tests.sh --all"],
        # TWO distinct runners: the wrapper's exit code + output are a MIX of two
        # runners' conventions — no single per-runner classifier can read it, so it
        # stays Tier-B opaque rather than guessing one.
        ["bash", "-lc", "pytest && npx jest"],
        # An unbalanced quote cannot be tokenized → stay opaque, never raise.
        ["bash", "-lc", "pytest 'unbalanced"],
    ])
    def test_opaque_shell_wrapper_is_unknown(self, argv):
        info = tr.detect_runner(".", argv)
        assert info.runner == tr.UNKNOWN
        assert info.source == "wrapper:shell"
        assert info.scopable is False

    @pytest.mark.parametrize("argv", [
        ["./run-tests.sh"],
        ["make", "check"],
        ["some-custom-binary", "--all"],
    ])
    def test_unrecognized_argv_is_unknown_wrapper(self, argv):
        info = tr.detect_runner(".", argv)
        assert info.runner == tr.UNKNOWN
        assert info.source == "wrapper:unrecognized"
        assert info.scopable is False

    def test_npm_test_with_no_resolvable_script_is_unknown(self, tmp_path):
        # `npm test` runs a package SCRIPT. P10 unwraps `scripts.test` when present;
        # with NO package.json (here an isolated empty tmp_path) there is nothing to
        # resolve, so it stays an opaque Tier-B wrapper. Pinned to a fixture cwd — NOT
        # the ambient `.` — because P10 reads package.json relative to the passed cwd.
        info = tr.detect_runner(str(tmp_path), ["npm", "test"])
        assert info.runner == tr.UNKNOWN
        assert info.scopable is False


class TestShellStringDetection:
    """`commit_push._split_test_command` wraps EVERY command carrying shell syntax
    (a `cd` step, a `VAR=val` prefix, an `&&` chain, a pipe) into `bash -lc "<cmd>"`,
    so reading argv[0] alone would call the most ordinary gate commands there are
    opaque — and hand them to the generic rc==0→passed classifier, false-greening a
    zero-test run of a silent-exit-0 runner. The runner is read back OUT of the string."""

    @pytest.mark.parametrize("cmd,want", [
        ("cd frontend && npx jasmine", tr.JASMINE),      # behind a `cd` step
        ("CI=1 go test ./...", tr.GO),                   # behind a VAR=val prefix
        ("CI=1 NODE_ENV=test npx jest --ci", tr.JEST),   # …several of them
        ("npm ci && npx vitest run", tr.VITEST),         # after an install step
        ("pytest -q | tee out.log", tr.PYTEST),          # mid-pipeline
        ("cd api && python3 -m pytest tests/ -q", tr.PYTEST),
        ("cd rs && cargo nextest run", tr.NEXTEST),
        ("(cd sub && bundle exec rspec)", tr.RSPEC),     # in a subshell, behind a launcher
        # A QUOTED operator needs no shell at all — `_command_needs_shell` over-matches
        # it (documented there as harmless). The tokenizer is quote-aware, so `A|B`
        # stays one token and the command is NOT split in half at the `|`.
        ("cd sub && go test -run 'A|B' ./...", tr.GO),
        ("cd app && bun test", tr.BUN),                  # a P10 Tier-C runner in the string
    ])
    def test_runner_read_out_of_the_shell_string(self, cmd, want):
        info = tr.detect_runner(".", commit_push._split_test_command(cmd))
        assert info.runner == want
        assert info.source == "shell"

    def test_shell_wrapped_runner_is_never_scopable(self):
        # P1's rule is "scopable iff argv AND recognized": a shell STRING is not an
        # argv we can append an exact-subset filter to (the runner may sit behind a
        # `cd`, mid-pipeline, or after an `&&` step), so a runner found inside one is
        # never scopable — even though the SAME runner as a bare argv is.
        assert tr.detect_runner(".", ["pytest", "-q"]).scopable is True
        assert tr.detect_runner(".", commit_push._split_test_command("cd api && pytest -q")).scopable is False

    def test_zero_test_run_behind_a_cd_is_not_a_false_green(self):
        # The defect this fix exists for: `cd frontend && npx jasmine` is wrapped in
        # `bash -lc`, jasmine EXITS 0 on zero specs, and an opaque wrapper's rc==0
        # meant `passed` — the gate reported a verified GREEN for a run that executed
        # NOTHING. It must classify no_tests (a loud "zero coverage" SKIP).
        out = "Randomized with seed 1\nNo specs found\nIncomplete: No specs found"
        info = tr.detect_runner(".", commit_push._split_test_command("cd frontend && npx jasmine"))
        assert tr.classify(info.runner, 0, out) == tr.NO_TESTS


class TestPythonInterpreterOptSkipping:
    """Leading `python -I/-X dev/-O/-W ignore` interpreter options must not hide
    the `-m <module>` / `manage.py` behind them."""

    @pytest.mark.parametrize("argv,want", [
        (["python", "-I", "-m", "pytest", "tests/"], tr.PYTEST),
        (["python3", "-X", "dev", "-m", "pytest"], tr.PYTEST),
        (["python", "-O", "-m", "unittest", "discover"], tr.UNITTEST),
        (["python", "-W", "ignore", "manage.py", "test"], tr.DJANGO),
        (["python3", "-X", "dev", "manage.py", "test", "app"], tr.DJANGO),
        (["python", "-I", "-m", "pytest"], tr.PYTEST),
    ])
    def test_interpreter_opts_skipped(self, argv, want):
        assert tr.detect_runner(".", argv).runner == want


class TestOpaqueWrapperNoFalseGreen:
    """The residual opaque wrappers (`npm test`, `make test`, `./run-tests.sh`) never
    name their runner ANYWHERE — not in argv, not in a shell string. `npm test` is the
    single most common gate command there is, and the jasmine / Karma / go / VSTest /
    gtest behind it all EXIT 0 on an empty run, so the generic classifier must not read
    a bare rc==0 as a verified pass."""

    @pytest.mark.parametrize("out", [
        "No specs found\nIncomplete: No specs found",            # jasmine
        "Executed 0 of 0 SUCCESS",                               # karma
        "ok  \tpkg\t0.5s [no tests to run]",                     # go, filtered to zero
        "No test is available in /app/bin/Tests.dll",            # dotnet / VSTest
        "[  PASSED  ] 0 tests",                                  # gtest
        "running 0 tests\ntest result: ok. 0 passed",            # cargo
        "Test run with 0 tests",                                 # swift
        "No tests found",                                        # jest
    ])
    def test_zero_test_markers_are_not_green(self, out):
        assert tr.classify(tr.UNKNOWN, 0, out) == tr.NO_TESTS

    @pytest.mark.parametrize("out", [
        "12 specs, 0 failures",                                  # jasmine
        "Executed 8 of 8 SUCCESS",                               # karma
        "ok  \tpkg\t0.5s",                                       # go
        "Tests:       3 passed, 3 total",                        # jest
        "459 passed in 4.20s",                                   # pytest
        "running 7 tests\ntest result: ok. 7 passed",            # cargo
        # A run that really executed must stay GREEN even when a SIBLING suite in the
        # same wrapper was empty — the go `[no test files]`-beside-a-passing-package
        # shape, which an unguarded marker scan would have masked as no_tests.
        "ok  \tpkga\t0.2s\n?   \tpkgc\t[no test files]",
        "",                                                      # no output at all
    ])
    def test_real_run_evidence_stays_green(self, out):
        assert tr.classify(tr.UNKNOWN, 0, out) == tr.PASSED

    def test_nonzero_exit_is_still_failed(self):
        # The nonzero path is untouched: through an opaque wrapper we cannot tell a
        # zero-test run from a broken step of the wrapper itself, and RED is the safe
        # answer either way.
        assert tr.classify(tr.UNKNOWN, 1, "No specs found") == tr.FAILED


# ── Detection — repo markers ─────────────────────────────────────────────────────

class TestDetectFromMarkers:
    """Marker-file fixtures per runner. Detection is called with NO command (argv
    empty) so it falls to the marker path — the reusable auto-detect surface."""

    def _detect(self, tmp_path):
        return tr.detect_runner(str(tmp_path), []).runner

    def test_pytest_ini(self, tmp_path):
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        assert self._detect(tmp_path) == tr.PYTEST

    def test_pyproject_pytest(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts = '-q'\n")
        assert self._detect(tmp_path) == tr.PYTEST

    def test_conftest(self, tmp_path):
        (tmp_path / "conftest.py").write_text("import pytest\n")
        assert self._detect(tmp_path) == tr.PYTEST

    def test_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/x\n")
        assert self._detect(tmp_path) == tr.GO

    def test_cargo_without_nextest(self, tmp_path, monkeypatch):
        (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
        # No .config/nextest.toml and the version probe fails → stable cargo.
        monkeypatch.setattr(tr, "_nextest_available", lambda base: False)
        assert self._detect(tmp_path) == tr.CARGO

    def test_cargo_with_nextest_config(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
        cfg = tmp_path / ".config"
        cfg.mkdir()
        (cfg / "nextest.toml").write_text("[profile.default]\n")
        assert self._detect(tmp_path) == tr.NEXTEST

    def test_maven(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project/>\n")
        assert self._detect(tmp_path) == tr.MAVEN

    def test_gradle(self, tmp_path):
        (tmp_path / "build.gradle").write_text("plugins { id 'java' }\n")
        assert self._detect(tmp_path) == tr.GRADLE

    def test_mix(self, tmp_path):
        (tmp_path / "mix.exs").write_text("defmodule X.MixProject do\nend\n")
        assert self._detect(tmp_path) == tr.MIX

    def test_rspec(self, tmp_path):
        (tmp_path / "Gemfile").write_text("gem 'rspec'\n")
        (tmp_path / "spec").mkdir()
        assert self._detect(tmp_path) == tr.RSPEC

    def test_minitest(self, tmp_path):
        (tmp_path / "Gemfile").write_text("gem 'minitest'\n")
        (tmp_path / "test").mkdir()
        assert self._detect(tmp_path) == tr.MINITEST

    def test_phpunit(self, tmp_path):
        (tmp_path / "phpunit.xml").write_text("<phpunit/>\n")
        assert self._detect(tmp_path) == tr.PHPUNIT

    def test_pest(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "Pest.php").write_text("<?php\n")
        assert self._detect(tmp_path) == tr.PEST

    def test_dotnet_csproj_with_test_sdk(self, tmp_path):
        (tmp_path / "App.Tests.csproj").write_text(
            '<Project><PackageReference Include="Microsoft.NET.Test.Sdk"/></Project>')
        assert self._detect(tmp_path) == tr.DOTNET

    def test_cmake_ctest(self, tmp_path):
        (tmp_path / "CMakeLists.txt").write_text("project(x)\nenable_testing()\n")
        assert self._detect(tmp_path) == tr.CTEST

    def test_swift_package(self, tmp_path):
        (tmp_path / "Package.swift").write_text(
            "// swift-tools-version:5.7\n.testTarget(name: \"xTests\")\n")
        assert self._detect(tmp_path) == tr.SWIFT

    def test_flutter_pubspec(self, tmp_path):
        (tmp_path / "pubspec.yaml").write_text("name: x\nflutter:\n  sdk: flutter\n")
        assert self._detect(tmp_path) == tr.FLUTTER

    def test_dart_pubspec(self, tmp_path):
        (tmp_path / "pubspec.yaml").write_text("name: x\nenvironment:\n  sdk: '>=3.0.0'\n")
        assert self._detect(tmp_path) == tr.DART

    def test_django_manage_py(self, tmp_path):
        (tmp_path / "manage.py").write_text("#!/usr/bin/env python\n")
        assert self._detect(tmp_path) == tr.DJANGO

    def test_no_markers_is_unknown(self, tmp_path):
        assert self._detect(tmp_path) == tr.UNKNOWN

    # ---- JS marker fixtures ----
    def test_vitest_config(self, tmp_path):
        (tmp_path / "vitest.config.ts").write_text("export default {}\n")
        assert self._detect(tmp_path) == tr.VITEST

    def test_jest_config(self, tmp_path):
        (tmp_path / "jest.config.js").write_text("module.exports = {}\n")
        assert self._detect(tmp_path) == tr.JEST

    def test_karma_conf(self, tmp_path):
        (tmp_path / "karma.conf.js").write_text("module.exports = function(){}\n")
        assert self._detect(tmp_path) == tr.KARMA

    def test_package_json_devdep_jest(self, tmp_path):
        (tmp_path / "package.json").write_text('{"devDependencies": {"jest": "^29"}}')
        assert self._detect(tmp_path) == tr.JEST

    def test_package_json_devdep_vitest(self, tmp_path):
        (tmp_path / "package.json").write_text('{"devDependencies": {"vitest": "^1"}}')
        assert self._detect(tmp_path) == tr.VITEST

    def test_package_json_no_known_runner_is_unknown(self, tmp_path):
        # A package.json whose test is an opaque npm script (P10 unwraps it) is not
        # a recognized runner from markers alone.
        (tmp_path / "package.json").write_text('{"scripts": {"test": "make test"}}')
        assert self._detect(tmp_path) == tr.UNKNOWN


class TestToxNoxForceGuard:
    """A tox.ini `[tox]` / noxfile.py FORCES the Tier-B wrapper runner, overriding
    the tox.ini→pytest signal — the whole test suite is really driven by tox/nox."""

    def test_tox_ini_beats_pytest_signal(self, tmp_path):
        (tmp_path / "tox.ini").write_text("[tox]\nenvlist = py311\n[pytest]\naddopts = -q\n")
        (tmp_path / "conftest.py").write_text("import pytest\n")  # pytest signal present
        assert tr.detect_runner(str(tmp_path), []).runner == tr.TOX

    def test_noxfile_beats_pytest_signal(self, tmp_path):
        (tmp_path / "noxfile.py").write_text("import nox\n")
        (tmp_path / "pytest.ini").write_text("[pytest]\n")  # pytest signal present
        assert tr.detect_runner(str(tmp_path), []).runner == tr.NOX

    def test_tox_ini_without_tox_section_is_pytest(self, tmp_path):
        # A tox.ini carrying ONLY a [pytest] section (used purely as a pytest config
        # home) is NOT a tox wrapper — it must still detect pytest.
        (tmp_path / "tox.ini").write_text("[pytest]\naddopts = -q\n")
        (tmp_path / "conftest.py").write_text("import pytest\n")
        assert tr.detect_runner(str(tmp_path), []).runner == tr.PYTEST

    def test_tox_and_nox_are_not_scopable(self):
        assert tr.detect_runner(".", ["tox"]).scopable is False
        assert tr.detect_runner(".", ["python3", "-m", "nox"]).scopable is False


# ── Gate wiring in commit_push.run_test_gate ─────────────────────────────────────

def _run_returning(rc, stdout="", stderr=""):
    """A `run` seam returning a fixed CompletedProcess — the gate never spawns a
    real subprocess, so the classifier is driven off the exit code + output alone."""
    def run(argv, *, cwd=None, timeout=None):
        return subprocess.CompletedProcess(list(argv), rc, stdout, stderr)
    return run


class TestGateNoTestsSkip:
    """A `no_tests` classification does NOT red the gate — it emits the loud SKIP
    notice and returns `skipped` (the free skill's 'no gate' posture; zero coverage,
    not green)."""

    def test_non_pytest_no_tests_skips_not_reds(self, monkeypatch, capsys):
        # A jasmine gate that exits 0 with "No specs found" — the silent-green class.
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "npx jasmine")
        status, tail = commit_push.run_test_gate(
            "/w", run=_run_returning(0, "Started\nNo specs found\n"), notice=_silent)
        assert status == "skipped"                          # NOT red, NOT green
        assert tail == ""
        out = capsys.readouterr().out
        assert "no tests detected for jasmine" in out
        assert "gate SKIPPED" in out and "not green" in out

    def test_npx_yes_flag_jasmine_no_tests_skips_not_green(self, monkeypatch, capsys):
        # `npx --yes jasmine`: the `--yes` flag hides the runner token, so detection
        # stays an opaque wrapper (UNKNOWN) — but the generic no-tests marker net
        # still refuses to green the silent "No specs found" rc==0 run. The gate
        # SKIPs loudly; the posture holds even without per-runner attribution.
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "npx --yes jasmine")
        status, tail = commit_push.run_test_gate(
            "/w", run=_run_returning(0, "Started\nNo specs found\n"), notice=_silent)
        assert status == "skipped"
        assert tail == ""
        assert "no tests detected for unknown" in capsys.readouterr().out

    def test_python_dash_i_dash_m_pytest_exit5_is_skip_not_red(self, monkeypatch, capsys):
        # `python -I -m pytest` (an interpreter option before `-m`) must still
        # resolve to pytest, not UNKNOWN — else exit 5 misclassifies as a generic
        # `failed` instead of `no_tests`.
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python -I -m pytest -q")
        status, tail = commit_push.run_test_gate(
            "/w", run=_run_returning(5, "no tests ran in 0.01s"), notice=_silent)
        assert status == "skipped"
        assert tail == ""
        assert "no tests detected for pytest" in capsys.readouterr().out

    def test_pytest_exit5_is_skip_not_red(self, monkeypatch, capsys):
        # pytest exit 5 (no tests collected) is a SKIP, not a red gate — the "pytest
        # exit 5 marked red" refutation must fail.
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -m pytest -q")
        status, tail = commit_push.run_test_gate(
            "/w", run=_run_returning(5, "no tests ran in 0.01s"), notice=_silent)
        assert status == "skipped"
        assert tail == ""
        assert "no tests detected for pytest" in capsys.readouterr().out

    def test_gradle_zero_test_project_skips_the_gate_never_greens_it(self, monkeypatch, capsys):
        # END-TO-END on the false-green: a real zero-test `gradle test` run exits 0, so
        # the gate MUST classify no_tests and SKIP (loudly). A "green" here is the bug.
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "gradle test")
        status, tail = commit_push.run_test_gate(
            "/w", run=_run_returning(0, _GRADLE_ZERO_TEST), notice=_silent)
        assert status == "skipped", f"zero-test Gradle gate returned {status!r}, not a skip"
        assert tail == ""
        assert "no tests detected for gradle" in capsys.readouterr().out

    def test_gradle_passing_run_greens_the_gate(self, monkeypatch, capsys):
        # The other direction: a real passing Gradle run must still be GREEN, not a skip.
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "gradle test")
        status, _ = commit_push.run_test_gate(
            "/w", run=_run_returning(0, _GRADLE_PASSING), notice=_silent)
        assert status == "green"
        assert "no tests detected" not in capsys.readouterr().out

    def test_android_zero_test_module_skips_never_greens(self, monkeypatch, capsys):
        # END-TO-END on the AGP false-green: `./gradlew testDebugUnitTest` on a zero-test
        # Android module exits 0; the gate MUST classify no_tests and SKIP, never green.
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "./gradlew testDebugUnitTest")
        status, tail = commit_push.run_test_gate(
            "/w", run=_run_returning(0, _GRADLE_ANDROID_ZERO_TEST), notice=_silent)
        assert status == "skipped", f"zero-test Android gate returned {status!r}, not a skip"
        assert tail == ""
        assert "no tests detected for gradle" in capsys.readouterr().out


class TestGateJasmineNonzeroReds:
    """A nonzero jasmine run whose only signal is a no-specs marker must RED the gate —
    it must never SKIP it and let the push proceed unverified."""

    def test_nonzero_no_specs_reds_the_gate(self, monkeypatch, capsys):
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "npx jasmine")
        status, tail = commit_push.run_test_gate(
            "/w", run=_run_returning(1, "Started\nNo specs found\n"), notice=_silent)
        assert status == "red", f"nonzero jasmine gate returned {status!r}, not red"
        # a plain `failed` → no class headline; the runner's own tail is shown
        assert "compile_error" not in tail and "env_error" not in tail
        assert "no tests detected" not in capsys.readouterr().out   # NOT a skip

    def test_rc0_no_specs_still_skips(self, monkeypatch, capsys):
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "npx jasmine")
        status, tail = commit_push.run_test_gate(
            "/w", run=_run_returning(0, "Started\nNo specs found\n"), notice=_silent)
        assert status == "skipped"
        assert tail == ""
        assert "no tests detected for jasmine" in capsys.readouterr().out


class TestGateEnvAndCompileError:
    """`env_error` / `compile_error` RED the gate with the CLASS named in the tail
    headline (never a bare nonzero exit)."""

    def test_env_error_reds_with_class_named(self, monkeypatch):
        # missing node_modules → env_error, NOT a spurious test failure.
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "npx vitest run")
        status, tail = commit_push.run_test_gate(
            "/w", run=_run_returning(1, "Cannot find module 'vite'"), notice=_silent)
        assert status == "red"
        assert "env_error" in tail
        assert "vitest" in tail

    def test_compile_error_reds_with_class_named(self, monkeypatch):
        # cargo exit 101 with a compile diagnostic → compile_error, NOT a test fail.
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "cargo test")
        status, tail = commit_push.run_test_gate(
            "/w", run=_run_returning(
                101, "error[E0308]: mismatched types\ncould not compile `x`"),
            notice=_silent)
        assert status == "red"
        assert "compile_error" in tail

    def test_pytest_env_error_runner_missing(self, monkeypatch):
        # python3 present but pytest not installed → env, not a spurious failure.
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -m pytest -q")
        status, tail = commit_push.run_test_gate(
            "/w", run=_run_returning(1, "No module named pytest"), notice=_silent)
        assert status == "red"
        assert "env_error" in tail

    def test_pytest_compile_error_exit2(self, monkeypatch):
        # A collection/import error (exit 2) → compile_error headline.
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -m pytest -q")
        status, tail = commit_push.run_test_gate(
            "/w", run=_run_returning(2, "ERROR tests/x.py - ImportError: no mod"),
            notice=_silent)
        assert status == "red"
        assert "compile_error" in tail

    def test_go_exit0_build_fail_reds_the_gate(self, monkeypatch, capsys):
        # golang/go#64286: the go gate EXITS 0 on a test-less package's build error —
        # the gate must RED with compile_error, never green-pass it through.
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "go test ./...")
        status, tail = commit_push.run_test_gate(
            "/w", run=_run_returning(
                0, "# example.com/a\n./a.go:5:9: undefined: Foo\nok  \texample.com/b\t0.2s"),
            notice=_silent)
        assert status == "red"
        assert "compile_error" in tail and "go" in tail
        assert "no tests detected" not in capsys.readouterr().out   # NOT a green skip

    def test_dotnet_nonzero_only_no_tests_reds_the_gate(self, monkeypatch, capsys):
        # A nonzero exit whose only signal is a no-tests marker (TreatNoTestsAsError /
        # broken discovery) must RED as a plain failure — never a green no-tests SKIP.
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "dotnet test")
        status, tail = commit_push.run_test_gate(
            "/w", run=_run_returning(1, "No test is available in App.Tests.dll."),
            notice=_silent)
        assert status == "red"
        # a plain `failed` → no class headline; the runner's own tail is shown
        assert "compile_error" not in tail and "env_error" not in tail
        assert "no tests detected" not in capsys.readouterr().out   # NOT a skip

    def test_dotnet_rc0_no_tests_still_skips(self, monkeypatch, capsys):
        # The rc==0 no-tests path is unchanged: a genuinely empty run still SKIPs.
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "dotnet test")
        status, tail = commit_push.run_test_gate(
            "/w", run=_run_returning(0, "No test is available in App.Tests.dll."),
            notice=_silent)
        assert status == "skipped"
        assert tail == ""
        assert "no tests detected for dotnet" in capsys.readouterr().out


class TestGatePassFailUnchanged:
    """The pass / fail paths stay as before F2: pass is green, a genuine failure reds
    with NO class headline (the tail is the runner's own output)."""

    def test_pass_is_green(self, monkeypatch):
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "npx vitest run")
        status, _ = commit_push.run_test_gate(
            "/w", run=_run_returning(0, "Test Files  3 passed (3)"), notice=_silent)
        assert status == "green"

    def test_fail_reds_no_headline(self, monkeypatch):
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "npx vitest run")
        status, tail = commit_push.run_test_gate(
            "/w", run=_run_returning(1, "FAIL  src/a.test.ts > adds"), notice=_silent)
        assert status == "red"
        assert "FAIL  src/a.test.ts" in tail
        assert "compile_error" not in tail and "env_error" not in tail


class TestFreeSkillNoGatePostureUnchanged:
    """The classifier wiring must NOT change the free skill's no-detectable-suite
    posture: with no configured command and no ``tests/`` dir the gate SKIPS (loud,
    never red), exactly as before F2. The 'no-tests-dir posture changed' refutation
    must fail."""

    def test_no_command_and_no_tests_dir_still_skips(self, monkeypatch, tmp_path):
        monkeypatch.delenv("BUDDHI_TEST_COMMAND", raising=False)  # (conftest also clears it)
        notices = []

        def notice(action, detail="", *, status="do", hint=None):
            notices.append((action, status))
            return ""

        status, tail = commit_push.run_test_gate(str(tmp_path), notice=notice)
        assert status == "skipped"
        assert tail == ""
        assert ("test-gate", "skip") in notices          # the loud no-suite notice

    def test_tests_dir_autodetects_pytest_default_and_greens(self, monkeypatch, tmp_path):
        # A tests/ dir → the pytest-default command auto-detects; a real pass is green
        # (the auto-detect posture is intact, just now routed through the classifier).
        monkeypatch.delenv("BUDDHI_TEST_COMMAND", raising=False)
        (tmp_path / "tests").mkdir()
        status, _ = commit_push.run_test_gate(
            str(tmp_path), run=_run_returning(0, "5 passed in 0.1s"), notice=_silent)
        assert status == "green"


# ── P10 · npm / yarn / pnpm / bun package-script UNWRAP ───────────────────────────

class TestP10PackageScriptUnwrap:
    """`npm test` / `yarn test` / `pnpm test` (and `<mgr> run <name>`) run a
    package.json SCRIPT, not a runner binary. P10 reads `scripts.<name>` and resolves
    THROUGH it — a recognized single runner keeps the repo Tier-A (its own classifier +
    scoped triage), so a plain `npm test` repo is NEVER second-class. Only an
    unrecognized single tool or a multi-tool chain degrades to a Tier-B wrapper."""

    def _pkg(self, tmp_path, test_script, extra=None):
        data = {"scripts": {"test": test_script}}
        if extra:
            data["scripts"].update(extra)
        (tmp_path / "package.json").write_text(json.dumps(data))
        return str(tmp_path)

    @pytest.mark.parametrize("cmd", [
        ["npm", "test"], ["npm", "run", "test"], ["npm", "t"],
        ["yarn", "test"], ["yarn", "run", "test"],
        ["pnpm", "test"], ["pnpm", "run", "test"],
        ["npm", "--silent", "run", "test"],
    ])
    def test_vitest_script_stays_tier_a(self, tmp_path, cmd):
        base = self._pkg(tmp_path, "vitest run")
        info = tr.detect_runner(base, cmd)
        assert info.runner == tr.VITEST, f"{cmd} should unwrap scripts.test → vitest"
        assert info.scopable is True                    # Tier-A: scoped triage feasible
        assert info.source == "npm-script"

    def test_script_args_after_dashdash_stay_tier_a(self, tmp_path):
        # `npm test -- --filter foo`: `--filter` is a workspace flag when it appears
        # BEFORE `--`, but after `--` it is forwarded verbatim to the script/runner —
        # it must not trip multi-project detection and degrade this to UNKNOWN.
        base = self._pkg(tmp_path, "vitest run")
        info = tr.detect_runner(base, ["npm", "test", "--", "--filter", "foo"])
        assert info.runner == tr.VITEST
        assert info.scopable is True
        assert info.source == "npm-script"
        # The forwarded args must actually be CARRIED into resolved_cmd — adapter
        # binding and scoped reruns read this, not the raw `npm test` argv — else a
        # behavior-changing flag silently never reaches the underlying runner.
        assert info.resolved_cmd == ["vitest", "run", "--filter", "foo"]

    @pytest.mark.parametrize("cmd", [
        ["yarn", "test", "--runxfail"],
        ["yarn", "run", "test", "--runxfail"],
        ["pnpm", "test", "--runxfail"],
        ["pnpm", "run", "test", "--runxfail"],
        ["bun", "run", "test", "--runxfail"],
    ])
    def test_yarn_pnpm_bun_forward_args_without_dashdash(self, tmp_path, cmd):
        # Unlike npm, yarn/pnpm/bun forward everything after the script name to the
        # script WITHOUT requiring a literal `--` (confirmed against real `yarn`/
        # `pnpm`/`bun` — `pnpm test --runxfail` runs the script WITH `--runxfail`).
        # Dropping it here would silently change the scoped rerun's pass/fail
        # semantics from the full gate's (an xfailed test that fails in the full
        # gate would falsely pass in isolation).
        base = self._pkg(tmp_path, "vitest run")
        info = tr.detect_runner(base, cmd)
        assert info.runner == tr.VITEST
        assert info.resolved_cmd == ["vitest", "run", "--runxfail"], cmd

    def test_npm_still_requires_dashdash_to_forward(self, tmp_path):
        # npm, unlike yarn/pnpm/bun, consumes/parses trailing args itself without a
        # literal `--` — it must NOT be treated as implicitly forwarding them.
        base = self._pkg(tmp_path, "vitest run")
        info = tr.detect_runner(base, ["npm", "test", "--runxfail"])
        assert info.runner == tr.VITEST
        assert info.resolved_cmd == ["vitest", "run"]

    @pytest.mark.parametrize("cmd", [["npm", "tst"], ["pnpm", "t"], ["pnpm", "tst"]])
    def test_npm_pnpm_t_tst_alias_to_test(self, tmp_path, cmd):
        # npm and pnpm both document `t`/`tst` as aliases for `test`.
        base = self._pkg(tmp_path, "vitest run")
        info = tr.detect_runner(base, cmd)
        assert info.runner == tr.VITEST, f"{cmd} should unwrap scripts.test → vitest"
        assert info.source == "npm-script"

    @pytest.mark.parametrize("cmd", [["yarn", "t"], ["yarn", "tst"]])
    def test_yarn_t_tst_is_literal_script_name_not_test_alias(self, tmp_path, cmd):
        # Yarn does NOT alias `t`/`tst` to `test` — `yarn <scriptName>` resolves the
        # LITERAL script name. A yarn project with no `scripts.t`/`scripts.tst` must
        # stay an opaque UNKNOWN wrapper, never silently classified via `scripts.test`.
        base = self._pkg(tmp_path, "vitest run")
        info = tr.detect_runner(base, cmd)
        assert info.runner == tr.UNKNOWN, f"{cmd} must not resolve through scripts.test"

    def test_yarn_t_resolves_its_own_literal_script(self, tmp_path):
        # When `scripts.t` DOES exist, `yarn t` must resolve through IT, not `test`.
        base = self._pkg(tmp_path, "jest", extra={"t": "vitest run"})
        info = tr.detect_runner(base, ["yarn", "t"])
        assert info.runner == tr.VITEST
        assert info.source == "npm-script"

    @pytest.mark.parametrize("cmd", [
        ["yarn", "-T", "test"],
        ["yarn", "run", "-T", "test"],
        ["yarn", "test", "-T"],
        ["yarn", "--top-level", "test"],
    ])
    def test_yarn_top_level_flag_degrades_to_multi_project(self, tmp_path, cmd):
        # Yarn Berry's `-T`/`--top-level` (confirmed via `yarn run -h=1`) checks the
        # ROOT workspace for the script instead of the current one — resolving
        # `scripts.<name>` from `cwd` would then bind to the wrong package.json, so
        # this must degrade the same as any other workspace-redirecting flag.
        base = self._pkg(tmp_path, "vitest run")
        info = tr.detect_runner(base, cmd)
        assert info.runner == tr.UNKNOWN
        assert info.source == "wrapper:multi-project"
        assert info.scopable is False

    def test_jest_script(self, tmp_path):
        base = self._pkg(tmp_path, "jest --coverage")
        assert tr.detect_runner(base, ["npm", "test"]).runner == tr.JEST

    def test_mocha_script(self, tmp_path):
        base = self._pkg(tmp_path, "mocha test/")
        assert tr.detect_runner(base, ["yarn", "test"]).runner == tr.MOCHA

    def test_yarn_r_flag_is_forwarded_not_a_workspace_flag(self, tmp_path):
        # pnpm's `-r`/`--recursive` is not a yarn flag at all (`yarn run -h=1` lists
        # `-T`/`--top-level`, not `-r`) — yarn forwards an unrecognized trailing
        # token verbatim to the script, so `yarn test -r setup.js` runs Mocha WITH
        # `-r setup.js` (Mocha's own `-r`/`--require`), it must stay Tier-A MOCHA,
        # not degrade to an opaque multi-project wrapper.
        base = self._pkg(tmp_path, "mocha test/")
        info = tr.detect_runner(base, ["yarn", "test", "-r", "setup.js"])
        assert info.runner == tr.MOCHA
        assert info.scopable is True
        assert info.resolved_cmd == ["mocha", "test/", "-r", "setup.js"]

    def test_pnpm_r_flag_still_degrades_to_multi_project(self, tmp_path):
        # pnpm's own `-r`/`--recursive` must still degrade — only yarn's non-flag
        # `-r` (forwarded) is the false positive being fixed above.
        base = self._pkg(tmp_path, "mocha test/")
        info = tr.detect_runner(base, ["pnpm", "-r", "test"])
        assert info.runner == tr.UNKNOWN
        assert info.source == "wrapper:multi-project"
        assert info.scopable is False

    def test_pnpm_r_flag_after_script_name_is_forwarded_not_a_workspace_flag(self, tmp_path):
        # `pnpm run <command> [<args>...]` (per `pnpm run --help`) forwards everything
        # after the script name to the invoked script — `pnpm test -r fE` runs pytest
        # WITH `-r fE` (pytest's own `-r`), it is not pnpm's `--recursive`. Only `-r`
        # BEFORE the script name (the case above) is pnpm's own workspace flag.
        base = self._pkg(tmp_path, "pytest")
        info = tr.detect_runner(base, ["pnpm", "test", "-r", "fE"])
        assert info.runner == tr.PYTEST
        assert info.scopable is True
        assert info.resolved_cmd == ["pytest", "-r", "fE"]

    def test_pnpm_run_flag_after_script_name_is_forwarded_not_a_workspace_flag(self, tmp_path):
        # The `run`-subcommand flag scan (P10b) must still end AT the script
        # positional: in `pnpm run test --filter unit`, `--filter unit` trails the
        # script name, so pnpm forwards it verbatim to the runner (vitest's own
        # `--filter`) — it is not pnpm's workspace filter and must not degrade the
        # unwrap to a multi-project wrapper.
        base = self._pkg(tmp_path, "vitest run")
        info = tr.detect_runner(base, ["pnpm", "run", "test", "--filter", "unit"])
        assert info.runner == tr.VITEST
        assert info.scopable is True
        assert info.resolved_cmd == ["vitest", "run", "--filter", "unit"]

    @pytest.mark.parametrize("trailing", [
        ["--filter", "unit"],
        ["--cwd", "packages/a"],
    ])
    def test_bun_run_flag_after_script_name_is_forwarded_not_a_workspace_flag(self, tmp_path, trailing):
        # bun's flag scan (P10b) must end AT the script positional too: `bun run
        # --help` shows `Usage: bun run [flags] <file or script>`, and on Bun 1.3.12
        # `bun run test --filter a` runs the `test` script WITH `--filter a` (node
        # then chokes on the unknown flag) — everything after the script name is
        # forwarded verbatim. So a `--filter`/`--cwd` TRAILING the script name is the
        # runner's own flag (vitest's `--filter`), NOT bun's workspace flag, and must
        # not degrade the unwrap to a multi-project wrapper. Only `--filter`/`--cwd`
        # BEFORE the script name (see `test_bun_filter_workspace_run_is_unknown`) are
        # bun's own workspace flags.
        base = self._pkg(tmp_path, "vitest run")
        info = tr.detect_runner(base, ["bun", "run", "test", *trailing])
        assert info.runner == tr.VITEST, trailing
        assert info.scopable is True
        assert info.resolved_cmd == ["vitest", "run", *trailing]

    def test_bun_test_with_filter_is_bun_runner_not_multi_project(self, tmp_path):
        # `bun test` is bun's OWN test runner (Tier-C), resolved by `_runner_from_argv`
        # — the workspace-flag scan must not hijack a trailing `--filter` on it and
        # mislabel it a multi-project wrapper. With the scan bounded to the pre-script
        # region (there is no `run` subcommand here), `bun test --filter x` stays the
        # bun runner.
        base = self._pkg(tmp_path, "vitest run")
        info = tr.detect_runner(base, ["bun", "test", "--filter", "x"])
        assert info.runner == tr.BUN
        assert info.source == "argv"

    def test_bin_path_script(self, tmp_path):
        # A repo pinning the local binary is still the recognized runner.
        base = self._pkg(tmp_path, "node_modules/.bin/jest")
        assert tr.detect_runner(base, ["npm", "test"]).runner == tr.JEST

    def test_env_prefix_script_stays_tier_a(self, tmp_path):
        # A `cross-env`/`VAR=val` env prefix wraps a REAL runner — it is stripped, not
        # degraded, so the underlying runner (and its classifier) still applies.
        for script, want in [
            ("cross-env NODE_ENV=test jest", tr.JEST),
            ("NODE_ENV=test vitest run", tr.VITEST),
            ("env CI=1 mocha test/", tr.MOCHA),
        ]:
            base = self._pkg(tmp_path, script)
            (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": script}}))
            assert tr.detect_runner(base, ["npm", "test"]).runner == want, script

    def test_script_reindirection_resolves(self, tmp_path):
        # `"test": "npm run test:ci"` → follow one hop to the real runner.
        base = self._pkg(tmp_path, "npm run test:ci", extra={"test:ci": "vitest run"})
        assert tr.detect_runner(base, ["npm", "test"]).runner == tr.VITEST

    def test_reindirection_does_not_forward_outer_args_through_npm_run(self, tmp_path):
        # `npm test -- --runxfail` with `"test": "npm run test:ci"`: npm appends the
        # forwarded args to the re-indirect command (`npm run test:ci --runxfail`), and
        # `npm run` forwards NOTHING to the inner script without its OWN `--` (verified
        # against npm 11.4.2: the inner runner sees argv `[]`). The resolved runner must
        # therefore NOT carry `--runxfail`, or a scoped rerun runs a command the gate
        # never did.
        base = self._pkg(tmp_path, "npm run test:ci", extra={"test:ci": "vitest run"})
        info = tr.detect_runner(base, ["npm", "test", "--", "--runxfail"])
        assert info.runner == tr.VITEST
        assert info.resolved_cmd == ["vitest", "run"]

    def test_reindirection_forwards_outer_args_through_yarn_run(self, tmp_path):
        # A yarn/pnpm/bun re-indirect forwards trailing args WITHOUT a `--`: `npm test --
        # --runxfail` with `"test": "yarn run test:ci"` runs `yarn run test:ci --runxfail`,
        # and yarn passes `--runxfail` on to the inner script — so the resolved runner DOES
        # carry it. The forwarding rule is re-applied per nesting level, not dropped.
        base = self._pkg(tmp_path, "yarn run test:ci", extra={"test:ci": "vitest run"})
        info = tr.detect_runner(base, ["npm", "test", "--", "--runxfail"])
        assert info.runner == tr.VITEST
        assert info.resolved_cmd == ["vitest", "run", "--runxfail"]

    @pytest.mark.parametrize("script", [
        "tsc && jest",              # chains a compile step + the runner
        "jest && eslint .",         # chains two tools
        "vitest run | tap-spec",    # pipes into a reporter
        "npm run lint; npm run t",  # sequences two scripts (spaced)
        "npm run lint;npm run t",   # sequences two scripts (unspaced ; — shlex-tokenized)
        "jest&&eslint",             # no-space chain (raw-substring scan would still see it, token scan too)
        "jest|tap-spec",            # no-space pipe
        "jest & webpack serve",     # single `&` backgrounds a second tool
        "a || b",                   # logical-or chain
        "jest > out.txt",           # redirection — output diverted, classifier blind
        "jest 2>&1",                # stderr redirection
        "jest\neslint",             # newline command separator (shlex treats \n as whitespace)
        "diff <(jest) <(other)",    # process substitution — argv[0] not even the runner
        "jest --maxWorkers=$(nproc)",  # command substitution (unquoted) — needs a shell
        'jest -t "$(pwd)"',         # command substitution in DOUBLE quotes — still executes
        'jest --config="$(pwd)/jest.config.js"',  # double-quoted $() embedded in a value
        "$(cat run-cmd.txt)",       # subshell — genuinely opaque
        "`cat cmd`",                # backtick command substitution
    ])
    def test_multi_tool_script_degrades_to_tier_b(self, tmp_path, script):
        base = self._pkg(tmp_path, script)
        info = tr.detect_runner(base, ["npm", "test"])
        assert info.runner == tr.UNKNOWN, f"{script!r} must degrade to Tier-B"
        assert info.scopable is False

    @pytest.mark.parametrize("script", [
        "MODE=$CI_MODE pytest",         # env prefix — re-run would set the LITERAL `$CI_MODE`
        "pytest -c $PYTEST_CONFIG",     # config selector — re-run reads a different config
        "pytest $EXTRA_FLAGS",          # `--runxfail` under the gate, dropped by the re-run
        "jest --maxWorkers=$JOBS",      # bare `$VAR`
        "vitest run --reporter=${REPORTER}",   # braced form
        "jest --maxWorkers=$(nproc)",   # `$(…)` — already Tier-B via _script_chains_tools
        "pytest -k $1",                 # positional parameter
    ])
    def test_shell_expansion_script_degrades_to_tier_b(self, tmp_path, script):
        # The package manager runs a script through a SHELL, which expands `$VAR`
        # before the runner starts; `shlex.split` does not, so the token survives
        # LITERAL into `resolved_cmd` and the shell-less scoped re-run would verify
        # under a different env/config than the gate that failed — silently, since
        # a literal `$CI_MODE` is still a valid (wrong) value. Honest degrade.
        base = self._pkg(tmp_path, script)
        info = tr.detect_runner(base, ["npm", "test"])
        assert info.runner == tr.UNKNOWN, f"{script!r} must degrade to Tier-B"
        assert info.scopable is False
        assert info.resolved_cmd is None

    @pytest.mark.parametrize("script,want", [
        # A shell metacharacter INSIDE a quoted argument is NOT composition — the run
        # is still a single recognized runner and must stay Tier-A. (Adversarial
        # regression: a raw-string scan misread the quoted `|` as a shell pipe and
        # false-downgraded these to Tier-B, which also false-REDs a legit no-tests run.)
        ("jest --testPathPattern='(unit|integration)'", tr.JEST),
        ('jest --testPathPattern="unit|integration"', tr.JEST),
        ("jest -t 'renders|updates'", tr.JEST),
        ("vitest run 'src/(a|b).test.ts'", tr.VITEST),
        ("mocha --grep 'foo|bar'", tr.MOCHA),
        # A `$` that introduces NO expansion is literal to the shell too, so
        # `shlex.split` reproduces it exactly — a trailing regex anchor must not be
        # swept up by the expansion degrade (`$VAR` forms are covered above, in
        # `test_shell_expansion_script_degrades_to_tier_b`).
        ("jest -t 'renders$'", tr.JEST),
        (r"jest --testPathPattern='\.test\.js$'", tr.JEST),
        ("vitest run --reporter=dot", tr.VITEST),
    ])
    def test_quoted_operator_or_anchor_in_arg_stays_tier_a(self, tmp_path, script, want):
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": script}}))
        info = tr.detect_runner(str(tmp_path), ["npm", "test"])
        assert info.runner == want, f"{script!r} should stay Tier-A ({want})"
        assert info.scopable is True

    def test_unrecognized_single_tool_degrades(self, tmp_path):
        # react-scripts wraps jest but is not itself a recognized runner → honest
        # whole-suite degrade rather than a wrong scoped-triage claim.
        base = self._pkg(tmp_path, "react-scripts test")
        assert tr.detect_runner(base, ["npm", "test"]).runner == tr.UNKNOWN

    def test_cyclic_script_terminates_unknown(self, tmp_path):
        # `"test": "npm test"` must not loop — bounded to UNKNOWN.
        base = self._pkg(tmp_path, "npm test")
        assert tr.detect_runner(base, ["npm", "test"]).runner == tr.UNKNOWN

    @pytest.mark.parametrize("hook", ["pretest", "posttest"])
    def test_lifecycle_hook_degrades_to_tier_b(self, tmp_path, hook):
        # npm runs `pretest`/`posttest` automatically around `npm test`
        # (docs.npmjs.com/cli/v11/using-npm/scripts#pre--post-scripts) — the
        # combined output is not attributable to `scripts.test`'s runner alone,
        # and a scoped rerun of ONLY `test` would skip the hook the full gate
        # actually ran under. Must degrade honestly rather than misclassify.
        base = self._pkg(tmp_path, "vitest run", extra={hook: "node setup.js"})
        info = tr.detect_runner(base, ["npm", "test"])
        assert info.runner == tr.UNKNOWN
        assert info.scopable is False
        assert info.source == "wrapper:npm-script-lifecycle"

    def test_blank_lifecycle_hook_stays_tier_a(self, tmp_path):
        # An empty/whitespace-only pretest entry is not a real hook — npm treats
        # it as absent, so this must not false-degrade a plain `npm test` repo.
        base = self._pkg(tmp_path, "vitest run", extra={"pretest": "  "})
        info = tr.detect_runner(base, ["npm", "test"])
        assert info.runner == tr.VITEST
        assert info.scopable is True

    def test_missing_script_is_unknown(self, tmp_path):
        (tmp_path / "package.json").write_text('{"scripts": {"build": "tsc"}}')
        assert tr.detect_runner(str(tmp_path), ["npm", "test"]).runner == tr.UNKNOWN

    def test_malformed_package_json_never_raises(self, tmp_path):
        (tmp_path / "package.json").write_text("{ not valid json ,,, ")
        assert tr.detect_runner(str(tmp_path), ["npm", "test"]).runner == tr.UNKNOWN

    def test_pathologically_nested_package_json_degrades_not_raises(self, tmp_path):
        # `json.loads` overflows the C scanner's recursion limit with RecursionError
        # — not ValueError — on a pathologically nested document; it must take the
        # same documented Tier-B degrade, never escape through `detect_runner`.
        (tmp_path / "package.json").write_text("[" * 200000 + "]" * 200000)
        info = tr.detect_runner(str(tmp_path), ["npm", "test"])
        assert info.runner == tr.UNKNOWN
        assert info.scopable is False

    def test_marker_only_unwrap_keeps_tier_a(self, tmp_path):
        # Marker-mode (no command) also unwraps scripts.test → a script-only repo is
        # still recognized for auto-detect / F2.
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
        assert tr.detect_runner(str(tmp_path), []).runner == tr.VITEST


class TestNpmBinPathDirs:
    """`npm_bin_path_dirs` backs the scoped-rerun PATH fix for a P10 npm-script
    unwrap — it must mirror npm's own ancestor `node_modules/.bin` PATH-prepend
    walk (docs.npmjs.com/cli/v11/using-npm/scripts#path) so a locally-installed
    binary (`cross-env`) resolves the same way for a direct subprocess.run as it
    does inside `npm test`."""

    def test_finds_own_and_ancestor_bin_dirs(self, tmp_path):
        root_bin = tmp_path / "node_modules" / ".bin"
        root_bin.mkdir(parents=True)
        pkg = tmp_path / "packages" / "app"
        pkg_bin = pkg / "node_modules" / ".bin"
        pkg_bin.mkdir(parents=True)
        dirs = tr.npm_bin_path_dirs(str(pkg))
        assert dirs[0] == str(pkg_bin)          # nearest first
        assert str(root_bin) in dirs

    def test_no_node_modules_anywhere_returns_empty(self, tmp_path):
        leaf = tmp_path / "a" / "b"
        leaf.mkdir(parents=True)
        assert tr.npm_bin_path_dirs(str(leaf)) == []

    def test_never_raises_on_bogus_base(self):
        assert tr.npm_bin_path_dirs("") == []
        assert tr.npm_bin_path_dirs(None) == []


class TestNpmScriptEnv:
    """`npm_script_env` rebuilds the script-visible variables npm injects into any
    `scripts.<name>` child (docs.npmjs.com/cli/v11/using-npm/scripts#environment).
    A scoped re-run bypasses npm, so without them a conftest / setup file that
    branches on `npm_lifecycle_event` runs a DIFFERENT branch than the full gate —
    an isolation verdict about an environment the gate never ran under."""

    def test_reconstructs_the_script_visible_vars(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps(
            {"scripts": {"test": "cross-env SPECIAL=1 pytest"}}))
        info = tr.detect_runner(str(tmp_path), ["npm", "test"])
        assert info.source == "npm-script" and info.script_name == "test"
        env = tr.npm_script_env(str(tmp_path), info)
        assert env["npm_lifecycle_event"] == "test"
        assert env["npm_lifecycle_script"] == "cross-env SPECIAL=1 pytest"
        assert env["npm_package_json"] == str(tmp_path / "package.json")
        assert env["INIT_CWD"] == str(tmp_path)

    def test_omits_init_cwd_for_a_bun_resolved_unwrap(self, tmp_path):
        # Bun does not set `INIT_CWD` on a `bun run` child (checked against Bun
        # 1.2.14) — synthesizing it would hand a scoped re-run a variable the full
        # gate's child never saw, which a conftest branching on its presence could
        # take a different path over.
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "pytest"}}))
        info = tr.detect_runner(str(tmp_path), ["bun", "run", "test"])
        assert info.source == "npm-script" and info.package_manager == "bun"
        env = tr.npm_script_env(str(tmp_path), info)
        assert "INIT_CWD" not in env
        assert env["npm_lifecycle_event"] == "test"

    def test_mixed_chain_keys_init_cwd_on_the_head_manager(self, tmp_path):
        # `npm test` → `"test": "bun run inner"` → pytest: npm — the manager the
        # gate actually invoked — sets INIT_CWD on its child, and bun (which never
        # sets it) passes it through to the runner. Keying the omission on the
        # TERMINAL manager dropped a variable the gate's child really saw. The
        # lifecycle event stays the TERMINAL script's — bun re-sets that one for
        # its own child.
        (tmp_path / "package.json").write_text(json.dumps(
            {"scripts": {"test": "bun run inner", "inner": "pytest"}}))
        info = tr.detect_runner(str(tmp_path), ["npm", "test"])
        assert info.source == "npm-script" and info.script_name == "inner"
        assert info.package_manager == "npm"       # HEAD of the chain, not bun
        env = tr.npm_script_env(str(tmp_path), info)
        assert env["INIT_CWD"] == str(tmp_path)
        assert env["npm_lifecycle_event"] == "inner"

    def test_mirror_chain_omits_init_cwd_when_head_is_bun(self, tmp_path):
        # The MIRROR of the chain above: `bun run test` → `"test": "npm run inner"` →
        # pytest. Keying the INIT_CWD omission off the HEAD (bun) correctly OMITS it,
        # and keying off "every manager in the chain is bun" (i.e. INCLUDING it here
        # because `npm` textually appears) would be WRONG: bun runs an inner `npm run`
        # through its OWN script runner (auto-aliased — verified on Bun 1.3.12: the
        # terminal runner's npm_config_user_agent stays `bun/…` and INIT_CWD is UNSET,
        # even under `--shell=system`), so the real gate's child sees NO INIT_CWD. The
        # textual `npm` never actually runs, so synthesizing INIT_CWD would hand a
        # scoped re-run a variable the full gate never had.
        (tmp_path / "package.json").write_text(json.dumps(
            {"scripts": {"test": "npm run inner", "inner": "pytest"}}))
        info = tr.detect_runner(str(tmp_path), ["bun", "run", "test"])
        assert info.source == "npm-script" and info.script_name == "inner"
        assert info.package_manager == "bun"       # HEAD of the chain, not npm
        env = tr.npm_script_env(str(tmp_path), info)
        assert "INIT_CWD" not in env
        assert env["npm_lifecycle_event"] == "inner"

    def test_reports_the_terminal_script_of_a_reindirection(self, tmp_path):
        # npm runs the INNER script as its own lifecycle event, so `npm run test:ci`
        # behind `test` makes `npm_lifecycle_event` "test:ci" — not "test".
        (tmp_path / "package.json").write_text(json.dumps(
            {"scripts": {"test": "npm run test:ci", "test:ci": "pytest -q"}}))
        info = tr.detect_runner(str(tmp_path), ["npm", "test"])
        assert info.script_name == "test:ci"
        env = tr.npm_script_env(str(tmp_path), info)
        assert env["npm_lifecycle_event"] == "test:ci"
        assert env["npm_lifecycle_script"] == "pytest -q"

    def test_never_fabricates_npm_config_vars(self, tmp_path):
        # npm_config_* comes from npm's own npmrc cascade — unreproducible without
        # npm, and guessing would swap an incomplete env for a fabricated one.
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "pytest"}}))
        info = tr.detect_runner(str(tmp_path), ["npm", "test"])
        env = tr.npm_script_env(str(tmp_path), info)
        assert not [k for k in env if k.startswith("npm_config_")]

    def test_empty_for_non_npm_script_detections(self, tmp_path):
        assert tr.npm_script_env(str(tmp_path), None) == {}
        argv_info = tr.detect_runner(str(tmp_path), ["python3", "-m", "pytest"])
        assert argv_info.source != "npm-script"
        assert tr.npm_script_env(str(tmp_path), argv_info) == {}
        # a Tier-B degrade carries no script_name → nothing to reconstruct
        (tmp_path / "package.json").write_text(json.dumps(
            {"scripts": {"test": "eslint . && jest"}}))
        tier_b = tr.detect_runner(str(tmp_path), ["npm", "test"])
        assert tier_b.runner == tr.UNKNOWN
        assert tr.npm_script_env(str(tmp_path), tier_b) == {}

    def test_never_raises_on_bogus_base(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "pytest"}}))
        info = tr.detect_runner(str(tmp_path), ["npm", "test"])
        assert tr.npm_script_env("", info) == {}       # no package.json at cwd
        assert tr.npm_script_env(None, info) == {}


class TestP10OpaqueAndMultiProjectWrappers:
    """Genuinely-opaque wrappers and multi-project orchestrators are FORCED to a
    Tier-B honest degrade (UNKNOWN): the gate + classifier still work, but scoped
    triage is never attempted on a command we cannot see through."""

    @pytest.mark.parametrize("argv", [
        ["make", "test"],
        ["make", "check"],
        ["./run-tests.sh"],
        ["nx", "run-many", "-t", "test"],
        ["turbo", "run", "test"],
        ["bazel", "test", "//..."],
        ["dbt", "test"],
        ["docker", "compose", "run", "test"],
    ])
    def test_opaque_wrapper_is_unknown_unscopable(self, argv):
        info = tr.detect_runner(".", argv)
        assert info.runner == tr.UNKNOWN
        assert info.scopable is False

    @pytest.mark.parametrize("argv,src", [
        (["pnpm", "-r", "test"], "wrapper:multi-project"),
        (["pnpm", "--recursive", "test"], "wrapper:multi-project"),
        (["pnpm", "--filter", "pkg", "test"], "wrapper:multi-project"),
        (["npm", "test", "-w", "packages/api"], "wrapper:multi-project"),
        (["npm", "test", "--workspaces"], "wrapper:multi-project"),
    ])
    def test_multi_project_run_is_unknown(self, tmp_path, argv, src):
        # Even with a resolvable scripts.test, a workspace/recursive run spans multiple
        # cwds — opaque, never unwrapped.
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
        info = tr.detect_runner(str(tmp_path), argv)
        assert info.runner == tr.UNKNOWN
        assert info.source == src
        assert info.scopable is False

    @pytest.mark.parametrize("argv", [
        ["npm", "--prefix=child", "test"],
        ["npm", "test", "--prefix=child"],
        ["npm", "test", "--prefix", "child"],
        ["yarn", "--cwd", "child", "test"],
        ["yarn", "--cwd=child", "test"],
        ["pnpm", "-C", "child", "test"],
        ["pnpm", "--dir", "child", "test"],
        ["pnpm", "--dir=child", "test"],
        ["bun", "--cwd=child", "run", "test"],
    ])
    def test_package_root_cwd_selector_is_unknown(self, tmp_path, argv):
        # npm's `--prefix`, yarn's `--cwd`, pnpm's `-C`/`--dir`, and bun's `--cwd` each
        # read `<dir>/package.json`, not the gate's cwd (confirmed against real npm
        # 11.4.2, yarn 1.22.22, pnpm 9.15.9, bun: `npm --prefix=child test` /
        # `npm test --prefix=child` / `yarn --cwd child test` / `pnpm -C child test`
        # all run `child/package.json`'s `scripts.test`). A ROOT package.json with a
        # DIFFERENT `scripts.test` here proves the gate does not misroute to it.
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "mocha"}}))
        child = tmp_path / "child"
        child.mkdir()
        (child / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
        info = tr.detect_runner(str(tmp_path), argv)
        assert info.runner == tr.UNKNOWN
        assert info.source == "wrapper:multi-project"
        assert info.scopable is False

    def _workspace(self, tmp_path, pnpm=False):
        # Root scripts.test=vitest, pkgs/a scripts.test=jest — a root unwrap that
        # classified the whole run as vitest would let a scoped re-run silently
        # skip pkgs/a's jest suite entirely.
        (tmp_path / "package.json").write_text(json.dumps(
            {"workspaces": ["pkgs/*"], "scripts": {"test": "vitest run"}}))
        pkg = tmp_path / "pkgs" / "a"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
        if pnpm:
            (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'pkgs/*'\n")
        return str(tmp_path)

    @pytest.mark.parametrize("argv", [
        ["bun", "run", "--filter=*", "test"],
        ["bun", "run", "--filter", "*", "test"],
        ["bun", "--filter=*", "run", "test"],
    ])
    def test_bun_filter_workspace_run_is_unknown(self, tmp_path, argv):
        # `bun run --filter <pat> <script>` runs the script in every MATCHING
        # workspace package (Bun's "Filter" docs) — a multi-package run whose
        # root-script unwrap skips the other packages' suites (#540 round-4
        # regression, audited 2026-07-18). Must degrade like pnpm's `--filter`.
        base = self._workspace(tmp_path)
        info = tr.detect_runner(base, argv)
        assert info.runner == tr.UNKNOWN, argv
        assert info.source == "wrapper:multi-project"
        assert info.scopable is False

    @pytest.mark.parametrize("argv", [
        ["bun", "run", "-F=*", "test"],
        ["bun", "run", "-F*", "test"],
        ["bun", "run", "-F", "*", "test"],
        ["bun", "-F=*", "run", "test"],
        ["bun", "-F*", "run", "test"],
    ])
    def test_bun_short_filter_workspace_run_is_unknown(self, tmp_path, argv):
        # `-F` is bun's documented short alias for `--filter` (`bun run --help`
        # lists `-F, --filter=<val>`) and all its forms — `-F=<pat>`, `-F<pat>`
        # (no `=`), and space-separated `-F <pat>` — filter-run the matching
        # workspace packages just like `--filter=<pat>` (confirmed against real
        # Bun 1.3.12), so the short form must degrade the same way (P10b round-2,
        # 2026-07-19).
        base = self._workspace(tmp_path)
        info = tr.detect_runner(base, argv)
        assert info.runner == tr.UNKNOWN, argv
        assert info.source == "wrapper:multi-project"
        assert info.scopable is False

    @pytest.mark.parametrize("argv", [
        ["bun", "run", "--shell", "system", "--filter=*", "test"],
        ["bun", "run", "--shell", "system", "--filter", "*", "test"],
        ["bun", "run", "--env-file", ".env.test", "--filter=*", "test"],
        ["bun", "run", "--elide-lines", "5", "-F", "*", "test"],
    ])
    def test_bun_value_flag_before_filter_still_degrades(self, tmp_path, argv):
        # A bun value-taking flag in SPACE-separated form (`--shell system`,
        # `--env-file <path>`, `--elide-lines <n>`) before a later `--filter`/`-F`
        # must have its value token skipped WITH the flag; otherwise the scan stops
        # at the value (`system`), misses the workspace filter, and `_package_script_
        # target` treats the value as the script positional — misrouting a filtered
        # workspace run to a single scopable script (`bun run --shell system
        # --filter='*' test` runs the MATCHING workspace packages, confirmed on Bun
        # 1.3.12; P10b, 2026-07-19).
        base = self._workspace(tmp_path)
        info = tr.detect_runner(base, argv)
        assert info.runner == tr.UNKNOWN, argv
        assert info.source == "wrapper:multi-project"
        assert info.scopable is False

    @pytest.mark.parametrize("argv", [
        ["bun", "run", "-r", "./setup.js", "--filter=*", "test"],
        ["bun", "run", "--preload", "./setup.js", "--filter", "*", "test"],
        ["bun", "run", "--require", "./setup.js", "--filter=*", "test"],
        ["bun", "run", "--import", "./setup.js", "-F", "*", "test"],
    ])
    def test_bun_preload_flag_before_filter_still_degrades(self, tmp_path, argv):
        # `bun run --help` lists `-r, --preload=<val>` plus `--require`/`--import` as
        # Node-compat aliases of `--preload`, none of which `_BUN_VALUE_FLAGS` covered
        # before P10b round-2 — `_run_flag_prefix_len` stopped its scan at the preload
        # module path and never reached a later `--filter`, reopening the
        # workspace-suite skip this patch closes (confirmed against real Bun 1.3.12:
        # `bun run -r ./setup.js --filter='*' test` preloads `./setup.js` into every
        # MATCHING workspace package, it does not run a `setup.js` script; 2026-07-19).
        base = self._workspace(tmp_path)
        info = tr.detect_runner(base, argv)
        assert info.runner == tr.UNKNOWN, argv
        assert info.source == "wrapper:multi-project"
        assert info.scopable is False

    @pytest.mark.parametrize("argv", [
        ["pnpm", "run", "--loglevel", "warn", "--filter=a", "test"],
        ["pnpm", "run", "--resume-from", "a", "--filter=a", "test"],
        ["bun", "run", "--install", "fallback", "--filter=a", "test"],
        ["bun", "run", "-d", "X:1", "--filter=a", "test"],
        ["bun", "run", "--define", "X:1", "-F", "a", "test"],
    ])
    def test_pnpm_bun_p10b_round3_value_flag_before_filter_still_degrades(self, tmp_path, argv):
        # pnpm's `--loglevel <level>` / `--resume-from <package>` and bun's
        # `--install <mode>` / `-d`/`--define <k:v>` are MANDATORY-value flags in
        # SPACE-separated form (confirmed on pnpm 10.33.0 / Bun 1.3.12: `pnpm run
        # --loglevel warn --filter=a test` and `bun run --install fallback
        # --filter=a test` both run only package `a`'s script) — before P10b
        # round-3 none of these were in `_PNPM_VALUE_FLAGS`/`_BUN_VALUE_FLAGS`, so
        # `_run_flag_prefix_len` stopped its scan at the flag's value and never
        # reached the later `--filter`, misrouting a filtered workspace run to a
        # single scopable script (2026-07-20).
        base = self._workspace(tmp_path, pnpm=argv[0] == "pnpm")
        info = tr.detect_runner(base, argv)
        assert info.runner == tr.UNKNOWN, argv
        assert info.source == "wrapper:multi-project"
        assert info.scopable is False

    @pytest.mark.parametrize("argv", [
        ["pnpm", "run", "--changed-files-ignore-pattern", "**/README.md", "--filter=a", "test"],
        ["pnpm", "run", "--test-pattern", "test/*", "--filter=a", "test"],
    ])
    def test_pnpm_p10b_round4_filtering_value_flag_before_filter_still_degrades(self, tmp_path, argv):
        # pnpm's `--changed-files-ignore-pattern <pattern>` / `--test-pattern
        # <pattern>` are MANDATORY-value flags listed under pnpm run --help's
        # Filtering options (confirmed on pnpm 10.33.0) — before P10b round-4
        # neither was in `_PNPM_VALUE_FLAGS`, so `_run_flag_prefix_len` stopped its
        # scan at the flag's value and never reached the later `--filter`,
        # misrouting a filtered workspace run to a single scopable script
        # (2026-07-21).
        base = self._workspace(tmp_path, pnpm=True)
        info = tr.detect_runner(base, argv)
        assert info.runner == tr.UNKNOWN, argv
        assert info.source == "wrapper:multi-project"
        assert info.scopable is False

    @pytest.mark.parametrize("argv", [
        ["bun", "run", "--conditions", "test", "--filter=a", "test"],
        ["bun", "run", "--port", "3000", "--filter=a", "test"],
        ["bun", "run", "--drop", "console", "-F", "a", "test"],
    ])
    def test_bun_p10b_round5_value_flag_before_filter_still_degrades(self, tmp_path, argv):
        # bun's `--conditions <val>` / `--port <val>` / `--drop <val>` are
        # MANDATORY-value flags — bun's own arg-parser table
        # (`src/runtime/cli/Arguments.rs`) declares each `<STR>`/`<STR>...` with no
        # trailing `?`, the same shape as `--define`/`--install` above (unlike the
        # `?`-suffixed optional-value `--inspect*` flags, which only take an
        # `=`-joined value) — before P10b round-5 none of these three were in
        # `_BUN_VALUE_FLAGS`, so `_run_flag_prefix_len` stopped its scan at the
        # flag's value and never reached the later `--filter`, misrouting a
        # filtered workspace run to a single scopable script (2026-07-21).
        base = self._workspace(tmp_path)
        info = tr.detect_runner(base, argv)
        assert info.runner == tr.UNKNOWN, argv
        assert info.source == "wrapper:multi-project"
        assert info.scopable is False

    @pytest.mark.parametrize("argv", [
        ["bun", "run", "--title", "ci", "--filter=a", "test"],
        ["bun", "run", "--title", "ci", "-F", "a", "test"],
    ])
    def test_bun_p10b_round6_title_value_flag_before_filter_still_degrades(self, tmp_path, argv):
        # bun's `--title <val>` is a MANDATORY-value flag (confirmed on Bun 1.3.12
        # via `bun run --help` and empirically: `bun run --title ci ci` sets the
        # process title to `ci` and still runs the `ci` script) — before P10b
        # round-6 it wasn't in `_BUN_VALUE_FLAGS`, so `_run_flag_prefix_len`
        # stopped its scan at the flag's value (`ci`) as a phantom script
        # positional, and never reached the later `--filter`, misrouting a
        # filtered workspace run to a single scopable script named `ci` (2026-07-21).
        base = self._workspace(tmp_path)
        info = tr.detect_runner(base, argv)
        assert info.runner == tr.UNKNOWN, argv
        assert info.source == "wrapper:multi-project"
        assert info.scopable is False

    @pytest.mark.parametrize("argv", [
        ["pnpm", "run", "-r", "test"],
        ["pnpm", "run", "--recursive", "test"],
        ["pnpm", "run", "--filter=a", "test"],
        ["pnpm", "run", "--filter", "a", "test"],
        ["pnpm", "run-script", "-r", "test"],
    ])
    def test_pnpm_workspace_flag_after_run_subcommand_is_unknown(self, tmp_path, argv):
        # pnpm accepts its command flags on either side of the `run` subcommand
        # (`pnpm run -r test` ≡ `pnpm -r run test`, both recursive; `pnpm run
        # --filter=a test` runs package `a`'s script, not cwd's) — a flag scan
        # that stopped at `run` unwrapped ROOT's script as Tier-A (#540 round-5
        # regression, audited 2026-07-18). Only the SCRIPT positional ends pnpm's
        # own-flag region.
        base = self._workspace(tmp_path, pnpm=True)
        info = tr.detect_runner(base, argv)
        assert info.runner == tr.UNKNOWN, argv
        assert info.source == "wrapper:multi-project"
        assert info.scopable is False

class TestBunJsxImportSourceValueFlag:
    """`bun run --jsx-import-source <val>` — a MANDATORY-value flag whose value must
    be skipped WITH the flag, so a later workspace `--filter`/`-F` is still seen.

    `--jsx-import-source` entered `_BUN_VALUE_FLAGS` without a test of its own, so
    this class closes that gap rather than inheriting it.
    The failure it guards is a FALSE Tier-A: without the entry the flag-prefix
    scan stops at the value token as a phantom script positional, the real
    `--filter` after it is never recognized, and a multi-package workspace run
    unwraps ROOT's script as scopable — a scoped re-run would then silently skip
    every other package's suite.
    """

    def _workspace(self, tmp_path):
        # Root scripts.test=vitest, pkgs/a scripts.test=jest — a root unwrap that
        # classified the whole run as vitest would let a scoped re-run silently
        # skip pkgs/a's jest suite entirely.
        (tmp_path / "package.json").write_text(json.dumps(
            {"workspaces": ["pkgs/*"], "scripts": {"test": "vitest run"}}))
        pkg = tmp_path / "pkgs" / "a"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
        return str(tmp_path)

    @pytest.mark.parametrize("argv", [
        ["bun", "run", "--jsx-import-source", "someval", "--filter=a", "test"],
        ["bun", "run", "--jsx-import-source", "someval", "--filter", "a", "test"],
        ["bun", "run", "--jsx-import-source", "someval", "-F", "a", "test"],
        # The value token is itself a plausible script name — the case that makes a
        # value-unaware scan mistake it for the script positional.
        ["bun", "run", "--jsx-import-source", "test", "--filter=a", "test"],
    ])
    def test_value_flag_before_filter_still_degrades(self, tmp_path, argv):
        base = self._workspace(tmp_path)
        info = tr.detect_runner(base, argv)
        assert info.runner == tr.UNKNOWN, argv
        assert info.source == "wrapper:multi-project"
        assert info.scopable is False

    def test_equals_joined_form_also_degrades(self, tmp_path):
        # `--jsx-import-source=<val>` consumes no following token, so the scan must
        # reach `--filter=a` on its own.
        base = self._workspace(tmp_path)
        info = tr.detect_runner(
            base, ["bun", "run", "--jsx-import-source=someval", "--filter=a", "test"])
        assert info.runner == tr.UNKNOWN
        assert info.source == "wrapper:multi-project"
        assert info.scopable is False

    def test_without_a_workspace_flag_the_script_still_unwraps_tier_a(self, tmp_path):
        # The value skip must not eat the SCRIPT positional: with no workspace flag
        # present, `bun run --jsx-import-source someval test` is a single-package
        # run and stays a scopable Tier-A unwrap.
        (tmp_path / "package.json").write_text(json.dumps(
            {"scripts": {"test": "vitest run"}}))
        info = tr.detect_runner(
            str(tmp_path), ["bun", "run", "--jsx-import-source", "someval", "test"])
        assert info.runner == tr.VITEST
        assert info.scopable is True
        assert info.resolved_cmd == ["vitest", "run"]


class TestP10TierCDetectors:
    """Tier-C — Bun + Deno (NEW in P10) detect + gate; Django + Karma (already in P2)
    confirmed complete. Bun/Deno are non-scopable (no P4–P9 triage adapter → honest
    whole-suite degrade)."""

    def test_bun_argv(self):
        info = tr.detect_runner(".", ["bun", "test"])
        assert info.runner == tr.BUN and info.source == "argv"
        assert info.scopable is False           # Tier-C: detect+gate, no scoped triage

    def test_deno_argv(self):
        info = tr.detect_runner(".", ["deno", "test", "tests/"])
        assert info.runner == tr.DENO and info.source == "argv"
        assert info.scopable is False

    def test_bun_run_test_unwraps_not_bun_runner(self, tmp_path):
        # `bun run test` runs the package SCRIPT (unwrap), NOT bun's built-in runner.
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
        assert tr.detect_runner(str(tmp_path), ["bun", "run", "test"]).runner == tr.VITEST

    @pytest.mark.parametrize("marker", ["bunfig.toml", "bun.lockb", "bun.lock"])
    def test_bun_markers(self, tmp_path, marker):
        (tmp_path / marker).write_text("")
        assert tr.detect_runner(str(tmp_path), []).runner == tr.BUN

    @pytest.mark.parametrize("marker", ["deno.json", "deno.jsonc", "deno.lock"])
    def test_deno_markers(self, tmp_path, marker):
        (tmp_path / marker).write_text("{}")
        assert tr.detect_runner(str(tmp_path), []).runner == tr.DENO

    def test_bun_marker_yields_to_real_runner(self, tmp_path):
        # A bun-as-package-manager repo that actually drives vitest is vitest, not bun.
        (tmp_path / "bun.lockb").write_text("")
        (tmp_path / "vitest.config.ts").write_text("export default {}\n")
        assert tr.detect_runner(str(tmp_path), []).runner == tr.VITEST

    def test_deno_task_is_opaque_not_deno_runner(self):
        # `deno task test` runs a deno.json TASK (arbitrary) — opaque, not the runner.
        assert tr.detect_runner(".", ["deno", "task", "test"]).runner == tr.UNKNOWN

    def test_django_detected_and_scopable_for_unittest_adapter(self, tmp_path):
        # Django reuses the unittest dotted-id contract → it MUST stay scopable so P3's
        # unittest adapter can route it (Tier-C-with-scoped-triage, unlike bun/deno).
        for argv in (["python", "manage.py", "test"], ["./manage.py", "test", "app"]):
            info = tr.detect_runner(str(tmp_path), argv)
            assert info.runner == tr.DJANGO
            assert info.scopable is True
        (tmp_path / "manage.py").write_text("#!/usr/bin/env python\n")
        assert tr.detect_runner(str(tmp_path), []).runner == tr.DJANGO


class TestP10KarmaSilentGreenGuard:
    """THE named guard: a Karma / `ng test` run with ZERO specs must classify
    `no_tests`, NEVER `passed` — regardless of exit code. Karma's default
    failOnEmptyTestSuite=true EXITS 1 (false-red) and the opt-out EXITS 0 (false-green,
    the silent-green a test gate exists to catch); both hinge on "Executed 0 of N"."""

    @pytest.mark.parametrize("rc", [0, 1])
    def test_zero_specs_is_no_tests_never_passed(self, rc):
        got = tr.classify(tr.KARMA, rc, "Executed 0 of 0 SUCCESS")
        assert got == tr.NO_TESTS, f"karma zero-spec (exit {rc}) false-classified as {got}"
        assert got != tr.PASSED

    def test_ng_test_detects_karma(self):
        assert tr.detect_runner(".", ["ng", "test"]).runner == tr.KARMA

    def test_zero_specs_with_error_is_red_not_skipped(self):
        # A BROKEN zero-spec run (TS compile error / browser disconnect) is RED, never
        # masked as a green no-tests SKIP.
        assert tr.classify(tr.KARMA, 1,
                           "error TS2304: Cannot find name 'Foo'.\nExecuted 0 of 0 ERROR") == tr.COMPILE_ERROR
        assert tr.classify(tr.KARMA, 1,
                           "Chrome DISCONNECTED\nExecuted 0 of 12 DISCONNECTED") == tr.COMPILE_ERROR
