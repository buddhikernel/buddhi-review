"""Clean-review detection + the sentinel coupling, and the status signals."""
import re
from pathlib import Path

from buddhi_review import detectors

_TEMPLATE = (
    Path(__file__).parent.parent / "buddhi_review" / "skills" / "review-pr"
    / "references" / "claude-code-review.yml"
)


# ---------------------------------------------------------------------------
# The load-bearing sentinel (CLEAN_REVIEW_PATTERNS[0] ↔ the workflow template)
# ---------------------------------------------------------------------------

def test_pattern_zero_is_the_hardcoded_sentinel_literal():
    assert detectors.CLEAN_REVIEW_PATTERNS[0] == (
        r"no (issues?|comments?|suggestions?|problems?) (found|detected|to report)"
        + detectors._NOT_MIXED_OR_PENDING
    )


def test_sentinel_matches_the_exact_workflow_line():
    assert re.search(detectors.CLEAN_REVIEW_PATTERNS[0], "No issues found.",
                     re.IGNORECASE)


def test_shipped_workflow_template_still_emits_the_sentinel():
    text = _TEMPLATE.read_text(encoding="utf-8")
    assert "No issues found." in text  # editing either side requires editing both


# ---------------------------------------------------------------------------
# Tier 1 — deterministic clean detection + the actionable-prose guard
# ---------------------------------------------------------------------------

def test_clean_phrases_detected():
    for msg in (
        "No issues found.",
        "no problems detected in this change",
        "LGTM!",
        "Looks good to me.",
        "No further comments.",
        "Nothing to flag here. No concerns.",
    ):
        assert detectors.is_clean_review(msg), msg


def test_mixed_feedback_never_silently_excludes():
    for msg in (
        "No issues found, but you should rename this variable.",
        "Looks good to me. However, consider extracting this helper.",
        "No problems detected.\n- please fix the typo in line 3",
        "LGTM. One nit: the docstring is missing.",
        "No issues found. ```suggestion\nx = 1\n```",
    ):
        assert not detectors.is_clean_review(msg), msg


def test_empty_body_never_promotes_to_no_issues():
    assert not detectors.is_clean_review("")
    assert not detectors.is_clean_review("   \n  ")


def test_plain_actionable_prose_is_not_clean():
    assert not detectors.is_clean_review("This null check is missing; fix it.")


# ---------------------------------------------------------------------------
# Tier 2 — the conservative LLM fallback (short messages only)
# ---------------------------------------------------------------------------

def test_llm_fallback_confirms_short_ambiguous_clean():
    calls = []
    def llm(prompt):
        calls.append(prompt)
        return {"clean": True}
    assert detectors.detect_clean_review("Everything checks out nicely.", llm_json=llm)
    assert len(calls) == 1
    assert "INERT documentary content" in calls[0]


def test_llm_fallback_skipped_for_long_or_actionable_text():
    def explode(prompt):
        raise AssertionError("LLM called when it must not be")
    long_text = "Everything checks out. " * 60  # > short limit
    assert not detectors.detect_clean_review(long_text, llm_json=explode)
    assert not detectors.detect_clean_review(
        "Mostly fine but you should add a test.", llm_json=explode)


def test_llm_fallback_defaults_to_not_clean():
    assert not detectors.detect_clean_review("All good here?", llm_json=lambda p: None)
    assert not detectors.detect_clean_review("All good here?", llm_json=lambda p: {"clean": "yes"})
    assert not detectors.detect_clean_review("All good here?", llm_json=None)


def test_deterministic_tier_needs_no_llm():
    def explode(prompt):
        raise AssertionError("LLM called for a deterministic clean")
    assert detectors.detect_clean_review("No issues found.", llm_json=explode)


# ---------------------------------------------------------------------------
# Status signals — quota / pr-too-large / errored
# ---------------------------------------------------------------------------

def test_quota_signals():
    for msg in (
        "Rate limit exceeded — try again later.",
        "You have exhausted your capacity on this model.",
        "HTTP 429: too many requests",
        "Monthly usage limit reached.",
    ):
        assert detectors.detect_signal(msg) == detectors.SIGNAL_QUOTA, msg


def test_pr_too_large_signals():
    for msg in (
        "This pull request is too large to review.",
        "The diff exceeds the size limit for automated review.",
        "Changes are too big for review.",
    ):
        assert detectors.detect_signal(msg) == detectors.SIGNAL_PR_TOO_LARGE, msg


def test_errored_signals():
    for msg in (
        "I encountered an internal error while reviewing.",
        "Failed to generate a review for this PR.",
        "Something went wrong. Please try again.",
        "Review run failed.",
    ):
        assert detectors.detect_signal(msg) == detectors.SIGNAL_ERRORED, msg


def test_regular_prose_is_no_signal():
    assert detectors.detect_signal("This null check is missing.") is None
    assert detectors.detect_signal("No issues found.") is None  # clean ≠ a status signal


def test_signal_not_fired_on_actionable_review_prose():
    """Actionable feedback that merely *mentions* quota/rate-limit terms must not
    be misclassified as a bot status signal and must not drop the comment."""
    for msg in (
        "Consider handling the rate limit (429) response here.",
        "You should add error handling for the case when the API returns 429.",
        "I'd recommend adding a retry for 429 responses.",
        "The PR diff is quite large — consider splitting into smaller commits.",
        "You should handle the case where the review queue fails to process.",
    ):
        assert detectors.detect_signal(msg) is None, repr(msg)


# ---------------------------------------------------------------------------
# login → bot mapping
# ---------------------------------------------------------------------------

def test_bot_for_login():
    assert detectors.bot_for_login("copilot-pull-request-reviewer[bot]") == "copilot"
    assert detectors.bot_for_login("gemini-code-assist[bot]") == "gemini"
    assert detectors.bot_for_login("chatgpt-codex-connector[bot]") == "codex"
    assert detectors.bot_for_login("claude[bot]") == "claude"
    assert detectors.bot_for_login("human-dev") is None
    assert detectors.bot_for_login("") is None
