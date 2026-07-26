"""A reviewer announcing its OWN retirement must stop counting as having reviewed.

A reviewer's consumer version was sunset. It now answers every trigger in ~2s with
a banner: "The consumer version of <product> on GitHub has been sunset. All code
review activity has officially ceased." Every pre-existing detector returned False
for that body, so it read as an ordinary substantive contribution — which fed
``reviewed_ever`` AND (as a raw review payload at the current head) a genuine
commit-sha credit.

THE SAFETY BUG, and the headline of this module: a bot saying "all code review
activity has officially ceased" was contributing a "this bot reviewed the merged
commit" signal to the never-merge-unreviewed gate. Where the retired bot is the
ONLY configured reviewer, the gate auto-merges code nobody read.

THE FALSE-POSITIVE GUARD is the load-bearing part, and the reason this module is
mostly negative assertions. A stray match silences a HEALTHY reviewer for the
whole run with NO retraction path — strictly worse than the bug. Adversarial
review of the first cut broke it exactly there: with any review-domain noun
accepted, 14 of 34 fresh realistic review bodies matched, and 8/8 on customer
repos where "review" IS the product domain noun (product reviews, peer review,
performance review, contract review, moderation queues). The whole adversarial
corpus is pinned below as :data:`ADVERSARIAL_FALSE_POSITIVES`; a regression that
re-admits any of it is the worst failure this feature can have.

GENERIC BY CONTRACT: no bot or vendor name appears in the detector or any cause
table — :class:`TestNoBotNamesHardcoded` greps the shipped constants to keep it
that way.
"""
import re

import pytest

from buddhi_review import detectors, gh_ingest, round_driver
from buddhi_review.loop import Comment

from test_head_aware_merge_gate import _gate_driver, _inline, _review
from test_round_driver import CLAUDE_ONLY, HEAD_SHA, GhRecorder, make_driver


# ─────────────────────────────────────────────────────────────────────
# The real banner shape, with the vendor name removed (this module is
# bot-agnostic by contract — see TestNoBotNamesHardcoded).
# ─────────────────────────────────────────────────────────────────────
RETIREMENT_BANNER = (
    "> [!CAUTION]\n"
    "> The consumer version of the review product on GitHub has been sunset. "
    "All code review activity has officially ceased."
)

# This capability's OWN PR text. A naive text match on sunset / discontinued /
# ceased would trip on it — the false-positive guard exists precisely for this.
THIS_PR_TITLE = (
    "fix(detectors): a reviewer that announces its own permanent retirement "
    "must stop counting as having reviewed"
)
THIS_PR_BODY = (
    "A reviewer's consumer version was sunset and it now answers every trigger "
    "with a banner saying all code review activity has officially ceased. That "
    "banner matches none of the placeholder detectors, so it read as an ordinary "
    "contribution and the bot was credited as having reviewed. This adds a "
    "generic `retired` cause — self-reported permanent shutdown (sunset, "
    "discontinued, ceased, retired, no longer available, end-of-life, has been "
    "shut down) — subtracts it from the reviewed set so a retirement notice can "
    "never satisfy the never-merge-unreviewed gate, gives it its own `Retired ⛔` "
    "status, and stops re-summoning the dead reviewer every round."
)

# A GENUINE review of this very PR, echoing the vocabulary it standardizes.
# Short enough to clear the length gate on purpose — the content gate, not the
# length gate, is what must save this reviewer.
GENUINE_REVIEW_OF_THIS_PR = (
    "Pull request overview: this adds a detector for a reviewer that announces "
    "its own permanent shutdown. The review activity has been correctly wired "
    "into the merge gate. One nit on line 40."
)


# ═════════════════════════════════════════════════════════════════════
# 1. The gap this closes: the banner matched NOTHING before.
# ═════════════════════════════════════════════════════════════════════

class TestDetectionGap:
    def test_banner_matches_no_pre_existing_detector(self):
        # Documents the exact hole: every pre-existing cause said "not me", so
        # the banner read as an ordinary contribution and the bot looked like a
        # reviewer that had reviewed.
        assert not detectors.QUOTA_RE.search(RETIREMENT_BANNER)
        assert not detectors.PR_TOO_LARGE_RE.search(RETIREMENT_BANNER)
        assert not detectors._errored_outside_quotes(RETIREMENT_BANNER)
        assert not detectors.is_clean_review(RETIREMENT_BANNER)

    def test_banner_is_now_detected_as_retired(self):
        assert detectors.is_retired_message(RETIREMENT_BANNER)
        assert detectors.detect_signal(RETIREMENT_BANNER) == detectors.SIGNAL_RETIRED

    def test_banner_is_a_placeholder_body(self):
        # The head-aware gate's sha-credit veto must reject it: a retirement
        # notice is the strongest possible statement that this body is not a
        # review of the commit it carries.
        assert detectors.is_placeholder_review_body(RETIREMENT_BANNER)

    def test_genuine_feedback_after_a_retirement_mention_stays_a_review(self):
        # detect_signal must route retirement through the GUARDED
        # is_retired_message(), not a raw RETIRED_PATTERNS scan: a raw scan
        # matches the leading sentence and ignores the review-feedback veto,
        # wrongly excluding a bot that is still mid-burst with real findings.
        body = "This code review service has been discontinued. Consider pinning the SDK."
        assert not detectors.is_retired_message(body)
        assert detectors.detect_signal(body) is None
        assert not detectors.is_placeholder_review_body(body)


# ═════════════════════════════════════════════════════════════════════
# 2. Detector positives / negatives. The negatives are the bar that
#    matters: a false positive silences a HEALTHY reviewer.
# ═════════════════════════════════════════════════════════════════════

RETIRED_POSITIVES = [
    RETIREMENT_BANNER,
    "The consumer version of the product on GitHub has been sunset. "
    "All code review activity has officially ceased.",
    "This code review service has been discontinued.",
    "Our review bot has been permanently retired; please use another tool.",
    "Our integration has been shut down and will no longer review pull requests.",
    "Our code review support is no longer available.",
    "We have ceased all code review operations on GitHub.",
    "This code review app has been decommissioned.",
    "All automated code review activity has been permanently disabled.",
    "Notice: this code review integration has been retired. No further code "
    "reviews will be posted.",
    "Our code review service has been shut down as of 2026-07-01.",
    "This review bot has reached end-of-life.",
    # Bare tense (no has/have/is auxiliary), self-anchored — must still match.
    "Our review bot ceased reviewing as of today.",
    # "will no longer review …", self-anchored by subject as well as object.
    "This code review service has been sunset and will no longer review pull "
    "requests.",
    "Our review bot has been retired and will no longer provide code reviews.",
    "We will no longer review pull requests.",
    # First-person cessation, object-anchored (the "code review" noun form is
    # already covered above by "We have ceased all code review operations").
    "We have discontinued reviewing pull requests.",
    "We have ceased reviewing all pull requests.",
]

# ─────────────────────────────────────────────────────────────────────
# THE ADVERSARIAL CORPUS. Every body below broke the FIRST cut of this
# detector — an adversarial pass reproduced 14 false positives out of 34 fresh
# realistic review bodies, and 8/8 on customer repos where "review" IS the
# product domain noun. On an ordinary PR the second pass never arms, so each one
# permanently silenced a healthy reviewer with no retraction path. They are
# pinned here verbatim: a regression that re-admits any of them is the worst
# failure this feature can have.
# ─────────────────────────────────────────────────────────────────────
ADVERSARIAL_FALSE_POSITIVES = [
    # Someone ELSE's dead review service, described as code feedback.
    "This helper still targets the REST review API, which reaches end-of-life "
    "in Q4. Not blocking for this PR, but worth a TODO.",
    "The vendor's review service has been sunset, and this module still imports "
    "its SDK. Please pin the fallback so the import error is handled gracefully.",
    "The third-party reviewing service we integrate with was shut down in June; "
    "the retry loop here will spin forever against a dead host. Add a circuit "
    "breaker.",
    "The upstream code review integration has been retired, so this adapter is "
    "unreachable. Delete it rather than leaving it half-wired.",
    "Note that the free reviewing tier has been discontinued by the provider, "
    "so this code path never executes in production any more.",
    "The starter plan's code review support is no longer available, which means "
    "the enterprise branch here is the only live one.",
    "Review activity dropped after the migration, and separately the legacy "
    "review service was decommissioned; both facts matter for this metric.",
    "Docstring says the review service has been shut down, but the code still "
    "calls it. Align the docstring with reality.",
    "Typo: 'sunsetted' -> 'sunset'. Also the sentence claims all review "
    "activity has ceased, which is not what the flag does.",
    "Since the review service was decommissioned we no longer need the abstract "
    "base; collapsing it into one class would remove ~80 lines.",
    "The review queue consumer has been terminated in staging, so the "
    "integration test will hang. Point it at the mock.",
    "This N+1 loads every review row; on repos where review coverage is no "
    "longer available it still issues the query. Guard the call.",
    "Our code review capability is no longer being maintained by that team, so "
    "this hook should be removed.",
    "The migration doc says review support is no longer offered for v1 repos; "
    "update the link.",
    # Customer repos where "review" is the PRODUCT domain noun. The loop runs on
    # the customer's repos, so this is the majority of the install base.
    "The product review service has been decommissioned by the vendor, so "
    "`fetch_reviews()` returns 502 in prod. Please add the fallback.",
    "Star-rating review functionality has been permanently disabled for EU "
    "customers; this component still renders it.",
    "Our reviews service is no longer available on the legacy cluster — point "
    "the client at the new host.",
    "The peer review service was shut down last year; this importer will never "
    "receive a payload.",
    "Performance review support is no longer offered in the HR module, so this "
    "migration drops a live table.",
    "The restaurant review feed has been discontinued upstream; cache the last "
    "snapshot instead of failing hard.",
    "Contract review capabilities are no longer supported by that API tier. "
    "This call needs the enterprise key.",
    "The manual review queue has been retired, so `enqueue_for_review()` is "
    "dead code now.",
    "## Pull request overview\n\nThis PR removes the legacy ratings client. The "
    "product review service was decommissioned by the vendor last quarter, so "
    "the adapter is dead code.",
    # Short bot summaries posted ON THIS VERY PR.
    "## Pull request overview\n\nThis PR teaches the loop that a reviewer "
    "announcing its own sunset is not a review. Looks correct.",
    "Summary: adds RETIRED_PATTERNS. The motivating case is a reviewer whose "
    "code review activity has officially ceased.",
    "This change handles the case where our review service has been sunset.",
    "The PR adds a detector for the banner. Note the banner claims all code "
    "review activity has officially ceased.",
    # The "(has) ceased (all) code review(ing)" pattern lacked the self-subject
    # anchor every sibling pattern requires, so it matched an ordinary sentence
    # about an unrelated subject that happens to use "ceases ... reviewing".
    "The callback ceases reviewing once the queue is empty.",
    "This poller ceases reviewing tasks after three failed retries, which is "
    "expected behavior.",
    # The GLOBAL-QUANTIFIER group had no self-reference anchor: "all|every" was
    # read as a self-announcement signature. A quantifier says nothing about WHO
    # stopped, and the feedback veto does not recognize scope qualifiers, so a
    # code-review OUTAGE reported as ordinary feedback matched. See
    # TestGlobalQuantifierNeedsAnnouncementRegister for the positive direction.
    "All code review activity has ceased in CI since the workflow condition "
    "changed.",
    "All code review coverage has ceased for this repository.",
    "Every code review service has been terminated on forks.",
    "All code review functionality has ceased in the pipeline.",
    "All code review operations have ceased in staging.",
    "All code review support has been permanently disabled for this repository.",
    "All code review activity has officially ceased in CI since the workflow "
    "condition changed.",
    # Same group, unscoped but in DESCRIPTIVE register (no permanence adverb,
    # no self-anchored shutdown claim beside it). A sunset banner declares;
    # feedback describes.
    "All code review activity has ceased.",
    "Every code review service has been shut down.",
    # The "will no longer review …" member carried only an OBJECT anchor (the
    # thing not being reviewed), never a SUBJECT anchor, so ordinary feedback
    # describing what the DIFF does to a reviewer matched and retired the
    # healthy bot that wrote it.
    "With the new condition, the bot will no longer review pull requests from "
    "forks; that breaks fork coverage.",
    "After the guard is added, CI will no longer review pull requests from forks.",
    "The workflow will no longer post automated code reviews for dependabot "
    "branches.",
    "The vendor bot will no longer provide code reviews here.",
    "Once merged, the action will no longer generate code reviews on drafts.",
    # The self-referential SUBJECT anchor was separated from the retirement
    # verb by a same-clause WILDCARD gap, so a NESTED second subject could sit
    # between them: the anchor matched "our code reviewer" while the verb in
    # fact predicated "the legacy service". Every body below is a healthy
    # reviewer REPORTING a dead thing it found in the diff — the exact sentence
    # shape a reviewer writes — and each retired that reviewer for the run with
    # no retraction path. See _RETIRED_SUBJECT_GAP.
    "Our code reviewer found that the legacy service has been retired.",
    "This code review helper shows the vendor service has been sunset.",
    "Our review service documentation says the old plan was discontinued.",
    "Our code reviewer says the other tool has been retired.",
    "Our code review bot warns the upstream integration was decommissioned.",
    "Our code review bot noted that the REST API reaches end-of-life in Q4.",
    "The legacy service reaches end-of-life, and our code review bot will "
    "need updating.",
    "Our code reviewer confirmed that CI will no longer review pull requests.",
    "Our code review tool reported that the poller ceases reviewing after "
    "retries.",
    # The FIRST-PERSON CESSATION member accepted a bare "reviewing" with NO
    # code/PR object, so ordinary first-person feedback about what the DIFF
    # stopped covering matched. None of these carries a feedback-veto marker,
    # so on a PR whose title/body lacks retirement vocabulary the second pass
    # never arms. See _RETIRED_REVIEW_OBJECT and
    # TestFirstPersonCessationNeedsACodeObject.
    "We have discontinued reviewing dependency updates in this workflow "
    "because the path filter is too broad; restore that coverage.",
    "We have ceased reviewing the vendored SDK files, which hides real drift.",
    "I have discontinued reviewing generated migrations in this repository.",
    "We have ceased reviewing anything under the vendor tree, and this PR does "
    "not restore it.",
]

RETIRED_NEGATIVES = [
    # Ordinary clean / placeholder / substantive bodies.
    "No issues found.",
    "LGTM!",
    "Consider adding a null check on line 42.",
    "You have reached your daily quota limit. Try again in 60 minutes.",
    "An error occurred while generating the review. Please try again later.",
    "The number of changes in this pull request is too large to generate a review.",
    # Substantive prose ABOUT retired/sunset things in the code under review.
    # Each of these is real reviewer feedback, not a self-report.
    "The `legacy_paths` helper is deprecated; consider removing it.",
    "This function was retired in v2 — the call on line 42 will fail.",
    "The old API endpoint has been discontinued upstream, so this request will 404.",
    "The `foo` flag was sunset in release 3.1; this code path is dead.",
    "The service tier used here was decommissioned last year; fall back to v2.",
    "Nit: the comment says 'end-of-life' but the code says EOL — align the wording.",
    "The v1 endpoint has been discontinued upstream — please review the fallback path.",
    "This helper was retired in the last refactor; the review comment above still applies.",
    "The `--legacy` flag has been removed. Reviewing the rest of the diff, everything looks fine.",
    "Test coverage was disabled here; review activity in CI has stopped for this path.",
    "The feature flag `retired_reviewer` is no longer available in config — this lookup will KeyError.",
    "The staging service has been terminated. Since code review of that module is out of scope, no action needed.",
    "Note: our review process has been formally documented; nothing has been discontinued.",
    "The old plan was sunset. Code review continues as normal on the new plan.",
    "The service has been shut down in tests via the fixture, and review of the shutdown path looks correct.",
    "The review job was disabled in CI; re-enable it before merging.",
    "The `review` table row was dropped; the reviewer column is no longer available in the schema.",
    # A genuine review OF THIS PR.
    GENUINE_REVIEW_OF_THIS_PR,
]


class TestRetiredDetector:
    @pytest.mark.parametrize("body", RETIRED_POSITIVES)
    def test_positive(self, body):
        assert detectors.is_retired_message(body)

    @pytest.mark.parametrize("body", RETIRED_NEGATIVES)
    def test_negative(self, body):
        assert not detectors.is_retired_message(body)

    @pytest.mark.parametrize("body", ADVERSARIAL_FALSE_POSITIVES)
    def test_adversarial_false_positive(self, body):
        assert not detectors.is_retired_message(body), (
            "re-admitted an adversarial false positive — this silences a HEALTHY "
            "reviewer for the whole run with no retraction path")

    def test_review_feedback_veto(self):
        # A body that comments on the DIFF is a review, whatever vocabulary it
        # borrows. The veto is the last line of defense and spends a false
        # NEGATIVE (the bot is merely awaited until quiescence) to buy immunity
        # from the false POSITIVE (a healthy reviewer silenced for the run).
        assert not detectors.is_retired_message(
            "This code review service has been discontinued. Consider pinning the SDK.")
        assert not detectors.is_retired_message(
            "Our code review bot has been retired.\n- fix the import on line 3")
        assert not detectors.is_retired_message(
            "Our code review service has been sunset.\n```python\nx = 1\n```")

    def test_domain_review_nouns_never_match(self):
        # (a) CODE review only — a bare "review" noun never counts. This is what
        # removes the entire product-domain class (product reviews, peer review,
        # performance review, contract review, moderation queues) at a stroke.
        for noun in ("product review", "peer review", "performance review",
                     "contract review", "manual review", "restaurant review"):
            assert not detectors.is_retired_message(
                f"Our {noun} service has been permanently discontinued."), noun

    def test_someone_elses_dead_service_never_matches(self):
        # (b) the SELF-REFERENCE anchor. Someone ELSE's dead service is review
        # feedback, not a self-report.
        for det in ("the vendor's", "the upstream", "the third-party",
                    "the legacy", "the consumer"):
            assert not detectors.is_retired_message(
                f"{det} code review integration has been retired."), det

    def test_empty_body(self):
        assert not detectors.is_retired_message("")
        assert not detectors.is_retired_message(None)
        assert not detectors.is_retired_message("   \n  ")

    def test_length_gate_rejects_a_long_review_that_quotes_the_banner(self):
        # A retirement notice is a short standalone banner; a genuine review that
        # merely quotes or discusses one is long. Measured on the RAW body —
        # BEFORE any boilerplate strip, since the observed real notice IS a
        # `> [!CAUTION]` admonition the stripper would remove wholesale.
        long_review = ("Some analysis of the diff. " * 30) + RETIREMENT_BANNER
        assert len(long_review) > detectors._RETIRED_MAX_LEN
        assert not detectors.is_retired_message(long_review)
        # …and the banner alone, unpadded, still matches.
        assert detectors.is_retired_message(RETIREMENT_BANNER)

    def test_quoted_vocabulary_is_stripped(self):
        # Backticked / fenced / quoted mentions of shutdown wording inside a
        # genuine review are the author MENTIONING the words, never the bot
        # reporting its own death.
        assert not detectors.is_retired_message(
            "The banner reads \"this code review service has been discontinued\" "
            "which the test asserts on.")

    @pytest.mark.parametrize("fence", ["```", "~~~"])
    def test_a_banner_quoted_in_either_fence_retires_nobody(self, fence):
        # BOTH CommonMark fences must be stripped. ``~~~`` is legal everywhere
        # ``` is, and while it went unstripped a healthy reviewer quoting a
        # retirement banner inside one was PERMANENTLY retired.
        body = (f"Docs-only update; the vendored banner copy now reads:\n\n"
                f"{fence}\nOur code review service has been discontinued.\n{fence}\n\n"
                f"Everything else matches upstream.")
        assert not detectors.is_retired_message(body)
        assert detectors.detect_signal(body) is None

    def test_the_retired_strip_covers_the_alt_fence(self):
        assert "~~~" in detectors._RETIRED_QUOTED_VOCAB_RE.pattern
        assert "~" in detectors._QUOTED_VOCAB_DELIMITERS  # the cheap pre-test
        assert detectors._strip_quoted_vocab("a ~~~dead~~~ b").strip() == "a   b".strip()

    @pytest.mark.parametrize("body", [
        # Each of these matches the retirement patterns ONLY through its
        # declining clause: blank that clause out and no retirement claim
        # remains. They are the inputs that isolate the conjunct — with it they
        # keep the full veto (and "this pull request" vetoes them); without it
        # they earn the exemption on the strength of the decline alone and
        # retire the reviewer.
        "Our integration will no longer review this pull request.",
        "Our review bot will no longer review this pull request.",
        "This code review tool will no longer review this pull request.",
    ])
    def test_the_earned_exemption_requires_an_INDEPENDENT_retirement_claim(self, body):
        # A decline alone is not an announcement. The
        # `_retired_patterns_match(b_no_decline)` half of `earns_exemption` is
        # what keeps the exemption from resting on the declining clause itself.
        lowered = body.lower()
        blanked = detectors._RETIRED_DECLINED_PR_RE.sub(" ", lowered)
        assert blanked != lowered                              # a decline IS present
        assert detectors._retired_patterns_match(lowered)      # …and carries the match
        assert not detectors._retired_patterns_match(blanked)  # …which is ALL it carries
        assert not detectors.is_retired_message(body)

    def test_an_independently_anchored_notice_still_earns_the_exemption(self):
        # The exemption is not dead code: a notice whose retirement claim stands
        # WITHOUT the declining clause may still name the PR it is refusing.
        body = ("Our code review service has been retired and will no longer "
                "review this pull request.")
        blanked = detectors._RETIRED_DECLINED_PR_RE.sub(" ", body.lower())
        assert detectors._retired_patterns_match(blanked)   # stands on its own
        assert detectors.is_retired_message(body)

    def test_someone_elses_dead_service_plus_a_decline_is_still_feedback(self):
        for body in [
            "The vendor's code review service has been discontinued, so CI will "
            "no longer review this pull request. Drop the adapter.",
            "The upstream review integration was sunset; this pull request will "
            "not be reviewed by it any more. Remove the badge.",
        ]:
            assert not detectors.is_retired_message(body), body

    def test_every_pattern_compiles(self):
        for p in detectors.RETIRED_PATTERNS:
            re.compile(p)
        assert len(detectors.RETIRED_PATTERNS) == (
            len(detectors._RETIRED_GLOBAL_PATTERNS)
            + len(detectors._RETIRED_ANCHORED_PATTERNS))

    @pytest.mark.parametrize("body", [
        "Our code review service has been retired and will no longer review "
        "this pull request.",
        "This code review service has been discontinued. This pull request "
        "will not be reviewed.",
    ])
    def test_notice_may_name_the_pr_it_declines_to_review(self, body):
        # The ONE way a shutdown notice legitimately names the PR: to say it is
        # DECLINING to review it. That clause is blanked from the COPY the veto
        # scans — but only because the retirement claim survives without it.
        assert detectors.is_retired_message(body)

    def test_declined_pr_exemption_does_not_widen_the_veto(self):
        # The exemption is EARNED: it applies only when the retirement claim
        # stands WITHOUT the declining clause. Someone else's dead service plus a
        # decline is ordinary feedback and keeps the full veto.
        assert not detectors.is_retired_message(
            "The vendor's code review service has been discontinued, so CI will "
            "no longer review this pull request. Drop the adapter.")
        # And every OTHER mention of "this PR" still vetoes in full.
        assert not detectors.is_retired_message(
            "This PR notes that this code review service has been discontinued.")


class TestTheVerbBindsToTheReviewerSubject:
    """(c) `_RETIRED_SUBJECT_GAP` — between a self-referential subject and its
    retirement verb, only material CONTINUING the subject noun phrase is
    admitted, so a NESTED second subject can never carry the verb."""

    @pytest.mark.parametrize("reporting", [
        "found that", "shows", "says", "warns", "noted that", "reported that",
        "confirmed that", "observed that",
    ])
    def test_a_nested_subject_never_carries_the_verb(self, reporting):
        assert not detectors.is_retired_message(
            f"Our code review bot {reporting} the legacy service has been retired.")

    @pytest.mark.parametrize("body", [
        "This code review integration has been retired.",
        "Our code review app on GitHub has been sunset.",
        "This code review service has been decommissioned.",
    ])
    def test_a_subject_bound_predicate_still_matches(self, body):
        assert detectors.is_retired_message(body)

    def test_the_self_shutdown_escape_hatch_is_bound_too(self):
        # _RETIRED_SELF_SHUTDOWN_RE (the global group's escape hatch) uses the
        # same subject gap, so a nested subject cannot supply it either.
        assert not detectors._RETIRED_SELF_SHUTDOWN_RE.search(
            "our review bot reported that the legacy service has been sunset")
        assert detectors._RETIRED_SELF_SHUTDOWN_RE.search(
            "this service has been sunset")


class TestFirstPersonCessationNeedsACodeObject:
    """A bare "reviewing <anything>" is ordinary first-person review prose about
    the diff, never a retirement notice — the OBJECT carries the code anchor."""

    @pytest.mark.parametrize("obj", [
        "dependency updates in this workflow", "the vendored SDK files",
        "generated migrations", "anything under the vendor tree",
        "the docs directory",
    ])
    def test_a_bare_reviewing_object_never_retires_anybody(self, obj):
        assert not detectors.is_retired_message(
            f"We have discontinued reviewing {obj}.")

    @pytest.mark.parametrize("body", [
        "We have discontinued reviewing pull requests.",
        "We have ceased reviewing all pull requests.",
        "We have ceased all code review operations.",
    ])
    def test_a_code_or_pr_object_still_matches(self, body):
        assert detectors.is_retired_message(body)

    def test_the_object_anchor_is_shared_with_will_no_longer_review(self):
        # One constant feeds both members, so the two can never drift apart.
        assert detectors._RETIRED_REVIEW_OBJECT in detectors._RETIRED_ANCHORED_PATTERNS[1]
        assert detectors._RETIRED_REVIEW_OBJECT in detectors._RETIRED_ANCHORED_PATTERNS[2]


class TestGlobalQuantifierNeedsAnnouncementRegister:
    """(f) The global-quantifier group is the ONE group with no inline
    self-reference anchor, so it carries two replacement guards: an announcement
    register and a repo-scope veto."""

    def test_bare_global_cessation_is_not_enough(self):
        # Descriptive register — a sunset banner declares; feedback describes.
        assert not detectors.is_retired_message("All code review activity has ceased.")

    def test_permanence_adverb_admits_the_real_banner(self):
        assert detectors.is_retired_message(
            "All code review activity has officially ceased.")

    @pytest.mark.parametrize("adverb", [
        "officially", "permanently", "formally", "indefinitely", "entirely",
        "completely", "fully",
    ])
    def test_every_permanence_adverb_is_accepted(self, adverb):
        assert detectors.is_retired_message(
            f"All code review activity has {adverb} ceased.")

    def test_now_is_not_a_permanence_adverb(self):
        # "now" reads as descriptive ("has now ceased"), not as a final
        # declaration, so it is deliberately absent from _RETIRED_FINAL_ADV.
        assert not detectors.is_retired_message(
            "All code review activity has now ceased.")

    def test_adverb_position_around_been_does_not_matter(self):
        assert detectors.is_retired_message(
            "All code review activity has been permanently disabled.")
        assert detectors.is_retired_message(
            "All code review activity has permanently been disabled.")

    def test_self_anchored_shutdown_beside_it_substitutes_for_the_adverb(self):
        # The escape hatch: an independent self-anchored shutdown claim in the
        # body stands in for the missing permanence adverb.
        assert detectors.is_retired_message(
            "This service has been sunset. All code review activity has ceased.")

    def test_someone_elses_shutdown_does_not_substitute(self):
        assert not detectors.is_retired_message(
            "The vendor service has been sunset. All code review activity has ceased.")

    @pytest.mark.parametrize("scoped", [
        "in CI", "for this repository", "on forks", "in the pipeline",
        "in staging", "for this repo", "in our workflows",
    ])
    def test_a_repo_scoped_cessation_is_an_outage_not_a_sunset(self, scoped):
        # A vendor sunset is global by definition; a scope qualifier names a
        # place inside the codebase under review, which makes it feedback.
        assert not detectors.is_retired_message(
            f"All code review activity has officially ceased {scoped}.")

    def test_platform_names_are_not_scope_qualifiers(self):
        # The real observed banner says "…on GitHub has been sunset".
        assert detectors.is_retired_message(RETIREMENT_BANNER)
        assert not detectors._RETIRED_SCOPED_QUALIFIER_RE.search("on github")

    def test_the_scope_veto_does_not_reach_the_anchored_patterns(self):
        # An ANCHORED pattern names its own subject, so a hit is conclusive and
        # the global-group guards do not apply to it.
        assert detectors.is_retired_message(
            "Our code review service has been decommissioned in this repository.")

    def test_flat_union_holds_every_pattern(self):
        assert detectors.RETIRED_PATTERNS == (
            detectors._RETIRED_GLOBAL_PATTERNS
            + detectors._RETIRED_ANCHORED_PATTERNS)


# ═════════════════════════════════════════════════════════════════════
# 3. The second-pass content gate — the PR whose own subject IS reviewer
#    sunsetting (this capability's own PR is one).
# ═════════════════════════════════════════════════════════════════════

def _llm(answer):
    """A quota_llm seam returning a fixed ``self_reporting`` verdict; ``None``
    models an unreachable / unparseable model."""
    def call(prompt):
        return None if answer is None else {"self_reporting": answer}
    return call


class TestFalsePositiveGuard:
    def test_this_capabilitys_own_pr_trips_the_vocabulary_predicate(self):
        assert detectors._pr_is_about_cause(
            detectors.SIGNAL_RETIRED, THIS_PR_TITLE, THIS_PR_BODY)

    def test_unrelated_pr_does_not_trip_the_predicate(self):
        assert not detectors._pr_is_about_cause(
            detectors.SIGNAL_RETIRED, "Add a null check to the parser",
            "Fixes a crash when the payload is empty.")

    def test_unknown_pr_meta_ARMS_the_guard_for_retired_only(self):
        # DELIBERATELY INVERTED. A transient `gh pr view` failure yields
        # (None, None); for the three recoverable causes that short-circuits to
        # "exclude", which is right for them. Retirement never recovers, so an
        # unreadable PR must NOT disable the guard.
        assert detectors._pr_is_about_cause(detectors.SIGNAL_RETIRED, None, None)
        for cause in (detectors.SIGNAL_QUOTA, detectors.SIGNAL_PR_TOO_LARGE,
                      detectors.SIGNAL_ERRORED):
            assert not detectors._pr_is_about_cause(cause, None, None), cause

    def test_gate_keeps_the_exclusion_on_an_unrelated_pr(self):
        # On the vast majority of PRs the predicate never fires, so the
        # deterministic verdict stands with no model call.
        calls = []

        def counting(prompt):
            calls.append(prompt)
            return {"self_reporting": False}

        assert detectors.detect_signal(
            RETIREMENT_BANNER, quota_llm=counting,
            pr_title="Add a null check", pr_body="Fixes a crash.",
        ) == detectors.SIGNAL_RETIRED
        assert calls == []

    @pytest.mark.parametrize("title", [
        "Kill the legacy code review integration",
        "kill the consumer review bot",
        "chore: killing the free tier reviewer",
    ])
    def test_the_blunt_kill_framing_also_arms_the_guard(self, title):
        # A cleanup PR titles itself bluntly. Without this vocabulary the guard
        # never armed on such a PR, so a healthy reviewer describing it was
        # excluded with no model call.
        assert detectors._pr_is_about_cause(detectors.SIGNAL_RETIRED, title, "")
        assert detectors.detect_signal(
            "Our code review service has been discontinued.",
            quota_llm=_llm(False), pr_title=title, pr_body="") is None

    def test_retired_is_classified_BEFORE_the_recoverable_causes(self):
        # The cascade order in detect_signal is load-bearing, not cosmetic: a
        # body carrying BOTH a retirement claim and a recoverable cause's
        # vocabulary must classify as the PERMANENT one, or the loop would
        # report a cause that implies the reviewer is coming back.
        both = ("Our code review service has been permanently discontinued. "
                "Your quota limit was exceeded.")
        assert detectors.QUOTA_RE.search(both)          # the weaker cause matches too
        assert detectors.is_retired_message(both)
        assert detectors.detect_signal(both) == detectors.SIGNAL_RETIRED

    def test_gate_suppresses_a_healthy_reviewer_on_a_sunsetting_pr(self):
        # The case this guard exists for: on a PR ABOUT reviewer sunsetting, a
        # reviewer merely DESCRIBING that content must stay active.
        assert detectors.detect_signal(
            "All code review activity has officially ceased.",
            quota_llm=_llm(False), pr_title=THIS_PR_TITLE, pr_body=THIS_PR_BODY,
        ) is None

    def test_gate_excludes_when_the_model_confirms_self_reporting(self):
        assert detectors.detect_signal(
            RETIREMENT_BANNER, quota_llm=_llm(True),
            pr_title=THIS_PR_TITLE, pr_body=THIS_PR_BODY,
        ) == detectors.SIGNAL_RETIRED

    def test_gate_fails_open_to_exclude_when_the_model_errors(self):
        assert detectors.detect_signal(
            RETIREMENT_BANNER, quota_llm=_llm(None),
            pr_title=THIS_PR_TITLE, pr_body=THIS_PR_BODY,
        ) == detectors.SIGNAL_RETIRED

    def test_a_genuine_review_of_this_pr_is_never_retired(self):
        # Deterministically — the content gate is not even needed here, the
        # review-feedback veto already saves this reviewer.
        assert not detectors.is_retired_message(GENUINE_REVIEW_OF_THIS_PR)
        assert detectors.detect_signal(
            GENUINE_REVIEW_OF_THIS_PR, quota_llm=_llm(True),
            pr_title=THIS_PR_TITLE, pr_body=THIS_PR_BODY) is None

    def test_retired_is_registered_in_every_cause_table(self):
        assert detectors.SIGNAL_RETIRED in detectors._PR_CAUSE_VOCAB
        assert detectors.SIGNAL_RETIRED in detectors._CAUSE_SELF_REPORT
        assert detectors.SIGNAL_RETIRED in detectors._PR_CAUSE_UNKNOWN_META_ARMS
        assert round_driver._STATUS_SHORT["retired"] == "Retired ⛔"
        assert "retired" in round_driver._SKIP_LONG


# ═════════════════════════════════════════════════════════════════════
# 4. A retirement notice is a RESPONSE, never a REVIEW.
# ═════════════════════════════════════════════════════════════════════

class TestRetirementIsNotAReview:
    def test_review_sha_is_never_credited_for_a_retirement_notice(self):
        assert round_driver._genuine_review_shas_by_bot(
            [_review("claude", "c1", body=RETIREMENT_BANNER)], []) == {}

    def test_a_genuine_review_still_gets_its_sha(self):
        assert round_driver._genuine_review_shas_by_bot(
            [_review("claude", "c1", body="please fix the null check")], []
        ) == {"claude": {"c1"}}

    def test_classify_signal_records_the_cause_and_never_credits_a_review(self):
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        out = driver._classify_signal(
            Comment(id="a", text=RETIREMENT_BANNER, source="claude[bot]"), now=1.0)
        assert out is None                       # not actionable
        assert "claude" in driver._retired
        assert driver.store.is_excluded("claude")
        assert "claude" not in driver.reviewed_ever
        assert driver._genuine_reviewers() == set()

    def test_a_prior_review_credit_is_revoked_when_the_notice_arrives(self):
        # A bot credited earlier in the run must LOSE that credit: a service
        # that announced its own shutdown did not review the merged code.
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        driver.reviewed_ever.add("claude")
        driver._clean_signal_head["claude"] = HEAD_SHA
        driver.done.add("claude")
        driver.approved.add("claude")
        driver._classify_signal(
            Comment(id="a", text=RETIREMENT_BANNER, source="claude[bot]"), now=1.0)
        assert "claude" not in driver.reviewed_ever
        assert "claude" not in driver._clean_signal_head
        assert "claude" not in driver.done
        assert "claude" not in driver.approved

    def test_genuine_reviewers_subtracts_even_a_re_added_entry(self):
        # The read-time subtraction is what makes the property hold for EVERY
        # writer of reviewed_ever, present and future — not just the eager
        # discard in _record_retired.
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        driver._record_retired("claude")
        driver.reviewed_ever.add("claude")      # some later path re-adds it
        assert driver._genuine_reviewers() == set()

    def test_a_retirement_notice_still_counts_as_a_RESPONSE(self):
        # It is a response (so the reviewer is not ALSO reported as wastefully
        # silent) but never a review.
        timeline = [(0, Comment(id="a", text=RETIREMENT_BANNER, source="claude[bot]"))]
        driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY)
        driver.run()
        assert "claude" in driver.responded_ever
        assert driver._genuine_reviewers() == set()


# ═════════════════════════════════════════════════════════════════════
# 5. THE REGRESSION — a fleet whose only reviewer retires must BLOCK.
# ═════════════════════════════════════════════════════════════════════

class TestNeverMergeUnreviewedGate:
    def test_sole_reviewer_retirement_blocks_the_merge(self):
        """THE REGRESSION. Before this change the banner read as an ordinary
        contribution: it fed ``reviewed_ever`` AND (as a raw review payload at
        the current head) a genuine commit-sha credit, so the head-aware gate
        PASSED and the loop auto-merged a PR nobody had read.

        Run end-to-end through the real round loop + clean-exit merge path, with
        auto-merge ON — the configuration where the bug actually merges."""
        timeline = [(0, Comment(id="a", text=RETIREMENT_BANNER, source="claude[bot]"))]
        driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY, auto_merge=True)
        outcome = driver.run()
        assert outcome.merged is False, (
            "a reviewer announcing its own retirement satisfied the "
            "never-merge-unreviewed gate — the PR merged unreviewed")
        assert gh.matching("gh", "merge", "--squash") == []
        assert "claude" in driver._retired

    def test_a_notice_already_on_the_pr_at_launch_also_blocks(self):
        # The PREFLIGHT path (a restart, or a PR the reviewer already answered
        # before this run started). The notice is ingested through the same
        # _classify_signal fold, so the cause is recorded before round 1 and the
        # gate blocks exactly as it does for a mid-round notice.
        timeline = [(0, Comment(id="a", text=RETIREMENT_BANNER, source="claude[bot]",
                                created_at="2026-01-01T00:00:00+00:00"))]
        driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY,
                                        auto_merge=True, preflight=True)
        outcome = driver.run()
        assert "claude" in driver._retired
        assert outcome.merged is False
        assert driver._genuine_reviewers() == set()

    def test_the_same_harness_merges_for_a_healthy_reviewer(self):
        # THE CONTROL for the regression above. Same fleet, same harness, same
        # auto-merge setting — only the message differs. Without it the block
        # above could be an artifact of the harness rather than the notice.
        timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
        driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY, auto_merge=True)
        assert driver.run().merged is True
        assert gh.matching("gh", "merge", "--squash")

    def test_gate_blocks_with_no_reviewer_reviewed_reason(self):
        driver, gh = _gate_driver(
            fleet={"claude"}, reviewed_ever={"claude"},
            rev_parse_seq=["c1"], last_substantive="c1",
            reviews=[_review("claude", "c1", body=RETIREMENT_BANNER)])
        driver._retired.add("claude")
        blocked, reason, head = driver._head_aware_merge_gate(clean_exit=True)
        assert blocked is True
        assert "[no-reviewer-reviewed]" in reason

    def test_a_retired_bots_shaless_anchor_is_not_admitted(self):
        # A sha-less clean signal anchored BEFORE the retirement notice must not
        # sneak the bot past the gate through the _clean_signal_head union. The
        # anchors are union-ed in unconditionally; what drops this one is the
        # _genuine_reviewers() key-filter applied straight after — the single
        # enforcement point every gate site reads.
        driver, gh = _gate_driver(
            fleet={"claude"}, reviewed_ever={"claude"},
            rev_parse_seq=["c1"], last_substantive="c1",
            reviews=[], clean_signal={"claude": "c1"})
        driver._retired.add("claude")
        blocked, reason, head = driver._head_aware_merge_gate(clean_exit=True)
        assert blocked is True

    def test_a_healthy_second_reviewer_still_lets_the_merge_through(self):
        # The guard must not over-block: one dead reviewer does not veto a
        # genuine review by another.
        driver, gh = _gate_driver(
            fleet={"claude", "codex"}, reviewed_ever={"claude", "codex"},
            rev_parse_seq=["c1"], last_substantive="c1",
            reviews=[_review("claude", "c1", body=RETIREMENT_BANNER),
                     _review("codex", "c1", body="please fix the null check")])
        driver._retired.add("claude")
        blocked, reason, head = driver._head_aware_merge_gate(clean_exit=True)
        assert blocked is False, reason

    def test_review_permits_merge_and_handback_reason_agree(self):
        driver, gh = _gate_driver(
            fleet={"claude"}, reviewed_ever={"claude"},
            rev_parse_seq=["c1"], last_substantive="c1", reviews=[])
        assert driver._review_permits_merge() is True   # before the notice
        driver._record_retired("claude")
        assert driver._review_permits_merge() is False  # after it


# ═════════════════════════════════════════════════════════════════════
# 6. Status + scheduling.
# ═════════════════════════════════════════════════════════════════════

class TestRetiredStatus:
    def test_renders_retired_not_active(self):
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        driver._record_retired("claude")
        driver._bot_state("claude").last_seen = 1.0   # it DID post something
        assert driver._skip_key("claude") == "retired"
        assert driver._bot_status_text("claude") == "Retired ⛔"

    def test_without_the_cause_the_same_bot_would_read_active(self):
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        driver._bot_state("claude").last_seen = 1.0
        assert driver._bot_status_text("claude") == "Active ✅"

    @pytest.mark.parametrize("masker", [
        "rate-limited", "excluded", "polish", "silent", "no-change",
    ])
    def test_outranks_every_other_hard_cause(self, masker):
        # It is the only PERMANENT cause, so no recoverable cause may mask it.
        # Each state below independently drives a DIFFERENT _skip_key branch;
        # with the retired branch removed or demoted, one of them would win.
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        driver._record_retired("claude")
        if masker == "rate-limited":
            driver._rate_limited_until["claude"] = 10 ** 9
        elif masker == "excluded":
            driver.store.exclude_quota("claude")
        elif masker == "polish":
            driver.polishing.add("claude")
        elif masker == "silent":
            driver.silent_dropped.add("claude")
        elif masker == "no-change":
            driver.reviewed_no_change.add("claude")
            assert driver._skip_key("claude") == "no-change"  # a REAL review wins
            return
        assert driver._skip_key("claude") == "retired"
        assert driver._bot_status_text("claude") == "Retired ⛔"

    def test_a_genuine_earlier_review_still_outranks_it(self):
        # It sits BELOW the completed-review outcomes: a real review from an
        # EARLIER round is a real review.
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        driver._record_retired("claude")
        driver.approved.add("claude")
        assert driver._skip_key("claude") == "approved"

    def test_not_re_requested(self):
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        driver._record_retired("claude")
        assert "claude" not in driver.expected_bots()

    def test_the_run_summons_a_retired_reviewer_only_once(self):
        timeline = [(0, Comment(id="a", text=RETIREMENT_BANNER, source="claude[bot]"))]
        driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY)
        driver.run()
        assert len(gh.matching("@claude review")) == 1

    def test_the_skip_log_names_the_cause(self):
        assert "permanent retirement" in round_driver._SKIP_LONG["retired"]


# ═════════════════════════════════════════════════════════════════════
# 7. PERMANENT for the run — never retracted.
# ═════════════════════════════════════════════════════════════════════

class TestPermanence:
    def test_a_later_clean_comment_does_not_retract(self):
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        driver._classify_signal(
            Comment(id="a", text=RETIREMENT_BANNER, source="claude[bot]"), now=1.0)
        out = driver._classify_signal(
            Comment(id="b", text="No issues found.", source="claude[bot]"), now=2.0)
        assert out is None
        assert "claude" in driver._retired
        assert driver.store.is_excluded("claude")
        assert "claude" not in driver.reviewed_ever
        assert "claude" not in driver.approved       # never crowned green
        assert driver._bot_state("claude").signal == detectors.SIGNAL_RETIRED

    def test_a_later_genuine_finding_does_not_re_credit_the_bot(self):
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        driver._classify_signal(
            Comment(id="a", text=RETIREMENT_BANNER, source="claude[bot]"), now=1.0)
        out = driver._classify_signal(
            Comment(id="b", text="Fix the null check on line 42.",
                    source="claude[bot]", path="a.py", diff_hunk="@@"), now=2.0)
        assert out is None                       # never flows to the kernel
        assert driver._genuine_reviewers() == set()

    def test_an_earlier_errored_stamp_cannot_relabel_it(self):
        # A bot that ERRORED earlier in the run and then announced its
        # retirement must keep reporting "Retired ⛔". The errored comeback keys
        # off error_created_at alone, so leaving that stamp set let the next
        # review-output comment reset the signal and relabel the row "excluded".
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        st = driver._bot_state("claude")
        st.signal = detectors.SIGNAL_ERRORED
        st.error_created_at = "2026-01-01T00:00:00+00:00"
        driver.store.exclude_errored("claude")
        driver._record_retired("claude")
        assert st.error_created_at is None
        assert driver._skip_key("claude") == "retired"
        assert driver._bot_status_text("claude") == "Retired ⛔"

    def test_the_errored_comeback_can_never_reach_it(self):
        # Retirement rides the PERMANENT tier; errored_comeback only retracts
        # the transient one.
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        driver._record_retired("claude")
        driver.store.errored_comeback("claude")
        assert driver.store.is_excluded("claude")

    def test_a_plus_one_reaction_never_crowns_it_approved(self):
        # A dead reviewer must never be crowned green. _fold_reactions defers to
        # any recorded signal, so a fresh +1 arriving after the notice is ignored
        # rather than folded into approved / reviewed_ever / _clean_signal_head.
        reaction = gh_ingest.Reaction(
            id="r1", content="+1", source="claude[bot]",
            created_at="2030-01-01T00:00:00+00:00")
        driver, clock, gh = make_driver(
            [], cfg=CLAUDE_ONLY, reactions=[(0, reaction)])
        driver._record_retired("claude")
        driver._reaction_baseline = set()      # the +1 reads as FRESH
        assert driver._fold_reactions(now=5.0) is False
        assert "claude" not in driver.approved
        assert "claude" not in driver.done
        assert "claude" not in driver.reviewed_ever
        assert "claude" not in driver._clean_signal_head
        assert driver._genuine_reviewers() == set()

    def test_the_cause_is_run_scoped_state_not_module_state(self):
        # A fresh driver starts clean — nothing leaks between runs.
        a, _, _ = make_driver([], cfg=CLAUDE_ONLY)
        a._record_retired("claude")
        b, _, _ = make_driver([], cfg=CLAUDE_ONLY)
        assert b._retired == set()
        assert not b.store.is_excluded("claude")


# ═════════════════════════════════════════════════════════════════════
# 8. GENERIC BY CONTRACT — no bot or vendor name anywhere.
# ═════════════════════════════════════════════════════════════════════

class TestNoBotNamesHardcoded:
    BOT_NAMES = ("gemini", "copilot", "codex", "claude", "chatgpt", "anthropic",
                 "google", "microsoft", "openai", "code assist")

    def test_detector_patterns_name_no_bot(self):
        blob = " ".join(detectors.RETIRED_PATTERNS).lower()
        for name in self.BOT_NAMES:
            assert name not in blob, f"{name!r} hardcoded in RETIRED_PATTERNS"

    def test_cause_tables_name_no_bot(self):
        blob = " ".join([
            detectors.PR_RETIREMENT_VOCAB_RE.pattern,
            detectors._RETIRED_SCOPED_QUALIFIER_RE.pattern,
            detectors._RETIRED_SELF_SHUTDOWN_RE.pattern,
            detectors._RETIRED_FEEDBACK_MARKER_RE.pattern,
            detectors._RETIRED_DECLINED_PR_RE.pattern,
            " ".join(detectors._CAUSE_SELF_REPORT[detectors.SIGNAL_RETIRED]),
            round_driver._STATUS_SHORT["retired"],
            round_driver._SKIP_LONG["retired"],
        ]).lower()
        for name in self.BOT_NAMES:
            assert name not in blob, f"{name!r} hardcoded in a retired cause table"

    @pytest.mark.parametrize("vendor", [
        "Gemini Code Assist", "Copilot", "Codex", "Claude", "Acme Reviewer",
    ])
    def test_the_detector_is_symmetric_across_vendors(self, vendor):
        # The capability is about the CLAIM, not the speaker: naming any vendor
        # in the notice changes NOTHING in either direction. A detector that
        # special-cased one vendor would break one half of this.
        notice = f"The consumer version of {vendor} on GitHub has been sunset. " \
                 "All code review activity has officially ceased."
        assert detectors.is_retired_message(notice)
        # …and the same vendor named in ordinary FEEDBACK still retires nobody.
        feedback = (f"The {vendor} integration has been sunset upstream, so this "
                    "adapter is dead code. Please delete it.")
        assert not detectors.is_retired_message(feedback)
