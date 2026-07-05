#!/usr/bin/env python3
"""session_worktrees.py — a tiny, durable map of Claude Code SESSION → the git
worktree that session is working in.

WHY THIS EXISTS. The /open-pr + /review-pr skills answer "which checkout should
the loop act on?" from the calling session's ``$PWD`` (``git rev-parse
--show-toplevel``). That breaks the moment the agent follows the standing "do your
work in a NEW worktree off main" rule: the session is SPAWNED in worktree A but
creates and operates on worktree B via ``git -C B`` — its shell ``$PWD`` never
leaves A. ``$PWD`` (A) is clean / not the checkout with the work (B), so the skill
opens the PR from the wrong place, even though "open the loop on the worktree I
just worked in" is unambiguous.

This registry closes that gap. The git guardrail hook (a PreToolUse(Bash) hook
that already sees every git command + the session id) records ``session_id → B``
the instant the agent runs ``git worktree add B`` or operates on a worktree via
``git -C B`` — automatically, with no agent step. The skills' worktree resolver
then reads this map and auto-selects B, regardless of where ``$PWD`` points.

STORAGE. One JSON object at ``~/.cache/buddhi/session-worktrees.json`` (the shared
buddhi cache dir), overridable via ``$BUDDHI_SESSION_WORKTREES_PATH`` (tests). The
file is BEST-EFFORT and self-repairing: every read tolerates a missing/corrupt
file (returns empty), every write is atomic (temp + os.replace) and prunes stale /
overflow entries so the file can never grow without bound. NOTHING here ever
raises to its caller — a registry hiccup must never break the hook or the skill.

Pure stdlib (os / json / time / tempfile). Safe and cheap for the hook to import
on every Bash call.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

# Keep the file small + fresh: at most this many sessions, none older than this.
_MAX_ENTRIES = 100
_MAX_AGE_S = 30 * 24 * 3600  # 30 days

_SCHEMA_VERSION = 1


def registry_path() -> str:
    """The registry file path: ``$BUDDHI_SESSION_WORKTREES_PATH`` when set (tests),
    else ``~/.cache/buddhi/session-worktrees.json`` (the shared buddhi cache dir)."""
    override = os.environ.get("BUDDHI_SESSION_WORKTREES_PATH")
    if override:
        return override
    return os.path.expanduser("~/.cache/buddhi/session-worktrees.json")


def _load() -> dict:
    """Parse the registry into ``{session_id: {worktree, repo, ts}}``. Any
    failure (absent file, corrupt JSON, unexpected shape) yields ``{}`` — the
    registry is advisory, never load-bearing."""
    try:
        with open(registry_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return {}
    out = {}
    for sid, rec in sessions.items():
        if isinstance(sid, str) and isinstance(rec, dict) and rec.get("worktree"):
            out[sid] = rec
    return out


def _prune(sessions: dict, *, now: float) -> dict:
    """Drop entries older than ``_MAX_AGE_S`` and keep only the ``_MAX_ENTRIES``
    most recent — so the file stays small and a long-dead session never lingers."""
    def _ts(rec):
        try:
            return float(rec.get("ts", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            return 0.0

    fresh = [(sid, rec) for sid, rec in sessions.items()
             if now - _ts(rec) <= _MAX_AGE_S]
    fresh.sort(key=lambda kv: _ts(kv[1]), reverse=True)
    return dict(fresh[:_MAX_ENTRIES])


def _atomic_write(sessions: dict) -> bool:
    """Write the registry atomically (temp file + os.replace) so a concurrent
    reader never sees a half-written file. Returns True on success, False on any
    error (best-effort — the caller swallows the result)."""
    path = registry_path()
    payload = {"version": _SCHEMA_VERSION, "sessions": sessions}
    try:
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".session-worktrees-", suffix=".json", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception:
        return False


def register(session_id, worktree_path, *, repo=None, now=None) -> bool:
    """Record ``session_id → worktree_path`` (abs-normalized), pruning stale /
    overflow entries. No-op (returns False) on a falsy session id or worktree.
    Never raises — a registry write failure must never break the caller (the
    git hook). The latest registration for a session WINS (most-recent work)."""
    try:
        if not session_id or not worktree_path:
            return False
        ts = time.time() if now is None else now
        abspath = os.path.abspath(os.path.expanduser(str(worktree_path)))
        sessions = _load()
        sessions[str(session_id)] = {
            "worktree": abspath,
            "repo": repo,
            "ts": ts,
        }
        sessions = _prune(sessions, now=ts)
        return _atomic_write(sessions)
    except Exception:
        return False


def lookup(session_id):
    """The worktree path recorded for ``session_id``, or None. Never raises."""
    try:
        if not session_id:
            return None
        rec = _load().get(str(session_id))
        if isinstance(rec, dict):
            wt = rec.get("worktree")
            return wt if isinstance(wt, str) and wt else None
        return None
    except Exception:
        return None


def all_entries():
    """Every recorded worktree, newest-first, as a list of ``{worktree, repo, ts}``
    (``repo`` may be None when the git hook recorded the worktree without one).
    Best-effort: ``[]`` on any failure, never raises."""
    def _ts(rec):
        try:
            return float(rec.get("ts", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            return 0.0

    try:
        recs = [r for r in _load().values()
                if isinstance(r, dict) and r.get("worktree")]
        recs.sort(key=_ts, reverse=True)
        return [{"worktree": r.get("worktree"), "repo": r.get("repo"), "ts": _ts(r)}
                for r in recs]
    except Exception:
        return []
