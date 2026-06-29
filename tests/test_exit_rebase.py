"""Exit-rebase on a manual-landing hand-back — real git, no network.

The loop's ONLY force-push: rebase the loop's OWN feature branch onto the latest
base and ``--force-with-lease`` push it so a hand-back PR can be merged cleanly.
Every test drives real git against a local bare remote (no network) so the
rebase / conflict-abort-restore / force-with-lease behaviour is exercised end to
end, not mocked.
"""
import subprocess

import pytest

from buddhi_review import commit_push


def _silent_notice(*a, **k):
    return ""


def _git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check,
                          capture_output=True, text=True)


def _sha(cwd, ref="HEAD"):
    return _git(cwd, "rev-parse", ref).stdout.strip()


def _write(path, text):
    path.write_text(text)


class Rec:
    """Records every git spawn and delegates to the real runner (real git)."""
    def __init__(self):
        self.calls = []

    def __call__(self, argv, *, cwd=None, timeout=None):
        self.calls.append(list(argv))
        return commit_push._default_run(argv, cwd=cwd)

    def pushes(self):
        return [c for c in self.calls if c[:2] == ["git", "push"]]

    def rebases(self):
        return [c for c in self.calls
                if c[:2] == ["git", "rebase"] and "--abort" not in c]


@pytest.fixture
def repo(tmp_path):
    """A bare remote + a clone on a feature branch (``feat/x``) off ``main``.

    Layout after setup: ``main`` and ``feat/x`` both pushed; the worktree is on
    ``feat/x`` with an upstream configured. Returns the working clone path.
    """
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-q", "-b", "main")
    _write(work / "base.py", "x = 1\n")
    _write(work / "shared.py", "value = 'base'\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    _git(work, "push", "-q", "-u", "origin", "main")
    _git(work, "checkout", "-q", "-b", "feat/x", "main")
    _write(work / "feature.py", "y = 2\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "feature work")
    _git(work, "push", "-q", "-u", "origin", "feat/x")
    return work


def _advance_base(work, *, filename="other.py", content="z = 3\n"):
    """Push a new commit onto ``main`` so ``feat/x`` falls behind, then return to
    ``feat/x``. The feature branch is now strictly behind base (no conflict)."""
    _git(work, "checkout", "-q", "main")
    _write(work / filename, content)
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base advances")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-q", "feat/x")


# ── Bucket A/B: a behind branch rebases + force-with-lease pushes ────────────────
def test_behind_branch_rebases_and_force_with_lease_pushes(repo):
    _advance_base(repo)
    main_sha = _sha(repo, "origin/main")
    rec = Rec()

    status, detail = commit_push.exit_rebase(
        str(repo), base="main", run=rec, notice=_silent_notice)

    assert status == "rebased" and detail == ""
    # The rebase pulled base into the branch's history (linear, on top of base).
    assert _git(repo, "merge-base", "--is-ancestor", main_sha, "HEAD",
                check=False).returncode == 0
    # The remote feature branch was updated to the rebased tip (push landed).
    assert _sha(repo, "origin/feat/x") == _sha(repo, "HEAD")
    # Exactly one rebase, and a push that used --force-with-lease.
    assert len(rec.rebases()) == 1
    pushes = rec.pushes()
    assert len(pushes) == 1
    assert any(a.startswith("--force-with-lease") for a in pushes[0])


def test_push_uses_force_with_lease_never_bare_force(repo):
    """The footgun guard: across EVERY spawned arg, never a bare -f / --force."""
    _advance_base(repo)
    rec = Rec()
    status, _ = commit_push.exit_rebase(
        str(repo), base="main", run=rec, notice=_silent_notice)
    assert status == "rebased"
    flat = [a for call in rec.calls for a in call]
    assert "-f" not in flat
    assert "--force" not in flat  # only the allowlisted --force-with-lease[=…] form
    assert any(a.startswith("--force-with-lease") for a in flat)


# ── Already current → no-op (no gratuitous force-push) ──────────────────────────
def test_already_current_is_noop(repo):
    # main has NOT advanced — feat/x already contains base.
    rec = Rec()
    status, detail = commit_push.exit_rebase(
        str(repo), base="main", run=rec, notice=_silent_notice)
    assert status == "current" and detail == ""
    assert rec.pushes() == []   # never force-push an already-current branch
    assert rec.rebases() == []


# ── Bucket C self-guard: a dirty worktree is skipped (never rebased) ─────────────
def test_dirty_worktree_is_skipped(repo):
    _advance_base(repo)
    (repo / "feature.py").write_text("y = 999  # uncommitted edit\n")  # dirty
    pre = _sha(repo)
    rec = Rec()
    status, detail = commit_push.exit_rebase(
        str(repo), base="main", run=rec, notice=_silent_notice)
    assert status == "skipped"
    assert "uncommitted" in detail
    assert rec.pushes() == [] and rec.rebases() == []
    assert _sha(repo) == pre  # nothing touched
    assert (repo / "feature.py").read_text() == "y = 999  # uncommitted edit\n"


# ── Conflict → abort + restore EXACT pre-rebase SHA + manual diagnosis ───────────
def test_conflict_aborts_and_restores_pre_rebase_sha(repo):
    # feat/x edits shared.py; main edits the SAME line differently → conflict.
    (repo / "shared.py").write_text("value = 'from feature'\n")
    _git(repo, "commit", "-aqm", "feature edits shared")
    _git(repo, "push", "-q", "origin", "feat/x")
    _git(repo, "checkout", "-q", "main")
    (repo / "shared.py").write_text("value = 'from base'\n")
    _git(repo, "commit", "-aqm", "base edits shared")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "checkout", "-q", "feat/x")

    pre = _sha(repo)
    remote_pre = _sha(repo, "origin/feat/x")
    rec = Rec()

    status, detail = commit_push.exit_rebase(
        str(repo), base="main", run=rec, notice=_silent_notice)

    assert status == "conflict"
    assert "shared.py" in detail            # names the conflicted file
    assert "git rebase" in detail           # gives the manual steps
    assert "--force-with-lease" in detail
    # The branch is restored to its EXACT pre-rebase state — no half-rebase.
    assert _sha(repo) == pre
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "feat/x"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""  # clean tree
    # Never pushed → remote feature branch untouched.
    assert rec.pushes() == []
    assert _sha(repo, "origin/feat/x") == remote_pre


# ── --force-with-lease is real: a remote that advanced under us is REJECTED ──────
def test_force_with_lease_rejects_when_remote_advanced(repo, tmp_path):
    """Prove the lease protects work: if the remote feature branch advances AFTER
    our fetch but BEFORE our push, the force-with-lease push is rejected and the
    remote is NOT clobbered."""
    _advance_base(repo)

    # A second clone that will push a new commit to feat/x mid-flight.
    attacker = tmp_path / "attacker"
    subprocess.run(["git", "clone", "-q", "-b", "feat/x",
                    str(tmp_path / "remote.git"), str(attacker)], check=True)
    _git(attacker, "config", "user.email", "a@example.com")
    _git(attacker, "config", "user.name", "a")

    def attacker_push():
        (attacker / "attacker.py").write_text("hijack = True\n")
        _git(attacker, "add", "-A")
        _git(attacker, "commit", "-qm", "attacker commit")
        _git(attacker, "push", "-q", "origin", "feat/x")

    class AdvanceOnPush(Rec):
        def __init__(self):
            super().__init__()
            self.fired = False

        def __call__(self, argv, *, cwd=None, timeout=None):
            # Advance the remote exactly once, right before our real push runs.
            if (not self.fired and argv[:2] == ["git", "push"]
                    and any("force-with-lease" in a for a in argv)):
                self.fired = True
                attacker_push()
            return super().__call__(argv, cwd=cwd, timeout=timeout)

    rec = AdvanceOnPush()
    attacker_sha_holder = {}

    status, detail = commit_push.exit_rebase(
        str(repo), base="main", run=rec, notice=_silent_notice)

    assert rec.fired                       # our push was attempted
    assert status == "error"
    assert "push" in detail                # reported, not silently swallowed
    # The remote feature branch keeps the attacker's commit — NOT clobbered.
    remote_sha = subprocess.run(
        ["git", "ls-remote", str(tmp_path / "remote.git"), "refs/heads/feat/x"],
        capture_output=True, text=True, check=True).stdout.split()[0]
    assert remote_sha == _sha(attacker, "HEAD")


# ── Unresolvable push target → skipped (never synthesise a force-push target) ────
def test_detached_head_is_skipped(repo):
    _advance_base(repo)
    _git(repo, "checkout", "-q", _sha(repo))  # detach HEAD
    rec = Rec()
    status, detail = commit_push.exit_rebase(
        str(repo), base="main", run=rec, notice=_silent_notice)
    assert status == "skipped"
    assert "push target" in detail
    assert rec.pushes() == [] and rec.rebases() == []


def test_branch_without_upstream_is_skipped(repo):
    _advance_base(repo)
    _git(repo, "checkout", "-q", "-b", "feat/no-upstream")  # named, no upstream
    rec = Rec()
    status, detail = commit_push.exit_rebase(
        str(repo), base="main", run=rec, notice=_silent_notice)
    assert status == "skipped"
    assert "push target" in detail
    assert rec.pushes() == [] and rec.rebases() == []
