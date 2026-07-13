"""Full-replay click-to-tail + opt-in auto-open for the console launcher.

Two macOS-only, additive behaviors of ``buddhi_review/launch-review.sh``:

A. Full-replay tail — the generated ``<log>.command`` runs ``tail -n +1 -f <log>``
   so a window opened at any time replays the whole log from line 1, not just the
   default last 10 lines.

B. Opt-in auto-open (default OFF) — under the launcher's Darwin guard, when
   ``BUDDHI_TAIL_AUTO_OPEN`` is truthy the launcher runs ``open -g <tailcmd>`` after
   a ``pgrep`` dedupe. A suppression env (``BUDDHI_TAIL_NO_AUTO_OPEN``) hard-disables
   it regardless — a batch fan-out exports it so it never pops N windows. Auto-open
   is resolved from the environment only (no config-key read).

Bash-harness style: the DETACHED loop interpreter is neutralized via the
``BUDDHI_LAUNCH_PYTHON=true`` seam, and the external ``open`` / ``pgrep`` are stubbed
via the ``BUDDHI_OPEN_BIN`` / ``BUDDHI_PGREP_BIN`` seams pointed at recorder scripts
— so the whole launcher (tailcmd write + auto-open) runs without spawning a real
review loop NOR a real Terminal window. ``TMPDIR`` is pinned to the test tmpdir so
the log + .command never touch the real ``/tmp``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from conftest import _log_line

from buddhi_review import tmp_paths

_PUBLIC_ROOT = Path(__file__).resolve().parent.parent
_LAUNCHER = _PUBLIC_ROOT / "buddhi_review" / "launch-review.sh"

_PR = "9"
_REPO = "acme/demo"  # sanitized; the launcher forwards --repo verbatim to python

# Recorder: append every argv to a file so the test can assert -g + the path.
_OPEN_RECORDER = """#!/bin/bash
echo "$@" >> "{out}"
exit 0
"""

# pgrep stubs: no-match (exit 1, the "no live window" case) vs match (exit 0).
_PGREP_NOMATCH = "#!/bin/bash\nexit 1\n"
_PGREP_MATCH = "#!/bin/bash\nexit 0\n"

# uname stub: echoes 'Darwin' so the launcher's Darwin guard fires on any OS.
# The launcher calls bare `uname` (PATH-resolved), so shadowing it via a stub
# directory prepended to PATH is sufficient — production behavior is unchanged.
_UNAME_DARWIN = "#!/bin/bash\necho Darwin\n"


@pytest.fixture
def harness(tmp_path):
    open_argv = tmp_path / "open_argv.txt"
    open_rec = tmp_path / "open_rec.sh"
    open_rec.write_text(_OPEN_RECORDER.format(out=open_argv))
    open_rec.chmod(0o755)

    pg_nomatch = tmp_path / "pgrep_nomatch.sh"
    pg_nomatch.write_text(_PGREP_NOMATCH)
    pg_nomatch.chmod(0o755)
    pg_match = tmp_path / "pgrep_match.sh"
    pg_match.write_text(_PGREP_MATCH)
    pg_match.chmod(0o755)

    stub_bin = tmp_path / "stub_bin"
    stub_bin.mkdir()
    uname_stub = stub_bin / "uname"
    uname_stub.write_text(_UNAME_DARWIN)
    uname_stub.chmod(0o755)

    def run(env_overrides=None, *, pgrep="nomatch"):
        env = dict(os.environ)
        # Pin the temp dir so LOG/TAILCMD land in the test tmpdir.
        env["TMPDIR"] = str(tmp_path)
        # Neutralize the DETACHED loop — point its interpreter at `true`.
        env["BUDDHI_LAUNCH_PYTHON"] = "true"
        env["BUDDHI_OPEN_BIN"] = str(open_rec)
        env["BUDDHI_PGREP_BIN"] = str(pg_match if pgrep == "match" else pg_nomatch)
        # Shadow system uname so the launcher's Darwin guard fires on any OS.
        env["PATH"] = f"{stub_bin}:{env.get('PATH', '/usr/bin:/bin')}"
        # Start from a clean auto-open state; overrides set the case under test.
        env.pop("BUDDHI_TAIL_AUTO_OPEN", None)
        env.pop("BUDDHI_TAIL_NO_AUTO_OPEN", None)
        if env_overrides:
            for k, v in env_overrides.items():
                if v is None:
                    env.pop(k, None)
                else:
                    env[k] = v
        return subprocess.run(
            ["bash", str(_LAUNCHER), _PR, "--repo", _REPO],
            capture_output=True, text=True, env=env, cwd=str(tmp_path))

    run.tmp = tmp_path
    run.open_argv = open_argv
    # Both filenames carry the repo name now (buddhi-<repo>-PR<pr>.log /
    # review-tail-<repo>-PR<pr>.command), single-sourced in tmp_paths.
    run.log = tmp_path / tmp_paths.log_name(_PR, _REPO)
    run.tailcmd = tmp_path / tmp_paths.tailcmd_name(_PR, _REPO)
    return run


def _opened(harness):
    return harness.open_argv.read_text().strip() if harness.open_argv.exists() else ""




# ── Cross-platform: arg validation + stream contract ───────────────────────────

def test_usage_when_no_args():
    r = subprocess.run(["bash", str(_LAUNCHER)], capture_output=True, text=True)
    assert r.returncode == 2
    assert "usage:" in r.stderr


def test_rejects_non_integer_pr():
    r = subprocess.run(["bash", str(_LAUNCHER), "abc", "--repo", _REPO],
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "positive integer" in r.stderr


def test_rejects_zero_pr():
    r = subprocess.run(["bash", str(_LAUNCHER), "0", "--repo", _REPO],
                       capture_output=True, text=True)
    assert r.returncode == 2


def test_stdout_carries_log_path_plus_notice_relay_lines(harness):
    r = harness()
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    # stdout carries the machine-readable `log:` datum FIRST, then the S3 NOTICE:
    # relay lines (the tier-neutral SKILL.md relays every NOTICE: line to chat).
    # Non-NOTICE decoration ("Cleared for takeoff", the follow hint) stays on stderr.
    assert lines[0] == f"log: {harness.log}"
    notices = [ln for ln in lines if ln.startswith("NOTICE: ")]
    assert notices, f"no NOTICE: line on stdout — the S3 relay would be dead:\n{r.stdout}"
    # Every stdout line is either the log datum or a NOTICE: line — no stray
    # decoration leaked onto the machine-readable stream.
    assert all(ln == f"log: {harness.log}" or ln.startswith("NOTICE: ") for ln in lines), r.stdout
    assert "launched" not in r.stdout
    assert "launched" in r.stderr


def test_notice_lines_point_at_the_live_log(harness):
    """The launcher emits `NOTICE: `-prefixed stdout lines that the tier-neutral
    SKILL.md relays verbatim — this engine's "where to watch" pointer is the local
    live log. Under the Darwin harness both the universal `tail` NOTICE and the
    macOS clickable `file://` NOTICE are present."""
    r = harness()
    assert r.returncode == 0, r.stderr
    notices = [ln for ln in r.stdout.splitlines() if ln.startswith("NOTICE: ")]
    # Universal pointer (every OS): the full-replay tail command.
    assert any("tail -n +1 -f" in ln for ln in notices), notices
    # macOS clickable pointer (the harness forces Darwin via the uname stub).
    assert any("file://" in ln for ln in notices), notices
    # Every relayed pointer resolves to THIS run's local log — the launcher never
    # points the user at a remote/off-box surface.
    assert all(str(harness.log) in ln or str(harness.tailcmd) in ln for ln in notices), notices
    assert "://" not in "".join(
        ln.split("file://")[0] for ln in notices), notices  # no other URL scheme


def test_follow_hint_uses_full_replay(harness):
    r = harness()
    assert r.returncode == 0, r.stderr
    # The printed follow hint is full-replay too (tail -n +1 -f), not bare tail -f.
    assert "tail -n +1 -f" in r.stderr
    assert "tail -f " not in r.stderr


# ── Repo-named log: buddhi-<repo>-PR<n>.log (no PR-number cross-repo collision) ──


def test_log_name_carries_the_repo_short_name(harness):
    # The headline fix: with --repo acme/demo the log basename is the repo-keyed
    # buddhi-demo-PR9.log (NOT the old repo-less buddhi-review-PR9.log), so a second
    # repo reviewing PR #9 writes a different file and the two never stomp.
    r = harness()
    assert r.returncode == 0, r.stderr
    assert _log_line(r.stdout) == str(harness.tmp / "buddhi-demo-PR9.log")
    assert "buddhi-review-PR9.log" not in r.stdout  # the old repo-less name is gone


def test_log_name_falls_back_to_local_without_repo(tmp_path):
    # No --repo (the launcher only receives it when the front door knows the repo) →
    # the <repo> segment is "local", matching tmp_paths.repo_short(None).
    env = dict(os.environ)
    env["TMPDIR"] = str(tmp_path)
    env.pop("BUDDHI_REVIEW_TMP", None)
    env["BUDDHI_LAUNCH_PYTHON"] = "true"
    env["BUDDHI_SKIP_LIVENESS_CHECK"] = "1"
    env["BUDDHI_TAIL_NO_AUTO_OPEN"] = "1"
    r = subprocess.run(["bash", str(_LAUNCHER), _PR],
                       capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert _log_line(r.stdout) == str(tmp_path / tmp_paths.log_name(_PR))
    assert _log_line(r.stdout).endswith("buddhi-local-PR9.log")


def test_log_name_ignores_flags_as_repo_value(tmp_path):
    # If --repo is followed by another flag (e.g. --cwd), it must not treat the
    # flag as the repo name — the log should fall back to "local", not "cwd".
    env = dict(os.environ)
    env["TMPDIR"] = str(tmp_path)
    env.pop("BUDDHI_REVIEW_TMP", None)
    env["BUDDHI_LAUNCH_PYTHON"] = "true"
    env["BUDDHI_SKIP_LIVENESS_CHECK"] = "1"
    env["BUDDHI_TAIL_NO_AUTO_OPEN"] = "1"
    r = subprocess.run(["bash", str(_LAUNCHER), _PR, "--repo", "--cwd", "/some/path"],
                       capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert _log_line(r.stdout).endswith("buddhi-local-PR9.log")


# ── A. Full-replay click-to-tail body (macOS) ──────────────────────────────────


def test_tailcmd_body_uses_full_replay(harness):
    r = harness()
    assert r.returncode == 0, r.stderr
    body = harness.tailcmd.read_text()
    assert "tail -n +1 -f" in body, body
    # The bare `tail -f <log>` form must be gone (it would show only last 10).
    assert "exec tail -f " not in body, body
    assert os.access(harness.tailcmd, os.X_OK), "the .command must be executable"



def test_watch_link_always_present_as_fallback(harness):
    # The file:// Watch link is the always-present universal fallback, whether or
    # not auto-open fired.
    r = harness()
    assert r.returncode == 0, r.stderr
    assert "Watch" in r.stderr
    assert f"file://{harness.tailcmd}" in r.stderr


# ── B. Opt-in auto-open (macOS) ────────────────────────────────────────────────


def test_auto_open_default_off(harness):
    # No BUDDHI_TAIL_AUTO_OPEN → open stub NOT invoked.
    r = harness()
    assert r.returncode == 0, r.stderr
    assert _opened(harness) == "", "open must not be invoked when auto-open is OFF"



def test_auto_open_on_invokes_open_with_g_and_path(harness):
    r = harness({"BUDDHI_TAIL_AUTO_OPEN": "1"}, pgrep="nomatch")
    assert r.returncode == 0, r.stderr
    assert _opened(harness) == f"-g {harness.tailcmd}"  # -g = background, no focus steal
    assert "[auto]" in r.stderr



def test_auto_open_truthy_spellings(harness):
    for val in ("true", "yes", "on", "TRUE", "Yes", "ON"):
        harness.open_argv.unlink(missing_ok=True)
        r = harness({"BUDDHI_TAIL_AUTO_OPEN": val}, pgrep="nomatch")
        assert r.returncode == 0, r.stderr
        assert _opened(harness).startswith("-g "), f"{val!r} should be truthy"



def test_auto_open_explicit_falsey_off(harness):
    for val in ("0", "no", "off", "false", "garbage"):
        harness.open_argv.unlink(missing_ok=True)
        r = harness({"BUDDHI_TAIL_AUTO_OPEN": val}, pgrep="nomatch")
        assert r.returncode == 0, r.stderr
        assert _opened(harness) == "", f"{val!r} is not truthy → auto-open OFF"



def test_auto_open_dedupe_pgrep_match(harness):
    # auto-open ON but a live window already exists (pgrep matches) → skip open.
    r = harness({"BUDDHI_TAIL_AUTO_OPEN": "1"}, pgrep="match")
    assert r.returncode == 0, r.stderr
    assert _opened(harness) == "", "a live pgrep match must dedupe the open"
    assert "already open" in r.stderr



def test_batch_suppression_env_hard_disables(harness):
    # The suppression env hard-disables auto-open even with the user's opt-in ON.
    r = harness({"BUDDHI_TAIL_NO_AUTO_OPEN": "1", "BUDDHI_TAIL_AUTO_OPEN": "1"},
                pgrep="nomatch")
    assert r.returncode == 0, r.stderr
    assert _opened(harness) == "", "BUDDHI_TAIL_NO_AUTO_OPEN must suppress auto-open"


# ── Temp-dir knob: BUDDHI_REVIEW_TMP is the plugin's documented override ────────

def test_buddhi_review_tmp_overrides_base_and_handles_spaces(tmp_path):
    """BUDDHI_REVIEW_TMP (what notifier.py keys the answer/escalation files off)
    must also relocate the launcher's log + click-to-tail .command, so all
    buddhi-review temp artifacts stay together. A space in the base path also
    exercises the %q quoting in the .command body and the pgrep-pattern escaping."""
    base = tmp_path / "buddhi tmp"  # the space is intentional
    base.mkdir()
    open_argv = tmp_path / "open_argv.txt"
    open_rec = tmp_path / "open_rec.sh"
    open_rec.write_text(_OPEN_RECORDER.format(out=open_argv))
    open_rec.chmod(0o755)
    pg_nomatch = tmp_path / "pgrep_nomatch.sh"
    pg_nomatch.write_text(_PGREP_NOMATCH)
    pg_nomatch.chmod(0o755)

    stub_bin = tmp_path / "stub_bin"
    stub_bin.mkdir()
    uname_stub = stub_bin / "uname"
    uname_stub.write_text(_UNAME_DARWIN)
    uname_stub.chmod(0o755)

    env = dict(os.environ)
    env["BUDDHI_REVIEW_TMP"] = str(base)
    env.pop("TMPDIR", None)  # prove the override wins authoritatively, not via TMPDIR
    env["BUDDHI_LAUNCH_PYTHON"] = "true"
    env["BUDDHI_OPEN_BIN"] = str(open_rec)
    env["BUDDHI_PGREP_BIN"] = str(pg_nomatch)
    env["BUDDHI_TAIL_AUTO_OPEN"] = "1"
    env.pop("BUDDHI_TAIL_NO_AUTO_OPEN", None)
    env["PATH"] = f"{stub_bin}:{env.get('PATH', '/usr/bin:/bin')}"

    r = subprocess.run(["bash", str(_LAUNCHER), _PR, "--repo", _REPO],
                       capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr

    log = base / tmp_paths.log_name(_PR, _REPO)
    tailcmd = base / tmp_paths.tailcmd_name(_PR, _REPO)
    # The log path on stdout reflects the BUDDHI_REVIEW_TMP base, not /tmp.
    assert _log_line(r.stdout) == str(log)
    assert tailcmd.exists(), "the .command must be written under BUDDHI_REVIEW_TMP"
    assert "tail -n +1 -f" in tailcmd.read_text()
    # The auto-open path handles the space in the base via %q (no crash, right path).
    assert open_argv.read_text().strip() == f"-g {tailcmd}"
