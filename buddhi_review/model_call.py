"""The ONE deterministic model-JSON call skeleton.

Every deterministic ``claude`` model call in the skill — classifier,
clean-review detector, quota detector, fix-verify — routes through this module,
so there is exactly one place that owns:

* the subprocess spawn (``claude -p`` / stdin ``--print`` for oversized prompts),
* bounded retry with a fixed delay,
* the tolerant JSON extraction (:func:`buddhi_review.classify.extract_json_object`),
* **explicit role-sized** ``--effort`` from :mod:`buddhi_review.plan_profile`
  (never host-inherited from ``~/.claude/settings.json``),
* MCP isolation (``--strict-mcp-config`` — these calls never use an MCP tool),
* the ``[1m]`` long-context escalation (ONLY on a >160K-token prompt).

A model error after the bounded retry surfaces as ``None`` / a raise, and the
caller escalates rather than retrying the call on another model.

The agentic per-comment FIXER is NOT routed here — it lives in
:mod:`buddhi_review.fix_apply` and degrades at its own argv boundary.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Callable, Dict, List, Optional, TextIO, Tuple

from buddhi_review import plan_profile
from buddhi_review.classify import extract_json_object


def _env_int(name: str, default: int, floor: int = 0) -> int:
    try:
        return max(floor, int(os.environ.get(name, "")))
    except (TypeError, ValueError):
        return default


def _dim_enabled(stream: TextIO) -> bool:
    """Dim styling is on only for a real TTY, and off under the same
    ``NO_COLOR`` / ``BUDDHI_LOOP_NO_COLOR`` env names the rest of the pipeline
    honours; the text itself always prints regardless."""
    if "NO_COLOR" in os.environ or "BUDDHI_LOOP_NO_COLOR" in os.environ:
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _emit_long_context(role: str, prompt: str, model: str) -> str:
    """One dim ``[model]`` line so a prompt-driven ``[1m]`` escalation is visible
    in the log (it is a per-call decision, not config). Returns the uncoloured
    text (what tests assert on). The line names only the single long-context
    model chosen for this call."""
    k_tokens = plan_profile.estimated_tokens(prompt) // 1000
    body = f"  [model] {role}: large prompt (≈{k_tokens}K tokens) → {model}"
    out = sys.stdout
    print(f"\033[2m{body}\033[0m" if _dim_enabled(out) else body, file=out, flush=True)
    return body


RETRIES = _env_int("BUDDHI_CLASSIFY_RETRIES", 1)
TIMEOUT = _env_int("BUDDHI_CLASSIFY_TIMEOUT", 120, floor=1)
RETRY_DELAY = 5.0
# Past this size the prompt rides stdin (--print) so it can grow past ARG_MAX.
STDIN_THRESHOLD = 100_000

# spawn(argv, input_text, timeout) -> CompletedProcess — the injectable seam.
Spawn = Callable[[List[str], Optional[str], int], "subprocess.CompletedProcess[str]"]


def _make_default_spawn(cwd: Optional[str] = None) -> Spawn:
    """Build the default ``claude`` spawn, optionally pinned to ``cwd``.

    The classifier's escalation criteria tell the model it is "running inside the
    repository" and may consult repo docs / the touched file before declaring a
    question undocumented. For that to hold in the *detached* launch — ``review-pr``
    started from outside the target checkout with ``--cwd <repo>`` — the subprocess
    must actually run in the target repo, not inherit the launcher's process cwd.
    ``cwd=None`` preserves the inherited-cwd behaviour (the in-checkout launch,
    where the launcher cwd already IS the repo). The seam stays a 3-arg
    ``Spawn`` — cwd is bound here at construction, not added to the call signature."""
    def _spawn(
        argv: List[str], input_text: Optional[str], timeout: int
    ) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            input=input_text, stdin=(subprocess.DEVNULL if input_text is None else None),
            cwd=cwd,
        )
    return _spawn


# The module-level default spawn (cwd inherited from the process) — what the seam
# tests and callers inject against. The classifier path swaps in a cwd-pinned
# variant on demand (see ``run_model_text``).
_default_spawn: Spawn = _make_default_spawn()


def build_argv(prompt: str, *, model: str, effort: str) -> Tuple[List[str], Optional[str]]:
    """Return ``(argv, stdin_text)``. The flag set is fixed by contract:
    explicit ``--model`` + ``--effort``, no session persistence, zero MCP."""
    base = [
        "claude", "--model", model, "--effort", effort,
        "--no-session-persistence", "--strict-mcp-config",
    ]
    if len(prompt) > STDIN_THRESHOLD:
        return base + ["--print"], prompt
    return base + ["-p", prompt], None


def resolve_model(prompt: str, *, role: str, plan: Optional[str] = None) -> str:
    """Role model via the plan table, escalated to ``[1m]`` only when the
    prompt's estimated tokens exceed the long-context threshold. A real
    escalation (the role's model did not already carry ``[1m]``) logs one dim
    ``[model]`` line so the long-context decision is visible in the run output."""
    model = plan_profile.model_for(role, plan)
    if plan_profile.needs_long_context(prompt):
        escalated = plan_profile.long_context_model(model)
        if escalated != model:
            _emit_long_context(role, prompt, escalated)
        model = escalated
    return model


def run_model_text(
    prompt: str,
    *,
    role: str,
    plan: Optional[str] = None,
    spawn: Spawn = _default_spawn,
    timeout: Optional[int] = None,
    cwd: Optional[str] = None,
) -> str:
    """ONE spawn (no retry here — callers like ``classify_comment`` own their
    own retry policy). Raises ``RuntimeError`` on a non-zero exit or a spawn
    failure; ``TimeoutExpired`` propagates (transient, caller-retryable).

    ``cwd`` pins the default spawn to the target repo so a detached-launch
    classifier genuinely runs "inside the repository" (it is ignored when a
    custom ``spawn`` is injected — that caller owns the subprocess)."""
    if cwd is not None and spawn is _default_spawn:
        spawn = _make_default_spawn(cwd)
    model = resolve_model(prompt, role=role, plan=plan)
    effort = plan_profile.effort_for(role, plan)
    argv, stdin_text = build_argv(prompt, model=model, effort=effort)
    try:
        proc = spawn(argv, stdin_text, timeout or TIMEOUT)
    except subprocess.TimeoutExpired:
        raise
    except OSError as exc:
        raise RuntimeError(f"model call ({role}) failed to launch claude: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise RuntimeError(f"model call ({role}) failed (rc={proc.returncode}): {detail}")
    return proc.stdout or ""


def run_model_json(
    prompt: str,
    *,
    role: str,
    plan: Optional[str] = None,
    spawn: Spawn = _default_spawn,
    retries: Optional[int] = None,
    timeout: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Optional[Dict]:
    """Bounded-retry JSON call: spawn → extract ONE JSON object → dict, or None
    after every attempt fails (spawn error / non-zero rc / unparseable). The
    caller decides what a None means (usually: escalate or fall back
    conservatively)."""
    attempts = (RETRIES if retries is None else max(0, retries)) + 1
    for attempt in range(1, attempts + 1):
        try:
            raw = run_model_text(prompt, role=role, plan=plan, spawn=spawn, timeout=timeout)
        except (RuntimeError, subprocess.TimeoutExpired):
            raw = ""
        obj = extract_json_object(raw) if raw else None
        if obj is not None:
            return obj
        if attempt < attempts:
            sleep(RETRY_DELAY)
    return None


def text_runner(
    role: str, *, plan: Optional[str] = None, spawn: Spawn = _default_spawn,
    timeout: Optional[int] = None, cwd: Optional[str] = None,
) -> Callable[[str], str]:
    """Adapter for the existing ``runner(prompt) -> raw text`` seams
    (``classify_comment``'s runner, ``fix_apply``'s verify_runner): one spawn
    per call, raising on failure so each caller's own retry/fail-open policy
    applies exactly as designed. ``cwd`` pins the default spawn to the target
    repo (see ``run_model_text``)."""

    def _runner(prompt: str) -> str:
        return run_model_text(
            prompt, role=role, plan=plan, spawn=spawn, timeout=timeout, cwd=cwd)

    return _runner
