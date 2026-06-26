"""In-run contextual upgrade nudge — the ONE permitted paid reference in the OSS skill.

When the free skill finishes a review run and the run ended at a point it handed
work BACK to a human (it could not carry it through on its own), this module emits
a single transient, non-blocking line: a benefit-named upgrade nudge tied to what
just happened, plus a Cmd-clickable domain. It is the only place in the OSS tree
that may reference a paid upgrade (execution-plan §E item 9 / §D4), and it does so
under hard limits:

  * **Benefit-named only.** The line cites ONE concrete benefit and names NO paid
    product, mechanism, or feature; it ships no feature list and no free-vs-paid
    comparison. The contextual benefit copy lives in :data:`_BENEFIT_BY_STATUS`.
  * **Shown only without an active paid backend.** Eligibility reuses the FREE-1
    backend discovery (:func:`paid_backend_active`): if any registered backend
    other than the free one is active, the skill is already running paid and the
    nudge is suppressed.
  * **Frequency-capped.** At most once per ``BUDDHI_UPSELL_MIN_INTERVAL_HOURS``
    (default 24h) and at most ``BUDDHI_UPSELL_MAX_SHOWS`` times ever (default 5),
    tracked in a local state file.
  * **Dismissible.** ``BUDDHI_UPSELL_DISMISS=1`` records a durable "never again"
    flag in that same state file, so it stays off across runs and shells.
  * **Suppressible.** ``BUDDHI_NO_UPSELL=1`` silences it for the run — the single
    shared check every upgrade nudge in the skill honours (:func:`upsell_suppressed`).
  * **Never phones home.** This module makes zero network calls; the only side
    effect is the local frequency-cap state file.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, TextIO

from buddhi_review.transparency import _colour_enabled

# The project's marketing domain — the Cmd-clickable upgrade destination. A full
# ``https://`` URL is what terminals reliably auto-linkify for Cmd-click.
DOMAIN = "buddhikernel.com"
_URL = f"https://{DOMAIN}"

# A run's terminal status (``RunOutcome.status``) → the single concrete benefit the
# nudge names for that hand-back moment. Only the two "the loop could not finish on
# its own and handed it back" statuses are nudge-worthy; a clean merge needed no
# help and an operator-chosen "stopped" is a deliberate halt we do not nag over, so
# both are absent here (and yield no nudge).
_BENEFIT_BY_STATUS: Dict[str, str] = {
    "needs-human": "finish runs like this without you stepping in",
    "max-rounds": "let a run like this keep going to the finish on its own",
}

# Frequency-cap defaults (both env-overridable).
_DEFAULT_MIN_INTERVAL_HOURS = 24.0
_DEFAULT_MAX_SHOWS = 5


# ── Suppression / dismissal / cap knobs ───────────────────────────────────────────

def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def upsell_suppressed() -> bool:
    """``BUDDHI_NO_UPSELL`` truthy → silence every upgrade nudge for this run.

    The single source of truth for the OSS upsell-suppression contract: the setup
    wizard's locked teasers and this in-run nudge both gate on it, so one env var
    turns off every paid reference in the skill. Default: nudges render.
    """
    return _env_truthy("BUDDHI_NO_UPSELL")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


def _min_interval_seconds() -> float:
    return max(0.0, _env_float("BUDDHI_UPSELL_MIN_INTERVAL_HOURS", _DEFAULT_MIN_INTERVAL_HOURS)) * 3600.0


def _max_shows() -> int:
    return max(0, _env_int("BUDDHI_UPSELL_MAX_SHOWS", _DEFAULT_MAX_SHOWS))


# ── Local frequency-cap state (never phones home) ─────────────────────────────────

def _state_path() -> Path:
    """The local state file holding the frequency-cap counters + the durable
    dismissal flag. ``BUDDHI_UPSELL_STATE`` overrides the path (used by tests)."""
    override = os.environ.get("BUDDHI_UPSELL_STATE")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "buddhi" / "upsell.json"


def _read_state(path: Path) -> Dict:
    """Read the state dict; a missing / unreadable / malformed file reads as ``{}``
    so a corrupt or absent file never crashes the loop (fail-open to "no record")."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, state: Dict) -> None:
    """Persist ``state`` atomically; best-effort. A write failure (read-only cache
    dir, full disk) is swallowed — the nudge is never worth crashing or blocking a
    run over, and the worst case of a lost write is the cap resets next run."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _frequency_ok(state: Dict, now: float) -> bool:
    """True when the cap permits a show now: under the lifetime max AND at least the
    minimum interval has elapsed since the last show. (Durable dismissal is checked
    separately by the caller.)"""
    shown = state.get("shown_count", 0)
    shown = shown if isinstance(shown, int) else 0
    if shown >= _max_shows():
        return False
    last = state.get("last_shown")
    if isinstance(last, (int, float)) and (now - last) < _min_interval_seconds():
        return False
    return True


# ── FREE-1 discovery: is an active paid backend present? ──────────────────────────

def paid_backend_active(backends: Optional[List] = None) -> bool:
    """True if any registered backend OTHER than the free one reports itself active.

    This reuses the FREE-1 backend discovery as the eligibility gate: the nudge is
    shown ONLY when the free skill runs without an active paid backend. A backend
    that errors while answering ``is_active`` is treated as inactive (an installed
    package must never break the free skill). ``backends`` is injectable for tests.
    """
    from buddhi_review import backends as _backends

    candidates = backends if backends is not None else _backends.discover_backends()
    free_name = _backends.FreeBackend.name
    for backend in candidates:
        if getattr(backend, "name", None) == free_name:
            continue
        try:
            if backend.is_active():
                return True
        except Exception:
            continue
    return False


# ── Rendering ─────────────────────────────────────────────────────────────────────

def format_nudge(status: str) -> Optional[str]:
    """The uncoloured nudge line for a run-terminal ``status``, or ``None`` when the
    status is not a hand-back moment we nudge on. Pure (no env, no I/O) — the text
    contract the tests pin."""
    benefit = _BENEFIT_BY_STATUS.get(status)
    if not benefit:
        return None
    return f"↑ Upgrade to {benefit} — {_URL}   (BUDDHI_NO_UPSELL=1 to silence)"


def _emit(text: str, stream: TextIO) -> None:
    # Dim so the nudge reads as a quiet, transient aside under the run summary —
    # never as an error or a blocking prompt.
    line = f"\033[2m{text}\033[0m" if _colour_enabled(stream) else text
    print(line, file=stream)


# ── The orchestrator ──────────────────────────────────────────────────────────────

def maybe_emit_run_end_nudge(
    status: str,
    *,
    stream: Optional[TextIO] = None,
    backends: Optional[List] = None,
    now: Optional[float] = None,
    state_path: Optional[Path] = None,
) -> Optional[str]:
    """Emit the contextual upgrade nudge for a finished run if every gate allows it.

    Returns the emitted text (also for tests), or ``None`` when nothing was shown.
    Gates, in order: durable dismissal request → non-nudge status → run-suppression
    → already-dismissed → active-paid-backend → frequency cap. ``stream`` / ``now`` /
    ``state_path`` / ``backends`` are injectable so the whole path is unit-testable
    without a terminal, a clock, the real cache file, or a live backend.
    """
    stream = stream if stream is not None else sys.stdout
    now = now if now is not None else time.time()
    path = state_path if state_path is not None else _state_path()
    state = _read_state(path)

    # An explicit, durable "never again" request is honoured first and unconditionally
    # recorded, so dismissal sticks even on the run the user sets the flag.
    if _env_truthy("BUDDHI_UPSELL_DISMISS"):
        if state.get("dismissed") is not True:
            state["dismissed"] = True
            _write_state(path, state)
        return None

    text = format_nudge(status)
    if text is None:
        return None
    if upsell_suppressed():
        return None
    if state.get("dismissed") is True:
        return None
    if paid_backend_active(backends):
        return None
    if not _frequency_ok(state, now):
        return None

    _emit(text, stream)
    shown = state.get("shown_count", 0)
    state["shown_count"] = (shown if isinstance(shown, int) else 0) + 1
    state["last_shown"] = now
    _write_state(path, state)
    return text
