"""Backend discovery + the launch-layer dispatcher — the front door.

This is the single place that decides WHICH engine runs a review loop. The skill
ships exactly one backend, :class:`FreeBackend`, which launches the console review
loop. Other engines may install themselves as separate packages and register
through the standard Python entry-points group :data:`BACKENDS_GROUP`; the
dispatcher discovers them, asks each whether it is active, and runs the
highest-priority active one. With nothing extra installed (the normal state),
discovery finds only the free backend and the loop runs free — today's behavior.

The seam is deliberately generic and capability-neutral:

  * it makes NO availability judgement of its own — it only calls a backend's own
    :meth:`Backend.is_active`, a yes/no question each backend answers for itself;
  * it knows NOTHING about any specific other backend — no import, no name, no
    feature reference; the lone shared string is the entry-point GROUP, the same
    mechanism any extensible Python application uses;
  * a backend decides entirely on its own whether it is "active"; this module never
    asks why and runs no checks of its own beyond reading that yes/no answer.

Because the chosen backend is selected BEFORE it runs, each backend owns its own
"where to watch" confirmation line: the free backend's launcher prints a terminal
log link, and a different backend would print whatever it points the user at. The
front door never prints that line itself.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional, Protocol, runtime_checkable

#: The entry-points group a backend registers under. This generic string is the
#: ONLY coupling between this skill and any separately-installed backend.
BACKENDS_GROUP = "buddhi_review.backends"


@runtime_checkable
class Backend(Protocol):
    """A review-loop backend. It answers exactly two questions:

      * :meth:`is_active` — should I handle review loops right now? (a plain yes/no
        the backend decides for itself);
      * :meth:`run_review_loop` — launch the loop for one PR and return an exit code
        (``0`` once the loop is launched).

    A backend MAY also expose two optional attributes the dispatcher reads to order
    candidates; both default safely when absent:

      * ``name``     — a short identifier, used only for de-duplication / logging;
      * ``priority`` — higher wins when several backends are active (default ``0``).
    """

    def is_active(self) -> bool: ...

    def run_review_loop(self, pr: str, repo: Optional[str], cwd: Optional[str],
                        **opts: Any) -> int: ...


# ── The free backend ────────────────────────────────────────────────────────────

class FreeBackend:
    """The free console review loop — always available, lowest priority.

    :meth:`run_review_loop` detaches the loop through the bundled
    ``launch-review.sh`` and returns immediately. That launcher prints the free
    "where to watch" hero line (a terminal-log link) — and it runs only AFTER the
    dispatcher has chosen this backend, so the line is never printed ahead of the
    decision.
    """

    name = "free"
    priority = 0

    def is_active(self) -> bool:
        # The free backend is the always-present fallback: active whenever asked.
        return True

    def run_review_loop(self, pr: str, repo: Optional[str], cwd: Optional[str],
                        **opts: Any) -> int:
        out = opts.pop("out", None)
        err = opts.pop("err", None)
        runner = opts.pop("runner", None)
        launcher = opts.pop("launcher", None)
        return launch_free_loop(pr, repo, cwd, out=out, err=err, runner=runner,
                                launcher=launcher, **opts)


def _loop_argv(pr: str, repo: Optional[str], cwd: Optional[str], opts: dict) -> List[str]:
    """Translate the review-flag opts into the ``run-loop`` CLI args that
    ``launch-review.sh`` forwards to the detached engine."""
    argv: List[str] = [str(pr)]
    if repo:
        argv += ["--repo", repo]
    if cwd:
        argv += ["--cwd", cwd]
    auto_merge = opts.get("auto_merge")
    if auto_merge is not None:
        argv.append("--auto-merge" if auto_merge else "--no-auto-merge")
    verify = opts.get("verify_fixes")
    if verify:
        argv += ["--verify-fixes", str(verify)]
    max_rounds = opts.get("max_rounds")
    if max_rounds is not None:
        argv += ["--max-rounds", str(max_rounds)]
    tfm = opts.get("test_failure_mode")
    if tfm:
        argv += ["--test-failure-mode", str(tfm)]
    fpd = opts.get("fix_pr_description")
    if fpd is not None:
        argv.append("--fix-pr-description" if fpd else "--no-fix-pr-description")
    if opts.get("rr"):
        argv.append("--rr")
    if opts.get("rr_active"):
        argv.append("--rr-active")
    if opts.get("rr_none"):
        argv.append("--rr-none")
    return argv


def _detached_run(cmd: List[str], stdout: Any = None, stderr: Any = None) -> None:
    """Run the launcher, routing its output to the specified streams."""
    subprocess.run(cmd, check=True, stdout=stdout or sys.stdout, stderr=stderr or sys.stderr)


def launch_free_loop(pr: str, repo: Optional[str], cwd: Optional[str], *,
                     out=None, err=None, runner: Optional[Callable[[List[str]], None]] = None,
                     launcher: Optional[str] = None, **opts: Any) -> int:
    """Detach the free review loop via ``launch-review.sh`` and return the exit code.

    Returns ``0`` on success (launcher runs cleanly), or non-zero if the launcher
    refuses (e.g., a startup gate blocks it, exit 2) or fails to spawn.

    ``launch-review.sh`` writes the per-PR log, prints the free hero line, and
    ``nohup``s ``python -m buddhi_review run-loop`` (the in-process engine). The
    ``runner`` / ``launcher`` seams keep this unit-testable without spawning.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    launcher_path = Path(launcher) if launcher else (Path(__file__).parent / "launch-review.sh")
    manual = "python3 -m buddhi_review run-loop " + shlex.join(_loop_argv(pr, repo, cwd, opts))
    if not launcher_path.exists():
        print(f"⚠ launcher not found at {launcher_path}; launch the loop manually: {manual}",
              file=err)
        return 1
    cmd = ["bash", str(launcher_path), *_loop_argv(pr, repo, cwd, opts)]
    run = runner or (lambda c: _detached_run(c, stdout=out, stderr=err))
    try:
        run(cmd)
    except subprocess.CalledProcessError as exc:
        # The launcher RAN and exited non-zero (e.g. a startup gate refused, exit 2).
        # It has already printed its own reason to err — the in-session refusal panel
        # — so propagate its exit code WITHOUT a misleading generic "could not launch"
        # line on top. This is what lets a detached-gate refusal surface as a clean
        # non-zero front-door result instead of a fake spawn failure.
        return (128 + abs(exc.returncode)) if exc.returncode < 0 else (exc.returncode or 1)
    except Exception as exc:  # the launcher could not be spawned at all
        print(f"⚠ could not launch the review loop ({exc}); run it manually: {manual}",
              file=err)
        return 1
    return 0


# ── Discovery + selection + the dispatcher ────────────────────────────────────────

def _iter_entry_points(group: str) -> list:
    """List entry points in ``group``, across the 3.9 / 3.10+ API split."""
    import importlib.metadata as md
    try:
        eps = md.entry_points(group=group)            # Python >= 3.10
    except TypeError:
        eps = md.entry_points().get(group, [])        # Python 3.9
    return list(eps)


def discover_backends(*, entry_points_fn: Optional[Callable[[str], list]] = None) -> List[Backend]:
    """Discover every registered backend, always including the built-in free one.

    A broken third-party backend (import error, bad constructor) is skipped, never
    fatal — an installed package must never be able to break the free skill. The
    free backend is normally registered via entry-points too (so an installed copy
    is found here); the built-in instance is appended only when discovery did not
    already surface one, so a source checkout / test run (no installed entry points)
    still has it.
    """
    eps = (entry_points_fn or _iter_entry_points)(BACKENDS_GROUP)
    found: List[Backend] = []
    for ep in eps:
        try:
            obj = ep.load()
            # isinstance(obj, type) handles class-based backends (the normal case).
            # callable(obj) covers factory functions that aren't classes; the
            # isinstance(Backend) guard is excluded here because runtime_checkable
            # Protocol checks on a class object (not an instance) can return True
            # for classes that have matching method names as class attributes,
            # which would incorrectly bypass the call.
            if isinstance(obj, type):
                backend = obj()
            elif callable(obj) and not isinstance(obj, Backend):
                backend = obj()
            else:
                backend = obj
            if not isinstance(backend, Backend):
                continue
        except Exception:
            continue
        found.append(backend)
    if not any(getattr(b, "name", None) == FreeBackend.name for b in found):
        found.append(FreeBackend())
    return found


def _is_active(backend: Backend) -> bool:
    try:
        return bool(backend.is_active())
    except Exception:
        # A backend that errors while answering is treated as inactive — the free
        # skill keeps running rather than crashing on a third-party fault.
        return False


def select_backend(backends: List[Backend]) -> Backend:
    """Return the highest-priority backend whose :meth:`Backend.is_active` is True.
    The free backend is always active (lowest priority), so this never fails; the
    fallback :class:`FreeBackend` is a defensive belt-and-braces guard."""
    active = [b for b in backends if _is_active(b)]
    if not active:
        return FreeBackend()
    def safe_priority(b: Backend) -> int:
        try:
            return int(getattr(b, "priority", 0))
        except Exception:
            return 0

    active.sort(key=safe_priority, reverse=True)
    return active[0]


def launch_review_loop(pr: str, repo: Optional[str], cwd: Optional[str], *,
                       backends: Optional[List[Backend]] = None, **opts: Any) -> int:
    """The front door: choose the active backend and run the loop on the PR.

    With nothing extra installed this resolves to :class:`FreeBackend` and runs the
    free console loop — today's behavior. ``backends`` is injectable for tests.
    """
    candidates = backends if backends is not None else discover_backends()
    backend = select_backend(candidates)
    try:
        return backend.run_review_loop(pr, repo, cwd, **opts)
    except Exception as exc:
        if isinstance(backend, FreeBackend):
            raise
        print(
            f"⚠ backend {getattr(backend, 'name', repr(backend))!r} failed ({exc}); "
            "falling back to free backend",
            file=opts.get("err", sys.stderr),
        )
        return FreeBackend().run_review_loop(pr, repo, cwd, **opts)
