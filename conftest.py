# Root conftest.py — carries the per-test circuit-breaker.
#
# `buddhi_review` is imported as an installed package (`pip install -e '.[test]'`),
# so this file exists only to host the session-wide breaker at the repo root (its
# `pytest_runtest_protocol` hook brackets EVERY test regardless of how pytest is
# invoked). Keep it minimal.


# ── Per-test circuit-breaker (2026-07-10 incident) ──────────────────────────
# An unbounded loop in a test consumed 80 GB of swap and hard-rebooted the box.
# Primary: SIGALRM interrupts the hung test -> a normal pytest failure, suite
# continues. Backstop: faulthandler kills the process if SIGALRM cannot land
# (a blocking C call, or a hang off the main thread).
import faulthandler as _faulthandler
import os as _os
import signal as _signal

import pytest as _pytest

try:
    _PER_TEST_TIMEOUT = int(_os.environ.get("BUDDHI_TEST_TIMEOUT", "60"))
    if _PER_TEST_TIMEOUT <= 0:
        raise ValueError("BUDDHI_TEST_TIMEOUT must be positive")
except (TypeError, ValueError):
    _PER_TEST_TIMEOUT = 60

# SIGALRM/setitimer are POSIX-only (absent on Windows) -- no-op the breaker
# there instead of crashing on the first test run.
_HAS_ALARM = hasattr(_signal, "SIGALRM") and hasattr(_signal, "setitimer")


def _on_alarm(signum, frame):
    raise TimeoutError(
        "test exceeded BUDDHI_TEST_TIMEOUT=%ds -- suspect an unbounded loop"
        % _PER_TEST_TIMEOUT)


@_pytest.hookimpl(wrapper=True)   # new-style wrapper (pluggy>=1.2, pinned in pyproject.toml's test extra);
# do NOT swap to hookwrapper=True — that's a different generator protocol
# (yields a _Result you must call .get_result() on) and would break the
# `return (yield)` below. NEVER add trylast=True either — it disables the abort.
def pytest_runtest_protocol(item, nextitem):
    # Brackets setup + call + teardown. SIGALRM turns a pure-Python
    # main-thread hang into a reported failure/error and lets the suite
    # continue; faulthandler (armed at 2x the budget) is the process-level
    # backstop for what SIGALRM cannot interrupt — a blocking C call, or a
    # hang off the main thread.
    if not _HAS_ALARM:
        return (yield)
    # signal.signal()/setitimer() raise ValueError off the main thread (e.g. a
    # multithreaded test runner/plugin) -- _armed gates arming AND the finally
    # restoration so a non-main-thread run falls through to the faulthandler
    # backstop instead of crashing the whole suite.
    _old = None
    _armed = False
    try:
        _old = _signal.signal(_signal.SIGALRM, _on_alarm)
        _signal.setitimer(_signal.ITIMER_REAL, _PER_TEST_TIMEOUT)
        _armed = True
    except ValueError:
        pass
    _faulthandler.dump_traceback_later(_PER_TEST_TIMEOUT * 2, exit=True)
    try:
        return (yield)
    finally:
        # Disarm the timer FIRST: if it stays armed while cancel_dump_traceback_later()
        # or signal.signal() run, a fired SIGALRM raises TimeoutError mid-finally and
        # aborts the rest of cleanup, leaking the timer + handler into later tests.
        # Gate on _old (signal.signal succeeded), not _armed: if setitimer raised
        # after signal.signal succeeded, _armed stays False but the handler was
        # already swapped in and must still be restored.
        if _old is not None:
            try:
                _signal.setitimer(_signal.ITIMER_REAL, 0)
                _signal.signal(_signal.SIGALRM, _old)
            except ValueError:
                pass
        _faulthandler.cancel_dump_traceback_later()
