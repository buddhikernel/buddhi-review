"""The interactive setup wizard — ``python3 -m buddhi_review setup``.

Walks the user through the free configuration surface and writes
``~/.config/review-loop/config.yaml`` (the path :mod:`buddhi_review.config`
reads). The ordered flow mirrors the reference wizard, with the two budget /
monitoring steps shown as single-line locked upgrade teasers (they persist
nothing):

  1. Tooling doctor      — Claude CLI, ``gh`` ≥ 2.87 + auth, reachable model tiers
  2. Plan selection      — drives ``plan_profiles.yml`` role→model resolution
  3. Repo binding        — infer ``owner/repo`` + toplevel from ``git remote``
  4. Provider budgets    — locked teaser (paid); persists nothing
  5. Reviewer fleet      — multi-select + validate each + capture ``auto_on_open``
  6. Live monitoring     — locked teaser (paid); persists nothing
  7. Summary + done      — read-back + the launch hint

Only the free keys are persisted: ``plan``, ``repo``, ``cwd``,
``active_reviewers``, ``auto_on_open``, ``notifications: console``. A shell-rc
secret (the Copilot ``GH_TOKEN`` escape hatch) is written via
:mod:`buddhi_review.shell_env`, never into config. The Claude reviewer path can
write the bundled ``claude-code-review.yml`` and set the ``CLAUDE_CODE_OAUTH_TOKEN``
repo secret.

The wizard is an interactive raw-mode TTY program; the ``/review-pr setup`` skill
step opens it in a fresh window via :mod:`buddhi_review.setup_launcher`. Every
external effect (subprocess runner, the selectors, prompts, the spawn helper, the
output stream) is injectable, so the step-gating logic is unit-testable without a
real terminal.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from buddhi_review import config, plan_profile, setup_launcher, shell_env, upsell
from buddhi_review.transparency import _colour_enabled

_GH_MIN = (2, 87)
_REVIEWERS = ("copilot", "gemini", "codex", "claude")
_GITHUB_APP_BOTS = ("copilot", "gemini", "codex")  # claude is workflow/mention-driven
_MODEL_TIERS = ("opus", "sonnet", "haiku")

# Each locked teaser is a single-line, single-BENEFIT contextual upgrade nudge
# (exec-plan §E: the one permitted paid reference — a benefit, NEVER a paid product
# name or mechanism enumeration), suppressible with BUDDHI_NO_UPSELL. The paid web
# work-tracking surface is deliberately NOT named or advertised here (it may be cut).
_BUDGETS_TEASER = "Budget controls — paid tier"
_MONITORING_TEASER = "Live run monitoring — paid tier"
# A single-line, suppressible benefit-only nudge (exec-plan §E: benefit only,
# no paid product name or mechanism details).
_PRO_SOON_TEASER = "More automation is coming soon — stay tuned."

# A one-line static note (NOT a live usage read) about the Claude review workflow's
# GitHub Actions cost.
_ACTIONS_NOTE = ("Note: the Claude review workflow runs on GitHub Actions and uses "
                 "your repo's Actions minutes (private repos have a free monthly "
                 "allowance; public repos are free) — see github.com/settings/billing.")


# ── Colour + output ──────────────────────────────────────────────────────────────

class _Palette:
    def __init__(self, enabled: bool):
        if enabled:
            self.RESET, self.BOLD, self.DIM = "\033[0m", "\033[1m", "\033[2m"
            self.CYAN, self.GREEN, self.RED, self.YELLOW, self.GREY = (
                "\033[36m", "\033[32m", "\033[31m", "\033[33m", "\033[90m")
        else:
            self.RESET = self.BOLD = self.DIM = ""
            self.CYAN = self.GREEN = self.RED = self.YELLOW = self.GREY = ""


def _upsell_suppressed() -> bool:
    """``BUDDHI_NO_UPSELL`` truthy → suppress the locked-teaser upgrade nudges
    (honours the OSS upsell-suppression contract). Default: teasers render. The
    check lives in ``upsell`` so the wizard teasers and the in-run nudge share one
    suppression switch."""
    return upsell.upsell_suppressed()


# ── Interactive TTY input primitives ──────────────────────────────────────────────

def _is_tty() -> bool:
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (ImportError, ValueError, OSError, AttributeError):
        return False


def _read_key() -> str:
    """A single keypress in raw mode → one of: ``up``, ``down``, ``space``,
    ``enter``, ``esc``, or the literal character."""
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        b = os.read(fd, 1)
        if not b:
            raise EOFError
        if b == b"\x1b":  # escape sequence (arrow keys)
            import select
            rlist, _, _ = select.select([fd], [], [], 0.05)
            if rlist:
                seq = os.read(fd, 2)
                return {b"[A": "up", b"[B": "down", b"OA": "up", b"OB": "down"}.get(seq, "esc")
            return "esc"
        if b in (b"\r", b"\n"):
            return "enter"
        if b == b" ":
            return "space"
        if b == b"\x03":  # Ctrl-C
            raise KeyboardInterrupt
        return b.decode("utf-8", errors="ignore")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _render_choices(options: Sequence[Tuple[str, str]], cursor: int, selected, radio: bool,
                    pal: _Palette, stream) -> None:
    for i, (label, detail) in enumerate(options):
        mark = ("◉" if i in selected else "◯") if radio else ("[x]" if i in selected else "[ ]")
        pointer = f"{pal.CYAN}❯{pal.RESET}" if i == cursor else " "
        line = f" {pointer} {mark} {pal.BOLD}{label}{pal.RESET}"
        if detail:
            line += f"  {pal.DIM}{detail}{pal.RESET}"
        print(line, file=stream)


def _clear_choices(n: int, stream) -> None:
    stream.write(f"\033[{n}A")
    for _ in range(n):
        stream.write("\033[2K\033[1B")
    stream.write(f"\033[{n}A")
    stream.flush()


def _numbered_select(prompt: str, options: Sequence[Tuple[str, str]], preselect: int,
                     pal: _Palette, stream, input_fn) -> int:
    print(prompt, file=stream)
    for i, (label, detail) in enumerate(options, 1):
        extra = f"  ({detail})" if detail else ""
        print(f"  {i}. {label}{extra}", file=stream)
    try:
        raw = input_fn(f"  Choose [1-{len(options)}] (default {preselect + 1}): ").strip()
    except EOFError:
        return preselect
    if not raw:
        return preselect
    try:
        idx = int(raw) - 1
    except ValueError:
        return preselect
    return idx if 0 <= idx < len(options) else preselect


def single_select(prompt: str, options: Sequence[Tuple[str, str]], *, preselect: int = 0,
                  pal: Optional[_Palette] = None, stream=None, input_fn=input) -> int:
    """A radio selector → the chosen index. Raw-mode arrows on a TTY; a numbered
    prompt otherwise."""
    stream = stream or sys.stdout
    pal = pal or _Palette(_colour_enabled(stream))
    if not options:
        return preselect
    if not _is_tty():
        return _numbered_select(prompt, options, preselect, pal, stream, input_fn)
    cursor = max(0, min(preselect, len(options) - 1))
    print(prompt, file=stream)
    _render_choices(options, cursor, {cursor}, True, pal, stream)
    while True:
        try:
            key = _read_key()
        except EOFError:
            return cursor
        if key == "up":
            cursor = (cursor - 1) % len(options)
        elif key == "down":
            cursor = (cursor + 1) % len(options)
        elif key == "enter":
            return cursor
        else:
            continue
        _clear_choices(len(options), stream)
        _render_choices(options, cursor, {cursor}, True, pal, stream)


def _numbered_multiselect(prompt: str, options: Sequence[Tuple[str, str]], preselected: set,
                          pal: _Palette, stream, input_fn) -> set:
    print(prompt, file=stream)
    for i, (label, detail) in enumerate(options, 1):
        mark = "x" if i - 1 in preselected else " "
        extra = f"  ({detail})" if detail else ""
        print(f"  [{mark}] {i}. {label}{extra}", file=stream)
    try:
        raw = input_fn("  Enter numbers to toggle (comma-separated), or blank to accept: ").strip()
    except EOFError:
        return set(preselected)
    chosen = set(preselected)
    if raw:
        for tok in raw.replace(",", " ").split():
            try:
                idx = int(tok) - 1
            except ValueError:
                continue
            if 0 <= idx < len(options):
                chosen.symmetric_difference_update({idx})
    return chosen


def multi_select(prompt: str, options: Sequence[Tuple[str, str]], *, preselected=None,
                 pal: Optional[_Palette] = None, stream=None, input_fn=input) -> set:
    """A checkbox selector → the set of chosen indices. Raw-mode (↑/↓ move, Space
    toggles, Enter confirms) on a TTY; a numbered toggle prompt otherwise."""
    stream = stream or sys.stdout
    pal = pal or _Palette(_colour_enabled(stream))
    selected = set(range(len(options))) if preselected is None else set(preselected)
    if not options:
        return selected
    if not _is_tty():
        return _numbered_multiselect(prompt, options, selected, pal, stream, input_fn)
    cursor = 0
    print(prompt, file=stream)
    print(f"  {pal.DIM}↑/↓ move · Space toggle · Enter confirm{pal.RESET}", file=stream)
    _render_choices(options, cursor, selected, False, pal, stream)
    while True:
        try:
            key = _read_key()
        except EOFError:
            return selected
        if key == "up":
            cursor = (cursor - 1) % len(options)
        elif key == "down":
            cursor = (cursor + 1) % len(options)
        elif key == "space":
            selected.symmetric_difference_update({cursor})
        elif key == "enter":
            return selected
        else:
            continue
        _clear_choices(len(options), stream)
        _render_choices(options, cursor, selected, False, pal, stream)


def _ask_yes_no(prompt: str, *, default: bool, input_fn, stream) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        raw = input_fn(f"  {prompt} {suffix}: ").strip().lower()
    except EOFError:
        return default
    if not raw:
        return default
    return raw in ("y", "yes")


# ── Panels ─────────────────────────────────────────────────────────────────────

def _panel(title: str, lines: Sequence[str], pal: _Palette, stream) -> None:
    print(f"\n{pal.BOLD}── {title} ──{pal.RESET}", file=stream)
    for ln in lines:
        print(f"  {ln}", file=stream)


def _row(status: str, text: str, pal: _Palette, stream) -> None:
    glyph = {"ok": f"{pal.GREEN}✓{pal.RESET}", "warn": f"{pal.YELLOW}⚠{pal.RESET}",
             "bad": f"{pal.RED}✗{pal.RESET}", "info": f"{pal.GREY}·{pal.RESET}"}.get(status, " ")
    print(f"  {glyph} {text}", file=stream)


def _kv(label: str, value: str, pal: _Palette, stream) -> None:
    print(f"  {label:<18} {pal.BOLD}{value}{pal.RESET}", file=stream)


def _teaser(text: str, pal: _Palette, stream) -> None:
    """Render a single-line locked upgrade teaser (suppressed by BUDDHI_NO_UPSELL)."""
    if _upsell_suppressed():
        return
    print(f"  {pal.GREY}🔒 {text}{pal.RESET}", file=stream)


# ── Subprocess seam ──────────────────────────────────────────────────────────────

def _default_run(argv, *, timeout=30, input=None):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=input)


def _run_ok(run, argv, *, timeout=15) -> Tuple[bool, str]:
    """``(returncode == 0, stdout)`` for ``argv``; any exception → ``(False, "")``."""
    try:
        r = run(argv, timeout=timeout)
    except Exception:
        return False, ""
    return getattr(r, "returncode", 1) == 0, (getattr(r, "stdout", "") or "")


# ── Pure helpers (unit-tested directly) ───────────────────────────────────────────

def gh_version_ok(version_str: str) -> Tuple[Optional[Tuple[int, int]], bool]:
    """Parse ``gh version X.Y …`` → ``((major, minor), >= 2.87)``. Unparseable →
    ``(None, False)``."""
    m = re.search(r"gh version (\d+)\.(\d+)", version_str or "")
    if not m:
        return None, False
    ver = (int(m.group(1)), int(m.group(2)))
    return ver, ver >= _GH_MIN


def recommend_plan(tiers: Dict[str, bool]) -> str:
    """Recommend a plan from the reachable model tiers: Opus → ``max-5x``;
    Sonnet only → ``pro``; neither → the default plan."""
    if tiers.get("opus"):
        return "max-5x" if "max-5x" in plan_profile.known_plans() else config.DEFAULT_PLAN
    if tiers.get("sonnet"):
        return "pro" if "pro" in plan_profile.known_plans() else config.DEFAULT_PLAN
    return config.DEFAULT_PLAN


def infer_repo(run, cwd: Optional[str] = None) -> Optional[str]:
    """``owner/repo`` parsed from the cwd's ``git remote get-url origin``, or None."""
    try:
        r = run(["git", "-C", cwd or ".", "remote", "get-url", "origin"], timeout=10)
    except Exception:
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    url = (getattr(r, "stdout", "") or "").strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def repo_toplevel(run, cwd: Optional[str] = None) -> Optional[str]:
    try:
        r = run(["git", "-C", cwd or ".", "rev-parse", "--show-toplevel"], timeout=10)
    except Exception:
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    return (getattr(r, "stdout", "") or "").strip() or None


def build_config(plan: str, repo: Optional[str], cwd: Optional[str],
                 active_reviewers: Sequence[str], auto_on_open: Dict[str, bool]) -> Dict[str, Any]:
    """The free config dict — ONLY the free keys. No budget / monitoring / push-channel
    key is ever emitted (those live nowhere in the free config)."""
    cfg: Dict[str, Any] = {
        "plan": plan,
        "active_reviewers": list(active_reviewers),
        "auto_on_open": {b: bool(v) for b, v in auto_on_open.items()},
        "notifications": "console",
    }
    if repo:
        cfg["repo"] = repo
    if cwd:
        cfg["cwd"] = cwd
    return cfg


def merge_preserving(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay ``new`` onto ``existing``, preserving any unknown existing keys
    (e.g. a hand-added ``repos:`` block). Free retires no key."""
    merged = dict(existing or {})
    merged.update(new)
    return merged


def write_config(cfg: Dict[str, Any], path: Path) -> bool:
    """Atomically write ``cfg`` to ``path`` (temp file + ``os.replace``) at 0600."""
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is a hard dep
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_str = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        tmp = Path(tmp_str)
        try:
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = None  # os.fdopen took ownership; don't close in the except block
                yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(path))
        except BaseException:
            if fd is not None:
                os.close(fd)
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


# ── Steps ─────────────────────────────────────────────────────────────────────

def step_doctor(*, run, which, pal, stream) -> Dict[str, Any]:
    """Step 1 — tooling doctor. Reports each check; never hard-exits (setup stays
    useful even if a tool is missing — the loop's own preflight catches it)."""
    _panel("Step 1 — Tooling doctor", [], pal, stream)
    claude_bin = which("claude")
    if claude_bin:
        _row("ok", f"Claude CLI found ({claude_bin})", pal, stream)
    else:
        _row("warn", "Claude CLI not found on PATH — install it to run reviews/fixes", pal, stream)

    gh_present, gh_out = _run_ok(run, ["gh", "--version"])
    ver, ok_ver = gh_version_ok(gh_out)
    if not gh_present:
        _row("warn", "gh CLI not found — install GitHub CLI and run `gh auth login`", pal, stream)
    elif ok_ver:
        _row("ok", f"gh {ver[0]}.{ver[1]} (>= {_GH_MIN[0]}.{_GH_MIN[1]})", pal, stream)
    else:
        shown = f"{ver[0]}.{ver[1]}" if ver else "unknown"
        _row("warn", f"gh {shown} is below {_GH_MIN[0]}.{_GH_MIN[1]} — Copilot review needs the newer gh", pal, stream)

    gh_auth, _ = _run_ok(run, ["gh", "auth", "status"]) if gh_present else (False, "")
    _row("ok" if gh_auth else "warn",
         "gh authenticated" if gh_auth else "gh not authenticated — run `gh auth login`",
         pal, stream)

    tiers: Dict[str, bool] = {}
    if claude_bin:
        for tier in _MODEL_TIERS:
            ok, _ = _run_ok(run, [claude_bin, "--model", tier, "--permission-mode",
                                  "bypassPermissions", "--strict-mcp-config", "-p", "ping"],
                            timeout=60)
            tiers[tier] = ok
        reachable = ", ".join(t for t in _MODEL_TIERS if tiers.get(t)) or "none reachable"
        _row("ok" if any(tiers.values()) else "warn", f"Model tiers: {reachable}", pal, stream)

    return {"claude_bin": claude_bin, "gh_ok": bool(gh_present and ok_ver),
            "gh_auth": gh_auth, "tiers": tiers}


def step_plan(doctor: Dict[str, Any], *, pal, stream, single_select=single_select,
              input_fn=input) -> str:
    """Step 2 — plan selection."""
    plans = plan_profile.known_plans()
    options: List[Tuple[str, str]] = []
    for name in plans:
        # Show the model that hard calls resolve to under this plan (informational).
        model = plan_profile.model_for("fix-substantive", plan=name)
        options.append((name, f"fixes run {model}"))
    rec = recommend_plan(doctor.get("tiers", {}))
    preselect = plans.index(rec) if rec in plans else 0
    _panel("Step 2 — Claude plan", ["Drives which model each role runs (resolved from your plan)."],
           pal, stream)
    idx = single_select("  Which Claude plan are you on?", options, preselect=preselect,
                        pal=pal, stream=stream, input_fn=input_fn)
    return plans[idx]


def step_repo(preset_repo: Optional[str], *, run, pal, stream, input_fn=input) -> Tuple[Optional[str], Optional[str]]:
    """Step 3 — repo binding."""
    _panel("Step 3 — Repo binding", [], pal, stream)
    cwd = repo_toplevel(run)
    repo = preset_repo or infer_repo(run)
    if repo:
        use = _ask_yes_no(f"Use {repo}?", default=True, input_fn=input_fn, stream=stream)
        if not use:
            try:
                manual = input_fn("  Enter owner/repo (or blank to skip): ").strip()
            except (EOFError, KeyboardInterrupt):
                manual = ""
            repo = manual or None
    else:
        try:
            repo = input_fn("  Could not infer the repo. Enter owner/repo (or blank to skip): ").strip() or None
        except (EOFError, KeyboardInterrupt):
            repo = None
    if repo:
        _row("ok", f"Bound to {repo}", pal, stream)
    else:
        _row("info", "No repo bound — it is inferred at runtime from the cwd's git remote", pal, stream)
    return repo, cwd


def step_budgets_locked(*, pal, stream) -> None:
    """Step 4 — provider budgets (paid). A locked teaser; persists nothing."""
    _panel("Step 4 — Provider budgets", [], pal, stream)
    _teaser(_BUDGETS_TEASER, pal, stream)


def _probe_claude_workflow(repo: Optional[str], run) -> Tuple[bool, str]:
    """``(workflow present on the default branch, note)`` via ``gh api``."""
    if not repo:
        return False, "no repo bound — cannot check for the Claude workflow"
    ok, out = _run_ok(run, ["gh", "api",
                            f"repos/{repo}/contents/.github/workflows/claude-code-review.yml",
                            "--jq", ".content"])
    if ok and out.strip():
        return True, "claude-code-review.yml present on the default branch"
    return False, "claude-code-review.yml missing (offer to add it)"


def _workflow_template_path() -> Path:
    return Path(__file__).parent / "skills" / "review-pr" / "references" / "claude-code-review.yml"


def _write_workflow_template(cwd: Optional[str]) -> Optional[Path]:
    """Copy the bundled ``claude-code-review.yml`` into ``<cwd>/.github/workflows/``.
    Returns the written path, or None on failure / missing template."""
    template = _workflow_template_path()
    if not (cwd and template.exists()):
        return None
    try:
        dest_dir = Path(cwd) / ".github" / "workflows"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "claude-code-review.yml"
        shutil.copyfile(template, dest)
        return dest
    except OSError:
        return None


def _gh_secret_exists(repo: str, name: str, run) -> bool:
    # gh secret list emits tab-separated rows: NAME\tUPDATED_AT; split("\t")[0] is reliable.
    ok, out = _run_ok(run, ["gh", "secret", "list", "--repo", repo])
    return ok and any(line.split("\t")[0].strip() == name for line in out.splitlines())


def _set_claude_secret(repo: str, *, run, spawn_command, getpass_fn, pal, stream) -> str:
    """Walk the user through setting ``CLAUDE_CODE_OAUTH_TOKEN`` as a repo secret.
    Returns a short status string."""
    name = "CLAUDE_CODE_OAUTH_TOKEN"
    # Either credential is sufficient — ANTHROPIC_API_KEY (pay-as-you-go) or
    # CLAUDE_CODE_OAUTH_TOKEN (subscription). Only prompt when both are absent.
    if _gh_secret_exists(repo, name, run) or _gh_secret_exists(repo, "ANTHROPIC_API_KEY", run):
        return "present"
    if not _is_tty():
        _row("info", f"Set the {name} repo secret later: `claude setup-token` then "
                     f"`gh secret set {name} --repo {repo}`", pal, stream)
        return "deferred"
    _row("info", "Opening `claude setup-token` in a new window to mint a token …", pal, stream)
    try:
        spawn_command("claude setup-token", label="claude-setup-token", stream=stream)
    except Exception:
        pass
    try:
        token = getpass_fn("  Paste the token (input hidden), or blank to skip: ").strip()
    except (EOFError, KeyboardInterrupt):
        token = ""
    if not token:
        return "skipped"
    # `gh secret set NAME --repo R` reads the value from stdin when --body is omitted.
    r = None
    try:
        r = run(["gh", "secret", "set", name, "--repo", repo], input=token, timeout=20)
        ok = getattr(r, "returncode", 1) == 0
    except Exception:
        ok = False
    if ok:
        _row("ok", f"{name} set on {repo}", pal, stream)
        return "set"
    err_detail = f" ({r.stderr.strip()})" if r is not None and getattr(r, "stderr", None) else ""
    _row("warn", f"Could not set {name}{err_detail} — set it by hand with `gh secret set {name} --repo {repo}`", pal, stream)
    return "failed"


def _offer_gh_token(*, run, getpass_fn, pal, stream, input_fn=input) -> None:
    """When gh is unauthenticated, offer the GH_TOKEN env escape hatch (persisted
    via shell_env, never config). The user may instead run `gh auth login`."""
    if not _is_tty():
        return
    if not _ask_yes_no("gh is not authenticated. Persist a GH_TOKEN now? "
                       "(otherwise run `gh auth login` yourself)",
                       default=False, input_fn=input_fn, stream=stream):
        return
    try:
        token = getpass_fn("  Paste a GitHub token (input hidden), or blank to skip: ").strip()
    except (EOFError, KeyboardInterrupt):
        token = ""
    if not token:
        return
    ok, path = shell_env.upsert({shell_env.GH_TOKEN_NAME: token}, also_env=True)
    if ok:
        _row("ok", f"GH_TOKEN written to {path} — open a new shell to pick it up", pal, stream)
    else:
        _row("warn", "Could not persist GH_TOKEN — set it in your shell rc by hand", pal, stream)


def step_reviewers(repo: Optional[str], cwd: Optional[str], doctor: Dict[str, Any], *,
                   run, spawn_command, getpass_fn, pal, stream,
                   multi_select=multi_select, input_fn=input) -> Tuple[List[str], Dict[str, bool]]:
    """Step 5 — reviewer fleet: multi-select, validate each, capture auto_on_open."""
    _panel("Step 5 — Reviewer fleet", ["Enable only the reviewers you have set up on this repo."],
           pal, stream)
    labels = {
        "copilot": "Copilot   (GitHub Copilot code review)",
        "gemini": "Gemini    (Gemini Code Assist app)",
        "codex": "Codex     (OpenAI Codex app)",
        "claude": "Claude    (claude-code-review.yml workflow)",
    }
    options = [(labels[b], "") for b in _REVIEWERS]
    chosen_idx = multi_select("  Which reviewers should run?", options, preselected=None,
                              pal=pal, stream=stream, input_fn=input_fn)
    enabled = [_REVIEWERS[i] for i in sorted(chosen_idx)]
    if not enabled:
        _row("warn", "No reviewers selected — the loop will have nothing to fan out to", pal, stream)

    auto_on_open: Dict[str, bool] = {}
    for bot in enabled:
        if bot == "copilot":
            if doctor.get("gh_auth"):
                _row("ok", "Copilot — gh authenticated", pal, stream)
            else:
                _row("warn", "Copilot needs gh authenticated (and a paid Copilot plan)", pal, stream)
                _offer_gh_token(run=run, getpass_fn=getpass_fn, pal=pal, stream=stream, input_fn=input_fn)
        elif bot in ("gemini", "codex"):
            _row("info", f"{bot.capitalize()} — trusted from your selection "
                         f"(verify the GitHub app is installed on this repo)", pal, stream)
        elif bot == "claude":
            # Claude review needs TWO independent things: the workflow on the
            # default branch AND the CLAUDE_CODE_OAUTH_TOKEN repo secret. Check both
            # — a repo with the workflow committed but no token is still
            # non-functional, so the secret walkthrough runs regardless of whether
            # the workflow is already present.
            present, note = _probe_claude_workflow(repo, run)
            _row("ok" if present else "warn", f"Claude — {note}", pal, stream)
            if repo:
                if not present:
                    local_file = Path(cwd) / ".github" / "workflows" / "claude-code-review.yml" if cwd else None
                    if local_file and local_file.exists():
                        _row("info", f"Claude — local workflow file already exists at {local_file}. Commit and push it to activate.", pal, stream)
                    elif cwd and _ask_yes_no(
                            "Write the bundled claude-code-review.yml into this repo now?",
                            default=True, input_fn=input_fn, stream=stream):
                        dest = _write_workflow_template(cwd)
                        if dest:
                            _row("ok", f"Wrote {dest} — commit + push it to the default branch", pal, stream)
                            print(f"  {pal.GREY}{_ACTIONS_NOTE}{pal.RESET}", file=stream)
                        else:
                            _row("warn", "Could not write the workflow template", pal, stream)
                if doctor.get("gh_auth"):
                    _set_claude_secret(repo, run=run, spawn_command=spawn_command,
                                       getpass_fn=getpass_fn, pal=pal, stream=stream)

        # auto_on_open: Claude is mention-driven (never auto-reviews on open); the
        # GitHub-App bots are asked (default True).
        if bot == "claude":
            auto_on_open["claude"] = False
        else:
            auto_on_open[bot] = _ask_yes_no(
                f"Does {bot.capitalize()} auto-review when a PR is opened?",
                default=True, input_fn=input_fn, stream=stream)
    return enabled, auto_on_open


def step_monitoring_locked(*, pal, stream) -> None:
    """Step 6 — live monitoring (paid). A locked teaser; persists nothing."""
    _panel("Step 6 — Live monitoring", [], pal, stream)
    _teaser(_MONITORING_TEASER, pal, stream)


def step_summary(plan: str, repo: Optional[str], reviewers: Sequence[str],
                 auto_on_open: Dict[str, bool], *, pal, stream) -> None:
    """Step 7a — read-back."""
    _panel("Step 7 — What's active", [], pal, stream)
    _kv("Claude plan", plan, pal, stream)
    _kv("Repo", repo or "(inferred at runtime)", pal, stream)
    _kv("Reviewers", ", ".join(reviewers) or "(none)", pal, stream)
    aoo = ", ".join(f"{b}:{'auto' if v else 'summon'}" for b, v in auto_on_open.items()) or "(none)"
    _kv("Auto-on-open", aoo, pal, stream)
    _kv("Notifications", "console", pal, stream)


def step_done(path: Path, *, pal, stream) -> None:
    """Step 7b — done + launch hint."""
    _panel("Step 7 — Done", [
        f"Config written : {path}",
        "Re-run setup   : /review-pr setup",
        "Review a PR    : /review-pr <pr-number>   (omit to auto-select)",
        "Create a PR    : /create-pr",
    ], pal, stream)
    _teaser(_PRO_SOON_TEASER, pal, stream)


# ── Orchestration ──────────────────────────────────────────────────────────────

def run(*, argv: Optional[Sequence[str]] = None, config_path: Optional[Path] = None,
        run: Optional[Callable] = None, which: Optional[Callable] = None,
        single_select: Callable = single_select, multi_select: Callable = multi_select,
        getpass_fn: Optional[Callable] = None, spawn_command: Optional[Callable] = None,
        input_fn: Callable = input, stream=None) -> int:
    """Run the full wizard. Every external effect is injectable for testing. Writes
    the free config and returns an exit code (0 on success)."""
    stream = stream or sys.stdout
    run = run or _default_run
    which = which or shutil.which
    getpass_fn = getpass_fn or _default_getpass
    spawn_command = spawn_command or setup_launcher.spawn_command
    cfg_path = config_path or config.config_path()
    pal = _Palette(_colour_enabled(stream))

    preset_repo = None
    args = list(argv or [])
    if "--repo" in args:
        i = args.index("--repo")
        if i + 1 < len(args):
            preset_repo = args[i + 1]

    try:
        print(f"{pal.BOLD}buddhi-review setup{pal.RESET}", file=stream)

        doctor = step_doctor(run=run, which=which, pal=pal, stream=stream)
        plan = step_plan(doctor, pal=pal, stream=stream, single_select=single_select, input_fn=input_fn)
        repo, cwd = step_repo(preset_repo, run=run, pal=pal, stream=stream, input_fn=input_fn)
        step_budgets_locked(pal=pal, stream=stream)  # paid teaser
        reviewers, auto_on_open = step_reviewers(
            repo, cwd, doctor, run=run, spawn_command=spawn_command, getpass_fn=getpass_fn,
            pal=pal, stream=stream, multi_select=multi_select, input_fn=input_fn)
        step_monitoring_locked(pal=pal, stream=stream)  # paid teaser

        new_cfg = build_config(plan, repo, cwd, reviewers, auto_on_open)
        existing = config.load_config(cfg_path) if cfg_path.exists() else {}
        merged = merge_preserving(existing, new_cfg)
        ok = write_config(merged, cfg_path)

        step_summary(plan, repo, reviewers, auto_on_open, pal=pal, stream=stream)
        if ok:
            step_done(cfg_path, pal=pal, stream=stream)
            return 0
        _row("bad", f"Could not write {cfg_path} — check the path's permissions", pal, stream)
        return 1
    except KeyboardInterrupt:
        print(f"\n{pal.RED}Setup aborted.{pal.RESET}", file=stream)
        return 1


def _default_getpass(prompt: str) -> str:
    import getpass
    try:
        return getpass.getpass(prompt)
    except Exception:
        return ""
