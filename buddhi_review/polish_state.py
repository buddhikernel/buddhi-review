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
written with the tip the loop carries into its next round and restored ONLY when
the PR's live HEAD still equals it. A HEAD that moved (a human's commit, a
rebase) invalidates the verdict — the reviewer may have real findings on the new
code — and nothing is restored.

STORAGE. One JSON object per PR under ``$BUDDHI_POLISH_STATE_DIR`` (default
``~/.cache/buddhi/polish-state``), named ``<owner__repo>-PR<pr>.json`` — the FULL
``owner/repo`` slug, so two owners with the same repo name (``a/foo`` vs ``b/foo``)
get distinct files and never fight over one filename. Belt-and-suspenders on top:
the record ALSO carries its full repo + PR and :func:`read_polish_state`
re-verifies both, so even a hand-moved or hand-edited file can only ever read as
"no state", never as another repo's verdict.

Every read is fail-CLOSED: a missing, corrupt, torn, foreign, or
schema-mismatched record reads as ``None``, never an exception and never a
partial restore. Every write is atomic (temp file + ``os.replace``, under the
same lock a concurrent writer takes), so a reader never sees a half-written file;
a write with an unknown tip is refused outright rather than stamping a tip a later
restore could match.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

SCHEMA_VERSION = 1
STATE_DIR_ENV = "BUDDHI_POLISH_STATE_DIR"

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def state_dir() -> str:
    """The per-PR polish-verdict store: ``$BUDDHI_POLISH_STATE_DIR`` when set,
    else ``~/.cache/buddhi/polish-state`` (the shared buddhi cache dir). One JSON
    file per PR — each PR has exactly one current verdict, rewritten every round."""
    env = os.environ.get(STATE_DIR_ENV)
    if env:
        return os.path.expanduser(env)
    return os.path.expanduser("~/.cache/buddhi/polish-state")


def state_path(pr, repo=None) -> str:
    """``<dir>/<owner__repo>-PR<pr>.json`` — keyed on the FULL ``owner/repo`` (the
    ``/`` becomes ``__``), so same-named repos under different owners never share a
    file. The one derivation for write, read, AND clear: all three call this, so
    the key can never drift between them."""
    full = str(repo or "local") or "local"
    slug = _UNSAFE_RE.sub("_", full.replace("/", "__"))
    return os.path.join(state_dir(), f"{slug}-PR{pr}.json")


def _matches(stored, wanted) -> bool:
    """True when a stored key component identifies the requested one. ``None`` is
    only ever matched by ``None``: a caller that could not infer a repo must not be
    handed a record that names one."""
    if wanted is None or stored is None:
        return wanted is None and stored is None
    return str(stored) == str(wanted)


def write_polish_state(pr, repo, tip_sha, bots: Iterable[str]) -> bool:
    """Persist the polish-only verdict ``bots`` for (``repo``, ``pr``) as reached at
    PR HEAD ``tip_sha``. Returns True on success.

    ``tip_sha`` MUST be the tip the loop carries into its next round, so a restore
    matches only when HEAD has not moved since the loop stopped.

    Fail-CLOSED on an unknown tip: a falsy ``tip_sha`` writes NOTHING, so a transient
    "could not read HEAD" can never persist a stamp a later restore might match.

    NO-CLOBBER: an EMPTY ``bots`` set never overwrites a NON-empty record at the SAME
    ``tip_sha``. Only ``--rr-active`` restores the verdict, so any other run mode (a
    plain re-run, ``--rr-none``) reaches its round end with an empty set purely
    because it never read the record — and would otherwise erase, at the very commit
    it is describing, a verdict another run legitimately reached. A moved tip always
    writes: that is a different commit, so the old record no longer speaks for it.

    Best-effort otherwise: any OS error returns False and the caller carries on (a
    missed stamp only costs a re-summon on the next restart)."""
    tip = str(tip_sha or "").strip()
    if not tip:
        return False
    path = state_path(pr, repo)
    names: List[str] = sorted({str(b) for b in (bots or []) if b})
    if not names:
        prior = read_polish_state(pr, repo)
        if prior and prior.get("tip_sha") == tip and prior.get("bots"):
            return False
    record = {
        "schema_version": SCHEMA_VERSION,
        "repo": repo,
        "pr": pr,
        "tip_sha": tip,
        "bots": names,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        lock_fd = None
        try:
            import fcntl
            lock_fd = os.open(path + ".lock", os.O_CREAT | os.O_WRONLY, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        try:
            tmp = path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                os.replace(tmp, path)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        finally:
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
        return True
    except OSError:
        return False


def read_polish_state(pr, repo=None) -> Optional[Dict]:
    """The persisted record for (``repo``, ``pr``), or ``None`` when there is none to
    trust. Fail-closed on EVERY doubt — a missing file, an OS error, malformed JSON, a
    non-object payload, an unknown schema version, an empty tip, a record whose stored
    repo+PR do not match the request (the short-name collision), or a malformed bot
    list. Never raises: a restore that cannot be trusted simply does not happen."""
    try:
        with open(state_path(pr, repo), encoding="utf-8", errors="replace") as f:
            obj = json.loads(f.read() or "null")
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    try:
        if int(obj.get("schema_version") or 0) != SCHEMA_VERSION:
            return None
    except (TypeError, ValueError):
        return None
    if not obj.get("tip_sha"):
        return None
    if not _matches(obj.get("pr"), pr) or not _matches(obj.get("repo"), repo):
        return None
    # ``bots`` must be a LIST. A hand-edited / truncated record could carry a string
    # (which would iterate into characters), a dict (into keys), or an int (which
    # would RAISE) — all of which would break the never-raise contract above.
    names = obj.get("bots")
    if names is None:
        names = []
    if not isinstance(names, (list, tuple)):
        return None
    obj["bots"] = sorted(str(b) for b in names if isinstance(b, str) and b)
    obj["tip_sha"] = str(obj["tip_sha"])
    return obj


def clear_polish_state(pr, repo=None) -> bool:
    """Remove the per-PR stamp — the landed-PR cleanup, so a merged PR leaves nothing
    behind to age out. Best-effort: a missing file or an OS error is not an error.
    Returns True iff a file was actually removed."""
    try:
        os.remove(state_path(pr, repo))
        return True
    except OSError:
        return False
