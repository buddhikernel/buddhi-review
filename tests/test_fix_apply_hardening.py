"""Fixer safety + hardening: the macOS write-confinement sandbox, the
single-source effort-timeout bump, and the verify/tripwire/rollback stdout
verdicts.

These exercise ONLY the hardening behaviour; the snapshot/rollback core, the
empirical-verify golden strings, and the base attempt loop (incl. the broad
non-zero-rc retry, timeout retry, and the SKIP:/success terminal cases) stay
pinned by ``test_fix_apply.py``.
"""
import io
import subprocess

import pytest

from buddhi_review import fix_apply
from buddhi_review.fix_apply import apply_fix, maybe_sandbox, verify_fix


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


# ===========================================================================
# single-source EFFORT_TIMEOUTS (medium 300→600s); broad same-model retry
# ===========================================================================

def test_effort_timeouts_single_source_medium_bumped():
    # The one table the fixer reads; medium bumped 300→600s so a substantive fix
    # on a large file is not abandoned to a too-short budget.
    assert fix_apply.EFFORT_TIMEOUTS == {"low": 120, "medium": 600, "high": 900}


def test_apply_fix_nonzero_exit_retries_within_bound_then_escalates(repo):
    # The broad contract: ANY non-zero rc is transient → restore + retry the
    # SAME model within BUDDHI_FIX_RETRIES, then escalate (no retry on another
    # model). The error text is NOT inspected — a deterministic-looking message
    # retries too.
    calls = []
    def fixer(prompt, *, model, effort, timeout, cwd):
        calls.append((model, effort))
        (repo / "tracked.py").write_text("half-applied\n")
        return 1, "SyntaxError: invalid syntax in user code"  # not special-cased
    out = apply_fix("claim", cwd=str(repo), model="sonnet", effort="high",
                    runner=fixer, retries=1)
    assert out.status == "transient-failed" and out.attempts == 2
    assert calls == [("sonnet", "high")] * 2  # SAME model/effort every attempt
    assert "retrying on another model" in out.detail
    assert (repo / "tracked.py").read_text() == "original\n"  # restored


def test_default_runner_returns_stdout_only(monkeypatch):
    # The runner returns the fixer's stdout verbatim and never folds stderr in —
    # the SKIP: scan sees only the model's own reply, on success AND on failure.
    monkeypatch.setattr(fix_apply, "maybe_sandbox", lambda argv, cwd: list(argv))
    class _Clean:
        returncode, stdout, stderr = 0, "SKIP: nothing to do", "incidental noise"
    monkeypatch.setattr(fix_apply.subprocess, "run", lambda *a, **k: _Clean())
    rc, text = fix_apply.default_fixer_runner(
        "p", model="sonnet", effort="low", timeout=5, cwd="/x")
    assert rc == 0 and text == "SKIP: nothing to do"

    class _Fail:
        returncode, stdout, stderr = 1, "", "Connection reset by peer"
    monkeypatch.setattr(fix_apply.subprocess, "run", lambda *a, **k: _Fail())
    rc, text = fix_apply.default_fixer_runner(
        "p", model="sonnet", effort="high", timeout=10, cwd="/x")
    assert rc == 1 and text == ""  # stderr is NOT folded in on a non-zero exit


# ===========================================================================
# macOS sandbox-exec write-confinement (fail-open, BUDDHI_FIXER_SANDBOX)
# ===========================================================================

def test_fixer_sandbox_enabled_env_then_platform(monkeypatch):
    monkeypatch.setenv("BUDDHI_FIXER_SANDBOX", "0")
    assert fix_apply._fixer_sandbox_enabled() is False
    for on in ("1", "true", "YES", "on"):
        monkeypatch.setenv("BUDDHI_FIXER_SANDBOX", on)
        assert fix_apply._fixer_sandbox_enabled() is True
    monkeypatch.delenv("BUDDHI_FIXER_SANDBOX", raising=False)
    monkeypatch.setattr(fix_apply.sys, "platform", "darwin")
    assert fix_apply._fixer_sandbox_enabled() is True  # default-on where it exists
    monkeypatch.setattr(fix_apply.sys, "platform", "linux")
    assert fix_apply._fixer_sandbox_enabled() is False


def test_sbx_quote_escapes_specials():
    assert fix_apply._sbx_quote("/a/b") == '"/a/b"'
    assert fix_apply._sbx_quote('a"b') == '"a\\"b"'
    assert fix_apply._sbx_quote("a\\b") == '"a\\\\b"'


def test_maybe_sandbox_wraps_and_confines(monkeypatch, tmp_path):
    import os
    primary = tmp_path / "primary"; worktree = tmp_path / "wt"
    primary.mkdir(); worktree.mkdir()
    monkeypatch.setenv("BUDDHI_FIXER_SANDBOX", "1")
    monkeypatch.setattr(fix_apply.shutil, "which", lambda n: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(fix_apply, "_primary_checkout_for", lambda cwd: str(primary))
    argv = maybe_sandbox(["claude", "-p", "fix it"], str(worktree))
    assert argv[0] == "/usr/bin/sandbox-exec" and argv[1] == "-p"
    profile = argv[2]
    assert "(deny file-write* (subpath" in profile
    assert "(allow file-write* (subpath" in profile
    assert os.path.realpath(str(primary)) in profile   # primary denied
    assert os.path.realpath(str(worktree)) in profile  # worktree allowed
    assert argv[3:] == ["claude", "-p", "fix it"]       # original argv preserved


def test_maybe_sandbox_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("BUDDHI_FIXER_SANDBOX", "0")
    assert maybe_sandbox(["claude", "-p"], "/some/wt") == ["claude", "-p"]


def test_maybe_sandbox_noop_without_sandbox_exec(monkeypatch):
    monkeypatch.setenv("BUDDHI_FIXER_SANDBOX", "1")
    monkeypatch.setattr(fix_apply.shutil, "which", lambda n: None)
    assert maybe_sandbox(["claude"], "/some/wt") == ["claude"]


def test_maybe_sandbox_noop_when_cwd_is_primary(monkeypatch, tmp_path):
    monkeypatch.setenv("BUDDHI_FIXER_SANDBOX", "1")
    monkeypatch.setattr(fix_apply.shutil, "which", lambda n: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(fix_apply, "_primary_checkout_for", lambda cwd: str(tmp_path))
    # cwd IS the primary checkout (an overridden run) — nothing to confine.
    assert maybe_sandbox(["claude"], str(tmp_path)) == ["claude"]


def test_maybe_sandbox_fails_open_on_error(monkeypatch):
    monkeypatch.setenv("BUDDHI_FIXER_SANDBOX", "1")
    monkeypatch.setattr(fix_apply.shutil, "which", lambda n: "/usr/bin/sandbox-exec")
    def boom(cwd):
        raise RuntimeError("git worktree list blew up")
    monkeypatch.setattr(fix_apply, "_primary_checkout_for", boom)
    assert maybe_sandbox(["claude", "-p"], "/some/wt") == ["claude", "-p"]  # unchanged


def test_default_runner_applies_sandbox(monkeypatch):
    seen = {}
    monkeypatch.setattr(fix_apply, "maybe_sandbox", lambda argv, cwd: ["SBX", *argv])
    def fake_run(argv, **kw):
        seen["argv"] = argv
        class _P:
            returncode, stdout, stderr = 0, "ok", ""
        return _P()
    monkeypatch.setattr(fix_apply.subprocess, "run", fake_run)
    fix_apply.default_fixer_runner("p", model="sonnet", effort="high", timeout=9, cwd="/wt")
    assert seen["argv"][0] == "SBX" and "claude" in seen["argv"]


# ===========================================================================
# verify 3-way verdict + tripwire firing reasons + rollback-failure on stdout
# ===========================================================================

def test_verify_fix_carries_fail_open_flag():
    ok = verify_fix("c", "d", runner=lambda p: '{"verdict": "CONFIRM", "reason": "good"}')
    assert ok["verdict"] == "CONFIRM" and ok["fail_open"] is False
    rej = verify_fix("c", "d", runner=lambda p: '{"verdict": "REJECT", "reason": "no"}')
    assert rej["verdict"] == "REJECT" and rej["fail_open"] is False
    unparse = verify_fix("c", "d", runner=lambda p: "not json")
    assert unparse["verdict"] == "CONFIRM" and unparse["fail_open"] is True
    def boom(p):
        raise RuntimeError("down")
    unreach = verify_fix("c", "d", runner=boom)
    assert unreach["verdict"] == "CONFIRM" and unreach["fail_open"] is True


def test_colour_enabled_respects_no_color(monkeypatch):
    class _TTY(io.StringIO):
        def isatty(self):
            return True
    tty = _TTY()
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("BUDDHI_LOOP_NO_COLOR", raising=False)
    assert fix_apply._colour_enabled(tty) is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert fix_apply._colour_enabled(tty) is False
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("BUDDHI_LOOP_NO_COLOR", "1")
    assert fix_apply._colour_enabled(tty) is False
    assert fix_apply._colour_enabled(io.StringIO()) is False  # non-tty never coloured


def test_a4_confirm_prints_verified_line(repo, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    def fixer(prompt, *, model, effort, timeout, cwd):
        (repo / "tracked.py").write_text("fixed\n")
        return 0, "done"
    out = apply_fix(
        "claim", cwd=str(repo), runner=fixer, label="SUBSTANTIVE",
        verify_runner=lambda p: '{"verdict": "CONFIRM", "reason": "addresses it"}',
        verify_mode="on", retries=0,
    )
    captured = capsys.readouterr().out
    assert "✓ fix verified (CONFIRM)" in captured
    assert out.status == "applied"


def test_a4_fail_open_prints_unverified_line(repo, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    def fixer(prompt, *, model, effort, timeout, cwd):
        (repo / "tracked.py").write_text("fixed\n")
        return 0, "done"
    out = apply_fix(
        "claim", cwd=str(repo), runner=fixer, label="SUBSTANTIVE",
        verify_runner=lambda p: "garbage not json", verify_mode="on", retries=0,
    )
    captured = capsys.readouterr().out
    assert "fix-verify unavailable — keeping fix UNVERIFIED (fail-open)" in captured
    assert "tripwire-forced" not in captured  # not forced here
    assert out.status == "applied"  # fail-open keeps the fix


def test_a5_tripwire_reasons_and_fail_open_loudest_when_forced(repo, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    def fixer(prompt, *, model, effort, timeout, cwd):
        (repo / "tracked.py").write_text("SOME_FLAGS = ()\n")  # trips the tripwire predicate
        return 0, "done"
    out = apply_fix(
        "claim", cwd=str(repo), runner=fixer,
        verify_runner=lambda p: "garbage not json",  # verify unavailable → fail-open
        verify_mode="auto", retries=0,
    )
    captured = capsys.readouterr().out
    assert "dangerous-change tripwire:" in captured
    assert "*_FLAGS" in captured and "forcing verify" in captured
    assert "fix-verify unavailable — keeping fix UNVERIFIED (fail-open, tripwire-forced)" in captured
    assert out.status == "applied"


def test_a4_reject_prints_line_and_rolls_back(repo, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    def fixer(prompt, *, model, effort, timeout, cwd):
        (repo / "tracked.py").write_text("bad fix\n")
        return 0, "done"
    out = apply_fix(
        "claim", cwd=str(repo), runner=fixer, label="SUBSTANTIVE",
        verify_runner=lambda p: '{"verdict": "REJECT", "reason": "does not address it"}',
        verify_mode="on", retries=0,
    )
    captured = capsys.readouterr().out
    assert "fix-verify REJECT — rolling back: does not address it" in captured
    assert out.status == "rejected"
    assert (repo / "tracked.py").read_text() == "original\n"  # rolled back


def test_rollback_failure_prints_warning(repo, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    # Force every restore to fail so the ⚠ rollback-failure alarm fires.
    monkeypatch.setattr(fix_apply, "restore_worktree", lambda cwd, snap: False)
    def fixer(prompt, *, model, effort, timeout, cwd):
        (repo / "tracked.py").write_text("bad fix\n")
        return 0, "done"
    out = apply_fix(
        "claim", cwd=str(repo), runner=fixer, label="SUBSTANTIVE",
        verify_runner=lambda p: '{"verdict": "REJECT", "reason": "no"}',
        verify_mode="on", retries=0,
    )
    captured = capsys.readouterr().out
    assert "could not roll back failed-attempt edits" in captured
    assert "partial edits may ride the next push" in captured
    assert out.status == "rejected"


def test_reject_rollback_failure_sets_rollback_failed_flag(repo, monkeypatch):
    # Option C: a REJECT whose rollback FAILS carries the orthogonal
    # rollback_failed=True signal (status stays the terminal "rejected"). The round
    # driver halts on this flag before pushing, so the refused residue can't leak.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(fix_apply, "restore_worktree", lambda cwd, snap: False)
    def fixer(prompt, *, model, effort, timeout, cwd):
        (repo / "tracked.py").write_text("bad fix\n")
        return 0, "done"
    out = apply_fix(
        "claim", cwd=str(repo), runner=fixer, label="SUBSTANTIVE",
        verify_runner=lambda p: '{"verdict": "REJECT", "reason": "no"}',
        verify_mode="on", retries=0,
    )
    assert out.status == "rejected" and out.rollback_failed is True


def test_reject_clean_rollback_clears_rollback_failed_flag(repo, monkeypatch):
    # The mirror: a REJECT whose rollback SUCCEEDS leaves rollback_failed=False, so
    # the gate does not false-halt — the worktree is provably clean.
    monkeypatch.setenv("NO_COLOR", "1")  # real restore_worktree → rollback succeeds
    def fixer(prompt, *, model, effort, timeout, cwd):
        (repo / "tracked.py").write_text("bad fix\n")
        return 0, "done"
    out = apply_fix(
        "claim", cwd=str(repo), runner=fixer, label="SUBSTANTIVE",
        verify_runner=lambda p: '{"verdict": "REJECT", "reason": "no"}',
        verify_mode="on", retries=0,
    )
    assert out.status == "rejected" and out.rollback_failed is False
    assert (repo / "tracked.py").read_text() == "original\n"  # really rolled back
