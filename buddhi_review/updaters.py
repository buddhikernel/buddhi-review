"""Updater discovery + install-method detection — the safety core of ``upgrade``.

``buddhi-review upgrade`` runs a package manager against the user's OWN Python
environment. That is the most destructive thing this skill can do, so the module is
built around ONE rule: **never write to an environment we cannot positively identify
as a safe, owned target.** Everything else — the method table, the entry-points seam
— hangs off that rule.

Two independent pieces live here:

  * **Install-method detection** (:func:`detect_install_method`) — figure out how THIS
    interpreter's ``buddhi-review`` got installed (editable checkout / pipx / uv tool /
    plain venv / an OS-managed interpreter) and turn that into an :class:`UpgradePlan`:
    either a concrete list of steps to run, or ``notify_only`` — the exact command the
    user should run themselves, with nothing executed on their behalf.
  * **The updaters seam** (:data:`UPDATERS_GROUP`) — the same shape as the backends
    seam: a generic entry-points group, a duck-typed Protocol, discovery that always
    includes the built-in :class:`FreeUpdater` and treats a broken third-party entry
    point as SKIPPED rather than fatal. The lone shared string is the GROUP name; this
    module names, imports, and knows about no other package.

**The safety gate is NOT the seam's to bypass.** ``cli._upgrade`` runs
:func:`detect_install_method` and honours ``notify_only`` BEFORE any discovered updater
is selected or run, so no installed package can talk this command into pip-installing
into an OS-managed interpreter. A separately-installed updater exists to update ITS OWN
package; it never gets a say in how ``buddhi-review`` itself is upgraded.

Detection precedence is editable → pipx → uv tool → venv → OS-managed, but precedence
never overrides the gate:

  * the safety question is asked about the interpreter we would actually WRITE to
    (``sys.prefix``), so an editable checkout installed into an OS-managed interpreter
    is still notify-only;
  * a virtual environment is a safe target even when the interpreter it was built from
    is OS-managed — the marker is only consulted OUTSIDE a venv (the same stance pip
    itself takes), because refusing to upgrade a venv on a Debian-family host would be
    both wrong and the common case;
  * an ordinary non-venv interpreter with NO marker is UNCERTAIN, which is also
    notify-only — "not positively identified as safe" and "known unsafe" get the same
    treatment.

When a plan DOES execute pip it always runs it as ``<python> -m pip``, never the
``pip`` console script (which is locked on Windows while it is running).
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

#: The entry-points group an updater registers under. This generic string is the ONLY
#: coupling between this skill and any separately-installed updater.
UPDATERS_GROUP = "buddhi_review.updaters"

#: The published distribution name this command upgrades.
DIST_NAME = "buddhi-review"

# ── Install-method kinds ────────────────────────────────────────────────────────────
EDITABLE = "editable"
PIPX = "pipx"
UV_TOOL = "uv-tool"
VENV = "venv"
SYSTEM = "system"
UNCERTAIN = "uncertain"

#: The PEP 668 marker filename that says "this interpreter belongs to the OS".
EXTERNALLY_MANAGED = "EXTERNALLY-MANAGED"
#: Markers identifying a pipx-managed / uv-tool-managed environment root.
_PIPX_MARKER = "pipx_metadata.json"
_UV_TOOL_MARKER = "uv-receipt.toml"


# ── The plan ────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Step:
    """One command in an upgrade plan.

    ``soft_fail`` marks a step whose failure means "we could not safely proceed", NOT
    "the upgrade broke": the only such step is the editable path's ``git pull``, where a
    failure leaves the checkout exactly as it was. Per the editable contract, that
    degrades to the notify-only outcome — print the manual commands, change nothing
    else, exit success — rather than reporting a broken upgrade.
    """

    argv: Tuple[str, ...]
    cwd: Optional[str] = None
    soft_fail: bool = False

    def display(self) -> str:
        line = shlex.join(self.argv)
        return f"{line}   (in {self.cwd})" if self.cwd else line


@dataclass(frozen=True)
class UpgradePlan:
    """How this installation would be upgraded — or why it will not be, by us.

    ``notify_only`` is the safety verdict: True means we print ``manual`` and execute
    NOTHING. ``steps`` is empty whenever ``notify_only`` is True, so a caller that
    ignores the flag still runs nothing — the invariant is belt-and-braces, asserted in
    :func:`_plan`.
    """

    method: str
    notify_only: bool
    reason: str
    steps: Tuple[Step, ...] = ()
    manual: Tuple[str, ...] = ()
    source_dir: Optional[str] = None


def _plan(method: str, *, notify_only: bool, reason: str,
          steps: Sequence[Step] = (), manual: Sequence[str] = (),
          source_dir: Optional[str] = None) -> UpgradePlan:
    """Build an :class:`UpgradePlan`, enforcing the "notify-only runs nothing" invariant."""
    return UpgradePlan(
        method=method,
        notify_only=notify_only,
        reason=reason,
        steps=() if notify_only else tuple(steps),
        manual=tuple(manual),
        source_dir=source_dir,
    )


# ── Environment probes (every one injectable, so the matrix is unit-testable) ────────

def _default_git(argv: Sequence[str], cwd: Optional[str]) -> Tuple[int, str]:
    """Run a READ-ONLY git command and return ``(returncode, stdout)``. Never raises:
    a missing binary / unreadable tree reads as a non-zero code, which every caller
    treats as "cannot confirm" → notify-only."""
    try:
        proc = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True)
    except Exception:
        return 1, ""
    return proc.returncode, proc.stdout or ""


def _distribution():
    """The installed ``buddhi-review`` distribution, via ``importlib.metadata``.

    Deliberately NOT a hand-scan of ``site-packages`` for ``*.dist-info``: the metadata
    API already resolves the distribution that THIS interpreter imports, which is
    exactly the one an upgrade would replace."""
    import importlib.metadata as md

    return md.distribution(DIST_NAME)


def _editable_source(dist: Any) -> Optional[str]:
    """The source tree of an EDITABLE install, or ``None``.

    Reads the dist-info ``direct_url.json`` (PEP 610) and returns the local directory
    only when ``dir_info`` marks the install editable and the URL is a ``file://``
    directory URL. Absent, malformed, or non-local metadata reads as "not editable"."""
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    dir_info = data.get("dir_info")
    if not isinstance(dir_info, dict):
        return None
    editable = dir_info.get("editable")
    # PEP 610 specifies a JSON boolean, and pip writes one — but the two failure
    # directions here are NOT symmetric, so the truthiness test is deliberately a
    # little wider than the spec. Reading a real editable install as "not editable"
    # sends it down the PyPI branch, which DETACHES the user's checkout; reading a
    # non-editable install as editable merely re-installs it from its own directory.
    # So a writer that recorded ``1`` or ``"true"`` is honoured, while ``False`` /
    # ``0`` / ``"false"`` / a missing key still read as not editable.
    if not (editable is True or editable == 1
            or (isinstance(editable, str) and editable.strip().lower() == "true")):
        return None
    url = data.get("url")
    if not isinstance(url, str) or not url.startswith("file://"):
        return None
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    try:
        path = url2pathname(urlparse(url).path)
    except Exception:
        return None
    return path or None


def _is_file(path: Path) -> bool:
    """``path.is_file()``, with EVERY ``OSError`` read as "no".

    ``Path.is_file`` only swallows the not-there family (ENOENT / ENOTDIR / EBADF /
    ELOOP); a permission error on a parent directory (EACCES / EPERM) propagates. Each
    of these probes runs against a directory nobody promised us access to, so an
    unreadable parent must read as "this marker is absent" rather than take detection
    down with an exception. Every marker probe goes through here so no one of them can
    drift back to the bare call."""
    try:
        return path.is_file()
    except OSError:
        return False


def _is_uv_tool(prefix: Path) -> bool:
    """True for a uv-managed tool environment, identified SOLELY by uv's own receipt.

    There is deliberately no path-shape fallback. A uv tool environment lives at
    ``<uv-data>/tools/<name>``, but so may an ordinary virtualenv somebody chose to keep
    there — the two are indistinguishable by path alone, so any layout heuristic tight
    enough to be meaningful still mis-fires on a real venv at that path. The failure it
    produces is quiet: ``uv tool upgrade`` would upgrade whatever uv manages (or
    nothing) and exit 0, and we would report a successful upgrade while the environment
    the user is actually running stayed untouched. The receipt is written by
    ``uv tool install`` and is the one signal that positively identifies the
    environment, so it is the only one consulted. A uv tool environment somehow missing
    its receipt falls through to the venv branch, which pip-upgrades it in place — the
    conservative, still-correct outcome."""
    return _is_file(prefix / _UV_TOOL_MARKER)


def _externally_managed(stdlib_dir: str) -> bool:
    """True when the PEP 668 ``EXTERNALLY-MANAGED`` marker sits beside the stdlib."""
    return _is_file(Path(stdlib_dir) / EXTERNALLY_MANAGED)


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


# ── Detection ───────────────────────────────────────────────────────────────────────

_RESYNC_HINT = "buddhi-review install-skills"


def _pip_upgrade_argv(python: str) -> Tuple[str, ...]:
    # ``<python> -m pip``, never the ``pip`` console script: on Windows the running
    # script's own executable is locked and pip cannot replace it.
    return (python, "-m", "pip", "install", "-U", DIST_NAME)


def detect_install_method(
    *,
    dist_fn: Optional[Callable[[], Any]] = None,
    prefix: Optional[str] = None,
    base_prefix: Optional[str] = None,
    stdlib_dir: Optional[str] = None,
    python: Optional[str] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    git: Optional[Callable[[Sequence[str], Optional[str]], Tuple[int, str]]] = None,
) -> UpgradePlan:
    """Classify how this ``buddhi-review`` was installed and return its upgrade plan.

    Every external fact is injectable — the distribution metadata, ``sys.prefix`` /
    ``sys.base_prefix``, the stdlib directory the PEP 668 marker lives beside, the
    interpreter path, ``shutil.which``, and the read-only git probe — so the whole
    detection matrix is exercised in tests without an installed package, a venv, or a
    git repository.

    The result is either an executable plan or ``notify_only`` guidance. Nothing here
    ever writes, installs, fetches, or mutates: the git probes are read-only and the
    upgrade steps are returned, not run.
    """
    prefix = prefix if prefix is not None else sys.prefix
    base_prefix = base_prefix if base_prefix is not None else sys.base_prefix
    stdlib_dir = stdlib_dir if stdlib_dir is not None else sysconfig.get_path("stdlib")
    python = python if python is not None else sys.executable
    which = which or shutil.which
    git = git or _default_git

    pip_manual = shlex.join(_pip_upgrade_argv(python))
    in_venv = not _same_path(prefix, base_prefix)

    # ── Which method? ──────────────────────────────────────────────────────────────
    try:
        dist = dist_fn() if dist_fn is not None else _distribution()
    except Exception:
        dist = None
    if dist is None:
        # Malformed or absent metadata: we cannot say what would be replaced.
        return _plan(
            UNCERTAIN, notify_only=True,
            reason=("buddhi-review's own install metadata could not be read, so the "
                    "install method is unknown — nothing was run."),
            manual=(pip_manual, _RESYNC_HINT),
        )

    source_dir = _editable_source(dist)
    prefix_path = Path(prefix)
    if source_dir is not None:
        method = EDITABLE
    elif _is_file(prefix_path / _PIPX_MARKER):
        method = PIPX
    elif _is_uv_tool(prefix_path):
        method = UV_TOOL
    elif in_venv:
        method = VENV
    elif _externally_managed(stdlib_dir):
        method = SYSTEM
    else:
        method = UNCERTAIN

    # ── The safety gate: is the TARGET we would write to an owned environment? ──────
    # Asked about sys.prefix, never the base interpreter — a venv built from an
    # OS-managed Python is still a safe, user-owned target, and that is the common
    # case on Debian-family hosts. Outside a venv nothing is safe: with the marker it
    # is OS-managed, without it we simply cannot classify it, and both answers mean
    # notify-only.
    if not in_venv:
        if method == EDITABLE:
            manual = _editable_manual(source_dir or "<source-dir>", python)
        else:
            manual = (pip_manual,)
        if _externally_managed(stdlib_dir):
            reason = ("This Python is managed by your operating system's package "
                      "manager (PEP 668), so buddhi-review will not run an installer "
                      "against it.")
        else:
            reason = ("This Python is not a virtual environment and could not be "
                      "positively identified as a safe upgrade target, so buddhi-review "
                      "will not run an installer against it.")
        return _plan(method, notify_only=True, reason=reason,
                     manual=manual + (_RESYNC_HINT,), source_dir=source_dir)

    # ── Safe target: build the method's concrete steps. ─────────────────────────────
    if method == EDITABLE:
        return _editable_plan(source_dir or "", python=python, which=which, git=git)
    if method == PIPX:
        return _tool_plan(PIPX, "pipx", ("pipx", "upgrade", DIST_NAME),
                          which=which, pip_manual=pip_manual)
    if method == UV_TOOL:
        return _tool_plan(UV_TOOL, "uv", ("uv", "tool", "upgrade", DIST_NAME),
                          which=which, pip_manual=pip_manual)
    return _plan(
        VENV, notify_only=False,
        reason="Installed in a virtual environment — upgrading it in place.",
        steps=(Step(_pip_upgrade_argv(python)),),
        manual=(pip_manual, _RESYNC_HINT),
    )


def _tool_plan(method: str, binary: str, argv: Sequence[str], *,
               which: Callable[[str], Optional[str]], pip_manual: str) -> UpgradePlan:
    """A plan that delegates to an external tool manager, or notify-only when that
    tool's binary is not on PATH (we will not guess a different upgrade route for an
    environment somebody else owns)."""
    manual = (shlex.join(argv), _RESYNC_HINT)
    # Falsy, not ``is None``: a lookup that answers with an empty string has not found
    # a runnable binary either, and treating it as found would exec an empty argv[0].
    if not which(binary):
        return _plan(
            method, notify_only=True,
            reason=(f"This install is managed by {binary}, but {binary} is not on "
                    f"PATH — nothing was run."),
            manual=manual,
        )
    return _plan(
        method, notify_only=False,
        reason=f"Installed by {binary} — upgrading through {binary}.",
        steps=(Step(tuple(argv)),),
        manual=manual,
    )


def _editable_manual(source_dir: str, python: str) -> Tuple[str, ...]:
    return (
        shlex.join(("git", "-C", source_dir, "pull")),
        shlex.join((python, "-m", "pip", "install", "-e", source_dir)),
    )


def _editable_plan(source_dir: str, *, python: str,
                   which: Callable[[str], Optional[str]],
                   git: Callable[[Sequence[str], Optional[str]], Tuple[int, str]]) -> UpgradePlan:
    """The editable (git checkout) plan: ``git pull`` then a re-install in place.

    An editable install is NEVER upgraded from PyPI — that would silently detach the
    user's own checkout from the package they import. If the checkout is not in a
    state we can safely advance (missing, not a git repository, dirty, on a detached
    HEAD, or git itself unavailable) the plan degrades to notify-only with the exact
    manual commands. We never stash, reset, force, or check out anything.
    """
    manual = _editable_manual(source_dir or "<source-dir>", python) + (_RESYNC_HINT,)

    def refuse(reason: str) -> UpgradePlan:
        return _plan(EDITABLE, notify_only=True, reason=reason, manual=manual,
                     source_dir=source_dir or None)

    if not source_dir:
        return refuse("This is an editable install but its source tree could not be "
                      "located — nothing was run.")
    if not which("git"):
        return refuse("This is an editable install but git is not on PATH — nothing "
                      "was run.")
    try:
        exists = Path(source_dir).is_dir()
    except OSError:
        exists = False
    if not exists:
        return refuse(f"This is an editable install but its source tree is missing "
                      f"({source_dir}) — nothing was run.")

    rc, out = git(("git", "-C", source_dir, "rev-parse", "--is-inside-work-tree"), source_dir)
    if rc != 0 or out.strip() != "true":
        return refuse(f"This is an editable install but {source_dir} is not a git "
                      f"repository — nothing was run.")
    rc, out = git(("git", "-C", source_dir, "status", "--porcelain"), source_dir)
    if rc != 0:
        return refuse(f"This is an editable install but the state of {source_dir} "
                      f"could not be read — nothing was run.")
    if out.strip():
        return refuse(f"This is an editable install and {source_dir} has uncommitted "
                      f"changes — nothing was run, so your work is untouched.")
    rc, _out = git(("git", "-C", source_dir, "symbolic-ref", "-q", "HEAD"), source_dir)
    if rc != 0:
        return refuse(f"This is an editable install and {source_dir} is on a detached "
                      f"HEAD — nothing was run.")

    return _plan(
        EDITABLE, notify_only=False,
        reason=f"Editable install from {source_dir} — pulling and re-installing in place.",
        steps=(
            # A failed pull leaves the checkout exactly as it was, so it degrades to
            # the same manual guidance instead of reporting a broken upgrade.
            # --ff-only: a plain `pull` obeys the user's merge/rebase config, which can
            # create a merge commit, rewrite local commits, or leave conflict markers —
            # none of which match the soft_fail message below. --ff-only either fast-
            # forwards cleanly or aborts untouched, so "nothing was changed" stays true.
            Step(("git", "-C", source_dir, "pull", "--ff-only"), cwd=source_dir, soft_fail=True),
            Step((python, "-m", "pip", "install", "-e", source_dir), cwd=source_dir),
        ),
        manual=manual,
        source_dir=source_dir,
    )


# ── The updaters seam ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UpdateOutcome:
    """What an updater did.

    ``returncode`` is the exit code the command should return. ``upgraded`` is the
    only thing the caller keys the skill re-sync off: True means the package on disk
    may have changed (INCLUDING the already-current case, where re-syncing the skills
    is exactly the documented repair path), False means nothing was installed and the
    re-sync must be skipped.
    """

    returncode: int
    upgraded: bool
    message: Optional[str] = None


@runtime_checkable
class Updater(Protocol):
    """Something that can perform an upgrade. It answers exactly two questions:

      * :meth:`is_active` — should I handle upgrades right now? (a plain yes/no the
        updater decides for itself, exactly as a backend does);
      * :meth:`run_update` — carry out the given :class:`UpgradePlan` and report the
        outcome.

    An updater MAY also expose two optional attributes, both read through ``getattr``
    with safe defaults so a minimal updater still works:

      * ``name``     — a short identifier, used for de-duplication / messages;
      * ``priority`` — higher wins when several updaters are active (default ``0``).

    The plan handed to :meth:`run_update` has ALREADY passed the caller's safety gate;
    an updater never re-decides whether upgrading this environment is allowed, and a
    separately-installed updater is there to update its OWN package, not this one.
    """

    def is_active(self) -> bool: ...

    def run_update(self, plan: UpgradePlan, **opts: Any) -> Any: ...


def _default_step_runner(argv: Sequence[str], cwd: Optional[str] = None) -> int:
    """Run one upgrade step in the foreground and return its exit code. Output is left
    on the user's terminal — a package upgrade should be visible while it happens."""
    try:
        return subprocess.run(list(argv), cwd=cwd).returncode
    except Exception:
        return 1


class FreeUpdater:
    """The built-in updater — always available, lowest priority.

    It executes the plan's steps in order and stops at the first failure. It makes NO
    safety judgement of its own: by the time it runs, the caller has already decided
    this environment may be written to.
    """

    name = "free"
    priority = 0

    def is_active(self) -> bool:
        # The built-in updater is the always-present fallback: active whenever asked.
        return True

    def run_update(self, plan: UpgradePlan, **opts: Any) -> UpdateOutcome:
        runner = opts.get("runner") or _default_step_runner
        if plan.notify_only or not plan.steps:
            # Defensive: a notify-only plan carries no steps, so there is nothing to
            # run and nothing was installed.
            return UpdateOutcome(0, upgraded=False,
                                 message="Nothing was run for this install method.")
        for step in plan.steps:
            rc = runner(step.argv, step.cwd)
            if rc == 0:
                continue
            if step.soft_fail:
                return UpdateOutcome(
                    0, upgraded=False,
                    message=(f"`{step.display()}` did not succeed, so nothing was "
                             f"changed. Run the upgrade yourself:"),
                )
            return UpdateOutcome(
                rc or 1, upgraded=False,
                message=f"Upgrade step failed: {step.display()}",
            )
        return UpdateOutcome(0, upgraded=True)


def _iter_entry_points(group: str) -> list:
    """List entry points in ``group``, across the 3.9 / 3.10+ API split."""
    import importlib.metadata as md

    try:
        eps = md.entry_points(group=group)            # Python >= 3.10
    except TypeError:
        eps = md.entry_points().get(group, [])        # Python 3.9
    return list(eps)


def discover_updaters(*, entry_points_fn: Optional[Callable[[str], list]] = None) -> List[Updater]:
    """Discover every registered updater, always including the built-in free one.

    A broken third-party updater (import error, bad constructor, wrong shape) is
    SKIPPED, never fatal — an installed package must never be able to break the free
    upgrade command. The free updater is normally registered via entry points too, so
    the built-in instance is appended only when discovery did not already surface one
    (a source checkout / test run has no entry points at all).
    """
    eps = (entry_points_fn or _iter_entry_points)(UPDATERS_GROUP)
    found: List[Updater] = []
    for ep in eps:
        try:
            obj = ep.load()
            # isinstance(obj, type) handles class-based updaters (the normal case);
            # callable(obj) covers factory functions. The isinstance(Updater) guard is
            # excluded from the callable branch because a runtime_checkable Protocol
            # check against a CLASS object (not an instance) can pass on matching
            # attribute names and would wrongly skip the call.
            if isinstance(obj, type):
                updater = obj()
            elif callable(obj) and not isinstance(obj, Updater):
                updater = obj()
            else:
                updater = obj
            if not isinstance(updater, Updater):
                continue
        except Exception:
            continue
        found.append(updater)
    if not any(_safe_name(u) == FreeUpdater.name for u in found):
        found.append(FreeUpdater())
    return found


def _safe_name(updater: Any) -> Optional[str]:
    """An updater's ``name`` as a plain ``str``, or ``None``.

    Two distinct third-party hazards are closed here, and returning the RAW value would
    leave both open:

      * ``getattr(obj, "name", default)`` only swallows :class:`AttributeError`, so a
        ``name`` property raising anything else would take the command down;
      * a non-string value carries the caller's own operations into third-party code —
        its ``__eq__`` runs in :func:`discover_updaters`' identity test (outside any
        ``try``, which would make a broken entry point FATAL and break this module's
        "skipped, never fatal" invariant) and its ``__str__`` / ``__format__`` runs in
        the failure handler's message (turning a HANDLED failure into an unhandled
        crash, immediately after an updater has begun changing the install).

    Coercing to ``str`` or ``None`` means every downstream use touches only built-in
    behaviour."""
    try:
        value = getattr(updater, "name", None)
    except Exception:
        return None
    return value if isinstance(value, str) else None


def _is_active(updater: Updater) -> bool:
    try:
        return bool(updater.is_active())
    except Exception:
        # An updater that errors while answering is treated as inactive — the free
        # command keeps working rather than crashing on a third-party fault.
        return False


def _safe_priority(updater: Updater) -> int:
    try:
        return int(getattr(updater, "priority", 0))
    except Exception:
        return 0


def select_updater(updaters: List[Updater]) -> Updater:
    """The highest-priority updater whose :meth:`Updater.is_active` is True. The free
    updater is always active at priority 0, so this never fails; the fallback
    :class:`FreeUpdater` is a belt-and-braces guard."""
    active = [u for u in updaters if _is_active(u)]
    if not active:
        return FreeUpdater()
    active.sort(key=_safe_priority, reverse=True)
    return active[0]


def _coerce_outcome(value: Any) -> UpdateOutcome:
    """Normalise whatever an updater returned into an :class:`UpdateOutcome`.

    A plain int is read as an exit code (0 → upgraded). Anything else — including
    ``None``, the Python convention for "finished fine" — is read as success; the only
    consequence of guessing generously here is an extra idempotent skill re-sync, which
    never clobbers a user's edited skill."""
    if isinstance(value, UpdateOutcome):
        return value
    if isinstance(value, bool):
        return UpdateOutcome(0 if value else 1, upgraded=bool(value))
    if isinstance(value, int):
        return UpdateOutcome(value, upgraded=value == 0)
    return UpdateOutcome(0, upgraded=True)


def perform_update(updater: Updater, plan: UpgradePlan, **opts: Any) -> UpdateOutcome:
    """Run ``updater`` against ``plan`` and normalise the result.

    An updater that RAISES is reported as a clean failure (non-zero, no re-sync) rather
    than silently re-run through the free updater: the upgrade may have partially
    applied, and running a second installer over a half-finished one is exactly the
    state this command exists to avoid.
    """
    try:
        return _coerce_outcome(updater.run_update(plan, **opts))
    except Exception as exc:
        # Every part of the message is built from something that cannot itself raise: a
        # hostile ``name`` (property, ``__str__``, or ``__format__``) or ``__repr__``
        # must not turn a handled failure into an unhandled one — least of all here,
        # just after a third-party updater has started changing the installation.
        # ``object.__repr__`` is called unbound so an overridden ``__repr__`` is bypassed.
        name = _safe_name(updater) or object.__repr__(updater)
        try:
            detail = repr(exc)
        except Exception:
            detail = type(exc).__name__
        return UpdateOutcome(
            1, upgraded=False,
            message=f"Updater {name} failed ({detail}); nothing further was run.",
        )
