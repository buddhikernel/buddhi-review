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
