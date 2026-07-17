"""Tests for the ``worktree_target list`` candidate enumerator — the multi-checkout
"which checkout should I open the PR from?" source the /open-pr + /review-pr skills
render.

Network-free: every "repo" is a local ``git init`` in a tmp dir (no clone / fetch),
and the PR list comes from the ``$BUDDHI_REVIEW_PRLIST_JSON`` seam instead of ``gh``
(an autouse fixture pins it to an EMPTY list, so a test that forgets to seed PRs can
never silently reach the network). The session→worktree registry is likewise pinned
to a tmp file.

Commit timestamps are pinned explicitly (``GIT_*_DATE``) wherever ranking is asserted
— two commits made in the same wall-clock second would otherwise tie and make the
"most recent first" ordering flaky.
"""
import json
import os
import subprocess

import pytest

from buddhi_review import session_worktrees as sw
from buddhi_review import worktree_target as wt


# ── local-git helpers (no network) ───────────────────────────────────────────
def _git(cwd, *args, when=None):
    env = dict(os.environ)
    if when:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    subprocess.run(["git", "-C", str(cwd), *args],
                   check=True, capture_output=True, text=True, env=env)


def _init_repo(path, origin="https://github.com/owner/repo.git"):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t.t")
    _git(path, "config", "user.name", "t")
    _git(path, "remote", "add", "origin", origin)
    (path / "README.md").write_text("x")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init", when="2024-01-01T00:00:00")
    return path


def _add_worktree(repo, branch, wtpath):
    _git(repo, "worktree", "add", "-q", "-b", branch, str(wtpath), "HEAD")
    return wtpath


def _commit(path, text="change", *, when="2024-02-01T00:00:00", name="work.txt"):
    (path / name).write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", text, when=when)
    return path


def _dirty(path, name="scratch.txt", text="uncommitted"):
    (path / name).write_text(text)
    return path


def _worktree_with_commit(repo, branch, wtpath, *, when="2024-02-01T00:00:00"):
    """A worktree carrying ONE commit ahead of base — the canonical open-pr candidate."""
    _add_worktree(repo, branch, wtpath)
    return _commit(wtpath, f"work on {branch}", when=when)


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Pin the session registry AND the PR-list seam to tmp files. The seam defaults
    to an EMPTY PR list so no test can fall through to a real ``gh`` call."""
    monkeypatch.setenv("BUDDHI_SESSION_WORKTREES_PATH",
                       str(tmp_path / "session-worktrees.json"))
    empty = tmp_path / "prs-empty.json"
    empty.write_text("[]")
    monkeypatch.setenv(wt.PRLIST_JSON_ENV, str(empty))
    return tmp_path


@pytest.fixture
def seed_prs(tmp_path, monkeypatch):
    """Seed the PR-list seam with the given PR dicts."""
    def _seed(prs):
        path = tmp_path / "prs.json"
        path.write_text(json.dumps(prs))
        monkeypatch.setenv(wt.PRLIST_JSON_ENV, str(path))
        return path
    return _seed


def _pr(number, branch, *, state="OPEN", title=None, updated="2024-03-01T00:00:00Z"):
    return {"number": number, "headRefName": branch, "state": state,
            "title": title or f"PR {number}", "updatedAt": updated,
            "url": f"https://github.com/owner/repo/pull/{number}"}


def _report(cwd, command="open-pr", **kw):
    return wt.build_report(str(cwd), "owner/repo", command, **kw)


def _ids(report):
    return [c["id"] for c in report["candidates"]]


def _values(report):
    return [o["value"] for o in report["present"]["options"]]


# ── mode: none ───────────────────────────────────────────────────────────────
def test_none_when_no_checkout_has_actionable_work(tmp_path):
    repo = _init_repo(tmp_path / "repo")            # clean primary on main, no worktrees
    report = _report(repo)
    assert report["candidate_count"] == 0
    assert report["candidates"] == []
    assert report["present"] == {"ask": False, "mode": "none", "auto_target": None,
                                 "free_input": False, "options": []}


# ── mode: single ─────────────────────────────────────────────────────────────
def test_single_candidate_is_auto_selected_without_asking(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    wtx = _worktree_with_commit(repo, "feat/x", tmp_path / "wt-x")
    report = _report(repo)
    present = report["present"]
    assert present["mode"] == "single"
    assert present["ask"] is False
    assert present["auto_target"] == f"wt:{wtx}"
    assert present["free_input"] is False
    assert _values(report) == [f"wt:{wtx}"]         # no "all" option on a sole candidate


def test_uncommitted_only_worktree_is_actionable(tmp_path):
    """Uncommitted work counts even with zero commits ahead — it was never PR'd."""
    repo = _init_repo(tmp_path / "repo")
    wtx = _add_worktree(repo, "feat/x", tmp_path / "wt-x")
    _dirty(wtx)
    report = _report(repo)
    assert report["present"]["mode"] == "single"
    cand = report["candidates"][0]
    assert cand["uncommitted"] is True
    assert cand["uncommitted_count"] == 1
    assert cand["ahead"] == 0
    assert cand["detail"] == "1 uncommitted file"


def test_primary_on_base_with_work_is_a_candidate(tmp_path):
    """The primary checkout sitting on the base branch WITH work is a real open-pr
    candidate (the skill branches off it) — and is labelled as such."""
    repo = _init_repo(tmp_path / "repo")
    _dirty(repo)
    report = _report(repo)
    assert report["present"]["mode"] == "single"
    cand = report["candidates"][0]
    assert cand["id"] == "primary"
    assert cand["kind"] == "primary"
    assert cand["is_base_branch"] is True
    assert cand["label"] == "main (primary checkout)"


# ── mode: two ────────────────────────────────────────────────────────────────
def test_two_candidates_ask_with_both_plus_all(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    wta = _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a", when="2024-02-01T00:00:00")
    wtb = _worktree_with_commit(repo, "feat/b", tmp_path / "wt-b", when="2024-02-02T00:00:00")
    report = _report(repo)
    present = report["present"]
    assert present["mode"] == "two"
    assert present["ask"] is True
    assert present["auto_target"] is None
    assert present["free_input"] is False           # no free-text "Other" in two mode
    # Most recent first, then the "all" fan-out.
    assert _values(report) == [f"wt:{wtb}", f"wt:{wta}", "all"]
    assert present["options"][-1]["label"] == "All (2)"


# ── mode: many ───────────────────────────────────────────────────────────────
def test_many_offers_recent_worktree_then_base_checkout_then_all(tmp_path):
    """3+ candidates: option 1 is the most-recent NON-base candidate, option 2 is the
    primary-on-base checkout (open-pr's "branch off main" path), option 3 is All —
    and free-text "Other" is enabled so the rest stay reachable."""
    repo = _init_repo(tmp_path / "repo")
    wta = _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a", when="2024-02-01T00:00:00")
    wtb = _worktree_with_commit(repo, "feat/b", tmp_path / "wt-b", when="2024-02-02T00:00:00")
    wtc = _worktree_with_commit(repo, "feat/c", tmp_path / "wt-c", when="2024-02-03T00:00:00")
    _dirty(repo)                                    # primary on main, with work
    report = _report(repo)
    present = report["present"]
    assert present["mode"] == "many"
    assert present["ask"] is True
    assert present["auto_target"] is None
    assert present["free_input"] is True            # "Other" is the 4th option
    assert _values(report) == [f"wt:{wtc}", "primary", "all"]
    assert present["options"][-1]["label"] == "All (4)"
    # Every candidate is still in the full array even though only 2 are rendered.
    assert set(_ids(report)) == {"primary", f"wt:{wta}", f"wt:{wtb}", f"wt:{wtc}"}


def test_many_without_a_base_checkout_falls_back_to_second_worktree(tmp_path):
    """No primary-on-base candidate → option 2 is simply the 2nd-most-recent."""
    repo = _init_repo(tmp_path / "repo")
    _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a", when="2024-02-01T00:00:00")
    wtb = _worktree_with_commit(repo, "feat/b", tmp_path / "wt-b", when="2024-02-02T00:00:00")
    wtc = _worktree_with_commit(repo, "feat/c", tmp_path / "wt-c", when="2024-02-03T00:00:00")
    report = _report(repo)                          # primary is clean → not a candidate
    assert report["present"]["mode"] == "many"
    assert _values(report) == [f"wt:{wtc}", f"wt:{wtb}", "all"]


# ── mode: caller — the session's own checkout auto-wins ──────────────────────
def test_caller_cwd_auto_selects_this_sessions_own_checkout(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    wtb = _worktree_with_commit(repo, "feat/b", tmp_path / "wt-b")
    report = _report(repo, caller_cwd=str(wtb))
    present = report["present"]
    assert present["mode"] == "caller"              # would be "two" without the flag
    assert present["ask"] is False
    assert present["auto_target"] == f"wt:{wtb}"
    assert report["caller_match"] == f"wt:{wtb}"
    assert report["session_match"] is None
    assert _values(report) == [f"wt:{wtb}"]


def test_caller_cwd_matches_from_a_subdirectory_of_the_checkout(tmp_path):
    """The skill passes a literal ``$PWD``, which may be BELOW the worktree root."""
    repo = _init_repo(tmp_path / "repo")
    _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    wtb = _worktree_with_commit(repo, "feat/b", tmp_path / "wt-b")
    sub = wtb / "pkg" / "deep"
    sub.mkdir(parents=True)
    report = _report(repo, caller_cwd=str(sub))
    assert report["present"]["mode"] == "caller"
    assert report["present"]["auto_target"] == f"wt:{wtb}"


def test_session_registry_auto_selects_the_worked_in_worktree(tmp_path):
    """``$PWD`` never left the (clean) primary checkout, but the guardrail hook
    recorded the worktree this session actually worked in — auto-select it."""
    repo = _init_repo(tmp_path / "repo")
    _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    wtb = _worktree_with_commit(repo, "feat/b", tmp_path / "wt-b")
    sw.register("sess", str(wtb))
    report = _report(repo, caller_cwd=str(repo), session_id="sess")
    present = report["present"]
    assert present["mode"] == "caller"
    assert present["auto_target"] == f"wt:{wtb}"
    assert report["session_match"] == f"wt:{wtb}"
    assert report["caller_match"] == f"wt:{wtb}"    # the resolved auto-target


def test_session_registry_matches_from_a_subdirectory_of_the_checkout(tmp_path):
    """The git-guardrail hook records whatever directory ``git -C`` was invoked
    against, which may be BELOW the worktree root — normalize before matching."""
    repo = _init_repo(tmp_path / "repo")
    _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    wtb = _worktree_with_commit(repo, "feat/b", tmp_path / "wt-b")
    sub = wtb / "pkg" / "deep"
    sub.mkdir(parents=True)
    sw.register("sess", str(sub))
    report = _report(repo, caller_cwd=str(repo), session_id="sess")
    present = report["present"]
    assert present["mode"] == "caller"
    assert present["auto_target"] == f"wt:{wtb}"
    assert report["session_match"] == f"wt:{wtb}"


def test_caller_cwd_wins_over_the_session_registry(tmp_path):
    """Both resolve → the LIVE cwd is authoritative; the registry is only a fallback."""
    repo = _init_repo(tmp_path / "repo")
    wta = _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    wtb = _worktree_with_commit(repo, "feat/b", tmp_path / "wt-b")
    sw.register("sess", str(wtb))
    report = _report(repo, caller_cwd=str(wta), session_id="sess")
    assert report["present"]["auto_target"] == f"wt:{wta}"
    assert report["caller_match"] == f"wt:{wta}"
    assert report["session_match"] is None          # never consulted


def test_session_record_that_is_not_a_candidate_is_ignored(tmp_path):
    """A recorded worktree with no actionable work (e.g. its PR already merged) is not
    a candidate — it must not be auto-selected, and the ask stands."""
    repo = _init_repo(tmp_path / "repo")
    _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    _worktree_with_commit(repo, "feat/b", tmp_path / "wt-b")
    clean = _add_worktree(repo, "feat/clean", tmp_path / "wt-clean")   # no work at all
    sw.register("sess", str(clean))
    report = _report(repo, caller_cwd=str(repo), session_id="sess")
    assert report["present"]["mode"] == "two"       # still asks
    assert report["session_match"] is None


def test_session_record_pointing_at_a_dead_path_is_ignored(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    _worktree_with_commit(repo, "feat/b", tmp_path / "wt-b")
    sw.register("sess", str(tmp_path / "never-existed"))
    report = _report(repo, caller_cwd=str(repo), session_id="sess")
    assert report["present"]["mode"] == "two"
    assert report["session_match"] is None


# ── the primary-on-base exception ────────────────────────────────────────────
def test_caller_in_primary_on_base_does_not_auto_win(tmp_path):
    """THE deliberate exception: a session sitting in the PRIMARY checkout on the BASE
    branch does NOT auto-win while other candidates exist — work parked on main is
    ambient, not task-scoped, so the operator is still asked."""
    repo = _init_repo(tmp_path / "repo")
    _dirty(repo)                                    # primary on main, with work
    _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    report = _report(repo, caller_cwd=str(repo))
    assert report["present"]["mode"] == "two"       # asked, NOT auto-selected
    assert report["present"]["ask"] is True
    assert report["caller_match"] is None


def test_session_record_of_primary_on_base_does_not_auto_win(tmp_path):
    """The same exception applies on the registry path — a future change to what the
    guardrail hook records can never silently subvert it."""
    repo = _init_repo(tmp_path / "repo")
    _dirty(repo)
    _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    sw.register("sess", str(repo))
    report = _report(repo, session_id="sess")
    assert report["present"]["mode"] == "two"
    assert report["session_match"] is None


def test_caller_in_primary_on_a_feature_branch_does_auto_win(tmp_path):
    """The exception is narrow: it is about the BASE branch, not about being the
    primary checkout. A primary on a FEATURE branch is task-scoped and auto-wins."""
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "checkout", "-q", "-b", "feat/primary")
    _commit(repo, "primary work", when="2024-02-05T00:00:00")
    _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    report = _report(repo, caller_cwd=str(repo))
    assert report["present"]["mode"] == "caller"
    assert report["present"]["auto_target"] == "primary"
    assert report["caller_match"] == "primary"


def test_sole_primary_on_base_candidate_is_still_auto_selected(tmp_path):
    """The exception only withholds the CALLER short-circuit; a sole candidate is
    auto-selected regardless, via ``single``."""
    repo = _init_repo(tmp_path / "repo")
    _dirty(repo)
    report = _report(repo, caller_cwd=str(repo))
    assert report["present"]["mode"] == "single"
    assert report["present"]["auto_target"] == "primary"


# ── open-pr candidacy: PR'd branches are excluded ────────────────────────────
def test_branch_with_an_open_pr_is_not_an_open_pr_candidate(tmp_path, seed_prs):
    """An OPEN PR already exists for that branch — that is review-pr territory."""
    seed_prs([_pr(7, "feat/a")])
    repo = _init_repo(tmp_path / "repo")
    _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    wtb = _worktree_with_commit(repo, "feat/b", tmp_path / "wt-b")
    report = _report(repo, command="open-pr")
    assert _ids(report) == [f"wt:{wtb}"]
    assert report["present"]["mode"] == "single"


def test_squash_merged_branch_is_not_an_open_pr_candidate(tmp_path, seed_prs):
    """A squash-merged branch reads as "commits ahead of base" forever. Its PR is
    CLOSED, so it is not caught by the open-PR filter — the all-PRs branch set is what
    drops it. Without this the operator is offered a shipped branch."""
    seed_prs([_pr(7, "feat/a", state="MERGED")])
    repo = _init_repo(tmp_path / "repo")
    _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    wtb = _worktree_with_commit(repo, "feat/b", tmp_path / "wt-b")
    report = _report(repo, command="open-pr")
    assert _ids(report) == [f"wt:{wtb}"]


def test_open_pr_branch_with_uncommitted_work_is_still_excluded(tmp_path, seed_prs):
    """The ``open_pr is not None`` guard is load-bearing on its OWN: an open-PR branch
    with NEW uncommitted edits is actionable by the uncommitted rule, and the
    all-PRs ``prd_branches`` filter only blocks the ahead-path — so ONLY this guard
    keeps it out of the new-PR set (it is review-pr territory: the PR already exists)."""
    seed_prs([_pr(7, "feat/a", state="OPEN")])
    repo = _init_repo(tmp_path / "repo")
    wta = _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    _dirty(wta)                                     # open-PR branch + new uncommitted work
    wtb = _worktree_with_commit(repo, "feat/b", tmp_path / "wt-b")
    report = _report(repo, command="open-pr")
    assert _ids(report) == [f"wt:{wtb}"]            # feat/a stays out despite the dirty tree
    assert report["present"]["mode"] == "single"


def test_a_checkout_only_behind_base_is_not_a_candidate(tmp_path):
    """Actionable NEW-PR work is uncommitted changes OR commits AHEAD — a checkout that
    is only BEHIND base (no local work of its own) has nothing to open a PR from and
    must never be offered."""
    repo = _init_repo(tmp_path / "repo")
    _add_worktree(repo, "feat/stale", tmp_path / "wt-stale")   # branched at commit0
    _commit(repo, "advance main", when="2024-03-01T00:00:00")  # main moves ahead
    report = _report(repo)
    assert report["candidate_count"] == 0          # stale worktree is behind-only → excluded
    assert report["present"]["mode"] == "none"


def test_merged_branch_with_uncommitted_work_stays_a_candidate(tmp_path, seed_prs):
    """...but NEW uncommitted work in that same worktree was never PR'd, so it is
    actionable again."""
    seed_prs([_pr(7, "feat/a", state="MERGED")])
    repo = _init_repo(tmp_path / "repo")
    wta = _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    _dirty(wta)
    report = _report(repo, command="open-pr")
    assert _ids(report) == [f"wt:{wta}"]


def test_detached_and_bare_checkouts_are_never_candidates(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    det = _add_worktree(repo, "feat/det", tmp_path / "wt-det")
    _commit(det, "detached work")
    _git(det, "checkout", "-q", "--detach")
    report = _report(repo)
    assert report["candidate_count"] == 0
    assert report["present"]["mode"] == "none"


# ── review-pr: every open PR, annotated with its checkout ────────────────────
def test_review_pr_enumerates_open_prs_and_annotates_the_checkout(tmp_path, seed_prs):
    seed_prs([
        _pr(11, "feat/a", title="Alpha", updated="2024-03-01T00:00:00Z"),
        _pr(12, "feat/ghost", title="Ghost", updated="2024-03-02T00:00:00Z"),
        _pr(13, "feat/closed", state="CLOSED", updated="2024-03-03T00:00:00Z"),
    ])
    repo = _init_repo(tmp_path / "repo")
    wta = _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    report = _report(repo, command="review-pr")

    assert report["present"]["mode"] == "two"       # the CLOSED PR is not enumerated
    assert _ids(report) == ["pr:12", "pr:11"]       # newest updatedAt first

    ghost = report["candidates"][0]                 # PR #12: open, checked out nowhere
    assert ghost["kind"] == "pr-only"
    assert ghost["path"] is None
    assert ghost["detail"].endswith("not checked out in any worktree")

    alpha = report["candidates"][1]                 # PR #11: checked out in wt-a
    assert alpha["kind"] == "worktree"
    assert alpha["path"] == str(wta)
    assert alpha["label"] == "PR #11: Alpha"
    assert alpha["open_pr"]["number"] == 11


def test_review_pr_all_option_excludes_uncheckedout_prs_from_the_count(
        tmp_path, seed_prs):
    """A `pr-only` PR (not checked out anywhere) can never be launched by
    review-pr — it stops at the checked-out check — so bundling it into "All"
    would promise a run that always fails partway through and can strand later,
    launchable candidates unvisited. The "All" option's count/detail must
    reflect only the LAUNCHABLE (checked-out) candidates."""
    seed_prs([
        _pr(11, "feat/a", updated="2024-03-01T00:00:00Z"),
        _pr(12, "feat/ghost", updated="2024-03-02T00:00:00Z"),
    ])
    repo = _init_repo(tmp_path / "repo")
    _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    report = _report(repo, command="review-pr")
    assert report["present"]["mode"] == "two"
    all_opt = report["present"]["options"][-1]
    assert all_opt["value"] == "all"
    assert all_opt["label"] == "All (1)"
    assert "not checked out locally" in all_opt["detail"]


def test_review_pr_all_option_label_unchanged_when_every_pr_is_checked_out(
        tmp_path, seed_prs):
    """No `pr-only` candidates in the mix → the "All" option keeps its plain,
    pre-existing wording (same assertion shape as the open-pr `All (n)` tests)."""
    seed_prs([_pr(11, "feat/a"), _pr(12, "feat/b")])
    repo = _init_repo(tmp_path / "repo")
    _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    _worktree_with_commit(repo, "feat/b", tmp_path / "wt-b")
    report = _report(repo, command="review-pr")
    all_opt = report["present"]["options"][-1]
    assert all_opt["label"] == "All (2)"
    assert all_opt["detail"] == "Run on every candidate, one after another."


def test_review_pr_none_when_no_open_pr(tmp_path, seed_prs):
    seed_prs([_pr(9, "feat/a", state="MERGED")])
    repo = _init_repo(tmp_path / "repo")
    _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    report = _report(repo, command="review-pr")
    assert report["present"]["mode"] == "none"


def test_review_pr_auto_selects_the_pr_this_session_is_working_in(tmp_path, seed_prs):
    seed_prs([_pr(11, "feat/a"), _pr(12, "feat/b")])
    repo = _init_repo(tmp_path / "repo")
    _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    wtb = _worktree_with_commit(repo, "feat/b", tmp_path / "wt-b")
    report = _report(repo, command="review-pr", caller_cwd=str(wtb))
    assert report["present"]["mode"] == "caller"
    assert report["present"]["auto_target"] == "pr:12"
    assert report["caller_match"] == "pr:12"


def test_review_pr_long_title_is_truncated_in_the_label(tmp_path, seed_prs):
    seed_prs([_pr(11, "feat/a", title="x" * 80)])
    repo = _init_repo(tmp_path / "repo")
    _worktree_with_commit(repo, "feat/a", tmp_path / "wt-a")
    label = _report(repo, command="review-pr")["candidates"][0]["label"]
    assert label.endswith("…")
    assert len(label) < 60                          # the full title still rides in open_pr


# ── the report contract (the skills parse these keys) ────────────────────────
def test_report_carries_the_full_presentation_contract(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _worktree_with_commit(repo, "feat/x", tmp_path / "wt-x")
    report = _report(repo, caller_cwd=str(repo))
    assert set(report) == {
        "command", "repo", "base", "base_resolved", "caller_cwd", "caller_match",
        "session_match", "candidate_count", "candidates", "present",
    }
    assert report["command"] == "open-pr"
    assert report["repo"] == "owner/repo"
    assert report["base"] == "main"
    assert report["base_resolved"] == "main"        # no origin/main ref in a local repo
    assert set(report["present"]) == {"ask", "mode", "auto_target", "free_input",
                                      "options"}
    assert set(report["present"]["options"][0]) == {"value", "label", "detail"}
    cand = report["candidates"][0]
    for key in ("id", "kind", "path", "branch", "detached", "bare", "uncommitted",
                "uncommitted_count", "ahead", "behind", "last_commit_ts",
                "activity_ts", "open_pr", "is_base_branch", "label", "detail"):
        assert key in cand, f"candidate is missing contract key {key!r}"


def test_base_override_is_honoured(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _worktree_with_commit(repo, "feat/x", tmp_path / "wt-x")
    report = _report(repo, base="master")
    assert report["base"] == "master"


# ── failure modes: never a silently-truncated candidate set ──────────────────
def test_unreadable_pr_list_raises_rather_than_reporting_no_candidates(tmp_path,
                                                                       monkeypatch):
    """A failed PR fetch must NOT read as "no PRs" — that would offer the operator a
    branch that already has an open PR (open-pr) or hide every PR (review-pr)."""
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setenv(wt.PRLIST_JSON_ENV, str(tmp_path / "does-not-exist.json"))
    with pytest.raises(RuntimeError, match="Failed to fetch"):
        _report(repo, command="open-pr")
    with pytest.raises(RuntimeError, match="Failed to fetch"):
        _report(repo, command="review-pr")


def test_open_pr_with_no_origin_remote_skips_the_pr_fetch(tmp_path, monkeypatch):
    """A brand-new local repo with no ``origin`` yet (the /open-pr new-repo case,
    before ``open_pr._prepare_on_base`` creates the remote via `gh repo create`) has
    no remote to have any PRs against — the fetch must be skipped rather than raised,
    or the new-repo creation path in Step 3 is never reached. Point the PR-list seam
    at a nonexistent file so a RuntimeError here would prove the fetch was still
    attempted, not merely lucky."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init", when="2024-01-01T00:00:00")
    _dirty(repo)
    monkeypatch.setenv(wt.PRLIST_JSON_ENV, str(tmp_path / "does-not-exist.json"))
    report = _report(repo, command="open-pr")
    assert report["candidate_count"] == 1
    assert report["present"]["mode"] == "single"


def test_open_pr_raises_when_git_remote_itself_fails(tmp_path, monkeypatch):
    """``git remote`` failing outright (repo corruption, an unreadable config, ...)
    must raise — NOT be silently coerced into "no origin remote" and read as the
    brand-new-repo case, which would truncate the candidate set instead of failing
    loud. Distinct from ``test_open_pr_with_no_origin_remote_skips_the_pr_fetch``,
    where ``git remote`` SUCCEEDS with an empty/short list."""
    repo = _init_repo(tmp_path / "repo")
    _worktree_with_commit(repo, "feat/x", tmp_path / "wt-x")

    real_run_git = wt._run_git

    def fake_run_git(cwd, *args, **kw):
        if args == ("remote",):
            return None
        return real_run_git(cwd, *args, **kw)

    monkeypatch.setattr(wt, "_run_git", fake_run_git)
    with pytest.raises(RuntimeError, match="git remote failed"):
        _report(repo, command="open-pr")


def test_non_repo_cwd_raises(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(RuntimeError, match="Not a git repository"):
        _report(plain)


def test_unknown_command_raises(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    with pytest.raises(ValueError, match="unknown command"):
        _report(repo, command="merge-pr")


# ── the CLI ──────────────────────────────────────────────────────────────────
def test_cli_list_emits_one_json_object(tmp_path, capsys):
    repo = _init_repo(tmp_path / "repo")
    wtx = _worktree_with_commit(repo, "feat/x", tmp_path / "wt-x")
    rc = wt.main(["list", "--cwd", str(repo), "--repo", "owner/repo",
                  "--command", "open-pr", "--caller-cwd", str(wtx),
                  "--session-id", "sess"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["present"]["mode"] == "single"
    assert report["present"]["auto_target"] == f"wt:{wtx}"


def test_cli_list_reports_an_error_as_json_and_exit_1(tmp_path, capsys):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    rc = wt.main(["list", "--cwd", str(plain), "--repo", "owner/repo",
                  "--command", "open-pr"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert "Not a git repository" in payload["detail"]


def test_cli_list_rejects_an_unknown_command_value(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    with pytest.raises(SystemExit):                 # argparse `choices` guard
        wt.main(["list", "--cwd", str(repo), "--repo", "owner/repo",
                 "--command", "merge-pr"])


def test_resolve_verb_still_works(tmp_path, capsys):
    """The pre-existing single-answer verb is untouched by the enumerator."""
    repo = _init_repo(tmp_path / "repo")
    worktree = _add_worktree(repo, "feat/x", tmp_path / "wt-x")
    sw.register("sess", str(worktree))
    rc = wt.main(["resolve", "--session-id", "sess",
                  "--repo", "owner/repo", "--cwd", str(repo)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == str(worktree)
