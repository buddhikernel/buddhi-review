"""Plan → role → model/effort resolution for the skill.

The table lives in ``plan_profiles.yml`` NEXT TO THIS FILE (``Path(__file__).
parent`` — the skill is installed as a package, so the data file must travel
with it, never resolve against a repo checkout). The engine reasons in tier
aliases (opus/sonnet/haiku); :func:`tier_model` resolves an alias through the
active plan's ``tier_map`` at the single ``claude --model`` boundary
(:mod:`buddhi_review.model_call`).

``[1m]`` long-context escalation is a *prompt-size* decision, not a plan
decision: :func:`needs_long_context` flags a prompt whose estimated token count
exceeds 160K, and :func:`long_context_model` appends the ``[1m]`` selector.

This module is also the **reviewer-fleet resolver surface** the round loop reads:
:func:`configured_bots`, :func:`auto_on_open_for`, :func:`auto_on_open_map`,
:func:`repo_confirmed`, and :func:`has_global_default`. The per-repo resolution
lives in :mod:`buddhi_review.config` (which owns the ``repos:`` schema); these
are the thin, named resolvers that supply the loaded config and present the API
the loop consumes. ``repo=None`` resolves to the global-default fleet.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # PyYAML is a hard dep of the package; guard so import never explodes.
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from buddhi_review import config
from buddhi_review.config import DEFAULT_PLAN, load_config

PROFILES_PATH = Path(__file__).parent / "plan_profiles.yml"
DEFAULT_EFFORT = "medium"
_FALLBACK_ROLE = {"model": "sonnet", "effort": DEFAULT_EFFORT}

# Escalate to the [1m] selector ONLY on a >160K-token prompt
# (~4 chars/token estimate).
LONG_CONTEXT_TOKEN_THRESHOLD = 160_000
_CHARS_PER_TOKEN = 4


@lru_cache(maxsize=1)
def _profiles() -> Dict[str, Any]:
    if yaml is None:
        return {}
    try:
        data = yaml.safe_load(PROFILES_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:  # pragma: no cover - packaging error
        raise RuntimeError(f"plan_profiles.yml unreadable at {PROFILES_PATH}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def active_plan(cfg: Optional[Dict[str, Any]] = None) -> str:
    """env ``BUDDHI_LOOP_PLAN`` → config ``plan:`` → default. An unknown plan
    name resolves to the default plan's behaviour (identity tier_map)."""
    env = os.environ.get("BUDDHI_LOOP_PLAN", "").strip()
    if env:
        return env
    if cfg is None:
        cfg = load_config()
    v = cfg.get("plan")
    return v if isinstance(v, str) and v else DEFAULT_PLAN


def _role_row(role: str) -> Dict[str, Any]:
    roles = _profiles().get("roles")
    if isinstance(roles, dict):
        row = roles.get(role)
        if isinstance(row, dict):
            return row
    return dict(_FALLBACK_ROLE)


def known_plans() -> List[str]:
    """The plan names defined in ``plan_profiles.yml`` (e.g. ``pro``, ``max-5x``,
    ``max-20x``), in file order — the menu the setup wizard offers. The default
    plan is guaranteed present (prepended if the table somehow omits it) so the
    wizard always has a valid choice."""
    plans = _profiles().get("plans")
    names = [str(k) for k in plans] if isinstance(plans, dict) else []
    if DEFAULT_PLAN not in names:
        names.insert(0, DEFAULT_PLAN)
    return names


def tier_model(tier: str, plan: Optional[str] = None) -> str:
    """Resolve a tier alias through the plan's tier_map (pro: opus → sonnet).
    Unknown plan or tier → the alias passes through unchanged."""
    plans = _profiles().get("plans")
    if isinstance(plans, dict):
        block = plans.get(plan or active_plan())
        if isinstance(block, dict):
            tm = block.get("tier_map")
            if isinstance(tm, dict) and tier in tm:
                return str(tm[tier])
    return tier


def model_for(role: str, plan: Optional[str] = None) -> str:
    return tier_model(str(_role_row(role).get("model", "sonnet")), plan)


def effort_for(role: str, plan: Optional[str] = None) -> str:
    v = _role_row(role).get("effort")
    return str(v) if isinstance(v, str) and v else DEFAULT_EFFORT


def estimated_tokens(prompt: str) -> int:
    return len(prompt) // _CHARS_PER_TOKEN


def needs_long_context(prompt: str) -> bool:
    return estimated_tokens(prompt) > LONG_CONTEXT_TOKEN_THRESHOLD


def long_context_model(model: str) -> str:
    return model if model.endswith("[1m]") else f"{model}[1m]"


# ── Reviewer fleet (per-repo resolution) ────────────────────────────────────────
# The resolution order — confirmed repos[<owner/repo>] entry → top-level global
# default → built-in defaults — lives in buddhi_review.config (the schema owner).
# These resolvers add the loaded config and present the named API the round loop
# consumes; data-only here (no gate, no round-1 summon wiring).

def configured_bots(repo: Optional[str] = None) -> List[str]:
    """The enabled reviewer fleet for ``repo`` — the starting universe before any
    per-run exclusions and per-bot readiness gates apply. ``repo=None`` returns
    the global-default fleet."""
    return list(config.active_reviewers(load_config(), repo))


def auto_on_open_for(bot: str, repo: Optional[str] = None) -> bool:
    """Whether ``bot`` auto-reviews on PR open for ``repo`` — the per-repo
    ``auto_on_open`` block when the repo is confirmed, else the global default,
    else ``DEFAULT_AUTO_ON_OPEN``. ``repo=None`` reads the global default."""
    return config.auto_on_open(load_config(), bot, repo)


def auto_on_open_map(repo: Optional[str] = None) -> Dict[str, bool]:
    """``{enabled reviewer → auto_on_open bool}`` across the configured fleet for
    ``repo``. Only enabled reviewers (:func:`configured_bots`) appear, each
    resolved against the SAME loaded config (one read, no per-bot re-load)."""
    cfg = load_config()
    return {bot: config.auto_on_open(cfg, bot, repo)
            for bot in config.active_reviewers(cfg, repo)}


def repo_confirmed(repo: Optional[str]) -> bool:
    """True when a reviewer fleet has been CONFIRMED for ``repo`` — a
    ``repos[<repo>]`` entry exists. Presence is the confirmation marker (an
    explicit empty fleet still counts). Drives the loop's fail-closed
    unconfirmed-repo gate; data-only here."""
    return config.repo_entry(load_config(), repo) is not None


def has_global_default() -> bool:
    """True when a global-default reviewer fleet is set (top-level
    ``active_reviewers`` present as a list). An unconfirmed repo may run against
    the global default only when this holds."""
    return config.has_global_default(load_config())


def label_gated_ci(repo: Optional[str] = None) -> bool:
    """Whether the "ready-for-ci" label gate guards the merge for ``repo`` — the
    per-repo ``label_gated_ci`` when the repo is confirmed, else the global flag,
    else the default (off). The named resolver the round loop / pre-merge gate
    reads; per-repo resolution lives in :func:`buddhi_review.config.label_gated_ci`.
    ``repo=None`` reads the global flag."""
    return config.label_gated_ci(load_config(), repo)
