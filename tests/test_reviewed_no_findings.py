"""R2 — a GENUINE review with zero findings is a thumbs-up, in display AND
scheduling.

Live case: a reviewer responded with a top-level "overview" review body that
carried no inline findings and no clean sentinel. The round card left its status
at the pending label (implying it had never responded) and later rounds
re-summoned it — re-pinging the cleanest response of all and burning reviewer
credits. Decided semantics (operator, 2026-07-02):

* Display — an explicit clean sentinel and a summary-only genuine review show
  the SAME status cell: "reviewed — no findings". "active" only ever means
  "hasn't responded yet".
* Scheduling — a genuine zero-findings responder is done for the run: excluded
  from re-summon in later rounds exactly like a clean-sentinel bot, and it
  counts as reviewed for the no-reviewer-reviewed merge gate.
* The carve-out — a placeholder ("wasn't able to review", quota, errored) is a
  response, not a review: it never gets the label, never rides the promotion,
  and the placeholder + errored-comeback machinery is untouched.
"""
import io
import json
import subprocess
from contextlib import redirect_stdout

from buddhi_review import detectors
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.fix_apply import FixOutcome
from buddhi_review.loop import Comment
from buddhi_review.round_driver import RoundDriver, RoundTimes
from buddhi_review.seams import ConsoleEscalation

SUMMARY_ONLY = ("## Pull request overview\n"
                "This pull request restores the real CI command and documents "
                "the gate's contract.")
UNABLE = "I wasn't able to review this pull request."


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _Notifier:
    name = "console"

    def startup_log(self):
        pass

    def send(self, ask):
        pass

    def read_answer(self, ask):
        return None

    def clear(self, ask):
        pass


class _Gh:
    """Records every spawned argv so the tests can assert WHO was (re)summoned."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, *, cwd=None, timeout=None):
        self.calls.append(list(argv))
        out = " M x.py\n" if argv[:3] == ["git", "status", "--porcelain"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    def copilot_summons(self):
        return [c for c in self.calls if any("requested_reviewers" in a for a in c)]

    def claude_summons(self):
        return [c for c in self.calls
                if c[:3] == ["gh", "pr", "comment"] and "@claude review" in " ".join(c)]


def _classify(prompt: str) -> str:
    """Route the classification by the fenced comment text: the one real
    finding these tests post is SUBSTANTIVE; every other body (overviews,
    apologies) gets the discard label a real classifier gives non-actionable
    text — the label that makes the promotion decision live or die on the
    carve-out guard, not on classification."""
    if "null check" in prompt:
        return json.dumps({"label": "SUBSTANTIVE", "reason": "real finding"})
    return json.dumps({"label": "INVALID", "reason": "not actionable"})


def _drive(timeline, cfg, **kw):
    clock = _Clock()
    gh = _Gh()

    def fetch(pr, repo=None, cwd=None):
        return [c for t, c in timeline if t <= clock.t]

    adapter = ReviewAdapter(escalation=ConsoleEscalation(notifier=_Notifier()))
    d = RoundDriver(
        "7", repo="o/r", cwd="/nonexistent", cfg=cfg, adapter=adapter,
        classify_runner=_classify,
        fix_dispatch=lambda c, r: FixOutcome(status="applied"),
        fetch=fetch, gh_run=gh, clock=clock, sleep=clock.sleep,
        notice=lambda *a, **k: "",
        times=RoundTimes(quiescence=60, poll_interval=30, min_bot_wait=420,
                         idle_timeout=900, max_wait_total=1800),
        answer_waiter=lambda esc, **k: {}, **kw,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        outcome = d.run()
    return outcome, buf.getvalue(), d, gh


def _table_row(out: str, label: str, round_no: int) -> str:
    """The reviewer's row inside the round-``round_no`` summary table."""
    lines = out.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.startswith(f"Round {round_no}") and "summary" in ln)
    for ln in lines[start:]:
        if ln.startswith("│") and f" {label}" in ln:
            return ln
        if ln.startswith("└"):
            break
    raise AssertionError(f"no {label} row in round {round_no} table:\n{out}")


# ---------------------------------------------------------------------------
# Display + scheduling: summary-only genuine review vs silent vs findings bot
# ---------------------------------------------------------------------------

CFG3 = {"active_reviewers": ["claude", "copilot", "gemini"],
        "auto_on_open": {"claude": False, "copilot": True, "gemini": True}}


def test_summary_only_review_is_done_for_the_run():
    # copilot: top-level review body, zero inline findings → thumbs-up.
    # claude: a real inline finding → fixed, re-summoned next round as today.
    # gemini: genuinely silent → stays the pending label, re-summoned.
    timeline = [
        (0, Comment(id="ov", text=SUMMARY_ONLY, source="copilot-pull-request-reviewer[bot]",
                    path=None, created_at="2026-07-01T00:00:00Z")),
        (0, Comment(id="f1", text="this null check is missing", source="claude[bot]",
                    path="x.py", created_at="2026-07-01T00:00:01Z")),
    ]
    outcome, out, d, gh = _drive(timeline, CFG3, max_rounds=3)

    assert outcome.status == "clean"
    # Scheduling: promoted to voluntarily-done + excluded from later rounds.
    assert "copilot" in d.done
    assert ("[round] → excluding copilot from subsequent rounds this run "
            "(reviewed — no findings)") in out
    assert "skipping copilot: voluntarily done (LGTM)" in out  # round-2 skip log
    # It genuinely reviewed — the no-reviewer-reviewed merge gate is fed.
    assert "copilot" in d.reviewed_ever
    # copilot is auto_on_open and done after round 1 → NEVER (re-)summoned.
    assert gh.copilot_summons() == []
    # claude (findings bot) is re-requested in round 2 exactly as today.
    assert len(gh.claude_summons()) >= 2
    # Display: the round-1 card shows the thumbs-up label, not a pending one.
    row1 = _table_row(out, "Copilot", 1)
    assert "reviewed — no findings" in row1
    assert "active" not in row1
    # The silent bot still reads "active" — pending only ever means "no response".
    assert "active" in _table_row(out, "Gemini", 1)
    # Later rounds keep the label (never falls back to "active" after it reviewed).
    assert "reviewed — no findings" in _table_row(out, "Copilot", 2)


def test_clean_sentinel_shares_the_same_label_and_scheduling():
    cfg = {"active_reviewers": ["claude"], "auto_on_open": {"claude": False}}
    timeline = [(0, Comment(id="ok", text="No issues found.", source="claude[bot]"))]
    outcome, out, d, gh = _drive(timeline, cfg, max_rounds=3)
    assert outcome.status == "clean" and outcome.rounds == 1
    assert "claude" in d.done and "claude" in d.reviewed_ever
    # The clean-sentinel path is untouched (its own log line still fires)…
    assert "[clean-review] claude: nothing to flag — voluntarily done" in out
    # …and its status cell is the SAME label as a summary-only genuine review.
    assert "reviewed — no findings" in _table_row(out, "Claude", 1)
    assert len(gh.claude_summons()) == 1  # the round-1 summon; never re-pinged


# ---------------------------------------------------------------------------
# The carve-out: a placeholder is a response, not a review
# ---------------------------------------------------------------------------

def test_quota_placeholder_keeps_its_own_label_and_never_promotes():
    cfg = {"active_reviewers": ["copilot", "claude"],
           "auto_on_open": {"copilot": True, "claude": False}}
    timeline = [
        (0, Comment(id="q", text="Rate limit exceeded for this model.",
                    source="copilot-pull-request-reviewer[bot]")),
        (0, Comment(id="f1", text="this null check is missing", source="claude[bot]",
                    path="x.py")),
    ]
    outcome, out, d, gh = _drive(timeline, cfg, max_rounds=2)
    assert "copilot" not in d.done
    assert "copilot" not in d.reviewed_ever  # must NOT satisfy the merge gate
    assert "excluding copilot" not in out
    assert "quota" in _table_row(out, "Copilot", 1)
    assert "reviewed — no findings" not in _table_row(out, "Copilot", 1)


def test_uncontracted_apologies_never_promote():
    """Adversarial (verify panel): 'could not review' / 'did not review'
    phrasings — no contraction, no 'able to' — are the same placeholder family
    and must neither promote nor satisfy the thumbs-up label."""
    from buddhi_review.round_driver import _NOT_A_REVIEW_RE
    for text in ("I could not review this pull request.",
                 "Copilot could not review any files in this pull request.",
                 "I did not review this pull request.",
                 "This PR has not been reviewed due to an internal limit."):
        assert _NOT_A_REVIEW_RE.search(text), text
    cfg = {"active_reviewers": ["copilot"], "auto_on_open": {"copilot": True}}
    timeline = [(0, Comment(id="cn", text="I could not review this pull request.",
                            source="copilot-pull-request-reviewer[bot]"))]
    outcome, out, d, gh = _drive(timeline, cfg, max_rounds=3)
    assert "copilot" not in d.done
    assert "reviewed — no findings" not in _table_row(out, "Copilot", 1)
    assert len(gh.copilot_summons()) >= 1  # round 2 still re-summons it


def test_review_first_apologies_never_promote():
    """Adversarial (verify panel, round 2): apologies with review-FIRST word
    order — 'Review skipped.', the zero-files overview line, 'too complex to
    review' — are placeholders too and must never be crowned."""
    from buddhi_review.round_driver import _NOT_A_REVIEW_RE
    for text in ("Review skipped.",
                 "Copilot reviewed 0 out of 12 changed files in this pull request.",
                 "This pull request is too complex to review.",
                 "The review was not performed."):
        assert _NOT_A_REVIEW_RE.search(text), text
    cfg = {"active_reviewers": ["copilot"], "auto_on_open": {"copilot": True}}
    timeline = [(0, Comment(
        id="z0", source="copilot-pull-request-reviewer[bot]",
        text="Copilot reviewed 0 out of 12 changed files in this pull request."))]
    outcome, out, d, gh = _drive(timeline, cfg, max_rounds=3)
    assert "copilot" not in d.done
    assert "reviewed — no findings" not in _table_row(out, "Copilot", 1)
    assert len(gh.copilot_summons()) >= 1


def test_transient_failure_apologies_never_promote():
    """Adversarial (verify panel, round 3): real overload/timeout copy
    apologises about the REQUEST (or names no review at all) — Gemini's
    documented overload message is the live example. None may be crowned."""
    from buddhi_review.round_driver import _NOT_A_REVIEW_RE
    overload = ("Sorry, I'm currently experiencing a high volume of requests "
                "and can't fulfill your request right now. Please try again later!")
    for text in (overload,
                 "Review request timed out before completion.",
                 "Gemini Code Assist is temporarily unavailable. Please try again later.",
                 "The review could not be completed due to a system issue."):
        assert _NOT_A_REVIEW_RE.search(text), text
    cfg = {"active_reviewers": ["gemini"], "auto_on_open": {"gemini": True}}
    timeline = [(0, Comment(id="ov", text=overload,
                            source="gemini-code-assist[bot]"))]
    outcome, out, d, gh = _drive(timeline, cfg, max_rounds=3)
    assert "gemini" not in d.done
    assert "reviewed — no findings" not in _table_row(out, "Gemini", 1)


def test_reversed_and_uncontracted_apology_variants_never_promote():
    """Adversarial (verify panel, round 4): near-variant word orders — reversed
    zero-files, uncontracted be-verbs, 'No review was generated', request-first
    negations — are the same placeholder family. None may be crowned."""
    from buddhi_review.round_driver import _NOT_A_REVIEW_RE
    for text in ("0 out of 12 files reviewed.",
                 "Copilot reviewed no files in this pull request.",
                 "The review was not successful.",
                 "No review was generated for this pull request.",
                 "Review generation stopped before any files were analyzed.",
                 "Your request could not be processed at this time."):
        assert _NOT_A_REVIEW_RE.search(text), text
    cfg = {"active_reviewers": ["copilot"], "auto_on_open": {"copilot": True}}
    timeline = [(0, Comment(id="v", text="The review was not successful.",
                            source="copilot-pull-request-reviewer[bot]"))]
    outcome, out, d, gh = _drive(timeline, cfg, max_rounds=3)
    assert "copilot" not in d.done
    assert "reviewed — no findings" not in _table_row(out, "Copilot", 1)


def test_reviewed_all_files_overview_still_promotes():
    """The positive control for the carve-out guard: a REAL zero-findings
    overview — 'reviewed 12 out of 12 changed files' — must not trip any
    apology pattern; it promotes exactly like the live motivating case."""
    body = ("## Pull request overview\n"
            "Copilot reviewed 12 out of 12 changed files in this pull request "
            "and generated no new remarks.")
    from buddhi_review.round_driver import _NOT_A_REVIEW_RE
    assert not _NOT_A_REVIEW_RE.search(body)
    cfg = {"active_reviewers": ["copilot"], "auto_on_open": {"copilot": True}}
    timeline = [(0, Comment(id="all", text=body,
                            source="copilot-pull-request-reviewer[bot]"))]
    outcome, out, d, gh = _drive(timeline, cfg, max_rounds=3)
    assert "copilot" in d.done
    assert "reviewed — no findings" in _table_row(out, "Copilot", 1)
    assert gh.copilot_summons() == []


def test_unable_to_review_body_is_not_crowned_and_is_resummoned():
    """An apology body that slips past the deliberately-narrow errored regexes
    (no 'error'/'failed' wording) must not ride the promotion: no label, not
    done, and the bot IS re-summoned in the next round."""
    assert detectors.detect_signal(UNABLE) is None  # it really does slip past
    cfg = {"active_reviewers": ["copilot"], "auto_on_open": {"copilot": True}}
    timeline = [(0, Comment(id="na", text=UNABLE,
                            source="copilot-pull-request-reviewer[bot]",
                            path=None))]
    outcome, out, d, gh = _drive(timeline, cfg, max_rounds=3)
    assert "copilot" not in d.done
    assert "excluding copilot" not in out
    assert "reviewed — no findings" not in _table_row(out, "Copilot", 1)
    # Round 2 re-summons it (auto_on_open bots are only POSTed on rounds ≥2, so
    # any requested_reviewers call proves the re-summon fired).
    assert len(gh.copilot_summons()) >= 1


# ---------------------------------------------------------------------------
# Promotion unit surface — inline or work-generating output never promotes
# ---------------------------------------------------------------------------

class _R:
    def __init__(self, label):
        self.classification = type("C", (), {"label": label})()


def _bare():
    return RoundDriver("7", repo="o/r", cfg=CFG3,
                       classify_runner=lambda p: "{}", clean_llm=None)


def test_promotion_requires_topfloor_discard_and_genuine_review():
    d = _bare()
    d.reviewed_ever.add("copilot")
    buf = io.StringIO()
    with redirect_stdout(buf):
        # An INLINE comment — even one judged INVALID — never promotes.
        d._promote_reviewed_no_findings(
            [Comment(id="i", text="x", source="copilot[bot]", path="a.py")],
            [_R("INVALID")])
    assert "copilot" not in d.done

    with redirect_stdout(buf):
        # A top-level body that generated WORK (substantive / escalate) never
        # promotes — the bot must re-review the fix.
        d._promote_reviewed_no_findings(
            [Comment(id="s", text="you should fix X", source="copilot[bot]")],
            [_R("SUBSTANTIVE")])
        d._promote_reviewed_no_findings(
            [Comment(id="b", text="is this intended?", source="copilot[bot]")],
            [_R("BUSINESS_QUESTION")])
    assert "copilot" not in d.done

    with redirect_stdout(buf):
        # Mixed output: one clean summary + one inline finding → not promoted.
        d._promote_reviewed_no_findings(
            [Comment(id="o", text=SUMMARY_ONLY, source="copilot[bot]"),
             Comment(id="i2", text="x", source="copilot[bot]", path="a.py")],
            [_R("INVALID"), _R("OUTDATED")])
    assert "copilot" not in d.done

    with redirect_stdout(buf):
        # The qualifying shape: top-level only, discard labels only.
        d._promote_reviewed_no_findings(
            [Comment(id="o2", text=SUMMARY_ONLY, source="copilot[bot]")],
            [_R("INVALID")])
    assert "copilot" in d.done


def test_promotion_never_fires_without_reviewed_ever():
    # Defensive: a bot absent from reviewed_ever (it never genuinely reviewed)
    # is never promoted even if a discard-labelled top-level comment shows up.
    d = _bare()
    buf = io.StringIO()
    with redirect_stdout(buf):
        d._promote_reviewed_no_findings(
            [Comment(id="o", text=SUMMARY_ONLY, source="copilot[bot]")],
            [_R("INVALID")])
    assert "copilot" not in d.done and buf.getvalue() == ""
