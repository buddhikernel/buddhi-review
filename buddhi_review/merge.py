"""Opt-in squash-merge on clean exit — with a ``⚙ [auto]`` transparency notice.

The squash-merge is the opt-in action; it never rebases, force-pushes, or
re-checks CI on your behalf. Disabled is the default; every path announces
itself via :func:`buddhi_review.transparency.automation_notice`.

When the repo opts into **label-gated CI** (``config.label_gated_ci``), CI is
deferred to a ``ready-for-ci`` label instead of running on every push. The
round driver calls :func:`wait_for_ci_green` at clean exit, BEFORE the merge:
it attaches the label (creating it if absent), then polls ``gh pr checks`` to a
GREEN verdict and only then lets the merge proceed. A red, never-settling, or
absent check set blocks the merge (the loop hands the PR back) — the gate never
false-greens on an empty / all-skipped rollup, and it is bounded so it can never
deadlock.
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Callable, List, Optional, Sequence

from buddhi_review.transparency import automation_notice

_GH_TIMEOUT = 120


def _default_run(argv: Sequence[str], *, cwd: Optional[str] = None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=_GH_TIMEOUT,
        stdin=subprocess.DEVNULL, cwd=cwd,
    )


# ── Label-gated CI (the pre-merge `ready-for-ci` gate) ──────────────────────────
# Mirrors the reference loop's pre-merge CI gate, scaled down to one OSS function:
# attach the `ready-for-ci` label so the label-gated workflow fires, then poll
# `gh pr checks` to green before letting the merge proceed.
_CI_SETTLE_SECS = 15.0     # let GitHub register the label-triggered check run
_CI_POLL_ATTEMPTS = 30     # 30 × 30s ≈ 15 min ceiling (bounded — never deadlocks)
_CI_POLL_INTERVAL = 30.0

# `gh pr checks --json … bucket` collapses each check to one of five buckets;
# the raw `state` is the fallback when a row omits the bucket. A check is only
# GREEN when at least one check PASSED and nothing is failing or still settling —
# an empty / all-skipped rollup is NOT green (it means CI never actually ran), so
# the gate keeps waiting (then times out and blocks) rather than merging blind.
_FAIL_BUCKETS = {"fail", "cancel"}
_PASS_BUCKETS = {"pass"}
_SKIP_BUCKETS = {"skipping"}
_FAIL_STATES = {"failure", "timed_out", "cancelled", "action_required",
                "startup_failure", "error", "stale"}
_PASS_STATES = {"success", "neutral"}
_SKIP_STATES = {"skipped"}


def _ci_verdict(rows: Optional[List[dict]]) -> str:
    """Collapse one ``gh pr checks`` poll into ``red`` / ``green`` / ``pending``.

    * ``red``     — any check is failing/cancelled/errored (fail FAST, even while
      others are still running).
    * ``green``   — at least one check PASSED and none is failing or pending
      (skipped checks are neutral — they neither block nor satisfy the floor).
    * ``pending`` — anything else: a check still running, an all-skipped set, OR
      an empty/absent rollup (checks not registered yet). NEVER green on absent
      checks, so the gate can never merge code CI never actually ran on."""
    if not rows:
        return "pending"  # no checks registered yet — never treat absence as green
    saw_pass = False
    saw_pending = False
    for r in rows:
        if not isinstance(r, dict):
            continue
        bucket = (r.get("bucket") or "").lower()
        state = (r.get("state") or "").lower()
        if bucket in _FAIL_BUCKETS or state in _FAIL_STATES:
            return "red"
        if bucket in _PASS_BUCKETS or (not bucket and state in _PASS_STATES):
            saw_pass = True
        elif bucket in _SKIP_BUCKETS or (not bucket and state in _SKIP_STATES):
            continue  # skipped: neutral — does not satisfy the "any check" floor
        else:
            saw_pending = True  # pending bucket, unknown bucket, or empty state
    if saw_pending:
        return "pending"
    return "green" if saw_pass else "pending"  # all-skipped ⇒ keep waiting, not green


def _fetch_pr_checks(
    pr: str, repo: Optional[str], cwd: Optional[str],
    run: Callable[..., "subprocess.CompletedProcess[str]"],
) -> List[dict]:
    """Fetch ``gh pr checks --json name,state,bucket`` rows. ``gh`` exits non-zero
    when checks are pending or failing (and when none are reported), so the
    return code is NOT consulted — the rows are parsed from stdout. Any error /
    empty / unparseable output → ``[]`` (read by the caller as 'still pending')."""
    cmd = ["gh", "pr", "checks", str(pr), "--json", "name,state,bucket"]
    if repo:
        cmd += ["-R", repo]
    try:
        proc = run(cmd, cwd=cwd)
    except (subprocess.SubprocessError, OSError):
        return []
    raw = (getattr(proc, "stdout", "") or "").strip()
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _attach_ready_for_ci(
    pr: str, *, repo: Optional[str], cwd: Optional[str],
    run: Callable[..., "subprocess.CompletedProcess[str]"],
) -> bool:
    """Attach the ``ready-for-ci`` label so the label-gated CI workflow fires.
    Self-bootstrapping: creates the label (neutral gray) first — ``gh label
    create`` exits non-zero when it already exists (the steady state), which is
    ignored; the add is authoritative. Idempotent (``--add-label`` is a no-op
    when already present). Returns True iff the add succeeded."""
    create = ["gh", "label", "create", "ready-for-ci", "--color", "cccccc"]
    edit = ["gh", "pr", "edit", str(pr), "--add-label", "ready-for-ci"]
    if repo:
        create += ["-R", repo]
        edit += ["-R", repo]
    try:
        run(create, cwd=cwd)  # already-exists exit is fine; the add below is authoritative
    except (subprocess.SubprocessError, OSError):
        pass  # create failure is non-fatal — the add surfaces a real problem
    try:
        proc = run(edit, cwd=cwd)
    except (subprocess.SubprocessError, OSError):
        return False
    return getattr(proc, "returncode", 1) == 0


def wait_for_ci_green(
    pr: str,
    *,
    repo: Optional[str] = None,
    cwd: Optional[str] = None,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = _default_run,
    notice: Callable[..., str] = automation_notice,
    sleep: Callable[[float], None] = time.sleep,
    settle_secs: float = _CI_SETTLE_SECS,
    attempts: int = _CI_POLL_ATTEMPTS,
    interval: float = _CI_POLL_INTERVAL,
) -> bool:
    """Pre-merge label-gated CI gate: attach ``ready-for-ci`` + poll CI to green.

    Returns True ONLY when CI is GREEN (the caller may merge); False on a failed
    label attach, a red verdict, or a poll that never settles (the caller blocks
    the merge and hands the PR back). The poll is bounded by ``attempts`` so it
    can never deadlock, and an absent / all-skipped rollup is treated as pending
    (never green), so it can never merge a PR CI did not actually run on. ``run``
    / ``sleep`` are injectable so the round driver can drive it on a fake clock."""
    if not _attach_ready_for_ci(pr, repo=repo, cwd=cwd, run=run):
        notice("premerge-ci",
               f"could not attach `ready-for-ci` label to PR #{pr} — label-gated "
               f"CI would not fire; not merging", status="stop",
               hint="check `gh` auth / repo permissions, or disable label-gated CI")
        return False
    notice("premerge-ci",
           f"label-gated CI: attached `ready-for-ci` to PR #{pr}, polling "
           f"`gh pr checks` to green before merge", status="do",
           hint="disable: label_gated_ci off in config / the setup wizard")
    if settle_secs > 0:
        sleep(settle_secs)
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        verdict = _ci_verdict(_fetch_pr_checks(pr, repo, cwd, run))
        if verdict == "green":
            notice("premerge-ci", f"PR #{pr} CI is green — proceeding to merge",
                   status="done")
            return True
        if verdict == "red":
            notice("premerge-ci",
                   f"PR #{pr} CI is red — not merging; handing back for a human",
                   status="stop", hint="fix the failing checks, then re-run")
            return False
        if attempt < attempts:  # pending → wait and re-poll
            sleep(interval)
    notice("premerge-ci",
           f"PR #{pr} CI did not settle (still pending after {attempts} polls) — "
           f"not merging; handing back", status="stop",
           hint="check the PR's checks on GitHub, then re-run")
    return False


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
