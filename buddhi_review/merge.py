"""Opt-in squash-merge on clean exit — with a ``⚙ [auto]`` transparency notice.

The squash-merge is the opt-in action; it never rebases, force-pushes, or
re-checks CI on your behalf. Disabled is the default; every path announces
itself via :func:`buddhi_review.transparency.automation_notice`.

When the repo opts into **label-gated CI** (``config.label_gated_ci``), CI is
deferred to a ``ready-for-ci`` label instead of running on every push. The
round driver calls :func:`wait_for_ci_green` at clean exit, BEFORE the merge:
it attaches the label (creating it if absent, retrying a transient label-add
blip), then polls ``gh pr checks`` to a GREEN verdict and only then lets the
merge proceed. A red, never-settling, or absent check set blocks the merge (the
loop hands the PR back) — the gate never false-greens on an ABSENT rollup (a
non-empty all-skipped rollup DOES count as green — CI ran, every job was
intentionally skipped), and it is bounded so it can never deadlock.

On a repo WITHOUT label-gated CI, :func:`check_pr_mergeable` is the general
pre-merge gate: it asks GitHub whether the PR is mergeable (conflicts / draft /
behind / branch-protection / failing or pending checks) before the merge, and
:func:`wait_for_ci_settle` waits out an in-flight check on the last fix push.
"""
from __future__ import annotations

import json
import re
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
# The `ready-for-ci` label add is one `gh pr edit` call and can hit a transient
# GitHub blip; retry it a few times before declaring the gate un-attachable (a
# failed attach means the label-gated workflow never fires, which blocks merge).
# Only the ADD retries — the `gh label create` bootstrap stays one best-effort call.
LABEL_ADD_ATTEMPTS = 3
LABEL_ADD_BACKOFF_S = 2.0

# `gh pr checks --json … bucket` collapses each check to one of five buckets;
# the raw `state` is the fallback when a row omits the bucket. A NON-EMPTY rollup
# with nothing failing and nothing still settling is GREEN — even when every row
# is SKIPPED (CI ran and every job was intentionally skipped by path filters /
# conditional matrices; that must not wedge the merge forever). An ABSENT / empty
# rollup is NOT green — it means the checks never registered, so the gate keeps
# waiting (then times out and blocks) rather than merging blind.
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
    * ``green``   — a NON-EMPTY rollup with nothing failing and nothing pending,
      i.e. every real check has resolved to pass OR skip. An all-SKIPPED rollup is
      green (CI ran, every job was intentionally skipped — it must not wedge the
      merge forever).
    * ``pending`` — anything else: a check still running, OR an empty/absent
      rollup (checks not registered yet). NEVER green on absent checks, so the
      gate can never merge code CI never actually ran on."""
    if not rows:
        return "pending"  # no checks registered yet — never treat absence as green
    saw_pass = False
    saw_skip = False
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
            saw_skip = True  # skipped: a real, resolved check — counts toward "CI ran"
        else:
            saw_pending = True  # pending bucket, unknown bucket, or empty state
    if saw_pending:
        return "pending"
    # Non-empty (guarded above), nothing failing, nothing pending → GREEN, even if
    # every row was skipped. A rollup that yielded no recognizable check row at all
    # (e.g. only malformed entries) stays pending — we green only on a real pass or
    # a real skip.
    return "green" if (saw_pass or saw_skip) else "pending"


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
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = LABEL_ADD_ATTEMPTS,
    backoff_s: float = LABEL_ADD_BACKOFF_S,
) -> bool:
    """Attach the ``ready-for-ci`` label so the label-gated CI workflow fires.
    Self-bootstrapping: creates the label (neutral gray) first — ``gh label
    create`` exits non-zero when it already exists (the steady state), which is
    ignored; the add is authoritative. Idempotent (``--add-label`` is a no-op
    when already present). The label ADD is retried up to ``attempts`` times with
    a linear backoff (``backoff_s * attempt`` between failed tries) so a transient
    GitHub blip does not wrongly block the merge; the ``gh label create`` bootstrap
    stays a single best-effort call. Returns True iff an add succeeded."""
    create = ["gh", "label", "create", "ready-for-ci", "--color", "cccccc"]
    edit = ["gh", "pr", "edit", str(pr), "--add-label", "ready-for-ci"]
    if repo:
        create += ["-R", repo]
        edit += ["-R", repo]
    try:
        run(create, cwd=cwd)  # already-exists exit is fine; the add below is authoritative
    except (subprocess.SubprocessError, OSError):
        pass  # create failure is non-fatal — the add surfaces a real problem
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        try:
            proc = run(edit, cwd=cwd)
        except (subprocess.SubprocessError, OSError):
            proc = None
        if proc is not None and getattr(proc, "returncode", 1) == 0:
            return True
        if attempt < attempts:
            sleep(backoff_s * attempt)  # transient blip — linear backoff, then retry
    return False


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
    can never deadlock, an absent rollup is treated as pending (never green), and a
    non-empty all-skipped rollup is treated as green (CI ran; every job intentionally
    skipped). ``run`` / ``sleep`` are injectable so the round driver can drive it on
    a fake clock."""
    if not _attach_ready_for_ci(pr, repo=repo, cwd=cwd, run=run, sleep=sleep):
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


# ── General pre-merge mergeability gate (the NON-label-CI path) ─────────────────
# GitHub check-rollup conclusions, bucketed. A CheckRun carries `conclusion` (or
# null + `status` while running); a StatusContext carries `state`. SKIPPED is in
# NEITHER set — a skipped check neither fails nor blocks (consistent with the
# all-skipped-is-green rule above).
_ROLLUP_FAIL = {"FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED",
                "CANCELLED", "STARTUP_FAILURE", "STALE"}
_ROLLUP_PENDING = {"PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "WAITING", "REQUESTED"}
_PENDING_REASON_PREFIX = "checks still pending"


def _inspect_rollup(rollup: Optional[Sequence[dict]]) -> "tuple[List[str], int]":
    """Bucket ``statusCheckRollup`` rows into (failing_names, pending_count).
    A non-required check that is FAILING or IN_PROGRESS keeps ``mergeStateStatus``
    CLEAN/UNSTABLE, so the rollup is inspected directly rather than trusting the
    state. Skipped checks fall through (they neither fail nor block)."""
    failing: List[str] = []
    pending = 0
    for c in rollup or []:
        if not isinstance(c, dict):
            continue
        conc = (c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
        if conc in _ROLLUP_FAIL:
            failing.append(c.get("name") or c.get("context") or "?")
        elif conc in _ROLLUP_PENDING:
            pending += 1
    return failing, pending


def reason_is_pending_checks(reason: str) -> bool:
    """True iff a :func:`check_pr_mergeable` reason denotes only in-flight checks
    (as opposed to a terminal block like a conflict). The caller waits those out
    via :func:`wait_for_ci_settle` rather than handing the PR back immediately."""
    return bool(reason) and reason.startswith(_PENDING_REASON_PREFIX)


_GIT_URL_OWNER_REPO_RE = re.compile(r"(?:^|[:/])([^/:]+/[^/:]+?)(?:\.git)?/?$")
_GIT_URL_HOST_RE = re.compile(
    r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*://)?(?:[^@/]+@)?([^/:]+)[:/][^/:]+/[^/:]+?(?:\.git)?/?$"
)


def _owner_repo(text: str) -> Optional[str]:
    """Normalise a git remote URL (SSH or HTTPS, ``.git`` suffix optional) or a
    ``gh``-style ``[HOST/]OWNER/REPO`` string down to a lowercase ``owner/repo``
    for comparison, or ``None`` if it does not look like one. The host, if any,
    is dropped here — use :func:`_host` alongside this when host disambiguation
    matters (see :func:`_remote_for_repo`)."""
    m = _GIT_URL_OWNER_REPO_RE.search(text.strip())
    return m.group(1).lower() if m else None


def _host(text: str) -> Optional[str]:
    """Extract the lowercase host from a git remote URL (SSH or HTTPS) or a
    ``gh``-style ``HOST/OWNER/REPO`` string, or ``None`` when the input has no
    host component (e.g. a bare ``OWNER/REPO``, which ``gh``'s ``[HOST/]``
    makes optional)."""
    m = _GIT_URL_HOST_RE.match(text.strip())
    return m.group(1).lower() if m else None


def _remote_for_repo(
    repo: str,
    *,
    cwd: Optional[str],
    run: Callable[..., "subprocess.CompletedProcess[str]"],
) -> Optional[str]:
    """Find the local remote (if any) whose URL resolves to the same
    ``owner/repo`` as ``repo`` (the ``-R``/``--repo`` target that
    :func:`check_pr_mergeable` queried) by matching ``git remote -v`` output.
    This is the AUTHORITATIVE base-remote signal: ``repo`` is the exact repo the
    PR's base branch lives on, so a match here is correct regardless of what
    ``branch.<base>.remote`` says (or whether it is even set). Returns ``None``
    on any git error or no match, so the caller can fall back.

    When ``repo`` carries an explicit host (``gh``'s ``[HOST/]OWNER/REPO``
    form, e.g. from a GitHub Enterprise checkout), the matched remote's URL
    must resolve to that SAME host — otherwise a checkout with remotes for the
    same ``owner/repo`` on two different hosts (``github.com`` and an
    Enterprise host) could match the wrong one. A host-less ``repo`` (the
    common bare ``owner/repo`` case) keeps matching by ``owner/repo`` alone,
    same as before."""
    target = _owner_repo(repo)
    if not target:
        return None
    target_host = _host(repo)
    try:
        r = run(["git", "remote", "-v"], cwd=cwd)
    except (subprocess.SubprocessError, OSError):
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    for line in (getattr(r, "stdout", "") or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[2] != "(fetch)":
            continue
        name, url = parts[0], parts[1]
        if _owner_repo(url) != target:
            continue
        if target_host and _host(url) != target_host:
            continue
        return name
    return None


def _base_remote(
    base_branch: str,
    *,
    cwd: Optional[str],
    run: Callable[..., "subprocess.CompletedProcess[str]"],
    repo: Optional[str] = None,
) -> str:
    """Resolve the remote that hosts ``base_branch``.

    ``repo`` (the exact ``owner/repo`` the PR's base lives on, passed through
    from :func:`check_pr_mergeable`) is tried FIRST via :func:`_remote_for_repo`
    — matching a configured remote's URL is authoritative, unlike guessing from
    local branch config. When ``repo`` is absent or matches no configured
    remote, this falls back to ``branch.<base>.remote`` (the same lookup
    :func:`buddhi_review.commit_push.exit_rebase` uses), and finally to
    ``origin``. The fallback chain still exists for callers that cannot supply
    ``repo`` (e.g. a bare checkout with no PR context), but in a fork checkout
    where the PR base is ``upstream/main``, the ``repo`` match now identifies
    ``upstream`` directly instead of guessing ``origin`` and risking a compare
    against the contributor's own stale fork copy of the base branch."""
    if repo:
        matched = _remote_for_repo(repo, cwd=cwd, run=run)
        if matched:
            return matched
    try:
        r = run(["git", "config", "--get", f"branch.{base_branch}.remote"], cwd=cwd)
        val = (getattr(r, "stdout", "") or "").strip()
        if getattr(r, "returncode", 1) == 0 and val:
            return val
    except (subprocess.SubprocessError, OSError):
        pass
    return "origin"


def _branch_is_behind_base(
    base_branch: str,
    *,
    cwd: Optional[str] = None,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = _default_run,
    repo: Optional[str] = None,
) -> bool:
    """True iff the checkout at ``cwd`` (the PR branch) is behind the base
    branch's remote tracking ref by >= 1 commit, decided by GIT independently of
    GitHub's ``mergeStateStatus``. The remote is resolved by :func:`_base_remote`
    — matched against ``repo`` (the exact base repo, when the caller has it)
    first, then ``branch.<base_branch>.remote``, then ``origin`` — so a fork
    worktree whose PR base is ``upstream/main`` is checked against ``upstream``
    rather than the fork's ``origin``.

    ``mergeStateStatus`` is single-valued: when a PR is BOTH behind its base AND
    has a failing check, GitHub reports UNSTABLE/BLOCKED (the red check), which
    MASKS BEHIND. :func:`check_pr_mergeable` would then report "checks failing"
    and the branch would never reach the exit-rebase path even though a rebase
    onto the fixed base can clear the red. This git behind-count is the
    CI-independent signal.

    Fail-safe: ANY git error (fetch/rev-list non-zero, missing git, timeout,
    non-int output) returns False — never claim a drift we could not verify, so a
    transient failure never triggers a blind force-push."""
    try:
        base_remote = _base_remote(base_branch, cwd=cwd, run=run, repo=repo)
        fetched = run(["git", "fetch", base_remote, base_branch], cwd=cwd)
        if getattr(fetched, "returncode", 1) != 0:
            return False
        counted = run(
            ["git", "rev-list", "--count", f"HEAD..{base_remote}/{base_branch}"], cwd=cwd)
        if getattr(counted, "returncode", 1) != 0:
            return False
        return int((getattr(counted, "stdout", "") or "0").strip() or "0") > 0
    except (subprocess.SubprocessError, OSError, ValueError):
        return False


def check_pr_mergeable(
    pr: str,
    *,
    repo: Optional[str] = None,
    cwd: Optional[str] = None,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = _default_run,
) -> "tuple[bool, str]":
    """Best-effort pre-merge safety gate: ask GitHub whether the PR is mergeable
    before firing ``gh pr merge``. Returns ``(ok, reason)`` — ``ok=True`` means
    it is safe to proceed; ``ok=False`` means GitHub would refuse the merge and
    the loop should hand the PR back (``reason`` names why).

    Blocks on: a draft PR, merge conflicts (``mergeable=CONFLICTING`` /
    ``mergeStateStatus=DIRTY``), a base branch that is ahead (``BEHIND`` — needs a
    rebase), branch protection (``BLOCKED``), any FAILING rollup check, and any
    still-PENDING rollup check. Fail-SOFT: any ``gh`` error / non-zero exit /
    unparseable output returns ``(True, "")`` — a transient blip must never wedge
    a mergeable PR, and ``gh pr merge`` stays the authoritative final check."""
    cmd = ["gh", "pr", "view", str(pr), "--json",
           "mergeable,mergeStateStatus,statusCheckRollup,isDraft,baseRefName"]
    if repo:
        cmd += ["-R", repo]
    try:
        proc = run(cmd, cwd=cwd)
    except (subprocess.SubprocessError, OSError):
        return True, ""
    if getattr(proc, "returncode", 1) != 0:
        return True, ""
    try:
        data = json.loads((getattr(proc, "stdout", "") or "{}"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return True, ""
    if not isinstance(data, dict):
        return True, ""
    mergeable = (data.get("mergeable") or "").upper()
    state = (data.get("mergeStateStatus") or "").upper()
    base = (data.get("baseRefName") or "").strip()
    if data.get("isDraft"):
        return False, "the PR is still a draft"
    if mergeable == "CONFLICTING" or state == "DIRTY":
        return False, "merge conflicts"
    if state == "BEHIND":
        return False, "base branch ahead — needs rebase/update"
    failing, pending = _inspect_rollup(data.get("statusCheckRollup") or [])
    if failing:
        joined = " | ".join(failing[:3])
        tail = f" (+{len(failing) - 3} more)" if len(failing) > 3 else ""
        # A failing check masks BEHIND in the single-valued mergeStateStatus, so a
        # PR that is BOTH behind base AND red reports "checks failing" and never
        # reaches the exit-rebase path — even though rebasing onto the fixed base
        # may clear the red. Detect behind independently (git) and route it to the
        # SAME drift reason as the BEHIND state above, so the branch is rebased
        # first; a genuine failure survives the rebase and hands back next pass.
        if base and _branch_is_behind_base(base, cwd=cwd, run=run, repo=repo):
            return False, "base branch ahead — needs rebase/update"
        return False, f"checks failing: {joined}{tail}"
    if pending:
        return False, f"{_PENDING_REASON_PREFIX} ({pending})"
    if state == "BLOCKED":
        return False, "blocked by branch protection (reviews or required checks)"
    return True, ""


def wait_for_ci_settle(
    pr: str,
    *,
    repo: Optional[str] = None,
    cwd: Optional[str] = None,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = _default_run,
    notice: Callable[..., str] = automation_notice,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = _CI_POLL_ATTEMPTS,
    interval: float = _CI_POLL_INTERVAL,
) -> bool:
    """Non-label pre-merge CI wait: after the last fix push the checks may still be
    in flight, so poll ``_ci_verdict(_fetch_pr_checks(...))`` until it settles.
    Returns True on ``green`` (safe to merge), False on ``red`` or on a poll that
    never settles (hand the PR back). Bounded by ``attempts`` (same 30×30s ceiling
    as the label path) so it can never deadlock; ``run``/``sleep`` are injectable
    for a fake clock."""
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        verdict = _ci_verdict(_fetch_pr_checks(pr, repo, cwd, run))
        if verdict == "green":
            notice("premerge-ci", f"PR #{pr} CI settled green — proceeding to merge",
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
    run: Callable[..., "subprocess.CompletedProcess[str]"] = _default_run,
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
