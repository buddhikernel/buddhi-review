"""The conftest per-test circuit-breaker (2026-07-10 incident) — SIGALRM path.

Spawns a REAL subprocess pytest over a throwaway probe file so the SHIPPED
conftest breaker actually arms (the pytester fixture would need plugin
enabling and would exercise a synthetic conftest, not ours). Only the SIGALRM
path is asserted: the faulthandler backstop (armed at 2x the budget) kills
the whole process and cannot be observed in-process.

The probe is written NEXT TO whichever conftest.py carries the breaker (the
repo root here), located by marker — so the locate logic ports unchanged to
any tree layout that keeps the breaker's marker comment.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_MARKER = "Per-test circuit-breaker (2026-07-10 incident)"


def _breaker_dir():
    """Directory whose conftest.py carries the breaker (the repo root here)."""
    for d in (_HERE, _HERE.parent):
        cf = d / "conftest.py"
        if cf.is_file() and _MARKER in cf.read_text(encoding="utf-8"):
            return d
    raise AssertionError("no conftest.py with the circuit-breaker found")


def _run_probe(body):
    """Write a one-off probe test next to the breaker conftest (same dir ⇒
    the conftest is guaranteed to load), run a subprocess pytest on it with a
    2s budget, and return (returncode, elapsed_seconds, combined_output)."""
    probe = _breaker_dir() / f"test_cb_probe_{os.getpid()}.py"
    probe.write_text(body, encoding="utf-8")
    env = dict(os.environ, BUDDHI_TEST_TIMEOUT="2")
    try:
        start = time.monotonic()
        r = subprocess.run(
            [sys.executable, "-m", "pytest", str(probe), "-q",
             "-p", "no:cacheprovider"],
            capture_output=True, text=True, env=env,
            cwd=str(_HERE.parent), timeout=30)
        return r.returncode, time.monotonic() - start, r.stdout + r.stderr
    finally:
        probe.unlink(missing_ok=True)


@unittest.skipIf(sys.platform == "win32", "SIGALRM circuit breaker is not supported on Windows")
class TestCircuitBreakerSigalrm(unittest.TestCase):
    def test_hung_test_is_aborted_within_the_budget(self):
        # The incident shape: an unbounded pure-Python loop on the main
        # thread. The breaker must turn it into an ordinary failure within
        # seconds — NOT let it run until RAM + swap are gone.
        rc, elapsed, out = _run_probe(
            "def test_hangs_forever():\n"
            "    while True:\n"
            "        pass\n")
        self.assertNotEqual(rc, 0)          # reported as a failure...
        self.assertLess(elapsed, 10)        # ...near the 2s budget, no hang
        self.assertIn("TimeoutError", out)  # the breaker fired, not some
                                            # unrelated collection error

    def test_fast_test_passes_undisturbed(self):
        rc, elapsed, out = _run_probe(
            "def test_fast():\n"
            "    assert True\n")
        self.assertEqual(rc, 0, out)        # the breaker adds no false alarms
        self.assertLess(elapsed, 10)


if __name__ == "__main__":
    unittest.main()
