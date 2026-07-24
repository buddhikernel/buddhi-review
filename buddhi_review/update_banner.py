"""Update-availability banner — one muted line at every skill-call launch surface.

When a newer Buddhi is available, the user should learn about it on the very next
``/review-pr`` / ``/open-pr`` they run — most users never re-open the setup wizard,
so the launch surface is the only place a stale install reliably surfaces. This
module emits ONE quiet, non-blocking line naming what is updatable and the exact
one-liner to act on it. It is silent when everything is current, shows at most once
per run, and is fully fail-open: any check error (offline PyPI, unreadable cache,
malformed data) yields no banner and never blocks or materially delays the launch.

Two independent sources feed the one line:

  * **Buddhi update** (PRIMARY): a newer ``buddhi-review`` release than the running
    version. A cheap, TTL-cached PyPI check — within the cache window it makes NO
    network call; when stale it does a single bounded refresh whose worst case is
    one short timeout per window (offline → fail-open, no banner). The one-liner is
    ``buddhi-review upgrade``, which detects how this copy was installed and does the
    method-correct thing (an OS-managed interpreter is told, never written to). The
    banner deliberately does NOT detect the install method itself — naming one command
    keeps it as cheap and as network-quiet as it is today.
  * **Claude review workflow notice** (SECONDARY): the ``claude-code-review.yml``
    INSTALLED in the reviewed repo is an older ``buddhi-managed-version`` than the
    bundled master copy (:func:`managed_files.needs_update` — a version-NUMBER
    comparison, never a content hash, so a user's customisations never phantom-fire
    it). Scoped to THAT ONE FILE: a Buddhi release or a pip bump that does not change
    the workflow's master copy never triggers it. The one-liner is to re-run
    ``/review-pr setup`` to update the workflow.

Applying either update stays the user's own existing flow — this module only NAMES
what is available; it never auto-installs, auto-launches, or phones home beyond the
single cached PyPI read.

Knobs (all env, all optional):
  * ``BUDDHI_NO_UPDATE_CHECK`` truthy — silence the banner entirely (and skip the
    network) for the run.
  * ``BUDDHI_UPDATE_STATE`` — override the cache-file path (used by tests).
  * ``BUDDHI_UPDATE_TTL_HOURS`` — cache freshness window (default 24h).
  * ``BUDDHI_UPDATE_TIMEOUT`` — the HARD wall-clock cap on the PyPI check, in seconds
    (default 1.5); the launch is never delayed beyond it even on a stalled resolver.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, TextIO

from buddhi_review import managed_files
from buddhi_review.transparency import _colour_enabled

# The published distribution name (PyPI) and the update one-liner the banner names.
# The one-liner is our OWN command, not a raw ``pip install -U``: ``upgrade`` detects
# the install method (editable checkout / pipx / uv tool / venv / OS-managed) and picks
# the right action, so a single method-blind string can never tell a user to run pip
# against an interpreter their OS package manager owns. Keeping the banner's text
# method-blind is deliberate — it stays as cheap and as quiet as it is today.
_DIST_NAME = "buddhi-review"
_PYPI_JSON_URL = f"https://pypi.org/pypi/{_DIST_NAME}/json"
_UPGRADE_CMD = f"{_DIST_NAME} upgrade"
# The free onboarding command the reader re-runs to refresh the installed workflow.
_SETUP_CMD = "/review-pr setup"

# The ONE managed file the secondary notice is scoped to — the Claude reviewer
# workflow. A stale OTHER managed file (e.g. the label-gated CI workflow) never
# triggers this banner.
_CLAUDE_REVIEW_FILE = "claude-code-review.yml"
_DEFAULT_WORKFLOW_LABEL = "Claude review workflow"

# Cache-freshness + bounded-fetch defaults (both env-overridable). The timeout is a
# HARD wall-clock cap on the whole PyPI check (see _bounded_fetch), so the launch is
# never delayed beyond it. The response body is size-capped as a memory backstop —
# PyPI's per-project JSON is a few tens of KB, well under this.
_DEFAULT_TTL_HOURS = 24.0
_DEFAULT_TIMEOUT_SECONDS = 1.5
_MAX_RESPONSE_BYTES = 5_000_000

# In-process "show at most once per run" guard. A skill call runs exactly one front
# door per process, so this is belt-and-braces — a second call in the same process
# stays silent. Tests reset it via :func:`_reset_run_guard`.
_emitted_this_run = False


def _reset_run_guard() -> None:
    """Clear the once-per-run guard (tests only)."""
    global _emitted_this_run
    _emitted_this_run = False


# ── Env knobs ─────────────────────────────────────────────────────────────────────

def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


def update_check_disabled() -> bool:
    """``BUDDHI_NO_UPDATE_CHECK`` truthy → no banner and no network for this run."""
    return _env_truthy("BUDDHI_NO_UPDATE_CHECK")


def _ttl_seconds() -> float:
    return max(0.0, _env_float("BUDDHI_UPDATE_TTL_HOURS", _DEFAULT_TTL_HOURS)) * 3600.0


def _timeout_seconds() -> float:
    t = _env_float("BUDDHI_UPDATE_TIMEOUT", _DEFAULT_TIMEOUT_SECONDS)
    return t if t > 0 else _DEFAULT_TIMEOUT_SECONDS


# ── Local cache (never phones home beyond the single bounded PyPI read) ────────────

def _state_path() -> Path:
    """The local cache file holding the last-known latest release + its check time.
    ``BUDDHI_UPDATE_STATE`` overrides the path (used by tests)."""
    override = os.environ.get("BUDDHI_UPDATE_STATE")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "buddhi" / "update-check.json"


def _read_state(path: Path) -> Dict:
    """Read the cache dict; a missing / unreadable / malformed file reads as ``{}``
    so a corrupt or absent cache never crashes the launch (fail-open to 'no record')."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, state: Dict) -> None:
    """Persist ``state`` atomically; best-effort. A write failure (read-only cache
    dir, full disk) is swallowed — the banner is never worth crashing a launch over,
    and the worst case of a lost write is one extra bounded check next run."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


# ── PRIMARY: newer buddhi-review release on PyPI ───────────────────────────────────

def _fetch_latest_from_pypi(timeout: float) -> Optional[str]:
    """One short HTTPS GET to PyPI's JSON API → the latest release string, or
    ``None`` on ANY failure (offline, timeout, non-200, malformed JSON). ``info.version``
    is PyPI's latest release (which may be a pre-release if no stable release exists).
    Any pre-release or suffixed version is filtered by :func:`update_available`, so
    a project publishing only pre-releases never triggers an update banner. Never raises.

    ``timeout`` is urllib's PER-SOCKET-OPERATION timeout — it bounds each connect/recv
    but NOT DNS resolution (``getaddrinfo`` runs before it) nor the total body-read
    time. The HARD wall-clock cap on the launch path is enforced separately by
    :func:`_bounded_fetch`; the response body is also size-capped here as a memory
    backstop against a pathological server."""
    import urllib.request

    try:
        req = urllib.request.Request(
            _PYPI_JSON_URL,
            headers={"Accept": "application/json",
                     "User-Agent": f"{_DIST_NAME}-update-check"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https URL
            payload = resp.read(_MAX_RESPONSE_BYTES)
        data = json.loads(payload.decode("utf-8"))
    except Exception:
        return None
    info = data.get("info") if isinstance(data, dict) else None
    version = info.get("version") if isinstance(info, dict) else None
    if isinstance(version, str) and version.strip():
        return version.strip()
    return None


def _bounded_fetch(timeout: float) -> Optional[str]:
    """Run :func:`_fetch_latest_from_pypi` under a HARD wall-clock cap of ``timeout``.

    urllib's socket timeout bounds each recv but NOT DNS resolution or the total
    body-read, so on a degraded network (a stalled resolver, a trickling response) the
    raw fetch can block far longer than ``timeout``. Because the check sits SYNCHRONOUSLY
    on the ``/review-pr`` / ``/open-pr`` launch path, we run it in a daemon thread and
    simply stop waiting once ``timeout`` elapses — the launch is never delayed beyond
    the cap, and an abandoned thread (daemon) dies on its own without blocking exit.
    Returns the fetched version, or ``None`` if it did not complete in time / failed."""
    import threading

    result: List[Optional[str]] = [None]

    def _worker() -> None:
        try:
            result[0] = _fetch_latest_from_pypi(timeout)
        except Exception:
            result[0] = None

    thread = threading.Thread(target=_worker, name="buddhi-update-check", daemon=True)
    thread.start()
    thread.join(timeout)
    # Whether the worker finished or we gave up at the cap, never wait past `timeout`.
    return result[0] if not thread.is_alive() else None


def latest_known(*, now: float, state_path: Path,
                 fetcher: Callable[[], Optional[str]], ttl_seconds: float) -> Optional[str]:
    """The latest known ``buddhi-review`` release string (or ``None``), cache-first.

    Within the TTL the cached value is returned with NO network call. When stale, a
    single bounded refresh runs; success updates the cache, a failure keeps any prior
    cached value — and EITHER way ``checked_at`` is stamped so a subsequent call
    inside the TTL never re-hits the network (bounding an offline host to one short
    timeout per window). Never raises."""
    state = _read_state(state_path)
    cached = state.get("latest")
    cached = cached.strip() if isinstance(cached, str) and cached.strip() else None
    checked_at = state.get("checked_at")
    if isinstance(checked_at, (int, float)) and (now - checked_at) < ttl_seconds:
        return cached  # fresh — reuse the cache, no network
    # Stale (or never checked) → one bounded refresh, fail-open.
    fetched: Optional[str] = None
    try:
        fetched = fetcher()
    except Exception:
        fetched = None
    new_state = dict(state)
    new_state["checked_at"] = now
    if isinstance(fetched, str) and fetched.strip():
        new_state["latest"] = fetched.strip()
    _write_state(state_path, new_state)
    latest = new_state.get("latest")
    return latest if isinstance(latest, str) and latest.strip() else None


def latest_release(*, now: Optional[float] = None, state_path: Optional[Path] = None,
                   fetcher: Optional[Callable[[], Optional[str]]] = None,
                   ttl_seconds: Optional[float] = None) -> Optional[str]:
    """The latest known ``buddhi-review`` release string, or ``None`` when unknown.

    The public, defaulted wrapper around :func:`latest_known` — same cache file, same
    TTL, same single bounded refresh, same fail-open contract. It exists so a caller
    that wants the verdict WITHOUT the muted banner (``buddhi-review upgrade --check``,
    which must speak up even when everything is current) reuses this module's cached
    check instead of opening a second, differently-behaved network path. ``None`` means
    "could not determine" — offline, unreadable cache, malformed payload — and is never
    to be reported as "you are up to date"."""
    try:
        now = now if now is not None else time.time()
        path = state_path if state_path is not None else _state_path()
        ttl = ttl_seconds if ttl_seconds is not None else _ttl_seconds()
        fetch = fetcher if fetcher is not None else (lambda: _bounded_fetch(_timeout_seconds()))
        return latest_known(now=now, state_path=path, fetcher=fetch, ttl_seconds=ttl)
    except Exception:
        return None


# ── Version comparison (fail-closed: uncertain → no banner) ─────────────────────────

_CLEAN_RELEASE_RE = re.compile(r"^\d+(?:\.\d+)*$")


def _release_tuple(version: Optional[str]):
    """A CLEAN release string (``1``, ``0.2``, ``0.10.3``) → its int tuple, else
    ``None``. A leading ``v`` is tolerated; ANY pre/dev/post/local suffix (``0.3.0rc1``,
    ``0.3.0.dev1``, ``0.3.0+local``) returns ``None`` so an uncertain compare
    fail-closes to 'no banner' rather than risking a phantom nag."""
    v = (version or "").strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    if not _CLEAN_RELEASE_RE.match(v):
        return None
    try:
        return tuple(int(part) for part in v.split("."))
    except ValueError:
        return None


def update_available(current: Optional[str], latest: Optional[str]) -> bool:
    """True ONLY when ``latest`` is a strictly-greater CLEAN release than ``current``.

    Numeric per-segment comparison (``0.10.0 > 0.9.0``), zero-padding shorter tuples
    (``0.2`` == ``0.2.0``). Fail-closed: a missing, suffixed, or unparseable version
    on either side → False, so a dev/local build (current suffixed) is never nagged
    and a pre-release latest is never pushed."""
    c = _release_tuple(current)
    l = _release_tuple(latest)
    if c is None or l is None:
        return False
    width = max(len(c), len(l))
    c += (0,) * (width - len(c))
    l += (0,) * (width - len(l))
    return l > c


# ── SECONDARY: the installed Claude review workflow is older than the master ────────

def _claude_review_spec() -> Optional[Dict[str, object]]:
    """The ``MANAGED_FILES`` entry for ``claude-code-review.yml``, or ``None``."""
    for spec in managed_files.MANAGED_FILES:
        if spec.get("name") == _CLAUDE_REVIEW_FILE:
            return spec
    return None


def workflow_out_of_date(cwd: Optional[str], *, spec: Optional[Dict[str, object]] = None) -> bool:
    """True iff the Claude review workflow INSTALLED under ``cwd`` exists AND carries a
    LOWER ``buddhi-managed-version`` than the shipped master (via
    :func:`managed_files.needs_update`).

    An ABSENT file → False: nothing is installed to update (that is a setup concern,
    not an update one). Scoped to ``claude-code-review.yml`` only. Never raises.

    Deliberately reads the LOCAL worktree copy, not the default branch (unlike
    :func:`wizard._installed_managed_file_text`, which needs the default-branch
    truth for a write decision). This is a cheap, local, no-extra-network check
    for a best-effort launch-time nudge — matching the module's fail-open, single
    -network-call design (see the module docstring). A worktree/default-branch
    version skew can make this nudge fire (or stay silent) a beat early/late; the
    worst case is a harmless "re-run setup" suggestion that the wizard's own
    default-branch check then confirms or no-ops. Adding a ``gh api`` round trip
    here to chase default-branch truth would trade that harmlessness for a real,
    uncached network dependency on every launch."""
    if not cwd:
        return False
    spec = spec if spec is not None else _claude_review_spec()
    if not spec:
        return False
    try:
        installed_path = Path(cwd) / str(spec["dest"])
        if not installed_path.is_file():
            return False
        installed_text = installed_path.read_text(encoding="utf-8")
    except OSError:
        return False
    installed = managed_files.file_version(installed_text)
    shipped = managed_files.shipped_version(spec["template"])  # type: ignore[arg-type]
    return managed_files.needs_update(installed, shipped)


# ── Rendering ──────────────────────────────────────────────────────────────────────

def format_banner(*, buddhi_latest: Optional[str] = None,
                  current: Optional[str] = None,
                  workflow_stale: bool = False,
                  workflow_label: str = _DEFAULT_WORKFLOW_LABEL) -> Optional[str]:
    """The uncoloured ONE-line banner naming each available update + how to apply it,
    or ``None`` when nothing is updatable. Pure — no env, no I/O, no network — so the
    exact text contract is the thing the tests pin."""
    parts: List[str] = []
    if buddhi_latest:
        have = f" (you have {current})" if current else ""
        parts.append(f"buddhi-review {buddhi_latest}{have} — run: {_UPGRADE_CMD}")
    if workflow_stale:
        parts.append(f"{workflow_label} is out of date — re-run {_SETUP_CMD} to update it")
    if not parts:
        return None
    return "↑ Update available — " + "   ·   ".join(parts)


def _emit(text: str, stream: TextIO) -> None:
    # Dim so the banner reads as a quiet, transient aside at launch — never as an
    # error or a blocking prompt. (Colour auto-strips off a non-TTY / under NO_COLOR.)
    line = f"\033[2m{text}\033[0m" if _colour_enabled(stream) else text
    print(line, file=stream)


# ── The orchestrator ────────────────────────────────────────────────────────────────

def maybe_emit_update_banner(
    *,
    cwd: Optional[str] = None,
    stream: Optional[TextIO] = None,
    current_version: Optional[str] = None,
    now: Optional[float] = None,
    state_path: Optional[Path] = None,
    fetcher: Optional[Callable[[], Optional[str]]] = None,
    ttl_seconds: Optional[float] = None,
    check_pypi: bool = True,
    workflow_spec: Optional[Dict[str, object]] = None,
    once: bool = True,
) -> Optional[str]:
    """Emit ONE muted update banner to ``stream`` (default stderr) at a skill-call
    launch surface, iff a newer ``buddhi-review`` release and/or an outdated installed
    Claude review workflow is detected. Returns the emitted text (also for tests), or
    ``None`` when nothing was shown.

    Contract: silent when everything is current; exactly one line when it fires; shows
    at most once per process (``once``); and FULLY fail-open — any error yields no
    banner, and the launch is never blocked or delayed beyond the bounded PyPI timeout
    (incurred at most once per TTL). Every external effect is injectable
    (``stream``/``now``/``state_path``/``fetcher``/``ttl_seconds``/``current_version``/
    ``workflow_spec``) so the whole path is unit-testable with no terminal, clock,
    cache file, or network."""
    global _emitted_this_run
    try:
        if update_check_disabled():
            return None
        if once and _emitted_this_run:
            return None
        stream = stream if stream is not None else sys.stderr
        now = now if now is not None else time.time()
        path = state_path if state_path is not None else _state_path()
        current = current_version if current_version is not None else _current_version()

        # PRIMARY — a newer buddhi-review release (cache-first, fail-open).
        buddhi_latest: Optional[str] = None
        if check_pypi:
            ttl = ttl_seconds if ttl_seconds is not None else _ttl_seconds()
            fetch = fetcher if fetcher is not None else (
                lambda: _bounded_fetch(_timeout_seconds()))
            latest = latest_known(now=now, state_path=path, fetcher=fetch, ttl_seconds=ttl)
            if update_available(current, latest):
                buddhi_latest = latest

        # SECONDARY — the installed Claude review workflow is older than the master.
        spec = workflow_spec if workflow_spec is not None else _claude_review_spec()
        workflow_stale = workflow_out_of_date(cwd, spec=spec)
        label = str((spec or {}).get("label") or _DEFAULT_WORKFLOW_LABEL)

        text = format_banner(buddhi_latest=buddhi_latest, current=current,
                             workflow_stale=workflow_stale, workflow_label=label)
        if not text:
            return None
        _emit(text, stream)
        if once:
            _emitted_this_run = True
        return text
    except Exception:
        # Fail-open: an update banner is never worth blocking or crashing a launch.
        return None


def _current_version() -> Optional[str]:
    """The running ``buddhi-review`` version, or ``None`` if it cannot be read."""
    try:
        from buddhi_review import __version__
        return __version__
    except Exception:
        return None
