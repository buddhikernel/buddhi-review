#!/usr/bin/env python3
"""worktree_target.py — decide WHICH checkout the /open-pr + /review-pr skills act on.

Two verbs, one question ("which checkout?") answered at two levels of ambition:

  resolve  Single answer. Given the calling session, return the ONE checkout the
           skill should open the PR from (the session's recorded worktree when it
           is a live checkout of the target repo, else the cwd). Prints a bare
           path; never raises; see :func:`resolve`.
  list     Enumerate ALL actionable candidate checkouts (and, for ``review-pr``,
           every open PR) plus a ready-to-render option set encoding the
           presentation rules. Prints ONE JSON object; see :func:`build_report`.

WHY ``resolve`` EXISTS. The skills derive the checkout from the calling session's
``$PWD`` (``git rev-parse --show-toplevel``). When the agent follows the standing
"do your work in a NEW worktree off main" rule, the session is SPAWNED in checkout
A but creates and operates on worktree B via ``git -C B`` — its shell ``$PWD``
never leaves A. ``session_worktrees`` (written automatically by the git-guardrail
hook) knows the session actually worked in B; this module reads that record and
returns B, so the skill opens the PR from the worktree the session worked in
WITHOUT asking — even when ``$PWD`` is elsewhere.

WHY ``list`` EXISTS. ``resolve`` answers only "is there a session-recorded
worktree?" — it cannot see that a repo has SEVERAL checkouts with actionable work
(the common case once work lives in worktrees under ``.claude/worktrees/``), so a
skill built on it alone silently acts on one checkout and never offers the others.
``list`` enumerates the actionable set and hands the skill a presentation contract
(``present.mode`` ∈ ``none`` | ``single`` | ``caller`` | ``two`` | ``many``) that
says exactly when to ask and with which options — so a single candidate is still
auto-selected silently, while a user with several worktrees is asked which one to
open the PR from instead of losing the choice.

SAFETY (both verbs). A session-recorded path is used ONLY when it (a) is a LIVE
git worktree — it exists on disk AND ``git rev-parse --is-inside-work-tree``
succeeds, (b) — for ``resolve`` — has an ``origin`` remote resolving to the TARGET
repo (same full ``owner/…/repo`` path, and, when both the record and the target
carry a host, the same host, so a same-slug repo on a different host / GitHub
Enterprise never matches), and (c) differs from the cwd checkout. For ``list`` the
recorded path must additionally BE one of the enumerated candidates, which is a
strictly stronger check (a candidate is by construction a worktree of this repo).
Any failure falls back to the cwd checkout / to asking — so a stale record, a
phantom mis-resolution, or a worktree of a different repo can never drive the loop
into the wrong place.

``resolve`` NEVER raises to its caller: the CLI always prints a usable path (the
cwd on any trouble), so the skill can consume it with ``$(...)`` unconditionally.
``list`` DOES fail loudly (a JSON ``{"status": "error", …}`` and exit 1) when the
repo or its PR list cannot be read — a truncated candidate set is worse than no
answer, because a missing candidate is invisible to the operator.

Pure stdlib (os / re / json / sys / subprocess / argparse) plus
``session_worktrees``. Read-only: nothing here mutates a working tree.

Test seam (no network, no ``gh`` required): ``$BUDDHI_REVIEW_PRLIST_JSON`` — a path
to a JSON file with the PR list (the same shape ``gh pr list --json
number,headRefName,url,title,updatedAt,state,isCrossRepository,headRepositoryOwner,
headRepository`` emits), used instead of shelling out. A fixture may omit the last
three fields — a missing/false ``isCrossRepository`` is treated as a same-repo PR.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

from buddhi_review import session_worktrees

# Longest PR title rendered inline in an option label; the full title still lands
# in the JSON so the skill can show more if it wants.
_TITLE_MAX = 52

# Test seam: a JSON file standing in for ``gh pr list`` (see the module docstring).
PRLIST_JSON_ENV = "BUDDHI_REVIEW_PRLIST_JSON"


def _run_git(cwd, *args, timeout=15):
    """``git -C <cwd> <args>`` stdout stripped on success, else None. Never raises
    (a missing git, a non-repo cwd, or a timeout all yield None)."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


def _is_live_worktree(path):
    """True iff ``path`` names a LIVE git worktree — it exists on disk AND git
    reports it is inside a work tree.

    A session→worktree record can be a PHANTOM: the git-guardrail hook resolves a
    relative worktree path against the fixed session cwd, so when the agent had
    ``cd``'d into another repo the recorded path can point at a directory that does
    not exist (or is not a checkout at all). It can also go stale (its worktree was
    removed). Ignoring such a path makes the skill fall back to the cwd checkout
    instead of opening the PR from a non-existent / wrong place. Never raises."""
    try:
        if not path or not os.path.isdir(path):
            return False
        return _run_git(path, "rev-parse", "--is-inside-work-tree") == "true"
    except Exception:
        return False


def _split_repo(spec):
    """Parse a repo spec (an ``origin`` URL or a bare ``owner/repo``) into
    ``(host, path)`` — ``host`` lowercased or None (a bare ``owner/repo`` carries
    no host), ``path`` the FULL lowercased ``owner/…/repo`` with any trailing
    ``.git`` removed. Handles the scp-like ``user@host:path``, ``scheme://host/path``
    (including a ``user@`` and a ``:port``), and extra path depth (kept, not
    collapsed — so ``teamx/a/b`` and ``teamy/a/b`` never coincide). None when the
    path has fewer than two components. Never raises."""
    try:
        if not spec:
            return None
        s = spec.strip()
        if not s:
            return None
        host = None
        m = re.match(r"^[^/@]+@([^/:]+):(.+)$", s)  # scp-like user@host:path
        if m:
            host = m.group(1).lower()
            s = m.group(2)
        elif "://" in s:
            rest = s.split("://", 1)[1]              # drop scheme
            if "/" in rest:
                hostpart, s = rest.split("/", 1)
            else:
                hostpart, s = rest, ""
            hostpart = hostpart.split("@")[-1]       # drop any user@
            host = (hostpart.split(":")[0].lower() or None)  # drop :port
        if s.endswith(".git"):
            s = s[:-4]
        parts = [p for p in s.strip("/").split("/") if p]
        if len(parts) < 2:
            return None
        return (host, "/".join(parts).lower())
    except Exception:
        return None


def _specs_match(pa, pb):
    """True iff two PARSED ``(host, path)`` specs name the same repo: equal full
    ``owner/…/repo`` path AND — when BOTH carry a host — the same host. A host-less
    spec matches that path on any host; two specs that BOTH carry a host must agree
    on it, so same-slug repos on different hosts (or GitHub Enterprise vs
    github.com) never compare equal. Never raises."""
    if not pa or not pb:
        return False
    if pa[1] != pb[1]:
        return False
    if pa[0] and pb[0] and pa[0] != pb[0]:
        return False
    return True


def _repos_match(a, b):
    """``_specs_match`` over two raw string specs (an ``origin`` URL or a bare
    ``owner/repo``). Never raises."""
    return _specs_match(_split_repo(a), _split_repo(b))


def _pr_head_is_local(pr, repo):
    """True iff ``pr``'s head branch lives IN ``repo`` (never raises).

    A fork PR can push ANY branch name — including one that collides with an
    unrelated local branch, e.g. a fork opened from its own ``main`` — so a
    branch-name-only match would attach that fork PR to the wrong checkout
    (see the module's SAFETY section). ``isCrossRepository`` false, or absent
    (a minimal test-seam fixture pre-dating this field, which represents a
    same-repo PR), means the head is a branch of THIS repo — always local. A
    cross-repository PR is local only when its head repo is, by coincidence,
    the SAME ``owner/repo`` as ``repo`` (e.g. a PR opened repo-to-repo rather
    than from a personal fork)."""
    try:
        if not pr.get("isCrossRepository"):
            return True
        owner = (pr.get("headRepositoryOwner") or {}).get("login") or ""
        name = (pr.get("headRepository") or {}).get("name") or ""
        if not owner or not name:
            return False
        return _repos_match(f"{owner}/{name}", repo)
    except Exception:
        return False


def _toplevel(path):
    """The real, absolute git toplevel of ``path`` (fallback: realpath(path)) so
    two paths naming the same checkout compare equal. Never raises."""
    top = _run_git(path, "rev-parse", "--show-toplevel")
    try:
        return os.path.realpath(top) if top else os.path.realpath(path)
    except Exception:
        return top or path


def resolve(session_id, repo, cwd):
    """The checkout the loop should open the PR from: the session's RECORDED
    worktree when it is a live git worktree of the target ``owner/repo`` and
    differs from ``cwd``; otherwise ``cwd`` (unchanged behaviour). Never raises."""
    try:
        if not session_id:
            return cwd
        recorded = session_worktrees.lookup(session_id)
        if not recorded or not _is_live_worktree(recorded):
            return cwd
        # The target repo spec: the explicit owner/repo when given, else the cwd's
        # own origin URL (so the common same-repo case works without the caller
        # resolving it). Cannot verify the origin match without a target → fall back.
        target = repo.strip() if (repo and repo.strip()) else \
            _run_git(cwd, "remote", "get-url", "origin")
        tspec = _split_repo(target)
        if not tspec:
            return cwd
        # A host-less target (a bare owner/repo, e.g. `gh` nameWithOwner) would
        # otherwise match that path on ANY host. Borrow the cwd's origin host when
        # cwd IS that same repo — the caller derived the target from cwd, so they
        # share a host — so the host guard still rejects a same-slug worktree on a
        # different host.
        if tspec[0] is None:
            cspec = _split_repo(_run_git(cwd, "remote", "get-url", "origin"))
            if cspec and cspec[0] and cspec[1] == tspec[1]:
                tspec = (cspec[0], tspec[1])
        rspec = _split_repo(_run_git(recorded, "remote", "get-url", "origin"))
        if not _specs_match(rspec, tspec):
            return cwd  # a worktree of a DIFFERENT repo — never cross-target
        if _toplevel(recorded) == _toplevel(cwd):
            return cwd  # already the cwd checkout — nothing to switch to
        return recorded
    except Exception:
        return cwd


# ── candidate enumeration (the ``list`` verb) ────────────────────────────────
def _run(argv, cwd=None, timeout=90):
    """Run a command; return ``(returncode, stdout, stderr)``. Never raises (a
    missing binary or a timeout comes back as rc 127 with the error in stderr)."""
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as exc:
        return 127, "", str(exc)


def _git_int(cwd, *args):
    """A git command expected to print a single integer. Raises RuntimeError when
    the command itself fails or times out — ``_run_git`` returns ``None`` for
    both — rather than coercing that into a count of 0: ``_candidates_create``
    keys candidacy on ``ahead > 0``, so a silent 0 here would hide an actionable
    checkout from the operator, exactly the failure mode this module's ``list``
    verb otherwise fails loudly on (see the module docstring). A genuine empty
    stdout on success is still 0."""
    val = _run_git(cwd, *args)
    if val is None:
        raise RuntimeError(f"git {' '.join(args)} failed for {cwd}")
    try:
        return int(val) if val else 0
    except ValueError:
        raise RuntimeError(
            f"git {' '.join(args)} for {cwd} printed non-integer output: {val!r}")


def detect_base(cwd):
    """Resolve the base branch NAME: origin/HEAD → the remote's OWN advertised
    default → a local main/master → "main"."""
    ref = _run_git(cwd, "symbolic-ref", "refs/remotes/origin/HEAD")
    if ref:  # e.g. refs/remotes/origin/main
        prefix = "refs/remotes/origin/"
        if ref.startswith(prefix):
            return ref[len(prefix):]
        return ref.rsplit("/", 1)[-1]
    # `git remote add` + a manual `fetch` (unlike `git clone`) never sets
    # origin/HEAD locally, so a custom default branch (e.g. "develop"/"trunk")
    # would otherwise be masked by an identically-named local main/master guess
    # below (a manually configured checkout can have BOTH a local "main" and a
    # remote whose real default is "trunk"). Ask the remote directly, first —
    # `git remote show origin` fails fast (missing origin, unreachable remote)
    # rather than hanging, so a brand-new repo with no remote yet still falls
    # through to the local-branch and "main" fallbacks below.
    shown = _run_git(cwd, "remote", "show", "origin", timeout=30)
    if shown:
        for line in shown.splitlines():
            line = line.strip()
            if line.startswith("HEAD branch:"):
                name = line[len("HEAD branch:"):].strip()
                if name and name != "(unknown)":
                    return name
                break
    for cand in ("main", "master"):
        if _run_git(cwd, "rev-parse", "--verify", "--quiet",
                    f"refs/heads/{cand}") is not None:
            return cand
    return "main"


def resolve_baseref(cwd, base, remote="origin"):
    """Prefer the remote-tracking ``<remote>/<base>`` (the real latest); fall back to
    the local ``<base>`` ref; None when neither resolves."""
    if _run_git(cwd, "rev-parse", "--verify", "--quiet",
                f"refs/remotes/{remote}/{base}") is not None:
        return f"{remote}/{base}"
    if _run_git(cwd, "rev-parse", "--verify", "--quiet",
                f"refs/heads/{base}") is not None:
        return base
    return None


def list_worktrees(cwd):
    """Parse ``git worktree list --porcelain`` into a list of dicts.

    Each entry: ``{path, head, branch (or ''), detached (bool), bare (bool)}``. The
    FIRST entry is always the primary working tree (git's contract).

    A worktree whose linked directory was deleted without ``git worktree remove``
    carries a ``prunable <reason>`` line instead of a normal checkout — its path no
    longer exists on disk, so introspecting it (``git status``/``rev-list`` etc.
    against a missing cwd) would fail. Such entries are dropped here rather than
    handed to the caller, so one stale checkout can't take down enumeration of
    every other, live candidate.

    Raises RuntimeError when the command fails — same fail-loud contract as
    :func:`fetch_prs`/:func:`build_report`: a silently-empty worktree list would
    read as "no candidates" and hide every checkout from the operator instead of
    surfacing the underlying git error."""
    rc, out, err = _run(["git", "-C", cwd, "worktree", "list", "--porcelain"])
    if rc != 0:
        raise RuntimeError(f"git worktree list failed for {cwd}: {err.strip()}")
    entries, cur = [], None
    for line in out.splitlines():
        if line.startswith("worktree "):
            if cur is not None and not cur["prunable"]:
                entries.append(cur)
            cur = {"path": line[len("worktree "):], "head": "",
                   "branch": "", "detached": False, "bare": False,
                   "prunable": False}
        elif cur is None:
            continue
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            cur["branch"] = (ref[len("refs/heads/"):]
                             if ref.startswith("refs/heads/") else ref)
        elif line.strip() == "detached":
            cur["detached"] = True
        elif line.strip() == "bare":
            cur["bare"] = True
        elif line.startswith("prunable") or line.strip() == "prunable":
            cur["prunable"] = True
    if cur is not None and not cur["prunable"]:
        entries.append(cur)
    for entry in entries:
        del entry["prunable"]
    return entries


def fetch_prs(repo, state="open"):
    """PRs as a list of ``{number, headRefName, url, title, updatedAt, state,
    isCrossRepository, headRepositoryOwner, headRepository}`` — the last three
    let :func:`_pr_head_is_local` qualify a branch-name match by source repo
    instead of trusting ``headRefName`` alone (a fork can reuse any name).

    ``state`` is "open" or "all" — the latter so ``open-pr`` can exclude branches that
    were ALREADY turned into a PR, including squash-merged ones (which still read as
    "commits ahead of base" forever). Honours the ``$BUDDHI_REVIEW_PRLIST_JSON`` test
    seam (seam entries carrying no ``state`` are treated as OPEN, so a fixture written
    for the open list stays valid); otherwise shells out to ``gh pr list``.

    Returns None on failure so a caller never confuses an error with "no PRs" — a
    silently-empty PR list would make every branch look un-PR'd. Also returns None
    when the result hits the ``--limit`` boundary exactly: ``gh pr list --limit``
    is "the maximum number of items to FETCH" (``gh pr list --help``), not a
    server-side page size, so a full page means the true count may be larger and
    older PRs (a branch's own prior PR, most likely) could be missing — the same
    fail-loud contract as an outright ``gh`` failure, rather than silently acting
    on a truncated set."""
    seam = os.environ.get(PRLIST_JSON_ENV)
    if seam:
        try:
            with open(seam, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, list):
            return None
        if state == "open":
            return [p for p in data if isinstance(p, dict)
                    and str(p.get("state", "OPEN")).upper() == "OPEN"]
        return [p for p in data if isinstance(p, dict)]
    limit = 2000
    rc, out, _ = _run([
        "gh", "pr", "list", "--repo", repo, "--state", state,
        "--json", "number,headRefName,url,title,updatedAt,state,"
                  "isCrossRepository,headRepositoryOwner,headRepository",
        "--limit", str(limit),
    ], timeout=180)
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    if len(data) >= limit:
        return None
    return data


# ── per-checkout introspection ───────────────────────────────────────────────
def _dirty_paths(cwd):
    """Changed paths via NUL-delimited porcelain. The ``-z`` form NEVER quotes paths,
    so spaces / non-ASCII / control chars survive intact (the default porcelain
    C-quotes them, which then fail to stat and degrade the activity ranking). ``[]``
    on a genuinely clean tree (empty stdout, rc 0).

    Raises RuntimeError when the command itself fails (e.g. a corrupt or unreadable
    index) — same fail-loud contract as :func:`_git_int`: a corrupt index can fail
    ``git status`` while commit-reading commands like ``rev-list``/``log`` still
    succeed, so silently returning ``[]`` here would make a dirty checkout with
    real local work read as clean and drop it from ``present`` entirely."""
    rc, out, err = _run(["git", "-C", cwd, "status", "--porcelain", "-z"])
    if rc != 0:
        raise RuntimeError(f"git status failed for {cwd}: {err.strip()}")
    fields = out.split("\0")
    paths, i = [], 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        # For R/C entries, `-z` output reverses the pair to "to from" (see
        # `git help status`, Porcelain Format Version 1: "the field order is
        # reversed (e.g from -> to becomes to from)") — so `path` here is
        # already the DESTINATION path, the one that exists on disk. The
        # NEXT NUL-delimited field is the source path, which no longer
        # exists post-rename; consume it so it is not mis-parsed as its own
        # status entry, but don't record it.
        paths.append(path)
        if "R" in code or "C" in code:
            i += 1
    return paths


def _dirty_mtime(path, dirty_paths):
    """Newest mtime among the changed files (so an actively-edited but uncommitted
    worktree ranks as recent). Falls back to the directory's own mtime."""
    best = 0
    for rel in dirty_paths:
        try:
            best = max(best, int(os.path.getmtime(os.path.join(path, rel))))
        except OSError:
            continue
    if best == 0:
        try:
            best = int(os.path.getmtime(path))
        except OSError:
            best = 0
    return best


def _truncate(text, limit=_TITLE_MAX):
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def introspect(entry, baseref, open_by_branch, is_primary):
    """Build the per-checkout record (git facts only; candidacy is decided later)."""
    path = entry["path"]
    branch = entry.get("branch", "")
    rec = {
        # The full path, not just its basename — two DIFFERENT worktrees (e.g. of
        # different repos, or an old removed-and-recreated checkout) can share a
        # basename, and this id is the ONLY key callers use to map a chosen option
        # back to its candidate (present.options[].value, auto_target, the
        # caller/session auto-select match below); a basename collision would let
        # `_build_present` — or the consuming skill — silently operate on the
        # wrong checkout. `git worktree list --porcelain` paths are absolute and
        # never repeat, so this is collision-free by construction.
        "id": "primary" if is_primary else "wt:" + path,
        "kind": "primary" if is_primary else "worktree",
        "path": path,
        "branch": branch,
        "detached": entry.get("detached", False),
        "bare": entry.get("bare", False),
        "uncommitted": False,
        "uncommitted_count": 0,
        "ahead": 0,
        "behind": 0,
        "last_commit_ts": 0,
        "activity_ts": 0,
        "open_pr": None,
    }
    if rec["bare"]:
        return rec

    dirty = _dirty_paths(path)
    rec["uncommitted_count"] = len(dirty)
    rec["uncommitted"] = bool(dirty)

    if baseref:
        rec["ahead"] = _git_int(path, "rev-list", "--count", f"{baseref}..HEAD")
        rec["behind"] = _git_int(path, "rev-list", "--count", f"HEAD..{baseref}")

    rec["last_commit_ts"] = _git_int(path, "log", "-1", "--format=%ct", "HEAD")
    rec["activity_ts"] = rec["last_commit_ts"]
    if rec["uncommitted"]:
        rec["activity_ts"] = max(rec["activity_ts"], _dirty_mtime(path, dirty))

    if branch and branch in open_by_branch:
        rec["open_pr"] = open_by_branch[branch]
    return rec


def _is_base_branch(rec, base):
    return rec["branch"] == base


# ── "is one of these candidates THIS session's own checkout?" ────────────────
def _match_candidate_by_path(candidates, target_path):
    """The SINGLE candidate whose worktree IS ``target_path`` (realpath +
    case-insensitive compare, so a symlinked /tmp and a case-insensitive macOS path
    still match), or None when zero or several do."""
    if not target_path:
        return None
    try:
        real = os.path.realpath(target_path)
    except Exception:
        return None
    matches = [c for c in candidates
               if c.get("path")
               and os.path.realpath(c["path"]).lower() == real.lower()]
    return matches[0] if len(matches) == 1 else None


def _caller_match_id(candidates, caller_cwd, base):
    """Id of the single candidate that IS the caller's own checkout.

    ``caller_cwd`` is the calling session's literal working directory (any directory
    inside a checkout — the skill passes ``$PWD``, not a resolved toplevel), so it is
    resolved to its git toplevel before comparing. Returns None when:

      * ``caller_cwd`` is unset, missing, or not inside a git work tree (the skill
        resolved the repo from elsewhere — no session claim, so ask as before);
      * zero or several candidates match; or
      * the match is the PRIMARY checkout sitting on the BASE branch — work parked on
        main is ambient, not task-scoped, so the operator is still asked while other
        candidates exist. (A worktree, or a primary on a FEATURE branch, IS
        task-scoped and does auto-win. A sole candidate is auto-selected regardless,
        via ``single`` mode, which is decided before this is consulted.)
    """
    if not caller_cwd or not os.path.isdir(caller_cwd):
        return None
    top = _run_git(caller_cwd, "rev-parse", "--show-toplevel")
    if not top:
        return None
    match = _match_candidate_by_path(candidates, top)
    if match is None:
        return None
    if match["kind"] == "primary" and match.get("branch") == base:
        return None
    return match["id"]


def _session_match_id(candidates, session_id, base):
    """Id of the candidate this SESSION has been working in, resolved from the
    session→worktree registry (written automatically by the git-guardrail hook on
    ``git worktree add`` / ``git -C <worktree>``).

    This closes the gap ``_caller_match_id`` cannot: the agent worked in a fresh
    worktree, but its shell ``$PWD`` never left the spawn checkout, so ``$PWD`` names
    a clean sibling and the picker would ask a question whose answer is unambiguous.
    The recorded worktree IS the answer.

    None when there is no record, the record names a path that is not a LIVE git
    worktree (a phantom mis-resolution or a removed checkout), or the record is not
    among the current candidates (e.g. its PR already merged). Applies the SAME
    primary-on-base exception as ``_caller_match_id``, so a future change to what the
    hook records can never silently subvert that rule."""
    if not session_id:
        return None
    try:
        recorded = session_worktrees.lookup(session_id)
    except Exception:
        return None
    if not _is_live_worktree(recorded):
        return None
    top = _run_git(recorded, "rev-parse", "--show-toplevel") or recorded
    match = _match_candidate_by_path(candidates, top)
    if match is None:
        return None
    if match["kind"] == "primary" and match.get("branch") == base:
        return None
    return match["id"]


# ── candidacy + operator-facing labels ───────────────────────────────────────
def _label_create(rec, base):
    branch = rec["branch"] or "(detached)"
    head = (f"{base} (primary checkout)"
            if (rec["kind"] == "primary" and _is_base_branch(rec, base))
            else branch)
    bits = []
    if rec["ahead"]:
        bits.append(f"{rec['ahead']} commit" + ("s" if rec["ahead"] != 1 else "")
                    + " ahead")
    if rec["uncommitted"]:
        bits.append(f"{rec['uncommitted_count']} uncommitted file"
                    + ("s" if rec["uncommitted_count"] != 1 else ""))
    if rec["behind"]:
        bits.append(f"{rec['behind']} behind {base}")
    detail = " · ".join(bits) if bits else "work present"
    return head, detail


def _label_review(rec):
    pr = rec["open_pr"] or {}
    num = pr.get("number")
    label = f"PR #{num}: {_truncate(pr.get('title'), 38)}" if num else rec["branch"]
    where = rec["path"] if rec["path"] else "not checked out in any worktree"
    upd = pr.get("updatedAt", "")
    detail = f"{rec['branch']} · updated {upd[:10] if upd else '?'} · {where}"
    return label, detail


def _candidates_create(records, base, prd_branches):
    """Checkouts with actionable NEW-PR work: uncommitted changes (never yet PR'd by
    definition) OR commits ahead of base on a branch that was never turned into a PR.
    The ``prd_branches`` guard drops squash-merged / closed branches that read as
    "ahead" forever but are already shipped. A branch with an OPEN PR is review-pr
    territory, not a new-PR candidate. Newest activity first."""
    out = []
    for rec in records:
        if rec["bare"] or rec["detached"]:
            continue
        if rec["open_pr"] is not None:
            continue
        actionable = rec["uncommitted"] or (
            rec["ahead"] > 0 and rec["branch"] not in prd_branches)
        if not actionable:
            continue
        cand = dict(rec)
        cand["is_base_branch"] = _is_base_branch(rec, base)
        cand["label"], cand["detail"] = _label_create(rec, base)
        out.append(cand)
    out.sort(key=lambda c: c["activity_ts"], reverse=True)
    return out


def _candidates_review(records, prs, repo):
    """Every OPEN PR, annotated with the worktree it is checked out in — or
    ``path: null`` / ``kind: "pr-only"`` when it is not checked out anywhere. Ranked
    by the PR's ``updatedAt`` (an ISO string sorts correctly), then last-commit
    time.

    A PR is matched to a local worktree by branch name ONLY when
    :func:`_pr_head_is_local` confirms the PR's head lives in ``repo`` — a
    fork PR sharing a local branch's name by coincidence must never be treated
    as checked out there (that worktree holds unrelated commits on a
    different remote, so fixes would land on the wrong branch)."""
    by_branch_record = {r["branch"]: r for r in records if r["branch"]}
    out = []
    for pr in prs:
        if not isinstance(pr, dict) or not pr.get("number"):
            continue
        branch = pr.get("headRefName", "")
        rec = by_branch_record.get(branch) if _pr_head_is_local(pr, repo) else None
        if rec is not None:
            cand = dict(rec)
        else:
            cand = {
                "id": f"pr:{pr['number']}", "kind": "pr-only", "path": None,
                "branch": branch, "detached": False, "bare": False,
                "uncommitted": False, "uncommitted_count": 0,
                "ahead": 0, "behind": 0, "last_commit_ts": 0, "activity_ts": 0,
            }
        cand["open_pr"] = pr
        cand["id"] = f"pr:{pr['number']}"
        cand["is_base_branch"] = False
        cand["_rank"] = pr.get("updatedAt") or ""
        cand["label"], cand["detail"] = _label_review(cand)
        out.append(cand)
    out.sort(key=lambda c: (c.get("_rank", ""), c["activity_ts"]), reverse=True)
    for cand in out:
        cand.pop("_rank", None)
    return out


# ── the presentation contract ────────────────────────────────────────────────
def _opt(value, label, detail):
    return {"value": value, "label": label, "detail": detail}


def _build_present(candidates, command, base, caller_id=None):
    """Encode the operator-facing option set — the contract the skill renders.

      * 0 candidates → no ask, nothing to do.
      * 1 candidate  → no ask, auto-select it.
      * caller match → no ask, auto-select the caller's OWN checkout (``caller_id``,
                       already filtered by ``_caller_match_id`` / ``_session_match_id``
                       — which withheld a primary-on-base match, so main-parked work
                       competing with worktrees still asks).
      * exactly 2    → ask: [A] / [B] / All.
      * 3 or more    → ask: [most-recent worktree] / [the base checkout] / All, plus
                       the skill's built-in free-text "Other" (the 4th option). For
                       review-pr (which has no "main" concept) the second slot is the
                       2nd-most-recent PR.
    """
    n = len(candidates)
    if n == 0:
        return {"ask": False, "mode": "none", "auto_target": None,
                "free_input": False, "options": []}
    if n == 1:
        cand = candidates[0]
        return {"ask": False, "mode": "single", "auto_target": cand["id"],
                "free_input": False,
                "options": [_opt(cand["id"], cand["label"], cand["detail"])]}

    caller = next((c for c in candidates if c["id"] == caller_id), None)
    if caller is not None:
        return {"ask": False, "mode": "caller", "auto_target": caller["id"],
                "free_input": False,
                "options": [_opt(caller["id"], caller["label"], caller["detail"])]}

    all_opt = _opt("all", f"All ({n})",
                   "Run on every candidate, one after another.")
    if n == 2:
        first, second = candidates[0], candidates[1]
        return {"ask": True, "mode": "two", "auto_target": None,
                "free_input": False,
                "options": [_opt(first["id"], first["label"], first["detail"]),
                            _opt(second["id"], second["label"], second["detail"]),
                            all_opt]}

    # n >= 3
    if command == "open-pr":
        base_cand = next((c for c in candidates
                          if c["kind"] == "primary" and c.get("is_base_branch")),
                         None)
        non_base = [c for c in candidates if c is not base_cand]
        opt1 = non_base[0] if non_base else candidates[0]
        opt2 = base_cand if base_cand is not None else (
            non_base[1] if len(non_base) > 1 else None)
    else:  # review-pr
        opt1, opt2 = candidates[0], candidates[1]

    opts = [_opt(opt1["id"], opt1["label"], opt1["detail"])]
    if opt2 is not None:
        opts.append(_opt(opt2["id"], opt2["label"], opt2["detail"]))
    opts.append(all_opt)
    return {"ask": True, "mode": "many", "auto_target": None,
            "free_input": True, "options": opts}


def build_report(cwd, repo, command, base=None, caller_cwd=None, session_id=None):
    """Enumerate candidates for ``command`` ("open-pr" | "review-pr") and return the
    full report dict (the JSON contract the skills consume):

        {command, repo, base, base_resolved, caller_cwd, caller_match,
         session_match, candidate_count, candidates[], present{}}

    ``caller_cwd`` (optional) is the calling session's own working directory; when it
    identifies exactly one candidate the ask is skipped. ``session_id`` (optional) is
    the calling session id: when ``caller_cwd`` names no candidate (the agent worked
    in a fresh worktree while ``$PWD`` stayed at the spawn checkout), the
    session→worktree registry resolves the worktree the session actually worked in and
    that candidate is auto-selected on the SAME path — so the loop opens on the
    worked-in worktree WITHOUT asking.

    Raises RuntimeError when the cwd is not a git repo or the PR list cannot be read —
    a truncated candidate set would hide a checkout from the operator."""
    if _run_git(cwd, "rev-parse", "--is-inside-work-tree") is None:
        raise RuntimeError(f"Not a git repository: {cwd}")
    base = base or detect_base(cwd)
    baseref = resolve_baseref(cwd, base)

    # open-pr needs ALL PRs (to exclude branches already turned into a PR, including
    # squash-merged ones, which stay "ahead of base" forever); review-pr needs only the
    # open set. One gh call per command either way.
    if command == "open-pr":
        # No `origin` remote at all means this repo has never been pushed — the
        # canonical brand-new-repo case ``open_pr._prepare_on_base`` handles via
        # `gh repo create`. There is no remote to have any PRs against yet, so treat
        # the list as empty instead of shelling out to `gh pr list` for a repo that
        # (from THIS checkout's perspective) doesn't have a known remote home — an
        # unconditional fetch here would otherwise raise and block the new-repo
        # creation path before the actuator ever runs.
        if _run_git(cwd, "remote", "get-url", "origin") is None:
            all_prs = []
        else:
            all_prs = fetch_prs(repo, "all")
            if all_prs is None:
                raise RuntimeError(f"Failed to fetch PRs from GitHub for {repo}")
        open_list = [p for p in all_prs
                     if isinstance(p, dict)
                     and str(p.get("state", "OPEN")).upper() == "OPEN"]
        # Only a PR whose head is IN this repo can retire a local branch by name —
        # a fork PR reusing a local branch's name is a coincidence, not a signal
        # that the local branch was already turned into a PR (_pr_head_is_local).
        prd_branches = {p["headRefName"] for p in all_prs
                        if isinstance(p, dict) and p.get("headRefName")
                        and _pr_head_is_local(p, repo)}
    elif command == "review-pr":
        open_list = fetch_prs(repo, "open")
        if open_list is None:
            raise RuntimeError(f"Failed to fetch open PRs from GitHub for {repo}")
        prd_branches = set()
    else:
        raise ValueError(f"unknown command {command!r}")

    # Same qualification for the open-PR-by-branch map that marks a checkout as
    # "already has an open PR" (introspect's rec["open_pr"]) — an unqualified match
    # would let an open fork PR hide actionable local work from open-pr (the
    # "if rec['open_pr'] is not None: continue" guard in _candidates_create).
    open_by_branch = {p["headRefName"]: p for p in open_list
                      if isinstance(p, dict) and p.get("headRefName")
                      and _pr_head_is_local(p, repo)}

    records = [
        introspect(entry, baseref, open_by_branch, is_primary=idx == 0)
        for idx, entry in enumerate(list_worktrees(cwd))
    ]

    if command == "open-pr":
        candidates = _candidates_create(records, base, prd_branches)
    else:
        candidates = _candidates_review(records, open_list, repo)

    # Only attempt caller/session matching with 2+ candidates: present.mode can never
    # become "caller" with 0/1 candidates (_build_present returns "none"/"single"
    # before the caller_id is consulted), so the git subprocess would be pointless.
    # The registry is the FALLBACK for when caller_cwd names no candidate — both
    # answer "this session's own checkout", so they feed the SAME auto-select path;
    # caller_cwd wins when both resolve (it is the live, authoritative cwd).
    caller_id = None
    session_match = None
    if len(candidates) >= 2:
        caller_id = _caller_match_id(candidates, caller_cwd, base)
        if caller_id is None:
            session_match = _session_match_id(candidates, session_id, base)
            caller_id = session_match

    return {
        "command": command,
        "repo": repo,
        "base": base,
        "base_resolved": baseref,
        "caller_cwd": caller_cwd,
        "caller_match": caller_id,
        "session_match": session_match,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "present": _build_present(candidates, command, base, caller_id=caller_id),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────
def _emit(obj):
    json.dump(obj, sys.stdout)
    sys.stdout.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="buddhi_review.worktree_target")
    sub = parser.add_subparsers(dest="cmd")
    r = sub.add_parser("resolve", help="print the checkout to open the PR from")
    r.add_argument("--session-id", default="")
    r.add_argument("--repo", default="")
    r.add_argument("--cwd", required=True)

    ls = sub.add_parser("list", help="enumerate actionable candidate checkouts (JSON)")
    ls.add_argument("--cwd", required=True, help="primary checkout path")
    ls.add_argument("--repo", required=True, help="owner/repo")
    ls.add_argument("--command", required=True, choices=["open-pr", "review-pr"])
    ls.add_argument("--base", default=None, help="base branch override")
    ls.add_argument("--caller-cwd", default=None,
                    help="the directory the calling session is actually working in "
                         "(the skill passes $PWD); when exactly one candidate is that "
                         "checkout it is auto-selected (mode 'caller') instead of asking")
    ls.add_argument("--session-id", default=None,
                    help="the calling Claude Code session id (the skill passes "
                         "$CLAUDE_CODE_SESSION_ID); when --caller-cwd names no "
                         "candidate, the session→worktree registry resolves the worktree "
                         "this session worked in and auto-selects it (also mode 'caller')")

    args = parser.parse_args(argv)
    if args.cmd == "resolve":
        # Always print a usable path so the skill's $(...) is safe; the cwd is the
        # fail-open fallback for any trouble.
        try:
            sys.stdout.write(resolve(args.session_id, args.repo, args.cwd) or args.cwd)
        except Exception:
            sys.stdout.write(args.cwd)
        sys.stdout.write("\n")
        return 0
    if args.cmd == "list":
        # Unlike `resolve`, this verb fails LOUDLY: a truncated candidate set would
        # hide a checkout from the operator, which is worse than no answer at all.
        try:
            _emit(build_report(args.cwd, args.repo, args.command, args.base,
                               caller_cwd=args.caller_cwd,
                               session_id=args.session_id))
            return 0
        except Exception as exc:
            _emit({"status": "error", "detail": str(exc)})
            return 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
