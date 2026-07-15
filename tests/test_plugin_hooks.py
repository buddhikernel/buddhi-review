"""Tests for the plugin bootstrap hooks (``scripts/ensure_install.py`` +
``scripts/guardrail_hook.py``).

These make a plugin-only install (no ``pip install buddhi-review``) work out of
the box: SessionStart installs the package into ``${CLAUDE_PLUGIN_DATA}/site``
ONCE, and the PreToolUse guardrail entry degrades fail-open — one stderr line, no
traceback — if the package is still absent. The decision logic is exercised with
injected seams (no real ``pip``); the guardrail's degrade + delegate paths are
driven end-to-end through a real subprocess.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO / "scripts"
_ENSURE = _SCRIPTS / "ensure_install.py"
_GUARDRAIL = _SCRIPTS / "guardrail_hook.py"
_SKILLS = ("open-pr", "review-pr")


def _load(path, name):
    """Import a bootstrap script (which lives outside the package) by file path."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ensure_mod():
    return _load(_ENSURE, "ensure_install_under_test")


# ── ensure_install.ensure(): the skip/install decision ──────────────────────────
def test_ensure_noop_without_plugin_data(ensure_mod):
    """No CLAUDE_PLUGIN_DATA → not running as a plugin → do nothing, install NOT
    attempted."""
    calls = []
    result = ensure_mod.ensure(
        None,
        importable=lambda site: pytest.fail("must not probe without a data dir"),
        installer=lambda site: calls.append(site) or True,
    )
    assert result == "noop-no-data"
    assert calls == []


def test_ensure_skips_when_already_importable(ensure_mod, tmp_path):
    """The ONLY skip signal: import succeeds (global pip OR a prior data-dir
    install). The installer must NOT fire — never reinstall over an existing
    package (adversarial: 'make the install fire when already installed')."""
    installer_calls = []
    result = ensure_mod.ensure(
        str(tmp_path),
        importable=lambda site: True,
        installer=lambda site: installer_calls.append(site) or True,
    )
    assert result == "skip-importable"
    assert installer_calls == [], "must not reinstall over an importable package"


def test_ensure_installs_when_absent(ensure_mod, tmp_path):
    installer_calls = []
    result = ensure_mod.ensure(
        str(tmp_path),
        importable=lambda site: False,
        installer=lambda site: installer_calls.append(site) or True,
    )
    assert result == "installed"
    # Installs into <data>/site, the same dir the guardrail entry adds to sys.path.
    assert installer_calls == [str(tmp_path / "site")]


def test_ensure_reports_install_failure(ensure_mod, tmp_path):
    """An offline / failed install is reported but never raises — the caller
    (main) still exits 0 so plugin load is never treated as failed."""
    result = ensure_mod.ensure(
        str(tmp_path),
        importable=lambda site: False,
        installer=lambda site: False,
    )
    assert result == "install-failed"


def test_ensure_main_always_fails_open(ensure_mod, monkeypatch):
    """main() must return 0 regardless — even if the environment names a data dir
    and the probe/install would run — because plugin load must never fail."""
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    assert ensure_mod.main() == 0


def test_already_importable_true_in_this_env(ensure_mod):
    """Sanity: in the test env buddhi_review IS installed, so the real subprocess
    probe returns True (this is what makes the SessionStart install skip)."""
    assert ensure_mod.already_importable(None) is True


# ── guardrail_hook.py: degrade fail-open when the package is absent ──────────────
def _run_guardrail(command, *, argv_prefix, env=None, cwd=None):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    proc = subprocess.run(
        [sys.executable, *argv_prefix, str(_GUARDRAIL)],
        input=json.dumps(payload), capture_output=True, text=True,
        env=env, cwd=cwd,
    )
    return proc


def _package_isolated_from(flags, cwd):
    """True iff buddhi_review is genuinely unimportable under ``flags`` FROM ``cwd``
    — so the degrade test isolates the package rather than silently passing. Must
    use the SAME neutral cwd as the real invocation: a cwd inside the repo would
    make the in-tree ``buddhi_review/`` importable via the current directory."""
    probe = subprocess.run([sys.executable, *flags, "-c", "import buddhi_review"],
                           capture_output=True, text=True, cwd=cwd)
    return probe.returncode != 0


def test_guardrail_degrades_fail_open_when_package_absent(tmp_path):
    """Package unimportable (``-S -E`` skips site-packages / editable / PYTHONPATH,
    and neither the script dir nor the neutral cwd has buddhi_review) → exit 0,
    exactly ONE stderr line naming ``pip install buddhi-review``, and NO deny block
    on stdout (fail-open)."""
    flags = ["-S", "-E"]
    if not _package_isolated_from(flags, str(tmp_path)):
        pytest.skip("environment cannot isolate buddhi_review from the interpreter")
    # A command the guardrail WOULD block if it were active — proving it fails open.
    proc = _run_guardrail("git rebase main", argv_prefix=flags, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "must NOT emit a deny block when degraded (fail-open)"
    lines = [ln for ln in proc.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly one stderr line, got: {proc.stderr!r}"
    assert "pip install buddhi-review" in lines[0], \
        f"degrade line must name the fix: {lines[0]!r}"
    assert "Traceback" not in proc.stderr and "ModuleNotFoundError" not in proc.stderr


def test_guardrail_delegates_to_real_hook_when_package_present():
    """With the package importable, the entry delegates to the real guardrail: a
    history-rewriting command is denied, a safe command is allowed."""
    blocked = _run_guardrail("git rebase main", argv_prefix=[], cwd=str(_REPO))
    assert blocked.returncode == 0, blocked.stderr
    hso = json.loads(blocked.stdout)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "BUDDHI_ALLOW_MANUAL_GIT=1" in hso["permissionDecisionReason"]

    safe = _run_guardrail("git commit -m x", argv_prefix=[], cwd=str(_REPO))
    assert safe.returncode == 0 and safe.stdout == "", \
        "a safe command must produce no deny block"


# ── the SKILL.md dispatch command routes correctly through a real shell ──────────
def _skill_hook_command(skill):
    text = (_REPO / "buddhi_review" / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---", 2)[1])
    cmds = [h["command"] for entry in fm["hooks"]["PreToolUse"]
            if entry.get("matcher") == "Bash"
            for h in entry.get("hooks", []) if h.get("type") == "command"]
    assert len(cmds) == 1, f"{skill}: expected exactly one Bash PreToolUse command"
    return cmds[0]


def _shell(command, payload, *, env, cwd):
    return subprocess.run(command, shell=True, input=payload, capture_output=True,
                          text=True, env=env, cwd=cwd)


@pytest.mark.parametrize("skill", _SKILLS)
def test_skill_command_pip_path_routes_to_module(skill, tmp_path):
    """No CLAUDE_PLUGIN_ROOT (pip install) → the command runs the module directly.
    Executed through a real shell exactly as Claude Code would, so a broken quoting
    or a non-POSIX-portable form is caught. A history-rewrite is denied; a safe
    command is allowed."""
    cmd = _skill_hook_command(skill)
    env = {k: v for k, v in __import__("os").environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    blocked = _shell(cmd, json.dumps({"tool_name": "Bash",
                     "tool_input": {"command": "git rebase main"}}), env=env, cwd=str(_REPO))
    assert blocked.returncode == 0, blocked.stderr
    assert json.loads(blocked.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    safe = _shell(cmd, json.dumps({"tool_name": "Bash",
                  "tool_input": {"command": "git commit -m x"}}), env=env, cwd=str(_REPO))
    assert safe.returncode == 0 and safe.stdout == ""


@pytest.mark.parametrize("skill", _SKILLS)
def test_skill_command_plugin_path_routes_to_entry(skill, tmp_path):
    """CLAUDE_PLUGIN_ROOT set (plugin install) → the command runs the plugin entry
    (scripts/guardrail_hook.py), which delegates to the real guardrail when the
    package is importable. Run from a neutral cwd so only the routing (not a stray
    in-tree import) explains the result."""
    import os
    cmd = _skill_hook_command(skill)
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(_REPO)
    blocked = _shell(cmd, json.dumps({"tool_name": "Bash",
                     "tool_input": {"command": "git rebase main"}}), env=env, cwd=str(tmp_path))
    assert blocked.returncode == 0, blocked.stderr
    assert json.loads(blocked.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
