"""Broadened clean-review detection: the "no … feedback" and "no <noun> to
<speak-verb>" all-clear families, the boilerplate stripper that feeds the
tier-2 length gate, and the long-footer verdict that only the LLM tier resolves.

A bot's top-level review sometimes approves in wording the original narrow
patterns missed ("There are no review comments to address, and I have no
additional feedback to provide.") and buries that one-line verdict under a long
footer (a vendor-sunset admonition block). The broadening recognises the
verdict; the stripper keeps the footer from gating it out of the LLM tier. The
conservative "ambiguous / in-progress ⇒ NOT clean" bias is preserved throughout.
"""
from buddhi_review import detectors


# A bot's full top-level review body: a one-line approval verdict followed by a
# long "[!IMPORTANT]" vendor-sunset admonition block that pushes the raw length
# well past the tier-2 short limit.
REVIEW_BODY_WITH_FOOTER = (
    "## Code Review\n\n"
    "This pull request removes a duplicate 'Round 2+' definition from the Wins "
    "tooltip footer, making the footer empty by default and only displaying "
    "information about escalated questions when they are present. A "
    "corresponding unit test has been added to verify this behavior. There are "
    "no review comments to address, and I have no additional feedback to "
    "provide.\n\n"
    "> [!IMPORTANT]\n"
    "> The consumer version of this AI review assistant on GitHub is being "
    "sunset. Starting next month, new organization installations will be "
    "blocked, and all review activity will officially cease the month "
    "after. For more details on the timeline and next steps, please review the "
    "linked help documentation.\n"
    "> Existing installations will keep working on a best-effort basis until the "
    "final cutoff date, after which the integration is retired and removed from "
    "the marketplace listing. Thank you for being an early adopter of the tool."
)

# The verdict sentence on its own — what survives _strip_review_boilerplate.
VERDICT_SENTENCE = (
    "There are no review comments to address, and I have no additional "
    "feedback to provide."
)

# A clean approval the regex tier does NOT recognise (no CLEAN_REVIEW_PATTERN
# matches), buried under the same long admonition footer. Because the stripped
# verdict is not deterministically clean, the short-circuit does NOT fire and the
# verdict genuinely reaches the LLM tier — so this fixture exercises the model
# seam (the deterministically-clean fixture above no longer does, by design).
AMBIGUOUS_VERDICT = (
    "I read the whole diff end to end and I'm comfortable shipping it as is; "
    "the change is coherent and the new test pins the behaviour it describes."
)
REVIEW_BODY_AMBIGUOUS_WITH_FOOTER = (
    "## Code Review\n\n"
    + AMBIGUOUS_VERDICT
    + "\n\n"
    "> [!IMPORTANT]\n"
    "> The consumer version of this AI review assistant on GitHub is being "
    "sunset. Starting next month, new organization installations will be "
    "blocked, and all review activity will officially cease the month "
    "after. For more details on the timeline and next steps, please review the "
    "linked help documentation. Existing installations will continue to function "
    "on a best-effort basis until the final cutoff date, after which the "
    "integration will be fully retired and removed from the marketplace listing.\n"
    "> If you rely on this integration, plan your migration to the supported "
    "offering well before the cutoff so your review coverage is uninterrupted."
)


# ---------------------------------------------------------------------------
# "no [further|additional|more|other|new] feedback" — regex tier alone
# ---------------------------------------------------------------------------

class TestNoFeedbackFamily:
    def test_no_additional_feedback(self):
        assert detectors.is_clean_review("I have no additional feedback to provide.")

    def test_no_more_feedback(self):
        assert detectors.is_clean_review("No more feedback from me.")

    def test_no_other_feedback(self):
        assert detectors.is_clean_review("No other feedback.")

    def test_no_new_feedback(self):
        assert detectors.is_clean_review("No new feedback on this revision.")

    def test_bare_no_feedback(self):
        assert detectors.is_clean_review("No feedback. Nice work.")

    def test_no_further_feedback(self):
        assert detectors.is_clean_review("No further feedback.")


# ---------------------------------------------------------------------------
# "no [qualifier] <review-noun> to <speak-verb>" — regex tier alone
# ---------------------------------------------------------------------------

class TestNoNounToSpeakVerb:
    def test_no_review_comments_to_address(self):
        assert detectors.is_clean_review("There are no review comments to address.")

    def test_no_comments_to_raise(self):
        assert detectors.is_clean_review("I have no comments to raise on this PR.")

    def test_no_concerns_to_flag(self):
        assert detectors.is_clean_review("No concerns to flag here.")

    def test_no_nits_to_share(self):
        assert detectors.is_clean_review("No nits to share — clean PR.")

    def test_no_outstanding_issues_to_address(self):
        assert detectors.is_clean_review("No outstanding issues to address.")

    def test_no_remaining_comments_to_make(self):
        assert detectors.is_clean_review("No remaining comments to make.")

    def test_no_findings_to_report(self):
        assert detectors.is_clean_review("No findings to report on this change.")

    def test_non_speak_verb_not_matched(self):
        # The verb set is bounded to "saying" verbs — a work verb ("investigate")
        # implies pending effort, so "no <noun> to <work-verb>" is NOT clean.
        assert not detectors.is_clean_review("No comments to investigate further.")

    def test_suggest_noun_stays_active_conservatively(self):
        # "suggestions" collides with the actionable guard ⇒ conservatively
        # active, never a deterministic clean verdict.
        assert not detectors.is_clean_review("No suggestions to make.")

    def test_verdict_sentence_is_clean_regex_only(self):
        # The exact wording that the original narrow patterns missed, with no
        # footer — caught by the broadened regex alone (no LLM needed).
        assert detectors.is_clean_review(VERDICT_SENTENCE)


# ---------------------------------------------------------------------------
# The broadened patterns must NOT swallow mixed / in-progress verdicts
# ---------------------------------------------------------------------------

class TestBroadenedAdversarial:
    """Each of these must read as ACTIVE (not a voluntary all-clear)."""

    def test_mixed_but_blocked(self):
        # "but …" inside the same sentence trips the look-ahead even though the
        # trailing clause has no _ACTIONABLE_RE keyword.
        assert not detectors.is_clean_review(
            "No comments to address, but the error handling on line 42 needs a "
            "null check.")

    def test_mixed_however_blocked(self):
        assert not detectors.is_clean_review(
            "No additional feedback; however, please rename the variable.")

    def test_mixed_though_blocked(self):
        assert not detectors.is_clean_review(
            "No nits to share, though the naming on line 9 is a bit off.")

    def test_in_progress_yet_blocked(self):
        assert not detectors.is_clean_review(
            "No comments to address yet — still reviewing.")

    def test_in_progress_so_far_blocked(self):
        assert not detectors.is_clean_review(
            "No issues to flag so far; continuing the review.")

    def test_feedback_in_progress_blocked(self):
        assert not detectors.is_clean_review("No feedback yet, still going.")

    def test_mixed_aside_from_blocked(self):
        # Exception markers introduce a real request with no _ACTIONABLE_RE
        # keyword in the trailing clause — the look-ahead must still block.
        assert not detectors.is_clean_review(
            "No nits to share aside from the typo on line 3.")

    def test_mixed_apart_from_blocked(self):
        assert not detectors.is_clean_review(
            "No remaining issues to flag apart from perf.")

    def test_mixed_besides_blocked(self):
        assert not detectors.is_clean_review(
            "No comments to raise besides the naming on line 9.")

    def test_mixed_other_than_blocked(self):
        assert not detectors.is_clean_review(
            "No comments to address other than the rename on line 5.")

    def test_exception_marker_in_next_sentence_is_fine(self):
        # The look-ahead is sentence-scoped: a positive remark in a SEPARATE
        # sentence must not block a genuinely clean verdict.
        assert detectors.is_clean_review(
            "No nits to share. Besides, great work on the refactor.")

    def test_plain_actionable_still_active(self):
        assert not detectors.is_clean_review(
            "Consider adding a test for the empty branch; this could NPE.")

    def test_actionable_after_clean_phrase_blocked_by_after_scan(self):
        # Cross-sentence actionable prose (the intra-sentence look-ahead only
        # scans one sentence) is caught by the actionable-prose after-scan when
        # it uses recognised recommendation vocabulary.
        assert not detectors.is_clean_review(
            "No comments to address. Separately, consider renaming foo.")
        assert not detectors.is_clean_review(
            "No comments to address. Separately, please rename foo.")

    def test_no_false_match_inside_a_word(self):
        # "\bno" must not fire inside "casino" / "piano" etc.
        assert not detectors.is_clean_review("The casino feedback loop is slow.")


# ---------------------------------------------------------------------------
# _strip_review_boilerplate — feeds the tier-2 length gate / classifier
# ---------------------------------------------------------------------------

class TestStripReviewBoilerplate:
    def test_strips_admonition_blockquote(self):
        out = detectors._strip_review_boilerplate(
            "LGTM.\n\n> [!IMPORTANT]\n> This product is being sunset soon.\n"
            "> See the docs.")
        assert "sunset" not in out
        assert out == "LGTM."

    def test_strips_details_footer(self):
        out = detectors._strip_review_boilerplate(
            "No issues found.\n<details><summary>Tip</summary>blah blah</details>")
        assert "blah" not in out
        assert "No issues found." in out

    def test_strips_details_footer_with_attributes(self):
        out = detectors._strip_review_boilerplate(
            "All good.\n<details open class='x'>\nhidden tip\n</details>")
        assert "hidden tip" not in out
        assert "All good." in out

    def test_strips_html_comment(self):
        out = detectors._strip_review_boilerplate(
            "Looks good.<!-- machine-readable tracking id 12345 -->")
        assert "tracking" not in out
        assert "Looks good." in out

    def test_empty_and_none_safe(self):
        assert detectors._strip_review_boilerplate("") == ""
        assert detectors._strip_review_boilerplate(None) == ""

    def test_footer_body_fits_length_gate_after_strip(self):
        # The full body exceeds the short limit; the verdict alone fits.
        assert len(REVIEW_BODY_WITH_FOOTER) > detectors.CLEAN_LLM_SHORT_LIMIT
        stripped = detectors._strip_review_boilerplate(REVIEW_BODY_WITH_FOOTER)
        assert len(stripped) <= detectors.CLEAN_LLM_SHORT_LIMIT
        assert "no review comments to address" in stripped.lower()
        assert "sunset" not in stripped.lower()


# ---------------------------------------------------------------------------
# Long-footer handling: a benign footer does not false-block a clean verdict,
# and an ambiguous verdict still reaches the conservative LLM tier once stripped.
# ---------------------------------------------------------------------------

class TestLongFooterVerdictResolves:
    def test_benign_footer_does_not_block_tier1(self):
        # The footer's "please review the linked docs" is benign (not a
        # recommendation about the code), so the narrow actionable-prose after-
        # scan does NOT block the clean verdict — the body reads clean at tier 1.
        assert detectors.is_clean_review(REVIEW_BODY_WITH_FOOTER)

    def test_clean_stripped_verdict_short_circuits_before_llm(self):
        # The clean verdict is detected with no model round-trip (tier 1 on the
        # full body already suffices now that the benign footer does not block).
        def explode(prompt):
            raise AssertionError("LLM called on a deterministically-clean verdict")

        assert detectors.detect_clean_review(REVIEW_BODY_WITH_FOOTER, llm_json=explode)

    def test_clean_verdict_with_benign_footer_needs_no_llm(self):
        # A clean verdict wrapped in a benign footer is clean with NO LLM seam.
        assert detectors.detect_clean_review(REVIEW_BODY_WITH_FOOTER, llm_json=None)

    def test_ambiguous_verdict_reaches_llm_on_stripped_body(self):
        # A verdict the regex tier misses still reaches the model after the long
        # footer is stripped — the model sees the verdict, not the footer.
        assert len(REVIEW_BODY_AMBIGUOUS_WITH_FOOTER) > detectors.CLEAN_LLM_SHORT_LIMIT
        assert not detectors.is_clean_review(AMBIGUOUS_VERDICT)  # regex tier misses it
        seen = []

        def llm(prompt):
            seen.append(prompt)
            return {"clean": True}

        assert detectors.detect_clean_review(REVIEW_BODY_AMBIGUOUS_WITH_FOOTER, llm_json=llm)
        assert len(seen) == 1
        assert "sunset" not in seen[0]
        assert "comfortable shipping" in seen[0].lower()

    def test_ambiguous_verdict_respects_conservative_llm(self):
        # An unsure / negative model verdict keeps the bot active.
        assert not detectors.detect_clean_review(
            REVIEW_BODY_AMBIGUOUS_WITH_FOOTER, llm_json=lambda p: {"clean": False})

    def test_all_boilerplate_body_never_calls_llm(self):
        # A body that is ENTIRELY inert boilerplate strips to "" — gated out
        # before any model call.
        def explode(prompt):
            raise AssertionError("LLM called on an all-boilerplate body")

        body = "<details><summary>tips</summary>see the changelog</details>"
        assert not detectors.detect_clean_review(body, llm_json=explode)


# ---------------------------------------------------------------------------
# A finding hidden in a collapsed <details>/admonition stays visible (NOT
# stripped) so it can never be silently dropped from clean classification.
# ---------------------------------------------------------------------------

class TestFindingsKeptVisibleToClassification:
    def test_keeps_details_with_should_finding(self):
        out = detectors._strip_review_boilerplate(
            "LGTM.\n<details><summary>Detail</summary>"
            "You should escape user input before rendering.</details>")
        assert "escape user input" in out

    def test_keeps_admonition_with_must_finding(self):
        out = detectors._strip_review_boilerplate(
            "Looks good.\n> [!WARNING]\n"
            "> You must validate the token before trusting it.")
        assert "validate the token" in out

    def test_keeps_details_with_bulleted_findings(self):
        out = detectors._strip_review_boilerplate(
            "No issues found.\n<details><summary>More</summary>\n"
            "- rename foo to bar\n- drop the dead branch\n</details>")
        assert "rename foo" in out

    def test_inert_footer_is_still_stripped(self):
        # The optimization survives: a genuinely non-finding footer is removed.
        out = detectors._strip_review_boilerplate(
            "No issues found.\n<details><summary>Tips</summary>"
            "See the changelog.</details>")
        assert "changelog" not in out
        assert "No issues found." in out

    def test_clean_summary_over_hidden_finding_is_not_clean(self):
        # A clean visible summary cannot promote the bot to done while a
        # recommendation hides in the collapsed block — even if the model would
        # call it clean, the finding (recognised recommendation vocabulary) trips
        # the actionable-prose guard first.
        body = ("LGTM.\n<details><summary>Detail</summary>\n\n"
                "Consider escaping user input before rendering.\n</details>")
        assert not detectors.detect_clean_review(
            body, llm_json=lambda p: {"clean": True})
