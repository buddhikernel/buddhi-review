"""F1 — a killed run's polish-only verdict survives the restart.

A reviewer whose round posted only non-substantive comments is dropped from
re-request for the rest of the run. That verdict lived in-process only: kill the
loop between rounds and an ``--rr-active`` restart re-summoned the reviewer,
burning a summon + a register delay + a full poll window on a bot with nothing
left to say (whose comments the poll then re-ingested anyway).

The verdict is now stamped per (repo, PR) against the tip the round PUSHED, and
restored only while the PR's live HEAD still equals that tip. The tip is the
POST-fix one deliberately: a polish-only reviewer is sticky *within* a run (never
re-summoned even as later fixes advance HEAD), so stamping the head the loop
carries into the next round — and restoring only there — reproduces exactly that
stickiness across a restart. A PRE-fix stamp would never match the head a restart
meets, and would re-summon the very reviewers this exists to skip.

Everything here is fail-CLOSED: an unknown tip (on write or on restore), a moved
HEAD, or a corrupt / absent / torn state file restores NOTHING and the reviewer is
simply summoned again.
"""
import json
import os
import subprocess

import pytest

from buddhi_review import polish_state, round_driver
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.fix_apply import FixOutcome
from buddhi_review.loop import Comment
from buddhi_review.round_driver import RoundDriver, RoundTimes
from buddhi_review.seams import ConsoleEscalation

PR = "7"
REPO = "o/r"
FLEET = {"active_reviewers": ["claude", "copilot"],
         "auto_on_open": {"claude": False, "copilot": True}}

# claude's finding is substantive (its fix advances HEAD); copilot's is cosmetic
# (nothing to fix → polish-only). One mixed round, exactly like the live case.
SUBSTANTIVE = Comment(id="s", text="this null check is missing", source="claude[bot]",
                      path="x.py", diff_hunk="@@ -1 +1 @@")
COSMETIC = Comment(id="c", text="rename tmp for clarity", source="copilot[bot]",
                   path="y.py", diff_hunk="@@ -2 +2 @@")


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


class GhHead:
    """Records every spawn and models the PR's moving tip: ``gh api …/pulls/<n>
    -q .head.sha`` answers with the CURRENT head, and a ``git push`` advances it
    (H0 → H1) exactly as a real fix push does. ``kill_on`` makes one spawn raise —
    the loop being killed mid-run."""

    def __init__(self, head="H0", advance_to="H1", kill_on=None, kill_after=0,
                 head_fails=False, name_with_owner=None):
        self.calls = []
        self.head = head
        self.advance_to = advance_to
        self.kill_on = kill_on           # substring of the spawn that kills the loop
        self.kill_after = kill_after     # …but let this many of them through first
        self.head_fails = head_fails     # the tip is unreadable (gh error)
        # The `gh repo view` answer for a repo-less invocation's owner/repo
        # inference; None models a cwd with no readable GitHub remote.
        self.name_with_owner = name_with_owner

    def __call__(self, argv, *, cwd=None, timeout=None):
        argv = list(argv)
        self.calls.append(argv)
        if self.kill_on and any(self.kill_on in a for a in argv):
            if self.kill_after <= 0:
                raise KeyboardInterrupt("loop killed")
            self.kill_after -= 1
        out = ""
        if argv[:3] == ["git", "status", "--porcelain"]:
            out = " M x.py\n"
        elif ".head.sha" in argv:
            if self.head_fails:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
            out = self.head + "\n"
        elif argv[:3] == ["gh", "repo", "view"]:
            if self.name_with_owner is None:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no remote")
            out = self.name_with_owner + "\n"
        if argv[:2] == ["git", "push"]:
            self.head = self.advance_to
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    def matching(self, *needles):
        return [c for c in self.calls if all(any(n in a for a in c) for n in needles)]


def classify(prompt):
    """Per-comment labels: the null-check finding is real work, the retention-policy
    comment is a question for a human, the rename is neither. The markers are matched
    against the whole classifier PROMPT, so they must be phrases the prompt's own
    guidance cannot contain."""
    if "retention policy" in prompt:
        label = "BUSINESS_QUESTION"
    elif "null check" in prompt:
        label = "SUBSTANTIVE"
    else:
        label = "COSMETIC"
    return json.dumps({"label": label, "reason": "t"})


def make_driver(timeline, *, gh, cfg=None, clock=None, repo=REPO, **kw):
    clock = clock or FakeClock()

    def fetch(pr, repo=None, cwd=None):
        return [c for t, c in timeline if t <= clock.t]

    kw.setdefault("preflight", False)
    kw.setdefault("threads_fetch", lambda pr, repo=None, cwd=None: [])
    kw.setdefault("resolve_thread", lambda thread_id, cwd=None: True)
    kw.setdefault("answer_waiter", lambda esc, **k: {})
    kw.setdefault("fix_dispatch", lambda c, r: FixOutcome(status="applied"))
    driver = RoundDriver(
        PR, repo=repo, cwd="/nonexistent", cfg=cfg or FLEET,
        adapter=ReviewAdapter(escalation=ConsoleEscalation(notifier=FakeNotifier())),
        classify_runner=classify,
        fetch=fetch, reactions_fetch=lambda pr, repo=None, cwd=None: [],
        gh_run=gh, clock=clock, sleep=clock.sleep, notice=lambda *a, **k: "",
        times=RoundTimes(quiescence=60, poll_interval=30, min_bot_wait=420,
                         idle_timeout=900, max_wait_total=1800, register_delay=0),
        **kw,
    )
    return driver, clock


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    """Every test gets its own polish-state home — never the operator's cache."""
    monkeypatch.setenv(polish_state.STATE_DIR_ENV, str(tmp_path / "polish-state"))
    return tmp_path


def _killed_mixed_round():
    """Run 1: a mixed round (polish bot + substantive bot), whose fix pushes
    H0 → H1, killed at the round-2 summon. Returns the driver + its gh recorder."""
    # Round 1 summons claude; the loop is killed at the ROUND-2 re-summon, i.e.
    # after round 1 recorded its polish verdict and pushed its fix.
    gh = GhHead(head="H0", advance_to="H1", kill_on="@claude review", kill_after=1)
    driver, clock = make_driver([(0, SUBSTANTIVE), (0, COSMETIC)], gh=gh, max_rounds=3)
    with pytest.raises(KeyboardInterrupt):
        driver.run()
    return driver, gh


# ---------------------------------------------------------------------------
# The anchor: a mixed round, killed, restarted
# ---------------------------------------------------------------------------

def test_mixed_round_polish_verdict_survives_a_kill_and_restart():
    driver1, gh1 = _killed_mixed_round()
    assert driver1.polishing == {"copilot"}          # round 1's polish-only verdict
    assert gh1.head == "H1"                          # the fix push advanced the tip

    state = polish_state.read_polish_state(PR, REPO)
    assert state is not None
    assert state["bots"] == ["copilot"]
    # The POST-fix tip — the head a restart actually meets. A pre-fix stamp (H0)
    # would never match it, and every reviewer this feature skips would be re-asked.
    assert state["tip_sha"] == "H1" and state["tip_sha"] != "H0"

    # ── the restart, at the live head the killed run left behind ──────────────
    gh2 = GhHead(head="H1", advance_to="H2")
    driver2, clock2 = make_driver([(0, SUBSTANTIVE), (0, COSMETIC)], gh=gh2,
                                  rr_active=True, preflight=True, auto_merge=True,
                                  max_rounds=3)
    outcome = driver2.run()
    assert "copilot" in driver2.polishing                  # verdict restored …
    assert gh2.matching("requested_reviewers") == []       # … so it is never re-asked
    assert "copilot" in driver2.reviewed_ever              # and it still counts as reviewed
    assert outcome.merged is True                          # SAFETY gate satisfied → merge
    # A merged PR's state is dead — the file is dropped.
    assert not os.path.exists(polish_state.state_path(PR, REPO))


# ---------------------------------------------------------------------------
# A restored polish verdict must not outlive a NEW real finding on the SAME
# stamped commit — a reviewer can post a later substantive comment after the
# state file was written but before HEAD moves again.
# ---------------------------------------------------------------------------

def test_new_finding_evicts_a_restored_polish_verdict_so_the_fix_is_reverified():
    # copilot's polish verdict was stamped at H1 by a prior run, but it later
    # posted a REAL finding on that same commit — after the stamp was written —
    # which is exactly what the restart preflight now finds sitting on the PR.
    # The restart must process that finding as this round's real work, and once
    # its fix lands copilot must be evicted from self.polishing so the next
    # round re-requests it to verify the fix, rather than staying invisible to
    # expected_bots() for the rest of the run.
    cfg = {"active_reviewers": ["copilot"], "auto_on_open": {"copilot": True}}
    polish_state.write_polish_state(PR, REPO, "H1", ["copilot"])
    new_finding = Comment(id="c2", text="this null check is missing",
                          source="copilot[bot]", path="y.py", diff_hunk="@@ -5 +5 @@")
    gh = GhHead(head="H1", advance_to="H2")
    driver, clock = make_driver([(0, new_finding)], gh=gh, cfg=cfg,
                                rr_active=True, preflight=True, max_rounds=3)
    driver.run()
    assert "copilot" not in driver.polishing         # the new finding evicted the stale verdict
    assert gh.head == "H2"                           # the fix was applied and pushed
    # Round 1 defers copilot (it already spoke at preflight) but round 2 re-requests
    # it to verify the fix — proof it is no longer stuck in self.polishing, which
    # would have filtered it out of expected_bots() for the rest of the run.
    assert gh.matching("requested_reviewers")


# ---------------------------------------------------------------------------
# The "killed AFTER the push, BEFORE verification" restart: the prior run fixed
# and pushed a finding but died before the reviewer re-reviewed and before its
# thread was resolved. On the restart the finding is still on the PR with an
# unresolved thread, so it is re-processed — and the fixer, seeing the fix already
# in the tree, reports `skipped-already-fixed`. That must NOT fold the reviewer to
# reviewed-no-change and merge the unverified fix: the reviewer is re-requested to
# verify the fixed head first.
# ---------------------------------------------------------------------------

def test_restart_reverifies_a_finding_whose_fix_the_killed_run_already_pushed():
    cfg = {"active_reviewers": ["copilot"], "auto_on_open": {"copilot": True}}
    finding = Comment(id="f1", text="this null check is missing",
                      source="copilot[bot]", path="y.py", diff_hunk="@@ -5 +5 @@")
    gh = GhHead(head="H1", advance_to="H2")               # fix already at HEAD; no push
    driver, clock = make_driver(
        [(0, finding)], gh=gh, cfg=cfg, rr_active=True, preflight=True,
        auto_merge=True, max_rounds=3,
        fix_dispatch=lambda c, r: FixOutcome(status="skipped", detail="already fixed"),
    )
    outcome = driver.run()
    # The already-fixed finding is NOT a dismissal on the restart — its reviewer
    # never confirmed the pushed fix, so it keeps its re-request slot …
    assert "copilot" not in driver.reviewed_no_change
    # … and round 1 does not clean-exit: round 2 re-requests copilot to verify.
    assert gh.matching("requested_reviewers")
    # Once verified (copilot posts nothing new → clean), the PR merges — the fix
    # inserts one verification round, it does not wedge the auto-restart.
    assert outcome.merged is True


def test_reverify_pending_at_the_round_budget_hands_back_instead_of_merging():
    # Same restart, but --max-rounds 1: there is NO round left to verify the already-
    # pushed fix. The forced-verify `continue` at the last round would only end the
    # for-loop and fall through to the budget-reached clean exit, auto-merging a fix no
    # reviewer ever confirmed. Instead the run must hand back — a re-run then verifies.
    cfg = {"active_reviewers": ["copilot"], "auto_on_open": {"copilot": True}}
    finding = Comment(id="f1", text="this null check is missing",
                      source="copilot[bot]", path="y.py", diff_hunk="@@ -5 +5 @@")
    gh = GhHead(head="H1", advance_to="H2")               # fix already at HEAD; no push
    driver, clock = make_driver(
        [(0, finding)], gh=gh, cfg=cfg, rr_active=True, preflight=True,
        auto_merge=True, max_rounds=1,
        fix_dispatch=lambda c, r: FixOutcome(status="skipped", detail="already fixed"),
    )
    outcome = driver.run()
    assert outcome.merged is False                    # the unverified fix must NOT merge
    assert outcome.status == "max-rounds"             # handed back at budget, not clean
    assert gh.matching("gh", "pr", "merge") == []     # no merge was attempted


def test_default_launch_still_dismisses_an_already_fixed_finding():
    # The re-verify carve-out is RESTART-only: a plain launch (no --rr-active) that
    # finds an already-fixed finding still folds its reviewer to reviewed-no-change,
    # exactly as before — _restart_reverify_ids stays empty off the restart path.
    cfg = {"active_reviewers": ["copilot"], "auto_on_open": {"copilot": False}}
    finding = Comment(id="f1", text="this null check is missing",
                      source="copilot[bot]", path="y.py", diff_hunk="@@ -5 +5 @@")
    gh = GhHead(head="H1", advance_to="H2")
    driver, clock = make_driver(
        [(0, finding)], gh=gh, cfg=cfg, preflight=True, auto_merge=True, max_rounds=3,
        fix_dispatch=lambda c, r: FixOutcome(status="skipped", detail="already fixed"),
    )
    driver.run()
    assert driver._restart_reverify_ids == set()          # never populated off-restart
    assert "copilot" in driver.reviewed_no_change          # dismissed as before


# ---------------------------------------------------------------------------
# A repo-less invocation (``--repo`` omitted → ``self.repo is None``) must key
# polish_state on the SAME owner/repo a run WITH ``--repo`` uses, so a restart
# restores regardless of whether either run happened to pass ``--repo``.
# ---------------------------------------------------------------------------

def test_repo_less_run_keys_polish_state_on_the_cwd_inferred_repo():
    # repo=None: RoundDriver must infer "o/r" from the cwd's gh remote rather
    # than falling back to the shared "local" key.
    gh = GhHead(head="H0", advance_to="H1", kill_on="@claude review", kill_after=1,
                name_with_owner=REPO)
    driver, clock = make_driver([(0, SUBSTANTIVE), (0, COSMETIC)], gh=gh,
                                 repo=None, max_rounds=3)
    with pytest.raises(KeyboardInterrupt):
        driver.run()
    assert driver.polishing == {"copilot"}
    # Stamped under the INFERRED repo, not the "local" fallback.
    assert polish_state.read_polish_state(PR, REPO)["bots"] == ["copilot"]
    assert not os.path.exists(polish_state.state_path(PR, None))


def test_restart_omitting_repo_still_restores_a_run_that_passed_it():
    # Run 1 passes --repo explicitly; the restart omits it but shares the same
    # cwd/remote, so it must resolve to the SAME key and restore the verdict.
    _killed_mixed_round()
    assert polish_state.read_polish_state(PR, REPO)["tip_sha"] == "H1"

    gh2 = GhHead(head="H1", advance_to="H2", name_with_owner=REPO)
    driver2, clock2 = make_driver([(0, SUBSTANTIVE), (0, COSMETIC)], gh=gh2,
                                  repo=None, rr_active=True, preflight=True, max_rounds=3)
    driver2.run()
    assert "copilot" in driver2.polishing                  # restored despite --repo omitted
    assert gh2.matching("requested_reviewers") == []


def test_polish_is_not_restored_when_head_has_moved():
    # HEAD advanced past the stamped tip (a human's commit, a rebase): the reviewer
    # may have real findings on the new code, so its polish verdict is void.
    _killed_mixed_round()
    assert polish_state.read_polish_state(PR, REPO)["tip_sha"] == "H1"

    gh = GhHead(head="H2", advance_to="H3")               # live HEAD ≠ stamped tip
    driver, clock = make_driver([], gh=gh, rr_active=True, preflight=True, max_rounds=3)
    driver.run()
    assert driver.polishing == set()                       # nothing restored
    assert gh.matching("requested_reviewers")              # copilot summoned again


# ---------------------------------------------------------------------------
# Fail-closed: an unknown tip, a corrupt file, no file
# ---------------------------------------------------------------------------

def test_unknown_tip_on_write_stamps_nothing():
    # The tip could not be read at the round's end → no stamp is written, so a later
    # restore can never match a state whose head the loop never knew.
    gh = GhHead(head="H0", advance_to="H1", head_fails=True,
                kill_on="@claude review", kill_after=1)
    driver, clock = make_driver([(0, SUBSTANTIVE), (0, COSMETIC)], gh=gh, max_rounds=3)
    with pytest.raises(KeyboardInterrupt):
        driver.run()
    assert driver.polishing == {"copilot"}                    # the verdict was reached …
    assert polish_state.read_polish_state(PR, REPO) is None   # … but never stamped
    assert not os.path.exists(polish_state.state_path(PR, REPO))


def test_unknown_live_head_on_restore_restores_nothing():
    # State exists, but the restart cannot read the PR's live head → restore nothing
    # (never crash, never guess), and summon the reviewer as usual.
    _killed_mixed_round()
    gh = GhHead(head="H1", head_fails=True)
    driver, clock = make_driver([], gh=gh, rr_active=True, preflight=True, max_rounds=3)
    driver.run()
    assert driver.polishing == set()
    assert gh.matching("requested_reviewers")


@pytest.mark.parametrize("body", [
    "{ not json",                                    # torn / corrupt write
    "",                                              # empty file
    json.dumps({"schema_version": 99, "repo": REPO, "pr": PR,
                "tip_sha": "H1", "bots": ["copilot"]}),      # foreign schema
    json.dumps({"schema_version": 1, "repo": "other/repo", "pr": PR,
                "tip_sha": "H1", "bots": ["copilot"]}),      # another repo's state
    json.dumps({"schema_version": 1, "repo": REPO, "pr": PR,
                "tip_sha": "", "bots": ["copilot"]}),        # blank tip
    json.dumps({"schema_version": 1, "repo": REPO, "pr": PR,
                "tip_sha": "H1", "bots": "copilot"}),        # malformed bot list
    json.dumps(["not", "an", "object"]),             # wrong shape
])
def test_untrustworthy_state_reads_as_none_and_resummons(body):
    path = polish_state.state_path(PR, REPO)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    assert polish_state.read_polish_state(PR, REPO) is None   # never raises

    gh = GhHead(head="H1")
    driver, clock = make_driver([], gh=gh, rr_active=True, preflight=True, max_rounds=3)
    driver.run()                                              # never crashes
    assert driver.polishing == set()
    assert gh.matching("requested_reviewers")                 # re-summoned


def test_absent_state_file_is_not_an_error():
    assert polish_state.read_polish_state(PR, REPO) is None
    assert polish_state.clear_polish_state(PR, REPO) is False   # nothing to drop
    gh = GhHead(head="H1")
    driver, clock = make_driver([], gh=gh, rr_active=True, preflight=True, max_rounds=3)
    driver.run()
    assert gh.matching("requested_reviewers")


# ---------------------------------------------------------------------------
# The auto-merge regression this must not cause
# ---------------------------------------------------------------------------

def test_all_polish_restart_still_auto_merges():
    # Every reviewer is polish-only at this head → the round-1 expected set is EMPTY.
    # The restored verdicts must be folded into reviewed_ever, or the
    # never-merge-unreviewed gate reads "nobody reviewed" and blocks the merge the
    # operator is entitled to.
    polish_state.write_polish_state(PR, REPO, "H1", ["claude", "copilot"])
    gh = GhHead(head="H1")
    driver, clock = make_driver([], gh=gh, rr_active=True, preflight=True,
                                auto_merge=True, max_rounds=3)
    outcome = driver.run()
    assert driver.polishing == {"claude", "copilot"}
    assert driver._run_start_fleet == {"claude", "copilot"}   # the gate's universe is intact
    assert driver.reviewed_ever == {"claude", "copilot"}      # …and they DID review
    assert outcome.merged is True
    assert gh.matching("@claude review") == []               # nobody re-asked
    assert not os.path.exists(polish_state.state_path(PR, REPO))   # cleared on merge


def test_polish_state_is_kept_when_the_run_hands_back_without_merging():
    # The hand-back (a blocked gate / red CI) is exactly the restart the state exists
    # for — only a MERGED PR's state is dropped.
    polish_state.write_polish_state(PR, REPO, "H1", ["copilot"])
    gh = GhHead(head="H1")
    driver, clock = make_driver([], gh=gh, rr_active=True, preflight=True,
                                auto_merge=False, max_rounds=3)
    outcome = driver.run()
    assert outcome.merged is False
    assert polish_state.read_polish_state(PR, REPO)["bots"] == ["copilot"]


# ---------------------------------------------------------------------------
# The store itself
# ---------------------------------------------------------------------------

def test_write_refuses_an_unknown_tip():
    assert polish_state.write_polish_state(PR, REPO, "", ["copilot"]) is False
    assert polish_state.write_polish_state(PR, REPO, "   ", ["copilot"]) is False
    assert polish_state.read_polish_state(PR, REPO) is None


def test_state_is_keyed_on_repo_and_pr():
    polish_state.write_polish_state(PR, REPO, "H1", ["copilot"])
    assert polish_state.read_polish_state(PR, REPO)["bots"] == ["copilot"]
    assert polish_state.read_polish_state("8", REPO) is None        # another PR
    assert polish_state.read_polish_state(PR, "other/repo") is None  # another repo
    assert polish_state.clear_polish_state(PR, REPO) is True
    assert polish_state.read_polish_state(PR, REPO) is None


def test_same_repo_name_under_different_owners_never_share_a_file():
    # The key is the FULL owner/repo: alice/app#7 and bob/app#7 map to DISTINCT files,
    # so two loops on same-named repos never fight over one filename. Each owner reads
    # only its own verdict; clearing one leaves the other's untouched.
    polish_state.write_polish_state("7", "alice/app", "HA", ["copilot"])
    assert polish_state.read_polish_state("7", "bob/app") is None       # no cross-read
    polish_state.write_polish_state("7", "bob/app", "HB", ["claude"])
    assert (polish_state.state_path("7", "alice/app")
            != polish_state.state_path("7", "bob/app"))                 # distinct files …
    assert polish_state.read_polish_state("7", "alice/app")["bots"] == ["copilot"]
    assert polish_state.read_polish_state("7", "bob/app")["bots"] == ["claude"]
    assert polish_state.clear_polish_state("7", "bob/app") is True      # … so clearing one
    assert polish_state.read_polish_state("7", "alice/app")["bots"] == ["copilot"]


def test_unknown_repo_never_restores_even_its_own_write():
    # An unknown repo (None) has no identity to key on: every repo-less run shares the
    # one ``local-PR<pr>.json`` file, so two forks with the same PR number and a
    # fork-shared head SHA would otherwise restore each other's verdict. Fail CLOSED —
    # a None-repo write is never read back, not even by another None-repo read.
    assert polish_state.write_polish_state("7", None, "H1", ["copilot"]) is True
    assert polish_state.read_polish_state("7", None) is None
    # A record hand-written with repo=None is likewise never handed to a None request.
    assert polish_state.read_polish_state("7", None) is None


# ---------------------------------------------------------------------------
# The verdict survives EVERY exit, not just a completed round
# ---------------------------------------------------------------------------

def test_polish_verdict_survives_an_escalation_handback_and_restart():
    # The flagship --rr-active flow: the loop escalates a business question and hands
    # back, the operator answers it out of band and re-runs. That hand-back fires
    # BEFORE the round's push, so a stamp written only after a completed round is
    # never written at all — and the restart re-summons the polish-only reviewers the
    # run had already finished with.
    question = Comment(id="q", text="who owns the retention policy for this table?",
                       source="claude[bot]", path="z.py", diff_hunk="@@ -3 +3 @@")
    gh = GhHead(head="H0")
    driver, clock = make_driver([(0, question), (0, COSMETIC)], gh=gh, max_rounds=3,
                                answer_waiter=lambda esc, **k: {"q": None})
    outcome = driver.run()
    assert outcome.status == "needs-human"           # handed back, nothing pushed
    assert driver.polishing == {"copilot"}
    state = polish_state.read_polish_state(PR, REPO)
    assert state["bots"] == ["copilot"] and state["tip_sha"] == "H0"

    # The operator answers the question, then re-runs --rr-active at the same head.
    gh2 = GhHead(head="H0")
    driver2, _ = make_driver([(0, question), (0, COSMETIC)], gh=gh2, rr_active=True,
                             preflight=True, max_rounds=3)
    driver2.run()
    assert "copilot" in driver2.polishing                # verdict restored …
    assert gh2.matching("requested_reviewers") == []     # … so it is never re-asked


def test_an_empty_polish_set_never_erases_a_verdict_at_the_same_tip():
    # NO-CLOBBER. Only --rr-active restores the verdict, so any other run mode reaches
    # its round end with an EMPTY set purely because it never read the record — and
    # must not erase, at the very commit it is describing, a verdict another run
    # legitimately reached.
    assert polish_state.write_polish_state(PR, REPO, "H1", ["copilot"]) is True
    assert polish_state.write_polish_state(PR, REPO, "H1", []) is False    # refused
    assert polish_state.read_polish_state(PR, REPO)["bots"] == ["copilot"]
    # A MOVED tip is a different commit — the old record no longer speaks for it.
    assert polish_state.write_polish_state(PR, REPO, "H2", []) is True
    assert polish_state.read_polish_state(PR, REPO)["bots"] == []


def test_a_plain_rerun_does_not_erase_the_verdict_at_the_same_tip():
    # The end-to-end shape of the same rule: a DEFAULT launch (which restores nothing)
    # reaches its round end with an empty polish set at an unchanged HEAD. The record
    # a prior run reached at that commit must survive it.
    polish_state.write_polish_state(PR, REPO, "H1", ["copilot"])
    gh = GhHead(head="H1")
    driver, clock = make_driver([(0, SUBSTANTIVE)], gh=gh, max_rounds=1, push=False)
    driver.run()
    assert driver.polishing == set()                                    # nothing restored
    assert polish_state.read_polish_state(PR, REPO)["bots"] == ["copilot"]


def test_forced_rr_clears_a_stale_verdict_so_the_reviewer_is_re_included():
    # The CONTRAST to the plain rerun above. --rr is an explicit "re-ping everyone":
    # it clears the in-memory soft exclusions (done/approved/polishing/…) so a
    # previously-satisfied reviewer is summoned again. The on-disk polish record is the
    # CROSS-RESTART form of that same soft exclusion, so --rr must drop it too.
    # Otherwise a --rr run that ends at an unadvanced HEAD with an empty polish set
    # cannot erase it — the empty no-clobber refuses the same-tip write and --rr never
    # sets restored_prior — and a later --rr-active restart restores the stale
    # [copilot], re-skipping the very reviewer --rr was explicitly used to re-include.
    polish_state.write_polish_state(PR, REPO, "H1", ["copilot"])
    gh = GhHead(head="H1")                              # HEAD never advances (push off)
    driver, _ = make_driver([(0, SUBSTANTIVE)], gh=gh, rr=True, max_rounds=1, push=False)
    driver.run()
    assert driver.polishing == set()                                     # cleared in memory …
    assert (polish_state.read_polish_state(PR, REPO) or {}).get("bots") == []   # … and on disk

    # End-to-end: the follow-up --rr-active restart at the same head now restores
    # nothing, so copilot is summoned again instead of being silently skipped.
    gh2 = GhHead(head="H1", advance_to="H2")
    driver2, _ = make_driver([], gh=gh2, rr_active=True, preflight=True, max_rounds=1)
    driver2.run()
    assert "copilot" not in driver2.polishing            # no stale verdict restored …
    assert gh2.matching("requested_reviewers") != []     # … so copilot is re-summoned


def test_a_restored_run_may_clear_an_invalidated_verdict_at_the_same_tip():
    # NO-CLOBBER has ONE exception. A run that RESTORED the verdict this tip carries
    # (restored_prior=True, an --rr-active restart) and then legitimately cleared it —
    # the reviewer posted a later substantive finding and was demoted on the same
    # unadvanced HEAD — holds the AUTHORITATIVE empty set. Refusing it would strand the
    # invalidated verdict on disk for the next restart to restore again.
    assert polish_state.write_polish_state(PR, REPO, "H1", ["copilot"]) is True
    assert polish_state.write_polish_state(PR, REPO, "H1", []) is False               # no-clobber
    assert polish_state.write_polish_state(PR, REPO, "H1", [], restored_prior=True) is True
    assert polish_state.read_polish_state(PR, REPO)["bots"] == []                     # cleared


def test_restored_verdict_evicted_without_a_push_is_cleared_from_disk():
    # The end-to-end shape: an --rr-active restart RESTORES copilot's polish verdict at
    # H1, but copilot then posts a REAL finding on that same commit. With pushing off
    # HEAD never advances, so the round-end persist writes its (now empty) polish set at
    # the SAME tip H1. Without the restored-run exception the empty write is refused and
    # the stale [copilot] survives — so the NEXT restart would restore copilot as
    # polish-only and never re-ask it, even though its verdict was invalidated.
    cfg = {"active_reviewers": ["copilot"], "auto_on_open": {"copilot": True}}
    polish_state.write_polish_state(PR, REPO, "H1", ["copilot"])
    new_finding = Comment(id="c2", text="this null check is missing",
                          source="copilot[bot]", path="y.py", diff_hunk="@@ -5 +5 @@")
    gh = GhHead(head="H1")                              # HEAD never advances (push off)
    driver, clock = make_driver([(0, new_finding)], gh=gh, cfg=cfg, push=False,
                                rr_active=True, preflight=True, max_rounds=1)
    driver.run()
    assert "copilot" not in driver.polishing           # the finding evicted the verdict …
    assert polish_state.read_polish_state(PR, REPO)["bots"] == []   # … and disk was cleared
