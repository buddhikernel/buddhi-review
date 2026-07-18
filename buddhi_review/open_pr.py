"""The ``open-pr`` actuator — create a PR from local work, then launch the loop.

This is the silent core of the ``/open-pr`` flow (the interactive first-run and
rebase gates live in the skill's ``SKILL.md``). It ports the reference create-pr
git decision tree directly:

  1. Resolve repo   — infer ``owner/repo`` from the cwd's git ``origin`` (via
                      ``gh repo view``), or take an explicit ``--repo``.
  2. Git tree       — detect feature-branch / clean / uncommitted / on-base; commit
                      + push as needed; ensure remote infra (Paths A–D + Cases 1–4).
                      In Path C / Case 3 (local base commits share history with the
                      remote), an automatic ``git pull --rebase`` syncs them before
                      branching; a conflict aborts and is handed back to the user.
  3. Rebase detect  — after the branch is ready, if it is still behind base, emit one
                      non-blocking notice so the human can rebase by hand; no
                      automatic rebase is attempted at this stage (the manual gate is the
                      skill's interactive UX).
  4. Create + launch— ``gh pr create`` (idempotent) → launch the review adapter
                      detached, then return immediately. The LAST stdout line is
                      the PR URL (``^https?://``-grepable); every decoration goes
                      to stderr.

Every external effect (the git/gh runner, the detached launcher, the two output
streams) is injectable, so the decision tree is unit-testable against a temp repo
with no network.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from buddhi_review.config import auto_merge as resolve_auto_merge, load_config
from buddhi_review.transparency import automation_notice


class OpenPrError(RuntimeError):
    """An open-pr step failed in a way the actuator cannot recover from."""


@dataclass
class GitState:
    base: str
    current: str
    on_feature: bool
    has_commits: bool
    uncommitted: bool
    ahead: bool  # local feature branch has commits beyond base


# ── Runner seam ──────────────────────────────────────────────────────────────────

def _default_run(argv: Sequence[str], *, cwd: Optional[str] = None, timeout: int = 60,
                 input: Optional[str] = None):
    return subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, input=input)


def _out(r) -> str:
    return (getattr(r, "stdout", "") or "").strip()


def _ok(r) -> bool:
    return getattr(r, "returncode", 1) == 0


def _git(run, cwd, *args, timeout: int = 60):
    return run(["git", "-C", cwd, *args], cwd=None, timeout=timeout)


# ── Repo resolution (step 2) ───────────────────────────────────────────────────────

def resolve_repo(cwd: str, repo: Optional[str], run) -> str:
    """Resolve ``owner/repo``: an explicit ``repo`` arg wins; else infer from the
    cwd's GitHub ``origin`` via ``gh repo view``. Raises :class:`OpenPrError`
    when neither resolves."""
    if repo:
        return repo
    try:
        r = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                cwd=cwd, timeout=20)
    except Exception:
        r = None
    resolved = _out(r) if r is not None else ""
    if resolved:
        return resolved
    raise OpenPrError(
        f"could not infer the repo from {cwd}. Pass --repo <owner/repo>, or run from "
        f"a directory with a configured GitHub remote.")


# ── Git state + decision (step 4) ──────────────────────────────────────────────────

def detect_base(cwd: str, run) -> str:
    """The base branch: ``origin/HEAD`` symbolic ref → a local ``main``/``master``
    → ``main``."""
    r = _git(run, cwd, "symbolic-ref", "refs/remotes/origin/HEAD", timeout=10)
    if _ok(r):
        ref = _out(r)
        if ref.startswith("refs/remotes/origin/"):
            return ref[len("refs/remotes/origin/"):]
    r = _git(run, cwd, "branch", "--list", "main", "master", timeout=10)
    if _ok(r) and _out(r):
        first = _out(r).splitlines()[0]
        return first.replace("*", "").strip() or "main"
    return "main"


def detect_state(cwd: str, base: str, run) -> GitState:
    current = _out(_git(run, cwd, "branch", "--show-current", timeout=10))
    has_commits = _ok(_git(run, cwd, "rev-parse", "HEAD", timeout=10))
    uncommitted = bool(_out(_git(run, cwd, "status", "--porcelain", timeout=15)))
    on_feature = bool(current) and current != base
    ahead = False
    if on_feature and has_commits:
        r = _git(run, cwd, "log", f"{base}..HEAD", "--oneline", timeout=15)
        if not _ok(r):
            # No LOCAL base branch (common in a fresh clone / a per-PR worktree that
            # only checked out the feature branch). Fall back to the remote-tracking
            # ref so a clean feature branch is not mis-classified as "nothing to do".
            r = _git(run, cwd, "log", f"origin/{base}..HEAD", "--oneline", timeout=15)
            if not _ok(r):
                raise OpenPrError(
                    f"Could not resolve base branch '{base}' or 'origin/{base}'. "
                    f"Please verify that the base branch exists locally or on the remote.")
        ahead = _ok(r) and bool(_out(r))
    return GitState(base=base, current=current, on_feature=on_feature,
                    has_commits=has_commits, uncommitted=uncommitted, ahead=ahead)


def decide_path(state: GitState) -> str:
    """Pick the FIRST matching path (mirrors the reference decision tree):

    * ``A`` — feature branch, clean, ahead of base (just push).
    * ``B`` — feature branch with uncommitted changes (commit + push).
    * ``C`` — on the base branch with work (ensure remote infra, then branch).
    * ``D`` — nothing to do.
    """
    if state.on_feature and not state.uncommitted and state.ahead:
        return "A"
    if state.on_feature and state.uncommitted:
        return "B"
    if not state.on_feature and state.uncommitted:
        return "C"
    # On base with committed-but-unpushed work also counts as C; a clean,
    # nothing-to-push tree is D. The actuator treats an ahead-of-base feature
    # branch with no uncommitted work and no new commits as D too.
    if not state.on_feature and state.has_commits and not state.uncommitted:
        return "C_or_D"
    return "D"


# ── Branch slug ────────────────────────────────────────────────────────────────────

def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return (slug[:48].rstrip("-")) or "change"


def _branch_name(branch: Optional[str], prefix: str, title: str) -> str:
    if branch:
        return branch
    return f"{prefix}/{slugify(title)}"


# ── Branch preparation (commit / branch / push / remote infra) ──────────────────────

def _push(run, cwd, _err) -> None:
    r = _git(run, cwd, "push", "-u", "origin", "HEAD", timeout=120)
    if not _ok(r):
        detail = _out(r) or (getattr(r, "stderr", "") or "").strip() or "see git output"
        raise OpenPrError(f"git push failed: {detail}")


def prepare_branch(cwd: str, repo: str, state: GitState, path: str, *, title: str, body: str,
                   branch: Optional[str], branch_prefix: str, run, err) -> Optional[str]:
    """Execute the chosen git path; return the head branch to open the PR from, or
    ``None`` when there is nothing to do (Path D)."""
    base = state.base

    if path == "A":  # clean feature branch, ahead — ensure pushed
        _push(run, cwd, err)
        return state.current

    if path == "B":  # feature branch, uncommitted — commit + push
        _git(run, cwd, "add", "-A", timeout=60)
        cr = _git(run, cwd, "commit", "-m", title, "-m", body, timeout=60)
        if not _ok(cr):
            raise OpenPrError(f"git commit failed: {_out(cr) or (getattr(cr, 'stderr', '') or '').strip() or 'see git output'}")
        _push(run, cwd, err)
        return state.current

    if path == "D":
        return None

    if path == "C_or_D":
        # On base with committed work: ship it only if the local base is genuinely
        # ahead of the remote. Refresh the tracking ref first (a stale/missing
        # origin/base would otherwise mis-count), and treat an INCONCLUSIVE rev-list
        # as "there may be work" rather than silently dropping it.
        _git(run, cwd, "fetch", "origin", base, timeout=120)
        r = _git(run, cwd, "rev-list", "--count", f"origin/{base}..HEAD", timeout=15)
        if _ok(r) and _out(r) in ("", "0"):
            return None  # confirmed nothing to ship
        path = "C"  # fall through to the remote-infra + branch flow

    # Path C — on the base branch with work: ensure remote infra, then branch.
    return _prepare_on_base(cwd, repo, state, title=title, body=body, branch=branch,
                            branch_prefix=branch_prefix, run=run, err=err)


def _prepare_on_base(cwd: str, repo: str, state: GitState, *, title: str, body: str,
                     branch: Optional[str], branch_prefix: str, run, err) -> str:
    base = state.base
    head = _branch_name(branch, branch_prefix, title)

    # 3a. Ensure origin exists.
    if not _ok(_git(run, cwd, "remote", "get-url", "origin", timeout=10)):
        cr = run(["gh", "repo", "create", repo, "--private", "--source=.", "--remote", "origin"],
                 cwd=cwd, timeout=60)
        if not _ok(cr):
            raise OpenPrError(f"gh repo create failed: {_out(cr) or (getattr(cr, 'stderr', '') or '').strip() or 'see gh output'}")

    # 3b. Fetch.
    _git(run, cwd, "fetch", "origin", timeout=120)

    # 3c. Does the remote have the base branch?
    ls = _git(run, cwd, "ls-remote", "--heads", "origin", base, timeout=30)
    remote_has_base = _ok(ls) and bool(_out(ls))

    if not remote_has_base:
        # Case 1 — remote has no base branch.
        if not state.has_commits:
            bc = _git(run, cwd, "commit", "--allow-empty", "-m", "chore: initialize repository", timeout=30)
            if not _ok(bc):
                raise OpenPrError(f"bootstrap commit failed: {_out(bc) or 'see git output (check user.name/email)'}")
        r = _git(run, cwd, "push", "-u", "origin", base, timeout=120)
        if not _ok(r):
            raise OpenPrError(f"could not push the base branch: {_out(r) or 'see git output'}")
        return _branch_commit_push(cwd, base, head, title=title, body=body, run=run, err=err)

    if not state.has_commits:
        # Case 2 — remote has base, local is unborn.
        _git(run, cwd, "add", "-A", timeout=60)
        wc = _git(run, cwd, "commit", "-m", "wip: save local work", timeout=60)
        if not _ok(wc):
            raise OpenPrError(f"wip commit failed: {_out(wc) or 'see git output (check user.name/email)'}")
        return _graft_onto_base(cwd, base, head, run=run, err=err)

    # Local has commits. Shared or unrelated history?
    shared = _ok(_git(run, cwd, "merge-base", "HEAD", f"origin/{base}", timeout=15))
    if shared:
        # Case 3 — rebase local work onto the remote base, then branch. A conflict
        # is handed back to the human (free never resolves a conflict for you),
        # matching the unrelated-history path below.
        # git pull --rebase fails on a dirty tree without rebase.autostash; stash first.
        stashed = False
        if state.uncommitted:
            sr = _git(run, cwd, "stash", "--include-untracked", timeout=30)
            stashed = _ok(sr) and "No local changes to save" not in _out(sr)
        pr = _git(run, cwd, "pull", "--rebase", "origin", base, timeout=120)
        if not _ok(pr):
            _git(run, cwd, "rebase", "--abort", timeout=30)
            if stashed:
                _git(run, cwd, "stash", "pop", timeout=30)
            raise OpenPrError(
                f"rebasing local work onto origin/{base} conflicted — resolve the "
                f"divergent history by hand, then re-run.")
        if stashed:
            sp = _git(run, cwd, "stash", "pop", timeout=30)
            if not _ok(sp):
                raise OpenPrError(
                    f"stash pop failed after rebase — the working tree may have conflicts: "
                    f"{_out(sp) or (getattr(sp, 'stderr', '') or '').strip() or 'see git output'}")
        return _branch_commit_push(cwd, base, head, title=title, body=body, run=run, err=err)
    # Case 4 — unrelated history: graft the local commits onto origin/base.
    return _graft_onto_base(cwd, base, head, run=run, err=err)


def _branch_commit_push(cwd: str, _base: str, head: str, *, title: str, body: str, run, err) -> str:
    """Create the feature branch off the current HEAD, commit any pending work,
    push. Used by Path C Cases 1 & 3."""
    cr = _git(run, cwd, "checkout", "-b", head, timeout=30)
    if not _ok(cr):
        # The branch may already exist; switch to it.
        co = _git(run, cwd, "checkout", head, timeout=30)
        if not _ok(co):
            raise OpenPrError(f"git checkout failed: {_out(co)}")
    _git(run, cwd, "add", "-A", timeout=60)
    # Commit only if there is something staged (a clean tree makes commit fail).
    if _out(_git(run, cwd, "status", "--porcelain", timeout=15)):
        ci = _git(run, cwd, "commit", "-m", title, "-m", body, timeout=60)
        if not _ok(ci):
            raise OpenPrError(f"git commit failed: {_out(ci) or (getattr(ci, 'stderr', '') or '').strip() or 'see git output'}")
    _push(run, cwd, err)
    return head


def _graft_onto_base(cwd: str, base: str, head: str, *, run, err) -> str:
    """Cherry-pick the local commits onto a fresh branch off ``origin/base`` and
    push. Used by Path C Cases 2 & 4 (unborn / unrelated history)."""
    # Commit any pending work FIRST so it travels with the graft. Case 4 can reach
    # here with a dirty tree; a stash would strand it (CWE: silent data loss). Case
    # 2 already committed its work, so this is a no-op there.
    committed_wip = False
    if _out(_git(run, cwd, "status", "--porcelain", timeout=15)):
        _git(run, cwd, "add", "-A", timeout=60)
        ci = _git(run, cwd, "commit", "-m", "wip: save local work", timeout=60)
        if not _ok(ci):
            raise OpenPrError(f"git commit failed: {_out(ci) or (getattr(ci, 'stderr', '') or '').strip() or 'see git output'}")
        committed_wip = True
    shas = _out(_git(run, cwd, "rev-list", "--reverse", f"origin/{base}..HEAD", timeout=30)).split()
    cr = _git(run, cwd, "checkout", "-B", "_buddhi_temp_pr", f"origin/{base}", timeout=30)
    if not _ok(cr):
        if committed_wip:
            _git(run, cwd, "checkout", base, timeout=30)
            _git(run, cwd, "reset", "--soft", "HEAD~1", timeout=30)
        raise OpenPrError(f"could not branch off origin/{base}: {_out(cr) or 'see git output'}")
    for sha in shas:
        pr = _git(run, cwd, "cherry-pick", sha, timeout=60)
        if not _ok(pr):
            _git(run, cwd, "cherry-pick", "--abort", timeout=30)
            _git(run, cwd, "checkout", base, timeout=30)
            if committed_wip:
                _git(run, cwd, "reset", "--soft", "HEAD~1", timeout=30)
            _git(run, cwd, "branch", "-D", "_buddhi_temp_pr", timeout=30)
            raise OpenPrError(
                f"cherry-pick of {sha[:8]} onto origin/{base} conflicted — resolve the "
                f"divergent history by hand, then re-run.")
    # Guard: -M force-overwrites an existing local branch; refuse rather than silently lose history.
    if _ok(_git(run, cwd, "rev-parse", "--verify", head, timeout=15)):
        _git(run, cwd, "checkout", base, timeout=30)
        if committed_wip:
            _git(run, cwd, "reset", "--soft", "HEAD~1", timeout=30)
        _git(run, cwd, "branch", "-D", "_buddhi_temp_pr", timeout=30)
        raise OpenPrError(
            f"local branch '{head}' already exists — delete or rename it, then re-run "
            f"(or pass a different --branch name).")
    br = _git(run, cwd, "branch", "-M", "_buddhi_temp_pr", head, timeout=30)
    if not _ok(br):
        _git(run, cwd, "checkout", base, timeout=30)
        if committed_wip:
            _git(run, cwd, "reset", "--soft", "HEAD~1", timeout=30)
        raise OpenPrError(f"git branch rename failed: {_out(br)}")
    _push(run, cwd, err)
    return head


# ── Rebase detection (step 5 — detect only; the rebase is left to the human) ───────

def behind_count(cwd: str, base: str, run) -> int:
    """How many commits the head is behind ``origin/base`` (0 on any uncertainty)."""
    _git(run, cwd, "fetch", "origin", base, timeout=60)
    r = _git(run, cwd, "rev-list", "--count", f"HEAD..origin/{base}", timeout=15)
    if not _ok(r):
        return 0
    try:
        return int(_out(r) or "0")
    except ValueError:
        return 0


# ── Create + launch (step 6) ───────────────────────────────────────────────────────

_URL_RE = re.compile(r"^https?://", re.MULTILINE)


def _extract_url(text: str) -> Optional[str]:
    urls = [ln.strip() for ln in (text or "").splitlines() if ln.strip().startswith(("http://", "https://"))]
    return urls[-1] if urls else None


def create_and_launch(repo: str, cwd: str, base: str, head: str, *, title: str, body: str,
                      run, launch: Callable, out, err, no_loop: bool = False,
                      max_rounds: Optional[int] = None) -> str:
    """``gh pr create`` (idempotent on "already exists") → launch the loop detached.
    Returns the PR URL and prints it as the LAST stdout line."""
    cr = run(["gh", "pr", "create", "-R", repo, "--base", base, "--head", head,
              "--title", title, "--body", body], cwd=cwd, timeout=120)
    url = ""
    if _ok(cr):
        url = _extract_url(getattr(cr, "stdout", "")) or ""
    else:
        combined = (getattr(cr, "stdout", "") or "") + (getattr(cr, "stderr", "") or "")
        if "already exists" in combined.lower():
            vr = run(["gh", "pr", "view", head, "-R", repo, "--json", "url", "-q", ".url"],
                     cwd=cwd, timeout=30)
            url = _extract_url(getattr(vr, "stdout", "")) if _ok(vr) else ""
            print("• PR already exists for this branch — reusing it.", file=err)
        else:
            raise OpenPrError(f"gh pr create failed: {_out(cr) or (getattr(cr, 'stderr', '') or '').strip() or 'see gh output'}")
    if not url:
        raise OpenPrError("could not determine the PR URL after gh pr create.")

    m = re.search(r"/(\d+)/?$", url)
    if not m:
        raise OpenPrError(f"could not extract a PR number from URL '{url}'.")
    pr_number = m.group(1)
    print(f"✓ PR #{pr_number} ready — {url}", file=err)

    if no_loop:
        print("• --no-loop set; skipping the review-loop launch.", file=err)
    else:
        # Launch the review loop detached and return immediately. max_rounds is
        # forwarded as a keyword ONLY when set, so a caller-injected `launch` seam
        # with the historical (pr_number, repo, cwd, err) signature keeps working
        # unchanged when --max-rounds is not passed.
        if max_rounds is not None:
            launch(pr_number, repo, cwd, err, max_rounds=max_rounds)
        else:
            launch(pr_number, repo, cwd, err)

    # stdout DATA contract: the PR URL is the LAST stdout line (may be preceded by
    # automation_notice transparency lines).
    print(url, file=out)
    return url


def _dispatch_launch(pr_number: str, repo: str, cwd: str, err, *,
                     max_rounds: Optional[int] = None) -> None:
    """Default launch callable: route the loop launch through the front-door
    dispatcher so a separately-installed, active backend can take over the same
    ``/open-pr`` — with none installed it runs the free loop, unchanged. Imported
    locally to keep ``open_pr``'s git decision tree free of a load-time
    dependency on the launch layer."""
    from buddhi_review.backends import launch_review_loop
    # Resolve auto-merge to a concrete bool BEFORE the backend hand-off (mirrors
    # cli.py's _review_pr), so the wizard's repos[<repo>].auto_merge reaches a
    # directly-invoked non-free backend too — open-pr has no --auto-merge flag of
    # its own, so this is just the config lookup (no tri-state to layer on top).
    # load_config()'s own missing/parse warnings print straight to sys.stderr
    # (same as cli.py's _review_pr, which calls load_config() the same way) —
    # not redirected to `err` here, since in real usage err IS sys.stderr and
    # redirecting would only matter for a test-injected stream no test asserts on.
    auto_merge = resolve_auto_merge(load_config(), repo)
    # The chosen backend's launcher prints its own "where to watch" line (free → a
    # terminal-log link) to stderr, so the actuator's stdout stays the PR URL.
    try:
        launch_review_loop(pr_number, repo, cwd, out=err, err=err, max_rounds=max_rounds,
                           auto_merge=auto_merge)
    except Exception as exc:  # never crash the actuator after the PR is open
        print(f"⚠ could not launch the review loop ({exc}); run it manually: "
              f"python3 -m buddhi_review review-pr {pr_number} --repo {repo} --cwd {cwd}", file=err)


# ── Orchestration ──────────────────────────────────────────────────────────────────

def actuate(*, repo: Optional[str], cwd: Optional[str], base: Optional[str], title: str,
            body: str, branch: Optional[str] = None, branch_prefix: str = "feat",
            no_loop: bool = False, max_rounds: Optional[int] = None,
            run: Optional[Callable] = None,
            launch: Optional[Callable] = None, out=None, err=None) -> int:
    """Run the full silent open-pr flow. Returns an exit code (0 on success).
    Decoration → ``err`` (stderr); the PR URL is the last line on ``out`` (stdout)."""
    run = run or _default_run
    launch = launch or _dispatch_launch
    out = out or sys.stdout
    err = err or sys.stderr
    cwd = cwd or os.getcwd()
    if not os.path.isdir(cwd):
        print(f"open-pr: --cwd '{cwd}' is not a directory.", file=err)
        return 2

    try:
        repo = resolve_repo(cwd, repo, run)
        base = base or detect_base(cwd, run)
        print(f"▸ {repo}  base {base}  cwd {cwd}", file=err)

        state = detect_state(cwd, base, run)
        path = decide_path(state)
        head = prepare_branch(cwd, repo, state, path, title=title, body=body, branch=branch,
                              branch_prefix=branch_prefix, run=run, err=err)
        if head is None:
            print(f"No changes to commit in {repo}. Nothing to do.", file=err)
            return 0

        behind = behind_count(cwd, base, run)
        if behind > 0:
            # Surface the behind-state so the human can rebase by hand. The ⚙ [auto]
            # transparency line is user-facing and goes to STDOUT by contract; the
            # PR URL still prints last, so it remains the final stdout line.
            automation_notice(
                "rebase gate",
                f"{head} is {behind} commit(s) behind origin/{base}",
                status="fallback",
                hint="rebase by hand if the PR shows conflicts",
                stream=out)

        create_and_launch(repo, cwd, base, head, title=title, body=body, run=run,
                          launch=launch, out=out, err=err, no_loop=no_loop,
                          max_rounds=max_rounds)
        return 0
    except OpenPrError as exc:
        print(f"open-pr: {exc}", file=err)
        return 1
