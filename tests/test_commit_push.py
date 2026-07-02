"""Per-round commit/push with the test gate that asks before a red push."""
import subprocess

import pytest

from buddhi_review import commit_push
from buddhi_review.notifier import Ask


class FakeNotifier:
    name = "console"
    def __init__(self):
        self.sent = []
    def startup_log(self):
        pass
    def send(self, ask):
        self.sent.append(ask)
    def read_answer(self, ask):
        return None
    def clear(self, ask):
        pass


def _silent_notice(*a, **k):
    return ""


@pytest.fixture
def repo(tmp_path):
    """A clone with an upstream so `git push` works for real."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
    def git(*args):
        subprocess.run(["git", *args], cwd=work, check=True, capture_output=True)
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (work / "f.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("push", "-q", "-u", "origin", "HEAD")
    return work


def test_resolve_test_command_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -m pytest -q sub/dir")
    assert commit_push.resolve_test_command(str(tmp_path)) == \
        ["python3", "-m", "pytest", "-q", "sub/dir"]
    monkeypatch.delenv("BUDDHI_TEST_COMMAND")
    (tmp_path / "tests").mkdir()
    assert commit_push.resolve_test_command(str(tmp_path)) == \
        ["python3", "-m", "pytest", "tests/", "-q"]


def test_no_suite_skips_loudly(monkeypatch, tmp_path):
    monkeypatch.delenv("BUDDHI_TEST_COMMAND", raising=False)
    notices = []
    def notice(action, detail="", *, status="do", hint=None):
        notices.append((action, status))
        return ""
    status, _ = commit_push.run_test_gate(str(tmp_path), notice=notice)
    assert status == "skipped"
    assert ("test-gate", "skip") in notices  # never silent


def test_commit_and_push_green_gate(monkeypatch, repo):
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -c pass")
    (repo / "f.py").write_text("x = 2\n")
    out = commit_push.commit_and_push(
        str(repo), message="fix: round 1", notice=_silent_notice)
    assert out == "pushed"
    log = subprocess.run(["git", "log", "origin/master..HEAD", "--oneline"],
                         cwd=repo, capture_output=True, text=True)
    main_log = subprocess.run(["git", "log", "origin/main..HEAD", "--oneline"],
                              cwd=repo, capture_output=True, text=True)
    assert (log.returncode == 0 and not log.stdout.strip()) or \
           (main_log.returncode == 0 and not main_log.stdout.strip())  # fully pushed


def test_commit_and_push_nothing_to_do(repo):
    assert commit_push.commit_and_push(
        str(repo), message="m", notice=_silent_notice) == "nothing"


def test_red_gate_escalates_only_never_edits(monkeypatch, repo):
    """On a red gate the skill asks on the console and stops, unless the
    human explicitly answers 1 (push as-is). It never edits or reverts
    your tests."""
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -c import\\ sys;sys.exit(1)")
    (repo / "f.py").write_text("x = 3\n")
    notifier = FakeNotifier()

    out = commit_push.commit_and_push(
        str(repo), message="m", notifier=notifier,
        answer_wait=lambda n, ask: "2", notice=_silent_notice)
    assert out == "stopped"
    assert notifier.sent and isinstance(notifier.sent[0], Ask)
    assert (repo / "f.py").read_text() == "x = 3\n"  # nothing edited, nothing reverted

    out = commit_push.commit_and_push(
        str(repo), message="m", notifier=notifier,
        answer_wait=lambda n, ask: None, notice=_silent_notice)
    assert out == "stopped"  # timeout → stop, never push a red tree silently

    out = commit_push.commit_and_push(
        str(repo), message="m", notifier=notifier,
        answer_wait=lambda n, ask: "1", notice=_silent_notice)
    assert out == "pushed"  # explicit operator bypass


def test_gate_disabled_is_loud(monkeypatch, repo):
    (repo / "f.py").write_text("x = 4\n")
    notices = []
    def notice(action, detail="", *, status="do", hint=None):
        notices.append((action, status))
        return ""
    out = commit_push.commit_and_push(
        str(repo), message="m", test_gate=False, notice=notice)
    assert out == "pushed"
    assert ("test-gate", "skip") in notices


# --------------------------------------------------------------- test-gate timeout

def test_test_gate_timeout_env(monkeypatch):
    monkeypatch.delenv("BUDDHI_TEST_GATE_TIMEOUT_SECS", raising=False)
    assert commit_push._test_gate_timeout() == 600            # default
    monkeypatch.setenv("BUDDHI_TEST_GATE_TIMEOUT_SECS", "120")
    assert commit_push._test_gate_timeout() == 120            # override honoured
    monkeypatch.setenv("BUDDHI_TEST_GATE_TIMEOUT_SECS", "0")
    assert commit_push._test_gate_timeout() == 600            # non-positive → default
    monkeypatch.setenv("BUDDHI_TEST_GATE_TIMEOUT_SECS", "-5")
    assert commit_push._test_gate_timeout() == 600
    monkeypatch.setenv("BUDDHI_TEST_GATE_TIMEOUT_SECS", "garbage")
    assert commit_push._test_gate_timeout() == 600            # unparseable → default


def test_run_test_gate_applies_the_env_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -c pass")
    monkeypatch.setenv("BUDDHI_TEST_GATE_TIMEOUT_SECS", "123")
    seen = {}

    def fake_run(cmd, *, cwd=None, timeout=None):
        seen["timeout"] = timeout
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    status, _ = commit_push.run_test_gate(str(tmp_path), run=fake_run, notice=_silent_notice)
    assert status == "green"
    assert seen["timeout"] == 123


def test_run_test_gate_timeout_is_red(monkeypatch, tmp_path):
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -c pass")

    def fake_run(cmd, *, cwd=None, timeout=None):
        raise subprocess.TimeoutExpired(cmd, timeout)

    status, tail = commit_push.run_test_gate(str(tmp_path), run=fake_run, notice=_silent_notice)
    assert status == "red"
    assert "failed to run" in tail


# --------------------------------------------------------- pre-commit-hook rejection

def test_precommit_hook_rejection_is_diagnosed(repo):
    # A pre-commit hook that rejects the commit → a distinct, actionable notice
    # (not the bare generic error); the return contract is unchanged ("error").
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (repo / "f.py").write_text("x = 9\n")
    notices = []

    def notice(action, detail="", *, status="do", hint=None):
        notices.append((action, detail, status))
        return ""

    out = commit_push.commit_and_push(
        str(repo), message="m", test_gate=False, notice=notice)
    assert out == "error"
    assert any(a == "commit" and "pre-commit hook" in d for a, d, _ in notices)
    # HEAD did not move — the round's fixes are NOT committed.
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo,
                         capture_output=True, text=True)
    assert log.stdout.count("\n") == 1  # only the base commit


def test_normal_commit_failure_not_misreported_as_hook_rejection(monkeypatch, tmp_path):
    # A `git add` failure (not a hook rejection) still returns "error" with NO
    # pre-commit-hook diagnosis.
    notices = []

    def notice(action, detail="", *, status="do", hint=None):
        notices.append((action, detail))
        return ""

    def fake_run(argv, *, cwd=None, timeout=None):
        rc = 1 if argv[:2] == ["git", "add"] else 0
        out = " M f.py\n" if argv[:3] == ["git", "status", "--porcelain"] else ""
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    out = commit_push.commit_and_push(
        str(tmp_path), message="m", run=fake_run, test_gate=False, notice=notice)
    assert out == "error"
    assert not any("pre-commit hook" in d for _, d in notices)
