"""A fix the loop left UNCOMMITTED in its worktree never merges on an earlier review.

A run can hand back with a SUBSTANTIVE fix still sitting uncommitted in the
worktree: the local test gate goes red and the operator answers "Stop the run" (or
leaves the question unanswered, which stops too), or a pre-commit hook rejects the
round's commit. The next run on that worktree starts with the change already on
disk. Its first commit stages the whole tree, so a COSMETIC-only round would carry
the stranded fix onto the PR — and a cosmetic-only round keeps the review already in
hand, so the PR would merge code no reviewer ever saw.

The rule pinned here: a round that STARTS with uncommitted content does not keep the
existing review for the commit that carries it. That head needs its own review, the
reviewer is asked for one, and the PR does not merge until it arrives.

These tests drive the REAL round loop, commit step and fixer harness against a real
git repository with a bare remote. Only `gh` is faked (a merge succeeds only when its
``--match-head-commit`` pin is the remote tip, as on GitHub), together with the test
gate's verdict and the operator's answers. Every case has a COSMETIC twin: the same
second run, on a tree the operator cleaned between the runs, still merges on the
review already in hand.
"""
import json
import os
import subprocess
import time
from datetime import datetime, timezone

import pytest

from buddhi_review import commit_push, escalation_wait, fix_apply, polish_state
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.loop import Comment
from buddhi_review.round_driver import RoundDriver, RoundTimes
from buddhi_review.seams import ConsoleEscalation
from test_round_driver import FakeClock, FakeNotifier

CLAUDE_ONLY = {"active_reviewers": ["claude"], "auto_on_open": {"claude": False}}
TIMES = RoundTimes(quiescence=60, poll_interval=30, min_bot_wait=420,
                   idle_timeout=900, max_wait_total=1800, register_delay=0)
SUBST = "SUBST-f1"     # the stranded SUBSTANTIVE fix, written into engine.py
COSM = "COSM-n2"       # the second run's cosmetic fix, written into style.py


def _CP(rc, out="", err=""):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr=err)


def git(repo, *args):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr}")
    return proc.stdout.strip()


# Counts commit attempts in the repository and rejects the ones listed in {FAILS}.
_HOOK = """#!/bin/sh
f="$(git rev-parse --git-dir)/commit_attempts"
n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 ))
echo $n > "$f"
case " {FAILS} " in *" $n "*) echo "hook: rejecting commit $n" >&2; exit 1;; esac
exit 0
"""


class World:
    """A PR branch `feat` (tip H0) on a bare remote, plus the gh fake."""

    def __init__(self, tmp):
        self.remote = tmp / "remote.git"
        self.repo = tmp / "work"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)],
                       check=True)
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.repo)],
                       check=True, capture_output=True)
        for key, value in (("user.name", "t"), ("user.email", "t@t"),
                           ("commit.gpgsign", "false")):
            git(self.repo, "config", key, value)
        for name, body in (("x.py", "x = 1\n"), ("engine.py", "# engine\n"),
                           ("style.py", "# style\n")):
            (self.repo / name).write_text(body)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "push", "-q", "origin", "HEAD:main")
        git(self.repo, "checkout", "-qb", "feat")
        with open(self.repo / "x.py", "a") as fh:
            fh.write("feature = True\n")
        git(self.repo, "commit", "-qam", "H0 (the PR as opened)")
        git(self.repo, "push", "-qu", "origin", "feat")
        self.h0 = self.remote_tip()
        self.summons = []   # the remote tip at each '@claude review' summon
        self.codex_summons = []   # …and at each '@codex review' summon
        self.merges = []    # the pins GitHub accepted

    def remote_tip(self):
        return git(self.remote, "rev-parse", "refs/heads/feat")

    def local_head(self):
        return git(self.repo, "rev-parse", "HEAD")

    def dirty(self):
        return bool(git(self.repo, "status", "--porcelain"))

    def install_hook(self, fails):
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text(_HOOK.replace("{FAILS}", " ".join(str(n) for n in fails)))
        os.chmod(hook, 0o755)

    def markers(self, sha):
        if not sha:
            return set()
        proc = subprocess.run(["git", "-C", str(self.repo), "show", f"{sha}:engine.py"],
                              capture_output=True, text=True)
        return {line.strip() for line in proc.stdout.splitlines()
                if line.startswith("SUBST-")}

    def gh_run(self, argv, *, cwd=None, timeout=None):
        argv = list(argv)
        if argv and argv[0] == "git":
            return subprocess.run(argv, cwd=cwd or str(self.repo), capture_output=True,
                                  text=True, timeout=timeout or 120)
        if argv[:3] == ["gh", "pr", "merge"]:
            pin = (argv[argv.index("--match-head-commit") + 1]
                   if "--match-head-commit" in argv else None)
            if pin is None or pin != self.remote_tip():
                return _CP(1, "", "GraphQL: Head branch was modified.")
            self.merges.append(pin)
            return _CP(0)
        if any("@claude review" in a for a in argv):
            self.summons.append(self.remote_tip())
            return _CP(0)
        if any("@codex review" in a for a in argv):
            self.codex_summons.append(self.remote_tip())
            return _CP(0)
        if argv[:3] == ["gh", "pr", "view"] and ".state" in argv:
            return _CP(0, "MERGED\n" if self.merges else "OPEN\n")
        return _CP(0, "")


class Reviewer:
    """Comments become visible after the k-th summon (k=0: already on the PR) and
    are anchored to the remote tip the reviewer was asked to look at. After its
    scripted comments it stays silent."""

    def __init__(self, world):
        self.world = world
        self.script = []
        self.anchor = {}

    def post(self, k, cid, text):
        self.script.append((k, Comment(id=cid, text=text, source="claude[bot]",
                                       path="x.py", diff_hunk="@@ -1 +1 @@",
                                       created_at="2026-01-01T00:30:00+00:00")))

    def visible(self):
        out = []
        for k, c in self.script:
            if len(self.world.summons) >= k:
                if c.id not in self.anchor:
                    self.anchor[c.id] = self.world.summons[k - 1] if k else self.world.h0
                out.append(c)
        return out

    def fetch(self, pr, repo=None, cwd=None):
        return self.visible()

    def inline(self, pr, repo=None, cwd=None):
        return [{"user": {"login": "claude[bot]"}, "original_commit_id": self.anchor[c.id]}
                for c in self.visible()]


def classify(prompt):
    return json.dumps({"label": "SUBSTANTIVE" if "[substantive]" in prompt else "COSMETIC",
                       "reason": "t"})


def writes(fname, marker):
    def run(world):
        with open(world.repo / fname, "a") as fh:
            fh.write(marker + "\n")
        return 0, "done"
    return run


def already_fixed(world):
    return 0, "SKIP: already fixed in the worktree"


def make_driver(world, reviewer, behaviours, *, test_gate, rr_active=False,
                preflight=False, cwd=None, cfg=CLAUDE_ONLY, max_rounds=3):
    clock = FakeClock()
    cwd = cwd or str(world.repo)

    def dispatch(c, r):
        def runner(prompt, *, model, effort, timeout, cwd):
            return behaviours[c.id](world)
        return fix_apply.apply_fix(c.text, cwd=cwd, runner=runner,
                                   verify_runner=None, label=r.classification.label,
                                   commented_files=[c.path] if c.path else ())
    return RoundDriver(
        "7", repo="o/r", cwd=cwd, cfg=cfg,
        adapter=ReviewAdapter(escalation=ConsoleEscalation(notifier=FakeNotifier())),
        classify_runner=classify, fix_dispatch=dispatch,
        fetch=reviewer.fetch, reactions_fetch=lambda pr, repo=None, cwd=None: [],
        reviews_fetch=lambda pr, repo=None, cwd=None: [], inline_fetch=reviewer.inline,
        threads_fetch=lambda pr, repo=None, cwd=None: [],
        resolve_thread=lambda thread_id, cwd=None: True,
        gh_run=world.gh_run, clock=clock, sleep=clock.sleep, notice=lambda *a, **k: "",
        wall_clock=lambda: datetime(2026, 1, 1, 1, 30, tzinfo=timezone.utc),
        times=TIMES, answer_waiter=lambda esc, **k: {}, auto_merge=True,
        preflight=preflight, push=True, test_gate=test_gate, max_rounds=max_rounds,
        rr_active=rr_active,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(polish_state.STATE_DIR_ENV, str(tmp_path / "polish"))
    gate = {"verdicts": [], "answers": []}

    def fake_gate(cwd, repo=None, run=None, notice=None, **k):
        verdict = gate["verdicts"].pop(0) if gate["verdicts"] else "green"
        return verdict, ("FAILED tests/test_x.py::test_y" if verdict == "red" else "")
    monkeypatch.setattr(commit_push, "run_test_gate", fake_gate)
    monkeypatch.setattr(escalation_wait, "wait_for_answer",
                        lambda n, ask, **k: gate["answers"].pop(0) if gate["answers"] else None)
    return World(tmp_path), gate


def _leave_fix_uncommitted(world, gate, reviewer, leftover):
    """Run 1: the reviewer's SUBSTANTIVE finding is fixed, and the run hands back
    with that fix still uncommitted in the worktree."""
    reviewer.post(1, "f1", "[substantive] the null check in engine is missing")
    if leftover == "hook-rejected":
        world.install_hook({1})          # rejects the first commit attempt only
    else:
        gate["verdicts"] = ["red"]
        gate["answers"] = ["2" if leftover == "red-gate-stop" else None]
    outcome = make_driver(world, reviewer, {"f1": writes("engine.py", SUBST)},
                          test_gate=leftover != "hook-rejected").run()
    assert outcome.status in ("stopped", "needs-human") and not outcome.merged
    assert world.remote_tip() == world.h0 and world.local_head() == world.h0
    assert world.dirty() and SUBST in (world.repo / "engine.py").read_text()
    gate["verdicts"], gate["answers"] = [], []


def _cosmetic_run(world, reviewer, mode, nit):
    """Run 2 on the same worktree: the old finding reads as already fixed, and the
    only new work is a COSMETIC nit — posted in answer to this run's first summon
    ("fresh") or already on the PR when it starts ("pre")."""
    k = len(world.summons) + (1 if nit == "fresh" else 0)
    reviewer.post(k, "n2", "[cosmetic] nit: wording in style")
    driver = make_driver(world, reviewer,
                         {"f1": already_fixed, "n2": writes("style.py", COSM)},
                         test_gate=True, rr_active=mode == "rr-active",
                         preflight=mode in ("preflight", "rr-active"))
    start = world.local_head()
    return driver, driver.run(), start


LEFTOVERS = ["red-gate-stop", "red-gate-unanswered", "hook-rejected"]
# (preflight, fresh) is absent on purpose: a preflight run does not summon a reviewer
# whose comments are already on the PR, so the fresh nit never arrives and there is
# no cosmetic commit to carry anything.
SECOND_RUNS = [("plain", "fresh"), ("plain", "pre"), ("preflight", "pre"),
               ("rr-active", "fresh"), ("rr-active", "pre")]


@pytest.mark.parametrize("mode,nit", SECOND_RUNS)
@pytest.mark.parametrize("leftover", LEFTOVERS)
def test_a_leftover_substantive_fix_is_never_merged_on_an_earlier_review(env, leftover,
                                                                        mode, nit):
    world, gate = env
    reviewer = Reviewer(world)
    _leave_fix_uncommitted(world, gate, reviewer, leftover)

    driver, outcome, start = _cosmetic_run(world, reviewer, mode, nit)

    carrier = world.remote_tip()
    assert SUBST in world.markers(carrier), "the cosmetic commit carried the leftover"
    assert SUBST in world.markers(driver._last_substantive_head), (
        "the carrying commit moved the reviewed-commit boundary")
    assert any(SUBST in world.markers(tip) for tip in world.summons), (
        "the reviewer was asked to review a head that contains the leftover")
    # The reviewer stays silent, so nobody has reviewed a head carrying the fix.
    assert world.merges == [] and outcome.merged is False


@pytest.mark.parametrize("mode,nit", SECOND_RUNS)
@pytest.mark.parametrize("leftover", LEFTOVERS)
def test_the_same_cosmetic_run_on_a_cleaned_tree_merges_on_the_existing_review(
        env, leftover, mode, nit):
    world, gate = env
    reviewer = Reviewer(world)
    _leave_fix_uncommitted(world, gate, reviewer, leftover)
    git(world.repo, "reset", "-q", "--hard", "HEAD")   # the operator discards the leftover
    assert not world.dirty()
    asked_before = len(world.summons)

    driver, outcome, start = _cosmetic_run(world, reviewer, mode, nit)

    assert outcome.merged is True and world.merges == [world.remote_tip()]
    assert SUBST not in world.markers(world.merges[0])
    assert COSM in (world.repo / "style.py").read_text()
    assert driver._last_substantive_head == start, "a cosmetic-only commit keeps the boundary"
    if mode != "rr-active":
        # Nobody is asked to look at the cosmetic commit: the only summon is a plain
        # launch's round-1 summon of the head it started on. (An --rr-active restart
        # re-asks for its own reason: the old finding reported already fixed.)
        assert world.remote_tip() not in world.summons
        assert world.summons[asked_before:] == ([start] if mode == "plain" else [])


class EditingReviewer(Reviewer):
    """A reviewer whose poll window coincides with an outside edit to the shared
    checkout: the first fetch after the summon writes SUBST into engine.py."""

    def __init__(self, world):
        super().__init__(world)
        self.edited = False

    def fetch(self, pr, repo=None, cwd=None):
        if self.world.summons and not self.edited:
            self.edited = True
            with open(self.world.repo / "engine.py", "a") as fh:
                fh.write(SUBST + "\n")
        return super().fetch(pr, repo=repo, cwd=cwd)


def test_an_edit_made_during_the_poll_is_never_merged_on_an_earlier_review(env):
    world, gate = env
    reviewer = EditingReviewer(world)
    reviewer.post(1, "n2", "[cosmetic] nit: wording in style")
    assert not world.dirty()          # the round starts on a clean tree

    driver = make_driver(world, reviewer, {"n2": writes("style.py", COSM)}, test_gate=True)
    outcome = driver.run()

    assert reviewer.edited
    assert SUBST in world.markers(world.remote_tip()), "the cosmetic commit carried the edit"
    assert SUBST in world.markers(driver._last_substantive_head), (
        "the carrying commit moved the reviewed-commit boundary")
    assert any(SUBST in world.markers(tip) for tip in world.summons), (
        "the reviewer was asked to review a head that contains the edit")
    assert world.merges == [] and outcome.merged is False


def _assert_edit_needs_its_own_review(world, driver, outcome):
    assert SUBST in world.markers(world.remote_tip()), "the cosmetic commit carried the edit"
    assert SUBST in world.markers(driver._last_substantive_head), (
        "the carrying commit moved the reviewed-commit boundary")
    assert any(SUBST in world.markers(tip) for tip in world.summons), (
        "the reviewer was asked to review a head that contains the edit")
    assert world.merges == [] and outcome.merged is False


def _edit_engine(world):
    with open(world.repo / "engine.py", "a") as fh:
        fh.write(SUBST + "\n")


def test_an_edit_made_during_classification_is_never_merged_on_an_earlier_review(env):
    # The classification pass (one model call per comment) runs after the poll and
    # before the first fixer; an outside edit landing while it runs is sampled too.
    world, gate = env
    reviewer = Reviewer(world)
    reviewer.post(1, "n2", "[cosmetic] nit: wording in style")
    driver = make_driver(world, reviewer, {"n2": writes("style.py", COSM)}, test_gate=True)

    def classify_during_an_edit(prompt):
        if not world.dirty():
            _edit_engine(world)
        return classify(prompt)
    driver.classify_runner = classify_during_an_edit

    outcome = driver.run()

    _assert_edit_needs_its_own_review(world, driver, outcome)


def test_an_edit_made_during_the_test_gate_is_never_merged_on_an_earlier_review(
        env, monkeypatch):
    # The fixers have finished and the gate is running: an outside edit lands in the
    # tree the commit step is about to stage.
    world, gate = env
    reviewer = Reviewer(world)
    reviewer.post(1, "n2", "[cosmetic] nit: wording in style")
    edits = []

    def gate_during_an_edit(cwd, repo=None, run=None, notice=None, **k):
        if not edits:
            edits.append(True)
            _edit_engine(world)
        return "green", ""
    monkeypatch.setattr(commit_push, "run_test_gate", gate_during_an_edit)

    driver = make_driver(world, reviewer, {"n2": writes("style.py", COSM)}, test_gate=True)
    outcome = driver.run()

    assert edits
    _assert_edit_needs_its_own_review(world, driver, outcome)


@pytest.mark.parametrize("operator_edits", [True, False])
def test_an_operator_repair_at_a_red_gate_is_never_merged_on_an_earlier_review(
        env, monkeypatch, operator_edits):
    # The gate goes red after a COSMETIC fix and the operator answers "I've fixed it —
    # re-run the gate & continue". Their repair is committed with the round's fix and
    # has never been reviewed. The twin: the operator re-runs a flaky gate without
    # touching the tree, and the cosmetic commit still merges on the review in hand.
    world, gate = env
    reviewer = Reviewer(world)
    reviewer.post(1, "n2", "[cosmetic] nit: wording in style")
    gate["verdicts"] = ["red"]

    def operator_answers(n, ask, **k):
        if operator_edits:
            _edit_engine(world)
        return "3"
    monkeypatch.setattr(escalation_wait, "wait_for_answer", operator_answers)

    driver = make_driver(world, reviewer, {"n2": writes("style.py", COSM)}, test_gate=True)
    start = world.local_head()
    outcome = driver.run()

    assert COSM in (world.repo / "style.py").read_text()
    if operator_edits:
        _assert_edit_needs_its_own_review(world, driver, outcome)
    else:
        assert outcome.merged is True and world.merges == [world.remote_tip()]
        assert driver._last_substantive_head == start, (
            "a commit holding only the fixers' output keeps the boundary")


def test_the_commit_check_vouches_only_for_what_the_fixers_left_on_disk(env):
    # Direct form of the comparison: a commit holding exactly what the fixers left
    # (an edit, a deletion, a new symlink) is theirs; any other content, or a
    # fingerprint that could not be taken, is not.
    world, gate = env
    driver = make_driver(world, Reviewer(world), {}, test_gate=False)
    assert driver._commit_carries_foreign(None) is True

    def fixers_write(edit):
        driver._round_review_head = world.local_head()
        edit()
        return driver._fixer_output_fingerprint()

    def commit():
        git(world.repo, "add", "-A")
        git(world.repo, "commit", "-qm", "round")

    def fixes():
        with open(world.repo / "style.py", "a") as fh:
            fh.write(COSM + "\n")
        (world.repo / "x.py").unlink()
        os.symlink("style.py", world.repo / "link")
    output = fixers_write(fixes)
    commit()
    assert driver._commit_carries_foreign(output) is False

    output = fixers_write(lambda: writes("style.py", COSM)(world))
    _edit_engine(world)                     # a file the fixers never touched
    commit()
    assert driver._commit_carries_foreign(output) is True

    output = fixers_write(lambda: writes("style.py", COSM)(world))
    writes("style.py", SUBST)(world)        # more content in the file they did touch
    commit()
    assert driver._commit_carries_foreign(output) is True

    output = fixers_write(lambda: writes("style.py", COSM)(world))
    driver._round_review_head = "0" * 40    # someone committed under the fixers
    commit()
    assert driver._commit_carries_foreign(output) is True


def test_the_commit_check_does_not_vouch_for_an_executable_bit_changed_after_the_fixers(env):
    # A chmod leaves the blob sha alone, so the mode is part of the identity: one made
    # after the fixers finished (the test gate, an operator repair) is foreign, while
    # the fixers' own chmod, made before the fingerprint, is theirs.
    world, gate = env
    driver = make_driver(world, Reviewer(world), {}, test_gate=False)

    def commit():
        git(world.repo, "add", "-A")
        git(world.repo, "commit", "-qm", "round")

    def fixers_write_then(after):
        driver._round_review_head = world.local_head()
        writes("style.py", COSM)(world)
        output = driver._fixer_output_fingerprint()
        after()
        commit()
        return output

    def chmod(bits):
        return lambda: os.chmod(world.repo / "style.py", bits)

    assert driver._commit_carries_foreign(fixers_write_then(lambda: None)) is False
    assert driver._commit_carries_foreign(fixers_write_then(chmod(0o755))) is True, (
        "a chmod +x after the fingerprint, with the blob unchanged, went unnoticed")
    assert driver._commit_carries_foreign(fixers_write_then(chmod(0o644))) is True, (
        "a chmod -x after the fingerprint, with the blob unchanged, went unnoticed")

    driver._round_review_head = world.local_head()
    os.chmod(world.repo / "style.py", 0o755)
    writes("style.py", COSM)(world)
    output = driver._fixer_output_fingerprint()
    commit()
    assert driver._commit_carries_foreign(output) is False, (
        "an executable bit the fixers themselves set is theirs")


def test_the_commit_check_ignores_a_disk_chmod_when_git_ignores_the_executable_bit(env):
    # With core.filemode off ``git add`` keeps the recorded mode whatever the disk
    # says, so a chmod the fixers made cannot mismatch the commit and is not foreign.
    world, gate = env
    git(world.repo, "config", "core.filemode", "false")
    driver = make_driver(world, Reviewer(world), {}, test_gate=False)
    driver._round_review_head = world.local_head()
    writes("style.py", COSM)(world)
    os.chmod(world.repo / "style.py", 0o755)
    output = driver._fixer_output_fingerprint()
    git(world.repo, "add", "-A")
    git(world.repo, "commit", "-qm", "round")
    assert git(world.repo, "ls-tree", "HEAD", "style.py").startswith("100644")
    assert driver._commit_carries_foreign(output) is False


@pytest.mark.parametrize("chmod_flag, committed", [("+x", "100755"), ("-x", "100644")])
def test_the_commit_check_does_not_vouch_for_an_index_chmod_after_the_fixers_when_filemode_is_off(
        env, chmod_flag, committed):
    # With core.filemode off the mode the commit stages is the INDEX's, so a
    # ``git update-index --chmod`` made after the fixers finished (a hook, an
    # operator) is an unreviewed change the blob sha does not show.
    world, gate = env
    git(world.repo, "config", "core.filemode", "false")
    driver = make_driver(world, Reviewer(world), {}, test_gate=False)
    if chmod_flag == "-x":
        git(world.repo, "update-index", "--chmod=+x", "style.py")
        git(world.repo, "commit", "-qm", "make style.py executable")
    driver._round_review_head = world.local_head()
    writes("style.py", COSM)(world)
    output = driver._fixer_output_fingerprint()
    git(world.repo, "update-index", f"--chmod={chmod_flag}", "style.py")
    git(world.repo, "add", "-A")
    git(world.repo, "commit", "-qm", "round")
    assert git(world.repo, "ls-tree", "HEAD", "style.py").startswith(committed)
    assert driver._commit_carries_foreign(output) is True, (
        "an index chmod after the fingerprint went unnoticed with core.filemode off")


def test_the_commit_check_vouches_for_an_index_chmod_the_fixers_made_when_filemode_is_off(env):
    # The fixers' own ``update-index --chmod`` is in the index before the fingerprint
    # is read, so the mode it records is theirs.
    world, gate = env
    git(world.repo, "config", "core.filemode", "false")
    driver = make_driver(world, Reviewer(world), {}, test_gate=False)
    driver._round_review_head = world.local_head()
    writes("style.py", COSM)(world)
    git(world.repo, "update-index", "--chmod=+x", "style.py")
    output = driver._fixer_output_fingerprint()
    git(world.repo, "add", "-A")
    git(world.repo, "commit", "-qm", "round")
    assert git(world.repo, "ls-tree", "HEAD", "style.py").startswith("100755")
    assert driver._commit_carries_foreign(output) is False


def test_the_commit_check_vouches_for_a_new_file_when_filemode_is_off(env):
    # A path the index does not hold is staged as ``100644`` whatever the disk says.
    world, gate = env
    git(world.repo, "config", "core.filemode", "false")
    driver = make_driver(world, Reviewer(world), {}, test_gate=False)
    driver._round_review_head = world.local_head()
    (world.repo / "fresh.py").write_text("x = 1\n")
    os.chmod(world.repo / "fresh.py", 0o755)
    output = driver._fixer_output_fingerprint()
    git(world.repo, "add", "-A")
    git(world.repo, "commit", "-qm", "round")
    assert git(world.repo, "ls-tree", "HEAD", "fresh.py").startswith("100644")
    assert driver._commit_carries_foreign(output) is False


def test_a_reviewer_parked_earlier_that_speaks_again_is_re_asked_for_a_carrying_commit(
        env, monkeypatch):
    # The only reviewer was parked polish-only before this round (an --rr-active
    # restore does exactly this), then a delayed cosmetic comment of its own reaches
    # the round's batch. An edit lands in the tree during the test gate, so the
    # round's commit carries it. Nobody else is expected, and the reviewer whose
    # comment the round acted on must be asked to review the new head — otherwise
    # the run clean-exits with a head nobody reviewed.
    world, gate = env
    reviewer = Reviewer(world)
    edits = []

    def gate_during_an_edit(cwd, repo=None, run=None, notice=None, **k):
        if not edits:
            edits.append(True)
            _edit_engine(world)
        return "green", ""
    monkeypatch.setattr(commit_push, "run_test_gate", gate_during_an_edit)

    driver = make_driver(world, reviewer, {"n2": writes("style.py", COSM)}, test_gate=True)
    driver.polishing.add("claude")
    assert driver.expected_bots() == []
    driver._preflight_batch = [Comment(
        id="n2", text="[cosmetic] nit: wording in style", source="claude[bot]",
        path="x.py", diff_hunk="@@ -1 +1 @@", created_at="2026-01-01T00:30:00+00:00")]

    outcome = driver.run()

    assert edits
    _assert_edit_needs_its_own_review(world, driver, outcome)


def test_a_done_reviewer_that_speaks_this_round_is_re_asked_for_a_carrying_commit(
        env, monkeypatch):
    # The only reviewer already signed off (done) and, in the same round, posts an
    # actionable cosmetic comment — ``_update_polishing`` parks it in neither set.
    # An edit lands in the tree during the test gate, so the round's commit carries
    # it. Nobody else is expected, so the done reviewer that spoke this round must
    # be re-admitted and asked to review the new head — otherwise the next round
    # clean-exits without the review the merge gate demands.
    world, gate = env
    reviewer = Reviewer(world)
    edits = []

    def gate_during_an_edit(cwd, repo=None, run=None, notice=None, **k):
        if not edits:
            edits.append(True)
            _edit_engine(world)
        return "green", ""
    monkeypatch.setattr(commit_push, "run_test_gate", gate_during_an_edit)

    driver = make_driver(world, reviewer, {"n2": writes("style.py", COSM)}, test_gate=True)
    driver.done.add("claude")
    driver.approved.add("claude")
    assert driver.expected_bots() == []
    driver._preflight_batch = [Comment(
        id="n2", text="[cosmetic] nit: wording in style", source="claude[bot]",
        path="x.py", diff_hunk="@@ -1 +1 @@", created_at="2026-01-01T00:30:00+00:00")]

    outcome = driver.run()

    assert edits
    _assert_edit_needs_its_own_review(world, driver, outcome)


def test_a_cosmetic_round_driven_from_a_relative_cwd_merges_on_the_existing_review(
        env, monkeypatch):
    # ``--cwd`` accepts a relative path. The fixers' output must then be read from
    # the same files git hashes; otherwise it cannot be fingerprinted, a purely
    # COSMETIC commit reads as one carrying unreviewed content, and the PR is held
    # for a review it does not need.
    world, gate = env
    monkeypatch.chdir(world.repo.parent)
    reviewer = Reviewer(world)
    reviewer.post(1, "n2", "[cosmetic] nit: wording in style")
    driver = make_driver(world, reviewer, {"n2": writes("style.py", COSM)},
                         test_gate=True, cwd=world.repo.name)
    start = world.local_head()

    outcome = driver.run()

    assert COSM in git(world.repo, "show", f"{world.remote_tip()}:style.py")
    assert world.remote_tip() != start, "the cosmetic fix was committed and pushed"
    assert driver._last_substantive_head == start, "a cosmetic-only commit keeps the boundary"
    assert world.remote_tip() not in world.summons, (
        "nobody is asked to review a commit holding only the fixers' output")
    assert outcome.merged is True and world.merges == [world.remote_tip()]


def _executable_style(world, filemode):
    """``style.py`` committed as ``100755`` under the given ``core.filemode``, so the
    mode the fingerprint records is read from the disk or from the index."""
    git(world.repo, "config", "core.filemode", filemode)
    os.chmod(world.repo / "style.py", 0o755)
    git(world.repo, "update-index", "--chmod=+x", "style.py")
    git(world.repo, "commit", "-qm", "make style.py executable")


def _driver_cwds(world):
    """Every spelling of the driver's directory the fingerprint has to resolve the
    way git does: relative to the process's own directory, the repository's top or
    a subdirectory of it, and a symlink that points INTO a subdirectory (where
    ``..`` taken lexically lands outside the repository)."""
    (world.repo / "pkg").mkdir()
    (world.repo / "pkg" / "mod.py").write_text("m = 1\n")
    git(world.repo, "add", "pkg/mod.py")
    git(world.repo, "commit", "-qm", "add pkg")
    os.symlink(world.repo / "pkg", world.repo.parent / "pkg-link")
    return {"relative-top": world.repo.name,
            "relative-subdir": os.path.join(world.repo.name, "pkg"),
            "symlinked-subdir": str(world.repo.parent / "pkg-link"),
            "relative-symlinked-subdir": "pkg-link"}


@pytest.mark.parametrize("filemode", ["true", "false"])
@pytest.mark.parametrize("where", ["relative-top", "relative-subdir", "symlinked-subdir",
                                   "relative-symlinked-subdir"])
def test_the_commit_check_reads_the_fixers_output_wherever_the_driver_runs(
        env, monkeypatch, where, filemode):
    # Direct form: a commit holding exactly what the fixers left is theirs, and one
    # carrying an edit they never made is not, whatever spelling of the worktree
    # the driver was given.
    world, gate = env
    _executable_style(world, filemode)
    cwd = _driver_cwds(world)[where]
    monkeypatch.chdir(world.repo.parent)
    driver = make_driver(world, Reviewer(world), {}, test_gate=False, cwd=cwd)

    def fixers_write():
        driver._round_review_head = world.local_head()
        writes("style.py", COSM)(world)
        (world.repo / "pkg" / "mod.py").unlink()
        return driver._fixer_output_fingerprint()

    def commit():
        git(world.repo, "add", "-A")
        git(world.repo, "commit", "-qm", "round")

    output = fixers_write()
    assert output is not None, "the fixers' output could not be read"
    assert output[1] == {
        "style.py": ("file", git(world.repo, "hash-object", "style.py"), "100755"),
        "pkg/mod.py": ("deleted",)}
    commit()
    assert git(world.repo, "ls-tree", "HEAD", "style.py").startswith("100755")
    assert driver._commit_carries_foreign(output) is False

    git(world.repo, "checkout", "-q", "HEAD~1", "--", "pkg/mod.py")
    git(world.repo, "commit", "-qm", "restore pkg")
    output = fixers_write()
    _edit_engine(world)                     # a file the fixers never touched
    commit()
    assert driver._commit_carries_foreign(output) is True


@pytest.mark.parametrize("where", ["relative-top", "relative-subdir", "symlinked-subdir"])
def test_the_index_mode_read_is_anchored_at_the_top_of_the_repository(
        env, monkeypatch, where):
    # With core.filemode off the mode is read from the index. ``ls-files`` answers a
    # path that resolves outside the index with no output and exit 0, which would
    # silently read every file as ``100644``; the read must name each path from the
    # top of the repository, not from wherever the driver happens to run.
    world, gate = env
    _executable_style(world, "false")
    cwd = _driver_cwds(world)[where]
    monkeypatch.chdir(world.repo.parent)
    driver = make_driver(world, Reviewer(world), {}, test_gate=False, cwd=cwd)
    (world.repo / "fresh.py").write_text("f = 1\n")

    assert driver._indexed_file_modes(["style.py", "pkg/mod.py", "fresh.py"]) == {
        "style.py": "100755", "pkg/mod.py": "100644", "fresh.py": "100644"}


# ---------------------------------------------------------------------------
# Races with the record itself. What the fixers left is read in several steps per
# path: an lstat for its kind and executable bit, then ``hash-object`` or
# ``readlink`` for its content, and the index for its mode when core.filemode is
# off. Comparing the commit with that record takes several more. Another process
# that writes the path, the index or a commit between two of those reads must
# never have its content read as the fixers': the record then counts as not taken,
# and the head goes to a reviewer. Each race is injected just before the read it
# slips in front of, and each has a twin in which nothing moves, whose commit
# still rides the review in hand.
# ---------------------------------------------------------------------------

HASH = ["git", "hash-object"]
DIFF = ["git", "diff-tree"]
HEAD_READ = ["git", "rev-parse", "HEAD"]


def interleave(driver, during, *steps):
    """While ``driver.<during>`` runs, perform each ``(argv prefix, action)`` step
    once, in order, just before the first git command it issues that starts with
    that prefix. Returns the prefixes that fired."""
    inner_run, method = driver.gh_run, getattr(driver, during)
    pending, fired, active = list(steps), [], []

    def run(argv, **kw):
        if active and pending and list(argv[:len(pending[0][0])]) == pending[0][0]:
            prefix, action = pending.pop(0)
            action()
            fired.append(prefix)
        return inner_run(argv, **kw)

    def wrapped(*args, **kwargs):
        active.append(True)
        try:
            return method(*args, **kwargs)
        finally:
            active.pop()

    driver.gh_run = run
    setattr(driver, during, wrapped)
    return fired


def _style_at(world, sha):
    return git(world.repo, "show", f"{sha}:style.py")


def _commit(world):
    git(world.repo, "add", "-A")
    git(world.repo, "commit", "-qm", "round")


def _rewrite_style_keeping_size_and_mtime(world):
    """Another process swaps the fixers' bytes for others of the same length in
    place and puts the modification time back, as ``cp -p``, ``rsync -t`` and
    ``touch -r`` do: only the change time and the content itself tell."""
    path = world.repo / "style.py"
    was = os.lstat(path)
    path.write_bytes(path.read_bytes().replace(COSM.encode(), b"EVIL-99"))
    os.utime(path, ns=(was.st_atime_ns, was.st_mtime_ns))
    # On a coarse filesystem clock the rewrite can share the fixers' tick; a
    # later utime moves the change time on, as any later rewrite would.
    deadline = time.monotonic() + 5
    while os.lstat(path).st_ctime_ns == was.st_ctime_ns and time.monotonic() < deadline:
        time.sleep(0.002)
        os.utime(path, ns=(was.st_atime_ns, was.st_mtime_ns))
    now = os.lstat(path)
    assert (now.st_size, now.st_mtime_ns, now.st_ino) == (
        was.st_size, was.st_mtime_ns, was.st_ino)
    assert now.st_ctime_ns != was.st_ctime_ns


FILE_RACES = {
    "appended": lambda world: writes("style.py", SUBST)(world),
    "same-size-mtime-kept": _rewrite_style_keeping_size_and_mtime,
    "index-chmod": lambda world: git(world.repo, "update-index", "--chmod=+x", "style.py"),
}


def _carried(world, race):
    """Did the round's commit carry what the racing process wrote?"""
    if race == "appended":
        return SUBST in _style_at(world, "HEAD")
    if race == "same-size-mtime-kept":
        return "EVIL-99" in _style_at(world, "HEAD")
    return git(world.repo, "ls-tree", "HEAD", "style.py").startswith("100755")


@pytest.mark.parametrize("race,filemode", [
    ("appended", "true"), ("appended", "false"),
    ("same-size-mtime-kept", "true"), ("same-size-mtime-kept", "false"),
    ("index-chmod", "false")])
def test_a_fixer_touched_file_changed_while_it_is_recorded_is_not_vouched_for(
        env, race, filemode):
    # Another process changes the file the fixers touched after the record has read
    # its kind and executable bit, just before it reads the content: new bytes, the
    # same number of bytes with the modification time put back, or (core.filemode
    # off, where the commit takes the INDEX's mode) an index chmod. The round then
    # commits exactly what that process left.
    world, gate = env
    git(world.repo, "config", "core.filemode", filemode)
    driver = make_driver(world, Reviewer(world), {}, test_gate=False)
    driver._round_review_head = world.local_head()
    writes("style.py", COSM)(world)
    fired = interleave(driver, "_fixer_output_fingerprint",
                       (HASH, lambda: FILE_RACES[race](world)))

    output = driver._fixer_output_fingerprint()
    _commit(world)

    assert fired == [HASH] and _carried(world, race)
    assert driver._commit_carries_foreign(output) is True, (
        f"the {race} change made while the fixers' output was being read was "
        f"vouched for as theirs")


def test_a_symlink_the_fixers_made_retargeted_while_it_is_recorded_is_not_vouched_for(
        env, monkeypatch):
    # Between the lstat that finds the fixers' new symlink and the readlink of its
    # target, another process points it somewhere else.
    world, gate = env
    driver = make_driver(world, Reviewer(world), {}, test_gate=False)
    driver._round_review_head = world.local_head()
    link = world.repo / "link"
    os.symlink("style.py", link)
    real_readlink, swapped = os.readlink, []

    def readlink(path, *args, **kwargs):
        if os.fspath(path).endswith(os.sep + "link") and not swapped:
            swapped.append(True)
            os.unlink(link)
            os.symlink("engine.py", link)
        return real_readlink(path, *args, **kwargs)
    monkeypatch.setattr(os, "readlink", readlink)

    output = driver._fixer_output_fingerprint()
    monkeypatch.setattr(os, "readlink", real_readlink)
    _commit(world)

    assert swapped and git(world.repo, "cat-file", "-p", "HEAD:link") == "engine.py"
    assert driver._commit_carries_foreign(output) is True, (
        "a symlink retargeted between its lstat and its readlink was vouched for")


@pytest.mark.parametrize("filemode", ["true", "false"])
def test_what_the_fixers_left_is_vouched_for_when_nothing_moves_while_it_is_recorded(
        env, filemode):
    # The twin: the same reads with nothing changing under them. An edit, a
    # deletion and a new symlink are all the fixers'. With core.filemode on, an
    # index chmod made during the read is not a change either: ``git add`` then
    # records the disk's bit, so the commit still holds exactly the fixers' output.
    world, gate = env
    git(world.repo, "config", "core.filemode", filemode)
    driver = make_driver(world, Reviewer(world), {}, test_gate=False)
    driver._round_review_head = world.local_head()
    writes("style.py", COSM)(world)
    (world.repo / "x.py").unlink()
    os.symlink("style.py", world.repo / "link")
    during = ((lambda: FILE_RACES["index-chmod"](world)) if filemode == "true"
              else (lambda: None))
    fired = interleave(driver, "_fixer_output_fingerprint", (HASH, during))

    output = driver._fixer_output_fingerprint()
    _commit(world)

    assert fired == [HASH] and output is not None
    assert git(world.repo, "ls-tree", "HEAD", "style.py").startswith("100644")
    assert driver._commit_carries_foreign(output) is False


@pytest.mark.parametrize("race", ["reverted", "arrives", "reverted-and-back", "none"])
def test_a_commit_made_while_the_round_s_commit_is_compared_is_not_vouched_for(env, race):
    # The comparison reads the commit's changes and then checks them path by path.
    # A commit landing meanwhile must not change which commit is being judged:
    # "reverted" — the round's commit carries an edit the fixers never made, and a
    # revert lands just as its changes are read; "arrives" — the round's commit is
    # the fixers' own, and a commit with new content lands just as its changes are
    # read; "reverted-and-back" — that revert lands, and HEAD is put back on the
    # carrying commit just before the comparison reads HEAD again. The twin: nothing
    # lands, and the fixers' own commit is theirs.
    world, gate = env
    driver = make_driver(world, Reviewer(world), {}, test_gate=False)
    driver._round_review_head = world.local_head()
    writes("style.py", COSM)(world)
    output = driver._fixer_output_fingerprint()
    assert output is not None
    if race in ("reverted", "reverted-and-back"):
        _edit_engine(world)                 # after the record: the commit carries it
    _commit(world)
    judged = world.local_head()

    def revert():
        git(world.repo, "checkout", "-q", "HEAD~1", "--", "engine.py")
        git(world.repo, "commit", "-qm", "revert engine.py")

    def arrive():
        _edit_engine(world)
        _commit(world)

    steps = {"reverted": [(DIFF, revert)],
             "arrives": [(DIFF, arrive)],
             "reverted-and-back": [
                 (DIFF, revert),
                 (HEAD_READ, lambda: git(world.repo, "reset", "-q", "--soft", judged))],
             "none": [(DIFF, lambda: None)]}[race]
    interleave(driver, "_commit_carries_foreign", *steps)

    verdict = driver._commit_carries_foreign(output)

    if race == "none":
        assert verdict is False, "the fixers' own commit was not vouched for"
    else:
        assert verdict is True, (
            f"a commit that {race} during the comparison was vouched for")


@pytest.mark.parametrize("filemode", ["true", "false"])
@pytest.mark.parametrize("edited", [True, False])
def test_an_edit_made_while_the_fixers_output_is_recorded_is_never_merged_on_an_earlier_review(
        env, edited, filemode):
    # A COSMETIC round on a clean tree. While the loop reads what its fixer left,
    # another process appends to the very file the fixer touched; the round's
    # commit carries that edit, and no reviewer has seen it. The twin: nobody
    # touches the tree, and the cosmetic commit merges on the review in hand with
    # nobody asked again.
    world, gate = env
    git(world.repo, "config", "core.filemode", filemode)
    reviewer = Reviewer(world)
    reviewer.post(1, "n2", "[cosmetic] nit: wording in style")
    driver = make_driver(world, reviewer, {"n2": writes("style.py", COSM)}, test_gate=True)
    fired = interleave(driver, "_fixer_output_fingerprint", (HASH, (
        (lambda: writes("style.py", SUBST)(world)) if edited else (lambda: None))))
    start = world.local_head()

    outcome = driver.run()

    tip = world.remote_tip()
    assert fired == [HASH] and COSM in _style_at(world, tip)
    if edited:
        assert SUBST in _style_at(world, tip), "the cosmetic commit carried the edit"
        assert SUBST in _style_at(world, driver._last_substantive_head), (
            "the carrying commit moved the reviewed-commit boundary")
        assert any(SUBST in _style_at(world, t) for t in world.summons), (
            "the reviewer was asked to review a head that contains the edit")
        assert world.merges == [] and outcome.merged is False
    else:
        assert outcome.merged is True and world.merges == [tip]
        assert driver._last_substantive_head == start, (
            "a commit holding only the fixers' output keeps the boundary")
        assert world.summons == [start], "nobody is asked to review the cosmetic commit"


# ---------------------------------------------------------------------------
# The fallback reviewers. A round that pushes changes no reviewer has seen — a
# SUBSTANTIVE fix, or a commit carrying a fix an earlier run left uncommitted —
# moves the reviewed-commit boundary, and the run takes another round to get that
# head reviewed. The reviewer that round asks may stay silent, answer only with a
# can't-review notice, be inside a usage-limit window, or answer with a comment
# written against an older head. The reviewers the run set aside (polish-only, or
# done) are the fallback: rather than end the run with the head unreviewed, the
# loop asks each of them once more while a round remains. Every case has a COSMETIC
# twin on a clean tree: nothing moved the boundary, the PR merges on the review in
# hand, and nobody is summoned after round 1.
# ---------------------------------------------------------------------------

TWO = {"active_reviewers": ["claude", "codex"],
       "auto_on_open": {"claude": False, "codex": False}}
LOGINS = {"claude": "claude[bot]", "codex": "chatgpt-codex-connector[bot]"}
# What the reviewer asked about the carrying head answers with, instead of a review.
NO_REVIEW = {
    "silent": None,
    "too-large": "The pull request is too large to review.",
    "errored": "Codex encountered an unexpected error while reviewing this PR.",
    "quota": "Codex usage limit reached: you have exhausted your quota for this period.",
}
FORMS = ["substantive", "leftover"]


class Fleet:
    """claude and codex, each answering its own summons. An entry becomes visible
    once ``after`` (by default its own reviewer) has been summoned ``k`` times
    (k=0: already on the PR). An inline finding is anchored to the remote tip that
    summon asked about, or to ``anchor`` for a comment written against an older
    head; anything else (an acknowledgment, a sign-off, a can't-review notice) is
    posted on the conversation and anchors nothing. After its script a reviewer is
    silent."""

    def __init__(self, world):
        self.world = world
        self.script = []
        self.anchor = {}

    def asked(self, bot):
        return {"claude": self.world.summons, "codex": self.world.codex_summons}[bot]

    def post(self, bot, k, cid, text, *, inline=True, after=None, anchor=None,
             source=None):
        c = Comment(id=cid, text=text, source=source or LOGINS[bot],
                    path="x.py" if inline else None,
                    diff_hunk="@@ -1 +1 @@" if inline else None,
                    from_issue_channel=not inline,
                    created_at="2026-01-01T00:30:00+00:00")
        self.script.append((after or bot, k, c, anchor))

    def visible(self):
        out = []
        for after, k, c, anchor in self.script:
            asked = self.asked(after)
            if len(asked) < k:
                continue
            if c.path and c.id not in self.anchor:
                self.anchor[c.id] = anchor or (asked[k - 1] if k else self.world.h0)
            out.append(c)
        return out

    def fetch(self, pr, repo=None, cwd=None):
        return self.visible()

    def inline(self, pr, repo=None, cwd=None):
        return [{"user": {"login": c.source}, "original_commit_id": self.anchor[c.id]}
                for c in self.visible() if c.path]


def _fleet_run(world, fleet, behaviours, max_rounds=3):
    driver = make_driver(world, fleet, behaviours, test_gate=True, cfg=TWO,
                         max_rounds=max_rounds)
    return driver, driver.run()


def _round_one(world, fleet, form, *, cosmetic):
    """Round 1 asks claude and codex about H0. claude's nit is fixed, which parks
    claude polish-only; codex stays expected. ``form`` is how the round's commit
    comes to carry changes no reviewer has seen:

    * "substantive" — codex's finding is SUBSTANTIVE and its fix lands (in the
      COSMETIC twin the same finding is cosmetic);
    * "leftover" — a fix an earlier run left uncommitted is still on disk and the
      commit carries it, while codex has only acknowledged the PR (in the twin the
      tree is clean).

    Returns the fixers for the round-1 comments."""
    fleet.post("claude", 1, "n1", "[cosmetic] nit: wording in style")
    behaviours = {"n1": writes("style.py", COSM)}
    if form == "substantive":
        label = "[cosmetic]" if cosmetic else "[substantive]"
        fleet.post("codex", 1, "f1", f"{label} the null check in engine is missing")
        behaviours["f1"] = writes("engine.py", "COSM-f1" if cosmetic else SUBST)
    else:
        fleet.post("codex", 1, "a1", "Codex is reviewing this pull request.",
                   inline=False)
        if not cosmetic:
            _edit_engine(world)            # the leftover, uncommitted before the run
    return behaviours


def _assert_merged_on_the_review_in_hand(world, driver, outcome):
    # The cosmetic twin: the commit moved nothing, so the PR merges on round 1's
    # reviews of H0, and neither reviewer is summoned after round 1.
    assert outcome.merged is True and world.merges == [world.remote_tip()]
    assert SUBST not in world.markers(world.remote_tip())
    assert driver._last_substantive_head == world.h0
    assert world.summons == [world.h0] and world.codex_summons == [world.h0]


@pytest.mark.parametrize("answer", sorted(NO_REVIEW))
@pytest.mark.parametrize("form", FORMS)
def test_a_carrying_head_the_expected_reviewer_does_not_review_goes_to_the_fallback(
        env, form, answer):
    # Round 2 asks codex — the reviewer still expected — about the carrying head
    # H1, and codex does not review it. claude, parked by round 1, is the fallback:
    # round 3 asks it about H1, its review arrives, and the PR merges on it.
    world, gate = env
    fleet = Fleet(world)
    behaviours = _round_one(world, fleet, form, cosmetic=False)
    if NO_REVIEW[answer]:
        fleet.post("codex", 2, "x2", NO_REVIEW[answer], inline=False)
    fleet.post("claude", 2, "n3", "[cosmetic] nit: docstring in style")
    behaviours["n3"] = writes("style.py", "COSM-n3")

    driver, outcome = _fleet_run(world, fleet, behaviours)

    h1 = driver._last_substantive_head
    assert SUBST in world.markers(h1), "round 1's commit carried the unseen change"
    assert world.codex_summons == [world.h0, h1], "codex was asked about H1"
    assert world.summons == [world.h0, h1], "the parked reviewer was asked about H1"
    assert outcome.merged is True and world.merges == [world.remote_tip()]
    assert "COSM-n3" in git(world.repo, "show", f"{world.remote_tip()}:style.py")


@pytest.mark.parametrize("answer", sorted(NO_REVIEW))
@pytest.mark.parametrize("form", FORMS)
def test_the_cosmetic_twin_of_an_unreviewed_carrying_head_merges_with_nobody_asked_again(
        env, form, answer):
    # The same script, but codex's finding is cosmetic (or the tree is clean): round
    # 1's commit holds only its fixers' cosmetic output, so the run ends there and
    # codex's round-2 answer and claude's round-3 nit are never reached.
    world, gate = env
    fleet = Fleet(world)
    behaviours = _round_one(world, fleet, form, cosmetic=True)
    if NO_REVIEW[answer]:
        fleet.post("codex", 2, "x2", NO_REVIEW[answer], inline=False)
    fleet.post("claude", 2, "n3", "[cosmetic] nit: docstring in style")
    behaviours["n3"] = writes("style.py", "COSM-n3")

    driver, outcome = _fleet_run(world, fleet, behaviours)

    _assert_merged_on_the_review_in_hand(world, driver, outcome)


def test_nobody_set_aside_is_asked_again_once_the_expected_reviewer_reviews_the_head(env):
    # codex, asked about the carrying head H1, reviews it (a nit anchored to H1).
    # That review is all the merge gate needs: claude stays parked.
    world, gate = env
    fleet = Fleet(world)
    behaviours = _round_one(world, fleet, "substantive", cosmetic=False)
    fleet.post("codex", 2, "n2", "[cosmetic] nit: rename in engine")
    behaviours["n2"] = writes("engine.py", "COSM-n2b")

    driver, outcome = _fleet_run(world, fleet, behaviours)

    h1 = driver._last_substantive_head
    assert world.codex_summons == [world.h0, h1]
    assert world.summons == [world.h0], "claude was not asked again"
    assert "claude" in driver.polishing
    assert outcome.merged is True and world.merges == [world.remote_tip()]


def test_a_carrying_round_that_leaves_nobody_expected_asks_the_set_aside_reviewers(
        env, capsys):
    # codex posts its SUBSTANTIVE finding together with a sign-off, so it is done
    # as well as fixed-for; claude is parked. Round 2 opens with nobody expected
    # and H1 unreviewed: the set-aside reviewers are asked about it instead of the
    # run ending there. A reviewer this repository does not enable (gemini, which
    # signed off too) is not one of them.
    world, gate = env
    fleet = Fleet(world)
    behaviours = _round_one(world, fleet, "substantive", cosmetic=False)
    fleet.post("codex", 1, "s1", "No issues found.", inline=False)
    fleet.post("gemini", 1, "g1", "No issues found.", inline=False, after="codex",
               source="gemini-code-assist[bot]")
    fleet.post("claude", 2, "n3", "[cosmetic] nit: docstring in style")
    behaviours["n3"] = writes("style.py", "COSM-n3")

    driver, outcome = _fleet_run(world, fleet, behaviours)

    h1 = driver._last_substantive_head
    assert SUBST in world.markers(h1)
    assert world.summons == [world.h0, h1] and world.codex_summons == [world.h0, h1]
    assert outcome.merged is True and world.merges == [world.remote_tip()]
    assert "re-asking claude, codex\n" in capsys.readouterr().out


def test_the_cosmetic_twin_of_a_round_that_leaves_nobody_expected_merges(env):
    world, gate = env
    fleet = Fleet(world)
    behaviours = _round_one(world, fleet, "substantive", cosmetic=True)
    fleet.post("codex", 1, "s1", "No issues found.", inline=False)

    driver, outcome = _fleet_run(world, fleet, behaviours)

    _assert_merged_on_the_review_in_hand(world, driver, outcome)


def _late_nit_round(world, fleet, *, cosmetic):
    """Round 1 as in the "substantive" form; in round 2 codex stays silent and the
    only comment is a late nit from claude written against H0."""
    behaviours = _round_one(world, fleet, "substantive", cosmetic=cosmetic)
    fleet.post("claude", 2, "n2", "[cosmetic] nit: spacing in style", after="codex",
               anchor=world.h0)
    behaviours["n2"] = writes("style.py", "COSM-n2b")
    fleet.post("claude", 2, "n3", "[cosmetic] nit: docstring in style")
    behaviours["n3"] = writes("style.py", "COSM-n3")
    return behaviours


def test_a_round_that_acts_only_on_a_comment_about_an_older_head_asks_the_fallback(env):
    # Round 2's only comment is claude's late nit on H0. Its fix is committed (H2)
    # and the round would finish clean — with nobody having reviewed a head at or
    # after H1. claude, parked, is asked about H2 instead, and its review lands.
    world, gate = env
    fleet = Fleet(world)
    behaviours = _late_nit_round(world, fleet, cosmetic=False)

    driver, outcome = _fleet_run(world, fleet, behaviours)

    h1 = driver._last_substantive_head
    assert SUBST in world.markers(h1)
    h2 = world.summons[-1]
    assert h2 != h1 and "COSM-n2b" in git(world.repo, "show", f"{h2}:style.py")
    assert world.summons == [world.h0, h2], "claude was asked about the head after H1"
    assert outcome.merged is True and world.merges == [world.remote_tip()]


def test_the_cosmetic_twin_of_a_late_comment_round_merges_with_nobody_asked_again(env):
    world, gate = env
    fleet = Fleet(world)
    behaviours = _late_nit_round(world, fleet, cosmetic=True)

    driver, outcome = _fleet_run(world, fleet, behaviours)

    _assert_merged_on_the_review_in_hand(world, driver, outcome)


def _usage_limited_round(world, fleet, *, cosmetic):
    """The roles swapped: claude's finding is the one fixed in round 1 and codex's
    nit parks codex. When round 2 asks claude, the review workflow reports claude's
    usage window exhausted until long after the run."""
    fleet.post("codex", 1, "n1", "[cosmetic] nit: wording in style")
    label = "[cosmetic]" if cosmetic else "[substantive]"
    fleet.post("claude", 1, "f1", f"{label} the null check in engine is missing")
    fleet.post("claude", 2, "m2",
               "<!-- claude-review-unavailable-v1 type=rate_limited "
               "resets_at=4000000000 -->", inline=False, source="github-actions[bot]")
    fleet.post("codex", 2, "n3", "[cosmetic] nit: docstring in style")
    return {"n1": writes("style.py", COSM),
            "f1": writes("engine.py", "COSM-f1" if cosmetic else SUBST),
            "n3": writes("style.py", "COSM-n3")}


def test_a_reviewer_inside_a_usage_limit_window_hands_the_carrying_head_to_the_fallback(
        env):
    world, gate = env
    fleet = Fleet(world)
    behaviours = _usage_limited_round(world, fleet, cosmetic=False)

    driver, outcome = _fleet_run(world, fleet, behaviours)

    h1 = driver._last_substantive_head
    assert SUBST in world.markers(h1)
    assert world.summons == [world.h0, h1], "claude was asked about H1"
    assert "claude" in driver._rate_limited_until
    assert world.codex_summons == [world.h0, h1], "the parked codex was asked about H1"
    assert outcome.merged is True and world.merges == [world.remote_tip()]


def test_the_cosmetic_twin_of_a_usage_limited_round_merges_with_nobody_asked_again(env):
    world, gate = env
    fleet = Fleet(world)
    behaviours = _usage_limited_round(world, fleet, cosmetic=True)

    driver, outcome = _fleet_run(world, fleet, behaviours)

    _assert_merged_on_the_review_in_hand(world, driver, outcome)


@pytest.mark.parametrize("fallback_answer", ["silent", "stale-sign-off"])
def test_the_fallback_asks_each_set_aside_reviewer_once_then_the_run_ends_blocked(
        env, capsys, fallback_answer):
    # A generous round budget, and nobody ever reviews H1: codex stays silent, and
    # claude, asked once as the fallback, stays silent too or answers only with a
    # sign-off written before it was asked (done again, but not a review of H1).
    # Each set-aside reviewer is asked once: the run ends at round 3, blocked.
    world, gate = env
    fleet = Fleet(world)
    behaviours = _round_one(world, fleet, "substantive", cosmetic=False)
    if fallback_answer == "stale-sign-off":
        fleet.post("claude", 2, "s3", "No issues found.", inline=False)

    driver, outcome = _fleet_run(world, fleet, behaviours, max_rounds=10)

    h1 = driver._last_substantive_head
    assert world.summons == [world.h0, h1] and world.codex_summons == [world.h0, h1]
    assert outcome.rounds == 3
    assert world.merges == [] and outcome.merged is False
    assert capsys.readouterr().out.count("; re-asking ") == 1


@pytest.mark.parametrize("path", ["nothing-to-act-on", "late-comment"])
def test_with_no_round_left_the_carrying_head_is_handed_back_blocked(env, path):
    # The round that would need the fallback is the last one the budget allows.
    # Nobody is taken back or asked; the merge gate blocks the unreviewed head.
    world, gate = env
    fleet = Fleet(world)
    if path == "late-comment":
        behaviours = _late_nit_round(world, fleet, cosmetic=False)
    else:
        behaviours = _round_one(world, fleet, "substantive", cosmetic=False)

    driver, outcome = _fleet_run(world, fleet, behaviours, max_rounds=2)

    assert world.summons == [world.h0], "claude was not asked with no round left"
    assert "claude" in driver.polishing, "and was not taken back either"
    assert outcome.rounds == 2
    assert world.merges == [] and outcome.merged is False


def test_a_cosmetic_round_whose_only_review_is_of_an_older_head_asks_nobody_again(env):
    # Nothing this run pushed carries unseen changes: claude's nit (written against
    # the commit before H0) is fixed on a clean tree. The merge gate still finds no
    # review of H0 or later and blocks — as it always has — but nobody is re-asked
    # for a cosmetic-only round.
    world, gate = env
    fleet = Fleet(world)
    older = git(world.repo, "rev-parse", "HEAD~1")
    fleet.post("claude", 1, "n1", "[cosmetic] nit: wording in style", anchor=older)
    fleet.post("codex", 1, "s1", "No issues found.", inline=False)

    driver, outcome = _fleet_run(world, fleet, {"n1": writes("style.py", COSM)})

    assert driver._last_substantive_head == world.h0
    assert world.summons == [world.h0] and world.codex_summons == [world.h0]
    assert outcome.rounds == 1
    assert world.merges == [] and outcome.merged is False


@pytest.mark.parametrize("why", ["usage-limited", "quota"])
def test_a_set_aside_reviewer_that_cannot_answer_is_not_asked(env, capsys, why):
    # claude was parked by round 1, but by the time codex leaves H1 unreviewed it
    # cannot answer: its usage window ran out in round 1 (long after the run ends),
    # or its quota notice arrived in round 2. Nobody is left to ask, so the run ends
    # blocked at round 2 without claiming to re-ask anyone.
    world, gate = env
    fleet = Fleet(world)
    behaviours = _round_one(world, fleet, "substantive", cosmetic=False)
    if why == "usage-limited":
        fleet.post("claude", 1, "m1",
                   "<!-- claude-review-unavailable-v1 type=rate_limited "
                   "resets_at=4000000000 -->", inline=False, source="github-actions[bot]")
    else:
        fleet.post("claude", 2, "q2", "Usage limit reached: you have exhausted your "
                   "quota for this period.", inline=False, after="codex")

    driver, outcome = _fleet_run(world, fleet, behaviours)

    assert "claude" in driver.polishing
    assert world.summons == [world.h0]
    assert outcome.rounds == 2
    assert world.merges == [] and outcome.merged is False
    assert "re-asking" not in capsys.readouterr().out


def test_a_reviewer_already_re_asked_for_a_carrying_commit_is_not_asked_again(env):
    # A leftover is on disk. Round 1: claude's nit is fixed (claude parked) and codex
    # signs off (done), so nobody is expected when the round's commit (H1) carries the
    # leftover: claude, which spoke, is re-asked about H1 at once. It answers only
    # with a sign-off that cannot be credited to H1 — done again, but no review of
    # it. claude has had its turn for H1; codex, set aside and not yet asked about
    # H1, is the one the fallback asks.
    world, gate = env
    fleet = Fleet(world)
    fleet.post("claude", 1, "n1", "[cosmetic] nit: wording in style")
    fleet.post("codex", 1, "s1", "No issues found.", inline=False)
    fleet.post("claude", 2, "s2", "No issues found.", inline=False)
    _edit_engine(world)

    driver, outcome = _fleet_run(world, fleet, {"n1": writes("style.py", COSM)})

    h1 = driver._last_substantive_head
    assert SUBST in world.markers(h1)
    assert world.summons == [world.h0, h1], "claude: asked about H1 once"
    assert world.codex_summons == [world.h0, h1], "codex: the fallback for H1"
    assert outcome.rounds == 3
    assert world.merges == [] and outcome.merged is False


def test_each_new_carrying_head_gets_its_own_fallback(env):
    # H1 carries codex's fix; codex leaves it unreviewed and claude, the fallback,
    # reviews it — with a SUBSTANTIVE finding of its own, whose fix makes H2 another
    # carrying head. claude, re-asked about H2 as its reviewer, answers only with a
    # sign-off that cannot be credited to H2. H2 is a new head: claude may be taken
    # back for it once more, and its review of H2 then lands.
    world, gate = env
    fleet = Fleet(world)
    behaviours = _round_one(world, fleet, "substantive", cosmetic=False)
    fleet.post("claude", 2, "f3", "[substantive] the bounds check in engine is missing")
    behaviours["f3"] = writes("engine.py", "SUBST-f3")
    fleet.post("claude", 3, "s4", "No issues found.", inline=False)
    fleet.post("claude", 4, "n5", "[cosmetic] nit: docstring in style")
    behaviours["n5"] = writes("style.py", "COSM-n5")

    driver, outcome = _fleet_run(world, fleet, behaviours, max_rounds=6)

    h1, h2 = world.codex_summons[1], driver._last_substantive_head
    assert SUBST in world.markers(h1) and "SUBST-f3" in world.markers(h2)
    # claude: round 1 about H0, the fallback for H1, the re-review of its own fix,
    # then the fallback for H2.
    assert world.summons == [world.h0, h1, h2, h2]
    assert outcome.merged is True and world.merges == [world.remote_tip()]
