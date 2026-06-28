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
provision the workflow server-side — when the checkout is on a feature branch it
opens a ``gh``-api PR that lands ``claude-code-review.yml`` on the default branch
(an issue_comment workflow runs ONLY from there) without touching the local tree,
falling back to a local copy — and set the ``CLAUDE_CODE_OAUTH_TOKEN`` secret. The
secret is scoped repo-only by default (least blast radius); on an org-owned repo
an explicit, separately-confirmed opt-in can scope it org-wide but visible to this
repo alone, gated on org-admin with a repo-scope fallback.

The wizard is an interactive raw-mode TTY program; the ``/review-pr setup`` skill
step opens it in a fresh window via :mod:`buddhi_review.setup_launcher`. Every
external effect (subprocess runner, the selectors, prompts, the spawn helper, the
output stream) is injectable, so the step-gating logic is unit-testable without a
real terminal.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
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

# A clear, single-purpose Claude re-check prompt. Names the workflow file
# explicitly and SPLITS the two independent facts (the workflow is committed on
# the default branch · the CLAUDE_CODE_OAUTH_TOKEN secret is set) instead of
# mashing them into one run-on question, so a user who just merged the install PR
# or set the secret in another window can re-confirm in one keypress.
_CLAUDE_RECHECK_PROMPT = (
    "Has the workflow file claude-code-review.yml been committed on the default "
    "branch (and the CLAUDE_CODE_OAUTH_TOKEN secret set)? Shall I confirm now?")


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


# ── GitHub-fact probes (the provisioning engine reads these) ──────────────────────

def _owner_type(owner: Optional[str], *, run) -> Optional[str]:
    """Whether a repo's ``owner`` is a GitHub Organization or a personal User
    account, via ``gh api users/<owner> --jq .type`` (the Users API reports both
    kinds and returns "Organization" for org logins). ``owner`` may be a bare login
    or an ``owner/repo`` slug — the repo part is dropped. Returns
    ``"Organization"`` / ``"User"``, or ``None`` when the lookup can't run (no
    owner, a malformed login, a gh error, or an unexpected value); callers treat
    ``None`` as "not an org" and stay on the safe repo-scoped path."""
    if not owner:
        return None
    login = str(owner).split("/", 1)[0].strip()
    if not login or not re.match(r"^[A-Za-z0-9_-]+$", login):
        return None
    ok, out = _run_ok(run, ["gh", "api", f"users/{login}", "--jq", ".type"])
    if not ok:
        return None
    t = out.strip()
    return t if t in ("Organization", "User") else None


def _default_branch(repo: Optional[str], *, run) -> Optional[str]:
    """The repo's default branch name (the ONLY branch an issue_comment workflow
    runs from), via ``gh repo view``. ``None`` when it can't be read."""
    if not repo:
        return None
    ok, out = _run_ok(run, ["gh", "repo", "view", repo, "--json", "defaultBranchRef",
                            "--jq", ".defaultBranchRef.name"])
    if not ok:
        return None
    return out.strip() or None


def _current_branch(cwd: Optional[str], *, run) -> Optional[str]:
    """The checked-out branch name in ``cwd``, or ``None`` (no cwd, not a repo,
    detached HEAD, or git unavailable). A detached HEAD reports 'HEAD' from
    rev-parse — treated as unknown since it is not a branch the workflow could live
    on."""
    if not cwd:
        return None
    ok, out = _run_ok(run, ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"])
    if not ok:
        return None
    b = out.strip()
    return b if (b and b != "HEAD") else None


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


def _create_file_pr(repo: str, default_branch: str, path: str, content: str,
                    message: str, title: str, branch: str, *,
                    recovery_path: Optional[str] = None, run) -> Tuple[bool, str]:
    """Open a PR that adds an arbitrary file (``path``, text ``content``) to the
    repo's DEFAULT branch entirely server-side — no local checkout is touched, so
    the user's feature branch and working tree are left exactly as they were:

      1. read the default branch's head SHA,
      2. create a fresh ref ``branch`` off it,
      3. PUT ``content`` to ``path`` on that ref (one commit, message ``message``),
      4. open a PR titled ``title`` (base=default, head=branch).

    Generic by design — it installs the Claude review workflow today and the
    ``ready-for-ci`` template later (F4). ``recovery_path`` (defaults to ``path``)
    is the file probed when a previous partial run already created ``branch``: its
    blob SHA is needed for the Contents-API update (a PUT over an existing file 422s
    without it), so a re-run reuses the branch and updates the file instead of
    failing. Returns ``(ok, detail)`` — ``detail`` is the PR URL on success, else a
    short reason. The PUT needs a token with the ``workflow`` scope; without it
    GitHub rejects the write and the caller falls back to a local copy."""
    path = str(path).lstrip("/")
    recovery_path = str(recovery_path or path).lstrip("/")
    enc_path = urllib.parse.quote(path, safe="/")
    enc_recovery = urllib.parse.quote(recovery_path, safe="/")
    enc_branch = urllib.parse.quote(branch, safe="")
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    try:
        head = run(["gh", "api", f"repos/{repo}/git/ref/heads/{default_branch}",
                    "--jq", ".object.sha"], timeout=20)
        if getattr(head, "returncode", 1) != 0 or not (getattr(head, "stdout", "") or "").strip():
            return (False, "couldn't read the default branch head")
        sha = head.stdout.strip()
        ref = run(["gh", "api", f"repos/{repo}/git/refs",
                   "-f", f"ref=refs/heads/{branch}", "-f", f"sha={sha}"], timeout=20)
        sha_args: List[str] = []
        if getattr(ref, "returncode", 1) != 0:
            # Branch creation failed — it may already exist from a partial earlier
            # run. Reuse it (so recovery needs no manual cleanup) but ONLY after
            # confirming it really exists; otherwise the create genuinely failed.
            probe = run(["gh", "api", f"repos/{repo}/git/ref/heads/{enc_branch}"], timeout=15)
            if getattr(probe, "returncode", 1) != 0:
                return (False, f"couldn't create branch '{branch}'")
            # The Contents API requires the blob sha when updating an existing file
            # (422 otherwise); probe `recovery_path` on the branch so a re-run that
            # already committed the file updates it cleanly.
            file_probe = run(["gh", "api",
                              f"repos/{repo}/contents/{enc_recovery}?ref={enc_branch}",
                              "--jq", ".sha"], timeout=15)
            if getattr(file_probe, "returncode", 1) == 0 and (file_probe.stdout or "").strip():
                sha_args = ["-f", f"sha={file_probe.stdout.strip()}"]
        put = run(["gh", "api", "-X", "PUT",
                   f"repos/{repo}/contents/{enc_path}",
                   "-f", f"message={message}",
                   "-f", f"content={b64}", "-f", f"branch={branch}"] + sha_args, timeout=20)
        if getattr(put, "returncode", 1) != 0:
            return (False, "couldn't write the file "
                           "(for workflow paths the gh token may need the 'workflow' scope)")
        pr = run(["gh", "pr", "create", "--repo", repo,
                  "--base", default_branch, "--head", branch,
                  "--title", title,
                  "--body", f"Adds `{path}` to the default branch. "
                            "(Opened by the Buddhi setup wizard.)"], timeout=30)
    except Exception as exc:
        return (False, type(exc).__name__)
    if getattr(pr, "returncode", 1) != 0:
        return (False, "branch pushed but opening the PR failed")
    out = (getattr(pr, "stdout", "") or "").strip()
    url = out.splitlines()[-1] if out else "(PR opened)"
    return (True, url)


def _offer_install_claude_workflow(repo: str, cwd: Optional[str], *, run, pal, stream,
                                   input_fn=input) -> Optional[str]:
    """Offer to install ``claude-code-review.yml`` when it is absent from the
    default branch. An issue_comment workflow runs ONLY from the default branch, so
    a silent write into a feature checkout never enables reviews. On a feature
    branch this offers the SERVER-SIDE route first — a ``gh``-api PR that lands the
    workflow on the default branch on a fresh branch, leaving the local checkout
    untouched — and falls back to a local copy only if that fails or is declined.
    Returns ``'pr'`` (a PR was opened), ``True`` (written into the local checkout),
    or ``None`` (nothing done)."""
    template = _workflow_template_path()
    if not template.exists():
        return None
    try:
        template_text: Optional[str] = template.read_text(encoding="utf-8")
    except OSError:
        template_text = None

    cur = _current_branch(cwd, run=run)
    default = _default_branch(repo, run=run)
    on_feature = bool(cur and default and cur != default)

    if on_feature and template_text is not None and _is_tty():
        _row("warn", f"This checkout is on '{cur}', not the default branch "
                     f"'{default}'. GitHub runs the review workflow only from the "
                     f"default branch, so committing it to '{cur}' won't enable "
                     f"Claude reviews until it lands on '{default}'.", pal, stream)
        if _ask_yes_no(f"Open a PR that adds the workflow to '{default}' on a fresh "
                       "branch instead?", default=True, input_fn=input_fn, stream=stream):
            ok, detail = _create_file_pr(
                repo, default,
                ".github/workflows/claude-code-review.yml",       # path
                template_text,                                    # content
                "Add Claude code-review workflow (Buddhi setup)",  # message
                "Add Claude code-review workflow",                # title
                "buddhi/add-claude-review-workflow",              # branch
                run=run)
            if ok:
                _row("ok", f"Opened a PR to add the workflow: {detail}", pal, stream)
                print(f"  {pal.GREY}{_ACTIONS_NOTE}{pal.RESET}", file=stream)
                _row("info", f"Merge that PR to put the workflow on '{default}', then "
                             "set the CLAUDE_CODE_OAUTH_TOKEN secret below.", pal, stream)
                return "pr"
            _row("warn", f"Could not open the PR automatically ({detail}). Falling "
                         "back to a local copy you can land yourself.", pal, stream)
            # fall through to the local-write offer

    # Local-copy fallback (the historical behavior).
    local_file = Path(cwd) / ".github" / "workflows" / "claude-code-review.yml" if cwd else None
    if local_file and local_file.exists():
        _row("info", f"Claude — local workflow file already exists at {local_file}. "
                     "Commit and push it to the default branch to activate.", pal, stream)
        return True
    if cwd and _ask_yes_no("Write the bundled claude-code-review.yml into this repo now?",
                           default=True, input_fn=input_fn, stream=stream):
        dest = _write_workflow_template(cwd)
        if dest:
            _row("ok", f"Wrote {dest} — commit + push it to the default branch", pal, stream)
            print(f"  {pal.GREY}{_ACTIONS_NOTE}{pal.RESET}", file=stream)
            return True
        _row("warn", "Could not write the workflow template", pal, stream)
    return None


def _gh_secret_exists(repo: str, name: str, *, org: Optional[str] = None, run) -> Optional[bool]:
    """Whether ``name`` is among the Actions secrets visible to ``repo``. Checks the
    repo's own secrets (``gh secret list --repo``); when ``repo``'s owner is an
    Organization it ALSO checks the org-level secrets actually shared with ``repo``
    via ``repos/{repo}/actions/organization-secrets`` (NOT ``gh secret list --org``,
    which lists ALL org secrets regardless of per-repo visibility — a
    ``selected``-visibility org secret absent from this repo's share list would
    false-positive and skip prompting). Returns ``True`` / ``False``, or ``None``
    when NEITHER check could run (so the caller offers to set it rather than
    silently assume it is present). ``org`` is auto-resolved from the owner when not
    given; pass it explicitly to avoid a repeat owner-type lookup."""
    if not repo:
        return None
    if org is None and "/" in repo:
        owner = repo.split("/", 1)[0]
        if _owner_type(owner, run=run) == "Organization":
            org = owner

    def _scan(args) -> Tuple[bool, bool]:
        # (ran, found). `gh secret list` prints "NAME\tUPDATED" per line; match the
        # first whitespace-delimited token exactly so a name that is a substring of
        # another can't false-positive.
        ok, out = _run_ok(run, ["gh", "secret", "list"] + args)
        if not ok:
            return (False, False)
        for ln in out.splitlines():
            tok = ln.split()
            if tok and tok[0].upper() == name.upper():
                return (True, True)
        return (True, False)

    ran_repo, found = _scan(["--repo", repo])
    if found:
        return True
    if org:
        # Repo-scoped org-secrets endpoint: only the org secrets actually shared
        # with THIS repo, not the org-wide list.
        ok, out = _run_ok(run, ["gh", "api", "--paginate",
                                f"repos/{repo}/actions/organization-secrets",
                                "--jq", ".secrets[].name"])
        if ok:
            org_names = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if name.upper() in (on.upper() for on in org_names):
                return True
            # Org check ran and missed it; if the repo check itself failed we still
            # can't rule out a repo-level secret — say unknown.
            return False if ran_repo else None
        return None  # org API failed; status unknown
    return False if ran_repo else None


def _org_admin(org: Optional[str], *, run) -> Optional[bool]:
    """Whether the authenticated gh user is an ADMIN (owner) of ``org``, via the
    org-membership API (``role`` is "admin" for owners, "member" otherwise). Only an
    org admin can create org-level Actions secrets, so the org-scope opt-in is gated
    on this. Returns ``True`` / ``False``, or ``None`` when the check can't run (no
    org, gh error, or the user isn't a member) — callers treat anything but ``True``
    as 'not confirmed' and stay repo-scoped."""
    if not org or not re.match(r"^[A-Za-z0-9_-]+$", org):
        return None
    ok, out = _run_ok(run, ["gh", "api", f"user/memberships/orgs/{org}", "--jq", ".role"])
    if not ok:
        return None
    return out.strip() == "admin"


def _set_gh_secret(repo: str, name: str, value: str, *, org: Optional[str] = None,
                   run) -> Tuple[bool, str]:
    """Set the Actions secret ``name`` to ``value`` by piping the value into
    ``gh secret set`` on stdin (the ``--body`` flag reads stdin when omitted, so the
    secret never appears on argv or in the process list).

    Scope: by default the secret is set on ``repo`` (``--repo``, the least blast
    radius). When ``org`` is given it is set org-wide with ``--visibility selected``,
    and the current repo is UNIONED into the existing selected-repo list (fetched
    first) so repos already sharing the secret keep access — gh ``--repos`` REPLACES
    the list, it does not append. Returns ``(ok, detail)``."""
    if not (repo and value):
        return (False, "missing repo or value")
    if org:
        # --visibility selected requires --repos; the org's repo is named without the
        # owner (gh resolves it within the org). Union the current repo into the
        # existing selected list so omitting it can't silently revoke other repos.
        repo_name = repo.split("/", 1)[-1]
        repos_to_set = [repo_name]
        ok, out = _run_ok(run, ["gh", "api", "--paginate",
                                f"orgs/{org}/actions/secrets/{name}/repositories",
                                "--jq", ".repositories[].name"])
        if ok:
            existing = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if existing:
                repos_to_set = existing if repo_name in existing else existing + [repo_name]
        argv = ["gh", "secret", "set", name, "--org", org,
                "--visibility", "selected", "--repos", ",".join(repos_to_set)]
    else:
        argv = ["gh", "secret", "set", name, "--repo", repo]
    try:
        r = run(argv, input=value, timeout=20)
    except Exception as exc:
        return (False, type(exc).__name__)
    if getattr(r, "returncode", 1) != 0:
        return (False, (getattr(r, "stderr", "") or "").strip() or "gh secret set failed")
    return (True, "secret set")


def _set_secret_scoped(repo: str, name: str, value: str, *, prefer_org: bool = False,
                       run) -> Tuple[bool, str, str]:
    """Set the Actions secret ``name`` to ``value``, choosing the scope SAFELY.

    Default → repo scope (``--repo``, the least blast radius). This seam NEVER goes
    org-wide on its own; ``prefer_org`` is the caller's EXPLICIT, separately-confirmed
    opt-in (offered only when ``_owner_type`` reports an Organization). Even with
    ``prefer_org`` it goes org-wide ONLY after confirming the gh user is an org admin
    (``_org_admin``); on a non-admin org, a permission denial, or ANY org-set failure
    it FALLS BACK to repo scope — the flow never fails because of an org attempt. The
    org secret is scoped to this repo only (``--visibility selected``), so it is not
    exposed across the whole org. Returns ``(ok, detail, scope)`` where ``scope`` is
    the scope actually used ("org" or "repo")."""
    if prefer_org and repo and "/" in repo:
        org = repo.split("/", 1)[0]
        if _org_admin(org, run=run) is True:
            ok, detail = _set_gh_secret(repo, name, value, org=org, run=run)
            if ok:
                return (ok, detail, "org")
            # org set failed (permission/denial/other) → fall back to repo scope
        # non-admin, unknown role, or failed org set → repo-scope fallback
    ok, detail = _set_gh_secret(repo, name, value, run=run)
    return (ok, detail, "repo")


def _set_claude_secret(repo: str, *, run, spawn_command, getpass_fn, pal, stream,
                       single_select=single_select, input_fn=input,
                       owner_type: Optional[str] = None) -> str:
    """Walk the user through setting ``CLAUDE_CODE_OAUTH_TOKEN``. Returns a short
    status string.

    Dual-credential (FREE): either credential satisfies the workflow — the
    pay-as-you-go ``ANTHROPIC_API_KEY`` or the subscription
    ``CLAUDE_CODE_OAUTH_TOKEN`` — and the bundled template accepts both, so the
    prompt is SKIPPED when EITHER is already present. The existence check is
    org-aware: an org-set secret won't appear in ``gh secret list --repo``.

    Safe scoping: the new secret is set on THIS repo only by default (least blast
    radius). When ``repo``'s owner is an Organization an EXPLICIT, separately-
    confirmed opt-in to scope it org-wide (visible to this repo alone) is offered
    and routed through ``_set_secret_scoped``, which enforces the org-admin check
    and the repo-scope fallback — so this never widens exposure without consent."""
    name = "CLAUDE_CODE_OAUTH_TOKEN"
    if owner_type is None and "/" in repo:
        owner_type = _owner_type(repo, run=run)
    org = repo.split("/", 1)[0] if (owner_type == "Organization" and "/" in repo) else None
    # Skip the prompt only when one credential is CONFIRMED present (True). An
    # unknown result (None) is NOT treated as present — we offer to set it.
    if (_gh_secret_exists(repo, name, org=org, run=run) is True or
            _gh_secret_exists(repo, "ANTHROPIC_API_KEY", org=org, run=run) is True):
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
    # Scope SAFELY: repo-only by default. On an org-owned repo, offer the explicit
    # org-wide opt-in (still visible to this repo alone); _set_secret_scoped checks
    # org-admin and falls back to repo scope on any denial.
    prefer_org = False
    if org and _is_tty():
        idx = single_select(
            f"{org} is a GitHub organization. Where should {name} live?",
            [("This repository only (recommended)",
              "least exposure — a repo secret on this repo alone"),
             (f"Org-wide, scoped to {repo.split('/')[-1]} only",
              "an org secret visible only to this repo (needs org-admin; "
              "falls back to repo scope otherwise)")],
            preselect=0, pal=pal, stream=stream, input_fn=input_fn)
        prefer_org = (idx == 1)
    ok, detail, scope = _set_secret_scoped(repo, name, token, prefer_org=prefer_org, run=run)
    if ok:
        _row("ok", f"{name} set on {repo} ({scope} scope)", pal, stream)
        return "set"
    _row("warn", f"Could not set {name} ({detail}) — set it by hand with "
                 f"`gh secret set {name} --repo {repo}`", pal, stream)
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


def _app_install_lines(bot: str, repo: Optional[str]) -> List[str]:
    """The GitHub-UI steps to install an app-backed reviewer on ``repo``. The vendor
    apps can't be installed via API, so setup GUIDES the install. Claude needs the
    ``github.com/apps/claude`` App too — its workflow + token alone 401 and post
    nothing (the silent-Claude failure: a workflow run that 401s with 'Claude Code
    is not installed on this repository')."""
    where = f"`{repo}`" if repo else "this repo"
    if bot == "claude":
        return [
            "Install the Claude GitHub App:  github.com/apps/claude",
            f"then grant it access to {where}.",
            "Without it the claude-code-review run fails 401 (\"Claude Code is not "
            "installed on this repository\") and claude[bot] posts NOTHING — the "
            "workflow + token alone are NOT enough.",
        ]
    if bot == "codex":
        return ["Install the OpenAI Codex app via Codex ▸ Settings ▸ Connectors ▸ "
                f"GitHub, and grant it access to {where}.",
                "It then replies to '@codex review' on a PR."]
    if bot == "gemini":
        return [f"Install  github.com/apps/gemini-code-assist  and grant it access "
                f"to {where}.",
                "It then replies to '/gemini review' on a PR."]
    return [f"Install {bot}'s GitHub app and grant it access to {where}."]


def _guide_app_install(bot: str, repo: Optional[str], *, pal, stream) -> None:
    """Print a PROMINENT (bold-headed panel) app-install guide so the steps never
    get lost among the other ✓/⚠ rows. Used for the vendor GitHub-App reviewers and
    — equally REQUIRED, not optional — the Claude GitHub App."""
    required = " (REQUIRED)" if bot == "claude" else ""
    _panel(f"{bot.capitalize()} GitHub App{required} — install it + grant repo access",
           _app_install_lines(bot, repo), pal, stream)


def step_reviewers(repo: Optional[str], cwd: Optional[str], doctor: Dict[str, Any], *,
                   run, spawn_command, getpass_fn, pal, stream,
                   multi_select=multi_select, single_select=single_select,
                   input_fn=input) -> Tuple[List[str], Dict[str, bool]]:
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
            # The vendor app can't be installed via API — GUIDE the install
            # prominently (legible, not a dim aside) so it isn't missed.
            _guide_app_install(bot, repo, pal=pal, stream=stream)
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
                    _offer_install_claude_workflow(repo, cwd, run=run, pal=pal,
                                                   stream=stream, input_fn=input_fn)
                if doctor.get("gh_auth"):
                    _set_claude_secret(repo, run=run, spawn_command=spawn_command,
                                       getpass_fn=getpass_fn, pal=pal, stream=stream,
                                       single_select=single_select, input_fn=input_fn)
                # P7 #1: a clear, single-purpose re-check — it names the workflow
                # file and splits the two facts (workflow committed · secret set)
                # so a user who just merged the install PR / set the secret can
                # re-confirm without re-running the wizard.
                if not present and _is_tty() and _ask_yes_no(
                        _CLAUDE_RECHECK_PROMPT, default=False,
                        input_fn=input_fn, stream=stream):
                    present2, note2 = _probe_claude_workflow(repo, run)
                    _row("ok" if present2 else "warn", f"Claude — {note2}", pal, stream)
            # The THIRD requirement, beyond the workflow + token: the Claude GitHub
            # App. Without it the workflow 401s ("Claude Code is not installed on
            # this repository") and posts nothing — guide it prominently so it's
            # never missed (the buddhi-review PR #3 silent-Claude failure).
            _guide_app_install("claude", repo, pal=pal, stream=stream)

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
            pal=pal, stream=stream, multi_select=multi_select, single_select=single_select,
            input_fn=input_fn)
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
