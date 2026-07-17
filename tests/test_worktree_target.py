"""Tests for buddhi_review.worktree_target — the /open-pr + /review-pr resolver
that prefers the worktree THIS session actually worked in over a stale ``$PWD``.

Network-free: every "repo" is a local ``git init`` in a tmp dir with a fake
``origin`` URL (no clone / fetch). The registry is pinned to a tmp file via
``$BUDDHI_SESSION_WORKTREES_PATH`` so lookups are hermetic.
"""
import subprocess

import pytest

from buddhi_review import session_worktrees as sw
from buddhi_review import worktree_target as wt


# ── local-git helpers (no network) ───────────────────────────────────────────
def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args],
                   check=True, capture_output=True, text=True)


def _init_repo(path, origin="https://github.com/owner/repo.git"):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t.t")
    _git(path, "config", "user.name", "t")
    _git(path, "remote", "add", "origin", origin)
    (path / "README.md").write_text("x")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _add_worktree(repo, branch, wtpath):
    _git(repo, "worktree", "add", "-q", "-b", branch, str(wtpath), "HEAD")
    return wtpath


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDHI_SESSION_WORKTREES_PATH",
                       str(tmp_path / "session-worktrees.json"))
    return tmp_path


# ── _split_repo ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("spec,expect", [
    ("https://github.com/Owner/Repo.git", ("github.com", "owner/repo")),
    ("https://github.com/owner/repo", ("github.com", "owner/repo")),
    ("git@github.com:owner/repo.git", ("github.com", "owner/repo")),
    ("ssh://git@github.com/owner/repo.git", ("github.com", "owner/repo")),
    ("https://github.com:443/owner/repo.git", ("github.com", "owner/repo")),
    ("owner/repo", (None, "owner/repo")),                     # bare slug, no host
    ("git@example.org:deep/nested/owner/repo.git",
     ("example.org", "deep/nested/owner/repo")),             # full depth kept
    ("", None),
    (None, None),
    ("justonepart", None),
])
def test_split_repo(spec, expect):
    assert wt._split_repo(spec) == expect


# ── _repos_match — the cross-repo guard ──────────────────────────────────────
def test_repos_match_same_repo_all_url_forms():
    forms = [
        "https://github.com/owner/repo.git",
        "https://github.com/owner/repo",
        "https://github.com/OWNER/REPO.git",
        "git@github.com:owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
        "https://github.com:443/owner/repo.git",
        "  https://github.com/owner/repo.git  ",
    ]
    for f in forms:
        assert wt._repos_match(f, "owner/repo") is True          # bare slug matches
        assert wt._repos_match(f, forms[0]) is True              # any two forms agree


def test_repos_match_rejects_same_slug_different_host():
    # The reported leak: same owner/repo slug on DIFFERENT hosts is NOT the same
    # repo when both specs carry a host.
    assert wt._repos_match("git@gitlab.com:acme/app.git",
                           "https://github.com/acme/app.git") is False
    assert wt._repos_match("https://ghe.internal.corp/platform/service.git",
                           "https://github.com/platform/service.git") is False


def test_repos_match_rejects_same_tail_different_subgroup():
    # Same-host but different top group sharing a subgroup/project tail: full-path
    # matching keeps them distinct.
    assert wt._repos_match("https://gitlab.com/teamx/backend/api.git",
                           "https://gitlab.com/teamy/backend/api.git") is False


def test_repos_match_bare_slug_matches_any_host():
    # A bare owner/repo (no host, from `gh` nameWithOwner) matches that path on any
    # host — the intended same-repo case for a GitHub-centric tool.
    assert wt._repos_match("https://github.com/owner/repo.git", "owner/repo") is True
    assert wt._repos_match("owner/repo", "owner/repo") is True


# ── _is_live_worktree ────────────────────────────────────────────────────────
def test_is_live_worktree_true_for_real_false_otherwise(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    live = _add_worktree(repo, "feat/live", tmp_path / "live-wt")
    assert wt._is_live_worktree(str(live)) is True
    assert wt._is_live_worktree(str(tmp_path / "does-not-exist")) is False
    stray = tmp_path / "plain-dir"
    stray.mkdir()
    assert wt._is_live_worktree(str(stray)) is False   # exists, not a git tree
    assert wt._is_live_worktree(None) is False
    assert wt._is_live_worktree("") is False


# ── resolve() ────────────────────────────────────────────────────────────────
def test_resolve_auto_targets_recorded_live_worktree_of_target_repo(tmp_path):
    repo = _init_repo(tmp_path / "repo", origin="https://github.com/owner/repo.git")
    worktree = _add_worktree(repo, "feat/x", tmp_path / "wt-x")
    sw.register("sess", str(worktree))
    # $PWD is the primary checkout; the session actually worked in `worktree`.
    assert wt.resolve("sess", "owner/repo", str(repo)) == str(worktree)


def test_resolve_derives_target_from_cwd_origin_when_repo_arg_blank(tmp_path):
    repo = _init_repo(tmp_path / "repo", origin="git@github.com:owner/repo.git")
    worktree = _add_worktree(repo, "feat/x", tmp_path / "wt-x")
    sw.register("sess", str(worktree))
    # No explicit --repo → derive the target from the cwd's own origin; still
    # auto-targets because the recorded worktree shares that origin.
    assert wt.resolve("sess", "", str(repo)) == str(worktree)


def test_resolve_ignores_recorded_path_that_is_not_live(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    sw.register("sess", str(tmp_path / "gone-wt"))   # never created
    assert wt.resolve("sess", "owner/repo", str(repo)) == str(repo)
    # A plain (non-git) directory is likewise ignored.
    stray = tmp_path / "stray"
    stray.mkdir()
    sw.register("sess2", str(stray))
    assert wt.resolve("sess2", "owner/repo", str(repo)) == str(repo)


def test_resolve_ignores_live_worktree_of_a_different_repo(tmp_path):
    repo = _init_repo(tmp_path / "repo", origin="https://github.com/owner/repo.git")
    other = _init_repo(tmp_path / "other", origin="https://github.com/someone/else.git")
    other_wt = _add_worktree(other, "feat/elsewhere", tmp_path / "other-wt")
    sw.register("sess", str(other_wt))
    # The recorded worktree is LIVE but belongs to a DIFFERENT repo → never target
    # it; fall back to the cwd checkout.
    assert wt.resolve("sess", "owner/repo", str(repo)) == str(repo)


def test_resolve_ignores_same_slug_worktree_on_a_different_host(tmp_path):
    # The reported cross-repo leak: cwd is github.com/acme/app, the recorded LIVE
    # worktree is a same-SLUG repo on a DIFFERENT host (gitlab.com/acme/app). It
    # must NOT be cross-targeted — fall back to the cwd checkout.
    repo = _init_repo(tmp_path / "repo", origin="https://github.com/acme/app.git")
    other = _init_repo(tmp_path / "other", origin="git@gitlab.com:acme/app.git")
    other_wt = _add_worktree(other, "feat/x", tmp_path / "other-wt")
    sw.register("sess", str(other_wt))
    assert wt.resolve("sess", "", str(repo)) == str(repo)                      # derived target
    assert wt.resolve("sess", "acme/app", str(repo)) == str(repo)             # explicit bare slug
    assert wt.resolve("sess", "https://github.com/acme/app.git", str(repo)) == str(repo)


def test_resolve_falls_back_to_cwd_without_a_record(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    assert wt.resolve("no-session-record", "owner/repo", str(repo)) == str(repo)


def test_resolve_returns_cwd_when_record_is_the_cwd_checkout(tmp_path):
    repo = _init_repo(tmp_path / "repo", origin="https://github.com/owner/repo.git")
    worktree = _add_worktree(repo, "feat/x", tmp_path / "wt-x")
    sw.register("sess", str(worktree))
    # $PWD IS the recorded worktree → nothing to switch to, return it unchanged.
    assert wt.resolve("sess", "owner/repo", str(worktree)) == str(worktree)


def test_resolve_empty_session_returns_cwd(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    assert wt.resolve("", "owner/repo", str(repo)) == str(repo)


# ── the CLI ──────────────────────────────────────────────────────────────────
def test_cli_prints_resolved_worktree(tmp_path, capsys):
    repo = _init_repo(tmp_path / "repo", origin="https://github.com/owner/repo.git")
    worktree = _add_worktree(repo, "feat/x", tmp_path / "wt-x")
    sw.register("sess", str(worktree))
    rc = wt.main(["resolve", "--session-id", "sess",
                  "--repo", "owner/repo", "--cwd", str(repo)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == str(worktree)


def test_cli_prints_cwd_when_nothing_to_switch_to(tmp_path, capsys):
    repo = _init_repo(tmp_path / "repo")
    rc = wt.main(["resolve", "--session-id", "none",
                  "--repo", "owner/repo", "--cwd", str(repo)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == str(repo)


# ── _pr_head_is_local — the fork-branch-collision guard ──────────────────────
def test_pr_head_is_local_true_for_same_repo_or_missing_field():
    assert wt._pr_head_is_local({"headRefName": "main"}, "owner/repo") is True
    assert wt._pr_head_is_local({"isCrossRepository": False}, "owner/repo") is True


def test_pr_head_is_local_false_for_fork_true_for_cross_pr_from_same_owner_repo():
    fork_pr = {
        "isCrossRepository": True,
        "headRepositoryOwner": {"login": "someone-else"},
        "headRepository": {"name": "fork"},
    }
    assert wt._pr_head_is_local(fork_pr, "owner/repo") is False
    same_repo_cross_pr = {
        "isCrossRepository": True,
        "headRepositoryOwner": {"login": "owner"},
        "headRepository": {"name": "repo"},
    }
    assert wt._pr_head_is_local(same_repo_cross_pr, "owner/repo") is True


def test_pr_head_is_local_false_when_repo_fields_missing_on_cross_repo_pr():
    assert wt._pr_head_is_local({"isCrossRepository": True}, "owner/repo") is False


# ── build_report — a fork PR must never attach by branch name alone ──────────
def _write_prlist(tmp_path, prs, name="prlist.json"):
    import json
    p = tmp_path / name
    p.write_text(json.dumps(prs))
    return str(p)


def test_build_report_fork_pr_does_not_attach_to_colliding_local_branch(
        tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo", origin="https://github.com/owner/repo.git")
    worktree = _add_worktree(repo, "shared-name", tmp_path / "wt-shared")
    (worktree / "f.txt").write_text("x")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", "work")

    # An OPEN fork PR whose head branch happens to be named identically to the
    # local worktree's branch — but it lives in a completely different repo.
    fork_pr = {
        "number": 99, "headRefName": "shared-name", "url": "https://x",
        "title": "unrelated fork PR", "updatedAt": "2024-01-01T00:00:00Z",
        "state": "OPEN", "isCrossRepository": True,
        "headRepositoryOwner": {"login": "someone-else"},
        "headRepository": {"name": "fork"},
    }
    monkeypatch.setenv(wt.PRLIST_JSON_ENV, _write_prlist(tmp_path, [fork_pr]))

    open_pr_report = wt.build_report(str(repo), "owner/repo", "open-pr")
    by_path = {c["path"]: c for c in open_pr_report["candidates"]}
    # The fork PR must not hide the local branch's actionable work behind a
    # phantom open_pr match.
    assert str(worktree) in by_path
    assert by_path[str(worktree)]["open_pr"] is None

    review_report = wt.build_report(str(repo), "owner/repo", "review-pr")
    pr_cand = next(c for c in review_report["candidates"] if c["id"] == "pr:99")
    # The fork PR shows up as a review candidate but is NOT attached to the local
    # worktree — it must read as not-checked-out-anywhere, not as that worktree.
    assert pr_cand["kind"] == "pr-only"
    assert pr_cand["path"] != str(worktree)


def test_build_report_same_repo_pr_still_attaches_to_local_branch(
        tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo", origin="https://github.com/owner/repo.git")
    worktree = _add_worktree(repo, "feat/x", tmp_path / "wt-x")
    (worktree / "f.txt").write_text("x")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", "work")

    same_repo_pr = {
        "number": 7, "headRefName": "feat/x", "url": "https://x",
        "title": "same-repo PR", "updatedAt": "2024-01-01T00:00:00Z",
        "state": "OPEN", "isCrossRepository": False,
    }
    monkeypatch.setenv(wt.PRLIST_JSON_ENV, _write_prlist(tmp_path, [same_repo_pr]))

    review_report = wt.build_report(str(repo), "owner/repo", "review-pr")
    pr_cand = next(c for c in review_report["candidates"] if c["id"] == "pr:7")
    assert pr_cand["path"] == str(worktree)


# ── introspect — collision-free candidate ids ─────────────────────────────────
def test_introspect_id_collision_free_for_same_basename_worktrees(tmp_path):
    repo = _init_repo(tmp_path / "repo", origin="https://github.com/owner/repo.git")
    wt_a = _add_worktree(repo, "feat/a", tmp_path / "groupA" / "task")
    wt_b = _add_worktree(repo, "feat/b", tmp_path / "groupB" / "task")

    entries = wt.list_worktrees(str(repo))
    recs = [wt.introspect(e, None, {}, is_primary=(i == 0))
            for i, e in enumerate(entries)]

    ids = [r["id"] for r in recs]
    assert len(ids) == len(set(ids))   # no id collides despite same basename

    by_path = {r["path"]: r["id"] for r in recs}
    assert by_path[str(wt_a)] != by_path[str(wt_b)]


# ── detect_base ────────────────────────────────────────────────────────────────
def _init_bare(path, initial_branch):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "--bare", "-b", initial_branch)
    return path


def test_detect_base_asks_the_remote_when_origin_head_is_unset_and_no_local_guess_matches(tmp_path):
    """``git remote add`` + a manual ``fetch`` (unlike ``git clone``) never sets
    origin/HEAD locally. When the real default is neither "main" nor "master" (e.g.
    "trunk") and no matching local branch exists either, detect_base must still
    resolve it by asking the remote directly instead of guessing "main". The
    "origin" here is a local bare repo (a filesystem path) so this stays
    network-free."""
    bare = _init_bare(tmp_path / "bare.git", "trunk")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "trunk")
    _git(seed, "config", "user.email", "t@t.t")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("x")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-q", "origin", "trunk")

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "scratch")   # local branch matches neither main/master
    _git(work, "config", "user.email", "t@t.t")
    _git(work, "config", "user.name", "t")
    (work / "f.txt").write_text("y")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init2")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "fetch", "-q", "origin")         # manual fetch — origin/HEAD stays unset

    assert wt._run_git(str(work), "symbolic-ref", "refs/remotes/origin/HEAD") is None
    assert wt.detect_base(str(work)) == "trunk"


def test_detect_base_still_defaults_to_main_with_no_origin_at_all(tmp_path):
    """The brand-new-repo case (no remote yet) must keep falling back to "main"
    unchanged — ``git remote show origin`` fails fast on a missing remote rather
    than hanging or raising."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "scratch")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("y")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    assert wt.detect_base(str(repo)) == "main"
