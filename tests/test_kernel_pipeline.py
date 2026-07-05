"""The headline test: the KERNEL decides each comment's disposition.

For each of the seven labels, a stubbed classifier feeds the label into the
classify → map → ``run_embedded`` pipeline, and we assert the kernel reaches the
intended disposition (same label + same disposition).
"""
from __future__ import annotations

import json

import pytest

from buddhi.closure import DISCARDED, ESCALATED, MODEL_HANDLED

from buddhi_review.adapter import ReviewAdapter
from buddhi_review.classify import CLASSIFICATION_FAILED
from buddhi_review.loop import Comment, process_comments

# label fixture text → (expected kernel status, expected disposition)
CASES = [
    ("SUBSTANTIVE", "missing null check", MODEL_HANDLED, "fix"),
    ("COSMETIC", "rename for clarity", MODEL_HANDLED, "fix"),
    ("OUTDATED", "refers to deleted code", DISCARDED, "skip"),
    ("INVALID", "this is just wrong", DISCARDED, "skip"),
    ("BUSINESS_QUESTION", "should we drop this column", ESCALATED, "escalate"),
    # PR_DESCRIPTION is model-handled now — the kernel acts and the label routes
    # it to the PR-body rewriter in the actuator (not the code fixer).
    ("PR_DESCRIPTION", "the body is stale", MODEL_HANDLED, "fix"),
]


def _runner_for(label_by_text):
    def runner(prompt: str) -> str:
        for text, label in label_by_text.items():
            if text in prompt:
                return json.dumps({"label": label})
        return "unparseable"
    return runner


@pytest.mark.parametrize("label,text,kstatus,disposition", CASES)
def test_each_label_reaches_kernel_disposition(label, text, kstatus, disposition):
    runner = _runner_for({text: label})
    [r] = process_comments([Comment(id="c", text=text)], classify_runner=runner)
    assert r.classification.label == label
    assert r.kernel_status == kstatus
    assert r.disposition == disposition


def test_garbage_becomes_classification_failed_and_escalates():
    runner = lambda prompt: "the model produced nothing usable"  # noqa: E731
    [r] = process_comments([Comment(id="c", text="???")], classify_runner=runner)
    assert r.classification.label == CLASSIFICATION_FAILED
    assert r.disposition == "escalate"  # a real finding, never polish-only


def test_every_valid_business_question_is_surfaced_never_budget_deferred():
    # The interrupt-budget pacing is neutralized: EVERY valid business question is
    # surfaced to the owner, none deferred-under-budget — even with the smallest
    # possible daily budget, and even for a long stream of questions. (The old
    # behavior paced escalations out after the first; that divergence is removed.)
    adapter = ReviewAdapter(daily_interrupt_budget=1)
    runner = _runner_for({f"q{i}": "BUSINESS_QUESTION" for i in range(8)})
    comments = [Comment(id=f"c{i}", text=f"q{i}", source=f"rev{i}") for i in range(8)]
    results = process_comments(comments, adapter=adapter, classify_runner=runner)
    dispositions = [r.disposition for r in results]
    assert all(d == "escalate" for d in dispositions)  # every ask surfaced
    assert "defer" not in dispositions                  # nothing budget-deferred


def test_budget_neutralized_at_the_policy_knobs():
    # The neutralization lives entirely in the policy pack's BudgetKnobs (kernel
    # untouched): a flat-zero graduated bar (base == cap) plus a zero high-stakes
    # threshold, so aggregate_budget can never DENY a valid ask for budget.
    from buddhi_review.policy import review_policy_pack
    knobs = review_policy_pack().budget
    assert knobs.base == 0.0 and knobs.cap == 0.0
    assert knobs.high_stakes_threshold == 0.0


def test_process_comment_threads_touched_path_and_diff_to_the_classifier():
    # ingest captures path/diff_hunk on the Comment; process_comment must pass them
    # to the classifier so the doc-gated criteria can consult the touched file.
    seen = {}

    def runner(prompt: str) -> str:
        seen["prompt"] = prompt
        return json.dumps({"label": "COSMETIC"})

    process_comments(
        [Comment(id="c", text="is this default right?",
                 path="src/policy.py", diff_hunk="@@ -1 +1 @@\n-old\n+new")],
        classify_runner=runner,
    )
    assert "src/policy.py" in seen["prompt"]
    assert "@@ -1 +1 @@" in seen["prompt"]


def test_console_escalation_delivered_to_notifier(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDHI_REVIEW_TMP", str(tmp_path))
    adapter = ReviewAdapter()
    runner = _runner_for({"drop the column": "BUSINESS_QUESTION"})
    process_comments([Comment(id="cQ", text="drop the column")], adapter=adapter, classify_runner=runner)
    # the kernel delivered the ask through the escalation seam during run_embedded
    assert len(adapter.escalation.delivered) == 1
    answer_file = tmp_path / "review-answer-local-cQ.md"
    assert answer_file.exists()
