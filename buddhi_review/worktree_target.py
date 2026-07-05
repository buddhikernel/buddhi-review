#!/usr/bin/env python3
"""worktree_target.py — resolve which checkout the /open-pr + /review-pr skills
should open the PR from, given the calling session.

WHY THIS EXISTS. The skills derive the checkout from the calling session's
``$PWD`` (``git rev-parse --show-toplevel``). When the agent follows the standing
"do your work in a NEW worktree off main" rule, the session is SPAWNED in checkout
A but creates and operates on worktree B via ``git -C B`` — its shell ``$PWD``
never leaves A. ``session_worktrees`` (written automatically by the git-guardrail
hook) knows the session actually worked in B; this module reads that record and
returns B, so the skill opens the PR from the worktree the session worked in
WITHOUT asking — even when ``$PWD`` is elsewhere.

SAFETY. The recorded path is used ONLY when it (a) is a LIVE git worktree — it
exists on disk AND ``git rev-parse --is-inside-work-tree`` succeeds, (b) has an
``origin`` remote resolving to the TARGET repo (same full ``owner/…/repo`` path,
and — when both the record and the target carry a host — the same host, so a
same-slug repo on a different host / GitHub Enterprise never matches), and (c)
differs from the cwd checkout. Any failure of those falls back to the cwd checkout
(today's behaviour) — so a stale / removed record, a phantom mis-resolution, or a
worktree belonging to a different repo can never drive the loop into the wrong
place.

NOTHING here ever raises to its caller. The CLI always prints a usable path (the
cwd on any trouble), so the skill can consume it with ``$(...)`` unconditionally.

Pure stdlib (os / re / sys / subprocess / argparse) plus ``session_worktrees``.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

from buddhi_review import session_worktrees


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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="buddhi_review.worktree_target")
    sub = parser.add_subparsers(dest="cmd")
    r = sub.add_parser("resolve", help="print the checkout to open the PR from")
    r.add_argument("--session-id", default="")
    r.add_argument("--repo", default="")
    r.add_argument("--cwd", required=True)
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
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
