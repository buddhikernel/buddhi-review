"""Guided verify-reject retry (`BUDDHI_VERIFY_REJECT_RETRIES`, 2026-07-10).

A fix-verify REJECT used to be terminal: the precise rejection reason was
computed but never fed back to the fixer, so a trivially-repairable defect
(PR #506: the fixer called a helper that wasn't defined or imported in the
edited file) escalated the whole loop for manual intervention. These tests
pin the ONE bounded guided retry added for that gap, adapted to the OSS
``apply_fix`` per-comment structure (its ``FixOutcome.status`` decides thread
resolution at the caller: only ``applied`` resolves; ``rejected`` /
``transient-failed`` leave the thread OPEN).

  R1 — a REJECTed-then-CORRECTED fix APPLIES, and the retry prompt carries the
       verifier's rejection reason (the first dispatch does NOT).
  R2 — a twice-REJECTed fix is terminal ``rejected`` with no third dispatch and
       a clean rollback (rollback_failed=False).
  R3 — BUDDHI_VERIFY_REJECT_RETRIES=0 disables the retry: single dispatch,
       terminal ``rejected`` — the pre-feature behaviour.
  R4 — a REJECT whose rollback FAILED is never retried (the worktree still
       contains the rejected patch); rollback_failed stays armed.
  R5 — a retry that SKIPs never resolves (no #31 laundering): terminal
       ``rejected``, and the SKIP's edits are rolled back defensively.
  R5b— a retry that emits a bare BLOCKED: line (a real tooling failure) is
       terminal ``rejected`` (NOT the first-dispatch ``transient-failed``): the
       guided path falls back to the pre-feature verify-REJECT disposition.
  R6 — the retry's verify pass is FORCED even when the ``auto`` gate would not
       have selected the retry's (benign) diff.
  R7 — the retry-force is NOT a tripwire: the A5 alarm still fires on a genuine
       trip, and a retry-forced verify does not print it.
  R8 — a retry whose verify is UNAVAILABLE (fail-open) is terminal ``rejected``:
       after an affirmative REJECT, an unverifiable corrected fix is rolled
       back, never ships; a failed rollback there arms rollback_failed.
"""
from __future__ import annotations

import subprocess

import pytest

from buddhi_review import fix_apply
from buddhi_review.fix_apply import apply_fix

REASON = ("the fix calls _recorded_log_path_is_safe() which is not defined "
          "or imported in that file")
_REJECT = f'{{"verdict": "REJECT", "reason": "{REASON}"}}'
_REJECT2 = '{"verdict": "REJECT", "reason": "still wrong"}'
_CONFIRM = '{"verdict": "CONFIRM", "reason": "addresses it"}'
_FAIL_OPEN = "garbage not json"  # unparseable → verify_fix fails OPEN


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "tracked.py").write_text("original\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    return tmp_path


def _run(repo, monkeypatch, *, fixer_steps, verify_steps, mode="on",
         budget=1, restore=None, label="SUBSTANTIVE"):
    """Drive ``apply_fix`` over ONE comment with per-attempt fixer + verify
    scripts. Each list is consumed one entry per call; the last entry repeats.

    ``fixer_steps``  — (content_to_write | None, stdout) per dispatch.
    ``verify_steps`` — raw verify-runner reply per verify call (REJECT/CONFIRM
                       JSON, or unparseable text → fail-open).
    ``restore``      — None ⇒ REAL ``restore_worktree``; a bool-list ⇒ each
                       restore call returns the next entry (last repeats).
    Returns (out, prompts, verify_calls, restore_calls).
    """
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(fix_apply, "VERIFY_REJECT_RETRIES", budget)
    prompts, verify_calls, restore_calls = [], [], []

    def fixer(prompt, *, model, effort, timeout, cwd):
        content, out = fixer_steps[min(len(prompts), len(fixer_steps) - 1)]
        prompts.append(prompt)
        if content is not None:
            (repo / "tracked.py").write_text(content)
        return 0, out

    def verify_runner(prompt):
        reply = verify_steps[min(len(verify_calls), len(verify_steps) - 1)]
        verify_calls.append(1)
        return reply

    if restore is not None:
        def _fake_restore(cwd, snap):
            r = restore[min(len(restore_calls), len(restore) - 1)]
            restore_calls.append(r)
            return r
        monkeypatch.setattr(fix_apply, "restore_worktree", _fake_restore)

    out = apply_fix(
        "the claim to fix", cwd=str(repo), runner=fixer, label=label,
        verify_runner=verify_runner, verify_mode=mode, retries=0,
    )
    return out, prompts, verify_calls, restore_calls


# ── R1 — REJECT → guided retry → CONFIRM applies ────────────────────────────

def test_rejected_then_corrected_fix_applies(repo, monkeypatch, capsys):
    out, prompts, verify_calls, _ = _run(
        repo, monkeypatch,
        fixer_steps=[("bad\n", "done"), ("corrected\n", "done")],
        verify_steps=[_REJECT, _CONFIRM])
    assert out.status == "applied"           # a corrected retry is applied
    assert out.rollback_failed is False
    assert len(prompts) == 2                  # exactly one re-dispatch
    assert len(verify_calls) == 2             # BOTH attempts verified
    assert (repo / "tracked.py").read_text() == "corrected\n"  # the fix survives
    # The retry announces itself on stdout (auto-action transparency).
    assert "guided retry" in capsys.readouterr().out


def test_attempts_accumulates_across_a_guided_retry_dispatch(repo, monkeypatch):
    # ``attempt`` resets to 0 on each while-loop re-entry (one per guided-retry
    # dispatch) — ``FixOutcome.attempts`` must still report the TOTAL fixer runs
    # across every dispatch, not just the final one's local count.
    out, prompts, _, _ = _run(
        repo, monkeypatch,
        fixer_steps=[("bad\n", "done"), ("corrected\n", "done")],
        verify_steps=[_REJECT, _CONFIRM])
    assert out.status == "applied"
    assert len(prompts) == 2
    assert out.attempts == 2


def test_retry_prompt_contains_the_rejection_reason(repo, monkeypatch):
    _, prompts, _, _ = _run(
        repo, monkeypatch,
        fixer_steps=[("bad\n", "done"), ("corrected\n", "done")],
        verify_steps=[_REJECT, _CONFIRM])
    assert "PREVIOUS ATTEMPT REJECTED" not in prompts[0]
    assert REASON not in prompts[0]
    assert "PREVIOUS ATTEMPT REJECTED" in prompts[1]
    assert "ROLLED BACK" in prompts[1]
    assert REASON in prompts[1]


# ── R2 / R3 — terminal rejections ───────────────────────────────────────────

def test_twice_rejected_fix_is_terminal_with_no_third_dispatch(repo, monkeypatch):
    out, prompts, verify_calls, _ = _run(
        repo, monkeypatch,
        fixer_steps=[("bad\n", "done"), ("worse\n", "done")],
        verify_steps=[_REJECT, _REJECT2])
    assert out.status == "rejected"
    assert out.rollback_failed is False       # both patches rolled back clean
    assert len(prompts) == 2                   # 1 attempt + 1 retry, no more
    assert len(verify_calls) == 2
    assert (repo / "tracked.py").read_text() == "original\n"  # rolled back


def test_zero_budget_disables_the_guided_retry(repo, monkeypatch):
    out, prompts, verify_calls, _ = _run(
        repo, monkeypatch, budget=0,
        fixer_steps=[("bad\n", "done")],
        verify_steps=[_REJECT])
    assert out.status == "rejected"
    assert len(prompts) == 1                   # NO re-dispatch
    assert (repo / "tracked.py").read_text() == "original\n"


def test_env_var_promotes_and_clamps_the_budget(monkeypatch):
    # VERIFY_REJECT_RETRIES = _env_int("BUDDHI_VERIFY_REJECT_RETRIES", 1): default
    # 1, clamped ≥ 0, garbage/blank → default. Pinned so a bad env value can never
    # make the budget negative (which would read as "never retry").
    for raw, expect in [("3", 3), ("0", 0), ("-4", 0), ("", 1), ("x", 1)]:
        monkeypatch.setenv("BUDDHI_VERIFY_REJECT_RETRIES", raw)
        assert fix_apply._env_int("BUDDHI_VERIFY_REJECT_RETRIES", 1) == expect
    monkeypatch.delenv("BUDDHI_VERIFY_REJECT_RETRIES", raising=False)
    assert fix_apply._env_int("BUDDHI_VERIFY_REJECT_RETRIES", 1) == 1


# ── R4 — a REJECT whose rollback FAILED is never retried ─────────────────────

def test_reject_with_failed_rollback_is_never_retried(repo, monkeypatch):
    # The worktree still CONTAINS the rejected patch — re-dispatching would
    # stack edits on un-rolled-back residue. Terminal + rollback_failed armed.
    out, prompts, _, _ = _run(
        repo, monkeypatch, restore=[False],   # every rollback fails
        fixer_steps=[("bad\n", "done")],
        verify_steps=[_REJECT])
    assert out.status == "rejected"
    assert out.rollback_failed is True         # halt-before-push armed
    assert len(prompts) == 1                    # NO retry despite budget


# ── R5 — a retry that SKIPs never resolves ──────────────────────────────────

def test_retry_skip_never_resolves_and_rolls_back(repo, monkeypatch):
    # A guided retry may only END in a CONFIRMed applied fix. A retry-SKIP keeps
    # the terminal 'rejected' outcome and rolls the SKIP's edits back — resolving
    # here would repeat the #31 SKIP+resolve laundering.
    out, prompts, verify_calls, _ = _run(
        repo, monkeypatch,
        fixer_steps=[("bad\n", "done"),
                     ("stray edit\n", "SKIP: cannot address the objection")],
        verify_steps=[_REJECT])
    assert out.status == "rejected"
    assert len(prompts) == 2                    # the retry did run
    assert len(verify_calls) == 1               # a SKIP has no diff to verify
    assert (repo / "tracked.py").read_text() == "original\n"  # SKIP edit rolled back


# ── R5b — a retry that BLOCKs never resolves ────────────────────────────────

def test_retry_blocked_is_terminal_rejected_not_escalated(repo, monkeypatch):
    # A guided retry may only END in a CONFIRMed applied fix. A retry whose fixer
    # prints a bare BLOCKED: line (a real tooling failure) falls back to the
    # pre-feature verify-REJECT disposition — terminal 'rejected', thread OPEN —
    # NOT the first-dispatch 'transient-failed'/escalated. This matches the guided
    # SKIP (R5) and fail-open (R8) paths and the PR's documented semantics.
    out, prompts, verify_calls, _ = _run(
        repo, monkeypatch,
        fixer_steps=[("bad\n", "done"),
                     ("stray edit\n", "BLOCKED: git index.lock held by another process")],
        verify_steps=[_REJECT])
    assert out.status == "rejected"             # terminal rejection, NOT transient-failed
    assert out.rollback_failed is False         # the BLOCKED retry's edits rolled back clean
    assert len(prompts) == 2                    # the retry did run
    assert len(verify_calls) == 1               # a BLOCKED has no diff to verify
    assert (repo / "tracked.py").read_text() == "original\n"  # BLOCKED edit rolled back


# ── R6 — the retry's verify is FORCED past the auto gate ────────────────────

def test_retry_verify_is_forced_past_the_auto_gate(repo, monkeypatch):
    # First diff adds an import (contract surface → auto verifies → REJECT); the
    # retry's diff is benign — auto would NOT verify it, but the retry FORCES it.
    out, prompts, verify_calls, _ = _run(
        repo, monkeypatch, mode="auto",
        fixer_steps=[("import os\noriginal\n", "done"), ("changed\n", "done")],
        verify_steps=[_REJECT, _CONFIRM])
    assert out.status == "applied"
    assert len(verify_calls) == 2               # retry verified despite auto
    assert (repo / "tracked.py").read_text() == "changed\n"


# ── R7 — the retry-force is NOT a tripwire ──────────────────────────────────

def test_tripwire_alarm_still_fires_on_a_genuine_trip(repo, monkeypatch, capsys):
    # mode=off + a *_FLAGS edit — the A5 tripwire forces the verify pass AND
    # prints its alarm. Pins the alarm against a regression that entangles it
    # with the retry-force branch.
    out, _, verify_calls, _ = _run(
        repo, monkeypatch, mode="off",
        fixer_steps=[("X_FLAGS = (1, 2)\n", "done")],
        verify_steps=[_CONFIRM])
    assert out.status == "applied"
    assert len(verify_calls) == 1               # tripwire-forced verify ran
    assert "dangerous-change tripwire" in capsys.readouterr().out


def test_retry_force_does_not_print_the_tripwire_alarm(repo, monkeypatch, capsys):
    out, _, verify_calls, _ = _run(
        repo, monkeypatch, mode="auto",
        fixer_steps=[("import os\noriginal\n", "done"), ("changed\n", "done")],
        verify_steps=[_REJECT, _CONFIRM])
    assert out.status == "applied"
    assert len(verify_calls) == 2
    assert "dangerous-change tripwire" not in capsys.readouterr().out


# ── R8 — a retry whose verify is UNAVAILABLE is terminal-rejected ────────────

def test_retry_fail_open_never_resolves_and_rolls_back(repo, monkeypatch):
    # The verifier goes dark on the retry (unparseable → fail-open). After an
    # affirmative REJECT, fail-open is NOT available: the retry's fix is rolled
    # back and the rejection stays terminal — thread OPEN, the pre-feature ending.
    out, prompts, verify_calls, _ = _run(
        repo, monkeypatch,
        fixer_steps=[("bad\n", "done"), ("corrected\n", "done")],
        verify_steps=[_REJECT, _FAIL_OPEN])
    assert out.status == "rejected"
    assert out.rollback_failed is False         # real rollback succeeded
    assert len(prompts) == 2                     # the retry did run
    assert len(verify_calls) == 2                # retry verify was attempted
    assert (repo / "tracked.py").read_text() == "original\n"  # retry fix rolled back


def test_retry_fail_open_rollback_failure_arms_the_poison_flag(repo, monkeypatch):
    # The unverifiable retry's rollback FAILS — rollback_failed arms (the halt-
    # before-push signal) and the rejection is still terminal-recorded. The first
    # REJECT's rollback succeeds (so the retry runs); the retry's fails.
    out, prompts, verify_calls, restores = _run(
        repo, monkeypatch, restore=[True, False],
        fixer_steps=[("bad\n", "done"), ("corrected\n", "done")],
        verify_steps=[_REJECT, _FAIL_OPEN])
    assert out.status == "rejected"
    assert out.rollback_failed is True
    assert len(prompts) == 2                     # retry ran (first rollback clean)
    assert restores == [True, False]             # 1st REJECT clean, retry fail-open dirty
