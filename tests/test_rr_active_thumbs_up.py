"""F1 — an ``--rr-active`` restart never re-asks a reviewer that already approved.

A restart used to re-ping every reviewer that had signed off before the kill: the
sign-off lives on GitHub, but the loop reconstructed nothing, so an approved bot
was summoned, waited on for a register delay, and polled for a full window — for a
verdict already in hand.

The approval is now re-derived live from GitHub, and **the LATEST signal wins**.
A stale "LGTM" must NOT silence a bot that has since posted real feedback —
including feedback posted as an INLINE review comment, the channel a
conversation-only scan would miss entirely. A bare ``+1`` reaction (the only
signal some reviewers ever emit) folds on its own; a ``+1`` posted AFTER a
substantive latest message also folds it — GitHub reactions carry a real
``created_at``, so a fresh +1 is compared against the message's stamp with the
same freshness rule the live ``_fold_reactions`` path applies. A ``+1`` that
is merely a TIE with the message (same instant, or itself undated) does not
outrank it.

The ``+1`` fold here is deliberately NOT the round loop's ``_fold_reactions``:
that one reads reactions only, and fails CLOSED until the preflight snapshot has
captured a stale-reaction baseline — which happens after this runs, and which
stamps every pre-existing ``+1`` stale. Reusing it would fold nothing at all;
``test_bare_plus_one_with_no_messages_is_an_approval`` is the proof it was not
reused.
"""
import json
import subprocess
from datetime import datetime, timezone

from buddhi_review import detectors, gh_ingest
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.fix_apply import FixOutcome
from buddhi_review.loop import Comment
from buddhi_review.round_driver import RoundDriver, RoundTimes
from buddhi_review.seams import ConsoleEscalation

UTC = timezone.utc
# The head this restart meets was committed BEFORE every seeded sign-off below, so
# each one provably post-dates the commit it is credited with reviewing — the F2
# freshness cutoff. (These suites model a restart at the head the reviewers actually
# approved; a sign-off OLDER than the head is the stale case, covered explicitly in
# test_head_aware_merge_gate.)
HEAD_TIME = "2026-06-30T00:00:00+00:00"
OLD = "2026-07-01T10:00:00Z"
NEW = "2026-07-02T10:00:00Z"
EVEN_NEWER = "2026-07-03T10:00:00Z"

CLAUDE_ONLY = {"active_reviewers": ["claude"], "auto_on_open": {"claude": False}}
SUMMON = "@claude review"


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


# The constant local HEAD this restart harness reports. Every restored approval
# and every seeded review anchors to it (staleness is out of scope for these
# tests — the head does not move), so the F2 head-aware gate resolves cleanly.
HEAD_SHA = "restarthead0000"


class Gh:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, *, cwd=None, timeout=None):
        self.calls.append(list(argv))
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "HEAD":
            return subprocess.CompletedProcess(argv, 0, stdout=HEAD_SHA + "\n", stderr="")
        if argv[:3] == ["git", "show", "-s"]:
            # The head's committer date — F2's freshness cutoff for sha-less signals.
            return subprocess.CompletedProcess(argv, 0, stdout=HEAD_TIME + "\n", stderr="")
        if argv[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        out = " M x.py\n" if argv[:3] == ["git", "status", "--porcelain"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    def matching(self, *needles):
        return [c for c in self.calls if all(any(n in a for a in c) for n in needles)]


def make_driver(comments, *, reactions=(), cfg=None, **kw):
    """A restart driver: every comment/reaction is ALREADY on the PR at launch."""
    clock = FakeClock()
    gh = Gh()
    driver = RoundDriver(
        "7", repo="o/r", cwd="/nonexistent", cfg=cfg or CLAUDE_ONLY,
        adapter=ReviewAdapter(escalation=ConsoleEscalation(notifier=FakeNotifier())),
        classify_runner=lambda prompt: json.dumps({"label": "SUBSTANTIVE", "reason": "t"}),
        fix_dispatch=lambda c, r: FixOutcome(status="applied"),
        fetch=lambda pr, repo=None, cwd=None: list(comments),
        reactions_fetch=lambda pr, repo=None, cwd=None: list(reactions),
        # F2 head-aware gate: model the raw pulls/<pr>/reviews payload. Each seeded
        # comment is anchored to the commit it was posted against — this restart
        # harness holds the tip constant, so that is HEAD_SHA for every one. A bare
        # +1 / issue-channel sentinel is sha-less and rides the restore's
        # clean-signal anchor (date-checked against HEAD_TIME) instead.
        reviews_fetch=lambda pr, repo=None, cwd=None: [
            {"user": {"login": c.source}, "commit_id": HEAD_SHA,
             "body": c.text, "state": "COMMENTED"} for c in comments
            if detectors.bot_for_login(c.source) is not None],
        inline_fetch=lambda pr, repo=None, cwd=None: [],
        threads_fetch=lambda pr, repo=None, cwd=None: [],
        resolve_thread=lambda thread_id, cwd=None: True,
        gh_run=gh, clock=clock, sleep=clock.sleep, notice=lambda *a, **k: "",
        times=RoundTimes(quiescence=60, poll_interval=30, min_bot_wait=60,
                         idle_timeout=120, max_wait_total=600, register_delay=0),
        answer_waiter=lambda esc, **k: {},
        rr_active=True, preflight=True, max_rounds=2,
        **kw,
    )
    return driver, clock, gh


def _plus_one(source="claude[bot]", rid="rx1", created_at=NEW):
    # created_at defaults FRESH (post-dates HEAD_TIME): a bare +1 is the sign-off
    # that carries neither a commit_id nor a message, so its reaction timestamp is
    # the only thing that can date-anchor it for the head-aware gate. Pass
    # created_at=OLD (or None) to model the stale / undated — unanchorable — case.
    return gh_ingest.Reaction(id=rid, content="+1", source=source,
                              created_at=created_at)


def _lgtm(cid="ok", when=OLD, **kw):
    return Comment(id=cid, text="LGTM", source="claude[bot]", created_at=when, **kw)


# ---------------------------------------------------------------------------
# Latest message wins — a stale sign-off never silences an engaged reviewer
# ---------------------------------------------------------------------------

def test_stale_lgtm_then_newer_review_body_is_resummoned():
    # The sign-off is old news: the reviewer's newest message is a top-level review
    # body carrying real feedback, so it is still engaged and must be re-asked.
    comments = [
        _lgtm(cid="ok", when=OLD, from_issue_channel=True),
        Comment(id="f", text="this null check is missing", source="claude[bot]",
                created_at=NEW),
    ]
    driver, clock, gh = make_driver(comments)
    driver.run()
    assert "claude" not in driver.approved      # the stale LGTM did NOT fold it
    assert gh.matching(SUMMON)                  # it is asked again


def test_stale_lgtm_on_the_review_body_channel_is_not_work_and_not_a_verdict():
    # A review-level "LGTM" lands on the REVIEW-BODY channel (from_issue_channel is
    # False), which the issue-channel drop does not cover. A superseded sign-off must
    # be dropped outright — not merely barred from folding. Left actionable, it would
    # be dispatched to the fixer, make its author a preflight responder (dropped from
    # round 1's summon AND poll), and then be promoted back into `done` as a
    # zero-finding review — silencing, on the strength of the stale LGTM itself, the
    # reviewer whose newest message says it is NOT finished.
    comments = [
        _lgtm(cid="ok", when=OLD),                       # review body, NOT issue channel
        Comment(id="f", text="I need another look at the retry path here.",
                source="claude[bot]", created_at=NEW, from_issue_channel=True),
    ]
    driver, clock, gh = make_driver(comments, auto_merge=True)
    outcome = driver.run()
    assert outcome.actions == []                         # the stale LGTM is not work
    assert "claude" not in driver._preflight_responders  # … and not a verdict
    assert "claude" not in driver.done and "claude" not in driver.approved
    assert gh.matching(SUMMON)                           # summoned in round 1
    assert outcome.merged is False                       # nothing reviewed this head


def test_stale_lgtm_then_newer_pr_comment_is_resummoned():
    # Same, on the PR-conversation channel: the newest message is not a clean review,
    # so no sign-off is preserved (and this bot never posted a finding, so round 1
    # summons it directly).
    comments = [
        _lgtm(cid="ok", when=OLD, from_issue_channel=True),
        Comment(id="f", text="I need another look at the retry path here.",
                source="claude[bot]", created_at=NEW, from_issue_channel=True),
    ]
    driver, clock, gh = make_driver(comments)
    driver.run()
    assert "claude" not in driver.approved
    assert gh.matching(SUMMON)


def test_stale_lgtm_then_newer_inline_comment_is_resummoned():
    # The channel a conversation-only scan would miss: the newest message is an
    # INLINE review comment (pulls/<n>/comments). It is substantive, so the stale
    # LGTM must not fold the reviewer.
    comments = [
        _lgtm(cid="ok", when=OLD, from_issue_channel=True),
        Comment(id="f", text="this null check is missing", source="claude[bot]",
                path="x.py", diff_hunk="@@ -1 +1 @@", created_at=NEW),
    ]
    driver, clock, gh = make_driver(comments)
    driver.run()
    assert "claude" not in driver.approved
    assert "claude" not in driver.done
    assert gh.matching(SUMMON)                  # re-summoned (round 2, on the fixed head)


def test_same_instant_lgtm_beside_a_finding_does_not_fold_the_bot():
    # A finding wins a TIE, not just a newer stamp. One review submission stamps its
    # body and its inline comments with the SAME second, and inline comments are fetched
    # before the body — so a "LGTM" summary body reaches the fold alongside a real
    # same-submission finding. _supersedes reads the equal instants as a tie (False), so
    # the strictly-newer rule cannot demote the body; the same-submission finding has to.
    # Otherwise the finding is fixed in round 1 but its author is crowned done, dropped
    # from expected_bots(), and the fixed head is never re-requested — the stale approval
    # then satisfies the auto-merge review gate.
    comments = [
        Comment(id="f", text="this null check is missing", source="claude[bot]",
                path="x.py", diff_hunk="@@ -1 +1 @@", created_at=NEW),  # inline finding
        _lgtm(cid="ok", when=NEW),                                       # same-instant body
    ]
    driver, clock, gh = make_driver(comments, auto_merge=True)
    driver.run()
    assert "claude" not in driver.approved      # the same-instant LGTM did NOT fold it …
    assert "claude" not in driver.done
    assert "claude" in driver._preflight_responders  # its finding is the verdict in hand
    assert gh.matching(SUMMON)                  # … re-summoned to verify the fixed head


def test_stale_plus_one_beside_a_newer_inline_comment_is_resummoned():
    # The +1 PRE-dates the reviewer's substantive latest message (a tie would lose
    # too — _supersedes requires STRICTLY newer). A reviewer whose newest message
    # is substantive, and whose +1 does not post-date it, is still engaged.
    comments = [
        Comment(id="f", text="this null check is missing", source="claude[bot]",
                path="x.py", diff_hunk="@@ -1 +1 @@", created_at=NEW),
    ]
    driver, clock, gh = make_driver(comments, reactions=[_plus_one(created_at=OLD)])
    driver.run()
    assert "claude" not in driver.approved
    assert gh.matching(SUMMON)


def test_fresh_plus_one_after_a_finding_outranks_it_and_is_not_resummoned():
    # The reviewer posted a finding, then — after the fix landed — reacted with a
    # bare +1 STRICTLY AFTER that finding's timestamp. GitHub reactions carry a
    # real created_at, so this +1 is a genuine later verdict: the same freshness
    # rule the live _fold_reactions path applies to every fresh +1 (fold it as
    # approval regardless of what the bot said before) must also apply on an
    # --rr-active restart, or the verdict already in hand is discarded and the
    # bot is re-summoned and re-polled for a fix it already approved.
    comments = [
        Comment(id="f", text="this null check is missing", source="claude[bot]",
                path="x.py", diff_hunk="@@ -1 +1 @@", created_at=OLD),
    ]
    driver, clock, gh = make_driver(comments, reactions=[_plus_one(created_at=NEW)],
                                    auto_merge=True)
    outcome = driver.run()
    assert "claude" in driver.done and "claude" in driver.approved
    assert gh.matching(SUMMON) == []             # never re-asked
    assert outcome.merged is True


# ---------------------------------------------------------------------------
# A live sign-off IS preserved — the reviewer is not re-asked
# ---------------------------------------------------------------------------

def test_latest_message_is_the_approval_so_the_bot_is_skipped():
    # Findings first, then the reviewer's own sign-off after the fix landed: the
    # LATEST message is the clean review, so the verdict stands. The bot is folded
    # voluntarily-done — never summoned, never polled — and still counts as having
    # reviewed, so the PR merges.
    comments = [
        Comment(id="f", text="this null check is missing", source="claude[bot]",
                path="x.py", diff_hunk="@@ -1 +1 @@", created_at=OLD),
        _lgtm(cid="ok", when=NEW, from_issue_channel=True),
    ]
    driver, clock, gh = make_driver(comments, auto_merge=True)
    outcome = driver.run()
    assert "claude" in driver.done and "claude" in driver.approved
    assert "claude" in driver.reviewed_ever
    assert gh.matching(SUMMON) == []            # never re-asked
    assert outcome.merged is True


def test_bare_plus_one_with_no_messages_is_an_approval():
    # The sign-off of a reviewer that posts NO message at all (a bare +1 on the PR
    # body). It must fold — and this is also the proof that the round loop's
    # _fold_reactions was NOT reused here: that one fails closed while the reaction
    # baseline is unset, which is exactly the state before the preflight snapshot
    # captures it, so reusing it would fold nothing.
    cfg = {"active_reviewers": ["codex"], "auto_on_open": {"codex": False}}
    driver, clock, gh = make_driver([], reactions=[_plus_one(source="codex[bot]")],
                                    cfg=cfg, auto_merge=True)
    outcome = driver.run()
    assert "codex" in driver.done and "codex" in driver.approved
    assert "codex" in driver.reviewed_ever
    assert driver.expected_bots() == []         # nothing left to ask
    assert outcome.merged is True
    assert clock.t == 0                          # no poll window was ever opened


def test_a_stale_bare_plus_one_is_folded_but_never_merges():
    # F2: the SAME fold — codex is still approved and never re-asked — but the +1
    # PRE-dates the commit now at HEAD, so it approved an EARLIER head. It cannot be
    # anchored to the head being merged, and the head-aware gate hands the PR back
    # rather than landing a commit nobody demonstrably reviewed.
    cfg = {"active_reviewers": ["codex"], "auto_on_open": {"codex": False}}
    stale = _plus_one(source="codex[bot]", created_at="2026-06-29T00:00:00Z")  # < HEAD_TIME
    driver, clock, gh = make_driver([], reactions=[stale], cfg=cfg, auto_merge=True)
    outcome = driver.run()
    assert "codex" in driver.approved            # the verdict is still in hand …
    assert driver.expected_bots() == []          # … so it is not re-summoned
    assert driver._clean_signal_head == {}       # … but it anchors to no head
    assert outcome.merged is False               # → handback, not an unreviewed merge
    assert gh.matching("gh", "merge", "--squash") == []


def test_an_undated_bare_plus_one_is_unanchorable_and_never_merges():
    # A +1 whose payload carried no timestamp cannot be dated against ANY head →
    # unanchorable → fail closed.
    cfg = {"active_reviewers": ["codex"], "auto_on_open": {"codex": False}}
    undated = _plus_one(source="codex[bot]", created_at=None)
    driver, clock, gh = make_driver([], reactions=[undated], cfg=cfg, auto_merge=True)
    outcome = driver.run()
    assert "codex" in driver.approved
    assert driver._clean_signal_head == {}
    assert outcome.merged is False


def test_a_bot_with_no_signal_at_all_is_still_summoned():
    # The control: no message, no reaction → no verdict in hand → summon as usual.
    driver, clock, gh = make_driver([])
    driver.run()
    assert "claude" not in driver.approved
    assert gh.matching(SUMMON)


def test_a_plus_one_never_signs_off_when_the_messages_could_not_be_read():
    # Fail-CLOSED. If the comment read errors, "this bot posted no message" is
    # IGNORANCE, not evidence — the unread message could be a failure placeholder or
    # a fresh finding. A +1 must not crown the reviewer "Approved" on that basis (it
    # would satisfy the never-merge-unreviewed gate and merge a PR nobody reviewed).
    reads = []

    def flaky(pr, repo=None, cwd=None):
        reads.append(1)
        if len(reads) == 1:
            raise RuntimeError("gh api failed")   # the re-derive's read — transient
        return []                                  # the poll's reads succeed

    driver, clock, gh = make_driver([], reactions=[_plus_one()], auto_merge=True)
    driver.fetch = flaky
    outcome = driver.run()
    assert "claude" not in driver.approved        # no sign-off on an unread PR
    assert "claude" not in driver.reviewed_ever
    assert gh.matching(SUMMON)                     # re-summoned instead
    assert outcome.merged is False


def test_a_quota_placeholder_the_model_tier_catches_is_never_an_approval():
    # A real quota death ("I've used all of my requests … so no comments were
    # generated") is missed by the deterministic regexes but caught by the quota model
    # tier. The sign-off check must read it with the SAME context the poll uses — a
    # blinder detector here would crown a reviewer the preflight snapshot then
    # hard-excludes, and that stale crown would satisfy the never-merge gate.
    quota_text = ("I've used all of my premium requests for this month, "
                  "so no comments were generated.")
    comments = [Comment(id="q", text=quota_text, source="claude[bot]",
                        created_at=OLD, from_issue_channel=True)]
    driver, clock, gh = make_driver(comments, auto_merge=True,
                                    quota_llm=lambda prompt: {"quota": True})
    outcome = driver.run()
    assert "claude" not in driver.approved         # a placeholder is not a sign-off
    assert "claude" not in driver.done
    assert "claude" not in driver.reviewed_ever    # … and not a review
    assert driver.store.is_excluded("claude")      # it is excluded, quota-exhausted
    assert outcome.merged is False                 # SAFETY gate blocks the merge


def test_an_undated_stale_sign_off_is_still_superseded_by_a_dated_finding():
    # GitHub always stamps, but a degraded payload can carry no timestamp. An UNDATED
    # sign-off must not outrank a DATED finding: it is the same "latest message wins"
    # rule, and the undated side is the one that must yield — otherwise the stale LGTM
    # folds the reviewer voluntarily-done and its real finding is never re-reviewed.
    comments = [
        Comment(id="ok", text="No issues found.", source="claude[bot]",
                from_issue_channel=True, created_at=None),          # undated sign-off
        Comment(id="f", text="this null check is missing", source="claude[bot]",
                path="x.py", diff_hunk="@@ -1 +1 @@", created_at=NEW),
    ]
    driver, clock, gh = make_driver(comments, auto_merge=True)
    driver.run()
    assert "claude" not in driver.approved     # the undated LGTM did NOT fold it …
    assert "claude" not in driver.done
    assert gh.matching(SUMMON)                 # … it is asked about the fixed head


def test_a_bare_plus_one_sign_off_is_still_re_checked_for_quota():
    # A bare +1 folds without ANY of the bot's text having been quota-checked (it
    # posted none). If it later posts a novel-wording quota message, the
    # between-rounds re-check must still be able to evict the sign-off — otherwise a
    # quota-dead reviewer rides its stale "Approved" into the merge gate. This is the
    # same reaction-done bookkeeping the poll's own +1 fold does.
    cfg = {"active_reviewers": ["codex", "claude"],
           "auto_on_open": {"codex": False, "claude": False}}
    novel = "My allotment for the cycle is spent; I will resume next period."
    comments = [Comment(id="q", text=novel, source="codex[bot]", created_at=NEW,
                        from_issue_channel=True)]
    driver, clock, gh = make_driver(
        comments, reactions=[_plus_one(source="codex[bot]")], cfg=cfg, auto_merge=True,
        quota_llm=lambda prompt: {"quota": novel in prompt})
    # The +1 arrives with no message from codex at restore time …
    driver.fetch = lambda pr, repo=None, cwd=None: []
    driver._run_start_fleet = set(driver.expected_bots())
    driver._rederive_prior_approvals()
    assert "codex" in driver.done and "codex" in driver._reaction_done   # …folded, and re-checkable
    # … and its later quota message evicts the sign-off rather than riding it.
    driver.fetch = lambda pr, repo=None, cwd=None: list(comments)
    driver._recheck_quota_between_rounds(comments)
    assert driver.store.is_excluded("codex")
    assert "codex" not in driver.done and "codex" not in driver.approved
    assert "codex" not in driver.reviewed_ever


# ---------------------------------------------------------------------------
# A live rate-limit marker that post-dates a re-derived approval un-crowns it
# ---------------------------------------------------------------------------

def test_a_live_rate_limit_marker_supersedes_a_stale_approval_and_blocks_merge():
    # claude approved an EARLIER head; another reviewer's fix then advanced the PR and
    # claude, re-requested, hit the usage limit — github-actions[bot] posts a rate-limit
    # marker NEWER than the approval. The re-derived approval is for a head claude never
    # re-reviewed, so it must NOT ride reviewed_ever into the auto-merge gate: claude is
    # un-crowned and released rate-limited, and the claude-only fleet refuses to merge.
    reset = datetime(2026, 7, 4, 13, 0, tzinfo=UTC)
    before = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    marker = Comment(
        id="m", from_issue_channel=True, source="github-actions[bot]", created_at=NEW,
        text=("<!-- claude-review-unavailable-v1 type=rate_limited "
              f"resets_at={int(reset.timestamp())} -->"))
    comments = [
        _lgtm(cid="ok", when=OLD, from_issue_channel=True),   # stale approval (earlier head)
        marker,                                                # newer: usage limit on re-request
    ]
    driver, clock, gh = make_driver(comments, auto_merge=True, wall_clock=lambda: before)
    outcome = driver.run()
    assert "claude" not in driver.approved          # stale approval un-crowned …
    assert "claude" not in driver.done
    assert "claude" not in driver.reviewed_ever      # … never counted as a review
    assert "claude" in driver._rate_limited_until    # released rate-limited instead
    assert gh.matching("gh", "merge", "--squash") == []
    assert outcome.merged is False                   # SAFETY gate blocks the merge


def test_a_post_reset_approval_after_a_stale_marker_still_folds_and_merges():
    # The inverse: the marker's window has already RESET (its resets_at is in the past),
    # and claude has since re-reviewed and approved. The stale marker must NOT un-crown
    # that genuine approval — a released reviewer that came back merges as usual.
    reset = datetime(2026, 7, 4, 13, 0, tzinfo=UTC)
    after = datetime(2026, 7, 4, 14, 0, tzinfo=UTC)      # the window already reset
    marker = Comment(
        id="m", from_issue_channel=True, source="github-actions[bot]", created_at=OLD,
        text=("<!-- claude-review-unavailable-v1 type=rate_limited "
              f"resets_at={int(reset.timestamp())} -->"))
    comments = [
        marker,                                            # older: a since-reset limit
        _lgtm(cid="ok", when=NEW, from_issue_channel=True),  # newer: the genuine approval
    ]
    driver, clock, gh = make_driver(comments, auto_merge=True, wall_clock=lambda: after)
    outcome = driver.run()
    assert "claude" in driver.approved and "claude" in driver.reviewed_ever
    assert "claude" not in driver._rate_limited_until   # the stale marker was ignored
    assert outcome.merged is True


def test_a_stale_unknown_reset_marker_does_not_uncrown_a_later_approval():
    # An UNKNOWN-reset marker (resets_at=0 / no reset time) has no window for the
    # stale-window check above to test, so an OLDER one that pre-dates a later genuine
    # approval used to un-crown it anyway — dropping reviewed_ever and blocking the
    # merge a real review earned. The recency guard now compares the marker's post
    # time against the re-derived sign-off: a marker older than the approval is stale
    # history (claude came back and approved after the limit cleared) and is ignored.
    marker = Comment(
        id="m", from_issue_channel=True, source="github-actions[bot]", created_at=OLD,
        text="<!-- claude-review-unavailable-v1 type=rate_limited resets_at=0 -->")
    comments = [
        marker,                                              # older: unknown-reset limit
        _lgtm(cid="ok", when=NEW, from_issue_channel=True),  # newer: the genuine approval
    ]
    driver, clock, gh = make_driver(comments, auto_merge=True)
    outcome = driver.run()
    assert "claude" in driver.approved and "claude" in driver.reviewed_ever
    assert "claude" not in driver._rate_limited_until   # the stale marker was ignored
    assert gh.matching(SUMMON) == []                    # never re-asked
    assert outcome.merged is True


def test_a_newer_unknown_reset_marker_still_uncrowns_a_stale_approval():
    # The guard is RECENCY-aware, not a blanket skip: an unknown-reset marker that
    # POST-DATES the sign-off is still live (claude was re-requested after approving an
    # earlier head and hit the limit), so it must un-crown as before. SAFETY: the
    # claude-only fleet must not ride a stale approval into the merge gate.
    marker = Comment(
        id="m", from_issue_channel=True, source="github-actions[bot]", created_at=NEW,
        text="<!-- claude-review-unavailable-v1 type=rate_limited resets_at=0 -->")
    comments = [
        _lgtm(cid="ok", when=OLD, from_issue_channel=True),  # stale approval (earlier head)
        marker,                                              # newer: usage limit on re-request
    ]
    driver, clock, gh = make_driver(comments, auto_merge=True)
    outcome = driver.run()
    assert "claude" not in driver.approved          # stale approval un-crowned …
    assert "claude" not in driver.done
    assert "claude" not in driver.reviewed_ever     # … never counted as a review
    assert gh.matching("gh", "merge", "--squash") == []
    assert outcome.merged is False                  # SAFETY gate blocks the merge


# ---------------------------------------------------------------------------
# A stale finding under the SAME reviewer's newer approval is not re-fixed
# ---------------------------------------------------------------------------

def test_a_stale_finding_under_a_newer_approval_is_not_re_dispatched():
    # An older inline finding + a NEWER clean approval from the SAME reviewer. Its latest
    # verdict has withdrawn the finding, so the restart must NOT re-dispatch the fixer
    # against it: `superseded` gates only the clean-review branch of _classify_signal, so
    # without the preflight drop the stale finding falls through as actionable and re-fixes
    # feedback already in hand.
    fixed = []
    comments = [
        Comment(id="f", text="this null check is missing", source="claude[bot]",
                path="x.py", diff_hunk="@@ -1 +1 @@", created_at=OLD),   # older finding
        _lgtm(cid="ok", when=NEW, from_issue_channel=True),              # newer approval
    ]
    driver, clock, gh = make_driver(comments, auto_merge=True)
    driver.fix_dispatch = lambda c, r: fixed.append(c.id) or FixOutcome(status="applied")
    outcome = driver.run()
    assert fixed == []                          # the stale finding was NOT re-fixed
    assert "claude" in driver.approved          # its newer approval stands …
    assert gh.matching(SUMMON) == []            # … so it is not re-summoned
    assert outcome.merged is True               # a genuine review happened → merge


def test_a_finding_edited_after_a_newer_approval_is_still_re_dispatched():
    # The finding's ORIGINAL post predates the approval (so created_at alone would call
    # it stale), but the reviewer EDITED it after posting the approval — updated_at is
    # the newest stamp of all. The edit postdates the sign-off, so it must still be
    # re-fixed rather than dropped as a stale finding under a "newer" approval that is
    # actually older than the edit.
    fixed = []
    comments = [
        Comment(id="f", text="this null check is missing too", source="claude[bot]",
                path="x.py", diff_hunk="@@ -1 +1 @@", created_at=OLD, updated_at=EVEN_NEWER),
        _lgtm(cid="ok", when=NEW, from_issue_channel=True),   # approval predates the edit
    ]
    driver, clock, gh = make_driver(comments, auto_merge=True)
    driver.fix_dispatch = lambda c, r: fixed.append(c.id) or FixOutcome(status="applied")
    driver.run()
    assert fixed == ["f"]                       # the edited finding WAS re-fixed
