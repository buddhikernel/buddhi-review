"""F9 Part (a): the bundled claude-code-review.yml auth-failure post-step guard.

An invalid/expired ``CLAUDE_CODE_OAUTH_TOKEN`` makes the Claude action's model
call 401: it posts ZERO comments yet the GitHub job still concludes
green-success, so the failure is invisible (it reads exactly like "Claude
reviewed and found nothing"). The post-step added by F9 inspects the action's
``execution_file`` output and fails the check RED on the token-invalid
signature.

The structural contract (step present, ``always()``-guarded, reads
``execution_file``, credential inputs unchanged) lives in
``test_claude_workflow_parity.py``. This file adds the BEHAVIORAL contract: the
bash ``run`` script is extracted FROM the shipped YAML and executed under
``bash`` + ``jq`` against sample execution-output JSON, so the test runs the
exact shipped logic — not a copy.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "buddhi_review" / "skills" / "review-pr" / "references"
    / "claude-code-review.yml"
)
_GUARD_NAME_FRAGMENT = "authentication error"


def _guard_step() -> dict:
    doc = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    steps = doc["jobs"]["review"]["steps"]
    step = next(
        (s for s in steps if _GUARD_NAME_FRAGMENT in (s.get("name") or "")), None
    )
    assert step is not None, "workflow missing the auth-failure guard post-step"
    return step


_HAS_JQ = shutil.which("jq") is not None
_HAS_BASH = shutil.which("bash") is not None


@pytest.mark.skipif(not (_HAS_JQ and _HAS_BASH),
                    reason="auth-guard parse test needs bash + jq on PATH")
class TestGuardScriptBehavior:
    """Execute the shipped run-script against sample execution-output JSON.

    Exit 1 = the job goes RED (auth failure surfaced); exit 0 = the job stays
    green (clean / non-auth / nothing to inspect)."""

    @staticmethod
    def _run(script: str, tmp_path: Path, exec_json: str | None) -> int:
        script_path = tmp_path / "guard.sh"
        script_path.write_text(script, encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", "")}
        if exec_json is None:
            env["CLAUDE_EXECUTION_FILE"] = str(tmp_path / "absent.json")
        else:
            f = tmp_path / "exec.json"
            f.write_text(exec_json, encoding="utf-8")
            env["CLAUDE_EXECUTION_FILE"] = str(f)
        return subprocess.run(["bash", str(script_path)], env=env,
                              capture_output=True, text=True).returncode

    @pytest.fixture()
    def script(self) -> str:
        return _guard_step()["run"]

    def test_clean_review_passes(self, script, tmp_path):
        body = json.dumps([
            {"type": "system", "subtype": "init"},
            {"type": "result", "subtype": "success", "is_error": False,
             "result": "No issues found."},
        ])
        assert self._run(script, tmp_path, body) == 0

    def test_auth_401_green_success_fails(self, script, tmp_path):
        # The documented bug: is_error:true + the 401 text, but a green job.
        body = json.dumps([
            {"type": "system", "subtype": "init"},
            {"type": "result", "subtype": "success", "is_error": True,
             "result": ("API Error: 401 {\"type\":\"error\",\"error\":"
                        "{\"type\":\"authentication_error\",\"message\":"
                        "\"Invalid bearer token\"}}")},
        ])
        assert self._run(script, tmp_path, body) == 1

    def test_signature_in_separate_message_fails(self, script, tmp_path):
        # is_error on the result message; the 401 text in a sibling assistant
        # message — must still fire (whole-file grep, gated by no clean result).
        body = json.dumps([
            {"type": "assistant", "message": {"content": [
                {"type": "text",
                 "text": "API Error: 401 authentication_error: Invalid bearer token"}]}},
            {"type": "result", "subtype": "error_during_execution",
             "is_error": True, "result": ""},
        ])
        assert self._run(script, tmp_path, body) == 1

    def test_app_not_installed_401_passes(self, script, tmp_path):
        # A different 401 with a different remediation (install the GitHub App) —
        # must NOT trip the token-re-mint guard.
        body = json.dumps([
            {"type": "result", "subtype": "error_during_execution",
             "is_error": True,
             "result": "401 Claude Code is not installed on this repository"},
        ])
        assert self._run(script, tmp_path, body) == 0

    def test_clean_review_quoting_phrase_passes(self, script, tmp_path):
        # A clean review whose reviewed diff quotes the auth phrase must NOT turn
        # the check red (the result message is is_error:false → guarded out).
        body = json.dumps([
            {"type": "assistant", "message": {"content": [
                {"type": "text",
                 "text": "The diff adds the literal '401 Invalid bearer token' "
                         "to a test fixture; looks correct."}]}},
            {"type": "result", "subtype": "success", "is_error": False,
             "result": "No issues found."},
        ])
        assert self._run(script, tmp_path, body) == 0

    def test_transient_tool_error_plus_quote_passes(self, script, tmp_path):
        # A recovered tool error (is_error:true) AND a diff quote, but the run
        # still produced a clean-success result → no fire.
        body = json.dumps([
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "is_error": True,
                 "content": "gh: transient failure"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "PR quotes invalid bearer token in its diff"}]}},
            {"type": "result", "subtype": "success", "is_error": False,
             "result": "No issues found."},
        ])
        assert self._run(script, tmp_path, body) == 0

    def test_auth_phrase_in_user_diff_with_failed_run_passes(self, script, tmp_path):
        # The PR diff (a user message) contains the auth phrase as a code change,
        # and the run fails for an unrelated reason (no clean result). The grep is
        # scoped to result/assistant/system messages, so user content can't fire.
        body = json.dumps([
            {"type": "user", "message": {"content": [
                {"type": "tool_result",
                 "content": (
                     "diff --git a/detectors.py b/detectors.py\n"
                     "+    'authentication_error': 'token expired',\n"
                     "+    'invalid bearer token': True,\n"
                 )}]}},
            {"type": "result", "subtype": "error_during_execution",
             "is_error": True, "result": "gh: network timeout"},
        ])
        assert self._run(script, tmp_path, body) == 0

    def test_missing_file_passes(self, script, tmp_path):
        assert self._run(script, tmp_path, None) == 0

    def test_empty_array_passes(self, script, tmp_path):
        assert self._run(script, tmp_path, "[]") == 0
