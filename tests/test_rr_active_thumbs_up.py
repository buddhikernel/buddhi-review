"""F1 — an ``--rr-active`` restart never re-asks a reviewer that already approved.

A restart used to re-ping every reviewer that had signed off before the kill: the
sign-off lives on GitHub, but the loop reconstructed nothing, so an approved bot
was summoned, waited on for a register delay, and polled for a full window — for a
verdict already in hand.

The approval is now re-derived live from GitHub, and **the reviewer's LATEST
message wins**. A stale "LGTM" must NOT silence a bot that has since posted real
feedback — including feedback posted as an INLINE review comment, the channel a
conversation-only scan would miss entirely. A bare ``+1`` reaction (the only
signal some reviewers ever emit) folds on its own, but can never outrank a newer
message: reactions carry no timestamp, so a ``+1`` beside a substantive latest
message is not treated as a sign-off.

The ``+1`` fold here is deliberately NOT the round loop's ``_fold_reactions``:
that one reads reactions only, and fails CLOSED until the preflight snapshot has
captured a stale-reaction baseline — which happens after this runs, and which
stamps every pre-existing ``+1`` stale. Reusing it would fold nothing at all;
``test_bare_plus_one_with_no_messages_is_an_approval`` is the proof it was not
reused.
"""
import json
import subprocess

from buddhi_review import gh_ingest
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.fix_apply import FixOutcome
from buddhi_review.loop import Comment
from buddhi_review.round_driver import RoundDriver, RoundTimes
from buddhi_review.seams import ConsoleEscalation

OLD = "2026-07-01T10:00:00Z"
NEW = "2026-07-02T10:00:00Z"

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


class Gh:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, *, cwd=None, timeout=None):
        self.calls.append(list(argv))
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


def _plus_one(source="claude[bot]", rid="rx1"):
    return gh_ingest.Reaction(id=rid, content="+1", source=source)


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


def test_stale_plus_one_beside_a_newer_inline_comment_is_resummoned():
    # A +1 reaction carries no timestamp, so it can never outrank a DATED message.
    # A reviewer whose newest message is substantive is still engaged, +1 or not.
    comments = [
        Comment(id="f", text="this null check is missing", source="claude[bot]",
                path="x.py", diff_hunk="@@ -1 +1 @@", created_at=NEW),
    ]
    driver, clock, gh = make_driver(comments, reactions=[_plus_one()])
    driver.run()
    assert "claude" not in driver.approved
    assert gh.matching(SUMMON)


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
