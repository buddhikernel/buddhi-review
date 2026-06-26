"""Config — ``~/.config/review-loop/config.yaml``.

Keys: ``plan``, ``active_reviewers``, ``auto_on_open``, ``repos``,
``notifications`` (always ``console``), ``repo``, ``cwd``. The notifier writes
to the console.

Reviewer availability is **per-repo** — Copilot/Gemini/Codex are GitHub Apps
installed per repo and ``claude[bot]`` needs its workflow in each repo — so the
fleet + the ``auto_on_open`` facts resolve per ``owner/repo`` through the
``repos:`` map. Resolution order diverges slightly by function:

* :func:`active_reviewers` — CONFIRMED ``repos[<repo>]`` entry that **carries**
  ``active_reviewers`` (even if malformed) wins; a valid list is returned as-is,
  a malformed value falls to ``DEFAULT_REVIEWERS`` **without** consulting the
  top-level global default. When the repo has no entry, or has one that lacks
  the key, the top-level ``active_reviewers`` list is used; absent that, the
  built-in four-bot set.

* :func:`auto_on_open` — CONFIRMED ``repos[<repo>]`` entry that **carries**
  ``auto_on_open`` shadows the top-level block entirely; a valid dict is
  looked up per-bot, a malformed value falls straight to ``DEFAULT_AUTO_ON_OPEN``
  (skipping the global ``auto_on_open`` block). When no per-repo key is present,
  the top-level block is used; absent that, ``DEFAULT_AUTO_ON_OPEN``.

Passing ``repo=None`` reads the global default, so a caller that does not
specify a repo gets the global-default fleet.

Absent config → defaults + a one-line log warning, never an error (the onboarding
gate prompts setup instead of degrading silently).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:  # PyYAML is a hard dep of the package; guard only so import never explodes.
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

DEFAULT_PLAN = "max-5x"
DEFAULT_REVIEWERS: Tuple[str, ...] = ("copilot", "gemini", "codex", "claude")
# Whether a bot posts a review automatically when a PR is opened — a fact the loop
# cannot infer, so it is config. Default: the three GitHub-App bots auto-comment;
# claude is summoned in round 1.
DEFAULT_AUTO_ON_OPEN: Dict[str, bool] = {
    "copilot": True,
    "gemini": True,
    "codex": True,
    "claude": False,
}


def config_path() -> Path:
    override = os.environ.get("BUDDHI_CONFIG")
    return Path(override) if override else Path.home() / ".config" / "review-loop" / "config.yaml"


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    p = path or config_path()
    if yaml is None:
        return {}
    if not p.exists():
        print(f"Warning: Config file not found at {p}. Using default settings.", file=sys.stderr)
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        print(f"Warning: Could not load or parse config file {p}: {e}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def plan(cfg: Dict[str, Any]) -> str:
    v = cfg.get("plan")
    return v if isinstance(v, str) and v else DEFAULT_PLAN


# ── Per-repo reviewer resolution (the ``repos:`` map) ───────────────────────────

def norm_repo(repo: Optional[str]) -> Optional[str]:
    """Normalise a repo identifier to the lowercased ``owner/repo`` key used in
    the ``repos:`` map, or ``None``. GitHub repo slugs are case-insensitive, so
    lowercasing lets a ``gh``-inferred ``Owner/Repo`` match a stored
    ``owner/repo`` key."""
    if not repo:
        return None
    key = str(repo).strip().lower()
    return key or None


def repos_map(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The ``repos:`` block — a map keyed ``owner/repo`` of per-repo reviewer
    config ``{active_reviewers, auto_on_open}``. ``{}`` when absent or not a map."""
    v = cfg.get("repos")
    return v if isinstance(v, dict) else {}


def repo_entry(cfg: Dict[str, Any], repo: Optional[str]) -> Optional[Dict[str, Any]]:
    """The ``repos[<repo>]`` mapping for ``repo`` (case-insensitive), or ``None``
    when the repo has no confirmed entry. A non-dict entry is ignored. The mere
    PRESENCE of the entry is the per-repo confirmation marker — an explicit empty
    fleet still counts as confirmed."""
    key = norm_repo(repo)
    if key is None:
        return None
    for k, v in repos_map(cfg).items():
        if str(k).strip().lower() == key and isinstance(v, dict):
            return v
    return None


def has_global_default(cfg: Dict[str, Any]) -> bool:
    """True when a global-default reviewer fleet is set — a top-level
    ``active_reviewers`` list. An unconfirmed repo may fall back to the global
    default only when this holds; without it the loop's gate fails closed."""
    return isinstance(cfg.get("active_reviewers"), list)


def active_reviewers(cfg: Dict[str, Any], repo: Optional[str] = None) -> Tuple[str, ...]:
    """The enabled reviewer fleet. A CONFIRMED repo's per-repo ``active_reviewers``
    (a ``repos[<repo>]`` entry that carries the key) wins; otherwise the top-level
    ``active_reviewers`` (the global default); otherwise the built-in four-bot set.
    An explicit empty list (the user confirmed "no bots for this repo") is honoured
    as-is. ``repo=None`` reads the global default."""
    entry = repo_entry(cfg, repo)
    if entry is not None and "active_reviewers" in entry:
        v = entry.get("active_reviewers")
    else:
        v = cfg.get("active_reviewers")
    if isinstance(v, list):
        return tuple(str(x) for x in v)
    return DEFAULT_REVIEWERS


def auto_on_open(cfg: Dict[str, Any], bot: str, repo: Optional[str] = None) -> bool:
    """Whether ``bot`` posts a review automatically when a PR is opened. A
    CONFIRMED repo's per-repo ``auto_on_open`` block (a ``repos[<repo>]`` entry
    that carries the key) wins; otherwise the top-level ``auto_on_open`` block;
    otherwise ``DEFAULT_AUTO_ON_OPEN`` (claude → False, the GitHub-App reviewers →
    True). The presence of a per-repo ``auto_on_open`` key shadows the top-level
    block even when malformed (then every bot falls to the per-bot default).
    ``repo=None`` reads the top-level block."""
    entry = repo_entry(cfg, repo)
    if entry is not None and "auto_on_open" in entry:
        m = entry.get("auto_on_open")
    else:
        m = cfg.get("auto_on_open")
    if isinstance(m, dict) and bot in m:
        return bool(m[bot])
    return DEFAULT_AUTO_ON_OPEN.get(bot, True)


def notifier_channel(cfg: Dict[str, Any]) -> str:
    """Notifications are delivered to the console. This is the only channel this
    package ships, regardless of what a hand-edited config sets."""
    return "console"
