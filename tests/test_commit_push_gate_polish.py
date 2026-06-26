"""Commit/push gate polish + escalation.

Covers the four behaviours in commit_push.py / merge.py:
  - the post-commit+push clean-tree (fix-residue) tripwire (best-effort).
  - blank-line phase breaks at the gate / commit / push / squash-merge
    transitions.
  - NO_COLOR-aware pytest-tail formatting in the red-gate panel
    (blank line before each pytest section, captured lines unaltered,
    ~200-line cap).
  - the "I've fixed it — re-run the gate & continue" option
    (commit pending → re-run FULL gate → push only if green; the gate is
    the sole arbiter, and it never auto-edits or reverts a test).
"""
import subprocess

import pytest

from buddhi_review import commit_push, merge
from buddhi_review.notifier import Ask


def _silent(*a, **k):
    return ""


def _capturing_notice():
    notices = []

    def notice(action, detail="", *, status="do", hint=None, stream=None):
        notices.append((action, detail, status, hint))
        return f"[auto] {action}"

    return notices, notice


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


@pytest.fixture
def repo(tmp_path):
    """A clone with an upstream so `git push` works for real (mirrors the
    test_commit_push.py fixture)."""
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


def _unpushed(repo):
    """Local commits not yet on the upstream (proves a red tree was NOT pushed)."""
    for base in ("origin/master", "origin/main"):
        r = subprocess.run(["git", "log", f"{base}..HEAD", "--oneline"],
                           cwd=repo, capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip()
    return ""


# ---------------------------------------------------------------------------
# pytest-tail formatting (blank before sections, cap, unaltered, NO_COLOR)
# ---------------------------------------------------------------------------

def test_section_regex_matches_pytest_rules_only():
    re_ = commit_push._PYTEST_SECTION_RE
    assert re_.match("=== FAILURES ===")
    assert re_.match("___ test_x ___")
    assert re_.match("----------- Captured stdout call -----------")
    assert re_.match("!!! Interrupted: 1 error during collection !!!")
    # Prose / rule-less footers must NOT match.
    assert not re_.match("- foo -")
    assert not re_.match("-- Docs: https://docs.pytest.org --")
    assert not re_.match("E   assert 1 == 2")


def test_format_pytest_tail_inserts_blank_before_each_section():
    tail = "\n".join([
        "tests/test_a.py F",
        "=== FAILURES ===",
        "___ test_a ___",
        "E   assert 1 == 2",
        "=== short test summary info ===",
        "FAILED tests/test_a.py::test_a",
    ])
    out = commit_push.format_pytest_tail(tail)
    # A blank separator precedes every section rule that follows a non-blank line.
    assert out == [
        "tests/test_a.py F",
        "",
        "=== FAILURES ===",
        "",
        "___ test_a ___",
        "E   assert 1 == 2",
        "",
        "=== short test summary info ===",
        "FAILED tests/test_a.py::test_a",
    ]


def test_format_pytest_tail_never_opens_with_a_blank():
    out = commit_push.format_pytest_tail("=== FAILURES ===\nE   boom")
    assert out[0] == "=== FAILURES ==="  # leading section rule keeps no blank before it


def test_format_pytest_tail_caps_real_lines_only():
    tail = "\n".join(f"line {i}" for i in range(500))
    out = commit_push.format_pytest_tail(tail, limit=10)
    # No section rules here, so output is exactly the last 10 captured lines.
    assert out == [f"line {i}" for i in range(490, 500)]
    # The cap counts REAL lines: inserted blanks never push real content out.
    tail2 = "a\n=== S1 ===\nb\n=== S2 ===\nc"
    capped = commit_push.format_pytest_tail(tail2, limit=5)
    assert [ln for ln in capped if ln] == ["a", "=== S1 ===", "b", "=== S2 ===", "c"]


def test_format_pytest_tail_empty_is_placeholder_and_lines_unaltered():
    assert commit_push.format_pytest_tail("") == ["(no output captured)"]
    assert commit_push.format_pytest_tail(None) == ["(no output captured)"]
    # Captured lines are reproduced byte-for-byte (whitespace/indent preserved).
    out = commit_push.format_pytest_tail("    indented body\n\ttab body")
    assert "    indented body" in out and "\ttab body" in out


def test_colour_enabled_honours_no_color_envs(monkeypatch):
    class TTY:
        def isatty(self):
            return True

    class NotTTY:
        def isatty(self):
            return False

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("BUDDHI_LOOP_NO_COLOR", raising=False)
    assert commit_push._colour_enabled(TTY()) is True
    assert commit_push._colour_enabled(NotTTY()) is False  # piped/captured → no colour
    monkeypatch.setenv("NO_COLOR", "1")
    assert commit_push._colour_enabled(TTY()) is False
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("BUDDHI_LOOP_NO_COLOR", "1")
    assert commit_push._colour_enabled(TTY()) is False


def test_red_gate_panel_strips_colour_under_no_color(monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    commit_push._print_red_gate_panel(["=== FAILURES ===", "E   boom"])
    out = capsys.readouterr().out
    assert "\033[" not in out                      # no ANSI escapes
    assert "[local-tests] ✗ test gate RED" in out  # the glyph + text still print
    assert "E   boom" in out


# ---------------------------------------------------------------------------
# clean-tree (fix-residue) tripwire — best-effort, never fails the loop
# ---------------------------------------------------------------------------

def test_tripwire_fires_loud_notice_on_residue():
    notices, notice = _capturing_notice()

    def run(argv, *, cwd=None, timeout=None):
        return subprocess.CompletedProcess(argv, 0, stdout=" M leftover.py\n?? new.py\n")

    commit_push._assert_clean_after_commit("/w", run=run, notice=notice)
    assert len(notices) == 1
    action, detail, status, hint = notices[0]
    assert action == "fix-residue tripwire" and status == "fallback"
    assert hint == "clean-tree tripwire"
    assert "leftover.py" in detail and "NOT on the PR" in detail


def test_tripwire_silent_on_clean_tree():
    notices, notice = _capturing_notice()

    def run(argv, *, cwd=None, timeout=None):
        return subprocess.CompletedProcess(argv, 0, stdout="")

    commit_push._assert_clean_after_commit("/w", run=run, notice=notice)
    assert notices == []


def test_tripwire_is_best_effort_swallows_errors():
    notices, notice = _capturing_notice()

    def boom(argv, *, cwd=None, timeout=None):
        raise RuntimeError("git exploded")

    # An exception must not propagate and must not emit a notice.
    commit_push._assert_clean_after_commit("/w", run=boom, notice=notice)
    assert notices == []

    def nonzero(argv, *, cwd=None, timeout=None):
        return subprocess.CompletedProcess(argv, 1, stdout=" M x\n", stderr="boom")

    commit_push._assert_clean_after_commit("/w", run=nonzero, notice=notice)
    assert notices == []  # a failed status check is ignored, never reported as residue

    # No cwd → no-op.
    commit_push._assert_clean_after_commit(None, run=boom, notice=notice)
    assert notices == []


def test_tripwire_fires_end_to_end_after_push():
    """commit_and_push runs the tripwire after a successful push; residue in the
    post-push status check surfaces the fallback notice."""
    notices, notice = _capturing_notice()
    seen = {"status": 0}

    def run(argv, *, cwd=None, timeout=None):
        if argv[:3] == ["git", "status", "--porcelain"]:
            seen["status"] += 1
            # 1st status = pre-commit dirty check; 2nd = the post-push tripwire.
            out = " M f.py\n" if seen["status"] == 1 else " M stray.py\n"
            return subprocess.CompletedProcess(argv, 0, stdout=out)
        return subprocess.CompletedProcess(argv, 0, stdout="")

    out = commit_push.commit_and_push("/w", message="m", run=run,
                                      test_gate=False, notice=notice)
    assert out == "pushed"
    assert ("fix-residue tripwire" in [n[0] for n in notices])


# ---------------------------------------------------------------------------
# blank-line phase breaks
# ---------------------------------------------------------------------------

def test_phase_breaks_bracket_gate_commit_push(capsys, monkeypatch):
    monkeypatch.delenv("BUDDHI_TEST_COMMAND", raising=False)

    def run(argv, *, cwd=None, timeout=None):
        dirty = argv[:3] == ["git", "status", "--porcelain"]
        return subprocess.CompletedProcess(argv, 0, stdout=" M x\n" if dirty else "")

    out = commit_push.commit_and_push("/w", message="m", run=run,
                                      test_gate=True, notice=_silent)
    assert out == "pushed"
    captured = capsys.readouterr().out
    # One blank line at each of the gate / commit / push transitions.
    assert captured.split("\n").count("") >= 3


def test_gate_announces_local_tests_header(capsys, monkeypatch):
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -c pass")

    def run(argv, *, cwd=None, timeout=None):
        if argv[:3] == ["python3", "-c", "pass"]:
            return subprocess.CompletedProcess(argv, 0, stdout="")  # green
        dirty = argv[:3] == ["git", "status", "--porcelain"]
        return subprocess.CompletedProcess(argv, 0, stdout=" M x\n" if dirty else "")

    out = commit_push.commit_and_push("/w", message="m", run=run,
                                      test_gate=True, notice=_silent)
    assert out == "pushed"
    assert "[local-tests] running python3 -c pass before push …" in capsys.readouterr().out


def test_merge_enabled_emits_phase_break_and_keeps_do_done(capsys):
    notices, notice = _capturing_notice()

    def run(argv, *, cwd=None):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    assert merge.squash_merge("9", enabled=True, run=run, notice=notice) is True
    assert capsys.readouterr().out == "\n"          # exactly the phase-break blank line
    assert [n[2] for n in notices] == ["do", "done"]  # auto-action trail unchanged


def test_merge_disabled_emits_no_phase_break(capsys):
    def boom(argv, *, cwd=None):
        raise AssertionError("gh ran while merge disabled")

    assert merge.squash_merge("9", enabled=False, run=boom, notice=_silent) is False
    assert capsys.readouterr().out == ""  # the skip path prints no landing-phase break


# ---------------------------------------------------------------------------
# the "I've fixed it — re-run the gate & continue" option
# ---------------------------------------------------------------------------

def test_red_gate_offers_the_three_escalation_options(monkeypatch):
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "false")
    notifier = FakeNotifier()

    def run(argv, *, cwd=None, timeout=None):
        dirty = argv[:3] == ["git", "status", "--porcelain"]
        if argv and argv[0] == "false":  # the (red) gate command
            return subprocess.CompletedProcess(argv, 1, stdout="=== FAILURES ===\nE boom")
        return subprocess.CompletedProcess(argv, 0, stdout=" M x\n" if dirty else "")

    out = commit_push.commit_and_push(
        "/w", message="m", run=run, notifier=notifier,
        answer_wait=lambda n, ask: "2", notice=_silent)
    assert out == "stopped"
    ask = notifier.sent[0]
    assert isinstance(ask, Ask)
    assert ask.options == [
        "Push as-is (bypass the gate this round)",
        "Stop the run",
        "I've fixed it — re-run the gate & continue",
    ]
    assert ask.recommended_index == 1  # stop stays the safe default
    # The red-gate panel detail carries the formatted (section-separated) tail.
    assert "=== FAILURES ===" in ask.detail


def test_ive_fixed_it_reruns_gate_then_pushes_when_green(repo, tmp_path, monkeypatch):
    """Answer 3: commit pending edits, re-run the FULL gate, push when green."""
    gate = tmp_path / "gate.py"
    gate.write_text("import os, sys\nsys.exit(0 if os.path.exists('FIXED') else 1)\n")
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", f"python3 {gate}")
    (repo / "f.py").write_text("x = 99\n")  # the round's fix
    notifier = FakeNotifier()

    def operator_fixes_it(n, ask):
        # Simulate the human editing the worktree at the host to fix the failure.
        (repo / "FIXED").write_text("ok\n")
        return "3"

    out = commit_push.commit_and_push(
        str(repo), message="fix: round 1", notifier=notifier,
        answer_wait=operator_fixes_it, notice=_silent)

    assert out == "pushed"
    assert notifier.sent and notifier.sent[0].options[-1].startswith("I've fixed it")
    # The operator's fix is committed AND pushed (tree is in sync with origin).
    assert _unpushed(repo) == ""
    tracked = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True).stdout
    assert "FIXED" in tracked


def test_ive_fixed_it_when_operator_self_commits_still_pushes(repo, tmp_path, monkeypatch):
    """Regression: the operator may commit their own host-side fix BEFORE answering
    3 (the prompt literally says they edited the worktree at the host). Option 3's
    own commit is then a no-op, and the gate goes green — the now-clean, committed
    tree must still be pushed, never misreported as an error."""
    gate = tmp_path / "gate.py"
    gate.write_text("import os, sys\nsys.exit(0 if os.path.exists('FIXED') else 1)\n")
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", f"python3 {gate}")
    (repo / "f.py").write_text("x = 99\n")  # the round's fix, still uncommitted

    def operator_commits_and_answers(n, ask):
        # The human fixes it AND commits the whole tree themselves at the host.
        (repo / "FIXED").write_text("ok\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "operator manual"], cwd=repo,
                       check=True, capture_output=True)
        return "3"

    out = commit_push.commit_and_push(
        str(repo), message="m", notifier=FakeNotifier(),
        answer_wait=operator_commits_and_answers, notice=_silent)

    assert out == "pushed"          # NOT "error" — the green commit is shipped
    assert _unpushed(repo) == ""    # the operator's commit reached origin
    tracked = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True).stdout
    assert "FIXED" in tracked


def test_ive_fixed_it_never_pushes_a_red_tree(repo, tmp_path, monkeypatch):
    """Answer 3 when the fix does NOT make the gate green → re-escalate; the
    operator then stops. The red tree is committed locally but NEVER pushed."""
    gate = tmp_path / "gate.py"
    gate.write_text("import os, sys\nsys.exit(0 if os.path.exists('FIXED') else 1)\n")
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", f"python3 {gate}")
    (repo / "f.py").write_text("x = 99\n")
    notifier = FakeNotifier()
    answers = iter(["3", "2"])  # "I've fixed it" (but it's still red) → then stop

    out = commit_push.commit_and_push(
        str(repo), message="m", notifier=notifier,
        answer_wait=lambda n, ask: next(answers), notice=_silent)

    assert out == "stopped"
    assert len(notifier.sent) == 2          # re-escalated once with the fresh state
    assert _unpushed(repo) != ""            # a local commit exists but was NOT pushed


def test_ive_fixed_it_rerun_limit_hands_over(repo, monkeypatch):
    """A non-converging stream of answer-3 hits the re-run cap and stops, so a
    non-interactive answer source can never spin the gate forever."""
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -c \"import sys;sys.exit(1)\"")
    monkeypatch.setenv("BUDDHI_TEST_FAILURE_RERUNS", "1")
    (repo / "f.py").write_text("x = 99\n")
    notifier = FakeNotifier()

    out = commit_push.commit_and_push(
        str(repo), message="m", notifier=notifier,
        answer_wait=lambda n, ask: "3", notice=_silent)
    assert out == "stopped"
    assert _unpushed(repo) != ""  # never pushed a red tree


def test_existing_push_as_is_and_stop_answers_preserved(repo, monkeypatch):
    """The new option is appended, so 1 = push-as-is and 2 = stop still hold."""
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -c \"import sys;sys.exit(1)\"")
    (repo / "f.py").write_text("x = 2\n")
    notifier = FakeNotifier()
    assert commit_push.commit_and_push(
        str(repo), message="m", notifier=notifier,
        answer_wait=lambda n, ask: "1", notice=_silent) == "pushed"

    (repo / "f.py").write_text("x = 3\n")
    assert commit_push.commit_and_push(
        str(repo), message="m", notifier=notifier,
        answer_wait=lambda n, ask: "2", notice=_silent) == "stopped"
