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
     toolchains), including the tox.ini/noxfile force-guard and the bash-lc / opaque
     wrapper → UNKNOWN.
  3. Gate wiring in ``commit_push.run_test_gate`` — `no_tests` SKIPs (never red) with
     the loud notice; `env_error` / `compile_error` RED with the class named in the
     tail; pytest exit 5 is a SKIP, not a red gate; and the free skill's
     no-detectable-suite "no gate" SKIP posture is unchanged by the classifier wiring.
"""
from __future__ import annotations

import subprocess

import pytest

from buddhi_review import commit_push
from buddhi_review import test_runner as tr


def _silent(*a, **k):
    return ""


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

    # ---- django (unittest runner) ----
    ("django-pass", tr.DJANGO, 0, "Ran 8 tests in 1.0s\nOK", tr.PASSED),
    ("django-no-tests", tr.DJANGO, 0, "Ran 0 tests in 0.000s\nOK", tr.NO_TESTS),
    ("django-fail", tr.DJANGO, 1, "Ran 2 tests\nFAILED (failures=1)", tr.FAILED),

    # ---- jest ----
    ("jest-pass", tr.JEST, 0, "Tests: 5 passed, 5 total", tr.PASSED),
    ("jest-fail", tr.JEST, 1, "Tests: 1 failed, 4 passed", tr.FAILED),
    ("jest-no-tests", tr.JEST, 1, "No tests found, exiting with code 1", tr.NO_TESTS),
    ("jest-no-tests-json", tr.JEST, 1, '{"numTotalTests":0,"numPassedTests":0}', tr.NO_TESTS),
    ("jest-missing-deps", tr.JEST, 1, "Cannot find module 'react' from 'src/App.test.js'", tr.ENV_ERROR),

    # ---- vitest ----
    ("vitest-pass", tr.VITEST, 0, "Test Files  3 passed (3)", tr.PASSED),
    ("vitest-fail", tr.VITEST, 1, "FAIL  src/a.test.ts > adds", tr.FAILED),
    ("vitest-no-tests", tr.VITEST, 1, "No test files found, exiting with code 1", tr.NO_TESTS),
    ("vitest-missing-deps", tr.VITEST, 1, "Failed to resolve import 'vite'", tr.ENV_ERROR),
    ("vitest-esbuild-missing-dep", tr.VITEST, 1, "X [ERROR] Could not resolve 'react'\n\n    src/App.tsx:1:18:", tr.ENV_ERROR),

    # ---- mocha ----
    ("mocha-pass", tr.MOCHA, 0, "  5 passing (20ms)", tr.PASSED),
    ("mocha-fail", tr.MOCHA, 2, "  3 passing\n  2 failing", tr.FAILED),
    ("mocha-no-tests", tr.MOCHA, 1, "Error: No test files found", tr.NO_TESTS),

    # ---- jasmine (marker-first: v2 exits 0, v3 exits 1, v4+ exits 2 on no specs;
    #      the "No specs found" marker is present in ALL versions) ----
    ("jasmine-pass", tr.JASMINE, 0, "5 specs, 0 failures", tr.PASSED),
    ("jasmine-fail", tr.JASMINE, 3, "5 specs, 2 failures", tr.FAILED),
    ("jasmine-no-specs-v2-exit0", tr.JASMINE, 0, "Started\n\nNo specs found\nFinished", tr.NO_TESTS),
    ("jasmine-no-specs-v3-exit1", tr.JASMINE, 1, "Started\nNo specs found\nIncomplete: No specs found", tr.NO_TESTS),
    ("jasmine-no-specs-v4-exit2", tr.JASMINE, 2, "Started\nNo specs found\nIncomplete: No specs found", tr.NO_TESTS),

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
    ("gradle-pass", tr.GRADLE, 0, "BUILD SUCCESSFUL in 4s", tr.PASSED),
    ("gradle-fail", tr.GRADLE, 1, "> There were failing tests. BUILD FAILED", tr.FAILED),
    ("gradle-compile", tr.GRADLE, 1, "> Task :compileJava FAILED\nCompilation failed", tr.COMPILE_ERROR),

    # ---- dotnet (VSTest SILENT EXIT 0 + MTP) ----
    ("dotnet-pass", tr.DOTNET, 0, "Passed!  - Failed: 0, Passed: 12, Total: 12", tr.PASSED),
    ("dotnet-fail", tr.DOTNET, 1, "Failed!  - Failed: 1, Passed: 11", tr.FAILED),
    ("dotnet-vstest-no-tests-exit0", tr.DOTNET, 0, "No test is available in App.Tests.dll. Make sure that test discoverer & executors are registered.", tr.NO_TESTS),
    ("dotnet-vstest-filter-no-match", tr.DOTNET, 0, "...but no test matches the specified selection criteria.", tr.NO_TESTS),
    ("dotnet-mtp-no-tests-8", tr.DOTNET, 8, "", tr.NO_TESTS),
    ("dotnet-build-fail", tr.DOTNET, 1, "Build FAILED.\nProgram.cs(3,1): error CS1002", tr.COMPILE_ERROR),
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
    (["npx", "vitest", "run"], tr.VITEST),
    (["npx", "jest"], tr.JEST),
    (["node_modules/.bin/jest"], tr.JEST),
    (["mocha", "test/"], tr.MOCHA),
    (["jasmine"], tr.JASMINE),
    (["ng", "test"], tr.KARMA),
    (["node", "--test"], tr.NODE_TEST),
    (["npx", "ava"], tr.AVA),
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
        ["bash", "-lc", "npm ci && npm test"],
        ["bash", "-c", "pytest"],
        ["sh", "-c", "make test"],
    ])
    def test_shell_wrapper_is_unknown(self, argv):
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

    def test_npm_test_is_not_a_recognized_runner(self):
        # `npm test` runs a package SCRIPT (unwrapping scripts.test is out of scope) —
        # it is NOT the vitest/jest binary, so from argv alone it stays opaque.
        info = tr.detect_runner(".", ["npm", "test"])
        assert info.runner == tr.UNKNOWN


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
        # A package.json whose test is an opaque npm script is not a recognized runner
        # from markers alone.
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

    def test_pytest_exit5_is_skip_not_red(self, monkeypatch, capsys):
        # pytest exit 5 (no tests collected) is a SKIP, not a red gate — the "pytest
        # exit 5 marked red" refutation must fail.
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -m pytest -q")
        status, tail = commit_push.run_test_gate(
            "/w", run=_run_returning(5, "no tests ran in 0.01s"), notice=_silent)
        assert status == "skipped"
        assert tail == ""
        assert "no tests detected for pytest" in capsys.readouterr().out


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
