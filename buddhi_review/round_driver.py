"""The quiescence polling round-loop + the re-request exclusion wiring.

One round = summon/re-request the expected reviewers → wait a short beat for the
triggers to register → hold the round open until **every expected bot has
quiesced** → classify + kernel-decide + act on the round's new comments →
commit/push the applied fixes → decide whether to run another round. The run
ends **clean** the moment a round produces no substantive progress; a round that
did land a substantive fix earns another review round, up to ``max_rounds``.

**Termination.** Another review round is requested ONLY when the round produced
real substantive progress — at least one ``SUBSTANTIVE`` comment whose fix
actually landed AND changed files. A cosmetic / PR-description / outdated /
invalid-only round — or a substantive comment the fixer skipped, or a
substantive fix that changed nothing — is a clean finish: any applied fixes are
committed/pushed, then the run exits clean without re-summoning anyone. When the
round budget is spent and the final round completed cleanly (no unanswered
escalation, no poisoned worktree, no failed push, no operator stop), the exit
routes through the same clean-exit gates as a naturally-clean finish rather than
an unconditional hand-back.

**Quiescence.** A bot is done for the round once it posts a definitive
single-shot signal (clean / quota-exhausted / PR-too-large / errored) OR has
been silent for ``BUDDHI_BOT_QUIESCENCE_SECS`` since its LAST contribution —
the timer resets on every contribution from the same bot, so the window slides
with a bursting bot. A bot that has NOT been seen this round never self-quiesces;
it holds the round open, bounded only by ``IDLE_TIMEOUT`` (no activity from
anyone, after ``MIN_BOT_WAIT``) and ``BUDDHI_MAX_WAIT_TOTAL`` (per-round
ceiling). ``BUDDHI_BOT_QUIESCENCE_SECS`` of 0 or a negative value falls back to
the default, not a 1s floor. An empty-body review never promotes a bot to
no-issues (empty bodies are dropped at ingest).

**Exclusion.** Three independent cause-buckets ride ``ReviewStore``: quota and
PR-too-large are permanent; errored is transient and retractable — a bot whose
comment is **strictly newer** than its recorded error signal comes back (an
unparseable/equal/missing timestamp keeps it excluded, conservatively). Three
soft, run-scoped driver sets ride alongside: a reviewer whose round posted only
non-substantive comments is dropped as **polish-only**, one whose real findings
were ALL dismissed on reassessment (no change applied) is dropped as
**reviewed — no change**, and a reviewer expected yet silent for a full round
is **dropped from re-request** (silence is not approval). Every summon / poll /
merge gate subtracts the derived union.
``--rr`` re-pings everyone: it clears the soft buckets (voluntarily-done +
polish + reviewed-no-change) at run start; the hard buckets are never cleared. ``--rr`` /
``--rr-active`` also widen the round-1 summon set (``--rr``) or exit clean when
nothing is active (``--rr-active``). ``--rr-none`` is the opposite pole: nobody
is summoned or polled (``expected_bots()`` is empty), the comments already on the
PR are still fixed and resolved, and the run merges on a clean exit (when auto-merge
is enabled) even with
zero reviews — the one explicit lift of the never-merge-unreviewed backstop.

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
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from buddhi_review import (
    commit_push, detectors, escalation_wait, gh_ingest, merge, polish_state,
)
from buddhi_review.actuators import ActionResult, FixDispatch, act_on_result
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.classify import DISCARD_LABELS
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


def _env_positive_or_default(name: str, default: int) -> int:
    """``name`` as a POSITIVE int, else ``default``. Unlike :func:`_env_int`
    (which floors to 1), a 0 or negative value falls back to ``default`` — a
    non-positive quiescence window is meaningless, so it means "use the default",
    not "poll every second"."""
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_trigger(name: str, default: str) -> str:
    v = os.environ.get(name, "").strip()
    return v or default


# Round budget that didn't measure the diff falls back here; a non-finite /
# overflowing size maps to a high defensive backstop instead of crashing.
MAX_ROUNDS_FALLBACK = 10
_MAX_ROUNDS_BACKSTOP = 100

# ``ActionResult.final`` values that mean the loop genuinely FINISHED with a
# comment this run — a fix that landed, or a finding the loop dismissed as
# outdated / invalid / already-fixed / already-converged. Only a review thread
# whose root comment reached one of these is auto-resolved by the pre-merge
# thread gate. Deliberately EXCLUDED: ``rejected`` (a fix the verify pass
# refused — the finding still stands), ``escalated`` and ``deferred`` (handed to
# a human / postponed). A thread tied to one of THOSE, or to no comment the loop
# touched at all (a human's open thread), is never resolved and keeps the PR
# un-merge-ready — the point of the gate.
_RESOLVED_FINALS = frozenset({
    "fixed", "skipped", "skipped-invalid", "skipped-already-fixed",
    "already-resolved",
})


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
        val = int(raw)
        return val if val > 0 else None  # 0/negative → treat as invalid → auto-size
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


def _supersedes(new: Optional[str], old: Optional[str]) -> bool:
    """True when a message stamped ``new`` is MORE RECENT than one stamped ``old``
    — strictly newer by the parsed instants, or the first DATED message to meet an
    undated one (GitHub always stamps; a missing stamp is a degraded payload and
    never outranks a real one). Equal instants → False, so the first message of a
    same-instant submission (a review body and its inline comments) is kept."""
    if _parse_iso(old) is None:
        return _parse_iso(new) is not None
    return _strictly_newer(new, old)


def _same_instant(new: Optional[str], old: Optional[str]) -> bool:
    """True ONLY when both stamps parse AND denote the SAME instant. Missing /
    unparseable → False (conservative). The errored comeback pairs this with
    :func:`_strictly_newer`: a review submission stamps its body and its inline
    comments with the SAME created_at, so equal stamps from the same bot are
    same-review evidence (the review that carried the false error signal also
    carried real output) — while a strictly-older comment still never retracts
    a newer error. Naive-vs-aware stamps compare unequal (excluded stays)."""
    n, o = _parse_iso(new), _parse_iso(old)
    if n is None or o is None:
        return False
    return n == o


def _utcnow() -> datetime:
    """Wall-clock now (UTC). The driver's ``clock`` seam is ``time.monotonic``
    for poll timing; the rate-limit comeback instead compares a wall-clock reset
    epoch, so it needs a distinct, separately-injectable wall-clock seam."""
    return datetime.now(timezone.utc)


# The claude-code-review.yml workflow (managed-version 2) surfaces a Claude
# usage-limit silence as a machine-readable PR marker comment authored by
# github-actions[bot] (deliberately NOT claude[bot] — bot_for_login never maps
# it, so it can never read as a posted review or a clean sentinel):
#   <!-- claude-review-unavailable-v1 type=rate_limited resets_at=<epoch> -->
#   <!-- claude-review-unavailable-v1 type=credits_exhausted -->
# The free loop acts on type=rate_limited ONLY: it releases claude from the wait
# and re-summons it once the reset instant passes. type=credits_exhausted is a
# paid-tier concept (the workflow still emits it, but the free loop has no
# billing-mode split, so it is logged and ignored — the reviewer falls through
# to the ordinary silent handling).
CLAUDE_UNAVAILABLE_MARKER_RE = re.compile(
    r"claude-review-unavailable-v1\s+type=(?P<type>rate_limited|credits_exhausted)"
    r"(?:\s+resets_at=(?P<resets_at>\d+))?")
CLAUDE_UNAVAILABLE_MARKER_AUTHOR = "github-actions[bot]"


@dataclass
class RoundTimes:
    """Round wait bounds — these defaults are the authoritative ones."""
    quiescence: float = float(_env_positive_or_default("BUDDHI_BOT_QUIESCENCE_SECS", 60))
    poll_interval: float = 30.0
    min_bot_wait: float = 420.0
    idle_timeout: float = 900.0
    max_wait_total: float = float(_env_int("BUDDHI_MAX_WAIT_TOTAL", 1800))
    # Beat between posting the round's re-requests and opening the poll window, so
    # the review triggers have time to register before the loop starts polling for
    # their output. Applied only when a summon actually landed.
    register_delay: float = 60.0


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
    ("Outd", 5), ("Inval", 6), ("Biz", 4), ("Fail", 5), ("Status", 22),
)
_TABLE_COUNT_KEYS: Tuple[str, ...] = (
    "posted", "sub", "cosm", "prdesc", "outd", "inval", "biz", "fail",
)

# The classification labels that count as a "real finding" — a round in which a
# reviewer posted at least one of these keeps that reviewer in the re-request
# gate; a reviewer whose whole round was other labels is dropped as polish-only.
_REAL_FINDING_LABELS = frozenset({
    "SUBSTANTIVE", "BUSINESS_QUESTION", "CLASSIFICATION_FAILED",
})

# Why a reviewer is not in a round's expected set, keyed by a stable reason code.
# The long form is the honest skip-log line; the short form is the table cell.
_SKIP_LONG: Dict[str, str] = {
    "approved": "voluntarily done (LGTM)",
    "done": "voluntarily done (reviewed — no findings)",
    "quota": "quota exhausted",
    "pr-too-large": "PR too large",
    "errored": "errored (retractable on a newer comment)",
    "rate-limited": "rate-limited (usage window exhausted — re-requested after it resets)",
    "no-change": "every finding dismissed on reassessment — no change applied",
    "polish": "polishing only — no substantive findings left",
    "silent": "silent for a full round — dropped from re-request",
    "excluded": "excluded",
    "not-requested": "not requested (not in the enabled reviewer fleet)",
}
_STATUS_SHORT: Dict[str, str] = {
    # The canonical round-summary label set. Done-for-the-run reviewers split
    # three ways: an EXPLICIT all-clear (the "No issues found." sentinel / the
    # clean-review detector — a sign-off) is "Approved 👍"; a genuine review
    # with zero actionable findings but NO explicit sign-off is "Reviewed — no
    # findings ✓"; a reviewer whose substantive findings were ALL dismissed on
    # reassessment (fixer skip — no change applied) is "Reviewed — no change ✓"
    # (the same ✓ — functionally "reviewed, nothing to do").
    "approved": "Approved 👍",
    "done": "Reviewed — no findings ✓",
    "no-change": "Reviewed — no change ✓",
    "quota": "Quota exhausted ⚠️",
    "pr-too-large": "PR too large 📦",
    "errored": "Could not review ❌",
    "rate-limited": "Rate-limited ⏳",
    "polish": "Polish-only 🧹",
    "silent": "No review posted 🔇",
    "excluded": "excluded",
    # A roster reviewer outside the enabled fleet this run — never summoned, so
    # its row is a quiet "for completeness" entry, distinct from the repo-gate
    # "Not configured (repo) 🔧" (cannot run here) and from "No review posted 🔇"
    # (was expected, stayed silent).
    "not-requested": "Not requested 🙅",
}
# Render-time statuses for reviewers with NO skip key (still expected).
# "Active ✅" only ever means "engaged this round"; an expected reviewer that
# posted nothing renders the same "No review posted 🔇" as a silent drop.
_STATUS_ACTIVE = "Active ✅"
_STATUS_NOT_CONFIGURED = "Not configured (repo) 🔧"

# The carve-out that is NOT a thumbs-up: an "I wasn't able to review …" body is
# a placeholder, not a review. The quota / PR-too-large / errored placeholders
# are filtered upstream (they detect as signals and never become actionable);
# this guards the SAME family phrased in ways those deliberately-narrow regexes
# don't match — it gates ONLY the reviewed-no-findings promotion (the safe
# direction: an over-match merely keeps today's behaviour, the bot stays
# expected), so a no-review apology is never crowned "reviewed — no findings"
# and silently dropped from re-summon.
_NOT_A_REVIEW_RE = re.compile(
    r"(?i)(?:"
    # negation → review/request: "wasn't able to review", "could not review",
    # "can't fulfill your request right now" (real overload copy apologises
    # about the REQUEST, not the review)
    r"\b(?:unable to|not able to|can[’']?t|cannot|won[’']?t|failed to|"
    r"skipped|(?:was|were|is|am|are)n[’']?t|"
    r"(?:could|did|do|does|will|would|has|have|had)(?:n[’']?t| not))\b"
    r"[^.!?\n]{0,60}\b(?:review|request)"
    # review → failure, any verb form: "Review skipped.", "review could not be
    # completed", "The review was not successful.", "Review generation stopped"
    r"|\breviews?\b[^.!?\n]{0,40}"
    r"\b(?:not|skipped|stopped|aborted|cancell?ed|failed)\b"
    # "No review was generated …"
    r"|\bno\s+reviews?\b"
    # zero files reviewed, any word order: "reviewed 0 out of 12 changed
    # files", "reviewed no files", "0 out of 12 files reviewed", "0 files …"
    r"|\breviewed\s+(?:0|no)\s"
    r"|\b0\s+(?:out\s+of\s+\d+\s+)?files?\b"
    # "your/the request could not be processed" — never an overview's PULL
    # request (the lookbehind excludes exactly that noun phrase)
    r"|(?<!pull )\brequests?\b[^.!?\n]{0,40}\b(?:could|can|did|will|would)\s+not\b"
    # "too complex/long/… to review" (too large/big already signals upstream)
    r"|\btoo\s+\w+\s+to\s+review\b"
    # the transient-failure family — overload / timeout / unavailability
    # apologies that never name the review at all
    r"|\btimed?\s+out\b|\bunavailable\b|\bhigh\s+(?:volume|demand)\b"
    r"|\btry\s+again\s+later\b"
    r")"
)


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


def _dim(text: str) -> str:
    """``text`` wrapped in the dim SGR when stdout is a colour TTY; plain text
    otherwise (honours ``NO_COLOR`` / ``BUDDHI_LOOP_NO_COLOR`` and non-TTY)."""
    if not _colour_enabled(sys.stdout):
        return text
    return f"\033[2m{text}\033[0m"


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


# Head-sha seam — the sha in ``$BUDDHI_REVIEW_HEAD_SHA`` short-circuits the gh
# call, mirroring gh_ingest's COMMENTS_JSON_ENV / REACTIONS_JSON_ENV.
HEAD_SHA_ENV = "BUDDHI_REVIEW_HEAD_SHA"


def _pr_head_sha(pr, repo, cwd, run) -> str:
    """The PR's tip commit sha via ``gh api``, or ``""`` on ANY failure (missing
    gh, non-zero exit, empty output). The ONE head-sha reader: the same call
    stamps the polish state at a round's end and re-checks it on an ``--rr-active``
    restore, so write and restore can never disagree about what "the tip" is. An
    empty return is the fail-closed value — the caller never writes a stamp, and
    never restores against one, on an unknown tip."""
    seeded = os.environ.get(HEAD_SHA_ENV)
    if seeded is not None:
        return seeded.strip()
    argv = ["gh", "api", f"repos/{repo or '{owner}/{repo}'}/pulls/{pr}",
            "-q", ".head.sha"]
    return _git_line(argv, cwd, run) or ""


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
        f"Launch from a dedicated per-PR worktree (the open-pr / review-pr "
        f"flow builds one), or set BUDDHI_ALLOW_PRIMARY_CHECKOUT=1 to override.")
    _print_refusal_banner(f"PREFLIGHT — PRIMARY CHECKOUT — {repo or 'this repo'} on {head}", reason)
    notice("primary-checkout gate",
           f"{REFUSED_TO_LAUNCH_MARKER} in the primary checkout on {head} — fixers "
           f"need a dedicated worktree", status="stop",
           hint="run via open-pr / review-pr; bypass: BUDDHI_ALLOW_PRIMARY_CHECKOUT=1")
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
        reactions_fetch: Optional[Callable[..., List["gh_ingest.Reaction"]]] = None,
        threads_fetch: Optional[Callable[..., List["gh_ingest.ReviewThread"]]] = None,
        resolve_thread: Optional[Callable[..., bool]] = None,
        gh_run: Optional[Callable[..., "subprocess.CompletedProcess[str]"]] = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], None] = time.sleep,
        notice: Callable[..., str] = automation_notice,
        times: Optional[RoundTimes] = None,
        max_rounds: Optional[int] = None,
        diff_lines: Optional[int] = None,
        auto_merge: bool = False,
        rr: bool = False,
        rr_active: bool = False,
        rr_none: bool = False,
        preflight: bool = True,
        push: bool = True,
        test_gate: bool = True,
        answer_waiter: Optional[Callable[..., Dict[str, Optional[str]]]] = None,
    ) -> None:
        self.pr = str(pr)
        self.repo = repo
        self.cwd = cwd or os.getcwd()
        self.cfg = cfg if cfg is not None else load_config()
        # An unconfirmed repo (no repos[<repo>] entry) with NO global default to
        # fall back to is running purely on the built-in default fleet. This flag
        # drives ONLY the one-time operator console notice below — it is a repo-
        # REGISTRATION fact, orthogonal to any per-reviewer availability, so it no
        # longer paints the round table (which would falsely badge the
        # undetectable Copilot/Codex/Gemini "Not configured"). The per-reviewer
        # table badge is driven by _repo_gate_excluded instead. The common
        # confirmed / global-default install is NOT flagged. (The launch gate only
        # lets this state through under the BUDDHI_ALLOW_UNCONFIRMED_REPO bypass.)
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
        # The PR-body reactions reader (same injectable/env-seamed shape as
        # ``fetch``): a bare +1 from a reviewer is a voluntarily-done signal that
        # never arrives as a comment, so it is read here instead.
        self.fetch_reactions = reactions_fetch or gh_ingest.fetch_reactions
        # The review-thread reader + resolver (same injectable/env-seamed shape as
        # ``fetch``): the pre-merge thread gate reads GitHub's per-thread resolved
        # state through ``fetch_threads`` and marks the run's own handled threads
        # resolved through ``resolve_thread``.
        self.fetch_threads = threads_fetch or gh_ingest.fetch_review_threads
        self.resolve_thread = resolve_thread or gh_ingest.resolve_review_thread
        # commit_push's runner accepts both cwd= and timeout=, so one injected
        # fake covers every gh/git/test spawn the driver makes.
        self.gh_run = gh_run or commit_push._default_run
        self.clock = clock
        self.wall_clock = wall_clock
        self.sleep = sleep
        self.notice = notice
        # ── Per-reviewer repo-availability gate ───────────────────────────────
        # Badge a reviewer that posted no review by what the loop can ACTUALLY,
        # RELIABLY detect with its user token. Only Claude is detectable (a
        # user-scoped Contents-API GET of claude-code-review.yml), so the gate set
        # can hold only "claude". It is populated ONCE before round 1 (see
        # _populate_repo_gate) and is monotonic for the run — never re-evaluated or
        # cleared mid-round. Empty until the probe runs.
        self._repo_gate_excluded: Set[str] = set()
        self._repo_gate_probed = False
        # Preserve the operator cue that a repo was never registered in Buddhi.
        # The per-reviewer gate badges ONLY Claude, so retiring the old blanket
        # "Not configured (repo)" table badge — which painted ALL four reviewers,
        # including the undetectable Copilot/Codex/Gemini — removes the only signal
        # that a repo has no confirmed fleet. Re-emit that signal ONCE here, as a
        # console notice (NOT a per-round table badge). Under the launch gate an
        # unregistered repo with no global default only reaches driver
        # construction via BUDDHI_ALLOW_UNCONFIRMED_REPO, so this cue rides that
        # bypass path.
        if self._repo_unconfigured:
            self.notice(
                "repo not registered",
                f"{self.repo} has no confirmed reviewer fleet and no global "
                f"default — running on the built-in default fleet. Reviewer "
                f"availability is only detectable for Claude here; a silent "
                f"Copilot/Codex/Gemini row reads 'No review posted', not "
                f"'Not configured'.",
                status="fallback",
                hint="confirm reviewers via the setup wizard",
            )
        self.times = times or RoundTimes()
        # Round budget: an explicit value wins; otherwise BUDDHI_MAX_ROUNDS env,
        # then auto-size from the diff (uncapped), then the fallback.
        self.max_rounds = resolve_max_rounds(max_rounds, diff_lines=diff_lines)
        self.auto_merge = auto_merge
        self.rr = rr
        self.rr_active = rr_active
        # --rr-none: summon nobody, poll nobody. expected_bots() returns [] so no
        # reviewer is nudged/polled/waited-on; existing comments are still fixed
        # (the first _wait_for_quiescence ingest returns them and all([]) quiesces
        # instantly), then _clean_exit merges even with zero reviews (when auto_merge
        # is set) — the one explicit lift of the never-merge-unreviewed block.
        self.rr_none = rr_none
        # Whether to snapshot the PR's pre-existing review state before round 1
        # (see _preflight_snapshot). On by default — a launched loop always meets
        # a PR that may already carry reviews. Off only skips that pre-pass; the
        # round loop is unchanged. The --rr / --rr-none modes skip preflight (they
        # redefine round 1 themselves); --rr-active runs it through
        # _rr_active_restore, the restart reconstruction.
        self.preflight = preflight
        self.push = push
        self.test_gate = test_gate
        self.answer_waiter = answer_waiter or escalation_wait.wait_for_delivered

        self.store = self.adapter.store
        self.done: Set[str] = set()           # voluntarily-done (clean review OR round-end promotion)
        # The explicit all-clear subset of `done` — reviewers whose clean
        # review the detector caught. Tracked as a set of its own (not just
        # BotState.signal) because a LATER hard signal (quota / errored /
        # PR-too-large placeholder) overwrites the mutable signal; the
        # sign-off already happened, so its "Approved 👍" label must not be
        # demoted to "Reviewed — no findings ✓" by a placeholder's arrival.
        self.approved: Set[str] = set()
        # Soft, run-scoped exclusion sets kept on the driver (NOT the ReviewStore
        # hard buckets), so they never touch the SAFETY / hard-cause reporting.
        # Cleared by --rr (re-requests everyone):
        #   polishing     — a reviewer whose round posted only non-substantive
        #                   comments (nothing left to fix); dropped from re-request.
        self.polishing: Set[str] = set()
        #   reviewed_no_change — a reviewer whose substantive comment(s) this
        #                   round were ALL dismissed on reassessment (fixer
        #                   skip — no change applied); dropped from re-request
        #                   so the run never loops re-asking a reviewer whose
        #                   findings it has already judged not worth changing.
        self.reviewed_no_change: Set[str] = set()
        # NOT cleared by --rr — a silent drop persists for the run; re-inclusion
        # fires in _classify_signal() when the bot posts a new comment mid-run:
        #   silent_dropped — a reviewer expected yet silent for a full round;
        #                   silence is not approval, so it is dropped mid-run.
        self.silent_dropped: Set[str] = set()
        self.bots: Dict[str, BotState] = {}
        self.processed_ids: Set[str] = set()
        # ── PR-state snapshot machinery ───────────────────────────────────────
        # _preflight_batch: actionable comments already on the PR before round 1
        #   (folded through _classify_signal at run start), processed in round 1
        #   without a poll window. Consumed once — cleared when round 1 reads it.
        self._preflight_batch: List[Comment] = []
        # _preflight_responders: every bot that posted ANYTHING at preflight
        #   (clean / finding / hard signal). Round 1 does not poll or re-summon
        #   these — they already gave their verdict this run and won't re-post
        #   until re-requested after a fix — so an already-reviewed PR never burns
        #   the min-bot wait. Their attendance is credited via responded_ever.
        self._preflight_responders: Set[str] = set()
        # _preflight_seen: every bot that posted ANYTHING at preflight (a verdict
        #   OR mere chatter). A chatter-only bot is NOT a responder (round 1 still
        #   polls it), but it WAS seen — round 1 stamps it seen so it quiesces on
        #   the normal window rather than holding the round open as a never-seen
        #   bot would (matching how a fresh-launch round treats a bot that spoke).
        self._preflight_seen: Set[str] = set()
        # _reaction_baseline: reaction ids considered STALE — captured at
        #   preflight and re-captured before every re-request. A +1 whose id is in
        #   this set was left before the current review round (an earlier commit /
        #   a prior round) and never marks a bot done; a +1 with a fresh id does.
        #   None (the initial value) means "no baseline has been established yet"
        #   — DISTINCT from an empty set ("captured, zero reactions present"). A
        #   fold never treats any +1 as fresh while the baseline is None, so a
        #   failed capture can never let a stale +1 masquerade as fresh (fail
        #   closed, never fail open with no baseline).
        self._reaction_baseline: Optional[Set[str]] = None
        # _reaction_done: subset of `done` added via a +1 reaction fold (not a
        #   text-based clean review). Used by the between-rounds quota re-check to
        #   apply the ungated LLM pass even to reaction-done bots — their comment
        #   texts were NOT checked for quota in-round, so a novel-wording quota
        #   message the keyword gate missed must still be catchable here. Cleared
        #   by --rr alongside done / approved.
        self._reaction_done: Set[str] = set()
        # _round_baseline: per-bot comment/review ids known through the end of the
        #   last completed round (preflight seeds round 1's). An item whose id is
        #   NOT in its bot's baseline is new-since-the-last-re-request — the only
        #   items the between-rounds quota re-check reconsiders. Run-scoped.
        self._round_baseline: Dict[str, Set[str]] = {}
        # _round_new_comments: the fresh comments the CURRENT round's poll ingested
        #   (new since the last poll, by processed_ids). Reset each round; feeds
        #   the between-rounds quota re-check.
        self._round_new_comments: List[Comment] = []
        # _rate_limited_until: bot → the UTC datetime its provider usage
        #   window resets (from a workflow rate_limited marker). While an
        #   entry is present the bot is excluded from re-request; the timed
        #   comeback pops it once the reset instant passes. datetime.min
        #   marks an unknown reset → the plain next-round retry.
        self._rate_limited_until: Dict[str, datetime] = {}
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
        # _premerge_ci_red: set when the pre-merge CI gate failed at a clean exit
        #   (the label-gated poll went red/never-settled, OR the non-label
        #   mergeability gate saw a failing/never-settling check) — used so a
        #   manual-landing rebase reports the honest state ("CI is red — merge at
        #   your discretion") instead of "ready to merge".
        self._premerge_ci_red: bool = False
        # _thread_gate_block_reason: set when the pre-merge thread gate blocked a
        #   clean exit — the string captures WHY it blocked ("a review thread is
        #   still unresolved", "could not check review threads (no owner/repo
        #   configured)", …). None when the gate never fired or the PR is merge-
        #   ready. Used so the manual-landing hand-back reports the honest reason.
        self._thread_gate_block_reason: Optional[str] = None
        # _handled_inline_ids: ids of the INLINE review comments the run genuinely
        #   finished (a resolving disposition). A review thread's ROOT is always an
        #   inline comment (a PullRequestReviewComment), a DISTINCT GitHub id
        #   namespace from review-body / conversation comments — so the thread gate
        #   matches thread roots against ONLY these ids, never a review-body id that
        #   could numerically collide with (and wrongly resolve) a human's thread.
        self._handled_inline_ids: Set[str] = set()
        # _claude_trigger_failed: True once an "@claude review" summon failed to
        #   post and was never later re-posted successfully. Drives the additive
        #   #g9a NOTIFICATION (never a block): a clean exit where the loop's
        #   primary reviewer never saw the code yet another reviewer did.
        self._claude_trigger_failed: bool = False
        # _silent_noted: bots that have already received the once-per-run per-round
        #   silent-reviewer guidance note (the dim [reviewer-silent] prerequisite
        #   hint that precedes the run-end persistent-silent banner).
        self._silent_noted: Set[str] = set()

    # ------------------------------------------------------------------ state

    def expected_bots(self) -> List[str]:
        """The expected-bot gate: enabled reviewers minus voluntarily-done, minus
        the soft driver drops (polish-only + silent + reviewed-no-change), minus the derived union of
        the three hard exclusion buckets."""
        if self.rr_none:
            # --rr-none: the operator asked for zero reviewers — so nobody is
            # expected. An empty set means _summon targets nobody, the poll never
            # waits (all([]) quiesces at once), and _run_start_fleet is empty,
            # which the _clean_exit rr-none gates read as an intentional (not
            # accidental) no-review merge.
            return []
        return [
            b for b in active_reviewers(self.cfg, self.repo)
            if b not in self.done
            and b not in self.polishing
            and b not in self.reviewed_no_change
            and b not in self.silent_dropped
            and b not in self._rate_limited_until
            and not self.store.is_excluded(b)
        ]

    def _bot_state(self, bot: str) -> BotState:
        return self.bots.setdefault(bot, BotState())

    # ----------------------------------------------------- skip reason + summary

    def _skip_key(self, bot: str) -> Optional[str]:
        """Why ``bot`` is not in this round's expected set → a stable reason code,
        or None when it is still expected (active). Done-for-the-run splits on
        HOW the reviewer got there: an explicit all-clear (the clean-review
        signal — sentinel / LGTM) is "approved"; the round-end promotion (a
        genuine review with zero findings and no sign-off) is "done"."""
        st = self._bot_state(bot)
        if bot in self.approved or st.signal == detectors.SIGNAL_CLEAN:
            return "approved"
        if bot in self.done:
            return "done"
        if bot in self.reviewed_no_change:
            return "no-change"
        if st.signal == detectors.SIGNAL_QUOTA:
            return "quota"
        if st.signal == detectors.SIGNAL_PR_TOO_LARGE:
            return "pr-too-large"
        if st.signal == detectors.SIGNAL_ERRORED:
            return "errored"
        if st.signal == detectors.SIGNAL_RATE_LIMITED:
            return "rate-limited"
        if bot in self.polishing:
            return "polish"
        if bot in self.silent_dropped:
            return "silent"
        if self.store.is_excluded(bot):
            return "excluded"
        if bot not in active_reviewers(self.cfg, self.repo):
            # Outside the enabled fleet — never summoned this run. Deliberately
            # the LOWEST-priority reason: any real state above (a sign-off, a
            # posted placeholder, a round-end demotion) outranks it, so a bot
            # that engaged anyway is never masked as merely not-requested.
            return "not-requested"
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

    def _bot_status_text(self, bot: str, *, expected: Optional[Sequence[str]] = None,
                         posted: int = 0) -> str:
        """The round-summary Status cell for ``bot``.

        ``expected`` is THIS round's expected set as computed at round start
        (before any mid-round exclusion), ``posted`` this round's classified-
        comment count for ``bot`` — both feed the round-scoping of the errored
        label below. ``expected=None`` (a caller with no round context) keeps
        the label un-scoped."""
        key = self._skip_key(bot)
        if key is None:
            if self._bot_state(bot).last_seen is not None:
                return _STATUS_ACTIVE  # engaged this round
            # Idle reviewer, no lifecycle signal. Badge ONLY what the loop can
            # reliably detect per-reviewer with its user token. Claude alone is
            # detectable (a Contents-API GET of claude-code-review.yml), so an
            # absent workflow puts "claude" in _repo_gate_excluded → "Not
            # configured (repo) 🔧". Copilot/Codex/Gemini are NEVER in that set —
            # Copilot's summon 422 is overloaded (enabled-but-busy vs not-enabled,
            # and can succeed-silently, cli/cli#11245) and a Codex/Gemini App
            # install needs an App JWT the loop's user token cannot mint (GET
            # repos/{repo}/installation → 404) — so their silence is the ONLY
            # honest signal and MUST stay "No review posted 🔇", never a "Not
            # configured" the loop cannot verify.
            return (_STATUS_NOT_CONFIGURED if bot in self._repo_gate_excluded
                    else _STATUS_SHORT["silent"])
        if key == "not-requested" and self._bot_state(bot).last_seen is not None:
            # A not-summoned reviewer that posted anyway THIS round is engaged,
            # not absent — activity earns the same "Active ✅" an expected
            # reviewer gets, never the not-requested fallback.
            return _STATUS_ACTIVE
        if (key == "errored" and expected is not None
                and bot not in expected and posted == 0
                and self._bot_state(bot).last_seen is None):
            # ROUND-SCOPED (round-2 mislabel incident, 2026-07-04): "Could not
            # review ❌" describes an EVENT — the round's attempt produced an
            # error placeholder. In LATER rounds the bot is deliberately NOT
            # re-summoned (expected_bots subtracts the errored exclusion), so
            # the honest per-round verdict is "Not requested 🙅" — the same
            # rendering every other skipped bot gets (e.g. polish-only).
            # Unlike quota / PR-too-large — persistent STATE that stays true
            # each round — a transient error says nothing about a round in
            # which the bot never ran. In the round the error fired, the bot
            # is still in the round-START expected set, so the label renders
            # there. Two arms keep it honest when the bot DID act this round
            # without being expected: ``posted`` (a classified comment — a
            # genuine one retracts via the comeback before the table renders)
            # and ``last_seen`` (any contribution this round, reset per round
            # in _wait_for_quiescence — e.g. a re-posted error placeholder,
            # which is seen but not a classified comment). Either keeps
            # "Could not review ❌"; only a round the bot truly sat out reads
            # "Not requested 🙅".
            return _STATUS_SHORT["not-requested"]
        return _STATUS_SHORT.get(key, key)

    def _round_table_rows(self, actionable: Sequence[Comment],
                          results: Sequence[CommentResult],
                          expected: Optional[Sequence[str]] = None) -> List[dict]:
        """One summary row per reviewer (canonical order) from this round's
        classified comments + each reviewer's terminal status. The table is the
        COMPLETE view: every built-in reviewer gets a row every round — one
        outside the enabled fleet renders "Not requested 🙅" — plus a row for any
        reviewer that actually posted. Display only: summoning, polling, and
        expectation stay on the enabled fleet. ``expected`` is this round's
        round-start expected set (round-scopes the errored label)."""
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
        roster = _canonical(list(REVIEWER_ORDER)
                            + list(active_reviewers(self.cfg, self.repo))
                            + list(counts))
        for bot in roster:
            d = counts.get(bot, {})
            row = {"bot_key": bot, "label": _REVIEWER_LABEL.get(bot, bot.capitalize()),
                   "status": self._bot_status_text(
                       bot, expected=expected, posted=d.get("posted", 0))}
            for k in _TABLE_COUNT_KEYS:
                row[k] = d.get(k, 0)
            rows.append(row)
        return rows

    def _render_round(self, round_no: int, actionable: Sequence[Comment],
                      results: Sequence[CommentResult],
                      expected: Optional[Sequence[str]] = None) -> None:
        _render_round_table(round_no, self.max_rounds,
                             self._round_table_rows(actionable, results, expected))
        if expected is not None:
            self._emit_silent_reviewer_guidance(expected)

    def _emit_silent_reviewer_guidance(self, expected: Sequence[str]) -> None:
        """#50: a dim, once-per-run guidance NOTE for a reviewer that was expected
        this round yet posted nothing — the prerequisite-setup hint that precedes
        the run-end persistent-silent banner. Fires only for a GENUINELY-expected
        reviewer (summoned, or ``auto_on_open``): a reviewer that responded, is
        excluded for a known reason (quota / errored / rate-limited), or was never
        summonable is skipped. The wording forks on ``auto_on_open`` (summoned in
        round 1 vs. expected to review on PR open). This is guidance, not a status
        cell — the round table already renders such a reviewer "No review posted 🔇"."""
        for bot in _canonical(expected):
            if bot in self._silent_noted:
                continue
            if self._bot_state(bot).last_seen is not None:
                continue  # responded this round → not silent
            if self.store.is_excluded(bot) or bot in self._rate_limited_until:
                continue  # a known cause (quota / errored / rate-limited), not a setup gap
            if not self._was_review_expected(bot):
                continue  # never summonable → don't nag
            self._silent_noted.add(bot)
            label = _REVIEWER_LABEL.get(bot, bot.capitalize())
            try:
                auto = bool(auto_on_open(self.cfg, bot, self.repo))
            except Exception:
                auto = False
            if auto:
                note = (f"{label} is enabled but posted nothing within its review "
                        f"window. It is configured to review on PR open (not summoned "
                        f"by the loop), so confirm its prerequisites (app / plan / "
                        f"workflow / secret, whichever applies) are set on this repo "
                        f"AND that automatic review on open is actually enabled here.")
            else:
                note = (f"{label} was summoned this round but posted nothing. A silence "
                        f"here usually means its prerequisite setup is incomplete — "
                        f"confirm the app / plan / workflow / secret / trigger for this "
                        f"reviewer is configured on this repo.")
            print(_dim(f"  [reviewer-silent] {note}"))

    # ------------------------------------------------------------- re-request

    def _summon(self, round_no: int, expected: Sequence[str]) -> None:
        """Round 1 summons only ``auto_on_open: false`` reviewers (the others
        already review on PR open), resolved PER-REPO from the loop's bound repo
        so a reviewer's auto-review setting can differ across repos. ``--rr`` and
        ``--rr-active`` both widen round 1 to re-request the whole expected set —
        bots don't re-review an existing PR spontaneously, so the flag's
        re-request half must actually fire (``--rr-active`` additionally exits
        clean when nothing is active, handled in ``run``). Rounds ≥2 re-request
        every still-expected bot.

        After the re-requests are posted, wait ``register_delay`` for the review
        triggers to register before the caller opens the poll window — but only
        when a summon actually landed (an all-``auto_on_open`` round-1 with no
        summon does not wait)."""
        if round_no == 1:
            targets = [
                b for b in expected
                if self.rr or self.rr_active or not auto_on_open(self.cfg, b, self.repo)
            ]
        else:
            targets = list(expected)
        # NB: a list comprehension, not any(...) — every target must be summoned;
        # any() would short-circuit on the first success and skip the rest.
        summoned = [self._request_review(bot) for bot in targets]
        if any(summoned) and self.times.register_delay > 0:
            self.sleep(self.times.register_delay)

    def _request_review(self, bot: str) -> bool:
        """Post ``bot``'s re-request trigger. Returns True iff the trigger
        actually landed (so the caller knows whether to wait out the register
        delay)."""
        if bot == "copilot":
            argv = [
                "gh", "api", "-X", "POST",
                f"repos/{self.repo or '{owner}/{repo}'}/pulls/{self.pr}/requested_reviewers",
                "-f", f"reviewers[]={COPILOT_REVIEWER_SLUG}",
            ]
        else:
            trigger = TRIGGER_COMMENTS.get(bot)
            if not trigger:
                return False
            argv = ["gh", "pr", "comment", self.pr, "--body", trigger]
            if self.repo:
                argv += ["-R", self.repo]
        try:
            proc = self.gh_run(argv, cwd=self.cwd)
            if proc.returncode != 0:
                self.notice("re-request", f"{bot} re-request failed: "
                            f"{(proc.stderr or '').strip()[:120]}", status="fallback")
                if bot == "claude":
                    self._claude_trigger_failed = True  # #g9a: primary reviewer not summoned
                return False
            # The summon landed — so a later silence is the reviewer's, not a
            # failed request (never flag a bot we could not actually summon).
            self.requested_ever.add(bot)
            if bot == "claude":
                self._claude_trigger_failed = False  # a later success clears the flag
            return True
        except (subprocess.SubprocessError, OSError) as exc:
            self.notice("re-request", f"{bot} re-request failed: {exc}", status="fallback")
            if bot == "claude":
                self._claude_trigger_failed = True
            return False

    # ------------------------------------------------------------- quiescence

    def _quiesced(self, bot: str, now: float, round_start: float) -> bool:
        st = self._bot_state(bot)
        if st.signal is not None or bot in self.done or self.store.is_excluded(bot):
            return True
        if st.last_seen is not None:
            return (now - st.last_seen) >= self.times.quiescence
        # A bot NOT seen this round never self-quiesces — it holds the round open
        # so a slow reviewer's output is never skipped past. The round is bounded
        # instead by the idle-timeout (after MIN_BOT_WAIT) and the max-wait
        # ceiling in _wait_for_quiescence.
        return False

    def _ingest_new(self) -> List[Comment]:
        comments = self.fetch(self.pr, repo=self.repo, cwd=self.cwd)
        fresh = [c for c in comments if c.id not in self.processed_ids]
        for c in fresh:
            self.processed_ids.add(c.id)
        return fresh

    def _scan_unavailable_markers(self, fresh: Sequence[Comment]) -> None:
        """Act on a workflow-posted Claude usage-limit marker in this batch.

        The claude-code-review.yml workflow (managed-version 2) posts ONE marker
        PR comment when its Claude run died on a usage limit — a state that
        otherwise reads as plain reviewer silence and burns the full poll window.
        Only a comment authored EXACTLY by github-actions[bot] is trusted (a PR
        participant pasting the marker text cannot gate the reviewer). The free
        loop acts on ``type=rate_limited`` ONLY: it records the reset instant and
        releases claude from the wait so the window is not burned; the timed
        comeback re-summons it once the reset passes. ``type=credits_exhausted``
        is a paid-tier concept (no billing-mode split here) — logged and ignored,
        so claude falls through to the ordinary silent handling."""
        newest = None  # (parsed_datetime_or_None, match)
        for c in fresh:
            if c.source != CLAUDE_UNAVAILABLE_MARKER_AUTHOR:
                continue
            m = CLAUDE_UNAVAILABLE_MARKER_RE.search(c.text or "")
            if not m:
                continue
            key = _parse_iso(c.created_at)
            if newest is None or (key is not None and (newest[0] is None or key > newest[0])):
                newest = (key, m)
        if newest is None:
            return
        m = newest[1]
        mtype = m.group("type")
        if mtype != "rate_limited":
            self.notice("claude-review-unavailable",
                        f"marker type={mtype} ignored (the free loop handles "
                        f"rate_limited only)", status="skip")
            return
        if "claude" in self.done:
            return  # already concluded this run — nothing to release
        epoch = int(m.group("resets_at") or 0)
        until = None
        # Guard the conversion: an implausibly large epoch (a future ms-scale
        # resetsAt from an SDK format change would pass the workflow's
        # digits-only sanitizer) makes datetime.fromtimestamp raise; this runs in
        # the poll hot path, so degrade a bad epoch to the unknown-reset
        # next-round-retry path rather than crash the run.
        if 0 < epoch <= 4102444800:  # <= year 2100 in seconds
            try:
                until = datetime.fromtimestamp(epoch, timezone.utc)
            except (ValueError, OverflowError, OSError):
                until = None
        if until is not None:
            when = until.isoformat()
            comeback = f"re-summons claude in the first round after {when}"
            # Stale marker: the reset window already elapsed before this loop
            # saw it. Quiescing the round on a past epoch would prematurely
            # close a round that may still have a real review in-flight.
            if until <= self.wall_clock():
                self.notice("claude-rate-limited",
                            f"stale rate-limit marker (resets_at {when} already "
                            f"elapsed) — ignoring", status="skip")
                return
        else:
            until = datetime.min.replace(tzinfo=timezone.utc)
            when = "an unknown time"
            comeback = "retries claude next round (no reset time in the marker)"
        self._rate_limited_until["claude"] = until
        self._bot_state("claude").signal = detectors.SIGNAL_RATE_LIMITED
        self.notice("claude-rate-limited",
                    f"subscription usage window exhausted (workflow marker) — "
                    f"released from this round's wait; resets {when}",
                    status="fallback", hint=comeback)

    def _apply_rate_limit_comeback(self) -> None:
        """Round-boundary comeback: pop every rate-limited bot whose reset instant
        has passed (datetime.min pops on the FIRST boundary → the plain next-round
        retry) and clear its released signal so it re-enters ``expected_bots``."""
        now = self.wall_clock()
        for bot in sorted(b for b, until in self._rate_limited_until.items() if until <= now):
            self._rate_limited_until.pop(bot, None)
            st = self._bot_state(bot)
            if st.signal == detectors.SIGNAL_RATE_LIMITED:
                st.signal = None
            # The rate-limit round quiesces without a real review from the bot,
            # so _record_round_attendance adds it to silent_dropped. Clear that
            # here so expected_bots() re-admits the bot after the reset.
            self.silent_dropped.discard(bot)
            self.silent_rounds[bot] = 0
            self.notice("rate-limited-comeback",
                        f"{bot} usage window has reset — re-requesting", status="done")

    # -------------------------------------------------------- reaction signals

    def _capture_reaction_baseline(self) -> None:
        """Snapshot the reaction ids currently on the PR as the STALE set. A +1
        whose id is in this baseline was left before the current re-request (an
        earlier commit or a prior round) and never marks a bot done; a +1 with a
        fresh id (added after this snapshot) does. Called at preflight and before
        every re-request. On a fetch error the baseline is set to None so the fold
        stays fail-closed until the next successful snapshot — a stale +1 that
        landed after the last good capture cannot masquerade as fresh."""
        try:
            reactions = self.fetch_reactions(self.pr, repo=self.repo, cwd=self.cwd)
        except (subprocess.SubprocessError, OSError, RuntimeError):
            self._reaction_baseline = None
            return
        self._reaction_baseline = {r.id for r in reactions}

    def _fold_reactions(self, now: float) -> bool:
        """Fetch the PR's reactions and fold every FRESH ``+1`` (id not in the
        stale baseline) from a recognized reviewer login into the SAME clean-review
        outcome the sentinel uses — the bot is voluntarily done (``Approved 👍``).
        A +1 NEVER overrides a hard cause (quota / PR-too-large / errored) or an
        already-recorded signal: the bot's state is checked first, exactly like the
        clean-review path. Returns True iff a fresh +1 was folded (round activity).
        Fail-CLOSED when no baseline has been established (``None``): without a
        real stale snapshot, fresh cannot be told from stale, so nothing is folded.
        Also fail-closed (no fold) on this fetch's own error."""
        if self._reaction_baseline is None:
            return False  # no baseline yet → cannot distinguish fresh from stale
        try:
            reactions = self.fetch_reactions(self.pr, repo=self.repo, cwd=self.cwd)
        except (subprocess.SubprocessError, OSError, RuntimeError):
            return False
        folded = False
        for r in reactions:
            if r.content != "+1" or r.id in self._reaction_baseline:
                continue  # not a thumbs-up, or a stale +1 from an earlier round
            bot = detectors.bot_for_login(r.source)
            if bot is None:
                continue
            st = self._bot_state(bot)
            # A +1 defers to any recorded state: a hard signal (quota / errored /
            # PR-too-large), an existing clean sign-off, or an already-done bot.
            if st.signal is not None or bot in self.done or self.store.is_excluded(bot):
                continue
            st.last_seen = now
            self.done.add(bot)
            self._reaction_done.add(bot)    # mark as reaction-done for quota re-check
            self.approved.add(bot)          # hard causes (quota/errored/PR-too-large) can still evict this
            st.signal = detectors.SIGNAL_CLEAN
            self.reviewed_ever.add(bot)     # a +1 IS a genuine clean review
            self.responded_ever.add(bot)
            if bot in self.silent_dropped:
                self.silent_dropped.discard(bot)
            folded = True
            print(f"[reaction] {bot}: +1 — voluntarily done")
        return folded

    # ----------------------------------------------------- round-baseline re-check

    def _note_baseline(self, comments: Sequence[Comment]) -> None:
        """Record each comment's id under its bot in the per-bot round baseline —
        the set of ids known through the end of the round that just ran (preflight
        seeds round 1's). Items already here are never re-checked for quota; items
        that arrive after are new-since-the-last-re-request."""
        for c in comments:
            bot = detectors.bot_for_login(c.source)
            if bot is not None:
                self._round_baseline.setdefault(bot, set()).add(c.id)

    def _recheck_quota_between_rounds(self, fresh: Sequence[Comment]) -> None:
        """Between-round quota re-check over this round's new-since-baseline items.

        The poll's inline quota detector reaches the model ONLY when the keyword
        gate fires (hot-path economy), so a quota message with novel wording can
        be classified as an ordinary finding in-round. Left unre-checked, the loop
        would keep re-requesting a bot that already signalled it is unavailable.
        This runs the landed LLM quota tier (``self.quota_llm`` — never a new
        model role) UNGATED over each still-expected, not-yet-excluded bot's
        new-since-baseline items; a quota verdict excludes the bot for the run and
        retracts its (mis-recorded) review from the merge gate. No-op when
        ``quota_llm`` is unset (deterministic runs are unchanged)."""
        if self.quota_llm is None:
            return
        for c in fresh:
            bot = detectors.bot_for_login(c.source)
            if bot is None:
                continue
            if c.id in self._round_baseline.get(bot, set()):
                continue  # known before this round's re-request — already handled
            if bot in self.done and bot not in self._reaction_done:
                continue  # text-based clean sign-off already passed quota check in-round
            st = self._bot_state(bot)
            already_excluded = self.store.is_excluded(bot)
            # Fast skip only when there is nothing left to do: already hard-excluded
            # AND not lingering in reviewed_ever. Do NOT skip merely because a
            # signal is set — a bot hard-excluded by ANOTHER comment in this batch
            # can still carry a novel-wording quota message the poll mis-recorded as
            # a review, and that entry must be purged or it satisfies the merge gate.
            if already_excluded and bot not in self.reviewed_ever:
                continue
            if detectors.quota_exhausted_via_llm(c.text, self.quota_llm):
                if not already_excluded:
                    self.store.exclude_quota(bot)
                    st.signal = detectors.SIGNAL_QUOTA
                # A quota message is never a review — drop the entry the poll
                # mis-recorded so it can never satisfy the never-merge gate, even
                # when the bot is already excluded for another comment this batch.
                self.reviewed_ever.discard(bot)
                self.done.discard(bot)           # quota hard-cause evicts any prior clean fold
                self.approved.discard(bot)       # quota hard-cause wins over any prior +1 fold
                self._reaction_done.discard(bot)  # keep in sync with done
                self.notice("exclusion",
                            f"{bot} excluded for the run: quota exhausted "
                            f"(detected on a between-rounds re-check)", status="skip")

    def _classify_signal(self, comment: Comment, now: float,
                         batch_finding_stamps: Optional[Dict[str, List[Optional[str]]]] = None,
                         superseded: bool = False,
                         ) -> Optional[str]:
        """Fold one fresh comment into the per-bot state. Returns the bot name
        when the comment is ACTIONABLE (must flow to the kernel), else None.
        ``batch_finding_stamps`` — per-bot ``created_at`` stamps of the inline
        findings in the SAME fetch batch (computed by the poll loop) — feeds
        the errored record-time check: a review that arrives WITH same-instant
        findings is a completed review, never an error placeholder. Stamps
        (not a bare bot set), so a STALE finding swept up by round 1's
        full-history first poll can never shield a genuinely NEW placeholder.

        ``superseded`` — a strictly NEWER message from the same bot exists in this
        batch, so a clean-review verdict in THIS comment is stale and must not fold
        the bot voluntarily-done. Set only by the preflight snapshot, which ingests
        the PR's whole history at once in ENDPOINT order (not chronological): a
        reviewer that said "LGTM" and then posted real findings is still engaged,
        and an old sign-off must never silence it for the run. The poll never passes
        it — a comment arriving live IS the bot's latest message."""
        bot = detectors.bot_for_login(comment.source)
        if bot is None:
            return None  # humans and unknown logins don't drive bot state or rounds
        st = self._bot_state(bot)
        st.last_seen = now
        # Re-include immediately on any new comment — _record_round_attendance()
        # only iterates over expected_bots(), which excludes silent_dropped, so
        # the discard() there never fires for a dropped bot.
        if bot in self.silent_dropped:
            self.silent_dropped.discard(bot)
            self.notice("silent-reinclusion", f"{bot} posted a new comment — re-included",
                        status="done")

        # The errored comeback is NOT decided here: a bot is retracted only on a
        # comment carrying REVIEW OUTPUT (classified SUBSTANTIVE or COSMETIC —
        # either proves the bot produced a review) that is not older than the
        # error signal, and that label is not known until the kernel classifies
        # the comment. An errored bot's fresh comment still flows downstream as
        # actionable (below) → classified → `_maybe_errored_comeback` retracts.
        # An OUTDATED / INVALID / question comment is NOT proof of recovery, so
        # it never brings the bot back.
        if self.quota_llm is not None:
            pr_title, pr_body = self._fetch_pr_title_body()
        else:
            pr_title, pr_body = None, None
        signal = detectors.detect_signal(
            comment.text, quota_llm=self.quota_llm,
            pr_title=pr_title, pr_body=pr_body,
        )
        if signal == detectors.SIGNAL_ERRORED:
            # Record-time completed-review check: a body that IS an inline
            # finding, or that arrives alongside SAME-INSTANT findings from the
            # same bot (a review submission stamps its body and inline comments
            # alike), is REVIEW OUTPUT — the bot demonstrably reviewed, so no
            # errored signal is recorded and the comment flows on to the normal
            # handling below. Deliberately NOT accepted as evidence here:
            #   * clean-review phrasing in the SAME body — a real failure
            #     placeholder states its own zero output ("review run failed;
            #     no comments were posted"), and reading that as an all-clear
            #     would crown the failed bot "Approved", satisfy the merge
            #     gate, and auto-merge a PR nobody reviewed;
            #   * a same-batch finding with a DIFFERENT stamp — round 1's
            #     first poll ingests the PR's whole history, and a stale
            #     finding proves nothing about a new placeholder.
            same_submission_finding = any(
                _same_instant(stamp, comment.created_at)
                for stamp in (batch_finding_stamps or {}).get(bot, ()))
            if comment.path or comment.diff_hunk or same_submission_finding:
                # Body is shielded — the bot demonstrably reviewed — but keep a
                # flag so detect_clean_review is skipped below. An errored body
                # that also contains "no issues found" phrasing must NOT be read
                # as an approval: the bot errored, and its inline findings are
                # the actual review output.
                signal = None
                shielded_errored_body = True
            else:
                shielded_errored_body = False
        else:
            shielded_errored_body = False
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
        if (not shielded_errored_body
                and detectors.detect_clean_review(comment.text, llm_json=self.clean_llm)):
            if superseded:
                # A stale sign-off that a NEWER message from the same bot supersedes
                # is not a verdict — and it is not work either. It must RETURN here,
                # not fall through: an actionable return would put the bot in
                # _preflight_responders (dropping it from round 1's summon AND poll)
                # and let the round-end promotion fold it into `done` on the strength
                # of the very sign-off this flag exists to ignore. Its newer,
                # substantive message is what the loop acts on.
                return None
            self.done.add(bot)
            self.approved.add(bot)  # sticky: a later hard signal must not
            st.signal = detectors.SIGNAL_CLEAN  # demote the sign-off's label
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

    def _promote_reviewed_no_findings(self, actionable: Sequence[Comment],
                                      results: Sequence[CommentResult]) -> None:
        """A GENUINE review with zero findings is done for the run — the same
        scheduling as an explicit clean sentinel, but a distinct label: with no
        explicit sign-off it renders "Reviewed — no findings ✓", never the
        sentinel's "Approved 👍". A bot
        qualifies when everything it posted this round is a top-level review body
        (no inline comment) and every one of those bodies classified into the
        discard labels (OUTDATED / INVALID) — i.e. the round generated NO work
        from it: nothing dispatched to the fixer, nothing escalated, nothing
        failed classification. It is promoted to voluntarily-done, so later
        rounds neither re-summon it (re-pinging the cleanest response of all,
        burning reviewer credits) nor render it back to "active" as if it had
        never responded.

        A placeholder can never qualify: quota / PR-too-large / errored bodies
        detect as signals upstream in ``_classify_signal`` and return before the
        actionable add, so they never reach this scan or ``reviewed_ever``; an
        "I wasn't able to review …" apology phrased past those regexes is caught
        by :data:`_NOT_A_REVIEW_RE` here — it keeps today's behaviour (expected,
        re-summoned, never labelled). The placeholder and errored-comeback
        machinery itself is untouched."""
        verdict: Dict[str, bool] = {}
        for c, r in zip(actionable, results):
            bot = detectors.bot_for_login(c.source)
            if bot is None:
                continue
            no_finding = (not c.path and not c.diff_hunk
                          and not _NOT_A_REVIEW_RE.search(c.text)
                          and r.classification.label in DISCARD_LABELS)
            verdict[bot] = verdict.get(bot, True) and no_finding
        for bot in _canonical([b for b, ok in verdict.items() if ok]):
            if bot in self.done or self._bot_state(bot).signal is not None:
                continue
            if bot not in self.reviewed_ever:
                continue  # only a genuine review promotes — never mere chatter
            self.done.add(bot)
            print(f"[round] → excluding {bot} from subsequent rounds this run "
                  f"(reviewed — no findings)")

    def _maybe_errored_comeback(self, comment: Comment, result: CommentResult) -> None:
        """An errored-excluded bot comes back ONLY on a comment carrying REVIEW
        OUTPUT — classified SUBSTANTIVE or COSMETIC (a cosmetic finding proves
        the bot produced a review just as a substantive one does) — that is not
        older than its recorded error signal: strictly newer, or the SAME
        instant (a review submission stamps its body and inline comments alike,
        so an equal stamp from the same bot is same-review evidence; see
        :func:`_same_instant`). Runs post-classification, when the label is
        known. Older / missing / unparseable stamps keep it excluded
        (conservative); OUTDATED / INVALID / question output never retracts."""
        bot = detectors.bot_for_login(comment.source)
        if bot is None:
            return
        st = self._bot_state(bot)
        if st.error_created_at is None:
            return
        if result.classification.label not in ("SUBSTANTIVE", "COSMETIC"):
            return
        # An EDITED comment can prove recovery by its edit time, so the
        # candidate stamp is updated_at-then-created_at; the recorded error
        # stamp stays created_at. The recency gate is unchanged (conservative):
        # a strictly-older comment never retracts a newer error.
        candidate = comment.updated_at or comment.created_at
        if (_strictly_newer(candidate, st.error_created_at)
                or _same_instant(candidate, st.error_created_at)):
            self.store.errored_comeback(bot)
            st.signal = None
            st.error_created_at = None
            self.notice("errored-comeback", f"{bot} posted review output at or "
                        "after its error signal — back in the re-request gate",
                        status="done")

    def _wait_for_quiescence(self, expected: Sequence[str], round_start: float,
                             preseen: Iterable[str] = ()) -> List[Comment]:
        # Round-scope the silence timer: a re-requested bot is "not seen yet THIS
        # round" and must be held to MIN_BOT_WAIT, never instantly quiesced on a
        # last_seen stamp left over from a prior round. Without this every round
        # ≥2 would close on its first poll and could auto-merge un-reviewed
        # (the signal/done/excluded short-circuits in `_quiesced` are unaffected).
        # EVERY tracked reviewer resets, not just the expected set: the round
        # summary reads the stamp as "posted THIS round", so a not-summoned
        # bot's stale stamp must never render "Active ✅" in a round it sat out.
        for st in self.bots.values():
            st.last_seen = None
        # ``preseen`` bots (chatter-only preflight responders) DID speak before the
        # round — stamp them seen at round_start so they quiesce on the normal
        # window instead of holding the round open as a never-seen bot would. A
        # real review arriving during that window still resets the timer normally.
        for b in preseen:
            self._bot_state(b).last_seen = round_start
        actionable: List[Comment] = []
        # Reset the round's new-comment record (feeds the between-rounds quota
        # re-check). Every comment the poll ingests this round lands here.
        self._round_new_comments = []
        last_activity = round_start
        while True:
            now = self.clock()
            fresh = self._ingest_new()
            self._round_new_comments.extend(fresh)
            # Workflow-surfaced Claude usage-limit marker (managed-version 2).
            # Scanned on the fresh batch (author-pinned to github-actions[bot],
            # which bot_for_login never maps, so the classify loop below can
            # never see it); _ingest_new's processed_ids de-dup makes it fire
            # exactly once, so no time-window freshness gate is needed.
            self._scan_unavailable_markers(fresh)
            # Inline-finding stamps per bot in this batch: a review submission
            # lands its body and inline comments together with the SAME
            # created_at, so the body of a findings-bearing review must not be
            # recorded as an error placeholder even when its text trips the
            # errored regex. Stamps, not a bot set — see _classify_signal.
            finding_stamps: Dict[str, List[Optional[str]]] = {}
            for c in fresh:
                if c.path or c.diff_hunk:
                    b = detectors.bot_for_login(c.source)
                    if b is not None:
                        finding_stamps.setdefault(b, []).append(c.created_at)
            for c in fresh:
                bot = self._classify_signal(c, now, batch_finding_stamps=finding_stamps)
                if bot is not None:
                    actionable.append(c)
            # A fresh +1 reaction (no comment) is a voluntarily-done signal — fold
            # it AFTER the comment classify so a hard signal on a comment wins, and
            # so the reacting bot quiesces (its round-open hold is released).
            # Skip the gh API call when no expected reviewer is still pending
            # (no signal, not done, not excluded) — it cannot produce a fold.
            if any(
                self._bot_state(b).signal is None
                and b not in self.done
                and not self.store.is_excluded(b)
                for b in expected
            ):
                reacted = self._fold_reactions(now)
            else:
                reacted = False
            if fresh or reacted:
                last_activity = now
            if all(self._quiesced(b, now, round_start) for b in expected):
                return actionable
            # The idle-timeout only bounds the round AFTER MIN_BOT_WAIT — a
            # never-seen bot (which now holds the round open) still gets its
            # minimum wait before an idle window can close the round out from
            # under it.
            if ((now - round_start) >= self.times.min_bot_wait
                    and (now - last_activity) >= self.times.idle_timeout):
                self.notice("round-wait", "idle timeout — closing the round", status="fallback")
                return actionable
            if (now - round_start) >= self.times.max_wait_total:
                self.notice("round-wait", "max round wait reached — closing the round",
                            status="fallback")
                return actionable
            self.sleep(self.times.poll_interval)

    def _update_polishing(self, actionable: Sequence[Comment],
                          results: Sequence[CommentResult],
                          actions: Sequence[ActionResult] = ()) -> None:
        """Round-end demotions (both soft, --rr-clearable):

        * **polish** — a reviewer whose comments this round were ALL
          non-substantive (none SUBSTANTIVE / BUSINESS_QUESTION /
          CLASSIFICATION_FAILED) has nothing left to fix; dropped from
          re-request for the rest of the run.
        * **reviewed — no change** — a reviewer whose real findings this round
          all ended dismissed with NO change applied (``final`` in
          ``{"skipped-invalid", "skipped-already-fixed"}``): the fixer judged
          the comment invalid / already-fixed / not applicable via a genuine
          validity judgment. A substantive comment the loop decided not to act
          on is not cosmetic — it gets its own label — and re-asking that
          reviewer would loop it against the same verdict, so it too is dropped
          from re-request. A fix-verify REJECT is NOT a dismissal (``final ==
          "rejected"``): the finding still stands, so its reviewer keeps its
          re-request slot and the REJECT is escalated at the round-level gate.

        A finding that was FIXED, rejected, escalated, or deferred keeps its
        reviewer in the re-request gate (a fix earns a re-review; a REJECT/
        escalation is still pending); CLASSIFICATION_FAILED counts as a real finding
        unconditionally (it rides the escalation exit). A reviewer that went
        voluntarily-done or is hard-excluded (quota / PR-too-large / errored)
        is never demoted here.

        On an ``--rr-active`` restart a deferred responder's pre-existing comments ARE
        its round-1 verdict, so they demote it exactly like any round: cosmetic-only →
        polish (left alone next round), dismissed real findings → reviewed-no-change,
        a surviving finding → keeps its slot and is re-requested by ``expected_bots()``.
        That is how the restart needs no separate summon debt."""
        commented: Set[str] = set()
        surviving: Set[str] = set()   # ≥1 real finding still standing
        dismissed: Set[str] = set()   # ≥1 real finding reassessed away
        finals = {a.comment_id: a.final for a in actions}
        for c, r in zip(actionable, results):
            bot = detectors.bot_for_login(c.source)
            if bot is None:
                continue
            commented.add(bot)
            label = r.classification.label
            if label not in _REAL_FINDING_LABELS:
                continue
            if (label != "CLASSIFICATION_FAILED"
                    and finals.get(c.id) in ("skipped-invalid", "skipped-already-fixed")):
                dismissed.add(bot)
            else:
                surviving.add(bot)
        for bot in commented - surviving:
            if bot in self.done or self.store.is_excluded(bot):
                continue  # a clean / hard-cause reason already owns this reviewer
            if bot in dismissed:
                self.reviewed_no_change.add(bot)
                print(f"[round] → excluding {bot} from subsequent rounds this "
                      f"run (reviewed — no change: every finding dismissed on "
                      f"reassessment)")
            else:
                self.polishing.add(bot)

    def _worktree_has_changes(self) -> bool:
        """True when ``git status --porcelain`` reports any change in the loop's
        worktree — the has-file-changes probe for the substantive re-review gate
        when pushing is off (with pushing on, the commit step's result is
        authoritative). Any git error → False (no change proven, conservatively)."""
        try:
            proc = self.gh_run(["git", "status", "--porcelain"], cwd=self.cwd)
        except (subprocess.SubprocessError, OSError):
            return False
        if getattr(proc, "returncode", 1) != 0:
            return False
        return bool((getattr(proc, "stdout", "") or "").strip())

    # --------------------------------------------------- --rr-active restart

    def _head_sha(self) -> str:
        """This PR's live tip sha, or ``""`` when it cannot be read."""
        return _pr_head_sha(self.pr, self.repo, self.cwd, self.gh_run)

    def _rr_active_restore(self) -> None:
        """Reconstruct, before round 1, every verdict an ``--rr-active`` restart
        already holds — so the loop never spends a summon, a register delay, and a
        poll window re-asking a reviewer it has already heard from.

        Three verdicts are recovered, in this order:

        * **approved** — a reviewer whose LATEST message on the PR is a clean
          review, or that signed off with a bare ``+1``, is folded voluntarily-done
          (which alone suppresses both its summon and its poll). Re-derived from
          GitHub, never from disk: approvals live on the PR. This MUST precede the
          preflight snapshot below, whose reaction baseline stamps every
          pre-existing ``+1`` stale.
        * **polish-only** — restored from the persisted per-PR state, and ONLY when
          the PR's live HEAD still equals the tip that state was stamped against
          (see :mod:`buddhi_review.polish_state`).
        * **already-responded** — the preflight snapshot folds the comments already on
          the PR, so a responder is
          neither re-summoned nor polled in round 1; its pre-existing comments are
          processed as its round-1 verdict instead. After that NOTHING is special: the
          existing ``expected_bots()`` + end-of-round rules decide round 2 — a
          substantive finding re-requests its bot to verify the fix, a cosmetic one
          lands in ``self.polishing`` and is left alone, an approval is done. There is
          no summon debt; the correct behaviour falls out of the rules the loop already
          runs.

        The restored / re-derived reviewers are folded into ``reviewed_ever``: they
        genuinely reviewed this PR, so a restart of an all-approved / all-polish PR
        must still clear the never-merge-unreviewed gate and auto-merge."""
        approved = self._rederive_prior_approvals()
        restored = self._restore_polish_state()
        deferred: Set[str] = set()
        # Run the restart snapshot at EVERY max_rounds — it is what defers a
        # verdict-in-hand bot out of round 1's summon and poll. The restart principle
        # ("round 1 summons only active bots whose verdict is not in hand") carries no
        # round-budget condition: at max_rounds == 1, summoning a deferred bot would
        # only re-review the OLD head anyway (round 1 is the last round), so deferring
        # is strictly more efficient with no safety change, and the reconstruction runs
        # unconditionally on the restart path. After the snapshot NOTHING is
        # special: expected_bots() and the round-end rules decide any further round — a
        # substantive comment re-requests its bot (it is in none of the exclusion sets),
        # a cosmetic one lands in self.polishing and is left alone, an approval is done.
        # No summon debt.
        if self.preflight:
            self._preflight_snapshot(restart=True)
            deferred = set(self._preflight_responders)
            # A HARD CAUSE always wins over a re-derived verdict. The snapshot reads
            # every comment through the full exclusion path, so it can hard-exclude
            # (quota / PR-too-large / errored) a reviewer whose message the re-derive
            # above read as a sign-off — a failure placeholder states its own zero
            # output and so reads "clean" on its own text. Un-crown such a reviewer
            # here, BEFORE the reviewed_ever fold below: a placeholder is a response,
            # not a review, and counting it would satisfy the never-merge-unreviewed
            # gate and merge a PR nobody actually looked at.
            for bot in sorted(approved | restored):
                if not self.store.is_excluded(bot):
                    continue
                approved.discard(bot)
                restored.discard(bot)
                self.done.discard(bot)
                self.approved.discard(bot)
                self.polishing.discard(bot)
        fleet = set(self._run_start_fleet)
        # A preserved approval / polish verdict IS a genuine review (restricted to
        # the run-start fleet, mirroring the gate's own universe). Without this fold
        # an all-approved restart reaches the clean exit with an empty reviewed_ever
        # and the SAFETY gate would block the very auto-merge it should allow.
        self.reviewed_ever |= (approved | restored) & fleet
        parts = []
        if approved:
            parts.append(f"approved={sorted(approved)}")
        if restored:
            parts.append(f"polish-only={sorted(restored)}")
        if deferred:
            parts.append(f"already-responded={sorted(deferred)}")
        if parts:
            self.notice("rr-active-restore",
                        f"PR #{self.pr}: not re-asking reviewers whose verdict is "
                        f"already in hand — {'; '.join(parts)}", status="skip",
                        hint="re-run with --rr to re-ping every reviewer anyway")

    def _rederive_prior_approvals(self) -> Set[str]:
        """Fold every reviewer that has ALREADY signed off into voluntarily-done,
        re-derived live from GitHub. Returns the set folded here.

        **Latest message wins.** A reviewer's most-recent message across all three
        channels (inline comments, review bodies, PR conversation) decides: only a
        LATEST message that is a clean review folds it. A stale LGTM followed by
        substantive feedback — including an inline comment, the channel a
        conversation-only scan would miss — must NOT silence a reviewer that is
        still engaged. A bare ``+1`` (the sign-off of a reviewer that posts no
        message at all) folds on its own; a ``+1`` alongside a substantive latest
        message does not (reactions carry no timestamp, so they can never outrank a
        message that does).

        Deliberately NOT ``_fold_reactions``: that reads ``+1`` reactions only, and
        fails closed while the reaction baseline is unset — which it is here, since
        this runs BEFORE the preflight snapshot captures it. Fail-soft on a reader
        error: whatever could not be read simply is not folded (the reviewer is
        re-summoned — the safe direction), with a warning naming the source."""
        fleet = set(self._run_start_fleet)
        if not fleet:
            return set()
        failures: List[str] = []
        latest: Dict[str, Tuple[Optional[str], str]] = {}   # bot -> (created_at, text)
        comments_read = True
        try:
            comments = self.fetch(self.pr, repo=self.repo, cwd=self.cwd)
        except Exception:
            comments = []
            comments_read = False
            failures.append("comments")
        for c in comments:
            bot = detectors.bot_for_login(c.source)
            if bot is None:
                continue
            known = latest.get(bot)
            if known is None or _supersedes(c.created_at, known[0]):
                latest[bot] = (c.created_at, c.text or "")
        plus_one: Set[str] = set()
        try:
            for r in self.fetch_reactions(self.pr, repo=self.repo, cwd=self.cwd):
                if r.content != "+1":
                    continue
                b = detectors.bot_for_login(r.source)
                if b is not None:
                    plus_one.add(b)
        except Exception:
            failures.append("reactions")
        if failures:
            self.notice("rr-active-restore",
                        f"could not read {' + '.join(failures)} — a prior approval "
                        f"from that source is not preserved; the reviewer is "
                        f"re-summoned", status="fallback")
        folded: Set[str] = set()
        for bot in sorted(fleet):
            st = self._bot_state(bot)
            # Defer to any recorded state, exactly like the clean-review path: a
            # hard cause (quota / PR-too-large / errored) or an existing sign-off
            # is never overwritten by this fold.
            if st.signal is not None or bot in self.done or self.store.is_excluded(bot):
                continue
            newest = latest.get(bot)
            if newest is None:
                # A bare +1 signs off only when the loop KNOWS the bot posted no
                # message — i.e. the comment read succeeded. If that read FAILED,
                # "no message" is ignorance, not evidence: the unread message could
                # be a failure placeholder or a fresh finding, and a +1 must never
                # crown such a reviewer "Approved" (which would satisfy the
                # never-merge-unreviewed gate). Fail closed → it is re-summoned.
                signed_off = comments_read and bot in plus_one
            else:
                signed_off = self._is_sign_off(newest[1])
            if not signed_off:
                continue
            self.done.add(bot)
            self.approved.add(bot)
            st.signal = detectors.SIGNAL_CLEAN
            self.responded_ever.add(bot)
            if newest is None:
                # Folded on a bare +1, so NO text of this bot was ever quota-checked.
                # Mark it reaction-done exactly as the poll's own +1 fold does, so the
                # between-rounds quota re-check still reconsiders anything it posts
                # later: a novel-wording quota message must be able to evict this
                # sign-off rather than ride it into the merge gate.
                self._reaction_done.add(bot)
            folded.add(bot)
            print(f"[rr-active] {bot}: already approved this PR — not re-requesting")
        return folded

    def _is_sign_off(self, text: str) -> bool:
        """True only for a message that is a GENUINE clean review — the same
        signal-first precedence ``_classify_signal`` applies, with the same
        classification context, and for the same reason: a FAILURE placeholder states
        its own zero output ("the review run failed; no comments were posted", "I've
        used all of my requests for this month, so no comments were generated") and
        therefore reads CLEAN to the clean-review detector on its own. A placeholder
        is a response, not a review — crowning it "Approved" would satisfy the
        never-merge-unreviewed gate and auto-merge a PR nobody reviewed.

        The quota model tier (``quota_llm``) and the PR subject are passed exactly as
        the poll passes them: the deterministic regexes miss real quota wordings that
        only the model tier catches, and a detector that is BLINDER here than the one
        the preflight snapshot uses would crown a bot the snapshot then hard-excludes.
        Any hard signal (quota / PR-too-large / errored), or the "wasn't able to
        review" apology those regexes do not catch, is therefore NOT a sign-off."""
        if self.quota_llm is not None:
            pr_title, pr_body = self._fetch_pr_title_body()
        else:
            pr_title, pr_body = None, None
        if detectors.detect_signal(text, quota_llm=self.quota_llm,
                                   pr_title=pr_title, pr_body=pr_body) is not None:
            return False
        if _NOT_A_REVIEW_RE.search(text or ""):
            return False
        return detectors.detect_clean_review(text, llm_json=self.clean_llm)

    def _restore_polish_state(self) -> Set[str]:
        """Restore the persisted polish-only reviewers, but ONLY when the PR's live
        HEAD still equals the tip they were stamped against. Returns the set
        restored (empty when the tip is unknown, no state exists, the state is
        unreadable, or HEAD has moved).

        HEAD-guarded because the polish verdict is tied to the diff the reviewer
        actually saw: once HEAD advances past it (a human's commit, a rebase, a
        fix from a run whose stamp never landed) the reviewer may have real
        findings on the new code, so it is re-summoned rather than restored."""
        head = self._head_sha()
        if not head:
            return set()   # unknown live HEAD → never restore (fail-closed)
        state = polish_state.read_polish_state(self.pr, self.repo)
        if not state or state.get("tip_sha") != head:
            return set()
        # Restricted to the run-start fleet: a stale file must never resurrect a
        # reviewer the operator has since disabled, nor override a hard exclusion.
        restored = {b for b in state["bots"]
                    if b in self._run_start_fleet and b not in self.done
                    and self._bot_state(b).signal is None
                    and not self.store.is_excluded(b)}
        self.polishing |= restored
        for bot in sorted(restored):
            print(f"[rr-active] {bot}: polish-only at this HEAD — not re-requesting")
        return restored

    def _persist_polish_state(self) -> None:
        """Stamp the run's CURRENT polish-only set against the tip this round
        leaves behind — called at every round end, AFTER the round's fixes are
        pushed, so the tip is the one the loop carries into the next round (and the
        one a restart would meet as live HEAD). Fail-closed: an unreadable tip
        writes nothing, so a later restore can never match a stamp taken on an
        unknown head. Best-effort — a failed write only costs a re-summon."""
        tip = self._head_sha()
        if not tip:
            return
        polish_state.write_polish_state(self.pr, self.repo, tip, sorted(self.polishing))

    # ------------------------------------------------------------------- run

    def _preflight_snapshot(self, restart: bool = False) -> None:
        """Before round 1, ingest every review/comment already on the PR and fold
        it through the SAME classification path the poll uses, so the loop starts
        already knowing who responded. Seeds ``self.done`` / ``self.approved``
        (clean reviews), the store's hard buckets (quota / PR-too-large / errored),
        ``reviewed_ever`` / ``responded_ever``, and ``self._rate_limited_until`` —
        reusing the poll's own code, so preflight and the poll can never diverge in
        how they read a comment. Actionable pre-existing comments are collected
        into ``self._preflight_batch`` (processed in round 1 with no poll wait);
        every responding bot is recorded in ``self._preflight_responders`` so
        round 1 neither re-summons nor waits on a reviewer that already spoke.

        Runs on a default launch AND on the ``--rr-active`` RESTART path (through
        ``_rr_active_restore``, at every ``max_rounds``) — there its responders are
        deferred out of round 1's summon and poll, but never out of the RUN: their
        pre-existing comments are processed as their round-1 verdict, and the existing
        end-of-round re-request rules then decide round 2 (a substantive finding →
        re-requested to verify the fix, a cosmetic one → polish, an approval → done).
        ``--rr`` and ``--rr-none`` skip it and keep their round-1 semantics untouched.
        ``processed_ids`` de-dup guarantees the round-1 poll never re-processes a folded
        comment, and the marker scan here fires at most once across preflight + poll.

        ``restart`` — the ``--rr-active`` RESTART reading of the PR's history, and the
        ONLY caller that gets the two rules below. A default launch is left exactly as
        it was, deliberately: both rules depend on round 1 actually SUMMONING the
        reviewer they release, which only ``--rr`` / ``--rr-active`` do (a default
        round 1 summons ``auto_on_open: false`` reviewers only, so an ``auto_on_open``
        reviewer released here would be polled-but-never-asked — silently silent for a
        whole round).

        * **Already-resolved inline findings are skipped.** On a restart they are
          FINISHED work, not a fresh verdict: folding them would defer their author
          out of round 1, re-fix its stale comments to "already fixed", demote it to
          reviewed-no-change, and drop it from re-request for the whole run — the
          reviewer would never be asked at all.
        * **Latest message wins for the clean fold** (``superseded``). Preflight reads
          the whole history at once in ENDPOINT order, which is not chronological, so
          an old "LGTM" could otherwise crown a reviewer voluntarily-done even though
          it has since posted real findings."""
        now = self.clock()
        fresh = self._ingest_new()
        # Establish the stale-reaction baseline: every reaction already on the PR
        # predates the loop's first re-request, so a +1 here never marks a bot done.
        self._capture_reaction_baseline()
        if not fresh:
            return
        # RESTART only (see the docstring): skipping a resolved root keeps its author
        # OUT of the responder set, and round 1 under --rr-active summons the whole
        # expected fleet — so the reviewer is genuinely asked about the current head
        # rather than replaying finished work. A default launch does NOT summon an
        # auto_on_open reviewer in round 1, so releasing it there would leave it
        # polled-but-never-asked; that path keeps its existing fold.
        resolved_roots = self._resolved_thread_roots() if restart else set()
        skipped_resolved = 0
        # Honour a pre-existing rate-limit marker BEFORE round 1 (so claude is
        # released ahead of the poll, not first seen inside it). Same fresh batch,
        # so _ingest_new's processed_ids de-dup makes it fire exactly once.
        self._scan_unavailable_markers(fresh)
        # Per-bot inline-finding stamps (identical to the poll) for the errored
        # record-time completed-review check.
        finding_stamps: Dict[str, List[Optional[str]]] = {}
        # Each bot's most-recent stamp in this batch — the RESTART-only
        # LATEST-MESSAGE-WINS rule for the clean fold (see the docstring).
        newest: Dict[str, Optional[str]] = {}
        for c in fresh:
            b = detectors.bot_for_login(c.source)
            if b is None:
                continue
            if c.path or c.diff_hunk:
                finding_stamps.setdefault(b, []).append(c.created_at)
            if restart and _supersedes(c.created_at, newest.get(b)):
                newest[b] = c.created_at
        for c in fresh:
            # Only INLINE comments are review-thread roots, so only they can be
            # matched against the resolved set. A review body / issue-channel
            # comment (a clean sentinel, a quota marker) has no thread and folds
            # normally — no sign-off, exclusion, or reviewed_ever seeding is lost.
            if c.path and c.id in resolved_roots:
                skipped_resolved += 1
                # Resolution ends the FINDING, not the reviewer's stated inability to
                # review: a quota / PR-too-large placeholder is still true after
                # someone ticks its thread resolved. Fold the hard signal (which
                # excludes the bot) but never the work — the comment itself is done.
                self._fold_hard_signal(c)
                continue
            rb = detectors.bot_for_login(c.source)
            # _supersedes, NOT _strictly_newer — the same predicate that BUILT
            # ``newest``, so the rule is symmetric. It differs in exactly one case:
            # an UNDATED comment (a degraded payload — GitHub always stamps). There a
            # dated message must still win, or an undated stale "LGTM" would fold its
            # author voluntarily-done, and the reviewer whose newest message is a real
            # finding would never be summoned at all.
            bot = self._classify_signal(
                c, now, batch_finding_stamps=finding_stamps,
                superseded=rb is not None and _supersedes(newest.get(rb), c.created_at))
            if bot is not None:
                self._preflight_batch.append(c)
            if rb is not None:
                # Posted something at preflight → responded (protects it from the
                # persistent-silent warning), and was SEEN (so round 1 treats it
                # like a bot that already spoke this round — it quiesces on the
                # normal window instead of holding the round open as a never-seen
                # bot would).
                self.responded_ever.add(rb)
                self._preflight_seen.add(rb)
                # But it counts as an already-given VERDICT — one round 1 need not
                # wait on — only if it reached a terminal state (a clean sign-off,
                # a hard/rate exclusion) or handed over an actionable finding.
                # Mere issue-channel chatter is NOT a verdict: that bot stays in
                # the round-1 poll so its real review is still awaited.
                if (bot is not None or rb in self.done
                        or self.store.is_excluded(rb) or rb in self._rate_limited_until):
                    self._preflight_responders.add(rb)
        # The between-round quota re-check must cover the preflight batch too: a
        # novel-wording quota message already on the PR would otherwise be recorded
        # as a genuine review and satisfy the never-merge gate. Run it BEFORE the
        # baseline is seeded (the re-check skips already-baselined ids), so the same
        # safety net the poll path enjoys applies to pre-existing comments.
        self._recheck_quota_between_rounds(fresh)
        # These pre-existing comments are round 1's baseline — new comments that
        # arrive after are attributed to (and re-checked in) the round they land in.
        self._note_baseline(fresh)
        # last_seen is a round-scoped stamp; clear the marks the fold set so the
        # first real round starts clean (round 1's poll resets them too, but a
        # round-1 fast exit — empty fleet — skips that reset).
        for st in self.bots.values():
            st.last_seen = None
        if skipped_resolved:
            print(f"[preflight] {skipped_resolved} already-resolved comment(s) "
                  f"skipped — their reviewers are summoned normally")
        if self._preflight_batch:
            print(f"[preflight] {len(self._preflight_batch)} pre-existing comment(s) "
                  f"to process in round 1 without waiting")

    def _fold_hard_signal(self, comment: Comment) -> None:
        """Apply ONLY the hard-cause exclusions (quota / PR-too-large) from a comment
        whose WORK is finished — an inline finding on a RESOLVED review thread, which
        the preflight fold skips. The exclusion outlives the resolution: a reviewer
        that said it was out of quota is still out of quota, and re-summoning it would
        burn a round on a bot that cannot answer.

        ERRORED is deliberately not recordable here, exactly as in ``_classify_signal``:
        an INLINE comment IS review output, so a body that trips the errored regex on a
        thread root is a finding, never a failure placeholder — and only inline roots
        reach this method."""
        bot = detectors.bot_for_login(comment.source)
        if bot is None:
            return
        st = self._bot_state(bot)
        if st.signal is not None or self.store.is_excluded(bot):
            return  # a cause is already recorded — never overwrite it
        if self.quota_llm is not None:
            pr_title, pr_body = self._fetch_pr_title_body()
        else:
            pr_title, pr_body = None, None
        signal = detectors.detect_signal(
            comment.text, quota_llm=self.quota_llm, pr_title=pr_title, pr_body=pr_body)
        if signal == detectors.SIGNAL_QUOTA:
            self.store.exclude_quota(bot)
            st.signal = signal
            self.notice("exclusion", f"{bot} excluded for the run: quota exhausted",
                        status="skip")
        elif signal == detectors.SIGNAL_PR_TOO_LARGE:
            self.store.exclude_pr_too_large(bot)
            st.signal = signal
            self.notice("exclusion", f"{bot} excluded for the run: PR too large",
                        status="skip")

    def _resolved_thread_roots(self) -> Set[str]:
        """The root-comment ids of every review thread GitHub reports RESOLVED.
        Fail-SOFT: any reader error (a gh/GraphQL blip, no owner/repo configured,
        a malformed node) degrades to an empty set — i.e. exactly today's
        behaviour, where no comment is filtered — and never crashes the preflight
        fold."""
        try:
            threads = self.fetch_threads(self.pr, repo=self.repo, cwd=self.cwd)
            return {t.root_comment_id for t in threads
                    if t.is_resolved and t.root_comment_id is not None}
        except Exception:
            return set()

    def _populate_repo_gate(self) -> None:
        """Populate ``self._repo_gate_excluded`` ONCE, before round 1, from the
        per-reviewer availability probe. Only Claude is reliably detectable with
        the loop's user token (a Contents-API GET of claude-code-review.yml), so
        an ABSENT workflow excludes "claude" — its idle row badges "Not configured
        (repo) 🔧". Copilot/Codex/Gemini are NEVER probed here and never enter the
        set: Copilot's summon 422 is overloaded (enabled-but-busy vs not-enabled,
        and it can succeed-silently — cli/cli#11245) and a Codex/Gemini App
        install can only be read with an App JWT the loop's user token cannot mint
        (GET repos/{repo}/installation → 404), so their silence is the ONLY honest
        signal. Monotonic for the run: guarded so it never re-evaluates or flips a
        badge mid-round. Fail-closed — the probe treats any gh/auth/timeout error
        as an absent workflow."""
        if self._repo_gate_probed:
            return
        self._repo_gate_probed = True
        # Skip the gh probe entirely when Claude is not in the enabled fleet —
        # _bot_status_text's "not-requested" path never reads _repo_gate_excluded.
        if "claude" not in active_reviewers(self.cfg, self.repo):
            return
        if not detectors.detect_claude_workflow_present(
                self.repo, cwd=self.cwd, run=self.gh_run):
            self._repo_gate_excluded.add("claude")

    def run(self) -> RunOutcome:
        """Snapshot the run-start fleet (for the SAFETY gate's empty-vs-silent
        distinction), drive the round loop, and ALWAYS emit the persistent
        silent-reviewer warning at run end (every exit path, via ``finally``)."""
        # Probe per-reviewer availability ONCE, before round 1 — its result is
        # monotonic for the run (see _populate_repo_gate).
        self._populate_repo_gate()
        if self.rr:
            # --rr re-pings everyone: clear the SOFT exclusions (voluntarily-done
            # + polish-only + reviewed-no-change) so a previously-satisfied
            # reviewer is summoned again. The hard buckets (quota / PR-too-large
            # / errored) are never cleared.
            self.done.clear()
            self._reaction_done.clear()
            self.approved.clear()
            self.polishing.clear()
            self.reviewed_no_change.clear()
        # Snapshot the run-start fleet BEFORE preflight folds responders into
        # done/exclusions, so the SAFETY gate still measures the FULL expected
        # fleet: a PR everyone already approved has an empty round-1 expected set
        # but a non-empty run-start fleet + reviewed_ever, so it merges (case c)
        # rather than reading as "no reviewers configured" (case a).
        self._run_start_fleet = set(self.expected_bots())
        if self.preflight and not (self.rr or self.rr_active or self.rr_none):
            self._preflight_snapshot()
        elif self.preflight and self.rr_active:
            # --rr-active is the RESTART flag: reconstruct what the killed run
            # already knew, so no reviewer whose verdict is already in hand is
            # re-asked. Runs strictly AFTER the run-start fleet snapshot above —
            # folding first would shrink the fleet and silently weaken the
            # never-merge-unreviewed SAFETY gate at the clean exit.
            self._rr_active_restore()
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
            self._apply_rate_limit_comeback()
            expected = _canonical(self.expected_bots())
            # Round 1 consumes the preflight batch — actionable comments already on
            # the PR, folded through _classify_signal at run start. Consumed once;
            # later rounds always see an empty batch.
            preflight_batch: List[Comment] = []
            if round_no == 1 and self._preflight_batch:
                preflight_batch = self._preflight_batch
                self._preflight_batch = []
            # A round with no expected reviewer AND no pre-existing work left is a
            # clean finish. Preflight having folded every responder into
            # done/exclusions is exactly how an already-reviewed PR reaches this
            # with round_no == 1, so the poll wait is skipped entirely.
            if not expected and not self.rr_none and not preflight_batch:
                if self.rr_active and round_no == 1:
                    print("[round] --rr-active: no still-active reviewers — clean exit")
                return self._clean_exit(round_no - 1)

            if self.rr_none:
                # --rr-none: no reviewer is summoned or polled (expected is empty).
                # The first _wait_for_quiescence ingest still returns the comments
                # already on the PR, and all([]) quiesces the round instantly, so
                # existing comments are fixed and the run then merges via the
                # _clean_exit rr-none gates. Skip the skipped-reviewer log + the
                # "expecting" banner: every reviewer is sidelined on purpose, which
                # is not the silent disappearance those lines exist to explain.
                if round_no == 1:
                    print("[round] --rr-none: summoning no reviewers — fixing any "
                          "comments already on the PR, then merging on a clean exit")
            else:
                self._log_skipped(expected)  # honest skip-reason logging
                print(f"[round] Round {round_no} of {self.max_rounds} — expecting: "
                      f"{', '.join(expected)}")
            # Round 1 neither re-summons nor waits on a preflight responder — it
            # already gave its verdict this run and will not re-post until it is
            # re-requested after a fix, so polling it would burn the min-bot wait
            # on an already-reviewed PR. Later rounds re-request + poll it as usual.
            poll_expected = ([b for b in expected if b not in self._preflight_responders]
                             if round_no == 1 else list(expected))
            # Snapshot the stale-reaction set before re-requesting: a +1 already on
            # the PR is stale; one arriving after the re-request is a fresh signal.
            self._capture_reaction_baseline()
            self._summon(round_no, poll_expected)
            # A chatter-only preflight bot that is still polled this round was seen
            # before it (it spoke at preflight), so it must not hold the round open
            # as a never-seen bot — stamp it seen at the poll's start.
            preseen = (self._preflight_seen & set(poll_expected)) if round_no == 1 else ()
            polled = self._wait_for_quiescence(poll_expected, self.clock(), preseen=preseen)
            self._record_round_attendance(poll_expected)  # silent-streak bookkeeping

            # Pre-existing comments need no poll window — they ride this round with
            # whatever the poll surfaced.
            actionable = list(preflight_batch) + polled
            # Their authors posted (at preflight); stamp them seen so the round
            # table renders them engaged rather than silent.
            for c in preflight_batch:
                b = detectors.bot_for_login(c.source)
                if b is not None:
                    self._bot_state(b).last_seen = self.clock()
            # Between-round quota re-check over this round's new-since-baseline
            # items, then fold those ids into the baseline for the next round.
            self._recheck_quota_between_rounds(self._round_new_comments)
            self._note_baseline(self._round_new_comments)

            if not actionable:
                self._render_round(round_no, [], [], expected)  # status-only round summary
                return self._clean_exit(round_no)

            results = process_comments(
                actionable, adapter=self.adapter, classify_runner=self.classify_runner,
                max_rounds=self.max_rounds,
            )
            round_actions = []
            for c, r in zip(actionable, results):
                self._maybe_errored_comeback(c, r)  # review-output retract (subst/cosmetic)
                round_actions.append(
                    act_on_result(c, r, adapter=self.adapter, fix_dispatch=self.fix_dispatch))
            self.actions.extend(round_actions)
            # Record which INLINE comments (path-anchored — the only comments that
            # are review-thread ROOTS) the run genuinely finished, so the pre-merge
            # thread gate resolves ONLY the run's own inline threads and can never
            # match a review-body / conversation id against a thread root.
            for c, a in zip(actionable, round_actions):
                if c.path and a.final in _RESOLVED_FINALS:
                    self._handled_inline_ids.add(a.comment_id)
            for a in round_actions:
                print(f"  [{a.final:16}] comment {a.comment_id} ({a.disposition})"
                      + (f" — {a.detail}" if a.detail else ""))
            # A summary-only genuine review (zero findings) is done for the run —
            # decided AFTER classification, BEFORE the table renders its status.
            # A deferred responder's pre-existing comments ARE its round-1 verdict on
            # the restart path, so they drive these demotions like any round's: a
            # cosmetic-only responder lands in self.polishing and is left alone next
            # round (the whole point — the loop already knows not to re-ask a polish
            # bot), and a substantive one is re-requested by expected_bots() to verify
            # the fix. That is why the deferral needs no summon debt.
            self._promote_reviewed_no_findings(actionable, results)
            # Round-end demotions BEFORE the table renders, so a reviewer about
            # to be dropped shows its actual next-round disposition (Polish-only
            # / Reviewed — no change) instead of a stale "Active": a reviewer
            # whose whole round was non-substantive has nothing left to fix, and
            # one whose real findings were ALL dismissed on reassessment must
            # not be re-asked (it would loop against the same verdict).
            self._update_polishing(actionable, results, round_actions)
            self._render_round(round_no, actionable, results, expected)  # per-reviewer round summary

            if self.adapter.escalation.delivered:
                answers = self.answer_waiter(self.adapter.escalation)
                self.adapter.escalation.delivered.clear()
                if any(k.startswith("fix-") and (v or "").strip() == "3"
                       for k, v in answers.items()):
                    print("[round] operator chose stop on a failed-fix escalation")
                    return self._handback("stopped", round_no)
                if any(v is None for v in answers.values()):
                    print("[round] unanswered escalation(s) — handing over for manual review")
                    return self._handback("needs-human", round_no)

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
                return self._handback("needs-human", round_no, rebase_skip=True)

            committed_changes = False
            if any(a.final == "fixed" for a in round_actions) and self.push:
                pushed = commit_push.commit_and_push(
                    self.cwd,
                    message=f"fix: address review comments (round {round_no})",
                    repo=self.repo,
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
                    return self._handback("stopped", round_no, rebase_skip=True)
                if pushed == "error":
                    # push failed: local commit exists but remote never got it;
                    # continuing could lead to squash_merge on stale remote code.
                    # Bucket C: local/remote diverged — never rebase/force-push it.
                    return self._handback("needs-human", round_no, rebase_skip=True)
                committed_changes = (pushed == "pushed")

            # Persist this round's polish-only verdicts against the tip the loop
            # now carries — AFTER the fixes are pushed, so the stamp names the head
            # a restart would meet. A polish-only reviewer is sticky within a run
            # (never re-summoned even as later fixes advance HEAD), so stamping the
            # POST-fix tip and restoring only at that tip reproduces exactly that
            # stickiness across a restart; a PRE-fix stamp would never match.
            self._persist_polish_state()

            # ── Substantive-progress gate ─────────────────────────────────────
            # Request another review round ONLY when this round produced real
            # substantive progress: at least one SUBSTANTIVE-labeled comment whose
            # fix actually LANDED (final == "fixed") AND changed files. A cosmetic
            # / PR-description / outdated / invalid-only round — or a substantive
            # comment the fixer skipped, or a substantive fix that changed nothing
            # — is a clean finish: the applied fixes were committed above, so exit
            # clean without re-summoning anyone. (A verify-REJECT does NOT reach
            # here as a clean finish: it escalated at the round-level gate above,
            # so an unanswered/stop REJECT already handed back or stopped the run.)
            # The file-change check reads the commit result when pushing
            # (``pushed`` = real changes committed); with pushing off it probes
            # the worktree directly.
            round_substantive = any(
                r.classification.label == "SUBSTANTIVE" and a.final == "fixed"
                for r, a in zip(results, round_actions)
            )
            if round_substantive and (committed_changes or self._worktree_has_changes()):
                continue
            return self._clean_exit(round_no)

        # The round budget is spent and the final round completed cleanly — no
        # unanswered escalation, no poisoned worktree, no failed push, no operator
        # stop (each of those returns above). Route the exit through the normal
        # clean-exit gates (SAFETY + optional CI + merge) rather than an
        # unconditional hand-back: a run that did its budgeted work and left
        # nothing outstanding is merge-eligible exactly like a naturally-clean
        # finish.
        print(f"[round] round budget ({self.max_rounds}) reached with the final "
              f"round clean — routing through the clean-exit gates")
        return self._clean_exit(self.max_rounds)

    def _handback(self, status: str, rounds: int, *, rebase_skip: bool = False) -> RunOutcome:
        """A NON-clean hand-back (operator stop, unanswered escalation, poisoned
        worktree, failed push). Stamps the run's polish-only verdicts before
        returning: every one of these exits is followed by the operator fixing
        something and re-running — most often ``--rr-active`` right after answering an
        escalated question — and a verdict this run genuinely reached must survive
        that, or the restart re-summons the very reviewers it should skip. Stamped
        against the LIVE PR head, which is the head the restart will meet (these exits
        fire before, or instead of, the round's push)."""
        self._persist_polish_state()
        return RunOutcome(status, rounds, False, self.actions, rebase_skip=rebase_skip)

    def _clean_exit(self, rounds: int) -> RunOutcome:
        print("[round] clean — every expected reviewer is done/excluded and "
              "no actionable comments remain")
        # #g9a NOTIFICATION (never a block, never gates the flow below): if the
        # loop's primary reviewer (@claude) was expected but its trigger never
        # landed and it never reviewed — yet another reviewer did — say so loudly.
        self._maybe_warn_claude_never_reviewed()
        if self.auto_merge:
            # ── SAFETY gate (parity of the reference loop's never-merge-unreviewed
            # backstop): never auto-merge code that no expected reviewer actually
            # reviewed. Three-way distinction (the run-start fleet snapshot is the
            # discriminator) ─────────────────────────────────────────────────────
            fleet = set(self._run_start_fleet)
            if not fleet and not self.rr_none:
                # (a) zero reviewers by design → quiet no-auto-merge. There is
                #     nothing to gate on, so leaving the PR open is the safe,
                #     unalarming outcome (no review ever happened). EXCEPTION:
                #     under --rr-none the empty fleet is the operator's explicit
                #     "resolve existing comments and merge" request, so it falls
                #     through to the merge below instead of this skip.
                self.notice("squash-merge",
                            f"no reviewers configured for this repo — leaving "
                            f"PR #{self.pr} open (nothing reviewed it)",
                            status="skip",
                            hint="add reviewers via the setup wizard, or merge by hand")
                return RunOutcome("clean", rounds, False, self.actions)
            if fleet and not (fleet & self.reviewed_ever):
                # (b) reviewers were expected but NONE genuinely reviewed → loud
                #     handback + block. A quota/error placeholder is a response,
                #     not a review, so it never lands in reviewed_ever (filtered in
                #     _classify_signal) and cannot satisfy this gate. Guarded on a
                #     non-empty fleet so --rr-none (empty fleet) is never blocked
                #     here — it has already passed (a) and merges at (c).
                self._block_unreviewed_merge(fleet)
                return RunOutcome("clean", rounds, False, self.actions)
            # (c) >=1 expected reviewer genuinely reviewed (incl. a clean approval)
            #     → the merge may proceed, subject to the pre-merge gates.
            # ── Thread-resolution gate ───────────────────────────────────────────
            # GitHub must confirm zero unresolved review threads before this PR is
            # merge-ready. Runs AFTER the SAFETY gate above (so an unreviewed PR is
            # already blocked and never reaches here) and BEFORE the label /
            # non-label pre-merge fork below, so ONE gate guards both pre-merge
            # paths. It first resolves the threads THIS run genuinely handled, then
            # re-confirms; a thread the loop did not touch (a human's open thread)
            # or could not finish (a rejected fix) keeps the PR un-merge-ready.
            if not self._thread_gate_ok():
                return RunOutcome("clean", rounds, False, self.actions)
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
            else:
                # #43/#44: without label-gated CI the label path's `wait_for_ci_green`
                # never runs, so this general mergeability gate is the pre-merge guard
                # on the non-label auto-merge path — it blocks a conflicted / draft /
                # behind / blocked / red PR, and waits out an in-flight check on the
                # last fix push before merging.
                if not self._premerge_gate_ok():
                    return RunOutcome("clean", rounds, False, self.actions)
            merged = merge.squash_merge(
                self.pr, repo=self.repo, enabled=True, cwd=self.cwd,
                run=self.gh_run, notice=self.notice,
            )
            return self._merged_outcome(rounds, merged)
        # ── auto-merge OFF ──────────────────────────────────────────────────────
        # #g9b: when running attended (a TTY), offer an interactive squash-merge
        # after confirming GitHub would accept it; a headless run (nohup / CI,
        # stdin not a terminal) never prompts — no input() is called — and simply
        # leaves the PR open for the operator to land.
        if sys.stdin.isatty():
            merged = self._interactive_merge_prompt()
            return self._merged_outcome(rounds, merged)
        merged = merge.squash_merge(
            self.pr, repo=self.repo, enabled=False, cwd=self.cwd,
            run=self.gh_run, notice=self.notice,
        )
        return self._merged_outcome(rounds, merged)

    def _merged_outcome(self, rounds: int, merged: bool) -> RunOutcome:
        """The clean-exit outcome + the landed-PR cleanup: once the PR is merged its
        persisted polish state is dead, so drop the file. A PR handed BACK (blocked
        gate, red CI, an unresolved thread) keeps its state — that is the restart the
        state exists for.

        A PR the loop did not merge itself may still have LANDED — the operator merged
        it by hand while the run was finishing, or GitHub's own auto-merge did. Probe
        for that (best-effort; an unreadable state simply keeps the stamp) so a landed
        PR never leaves a stamp behind to age out."""
        if merged or self._pr_is_merged():
            polish_state.clear_polish_state(self.pr, self.repo)
        return RunOutcome("clean", rounds, merged, self.actions)

    def _pr_is_merged(self) -> bool:
        """True only when GitHub CONFIRMS the PR is merged. Any failure (no gh, a
        non-zero exit, unreadable output) → False: an unconfirmed state keeps the
        polish stamp, which costs nothing but a stale file."""
        state = _git_line(
            ["gh", "pr", "view", str(self.pr), "--json", "state", "-q", ".state"]
            + (["-R", self.repo] if self.repo else []),
            self.cwd, self.gh_run)
        return (state or "").strip().upper() == "MERGED"

    def _thread_gate_ok(self) -> bool:
        """Pre-merge review-thread gate. Returns True iff the merge may proceed on
        thread state. Resolves the review threads THIS run genuinely handled (their
        root is an INLINE comment the loop brought to a ``_RESOLVED_FINALS``
        disposition — tracked in ``_handled_inline_ids``), then confirms GitHub
        reports zero unresolved threads.

        Never auto-resolves a thread the loop did not handle: a human's still-open
        thread, or a comment whose fix the verify pass rejected, is left untouched
        and BLOCKS the merge (the whole point of the gate). The blocking decision
        for such a thread rides the FIRST (successful) read — no re-query is needed
        for a thread we never touched — so a later transient error can never let it
        slip through. Fail-soft everywhere else: a ``gh`` / GraphQL read error (the
        reader raises) is NOT an unresolved thread, so it logs and proceeds,
        mirroring ``check_pr_mergeable``'s fail-open contract — a transient blip
        never wedges a mergeable PR.

        The one exception to fail-soft is the confirming re-query: it fails open ONLY
        when every ``resolve_thread`` call reported success. If a resolve mutation
        FAILED (the resolver returns False on a gh error) and the re-read ALSO errors,
        the threads' true state is genuinely unconfirmed, so the gate blocks rather
        than laundering unconfirmed state into a merge."""
        try:
            threads = self.fetch_threads(self.pr, repo=self.repo, cwd=self.cwd)
        except (subprocess.SubprocessError, OSError):
            # Fail-soft: a transient read error is NOT an unresolved thread, so it
            # must never wedge a mergeable PR. A plain console warning (not an
            # auto-action notice) — the gate simply could not run, it took no
            # merge decision, mirroring the silent degrade of the reaction fold.
            print(f"[thread-gate] could not read PR #{self.pr} review threads — "
                  f"proceeding (a transient read error is not an unresolved thread)")
            return True
        except gh_ingest.RepoNotConfiguredError:
            # A missing or malformed repo string is a non-transient configuration
            # error — gh_ingest.fetch_review_threads needs owner/repo for GraphQL
            # variables and cannot degrade gracefully. Block rather than silently
            # skip the thread check and allow an auto-merge with open threads.
            self._thread_gate_block_reason = (
                "could not check review threads (no owner/repo configured)")
            self.notice("thread-gate",
                        f"PR #{self.pr}: cannot check review threads — "
                        f"no owner/repo configured for GraphQL thread reader",
                        status="stop",
                        hint="re-run with --repo owner/name to enable the thread gate")
            return False
        except RuntimeError:
            # Other RuntimeErrors (GraphQL blips, gh network failures) are transient.
            print(f"[thread-gate] could not read PR #{self.pr} review threads — "
                  f"proceeding (a transient read error is not an unresolved thread)")
            return True
        unresolved = [t for t in threads if not t.is_resolved]
        if not unresolved:
            return True
        # The run's OWN threads: an unresolved thread whose root comment the loop
        # brought to a genuine done verdict this run. ``_handled_inline_ids`` is
        # scoped to INLINE comment ids (the only comments that are thread roots),
        # so a review-body / conversation id can never match a thread root here.
        # Everything else unresolved is "foreign" — a human's open thread or a
        # rejected fix — never touched.
        handled = self._handled_inline_ids
        own = [t for t in unresolved
               if t.root_comment_id is not None and t.root_comment_id in handled]
        foreign = [t for t in unresolved if t not in own]
        all_resolves_ok = True
        for t in own:
            try:
                # The resolver reports failure by RETURNING False (it swallows gh /
                # GraphQL errors internally), not by raising — so track the return
                # value, not just exceptions. A failed resolve means the thread may
                # still be unresolved; the re-query below is normally the source of
                # truth, but if it ALSO errors we must not fail-open (see below).
                if not self.resolve_thread(t.id, cwd=self.cwd):
                    all_resolves_ok = False
            except (subprocess.SubprocessError, OSError, RuntimeError):
                # A raising resolve is likewise unconfirmed.
                all_resolves_ok = False
        if foreign:
            # An unresolved thread the loop never handled → definitely not merge-
            # ready, decided on the first (good) read; no re-query, so a transient
            # error at re-query time can never launder it into a merge.
            self._thread_gate_block_reason = "a review thread is still unresolved"
            self.notice("thread-gate",
                        f"PR #{self.pr}: {len(foreign)} review thread(s) still "
                        f"unresolved that the loop did not address — not merging; "
                        f"handing back", status="stop",
                        hint="resolve the open thread(s) on GitHub, then re-run")
            return False
        # Only the run's own threads were unresolved — re-query to confirm the
        # resolve mutations actually landed on GitHub.
        try:
            threads2 = self.fetch_threads(self.pr, repo=self.repo, cwd=self.cwd)
        except (subprocess.SubprocessError, OSError, RuntimeError):
            if all_resolves_ok:
                # Fail-soft on the confirming re-query too: every resolve mutation
                # reported success, so the run's own threads ARE resolved on GitHub;
                # a transient re-read blip must not wedge the now-clean PR.
                print(f"[thread-gate] could not re-read PR #{self.pr} review threads "
                      f"after resolving — proceeding (all resolve mutations succeeded)")
                return True
            # A resolve mutation reported failure AND the confirming re-read errored:
            # the thread's true state is genuinely unconfirmed. Do NOT fail-open into
            # a merge on unconfirmed state — block and hand back instead.
            self._thread_gate_block_reason = "a review thread is still unresolved"
            self.notice("thread-gate",
                        f"PR #{self.pr}: could not confirm this run's review-thread "
                        f"resolution (a resolve call failed and the confirming re-read "
                        f"also errored) — not merging; handing back", status="stop",
                        hint="resolve the open thread(s) on GitHub, then re-run")
            return False
        remaining = [t for t in threads2 if not t.is_resolved]
        if remaining:
            self._thread_gate_block_reason = "a review thread is still unresolved"
            self.notice("thread-gate",
                        f"PR #{self.pr}: {len(remaining)} review thread(s) still "
                        f"unresolved after resolving this run's own — not merging; "
                        f"handing back", status="stop",
                        hint="resolve the open thread(s) on GitHub, then re-run")
            return False
        return True

    def _premerge_gate_ok(self) -> bool:
        """#43/#44 non-label pre-merge gate. Returns True iff the merge may proceed.
        Blocks (False) on a GitHub-side non-mergeable state — conflicts / draft /
        behind / blocked / failing checks — and, when checks are still in flight
        from the last fix push, waits them out before allowing the merge. Fail-soft:
        an errored/unreadable ``gh`` read never blocks (``check_pr_mergeable``
        returns mergeable). A CI-red / never-settling block also sets
        ``_premerge_ci_red`` so the manual-landing hand-back reports 'CI is red'; a
        structural block (conflict / behind / …) leaves the flag unset so the
        post-rebase live re-check reports the honest current reason (a rebase may
        even have resolved a 'behind')."""
        ok, reason = merge.check_pr_mergeable(
            self.pr, repo=self.repo, cwd=self.cwd, run=self.gh_run)
        if ok:
            return True
        if merge.reason_is_pending_checks(reason):
            self.notice("premerge-check",
                        f"PR #{self.pr}: {reason} — waiting for CI to settle "
                        f"before merge", status="do")
            if not merge.wait_for_ci_settle(
                    self.pr, repo=self.repo, cwd=self.cwd, run=self.gh_run,
                    notice=self.notice, sleep=self.sleep):
                self._premerge_ci_red = True  # CI failed / never settled
                return False
            # CI settled green — re-check full mergeability: BLOCKED (missing required
            # reviews) is evaluated after pending checks in check_pr_mergeable, so the
            # initial pass may have short-circuited on "pending" before reaching it.
            ok, reason = merge.check_pr_mergeable(
                self.pr, repo=self.repo, cwd=self.cwd, run=self.gh_run)
            if ok:
                return True
            # not mergeable after settle — fall through to the shared hand-back below
        if reason.startswith("checks failing"):
            self._premerge_ci_red = True  # a red check → the 'CI is red' hand-back
        self.notice("premerge-check",
                    f"PR #{self.pr} is not mergeable ({reason}) — not merging; "
                    f"handing back", status="stop",
                    hint="resolve it on GitHub, then re-run")
        return False

    def _review_permits_merge(self) -> bool:
        """The never-merge-unreviewed backstop's verdict, independent of the merge
        trigger: True iff at least one expected reviewer genuinely reviewed, OR the
        operator deliberately expected none (``--rr-none``). False when a configured
        fleet was expected but nobody reviewed, or no reviewers exist and it was not
        a deliberate ``--rr-none`` — in both, a merge would land code nothing saw.
        This is the same three-way distinction the auto-merge SAFETY gate applies;
        the interactive path consults it so an attended merge is offered only for a
        PR that was actually reviewed."""
        if self.rr_none:
            return True
        fleet = set(self._run_start_fleet)
        if not fleet:
            return False  # (a) no reviewers configured → nothing reviewed
        return bool(fleet & self.reviewed_ever)  # (c) someone reviewed vs (b) nobody

    def _interactive_merge_prompt(self) -> bool:
        """#g9b: auto-merge is off and we are attached to a terminal — offer to
        squash-merge now, but only for a PR that was actually reviewed and that
        GitHub would accept. Returns True iff the operator confirmed AND the merge
        landed. The caller gates on ``sys.stdin.isatty()``, so a headless run never
        reaches here."""
        # The never-merge-unreviewed backstop applies to the interactive path too:
        # do not offer to merge a PR that no expected reviewer reviewed (a configured
        # fleet that stayed silent, or no reviewers at all). Auto-merge OFF used to
        # always leave the PR open, so this keeps the interactive convenience from
        # inviting a merge of code nothing saw.
        if not self._review_permits_merge():
            self.notice("merge-prompt",
                        f"PR #{self.pr} was not reviewed by any expected reviewer — "
                        f"not offering an interactive merge; leaving it open",
                        status="skip",
                        hint="review it yourself, then `gh pr merge` when ready")
            return False
        ok, reason = merge.check_pr_mergeable(
            self.pr, repo=self.repo, cwd=self.cwd, run=self.gh_run)
        if not ok:
            self.notice("merge-prompt",
                        f"PR #{self.pr} is not mergeable yet ({reason}) — not "
                        f"offering an interactive merge; handing it back",
                        status="skip")
            return False
        try:
            suffix = f" ({self.repo})" if self.repo else ""
            answer = input(f"Merge PR #{self.pr}{suffix} now? (yes/no): ").strip().lower()
        except (EOFError, OSError):
            print("stdin lost — skipping the interactive merge prompt.")
            return False
        if answer not in ("yes", "y"):
            print("Merge skipped — run `gh pr merge` when ready.")
            return False
        return merge.squash_merge(
            self.pr, repo=self.repo, enabled=True, cwd=self.cwd,
            run=self.gh_run, notice=self.notice)

    def _maybe_warn_claude_never_reviewed(self) -> None:
        """#g9a NOTIFICATION (never a block): the loop's primary reviewer (@claude)
        was expected but its review trigger never landed and it never reviewed —
        yet at least one OTHER expected reviewer did, so the run reaches a clean
        exit with the strongest reviewer having never seen the code. Emit a loud
        console banner so the operator knows; the run still finishes normally (the
        generalized SAFETY gate already guarantees SOMEONE reviewed). Distinct from
        the auth-failure banner (a 401 AFTER the trigger posted) and the persistent-
        silent banner (a summonable reviewer that stayed quiet)."""
        fleet = set(self._run_start_fleet)
        if not self._claude_trigger_failed:
            return
        if "claude" not in fleet or "claude" in self.reviewed_ever:
            return
        if not (fleet & self.reviewed_ever):
            return  # nobody reviewed → the SAFETY gate handles that (block), not this note
        _print_refusal_banner(
            f"PRIMARY REVIEWER SKIPPED — @claude never reviewed PR #{self.pr}",
            f"The '@claude review' trigger could not be posted, so Claude — the "
            f"loop's primary reviewer — never saw this code. Another reviewer did "
            f"review it, so the run is finishing normally, but the strongest review "
            f"was missed. Confirm '@claude review' can be posted on this repo (the "
            f"Claude GitHub App is installed and the CLAUDE_CODE_OAUTH_TOKEN secret "
            f"is set) before relying on this PR's review.")

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
        if self._thread_gate_block_reason is not None:
            return self._thread_gate_block_reason
        if self.rr_none:
            # --rr-none asked for no review by design, so an empty fleet here is
            # not "unconfigured" — the comments were fixed and the PR is ready.
            # (Only reached when auto-merge is off; the auto-merge path merges.)
            return None
        fleet = set(self._run_start_fleet)
        if not fleet:
            return "no reviewers are configured for this repo"
        if not (fleet & self.reviewed_ever):
            return "no expected reviewer actually reviewed this PR"
        # #52: a genuine review happened and no cached CI-red flag fired — but
        # GitHub itself may still refuse the merge (conflicts / behind / branch
        # protection / a red or pending check). On the non-label / auto-merge-off
        # path nothing set _premerge_ci_red, so consult GitHub live here: a
        # hand-back is called "ready to merge" only when GitHub truly would merge
        # it. Fail-soft (check_pr_mergeable returns mergeable on any gh error), so
        # a transient blip never downgrades a genuinely-ready PR.
        ok, reason = merge.check_pr_mergeable(
            self.pr, repo=self.repo, cwd=self.cwd, run=self.gh_run)
        if not ok:
            return f"GitHub will not merge it yet ({reason})"
        return None  # Bucket A — a genuine clean review, GitHub-mergeable; ready to merge

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
        # Pass self.repo so exit_rebase resolves the base remote the SAME way the
        # behind-drift check (merge._branch_is_behind_base) did — matching the PR's
        # base repo to a configured remote's URL. Without it, a fork PR found behind
        # upstream/main could be rebased onto the fork's stale origin/main (or
        # reported "current"), leaving a behind+red PR stuck on the next pass.
        status, detail = commit_push.exit_rebase(
            self.cwd, base=base, repo=self.repo, run=self.gh_run, notice=self.notice)
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
        if bot in self._rate_limited_until:
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
        """Round-end bookkeeping for the persistent-silent-reviewer warning AND the
        mid-run silent drop. For every reviewer EXPECTED this round: if it posted
        anything (its round-scoped ``last_seen`` is set — a comment, clean
        approval, OR a quota/error placeholder all count) it has responded, is
        permanently protected from the warning, and is re-included if it had been
        dropped; otherwise its consecutive-silent round count is bumped and — if
        the loop genuinely expected it (summoned, or auto_on_open) — it is dropped
        from re-request for the rest of the run (silence is not approval).
        Reviewers not expected this round (done / excluded) are left untouched."""
        for bot in expected:
            if self._bot_state(bot).last_seen is not None:
                self.responded_ever.add(bot)
                self.silent_rounds[bot] = 0        # responded → the streak resets
                # Defensive no-op for silent_dropped bots: expected_bots() already
                # filters them out, so they are never in `expected` — real
                # re-inclusion fires in _classify_signal() when they post again.
                self.silent_dropped.discard(bot)
            else:
                self.silent_rounds[bot] = self.silent_rounds.get(bot, 0) + 1
                if self._was_review_expected(bot):
                    self.silent_dropped.add(bot)

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
            if bot in self._rate_limited_until:
                continue  # rate-limited by a workflow marker — not a setup failure
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
