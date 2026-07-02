"""PR-description auto-rewrite — the actuator that addresses a ``PR_DESCRIPTION``
comment by rewriting the PR body in place, and its escalate/off-switch fallbacks.

Network-free: the ``gh`` runner and the rewrite model are both injected fakes.
The load-bearing safety property under test is that the rewriter NEVER posts model
output onto the PR unless the current body was fetched and parsed cleanly.
"""
from __future__ import annotations

import subprocess

from buddhi_review import actuators
from buddhi_review.actuators import (
    _commented_line_from_hunk,
    build_pr_description_prompt,
    default_fix_dispatch,
    rewrite_pr_description,
)
from buddhi_review.classify import Classification
from buddhi_review.fix_apply import FixOutcome
from buddhi_review.loop import Comment, CommentResult


class _FakeGh:
    """Records every gh call; returns configurable rc/stdout for view + edit."""

    def __init__(self, *, view_rc=0, view_stdout='{"body": "old body"}',
                 edit_rc=0, raise_on=None):
        self.calls = []
        self.view_rc = view_rc
        self.view_stdout = view_stdout
        self.edit_rc = edit_rc
        self.raise_on = raise_on  # "view" | "edit" | None

    def __call__(self, argv, *, cwd=None, input_text=None):
        argv = list(argv)
        self.calls.append((argv, input_text))
        is_view, is_edit = "view" in argv, "edit" in argv
        if (self.raise_on == "view" and is_view) or (self.raise_on == "edit" and is_edit):
            raise OSError("gh not found")
        if is_view:
            return subprocess.CompletedProcess(argv, self.view_rc, stdout=self.view_stdout, stderr="e")
        if is_edit:
            return subprocess.CompletedProcess(argv, self.edit_rc, stdout="", stderr="e")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    @property
    def edit_called(self):
        return any("edit" in argv for argv, _ in self.calls)

    def edit_body(self):
        for argv, inp in self.calls:
            if "edit" in argv:
                return inp
        return None


def _comment(text="the PR body still names /v1/upload"):
    return Comment(id="c1", text=text)


def _result(disposition, label="SUBSTANTIVE"):
    return CommentResult(comment_id="c1", classification=Classification(label=label),
                         kernel_status="X", disposition=disposition)


# ---------------------------------------------------------------------------
# rewrite_pr_description — happy path + every failure escalates without posting
# ---------------------------------------------------------------------------

def test_rewrite_success_posts_new_body_via_stdin():
    gh = _FakeGh(view_stdout='{"body": "old: /v1/upload"}')
    out = rewrite_pr_description(
        _comment(), pr="7", repo="o/r", cwd=None, gh_run=gh,
        rewrite_runner=lambda p: "new: /v2/upload",
    )
    assert out.status == "applied"
    assert gh.edit_called
    assert gh.edit_body() == "new: /v2/upload"  # posted via --body-file - on stdin


def test_rewrite_prompt_sees_current_body_and_comment():
    seen = {}
    gh = _FakeGh(view_stdout='{"body": "OLD DESCRIPTION"}')
    def model(prompt):
        seen["p"] = prompt
        return "REWRITTEN"
    rewrite_pr_description(_comment("COMMENT TEXT"), pr="7", repo=None, cwd=None,
                          gh_run=gh, rewrite_runner=model)
    assert "OLD DESCRIPTION" in seen["p"] and "COMMENT TEXT" in seen["p"]


def test_rewrite_model_failure_escalates_without_posting():
    gh = _FakeGh(view_stdout='{"body": "old"}')
    def boom(p):
        raise RuntimeError("model down")
    out = rewrite_pr_description(_comment(), pr="7", repo=None, cwd=None,
                                gh_run=gh, rewrite_runner=boom)
    assert out.status == "transient-failed"
    assert not gh.edit_called  # a model failure never posts


def test_rewrite_empty_model_output_escalates_without_posting():
    gh = _FakeGh(view_stdout='{"body": "old"}')
    out = rewrite_pr_description(_comment(), pr="7", repo=None, cwd=None,
                                gh_run=gh, rewrite_runner=lambda p: "   ")
    assert out.status == "transient-failed"
    assert not gh.edit_called


def test_rewrite_fetch_failure_never_calls_model_or_posts():
    # ADVERSARIAL: a gh pr view non-zero exit must short-circuit BEFORE the model,
    # so no model text can ever reach the PR.
    gh = _FakeGh(view_rc=1, view_stdout="")
    called = []
    out = rewrite_pr_description(_comment(), pr="7", repo=None, cwd=None, gh_run=gh,
                                rewrite_runner=lambda p: called.append(1) or "new body")
    assert out.status == "transient-failed"
    assert called == []          # the model was never called
    assert not gh.edit_called    # nothing was posted


def test_rewrite_fetch_raises_never_posts():
    gh = _FakeGh(raise_on="view")
    called = []
    out = rewrite_pr_description(_comment(), pr="7", repo=None, cwd=None, gh_run=gh,
                                rewrite_runner=lambda p: called.append(1) or "new body")
    assert out.status == "transient-failed"
    assert called == [] and not gh.edit_called


def test_rewrite_unparseable_fetch_never_posts():
    gh = _FakeGh(view_stdout="not json at all")
    called = []
    out = rewrite_pr_description(_comment(), pr="7", repo=None, cwd=None, gh_run=gh,
                                rewrite_runner=lambda p: called.append(1) or "new body")
    assert out.status == "transient-failed"
    assert called == [] and not gh.edit_called


def test_rewrite_missing_body_field_never_posts():
    gh = _FakeGh(view_stdout='{"title": "no body key here"}')
    called = []
    out = rewrite_pr_description(_comment(), pr="7", repo=None, cwd=None, gh_run=gh,
                                rewrite_runner=lambda p: called.append(1) or "new body")
    assert out.status == "transient-failed"
    assert called == [] and not gh.edit_called


def test_rewrite_non_string_body_never_coerced_or_posted():
    # A non-string, non-null body (int / list / dict) is a malformed payload: it is
    # NEVER str()-coerced onto the PR — the model is not called and nothing posts.
    for weird in ('{"body": 123}', '{"body": ["a", "b"]}', '{"body": {"k": "v"}}',
                  '{"body": true}'):
        gh = _FakeGh(view_stdout=weird)
        called = []
        out = rewrite_pr_description(_comment(), pr="7", repo=None, cwd=None, gh_run=gh,
                                    rewrite_runner=lambda p: called.append(1) or "new body")
        assert out.status == "transient-failed", weird
        assert called == [] and not gh.edit_called, weird


def test_rewrite_edit_failure_escalates():
    gh = _FakeGh(view_stdout='{"body": "old"}', edit_rc=1)
    out = rewrite_pr_description(_comment(), pr="7", repo=None, cwd=None,
                                gh_run=gh, rewrite_runner=lambda p: "new body")
    assert out.status == "transient-failed"
    assert gh.edit_called  # it tried to post, gh rejected it


def test_rewrite_edit_raises_escalates():
    gh = _FakeGh(view_stdout='{"body": "old"}', raise_on="edit")
    out = rewrite_pr_description(_comment(), pr="7", repo=None, cwd=None,
                                gh_run=gh, rewrite_runner=lambda p: "new body")
    assert out.status == "transient-failed"


def test_rewrite_unchanged_body_skips_without_posting():
    gh = _FakeGh(view_stdout='{"body": "same body"}')
    out = rewrite_pr_description(_comment(), pr="7", repo=None, cwd=None,
                                gh_run=gh, rewrite_runner=lambda p: "same body")
    assert out.status == "skipped"
    assert not gh.edit_called


def test_rewrite_null_body_is_rewritten():
    # A null/empty description is a clean fetch (not a failure) → it gets rewritten.
    gh = _FakeGh(view_stdout='{"body": null}')
    out = rewrite_pr_description(_comment(), pr="7", repo=None, cwd=None,
                                gh_run=gh, rewrite_runner=lambda p: "fresh body")
    assert out.status == "applied"
    assert gh.edit_body() == "fresh body"


def test_rewrite_no_pr_number_escalates_without_touching_gh():
    gh = _FakeGh()
    out = rewrite_pr_description(_comment(), pr=None, repo=None, cwd=None,
                                gh_run=gh, rewrite_runner=lambda p: "new")
    assert out.status == "transient-failed"
    assert gh.calls == []


def test_rewrite_passes_repo_flag_to_gh():
    gh = _FakeGh(view_stdout='{"body": "old"}')
    rewrite_pr_description(_comment(), pr="7", repo="acme/widget", cwd=None,
                          gh_run=gh, rewrite_runner=lambda p: "new")
    view_argv = gh.calls[0][0]
    assert "-R" in view_argv and "acme/widget" in view_argv


# ---------------------------------------------------------------------------
# default_fix_dispatch — routing, off-switch, no-seam, code-fix separation
# ---------------------------------------------------------------------------

def test_dispatch_pr_description_routes_to_rewriter():
    gh = _FakeGh(view_stdout='{"body": "old: /v1"}')
    disp = default_fix_dispatch(cwd=None, pr="7", repo="o/r", gh_run=gh,
                                rewrite_runner=lambda p: "new: /v2")
    out = disp(_comment(), _result("fix", label="PR_DESCRIPTION"))
    assert out.status == "applied" and gh.edit_called


def test_dispatch_off_switch_skips_without_touching_pr():
    gh = _FakeGh(view_stdout='{"body": "old"}')
    disp = default_fix_dispatch(cwd=None, pr="7", repo="o/r", gh_run=gh,
                                fix_pr_description=False, rewrite_runner=lambda p: "new")
    out = disp(_comment(), _result("fix", label="PR_DESCRIPTION"))
    assert out.status == "skipped"
    assert gh.calls == []  # off-switch never touches the PR


def test_dispatch_no_rewrite_seam_escalates():
    disp = default_fix_dispatch(cwd=None, pr="7", repo="o/r", rewrite_runner=None)
    out = disp(_comment(), _result("fix", label="PR_DESCRIPTION"))
    assert out.status == "transient-failed"


def test_dispatch_substantive_uses_code_fixer_not_rewriter(monkeypatch):
    # A SUBSTANTIVE comment goes through apply_fix (with the label + parsed line
    # threaded), never the PR-body rewriter or gh.
    gh = _FakeGh()
    calls = []
    monkeypatch.setattr(actuators, "apply_fix",
                        lambda *a, **k: calls.append(k) or FixOutcome(status="applied"))
    disp = default_fix_dispatch(cwd="/x", pr="7", repo="o/r", gh_run=gh,
                                rewrite_runner=lambda p: "new")
    out = disp(Comment(id="c1", text="bug", path="a.py",
                       diff_hunk="@@ -1,2 +5,3 @@\n+x\n+y"),
               _result("fix", label="SUBSTANTIVE"))
    assert out.status == "applied"
    assert gh.calls == []                       # the rewriter/gh were untouched
    assert calls[0]["label"] == "SUBSTANTIVE"
    assert calls[0]["commented_line"] == 6      # anchored line parsed from the hunk


# ---------------------------------------------------------------------------
# Helpers — hunk anchor parse + inert-fenced prompt
# ---------------------------------------------------------------------------

def test_commented_line_from_hunk_parses_anchor():
    assert _commented_line_from_hunk("@@ -10,3 +10,5 @@ def f():\n+a\n+b") == 11
    assert _commented_line_from_hunk("@@ -1,2 +5,3 @@\n+x\n+y") == 6
    assert _commented_line_from_hunk(None) is None
    assert _commented_line_from_hunk("no @@ header here") is None


def test_pr_description_prompt_is_inert_fenced():
    p = build_pr_description_prompt("BODY", "COMMENT", nonce="N")
    assert "INERT documentary content" in p
    assert "<<N\nBODY\nN\n" in p and "<<N\nCOMMENT\nN\n" in p
    assert "ONLY the full updated description" in p
