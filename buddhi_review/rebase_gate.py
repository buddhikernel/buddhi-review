"""Free rebase-gate engine verb: ``python -m buddhi_review rebase-check``.

Reports whether the current branch is behind its base and (if so) whether a
rebase would be clean — check-only, NEVER mutates the working tree.

status ∈ ``up-to-date`` | ``clean`` | ``conflicts`` | ``dirty`` | ``error``

The free-path guidance text tells the operator the manual rebase steps (the
prose that used to live in ``open-pr/SKILL.md`` §2 now comes from the engine
so the skill text can be tier-neutral).

When an active backend exposes ``run_rebase(cwd, base)`` the verb MAY
delegate the actual rebase action to it (paid capability hook — resolved via
``getattr``, never a Protocol change, so a backend without the method is
silently treated as free-tier).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ── subprocess seam ────────────────────────────────────────────────────────────

def _default_run(argv: Sequence[str], *, cwd: Optional[str] = None,
                 timeout: int = 60):
    return subprocess.run(list(argv), cwd=cwd, capture_output=True,
                          text=True, timeout=timeout)


def _rc(r: Any) -> int:
    return getattr(r, "returncode", 1)


def _stdout(r: Any) -> str:
    return (getattr(r, "stdout", "") or "").strip()


def _stderr(r: Any) -> str:
    return (getattr(r, "stderr", "") or "").strip()


def _git(cwd: str, *args: str, run=_default_run, timeout: int = 60) -> Any:
    return run(["git", "-C", cwd, *args], cwd=None, timeout=timeout)


# ── git helpers ────────────────────────────────────────────────────────────────

def _count_commits(cwd: str, rev_range: str, run: Any) -> Optional[int]:
    """rev-list --count, distinguishing command FAILURE (None) from a true 0.

    A transient git error never silently reads as "behind 0 / up-to-date"."""
    r = _git(cwd, "rev-list", "--count", rev_range, run=run)
    if _rc(r) != 0:
        return None
    val = _stdout(r)
    return int(val) if val.isdigit() else None


def _resolve_baseref(cwd: str, base: str, remote: str,
                     run: Any) -> Optional[str]:
    """Resolve ``origin/<base>`` to the ref the commit-counting can use.

    Prefers the remote-tracking ref (refreshed by fetch); falls back to a
    plain local ref of the same name."""
    for ref in (f"refs/remotes/{remote}/{base}", f"{remote}/{base}"):
        r = _git(cwd, "rev-parse", "--verify", "--quiet", ref, run=run)
        if _rc(r) == 0 and _stdout(r):
            return ref
    r = _git(cwd, "rev-parse", "--verify", "--quiet", base, run=run)
    if _rc(r) == 0 and _stdout(r):
        return base
    return None


def _merge_tree_clean(cwd: str, baseref: str,
                      run: Any) -> Tuple[str, List[str]]:
    """Best-effort conflict prediction WITHOUT touching the working tree.

    Returns (status, conflict_files) where status ∈ clean | conflicts | unknown.

    Prefers ``git merge-tree --write-tree`` (git ≥ 2.38); falls back to the
    legacy 3-arg form. KNOWN FALSE-POSITIVE: merge-tree three-way-merges the
    two FINAL trees whereas a real rebase replays each commit as a patch, so a
    branch whose intermediate commit edits a base-changed line and a later
    commit reverts it nets to a clean final tree (reported 'clean' here) yet
    conflicts on replay. Accepted: ``do_rebase`` (paid tier) is the truth
    source; this is operator-facing context only."""
    r = run(["git", "merge-tree", "--write-tree", baseref, "HEAD"], cwd=cwd)
    blob = (_stdout(r) + "\n" + _stderr(r)).lower()
    if _rc(r) == 0:
        return "clean", []
    if _rc(r) == 1 and "unknown option" not in blob and "usage:" not in blob:
        # ``--write-tree`` conflict output: "Conflicted file info" lines are
        # ``<mode> <object> <stage>\t<path>`` — the path follows the first tab.
        files = [line.split("\t", 1)[1].strip()
                 for line in _stdout(r).splitlines() if "\t" in line]
        return "conflicts", sorted({f for f in files if f})
    # Old git without --write-tree → legacy 3-arg form.
    mb_r = _git(cwd, "merge-base", baseref, "HEAD", run=run)
    if _rc(mb_r) != 0 or not _stdout(mb_r):
        return "unknown", []
    r2 = run(["git", "merge-tree", _stdout(mb_r), baseref, "HEAD"], cwd=cwd)
    if _rc(r2) != 0:
        return "unknown", []
    return ("conflicts", []) if "<<<<<<<" in (_stdout(r2) + _stderr(r2)) else ("clean", [])


# ── The free rebase-check ──────────────────────────────────────────────────────

def rebase_check(cwd: str, base: str, *, fetch: bool = True,
                 run: Any = _default_run) -> Dict[str, Any]:
    """Read-only: report whether <cwd>'s HEAD is based on the latest remote/<base>.

    status ∈ up-to-date | clean | conflicts | dirty | error.

    A dirty working tree is reported as ``dirty`` (a rebase would fail); the
    behind/ahead counts are still populated when determinable. A failed fetch
    or rev-list yields ``error`` (never a false ``up-to-date``): the freshness
    guarantee cannot be met against a possibly-stale local ref, so the skill
    should ask rather than trust it."""
    if not os.path.isdir(cwd):
        return {"status": "error", "detail": f"cwd does not exist: {cwd}"}
    if _rc(_git(cwd, "rev-parse", "--is-inside-work-tree", run=run)) != 0:
        return {"status": "error", "detail": "not a git work tree"}

    # Dirty-tree probe (porcelain: non-empty → uncommitted changes present).
    dirty_r = _git(cwd, "status", "--porcelain", run=run)
    dirty = _rc(dirty_r) == 0 and bool(_stdout(dirty_r))

    # Resolve the configured remote for this branch.
    branch_r = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD", run=run)
    branch = _stdout(branch_r) if _rc(branch_r) == 0 else ""
    has_branch = bool(branch) and branch != "HEAD"
    remote = "origin"
    if has_branch:
        cfg_r = _git(cwd, "config", f"branch.{branch}.remote", run=run)
        if _rc(cfg_r) == 0 and _stdout(cfg_r):
            remote = _stdout(cfg_r)

    if fetch:
        fr = run(["git", "fetch", remote, base], cwd=cwd)
        if _rc(fr) != 0:
            out: Dict[str, Any] = {
                "status": "error", "base": base, "base_resolved": None,
                "behind": None, "ahead": None, "conflict_files": [],
                "fetch_failed": True,
                "detail": (f"git fetch {remote} {base} failed; cannot verify "
                           f"base freshness: {_stderr(fr)[:200]}"),
            }
            if dirty:
                out["dirty"] = True
            return out

    baseref = _resolve_baseref(cwd, base, remote, run)
    if not baseref:
        return {"status": "error", "base": base, "base_resolved": None,
                "detail": f"could not resolve base ref for {base!r}"}

    behind = _count_commits(cwd, f"HEAD..{baseref}", run)
    ahead = _count_commits(cwd, f"{baseref}..HEAD", run)

    if behind is None:
        result: Dict[str, Any] = {
            "status": "error", "base": base, "base_resolved": baseref,
            "behind": None, "ahead": ahead, "conflict_files": [],
            "detail": f"could not count commits vs {baseref}",
        }
        if dirty:
            result["dirty"] = True
        return result

    out2: Dict[str, Any] = {
        "base": base, "base_resolved": baseref,
        "behind": behind, "ahead": ahead, "conflict_files": [],
    }

    if dirty:
        out2["status"] = "dirty"
        out2["dirty"] = True
        out2["detail"] = (
            "uncommitted changes present; commit or stash them before rebasing."
            + (f" Branch is {behind} commit(s) behind {baseref}."
               if behind > 0 else ""))
        return out2

    if behind == 0:
        out2["status"] = "up-to-date"
        return out2

    tree_status, files = _merge_tree_clean(cwd, baseref, run)
    # Normalise unknown → clean (conservative: offer the user the manual rebase
    # steps, and let the real rebase be the truth source about conflicts).
    if tree_status == "unknown":
        tree_status = "clean"
    out2["status"] = tree_status
    out2["conflict_files"] = files
    out2["detail"] = (
        f"{behind} commit(s) behind {baseref}; "
        + {"clean": "rebase looks clean.",
           "conflicts": "rebase would conflict."}[tree_status])
    return out2


# ── Guidance text (SKILL.md prose moved into engine output) ───────────────────

_MANUAL_STEPS = """\
  1. Commit or stash any pending work:
       git stash --include-untracked   # or: git add -A && git commit -m "wip"
  2. Rebase onto the latest base:
       git rebase origin/{base}
  3. If conflicts arise, resolve them, then run:
       git rebase --continue
  4. Push the rebased branch:
       git push --force-with-lease"""


def guidance_text(result: Dict[str, Any]) -> str:
    """Human-readable guidance based on the rebase_check result.

    This is the text the SKILL.md gate used to carry as prose; the engine now
    owns it so the skill text can be tier-neutral and reference this verb."""
    status = result.get("status", "error")
    base = result.get("base", "main")
    baseref = result.get("base_resolved") or f"origin/{base}"
    behind = result.get("behind")

    if status == "up-to-date":
        return f"Branch is up-to-date with {baseref}. No rebase needed."

    if status == "dirty":
        behind_msg = (f" (also {behind} commit(s) behind {baseref})"
                      if behind else "")
        return (f"Uncommitted changes present{behind_msg}; "
                "commit or stash them before rebasing:\n"
                f"  git stash --include-untracked\nThen rebase:\n"
                f"  git rebase origin/{base}")

    if status == "clean":
        return (f"Branch is {behind} commit(s) behind {baseref}; "
                "rebase looks clean.\nTo rebase manually:\n"
                + _MANUAL_STEPS.format(base=base))

    if status == "conflicts":
        files = result.get("conflict_files", [])
        file_list = ("\nExpected conflict files:\n  " + "\n  ".join(files)
                     if files else "")
        return (f"Branch is {behind} commit(s) behind {baseref}; "
                f"rebase would conflict.{file_list}\n"
                "Resolve conflicts manually after:\n"
                + _MANUAL_STEPS.format(base=base))

    # error
    detail = result.get("detail", "")
    return (f"Could not determine rebase status. {detail}\n"
            f"Check manually: git status && git log {baseref}..HEAD --oneline")


# ── Paid capability hook ───────────────────────────────────────────────────────

def _delegate_to_backend(backend: Any, cwd: str,
                         base: str) -> Optional[Dict[str, Any]]:
    """Delegate to the active backend's ``run_rebase`` if it exposes one.

    Never a Protocol change — a backend without ``run_rebase`` is silently
    treated as free-tier. Returns the backend's result dict, or None when the
    backend does not offer the capability or the call fails."""
    fn = getattr(backend, "run_rebase", None)
    if fn is None or not callable(fn):
        return None
    try:
        return dict(fn(cwd, base))
    except Exception:
        return None


# ── CLI entry point ────────────────────────────────────────────────────────────

def run_check_verb(
    cwd: str,
    base: str,
    *,
    fetch: bool = True,
    run: Any = _default_run,
    backend: Any = None,
    out: Any = None,
    json_only: bool = False,
) -> int:
    """The ``rebase-check`` subcommand body.

    Free default: run :func:`rebase_check`, print JSON, then print guidance.
    When an active backend exposes ``run_rebase(cwd, base)``, the verb
    delegates the actual rebase action to it instead."""
    out = out or sys.stdout

    # Capability hook: if the active backend can rebase, delegate to it.
    if backend is not None:
        backend_result = _delegate_to_backend(backend, cwd, base)
        if backend_result is not None:
            print(json.dumps(backend_result), file=out)
            # Backend-driven results: 0 on success, 1 on any failure state.
            return 0 if backend_result.get("status") in (
                "rebased", "up-to-date", "current") else 1

    # Free path: check + guidance only (never mutates).
    result = rebase_check(cwd, base, fetch=fetch, run=run)
    print(json.dumps(result), file=out)

    if not json_only:
        print("", file=out)
        print(guidance_text(result), file=out)

    status = result.get("status", "error")
    # 0 for any valid check result (up-to-date/clean/conflicts/dirty); the
    # caller reads the JSON to decide whether action is needed. 1 only for
    # "error" (check itself failed — we do not know the rebase state).
    return 0 if status != "error" else 1
