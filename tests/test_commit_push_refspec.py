"""Explicit-refspec round-fix push (robustness).

The per-round push (`commit_and_push`) used a BARE `git push`, which relies on
the branch's configured upstream. When the local branch name != its upstream
merge name (the default `push.default=simple` refuses) — e.g. a branch created
off `origin/<other-branch>` that was later deleted — git exits 128 and the loop
ends "manual review required" even though the branch is perfectly pushable by its
own name.

The fix pushes by EXPLICIT refspec (`<remote> HEAD:refs/heads/<branch>`) so the
destination is the branch's own name regardless of `push.default` / a mismatched
or dangling upstream — BUT only when an upstream remote (`branch.<name>.remote`)
is configured. With no upstream remote (a named branch that was never pushed) it
falls back to a bare push, preserving the fail-loud fail-safe.

Real-git, network-free: a local bare repo is `origin`; a working clone whose
branch carries a mismatched / dangling upstream proves a bare `git push` fails
(exit 128) there while the explicit refspec succeeds.
"""
from __future__ import annotations

import subprocess

from buddhi_review import commit_push

DEVNULL = subprocess.DEVNULL


def _silent(*a, **k):
    return ""


# ── real-git scaffolding (network-free) ─────────────────────────────────────
def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=DEVNULL, stderr=DEVNULL)


def _git_rc(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _rev(cwd, ref="HEAD"):
    return subprocess.run(["git", "rev-parse", ref], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _init_clone(tmp_path):
    """A bare `origin` + a working clone with `main` pushed and deterministic,
    hermetic git config (identity set, signing + hooks off, push.default pinned)."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True,
                   stdout=DEVNULL, stderr=DEVNULL)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "t@t.t")
    _git(work, "config", "user.name", "t")
    _git(work, "config", "commit.gpgsign", "false")
    _git(work, "config", "core.hooksPath", "/dev/null")
    # Pin push.default=simple: git's default since 2.0, but a global override
    # could change it; the bug is `simple`-mode specific.
    _git(work, "config", "push.default", "simple")
    _git(work, "remote", "add", "origin", str(bare))
    (work / "README.md").write_text("init")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-q", "-u", "origin", "main")
    return bare, work


def _branch_with_mismatched_upstream(work, branch="feat/compass",
                                     upstream="old-base", commit=True):
    """Create `branch` whose configured upstream is the DIFFERENTLY-NAMED (and,
    since `upstream` is never created on origin, DANGLING) `origin/<upstream>`.
    A bare `git push` under push.default=simple refuses this (exit 128)."""
    _git(work, "checkout", "-q", "-b", branch)
    _git(work, "config", f"branch.{branch}.remote", "origin")
    _git(work, "config", f"branch.{branch}.merge", f"refs/heads/{upstream}")
    (work / "fix.txt").write_text("round fix")
    if commit:
        _git(work, "add", "-A")
        _git(work, "commit", "-qm", "round fix")


# ── the reproduction: explicit refspec pushes where a bare push dies ─────────
def test_push_argv_explicit_when_upstream_remote_configured(tmp_path):
    _, work = _init_clone(tmp_path)
    _branch_with_mismatched_upstream(work)
    assert commit_push._resolve_push_target(str(work)) == ("origin", "feat/compass")
    assert commit_push._push_argv(str(work)) == [
        "git", "push", "origin", "HEAD:refs/heads/feat/compass"]


def test_explicit_refspec_push_succeeds_over_mismatched_dangling_upstream(tmp_path):
    _, work = _init_clone(tmp_path)
    _branch_with_mismatched_upstream(work)

    # Prove the bug exists in this fixture: a BARE push refuses (exit 128).
    bare_push = _git_rc(work, "push")
    assert bare_push.returncode == 128, (
        "fixture does not reproduce the bug — a bare push unexpectedly succeeded "
        f"(rc={bare_push.returncode}): {bare_push.stderr}")

    # The explicit-refspec argv pushes and SUCCEEDS.
    proc = commit_push._default_run(
        commit_push._push_argv(str(work)), cwd=str(work))
    assert proc.returncode == 0, proc.stderr

    # …and the branch landed on origin under its OWN name at the local SHA.
    landed = _git_rc(work, "ls-remote", "origin",
                     "refs/heads/feat/compass").stdout.split()
    assert landed and landed[0] == _rev(work, "HEAD")


def test_commit_and_push_uses_explicit_refspec_end_to_end(tmp_path):
    """The wired push path (commit_and_push) ships a round fix over a mismatched/
    dangling upstream that a bare push could not."""
    _, work = _init_clone(tmp_path)
    _branch_with_mismatched_upstream(work, commit=False)  # leave the fix dirty
    out = commit_push.commit_and_push(
        str(work), message="fix: round 1", test_gate=False, notice=_silent)
    assert out == "pushed"
    landed = _git_rc(work, "ls-remote", "origin",
                     "refs/heads/feat/compass").stdout.split()
    assert landed and landed[0] == _rev(work, "HEAD")


# ── happy path: no regression for a normal same-named upstream ───────────────
def test_push_argv_happy_path_same_named_upstream(tmp_path):
    _, work = _init_clone(tmp_path)
    _git(work, "checkout", "-q", "-b", "feat/normal")
    (work / "a.txt").write_text("x")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "a")
    _git(work, "push", "-q", "-u", "origin", "feat/normal")  # same-named upstream
    assert commit_push._push_argv(str(work)) == [
        "git", "push", "origin", "HEAD:refs/heads/feat/normal"]


# ── fail-safe: no upstream remote → bare push (named branch, never pushed) ────
def test_push_argv_falls_back_to_bare_without_upstream_remote(tmp_path):
    """A named branch with NO configured upstream. _push_argv MUST NOT synthesise
    `origin HEAD:refs/heads/...` — that could land a stray branch and falsely
    report success. It falls back to a bare push (which fails loudly as designed)."""
    _, work = _init_clone(tmp_path)
    _git(work, "checkout", "-q", "-b", "feat/forkless")
    (work / "c.txt").write_text("z")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "c")
    assert _git_rc(work, "config", "--get",
                   "branch.feat/forkless.remote").returncode != 0
    assert commit_push._resolve_push_target(str(work)) == (None, None)
    assert commit_push._push_argv(str(work)) == ["git", "push"]


def test_no_upstream_guard_fires_even_with_push_default_set(tmp_path):
    """The (None, None) no-upstream guard fires on a branch with no
    branch.<name>.remote even when remote.pushDefault is set globally."""
    _, work = _init_clone(tmp_path)
    _git(work, "config", "remote.pushDefault", "origin")
    _git(work, "checkout", "-q", "-b", "feat/forkless-with-default")
    (work / "d.txt").write_text("d")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "d")
    assert _git_rc(work, "config", "--get",
                   "branch.feat/forkless-with-default.remote").returncode != 0
    assert commit_push._resolve_push_target(str(work)) == (None, None)
    assert commit_push._push_argv(str(work)) == ["git", "push"]


# ── fail-safe: detached HEAD → bare push ─────────────────────────────────────
def test_push_argv_falls_back_to_bare_on_detached_head(tmp_path):
    _, work = _init_clone(tmp_path)
    _git(work, "checkout", "-q", "--detach", "HEAD")
    assert commit_push._resolve_push_target(str(work)) == (None, None)
    assert commit_push._push_argv(str(work)) == ["git", "push"]


# ── push-remote precedence: pushRemote > pushDefault > remote ────────────────
def test_push_argv_honours_branch_push_remote(tmp_path):
    """branch.<name>.pushRemote beats branch.<name>.remote (fork workflow:
    pull from upstream, push to contributor's origin)."""
    bare_upstream = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare_upstream)], check=True,
                   stdout=DEVNULL, stderr=DEVNULL)
    _, work = _init_clone(tmp_path)
    _git(work, "remote", "add", "upstream", str(bare_upstream))
    _git(work, "checkout", "-q", "-b", "feat/fork-branch")
    _git(work, "config", "branch.feat/fork-branch.remote", "upstream")
    _git(work, "config", "branch.feat/fork-branch.pushRemote", "origin")
    assert commit_push._resolve_push_target(str(work)) == ("origin", "feat/fork-branch")
    assert commit_push._push_argv(str(work)) == [
        "git", "push", "origin", "HEAD:refs/heads/feat/fork-branch"]


def test_push_argv_honours_remote_push_default(tmp_path):
    """remote.pushDefault beats branch.<name>.remote when pushRemote is absent."""
    bare_upstream = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare_upstream)], check=True,
                   stdout=DEVNULL, stderr=DEVNULL)
    _, work = _init_clone(tmp_path)
    _git(work, "remote", "add", "upstream", str(bare_upstream))
    _git(work, "checkout", "-q", "-b", "feat/default-push")
    _git(work, "config", "branch.feat/default-push.remote", "upstream")
    _git(work, "config", "remote.pushDefault", "origin")
    assert commit_push._resolve_push_target(str(work)) == ("origin", "feat/default-push")


def test_push_argv_pushremote_beats_push_default(tmp_path):
    """branch.<name>.pushRemote wins over remote.pushDefault."""
    _, work = _init_clone(tmp_path)
    _git(work, "checkout", "-q", "-b", "feat/prec")
    _git(work, "config", "branch.feat/prec.remote", "origin")
    _git(work, "config", "branch.feat/prec.pushRemote", "origin")
    _git(work, "config", "remote.pushDefault", "other")
    remote, _branch = commit_push._resolve_push_target(str(work))
    assert remote == "origin"  # pushRemote wins, not pushDefault


# ── seam robustness: an odd/raising run-seam result → bare-push fail-safe ─────
def test_resolve_push_target_degrades_to_bare_on_run_seam_error():
    def boom(argv, *, cwd=None, timeout=None):
        raise RuntimeError("git exploded")

    assert commit_push._resolve_push_target("/w", run=boom) == (None, None)
    assert commit_push._push_argv("/w", run=boom) == ["git", "push"]


def test_resolve_push_target_degrades_to_bare_on_non_completedprocess():
    class Weird:  # no returncode / stdout attributes
        pass

    def weird(argv, *, cwd=None, timeout=None):
        return Weird()

    assert commit_push._resolve_push_target("/w", run=weird) == (None, None)
    assert commit_push._push_argv("/w", run=weird) == ["git", "push"]
