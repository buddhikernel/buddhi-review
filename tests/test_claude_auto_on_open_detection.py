"""Claude ``auto_on_open`` detection — the read+parse path.

Claude is the one reviewer whose "review on PR open" is machine-readable: its
``claude-code-review.yml`` workflow is API-readable, so a parse of the ``on:``
triggers (plus a job-gating check) answers True / False / None. The GitHub-App
reviewers' settings are not API-exposed, so they stay user-asked. These tests
cover the pure parser and the ``gh``-fetch wrapper with an injected runner
(never the network).
"""
import base64
import subprocess
from pathlib import Path

import pytest

from buddhi_review import detectors

_TEMPLATE = (
    Path(__file__).parent.parent / "buddhi_review" / "skills" / "review-pr"
    / "references" / "claude-code-review.yml"
)


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


def _b64(text):
    # GitHub returns the file content base64-encoded, with embedded newlines.
    raw = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return raw[:40] + "\n" + raw[40:] + "\n"


# ---------------------------------------------------------------------------
# The PyYAML on:→True footgun (the load-bearing parse hazard)
# ---------------------------------------------------------------------------

def test_bare_on_key_is_parsed_under_boolean_true():
    # A normal workflow's `on:` is an UNQUOTED key → PyYAML (YAML 1.1) loads it as
    # the boolean True, not the string "on". The parser must still find triggers.
    yml = "name: X\non:\n  pull_request:\n    types: [opened]\njobs:\n  r:\n    runs-on: u\n"
    assert detectors.workflow_triggers_on_open(yml) is True


def test_quoted_on_key_also_works():
    yml = 'name: X\n"on":\n  pull_request:\n    types: [opened]\n'
    assert detectors.workflow_triggers_on_open(yml) is True


# ---------------------------------------------------------------------------
# Trigger shapes — list / scalar / dict / null / types forms
# ---------------------------------------------------------------------------

def test_list_form_on_pull_request():
    assert detectors.workflow_triggers_on_open("on: [pull_request]\n") is True


def test_scalar_form_on_pull_request():
    assert detectors.workflow_triggers_on_open("on: pull_request\n") is True


def test_pull_request_null_body_fires_on_open():
    # `pull_request:` with an empty body → no `types:` filter → defaults to opened.
    assert detectors.workflow_triggers_on_open("on:\n  pull_request:\n") is True


def test_types_null_fires_on_open():
    yml = "on:\n  pull_request:\n    types:\n"
    assert detectors.workflow_triggers_on_open(yml) is True


def test_types_list_with_opened():
    yml = "on:\n  pull_request:\n    types: [opened, synchronize]\n"
    assert detectors.workflow_triggers_on_open(yml) is True


def test_types_list_without_opened_is_false():
    # synchronize/reopened/ready_for_review do NOT fire on initial PR creation.
    yml = "on:\n  pull_request:\n    types: [synchronize, reopened, ready_for_review]\n"
    assert detectors.workflow_triggers_on_open(yml) is False


def test_types_scalar_opened():
    # GitHub allows a bare scalar activity type; must not iterate char-by-char.
    yml = "on:\n  pull_request:\n    types: opened\n"
    assert detectors.workflow_triggers_on_open(yml) is True


def test_types_scalar_non_open_is_false():
    # The False direction of the scalar-normalization path: a bare non-open scalar
    # must NOT fire on open. Pins the branch asymmetrically from the True scalar
    # case (a "treat any scalar as firing on open" regression would slip otherwise).
    assert detectors.workflow_triggers_on_open(
        "on:\n  pull_request:\n    types: reopened\n") is False
    assert detectors.workflow_triggers_on_open(
        "on:\n  pull_request:\n    types: closed\n") is False


def test_pull_request_target_variant():
    yml = "on:\n  pull_request_target:\n    types: [opened]\n"
    assert detectors.workflow_triggers_on_open(yml) is True


def test_no_pull_request_trigger_is_false():
    yml = "on:\n  issue_comment:\n    types: [created]\n"
    assert detectors.workflow_triggers_on_open(yml) is False


# ---------------------------------------------------------------------------
# Unparseable / wrong-shape input → None (unknown)
# ---------------------------------------------------------------------------

def test_malformed_yaml_returns_none():
    assert detectors.workflow_triggers_on_open("key: [unterminated\n") is None


def test_non_mapping_doc_returns_none():
    assert detectors.workflow_triggers_on_open("- a\n- b\n") is None


def test_on_value_wrong_type_returns_none():
    # `on:` is neither str/list/dict (here an int) → can't classify → None.
    assert detectors.workflow_triggers_on_open('"on": 5\n') is None


def test_pathologically_nested_yaml_returns_none():
    # The YAML is a repo-supplied workflow file, so a hostile/deeply-nested
    # document can make PyYAML raise a NON-YAMLError (RecursionError). It must
    # collapse to the safe None ("unknown" → mention-driven), never escape the
    # detector — a narrow `except yaml.YAMLError` would let it propagate.
    hostile = "on: " + "[" * 4000 + "]" * 4000 + "\n"
    assert detectors.workflow_triggers_on_open(hostile) is None


# ---------------------------------------------------------------------------
# All-jobs-gated-out — a pull_request trigger whose jobs never run on open
# ---------------------------------------------------------------------------

def test_comment_gated_job_is_not_auto_on_open():
    # Trigger fires on open, but the only job's `if:` requires a comment object →
    # SKIPPED on a pull_request event → treat as mention-driven (False), so the
    # loop still summons @claude in round 1.
    yml = (
        "on:\n  pull_request:\n    types: [opened]\n"
        "jobs:\n  review:\n"
        "    if: ${{ contains(github.event.comment.body, '@claude') }}\n"
        "    runs-on: ubuntu-latest\n"
    )
    assert detectors.workflow_triggers_on_open(yml) is False


def test_job_without_if_runs_on_open():
    yml = (
        "on:\n  pull_request:\n    types: [opened]\n"
        "jobs:\n  review:\n    runs-on: ubuntu-latest\n"
    )
    assert detectors.workflow_triggers_on_open(yml) is True


def test_job_if_admitting_pull_request_runs_on_open():
    yml = (
        "on:\n  pull_request:\n    types: [opened]\n"
        "jobs:\n  review:\n"
        "    if: ${{ github.event_name == 'pull_request' }}\n"
        "    runs-on: ubuntu-latest\n"
    )
    assert detectors.workflow_triggers_on_open(yml) is True


def test_mixed_jobs_one_ungated_runs_on_open():
    yml = (
        "on:\n  pull_request:\n    types: [opened]\n"
        "jobs:\n"
        "  gated:\n"
        "    if: ${{ contains(github.event.comment.body, '@claude') }}\n"
        "    runs-on: ubuntu-latest\n"
        "  open:\n    runs-on: ubuntu-latest\n"
    )
    assert detectors.workflow_triggers_on_open(yml) is True


def test_non_dict_jobs_trusts_the_trigger():
    yml = "on:\n  pull_request:\n    types: [opened]\njobs: []\n"
    assert detectors.workflow_triggers_on_open(yml) is True


# ---------------------------------------------------------------------------
# The shipped template — the coupling test
# ---------------------------------------------------------------------------

def test_shipped_template_is_mention_driven():
    # The bundled claude-code-review.yml is issue_comment + review_comment only
    # (no pull_request trigger), so it must read as NOT auto-on-open — otherwise
    # the loop would wrongly skip the round-1 @claude summon.
    text = _TEMPLATE.read_text(encoding="utf-8")
    assert detectors.workflow_triggers_on_open(text) is False


# ---------------------------------------------------------------------------
# detect_claude_auto_on_open — the gh-fetch wrapper (injected runner)
# ---------------------------------------------------------------------------

def test_detect_via_gh_fetch_true():
    yml = "on:\n  pull_request:\n    types: [opened]\njobs:\n  r:\n    runs-on: u\n"
    captured = {}

    def run(argv, *, cwd=None):
        captured["argv"] = argv
        return _completed(stdout=_b64(yml))

    assert detectors.detect_claude_auto_on_open("octo/repo", run=run) is True
    assert captured["argv"][:2] == ["gh", "api"]
    assert detectors.CLAUDE_WORKFLOW_PATH in captured["argv"][2]


def test_detect_decodes_github_wrapped_base64():
    # GitHub returns .content base64-wrapped with embedded newlines. The source
    # strips ALL whitespace before decoding; this pins that strip as load-bearing —
    # a switch to a strict (validate=True) decode of the raw payload would reject
    # real gh output, so the strip must stay.
    yml = "on:\n  pull_request:\n    types: [opened]\njobs:\n  r:\n    runs-on: u\n"
    raw = base64.b64encode(yml.encode("utf-8")).decode("ascii")
    wrapped = "\n".join(raw[i:i + 60] for i in range(0, len(raw), 60)) + "\n"
    # The wrapped (newline-bearing) form is exactly what makes the strip necessary:
    # a strict decode of it rejects the embedded newlines.
    with pytest.raises(Exception):
        base64.b64decode(wrapped, validate=True)
    run = lambda argv, *, cwd=None: _completed(stdout="  " + wrapped + "  ")
    assert detectors.detect_claude_auto_on_open("octo/repo", run=run) is True


def test_detect_via_gh_fetch_false_for_mention_driven():
    text = _TEMPLATE.read_text(encoding="utf-8")
    run = lambda argv, *, cwd=None: _completed(stdout=_b64(text))
    assert detectors.detect_claude_auto_on_open("octo/repo", run=run) is False


def test_detect_missing_workflow_is_none():
    # gh api 404 on a missing file → non-zero rc → None (unknown → mention-driven).
    run = lambda argv, *, cwd=None: _completed(returncode=1, stderr="HTTP 404")
    assert detectors.detect_claude_auto_on_open("octo/repo", run=run) is None


def test_detect_empty_stdout_is_none():
    run = lambda argv, *, cwd=None: _completed(stdout="   \n")
    assert detectors.detect_claude_auto_on_open("octo/repo", run=run) is None


def test_detect_undecodable_content_is_none():
    # Present but the base64 payload can't be decoded → unknown → None.
    run = lambda argv, *, cwd=None: _completed(stdout="abc")  # not valid base64
    assert detectors.detect_claude_auto_on_open("octo/repo", run=run) is None


def test_detect_runner_oserror_is_none():
    def run(argv, *, cwd=None):
        raise OSError("gh not found")

    assert detectors.detect_claude_auto_on_open("octo/repo", run=run) is None


def test_detect_no_repo_is_none():
    def run(argv, *, cwd=None):
        raise AssertionError("must not fetch without a repo")

    assert detectors.detect_claude_auto_on_open(None, run=run) is None


def test_detect_env_seam_bypasses_gh(monkeypatch):
    # BUDDHI_CLAUDE_WORKFLOW_YML supplies the YAML directly; gh is never invoked.
    monkeypatch.setenv(
        detectors.CLAUDE_WORKFLOW_YML_ENV,
        "on:\n  pull_request:\n    types: [opened]\n",
    )

    def run(argv, *, cwd=None):
        raise AssertionError("env seam set → must not call gh")

    assert detectors.detect_claude_auto_on_open("octo/repo", run=run) is True


def test_detect_env_seam_mention_driven_false(monkeypatch):
    monkeypatch.setenv(
        detectors.CLAUDE_WORKFLOW_YML_ENV,
        "on:\n  issue_comment:\n    types: [created]\n",
    )
    assert detectors.detect_claude_auto_on_open("octo/repo") is False


# ---------------------------------------------------------------------------
# detect_claude_workflow_present — the NARROW presence probe (is the file there?)
# It answers a DIFFERENT question than auto_on_open: a mention-driven template is
# present-but-False for auto_on_open, yet MUST read present here.
# ---------------------------------------------------------------------------

def test_present_true_on_successful_fetch():
    captured = {}

    def run(argv, *, cwd=None):
        captured["argv"] = argv
        return _completed(stdout=_b64("on:\n  issue_comment:\n"))

    assert detectors.detect_claude_workflow_present("octo/repo", run=run) is True
    assert captured["argv"][:2] == ["gh", "api"]
    assert detectors.CLAUDE_WORKFLOW_PATH in captured["argv"][2]


def test_present_true_for_mention_driven_template():
    # The shipped template is mention-driven (auto_on_open → False) but is fully
    # present: presence must NOT gate on trigger shape.
    text = _TEMPLATE.read_text(encoding="utf-8")
    run = lambda argv, *, cwd=None: _completed(stdout=_b64(text))
    assert detectors.detect_claude_auto_on_open("octo/repo", run=run) is False
    assert detectors.detect_claude_workflow_present("octo/repo", run=run) is True


def test_present_false_on_missing_workflow():
    # gh api 404 → non-zero rc → absent (fail-closed).
    run = lambda argv, *, cwd=None: _completed(returncode=1, stderr="HTTP 404")
    assert detectors.detect_claude_workflow_present("octo/repo", run=run) is False


def test_present_false_on_empty_body():
    run = lambda argv, *, cwd=None: _completed(stdout="   \n")
    assert detectors.detect_claude_workflow_present("octo/repo", run=run) is False


def test_present_false_on_runner_error_fails_closed():
    def run(argv, *, cwd=None):
        raise OSError("gh not found")

    assert detectors.detect_claude_workflow_present("octo/repo", run=run) is False


def test_present_repo_none_uses_owner_repo_placeholder():
    # repo=None is a supported loop mode: like the loop's other gh calls, the
    # endpoint uses the {owner}/{repo} placeholder gh substitutes from cwd's
    # remote — so a present workflow still reads present on a --repo-less run.
    captured = {}

    def run(argv, *, cwd=None):
        captured["argv"] = argv
        return _completed(stdout=_b64("on:\n  issue_comment:\n"))

    assert detectors.detect_claude_workflow_present(None, run=run) is True
    assert "repos/{owner}/{repo}/contents/" in captured["argv"][2]


def test_present_false_repo_none_when_gh_cannot_resolve():
    # No git remote / gh can't resolve the placeholder → non-zero rc → fail-closed
    # absent (honest: presence unconfirmable).
    run = lambda argv, *, cwd=None: _completed(returncode=1, stderr="no default remote")
    assert detectors.detect_claude_workflow_present(None, run=run) is False


def test_present_true_via_env_seam(monkeypatch):
    # A seeded workflow YAML represents a present workflow; gh is never invoked.
    monkeypatch.setenv(detectors.CLAUDE_WORKFLOW_YML_ENV, "on:\n  issue_comment:\n")

    def run(argv, *, cwd=None):
        raise AssertionError("env seam set → must not call gh")

    assert detectors.detect_claude_workflow_present("octo/repo", run=run) is True
