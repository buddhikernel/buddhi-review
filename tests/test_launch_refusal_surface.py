"""Foreground liveness-poll: the console launcher surfaces a detached startup-gate
refusal IN THIS SESSION instead of leaving it silent in the log.

Background: ``review-pr`` is a fast front door that detaches the loop as
``python -m buddhi_review run-loop`` via ``launch-review.sh``. The two launch
preflight gates (primary-checkout + repo-confirmation) run INSIDE that detached
run-loop, so when a gate refuses the loop exits immediately and only its log
carries the reason — the user's session would otherwise see just ``log: …``.

Fix under test: after the ``nohup`` spawn, ``launch-review.sh`` polls the new PID
briefly. If it is already gone AND its log carries the
``round_driver.REFUSED_TO_LAUNCH_MARKER`` ("refused to launch") phrase, the
launcher prints a red refusal panel to STDERR and exits 2. A loop that survives
the short cap (the normal case) gets the usual ``log:``/``Watch`` lines and exit 0.

Harness: the detached interpreter is the ``BUDDHI_LAUNCH_PYTHON`` seam pointed at a
stub. The refusal stub writes the marker to its stdout (which the launcher
redirects into the log via ``>"$LOG" 2>&1``) then exits non-zero; the alive stub
sleeps past the poll cap. The stub's marker line is BUILT from the imported
constant so the Python emit-side and the bash grep-side can never drift. ``TMPDIR``
is pinned to the test tmpdir so the log never touches the real ``/tmp``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from buddhi_review import tmp_paths
from buddhi_review.round_driver import REFUSED_TO_LAUNCH_MARKER

_PUBLIC_ROOT = Path(__file__).resolve().parent.parent
_LAUNCHER = _PUBLIC_ROOT / "buddhi_review" / "launch-review.sh"

_PR = "9"
_REPO = "acme/demo"  # sanitized; forwarded verbatim to the (stubbed) python

# Stubs only act when invoked AS the detached run-loop (the launcher also reuses
# this same interpreter on macOS to URL-encode the tail path); for any other reuse
# they exit cleanly so only the run-loop path is exercised. The marker line is
# built from the imported constant, not a hand-typed literal, so it tracks the
# Python source automatically.
_REFUSAL_STUB = (
    "#!/bin/bash\n"
    'case " $* " in *" run-loop "*) '
    f'echo "✗ [auto] repo-config gate — {REFUSED_TO_LAUNCH_MARKER} on {_REPO}"; '
    "exit 2;; esac\nexit 0\n"
)
# A healthy loop stays alive well past the (test-shortened) poll cap.
_ALIVE_STUB = (
    "#!/bin/bash\n"
    'case " $* " in *" run-loop "*) sleep 3; exit 0;; esac\nexit 0\n'
)


@pytest.fixture
def harness(tmp_path):
    refusal = tmp_path / "refusal_stub.sh"
    refusal.write_text(_REFUSAL_STUB)
    refusal.chmod(0o755)
    alive = tmp_path / "alive_stub.sh"
    alive.write_text(_ALIVE_STUB)
    alive.chmod(0o755)

    def run(stub, *, liveness_wait=None, extra_env=None):
        env = dict(os.environ)
        # Pin the temp dir so the log lands in the test tmpdir, never real /tmp.
        # Also clear BUDDHI_REVIEW_TMP: the launcher prefers it over TMPDIR, so a
        # developer's env setting would silently redirect the log outside tmp_path.
        env["TMPDIR"] = str(tmp_path)
        env.pop("BUDDHI_REVIEW_TMP", None)
        # Neutralize the DETACHED loop — point its interpreter at the chosen stub.
        env["BUDDHI_LAUNCH_PYTHON"] = str(stub)
        env.pop("BUDDHI_SKIP_LIVENESS_CHECK", None)
        env.pop("BUDDHI_LIVENESS_WAIT", None)
        if liveness_wait is not None:
            env["BUDDHI_LIVENESS_WAIT"] = str(liveness_wait)
        # Keep the macOS auto-open path quiet — no real Terminal windows.
        env["BUDDHI_TAIL_NO_AUTO_OPEN"] = "1"
        if extra_env:
            for k, v in extra_env.items():
                if v is None:
                    env.pop(k, None)
                else:
                    env[k] = v
        return subprocess.run(
            ["bash", str(_LAUNCHER), _PR, "--repo", _REPO],
            capture_output=True, text=True, env=env, cwd=str(tmp_path))

    run.tmp = tmp_path
    run.refusal = refusal
    run.alive = alive
    # The log carries the repo name now: buddhi-<repo>-PR<pr>.log (via the single
    # source tmp_paths.log_name), so two repos sharing a PR number never collide.
    run.log = tmp_path / tmp_paths.log_name(_PR, _REPO)
    return run


def test_refusal_surfaces_panel_on_stderr_and_exits_2(harness):
    r = harness(harness.refusal)
    assert r.returncode == 2, (r.stdout, r.stderr)
    # The refusal panel is the in-session surfacing — STDERR, not stdout.
    assert "refused to launch" in r.stderr.lower()
    assert f"PR #{_PR}" in r.stderr
    # The "why" tail of the log (carrying the gate's marker line) is echoed too.
    assert REFUSED_TO_LAUNCH_MARKER in r.stderr
    # A refused launch must NOT print the success `log:` datum on stdout.
    assert "log:" not in r.stdout


def test_refusal_panel_is_plain_text_under_no_color(harness):
    # NO_COLOR (and a non-TTY pipe) → no ANSI escapes, but the panel + reason
    # still print and the exit code is still 2.
    r = harness(harness.refusal, extra_env={"NO_COLOR": "1"})
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "\033[" not in r.stderr  # no colour codes leaked
    assert "Review loop refused to launch" in r.stderr


def test_alive_loop_prints_normal_output_and_exits_0(harness):
    # A loop that survives the short cap is a normal launch: the `log:` datum on
    # stdout, the launch notice on stderr, exit 0, and NO refusal panel.
    r = harness(harness.alive, liveness_wait=1)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert r.stdout.strip() == f"log: {harness.log}"
    assert "launched" in r.stderr
    assert "refused to launch" not in r.stderr.lower()


def test_skip_liveness_check_bypasses_the_poll(harness):
    # BUDDHI_SKIP_LIVENESS_CHECK=1 (what a batch fan-out exports to avoid N×wait)
    # skips the poll entirely — even a refusing stub falls through to the normal
    # success output. The refusal is then only visible in the log, by design.
    r = harness(harness.refusal, extra_env={"BUDDHI_SKIP_LIVENESS_CHECK": "1"})
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert r.stdout.strip() == f"log: {harness.log}"
    assert "refused to launch" not in r.stderr.lower()


def test_fast_clean_exit_without_marker_proceeds_normally(harness):
    # A loop interpreter that exits fast with NO marker (a clean/no-op exit, e.g.
    # a recorder) is deliberately left alone: the poll only refuses on the marker,
    # so this proceeds to the normal output and exit 0 — never a false refusal.
    noop = harness.tmp / "noop_stub.sh"
    noop.write_text("#!/bin/bash\nexit 0\n")
    noop.chmod(0o755)
    r = harness(noop)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert r.stdout.strip() == f"log: {harness.log}"
    assert "refused to launch" not in r.stderr.lower()
