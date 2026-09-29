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
                preflight=False):
    clock = FakeClock()

    def dispatch(c, r):
        def runner(prompt, *, model, effort, timeout, cwd):
            return behaviours[c.id](world)
        return fix_apply.apply_fix(c.text, cwd=str(world.repo), runner=runner,
                                   verify_runner=None, label=r.classification.label,
                                   commented_files=[c.path] if c.path else ())
    return RoundDriver(
        "7", repo="o/r", cwd=str(world.repo), cfg=CLAUDE_ONLY,
        adapter=ReviewAdapter(escalation=ConsoleEscalation(notifier=FakeNotifier())),
        classify_runner=classify, fix_dispatch=dispatch,
        fetch=reviewer.fetch, reactions_fetch=lambda pr, repo=None, cwd=None: [],
        reviews_fetch=lambda pr, repo=None, cwd=None: [], inline_fetch=reviewer.inline,
        threads_fetch=lambda pr, repo=None, cwd=None: [],
        resolve_thread=lambda thread_id, cwd=None: True,
        gh_run=world.gh_run, clock=clock, sleep=clock.sleep, notice=lambda *a, **k: "",
        wall_clock=lambda: datetime(2026, 1, 1, 1, 30, tzinfo=timezone.utc),
        times=TIMES, answer_waiter=lambda esc, **k: {}, auto_merge=True,
        preflight=preflight, push=True, test_gate=test_gate, max_rounds=3,
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
