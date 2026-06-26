"""Richer red-test-gate escalation message: a real failure excerpt + labelled
action options.

A captured `pytest -q` tail is mostly leading `...... [ NN%]` progress dots; the
detail an operator needs (the `short test summary info` / `FAILURES` block) is at
the END. The escalation message must surface that slice — not screens of dots —
and present the action options with their full labels so the operator knows WHAT
they are choosing.
"""
from __future__ import annotations

import subprocess

from buddhi_review import commit_push
from buddhi_review.notifier import Ask


def _silent(*a, **k):
    return ""


class FakeNotifier:
    name = "console"

    def __init__(self):
        self.sent = []

    def startup_log(self):
        pass

    def send(self, ask):
        self.sent.append(ask)

    def read_answer(self, ask):
        return None

    def clear(self, ask):
        pass


# A realistic captured `pytest -q` tail: progress dots dominate the HEAD; the
# short-test-summary (what the operator needs) is at the END.
_REALISTIC_TAIL = (
    "........................................ [ 68%]\n"
    "..............................F......... [ 93%]\n"
    "........................................ [100%]\n"
    "=========================== short test summary info ===========================\n"
    "FAILED tests/test_x.py::test_a - OSError: in use\n"
    "FAILED tests/test_y.py::test_b - AssertionError: assert None == 2662\n"
    "3 failed, 4605 passed in 183s\n")


# ── failure_excerpt: the meaningful slice, never the leading dots ────────────
def test_failure_excerpt_shows_summary_not_progress_dots():
    ex = commit_push.failure_excerpt(_REALISTIC_TAIL)
    assert "FAILED tests/test_x.py::test_a - OSError: in use" in ex
    assert "3 failed, 4605 passed" in ex
    assert "[ 68%]" not in ex and "[100%]" not in ex  # progress dots dropped
    # The summary header anchors the slice (everything before it is dots).
    assert ex.startswith("=========================== short test summary info")


def test_failure_excerpt_prefers_failures_block_when_no_summary():
    tail = (
        "....... [ 50%]\n"
        "....F.. [100%]\n"
        "=================================== FAILURES ===================================\n"
        "___________________________________ test_a ____________________________________\n"
        "E   assert 1 == 2\n")
    ex = commit_push.failure_excerpt(tail)
    assert ex.startswith("======") and "FAILURES" in ex
    assert "E   assert 1 == 2" in ex
    assert "[ 50%]" not in ex  # dots dropped


def test_failure_excerpt_falls_back_to_tail_not_head():
    # No summary/FAILURES markers + no progress lines → fall back to the TAIL
    # (the end, where errors live), capped — never the head.
    tail = "\n".join(f"err{i}" for i in range(40))
    ex = commit_push.failure_excerpt(tail, max_lines=10)
    assert "err39" in ex and "err0" not in ex
    assert "earlier line(s) omitted" in ex  # honest about the dropped head


def test_failure_excerpt_caps_long_section_with_note():
    tail = "\n".join(["=== FAILURES ==="] + [f"E line {i}" for i in range(30)])
    ex = commit_push.failure_excerpt(tail, max_lines=24)
    assert "E line 0" in ex
    assert "E line 29" not in ex            # capped before the end
    # 31 meaningful lines; 23 real kept (header + 22 E-lines) + 1 note = 24, so
    # exactly 8 are dropped — the note counts what was actually omitted.
    assert "+8 more line(s)" in ex
    out = ex.splitlines()
    assert len([ln for ln in out if ln.startswith("E line ")]) == 23 - 1  # 22 kept


def test_failure_excerpt_clamps_tiny_max_lines():
    # max_lines < 2 is clamped so the note can never become the ONLY output and
    # the tail-fallback never hits the `[-0:]` whole-list slice.
    tail = "\n".join(["=== FAILURES ==="] + [f"E line {i}" for i in range(10)])
    ex = commit_push.failure_excerpt(tail, max_lines=1)
    out = ex.splitlines()
    assert len(out) == 2                       # one real line + the note
    assert out[0] == "=== FAILURES ==="        # a real line survived
    assert "more line(s)" in out[-1]
    # The tail-fallback path (no markers) at max_lines=1 must NOT dump everything.
    tail2 = "\n".join(f"err{i}" for i in range(10))
    ex2 = commit_push.failure_excerpt(tail2, max_lines=1)
    assert len(ex2.splitlines()) == 2 and "earlier line(s) omitted" in ex2


def test_failure_excerpt_all_progress_is_graceful():
    ex = commit_push.failure_excerpt("..... [ 50%]\n..... [100%]\n")
    assert "no failure detail" in ex


def test_failure_excerpt_empty_tail_is_placeholder():
    assert "no failure detail" in commit_push.failure_excerpt("")
    assert "no failure detail" in commit_push.failure_excerpt(None)


# ── _print_red_gate_panel: clearly-labelled action options ───────────────────
def test_red_gate_panel_renders_labelled_options(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    commit_push._print_red_gate_panel(
        ["FAILED tests/test_x.py::test_a"],
        options=["Push as-is (bypass the gate this round)",
                 "Stop the run",
                 "I've fixed it — re-run the gate & continue"],
        recommended_index=1)
    out = capsys.readouterr().out
    assert "How to proceed" in out
    assert "1. Push as-is (bypass the gate this round)" in out
    assert "2. Stop the run" in out
    assert "3. I've fixed it — re-run the gate & continue" in out
    # The recommended marker lands on the Stop option (index 1), not Push (0).
    push_line = next(ln for ln in out.splitlines() if "1. Push as-is" in ln)
    stop_line = next(ln for ln in out.splitlines() if "2. Stop the run" in ln)
    assert "(recommended)" in stop_line and "(recommended)" not in push_line


def test_red_gate_panel_without_options_omits_block(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    commit_push._print_red_gate_panel(["FAILED tests/test_x.py::test_a"])
    out = capsys.readouterr().out
    assert "How to proceed" not in out
    assert "FAILED tests/test_x.py::test_a" in out  # the failure still prints


# ── end-to-end: the red gate escalates with the excerpt + labelled options ───
def test_red_gate_escalation_shows_excerpt_and_labelled_options(capsys, monkeypatch):
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "false")
    monkeypatch.setenv("NO_COLOR", "1")
    notifier = FakeNotifier()

    def run(argv, *, cwd=None, timeout=None):
        if argv and argv[0] == "false":  # the (red) gate command
            return subprocess.CompletedProcess(argv, 1, stdout=_REALISTIC_TAIL)
        dirty = argv[:3] == ["git", "status", "--porcelain"]
        return subprocess.CompletedProcess(argv, 0, stdout=" M x\n" if dirty else "")

    out = commit_push.commit_and_push(
        "/w", message="m", run=run, notifier=notifier,
        answer_wait=lambda n, ask: "2", notice=_silent)
    assert out == "stopped"

    captured = capsys.readouterr().out
    # The panel shows the meaningful failure detail, not the progress dots.
    assert "short test summary info" in captured
    assert "FAILED tests/test_x.py::test_a" in captured
    assert "[ 68%]" not in captured and "[100%]" not in captured
    # …and the clearly-labelled action options, in one self-contained block.
    assert "How to proceed" in captured
    assert "1. Push as-is (bypass the gate this round)" in captured
    assert "2. Stop the run" in captured

    # The structured ask carries the same excerpt — not the dots.
    ask = notifier.sent[0]
    assert isinstance(ask, Ask)
    assert "short test summary info" in ask.detail
    assert "[ 68%]" not in ask.detail
    assert ask.options[1] == "Stop the run" and ask.recommended_index == 1
