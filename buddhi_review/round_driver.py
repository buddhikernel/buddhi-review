"""The quiescence polling round-loop + the re-request exclusion wiring.

One round = summon/re-request the expected reviewers → hold the round open
until **every expected bot has quiesced** → classify + kernel-decide + act on
the round's new comments → commit/push the applied fixes → next round, until a
clean round or ``max_rounds``.

**Quiescence.** A bot is done for the round once it posts a definitive
single-shot signal (clean / quota-exhausted / PR-too-large / errored) OR has
been silent for ``BUDDHI_BOT_QUIESCENCE_SECS`` since its LAST contribution —
the timer resets on every contribution from the same bot, so the window slides
with a bursting bot. A bot that never contributes is not declared silent-done
before ``MIN_BOT_WAIT``. Hard outer bounds: ``IDLE_TIMEOUT`` (no activity from
anyone) and ``BUDDHI_MAX_WAIT_TOTAL`` (per-round ceiling). An empty-body review
never promotes a bot to no-issues (empty bodies are dropped at ingest).

**Exclusion.** Three independent cause-buckets ride
``ReviewStore``: quota and PR-too-large are permanent; errored is transient and
retractable — a bot whose comment is **strictly newer** than its recorded error
signal comes back (an unparseable/equal/missing timestamp keeps it excluded,
conservatively). Every summon / poll / merge gate subtracts the derived union.
``--rr`` / ``--rr-active`` never clear any bucket; they only widen the round-1
summon set (``--rr``) or exit clean when nothing is active (``--rr-active``).

Clock, sleep, the comment fetch, and the ``gh`` runner are all injectable —
the test suite drives rounds with a fake clock and never sleeps or touches the
network.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import textwrap
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from buddhi_review import commit_push, detectors, escalation_wait, gh_ingest, merge
from buddhi_review.actuators import ActionResult, FixDispatch, act_on_result
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.config import (
    active_reviewers, auto_on_open, has_global_default, label_gated_ci,
    load_config, repo_entry,
)
from buddhi_review.loop import Comment, CommentResult, process_comments
from buddhi_review.transparency import _colour_enabled, automation_notice


def _env_int(name: str, default: int, floor: int = 1) -> int:
    try:
        return max(floor, int(os.environ.get(name, "")))
    except (TypeError, ValueError):
        return default


def _env_trigger(name: str, default: str) -> str:
    v = os.environ.get(name, "").strip()
    return v or default


# Round budget that didn't measure the diff falls back here; a non-finite /
# overflowing size maps to a high defensive backstop instead of crashing.
MAX_ROUNDS_FALLBACK = 10
_MAX_ROUNDS_BACKSTOP = 100


def pick_max_rounds(lines) -> int:
    """Diff size (added + deleted lines) → the review→fix round budget.

    +1 round per doubling of diff size, with a floor of 2 (one fix round + one
    confirmation round) and NO upper cap — a larger change earns proportionally
    more rounds rather than being squeezed into a fixed ceiling. Growth is
    logarithmic (a one-million-line diff still maps to only ~17 rounds), so the
    budget stays sane while scaling honestly with the work.

    Closed form: ``max(2, floor(log2(lines / 25)) + 2)``. ``lines`` below 25
    (including NaN and negatives) → 2; a non-finite or overflowing size → a high
    defensive backstop (unreachable for a real, finite diff)."""
    try:
        if isinstance(lines, float):
            if math.isnan(lines):
                return 2
            if math.isinf(lines):
                return _MAX_ROUNDS_BACKSTOP if lines > 0 else 2
            lines = int(lines)
        else:
            lines = int(lines)

        if lines < 25:
            return 2
        if lines.bit_length() > 1024:
            return _MAX_ROUNDS_BACKSTOP
        return (lines // 25).bit_length() + 1
    except (TypeError, ValueError):
        return 2


def _env_max_rounds() -> Optional[int]:
    """``BUDDHI_MAX_ROUNDS`` as a positive int, or None when unset/invalid.
    None (not a default) lets the resolver tell "override present" apart from
    "no override → auto-size from the diff"."""
    raw = os.environ.get("BUDDHI_MAX_ROUNDS")
    if raw in (None, ""):
        return None
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return None


def resolve_max_rounds(explicit: Optional[int], *, diff_lines: Optional[int] = None) -> int:
    """Resolve the round budget. Order: an explicit value → ``BUDDHI_MAX_ROUNDS``
    env → auto-size from the PR diff (:func:`pick_max_rounds`, uncapped) → the
    fallback used only when the diff size is unknown."""
    if explicit is not None:
        return max(1, explicit)
    env = _env_max_rounds()
    if env is not None:
        return env
    if diff_lines is not None:
        return pick_max_rounds(diff_lines)
    return MAX_ROUNDS_FALLBACK


def _parse_iso(stamp: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 stamp (``…Z`` normalised to ``+00:00``), or None when
    absent/unparseable. None drives the 'unparseable → stay excluded' rule."""
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


def _strictly_newer(new: Optional[str], old: Optional[str]) -> bool:
    """True ONLY when both stamps parse AND ``new`` is strictly after ``old``.
    Equal / missing / unparseable → False (conservative; keeps the bot excluded)
    and avoids the lexicographic mis-ordering of mixed offsets ('Z' vs '+00:00')."""
    n, o = _parse_iso(new), _parse_iso(old)
    if n is None or o is None:
        return False
    try:
        return n > o
    except TypeError:
        return False


@dataclass
class RoundTimes:
    """Round wait bounds — these defaults are the authoritative ones."""
    quiescence: float = float(_env_int("BUDDHI_BOT_QUIESCENCE_SECS", 60))
    poll_interval: float = 30.0
    min_bot_wait: float = 420.0
    idle_timeout: float = 900.0
    max_wait_total: float = float(_env_int("BUDDHI_MAX_WAIT_TOTAL", 1800))


# Vendor re-request triggers — env-seamed so a vendor slug rename is config,
# not a source edit. Copilot is re-requested via the review-request API; the
# other three are comment-triggered.
COPILOT_REVIEWER_SLUG = _env_trigger("BUDDHI_TRIGGER_COPILOT", "copilot-pull-request-reviewer[bot]")
TRIGGER_COMMENTS: Dict[str, str] = {
    "gemini": _env_trigger("BUDDHI_TRIGGER_GEMINI", "/gemini review"),
    "codex": _env_trigger("BUDDHI_TRIGGER_CODEX", "@codex review"),
    "claude": _env_trigger("BUDDHI_TRIGGER_CLAUDE", "@claude review"),
}

# The display name the bundled claude-code-review.yml publishes as a PR check
# (line 1: `name: Claude Code Review`). The auth-failure probe resolves the
# Claude run id from `gh pr checks` by matching this name.
CLAUDE_REVIEW_CHECK_NAME = "Claude Code Review"


@dataclass
class BotState:
    last_seen: Optional[float] = None       # driver-clock stamp of the last contribution
    signal: Optional[str] = None            # clean | quota | pr-too-large | errored
    error_created_at: Optional[str] = None  # ISO stamp of the errored signal (comeback rule)


@dataclass
class RunOutcome:
    status: str                 # clean | max-rounds | stopped | needs-human
    rounds: int = 0
    merged: bool = False
    actions: List[ActionResult] = field(default_factory=list)
    # rebase_skip: this hand-back is a dirty/diverged/unverifiable state that must
    #   NEVER be rebased + force-pushed on the way out — a poisoned worktree, a
    #   failed push (a local commit the remote never got), or a red-gate stop with
    #   uncommitted/unpushed residue. Set ONLY at those exits; the manual-landing
    #   exit-rebase honours it and skips entirely (commit_push.exit_rebase + its
    #   own clean-worktree guard are the second line of defence).
    rebase_skip: bool = False


# The §5 parity vocabulary: a run ends in exactly one of three terminal
# dispositions, and parity is graded against this 3-way (NOT the richer internal
# RunOutcome.status). The collapse keys ONLY off the public RunOutcome fields:
#   • merge             — status "clean" AND merged: the loop merged the PR itself
#                         (clean exit + auto-merge opted in).
#   • stop              — status "stopped": the OPERATOR chose to halt the run (a
#                         "Stop" answer to a failed-fix escalation, or a stop
#                         signalled mid-push).
#   • escalate-to-human — everything else: the loop handed the PR BACK for a human
#                         to act. This bucket spans an unanswered business question,
#                         a failed fix awaiting a human, a safety halt (push error /
#                         poisoned worktree — both surface as "needs-human", which
#                         is indistinguishable from an unanswered escalation by the
#                         public fields, so it lands here, not in "stop"),
#                         max-rounds exhaustion, and a clean review the operator
#                         must merge themselves (auto-merge off).
def run_terminal_disposition(outcome: Optional[RunOutcome]) -> str:
    """Collapse a :class:`RunOutcome` into the §5 parity 3-way
    (``merge`` / ``escalate-to-human`` / ``stop``). See the table above for the
    mapping rationale; this is the single source the verdict-parity suite grades
    a run's terminal disposition against."""
    if outcome is None:
        return "escalate-to-human"
    if outcome.status == "clean" and outcome.merged:
        return "merge"
    if outcome.status == "stopped":
        return "stop"
    # needs-human, max-rounds, or a clean-but-unmerged exit — all hand back to a
    # human (answer a question, take over a failed fix, or merge it yourself).
    return "escalate-to-human"


# ---------------------------------------------------------------- console render

# The single display order for every user-facing reviewer list (the "expecting"
# line, the round-summary table): Claude → Copilot → Codex → Gemini. This is a
# DISPLAY order only — it never changes which reviewers run or in what order they
# are summoned.
REVIEWER_ORDER: Tuple[str, ...] = ("claude", "copilot", "codex", "gemini")
_REVIEWER_LABEL: Dict[str, str] = {
    "claude": "Claude", "copilot": "Copilot", "codex": "Codex", "gemini": "Gemini",
}


def _canonical(bots: Iterable[str]) -> List[str]:
    """Order ``bots`` by :data:`REVIEWER_ORDER`; any reviewer outside that list
    keeps its given order, after the known ones. Also DEDUPLICATES (each reviewer
    appears at most once), so a config that lists a reviewer twice never
    double-renders a summary row or double-logs a skip."""
    bots = list(bots)
    known = [b for b in REVIEWER_ORDER if b in bots]
    seen = set(known)
    extra = []
    for b in bots:
        if b not in seen:
            seen.add(b)
            extra.append(b)
    return known + extra


# Classification label → the round-summary table column it tallies under.
_LABEL_COL: Dict[str, str] = {
    "SUBSTANTIVE": "sub",
    "COSMETIC": "cosm",
    "PR_DESCRIPTION": "prdesc",
    "OUTDATED": "outd",
    "INVALID": "inval",
    "BUSINESS_QUESTION": "biz",
    "CLASSIFICATION_FAILED": "fail",
}

# (header, display width) per column, left to right.
_TABLE_COLS: Tuple[Tuple[str, int], ...] = (
    ("Bot", 9), ("Posted", 6), ("Subst", 6), ("Cosm", 5), ("PR-d", 5),
    ("Outd", 5), ("Inval", 6), ("Biz", 4), ("Fail", 5), ("Status", 21),
)
_TABLE_COUNT_KEYS: Tuple[str, ...] = (
    "posted", "sub", "cosm", "prdesc", "outd", "inval", "biz", "fail",
)

# Why a reviewer is not in a round's expected set, keyed by a stable reason code.
# The long form is the honest skip-log line; the short form is the table cell.
_SKIP_LONG: Dict[str, str] = {
    "done": "voluntarily done (LGTM)",
    "quota": "quota exhausted",
    "pr-too-large": "PR too large",
    "errored": "errored (retractable on a newer comment)",
    "excluded": "excluded",
}
_STATUS_SHORT: Dict[str, str] = {
    "done": "done",
    "quota": "quota",
    "pr-too-large": "PR too large",
    "errored": "errored",
    "excluded": "excluded",
}


def _strip_cell_emoji(s: str) -> str:
    """Drop colour-emoji glyphs (plus variation selectors and ZWJ) from a table
    cell. A box-drawing table aligns by monospace cell, but a colour emoji renders
    at an inconsistent advance across terminals — one cell here, two there — which
    pushes the right border out of true. Stripping the decorative emoji leaves
    pure narrow text so the rectangle stays exact. CJK (legitimately two cells) is
    kept; only emoji ranges are removed."""
    out = []
    for ch in s:
        o = ord(ch)
        if (0x1F000 <= o <= 0x1FAFF        # pictographic supplementary plane
                or 0x2600 <= o <= 0x27BF    # misc symbols + dingbats
                or 0x2300 <= o <= 0x23FF    # technical (hourglass / watch / …)
                or 0x2B00 <= o <= 0x2BFF    # arrows / stars
                or 0x1F1E6 <= o <= 0x1F1FF  # regional indicators (flags)
                or o in (0xFE0F, 0xFE0E, 0x200D)):  # VS16 / VS15 / ZWJ
            continue
        out.append(ch)
    # Collapse the gap an interior emoji leaves ("a 👍 b" → "a b") and trim the
    # trailing space a end-of-cell emoji leaves; a blank/space-only cell is left
    # untouched.
    return re.sub(r" {2,}", " ", "".join(out)).rstrip() if s.strip() else s


def _display_width(s: str) -> int:
    """Monospace display width: East-Asian wide/fullwidth glyphs count as 2,
    combining marks as 0, everything else as 1."""
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _pad_cell(text: str, width: int) -> str:
    """Left-justify ``text`` to ``width`` display columns. Anything wider is
    truncated (with an ellipsis) so a cell can never push the right border out."""
    text = str(text)
    if _display_width(text) <= width:
        return text + " " * (width - _display_width(text))
    out = ""
    for ch in text:
        if _display_width(out + ch) > width - 1:  # leave one column for the ellipsis
            break
        out += ch
    out += "…"
    return out + " " * max(0, width - _display_width(out))


def _render_round_table(round_no: int, max_rounds: Optional[int], rows: List[dict]) -> None:
    """Print an aligned per-reviewer round-summary box table to stdout. Each row is
    a dict with ``bot_key``/``label``, the count keys in :data:`_TABLE_COUNT_KEYS`,
    and ``status``. A TOTAL row is appended automatically. Console only."""
    widths = [w for _, w in _TABLE_COLS]
    top = "┌" + "┬".join("─" * (w + 2) for w in widths) + "┐"
    mid = "├" + "┼".join("─" * (w + 2) for w in widths) + "┤"
    bottom = "└" + "┴".join("─" * (w + 2) for w in widths) + "┘"

    def _row(values: Sequence) -> str:
        out = "│"
        for val, w in zip(values, widths):
            out += " " + _pad_cell(_strip_cell_emoji(str(val)), w) + " │"
        return out

    of = f" of {max_rounds}" if max_rounds else ""
    print()
    print(f"Round {round_no}{of} summary")
    print(top)
    print(_row([h for h, _ in _TABLE_COLS]))
    print(mid)
    totals = {k: 0 for k in _TABLE_COUNT_KEYS}
    for r in rows:
        print(_row([r["label"]] + [r.get(k, 0) for k in _TABLE_COUNT_KEYS] + [r.get("status", "")]))
        for k in _TABLE_COUNT_KEYS:
            totals[k] += int(r.get(k, 0))
    print(mid)
    print(_row(["TOTAL"] + [totals[k] for k in _TABLE_COUNT_KEYS] + [""]))
    print(bottom)
    print()


# ----------------------------------------------------------- launch preflight
# Two console gates run BEFORE the round loop (wired from cli._review_pr):
#   • refuse_primary_checkout — never run fixers in the repo's PRIMARY checkout
#     while it sits on the PR branch; any uncommitted fix residue there could
#     strand on the default branch after the PR merges. Require a linked worktree.
#   • enforce_repo_confirmation_gate — reviewer availability is per-repo, so a
#     repo with no confirmed fleet AND no global default to fall back to is
#     refused rather than run on a guessed fleet.
# Both print to the console — no other channel.

# Cross-process refusal contract. When EITHER gate refuses, this exact phrase is
# emitted to stdout (via the gate's automation_notice, captured into the detached
# run-loop's log by launch-review.sh's `>"$LOG" 2>&1`). The launcher's foreground
# liveness-poll greps the log for this literal: a loop that died fast AND carries
# this marker is a startup-gate refusal, so the launcher surfaces a red panel in
# the user's session and exits 2 instead of a false "launched" notice. Keep this
# string in lockstep with the `grep` literal in launch-review.sh — the launcher
# subprocess test (test_launch_refusal_surface.py) imports this constant and
# builds its stub from it so the two can never drift.
REFUSED_TO_LAUNCH_MARKER = "refused to launch"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _print_refusal_banner(title: str, body: str) -> None:
    """A loud red bordered console banner. Honours ``NO_COLOR`` /
    ``BUDDHI_LOOP_NO_COLOR`` and a non-TTY stream (the glyph + text still print,
    uncoloured)."""
    use = _colour_enabled(sys.stdout)
    red = "\033[31m" if use else ""
    bold = "\033[1m" if use else ""
    reset = "\033[0m" if use else ""
    bar = "═" * 74
    print(f"\n{red}{bar}{reset}")
    print(f"{red}{bold}✗ {title}{reset}")
    for line in textwrap.wrap(body, 72):
        print(f"{red}  {line}{reset}")
    print(f"{red}{bar}{reset}\n")


def _git_line(argv: Sequence[str], cwd: Optional[str], run) -> Optional[str]:
    """Run a git/gh command and return the first stripped stdout line, or None on
    any non-zero exit / error / empty output. Never raises."""
    try:
        proc = run(list(argv), cwd=cwd)
    except (subprocess.SubprocessError, OSError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    out = (proc.stdout or "").strip()
    return out.splitlines()[0].strip() if out else None


def _is_primary_checkout(cwd: str, run) -> bool:
    """True only when ``cwd`` is CONFIRMED to be the repository's PRIMARY working
    tree (the first ``git worktree list --porcelain`` entry), not a linked
    worktree. Any uncertainty (git error, not a repo, path mismatch) → False, so
    a probe failure never hard-blocks a legitimate run ("unknown" = not-primary).
    Compares git toplevels, so a ``cwd`` pointing at a SUBDIRECTORY of the primary
    checkout still resolves as primary."""
    try:
        proc = run(["git", "worktree", "list", "--porcelain"], cwd=cwd)
    except (subprocess.SubprocessError, OSError):
        return False
    if getattr(proc, "returncode", 1) != 0:
        return False
    primary = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("worktree "):
            primary = line[len("worktree "):].strip()
            break
    if not primary:
        return False
    toplevel = _git_line(["git", "rev-parse", "--show-toplevel"], cwd, run)
    if not toplevel:
        return False
    return os.path.realpath(toplevel) == os.path.realpath(primary)


def _pr_head_branch(pr, repo, cwd, run) -> Optional[str]:
    """The PR's head branch via ``gh pr view``, or None on any failure."""
    argv = ["gh", "pr", "view", str(pr), "--json", "headRefName",
            "-q", ".headRefName"]
    if repo:
        argv += ["-R", repo]
    return _git_line(argv, cwd, run)


def refuse_primary_checkout(pr, repo, cwd, *, run=None,
                            notice: Callable[..., str] = automation_notice) -> Optional[str]:
    """Refuse to launch when ``cwd`` is the repo's PRIMARY checkout sitting on the
    PR head branch — fixers must run in a dedicated linked worktree, never a
    checkout whose uncommitted residue could strand on the default branch when it
    is switched back after the PR merges. Returns the refusal reason (the caller
    aborts) after printing a loud banner + a ``✗ [auto]`` console notice, or None
    to proceed. Fails OPEN on any uncertainty (git/gh error, detached HEAD,
    branch mismatch) so a legitimate run is never hard-blocked. Bypass:
    ``BUDDHI_ALLOW_PRIMARY_CHECKOUT=1``. Console only."""
    if _env_flag("BUDDHI_ALLOW_PRIMARY_CHECKOUT"):
        return None
    cwd = cwd or os.getcwd()
    run = run or commit_push._default_run
    if not _is_primary_checkout(cwd, run):
        return None
    head = _pr_head_branch(pr, repo, cwd, run)
    current = _git_line(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd, run)
    # Only the "primary checkout ON the PR head branch" case is dangerous; any
    # unknown (no head / detached / branch mismatch) proceeds.
    if not head or not current or head != current:
        return None
    reason = (
        f"Preflight check (a pre-launch safety check) failed — the loop is "
        f"pointed at the PRIMARY checkout ({cwd}), which is sitting "
        f"on the PR head branch {head}. Running fixers there risks leaking an "
        f"uncommitted fix edit onto the default branch after the PR merges. "
        f"Launch from a dedicated per-PR worktree (the create-pr / review-pr "
        f"flow builds one), or set BUDDHI_ALLOW_PRIMARY_CHECKOUT=1 to override.")
    _print_refusal_banner(f"PREFLIGHT — PRIMARY CHECKOUT — {repo or 'this repo'} on {head}", reason)
    notice("primary-checkout gate",
           f"{REFUSED_TO_LAUNCH_MARKER} in the primary checkout on {head} — fixers "
           f"need a dedicated worktree", status="stop",
           hint="run via create-pr / review-pr; bypass: BUDDHI_ALLOW_PRIMARY_CHECKOUT=1")
    return reason


def enforce_repo_confirmation_gate(repo, cfg, *, exit_fn=sys.exit,
                                   notice: Callable[..., str] = automation_notice) -> None:
    # exit_fn is injectable for unit tests (pass a raising stub or a spy); the
    # default sys.exit is intentional — the gate is wired exclusively from
    # cli._review_pr, so process-exit here is the correct outcome.  The module
    # comment above this block explains why both preflight gates live in
    # round_driver (testability, cohesion) rather than in cli.py.
    """Fail-closed unconfirmed-repo gate (console only). Reviewer availability is
    per-repo, so a repo with NO confirmed reviewer fleet AND no global default to
    fall back to is refused (loud banner + ``✗ [auto]`` notice + ``exit_fn(2)``).
    A repo with a global default proceeds on it with a ``⚠ [auto]`` fallback
    notice; a confirmed repo, ``repo=None``, or a probe error is a silent no-op.
    Bypass: ``BUDDHI_ALLOW_UNCONFIRMED_REPO=1`` (runs on the built-in defaults)."""
    allow = _env_flag("BUDDHI_ALLOW_UNCONFIRMED_REPO")
    try:
        unconfirmed = bool(repo) and repo_entry(cfg, repo) is None
        has_default = has_global_default(cfg)
    except Exception:
        return
    if not unconfirmed:
        return
    if not has_default and not allow:
        _print_refusal_banner(
            f"PREFLIGHT — REPO NOT CONFIRMED — {repo}",
            f"Preflight check (a pre-launch safety check) failed — "
            f"{repo} has no confirmed reviewer fleet and no global default to "
            f"fall back to. Reviewers are set up per repo, so the loop will not "
            f"guess a fleet for a repo you have not configured. Confirm reviewers "
            f"for {repo} (run the setup wizard), or set "
            f"BUDDHI_ALLOW_UNCONFIRMED_REPO=1 to run with the built-in defaults.")
        notice("repo-config gate",
               f"{REFUSED_TO_LAUNCH_MARKER} on {repo} — no confirmed reviewer fleet "
               f"and no global default", status="stop",
               hint="confirm reviewers via the setup wizard; "
                    "bypass: BUDDHI_ALLOW_UNCONFIRMED_REPO=1")
        exit_fn(2)
        return  # reached only if exit_fn is a non-raising test double
    # Proceeding on a fallback fleet — a convenience action taken without a
    # per-repo confirm, so record it on the ⚙ [auto] trail.
    fleet = ", ".join(active_reviewers(cfg, repo)) or "none"
    notice("repo-config fallback",
           f"{repo} has no confirmed reviewer fleet — using "
           + ("the global default" if has_default else "the built-in defaults")
           + f" ({fleet})", status="fallback",
           hint="confirm a per-repo fleet via the setup wizard")


class RoundDriver:
    def __init__(
        self,
        pr: str,
        *,
        repo: Optional[str] = None,
        cwd: Optional[str] = None,
        cfg: Optional[dict] = None,
        adapter: Optional[ReviewAdapter] = None,
        classify_runner: Callable[[str], str],
        fix_dispatch: Optional[FixDispatch] = None,
        clean_llm: Optional[Callable[[str], Optional[dict]]] = None,
        quota_llm: Optional[Callable[[str], Optional[dict]]] = None,
        fetch: Optional[Callable[..., List[Comment]]] = None,
        gh_run: Optional[Callable[..., "subprocess.CompletedProcess[str]"]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        notice: Callable[..., str] = automation_notice,
        times: Optional[RoundTimes] = None,
        max_rounds: Optional[int] = None,
        diff_lines: Optional[int] = None,
        auto_merge: bool = False,
        rr: bool = False,
        rr_active: bool = False,
        push: bool = True,
        test_gate: bool = True,
        answer_waiter: Optional[Callable[..., Dict[str, Optional[str]]]] = None,
    ) -> None:
        self.pr = str(pr)
        self.repo = repo
        self.cwd = cwd or os.getcwd()
        self.cfg = cfg if cfg is not None else load_config()
        # An unconfirmed repo (no repos[<repo>] entry) with NO global default to
        # fall back to is running purely on the built-in default fleet — surfaced
        # in the round table as "not configured (repo)" so an idle reviewer there
        # does not read as a neutral "active". The common confirmed / global-
        # default install is NOT flagged. (The launch gate only lets this state
        # through under the BUDDHI_ALLOW_UNCONFIRMED_REPO bypass.)
        self._repo_unconfigured = (
            bool(self.repo)
            and repo_entry(self.cfg, self.repo) is None
            and not has_global_default(self.cfg)
        )
        self.adapter = adapter or ReviewAdapter()
        self.classify_runner = classify_runner
        self.fix_dispatch = fix_dispatch
        self.clean_llm = clean_llm
        # The quota-detector seam (same shape as clean_llm): enables the tier-2
        # quota check for wording the deterministic regex misses. Left None in
        # tests → deterministic-only classification.
        self.quota_llm = quota_llm
        self.fetch = fetch or gh_ingest.fetch_comments
        # commit_push's runner accepts both cwd= and timeout=, so one injected
        # fake covers every gh/git/test spawn the driver makes.
        self.gh_run = gh_run or commit_push._default_run
        self.clock = clock
        self.sleep = sleep
        self.notice = notice
        self.times = times or RoundTimes()
        # Round budget: an explicit value wins; otherwise BUDDHI_MAX_ROUNDS env,
        # then auto-size from the diff (uncapped), then the fallback.
        self.max_rounds = resolve_max_rounds(max_rounds, diff_lines=diff_lines)
        self.auto_merge = auto_merge
        self.rr = rr
        self.rr_active = rr_active
        self.push = push
        self.test_gate = test_gate
        self.answer_waiter = answer_waiter or escalation_wait.wait_for_delivered

        self.store = self.adapter.store
        self.done: Set[str] = set()           # voluntarily-done (clean review)
        self.bots: Dict[str, BotState] = {}
        self.processed_ids: Set[str] = set()
        self.actions: List[ActionResult] = []

        # ── F5 run-cumulative review tracking (the SAFETY gate + silent-reviewer
        # warning) ──────────────────────────────────────────────────────────────
        # reviewed_ever: reviewers that GENUINELY reviewed this run — a clean
        #   approval / "No issues found." sentinel OR an actionable comment that
        #   flowed to the kernel. Accumulated in REAL TIME in _classify_signal.
        #   Quota / PR-too-large / errored placeholders are caught upstream (they
        #   detect as signals and return before the actionable add), so a
        #   placeholder can NEVER land here — it is a response, not a review.
        self.reviewed_ever: Set[str] = set()
        # responded_ever: reviewers that posted ANYTHING in any round (comment,
        #   clean approval, OR a quota/error placeholder). A single response in
        #   any round permanently protects a reviewer from the silent warning.
        self.responded_ever: Set[str] = set()
        # requested_ever: reviewers the loop successfully re-requested at least
        #   once — so silence is the reviewer's, not a failed summon.
        self.requested_ever: Set[str] = set()
        # silent_rounds: per-reviewer count of rounds it was expected yet silent
        #   (drives the "never responded across N rounds" message).
        self.silent_rounds: Dict[str, int] = {}
        # _run_start_fleet: the expected fleet snapshotted at run start — the
        #   non-empty check that distinguishes "no reviewers by design" (quiet)
        #   from "reviewers expected but none reviewed" (loud block).
        self._run_start_fleet: Set[str] = set()
        # _premerge_ci_red: set when the pre-merge label-gated CI gate failed at a
        #   clean exit — used so a manual-landing rebase reports the honest state
        #   ("CI is red — merge at your discretion") instead of "ready to merge".
        self._premerge_ci_red: bool = False

    # ------------------------------------------------------------------ state

    def expected_bots(self) -> List[str]:
        """The expected-bot gate: enabled reviewers minus voluntarily-done minus
        the derived union of the three exclusion buckets."""
        return [
            b for b in active_reviewers(self.cfg, self.repo)
            if b not in self.done and not self.store.is_excluded(b)
        ]

    def _bot_state(self, bot: str) -> BotState:
        return self.bots.setdefault(bot, BotState())

    # ----------------------------------------------------- skip reason + summary

    def _skip_key(self, bot: str) -> Optional[str]:
        """Why ``bot`` is not in this round's expected set → a stable reason code,
        or None when it is still expected (active)."""
        st = self._bot_state(bot)
        if bot in self.done or st.signal == detectors.SIGNAL_CLEAN:
            return "done"
        if st.signal == detectors.SIGNAL_QUOTA:
            return "quota"
        if st.signal == detectors.SIGNAL_PR_TOO_LARGE:
            return "pr-too-large"
        if st.signal == detectors.SIGNAL_ERRORED:
            return "errored"
        if self.store.is_excluded(bot):
            return "excluded"
        return None

    def _log_skipped(self, expected: Sequence[str]) -> None:
        """Honest skip-reason logging: for every enabled reviewer NOT waited on
        this round, say WHY (done / quota / PR-too-large / errored / excluded) so
        a shorter expected set never reads as a silent disappearance."""
        expected_set = set(expected)
        for bot in _canonical(active_reviewers(self.cfg, self.repo)):
            if bot in expected_set:
                continue
            reason = _SKIP_LONG.get(self._skip_key(bot), "not expected")
            print(f"[round] skipping {bot}: {reason}")

    def _bot_status_text(self, bot: str) -> str:
        """The round-summary Status cell for ``bot``."""
        key = self._skip_key(bot)
        if key is None:
            if self._bot_state(bot).last_seen is not None:
                return "reviewed"
            # Idle on a repo running with no confirmed fleet and no global
            # default → say so honestly instead of a neutral "active".
            return "not configured (repo)" if self._repo_unconfigured else "active"
        return _STATUS_SHORT.get(key, key)

    def _round_table_rows(self, actionable: Sequence[Comment],
                          results: Sequence[CommentResult]) -> List[dict]:
        """One summary row per enabled reviewer (canonical order) from this round's
        classified comments + each reviewer's terminal status."""
        counts: Dict[str, Dict[str, int]] = {}
        for c, r in zip(actionable, results):
            bot = detectors.bot_for_login(c.source)
            if bot is None:
                continue
            d = counts.setdefault(bot, {})
            d["posted"] = d.get("posted", 0) + 1
            col = _LABEL_COL.get(r.classification.label)
            if col:
                d[col] = d.get(col, 0) + 1
        rows: List[dict] = []
        for bot in _canonical(active_reviewers(self.cfg, self.repo)):
            d = counts.get(bot, {})
            row = {"bot_key": bot, "label": _REVIEWER_LABEL.get(bot, bot.capitalize()),
                   "status": self._bot_status_text(bot)}
            for k in _TABLE_COUNT_KEYS:
                row[k] = d.get(k, 0)
            rows.append(row)
        return rows

    def _render_round(self, round_no: int, actionable: Sequence[Comment],
                      results: Sequence[CommentResult]) -> None:
        _render_round_table(round_no, self.max_rounds,
                             self._round_table_rows(actionable, results))

    # ------------------------------------------------------------- re-request

    def _summon(self, round_no: int, expected: Sequence[str]) -> None:
        """Round 1 summons only ``auto_on_open: false`` reviewers (the others
        already review on PR open), resolved PER-REPO from the loop's bound repo
        so a reviewer's auto-review setting can differ across repos. ``--rr`` and
        ``--rr-active`` both widen round 1 to re-request the whole expected set —
        bots don't re-review an existing PR spontaneously, so the flag's
        re-request half must actually fire (``--rr-active`` additionally exits
        clean when nothing is active, handled in ``run``). Rounds ≥2 re-request
        every still-expected bot."""
        if round_no == 1:
            targets = [
                b for b in expected
                if self.rr or self.rr_active or not auto_on_open(self.cfg, b, self.repo)
            ]
        else:
            targets = list(expected)
        for bot in targets:
            self._request_review(bot)

    def _request_review(self, bot: str) -> None:
        if bot == "copilot":
            argv = [
                "gh", "api", "-X", "POST",
                f"repos/{self.repo or '{owner}/{repo}'}/pulls/{self.pr}/requested_reviewers",
                "-f", f"reviewers[]={COPILOT_REVIEWER_SLUG}",
            ]
        else:
            trigger = TRIGGER_COMMENTS.get(bot)
            if not trigger:
                return
            argv = ["gh", "pr", "comment", self.pr, "--body", trigger]
            if self.repo:
                argv += ["-R", self.repo]
        try:
            proc = self.gh_run(argv, cwd=self.cwd)
            if proc.returncode != 0:
                self.notice("re-request", f"{bot} re-request failed: "
                            f"{(proc.stderr or '').strip()[:120]}", status="fallback")
            else:
                # The summon landed — so a later silence is the reviewer's, not a
                # failed request (never flag a bot we could not actually summon).
                self.requested_ever.add(bot)
        except (subprocess.SubprocessError, OSError) as exc:
            self.notice("re-request", f"{bot} re-request failed: {exc}", status="fallback")

    # ------------------------------------------------------------- quiescence

    def _quiesced(self, bot: str, now: float, round_start: float) -> bool:
        st = self._bot_state(bot)
        if st.signal is not None or bot in self.done or self.store.is_excluded(bot):
            return True
        if st.last_seen is not None:
            return (now - st.last_seen) >= self.times.quiescence
        return (now - round_start) >= self.times.min_bot_wait

    def _ingest_new(self) -> List[Comment]:
        comments = self.fetch(self.pr, repo=self.repo, cwd=self.cwd)
        fresh = [c for c in comments if c.id not in self.processed_ids]
        for c in fresh:
            self.processed_ids.add(c.id)
        return fresh

    def _classify_signal(self, comment: Comment, now: float) -> Optional[str]:
        """Fold one fresh comment into the per-bot state. Returns the bot name
        when the comment is ACTIONABLE (must flow to the kernel), else None."""
        bot = detectors.bot_for_login(comment.source)
        if bot is None:
            return None  # humans and unknown logins don't drive bot state or rounds
        st = self._bot_state(bot)
        st.last_seen = now

        # The errored comeback is NOT decided here: a bot is retracted only
        # on a SUBSTANTIVE comment strictly newer than the error signal, and the
        # SUBSTANTIVE label is not known until the kernel classifies the comment.
        # An errored bot's fresh comment still flows downstream as actionable
        # (below) → classified → `_maybe_errored_comeback` retracts iff it is
        # SUBSTANTIVE + strictly newer. A cosmetic / OUTDATED / INVALID /
        # question comment is NOT proof of recovery, so it never brings the bot back.
        pr_title, pr_body = self._fetch_pr_title_body()
        signal = detectors.detect_signal(
            comment.text, quota_llm=self.quota_llm,
            pr_title=pr_title, pr_body=pr_body,
        )
        if signal == detectors.SIGNAL_QUOTA:
            self.store.exclude_quota(bot)
            st.signal = signal
            self.notice("exclusion", f"{bot} excluded for the run: quota exhausted", status="skip")
            return None
        if signal == detectors.SIGNAL_PR_TOO_LARGE:
            self.store.exclude_pr_too_large(bot)
            st.signal = signal
            self.notice("exclusion", f"{bot} excluded for the run: PR too large", status="skip")
            return None
        if signal == detectors.SIGNAL_ERRORED:
            self.store.exclude_errored(bot)
            st.signal = signal
            st.error_created_at = comment.created_at
            self.notice("exclusion", f"{bot} excluded (errored — retractable on a newer comment)",
                        status="skip")
            return None
        if detectors.detect_clean_review(comment.text, llm_json=self.clean_llm):
            self.done.add(bot)
            st.signal = detectors.SIGNAL_CLEAN
            # A clean approval / "No issues found." sentinel IS a genuine review —
            # it feeds the SAFETY gate's reviewed-set (a fleet that approves clean
            # with zero comments still merges; the critical no-false-positive case).
            self.reviewed_ever.add(bot)
            print(f"[clean-review] {bot}: nothing to flag — voluntarily done")
            return None
        # The PR conversation channel (issues/<pr>/comments) is scanned for the
        # clean sentinel + the signals above ONLY — never routed to the fixer.
        # Per claude-code-review.yml every actionable finding MUST be an inline
        # review comment; a substantive finding posted top-level is ignored by
        # contract, so top-level bot chatter (status/summary) must not be
        # classified and acted on as if it were a real finding.
        if comment.from_issue_channel:
            return None
        # An actionable inline / review-body comment is a genuine review (the
        # placeholders were already filtered above), so it feeds the SAFETY gate.
        self.reviewed_ever.add(bot)
        return bot

    def _maybe_errored_comeback(self, comment: Comment, result: CommentResult) -> None:
        """An errored-excluded bot comes back ONLY on a SUBSTANTIVE comment
        strictly newer than its recorded error signal. Runs post-classification,
        when the label is known. Equal / missing / unparseable stamps keep it
        excluded (``_strictly_newer`` is conservative)."""
        bot = detectors.bot_for_login(comment.source)
        if bot is None:
            return
        st = self._bot_state(bot)
        if st.error_created_at is None:
            return
        if result.classification.label != "SUBSTANTIVE":
            return
        if _strictly_newer(comment.created_at, st.error_created_at):
            self.store.errored_comeback(bot)
            st.signal = None
            st.error_created_at = None
            self.notice("errored-comeback", f"{bot} posted a newer substantive "
                        "comment — back in the re-request gate", status="done")

    def _wait_for_quiescence(self, expected: Sequence[str], round_start: float) -> List[Comment]:
        # Round-scope the silence timer: a re-requested bot is "not seen yet THIS
        # round" and must be held to MIN_BOT_WAIT, never instantly quiesced on a
        # last_seen stamp left over from a prior round. Without this every round
        # ≥2 would close on its first poll and could auto-merge un-reviewed
        # (the signal/done/excluded short-circuits in `_quiesced` are unaffected).
        for bot in expected:
            self._bot_state(bot).last_seen = None
        actionable: List[Comment] = []
        last_activity = round_start
        while True:
            now = self.clock()
            fresh = self._ingest_new()
            for c in fresh:
                bot = self._classify_signal(c, now)
                if bot is not None:
                    actionable.append(c)
            if fresh:
                last_activity = now
            if all(self._quiesced(b, now, round_start) for b in expected):
                return actionable
            if (now - last_activity) >= self.times.idle_timeout:
                self.notice("round-wait", "idle timeout — closing the round", status="fallback")
                return actionable
            if (now - round_start) >= self.times.max_wait_total:
                self.notice("round-wait", "max round wait reached — closing the round",
                            status="fallback")
                return actionable
            self.sleep(self.times.poll_interval)

    # ------------------------------------------------------------------- run

    def run(self) -> RunOutcome:
        """Snapshot the run-start fleet (for the SAFETY gate's empty-vs-silent
        distinction), drive the round loop, and ALWAYS emit the persistent
        silent-reviewer warning at run end (every exit path, via ``finally``)."""
        self._run_start_fleet = set(self.expected_bots())
        try:
            outcome = self._run_loop()
            # On a manual-landing hand-back, rebase the loop's OWN branch onto the
            # latest base + --force-with-lease push it, so the operator can merge a
            # current PR. No-op on a merged run; skips dirty/diverged Bucket-C
            # states; reports the HONEST post-rebase state (see _maybe_exit_rebase).
            self._maybe_exit_rebase(outcome)
            return outcome
        finally:
            self._warn_persistently_silent()

    def _run_loop(self) -> RunOutcome:
        for round_no in range(1, self.max_rounds + 1):
            expected = _canonical(self.expected_bots())
            if not expected:
                if self.rr_active and round_no == 1:
                    print("[round] --rr-active: no still-active reviewers — clean exit")
                return self._clean_exit(round_no - 1)

            self._log_skipped(expected)  # honest skip-reason logging
            print(f"[round] Round {round_no} of {self.max_rounds} — expecting: "
                  f"{', '.join(expected)}")
            self._summon(round_no, expected)
            actionable = self._wait_for_quiescence(expected, self.clock())
            self._record_round_attendance(expected)  # silent-streak bookkeeping

            if not actionable:
                self._render_round(round_no, [], [])  # status-only round summary
                return self._clean_exit(round_no)

            results = process_comments(
                actionable, adapter=self.adapter, classify_runner=self.classify_runner,
                max_rounds=self.max_rounds,
            )
            round_actions = []
            for c, r in zip(actionable, results):
                self._maybe_errored_comeback(c, r)  # SUBSTANTIVE-only retract
                round_actions.append(
                    act_on_result(c, r, adapter=self.adapter, fix_dispatch=self.fix_dispatch))
            self.actions.extend(round_actions)
            for a in round_actions:
                print(f"  [{a.final:16}] comment {a.comment_id} ({a.disposition})"
                      + (f" — {a.detail}" if a.detail else ""))
            self._render_round(round_no, actionable, results)  # per-reviewer round summary

            if self.adapter.escalation.delivered:
                answers = self.answer_waiter(self.adapter.escalation)
                self.adapter.escalation.delivered.clear()
                if any(k.startswith("fix-") and (v or "").strip() == "3"
                       for k, v in answers.items()):
                    print("[round] operator chose stop on a failed-fix escalation")
                    return RunOutcome("stopped", round_no, False, self.actions)
                if any(v is None for v in answers.values()):
                    print("[round] unanswered escalation(s) — handing over for manual review")
                    return RunOutcome("needs-human", round_no, False, self.actions)

            # Poisoned-worktree gate (orthogonal to disposition): if ANY fix this
            # round could not prove a clean rollback, un-rolled-back residue may be
            # sitting in the shared worktree, where the per-round ``git add -A``
            # would sweep it onto the PR. Halt for a human BEFORE the push —
            # regardless of each comment's `final`, so a terminal "rejected" (or
            # "skipped-invalid") whose cleanup failed cannot leak silently. This
            # sits after the escalation gate above, so an explicit operator "stop"
            # is still reported as "stopped" rather than "needs-human".
            if any(a.rollback_failed for a in round_actions):
                print("[round] a fixer rollback could not be proven clean — "
                      "poisoned worktree, halting before push for manual review")
                # Bucket C: a poisoned worktree must never be rebased/force-pushed.
                return RunOutcome("needs-human", round_no, False, self.actions,
                                  rebase_skip=True)

            if any(a.final == "fixed" for a in round_actions) and self.push:
                pushed = commit_push.commit_and_push(
                    self.cwd,
                    message=f"fix: address review comments (round {round_no})",
                    run=self.gh_run,
                    notifier=self.adapter.escalation.notifier,
                    answer_wait=lambda n, ask: escalation_wait.wait_for_answer(
                        n, ask, sleep=self.sleep, clock=self.clock),
                    test_gate=self.test_gate,
                    notice=self.notice,
                )
                if pushed == "stopped":
                    # Stopped on a red gate with uncommitted/unpushed round residue
                    # → Bucket C: do not rebase/force-push an unverified tree.
                    return RunOutcome("stopped", round_no, False, self.actions,
                                      rebase_skip=True)
                if pushed == "error":
                    # push failed: local commit exists but remote never got it;
                    # continuing could lead to squash_merge on stale remote code.
                    # Bucket C: local/remote diverged — never rebase/force-push it.
                    return RunOutcome("needs-human", round_no, False, self.actions,
                                      rebase_skip=True)

        print(f"[round] max rounds ({self.max_rounds}) reached — not merging")
        return RunOutcome("max-rounds", self.max_rounds, False, self.actions)

    def _clean_exit(self, rounds: int) -> RunOutcome:
        print("[round] clean — every expected reviewer is done/excluded and "
              "no actionable comments remain")
        if self.auto_merge:
            # ── SAFETY gate (parity of the reference loop's never-merge-unreviewed
            # backstop): never auto-merge code that no expected reviewer actually
            # reviewed. Three-way distinction (the run-start fleet snapshot is the
            # discriminator) ─────────────────────────────────────────────────────
            fleet = set(self._run_start_fleet)
            if not fleet:
                # (a) zero reviewers by design → quiet no-auto-merge. There is
                #     nothing to gate on, so leaving the PR open is the safe,
                #     unalarming outcome (no review ever happened).
                self.notice("squash-merge",
                            f"no reviewers configured for this repo — leaving "
                            f"PR #{self.pr} open (nothing reviewed it)",
                            status="skip",
                            hint="add reviewers via the setup wizard, or merge by hand")
                return RunOutcome("clean", rounds, False, self.actions)
            if not (fleet & self.reviewed_ever):
                # (b) reviewers were expected but NONE genuinely reviewed → loud
                #     handback + block. A quota/error placeholder is a response,
                #     not a review, so it never lands in reviewed_ever (filtered in
                #     _classify_signal) and cannot satisfy this gate.
                self._block_unreviewed_merge(fleet)
                return RunOutcome("clean", rounds, False, self.actions)
            # (c) >=1 expected reviewer genuinely reviewed (incl. a clean approval)
            #     → the merge may proceed (subject to the label-gated CI gate).
            if label_gated_ci(self.cfg, self.repo):
                ci_ok = merge.wait_for_ci_green(
                    self.pr, repo=self.repo, cwd=self.cwd, run=self.gh_run,
                    notice=self.notice, sleep=self.sleep,
                )
                if not ci_ok:
                    # CI red / never-settled — hand the PR back, do not merge. Flag
                    # it so the manual-landing rebase reports the honest state.
                    self._premerge_ci_red = True
                    return RunOutcome("clean", rounds, False, self.actions)
        merged = merge.squash_merge(
            self.pr, repo=self.repo, enabled=self.auto_merge, cwd=self.cwd,
            run=self.gh_run, notice=self.notice,
        )
        return RunOutcome("clean", rounds, merged, self.actions)

    # --------------------------------------------------- PR metadata (lazy, cached)

    def _fetch_pr_title_body(self) -> Tuple[Optional[str], Optional[str]]:
        """Best-effort (pr_title, pr_body) from ``gh pr view``, fetched at most
        once per run. Returns (None, None) on any failure so the quota
        second-pass simply doesn't arm (deterministic verdict holds)."""
        if not hasattr(self, "_pr_meta_cache"):
            title = body = None
            try:
                argv = ["gh", "pr", "view", str(self.pr), "--json", "title,body"]
                if self.repo:
                    argv += ["-R", self.repo]
                proc = self.gh_run(argv, cwd=self.cwd)
                if getattr(proc, "returncode", 1) == 0:
                    data = json.loads(getattr(proc, "stdout", "") or "{}")
                    if isinstance(data, dict):
                        title = data.get("title") or None
                        body = data.get("body") or None
            except Exception:
                pass
            self._pr_meta_cache: Tuple[Optional[str], Optional[str]] = (title, body)
        return self._pr_meta_cache

    # --------------------------------------------------- manual-landing exit-rebase

    def _base_branch(self) -> Optional[str]:
        """The PR's base branch via ``gh pr view``, or None on any failure (the
        caller then skips the rebase — it can't rebase onto an unknown base)."""
        argv = ["gh", "pr", "view", str(self.pr), "--json", "baseRefName",
                "-q", ".baseRefName"]
        if self.repo:
            argv += ["-R", self.repo]
        return _git_line(argv, self.cwd, self.gh_run)

    def _handback_caution_reason(self, outcome: RunOutcome) -> Optional[str]:
        """Why a rebased hand-back is NOT 'ready to merge' (Bucket B), or None when
        it genuinely is ready (Bucket A: a real review happened and nothing is
        wrong). Read only on a successful rebase to phrase the honest outcome."""
        if outcome.status == "stopped":
            return "the run was stopped before a clean finish"
        if outcome.status == "max-rounds":
            return "the review did not reach a clean finish within the round budget"
        if outcome.status == "needs-human":
            return "a review thread or fix still needs you"
        # A clean, unmerged exit: ready ONLY if a real review happened and the
        # pre-merge CI (when gated) was not red.
        if self._premerge_ci_red:
            return "the pre-merge CI is red"
        fleet = set(self._run_start_fleet)
        if not fleet:
            return "no reviewers are configured for this repo"
        if not (fleet & self.reviewed_ever):
            return "no expected reviewer actually reviewed this PR"
        return None  # Bucket A — a genuine clean review; ready to merge

    def _maybe_exit_rebase(self, outcome: RunOutcome) -> None:
        """Exit-rebase wrapper that can never lose the run's outcome: the rebase is
        a courtesy on the way out, so ANY unexpected error degrades to a console
        note and the hand-back still returns normally."""
        try:
            self._exit_rebase_impl(outcome)
        except Exception as exc:  # never let the courtesy rebase crash the run
            self.notice("manual-landing",
                        f"manual-landing rebase skipped after an unexpected error "
                        f"({exc}) — merge PR #{self.pr} by hand", status="fallback")

    def _exit_rebase_impl(self, outcome: RunOutcome) -> None:
        """On a manual-landing hand-back, rebase the loop's OWN branch onto the
        latest base and ``--force-with-lease`` push it so the operator can merge a
        current PR (NEW capability — see :func:`commit_push.exit_rebase`).

        Buckets: rebase the A+B hand-backs; SKIP Bucket C (poisoned worktree /
        push-failed / unverifiable — ``outcome.rebase_skip``). A merged run and a
        push-disabled run are no-ops, and an already-current branch is never
        force-pushed. After a clean rebase the hand-back line states the REAL
        state — 'ready to merge' ONLY when a real review happened and nothing is
        wrong (Bucket A); otherwise 'merge at your discretion' with the reason
        (Bucket B). Never relabels a Bucket-B PR as ready."""
        if run_terminal_disposition(outcome) == "merge":
            return  # the loop merged it — nothing to rebase
        if not self.push:
            return  # pushing is off for this run — never force-push
        if outcome.rebase_skip:
            self.notice("manual-landing",
                        f"not rebasing PR #{self.pr} for manual landing — the "
                        f"worktree/branch is in an unverifiable state (uncommitted "
                        f"residue or an unpushed commit); resolve it by hand before "
                        f"merging", status="skip")
            return
        base = self._base_branch()
        if not base:
            self.notice("manual-landing",
                        f"could not determine PR #{self.pr}'s base branch — not "
                        f"rebasing for manual landing", status="skip")
            return
        # Guard: verify the worktree is on the PR's own feature branch before
        # force-pushing — a mis-pointed worktree would otherwise rebase+push
        # whichever unrelated branch happens to be checked out in cwd.
        # Fails CLOSED: if either value is unresolvable we cannot confirm we
        # are on the right branch, so we skip rather than risk force-pushing
        # an unrelated branch under a transient gh/git failure.
        pr_head = _pr_head_branch(self.pr, self.repo, self.cwd, self.gh_run)
        current_branch = _git_line(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], self.cwd, self.gh_run)
        if not pr_head or not current_branch:
            self.notice("manual-landing",
                        f"could not verify the worktree branch for PR "
                        f"#{self.pr} — not rebasing until branch identity "
                        f"can be confirmed", status="skip")
            return
        if pr_head != current_branch:
            self.notice("manual-landing",
                        f"not rebasing PR #{self.pr} — the worktree is on "
                        f"{current_branch!r} but the PR head is {pr_head!r}; "
                        f"resolve by hand", status="skip")
            return
        status, detail = commit_push.exit_rebase(
            self.cwd, base=base, run=self.gh_run, notice=self.notice)
        if status == "rebased":
            reason = self._handback_caution_reason(outcome)
            if reason is None:
                self.notice("manual-landing",
                            f"PR #{self.pr} rebased onto the latest {base} — "
                            f"ready to merge", status="done")
            else:
                self.notice("manual-landing",
                            f"PR #{self.pr} rebased onto the latest {base}, but "
                            f"{reason} — merge at your discretion", status="fallback")
        elif status == "current":
            self.notice("manual-landing",
                        f"PR #{self.pr} is already up to date with {base} — nothing "
                        f"to rebase", status="skip")
        elif status == "conflict":
            self.notice("manual-landing",
                        f"PR #{self.pr} could not be rebased for manual landing: "
                        f"{detail}", status="stop")
        elif status == "skipped":
            self.notice("manual-landing",
                        f"PR #{self.pr} left as-is for manual landing — {detail}",
                        status="skip")
        else:  # "error"
            self.notice("manual-landing",
                        f"PR #{self.pr} — {detail}", status="fallback")

    def _block_unreviewed_merge(self, fleet: Set[str]) -> None:
        """Loud console handback when a would-be clean auto-merge is refused
        because NO expected reviewer ever reviewed the PR (case b). Names the
        configured fleet so the admin can see exactly which reviewers stayed
        silent; the per-reviewer 'never responded' banner (emitted at run end)
        carries the actionable fix."""
        names = ", ".join(_canonical(fleet)) or "the configured fleet"
        _print_refusal_banner(
            f"NOT MERGING — NO REVIEW — PR #{self.pr}",
            f"The review round is clean, but NONE of the expected reviewers "
            f"({names}) actually reviewed this PR — no comment and no clean "
            f"approval. Auto-merging now would land code that no reviewer ever "
            f"saw, so the loop is leaving PR #{self.pr} open and handing it back. "
            f"Install/enable the reviewers (run the setup wizard) so a real "
            f"review arrives, or merge it yourself after reviewing by hand.")

    def _emit_auth_failure_banner(self, bot: str) -> None:
        """Loud, actionable console banner when a silent reviewer's run failed
        AUTHENTICATION — an invalid / expired / wrong ``CLAUDE_CODE_OAUTH_TOKEN``
        (a 401). Distinct from the persistent-silent banner (which says "turn the
        reviewer off"): here the reviewer IS wired up and the fix is to RE-MINT
        the token. Emitted in place of the silent banner when ``_detect_auth_
        failure`` finds the token-invalid signature in the reviewer's failed
        check-run log. The free product ships no settings UI, so the call to
        action is the setup entry point (``/review-pr setup``)."""
        label = _REVIEWER_LABEL.get(bot, bot.capitalize())
        _print_refusal_banner(
            f"REVIEWER AUTH FAILED — '{bot}' token invalid or expired",
            f"The {label} review ran but its model call returned 401 (an invalid "
            f"or expired bearer token), so it posted no review while the GitHub "
            f"job still concluded — which is why this looked like silence. This is "
            f"NOT a reviewer to turn off and NOT a setup step left half-wired: the "
            f"CLAUDE_CODE_OAUTH_TOKEN credential itself is bad. Re-mint and "
            f"re-store it — run /review-pr setup, which re-mints via `claude "
            f"setup-token` and re-stores the CLAUDE_CODE_OAUTH_TOKEN repo secret. "
            f"Until the token is re-minted, {label} cannot review this or any PR.")

    # ---- auth-failure check-run probe (the FREE twin of the loop's 401 signal) ----
    # round_driver ingests only PR comments (gh_ingest.fetch_comments), and a real
    # 401 posts ZERO comments while the GitHub job concludes — so the failure is
    # NOT observable in comments. The bundled claude-code-review.yml's post-step
    # makes the job RED on the token-invalid signature (reading the action's
    # execution_file), and emits its own ``::error`` line into the run log. That
    # ``::error`` survives ``show_full_output: false`` (which redacts only the
    # claude-code-action SDK output, not a later step's stdout), so the failure IS
    # observable in the failed run log. This probe reads it: a SILENT reviewer
    # whose "Claude Code Review" run carries the auth signature is a 401, not an
    # uninstalled bot — so we point the operator at re-minting, not removal.

    def _pr_checks_rows(self) -> List[dict]:
        """``gh pr checks`` rows for this PR, or [] on any error. Never raises.
        ``gh pr checks`` exits non-zero when checks are pending or failing (the
        normal case when a Claude review 401'd), so returncode is NOT consulted —
        rows are parsed from stdout regardless (same approach as merge._fetch_pr_checks)."""
        argv = ["gh", "pr", "checks", self.pr,
                "--json", "name,workflow,link,bucket,state"]
        if self.repo:
            argv += ["-R", self.repo]
        try:
            proc = self.gh_run(argv, cwd=self.cwd)
        except (subprocess.SubprocessError, OSError):
            return []
        try:
            rows = json.loads((proc.stdout or "").strip() or "[]")
        except (ValueError, TypeError):
            return []
        return rows if isinstance(rows, list) else []

    def _claude_review_run_id(self, rows: List[dict]) -> Optional[str]:
        """The workflow run id of the "Claude Code Review" check, REGARDLESS of
        conclusion, parsed from the run link. None on no match."""
        for key in ("name", "workflow"):
            for row in rows or []:
                if isinstance(row, dict) and row.get(key) == CLAUDE_REVIEW_CHECK_NAME:
                    m = re.search(r"/runs/(\d+)(?:/|$)", row.get("link") or "")
                    if m:
                        return m.group(1)
        return None

    def _fetch_run_log(self, run_id: str) -> str:
        """The failed-step log (then the full log) of run ``run_id``, or "" on any
        error. ``--log-failed`` is small and carries the post-step's ``::error``
        on an auth failure (the post-step is the step that exits non-zero)."""
        for extra in (["--log-failed"], ["--log"]):
            argv = ["gh", "run", "view", str(run_id), *extra]
            if self.repo:
                argv += ["-R", self.repo]
            try:
                proc = self.gh_run(argv, cwd=self.cwd)
            except (subprocess.SubprocessError, OSError):
                continue
            if getattr(proc, "returncode", 1) == 0 and (proc.stdout or "").strip():
                return proc.stdout
        return ""

    def _detect_auth_failure(self, bot: str) -> bool:
        """Best-effort: True iff ``bot``'s "Claude Code Review" run carries the
        token-invalid 401 signature. Scoped to ``claude`` (the only reviewer that
        authenticates with CLAUDE_CODE_OAUTH_TOKEN; the others use the GitHub App).
        Any missing repo / gh / network error → False (fall back to the generic
        silent banner). Never raises."""
        if bot != "claude" or not self.repo:
            return False
        try:
            run_id = self._claude_review_run_id(self._pr_checks_rows())
            if run_id is None:
                return False
            log = self._fetch_run_log(run_id)
            # A cleanly-successful result in the log (``"is_error": false``) means
            # the run worked and any 401 phrase is quoted review content, not a
            # real auth failure — short-circuit before the signature test.
            return (bool(log)
                    and not detectors.CLEAN_RESULT_RE.search(log)
                    and bool(detectors.AUTH_FAILED_RE.search(log)))
        except Exception:
            return False

    # ------------------------------------------------- silent-reviewer warning

    def _record_round_attendance(self, expected: Sequence[str]) -> None:
        """Round-end bookkeeping for the persistent-silent-reviewer warning. For
        every reviewer EXPECTED this round: if it posted anything (its round-scoped
        ``last_seen`` is set — a comment, clean approval, OR a quota/error
        placeholder all count) it has responded and is permanently protected from
        the warning; otherwise its consecutive-silent round count is bumped.
        Reviewers not expected this round (done / excluded) are left untouched."""
        for bot in expected:
            if self._bot_state(bot).last_seen is not None:
                self.responded_ever.add(bot)
            else:
                self.silent_rounds[bot] = self.silent_rounds.get(bot, 0) + 1

    def _was_review_expected(self, bot: str) -> bool:
        """True iff the loop genuinely EXPECTED a review from ``bot`` — it was
        successfully summoned at least once, OR it is configured
        ``auto_on_open=true`` (the loop deliberately does not summon such a bot;
        it is expected to review on PR open, so total silence is still meaningful
        — the new-repo uninstalled-fleet case). Never flags a bot the loop could
        not summon."""
        if bot in self.requested_ever:
            return True
        try:
            return bool(auto_on_open(self.cfg, bot, self.repo))
        except Exception:
            return False

    def _warn_persistently_silent(self) -> None:
        """Run-end: for every run-start reviewer that was genuinely expected yet
        NEVER responded in any round (and has no other known reason — not
        quota/PR-too-large/errored-excluded), emit a loud console warning naming
        the reviewer and the rounds it was silent. The free product ships no
        settings UI, so the call to action points at the setup wizard / config."""
        for bot in _canonical(self._run_start_fleet):
            if bot in self.responded_ever:
                continue  # responded at least once → not wastefully silent
            if self.store.is_excluded(bot):
                continue  # quota / PR-too-large / errored — a known reason, not silence
            if not self._was_review_expected(bot):
                continue  # never actually summonable → don't penalize
            rounds = self.silent_rounds.get(bot, 0)
            if rounds < 1:
                continue
            # A silent reviewer can mean two very different things with opposite
            # fixes: (1) not installed/enabled → remove it / install it, or (2)
            # installed but its credential 401'd (the run posted nothing yet the
            # job concluded green-and-silent). Before the generic "remove it"
            # banner, probe the reviewer's failed check-run for the token-invalid
            # signature; if found, emit the RE-MINT banner instead — same silence,
            # opposite remediation.
            if self._detect_auth_failure(bot):
                self._emit_auth_failure_banner(bot)
            else:
                self._emit_silent_banner(bot, rounds)

    def _emit_silent_banner(self, bot: str, rounds: int) -> None:
        """Loud, actionable console banner for a configured reviewer that never
        responded across the run — it wasted loop time and is almost certainly not
        installed/enabled on this repo. Mirrors the reference loop's persistent-
        silent escalation; the CTA names the setup wizard / config (the free
        product ships no settings UI)."""
        label = _REVIEWER_LABEL.get(bot, bot.capitalize())
        n = max(1, int(rounds))
        rnd = "round" if n == 1 else "rounds"
        _print_refusal_banner(
            f"REVIEWER SILENT — '{bot}' never responded across {n} {rnd}",
            f"The loop expected a review from {label} and waited, but it posted "
            f"nothing — no comment and no clean approval. That wasted loop time "
            f"on a reviewer that is almost certainly not installed/enabled on "
            f"this repo. Remove '{bot}' from your reviewer fleet (run the setup "
            f"wizard, or edit active_reviewers in your config) until its app / "
            f"workflow / secret is installed — or, if it should auto-review on PR "
            f"open, install it and enable that setting. The loop will keep "
            f"requesting and waiting on it every run until you do.")
