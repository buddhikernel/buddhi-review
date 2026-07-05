"""Tests for buddhi_review.git_guardrail_hook — the PreToolUse guard that blocks
the agent from hand-running history-rewriting git commands (rebase / merge /
reset --hard / cherry-pick / force-push) while leaving every safe command and the
sanctioned helper scripts untouched. See git_guardrail_hook.py for the rationale.

Includes a real subprocess harness that drives ``python3 -m
buddhi_review.git_guardrail_hook`` exactly the way the SKILL.md ``hooks:``
frontmatter does, so the wired invocation is proven end-to-end.
"""
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from buddhi_review import git_guardrail_hook as g
from buddhi_review import session_worktrees as sw

_PUBLIC = Path(__file__).resolve().parent.parent
_SKILLS_DIR = _PUBLIC / "buddhi_review" / "skills"
_SKILLS = ("review-pr", "open-pr")


# ── decide(): commands that MUST be blocked ──────────────────────────────────
BLOCK = [
    "git rebase main",
    "git rebase -i HEAD~3",
    "git rebase origin/main",
    "git merge origin/main",
    "git merge --no-ff feature",
    "git reset --hard HEAD",
    "git reset --hard origin/main",
    "git reset --keep HEAD~1",
    "git cherry-pick abc123",
    "git push --force origin x",
    "git push -f origin x",
    "git push --force-with-lease origin x",
    "git push --force-with-lease=main origin x",
    "git push --mirror",
    "git push origin +main",
    "git -C /some/path rebase main",
    "git -C My\\ Folder rebase main",        # escaped space in path must not bypass
    "git -c user.name=x merge y",
    "git -c user.name=My\\ Name rebase main",  # escaped space in value must not bypass
    "FOO=1 git merge x",
    "/usr/bin/git rebase main",
    "git --config-env var=VAL rebase main",  # --config-env consumes next token
    "git.exe rebase main",                  # Windows .exe suffix
    "git add -A && git rebase main",      # second command in a chain
    "git status; git reset --hard",       # second command after ';'
    "git rebase main;",                   # trailing semicolon without space
    "git status;git reset --hard",        # no space around semicolon
    "git status&&git reset --hard",       # no space around &&
    "(git rebase main)",                  # parenthesized command
    "git push -vf origin x",              # combined short option -vf
    "git push -fv origin x",              # combined short option -fv
    "git add .\ngit rebase main",         # multi-line command
    "git \\\n  rebase \\\n  main",        # line continuation
    "if true; then git rebase main; fi",  # shell keyword 'then'
    "{ git rebase main; }",               # shell grouping
    "sudo git rebase main",               # command prefix 'sudo'
    "env git rebase main",                # command prefix 'env'
    "time git rebase main",               # command prefix 'time'
    "nohup git rebase main",              # command prefix 'nohup'
    "sudo -u root git rebase main",       # prefix + arg-taking flag bypass
    "sudo -u root git push --force",      # prefix + arg-taking flag, force-push
    "env -i git rebase main",             # prefix + flag-only bypass
    "sudo --user root git rebase main",   # long-option prefix + value bypass
    "env --unset FOO git rebase main",    # long-option prefix + value bypass
    "env -C /tmp git rebase main",        # env -C consumes next token; git must still be caught
    "env -C . git push --force",          # same via force-push path
    "time -p git rebase main",            # time's -p is POSIX flag, not value-taking
    "command -p git rebase main",         # command's -p is path flag, not value-taking
    "time -p git push --force",           # same bypass via force-push path
    "git reset --har HEAD",               # prefix bypass for --hard
    "git reset --ke HEAD~1",              # prefix bypass for --keep
    "git push --force-with-le origin x",  # prefix bypass for --force-with-lease
    "git push --mir",                     # prefix bypass for --mirror
]


# ── decide(): commands that MUST be allowed ──────────────────────────────────
ALLOW = [
    "git status",
    "git log --oneline -5",
    "git diff HEAD~1",
    "git show HEAD",
    "git add .",
    "git commit -m 'wip: git rebase notes'",   # 'git rebase' is inside the message
    "git commit -m 'wip\ngit rebase notes'",   # multi-line commit message
    "git push origin my-branch",
    "git push --set-upstream origin x",
    "git fetch origin main",
    "git rebase --abort",                       # recovery
    "git rebase --help",
    "git rebase -h",
    "git merge --abort",
    "git merge --help",
    "git merge -h",
    "git cherry-pick --abort",
    "git cherry-pick --help",
    "git cherry-pick -h",
    "git reset HEAD~1",                          # mixed (no --hard)
    "git reset --mixed HEAD~1",
    "git reset --soft HEAD~1",
    "git checkout -b feature",
    "git switch main",
    "git worktree add .claude/worktrees/x -b b origin/main",
    "git revert abc123",                        # new commit, not a rewrite
    "git stash",
    "python3 -m buddhi_review open-pr --title t --body b",
    "bash launch-review.sh 42 --repo o/r",
    "echo git rebase main",                     # 'git' is an arg to echo, not a command
    "grep -r 'git merge' .",
]


@pytest.mark.parametrize("cmd", BLOCK)
def test_blocked(cmd):
    blocked, reason = g.decide(cmd)
    assert blocked is True, f"expected BLOCK: {cmd!r}"
    assert reason and "BUDDHI_ALLOW_MANUAL_GIT=1" in reason


@pytest.mark.parametrize("cmd", ALLOW)
def test_allowed(cmd):
    blocked, _ = g.decide(cmd)
    assert blocked is False, f"expected ALLOW: {cmd!r}"


def test_override_token_bypasses():
    blocked, _ = g.decide("BUDDHI_ALLOW_MANUAL_GIT=1 git rebase main")
    assert blocked is False


def test_override_token_mid_command_does_not_bypass():
    # Token as an arg/earlier command must NOT disable the guard for a later git rebase.
    blocked, _ = g.decide("echo BUDDHI_ALLOW_MANUAL_GIT=1; git rebase main")
    assert blocked is True


def test_empty_command_allowed():
    assert g.decide("") == (False, "")


def test_unbalanced_quotes_does_not_crash():
    # Tokenizer falls back to a whitespace split; must still catch the rebase.
    blocked, _ = g.decide('git rebase "main')
    assert blocked is True
    blocked, _ = g.decide('git status; git rebase "main')
    assert blocked is True


# ── main(): stdin → stdout permission decision (monkeypatched I/O) ────────────
def _run_main(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    rc = g.main()
    return rc, out.getvalue()


def test_main_denies_blocked_bash(monkeypatch):
    rc, out = _run_main(monkeypatch, {
        "tool_name": "Bash",
        "tool_input": {"command": "git rebase main"},
    })
    assert rc == 0
    payload = json.loads(out)
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "git rebase" in hso["permissionDecisionReason"]


def test_main_allows_safe_bash(monkeypatch):
    rc, out = _run_main(monkeypatch, {
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m x"},
    })
    assert rc == 0
    assert out == ""   # no output → no decision → allowed


def test_main_ignores_non_bash_tool(monkeypatch):
    rc, out = _run_main(monkeypatch, {
        "tool_name": "Edit",
        "tool_input": {"file_path": "x", "old_string": "git rebase", "new_string": "y"},
    })
    assert rc == 0
    assert out == ""


def test_main_fails_open_on_bad_json(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert g.main() == 0
    assert out.getvalue() == ""


def test_main_fails_open_on_malformed_tool_input(monkeypatch):
    rc, out = _run_main(monkeypatch, {
        "tool_name": "Bash",
        "tool_input": "not a dict",
    })
    assert rc == 0
    assert out == ""


def test_main_fails_open_on_non_object_json(monkeypatch):
    # Valid JSON but not an object (a bare list) → fail OPEN, no crash.
    monkeypatch.setattr("sys.stdin", io.StringIO("[1, 2, 3]"))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert g.main() == 0
    assert out.getvalue() == ""


# ── the SKILL.md frontmatter must wire the hook, or the guard ships dead ──────
def _frontmatter(skill_name):
    text = (_SKILLS_DIR / skill_name / "SKILL.md").read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) >= 3:
        return yaml.safe_load(parts[1])
    raise ValueError(f"No frontmatter found in {skill_name}/SKILL.md")


@pytest.mark.parametrize("skill", _SKILLS)
def test_skill_frontmatter_wires_the_hook(skill):
    fm = _frontmatter(skill)
    pre = (fm.get("hooks") or {}).get("PreToolUse")
    assert isinstance(pre, list) and pre, \
        f"{skill}: SKILL.md must declare a PreToolUse hook"
    cmds = [h.get("command", "") for entry in pre
            if entry.get("matcher") == "Bash"
            for h in (entry.get("hooks") or [])
            if h.get("type") == "command"]
    assert any("buddhi_review.git_guardrail_hook" in c for c in cmds), \
        f"{skill}: PreToolUse(Bash) hook does not invoke buddhi_review.git_guardrail_hook"


# ── real subprocess harness: drive the wired invocation end-to-end ───────────
def _invoke_hook(command, *, extra=None):
    """Run the hook exactly as the SKILL.md ``hooks:`` frontmatter does —
    ``python3 -m buddhi_review.git_guardrail_hook`` — feeding the PreToolUse JSON
    on stdin and returning (returncode, stdout)."""
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    if extra:
        payload.update(extra)
    return _invoke_hook_raw(json.dumps(payload))


def _invoke_hook_raw(stdin_text):
    proc = subprocess.run(
        [sys.executable, "-m", "buddhi_review.git_guardrail_hook"],
        input=stdin_text, capture_output=True, text=True, cwd=str(_PUBLIC),
    )
    return proc.returncode, proc.stdout


@pytest.mark.parametrize("cmd", [
    "git rebase main", "git merge origin/main", "git reset --hard HEAD",
    "git cherry-pick abc123", "git push --force origin x",
])
def test_subprocess_blocks_history_rewrites(cmd):
    rc, out = _invoke_hook(cmd)
    assert rc == 0
    hso = json.loads(out)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "BUDDHI_ALLOW_MANUAL_GIT=1" in hso["permissionDecisionReason"]


@pytest.mark.parametrize("cmd", [
    "git add .", "git commit -m x", "git push origin my-branch",
    "git rebase --abort", "git merge --abort", "git cherry-pick --skip",
])
def test_subprocess_allows_safe_and_recovery(cmd):
    rc, out = _invoke_hook(cmd)
    assert rc == 0 and out == ""   # no deny block emitted → allowed


def test_subprocess_honors_override():
    rc, out = _invoke_hook("BUDDHI_ALLOW_MANUAL_GIT=1 git rebase main")
    assert rc == 0 and out == ""


def test_subprocess_ignores_non_bash_tool():
    rc, out = _invoke_hook_raw(json.dumps({
        "tool_name": "Edit", "tool_input": {"file_path": "x"}}))
    assert rc == 0 and out == ""


def test_subprocess_fails_open_on_bad_json():
    rc, out = _invoke_hook_raw("{not json at all")
    assert rc == 0 and out == ""


# ── the hook must stay decoupled from the kernel-importing package chain ──────
def test_package_import_does_not_pull_kernel_chain():
    """``python3 -m buddhi_review.git_guardrail_hook`` imports the package root
    first; that must NOT pull in the classify/transparency chain (which imports
    the buddhi kernel). Otherwise a kernel-absent / interpreter-mismatch install
    would crash the PreToolUse hook with a traceback on every Bash call instead
    of failing open. Run in a fresh interpreter so sys.modules starts clean."""
    code = (
        "import sys; import buddhi_review; "
        "import buddhi_review.git_guardrail_hook; "
        "leaked = [m for m in "
        "('buddhi_review.classify','buddhi_review.transparency','buddhi.policy') "
        "if m in sys.modules]; "
        "print(','.join(leaked))"
    )
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, cwd=str(_PUBLIC))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", \
        f"package import pulled the kernel chain: {proc.stdout.strip()!r}"


def test_lazy_public_reexports_still_resolve():
    """The PEP 562 lazy re-exports must keep the public package API working —
    ``from buddhi_review import Classification`` etc. resolve on access."""
    import buddhi_review
    assert buddhi_review.Classification is not None
    assert callable(buddhi_review.classify_comment)
    assert callable(buddhi_review.parse_classification)
    assert callable(buddhi_review.automation_notice)
    with pytest.raises(AttributeError):
        buddhi_review.does_not_exist


# ── session → worktree capture (the open-pr / review-pr auto-target fix) ───────
@pytest.fixture
def _isolate_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDHI_SESSION_WORKTREES_PATH",
                       str(tmp_path / "session-worktrees.json"))
    return tmp_path


def _feed(monkeypatch, command, *, session_id="sess-x", cwd="/spawn/dir"):
    return _run_main(monkeypatch, {
        "session_id": session_id, "cwd": cwd,
        "tool_name": "Bash", "tool_input": {"command": command},
    })


def test_capture_on_worktree_add_with_dash_c_and_branch(monkeypatch, _isolate_registry):
    # The standing flow: git -C <repo> worktree add .claude/worktrees/<slug> -b <branch> <base>
    _feed(monkeypatch, "git -C /Users/me/repo worktree add "
          ".claude/worktrees/nifty -b fix/x origin/main", session_id="sA")
    assert sw.lookup("sA") == "/Users/me/repo/.claude/worktrees/nifty"


def test_capture_relative_add_resolves_against_shell_cwd(monkeypatch, _isolate_registry):
    _feed(monkeypatch, "git worktree add .claude/worktrees/zoo",
          session_id="sB", cwd="/Users/me/repo2")
    assert sw.lookup("sB") == "/Users/me/repo2/.claude/worktrees/zoo"


def test_capture_leading_cd_resolves_worktree_add_against_the_cd_target(
        monkeypatch, _isolate_registry):
    # cd-resolution: a leading `cd X && git worktree add <rel>` in ONE command
    # resolves <rel> against X — the dir the git actually runs in — not the fixed
    # payload cwd. Without this the recorded path is a phantom under the spawn
    # checkout and the resolver falls back to $PWD. (A CREATION target is not
    # disk-checked, so the correct path is recorded even before it exists.)
    _feed(monkeypatch,
          "cd /Users/me/other-repo && git worktree add .claude/worktrees/cdw -b b main",
          session_id="sCd", cwd="/spawn/dir")
    assert sw.lookup("sCd") == "/Users/me/other-repo/.claude/worktrees/cdw"


def test_capture_subshell_cd_isolates_directory_change(monkeypatch, _isolate_registry):
    # Subshell isolation: a cd inside a subshell (X) must not affect a command
    # that runs outside the subshell.
    _feed(monkeypatch,
          "(cd /Users/me/other-repo && git worktree add .claude/worktrees/sub -b b main)"
          " && git worktree add .claude/worktrees/outer -b b main",
          session_id="sSub", cwd="/spawn/dir")
    assert sw.lookup("sSub") == "/spawn/dir/.claude/worktrees/outer"


def test_capture_separate_call_add_keeps_payload_cwd(monkeypatch, _isolate_registry):
    # Honest bound: cd-resolution is single-command-scoped. A bare `git worktree
    # add <rel>` in a SEPARATE call (no `cd` in its string) still resolves against
    # the payload cwd; the resolver's live-worktree filter is the safety net for
    # the phantom that a cross-repo prior `cd` in an earlier call can produce.
    _feed(monkeypatch, "git worktree add .claude/worktrees/sep -b b main",
          session_id="sSep", cwd="/spawn/dir")
    assert sw.lookup("sSep") == "/spawn/dir/.claude/worktrees/sep"


def test_capture_on_git_dash_c_into_existing_worktree(
        monkeypatch, _isolate_registry, tmp_path):
    # Operating on a worktree created in a prior session is still captured — but a
    # `-C` OPERATION targets a LIVE checkout, so the worktree must actually exist on
    # disk. (A non-existent -C path is a phantom mis-resolution and is dropped; see
    # the phantom-guard tests below.)
    wt = tmp_path / "repo" / ".claude" / "worktrees" / "bar"
    wt.mkdir(parents=True)
    _feed(monkeypatch, f"git -C {wt} status", session_id="sC")
    assert sw.lookup("sC") == str(wt)


def test_no_capture_for_primary_checkout_dash_c(monkeypatch, _isolate_registry):
    # A -C into the PRIMARY checkout (not under .claude/worktrees) is NOT a
    # task-scoped worktree → no registration.
    _feed(monkeypatch, "git -C /Users/me/repo status", session_id="sD")
    assert sw.lookup("sD") is None


def test_no_capture_without_session_id(monkeypatch, _isolate_registry):
    _run_main(monkeypatch, {
        "cwd": "/Users/me/repo", "tool_name": "Bash",
        "tool_input": {"command": "git worktree add .claude/worktrees/x"},
    })
    # Nothing to key on → nothing recorded (and no crash).
    assert sw.lookup("") is None


def test_no_capture_for_unrelated_command(monkeypatch, _isolate_registry):
    _feed(monkeypatch, "git status && ls -la", session_id="sE")
    assert sw.lookup("sE") is None


def test_no_capture_for_off_tree_worktree_add(monkeypatch, _isolate_registry):
    # Defense-in-depth: a `worktree add` whose target is NOT under
    # .claude/worktrees (off-convention) is not recorded.
    _feed(monkeypatch, "git -C /Users/me/repo worktree add /tmp/sibling -b b main",
          session_id="sOff")
    assert sw.lookup("sOff") is None


def test_capture_picks_worktree_add_in_a_chain(monkeypatch, _isolate_registry):
    # `git fetch && git worktree add …` — the add target is captured, not fetch.
    _feed(monkeypatch, "git -C /r fetch origin && git -C /r worktree add "
          ".claude/worktrees/chained -b b main", session_id="sF")
    assert sw.lookup("sF") == "/r/.claude/worktrees/chained"


def test_capture_never_changes_the_deny_decision(
        monkeypatch, _isolate_registry, tmp_path):
    # A blocked command that ALSO names a (live) worktree path still denies (capture
    # is a pure side-effect that runs after the decision).
    wt = tmp_path / "repo" / ".claude" / "worktrees" / "wt"
    wt.mkdir(parents=True)
    rc, out = _feed(monkeypatch, f"git -C {wt} reset --hard HEAD~1", session_id="sG")
    assert rc == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    # …and the (existing) worktree was still captured.
    assert sw.lookup("sG") == str(wt)


def test_capture_failure_never_breaks_the_hook(monkeypatch, _isolate_registry):
    # If the registry write blows up, the hook still returns cleanly (fail-open).
    monkeypatch.setattr(sw, "register",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rc, out = _feed(monkeypatch, "git worktree add .claude/worktrees/x",
                    session_id="sH")
    assert rc == 0 and out == ""   # allowed, no crash


# ── phantom-worktree guard (cross-repo mis-registration) ──────────────────────
def test_no_capture_for_dash_c_relative_phantom(monkeypatch, _isolate_registry):
    # The failure this guards: after the agent `cd`s into another repo, a RELATIVE
    # `git -C .claude/worktrees/X` is resolved against the fixed session cwd (the
    # repo it cd'd AWAY from), yielding a directory that does not exist. A `-C`
    # OPERATION targets a live checkout, so a non-existent candidate is dropped —
    # never recorded as a phantom the resolver would later mis-target.
    _feed(monkeypatch, "git -C .claude/worktrees/ghost status",
          session_id="sPh", cwd="/no/such/spawn/repo")
    assert sw.lookup("sPh") is None


def test_no_capture_for_dash_c_absolute_removed_worktree(
        monkeypatch, _isolate_registry, tmp_path):
    # An absolute -C into a worktree that no longer exists on disk (removed) is
    # likewise a phantom for the resolver → dropped, not recorded.
    gone = tmp_path / "repo" / ".claude" / "worktrees" / "removed"  # never created
    _feed(monkeypatch, f"git -C {gone} status", session_id="sGone")
    assert sw.lookup("sGone") is None


def test_worktree_add_target_recorded_before_it_exists(monkeypatch, _isolate_registry):
    # Timing exception: a `worktree add` TARGET is about to be created and does NOT
    # exist yet at PreToolUse time, so the disk-existence phantom guard (which
    # applies ONLY to `-C` OPERATIONS) must NOT drop it — creation is still
    # recorded. A phantom CREATION is dropped later by the resolver's live-worktree
    # filter, not here.
    _feed(monkeypatch, "git -C /Users/me/repo worktree add "
          ".claude/worktrees/fresh -b b main", session_id="sFresh")
    assert sw.lookup("sFresh") == "/Users/me/repo/.claude/worktrees/fresh"


def test_capture_malformed_payload_never_raises(monkeypatch, _isolate_registry):
    # A payload whose tool_input is not a dict must neither raise nor record.
    rc, out = _run_main(monkeypatch, {
        "session_id": "sBad", "cwd": "/spawn/dir",
        "tool_name": "Bash", "tool_input": "not a dict",
    })
    assert rc == 0 and out == ""
    assert sw.lookup("sBad") is None
