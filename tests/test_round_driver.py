"""Quiescence round-loop + exclusion wiring — fake clock, no network."""
import json
import subprocess
from dataclasses import replace
from datetime import datetime, timezone

from buddhi_review import detectors, gh_ingest, polish_state, round_driver
from buddhi_review.actuators import FixDispatch
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.fix_apply import FixOutcome
from buddhi_review.loop import Comment
from buddhi_review.round_driver import RoundDriver, RoundTimes
from buddhi_review.seams import ConsoleEscalation

UTC = timezone.utc


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


class FakeThreads:
    """A stateful stand-in for GitHub's review-thread API used by the pre-merge
    thread gate. ``fetch`` returns the current threads (fresh copies); ``resolve``
    flips one to resolved so a subsequent ``fetch`` sees it, exactly like the real
    ``resolveReviewThread`` mutation. Set ``fail=True`` to make the reader raise a
    transient gh / GraphQL error; ``resolve_fail=True`` to make resolve() a no-op
    (the mutation silently failed)."""

    def __init__(self, threads=()):
        self._threads = {t.id: t for t in threads}
        self.fail = False
        self.resolve_fail = False
        self.resolved = []          # thread ids passed to resolve(), in order
        self.fetches = 0            # number of fetch() calls (re-query counting)

    def thread(self, id, root_comment_id, is_resolved=False, replies=()):
        # Mirror the real reader: comment_ids holds the root AND every reply, so a
        # test can model a resolved thread with follow-up comments via `replies`.
        ids = ([root_comment_id] if root_comment_id is not None else []) + list(replies)
        self._threads[id] = gh_ingest.ReviewThread(
            id=id, is_resolved=is_resolved, root_comment_id=root_comment_id,
            comment_ids=frozenset(ids))
        return self

    def fetch(self, pr, repo=None, cwd=None):
        self.fetches += 1
        if self.fail:
            raise RuntimeError("gh graphql boom")
        return [replace(t) for t in self._threads.values()]

    def resolve(self, thread_id, cwd=None):
        self.resolved.append(thread_id)
        if self.resolve_fail:
            return False
        t = self._threads.get(thread_id)
        if t is not None:
            self._threads[thread_id] = replace(t, is_resolved=True)
        return True


def make_driver(timeline, *, cfg, clock=None, gh=None, times=None, classify=None,
                fix=None, answer_waiter=None, reactions=None, **kw):
    """timeline = [(t_visible, Comment), ...] — comments appear at clock time.
    reactions = [(t_visible, gh_ingest.Reaction), ...] — same clock semantics."""
    clock = clock or FakeClock()
    gh = gh or GhRecorder()
    def fetch(pr, repo=None, cwd=None):
        return [c for t, c in timeline if t <= clock.t]
    def fetch_reactions(pr, repo=None, cwd=None):
        return [r for t, r in (reactions or []) if t <= clock.t]
    adapter = ReviewAdapter(escalation=ConsoleEscalation(notifier=FakeNotifier()))
    # make_driver models a FRESH launch — the timeline's comments arrive DURING
    # the round (after a summon), so preflight (the pre-round-1 snapshot of a PR's
    # pre-existing reviews) is off by default here; the dedicated preflight tests
    # pass preflight=True and seed comments/reactions already on the PR.
    kw.setdefault("preflight", False)
    # Network-free thread-gate seams by default (no threads → the gate is a no-op
    # and merge tests behave exactly as before). A test exercising the gate passes
    # its own threads_fetch / resolve_thread (e.g. a FakeThreads) via **kw.
    kw.setdefault("threads_fetch", lambda pr, repo=None, cwd=None: [])
    kw.setdefault("resolve_thread", lambda thread_id, cwd=None: True)
    driver = RoundDriver(
        "7", repo="o/r", cwd="/nonexistent", cfg=cfg, adapter=adapter,
        classify_runner=classify or label_runner("INVALID"),
        fix_dispatch=fix,
        fetch=fetch, reactions_fetch=fetch_reactions, gh_run=gh,
        clock=clock, sleep=clock.sleep,
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


def test_shielded_errored_body_with_clean_phrasing_not_approved():
    # A review body that trips the errored regex AND contains "no issues found"
    # phrasing, shielded by a same-instant inline finding, must NOT be promoted
    # to clean-approved. The bot errored; its inline findings are the actual
    # review output. Crowning it "Approved" would satisfy the merge gate even
    # though nobody truly reviewed the PR.
    driver, clock, gh = make_driver([], cfg=CLAUDE_ONLY)
    body = Comment(
        id="a",
        text="I encountered an internal error while reviewing some files. "
             "No issues found in the rest.",
        source="claude[bot]",
        created_at="2026-06-10T00:00:00Z",
    )
    stamps = {"claude": ["2026-06-10T00:00:00Z"]}
    result = driver._classify_signal(body, 0.0, batch_finding_stamps=stamps)
    assert result == "claude"           # shielded → actionable, not suppressed
    assert not driver.store.is_excluded("claude")  # not recorded as errored
    assert "claude" not in driver.done      # NOT promoted to done
    assert "claude" not in driver.approved  # NOT clean-approved


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
    # shared worktree. Its disposition is "rejected" and it escalates for a human;
    # here the escalation is answered with {} (proceed), so the round MUST still
    # halt at the poisoned-worktree gate before the push, or the un-rolled-back,
    # explicitly-refused residue would ride the sibling's `git add -A` onto the PR.
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
    # Same shape but the rollback SUCCEEDED (rollback_failed=False): the poison
    # gate must NOT fire, and here the REJECT escalation is answered with {}
    # (proceed), so the sibling's applied fix pushes and the run proceeds normally.
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


def test_reject_unanswered_escalation_hands_back_never_merges():
    # A verify-REJECT is surfaced for a human like a failed fix (never silently
    # dismissed). With a clean rollback the poison gate does NOT fire, so what
    # keeps the rejected finding from auto-merging is the escalation: left
    # unanswered, the loop hands back (needs-human) rather than counting the
    # rejected finding as clean progress and merging it unfixed.
    timeline = [(0, Comment(id="a", text="this null check is missing",
                            source="claude[bot]"))]
    fix: FixDispatch = lambda c, r: FixOutcome(
        status="rejected", rollback_failed=False, detail="verify REJECT")
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        auto_merge=True, answer_waiter=lambda esc, **k: {"fix-a": None},
    )
    outcome = driver.run()
    assert outcome.status == "needs-human"    # unanswered REJECT → hand back
    assert not gh.matching("gh", "merge")     # a standing rejected finding never auto-merges


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
    # copilot's substantive finding is dismissed by the fixer (a genuine SKIP
    # citing "already handled upstream" → final "skipped-already-fixed", NO change
    # applied): it renders "Reviewed — no change" and is dropped from re-request,
    # while claude's FIXED finding drives round 2 exactly as today.
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
    # A reviewer whose substantive finding was dismissed (a genuine invalid SKIP)
    # AND whose cosmetic nit was applied lands in reviewed-no-change (the
    # dismissed-substantive signal outranks plain polish — mirroring the
    # reassessed-away precedence). A verify-REJECT would NOT dismiss (it escalates
    # + stays engaged); the dismissal here is a genuine validity-judgment SKIP.
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
            return FixOutcome(status="skipped", detail="SKIP: the cited path is unreachable")
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


# ===========================================================================
# PR-state snapshot machinery: preflight · reactions · round baselines
# (make_driver defaults preflight=False; these tests opt in with preflight=True
# and/or a reactions timeline, so a comment/reaction already on the PR is folded
# BEFORE round 1.)
# ===========================================================================

# --------------------------------------------------------------- preflight (#10)

def test_preflight_processes_pre_existing_comment_in_round1_without_waiting():
    # A finding already on the PR at launch is folded at preflight and fixed in
    # round 1 with NO poll wait — the finding-poster already gave its verdict, so
    # round 1 neither re-summons nor waits on it.
    fixed = []

    def fix(c, r):
        fixed.append(c.id)
        return FixOutcome(status="applied")

    timeline = [(0, Comment(id="a", text="rename tmp for clarity", source="claude[bot]"))]
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("COSMETIC"), fix=fix,
        auto_merge=True, answer_waiter=lambda esc, **k: {}, preflight=True)
    outcome = driver.run()
    assert outcome.status == "clean" and outcome.rounds == 1
    assert fixed == ["a"]                          # the pre-existing comment was fixed
    assert clock.t < driver.times.min_bot_wait      # NO min-bot wait was burned
    assert gh.matching("@claude review") == []      # finding-poster not re-summoned round 1
    assert outcome.merged is True                   # a genuine review happened → merge


def test_preflight_all_clean_skips_the_poll_entirely():
    # Every reviewer already approved before launch → the fleet is empty at round 1
    # and the existing "no expected bots → clean exit" branch fires: the poll window
    # is never opened, so an already-reviewed PR does not burn the min-bot wait.
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY, auto_merge=True,
                                    preflight=True)
    outcome = driver.run()
    assert outcome.status == "clean"
    assert "claude" in driver.approved and "claude" in driver.reviewed_ever
    assert clock.t == 0                              # poll never opened → no wait
    assert gh.matching("@claude review") == []      # nobody re-summoned
    assert outcome.merged is True                   # the clean review still merges
    assert outcome.rounds == 0                       # exited before any polling round


def test_preflight_partial_clean_still_summons_the_unreviewed_bot():
    # copilot already approved (preflight-done); claude has NOT reviewed. Round 1
    # skips copilot (already responded) but still summons + waits on claude.
    cfg = {"active_reviewers": ["claude", "copilot"],
           "auto_on_open": {"claude": False, "copilot": True}}
    timeline = [
        (0, Comment(id="c", text="No issues found.", source="copilot[bot]")),
        (30, Comment(id="a", text="No issues found.", source="claude[bot]")),
    ]
    driver, clock, gh = make_driver(timeline, cfg=cfg, auto_merge=True, preflight=True)
    outcome = driver.run()
    assert "copilot" in driver.done                  # folded clean at preflight
    assert gh.matching("requested_reviewers") == []  # copilot NOT re-summoned
    assert gh.matching("@claude review")             # claude WAS summoned (unreviewed)
    assert "claude" in driver.done
    assert outcome.merged is True


def test_preflight_honors_pre_existing_rate_limit_marker_and_scans_once():
    # A rate-limit marker already on the PR is honored at preflight (claude released
    # before round 1), and the marker is scanned exactly once across preflight +
    # poll (processed_ids de-dup). A second bot keeps the poll running so the two
    # scan paths are both exercised.
    reset = datetime(2026, 7, 4, 13, 0, tzinfo=UTC)
    before = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    marker = Comment(
        id="m", from_issue_channel=True, source="github-actions[bot]",
        created_at="2026-07-04T10:00:00Z",
        text=("<!-- claude-review-unavailable-v1 type=rate_limited "
              f"resets_at={int(reset.timestamp())} -->"))
    cfg = {"active_reviewers": ["claude", "copilot"],
           "auto_on_open": {"claude": False, "copilot": True}}
    timeline = [(0, marker),
                (30, Comment(id="c", text="No issues found.", source="copilot[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=cfg, wall_clock=lambda: before,
                                    auto_merge=True, preflight=True)
    scans = []
    _orig = driver._scan_unavailable_markers
    driver._scan_unavailable_markers = (
        lambda fresh: scans.append([c.id for c in fresh]) or _orig(fresh))
    outcome = driver.run()
    assert driver._rate_limited_until.get("claude") == reset   # released at preflight
    assert driver._bot_state("claude").signal == detectors.SIGNAL_RATE_LIMITED
    assert sum(batch.count("m") for batch in scans) == 1        # scanned exactly once
    assert len(scans) >= 2                                      # preflight + poll ran
    assert "copilot" in driver.done                            # the poll still ran


def test_preflight_de_dups_a_folded_comment_from_the_poll():
    # A pre-existing finding folded at preflight is NEVER re-ingested by the poll —
    # processed_ids guarantees it is dispatched to the fixer exactly once.
    fixed = []

    def fix(c, r):
        fixed.append(c.id)
        return FixOutcome(status="applied")

    # fetch always returns the finding (as if it stays on the PR); only preflight
    # consumes it, the poll must not re-see it.
    driver, clock, gh = make_driver(
        [(0, Comment(id="a", text="rename tmp", source="claude[bot]"))],
        cfg=CLAUDE_ONLY, classify=label_runner("COSMETIC"), fix=fix,
        answer_waiter=lambda esc, **k: {}, preflight=True)
    driver.run()
    assert fixed == ["a"]                 # dispatched once, not twice
    assert "a" in driver.processed_ids


def test_preflight_chatter_only_bot_is_still_polled_for_its_real_review():
    # A bot that posts only issue-channel chatter before launch (no verdict) is
    # NOT a preflight responder — round 1 still summons + waits for it, and its
    # real review (arriving during the round) is caught. Guards against skipping a
    # reviewer that only said "reviewing…" but had not actually reviewed.
    chatter = Comment(id="ch", from_issue_channel=True, source="claude[bot]",
                      text="I'm reviewing this PR now.")
    timeline = [(0, chatter),
                (30, Comment(id="ok", text="No issues found.", source="claude[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY, auto_merge=True,
                                    preflight=True)
    outcome = driver.run()
    assert "claude" not in driver._preflight_responders   # chatter is not a verdict
    assert gh.matching("@claude review")                  # claude WAS summoned round 1
    assert "claude" in driver.done                        # its real review was caught
    assert clock.t < driver.times.min_bot_wait            # seen at preflight → no long hold
    assert outcome.merged is True


# --------------------------------------------------------------- reactions (#g13a)

def _reaction(rid, content="+1", source="chatgpt-codex-connector[bot]"):
    return gh_ingest.Reaction(id=rid, content=content, source=source)


def test_fresh_plus_one_reaction_marks_bot_done():
    # codex posts NO comment — only a +1 reaction that lands after the summon (a
    # fresh id, not in the stale baseline). It routes through the same clean outcome
    # the sentinel uses: Approved / reviewed / done.
    cfg = {"active_reviewers": ["codex"], "auto_on_open": {"codex": False}}
    driver, clock, gh = make_driver(
        [], cfg=cfg, reactions=[(30, _reaction("rx1"))], auto_merge=True)
    outcome = driver.run()
    assert "codex" in driver.done and "codex" in driver.approved
    assert "codex" in driver.reviewed_ever        # a +1 IS a genuine clean review
    assert outcome.merged is True
    assert clock.t < driver.times.min_bot_wait     # the +1 quiesced the round


def test_stale_plus_one_reaction_does_not_mark_done():
    # A +1 already present at round start (id in the baseline) is stale — from an
    # earlier commit — and never marks the bot done; codex stays expected and,
    # silent all round, is dropped.
    cfg = {"active_reviewers": ["codex"], "auto_on_open": {"codex": True}}
    driver, clock, gh = make_driver(
        [], cfg=cfg, reactions=[(0, _reaction("rx-old", source="codex[bot]"))])
    driver.run()
    assert "codex" not in driver.done               # stale +1 is not a fresh sign-off
    assert "codex" in driver.silent_dropped         # silent a full round → dropped


def test_reaction_capture_failure_does_not_fold_a_stale_plus_one():
    # If the baseline CAPTURE fetch fails transiently (no baseline is ever
    # established), a stale +1 present before the run must NOT be folded — the
    # None-baseline sentinel keeps the fold fail-closed, so a phantom empty
    # baseline can never let a stale +1 masquerade as fresh and auto-merge.
    cfg = {"active_reviewers": ["codex"], "auto_on_open": {"codex": True}}
    driver, clock, gh = make_driver([], cfg=cfg, auto_merge=True, preflight=True)
    calls = {"n": 0}
    stale = [gh_ingest.Reaction(id="rx-old", content="+1", source="codex[bot]")]

    def flaky(pr, repo=None, cwd=None):
        calls["n"] += 1
        if calls["n"] <= 2:            # the preflight + round-1 baseline captures fail
            raise RuntimeError("gh transient")
        return stale                    # a later fetch would surface the stale +1

    driver.fetch_reactions = flaky
    outcome = driver.run()
    assert "codex" not in driver.done       # stale +1 never folded (fail closed)
    assert "codex" not in driver.approved
    assert outcome.merged is False          # SAFETY gate blocks the unreviewed merge


def test_plus_one_never_overrides_a_quota_exclusion():
    # codex hits quota (via a comment), and a fresh +1 lands the same round — the
    # +1 must NOT flip it to done; the hard exclusion stands.
    cfg = {"active_reviewers": ["codex"], "auto_on_open": {"codex": False}}
    timeline = [(1, Comment(id="q", text="Rate limit exceeded for this model.",
                            source="codex[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=cfg, reactions=[(1, _reaction("rx2"))],
                                    auto_merge=True)
    outcome = driver.run()
    assert driver.store.is_excluded("codex")        # quota exclusion holds
    assert "codex" not in driver.done               # the +1 did not override it
    assert "codex" not in driver.approved
    assert outcome.merged is False                  # never reviewed → no merge


def test_plus_one_never_overrides_an_errored_exclusion():
    # Same rule for the errored bucket: a fresh +1 must not retract an errored
    # exclusion (only genuine review output does, via _maybe_errored_comeback).
    cfg = {"active_reviewers": ["codex"], "auto_on_open": {"codex": False}}
    timeline = [(1, Comment(id="e", text="I encountered an internal error while "
                            "generating the review.", source="codex[bot]"))]
    driver, clock, gh = make_driver(timeline, cfg=cfg, reactions=[(1, _reaction("rx3"))])
    driver.run()
    assert driver.store.is_excluded("codex")        # errored exclusion holds
    assert "codex" not in driver.done               # the +1 did not override it


# --------------------------------------------------------------- baselines (#g13c)

def test_late_comment_attributes_to_the_next_round_exactly_once():
    # A comment that is not present during round 1's poll is picked up by round 2's
    # poll exactly once (processed_ids), fixed there — never lost, never doubled.
    seen = []

    def classify(prompt):
        label = "COSMETIC" if "finding two" in prompt else "SUBSTANTIVE"
        return json.dumps({"label": label, "reason": "t"})

    def fix(c, r):
        seen.append(c.id)
        return FixOutcome(status="applied")

    timeline = [
        (1, Comment(id="a", text="finding one", source="claude[bot]")),
        (200, Comment(id="b", text="finding two", source="claude[bot]")),
    ]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY, classify=classify,
                                    fix=fix, max_rounds=5,
                                    answer_waiter=lambda esc, **k: {})
    driver.run()
    assert seen == ["a", "b"]                         # a in round 1, b in round 2
    assert seen.count("b") == 1                       # never double-processed
    assert "b" in driver._round_baseline.get("claude", set())  # recorded in the baseline


def test_between_round_quota_recheck_excludes_on_novel_wording():
    # A quota message whose wording BOTH the regex and the keyword gate miss is
    # classified as an ordinary finding in-round; the between-rounds re-check runs
    # the LLM quota tier (ungated) over the new-since-baseline item and excludes the
    # bot, retracting its mis-recorded review from the merge gate.
    novel = "My allotment for the cycle is spent; I will resume next period."
    assert detectors.detect_signal(novel, quota_llm=lambda p: {"quota": True}) is None

    def quota_llm(prompt):
        return {"quota": novel in prompt}

    timeline = [(1, Comment(id="q", text=novel, source="claude[bot]"))]
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"),
        fix=lambda c, r: FixOutcome(status="applied"), max_rounds=3,
        quota_llm=quota_llm, answer_waiter=lambda esc, **k: {})
    driver.run()
    assert driver.store.is_excluded("claude")                 # excluded by the re-check
    assert driver._bot_state("claude").signal == detectors.SIGNAL_QUOTA
    assert "claude" not in driver.reviewed_ever               # a quota msg is not a review


def test_preflight_novel_quota_is_re_checked_and_never_merges():
    # A novel-wording quota message ALREADY on the PR (folded at preflight) is run
    # through the same LLM quota re-check the poll path uses — so a reviewer that
    # only ever said "I'm out of quota" is excluded and never counts as a genuine
    # review, and the never-merge-unreviewed gate blocks the auto-merge.
    novel = "My allotment for the cycle is spent; I will resume next period."

    def quota_llm(prompt):
        return {"quota": novel in prompt}

    timeline = [(0, Comment(id="q", text=novel, source="claude[bot]"))]
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"),
        fix=lambda c, r: FixOutcome(status="applied"), max_rounds=3,
        quota_llm=quota_llm, auto_merge=True, answer_waiter=lambda esc, **k: {},
        preflight=True)
    outcome = driver.run()
    assert driver.store.is_excluded("claude")     # excluded by the preflight re-check
    assert "claude" not in driver.reviewed_ever   # a quota msg is not a review
    assert outcome.merged is False                # never-merge-unreviewed gate holds


def test_double_quota_message_does_not_leave_a_polluted_review():
    # A reviewer posting BOTH a novel-wording quota body (mis-recorded as a review)
    # AND a regex-caught quota in the SAME batch: the regex one hard-excludes it,
    # and the re-check must STILL purge the mis-recorded review from the novel one —
    # otherwise the never-merge gate would count a reviewer that never reviewed.
    novel = "My allotment for the cycle is spent; I will resume next period."

    def quota_llm(prompt):
        return {"quota": novel in prompt}

    timeline = [
        (1, Comment(id="nov", text=novel, source="claude[bot]")),
        (1, Comment(id="reg", text="Rate limit exceeded for this model.",
                    source="claude[bot]")),
    ]
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"),
        fix=lambda c, r: FixOutcome(status="applied"), max_rounds=2,
        quota_llm=quota_llm, auto_merge=True, answer_waiter=lambda esc, **k: {})
    outcome = driver.run()
    assert driver.store.is_excluded("claude")
    assert "claude" not in driver.reviewed_ever   # the mis-recorded review is purged
    assert outcome.merged is False                # never-merge-unreviewed gate holds


def test_between_round_recheck_is_noop_without_the_quota_seam():
    # With no quota_llm wired the re-check never fires — a plain finding stays a
    # finding and the bot is not excluded.
    novel = "My allotment for the cycle is spent; I will resume next period."
    timeline = [(1, Comment(id="q", text=novel, source="claude[bot]"))]
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"),
        fix=lambda c, r: FixOutcome(status="applied"), max_rounds=1,
        answer_waiter=lambda esc, **k: {})
    driver.run()
    assert not driver.store.is_excluded("claude")


def test_reaction_done_bot_still_gets_quota_recheck():
    # A bot folded via +1 reaction (reaction_done) must NOT skip the between-rounds
    # quota re-check. If the bot ALSO posted a novel-wording quota message that the
    # keyword gate missed in-round (and was mis-recorded as a review), the re-check
    # must catch it and exclude the bot — the hard cause wins over the +1 fold.
    cfg = {"active_reviewers": ["codex"], "auto_on_open": {"codex": False}}
    novel = "My allotment for the cycle is spent; I will resume next period."
    assert detectors.detect_signal(novel, quota_llm=lambda p: {"quota": True}) is None

    def quota_llm(prompt):
        return {"quota": novel in prompt}

    # The novel quota comment lands at t=1; the +1 reaction also lands at t=1
    # (after the summon at t=0). _fold_reactions folds the +1 first; without the
    # fix the done-guard would skip the quota re-check on the comment.
    timeline = [(1, Comment(id="q", text=novel, source="codex[bot]"))]
    driver, clock, gh = make_driver(
        timeline, cfg=cfg, reactions=[(1, _reaction("rx-q", source="codex[bot]"))],
        classify=label_runner("SUBSTANTIVE"),
        fix=lambda c, r: FixOutcome(status="applied"), max_rounds=3,
        quota_llm=quota_llm, auto_merge=True, answer_waiter=lambda esc, **k: {})
    outcome = driver.run()
    assert driver.store.is_excluded("codex")              # quota re-check caught it
    assert "codex" not in driver.reviewed_ever            # mis-recorded review purged
    assert outcome.merged is False                        # SAFETY gate blocks merge


def test_quota_recheck_evicts_reaction_done_bot_from_done():
    # After the between-rounds quota re-check detects quota on a reaction-done bot,
    # the bot must be removed from done (fix #2) so the quota hard-cause is the
    # unambiguous recorded outcome and no stale clean-state entry remains.
    cfg = {"active_reviewers": ["codex"], "auto_on_open": {"codex": False}}
    novel = "My allotment for the cycle is spent; I will resume next period."

    def quota_llm(prompt):
        return {"quota": novel in prompt}

    timeline = [(1, Comment(id="q", text=novel, source="codex[bot]"))]
    driver, clock, gh = make_driver(
        timeline, cfg=cfg, reactions=[(1, _reaction("rx-q2", source="codex[bot]"))],
        classify=label_runner("SUBSTANTIVE"),
        fix=lambda c, r: FixOutcome(status="applied"), max_rounds=3,
        quota_llm=quota_llm, answer_waiter=lambda esc, **k: {})
    driver.run()
    assert driver.store.is_excluded("codex")
    assert "codex" not in driver.done           # evicted from done by quota detection
    assert "codex" not in driver._reaction_done  # in sync with done


# ------------------------------------------------- --rr-active restart (F1)
# --rr-active is the RESTART flag. On the restart the preflight snapshot processes a
# responder's pre-existing comments as its round-1 verdict, so it is dropped from round
# 1's summon and poll — and the EXISTING end-of-round rules (not a summon debt) decide
# round 2: a substantive finding is re-requested to verify the fix, a cosmetic one is
# left alone in self.polishing, an approval is done.

def test_rr_active_summons_only_bots_with_no_verdict_in_hand():
    # Round 1 re-requests only the active bots whose verdict is NOT already on the PR.
    # claude left an unresolved finding (its verdict → deferred, fixed instead of asked);
    # copilot left nothing (no verdict → summoned to review this head).
    cfg = {"active_reviewers": ["claude", "copilot"],
           "auto_on_open": {"claude": True, "copilot": True}}
    timeline = [(0, Comment(id="a", text="this null check is missing", source="claude[bot]",
                            path="x.py", diff_hunk="@@ -1 +1 @@"))]
    driver, clock, gh = make_driver(
        timeline, cfg=cfg, classify=label_runner("SUBSTANTIVE"),
        fix=lambda c, r: FixOutcome(status="applied"), rr_active=True, preflight=True,
        max_rounds=2, answer_waiter=lambda esc, **k: {})
    driver.run()
    assert "claude" in driver._preflight_responders   # deferred — verdict already in hand
    assert gh.matching("requested_reviewers")         # copilot (no verdict) IS summoned


def test_rr_active_cosmetic_only_responder_is_not_force_re_reviewed():
    # THE bug the deleted summon-debt caused. A bot whose round-1 (pre-existing) comment
    # is COSMETIC has nothing to fix, so the existing polish rule puts it in
    # self.polishing and LEAVES IT ALONE. The debt used to override that rule and
    # re-summon it — confusing the operator. With no debt the normal rules govern:
    # deferred out of round 1, comment processed with no wait, demoted to polish, and
    # NEVER re-requested — the run finishes in round 1.
    fixed_at = []

    def fix(c, r):
        fixed_at.append(clock.t)
        return FixOutcome(status="applied")

    times = RoundTimes(quiescence=60, poll_interval=30, min_bot_wait=420,
                       idle_timeout=900, max_wait_total=1800, register_delay=60)
    timeline = [(0, Comment(id="a", text="rename tmp for clarity", source="claude[bot]",
                            path="x.py", diff_hunk="@@ -1 +1 @@"))]
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("COSMETIC"), fix=fix,
        rr_active=True, preflight=True, max_rounds=3, times=times,
        answer_waiter=lambda esc, **k: {})
    outcome = driver.run()
    assert "claude" in driver._preflight_responders   # deferred out of round 1 …
    assert fixed_at == [0]                            # … its comment processed with NO wait
    assert "claude" in driver.polishing               # cosmetic → the polish rule owns it
    assert gh.matching("@claude review") == []        # NEVER re-requested (no debt override)
    assert outcome.rounds == 1                         # so the run finishes in round 1


def test_rr_active_failure_placeholder_is_never_an_approval():
    # A failure placeholder states its own zero output ("no comments were posted"),
    # which reads CLEAN to the clean-review detector on its own. It is a RESPONSE,
    # not a review: crowning it "Approved" would satisfy the never-merge-unreviewed
    # gate and auto-merge a PR nobody looked at.
    timeline = [(0, Comment(id="e", text="The review run failed; no comments were posted.",
                            source="claude[bot]", from_issue_channel=True))]
    driver, clock, gh = make_driver(timeline, cfg=CLAUDE_ONLY, rr_active=True,
                                    preflight=True, auto_merge=True)
    outcome = driver.run()
    assert "claude" not in driver.approved        # not a sign-off …
    assert "claude" not in driver.reviewed_ever   # … and not a review
    assert driver.store.is_excluded("claude")     # it is an ERRORED placeholder
    assert outcome.merged is False                # SAFETY gate blocks the merge


def test_rr_active_preflight_resummons_bot_with_only_resolved_comments():
    # The resolved-thread guard. A bot whose pre-existing comments all sit on
    # RESOLVED threads has NO outstanding verdict. Folding them would make it a
    # deferred responder, re-fix its finished comments to "already fixed", demote it
    # to reviewed-no-change, and drop it from re-request for the whole run — the bot
    # would never be asked at all. It must be summoned in round 1 like any other.
    fixed = []

    def fix(c, r):
        fixed.append(c.id)
        return FixOutcome(status="applied")

    threads = FakeThreads().thread("T1", root_comment_id="a", is_resolved=True)
    timeline = [(0, Comment(id="a", text="rename tmp for clarity", source="claude[bot]",
                            path="x.py", diff_hunk="@@ -1 +1 @@"))]
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("COSMETIC"), fix=fix,
        rr_active=True, preflight=True, threads_fetch=threads.fetch,
        resolve_thread=threads.resolve, answer_waiter=lambda esc, **k: {})
    outcome = driver.run()
    assert fixed == []                                   # resolved work is not re-fixed
    assert outcome.actions == []
    assert driver._preflight_responders == set()         # not a responder …
    assert driver._preflight_seen == set()
    assert gh.matching("@claude review")                 # … so round 1 summons it
    assert "claude" not in driver.reviewed_no_change     # never silently dropped


def test_rr_active_preflight_skips_a_reply_in_a_resolved_thread():
    # A resolved thread holds a ROOT finding ("a") AND a follow-up REPLY ("b"), both
    # finished work. resolved_roots used to carry only the root id, so the reply stayed
    # "active": folding it made claude a deferred responder, re-fixed the stale reply to
    # "already fixed", demoted it to reviewed-no-change, and dropped it from round 1's
    # summon — the exact skip this guard exists for. The WHOLE thread's comment ids are
    # now skipped, so claude is summoned normally.
    fixed = []

    def fix(c, r):
        fixed.append(c.id)
        return FixOutcome(status="applied")

    threads = FakeThreads().thread("T1", root_comment_id="a", is_resolved=True,
                                   replies=["b"])
    timeline = [
        (0, Comment(id="a", text="rename tmp for clarity", source="claude[bot]",
                    path="x.py", diff_hunk="@@ -1 +1 @@")),
        (0, Comment(id="b", text="and tmp2 could be clearer too", source="claude[bot]",
                    path="x.py", diff_hunk="@@ -1 +1 @@")),
    ]
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("COSMETIC"), fix=fix,
        rr_active=True, preflight=True, threads_fetch=threads.fetch,
        resolve_thread=threads.resolve, answer_waiter=lambda esc, **k: {})
    outcome = driver.run()
    assert fixed == []                                   # neither root nor reply re-fixed
    assert outcome.actions == []
    assert driver._preflight_responders == set()         # the reply did not make it a responder
    assert driver._preflight_seen == set()
    assert gh.matching("@claude review")                 # … so round 1 summons it
    assert "claude" not in driver.reviewed_no_change     # never silently dropped


def test_rr_active_preflight_deferred_bot_resummoned_in_round2():
    # A deferred bot is NOT folded into done: once round 1's fix lands, round 2
    # re-summons it — on the FIXED head — so it reviews the code its finding produced.
    fix: FixDispatch = lambda c, r: FixOutcome(status="applied")
    timeline = [(0, Comment(id="a", text="this null check is missing", source="claude[bot]",
                            path="x.py", diff_hunk="@@ -1 +1 @@"))]
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"), fix=fix,
        rr_active=True, preflight=True, max_rounds=2,
        answer_waiter=lambda esc, **k: {})
    outcome = driver.run()
    assert outcome.rounds == 2
    assert gh.matching("git", "push")                   # round 1's fix landed first
    assert len(gh.matching("@claude review")) == 1      # summoned in round 2 only
    assert "claude" not in driver.done                  # a finding-poster is never folded


def test_rr_active_defers_verdict_in_hand_bot_at_max_rounds_1():
    # Cross-repo parity (G1): the restart principle "round 1 summons only active bots
    # whose verdict is NOT in hand" carries no round-budget condition. A bot with an unresolved
    # comment already on the PR has a verdict in hand, so at max_rounds == 1 it is
    # deferred — NOT summoned, no re-request fired — and its comment is processed via the
    # restart snapshot, IDENTICAL to the max_rounds >= 2 case. (Summoning here would only
    # re-review the OLD head anyway, so deferring is strictly more efficient, no safety
    # change.)
    times = RoundTimes(quiescence=60, poll_interval=30, min_bot_wait=420,
                       idle_timeout=900, max_wait_total=1800, register_delay=60)
    timeline = [(0, Comment(id="a", text="rename tmp for clarity", source="claude[bot]",
                            path="x.py", diff_hunk="@@ -1 +1 @@"))]
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("COSMETIC"),
        fix=lambda c, r: FixOutcome(status="applied"), rr_active=True, preflight=True,
        max_rounds=1, times=times, answer_waiter=lambda esc, **k: {})
    driver.run()
    assert "claude" in driver._preflight_responders   # the snapshot ran and deferred it …
    assert gh.matching("@claude review") == []        # … so NO summon / re-request fired
    assert gh.matching("requested_reviewers") == []
    assert "a" in driver.processed_ids                # its comment was processed via preflight


def test_rr_active_run_start_fleet_still_full_after_restores(tmp_path, monkeypatch):
    # The SAFETY gate's discriminator must NOT shrink. The run-start fleet is
    # snapshotted BEFORE the approval re-derive / polish restore / preflight fold, so
    # an all-approved + all-polish restart still reads as "reviewers existed and
    # reviewed" (merge), never "no reviewers configured" (quiet skip).
    monkeypatch.setenv(polish_state.STATE_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(round_driver.HEAD_SHA_ENV, "H1")
    polish_state.write_polish_state("7", "o/r", "H1", ["copilot"])
    cfg = {"active_reviewers": ["claude", "copilot"],
           "auto_on_open": {"claude": False, "copilot": True}}
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]",
                            from_issue_channel=True))]
    driver, clock, gh = make_driver(timeline, cfg=cfg, rr_active=True, preflight=True,
                                    auto_merge=True)
    outcome = driver.run()
    assert driver._run_start_fleet == {"claude", "copilot"}   # unchanged by the restores
    assert "claude" in driver.approved                        # re-derived approval
    assert "copilot" in driver.polishing                      # restored polish verdict
    assert driver.reviewed_ever == {"claude", "copilot"}      # both count as reviewed
    assert outcome.merged is True                             # SAFETY gate still passes
    assert gh.matching("@claude review") == []                # nobody re-asked


def test_rr_active_empty_summon_set_with_substantive_fix_goes_to_round2():
    # An empty round-1 summon set (the sole reviewer's finding is deferred) plus a
    # substantive fix must NOT clean-exit-and-merge at round 1 — the existing
    # substantive-progress rule sends it to round 2 so the reviewer reviews the fixed
    # head, exactly as on any run. No --rr-active-specific early exit shortcuts this.
    timeline = [(0, Comment(id="a", text="this null check is missing", source="claude[bot]",
                            path="x.py", diff_hunk="@@ -1 +1 @@"))]
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"),
        fix=lambda c, r: FixOutcome(status="applied"), rr_active=True, preflight=True,
        auto_merge=True, max_rounds=3, answer_waiter=lambda esc, **k: {})
    outcome = driver.run()
    assert outcome.rounds >= 2                     # substantive fix → another round …
    assert gh.matching("@claude review")           # … which summons claude on the fixed head


def test_rr_active_preflight_latest_message_uses_edit_time_not_post_time():
    # A restart's latest-message-wins clean fold must order by EFFECTIVE stamp
    # (updated_at-then-created_at), not raw created_at. claude posts a finding, THEN
    # an LGTM (newer created_at) — but later EDITS the finding, so its updated_at
    # postdates the LGTM. The LGTM is therefore STALE and must not fold claude
    # voluntarily-done: the finding still stands and must be re-verified after the fix.
    timeline = [
        (0, Comment(id="a", text="this null check is missing", source="claude[bot]",
                    path="x.py", diff_hunk="@@ -1 +1 @@",
                    created_at="2026-06-10T00:00:00Z",
                    updated_at="2026-06-10T00:10:00Z")),   # edited AFTER the LGTM below
        (0, Comment(id="b", text="No issues found.", source="claude[bot]",
                    created_at="2026-06-10T00:05:00Z")),
    ]
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, classify=label_runner("SUBSTANTIVE"),
        fix=lambda c, r: FixOutcome(status="applied"), rr_active=True, preflight=True,
        max_rounds=2, answer_waiter=lambda esc, **k: {})
    outcome = driver.run()
    assert "claude" not in driver.approved              # stale LGTM must not fold it done
    assert "claude" not in driver.done
    assert outcome.rounds == 2
    assert gh.matching("git", "push")                   # round 1's fix landed …
    assert len(gh.matching("@claude review")) == 1      # … and claude IS re-summoned to verify it
