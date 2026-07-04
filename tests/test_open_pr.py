"""open_pr.py — the open-pr actuator (git decision tree + create + launch).

The git half runs against a real temp repo; ``gh`` and the detached launcher are
injected seams so the suite is network-free and opens no real PR.
"""
import io
import subprocess
import types
from pathlib import Path

import pytest

from buddhi_review import open_pr


# ── git temp-repo helpers ──────────────────────────────────────────────────────────

def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _init_repo_with_remote(tmp_path):
    """A repo on `main` with one commit, pushed to a bare `origin`."""
    work = tmp_path / "work"
    work.mkdir()
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Tester")
    (work / "README.md").write_text("hi\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work, bare


def _seam(*, pr_url="https://github.com/acme/widgets/pull/7", already_exists=False):
    calls = []
    launched = []

    def run(argv, cwd=None, timeout=60, input=None):
        R = types.SimpleNamespace
        if argv and argv[0] == "gh":
            calls.append(list(argv))
            if argv[:3] == ["gh", "pr", "create"]:
                if already_exists:
                    return R(returncode=1, stdout="",
                             stderr="a pull request for branch already exists")
                return R(returncode=0, stdout=pr_url + "\n", stderr="")
            if argv[:3] == ["gh", "pr", "view"]:
                return R(returncode=0, stdout=pr_url + "\n", stderr="")
            if argv[:2] == ["gh", "repo"]:
                return R(returncode=0, stdout="acme/widgets\n", stderr="")
            return R(returncode=0, stdout="", stderr="")
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, input=input)

    def launch(pr_number, repo, cwd, err):
        launched.append((pr_number, repo, cwd))

    return run, launch, calls, launched


def _run_actuate(work, run, launch, **kw):
    out, err = io.StringIO(), io.StringIO()
    rc = open_pr.actuate(repo="acme/widgets", cwd=str(work), base="main",
                         title="Add a thing", body="why", run=run, launch=launch,
                         out=out, err=err, **kw)
    return rc, out.getvalue(), err.getvalue()


# ── decide_path (pure) ─────────────────────────────────────────────────────────────

def test_decide_path():
    S = open_pr.GitState
    assert open_pr.decide_path(S("main", "feat/x", True, True, False, True)) == "A"
    assert open_pr.decide_path(S("main", "feat/x", True, True, True, False)) == "B"
    assert open_pr.decide_path(S("main", "main", False, True, True, False)) == "C"
    assert open_pr.decide_path(S("main", "main", False, True, False, False)) == "C_or_D"
    assert open_pr.decide_path(S("main", "", False, False, False, False)) == "D"


# ── resolve_repo ───────────────────────────────────────────────────────────────────

def test_resolve_repo_explicit_wins():
    assert open_pr.resolve_repo("/x", "acme/widgets", run=None) == "acme/widgets"


def test_resolve_repo_infers_from_gh():
    def run(argv, cwd=None, timeout=60, input=None):
        return types.SimpleNamespace(returncode=0, stdout="acme/widgets\n")
    assert open_pr.resolve_repo("/x", None, run) == "acme/widgets"


def test_resolve_repo_raises_without_remote():
    def run(argv, cwd=None, timeout=60, input=None):
        return types.SimpleNamespace(returncode=1, stdout="")
    with pytest.raises(open_pr.OpenPrError):
        open_pr.resolve_repo("/x", None, run)


# ── Full flows ─────────────────────────────────────────────────────────────────────

def test_path_a_clean_feature_branch(tmp_path):
    work, _ = _init_repo_with_remote(tmp_path)
    _git(work, "checkout", "-b", "feat/x")
    (work / "f.txt").write_text("change\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "work")
    run, launch, calls, launched = _seam()
    rc, out, err = _run_actuate(work, run, launch)
    assert rc == 0
    assert out.strip() == "https://github.com/acme/widgets/pull/7"  # ONLY the URL on stdout
    assert any(c[:3] == ["gh", "pr", "create"] for c in calls)
    assert launched == [("7", "acme/widgets", str(work))]


def test_path_b_uncommitted_on_feature(tmp_path):
    work, _ = _init_repo_with_remote(tmp_path)
    _git(work, "checkout", "-b", "feat/y")
    (work / "f.txt").write_text("uncommitted\n", encoding="utf-8")  # not committed
    run, launch, calls, launched = _seam()
    rc, out, err = _run_actuate(work, run, launch)
    assert rc == 0
    # The uncommitted change was committed before the PR opened.
    porcelain = subprocess.run(["git", "-C", str(work), "status", "--porcelain"],
                               capture_output=True, text=True).stdout.strip()
    assert porcelain == ""
    assert launched and launched[0][0] == "7"


def test_path_c_on_base_creates_branch(tmp_path):
    work, _ = _init_repo_with_remote(tmp_path)
    (work / "f.txt").write_text("new work on base\n", encoding="utf-8")  # uncommitted, on main
    run, launch, calls, launched = _seam()
    rc, out, err = _run_actuate(work, run, launch, branch="feat/new-thing")
    assert rc == 0
    branch = subprocess.run(["git", "-C", str(work), "branch", "--show-current"],
                            capture_output=True, text=True).stdout.strip()
    assert branch == "feat/new-thing"  # actuator created + switched to it
    assert launched and launched[0][0] == "7"


def test_path_d_nothing_to_do(tmp_path):
    work, _ = _init_repo_with_remote(tmp_path)  # clean, on main, pushed
    run, launch, calls, launched = _seam()
    rc, out, err = _run_actuate(work, run, launch)
    assert rc == 0
    assert out.strip() == ""  # no PR URL emitted
    assert not any(c[:3] == ["gh", "pr", "create"] for c in calls)
    assert "Nothing to do" in err
    assert launched == []


def test_already_exists_reuses_pr(tmp_path):
    work, _ = _init_repo_with_remote(tmp_path)
    _git(work, "checkout", "-b", "feat/dupe")
    (work / "f.txt").write_text("x\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "w")
    run, launch, calls, launched = _seam(already_exists=True)
    rc, out, err = _run_actuate(work, run, launch)
    assert rc == 0
    assert out.strip() == "https://github.com/acme/widgets/pull/7"
    assert any(c[:3] == ["gh", "pr", "view"] for c in calls)  # fell back to view


def test_behind_base_emits_notice_never_rebases(tmp_path):
    work, bare = _init_repo_with_remote(tmp_path)
    # Advance origin/main from a second clone so the feature branch is behind.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(bare), str(other)], check=True, capture_output=True)
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "Other")
    (other / "g.txt").write_text("upstream\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "upstream commit")
    _git(other, "push", "origin", "main")
    # Local feature branch off the OLD main (now behind by 1).
    _git(work, "checkout", "-b", "feat/behind")
    (work / "h.txt").write_text("local\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "local work")
    tip_before = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
    run, launch, calls, launched = _seam()
    rc, out, err = _run_actuate(work, run, launch)
    assert rc == 0
    # The ⚙ [auto] transparency line goes to STDOUT (§3.6), not stderr.
    assert "[auto] rebase gate" in out
    assert "behind origin/main" in out
    # OSS purity: the notice cites the concrete action, never "free does not auto-rebase".
    assert "auto-rebase" not in (out + err)
    # Never auto-rebases: the local branch tip is byte-for-byte unchanged.
    tip_after = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                               capture_output=True, text=True).stdout.strip()
    assert tip_after == tip_before
    # The PR URL is still the LAST stdout line despite the notice above it.
    assert out.strip().splitlines()[-1] == "https://github.com/acme/widgets/pull/7"
    assert launched and launched[0][0] == "7"


def test_no_implementer_session_or_keep_open_notice(tmp_path):
    work, _ = _init_repo_with_remote(tmp_path)
    _git(work, "checkout", "-b", "feat/z")
    (work / "f.txt").write_text("x\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "w")
    run, launch, calls, launched = _seam()
    rc, out, err = _run_actuate(work, run, launch)
    assert rc == 0
    # Free never passes --implementer-session to gh, and never prints the paid notice.
    for c in calls:
        assert "--implementer-session" not in c
    assert "keep this session open" not in (out + err).lower()


def test_source_has_no_paid_consult_markers():
    text = Path(open_pr.__file__).read_text(encoding="utf-8")
    assert "--implementer-session" not in text
    assert "keep this session open" not in text.lower()


# ── Path C remote-infra cases (1 / 2 / 3) ──────────────────────────────────────────

def _branches_on_bare(bare):
    out = subprocess.run(["git", "-C", str(bare), "branch", "--format=%(refname:short)"],
                         capture_output=True, text=True).stdout.split()
    return set(out)


def test_case1_remote_missing_base(tmp_path):
    """On base, work present, the remote has NO base branch yet: push base, then
    branch the feature off it."""
    work = tmp_path / "work"
    work.mkdir()
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Tester")
    (work / "README.md").write_text("hi\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))  # remote exists but has NO main yet
    (work / "f.txt").write_text("work on base\n", encoding="utf-8")  # uncommitted
    run, launch, calls, launched = _seam()
    rc, out, err = _run_actuate(work, run, launch, branch="feat/c1")
    assert rc == 0
    branches = _branches_on_bare(bare)
    assert "main" in branches and "feat/c1" in branches  # base established + feature pushed
    assert launched and launched[0][0] == "7"


def test_case2_local_unborn_remote_has_base(tmp_path):
    """Remote has base, local repo is unborn (no commits) with staged work: graft
    the work onto origin/base — the work must NOT be lost."""
    work, bare = _init_repo_with_remote(tmp_path)
    # A fresh, unborn local repo pointing at the SAME bare remote.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _git(fresh, "init", "-b", "main")
    _git(fresh, "config", "user.email", "t@example.com")
    _git(fresh, "config", "user.name", "Tester")
    _git(fresh, "remote", "add", "origin", str(bare))
    _git(fresh, "fetch", "origin")
    (fresh / "new.txt").write_text("brand new work\n", encoding="utf-8")  # staged, no HEAD yet
    _git(fresh, "add", "-A")
    run, launch, calls, launched = _seam()
    rc, out, err = _run_actuate(fresh, run, launch, branch="feat/c2")
    assert rc == 0
    assert "feat/c2" in _branches_on_bare(bare)
    # The grafted branch carries the new work (not dropped by a stash).
    files = subprocess.run(["git", "-C", str(bare), "show", "feat/c2:new.txt"],
                           capture_output=True, text=True)
    assert files.returncode == 0 and "brand new work" in files.stdout
    assert launched and launched[0][0] == "7"


def test_graft_onto_base_refuses_existing_local_branch(tmp_path):
    """_graft_onto_base (Case 4 — unrelated history) must refuse with a clear error
    when a local branch with the desired name already exists, not silently overwrite it."""
    # Build a remote with an established main branch.
    _, bare = _init_repo_with_remote(tmp_path)
    # Build a SEPARATE local repo with its own commit (unrelated to origin/main).
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _git(fresh, "init", "-b", "main")
    _git(fresh, "config", "user.email", "t@example.com")
    _git(fresh, "config", "user.name", "Tester")
    (fresh / "local.txt").write_text("unrelated history\n", encoding="utf-8")
    _git(fresh, "add", "-A")
    _git(fresh, "commit", "-m", "local-only commit")
    _git(fresh, "remote", "add", "origin", str(bare))
    _git(fresh, "fetch", "origin")
    # Pre-create the collision branch so _graft_onto_base hits the guard.
    _git(fresh, "branch", "feat/collision")
    run, launch, calls, launched = _seam()
    rc, out, err = _run_actuate(fresh, run, launch, branch="feat/collision")
    assert rc != 0
    assert "feat/collision" in err
    assert "already exists" in err


def test_case3_shared_history_divergence_rebases(tmp_path):
    """On base, local has a committed-but-unpushed commit, origin/base advanced
    (shared history): pull --rebase, then branch — the feature carries BOTH commits."""
    work, bare = _init_repo_with_remote(tmp_path)
    # Advance origin/main from a second clone.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(bare), str(other)], check=True, capture_output=True)
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "Other")
    (other / "upstream.txt").write_text("upstream\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "upstream commit")
    _git(other, "push", "origin", "main")
    # Local commits a change on main WITHOUT pushing (diverges from origin/main).
    (work / "local.txt").write_text("local\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "local commit on base")
    run, launch, calls, launched = _seam()
    rc, out, err = _run_actuate(work, run, launch, branch="feat/c3")
    assert rc == 0
    assert "feat/c3" in _branches_on_bare(bare)
    # The pushed feature branch contains BOTH the upstream commit and the local one.
    msgs = subprocess.run(["git", "-C", str(bare), "log", "feat/c3", "--format=%s"],
                          capture_output=True, text=True).stdout
    assert "local commit on base" in msgs
    assert "upstream commit" in msgs
    assert launched and launched[0][0] == "7"
