"""Quiescence round-loop + exclusion wiring — fake clock, no network."""
import json
import subprocess

from buddhi_review import round_driver
from buddhi_review.actuators import FixDispatch
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.fix_apply import FixOutcome
from buddhi_review.loop import Comment
from buddhi_review.round_driver import RoundDriver, RoundTimes
from buddhi_review.seams import ConsoleEscalation


class FakeClock:
    def __init__(self):
        self.t = 0.0
    def __call__(self):
        return self.t
    def sleep(self, s):
        self.t += s


class FakeNotifier:
    name = "console"
    def __init__(self):
        self.sent = []
    def startup_log(self):
        pass
    def send(self, ask):
        self.sent.append(ask)
    def read_answer(self, ask):
        return None
    def clear(self, ask):
        pass


class GhRecorder:
    """Records every gh/git/test spawn; answers `git status` with a dirty tree."""
    def __init__(self):
        self.calls = []
    def __call__(self, argv, *, cwd=None, timeout=None):
        self.calls.append(list(argv))
        out = " M x.py\n" if argv[:3] == ["git", "status", "--porcelain"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
    def matching(self, *needles):
        return [c for c in self.calls if all(any(n in a for a in c) for n in needles)]


def label_runner(label):
    return lambda prompt: json.dumps({"label": label, "reason": "t"})


def make_driver(timeline, *, cfg, clock=None, gh=None, times=None, classify=None,
                fix=None, answer_waiter=None, **kw):
    """timeline = [(t_visible, Comment), ...] — comments appear at clock time."""
    clock = clock or FakeClock()
    gh = gh or GhRecorder()
    def fetch(pr, repo=None, cwd=None):
        return [c for t, c in timeline if t <= clock.t]
    adapter = ReviewAdapter(escalation=ConsoleEscalation(notifier=FakeNotifier()))
    driver = RoundDriver(
        "7", repo="o/r", cwd="/nonexistent", cfg=cfg, adapter=adapter,
        classify_runner=classify or label_runner("INVALID"),
        fix_dispatch=fix,
        fetch=fetch, gh_run=gh, clock=clock, sleep=clock.sleep,
        notice=lambda *a, **k: "",
        # register_delay=0 by default so the existing timing tests isolate the
        # quiescence / gating behaviour from the post-summon register delay; the
        # dedicated register-delay tests below set it explicitly.
        times=times or RoundTimes(quiescence=60, poll_interval=30,
                                  min_bot_wait=420, idle_timeout=900,
                                  max_wait_total=1800, register_delay=0),
        answer_waiter=answer_waiter,
        **kw,
    )
    return driver, clock, gh


CLAUDE_ONLY = {"active_reviewers": ["claude"], "auto_on_open": {"claude": False}}


# ---------------------------------------------------------------------------
# Quiescence semantics
# ---------------------------------------------------------------------------

def test_silent_unseen_bot_holds_round_then_is_dropped():
    # A never-seen expected bot does NOT self-quiesce — it holds the round open
    # until the idle-timeout (after MIN_BOT_WAIT) closes it. Having then been
    # silent a full round, it is dropped from re-request for the rest of the run.
    cfg = {"active_reviewers": ["copilot"], "auto_on_open": {"copilot": True}}
    driver, clock, gh = make_driver([], cfg=cfg)
    outcome = driver.run()
    assert outcome.status == "clean"           # nothing actionable ever arrived
    assert clock.t >= 900                       # held open to the idle timeout, not 420
    assert "copilot" in driver.silent_dropped   # silent a full round → dropped
    assert gh.matching("requested_reviewers") == []  # auto_on_open: no round-1 summon


def test_quiescence_timer_resets_on_burst():
    timeline = [
        (0, Comment(id="a", text="prose comment one", source="claude[bot]")),
        (50, Comment(id="b", text="prose comment two", source="claude[bot]")),
    ]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY)
    driver.run()
    # polls at 0,30,60…: the second comment lands in the t=60 poll, resetting the
    # window — the bot is silent-done at t=120, NOT t=60.
    assert clock.t >= 120


def test_clean_review_is_immediate_quiescence_and_excludes_from_rerequest():
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY, auto_merge=True)
    outcome = driver.run()
    assert outcome.status == "clean" and outcome.rounds == 1
    assert outcome.merged is True
    assert gh.matching("gh", "merge", "--squash")
    assert "claude" in driver.done
    assert clock.t < 60  # definitive signal — no silence window needed


def test_issue_channel_chatter_is_not_actionable():
    """A non-sentinel top-level comment from a recognized bot (issues channel)
    must NOT flow to the fixer — findings come inline per claude-code-review.yml.
    The identical text on an INLINE comment stays actionable."""
    driver, clock, _ = make_driver([], cfg=CLAUDE_ONLY)
    chatter = Comment(id="x", text="I'm reviewing this PR now, looks interesting.",
                      source="claude[bot]", from_issue_channel=True)
    inline = Comment(id="y", text="I'm reviewing this PR now, looks interesting.",
                     source="claude[bot]", from_issue_channel=False)
    assert driver._classify_signal(chatter, clock.t) is None      # suppressed
    assert driver._classify_signal(inline, clock.t) == "claude"   # actionable


def test_issue_channel_sentinel_and_signals_still_fire():
    """Suppressing chatter must NOT cost the clean sentinel: the sentinel arrives
    on the issue channel and must still flip the bot to voluntarily-done."""
    driver, clock, _ = make_driver([], cfg=CLAUDE_ONLY)
    sentinel = Comment(id="s", text="No issues found.", source="claude[bot]",
                       from_issue_channel=True)
    assert driver._classify_signal(sentinel, clock.t) is None
    assert "claude" in driver.done


def test_max_wait_total_ceiling():
    # a bot that bursts forever: a fresh comment every 50s keeps both the
    # quiescence window and the idle timer alive — only the max-wait ceiling closes
    # the round (max_rounds=1 isolates round 1's wait). The comments are INVALID, so
    # the round yields no substantive progress and the run exits clean.
    timeline = [
        (i * 50, Comment(id=f"c{i}", text=f"more prose {i}", source="claude[bot]"))
        for i in range(60)
    ]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY, max_rounds=1)
    outcome = driver.run()
    assert outcome.status == "clean"          # INVALID-only round → clean finish
    assert 1800 <= clock.t < 1900             # closed by the max-wait ceiling


def test_idle_timeout_closes_a_stuck_round():
    times = RoundTimes(quiescence=600, poll_interval=30, min_bot_wait=60,
                       idle_timeout=120, max_wait_total=1800)
    timeline = [(0, Comment(id="a", text="prose", source="claude[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY, times=times)
    driver.run()
    # quiescence (600) can't be reached before idle (120) fires.
    assert 120 <= clock.t < 600


# ---------------------------------------------------------------------------
# Round-1 summon + re-request (auto_on_open, --rr, --rr-active)
# ---------------------------------------------------------------------------

def test_round1_summons_only_non_auto_on_open():
    cfg = {"active_reviewers": ["copilot", "claude"],
           "auto_on_open": {"copilot": True, "claude": False}}
    timeline = [
        (0, Comment(id="a", text="No issues found.", source="claude[bot]")),
        (0, Comment(id="b", text="No issues found.", source="copilot[bot]")),
    ]
    driver, clock, gh = make_driver(timeline, cfg=cfg)
    driver.run()
    assert gh.matching("@claude review")           # summoned (auto_on_open false)
    assert gh.matching("requested_reviewers") == []  # copilot NOT re-summoned in round 1


def test_rr_widens_round1_to_everyone():
    cfg = {"active_reviewers": ["copilot", "claude"],
           "auto_on_open": {"copilot": True, "claude": False}}
    timeline = [
        (0, Comment(id="a", text="No issues found.", source="claude[bot]")),
        (0, Comment(id="b", text="No issues found.", source="copilot[bot]")),
    ]
    driver, clock, gh = make_driver(timeline, cfg=cfg, rr=True)
    driver.run()
    assert gh.matching("requested_reviewers")  # copilot re-requested too


def test_rr_active_exits_clean_when_nothing_active():
    driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY, rr_active=True)
    driver.store.exclude_quota("claude")
    outcome = driver.run()
    assert outcome.status == "clean" and outcome.rounds == 0
    assert gh.matching("@claude review") == []  # never summoned


def test_rr_active_rerequests_active_bots_in_round1():
    # --rr-active must actually re-request the still-active reviewers in round 1
    # (its whole point on an existing PR) — including auto_on_open:true bots that
    # would otherwise NOT be summoned in round 1.
    cfg = {"active_reviewers": ["copilot"], "auto_on_open": {"copilot": True}}
    timeline = [(0, Comment(id="a", text="No issues found.", source="copilot[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=cfg, rr_active=True)
    driver.run()
    assert gh.matching("requested_reviewers")  # copilot WAS re-requested


# ---------------------------------------------------------------------------
# Exclusion wiring + the strictly-newer errored comeback
# ---------------------------------------------------------------------------

def test_quota_signal_excludes_permanently():
    timeline = [(0, Comment(id="a", text="Rate limit exceeded for this model.",
                            source="claude[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY)
    outcome = driver.run()
    assert driver.store.is_excluded("claude")
    assert outcome.status == "clean"  # nothing actionable; the excluded bot doesn't gate
    # excluded → never re-requested again (only the single round-1 summon)
    assert len(gh.matching("@claude review")) == 1


def test_pr_too_large_signal_excludes_permanently():
    timeline = [(0, Comment(id="a", text="This pull request is too large to review.",
                            source="claude[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY)
    driver.run()
    assert driver.store.is_excluded("claude")


def test_quota_second_pass_wires_pr_context_through_classify_signal():
    """_classify_signal fetches pr_title/pr_body from gh pr view and passes them
    to detect_signal so a reviewer summarizing a quota-themed PR is not excluded."""
    pr_json = json.dumps({"title": "Add monthly quota / rate-limit handling",
                          "body": "Implements the daily limit reset and quota accounting."})
    quota_echo = ("This PR adds monthly quota limit handling; the quota is reset "
                  "each cycle and the usage limit is enforced per account.")

    class QuotaPrGh(GhRecorder):
        def __call__(self, argv, *, cwd=None, timeout=None):
            if "title,body" in " ".join(argv):
                return subprocess.CompletedProcess(argv, 0, stdout=pr_json, stderr="")
            return super().__call__(argv, cwd=cwd, timeout=timeout)

    gh = QuotaPrGh()
    # quota_llm says the bot is describing the PR's own content (not self-reporting)
    driver, clock, _ = make_driver([], cfg=CLAUDE_ONLY, gh=gh,
                                   quota_llm=lambda p: {"self_reporting": False})
    comment = Comment(id="x", text=quota_echo, source="claude[bot]")
    driver._classify_signal(comment, clock.t)
    assert not driver.store.is_excluded("claude"), (
        "reviewer echoing quota vocab on a quota-themed PR must not be excluded"
    )


# A comeback is only observable while the loop is still polling, so these
# timelines carry a second, silent bot (copilot) whose MIN_BOT_WAIT keeps the
# round open past the comeback comment — the comeback lands during later polls
# of a still-active round.
TWO_BOTS = {"active_reviewers": ["copilot", "claude"],
            "auto_on_open": {"copilot": True, "claude": False}}


def test_errored_comeback_only_on_substantive_strictly_newer():
    # comeback fires on a SUBSTANTIVE comment strictly newer than the error.
    timeline = [
        (0, Comment(id="a", text="I encountered an internal error while reviewing.",
                    source="claude[bot]", created_at="2026-06-10T00:00:00Z")),
        (30, Comment(id="b", text="this null check is missing",
                     source="claude[bot]", created_at="2026-06-10T00:05:00Z")),
    ]
    driver, clock, gh = make_driver(timeline, cfg=TWO_BOTS,
                                    classify=label_runner("SUBSTANTIVE"))
    driver.run()
    assert not driver.store.is_excluded("claude")  # came back


def test_errored_comeback_on_cosmetic_newer_comment():
    # A newer COSMETIC comment IS proof of recovery — a cosmetic finding proves
    # the bot produced review output, exactly as a substantive one does.
    timeline = [
        (0, Comment(id="a", text="I encountered an internal error while reviewing.",
                    source="claude[bot]", created_at="2026-06-10T00:00:00Z")),
        (30, Comment(id="b", text="nit: trailing whitespace here",
                     source="claude[bot]", created_at="2026-06-10T00:05:00Z")),
    ]
    driver, clock, gh = make_driver(timeline, cfg=TWO_BOTS,
                                    classify=label_runner("COSMETIC"))
    driver.run()
    assert not driver.store.is_excluded("claude")  # came back


def test_errored_no_comeback_on_non_review_output_newer_comment():
    # A newer OUTDATED/INVALID comment is NOT proof of recovery — only review
    # output (SUBSTANTIVE / COSMETIC) retracts.
    for label in ("INVALID", "OUTDATED"):
        timeline = [
            (0, Comment(id="a", text="I encountered an internal error while reviewing.",
                        source="claude[bot]", created_at="2026-06-10T00:00:00Z")),
            (30, Comment(id="b", text="some newer remark",
                         source="claude[bot]", created_at="2026-06-10T00:05:00Z")),
        ]
        driver, clock, gh = make_driver(timeline, cfg=TWO_BOTS,
                                        classify=label_runner(label))
        driver.run()
        assert driver.store.is_excluded("claude"), f"label={label}"


def test_errored_comeback_on_equal_stamp_same_review():
    # An EQUAL stamp retracts: a review submission stamps its body and inline
    # comments with the same created_at, so equal-stamp review output from the
    # same bot is same-review evidence — the review that carried the false
    # error signal also carried real output.
    timeline = [
        (0, Comment(id="a", text="I encountered an internal error while reviewing.",
                    source="claude[bot]", created_at="2026-06-10T00:00:00Z")),
        (30, Comment(id="b", text="this null check is missing",
                     source="claude[bot]", created_at="2026-06-10T00:00:00Z")),
    ]
    driver, clock, gh = make_driver(timeline, cfg=TWO_BOTS,
                                    classify=label_runner("SUBSTANTIVE"))
    driver.run()
    assert not driver.store.is_excluded("claude")  # equal stamp → came back


def test_errored_no_comeback_on_older_missing_or_unparseable_timestamp():
    # Strictly-older / missing / unparseable → stay excluded, conservatively
    # (an old comment posted BEFORE the error proves nothing about recovery).
    for second_stamp in ("2026-06-09T23:59:59Z", None, "N/A", "yesterday"):
        timeline = [
            (0, Comment(id="a", text="I encountered an internal error while reviewing.",
                        source="claude[bot]", created_at="2026-06-10T00:00:00Z")),
            (30, Comment(id="b", text="this null check is missing",
                         source="claude[bot]", created_at=second_stamp)),
        ]
        driver, clock, gh = make_driver(timeline, cfg=TWO_BOTS,
                                        classify=label_runner("SUBSTANTIVE"))
        driver.run()
        assert driver.store.is_excluded("claude"), f"stamp={second_stamp!r}"


# --- record-time completed-review check: a body that IS review output must
# never be recorded as an errored placeholder in the first place ------------
# These call _classify_signal directly (not via run()) so the assertions pin
# the RECORD-TIME decision itself — an end-to-end run would let the errored
# comeback retract a wrongly-recorded signal and mask a record-time bug.


def test_inline_finding_never_records_errored():
    # An INLINE comment (path set) whose text trips the errored regex is a
    # finding about the reviewed code, not a placeholder — it must flow to the
    # kernel as actionable, and the bot must stay un-excluded.
    driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
    c = Comment(id="a",
                text="The review process encountered an error here when the "
                     "token is stale — handle that case.",
                source="claude[bot]", created_at="2026-06-10T00:00:00Z",
                path="src/auth.py")
    assert driver._classify_signal(c, 0.0) == "claude"   # actionable
    assert not driver.store.is_excluded("claude")
    assert driver._bot_state("claude").error_created_at is None


def test_review_body_with_same_submission_findings_never_records_errored():
    # A review body that trips the errored regex but lands in the SAME fetch
    # batch as a SAME-INSTANT inline finding from the same bot is a completed
    # review's summary — the submission demonstrably produced output, so no
    # errored signal is recorded.
    driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
    body = Comment(id="a", text="Failed to generate a review? No — see the "
                                "inline notes.",
                   source="claude[bot]", created_at="2026-06-10T00:00:00Z")
    stamps = {"claude": ["2026-06-10T00:00:00Z"]}
    assert driver._classify_signal(body, 0.0, batch_finding_stamps=stamps) == "claude"
    assert not driver.store.is_excluded("claude")


def test_stale_batch_findings_never_shield_a_new_placeholder():
    # Round 1's first poll ingests the PR's WHOLE history, so a days-old
    # inline finding rides the same batch as a genuinely new placeholder —
    # it proves nothing about the new failure and must not shield it.
    driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
    ph = Comment(id="a", text="I encountered an internal error while reviewing.",
                 source="claude[bot]", created_at="2026-06-10T00:00:00Z")
    stamps = {"claude": ["2026-06-08T00:00:00Z"]}  # two days older
    assert driver._classify_signal(ph, 0.0, batch_finding_stamps=stamps) is None
    assert driver.store.is_excluded("claude")


def test_other_bots_findings_never_shield_a_placeholder():
    # The shield is per-bot: bot A's inline finding says nothing about bot B.
    driver, clock, gh = make_driver([], cfg=TWO_BOTS)
    ph = Comment(id="a", text="I encountered an internal error while reviewing.",
                 source="claude[bot]", created_at="2026-06-10T00:00:00Z")
    stamps = {"copilot": ["2026-06-10T00:00:00Z"]}
    assert driver._classify_signal(ph, 0.0, batch_finding_stamps=stamps) is None
    assert driver.store.is_excluded("claude")


def test_failure_placeholder_with_zero_output_phrasing_records_errored():
    # "The review run failed; no comments were posted." — the zero-output
    # sentence is the FAILURE's own consequence, not an all-clear. Clean-review
    # phrasing inside an errored-matching body must NOT shield the signal: a
    # failed reviewer crowned "Approved" would satisfy the never-merge-
    # unreviewed gate and auto-merge a PR nobody reviewed.
    for text in (
        "The review run failed; no comments were posted.",
        "Review run failed — no comments were generated.",
    ):
        timeline = [(0, Comment(id="a", text=text, source="claude[bot]",
                                created_at="2026-06-10T00:00:00Z"))]
        driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY)
        driver.run()
        assert driver.store.is_excluded("claude"), text
        assert "claude" not in driver.done, text
        assert "claude" not in driver.approved, text
        assert "claude" not in driver.reviewed_ever, text


def test_real_error_placeholder_still_records_errored():
    # A genuine placeholder (no findings, no inline path) still records the
    # errored exclusion — the record-time check narrows what COUNTS as
    # errored, it does not disable the detector.
    timeline = [
        (0, Comment(id="a", text="I encountered an internal error while reviewing.",
                    source="claude[bot]", created_at="2026-06-10T00:00:00Z")),
    ]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY)
    driver.run()
    assert driver.store.is_excluded("claude")


def test_strictly_newer_helper():
    from buddhi_review.round_driver import _strictly_newer
    assert _strictly_newer("2026-06-10T00:05:00Z", "2026-06-10T00:00:00Z")
    assert not _strictly_newer("2026-06-10T00:00:00Z", "2026-06-10T00:00:00Z")  # equal
    assert not _strictly_newer(None, "2026-06-10T00:00:00Z")                    # missing
    assert not _strictly_newer("N/A", "2026-06-10T00:00:00Z")                   # unparseable
    # mixed offsets must compare by instant, not lexicographically
    assert not _strictly_newer("2026-06-10T00:00:00+00:00", "2026-06-10T00:00:00Z")


def test_same_instant_helper():
    from buddhi_review.round_driver import _same_instant
    assert _same_instant("2026-06-10T00:00:00Z", "2026-06-10T00:00:00Z")
    # mixed offsets compare by instant, not lexicographically
    assert _same_instant("2026-06-10T00:00:00+00:00", "2026-06-10T00:00:00Z")
    assert not _same_instant("2026-06-10T00:05:00Z", "2026-06-10T00:00:00Z")
    assert not _same_instant(None, "2026-06-10T00:00:00Z")   # missing
    assert not _same_instant("N/A", "2026-06-10T00:00:00Z")  # unparseable
    # a naive stamp never equals an aware one (conservative: stays excluded)
    assert not _same_instant("2026-06-10T00:00:00", "2026-06-10T00:00:00Z")


def test_human_comments_never_drive_bot_state():
    timeline = [(0, Comment(id="a", text="Rate limit exceeded", source="human-dev"))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY)
    outcome = driver.run()
    assert not driver.store.is_excluded("claude")
    assert outcome.status == "clean" and outcome.actions == []


# ---------------------------------------------------------------------------
# The full round flow: fix → push → re-request → clean
# ---------------------------------------------------------------------------

def test_fix_flow_pushes_then_next_round_goes_clean():
    # Round 1 fixes a substantive comment; round 2's "No issues found." lands
    # within round 2's MIN_BOT_WAIT window (round 1 closes ~t=60) and MUST be
    # consumed — the round-scoped silence timer means round 2 cannot instant-
    # quiesce on round 1's stale last_seen.
    timeline = [
        (0, Comment(id="a", text="this null check is missing", source="claude[bot]")),
        (90, Comment(id="b", text="No issues found.", source="claude[bot]")),
    ]
    fix: FixDispatch = lambda c, r: FixOutcome(status="applied")
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        auto_merge=True, answer_waiter=lambda esc, **k: {},
    )
    outcome = driver.run()
    assert outcome.status == "clean" and outcome.rounds == 2
    assert [a.final for a in outcome.actions] == ["fixed"]
    assert gh.matching("git", "push")                    # round-1 fixes pushed
    assert len(gh.matching("@claude review")) == 2       # summon + round-2 re-request
    assert gh.matching("gh", "merge", "--squash")        # clean exit merged
    # the round-2 review was ACTUALLY consumed (not a vacuous instant-quiesce)
    assert "b" in driver.processed_ids
    assert "claude" in driver.done


def test_round2_holds_open_for_rerequested_bot_no_instant_quiesce():
    # Regression: round 1's last_seen must NOT leak into round 2. A bot that
    # contributed in round 1 but is silent in round 2 is never-seen THAT round, so
    # it holds the round open to the idle-timeout (after MIN_BOT_WAIT) — never
    # declared done on its first round-2 poll off a stale round-1 last_seen.
    timeline = [
        (0, Comment(id="a", text="this null check is missing", source="claude[bot]")),
    ]
    fix: FixDispatch = lambda c, r: FixOutcome(status="applied")
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        max_rounds=2, answer_waiter=lambda esc, **k: {},
    )
    outcome = driver.run()
    # round 1 closes ~t=60; round 2 then holds the silent re-requested bot open to
    # the idle-timeout before clean-exit — so total ≫ 60.
    assert clock.t >= 60 + 900
    assert outcome.rounds == 2


def test_unanswered_escalation_hands_over():
    timeline = [(0, Comment(id="a", text="should we drop this column?",
                            source="claude[bot]"))]
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("BUSINESS_QUESTION"),
        answer_waiter=lambda esc, **k: {"a": None},
    )
    outcome = driver.run()
    assert outcome.status == "needs-human"
    assert not gh.matching("gh", "merge")


def test_stop_answer_on_failed_fix_stops_the_run():
    timeline = [(0, Comment(id="a", text="this null check is missing",
                            source="claude[bot]"))]
    fix: FixDispatch = lambda c, r: FixOutcome(status="transient-failed", detail="x")
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        answer_waiter=lambda esc, **k: {"fix-a": "3"},
    )
    outcome = driver.run()
    assert outcome.status == "stopped"


# ---------------------------------------------------------------------------
# Poisoned-worktree gate (Option C): a rollback that could not be proven clean
# halts the round BEFORE the push, regardless of disposition.
# ---------------------------------------------------------------------------

def test_reject_rollback_failure_halts_before_push():
    # A fix-verify REJECT whose rollback could not be proven clean poisons the
    # shared worktree. Its disposition is "rejected" (→ skipped-invalid, NO
    # escalation), so the escalation gate never fires for it — yet the round MUST
    # still halt before the push, or the un-rolled-back, explicitly-refused residue
    # would ride the sibling's repo-wide `git add -A` onto the PR.
    timeline = [
        (0, Comment(id="a", text="this null check is missing", source="claude[bot]")),
        (0, Comment(id="b", text="rename this helper", source="claude[bot]")),
    ]
    def fix(c, r):
        if c.id == "a":
            return FixOutcome(status="rejected", rollback_failed=True, detail="refused")
        return FixOutcome(status="applied")  # the sibling fix that would trigger a push
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        auto_merge=True, answer_waiter=lambda esc, **k: {},
    )
    outcome = driver.run()
    assert outcome.status == "needs-human"
    assert not gh.matching("git", "push")     # poisoned worktree never pushed
    assert not gh.matching("gh", "merge")     # and never merged


def test_reject_clean_rollback_does_not_false_halt():
    # Same shape but the rollback SUCCEEDED (rollback_failed=False): the gate must
    # NOT fire, so the sibling's applied fix pushes and the run proceeds normally.
    timeline = [
        (0, Comment(id="a", text="this null check is missing", source="claude[bot]")),
        (0, Comment(id="b", text="rename this helper", source="claude[bot]")),
        (90, Comment(id="c", text="No issues found.", source="claude[bot]")),
    ]
    def fix(c, r):
        if c.id == "a":
            return FixOutcome(status="rejected", rollback_failed=False, detail="refused cleanly")
        return FixOutcome(status="applied")
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        auto_merge=True, answer_waiter=lambda esc, **k: {},
    )
    outcome = driver.run()
    assert outcome.status != "needs-human"    # the gate did NOT false-halt
    assert gh.matching("git", "push")         # clean rollback → sibling fix pushed


def test_transient_rollback_failure_escalates_then_gate_still_halts():
    # transient-failed already escalates; if the operator answers "apply manually"
    # (not stop, not unanswered), the escalation gate falls through — but a rollback
    # that ALSO failed must still halt at the poisoned-worktree gate before a push.
    timeline = [(0, Comment(id="a", text="this null check is missing",
                            source="claude[bot]"))]
    fix: FixDispatch = lambda c, r: FixOutcome(
        status="transient-failed", rollback_failed=True, detail="x")
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        answer_waiter=lambda esc, **k: {"fix-a": "1"},   # "apply manually" → proceed
    )
    outcome = driver.run()
    assert outcome.status == "needs-human"
    assert not gh.matching("git", "push")


def test_push_error_hands_over_without_merging():
    # A failed git push must not fall through to a squash_merge.  The loop
    # should return "needs-human" immediately so a stale remote can't be merged.
    timeline = [(0, Comment(id="a", text="missing null check", source="claude[bot]"))]
    fix: FixDispatch = lambda c, r: FixOutcome(status="applied")

    class PushFailGh(GhRecorder):
        def __call__(self, argv, *, cwd=None, timeout=None):
            rc = 1 if argv[:2] == ["git", "push"] else 0
            out = " M x.py\n" if argv[:3] == ["git", "status", "--porcelain"] else ""
            return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="push rejected")

    gh = PushFailGh()
    driver, _, _ = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        gh=gh, auto_merge=True, answer_waiter=lambda esc, **k: {},
    )
    outcome = driver.run()
    assert outcome.status == "needs-human"
    assert not gh.matching("gh", "merge")


def test_max_rounds_clean_final_round_routes_to_merge():
    # Every budgeted round lands a substantive fix and pushes cleanly; the budget
    # is spent with the final round clean (no escalation / poison / push failure).
    # The exhaustion exit routes through the normal clean-exit gates, so with
    # auto-merge on and a genuine review it MERGES — not an unconditional hand-back.
    timeline = [
        (i * 10, Comment(id=f"c{i}", text=f"missing check {i}", source="claude[bot]"))
        for i in range(2000)
    ]
    fix: FixDispatch = lambda c, r: FixOutcome(status="applied")
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        auto_merge=True, max_rounds=2, answer_waiter=lambda esc, **k: {},
    )
    outcome = driver.run()
    assert outcome.status == "clean" and outcome.rounds == 2
    assert outcome.merged is True
    assert gh.matching("gh", "merge", "--squash")


def test_max_rounds_final_round_unanswered_escalation_hands_back():
    # A final budgeted round with an UNRESOLVED problem must still hand back
    # unmerged: round 1 lands a substantive fix (→ another round), and the last
    # round raises a business question that goes unanswered — the escalation gate
    # returns needs-human BEFORE the exhaustion routing, so the run never merges.
    def classify(prompt):
        if "should we" in prompt:
            return json.dumps({"label": "BUSINESS_QUESTION", "reason": "t"})
        return json.dumps({"label": "SUBSTANTIVE", "reason": "t"})
    timeline = [
        (0, Comment(id="a", text="this null check is missing", source="claude[bot]")),
        (90, Comment(id="b", text="should we drop this column?", source="claude[bot]")),
    ]
    fix: FixDispatch = lambda c, r: FixOutcome(status="applied")
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=classify, fix=fix,
        auto_merge=True, max_rounds=2, answer_waiter=lambda esc, **k: {"b": None},
    )
    outcome = driver.run()
    assert outcome.status == "needs-human"
    assert not gh.matching("gh", "merge")


# ---------------------------------------------------------------------------
# Reviewer auth-failure signal (F9): the realistic Claude 401 posts ZERO comments
# while the GitHub job concludes, so the loop observes it via a CHECK-RUN probe
# (not comments) at run end — a SILENT claude whose "Claude Code Review" run log
# carries the token-invalid signature gets the LOUD re-mint banner instead of the
# generic "remove it from your fleet" silent banner.
# ---------------------------------------------------------------------------

_CLAUDE_FAILED_CHECK = json.dumps([{
    "name": "Claude Code Review", "workflow": "Claude Code Review",
    "link": "https://github.com/o/r/actions/runs/123/job/9",
    "bucket": "fail", "state": "FAILURE",
}])
# The post-step's own ``::error`` line, which survives show_full_output:false.
_AUTH_LOG = (
    "review\tFail the check on a Claude authentication error\n"
    "review\t::error title=Claude review auth failed::CLAUDE_CODE_OAUTH_TOKEN is "
    "invalid or expired — the Claude review returned 401 (Invalid bearer token) "
    "and posted nothing while the job stayed green.\n"
)
_NONAUTH_LOG = (
    "review\tRun anthropics/claude-code-action@v1\n"
    "review\tError: Could not fetch an OIDC token from the GitHub provider.\n"
)
# A SUCCEEDED run (clean SDK result, ``"is_error": false``) whose reviewed diff
# quoted the 401 signature — a review OF auth code, not a real token failure.
_CLEAN_RESULT_LOG = (
    "review\tRun anthropics/claude-code-action@v1\n"
    'review\t{"type":"result","is_error":false} — the review confirmed the code '
    "returns 401 (Invalid bearer token) on a bad token.\n"
)


class AuthProbeGh(GhRecorder):
    """A gh runner that answers the auth-failure probe's `gh pr checks` /
    `gh run view` calls; everything else behaves like GhRecorder."""
    def __init__(self, *, checks_json="[]", run_log=""):
        super().__init__()
        self.checks_json = checks_json
        self.run_log = run_log
    def __call__(self, argv, *, cwd=None, timeout=None):
        self.calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "checks"]:
            return subprocess.CompletedProcess(argv, 0, stdout=self.checks_json, stderr="")
        if argv[:3] == ["gh", "run", "view"]:
            return subprocess.CompletedProcess(argv, 0, stdout=self.run_log, stderr="")
        out = " M x.py\n" if argv[:3] == ["git", "status", "--porcelain"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


def test_silent_claude_auth_failure_emits_remint_banner(capsys):
    gh = AuthProbeGh(checks_json=_CLAUDE_FAILED_CHECK, run_log=_AUTH_LOG)
    driver, clock, _ = make_driver([], cfg=CLAUDE_ONLY, gh=gh)
    driver.run()
    out = capsys.readouterr().out
    assert "REVIEWER AUTH FAILED" in out          # the re-mint banner fired ...
    assert "/review-pr setup" in out              # ... with the re-run-setup directive
    assert "CLAUDE_CODE_OAUTH_TOKEN" in out
    assert "REVIEWER SILENT" not in out           # NOT the generic "remove it" banner


def test_silent_claude_clean_run_emits_generic_silent_banner(capsys):
    # No failed Claude check / no auth signature → the silence is "not installed",
    # so the generic banner fires, NOT the re-mint banner.
    gh = AuthProbeGh(checks_json="[]", run_log="")
    driver, clock, _ = make_driver([], cfg=CLAUDE_ONLY, gh=gh)
    driver.run()
    out = capsys.readouterr().out
    assert "REVIEWER SILENT" in out
    assert "REVIEWER AUTH FAILED" not in out


def test_silent_claude_non_auth_failure_emits_generic_banner(capsys):
    # The Claude check failed, but for a NON-auth reason (OIDC) — the log carries
    # no token-invalid signature, so this must NOT misfire the re-mint banner.
    gh = AuthProbeGh(checks_json=_CLAUDE_FAILED_CHECK, run_log=_NONAUTH_LOG)
    driver, clock, _ = make_driver([], cfg=CLAUDE_ONLY, gh=gh)
    driver.run()
    out = capsys.readouterr().out
    assert "REVIEWER AUTH FAILED" not in out
    assert "REVIEWER SILENT" in out


def test_silent_claude_clean_result_log_is_not_an_auth_failure(capsys):
    # The run SUCCEEDED (``"is_error": false`` in the log) but the reviewed diff
    # quoted the 401 signature. The clean-result guard must short-circuit so this
    # takes the generic silent path, NOT the re-mint banner (which would tell the
    # user to re-mint a working token).
    gh = AuthProbeGh(checks_json=_CLAUDE_FAILED_CHECK, run_log=_CLEAN_RESULT_LOG)
    driver, clock, _ = make_driver([], cfg=CLAUDE_ONLY, gh=gh)
    assert driver._detect_auth_failure("claude") is False
    driver.run()
    out = capsys.readouterr().out
    assert "REVIEWER AUTH FAILED" not in out
    assert "REVIEWER SILENT" in out


def test_auth_probe_is_claude_only_and_does_not_probe_others(capsys):
    # A silent non-claude reviewer (GitHub-App auth, not CLAUDE_CODE_OAUTH_TOKEN)
    # must take the generic path with NO check-run probe and NO re-mint banner.
    cfg = {"active_reviewers": ["copilot"], "auto_on_open": {"copilot": True}}
    gh = AuthProbeGh(checks_json=_CLAUDE_FAILED_CHECK, run_log=_AUTH_LOG)
    driver, clock, _ = make_driver([], cfg=cfg, gh=gh)
    driver.run()
    out = capsys.readouterr().out
    assert "REVIEWER AUTH FAILED" not in out
    assert "REVIEWER SILENT" in out
    assert gh.matching("gh", "pr", "checks") == []   # never probed for a non-claude bot


def test_detect_auth_failure_never_raises_on_gh_error():
    # A gh/network explosion in the probe must degrade to False (generic banner),
    # never crash the run-end warning.
    class BoomGh(GhRecorder):
        def __call__(self, argv, *, cwd=None, timeout=None):
            self.calls.append(list(argv))
            if argv[:2] == ["gh", "pr"] and "checks" in argv:
                raise OSError("network down")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    driver, clock, _ = make_driver([], cfg=CLAUDE_ONLY, gh=BoomGh())
    assert driver._detect_auth_failure("claude") is False


def test_pr_checks_rows_returns_rows_on_nonzero_exit(capsys):
    # `gh pr checks` exits non-zero when checks are pending or failing — exactly
    # the case when a Claude review 401'd.  Returncode must NOT suppress the rows;
    # the auth-failure probe relies on them to find the failed run id.
    class FailingChecksGh(AuthProbeGh):
        def __call__(self, argv, *, cwd=None, timeout=None):
            result = super().__call__(argv, cwd=cwd, timeout=timeout)
            if argv[:3] == ["gh", "pr", "checks"]:
                return subprocess.CompletedProcess(argv, 1, stdout=result.stdout, stderr="")
            return result

    gh = FailingChecksGh(checks_json=_CLAUDE_FAILED_CHECK, run_log=_AUTH_LOG)
    driver, clock, _ = make_driver([], cfg=CLAUDE_ONLY, gh=gh)
    rows = driver._pr_checks_rows()
    assert len(rows) > 0, "rows must not be dropped when gh pr checks exits non-zero"
    assert driver._detect_auth_failure("claude") is True


def test_silent_claude_auth_failure_emits_remint_banner_even_on_nonzero_checks_exit(capsys):
    # End-to-end: when gh pr checks exits non-zero (failing check) but still
    # prints JSON rows, the re-mint banner must fire — not the generic silent banner.
    class FailingChecksGh(AuthProbeGh):
        def __call__(self, argv, *, cwd=None, timeout=None):
            result = super().__call__(argv, cwd=cwd, timeout=timeout)
            if argv[:3] == ["gh", "pr", "checks"]:
                return subprocess.CompletedProcess(argv, 1, stdout=result.stdout, stderr="")
            return result

    gh = FailingChecksGh(checks_json=_CLAUDE_FAILED_CHECK, run_log=_AUTH_LOG)
    driver, clock, _ = make_driver([], cfg=CLAUDE_ONLY, gh=gh)
    driver.run()
    out = capsys.readouterr().out
    assert "REVIEWER AUTH FAILED" in out
    assert "REVIEWER SILENT" not in out


def test_no_auth_banner_on_clean_review(capsys):
    # claude responded (clean) → not silent → the probe never runs.
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY, auto_merge=True)
    driver.run()
    assert "REVIEWER AUTH FAILED" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Manual-landing exit-rebase: Bucket-C hand-backs carry rebase_skip=True so the
# exit-rebase NEVER force-pushes a dirty/diverged/unverifiable branch.
# ---------------------------------------------------------------------------

def test_poisoned_worktree_handback_sets_rebase_skip():
    timeline = [(0, Comment(id="a", text="missing null check", source="claude[bot]"))]
    fix: FixDispatch = lambda c, r: FixOutcome(
        status="rejected", rollback_failed=True, detail="refused")
    driver, _, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        answer_waiter=lambda esc, **k: {},
    )
    outcome = driver.run()
    assert outcome.status == "needs-human" and outcome.rebase_skip is True
    assert not gh.matching("git", "push")              # poisoned tree never pushed
    assert not gh.matching("git", "rebase")            # and never rebased


def test_push_failed_handback_sets_rebase_skip():
    timeline = [(0, Comment(id="a", text="missing null check", source="claude[bot]"))]
    fix: FixDispatch = lambda c, r: FixOutcome(status="applied")

    class PushFailGh(GhRecorder):
        def __call__(self, argv, *, cwd=None, timeout=None):
            rc = 1 if argv[:2] == ["git", "push"] else 0
            out = " M x.py\n" if argv[:3] == ["git", "status", "--porcelain"] else ""
            return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="push rejected")

    gh = PushFailGh()
    driver, _, _ = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        gh=gh, answer_waiter=lambda esc, **k: {},
    )
    outcome = driver.run()
    assert outcome.status == "needs-human" and outcome.rebase_skip is True
    assert not gh.matching("git", "rebase")            # diverged remote never rebased


def test_red_gate_stop_handback_sets_rebase_skip(monkeypatch):
    timeline = [(0, Comment(id="a", text="missing null check", source="claude[bot]"))]
    fix: FixDispatch = lambda c, r: FixOutcome(status="applied")
    # The per-round push stopped on a red test gate, leaving uncommitted residue.
    monkeypatch.setattr(round_driver.commit_push, "commit_and_push",
                        lambda *a, **k: "stopped")
    driver, _, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        answer_waiter=lambda esc, **k: {},
    )
    outcome = driver.run()
    assert outcome.status == "stopped" and outcome.rebase_skip is True
    assert not gh.matching("git", "rebase")


def test_eligible_handback_does_not_set_rebase_skip():
    # An unanswered escalation is a Bucket-A/B hand-back — rebase-eligible.
    timeline = [(0, Comment(id="a", text="should we drop this column?",
                            source="claude[bot]"))]
    driver, _, _ = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("BUSINESS_QUESTION"),
        answer_waiter=lambda esc, **k: {"a": None},
    )
    outcome = driver.run()
    assert outcome.status == "needs-human" and outcome.rebase_skip is False


def test_no_auth_banner_on_normal_review_round(capsys):
    # claude responded with a finding then a clean re-review → not silent.
    timeline = [
        (0, Comment(id="a", text="this null check is missing", source="claude[bot]")),
        (90, Comment(id="b", text="No issues found.", source="claude[bot]")),
    ]
    fix: FixDispatch = lambda c, r: FixOutcome(status="applied")
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        auto_merge=True, answer_waiter=lambda esc, **k: {},
    )
    driver.run()
    assert "REVIEWER AUTH FAILED" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Substantive-progress gate: only a landed SUBSTANTIVE fix that changed files
# earns another round; everything else is a clean finish.
# ---------------------------------------------------------------------------

def test_cosmetic_only_round_ends_the_run_clean():
    # A round whose only fix is COSMETIC produces no substantive progress: the
    # cosmetic fix is committed/pushed, then the run ends clean (merges under
    # auto-merge) with NO re-request round.
    timeline = [(0, Comment(id="a", text="rename tmp for clarity", source="claude[bot]"))]
    fix: FixDispatch = lambda c, r: FixOutcome(status="applied")
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("COSMETIC"), fix=fix,
        auto_merge=True, answer_waiter=lambda esc, **k: {},
    )
    outcome = driver.run()
    assert outcome.status == "clean" and outcome.rounds == 1
    assert outcome.merged is True
    assert [a.final for a in outcome.actions] == ["fixed"]  # cosmetic fix applied
    assert gh.matching("git", "push")                       # …committed + pushed
    assert len(gh.matching("@claude review")) == 1          # but NO round-2 re-request
    assert gh.matching("gh", "merge", "--squash")


def test_substantive_fix_with_no_file_change_ends_clean():
    # A SUBSTANTIVE fix that applied but changed NO files (commit_and_push returns
    # "nothing"; the worktree probe is clean) is not substantive PROGRESS → the run
    # ends clean without a re-request round.
    timeline = [(0, Comment(id="a", text="this null check is missing", source="claude[bot]"))]
    fix: FixDispatch = lambda c, r: FixOutcome(status="applied")

    class CleanTreeGh(GhRecorder):
        # git status is always CLEAN → commit_and_push returns "nothing".
        def __call__(self, argv, *, cwd=None, timeout=None):
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    gh = CleanTreeGh()
    driver, clock, _ = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        gh=gh, max_rounds=3, answer_waiter=lambda esc, **k: {},
    )
    outcome = driver.run()
    assert outcome.status == "clean" and outcome.rounds == 1
    assert len(gh.matching("@claude review")) == 1  # no confirmation round requested


def test_substantive_fix_earns_another_round():
    # The positive control: a landed SUBSTANTIVE fix that DID change files earns a
    # re-request round, which then comes in clean.
    timeline = [
        (0, Comment(id="a", text="this null check is missing", source="claude[bot]")),
        (90, Comment(id="b", text="No issues found.", source="claude[bot]")),
    ]
    fix: FixDispatch = lambda c, r: FixOutcome(status="applied")
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        auto_merge=True, answer_waiter=lambda esc, **k: {},
    )
    outcome = driver.run()
    assert outcome.status == "clean" and outcome.rounds == 2
    assert len(gh.matching("@claude review")) == 2   # summon + confirmation round
    assert outcome.merged is True


# ---------------------------------------------------------------------------
# Sticky polish-exclusion + soft-reset on --rr
# ---------------------------------------------------------------------------

def test_polish_only_reviewer_dropped_from_rerequest():
    # copilot posts only a cosmetic nit (no real finding) → dropped as polish-only;
    # claude's substantive fix drives round 2, where copilot is no longer summoned.
    cfg = {"active_reviewers": ["claude", "copilot"],
           "auto_on_open": {"claude": False, "copilot": False}}

    def classify(prompt):
        label = "COSMETIC" if "nit:" in prompt else "SUBSTANTIVE"
        return json.dumps({"label": label, "reason": "t"})

    timeline = [
        (0, Comment(id="a", text="this null check is missing", source="claude[bot]")),
        (0, Comment(id="b", text="nit: rename tmp", source="copilot[bot]")),
        (90, Comment(id="c", text="No issues found.", source="claude[bot]")),
    ]
    fix: FixDispatch = lambda c, r: FixOutcome(status="applied")
    driver, clock, gh = make_driver(
        timeline, cfg=cfg, classify=classify, fix=fix,
        max_rounds=3, answer_waiter=lambda esc, **k: {},
    )
    driver.run()
    assert "copilot" in driver.polishing                 # cosmetic-only → polish-dropped
    assert len(gh.matching("requested_reviewers")) == 1  # copilot summoned round 1 only
    assert len(gh.matching("@claude review")) == 2       # claude re-requested in round 2


def test_reviewer_with_a_real_finding_is_not_polished():
    # A reviewer that posts a SUBSTANTIVE finding this round is never demoted to
    # polish, even if the fix landed and it has "nothing left" — it earns re-review.
    timeline = [
        (0, Comment(id="a", text="this null check is missing", source="claude[bot]")),
        (90, Comment(id="b", text="No issues found.", source="claude[bot]")),
    ]
    fix: FixDispatch = lambda c, r: FixOutcome(status="applied")
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        answer_waiter=lambda esc, **k: {},
    )
    driver.run()
    assert "claude" not in driver.polishing
    assert "claude" not in driver.reviewed_no_change  # a FIXED finding survives


# ---------------------------------------------------------------------------
# Reviewed — no change: a dismissed-substantive reviewer is dropped with its
# own label (never the polish mislabel) and never re-asked this run
# ---------------------------------------------------------------------------

def test_dismissed_substantive_reviewer_is_reviewed_no_change(capsys):
    # copilot's substantive finding is dismissed by the fixer (skip — judged
    # invalid / already-fixed → final "skipped-invalid", NO change applied):
    # it renders "Reviewed — no change" and is dropped from re-request, while
    # claude's FIXED finding drives round 2 exactly as today.
    cfg = {"active_reviewers": ["claude", "copilot"],
           "auto_on_open": {"claude": False, "copilot": False}}

    def classify(prompt):
        return json.dumps({"label": "SUBSTANTIVE", "reason": "t"})

    timeline = [
        (0, Comment(id="a", text="this null check is missing", source="claude[bot]")),
        (0, Comment(id="b", text="this validation is wrong", source="copilot[bot]")),
        (90, Comment(id="c", text="No issues found.", source="claude[bot]")),
    ]

    def fix(c, r):
        if c.id == "b":
            return FixOutcome(status="skipped", detail="already handled upstream")
        return FixOutcome(status="applied")

    driver, clock, gh = make_driver(
        timeline, cfg=cfg, classify=classify, fix=fix,
        max_rounds=3, answer_waiter=lambda esc, **k: {},
    )
    driver.run()
    out = capsys.readouterr().out
    # The demotion: its own set + its own log line, never the polish bucket.
    assert "copilot" in driver.reviewed_no_change
    assert "copilot" not in driver.polishing
    assert ("[round] → excluding copilot from subsequent rounds this run "
            "(reviewed — no change: every finding dismissed on "
            "reassessment)") in out
    # Anti-loop: copilot is summoned round 1 only, never re-asked.
    assert len(gh.matching("requested_reviewers")) == 1
    assert len(gh.matching("@claude review")) == 2       # claude re-asked as today
    # The SAME round's table already shows the terminal label, not "Active",
    # and never the polish mislabel.
    copilot_rows = [ln for ln in out.splitlines()
                    if ln.startswith("│") and " Copilot" in ln]
    assert copilot_rows and all("Reviewed — no change" in ln for ln in copilot_rows)
    assert all("Polish-only" not in ln for ln in copilot_rows)


def test_mixed_dismissed_substantive_and_cosmetic_is_reviewed_no_change(capsys):
    # A reviewer whose substantive finding was dismissed AND whose cosmetic nit
    # was applied lands in reviewed-no-change (the dismissed-substantive signal
    # outranks plain polish — mirroring the reassessed-away precedence).
    cfg = {"active_reviewers": ["copilot"], "auto_on_open": {"copilot": False}}

    def classify(prompt):
        label = "COSMETIC" if "nit:" in prompt else "SUBSTANTIVE"
        return json.dumps({"label": label, "reason": "t"})

    timeline = [
        (0, Comment(id="a", text="this null check is missing", source="copilot[bot]")),
        (0, Comment(id="b", text="nit: rename tmp", source="copilot[bot]")),
    ]

    def fix(c, r):
        if c.id == "a":
            return FixOutcome(status="rejected", detail="fix-verify REJECT")
        return FixOutcome(status="applied")

    driver, clock, gh = make_driver(
        timeline, cfg=cfg, classify=classify, fix=fix,
        max_rounds=3, answer_waiter=lambda esc, **k: {},
    )
    driver.run()
    out = capsys.readouterr().out
    assert "copilot" in driver.reviewed_no_change
    assert "copilot" not in driver.polishing
    row1 = next(ln for ln in out.splitlines()
                if ln.startswith("│") and " Copilot" in ln)
    assert "Reviewed — no change" in row1 and "Polish-only" not in row1


def test_failed_fix_escalation_keeps_reviewer_engaged():
    # A fix that FAILED (→ escalated) is not a dismissal — the comment is still
    # pending, so the reviewer stays in the re-request gate. copilot's LANDED
    # substantive fix drives round 2, where claude must be re-asked.
    cfg = {"active_reviewers": ["claude", "copilot"],
           "auto_on_open": {"claude": False, "copilot": False}}
    timeline = [
        (0, Comment(id="a", text="this null check is missing", source="claude[bot]")),
        (0, Comment(id="b", text="this validation is wrong", source="copilot[bot]")),
        (90, Comment(id="c", text="No issues found.", source="claude[bot]")),
        (91, Comment(id="d", text="No issues found.", source="copilot[bot]")),
    ]

    def fix(c, r):
        if c.id == "a":
            return FixOutcome(status="transient-failed", detail="x")
        return FixOutcome(status="applied")

    driver, clock, gh = make_driver(
        timeline, cfg=cfg, classify=label_runner("SUBSTANTIVE"), fix=fix,
        max_rounds=3, answer_waiter=lambda esc, **k: {"fix-a": "1"},
    )
    driver.run()
    assert "claude" not in driver.reviewed_no_change
    assert "claude" not in driver.polishing
    assert len(gh.matching("@claude review")) == 2       # re-asked in round 2


def test_deferred_fix_keeps_reviewer_engaged():
    # Decision-only run (no fixer wired): the substantive comment is deferred,
    # not dismissed — the reviewer is never demoted and stays in the expected
    # set (its row reads "Active", not a terminal drop label).
    timeline = [
        (0, Comment(id="a", text="this null check is missing", source="claude[bot]")),
    ]
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=None,
        answer_waiter=lambda esc, **k: {},
    )
    driver.run()
    assert "claude" not in driver.reviewed_no_change
    assert "claude" not in driver.polishing
    assert "claude" in driver.expected_bots()            # still engaged


def test_clean_approval_survives_a_later_quota_placeholder(capsys):
    # End-to-end: claude posts "No issues found." and THEN a quota placeholder
    # in the same round. The placeholder overwrites the clean signal, but the
    # sticky approval keeps the label "Approved" — never the promotion's
    # "Reviewed — no findings" (the sign-off already happened).
    cfg = {"active_reviewers": ["claude", "gemini"],
           "auto_on_open": {"claude": False, "gemini": True}}
    timeline = [
        (0, Comment(id="ok", text="No issues found.", source="claude[bot]",
                    created_at="2026-07-01T00:00:00Z")),
        # gemini stays silent, holding the round open past the placeholder.
        (30, Comment(id="q", text="Rate limit exceeded for this model.",
                     source="claude[bot]", created_at="2026-07-01T00:00:30Z")),
    ]
    driver, clock, gh = make_driver(timeline, cfg=cfg)
    driver.run()
    out = capsys.readouterr().out
    assert "claude" in driver.approved and "claude" in driver.done
    row1 = next(ln for ln in out.splitlines()
                if ln.startswith("│") and " Claude" in ln)
    assert "Approved" in row1
    assert "Reviewed — no findings" not in row1


def test_same_round_table_shows_polish_only(capsys):
    # The demotions land BEFORE the table renders: a cosmetic-only reviewer's
    # own round already reads "Polish-only", not a stale "Active".
    cfg = {"active_reviewers": ["copilot"], "auto_on_open": {"copilot": False}}
    timeline = [(0, Comment(id="b", text="nit: rename tmp", source="copilot[bot]"))]
    fix: FixDispatch = lambda c, r: FixOutcome(status="applied")
    driver, clock, gh = make_driver(
        timeline, cfg=cfg, classify=label_runner("COSMETIC"), fix=fix,
        max_rounds=3, answer_waiter=lambda esc, **k: {},
    )
    driver.run()
    out = capsys.readouterr().out
    row1 = next(ln for ln in out.splitlines()
                if ln.startswith("│") and " Copilot" in ln)
    assert "Polish-only" in row1 and "Active" not in row1


def test_rr_clears_soft_exclusions_but_keeps_hard_buckets():
    # --rr re-pings everyone: the soft buckets (voluntarily-done + polish +
    # reviewed-no-change) are cleared at run start so a previously-satisfied
    # reviewer is summoned again; a hard bucket (quota) survives.
    driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY, rr=True)
    driver.done.add("claude")               # a stale voluntarily-done…
    driver.polishing.add("claude")          # …a stale polish drop…
    driver.reviewed_no_change.add("claude")  # …and a stale no-change drop
    driver.store.exclude_quota("copilot")   # a HARD bucket that must survive
    driver.run()
    assert "claude" not in driver.done          # soft buckets cleared by --rr
    assert "claude" not in driver.polishing
    assert "claude" not in driver.reviewed_no_change
    assert driver.store.is_excluded("copilot")  # hard bucket untouched
    assert gh.matching("@claude review")        # claude summoned (soft exclusion cleared)


# ---------------------------------------------------------------------------
# Mid-run silent drop + never-seen bot holds the round open
# ---------------------------------------------------------------------------

def test_silent_reviewer_dropped_mid_run():
    # copilot is expected but silent for the whole round; claude's substantive fix
    # keeps the run going. copilot is dropped from re-request (silence ≠ approval)
    # and no longer appears in the expected set or the round-2 summon.
    cfg = {"active_reviewers": ["claude", "copilot"],
           "auto_on_open": {"claude": False, "copilot": False}}
    timeline = [
        (0, Comment(id="a", text="this null check is missing", source="claude[bot]")),
        # claude's clean re-review lands only AFTER round 1's idle close (copilot
        # holds round 1 open as a never-seen bot), so it belongs to round 2.
        (1000, Comment(id="c", text="No issues found.", source="claude[bot]")),
    ]
    fix: FixDispatch = lambda c, r: FixOutcome(status="applied")
    driver, clock, gh = make_driver(
        timeline, cfg=cfg, classify=label_runner("SUBSTANTIVE"), fix=fix,
        max_rounds=3, answer_waiter=lambda esc, **k: {},
    )
    driver.run()
    assert "copilot" in driver.silent_dropped             # silent a full round → dropped
    assert "copilot" not in driver.expected_bots()        # …and out of the expected set
    assert len(gh.matching("requested_reviewers")) == 1   # summoned round 1 only, not round 2
    assert len(gh.matching("@claude review")) == 2        # claude re-requested in round 2


def test_silent_reviewer_that_comes_back_is_reincluded():
    # A reviewer dropped for silence is re-included the moment it responds again.
    driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
    driver.silent_dropped.add("claude")
    driver.silent_rounds["claude"] = 1
    driver.bots.setdefault("claude", round_driver.BotState()).last_seen = 5.0  # responded
    driver._record_round_attendance(["claude"])
    assert "claude" not in driver.silent_dropped
    assert driver.silent_rounds["claude"] == 0


def test_silent_dropped_bot_reincluded_via_classify_signal():
    # _record_round_attendance() only iterates expected_bots(), which excludes
    # silent_dropped — so a dropped bot that posts a new comment must be
    # re-included via _classify_signal(), not via round-end bookkeeping.
    driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
    driver.silent_dropped.add("claude")
    comment = Comment(id="x", text="this looks wrong", source="claude[bot]")
    driver._classify_signal(comment, clock.t)
    assert "claude" not in driver.silent_dropped
    assert "claude" in driver.expected_bots()


# ---------------------------------------------------------------------------
# Post-summon register delay
# ---------------------------------------------------------------------------

def test_register_delay_waits_before_polling_when_a_summon_lands():
    # After a summon actually lands, the loop waits register_delay before opening
    # the poll window, so the round can't close before the delay elapses.
    times = RoundTimes(quiescence=60, poll_interval=30, min_bot_wait=420,
                       idle_timeout=900, max_wait_total=1800, register_delay=60)
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY, times=times, auto_merge=True)
    driver.run()
    assert clock.t >= 60          # the register delay elapsed before the round closed


def test_no_register_delay_when_no_summon_lands():
    # An all-auto_on_open fleet is not summoned in round 1, so no re-request lands
    # and the register delay is skipped — the poll window opens immediately.
    times = RoundTimes(quiescence=60, poll_interval=30, min_bot_wait=420,
                       idle_timeout=900, max_wait_total=1800, register_delay=60)
    cfg = {"active_reviewers": ["copilot"], "auto_on_open": {"copilot": True}}
    timeline = [(0, Comment(id="a", text="No issues found.", source="copilot[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=cfg, times=times, auto_merge=True)
    driver.run()
    assert clock.t < 60                              # no summon → no register delay
    assert gh.matching("requested_reviewers") == []


# ---------------------------------------------------------------------------
# BUDDHI_BOT_QUIESCENCE_SECS env clamp
# ---------------------------------------------------------------------------

def test_env_positive_or_default_clamps_nonpositive(monkeypatch):
    f = round_driver._env_positive_or_default
    monkeypatch.setenv("X_Q", "0"); assert f("X_Q", 60) == 60      # zero → default
    monkeypatch.setenv("X_Q", "-5"); assert f("X_Q", 60) == 60     # negative → default
    monkeypatch.setenv("X_Q", "garbage"); assert f("X_Q", 60) == 60  # unparseable → default
    monkeypatch.setenv("X_Q", "45"); assert f("X_Q", 60) == 45     # positive → honoured
    monkeypatch.delenv("X_Q", raising=False); assert f("X_Q", 60) == 60  # unset → default


# ---------------------------------------------------------------------------
# Errored comeback reads updated_at (edit time) before created_at
# ---------------------------------------------------------------------------

def test_errored_comeback_uses_updated_at_when_created_at_is_older():
    # An EDITED substantive comment proves recovery by its updated_at, even though
    # its created_at is NOT newer than the recorded error stamp.
    timeline = [
        (0, Comment(id="a", text="I encountered an internal error while reviewing.",
                    source="claude[bot]", created_at="2026-06-10T00:05:00Z")),
        (30, Comment(id="b", text="this null check is missing", source="claude[bot]",
                     created_at="2026-06-10T00:00:00Z",     # created BEFORE the error…
                     updated_at="2026-06-10T00:10:00Z")),    # …but edited AFTER it
    ]
    driver, clock, gh = make_driver(timeline, cfg=TWO_BOTS,
                                    classify=label_runner("SUBSTANTIVE"))
    driver.run()
    assert not driver.store.is_excluded("claude")   # came back on the edit time


def test_errored_comeback_updated_at_takes_precedence_over_created_at():
    # updated_at is the primary candidate: an edit stamp OLDER than the error keeps
    # the bot excluded even when created_at alone would be newer.
    timeline = [
        (0, Comment(id="a", text="I encountered an internal error while reviewing.",
                    source="claude[bot]", created_at="2026-06-10T00:05:00Z")),
        (30, Comment(id="b", text="this null check is missing", source="claude[bot]",
                     created_at="2026-06-10T00:06:00Z",     # created after the error…
                     updated_at="2026-06-10T00:04:00Z")),    # …but updated_at is OLDER → wins
    ]
    driver, clock, gh = make_driver(timeline, cfg=TWO_BOTS,
                                    classify=label_runner("SUBSTANTIVE"))
    driver.run()
    assert driver.store.is_excluded("claude")   # updated_at (older) → no comeback
