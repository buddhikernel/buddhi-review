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
import json
import re
import subprocess

import pytest

from buddhi_review import detectors, gh_ingest, round_driver
from buddhi_review.loop import Comment

from test_head_aware_merge_gate import _gate_driver, _inline, _review
from test_round_driver import (
    CLAUDE_ONLY, HEAD_SHA, FakeThreads, GhRecorder, make_driver)


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
    # (j) PROSPECTIVE announcements — progressive and future. Every tense above
    # is COMPLETED, so these two commonest banner shapes after "has been sunset"
    # matched nothing at all and were credited as reviews by the merge gate.
    "Our code review service is being retired. No further code reviews will be "
    "provided.",
    "Our review bot will be shut down permanently.",
    "This code review service is being decommissioned.",
    "Our code review service will be sunset.",
    "Our review bot will shut down.",
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
    # The SAME first-person member, now carrying the code/PR object that
    # tightening satisfied — so the object anchor no longer separates a sunset
    # banner from ordinary feedback. It cannot: "we" is the PROJECT under review
    # here, which is how reviewers routinely write. Neither feedback guard fires
    # (no fence, no "this PR", and "restore" is not a recommendation verb), the
    # claim is not deictic so the second pass never arms on an ordinary PR, and
    # the wording is well under the length gate — so the driver dropped the
    # finding AND permanently retired its healthy author. The repo/CI scope
    # qualifier is what separates the two readings. See
    # _RETIRED_SCOPE_VETOED_PATTERNS and TestFirstPersonCessationTakesTheScopeVeto.
    "We have discontinued reviewing pull requests from forks in CI; restore "
    "that coverage.",
    "We have ceased reviewing code in this repository since the workflow "
    "condition changed.",
    "We have discontinued reviewing PRs for this repository; that drops fork "
    "coverage.",
    "In CI, we have discontinued reviewing pull requests.",
    "We have ceased all code review operations in staging.",
    "We have discontinued code review for this repo.",
    # Tightening (b) covered the third-party locus only in the DETERMINER slot,
    # so the same fact stated with the locus in ADVERBIAL position left the
    # deictic "this" free to read as self-reference. Each body below is an
    # inline finding about a vendored integration in the DIFF, carries no
    # feedback-veto marker, and permanently retired its healthy author.
    # See _RETIRED_ELSEWHERE_LOCUS_RE.
    "This code review integration has been retired upstream.",
    "This code review action was sunset upstream, so the workflow step is a "
    "no-op now.",
    "This code review integration has been retired by the vendor.",
    "This code review app was decommissioned in the upstream repo.",
    # The availability member listed the global "all" beside the self
    # determiners, so a quantifier — which names nobody — was read as a
    # self-announcement and the repo-scope veto never ran. See
    # TestTheGlobalAvailabilityMemberTakesTheScopeVeto.
    "All code review support is no longer available in CI because the workflow "
    "condition was removed.",
    "All code review coverage is no longer available on forks.",
    # A shutdown verb that ends at a bare review verb swallows the OBJECT that
    # narrows it, so a coverage complaint about the diff read as a permanent
    # shutdown. See TestANarrowingReviewObjectDefeatsTheShutdownClaim.
    "Our review bot has ceased reviewing dependency updates after the workflow "
    "condition changed.",
    "This code review service has ceased reviewing generated migrations.",
    "Our code review bot has discontinued reviewing the docs directory.",
    # (i) The same narrowing with ANY object. Guard (h) only rejected a trailing
    # REVIEW verb, but a transitive shutdown verb swallows whatever object
    # follows it, and the object is what the cessation is about: each body below
    # says the speaker's service DID SOMETHING TO THE CODE, never that the
    # speaker is dead. None carries a feedback-veto marker, none is deictic (so
    # the second pass never arms on an ordinary PR) and all are well under the
    # length gate. See TestANarrowingObjectOfAnyKindDefeatsTheShutdownClaim.
    "Our code review service has discontinued the legacy endpoint, so the "
    "adapter now returns 404.",
    "Our code review bot has retired support for the v1 API.",
    "Our code review service has discontinued Python 2 support.",
    "Our review bot has terminated the legacy webhook, so the callback never "
    "fires.",
    "Our code review app has shut down the staging queue for this repo.",
    "This code review integration has decommissioned the old ratings table.",
    "Our code review tool has sunset its JSON output format.",
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

    @pytest.mark.parametrize("fence", ["```", "~~~"])
    def test_the_feedback_veto_covers_both_commonmark_fences(self, fence):
        # A fenced code sample is strong "this is a review" evidence, and ``~~~``
        # is a legal fence everywhere ``` is. While only the backtick branch
        # existed, the identical body written with tildes skipped the veto and
        # PERMANENTLY retired the healthy reviewer that wrote it.
        body = (f"Our code review service has been sunset.\n\n"
                f"{fence}python\nx = 1\n{fence}\n")
        assert not detectors.is_retired_message(body)
        assert detectors.detect_signal(body) is None
        assert not detectors.is_placeholder_review_body(body)

    def test_both_fences_are_feedback_markers(self):
        for fence in ("```", "~~~"):
            assert detectors._RETIRED_FEEDBACK_MARKER_RE.search(
                f"{fence}\nx = 1\n{fence}"), fence

    @pytest.mark.parametrize("body", [
        # A CLEANUP IMPERATIVE is a review. The marker list started at the
        # advisory verbs (consider / suggest / recommend), but the commonest way
        # a reviewer asks for code to GO is the bare imperative — and this shape
        # arms nothing else: the anchor is not deictic, so the content gate stays
        # shut, and an ordinary cleanup PR title carries no retirement
        # vocabulary either. So the reviewer was deterministically RETIRED, its
        # finding dropped and its later output ignored for the whole run, with
        # no retraction path.
        "Our review bot has been retired; remove its adapter.",
        "Our review bot has been retired. Delete the vendored client.",
        "Our code review service has been sunset — drop the unused import.",
        "Our code review integration has been discontinued. Rename the helper "
        "to match.",
        "Our code review service has been decommissioned; replace the call "
        "with the new endpoint.",
        "Our review bot has been retired, so extract the shared branch.",
        "Our code review app has been shut down. Inline the one-line wrapper.",
    ])
    def test_a_cleanup_imperative_keeps_its_author(self, body):
        assert detectors._RETIRED_FEEDBACK_MARKER_RE.search(body.lower()), body
        assert not detectors.is_retired_message(body), body
        assert detectors.detect_signal(
            body, quota_llm=_llm(True),
            pr_title="Clean up legacy integration",
            pr_body="Removes the old vendor adapter.") != detectors.SIGNAL_RETIRED

    @pytest.mark.parametrize("body", [
        # …and BASE FORMS only. "removed" / "withdrawn" are members of
        # _RETIRED_WEAK_VERB, so vetoing the participle would blank the
        # weak-verb route the detector must keep catching.
        "All code review activity has been permanently removed.",
        "All code review support has been permanently withdrawn.",
    ])
    def test_the_imperative_veto_spares_the_participle(self, body):
        assert not detectors._RETIRED_FEEDBACK_MARKER_RE.search(body.lower()), body
        assert detectors.is_retired_message(body), body

    @pytest.mark.parametrize("body", [
        # …and the UNINSTALL instruction is exempt. A dead reviewer leaves an
        # installed artifact behind, so the banner tells you to take it out —
        # and the cleanup branch above then blanked the notice, which let
        # is_placeholder_review_body CREDIT the banner's commit sha to the
        # never-merge-unreviewed gate.
        "Our code review service has been permanently retired. "
        "Please remove the GitHub App.",
        "Our code review service has been permanently retired. Please remove "
        "the GitHub App from your organization.",
        "This code review service has been discontinued. Remove the GitHub App "
        "installation.",
        "Our review bot has been retired. Please uninstall the GitHub App and "
        "remove the marketplace listing.",
    ])
    def test_an_uninstall_instruction_does_not_blank_the_notice(self, body):
        # The veto branch itself is NOT loosened: it still matches the raw body.
        assert detectors._RETIRED_FEEDBACK_MARKER_RE.search(body.lower()), body
        assert detectors.is_retired_message(body), body
        # The half the regression actually broke.
        assert detectors.is_placeholder_review_body(body), body

    @pytest.mark.parametrize("body", [
        # The exemption is EARNED, exactly like the declined-PR one: someone
        # ELSE's dead service leaves no retirement claim standing once the
        # uninstall clause is blanked, so it keeps the full veto.
        "The vendor's code review service has been discontinued, so remove the "
        "GitHub App.",
        "Remove the GitHub App; it is unused.",
        # …and the object is an ALLOWLIST of the reviewer's own installation,
        # never a code noun phrase that merely starts with one.
        "Our code review service has been retired. Remove the installation "
        "step from the workflow.",
    ])
    def test_the_uninstall_exemption_is_earned_and_narrow(self, body):
        assert not detectors.is_retired_message(body), body

    def test_the_uninstall_exemption_leaves_the_cleanup_branch_intact(self):
        # The pin the exemption must not spend: a code-shaped cleanup object is
        # a review, and stays vetoed in full.
        assert not detectors.is_retired_message(
            "Our review bot has been retired; remove its adapter.")

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

    def test_a_third_party_locus_beats_the_deictic_this(self):
        # (b), adverbial half. English states the same third-party fact with the
        # locus AFTER the verb, which frees the determiner to be the deictic
        # "this" — and "this" then points at the integration in the DIFF, not at
        # the speaker. Nothing in the SUBJECT distinguishes the two readings, so
        # the locus is what decides.
        feedback = "This code review integration has been retired upstream."
        notice = ("Notice: this code review integration has been retired. "
                  "No further code reviews will be posted.")
        assert not detectors.is_retired_message(feedback)
        assert detectors.detect_signal(feedback) is None
        assert not detectors.is_placeholder_review_body(feedback)
        # …and the near-identical genuine notice is untouched. This pair is the
        # whole point: dropping "this" from _RETIRED_SELF_DET would have taken
        # the notice down with the feedback.
        assert detectors.is_retired_message(notice)

    def test_the_locus_must_qualify_the_matched_predicate(self):
        # Bound to THIS clause, like the repo-scope veto: an "upstream" sitting
        # in another sentence is not the locus of the retirement claim, and
        # vetoing on it would hide a real banner.
        assert detectors.is_retired_message(
            "Our code review service has been discontinued. The SDK it wrapped "
            "lives upstream.")
        # A date is not a locus — a banner routinely stamps itself with one.
        assert detectors.is_retired_message(
            "Our code review service has been shut down as of 2026-07-01.")

    def test_a_fronted_locus_beats_the_deictic_this_too(self):
        # English states the same third-party fact with the locus BEFORE the
        # clause instead of after it — "Upstream, this code review integration
        # has been retired" is word-for-word the same claim as "…has been
        # retired upstream." A suffix-only check misses it and permanently
        # retires the healthy reviewer that posted it as feedback.
        assert not detectors.is_retired_message(
            "Upstream, this code review integration has been retired.")
        assert detectors.detect_signal(
            "Upstream, this code review integration has been retired.") is None

    def test_the_fronted_locus_must_stay_in_the_same_clause(self):
        # A fronted locus in an EARLIER, separate sentence is not the locus of
        # THIS claim, and vetoing on it would hide a real banner — the same
        # boundary the trailing form is already held to.
        assert detectors.is_retired_message(
            "The SDK lives upstream. Our code review service has been "
            "discontinued.")

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


class TestFirstPersonCessationTakesTheScopeVeto:
    """(g) The object anchor above says WHAT stopped, never WHO stopped it, and
    this member's subject anchor is a bare PRONOUN. A reviewer writes "we" for
    the PROJECT under review all the time, so a cessation scoped to the reader's
    own repo / CI is an outage report, never a vendor sunset — the same guard the
    global-quantifier group carries. See _RETIRED_SCOPE_VETOED_PATTERNS."""

    @pytest.mark.parametrize("scoped", [
        "We have discontinued reviewing pull requests from forks in CI; "
        "restore that coverage.",
        "We have discontinued reviewing pull requests from forks in CI.",
        "We have ceased reviewing code in this repository since the workflow "
        "condition changed.",
        "We have discontinued reviewing PRs for this repository.",
        "We have ceased all code review operations in staging.",
        "We have ceased reviewing code in our workflows.",
        "We have discontinued reviewing pull requests on forks.",
        # Fronted adjunct: the qualifier still scopes the cessation.
        "In CI, we have discontinued reviewing pull requests.",
        # `;` is not a sentence break for the veto window.
        "We have discontinued reviewing pull requests; in this repository the "
        "workflow was removed.",
    ])
    def test_a_repo_scoped_first_person_cessation_is_not_a_sunset(self, scoped):
        assert not detectors.is_retired_message(scoped), (
            "a repo/CI-scoped first-person cessation is an outage found in the "
            "diff; retiring on it silences a HEALTHY reviewer for the whole run "
            "with no retraction path AND drops its finding")

    @pytest.mark.parametrize("banner", [
        "We have ceased all code review operations.",
        "We have discontinued reviewing pull requests.",
        "We have ceased reviewing all pull requests.",
        "We have discontinued providing code review.",
        # A platform name is not a scope qualifier — the real observed banner
        # names the platform its dead service ran on.
        "We have ceased all code review operations on GitHub.",
    ])
    def test_an_unscoped_first_person_banner_still_retires(self, banner):
        assert detectors.is_retired_message(banner)

    def test_the_veto_reaches_only_the_first_person_member(self):
        # The subject-noun-phrase members name the speaker's own SERVICE as an
        # entity, so their hit stays conclusive and this stays pinned.
        assert detectors.is_retired_message(
            "Our code review service has been decommissioned in this repository.")

    def test_the_vetoed_set_holds_exactly_the_members_the_list_uses(self):
        # Named constants, so the veto can never be wired to a stale copy of a
        # pattern — and each member keeps its position in the anchored list.
        assert detectors._RETIRED_SCOPE_VETOED_PATTERNS == frozenset({
            detectors._RETIRED_FIRST_PERSON_CESSATION,
            detectors._RETIRED_GLOBAL_NO_LONGER_AVAILABLE,
        })
        assert (detectors._RETIRED_ANCHORED_PATTERNS[1]
                == detectors._RETIRED_FIRST_PERSON_CESSATION)
        assert (detectors._RETIRED_ANCHORED_PATTERNS[4]
                == detectors._RETIRED_GLOBAL_NO_LONGER_AVAILABLE)

    def test_the_reviewer_keeps_its_voice_and_its_finding(self):
        # End to end on an ORDINARY PR: the PR meta does not arm the second pass
        # and the claim is not deictic, so before the veto this classified as
        # SIGNAL_RETIRED with no model call — dropping the finding and retiring a
        # healthy reviewer permanently.
        body = ("We have discontinued reviewing pull requests from forks in CI; "
                "restore that coverage.")
        calls = []

        def counting(prompt):
            calls.append(prompt)
            return {"self_reporting": True}

        assert detectors.detect_signal(
            body, quota_llm=counting,
            pr_title="Add a null check to the parser",
            pr_body="Fixes a crash when the payload is empty.") is None
        assert calls == []
        # And the notice-vs-review split agrees: this body is a REVIEW, so the
        # merge gate must credit it rather than refuse its sha.
        assert not detectors.is_placeholder_review_body(body)


class TestTheGlobalAvailabilityMemberTakesTheScopeVeto:
    """(g), second member. "All <code review> support is no longer available"
    was listed beside the self-determiners as though the quantifier itself were
    a self-announcement signature — the theory guard (f) exists to retract. A
    quantifier says nothing about WHO stopped, so a repo/CI-scoped availability
    report is an outage found in the diff. See
    _RETIRED_GLOBAL_NO_LONGER_AVAILABLE."""

    @pytest.mark.parametrize("scoped", [
        "All code review support is no longer available in CI because the "
        "workflow condition was removed.",
        "All code review support is no longer available for this repository.",
        "All code review coverage is no longer available on forks.",
        "All code review functionality is no longer supported in the pipeline.",
        "All code review activity is no longer available in staging.",
        # Fronted adjunct: a fresh subject intervenes, so it still scopes the
        # availability claim.
        "In this repository, all code review support is no longer available.",
        # `;` is not a sentence break for the veto window.
        "All code review support is no longer available; in this repository "
        "the workflow was removed.",
    ])
    def test_a_repo_scoped_availability_report_is_not_a_sunset(self, scoped):
        assert not detectors.is_retired_message(scoped), (
            "a repo/CI-scoped availability report is an outage found in the "
            "diff; retiring on it silences a HEALTHY reviewer for the whole run "
            "with no retraction path AND drops its finding")

    @pytest.mark.parametrize("banner", [
        "All code review support is no longer available.",
        "All code review functionality is no longer offered.",
        # A platform name is not a scope qualifier.
        "All code review support is no longer available on GitHub.",
    ])
    def test_an_unscoped_global_availability_banner_still_retires(self, banner):
        # The announcement register (_RETIRED_TENSE_FINAL) is deliberately NOT
        # transplanted here: "is no longer available" has no verb slot for a
        # permanence adverb, so requiring one would delete the member outright —
        # and an undetected banner is CREDITED AS A REVIEW by the merge gate.
        assert detectors.is_retired_message(banner)

    @pytest.mark.parametrize("scoped", [
        "Our code review support is no longer available in CI.",
        "This code review service is no longer available for this repository.",
    ])
    def test_the_self_determiner_half_stays_conclusive(self, scoped):
        # The split is by determiner precisely so the veto reaches only the
        # quantifier. "this|our|my" names the speaker's own machinery, so a
        # scope qualifier does not unmake the claim.
        assert detectors.is_retired_message(scoped)

    def test_the_reviewer_keeps_its_voice_and_its_finding(self):
        # End to end on an ORDINARY PR: the PR meta does not arm the second pass
        # and the claim is not deictic, so before the veto this classified as
        # SIGNAL_RETIRED with no model call.
        body = ("All code review support is no longer available in CI because "
                "the workflow condition was removed.")
        calls = []

        def counting(prompt):
            calls.append(prompt)
            return {"self_reporting": True}

        assert detectors.detect_signal(
            body, quota_llm=counting,
            pr_title="Add a null check to the parser",
            pr_body="Fixes a crash when the payload is empty.") is None
        assert calls == []
        assert not detectors.is_placeholder_review_body(body)


class TestANarrowingReviewObjectDefeatsTheShutdownClaim:
    """(h) A shutdown verb that ends at a bare review verb swallows whatever
    object follows, and an object NARROWS the cessation to that object. A
    narrowed cessation reports a coverage defect in the diff, never a permanent
    shutdown. See _RETIRED_NARROWED_REVIEW_GUARD / _RETIRED_UNRESTRICTED_TAIL."""

    @pytest.mark.parametrize("body", [
        "Our review bot has ceased reviewing dependency updates after the "
        "workflow condition changed.",
        "Our review bot has ceased reviewing the vendored SDK files.",
        "This code review service has ceased reviewing generated migrations.",
        "Our code review bot has discontinued reviewing the docs directory.",
        "Our review bot has ceased reviewing anything under the vendor tree.",
        "Our code review integration has stopped reviewing draft pull requests "
        "for the docs folder.",
    ])
    def test_a_narrowed_cessation_never_retires_anybody(self, body):
        assert not detectors.is_retired_message(body), (
            "a cessation narrowed to an object is a COVERAGE complaint about "
            "the diff; retiring on it silences a HEALTHY reviewer for the whole "
            "run with no retraction path")

    @pytest.mark.parametrize("body", [
        # Unrestricted: nothing follows the review verb but a clause end…
        "Our review bot has ceased reviewing.",
        "Our review bot has ceased reviewing; use another tool.",
        # …a finality / temporal adverbial…
        "Our review bot ceased reviewing as of today.",
        "Our review bot has ceased reviewing permanently.",
        # …or an explicit code/PR-wide object.
        "Our review bot has ceased reviewing all pull requests.",
        "This code review service has ceased reviewing pull requests.",
        "Our code review service has ceased all code review.",
    ])
    def test_an_unrestricted_or_pr_wide_cessation_still_retires(self, body):
        assert detectors.is_retired_message(body)

    @pytest.mark.parametrize("body", [
        # The guard only bites when a review verb trails the shutdown verb
        # DIRECTLY. Everything else after it is untouched.
        "This code review service has been discontinued.",
        "Our review bot has been permanently retired; please use another tool.",
        "This code review service has been sunset and will no longer review "
        "pull requests.",
        "Our code review service has been shut down as of 2026-07-01.",
    ])
    def test_the_guard_leaves_every_other_shutdown_shape_alone(self, body):
        assert detectors.is_retired_message(body)

    def test_the_escape_hatch_carries_the_same_guard(self):
        # _RETIRED_SELF_SHUTDOWN_RE substitutes for the global group's missing
        # permanence adverb, so a narrowed claim must not arm it either.
        assert not detectors.is_retired_message(
            "Our review bot has ceased reviewing dependency updates. All code "
            "review activity has ceased.")
        assert detectors.is_retired_message(
            "Our review bot has ceased reviewing. All code review activity "
            "has ceased.")

    def test_the_object_anchor_is_shared_with_the_first_person_member(self):
        # One constant feeds the first-person member, the "will no longer
        # review …" member and both ceased-review orders, so they cannot drift.
        assert (detectors._RETIRED_REVIEW_OBJECT
                in detectors._RETIRED_CEASED_REVIEW)
        assert (detectors._RETIRED_REVIEW_OBJECT
                in detectors._RETIRED_NARROWED_REVIEW_GUARD)
        assert all(detectors._RETIRED_CEASED_REVIEW in p
                   for p in detectors._RETIRED_ANCHORED_PATTERNS[-2:])


class TestANarrowingObjectOfAnyKindDefeatsTheShutdownClaim:
    """(i) The narrowing has nothing to do with the word "review": a transitive
    shutdown verb swallows whatever object follows, so "our code review service
    has discontinued THE LEGACY ENDPOINT" reports a change in the diff, not a
    shutdown. Only an ACTIVE verb phrase can take a direct object, so the tense
    is split by voice and the guard rides the active half alone. See
    _RETIRED_TENSE_NO_OBJECT / _RETIRED_OBJECTLESS_TAIL /
    _RETIRED_SHUTDOWN_PREDICATE."""

    @pytest.mark.parametrize("body", [
        "Our code review service has discontinued the legacy endpoint, so the "
        "adapter now returns 404.",
        "Our code review bot has retired support for the v1 API.",
        "Our code review service has discontinued Python 2 support.",
        "Our review bot has terminated the legacy webhook, so the callback "
        "never fires.",
        "Our code review app has shut down the staging queue for this repo.",
        "This code review integration has decommissioned the old ratings table.",
        "Our code review tool has sunset its JSON output format.",
    ])
    def test_a_transitive_shutdown_verb_never_retires_anybody(self, body):
        assert not detectors.is_retired_message(body), (
            "a shutdown verb narrowed by a direct object describes the DIFF; "
            "retiring on it silences a HEALTHY reviewer for the whole run with "
            "no retraction path")
        assert detectors.detect_signal(body) is None
        assert not detectors.is_placeholder_review_body(body)

    @pytest.mark.parametrize("body", [
        # PASSIVE / copular — no object is grammatically possible, so these are
        # untouched by the guard whatever follows them.
        "This code review service has been discontinued.",
        "Our review bot has been permanently retired; please use another tool.",
        "Our code review service has been shut down as of 2026-07-01.",
        "Our code review app has been sunset following the acquisition.",
        "Our code review service is decommissioned.",
        "Our code review service has been decommissioned in this repository.",
        # ACTIVE but object-LESS: a clause end, an adverbial, a coordination…
        "Our code review service has ceased.",
        "Our code review service has ceased permanently.",
        "Our code review bot has shut down for good.",
        # …a whole-service complement that narrows nothing…
        "Our code review service has ceased operations.",
        "Our code review service has ceased all code review.",
        # …or a trailing review verb, which guard (h) judges on its own terms.
        "Our review bot has ceased reviewing.",
        "Our review bot has ceased reviewing as of today.",
        "Our review bot has ceased reviewing all pull requests.",
    ])
    def test_an_objectless_shutdown_still_retires(self, body):
        assert detectors.is_retired_message(body)

    def test_the_passive_half_of_the_tense_split_takes_no_object_guard(self):
        # The guard rides _RETIRED_TENSE_ACTIVE only. Pinning the split keeps a
        # later widening from silently pushing the passive banner shapes — the
        # overwhelming majority of real notices — through an allowlist they
        # never needed.
        assert detectors._RETIRED_OBJECTLESS_TAIL in (
            detectors._RETIRED_SHUTDOWN_PREDICATE)
        assert detectors._RETIRED_TENSE_ACTIVE in detectors._RETIRED_TENSE
        assert detectors._RETIRED_TENSE_NO_OBJECT in detectors._RETIRED_TENSE

    def test_the_predicate_is_shared_with_the_escape_hatch(self):
        # One constant feeds the self-referential-shutdown member and the global
        # group's escape hatch, so the two can never drift apart.
        assert (detectors._RETIRED_SHUTDOWN_PREDICATE
                in detectors._RETIRED_ANCHORED_PATTERNS[0])
        assert detectors._RETIRED_SHUTDOWN_PREDICATE in (
            detectors._RETIRED_SELF_SHUTDOWN_RE.pattern)
        assert not detectors._RETIRED_SELF_SHUTDOWN_RE.search(
            "our review bot has discontinued the legacy endpoint")
        assert detectors._RETIRED_SELF_SHUTDOWN_RE.search(
            "our review bot has been discontinued")

    def test_a_narrowed_claim_does_not_arm_the_global_escape_hatch(self):
        # The escape hatch substitutes for the global group's missing permanence
        # adverb, so a narrowed claim must not arm it either.
        assert not detectors.is_retired_message(
            "Our review bot has discontinued the legacy endpoint. All code "
            "review activity has ceased.")
        assert detectors.is_retired_message(
            "Our review bot has been discontinued. All code review activity "
            "has ceased.")


class TestProgressiveAndFutureNoticesAreRecognized:
    """(j) Every other tense here is COMPLETED, so a reviewer announcing its own
    shutdown as IN PROGRESS ("is being retired") or as COMING ("will be shut
    down permanently") matched nothing — and an undetected banner is CREDITED AS
    A REVIEW by the never-merge-unreviewed gate, the regression this whole cause
    exists to close. See _RETIRED_TENSE_PROSPECTIVE_NO_OBJECT."""

    @pytest.mark.parametrize("body", [
        # Progressive passive.
        "Our code review service is being retired. No further code reviews "
        "will be provided.",
        "This code review service is being decommissioned.",
        "This code review integration is being permanently retired.",
        # Future passive.
        "Our review bot will be shut down permanently.",
        "Our code review service will be sunset.",
        "Our code review app will soon be decommissioned.",
        # Future active / intransitive.
        "Our review bot will shut down.",
        "Our code review service will cease permanently.",
    ])
    def test_a_prospective_notice_is_detected(self, body):
        assert detectors.is_retired_message(body)
        # …and the merge gate refuses it a commit-sha credit, which is the half
        # the miss was actually costing.
        assert detectors.is_placeholder_review_body(body)

    def test_the_two_reported_shapes_classify_as_retired(self):
        # The exact wordings the review flagged: neither matched `is being` nor
        # `will be`, so is_retired_message, detect_signal and the placeholder
        # check all returned False and the notice could take review credit.
        for body in ("Our code review service is being retired. No further "
                     "code reviews will be provided.",
                     "Our review bot will be shut down permanently"):
            assert detectors.is_retired_message(body), body
            assert detectors.detect_signal(body) == detectors.SIGNAL_RETIRED
            assert detectors.is_placeholder_review_body(body)

    @pytest.mark.parametrize("body", [
        "Our code review service will be retired on August 1.",
        "Our code review service will be retired on 2026-09-01.",
        "Our code review bot will be shut down next month.",
        "Our review bot is being retired in Q4.",
        "Our code review service will be sunset at the end of the quarter.",
        "Our code review app will be decommissioned starting next release.",
        "Our review bot will no longer review pull requests starting next month.",
    ])
    def test_a_scheduled_future_notice_retires_nobody(self, body):
        # A dated prospective claim is a DEPRECATION WARNING from a reviewer
        # that is still alive and still reviewing this PR; retiring on it
        # silences a healthy reviewer for the whole run with no retraction path.
        assert not detectors.is_retired_message(body), body
        assert detectors.detect_signal(body) is None

    @pytest.mark.parametrize("body", [
        # The date FRONTS the clause instead of trailing it — the identical
        # deprecation warning, stated the other way round. A suffix-only check
        # misses it and permanently retires the still-alive reviewer.
        "Effective August 1, our review bot will be shut down permanently.",
        "Starting next month, our code review bot will be shut down.",
        "In Q4, our review bot is being retired.",
    ])
    def test_a_fronted_scheduled_notice_retires_nobody_too(self, body):
        assert not detectors.is_retired_message(body), body
        assert detectors.detect_signal(body) is None

    def test_the_fronted_schedule_must_stay_in_the_same_clause(self):
        # A fronted date in an EARLIER, separate sentence does not date THIS
        # claim, and vetoing on it would hide a real banner — the same
        # boundary the trailing form is already held to.
        assert detectors.is_retired_message(
            "Our release ships on August 1. Our review bot will be shut down "
            "permanently.")

    def test_a_completed_claim_keeps_its_date(self):
        # The veto is keyed on the PROSPECTIVE marker in the matched span, so a
        # completed claim is untouched however it is dated — a banner routinely
        # stamps itself with the day the service died.
        assert detectors.is_retired_message(
            "Our code review service has been shut down as of 2026-07-01.")
        assert detectors.is_retired_message(
            "Our code review service was sunset on August 1.")

    @pytest.mark.parametrize("body", [
        # The future is the register a reviewer writes the DIFF'S OWN
        # consequences in, so the prospective tenses are admitted ONLY where a
        # subject noun phrase already names the speaker's own service — never on
        # the unanchored global-quantifier group.
        "All code review coverage will be permanently removed once the "
        "workflow is deleted.",
        "All code review activity will be permanently disabled.",
        "Every code review service will be shut down.",
        # …nor on a narrowing object, exactly as the completed active tense.
        "Our code review service will sunset the legacy endpoint.",
        "Our review bot will shut down the staging queue.",
        # …nor when the shutdown is somebody else's.
        "This code review service is being retired upstream.",
    ])
    def test_the_widening_stops_at_the_self_anchored_predicate(self, body):
        assert not detectors.is_retired_message(body), body

    @pytest.mark.parametrize("body", [
        # The future is also how a reviewer describes what the DIFF will do at
        # RUNTIME. A banner declares its shutdown outright; a conditional or
        # temporal subordinate clause on the predicate marks the other reading,
        # and each body below would otherwise retire its healthy author.
        "Our code review service will shut down when the runner exits.",
        "Our review bot was being shut down while the fixture ran.",
        "Our code review bot will be sunset if the token is missing.",
        "Our code review service will cease once the queue drains.",
        "Our review bot will be shut down unless the workflow is restored.",
    ])
    def test_a_conditional_prospective_claim_retires_nobody(self, body):
        assert not detectors.is_retired_message(body), body
        assert detectors.detect_signal(body) is None

    def test_the_prospective_tenses_are_not_in_the_shared_tense_constant(self):
        # Pinned: folding them into _RETIRED_TENSE would put the future tense on
        # the global-quantifier group, which has no self-reference anchor.
        assert (detectors._RETIRED_TENSE_PROSPECTIVE_NO_OBJECT
                not in detectors._RETIRED_TENSE)
        assert (detectors._RETIRED_TENSE_PROSPECTIVE_NO_OBJECT
                in detectors._RETIRED_SHUTDOWN_PREDICATE)
        assert (detectors._RETIRED_TENSE_PROSPECTIVE_ACTIVE
                in detectors._RETIRED_SHUTDOWN_PREDICATE)

    def test_the_escape_hatch_carries_the_prospective_tenses_too(self):
        # One predicate constant feeds the anchored member and the global
        # group's escape hatch, so a prospective banner arms both.
        assert detectors._RETIRED_SELF_SHUTDOWN_RE.search(
            "our review bot will be shut down")
        assert detectors.is_retired_message(
            "Our review bot will be shut down. All code review activity has "
            "ceased.")
        assert not detectors.is_retired_message(
            "Our review bot will be shut down next month. All code review "
            "activity has ceased.")

    def test_the_driver_records_a_prospective_notice_end_to_end(self):
        # The whole point of detecting it: the reviewer is recorded as retired
        # and subtracted from the reviewed set, so a prospective banner can
        # never satisfy the never-merge-unreviewed gate.
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        body = "Our review bot will be shut down permanently."
        assert driver._classify_signal(
            Comment(id="a", text=body, source="claude[bot]"), now=1.0) is None
        assert "claude" in driver._retired
        assert driver.store.is_excluded("claude")
        assert "claude" not in driver.reviewed_ever
        assert driver._genuine_reviewers() == set()


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

    @pytest.mark.parametrize("banner", [
        # The banner names the PLATFORM its dead service ran on. Whole-body
        # veto matched "on GitHub Actions" and hid the banner — and a hidden
        # banner is credited as a review by the never-merge-unreviewed gate.
        RETIREMENT_BANNER.replace("on GitHub", "on GitHub Actions"),
        "Our app on GitHub Actions has been sunset and all code review "
        "activity has officially ceased.",
        # An unrelated qualifier in a DIFFERENT sentence of the same banner.
        "All code review activity has officially ceased. Generated in CI.",
    ])
    def test_a_qualifier_outside_the_cessation_clause_does_not_veto(self, banner):
        assert detectors.is_retired_message(banner), (
            "a scope qualifier that does not modify the cessation clause "
            "vetoed a real sunset banner — the notice is then credited as a "
            "review by the reviewed-head merge gate")

    @pytest.mark.parametrize("scoped", [
        # The qualifier sits BETWEEN the global head and its own verb, so it
        # scopes this very cessation — it must still veto even though a
        # retirement predicate follows it directly.
        "All code review activity in CI has been permanently disabled.",
        "All code review activity on forks has been permanently disabled.",
        # Fronted adjunct: a fresh subject intervenes, so it is not a
        # subject-noun-phrase qualifier and still scopes the cessation.
        "In this repository, all code review activity has officially ceased.",
        # `;` is not a sentence break for the veto window.
        "All code review activity has officially ceased; in this repository "
        "the workflow was removed.",
    ])
    def test_a_qualifier_modifying_the_cessation_still_vetoes(self, scoped):
        assert not detectors.is_retired_message(scoped)

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
# 3b. The DEICTIC "this" — the one anchor that does not say WHO.
#
# "This code review integration has been retired in favor of the new App."
# is word-for-word both a shutdown banner and an inline finding about a
# vendored integration in the diff. The adverbial-locus veto separates the
# two only when a third-party locus is stated ("…retired upstream"); with
# none — or with a migration clause in its place — nothing in the wording
# decides, so the claim is handed to the content gate instead of silencing
# a possibly-healthy reviewer on a coin flip.
# ═════════════════════════════════════════════════════════════════════

# The reported false positive: no locus, no feedback-veto marker, short enough
# to clear the length gate, on a PR whose metadata says nothing about
# retirement — so nothing before this guard saves its author.
DEICTIC_FINDING = ("This code review integration has been retired in favor of "
                   "the new GitHub App.")
UNRELATED_PR = {"pr_title": "Add a null check to the parser",
                "pr_body": "Fixes a crash when the payload is empty."}


class TestDeicticClaimsGoThroughTheContentGate:
    def test_the_deictic_finding_is_flagged_as_deictic_only(self):
        assert detectors._retired_claim_is_deictic_only(DEICTIC_FINDING)

    def test_the_gate_keeps_a_healthy_reviewer_that_wrote_it(self):
        # THE FIX. Before it, an unrelated PR title meant no model call at all,
        # so this finding permanently excluded its author and dropped the
        # actionable feedback with no retraction path.
        assert detectors.detect_signal(
            DEICTIC_FINDING, quota_llm=_llm(False), **UNRELATED_PR) is None

    def test_the_gate_still_excludes_when_the_model_reads_it_as_a_banner(self):
        assert detectors.detect_signal(
            DEICTIC_FINDING, quota_llm=_llm(True), **UNRELATED_PR
        ) == detectors.SIGNAL_RETIRED

    def test_a_broken_gate_falls_back_to_the_deterministic_verdict(self):
        # An unreachable / unparseable model is not an answer, so the regex
        # verdict stands — exactly what happens with no quota_llm wired.
        assert detectors.detect_signal(
            DEICTIC_FINDING, quota_llm=_llm(None), **UNRELATED_PR
        ) == detectors.SIGNAL_RETIRED
        assert detectors.detect_signal(DEICTIC_FINDING) == detectors.SIGNAL_RETIRED

    def test_the_merge_gate_still_refuses_the_review_credit(self):
        # The half that must NOT move. is_placeholder_review_body is LLM-free
        # and biased to over-block: whichever way the reference resolves, this
        # body is not a review of the commit it rides, so the never-merge-
        # unreviewed gate keeps refusing its sha. That is why routing the
        # exclusion decision to the model cannot reopen the safety bug.
        assert detectors.is_retired_message(DEICTIC_FINDING)
        assert detectors.is_placeholder_review_body(DEICTIC_FINDING)

    @pytest.mark.parametrize("body", [
        RETIREMENT_BANNER,                                   # global quantifier
        "We have ceased all code review operations on GitHub.",  # first person
        "All automated code review activity has been permanently disabled.",
        # Deictic AND independently anchored — the "our" clause still names the
        # speaker with "this" neutralized, so the verdict never rested on it.
        # (The "our" clause has an ambiguity of its own, settled by the same
        # gate one class down; what is pinned here is that the DEICTIC reason
        # does not arm.)
        "This code review integration has been retired. Our code review service "
        "has been discontinued.",
    ])
    def test_a_corroborated_claim_never_arms_the_gate(self, body):
        # Only the claims that hang on "this" alone pay for a model call on the
        # deictic account; every other anchor names the speaker by itself.
        assert not detectors._retired_claim_is_deictic_only(body), body
        calls = []

        def counting(prompt):
            calls.append(prompt)
            return {"self_reporting": False}

        assert detectors.detect_signal(
            body, quota_llm=counting, **UNRELATED_PR
        ) == detectors.SIGNAL_RETIRED
        assert calls == []

    def test_the_prompt_states_the_reason_it_actually_armed(self):
        # A framing the message does not fit biases the answer. Armed by the
        # deictic reason on an unrelated PR, the prompt must not assert that the
        # PR is about retirement, must name the ambiguity, and must break a tie
        # toward "not self-reporting" — this detector's standing asymmetry.
        prompts = []

        def capture(prompt):
            prompts.append(prompt)
            return {"self_reporting": True}

        detectors.detect_signal(DEICTIC_FINDING, quota_llm=capture,
                                **UNRELATED_PR)
        assert len(prompts) == 1
        prompt = prompts[0]
        assert "whose OWN subject involves" not in prompt
        assert '"this"' in prompt
        assert '{"self_reporting": false}' in prompt

    def test_a_retirement_themed_pr_keeps_the_pr_subject_framing(self):
        # Both reasons can arm at once; the PR-subject framing is still the true
        # one there, and the deictic paragraph rides on top of it.
        prompts = []

        def capture(prompt):
            prompts.append(prompt)
            return {"self_reporting": True}

        detectors.detect_signal(DEICTIC_FINDING, quota_llm=capture,
                                pr_title=THIS_PR_TITLE, pr_body=THIS_PR_BODY)
        assert len(prompts) == 1
        assert "whose OWN subject involves" in prompts[0]
        assert '"this"' in prompts[0]
        assert '{"self_reporting": false}' in prompts[0]

    def test_the_possessive_our_raises_its_own_reason_not_the_deictic_one(self):
        # The two reasons are disjoint: an "our" claim is not deictic, and the
        # gate it arms must be told about "our", not about "this".
        assert not detectors._retired_claim_is_deictic_only(POSSESSIVE_FINDING)
        assert detectors._retired_claim_is_possessive_only(POSSESSIVE_FINDING)

    def test_the_three_recoverable_causes_never_raise_the_deictic_reason(self):
        # Their exclusions recover on their own, and none of them is anchored by
        # a determiner, so the tie-breaker they are told stays "true" (exclude).
        prompts = []

        def capture(prompt):
            prompts.append(prompt)
            return {"self_reporting": True}

        detectors.detect_signal(
            "This pull request is too large to review.", quota_llm=capture,
            pr_title="Handle the too-large-diff placeholder",
            pr_body="The reviewer answers oversized diffs with a placeholder.")
        assert len(prompts) == 1
        assert '{"self_reporting": true}' in prompts[0]


# ═════════════════════════════════════════════════════════════════════
# 3b-ii. The POSSESSIVE "our" — the other anchor that does not say WHO.
#
# "our" was read as conclusive speaker self-reference, but the owner it
# names is whoever is TALKING, and a reviewer talks in the voice of the
# project it is reviewing constantly — the identical weakness the
# first-person "we" member already pays for. On a repository whose own
# product IS a code review service (this one is), an ordinary finding wears
# the banner's exact wording.
#
# The remedy is the deictic one and ONLY that one: the claim is routed
# through the content gate. Nothing deterministic moves — is_retired_message,
# the merge gate that reads it LLM-free, and the migration-guidance exemption
# all keep treating "our" as conclusive.
# ═════════════════════════════════════════════════════════════════════

# The reported false positive: a healthy reviewer's finding about the
# CUSTOMER'S own retired service, with the live defect trailing it. No
# feedback-veto marker, no repo/CI scope qualifier, under the length gate, on a
# PR about the webhook — so nothing before the gate saves its author.
POSSESSIVE_FINDING = (
    "Our code review service has been permanently retired, but the webhook "
    "remains enabled and now returns 404.")
WEBHOOK_PR = {"pr_title": "Fix the webhook 404",
              "pr_body": "The handler still answers after the service went away."}


class TestPossessiveClaimsGoThroughTheContentGate:
    def test_the_possessive_finding_is_flagged_as_possessive_only(self):
        assert detectors._retired_claim_is_possessive_only(POSSESSIVE_FINDING)

    def test_the_gate_keeps_a_healthy_reviewer_that_wrote_it(self):
        # THE FIX. Before it, a PR whose subject is not retirement meant no
        # model call at all, so this finding permanently excluded its author and
        # never reached the fixer, with no retraction path.
        assert detectors.detect_signal(
            POSSESSIVE_FINDING, quota_llm=_llm(False), **WEBHOOK_PR) is None

    def test_the_gate_still_excludes_when_the_model_reads_it_as_a_banner(self):
        assert detectors.detect_signal(
            POSSESSIVE_FINDING, quota_llm=_llm(True), **WEBHOOK_PR
        ) == detectors.SIGNAL_RETIRED

    def test_a_broken_gate_falls_back_to_the_deterministic_verdict(self):
        assert detectors.detect_signal(
            POSSESSIVE_FINDING, quota_llm=_llm(None), **WEBHOOK_PR
        ) == detectors.SIGNAL_RETIRED
        assert detectors.detect_signal(POSSESSIVE_FINDING) == detectors.SIGNAL_RETIRED

    def test_the_merge_gate_still_refuses_the_review_credit(self):
        # The half that must NOT move, exactly as for the deictic claim.
        assert detectors.is_retired_message(POSSESSIVE_FINDING)
        assert detectors.is_placeholder_review_body(POSSESSIVE_FINDING)

    def test_the_prompt_names_the_determiner_actually_in_play(self):
        # A framing the message does not fit biases the answer: "it says
        # 'this'" is simply false of an "our" claim, so the paragraph is
        # determiner-specific and the tie still breaks toward the healthy
        # reviewer.
        prompts = []

        def capture(prompt):
            prompts.append(prompt)
            return {"self_reporting": True}

        detectors.detect_signal(POSSESSIVE_FINDING, quota_llm=capture,
                                **WEBHOOK_PR)
        assert len(prompts) == 1
        assert '"our"' in prompts[0]
        assert 'it says "this"' not in prompts[0]
        assert '{"self_reporting": false}' in prompts[0]

    @pytest.mark.parametrize("body", [
        # "my" is left conclusive — a reviewer never writes it in the project's
        # voice — and so is every anchor that names the speaker without a
        # determiner at all.
        "My code review service has been permanently retired.",
        "We have ceased all code review operations on GitHub.",
        RETIREMENT_BANNER,
    ])
    def test_the_other_anchors_still_keep_the_deterministic_verdict(self, body):
        calls = []

        def counting(prompt):
            calls.append(prompt)
            return {"self_reporting": False}

        assert not detectors._retired_claim_is_possessive_only(body), body
        assert detectors.detect_signal(
            body, quota_llm=counting, **WEBHOOK_PR) == detectors.SIGNAL_RETIRED
        assert calls == []

    def test_migration_guidance_is_still_exempt_from_the_feedback_guard(self):
        # The pin the fix must not spend. A genuine "our" banner routinely
        # carries migration guidance, which _REVIEW_FEEDBACK_RE matches — so the
        # possessive reason arms the GATE only and never withdraws that
        # exemption. With no model wired the notice classifies exactly as before.
        assert detectors._REVIEW_FEEDBACK_RE.search(NOTICE_WITH_MIGRATION_PROSE)
        assert detectors.detect_signal(
            NOTICE_WITH_MIGRATION_PROSE) == detectors.SIGNAL_RETIRED


# ═════════════════════════════════════════════════════════════════════
# 3c. The generic review-feedback guard vs. MIGRATION GUIDANCE.
#
# A real shutdown banner tells you where to go next — "You should migrate
# to X", or a bulleted list of steps. _REVIEW_FEEDBACK_RE matches the bare
# "should" and the bullet, so it short-circuited detect_signal ahead of the
# retirement branch: the PERMANENT cause was silently downgraded to
# actionable feedback and the dead reviewer was credited as having reviewed.
# A SELF-ANCHORED notice is now exempt from that guard; a deictic-"this"
# claim still keeps it.
# ═════════════════════════════════════════════════════════════════════

# Self-anchored ("our"), plus the one clause that used to sink it.
NOTICE_WITH_MIGRATION_PROSE = (
    "Our code review service has been permanently retired. "
    "You should migrate to the new GitHub App.")
NOTICE_WITH_MIGRATION_BULLETS = (
    "Our code review service has been permanently retired.\n\n"
    "- Migrate to the new GitHub App.\n"
    "- Update your workflow triggers.")


class TestMigrationGuidanceDoesNotDowngradeANotice:
    @pytest.mark.parametrize("body", [NOTICE_WITH_MIGRATION_PROSE,
                                      NOTICE_WITH_MIGRATION_BULLETS])
    def test_the_notice_classifies_as_retired(self, body):
        # The regression: the guard fired on "should" / the bullet and returned
        # None, so the driver never recorded the retirement.
        assert detectors._REVIEW_FEEDBACK_RE.search(body)   # the guard DOES match
        assert detectors.is_retired_message(body)
        assert detectors.detect_signal(body) == detectors.SIGNAL_RETIRED

    @pytest.mark.parametrize("body", [NOTICE_WITH_MIGRATION_PROSE,
                                      NOTICE_WITH_MIGRATION_BULLETS])
    def test_the_exemption_does_not_bypass_the_content_gate(self, body):
        # Exempt from the guard, still subject to the per-cause gate: on a PR
        # about reviewer sunsetting the model gets the final word.
        assert detectors.detect_signal(
            body, quota_llm=_llm(False),
            pr_title=THIS_PR_TITLE, pr_body=THIS_PR_BODY) is None

    @pytest.mark.parametrize("body", [NOTICE_WITH_MIGRATION_PROSE,
                                      NOTICE_WITH_MIGRATION_BULLETS])
    def test_the_driver_records_the_retirement_end_to_end(self, body):
        # The half the bug actually broke: with the notice read as ordinary
        # feedback, _classify_signal credited the dead reviewer in
        # reviewed_ever, where it satisfied the never-merge-unreviewed gate.
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        assert driver._classify_signal(
            Comment(id="a", text=body, source="claude[bot]"), now=1.0) is None
        assert "claude" in driver._retired
        assert driver.store.is_excluded("claude")
        assert "claude" not in driver.reviewed_ever
        assert driver._genuine_reviewers() == set()

    def test_a_deictic_claim_still_keeps_the_guard(self):
        # The safety half. "this" does not say WHO, and the content gate that
        # settles it needs a model — so with none wired the guard must still
        # save the healthy reviewer whose bulleted review wrote this.
        # The bullet says "migrate off", not "drop": a cleanup imperative is
        # itself a feedback marker (_RETIRED_FEEDBACK_MARKER_RE), which would
        # settle the body one layer earlier and leave the guard untested here.
        body = DEICTIC_FINDING + "\n\n- You should migrate off the vendored client."
        assert detectors.is_retired_message(body)
        assert detectors._retired_claim_is_deictic_only(body)
        assert detectors.detect_signal(body) is None
        assert detectors.detect_signal(body, quota_llm=_llm(True),
                                       **UNRELATED_PR) is None

    def test_the_guard_still_vetoes_the_recoverable_causes(self):
        # The exemption is RETIRED-only: the three recoverable causes keep
        # deferring to the guard, so a finding that merely discusses a rate
        # limit / size cap / review failure is never a status signal.
        for body in ("- Consider handling the rate limit (429) here.",
                     "- This diff should be split; it is too large to review.",
                     "- The review run failed check should be handled here."):
            assert detectors._REVIEW_FEEDBACK_RE.search(body), body
            assert detectors.detect_signal(body) is None, body

    def test_a_bulleted_review_borrowing_the_vocabulary_stays_a_review(self):
        # is_retired_message's own veto (broader than the guard) is what screens
        # these, and it still runs first in the exemption test.
        for body in ("Our code review bot has been retired.\n"
                     "- fix the import on line 3",
                     "The vendor's code review service has been discontinued.\n"
                     "- delete the adapter"):
            assert not detectors.is_retired_message(body), body
            assert detectors.detect_signal(body) is None, body


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

    def test_an_inline_finding_about_a_vendored_integration_keeps_its_author(self):
        # The end-to-end shape of the deictic-"this" false positive: an ordinary
        # inline finding on a PR whose metadata carries no retirement vocabulary,
        # so no model disambiguation arms. Before the locus veto _classify_signal
        # recorded RETIRED, dropped the finding, and removed the healthy reviewer
        # AND its review credit for the whole run, with no retraction path.
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        out = driver._classify_signal(
            Comment(id="a",
                    text="This code review integration has been retired upstream.",
                    source="claude[bot]", path="adapters/vendor.py",
                    diff_hunk="@@ -1,2 +1,2 @@\n-old\n+new"),
            now=1.0)
        assert out == "claude"                   # the finding still flows to the kernel
        assert "claude" not in driver._retired
        assert not driver.store.is_excluded("claude")
        assert "claude" in driver.reviewed_ever

    def test_the_same_wording_as_a_real_notice_still_retires(self):
        # The guard is a locus test, not a blanket amnesty for "this": the
        # notice shape it must never stop catching.
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        assert driver._classify_signal(
            Comment(id="a", text="This code review service has been discontinued.",
                    source="claude[bot]"), now=1.0) is None
        assert "claude" in driver._retired

    def test_a_locusless_deictic_finding_keeps_its_author_through_the_gate(self):
        # The same end-to-end shape as the test above, for the deictic claim no
        # locus rule can settle: the driver fetches the (unrelated) PR meta,
        # detect_signal arms the content gate on the ambiguity alone, and the
        # model reads the message as a review of the diff. The finding reaches
        # the kernel and its author stays live and credited.
        pr_json = json.dumps({"title": "Add a null check to the parser",
                              "body": "Fixes a crash when the payload is empty."})

        class UnrelatedPrGh(GhRecorder):
            def __call__(self, argv, *, cwd=None, timeout=None):
                if "title,body" in " ".join(argv):
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=pr_json, stderr="")
                return super().__call__(argv, cwd=cwd, timeout=timeout)

        driver, clock, gh = make_driver(
            [], cfg=CLAUDE_ONLY, gh=UnrelatedPrGh(),
            quota_llm=lambda prompt: {"self_reporting": False})
        out = driver._classify_signal(
            Comment(id="a", text=DEICTIC_FINDING, source="claude[bot]",
                    path="adapters/vendor.py",
                    diff_hunk="@@ -1,2 +1,2 @@\n-old\n+new"),
            now=1.0)
        assert out == "claude"                   # the finding still flows on
        assert "claude" not in driver._retired
        assert not driver.store.is_excluded("claude")
        assert "claude" in driver.reviewed_ever

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

    def test_a_resolved_notice_outlives_an_rr_active_restore(self):
        """THE RESTART HOLE. Under ``--rr-active`` the restore runs BEFORE the
        preflight snapshot: a newer clean sign-off crowns the bot
        (``st.signal = CLEAN``, approved, done), and the older retirement notice
        sits on a RESOLVED thread — the one comment shape whose ONLY reader is
        ``_fold_hard_signal``. Applying that method's "a cause is already
        recorded" guard to retirement as well meant the notice was never
        classified: the dead reviewer kept its crown, was folded into
        ``reviewed_ever``, and satisfied the never-merge-unreviewed gate alone.

        A live run reaches the opposite state — the notice is the OLDER message,
        so ``_classify_signal`` records it first and the later clean comment
        cannot retract it (:class:`TestPermanence`). The restart must agree."""
        threads = FakeThreads().thread("T1", root_comment_id="a", is_resolved=True)
        timeline = [
            (0, Comment(id="a", text=RETIREMENT_BANNER, source="claude[bot]",
                        path="x.py", diff_hunk="@@ -1 +1 @@",
                        created_at="2026-01-01T00:00:00+00:00")),
            (0, Comment(id="b", text="No issues found.", source="claude[bot]",
                        created_at="2026-01-02T00:00:00+00:00")),
        ]
        driver, clock, gh = make_driver(
            timeline, cfg=CLAUDE_ONLY, auto_merge=True, rr_active=True,
            preflight=True, threads_fetch=threads.fetch,
            resolve_thread=threads.resolve, answer_waiter=lambda esc, **k: {})
        outcome = driver.run()
        assert "claude" in driver._retired
        assert driver.store.is_excluded("claude")
        assert "claude" not in driver.approved        # the restored crown is revoked
        assert "claude" not in driver.reviewed_ever
        assert driver._genuine_reviewers() == set()
        assert outcome.merged is False, (
            "a retired reviewer's restored sign-off satisfied the "
            "never-merge-unreviewed gate — the PR merged unreviewed")
        assert gh.matching("gh", "merge", "--squash") == []

    def test_the_same_restart_harness_merges_for_a_healthy_reviewer(self):
        # THE CONTROL for the restart hole above. Same restart, same resolved
        # thread, same sign-off — only the resolved comment's body differs, so a
        # block above cannot be an artifact of --rr-active or of the thread gate.
        threads = FakeThreads().thread("T1", root_comment_id="a", is_resolved=True)
        timeline = [
            (0, Comment(id="a", text="rename tmp for clarity", source="claude[bot]",
                        path="x.py", diff_hunk="@@ -1 +1 @@",
                        created_at="2026-01-01T00:00:00+00:00")),
            (0, Comment(id="b", text="No issues found.", source="claude[bot]",
                        created_at="2026-01-02T00:00:00+00:00")),
        ]
        driver, clock, gh = make_driver(
            timeline, cfg=CLAUDE_ONLY, auto_merge=True, rr_active=True,
            preflight=True, threads_fetch=threads.fetch,
            resolve_thread=threads.resolve, answer_waiter=lambda esc, **k: {})
        outcome = driver.run()
        assert "claude" not in driver._retired
        assert outcome.merged is True
        assert gh.matching("gh", "merge", "--squash")

    def test_the_recorded_cause_carve_out_is_retirement_ONLY(self):
        # The already-recorded guard still protects every RECOVERABLE cause: a
        # bot excluded for one of them is never re-labelled by a resolved comment
        # carrying another. Only retirement — permanent, and otherwise unreadable
        # on the restart path — may overwrite what is on record.
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        st = driver._bot_state("claude")
        st.signal = detectors.SIGNAL_ERRORED
        driver._fold_hard_signal(
            Comment(id="a", text="You have exceeded your monthly quota of requests.",
                    source="claude[bot]", path="x.py", diff_hunk="@@"))
        assert st.signal == detectors.SIGNAL_ERRORED     # not re-labelled quota
        assert not driver.store.is_excluded("claude")

    def test_a_recorded_cause_still_short_circuits_before_classification(
            self, monkeypatch):
        # The carve-out must not make every resolved comment on an
        # already-decided bot pay for the full (model-gated) classification: an
        # ordinary body returns on the cheap deterministic pre-check, and an
        # already-retired bot has nothing left to redo.
        calls = []
        monkeypatch.setattr(round_driver.detectors, "detect_signal",
                            lambda *a, **k: calls.append(a) or None)
        driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
        driver._bot_state("claude").signal = detectors.SIGNAL_CLEAN
        driver._fold_hard_signal(
            Comment(id="a", text="rename tmp for clarity", source="claude[bot]",
                    path="x.py", diff_hunk="@@"))
        driver._record_retired("claude")
        driver._fold_hard_signal(
            Comment(id="b", text=RETIREMENT_BANNER, source="claude[bot]",
                    path="x.py", diff_hunk="@@"))
        assert calls == []


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
