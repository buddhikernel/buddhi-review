"""Opt-in squash-merge on clean exit — with a ``⚙ [auto]`` transparency notice.

The squash-merge is the opt-in action; it never rebases, force-pushes, or
re-checks CI on your behalf. Disabled is the default; every path announces
itself via :func:`buddhi_review.transparency.automation_notice`.
"""
from __future__ import annotations

import subprocess
from typing import Callable, Optional, Sequence

from buddhi_review.transparency import automation_notice

_GH_TIMEOUT = 120


def _default_run(argv: Sequence[str], *, cwd: Optional[str] = None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=_GH_TIMEOUT,
        stdin=subprocess.DEVNULL, cwd=cwd,
    )


def squash_merge(
    pr: str,
    *,
    repo: Optional[str] = None,
    enabled: bool = False,
    cwd: Optional[str] = None,
    run: Callable[[Sequence[str], Optional[str]], "subprocess.CompletedProcess[str]"] = _default_run,
    notice: Callable[..., str] = automation_notice,
) -> bool:
    """Squash-merge ``pr`` + delete its branch, iff opted in. Returns True only
    on a completed merge. Never rebases, never force-pushes — a dirty/behind PR
    simply fails the merge and is reported as a fallback."""
    if not enabled:
        notice(
            "squash-merge", f"PR #{pr} left open",
            status="skip", hint="enable: --auto-merge",
        )
        return False
    # Visual break — the squash-merge / landing is its own block, separated from
    # the round loop's clean-exit line. Bare blank line, not a notice, so
    # the auto-action trail stays exactly do → done.
    print(flush=True)
    notice("squash-merge", f"landing PR #{pr} — squash-merge + delete branch", status="do",
           hint="disable: --no-auto-merge")
    argv = ["gh", "pr", "merge", str(pr), "--squash", "--delete-branch"]
    if repo:
        argv += ["-R", repo]
    try:
        proc = run(argv, cwd=cwd)
    except (subprocess.TimeoutExpired, OSError) as exc:
        notice("squash-merge", f"merge failed: {exc}", status="fallback")
        return False
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:200]
        notice("squash-merge", f"merge failed: {detail}", status="fallback")
        return False
    notice("squash-merge", f"PR #{pr} landed — squash-merged, branch deleted", status="done")
    return True
