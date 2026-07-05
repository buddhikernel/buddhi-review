"""The 2.4 answer-poll, the 2.5 opt-in squash-merge, and the disposition actuators."""
import subprocess

from buddhi_review import merge
from buddhi_review.actuators import act_on_result
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.classify import Classification
from buddhi_review.escalation_wait import wait_for_answer, wait_for_delivered
from buddhi_review.fix_apply import FixOutcome
from buddhi_review.loop import Comment, CommentResult
from buddhi_review.notifier import Ask
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
    def __init__(self, answers):
        self._answers = answers  # ask_id -> [None, None, "2", ...]
        self.cleared = []
        self.sent = []
    def startup_log(self):
        pass
    def send(self, ask):
        self.sent.append(ask)
    def read_answer(self, ask):
        seq = self._answers.get(ask.id, [])
        return seq.pop(0) if seq else None
    def clear(self, ask):
        self.cleared.append(ask.id)


# ---------------------------------------------------------------------------
# 2.4 — answer poll
# ---------------------------------------------------------------------------

def test_wait_for_answer_polls_until_answered():
    clock = FakeClock()
    notifier = FakeNotifier({"q1": [None, None, "2"]})
    ask = Ask(id="q1", question="?", options=["a", "b"])
    got = wait_for_answer(notifier, ask, timeout=60, poll_interval=2,
                          sleep=clock.sleep, clock=clock)
    assert got == "2"
    assert clock.t == 4.0  # two empty polls → two sleeps


def test_wait_for_answer_times_out_none():
    clock = FakeClock()
    notifier = FakeNotifier({})
    ask = Ask(id="q1", question="?")
    got = wait_for_answer(notifier, ask, timeout=5, poll_interval=2,
                          sleep=clock.sleep, clock=clock)
    assert got is None


def test_wait_for_delivered_orders_and_collects():
    clock = FakeClock()

    class KernelAsk:  # the kernel PreReasonedAsk shape the seam translates
        def __init__(self, item_id):
            class Q:
                pass
            self.question = Q()
            self.question.item_id = item_id
            self.question.question = "?"
            self.question.payload = ""
            self.options = []
            self.recommended_index = 0

    esc = ConsoleEscalation(notifier=FakeNotifier({"a": ["1"], "b": [None, "free text"]}))
    esc.delivered = [KernelAsk("a"), KernelAsk("b")]
    answers = wait_for_delivered(esc, timeout=30, poll_interval=2,
                                 sleep=clock.sleep, clock=clock)
    assert answers == {"a": "1", "b": "free text"}


# ---------------------------------------------------------------------------
# 2.5 — opt-in squash-merge + transparency
# ---------------------------------------------------------------------------

def _capture_notices():
    notices = []
    def notice(action, detail="", *, status="do", hint=None, stream=None):
        notices.append((action, detail, status, hint))
        return f"[auto] {action}"
    return notices, notice


def test_merge_disabled_emits_skip_and_never_runs():
    notices, notice = _capture_notices()
    def explode(argv):
        raise AssertionError("gh ran while merge disabled")
    assert merge.squash_merge("9", enabled=False, run=explode, notice=notice) is False
    assert notices == [("squash-merge", "PR #9 left open", "skip", "enable: --auto-merge")]


def test_merge_enabled_runs_gh_and_reports_done():
    notices, notice = _capture_notices()
    argvs = []
    def fake_run(argv, *, cwd=None):
        argvs.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    assert merge.squash_merge("9", repo="o/r", enabled=True, run=fake_run, notice=notice) is True
    assert argvs == [["gh", "pr", "merge", "9", "--squash", "--delete-branch", "-R", "o/r"]]
    assert [n[2] for n in notices] == ["do", "done"]


def test_merge_failure_is_fallback_not_crash():
    notices, notice = _capture_notices()
    def fake_run(argv, *, cwd=None):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not mergeable")
    assert merge.squash_merge("9", enabled=True, run=fake_run, notice=notice) is False
    assert notices[-1][2] == "fallback"
    assert "not mergeable" in notices[-1][1]


# ---------------------------------------------------------------------------
# actuators — disposition → action
# ---------------------------------------------------------------------------

def _result(disposition, label="SUBSTANTIVE"):
    return CommentResult(
        comment_id="c1",
        classification=Classification(label=label),
        kernel_status="X",
        disposition=disposition,
    )


def test_fix_disposition_applied_maps_to_fixed():
    adapter = ReviewAdapter(escalation=ConsoleEscalation(notifier=FakeNotifier({})))
    a = act_on_result(
        Comment(id="c1", text="t"), _result("fix"), adapter=adapter,
        fix_dispatch=lambda c, r: FixOutcome(status="applied"),
    )
    assert a.final == "fixed"


def test_fix_disposition_skip_splits_invalid_vs_already_fixed():
    # A genuine SKIP renders the honest sub-label the fixer stated in its reason:
    # an already-fixed marker → skipped-already-fixed, else skipped-invalid.
    adapter = ReviewAdapter(escalation=ConsoleEscalation(notifier=FakeNotifier({})))
    invalid = act_on_result(
        Comment(id="c1", text="t"), _result("fix"), adapter=adapter,
        fix_dispatch=lambda c, r: FixOutcome(status="skipped",
                                             detail="SKIP: the cited flag is wrong"),
    )
    assert invalid.final == "skipped-invalid"
    already = act_on_result(
        Comment(id="c1", text="t"), _result("fix"), adapter=adapter,
        fix_dispatch=lambda c, r: FixOutcome(status="skipped",
                                             detail="SKIP: already handled upstream"),
    )
    assert already.final == "skipped-already-fixed"
    # An empty/unclassifiable reason defaults to the invalid bucket.
    bare = act_on_result(
        Comment(id="c1", text="t"), _result("fix"), adapter=adapter,
        fix_dispatch=lambda c, r: FixOutcome(status="skipped"),
    )
    assert bare.final == "skipped-invalid"


def test_fix_disposition_reject_is_its_own_label_and_escalates():
    # A verify-REJECT keeps its own honest 'rejected' label (NOT laundered as
    # skipped-invalid) AND surfaces for a human on the fix- rail so it is never
    # silently counted as clean progress toward auto-merge.
    notifier = FakeNotifier({})
    adapter = ReviewAdapter(escalation=ConsoleEscalation(notifier=notifier))
    a = act_on_result(
        Comment(id="c1", text="t"), _result("fix"), adapter=adapter,
        fix_dispatch=lambda c, r: FixOutcome(status="rejected", detail="verify REJECT"),
    )
    assert a.final == "rejected"
    assert notifier.sent and notifier.sent[0].id == "fix-c1"        # the human gets the ask
    assert adapter.escalation.delivered and adapter.escalation.delivered[0].id == "fix-c1"


def test_fix_transient_failure_escalates_not_ladders():
    notifier = FakeNotifier({})
    adapter = ReviewAdapter(escalation=ConsoleEscalation(notifier=notifier))
    a = act_on_result(
        Comment(id="c1", text="t"), _result("fix"), adapter=adapter,
        fix_dispatch=lambda c, r: FixOutcome(status="transient-failed", detail="2 attempts"),
    )
    assert a.final == "escalated"
    assert notifier.sent and notifier.sent[0].id == "fix-c1"  # the human gets the ask


def test_fix_disposition_threads_rollback_failed_flag():
    # act_on_result carries FixOutcome.rollback_failed onto ActionResult for EVERY
    # fix disposition (so the round driver's poisoned-worktree gate can see it);
    # a clean fix (and the non-fix paths) default to False.
    adapter = ReviewAdapter(escalation=ConsoleEscalation(notifier=FakeNotifier({})))
    for status, final in (("applied", "fixed"), ("rejected", "rejected"),
                          ("transient-failed", "escalated")):
        a = act_on_result(
            Comment(id="c1", text="t"), _result("fix"), adapter=adapter,
            fix_dispatch=lambda c, r, s=status: FixOutcome(status=s, rollback_failed=True),
        )
        assert a.final == final and a.rollback_failed is True
    clean = act_on_result(
        Comment(id="c1", text="t"), _result("fix"), adapter=adapter,
        fix_dispatch=lambda c, r: FixOutcome(status="skipped", rollback_failed=False),
    )
    assert clean.final == "skipped-invalid" and clean.rollback_failed is False


def test_fix_without_fixer_defers():
    adapter = ReviewAdapter(escalation=ConsoleEscalation(notifier=FakeNotifier({})))
    a = act_on_result(Comment(id="c1", text="t"), _result("fix"), adapter=adapter)
    assert a.final == "deferred"


def test_non_fix_dispositions_pass_through():
    adapter = ReviewAdapter(escalation=ConsoleEscalation(notifier=FakeNotifier({})))
    cases = {
        "escalate": "escalated",
        "skip": "skipped",
        "defer": "deferred",
        "already-resolved": "already-resolved",
    }
    for disposition, final in cases.items():
        a = act_on_result(Comment(id="c1", text="t"), _result(disposition), adapter=adapter)
        assert a.final == final
