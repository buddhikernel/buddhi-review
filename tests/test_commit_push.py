"""Per-round commit/push with the test gate that asks before a red push."""
import subprocess
import textwrap

import pytest

from buddhi_review import commit_push, wizard
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


# ── The executor: shell-syntax detection + the argv split ──────────────────────
#
# The `shlex`-vs-`&&` bug: `shlex.split("npm ci && npm test")` yields a literal
# "&&" token that execvp hands to npm as an argument, so the suite never runs and
# the gate is vacuously green/red on the wrong thing. Any command carrying shell
# syntax must run via `bash -lc`; a bare command stays a plain argv (scopable).


class TestSplitTestCommand:
    """`_split_test_command` — the single rule turning a configured command STRING
    into the argv the gate executes. Unit-level, no config/env involved."""

    @pytest.mark.parametrize("command", [
        "npm ci && npm test",          # &&
        "a || b",                      # ||
        "go test ./... | tee out",     # pipe
        "a; b",                        # ;
        "CI=true npm test",            # leading VAR=val prefix
        "cd frontend && vitest run",   # cd step
    ])
    def test_shell_syntax_forces_bash_lc(self, command):
        assert commit_push._split_test_command(command) == ["bash", "-lc", command]

    @pytest.mark.parametrize("command", [
        "jest > report.txt",           # stdout redirect
        "pytest 2> err.log",           # stderr redirect
        "pytest --workers=$WORKERS",   # env-var expansion
        "pytest $(ls tests)",          # command substitution
        "pytest `ls tests`",           # backtick substitution
        "setup & pytest",              # background &
        "(cd sub && pytest)",          # subshell grouping
        "python <<EOF",                # heredoc
        "pytest\nsecond line",         # newline
    ])
    def test_extended_shell_syntax_forces_bash_lc(self, command):
        # Beyond &&/||/|/; — redirection, expansion, subshell, background, heredoc,
        # and a newline are all shell syntax execvp can't honour, so they run via
        # bash -lc, never a shlex.split that hands the raw metacharacter to the
        # runner (the same bug class the && fix addresses, just a wider operator
        # set). This set must stay identical to the reference implementation's.
        assert commit_push._split_test_command(command) == ["bash", "-lc", command]

    @pytest.mark.parametrize("command,argv", [
        ("mocha test/**/*.spec.js", ["mocha", "test/**/*.spec.js"]),  # glob left literal
        ("pytest tests/test_a.py", ["pytest", "tests/test_a.py"]),
        ("go test ./...", ["go", "test", "./..."]),
    ])
    def test_glob_and_plain_commands_stay_bare(self, command, argv):
        # Glob chars are NOT shell-escalated: test runners expand their own path
        # globs, so the literal pattern is passed straight through (execvp).
        assert commit_push._split_test_command(command) == argv

    @pytest.mark.parametrize("command,argv", [
        ("go test ./...", ["go", "test", "./..."]),        # `/` is not a shell op
        ("npx vitest run", ["npx", "vitest", "run"]),
        ("pytest 'tests/a b.py'", ["pytest", "tests/a b.py"]),  # shlex honours quoting
    ])
    def test_bare_commands_split_directly(self, command, argv):
        assert commit_push._split_test_command(command) == argv

    def test_malformed_quote_never_raises(self):
        # An unbalanced quote makes shlex.split raise ValueError; the resolver must
        # NOT let it escape (the gate's never-raises contract). It falls back to
        # bash -lc, so the malformed command becomes a clean red gate, not a crash.
        cmd = commit_push._split_test_command("pytest 'unbalanced")  # must not raise
        assert cmd == ["bash", "-lc", "pytest 'unbalanced"]

    @pytest.mark.parametrize("command", ["", "   "])
    def test_blank_is_not_shell_and_splits_empty(self, command):
        assert commit_push._command_needs_shell(command) is False
        assert commit_push._split_test_command(command) == []


class TestResolveTestCommand:
    """The resolver: env → per-repo → global → auto-detect, with a shell-operator
    command wrapped as `bash -lc` and a bare command split to argv."""

    def test_env_shell_operator_runs_via_bash_lc(self, monkeypatch, tmp_path):
        # The real shlex-vs-&& executor bug: a `&&` command MUST NOT be shlex-split
        # (that yields a literal "&&" arg); it runs through `bash -lc`.
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "npm ci && npm test")
        cmd = commit_push.resolve_test_command(str(tmp_path))
        assert cmd == ["bash", "-lc", "npm ci && npm test"]
        assert "&&" not in cmd[:-1]                        # never a bare "&&" argv token

    def test_env_bare_command_splits_to_argv(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "npx vitest run")
        assert commit_push.resolve_test_command(str(tmp_path)) == ["npx", "vitest", "run"]

    def test_blank_env_falls_through_to_autodetect(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "   ")
        (tmp_path / "tests").mkdir()
        assert commit_push.resolve_test_command(str(tmp_path)) == \
            ["python3", "-m", "pytest", "tests/", "-q"]

    def test_per_repo_then_global_resolution(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(textwrap.dedent("""\
            test_command: "go test ./..."
            repos:
              acme/webapp:
                test_command: "npx vitest run"
        """))
        monkeypatch.setenv("BUDDHI_CONFIG", str(cfg))
        # per-repo wins for the configured repo
        assert commit_push.resolve_test_command(str(tmp_path), "acme/webapp") == \
            ["npx", "vitest", "run"]
        # a repo with no per-repo entry inherits the global
        assert commit_push.resolve_test_command(str(tmp_path), "acme/other") == \
            ["go", "test", "./..."]
        # repo=None reads the global default (not the auto-detect default)
        assert commit_push.resolve_test_command(str(tmp_path)) == ["go", "test", "./..."]

    def test_env_wins_over_per_repo_and_global(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(textwrap.dedent("""\
            test_command: "go test ./..."
            repos:
              acme/webapp:
                test_command: "npx vitest run"
        """))
        monkeypatch.setenv("BUDDHI_CONFIG", str(cfg))
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "make test")
        assert commit_push.resolve_test_command(str(tmp_path), "acme/webapp") == \
            ["make", "test"]

    def test_blank_per_repo_falls_through_to_global(self, tmp_path, monkeypatch):
        # A blank per-repo value must NOT shadow the global (unlike label_gated_ci,
        # whose mere key presence shadows) — a config predating the key is unchanged.
        cfg = tmp_path / "config.yaml"
        cfg.write_text(textwrap.dedent("""\
            test_command: "go test ./..."
            repos:
              acme/webapp:
                test_command: "   "
        """))
        monkeypatch.setenv("BUDDHI_CONFIG", str(cfg))
        assert commit_push.resolve_test_command(str(tmp_path), "acme/webapp") == \
            ["go", "test", "./..."]

    def test_per_repo_shell_command_runs_via_bash_lc(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(textwrap.dedent("""\
            repos:
              acme/webapp:
                test_command: "npm ci && npm test"
        """))
        monkeypatch.setenv("BUDDHI_CONFIG", str(cfg))
        assert commit_push.resolve_test_command(str(tmp_path), "acme/webapp") == \
            ["bash", "-lc", "npm ci && npm test"]

    def test_configured_command_beats_autodetect(self, tmp_path, monkeypatch):
        # A `tests/` dir is present, but an explicit command still wins — otherwise a
        # polyglot repo that happens to carry tests/ silently runs pytest.
        (tmp_path / "tests").mkdir()
        cfg = tmp_path / "config.yaml"
        cfg.write_text('test_command: "npx vitest run"\n')
        monkeypatch.setenv("BUDDHI_CONFIG", str(cfg))
        assert commit_push.resolve_test_command(str(tmp_path)) == ["npx", "vitest", "run"]

    def test_unconfigured_no_tests_dir_still_returns_none(self, tmp_path):
        # The documented no-`tests/`-dir → no-gate skip posture is PRESERVED: an
        # unconfigured repo with no tests/ dir yields None (the caller's loud skip),
        # exactly as before this seam existed.
        assert commit_push.resolve_test_command(str(tmp_path)) is None

    def test_detected_ci_command_with_operator_reaches_bash_lc(self, tmp_path, monkeypatch):
        # End-to-end regression for the ORIGINAL bug: the wizard's own detector emits
        # "npm ci && npm test" for a package-lock'd Node repo; persisted as the
        # per-repo test_command it must reach the gate as a `bash -lc` argv, never a
        # shlex.split that hands npm a literal "&&".
        (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
        (tmp_path / "package-lock.json").write_text("{}")
        detected = wizard._detect_ci_command(str(tmp_path))
        assert detected == "npm ci && npm test"

        cfg = tmp_path / "config.yaml"
        cfg.write_text(f'repos:\n  acme/webapp:\n    test_command: "{detected}"\n')
        monkeypatch.setenv("BUDDHI_CONFIG", str(cfg))
        assert commit_push.resolve_test_command(str(tmp_path), "acme/webapp") == \
            ["bash", "-lc", "npm ci && npm test"]


class TestGateConsumesResolvedCommand:
    """`run_test_gate` executes the RESOLVED argv — the shell wrap survives all the
    way to the subprocess call, and the per-repo scope reaches the resolver."""

    def _capture_run(self, captured, rc=0):
        def run(argv, *, cwd=None, timeout=None):
            captured.append(list(argv))
            return subprocess.CompletedProcess(list(argv), rc, "", "")
        return run

    def test_shell_operator_command_runs_via_bash_lc(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "npm ci && npm test")
        cmds = []
        status, _ = commit_push.run_test_gate(
            str(tmp_path), run=self._capture_run(cmds), notice=_silent_notice)
        assert status == "green"
        assert cmds == [["bash", "-lc", "npm ci && npm test"]]

    def test_bare_command_runs_as_plain_argv(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "npx vitest run")
        cmds = []
        commit_push.run_test_gate(str(tmp_path), run=self._capture_run(cmds),
                                  notice=_silent_notice)
        assert cmds == [["npx", "vitest", "run"]]

    def test_per_repo_command_reaches_the_gate(self, monkeypatch, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text('repos:\n  acme/webapp:\n    test_command: "npx vitest run"\n')
        monkeypatch.setenv("BUDDHI_CONFIG", str(cfg))
        cmds = []
        commit_push.run_test_gate(str(tmp_path), repo="acme/webapp",
                                  run=self._capture_run(cmds), notice=_silent_notice)
        assert cmds == [["npx", "vitest", "run"]]

    def test_nonzero_exit_reds_the_gate(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "npx vitest run")
        status, _ = commit_push.run_test_gate(
            str(tmp_path), run=self._capture_run([], rc=1), notice=_silent_notice)
        assert status == "red"

    def test_malformed_command_reds_the_gate_never_raises(self, monkeypatch, tmp_path):
        # An unbalanced quote resolves to `bash -lc <malformed>`; bash exits nonzero
        # → a clean RED gate. It must never raise out of run_test_gate.
        monkeypatch.setenv("BUDDHI_TEST_COMMAND", "pytest 'unbalanced")
        cmds = []
        status, _ = commit_push.run_test_gate(
            str(tmp_path), run=self._capture_run(cmds, rc=2), notice=_silent_notice)
        assert status == "red"
        assert cmds == [["bash", "-lc", "pytest 'unbalanced"]]


def test_commit_and_push_threads_repo_to_the_gate(monkeypatch, repo):
    """The per-repo command is useless if the caller never passes the repo through:
    `commit_and_push(repo=…)` must scope the gate's resolution.

    MUTATION: drop `repo=repo` from commit_and_push's run_test_gate call and this
    fails (the per-repo vitest command would never resolve).
    """
    cfg = repo / "config.yaml"
    cfg.write_text('repos:\n  acme/webapp:\n    test_command: "npx vitest run"\n')
    monkeypatch.setenv("BUDDHI_CONFIG", str(cfg))
    seen = []
    real_run = commit_push._default_run

    def run(argv, *, cwd=None, timeout=commit_push._GIT_TIMEOUT):
        if list(argv[:1]) not in (["git"],):
            seen.append(list(argv))
            return subprocess.CompletedProcess(list(argv), 0, "", "")
        return real_run(argv, cwd=cwd, timeout=timeout)

    (repo / "f.py").write_text("x = 3\n")
    out = commit_push.commit_and_push(str(repo), message="m", repo="acme/webapp",
                                      run=run, notice=_silent_notice)
    assert out == "pushed"
    assert seen == [["npx", "vitest", "run"]]


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


def test_commit_and_push_droppings_only_is_nothing(monkeypatch, repo):
    """A round that only left an editor/backup dropping (e.g. a failed fixer's
    `foo.bak`, no real edit) must NOT masquerade as landed progress: `_stage_all`
    excludes the dropping, nothing is staged/committed, and HEAD already matches
    upstream — so the push below would ship nothing new. That's "nothing", not
    "pushed" (which would trick RoundDriver into re-summoning another review
    round over a no-op)."""
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -c pass")
    (repo / "foo.bak").write_text("stray")
    out = commit_push.commit_and_push(
        str(repo), message="m", notice=_silent_notice)
    assert out == "nothing"
    assert (repo / "foo.bak").exists()  # left alone, never swept into a commit


def test_commit_and_push_rerun_gate_still_pushes_when_already_committed(monkeypatch, repo):
    """The final commit block's ``git diff --cached --quiet`` is ALSO clean on the
    legitimate "I've fixed it" re-run path (answer 3 commits the operator's pending
    edits mid-gate-loop — see the block above) — that must still push and report
    "pushed", not get caught by the new droppings-only no-op guard: HEAD is ahead
    of upstream from the loop's own commit, so `_push_is_noop` correctly says no."""
    marker = repo / "gate_ok"
    monkeypatch.setenv(
        "BUDDHI_TEST_COMMAND",
        "python3 -c \"import os,sys; sys.exit(0 if os.path.exists('gate_ok') else 1)\"",
    )
    (repo / "f.py").write_text("x = 9\n")
    notifier = FakeNotifier()
    calls = []

    def answer_wait(n, ask):
        calls.append(ask)
        marker.write_text("ok")  # operator "fixed it" — flips the gate green
        return "3"

    out = commit_push.commit_and_push(
        str(repo), message="m", notifier=notifier,
        answer_wait=answer_wait, notice=_silent_notice)
    assert out == "pushed"
    assert len(calls) == 1
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    upstream = subprocess.run(["git", "rev-parse", "@{u}"], cwd=repo,
                              capture_output=True, text=True).stdout.strip()
    assert head == upstream  # actually shipped, not left dangling


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
