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


def test_probe_token_401_detected_in_failed_log():
    run = _probe_run(log_failed=_AUTH_401_LOG)
    assert detectors.latest_run_token_auth_failed("acme/widgets", run=run) is True


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
