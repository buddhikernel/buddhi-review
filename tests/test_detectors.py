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
    # A clean phrase followed by a real recommendation — either inside the same
    # sentence (intra-sentence look-ahead) or after it (the actionable-prose
    # after-scan) — must NOT read as a voluntary all-clear.
    for msg in (
        "No issues found, but you should rename this variable.",       # intra-sentence "but"
        "Looks good to me. However, consider extracting this helper.", # trailing "consider"
        "No problems detected.\n- please fix the typo in line 3",      # trailing "please fix"
        "LGTM. One nit: consider renaming the variable.",              # trailing "consider"
        "No issues found. Please update the error handling.",          # trailing "please update"
        "No comments to address, but the null check on line 42 needs to be added.",
        "No issues found. You must add tests.",                         # strong "must add"
        "No problems detected. This must be fixed before merging.",     # "must be fixed"
        # Bullet / numbered lists — unambiguous review findings
        "Overall looks good.\n- Line 3: the null check is missing\n- Line 10: this is a bug",
        "Looks good.\n1. The error handler is missing\n2. This logic is incorrect",
        # Cross-sentence bare finding markers the intra-sentence lookahead misses
        "Looks good overall. However, the null check on line 3 is missing.",
        "No comments generated. However, this logic is incorrect.",
        "LGTM. This has a bug in the edge case.",
        "No issues found. The todo comment on line 5 was left in.",
    ):
        assert not detectors.is_clean_review(msg), msg


def test_suggestion_fence_after_clean_phrase_is_not_clean():
    # A GitHub suggestion block is concrete actionable content — a clean phrase
    # followed by one must NOT be promoted to a voluntary all-clear.
    assert not detectors.is_clean_review(
        "No issues found.\n```suggestion\n-old line\n+new line\n```")
    assert not detectors.is_clean_review(
        "LGTM.\n```suggestion\nreturn x + 1\n```")


def test_empty_body_never_promotes_to_no_issues():
    assert not detectors.is_clean_review("")
    assert not detectors.is_clean_review("   \n  ")


def test_plain_actionable_prose_is_not_clean():
    assert not detectors.is_clean_review("This null check is missing; fix it.")


# ---------------------------------------------------------------------------
# Negation guard on the bare-approval patterns — "not LGTM" / "not looks good"
# ---------------------------------------------------------------------------

def test_negated_approval_is_not_clean():
    for msg in (
        "not LGTM",
        "This is not LGTM to me.",
        "This does not look good and needs rework.",
        "This is not looks good territory.",
        # whitespace variants: tab and newline between "not" and LGTM/looks-good
        "not\tLGTM",
        "not\nLGTM",
        "not\tlooks good",
        "not\nlooks good",
    ):
        assert not detectors.is_clean_review(msg), msg


def test_affirmative_approval_still_clean():
    for msg in ("LGTM!", "lgtm 🚀", "Looks good.", "Looks good to me."):
        assert detectors.is_clean_review(msg), msg


# ---------------------------------------------------------------------------
# Markdown emphasis stripped before matching ("no **new** comments")
# ---------------------------------------------------------------------------

def test_emphasis_stripped_before_matching():
    for msg in (
        "no **new** comments were generated",
        "__LGTM__",
        "*Looks good to me.*",
        "Generated no *new* comments.",
    ):
        assert detectors.is_clean_review(msg), msg


def test_star_bullet_survives_emphasis_strip():
    # A `* item` Markdown bullet after a clean phrase must NOT be destroyed by
    # the emphasis-strip pass — the `*` is a list marker, not emphasis, and the
    # bullet-detector in _ACTIONABLE_PROSE_RE must still fire on it.
    assert not detectors.is_clean_review(
        "LGTM\n* line 42 has an off-by-one here")
    assert not detectors.is_clean_review(
        "No issues found.\n* Add a null check on the input parameter.")
    # `-` and numbered bullets were never affected; confirm they still work.
    assert not detectors.is_clean_review(
        "LGTM\n- rename foo to something clearer")
    assert not detectors.is_clean_review(
        "No issues found.\n1. The error handler is missing")


# ---------------------------------------------------------------------------
# Completion phrasing: "didn't / did not find any [major] issues"
# ---------------------------------------------------------------------------

def test_didnt_find_issues_completion_clean():
    for msg in (
        "Didn't find any major issues. Swish!",
        "Did not find any issues.",
        "Didn't find any bugs.",
        "did not find any significant problems",
        "Didn't find any concerns in this change.",
    ):
        assert detectors.is_clean_review(msg), msg


def test_didnt_find_issues_mixed_not_clean():
    assert not detectors.is_clean_review(
        "Didn't find any major issues, but please add a test for the empty branch.")
    assert not detectors.is_clean_review(
        "Didn't find any issues. However, consider extracting the helper.")


# ---------------------------------------------------------------------------
# Review-output count phrasings — active + passive "no comments"
# ---------------------------------------------------------------------------

def test_active_no_comments_output_clean():
    for msg in (
        "Copilot reviewed 3 out of 3 changed files and generated no comments.",
        "Generated no new comments.",           # round-2 re-review phrasing
        "Produced no findings.",
        "Raised zero concerns.",
        "Posted 0 comments on this revision.",
    ):
        assert detectors.is_clean_review(msg), msg


def test_passive_no_comments_output_clean():
    for msg in (
        "No comments were generated.",
        "Zero issues raised.",
        "0 findings reported.",
        "No new comments were generated on this revision.",
    ):
        assert detectors.is_clean_review(msg), msg


def test_output_count_mixed_not_clean():
    assert not detectors.is_clean_review(
        "Generated no comments, but consider adding a test.")
    assert not detectors.is_clean_review(
        "No comments were generated. Please rename the helper.")


# ---------------------------------------------------------------------------
# Bare "No concerns." is NOT a deterministic clean verdict — it is ambiguous
# enough to defer to the conservative tier-2 check (the qualified "no concerns
# to flag" family still reads clean).
# ---------------------------------------------------------------------------

def test_bare_no_concerns_not_deterministically_clean():
    assert not detectors.is_clean_review("No concerns.")
    assert not detectors.is_clean_review("No concerns")
    # the qualified families still read clean deterministically
    assert detectors.is_clean_review("No concerns to flag here.")
    assert detectors.is_clean_review("No further concerns.")


# ---------------------------------------------------------------------------
# The deterministic guard is narrow by design: a trailing recommendation is
# caught only when it uses recognised recommendation vocabulary. Bare
# after-the-verdict prose ("one nit: X", "you should X") is left to the
# conservative tier-2 / inline-comment path rather than blocked here — the
# common mixed forms (intra-sentence contrast, and the recommendation verbs)
# are still caught above.
# ---------------------------------------------------------------------------

def test_bare_nit_blocks_clean_verdict():
    # "nit" is a review finding marker — a clean phrase followed by a nit must NOT
    # be promoted to a voluntary all-clear; tier-1 must reject it.
    assert not detectors.is_clean_review("LGTM. One nit: the docstring is missing.")
    assert not detectors.is_clean_review("No issues found. Nit: rename this variable.")


def test_bare_imperative_blocks_clean_verdict():
    # A bare imperative action verb (without "please") after a clean phrase is
    # still a real review request and must NOT read as a voluntary all-clear.
    assert not detectors.is_clean_review("No issues found. Fix the typo in line 3.")
    assert not detectors.is_clean_review("Looks good. Rename the helper function.")
    # Bare imperative BEFORE the clean phrase must also be caught.
    assert not detectors.is_clean_review("Fix the typo on line 3. No issues found.")
    assert not detectors.is_clean_review("Rename this helper. LGTM.")


def test_you_should_blocks_clean_verdict():
    # Subject-first "you should" / "we should" is actionable recommendation form
    # and must block a clean verdict even when a clean phrase precedes it.
    assert not detectors.is_clean_review(
        "No comments to address. Separately, you should rename foo.")
    assert not detectors.is_clean_review(
        "No issues found. We should add a null check here.")


def test_actionable_prefix_blocks_clean_verdict():
    # Feedback BEFORE the clean phrase must also be caught — "Fix typo. LGTM"
    # should not pass as a clean review.
    assert not detectors.is_clean_review("Consider adding a test. LGTM")
    assert not detectors.is_clean_review("Please fix the typo. No issues found.")
    assert not detectors.is_clean_review(
        "I recommend renaming this variable. Overall looks good, no comments.")
    # Genuine clean messages without a prefix recommendation must still pass.
    assert detectors.is_clean_review("LGTM")
    assert detectors.is_clean_review("No issues found.")
    # "must be merged" is an approval footer, not a recommendation — guard must not fire.
    assert detectors.is_clean_review("No issues found. This must be merged after CI.")


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
    # A recommendation the guard recognises never reaches the model.
    assert not detectors.detect_clean_review(
        "Mostly fine, but consider adding a test.", llm_json=explode)


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


def test_quota_signals_widened_coverage():
    # Cool-down / exhaustion shapes the narrower regex used to miss. Every one
    # pairs the limit/quota noun with an exhaustion verb or an explicit cool-down
    # — a bare noun-phrase alone is intentionally NOT enough (see the FP tests).
    for msg in (
        "You've reached your monthly review limit.",
        "Daily quota limit hit.",
        "You are rate-limited. Please try again in 60 minutes.",
        "Out of credits.",
        "Come back in 24 hours — the usage cap is exhausted.",
        "API request limit exceeded.",
        "Your monthly token allowance is exhausted.",
        "Service unavailable for 24 hours.",
    ):
        assert detectors.detect_signal(msg) == detectors.SIGNAL_QUOTA, msg


def test_quota_declarative_findings_are_not_signals():
    # A reviewer DESCRIBING a PR's rate-limit / quota / token-limit code (bare
    # noun-phrase, no exhaustion verb) is a FINDING, not the reviewer being out of
    # quota — it must flow to the fixer, never permanently exclude the reviewer.
    for msg in (
        "The rate limit check is missing on this endpoint.",
        "The token limit is never reset, so requests are blocked forever.",
        "The monthly usage limit comparison uses > instead of >=.",
        "The daily limit is off by one here.",
        "429 handling looks right.",
        "This returns 429 without a Retry-After header.",
        "The rate limit resets each hour as documented.",
        "The api request limit is enforced twice, double-counting each call.",
        "The rate-limited requests are queued rather than dropped.",
        "This exceeds the token limit check.",
        "The budget is exhausted gracefully with a clear error message.",
    ):
        assert detectors.detect_signal(msg) is None, msg


def test_pr_too_large_exceeds_maximum_family():
    # Copilot's "exceeds the maximum number of files/changes/tokens" refusal —
    # anchored to a review-refusal context (the bot could not review).
    for msg in (
        "Copilot wasn't able to review this pull request because it exceeds the "
        "maximum number of files (300).",
        "Unable to review this PR: it exceeds the maximum number of changes.",
        "Could not review — the diff exceeds the maximum number of tokens.",
    ):
        assert detectors.detect_signal(msg) == detectors.SIGNAL_PR_TOO_LARGE, msg


def test_pr_too_large_excludes_lint_style_maximum():
    # Per-line / per-character "maximum number of" is lint feedback, NOT a
    # size-limit refusal — it must flow through as a finding, not a bot skip.
    for msg in (
        "This function exceeds the maximum number of lines allowed by the linter.",
        "The identifier exceeds the maximum number of characters.",
    ):
        assert detectors.detect_signal(msg) is None, msg


def test_pr_too_large_exceeds_maximum_without_refusal_is_a_finding():
    # "exceeds the maximum number of files/tokens/changes" WITHOUT a review-refusal
    # context is a finding about the reviewed code's own limits — not a bot refusal.
    for msg in (
        "Because the concatenated context exceeds the maximum number of tokens "
        "the endpoint accepts, requests over ~8k get rejected at runtime.",
        "Each generated changeset here exceeds the maximum number of files the "
        "deploy tool will apply atomically.",
        "This function exceeds the maximum number of changes we allow per commit; "
        "split it.",
    ):
        assert detectors.detect_signal(msg) is None, msg


def test_errored_something_went_wrong_is_anchored():
    # A review-process anchor within a short window is required, so substantive
    # prose that mentions "something went wrong" / "encountered an error" about
    # the code under review is NOT misread as a bot error placeholder.
    assert detectors.detect_signal(
        "Something went wrong with the cache invalidation logic.") is None
    assert detectors.detect_signal(
        "The parser encountered an error state that is not handled here.") is None
    # Genuine bot error placeholders still classify as errored.
    assert detectors.detect_signal(
        "Something went wrong while generating the review.") == detectors.SIGNAL_ERRORED
    assert detectors.detect_signal(
        "Copilot encountered an error and was unable to review this pull request."
    ) == detectors.SIGNAL_ERRORED


# ---------------------------------------------------------------------------
# Tier-2 quota check (behind the injected model seam) — fires only when the
# deterministic regex misses AND the message carries quota vocabulary.
# ---------------------------------------------------------------------------

def test_quota_tier2_fires_when_regex_misses_but_keyword_gate_hits():
    msg = "Please retry after the current review window."
    assert detectors.detect_signal(msg) is None                      # regex misses
    calls = []
    def llm(prompt):
        calls.append(prompt)
        return {"quota": True}
    assert detectors.detect_signal(msg, quota_llm=llm) == detectors.SIGNAL_QUOTA
    assert len(calls) == 1
    assert "INERT documentary content" in calls[0]


def test_quota_tier2_is_keyword_gated_no_call_on_benign_prose():
    def explode(prompt):
        raise AssertionError("LLM called on benign prose")
    assert detectors.detect_signal("This looks structurally fine.", quota_llm=explode) is None
    assert detectors.detect_signal("LGTM", quota_llm=explode) is None


def test_quota_tier2_is_conservative_on_negative_or_unparseable():
    msg = "Please retry after the current review window."
    assert detectors.detect_signal(msg, quota_llm=lambda p: {"quota": False}) is None
    assert detectors.detect_signal(msg, quota_llm=lambda p: None) is None
    assert detectors.detect_signal(msg, quota_llm=lambda p: {"quota": "yes"}) is None


def test_request_cap_placeholders_route_to_tier2():
    # Per-day request-cap / premium-request exhaustion placeholders (real bot
    # copy) carry "requests/budget" nouns the deterministic pass does not match;
    # the keyword gate routes them to the model, which resolves them to quota.
    for msg in (
        "You've used all your requests for today.",
        "You have run out of premium requests. They will reset next month.",
        "Your available requests have been exhausted for now.",
        "You have no requests remaining for today.",
        "This review consumed your remaining token budget for today. Try again tomorrow.",
    ):
        assert detectors.detect_signal(msg) is None, msg  # deterministic stays quiet
        assert bool(detectors._QUOTA_GATE_KEYWORDS_RE.search(msg)), msg  # gate fires
        assert detectors.detect_signal(
            msg, quota_llm=lambda p: {"quota": True}) == detectors.SIGNAL_QUOTA, msg
    # a FINDING that reuses the same nouns is still vetoed by the model.
    assert detectors.detect_signal(
        "The counter of requests remaining is decremented twice per call.",
        quota_llm=lambda p: {"quota": False}) is None


def test_throttle_defers_to_tier2_not_deterministic():
    # "throttled" alone is ambiguous (a bot self-report vs. a description of the
    # reviewed code), so it is NOT a deterministic quota signal — it routes to the
    # keyword-gated tier-2 check, which resolves it by intent.
    assert detectors.detect_signal("You are being throttled for now.") is None
    assert detectors.detect_signal(
        "You are being throttled for now.",
        quota_llm=lambda p: {"quota": True}) == detectors.SIGNAL_QUOTA
    assert detectors.detect_signal(
        "The throttled requests are queued rather than dropped.",
        quota_llm=lambda p: {"quota": False}) is None


# ---------------------------------------------------------------------------
# Quota second-pass on a quota-themed PR: a reviewer's FINDING about the PR's
# own quota code must not read as the reviewer being out of quota.
# ---------------------------------------------------------------------------

_QUOTA_PR_TITLE = "Add monthly quota / rate-limit handling"
_QUOTA_PR_BODY = "Implements the daily limit reset and quota accounting."
# A healthy reviewer summarizing that PR — echoes the quota vocabulary and trips
# the deterministic QUOTA_RE.
_QUOTA_ECHO = ("This PR adds monthly quota limit handling; the quota is reset "
               "each cycle and the usage limit is enforced per account.")


def test_quota_gate_suppresses_finding_on_quota_pr():
    # PR is about quotas AND the model says the bot is describing PR content →
    # the exclusion is suppressed (the bot stays active).
    assert detectors.detect_signal(
        _QUOTA_ECHO, quota_llm=lambda p: {"self_reporting": False},
        pr_title=_QUOTA_PR_TITLE, pr_body=_QUOTA_PR_BODY) is None


def test_quota_gate_keeps_exclusion_when_self_reporting():
    assert detectors.detect_signal(
        _QUOTA_ECHO, quota_llm=lambda p: {"self_reporting": True},
        pr_title=_QUOTA_PR_TITLE, pr_body=_QUOTA_PR_BODY) == detectors.SIGNAL_QUOTA


def test_quota_gate_does_not_fire_on_non_quota_pr():
    # PR is NOT about quotas → no second-pass, no model call, deterministic verdict.
    def explode(prompt):
        raise AssertionError("gate ran on a PR that is not about quotas")
    assert detectors.detect_signal(
        _QUOTA_ECHO, quota_llm=explode,
        pr_title="Refactor the button component", pr_body="cosmetic tidy-up"
    ) == detectors.SIGNAL_QUOTA


def test_quota_gate_fails_open_on_model_error():
    # A glitchy gate must never swallow a real quota signal → fail-open (exclude).
    assert detectors.detect_signal(
        _QUOTA_ECHO, quota_llm=lambda p: None,
        pr_title=_QUOTA_PR_TITLE, pr_body=_QUOTA_PR_BODY) == detectors.SIGNAL_QUOTA
    assert detectors.detect_signal(
        _QUOTA_ECHO, quota_llm=lambda p: {"self_reporting": "maybe"},
        pr_title=_QUOTA_PR_TITLE, pr_body=_QUOTA_PR_BODY) == detectors.SIGNAL_QUOTA


def test_quota_gate_no_pr_context_is_deterministic():
    # No PR context supplied (the default) → deterministic verdict, no gate.
    def explode(prompt):
        raise AssertionError("gate ran without PR context")
    assert detectors.detect_signal(
        _QUOTA_ECHO, quota_llm=explode) == detectors.SIGNAL_QUOTA


# ---------------------------------------------------------------------------
# Auth-failure signature — AUTH_FAILED_RE (used by the round driver's check-run
# probe against the failed "Claude Code Review" run log, NOT comment text).
# ---------------------------------------------------------------------------

def test_auth_failed_regex_matches_the_401_signatures():
    """AUTH_FAILED_RE recognises the token-invalid 401 family — the signature the
    bundled workflow's post-step fails the GitHub job red on and emits into the run
    log. The first alternatives are the post-step's own grep set; plus a named
    credential reported expired."""
    for msg in (
        # the literal ``::error`` message the post-step writes into the run log
        "::error title=Claude review auth failed::CLAUDE_CODE_OAUTH_TOKEN is "
        "invalid or expired — the Claude review returned 401 (Invalid bearer token)",
        "API Error: 401 Invalid bearer token",
        "401 Unauthorized: Invalid bearer token",
        'authentication_error: {"message":"Invalid bearer token"}',
        "authentication_failed",
        "OAuth authentication failed.",
        "Your token has expired; re-mint it.",
        "The OAuth token is expired.",
        "The CLAUDE_CODE_OAUTH_TOKEN has expired.",
        "API key expired.",
    ):
        assert detectors.AUTH_FAILED_RE.search(msg), msg


def test_auth_failed_regex_does_not_match_errored_clean_or_a_finding():
    """The auth regex must NOT match an ordinary (transient) errored message, a
    clean review, a finding that merely mentions an HTTP 401/unauthorized status,
    OR a git-checkout "Authentication failed" (space form, a different failure with
    a different fix) — any of which would wrongly trip the token-re-mint guard."""
    for msg in (
        # errored / clean
        "I encountered an internal error while reviewing.",
        "Failed to generate a review for this PR.",
        "Something went wrong. Please try again.",
        "Review run failed.",
        "No issues found.",
        "LGTM!",
        "no problems detected in this change",
        # genuine findings about the reviewed code's auth handling
        "The 401 status code is appropriate here.",
        "The 401 response is correct per the API specification.",
        "HTTP 401 Unauthorized is returned for unauthenticated requests.",
        "This is an unauthorized endpoint — access is restricted.",
        "Returning 401 here is consistent with RFC 6750.",
        "We reviewed 401 error handling in the API.",
        # a git-checkout auth failure (space form) — NOT a token-invalid 401
        "fatal: Authentication failed for 'https://github.com/o/r.git/'",
        # an OIDC token-fetch failure — a different setup fix, not a re-mint
        "Error: Could not fetch an OIDC token from the GitHub provider.",
    ):
        assert not detectors.AUTH_FAILED_RE.search(msg), msg


def test_detect_signal_never_classifies_an_auth_finding_as_a_status():
    """Auth detection is deliberately NOT comment-based: a real reviewer 401 posts
    no comment, so the only comments carrying an auth term are FINDINGS about the
    reviewed code's auth handling. detect_signal must NOT classify those as a
    status signal — that would silently drop a real finding. They flow through as
    None (actionable) and reach the kernel; the round driver's check-run probe
    owns auth detection. A genuine errored message still classifies as errored."""
    for finding in (
        "This logs the bearer token on an authentication_error.",
        "The invalid_token case falls through to the success handler.",
        "The expired token is still accepted on the second call.",
        "HTTP 401 Unauthorized is returned for unauthenticated requests.",
        "The authentication failure leaks the stack trace to the client.",
    ):
        assert detectors.detect_signal(finding) is None, repr(finding)
    assert detectors.detect_signal(
        "I encountered an internal error while reviewing.") == detectors.SIGNAL_ERRORED


def test_signal_not_fired_on_actionable_review_prose():
    """Actionable feedback that merely *mentions* quota/rate-limit/auth terms must
    not be misclassified as a bot status signal and must not drop the comment."""
    for msg in (
        "Consider handling the rate limit (429) response here.",
        "You should add error handling for the case when the API returns 429.",
        "I'd recommend adding a retry for 429 responses.",
        "The PR diff is quite large — consider splitting into smaller commits.",
        "You should handle the case where the review queue fails to process.",
        "Consider returning 401 when the bearer token is missing.",
        "You should reject an expired token instead of returning 200.",
        "Recommend handling the unauthorized case explicitly.",
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


def test_bot_for_login_claude_requires_the_app_login():
    # The Claude marker is the full ``claude[bot]`` App login, so an unrelated
    # login that merely contains the word "claude" does NOT fold into the claude
    # bot's state.
    assert detectors.bot_for_login("claude-code-helper[bot]") is None
    assert detectors.bot_for_login("claudia-reviewer") is None
    assert detectors.bot_for_login("some-claude-fan") is None


# ---------------------------------------------------------------------------
# F10: repo-scoped token-401 probe (latest_run_token_auth_failed) — the setup
# wizard's re-mint evidence. Best-effort, never raises; reuses AUTH_FAILED_RE.
# ---------------------------------------------------------------------------
import types as _types  # noqa: E402


def _proc(returncode=0, stdout=""):
    return _types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def _probe_run(*, list_out='[{"databaseId": 7, "conclusion": "success"}]', list_rc=0,
               log_failed="", log_full="", raise_on=None):
    """Fake `run` for the probe: serves `gh run list` (the run window, newest-first,
    each with a conclusion) then `gh run view` (--log-failed, falling back to --log)."""
    def run(argv, **kw):
        joined = " ".join(argv)
        if raise_on and raise_on in joined:
            raise OSError("gh missing")
        if argv[:3] == ["gh", "run", "list"]:
            return _proc(returncode=list_rc, stdout=list_out)
        if argv[:3] == ["gh", "run", "view"]:
            out = log_failed if "--log-failed" in argv else log_full
            return _proc(returncode=0 if out else 1, stdout=out)
        return _proc()
    return run


# The bundled workflow's post-step ::error message (lands in the run log on a 401).
_AUTH_401_LOG = ("review\t2026-01-01T00:00:00Z\t::error title=Claude review auth "
                 "failed::CLAUDE_CODE_OAUTH_TOKEN is invalid or expired — the Claude "
                 "review returned 401 (Invalid bearer token) and posted nothing.")
_CLEAN_LOG = "review\t2026-01-01T00:00:00Z\tNo Claude authentication failure detected."
# The App-not-installed failure is ALSO a 401 but reads differently and needs a
# DIFFERENT fix (install the App, not re-mint) — must NOT match.
_APP_NOT_INSTALLED_LOG = ("review\t2026-01-01T00:00:00Z\t401 Claude Code is not "
                          "installed on this repository")
# A run that SUCCEEDED (emitted a clean SDK result) but whose reviewed diff
# quoted the 401 signature — a review OF auth code, not a real auth failure.
_CLEAN_RESULT_WITH_401_QUOTE = (
    'review\t2026-01-01T00:00:00Z\t{"type":"result","is_error":false} — the '
    "review noted the code returns 401 (Invalid bearer token) on bad auth.")


def test_probe_token_401_detected_in_failed_log():
    run = _probe_run(log_failed=_AUTH_401_LOG)
    assert detectors.latest_run_token_auth_failed("acme/widgets", run=run) is True


def test_probe_clean_result_short_circuits_over_quoted_401():
    # is_error:false present → the run succeeded → any 401 phrase is quoted review
    # content, not a real token failure. Must NOT flag (would blind-re-mint a
    # working token).
    run = _probe_run(log_failed="", log_full=_CLEAN_RESULT_WITH_401_QUOTE)
    assert detectors.latest_run_token_auth_failed("acme/widgets", run=run) is False
    # CLEAN_RESULT_RE matches the full SDK result object shape (requires "type":"result"
    # on the same line as "is_error":false) — bare "is_error":false alone does NOT match.
    assert detectors.CLEAN_RESULT_RE.search('{"type":"result","is_error":false}')
    assert detectors.CLEAN_RESULT_RE.search('{"type": "result", "is_error": false}')
    assert not detectors.CLEAN_RESULT_RE.search('{"is_error":false}')


def test_probe_clean_run_not_flagged():
    run = _probe_run(log_failed="", log_full=_CLEAN_LOG)
    assert detectors.latest_run_token_auth_failed("acme/widgets", run=run) is False


def test_probe_app_not_installed_is_not_a_token_401():
    run = _probe_run(log_failed=_APP_NOT_INSTALLED_LOG)
    assert detectors.latest_run_token_auth_failed("acme/widgets", run=run) is False


def test_probe_falls_back_to_full_log_when_no_failed_step():
    # A stale workflow without the post-step 401s GREEN — no failed step, so the 401
    # lives only in the full --log.
    run = _probe_run(log_failed="", log_full=_AUTH_401_LOG)
    assert detectors.latest_run_token_auth_failed("acme/widgets", run=run) is True


def test_probe_no_run_returns_false():
    run = _probe_run(list_out="[]")
    assert detectors.latest_run_token_auth_failed("acme/widgets", run=run) is False


def test_probe_missing_repo_returns_false():
    run = _probe_run(log_failed=_AUTH_401_LOG)
    assert detectors.latest_run_token_auth_failed("", run=run) is False
    assert detectors.latest_run_token_auth_failed(None, run=run) is False


def test_probe_malformed_run_list_json_returns_false():
    run = _probe_run(list_out="not json")
    assert detectors.latest_run_token_auth_failed("acme/widgets", run=run) is False


def test_probe_raising_gh_seam_never_raises():
    # Best-effort contract: any gh/network error → False ("couldn't tell"), no raise.
    assert detectors.latest_run_token_auth_failed(
        "acme/widgets", run=_probe_run(raise_on="run list")) is False
    assert detectors.latest_run_token_auth_failed(
        "acme/widgets", run=_probe_run(log_failed=_AUTH_401_LOG, raise_on="run view")) is False


def test_probe_uses_workflow_basename_and_repo_flag():
    # The list query is scoped by --workflow <basename> + --repo (no local cwd reliance).
    seen = {}

    def run(argv, **kw):
        if argv[:3] == ["gh", "run", "list"]:
            seen["list"] = argv
            return _proc(returncode=0, stdout='[{"databaseId": 7, "conclusion": "success"}]')
        if argv[:3] == ["gh", "run", "view"]:
            return _proc(returncode=0, stdout=_AUTH_401_LOG)
        return _proc()
    assert detectors.latest_run_token_auth_failed("acme/widgets", run=run) is True
    assert "--workflow" in seen["list"]
    assert detectors.CLAUDE_REVIEW_WORKFLOW in seen["list"]
    assert "--repo" in seen["list"] and "acme/widgets" in seen["list"]


def test_probe_skips_skipped_runs_to_the_executed_run():
    """REGRESSION (the buddhi-review silent self-heal bug): the NEWEST Claude run is
    frequently a `skipped` no-op (a non-`@claude` comment trips the workflow's `if:`
    guard) with an EMPTY log. The probe must look PAST it to the most recent EXECUTED
    run and detect THAT run's 401 — not stop at the skipped run and false-negative."""
    # gh returns newest-first: a skipped no-op (id 9) then the executed 401 run (id 7).
    list_out = ('[{"databaseId": 9, "conclusion": "skipped"}, '
                '{"databaseId": 7, "conclusion": "success"}]')
    seen = {}

    def run(argv, **kw):
        if argv[:3] == ["gh", "run", "list"]:
            return _proc(returncode=0, stdout=list_out)
        if argv[:3] == ["gh", "run", "view"]:
            seen["view_id"] = argv[3]
            # Only the EXECUTED run (7) carries the 401; the skipped run (9) has no
            # log. A correct probe never views 9.
            if argv[3] != "7":
                return _proc(returncode=1, stdout="")
            out = "" if "--log-failed" in argv else _AUTH_401_LOG  # green-stale: 401 in full log
            return _proc(returncode=0 if out else 1, stdout=out)
        return _proc()

    assert detectors.latest_run_token_auth_failed("acme/widgets", run=run) is True
    assert seen.get("view_id") == "7", f"probed the wrong run: {seen.get('view_id')!r}"


def test_probe_all_skipped_window_returns_false():
    """A window of ONLY non-executed runs (all skipped/cancelled) → no executed run to
    inspect → False ('couldn't tell'), never a blind re-mint."""
    list_out = ('[{"databaseId": 9, "conclusion": "skipped"}, '
                '{"databaseId": 8, "conclusion": "cancelled"}, '
                '{"databaseId": 7, "conclusion": "action_required"}]')
    run = _probe_run(list_out=list_out, log_full=_AUTH_401_LOG)
    assert detectors.latest_run_token_auth_failed("acme/widgets", run=run) is False
