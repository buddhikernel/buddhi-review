"""Per-PR persistence of a run's polish-only verdicts, so a restart does not
re-ask a reviewer whose verdict the loop already holds.

A reviewer whose round posted only non-substantive comments is dropped from
re-request for the rest of the run (``RoundDriver.polishing``). That verdict is
in-process state: kill the loop between rounds and it is gone, so an
``--rr-active`` restart re-summons a reviewer that has nothing left to say —
burning a summon, a register delay, and a full poll window on a bot whose
comments the poll then re-ingests anyway.

The verdict is tied to a specific tip: a polish-only reviewer is sticky *within*
a run (it is not re-summoned even as later fixes advance HEAD), so the stamp is
written with the tip the loop pushed at the end of that round and restored ONLY
when the PR's live HEAD still equals it. A HEAD that moved (a human's commit, a
rebase) invalidates the verdict — the reviewer may have real findings on the new
code — and nothing is restored.

STORAGE. One JSON object per (repo, PR) under ``~/.cache/buddhi/polish-state/``
(the shared buddhi cache dir; ``$BUDDHI_REVIEW_POLISH_STATE_DIR`` overrides it in
tests). Every read is fail-closed — a missing, corrupt, torn, foreign, or
schema-mismatched file reads as "no state" (``None``), never an exception and
never a partial restore. Every write is atomic (temp file + ``os.replace``), so a
concurrent reader never sees a half-written file, and a write with an unknown tip
is refused outright rather than stamping a tip a later restore could match.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Dict, Iterable, List, Optional

SCHEMA_VERSION = 1
STATE_DIR_ENV = "BUDDHI_REVIEW_POLISH_STATE_DIR"

# Anything outside this class is replaced in the on-disk file name — the key
# carries a repo slug ("owner/name") whose separator is a path separator.
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def state_dir() -> str:
    """The polish-state directory: ``$BUDDHI_REVIEW_POLISH_STATE_DIR`` when set
    (tests), else ``~/.cache/buddhi/polish-state`` (the shared buddhi cache dir)."""
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return override
    return os.path.expanduser("~/.cache/buddhi/polish-state")


def _slug(value: str) -> str:
    """A file-name-safe rendering of one key component ("owner/repo" →
    "owner_repo"). Never empty, so a missing component still yields a valid name."""
    return _UNSAFE_RE.sub("_", str(value or "")) or "_"


def state_path(pr, repo) -> str:
    """The state file for this (repo, PR). Keyed on BOTH — one loop can drive the
    same PR number in different repos, and a per-PR file keeps a corrupt entry
    from taking any other PR's state down with it."""
    return os.path.join(state_dir(),
                        f"{_slug(repo or 'norepo')}__{_slug(pr)}.json")


def write_polish_state(pr, repo, tip_sha, bots: Iterable[str]) -> bool:
    """Stamp ``bots`` (the polish-only reviewers) against ``tip_sha`` (the commit
    the loop carries into the next round). Returns True on success.

    Fail-CLOSED on an unknown tip: an empty/blank ``tip_sha`` is never written, so
    a later restore can never match a stamp whose tip the loop could not read.
    Best-effort otherwise — any IO error returns False and the caller carries on
    (a missed stamp only costs a re-summon on the next restart)."""
    tip = str(tip_sha or "").strip()
    if not tip:
        return False
    payload = {
        "schema_version": SCHEMA_VERSION,
        "repo": str(repo or ""),
        "pr": str(pr),
        "tip_sha": tip,
        "bots": sorted({str(b) for b in bots}),
        "ts": int(time.time()),
    }
    path = state_path(pr, repo)
    try:
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".polish-state-", suffix=".json", dir=d)
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
    except (OSError, ValueError, TypeError):
        return False


def read_polish_state(pr, repo) -> Optional[Dict]:
    """This (repo, PR)'s persisted polish state, or ``None`` when there is none to
    trust. Fail-closed: an absent / unreadable / corrupt / torn file, a foreign
    schema version, a payload that is not an object, a key that does not match the
    requested (repo, PR), a blank tip, or a malformed bot list all read as None.
    Never raises — a restore that cannot be trusted simply does not happen."""
    try:
        with open(state_path(pr, repo), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != SCHEMA_VERSION:
        return None
    # The key is re-checked against the payload: a slug collision (two repos whose
    # names differ only in a replaced character) must never restore the wrong PR's
    # verdicts.
    if data.get("repo") != str(repo or "") or data.get("pr") != str(pr):
        return None
    tip = data.get("tip_sha")
    if not isinstance(tip, str) or not tip.strip():
        return None
    raw = data.get("bots")
    if not isinstance(raw, list) or any(not isinstance(b, str) for b in raw):
        return None
    bots: List[str] = sorted({b for b in raw if b})
    return {"schema_version": SCHEMA_VERSION, "repo": data.get("repo"),
            "pr": data.get("pr"), "tip_sha": tip.strip(), "bots": bots,
            "ts": data.get("ts")}


def clear_polish_state(pr, repo) -> bool:
    """Drop this (repo, PR)'s state — the merged-PR cleanup. Returns True when a
    file was removed. Absent file / IO error → False, never an exception."""
    try:
        os.remove(state_path(pr, repo))
        return True
    except OSError:
        return False
