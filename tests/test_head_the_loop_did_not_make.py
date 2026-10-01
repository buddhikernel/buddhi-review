"""A head holding a commit the loop did not make itself always needs its own review.

The merge gate lets a PR merge on the review of an older head only when every
commit after the reviewed-commit boundary is one of the run's own cosmetic commits.
The head can gain a commit the loop never made in several ways: a fixer that
commits (and pushes) its own fix — the round's commit step then finds a clean tree
and pushes nothing —, an operator or another process committing or pushing from the
loop's checkout, a rebase force-pushed from it, or a push, force-push or "Update
branch" made somewhere else. Whatever the round's labels were, such a commit must
never ride the review in hand:

* a commit the loop did not make, on a head GitHub shows, moves the boundary onto
  that head, and a reviewer is asked about it while a round remains;
* a commit only the loop's worktree has is pushed first (never forced), so the
  reviewer can see it — or, with no round left, the run hands back;
* a PR head the worktree does not hold is handed back, and never rebased and
  force-pushed over on the way out;
* a head that cannot be read counts as one the loop did not make;
* the merge gate judges the head the last check vetted, so a commit landing in the
  checkout while the merge is being decided never merges on an earlier review.

These tests drive the REAL round loop, commit step and fixer harness against a real
git repository with a bare remote; only ``gh`` is faked (a merge succeeds only when
its ``--match-head-commit`` pin is the remote tip, as on GitHub). Every case has a
COSMETIC twin in which the loop makes every commit itself: the PR still merges on
the review in hand and nobody is asked again.
"""
import os
import subprocess

import pytest

from buddhi_review import commit_push, escalation_wait, polish_state
from test_leftover_fix_boundary import (
    CLAUDE_ONLY, COSM, SUBST, TWO, Fleet, World, _CP, git, make_driver, writes)

# A sign-off posted after the round's summon and after every commit the tests make.
LATE = "2099-01-01T00:00:00+00:00"


class GitHub(World):
    """:class:`World` plus the PR's base and head branch names (so the manual-landing
    rebase runs as it would against GitHub), scripted answers for the PR-head read
    (``gh api …/pulls/N -q .head.sha``), a record of every push the loop attempts
    and of every git command it runs, git failures on demand (the history, or the
    worktree's head), and a second clone for pushes made elsewhere."""

    def __init__(self, tmp):
        super().__init__(tmp)
        self.tmp = tmp
        self.head_read = None     # optional callable → a CompletedProcess to answer with
        self.lag = 0              # reads after each push that still show the tip before it
        self._stale = None
        self._lag_left = 0
        self.push_attempts = []
        self.git_calls = []
        self.history_unreadable = False   # every ``git rev-list`` fails
        self.head_read_failures = 0       # the next N ``git rev-parse HEAD`` fail

    def gh_run(self, argv, *, cwd=None, timeout=None):
        argv = list(argv)
        if argv[:1] == ["git"]:
            self.git_calls.append(argv)
            if argv[:2] == ["git", "rev-list"] and self.history_unreadable:
                return _CP(128, "", "fatal: bad object")
            if argv == ["git", "rev-parse", "HEAD"] and self.head_read_failures:
                self.head_read_failures -= 1
                return _CP(128, "", "fatal: unable to read HEAD")
        if argv[:2] == ["gh", "api"] and ".head.sha" in argv:
            if self.head_read is not None:
                answer = self.head_read()
                if answer is not None:
                    return answer
            if self._lag_left:
                self._lag_left -= 1
                return _CP(0, self._stale + "\n")
        if argv[:3] == ["gh", "pr", "view"] and "baseRefName" in argv:
            return _CP(0, "main\n")
        if argv[:3] == ["gh", "pr", "view"] and "headRefName" in argv:
            return _CP(0, "feat\n")
        if argv[:2] == ["git", "push"]:
            self.push_attempts.append(argv)
            before = self.remote_tip()
            proc = super().gh_run(argv, cwd=cwd, timeout=timeout)
            if proc.returncode == 0 and self.remote_tip() != before and self.lag:
                self._stale, self._lag_left = before, self.lag
            return proc
        return super().gh_run(argv, cwd=cwd, timeout=timeout)

    def elsewhere(self):
        """A clone of the PR branch on another machine."""
        other = self.tmp / "elsewhere"
        subprocess.run(["git", "clone", "-q", "-b", "feat", str(self.remote), str(other)],
                       check=True, capture_output=True)
        for key, value in (("user.name", "o"), ("user.email", "o@o"),
                           ("commit.gpgsign", "false")):
            git(other, "config", key, value)
        return other

    def tip_content(self, sha, path="engine.py"):
        return git(self.remote, "show", f"{sha}:{path}")


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv(polish_state.STATE_DIR_ENV, str(tmp_path / "polish"))
    monkeypatch.setattr(commit_push, "run_test_gate",
                        lambda cwd, repo=None, run=None, notice=None, **k: ("green", ""))
    monkeypatch.setattr(escalation_wait, "wait_for_answer", lambda n, ask, **k: None)
    return GitHub(tmp_path)


def run(world, fleet, behaviours, *, cfg=CLAUDE_ONLY, max_rounds=3):
    driver = make_driver(world, fleet, behaviours, test_gate=True, cfg=cfg,
                         max_rounds=max_rounds)
    return driver, driver.run()


def sign_off(fleet, bot, k, cid):
    fleet.post(bot, k, cid, "No issues found.", inline=False)
    fleet.script[-1][2].created_at = LATE


def self_commits(fname, marker, *, push):
    """A fixer that commits its own fix — and, with ``push``, pushes it too."""
    def fixer(world):
        with open(world.repo / fname, "a") as fh:
            fh.write(marker + "\n")
        git(world.repo, "commit", "-qam", "the fixer's own commit")
        if push:
            git(world.repo, "push", "-q", "origin", "HEAD:refs/heads/feat")
        return 0, "done"
    return fixer


def _merged_on_the_review_in_hand(world, driver, outcome):
    """The cosmetic twin: the loop made every commit itself, so the PR merges on
    round 1's review of H0 and nobody is asked again."""
    assert outcome.merged is True and world.merges == [world.remote_tip()]
    assert SUBST not in world.markers(world.remote_tip())
    assert driver._last_substantive_head == world.h0
    assert world.summons == [world.h0]


# ── a fixer commits its own fix ──────────────────────────────────────────────


@pytest.mark.parametrize("label", ["cosmetic", "substantive"])
@pytest.mark.parametrize("reviewed", [True, False])
def test_a_fixer_that_commits_and_pushes_its_own_fix_is_never_merged_on_an_earlier_review(
        world, label, reviewed):
    # The fixer commits and pushes the fix itself, so the round's commit step finds
    # a clean tree and pushes nothing. The head is still one the loop did not make:
    # the reviewer is asked about it, and the PR merges only once it has been
    # reviewed there.
    fleet = Fleet(world)
    fleet.post("claude", 1, "f1", f"[{label}] the null check in engine is missing")
    if reviewed:
        fleet.post("claude", 2, "n2", "[cosmetic] nit: wording in style")
    driver, outcome = run(world, fleet, {"f1": self_commits("engine.py", SUBST, push=True),
                                         "n2": writes("style.py", COSM)})

    assert len(world.summons) == 2, "the reviewer was asked about the fixer's commit"
    fixers_head = world.summons[1]
    assert SUBST in world.markers(fixers_head)
    assert driver._last_substantive_head == fixers_head
    if reviewed:
        assert outcome.merged is True and world.merges == [world.remote_tip()]
        assert COSM in world.tip_content(world.remote_tip(), "style.py")
    else:
        assert world.merges == [] and outcome.merged is False


def test_the_cosmetic_twin_of_a_fixer_s_own_commit_merges_with_nobody_asked_again(world):
    # The same fix left in the tree for the round's commit step: the loop's own
    # cosmetic commit keeps the review in hand.
    fleet = Fleet(world)
    fleet.post("claude", 1, "f1", "[cosmetic] the null check in engine is missing")
    fleet.post("claude", 2, "n2", "[cosmetic] nit: wording in style")
    driver, outcome = run(world, fleet, {"f1": writes("engine.py", "COSM-f1"),
                                         "n2": writes("style.py", COSM)})

    _merged_on_the_review_in_hand(world, driver, outcome)


def test_a_fixer_commit_left_only_in_the_worktree_is_pushed_and_reviewed(world):
    # The fixer commits without pushing: GitHub does not have the commit, so no
    # reviewer could see it. The loop pushes it (never forced), asks the reviewer
    # about it, and merges once it is reviewed.
    fleet = Fleet(world)
    fleet.post("claude", 1, "f1", "[cosmetic] the null check in engine is missing")
    fleet.post("claude", 2, "n2", "[cosmetic] nit: wording in style")
    driver, outcome = run(world, fleet, {"f1": self_commits("engine.py", SUBST, push=False),
                                         "n2": writes("style.py", COSM)})

    assert len(world.summons) == 2
    fixers_head = world.summons[1]
    assert SUBST in world.markers(fixers_head)
    assert driver._last_substantive_head == fixers_head
    assert outcome.merged is True and world.merges == [world.remote_tip()]


@pytest.mark.parametrize("why", ["no-round-left", "push-refused"])
def test_a_fixer_commit_left_only_in_the_worktree_that_cannot_be_reviewed_hands_back(
        world, why):
    # With no round left the loop pushes nothing it did not make; when the push is
    # refused there is no way to show the commit to a reviewer. Either way the run
    # hands back, leaves the PR as it was, and skips the exit rebase (which would
    # push the unreviewed commit onto the PR).
    if why == "push-refused":
        hook = world.repo / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\necho 'hook: pushes refused' >&2\nexit 1\n")
        os.chmod(hook, 0o755)
    fleet = Fleet(world)
    fleet.post("claude", 1, "f1", "[cosmetic] the null check in engine is missing")
    driver, outcome = run(world, fleet,
                          {"f1": self_commits("engine.py", SUBST, push=False)},
                          max_rounds=1 if why == "no-round-left" else 3)

    assert SUBST in world.markers(world.local_head())
    assert world.remote_tip() == world.h0 and world.summons == [world.h0]
    assert outcome.status == "needs-human" and outcome.rebase_skip is True
    assert world.merges == [] and outcome.merged is False


def test_the_loop_s_push_carrying_a_fixer_s_own_commit_is_reviewed(world):
    # One fixer commits its fix without pushing, another leaves its fix in the
    # tree; the round's commit step commits the second on top of the first and
    # pushes both. The head holds a commit the loop did not make.
    fleet = Fleet(world)
    fleet.post("claude", 1, "f1", "[cosmetic] the null check in engine is missing")
    fleet.post("claude", 1, "n1", "[cosmetic] nit: wording in style")
    driver, outcome = run(world, fleet, {"f1": self_commits("engine.py", SUBST, push=False),
                                         "n1": writes("style.py", COSM)})

    assert len(world.summons) == 2
    carrying = world.summons[1]
    assert SUBST in world.markers(carrying) and COSM in world.tip_content(carrying,
                                                                          "style.py")
    assert driver._last_substantive_head == carrying
    assert world.merges == [] and outcome.merged is False


def test_the_cosmetic_twin_of_a_carried_fixer_commit_merges_with_nobody_asked_again(world):
    fleet = Fleet(world)
    fleet.post("claude", 1, "f1", "[cosmetic] the null check in engine is missing")
    fleet.post("claude", 1, "n1", "[cosmetic] nit: wording in style")
    driver, outcome = run(world, fleet, {"f1": writes("engine.py", "COSM-f1"),
                                         "n1": writes("style.py", COSM)})

    _merged_on_the_review_in_hand(world, driver, outcome)


# ── a commit an earlier run left only in the worktree ─────────────────────────


@pytest.mark.parametrize("leftover", [True, False])
def test_a_sign_off_is_never_credited_to_a_commit_only_the_worktree_has(world, leftover):
    # An earlier run's fixer committed a fix without pushing it. codex signs off on
    # what it was asked about — the PR's head on GitHub, H0 — and claude's nit is
    # fixed on top of the leftover commit and pushed with it. The sign-off is about
    # H0, not the leftover: the pushed head is asked about, and the PR merges only
    # on codex's sign-off there. The twin (no leftover) merges on round 1.
    if leftover:
        with open(world.repo / "engine.py", "a") as fh:
            fh.write(SUBST + "\n")
        git(world.repo, "commit", "-qam", "left by a fixer, never pushed")
    fleet = Fleet(world)
    fleet.post("claude", 1, "n1", "[cosmetic] nit: wording in style")
    sign_off(fleet, "codex", 1, "s1")
    sign_off(fleet, "codex", 2, "s2")
    driver, outcome = run(world, fleet, {"n1": writes("style.py", COSM)}, cfg=TWO)

    assert outcome.merged is True and world.merges == [world.remote_tip()]
    if leftover:
        carrying = world.remote_tip()
        assert SUBST in world.markers(carrying)
        assert driver._last_substantive_head == carrying
        assert world.codex_summons == [world.h0, carrying], (
            "codex was asked about the head that carries the leftover")
    else:
        _merged_on_the_review_in_hand(world, driver, outcome)
        assert world.codex_summons == [world.h0]


# ── a commit made from the loop's checkout while it waits ────────────────────


def _commit_and_push(world):
    with open(world.repo / "engine.py", "a") as fh:
        fh.write(SUBST + "\n")
    git(world.repo, "commit", "-qam", "an operator's commit")
    git(world.repo, "push", "-q", "origin", "HEAD:refs/heads/feat")


def _amend_and_force_push(world):
    with open(world.repo / "engine.py", "a") as fh:
        fh.write(SUBST + "\n")
    git(world.repo, "commit", "-q", "--amend", "-a", "--no-edit")
    git(world.repo, "push", "-q", "-f", "origin", "HEAD:refs/heads/feat")


CHECKOUT_ACTIONS = {"commit-and-push": _commit_and_push,
                    "amend-and-force-push": _amend_and_force_push,
                    "none": None}


class During(Fleet):
    """A :class:`Fleet` whose first fetch after claude's ``after``-th summon runs
    ``action`` — something done to the PR branch while the loop waits for
    reviewers."""

    def __init__(self, world, action, after=1):
        super().__init__(world)
        self.action, self.after, self.done = action, after, False

    def fetch(self, pr, repo=None, cwd=None):
        if len(self.world.summons) >= self.after and not self.done:
            self.done = True
            if self.action:
                self.action(self.world)
        return super().fetch(pr, repo=repo, cwd=cwd)


@pytest.mark.parametrize("action", sorted(CHECKOUT_ACTIONS))
def test_a_commit_pushed_from_the_checkout_while_the_loop_waits_is_reviewed_before_the_merge(
        world, action):
    # claude signs off on H0; meanwhile a commit is pushed (or H0 is amended and
    # force-pushed) from the loop's own checkout. Nothing is left to fix, but the
    # head is not the one claude signed off on: claude is asked again, and its
    # sign-off on the new head is what the PR merges on. The twin ("none") merges
    # on the first sign-off with nobody asked again.
    fleet = During(world, CHECKOUT_ACTIONS[action])
    sign_off(fleet, "claude", 1, "s1")
    sign_off(fleet, "claude", 2, "s2")
    driver, outcome = run(world, fleet, {})

    assert outcome.merged is True and world.merges == [world.remote_tip()]
    if action == "none":
        _merged_on_the_review_in_hand(world, driver, outcome)
        return
    moved = world.remote_tip()
    assert SUBST in world.markers(moved)
    assert driver._last_substantive_head == moved
    assert world.summons == [world.h0, moved]


# ── a push made somewhere else ───────────────────────────────────────────────


def _outside_push(world, other):
    with open(other / "engine.py", "a") as fh:
        fh.write(SUBST + "\n")
    git(other, "commit", "-qam", "pushed from another machine")
    git(other, "push", "-q", "origin", "HEAD:refs/heads/feat")


def _outside_force_push(world, other):
    with open(other / "engine.py", "a") as fh:
        fh.write(SUBST + "\n")
    git(other, "commit", "-q", "--amend", "-a", "--no-edit")
    git(other, "push", "-q", "-f", "origin", "HEAD:refs/heads/feat")


def _update_branch(world, other):
    # GitHub's "Update branch": the base is merged into the PR branch.
    git(other, "merge", "-q", "--no-edit", "origin/main")
    git(other, "push", "-q", "origin", "HEAD:refs/heads/feat")


OUTSIDE = {"push": _outside_push, "force-push": _outside_force_push,
           "update-branch": _update_branch}


def _move_base(other):
    git(other, "checkout", "-q", "-b", "base", "origin/main")
    (other / "base.txt").write_text("the base moved\n")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "the base moves on")
    git(other, "push", "-q", "origin", "HEAD:refs/heads/main")
    git(other, "checkout", "-q", "feat")
    git(other, "fetch", "-q", "origin")


@pytest.mark.parametrize("change", sorted(OUTSIDE) + ["none"])
def test_a_push_made_elsewhere_is_handed_back_never_merged_and_never_overwritten(
        world, change):
    # While the loop waits for reviewers the base branch moves on, and the PR branch
    # is pushed to, force-pushed or updated from another machine. The loop's
    # worktree does not hold that head, so nothing it asks for can merge it: the run
    # hands back, and the manual-landing rebase — which would rebase the worktree's
    # stale head and force-push it over the new one — is skipped. The twin ("none"):
    # only the base moves, and the PR merges on claude's sign-off.
    pushed = []

    def elsewhere(world):
        other = world.elsewhere()
        _move_base(other)
        if change != "none":
            OUTSIDE[change](world, other)
        pushed.append(world.remote_tip())

    fleet = During(world, elsewhere)
    sign_off(fleet, "claude", 1, "s1")
    driver, outcome = run(world, fleet, {})

    if change == "none":
        _merged_on_the_review_in_hand(world, driver, outcome)
        return
    assert world.remote_tip() == pushed[0], "the push made elsewhere is untouched"
    assert world.push_attempts == [], "the loop never pushed over a head it does not hold"
    assert outcome.status == "needs-human" and outcome.rebase_skip is True
    assert world.merges == [] and outcome.merged is False


# ── GitHub's view of the head ────────────────────────────────────────────────


def test_github_still_showing_the_head_before_the_loop_s_own_push_costs_no_review(world):
    # GitHub reports a PR's new head a moment after a push. Until it does, the
    # head the loop just pushed is still its own: the cosmetic commit merges on the
    # review in hand, with nobody asked again.
    world.lag = 4
    fleet = Fleet(world)
    fleet.post("claude", 1, "n1", "[cosmetic] nit: wording in style")
    fleet.post("claude", 2, "n2", "[cosmetic] nit: docstring in style")
    driver, outcome = run(world, fleet, {"n1": writes("style.py", COSM),
                                         "n2": writes("style.py", "COSM-n2")})

    assert world._lag_left < world.lag, "the loop read the lagging head"
    _merged_on_the_review_in_hand(world, driver, outcome)


@pytest.mark.parametrize("when", ["round-start", "round-end", "never"])
def test_a_pr_head_that_cannot_be_read_counts_as_one_the_loop_did_not_make(world, when):
    # GitHub cannot be asked for the PR's head — before the first summon (so what
    # the reviewer saw is unknown) or right after the round's cosmetic commit is
    # pushed (so whether GitHub holds anything else is unknown). The commit does
    # not keep the review in hand: claude is asked about it, and the PR merges on
    # that review. The twin ("never") merges after round 1.
    pushes = []

    def head_read():
        if when == "round-start" and not world.summons:
            return _CP(1, "", "gh: HTTP 502")
        if when == "round-end" and world.remote_tip() != world.h0 and not pushes:
            pushes.append(True)
            return _CP(1, "", "gh: HTTP 502")
        return None
    world.head_read = head_read

    fleet = Fleet(world)
    fleet.post("claude", 1, "n1", "[cosmetic] nit: wording in style")
    fleet.post("claude", 2, "n2", "[cosmetic] nit: docstring in style")
    driver, outcome = run(world, fleet, {"n1": writes("style.py", COSM),
                                         "n2": writes("style.py", "COSM-n2")})

    assert outcome.merged is True and world.merges == [world.remote_tip()]
    if when == "never":
        _merged_on_the_review_in_hand(world, driver, outcome)
        return
    loop_commit = world.summons[1]
    assert COSM in world.tip_content(loop_commit, "style.py")
    assert world.summons == [world.h0, loop_commit]
    assert driver._last_substantive_head == loop_commit


@pytest.mark.parametrize("pushed", ["from-the-checkout", "elsewhere", "nothing"])
def test_a_commit_pushed_after_the_last_round_is_checked_is_caught_before_the_merge(
        world, pushed):
    # The last round's cosmetic commit is pushed and checked; then, while the
    # round's verdicts are being recorded, a commit is pushed — from the checkout,
    # or from another machine while the base moves on. The merge decision checks
    # the head again: the checkout's commit never merges on the review of H0, and
    # the head pushed elsewhere is handed back without being rebased over. The
    # twin (nothing pushed) merges.
    fleet = Fleet(world)
    fleet.post("claude", 1, "n1", "[cosmetic] nit: wording in style")
    driver = make_driver(world, fleet, {"n1": writes("style.py", COSM)}, test_gate=True,
                         cfg=CLAUDE_ONLY)
    persist = driver._persist_polish_state
    landed = []

    def persist_while_a_push_lands(**kwargs):
        if not landed:
            if pushed == "from-the-checkout":
                _commit_and_push(world)
            elif pushed == "elsewhere":
                other = world.elsewhere()
                _move_base(other)
                _outside_push(world, other)
            landed.append(world.remote_tip())
        return persist(**kwargs)
    driver._persist_polish_state = persist_while_a_push_lands

    outcome = driver.run()

    if pushed == "nothing":
        _merged_on_the_review_in_hand(world, driver, outcome)
        return
    assert world.merges == [] and outcome.merged is False
    if pushed == "elsewhere":
        assert world.remote_tip() == landed[0], "the push made elsewhere is untouched"
        assert outcome.status == "needs-human" and outcome.rebase_skip is True
        return
    assert SUBST in world.markers(world.remote_tip())
    assert driver._last_substantive_head == world.remote_tip()


# ── a commit landing while the merge is being decided ────────────────────────


def _checkout_commit(world, *, push):
    with open(world.repo / "engine.py", "a") as fh:
        fh.write(SUBST + "\n")
    git(world.repo, "commit", "-qam", "an operator's commit")
    if push:
        git(world.repo, "push", "-q", "origin", "HEAD:refs/heads/feat")


@pytest.mark.parametrize("when", ["while-the-check-reads-github", "after-the-check",
                                  "never"])
def test_a_commit_made_while_the_merge_is_decided_never_merges_on_an_earlier_review(
        world, when):
    # The last round's cosmetic commit is pushed and the merge decision starts: the
    # pre-merge check reads the worktree's head, then asks GitHub for the PR's head.
    # A commit is made in the loop's checkout while that answer is on its way, and
    # pushed while the review threads are resolved — or made and pushed right after
    # the check, before the merge gate. Either way the check never saw the commit:
    # the gate judges the head the check vetted, the head is found to have moved
    # before the merge, and the commit never merges on claude's review of H0. The
    # twin (no commit) merges on the review in hand.
    fleet = Fleet(world)
    fleet.post("claude", 1, "n1", "[cosmetic] nit: wording in style")
    driver = make_driver(world, fleet, {"n1": writes("style.py", COSM)}, test_gate=True,
                         cfg=CLAUDE_ONLY)
    state = {"deciding": False, "committed": False, "pushed": False}

    clean_exit = driver._clean_exit

    def deciding(rounds):
        state["deciding"] = True
        return clean_exit(rounds)
    driver._clean_exit = deciding

    def head_read():
        if (when == "while-the-check-reads-github" and state["deciding"]
                and not state["committed"]):
            state["committed"] = True
            _checkout_commit(world, push=False)
        return None                        # GitHub answers with the head it has
    world.head_read = head_read

    fetch_threads = driver.fetch_threads

    def resolving_threads(pr, repo=None, cwd=None):
        if state["committed"] and not state["pushed"]:
            state["pushed"] = True
            git(world.repo, "push", "-q", "origin", "HEAD:refs/heads/feat")
        return fetch_threads(pr, repo=repo, cwd=cwd)
    driver.fetch_threads = resolving_threads

    warn = driver._maybe_warn_claude_never_reviewed

    def right_after_the_check():
        if when == "after-the-check" and not state["committed"]:
            state["committed"] = state["pushed"] = True
            _checkout_commit(world, push=True)
        return warn()
    driver._maybe_warn_claude_never_reviewed = right_after_the_check

    outcome = driver.run()

    if when == "never":
        _merged_on_the_review_in_hand(world, driver, outcome)
        return
    assert state["committed"] and state["pushed"]
    moved = world.remote_tip()
    assert SUBST in world.markers(moved) and world.local_head() == moved
    assert world.merges == [] and outcome.merged is False
    assert driver._last_substantive_head == moved
    assert world.summons == [world.h0]


# ── git cannot answer ────────────────────────────────────────────────────────


@pytest.mark.parametrize("label", ["cosmetic", "substantive"])
def test_a_history_that_cannot_be_read_never_lets_a_fixer_s_commit_ride_an_earlier_review(
        world, label):
    # Every ``git rev-list`` fails, so the check cannot tell which commits after
    # the boundary the loop made itself. A fixer commits and pushes its own fix;
    # that head counts as one the loop did not make: claude is asked about it, and
    # the PR merges only on its sign-off there, never on the review of H0.
    world.history_unreadable = True
    fleet = Fleet(world)
    fleet.post("claude", 1, "f1", f"[{label}] the null check in engine is missing")
    sign_off(fleet, "claude", 2, "s2")
    driver, outcome = run(world, fleet, {"f1": self_commits("engine.py", SUBST, push=True)})

    fixers_head = world.remote_tip()
    assert SUBST in world.markers(fixers_head)
    assert outcome.merged is True and world.merges == [fixers_head]
    assert world.summons == [world.h0, fixers_head], (
        "the fixer's commit merged only after claude was asked about it")
    assert driver._last_substantive_head == fixers_head


def test_the_cosmetic_twin_of_an_unreadable_history_merges_with_nobody_asked_again(world):
    # The same fix left in the tree for the round's commit step, with a history git
    # can read: the loop's own commit keeps the review in hand.
    fleet = Fleet(world)
    fleet.post("claude", 1, "f1", "[cosmetic] the null check in engine is missing")
    sign_off(fleet, "claude", 2, "s2")
    driver, outcome = run(world, fleet, {"f1": writes("engine.py", "COSM-f1")})

    _merged_on_the_review_in_hand(world, driver, outcome)


@pytest.mark.parametrize("case", ["a-commit-lands-and-the-read-fails", "the-read-fails",
                                  "nothing"])
def test_a_worktree_head_that_cannot_be_read_at_the_pre_merge_check_is_never_merged(
        world, case):
    # The last round's cosmetic commit is pushed and checked. Then a commit is
    # pushed from the checkout, and the pre-merge check's read of the worktree's
    # head fails once. What the head holds is unknown, so the boundary is dropped
    # and the merge gate blocks: the commit never merges on claude's review of H0 —
    # nor does the loop's own commit when only the read fails. The twin (nothing
    # lands, the head reads) merges on the review in hand.
    fleet = Fleet(world)
    fleet.post("claude", 1, "n1", "[cosmetic] nit: wording in style")
    driver = make_driver(world, fleet, {"n1": writes("style.py", COSM)}, test_gate=True,
                         cfg=CLAUDE_ONLY)
    persist = driver._persist_polish_state
    armed = []

    def persist_then_the_read_fails(**kwargs):
        result = persist(**kwargs)
        if not armed:
            armed.append(True)
            if case == "a-commit-lands-and-the-read-fails":
                _checkout_commit(world, push=True)
            if case != "nothing":
                world.head_read_failures = 1
        return result
    driver._persist_polish_state = persist_then_the_read_fails

    outcome = driver.run()

    if case == "nothing":
        _merged_on_the_review_in_hand(world, driver, outcome)
        return
    assert world.head_read_failures == 0, "the pre-merge check's read failed"
    assert world.merges == [] and outcome.merged is False
    assert driver._last_substantive_head is None
    if case == "a-commit-lands-and-the-read-fails":
        assert SUBST in world.markers(world.remote_tip())


# ── a sign-off already on the PR when the run starts ─────────────────────────


@pytest.mark.parametrize("mode", ["preflight", "rr-active"])
@pytest.mark.parametrize("leftover", [True, False])
def test_a_sign_off_already_on_the_pr_is_never_credited_to_a_commit_only_the_worktree_has(
        world, mode, leftover):
    # An earlier run's fixer committed a fix without pushing it, and claude signed
    # off on H0 before this run started. The run finds that sign-off already on
    # the PR, so round 1 asks only codex, which signs off too. Both sign-offs are
    # about H0, not the leftover: the leftover is pushed, both reviewers are asked
    # about the head that carries it, and the PR merges on their sign-offs there.
    # The twin (no leftover) merges on the sign-offs of H0 with nobody asked again.
    if leftover:
        with open(world.repo / "engine.py", "a") as fh:
            fh.write(SUBST + "\n")
        git(world.repo, "commit", "-qam", "left by a fixer, never pushed")
    fleet = Fleet(world)
    sign_off(fleet, "claude", 0, "s0")      # on the PR before this run
    sign_off(fleet, "codex", 1, "c1")
    sign_off(fleet, "claude", 1, "s1")
    sign_off(fleet, "codex", 2, "c2")
    driver = make_driver(world, fleet, {}, test_gate=True, cfg=TWO, preflight=True,
                         rr_active=mode == "rr-active")
    outcome = driver.run()

    assert outcome.merged is True and world.merges == [world.remote_tip()]
    if not leftover:
        assert world.merges == [world.h0]
        assert driver._last_substantive_head == world.h0
        assert world.summons == [] and world.codex_summons == [world.h0]
        return
    carrying = world.remote_tip()
    assert SUBST in world.markers(carrying)
    assert world.summons == [carrying] and world.codex_summons == [world.h0, carrying], (
        "the leftover merged only after both reviewers were asked about it")
    assert driver._last_substantive_head == carrying


# ── what the reviewers were shown, and who is asked again ────────────────────


@pytest.mark.parametrize("lag", [0, 4])
def test_a_sign_off_on_the_loop_s_own_substantive_push_counts_while_github_lags(world, lag):
    # Round 1's substantive fix is the loop's own commit. GitHub keeps reporting
    # H0 for a few reads after the push, and round 2 asks claude about the pushed
    # commit. claude's sign-off is about that commit — the head the loop pushed —
    # so the PR merges on it rather than asking again or handing back.
    world.lag = lag
    fleet = Fleet(world)
    fleet.post("claude", 1, "f1", "[substantive] the null check in engine is missing")
    sign_off(fleet, "claude", 2, "s2")
    driver, outcome = run(world, fleet, {"f1": writes("engine.py", "SUBST-loop")})

    fixed = world.remote_tip()
    if lag:
        assert world._lag_left < lag, "the loop read the lagging head"
    assert world.summons == [world.h0, fixed]
    assert driver._last_substantive_head == fixed
    assert outcome.merged is True and world.merges == [fixed]


def test_a_reviewer_set_aside_this_round_is_asked_at_once_about_a_head_the_loop_did_not_make(
        world):
    # claude's finding is cosmetic, which sets claude aside once it is fixed — and
    # the fixer commits and pushes the fix itself. Nobody else is expected, so
    # claude, who spoke this round, is asked about the fixer's head right away: in
    # the one round left, its sign-off there lets the PR merge.
    fleet = Fleet(world)
    fleet.post("claude", 1, "f1", "[cosmetic] the null check in engine is missing")
    sign_off(fleet, "claude", 2, "s2")
    driver, outcome = run(world, fleet, {"f1": self_commits("engine.py", SUBST, push=True)},
                          max_rounds=2)

    fixers_head = world.remote_tip()
    assert SUBST in world.markers(fixers_head)
    assert world.summons == [world.h0, fixers_head]
    assert outcome.merged is True and world.merges == [fixers_head]


def test_each_head_the_loop_did_not_make_gets_its_own_turn_of_the_fallback(world):
    # Round 1: the fixer commits and pushes its own fix, so claude — set aside by
    # its cosmetic finding — is asked about that head, and signs off in round 2.
    # Meanwhile a commit is pushed from the checkout: a new head no reviewer has
    # seen. claude already had its turn for the fixer's head, but this is another
    # head, so it is asked again, and the PR merges on its sign-off there.
    fleet = During(world, _commit_and_push, after=2)
    fleet.post("claude", 1, "f1", "[cosmetic] the null check in engine is missing")
    sign_off(fleet, "claude", 2, "s2")
    sign_off(fleet, "claude", 3, "s3")
    driver, outcome = run(world, fleet, {"f1": self_commits("engine.py", "SUBST-fixer",
                                                            push=True)}, max_rounds=4)

    assert fleet.done
    fixers_head, pushed = world.summons[1], world.remote_tip()
    assert world.markers(fixers_head) == {"SUBST-fixer"}
    assert world.markers(pushed) == {"SUBST-fixer", SUBST}
    assert outcome.merged is True and world.merges == [pushed]
    assert world.summons == [world.h0, fixers_head, pushed], (
        "claude was asked about the head pushed from the checkout")


def test_a_sign_off_counts_for_the_head_github_showed_the_reviewer(world):
    # Round 1's substantive fix is the loop's own commit S1. Before round 2 asks
    # claude about it, the branch is force-pushed from another machine to a
    # commit without that fix; while claude looks at it, the branch is
    # force-pushed back to S1. claude's sign-off is about the head it was shown,
    # not S1: claude is asked about S1, and the PR merges on its sign-off there.
    shown = []

    def force_push_back(world):
        git(world.tmp / "elsewhere", "push", "-q", "-f", "origin",
            f"{world.local_head()}:refs/heads/feat")

    fleet = During(world, force_push_back, after=2)
    fleet.post("claude", 1, "f1", "[substantive] the null check in engine is missing")
    sign_off(fleet, "claude", 2, "s2")
    sign_off(fleet, "claude", 3, "s3")
    driver = make_driver(world, fleet, {"f1": writes("engine.py", "SUBST-loop")},
                         test_gate=True, cfg=CLAUDE_ONLY)
    persist = driver._persist_polish_state

    def persist_then_force_push_elsewhere(**kwargs):
        result = persist(**kwargs)
        if not shown:
            other = world.elsewhere()
            git(other, "reset", "-q", "--hard", world.h0)
            with open(other / "style.py", "a") as fh:
                fh.write("pushed from another machine\n")
            git(other, "commit", "-qam", "another machine's commit")
            git(other, "push", "-q", "-f", "origin", "HEAD:refs/heads/feat")
            shown.append(world.remote_tip())
        return result
    driver._persist_polish_state = persist_then_force_push_elsewhere

    outcome = driver.run()

    fixed = world.local_head()
    assert fleet.done and world.summons[1] == shown[0] != fixed
    assert "SUBST-loop" not in git(world.remote, "show", f"{shown[0]}:engine.py")
    assert outcome.merged is True and world.merges == [fixed]
    assert world.summons == [world.h0, shown[0], fixed], (
        "S1 merged only after claude was asked about it")


def test_a_pr_head_that_reads_like_a_git_option_is_never_handed_to_git(world):
    # GitHub's answer for the PR's head is not a commit id but a string git would
    # take for an option. It counts as a head that cannot be read — the PR does not
    # merge on it — and it never reaches a git command line.
    bogus = "--output=/dev/null"
    world.head_read = lambda: _CP(0, bogus + "\n")
    fleet = Fleet(world)
    fleet.post("claude", 1, "n1", "[cosmetic] nit: wording in style")
    driver, outcome = run(world, fleet, {"n1": writes("style.py", COSM)})

    assert world.git_calls, "the loop ran git"
    assert not [argv for argv in world.git_calls if any(bogus in a for a in argv)]
    assert world.merges == [] and outcome.merged is False
