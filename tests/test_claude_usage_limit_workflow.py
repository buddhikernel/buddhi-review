"""Behavioral harness for the claude-code-review.yml usage-limit surface step.

Managed-version 2 adds a fail-safe post-step ("Surface a Claude usage-limit
silence on the PR") that greps the SDK execution file and posts ONE marker PR
comment when the Claude run died on a usage limit — the signal RoundDriver's
rate-limit rail parses (see test_claude_rate_limit_marker.py for the loop side).
This pins the WORKFLOW side: the inline bash is extracted FROM the YAML and run
under bash+jq against synthetic execution JSON, with ``gh`` stubbed to capture
the posted marker, so the test exercises the exact shipped logic (mirroring
test_claude_auth_guard_workflow.py).

Load-bearing behaviors pinned here (each has a documented failure mode):
  * clean-success gate — a clean review whose diff quotes a limit phrase gets NO
    marker;
  * REJECTED-only rate_limit_event gate — a benign status:"allowed" event on an
    UNRELATED failure must NOT fabricate a rate_limited marker (the SDK emits an
    allowed event on essentially every run);
  * resets_at is taken from the REJECTED event, not the first event in order;
  * credits-beats-rate-limited ordering;
  * SDK-messages-only scoping — a user-message tool_result cannot fabricate a
    marker;
  * fail-safe exit 0 on every path.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
CANONICAL = _ROOT / ".github" / "workflows" / "claude-code-review.yml"
TEMPLATE = _ROOT / "buddhi_review" / "skills" / "review-pr" / "references" / "claude-code-review.yml"
_COPIES = [CANONICAL, TEMPLATE]
_IDS = ["canonical", "template"]
_STEP_NAME_FRAGMENT = "usage-limit silence"


def _step(path: Path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = doc["jobs"]["review"]["steps"]
    return next((s for s in steps if _STEP_NAME_FRAGMENT in (s.get("name") or "")), None)


# ── Static structure + contract ──────────────────────────────────────────────

@pytest.mark.parametrize("path", _COPIES, ids=_IDS)
def test_step_present_runs_always_self_contained(path):
    step = _step(path)
    assert step is not None, "missing the usage-limit surface step"
    assert str(step.get("if")).strip() == "always()"
    assert "execution_file" in ((step.get("env") or {}).get("CLAUDE_EXECUTION_FILE") or "")
    run = step["run"]
    for forbidden in ("tools/", "buddhi_review", "round_driver", "python3 ", "../"):
        assert forbidden not in run, f"step references {forbidden!r}; must be self-contained"
    assert "command -v jq" in run


@pytest.mark.parametrize("path", _COPIES, ids=_IDS)
def test_marker_contract_and_version(path):
    text = path.read_text(encoding="utf-8")
    assert "# buddhi-managed-version: 2" in text
    assert "claude-review-unavailable-v1" in text
    assert "type=rate_limited resets_at=" in text
    assert "type=credits_exhausted" in text
    # The rejected-event gate is the load-bearing correctness fix.
    assert 'status == "rejected"' in text


def test_two_copies_are_byte_identical():
    assert CANONICAL.read_text(encoding="utf-8") == TEMPLATE.read_text(encoding="utf-8"), (
        "the .github/workflows canonical and the shipped template have drifted")


# ── Behavior ─────────────────────────────────────────────────────────────────

_HAS_JQ = shutil.which("jq") is not None
_HAS_BASH = shutil.which("bash") is not None
REJECTED_EPOCH = 1751900000
ALLOWED_EPOCH = 1751600000


@pytest.mark.skipif(not (_HAS_JQ and _HAS_BASH),
                    reason="usage-limit surface test needs bash + jq on PATH")
class TestSurfaceStepBehavior:
    @staticmethod
    def _run(script: str, tmp_path: Path, exec_obj):
        """Return (returncode, posted_body_or_None) with gh stubbed."""
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        capture = tmp_path / "gh_body.txt"
        gh = bindir / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            "body=\"\"\n"
            "while [ $# -gt 0 ]; do\n"
            "  if [ \"$1\" = \"--body\" ]; then shift; body=\"$1\"; fi\n"
            "  shift\n"
            "done\n"
            f"printf '%s' \"$body\" > '{capture}'\n"
            "exit 0\n", encoding="utf-8")
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        sp = tmp_path / "surface.sh"
        sp.write_text(script, encoding="utf-8")
        env = {"PATH": f"{bindir}:{os.environ.get('PATH', '')}",
               "PR_NUMBER": "7", "REPO": "o/r", "GH_TOKEN": "x"}
        if exec_obj is None:
            env["CLAUDE_EXECUTION_FILE"] = str(tmp_path / "absent.json")
        else:
            f = tmp_path / "exec.json"
            f.write_text(json.dumps(exec_obj), encoding="utf-8")
            env["CLAUDE_EXECUTION_FILE"] = str(f)
        rc = subprocess.run(["bash", str(sp)], env=env, capture_output=True, text=True).returncode
        posted = capture.read_text(encoding="utf-8") if capture.exists() else None
        return rc, posted

    @pytest.fixture(params=_COPIES, ids=_IDS)
    def script(self, request):
        return _step(request.param)["run"]

    def _rl(self, status, epoch, kind="five_hour"):
        return {"type": "rate_limit_event",
                "rate_limit_info": {"status": status, "rateLimitType": kind, "resetsAt": epoch}}

    def _res(self, is_error, text=""):
        return {"type": "result", "subtype": "success", "is_error": is_error, "result": text}

    def test_clean_success_posts_nothing(self, script, tmp_path):
        rc, posted = self._run(script, tmp_path, [
            self._rl("allowed", ALLOWED_EPOCH),
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "the diff adds 'hit your session limit' to a fixture"}]}},
            self._res(False, "No issues found.")])
        assert rc == 0 and posted is None

    def test_allowed_event_on_unrelated_failure_posts_nothing(self, script, tmp_path):
        rc, posted = self._run(script, tmp_path, [
            self._rl("allowed", ALLOWED_EPOCH),
            self._res(True, "API Error: 500 internal server error")])
        assert rc == 0 and posted is None

    def test_rejected_event_posts_rate_limited(self, script, tmp_path):
        rc, posted = self._run(script, tmp_path, [
            self._rl("rejected", REJECTED_EPOCH, "seven_day"),
            self._res(True, "You've hit your session limit")])
        assert rc == 0 and posted and f"type=rate_limited resets_at={REJECTED_EPOCH}" in posted

    def test_resets_at_is_the_rejected_events(self, script, tmp_path):
        rc, posted = self._run(script, tmp_path, [
            self._rl("allowed", ALLOWED_EPOCH, "five_hour"),
            self._rl("rejected", REJECTED_EPOCH, "seven_day"),
            self._res(True, "You've hit your session limit")])
        assert rc == 0 and posted and f"resets_at={REJECTED_EPOCH}" in posted
        assert str(ALLOWED_EPOCH) not in posted

    def test_credits_marker_and_precedence(self, script, tmp_path):
        rc, posted = self._run(script, tmp_path, [
            self._rl("rejected", REJECTED_EPOCH),
            self._res(True, "credit balance is too low; also hit your session limit")])
        assert rc == 0 and posted and "type=credits_exhausted" in posted
        assert "type=rate_limited" not in posted

    def test_limit_phrase_only_in_user_message_posts_nothing(self, script, tmp_path):
        rc, posted = self._run(script, tmp_path, [
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "the PR body says 'you hit your session limit'"}]}},
            self._res(True, "some unrelated tool crash")])
        assert rc == 0 and posted is None

    def test_missing_execution_file_exits_zero(self, script, tmp_path):
        rc, posted = self._run(script, tmp_path, None)
        assert rc == 0 and posted is None
