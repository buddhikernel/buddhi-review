"""A PR's committed ``.claude`` settings cannot run code in ANY of the loop's
``claude`` spawns — proven through the real ``cli._run_loop`` wiring.

``_run_loop`` is driven for real; only its launch gates, notifier and
``RoundDriver`` are replaced by capture stubs. The six ``claude`` seams it wires
(classifier, clean-review detector, quota detector, fix verifier, PR-description
rewriter, and the fixer via ``fix_dispatch``) are then each invoked once. The
``claude`` subprocess and ``gh`` are stubbed (``settings_guard_support.ClaudeStub``
records the directory each child would run in and the settings it would load);
every git call is real. The base-ref resolver ``_run_loop`` installs is the real
one: ``gh`` answers ``baseRefName``, ``origin`` is a GitHub URL, and a real
``git fetch`` fails offline in milliseconds.
"""
import contextlib
import io
import os
import subprocess
from contextlib import redirect_stdout

import pytest

from buddhi_review import claude_settings_guard, cli, merge
from buddhi_review.classify import Classification
from buddhi_review.loop import Comment, CommentResult
from settings_guard_support import (
    LOCAL_SETTINGS,
    OFFLINE_GIT,
    ROOT_LOCAL_SETTINGS,
    SETTINGS,
    ClaudeStub,
    command_hook,
    git,
    hostile,
    make_pr_repo,
    markers_present,
    seen_settings,
    touch,
    write,
)

SEAMS = (
    "classifier (classify_runner)",
    "clean detector (clean_llm)",
    "quota detector (quota_llm)",
    "fixer verifier (verify_runner)",
    "PR-description rewriter (rewrite_runner)",
    "fixer (default_fixer_runner via fix_dispatch)",
)


class _Outcome:
    status, rounds, merged = "clean", 1, False


def _drive(monkeypatch, repo, *, detached: bool, stub: ClaudeStub = None):
    """Run the real ``_run_loop`` against ``repo`` and invoke all six seams it
    wired. Returns ``(spawns_by_seam, stub, stdout)``."""
    stub = stub or ClaudeStub(real_run=subprocess.run)
    for k, v in OFFLINE_GIT.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(subprocess, "run", stub)
    captured = {}

    class _Driver:
        def __init__(self, *a, **kw):
            captured.update(kw)

        def run(self):
            return _Outcome()

    real_dispatch = cli.default_fix_dispatch

    def _dispatch(**kw):
        captured["dispatch"] = kw
        return real_dispatch(**kw)

    monkeypatch.setattr(cli.round_driver, "RoundDriver", _Driver)
    monkeypatch.setattr(cli, "default_fix_dispatch", _dispatch)
    monkeypatch.setattr(cli.round_driver, "refuse_primary_checkout", lambda *a, **k: None)
    monkeypatch.setattr(cli.round_driver, "enforce_repo_confirmation_gate", lambda *a, **k: None)
    monkeypatch.setattr(cli, "ConsoleNotifier",
                        lambda *a, **k: type("N", (), {"startup_log": lambda self: None})())
    monkeypatch.setattr(cli.upsell, "maybe_emit_run_end_nudge", lambda *a, **k: None)
    monkeypatch.chdir(repo.primary if detached else repo.wt)
    args = cli.build_parser().parse_args(
        ["run-loop", "7", "--repo", "o/r", "--max-rounds", "3", "--cwd", str(repo.wt)])
    out = io.StringIO()
    with redirect_stdout(out):
        assert cli._run_loop(args) == 0

    comment = Comment(id="c1", text="this null check is missing", path="src/app.py")
    result = CommentResult(
        comment_id="c1", kernel_status="decided", disposition="fix",
        classification=Classification(label="SUBSTANTIVE", model="sonnet", effort="low",
                                      reason="a real defect"),
    )
    calls = (
        lambda: captured["classify_runner"]("classify this comment"),
        lambda: captured["clean_llm"]("is this review clean?"),
        lambda: captured["quota_llm"]("is this a quota notice?"),
        lambda: captured["dispatch"]["verify_runner"]("verify this fix"),
        lambda: captured["dispatch"]["rewrite_runner"]("rewrite this body"),
        lambda: captured["fix_dispatch"](comment, result),
    )
    spawns = {}
    stub.claude_stdout = "{}"
    for seam, call in zip(SEAMS, calls):
        if seam.startswith("fixer ("):
            stub.claude_stdout = "SKIP: nothing to change"
        before = len(stub.spawns)
        with redirect_stdout(out):
            call()
        assert len(stub.spawns) == before + 1, f"{seam}: expected exactly one claude spawn"
        spawns[seam] = stub.spawns[-1]
    return spawns, stub, out.getvalue()


def _table(title: str, spawns, repo) -> str:
    """§0's before-state format, paths shortened to <probe>/…"""
    def short(p):
        return None if p is None else p.replace(os.path.realpath(str(repo.primary.parent)), "<probe>")
    lines = [title]
    for seam, s in spawns.items():
        line = (f"{seam:46} cwd_kwarg={short(s.cwd_kwarg)!s:12} effective={short(s.effective):14} "
                f"settings={bool(s.raw)} hooks={s.hooks}")
        if s.sandboxed:
            line += " sandbox=True"
        lines.append(line)
    lines.append(f"hook markers present: {markers_present(repo.markers)}")
    return "\n".join(lines)


def _hostile_repo(tmp_path):
    markers = tmp_path / "markers"
    repo = make_pr_repo(tmp_path, {}, {SETTINGS: hostile(markers)})
    assert not markers_present(repo.markers)
    return repo


# ── A1 / A2: the §0 exploit, after and with the guard cut out ──────────────────────

@pytest.mark.parametrize("detached", [False, True], ids=["in-checkout", "detached"])
def test_no_seam_sees_a_pr_hook_and_all_run_in_the_checkout(monkeypatch, tmp_path, detached):
    """A1: all six children see no hooks and run in the checkout; afterwards the
    file is byte-identical, git status is empty, and the index flags are unchanged."""
    repo = _hostile_repo(tmp_path)
    before, flags = repo.settings_bytes(), repo.flags()
    spawns, _, _ = _drive(monkeypatch, repo, detached=detached)
    print(_table("detached launch" if detached else "in-checkout launch", spawns, repo))
    wt = os.path.realpath(str(repo.wt))
    wrong = [f"{seam}: ran in {s.effective}, given cwd={s.cwd_kwarg}, saw hooks {s.hooks}"
             for seam, s in spawns.items()
             if s.effective != wt or s.cwd_kwarg is None
             or os.path.realpath(s.cwd_kwarg) != wt or s.hooks]
    assert wrong == []
    assert markers_present(repo.markers) == []
    assert repo.settings_bytes() == before
    assert repo.status() == ""
    assert repo.flags() == flags


@pytest.mark.parametrize("detached", [False, True], ids=["in-checkout", "detached"])
def test_without_the_guard_every_seam_sees_the_pr_hooks(monkeypatch, tmp_path, detached):
    """A2: the same drive with the guard cut out reproduces §0 on all six seams —
    so A1 is not vacuous."""
    @contextlib.contextmanager
    def _unguarded(cwd):
        yield

    monkeypatch.setattr(claude_settings_guard, "window", _unguarded)
    repo = _hostile_repo(tmp_path)
    spawns, _, _ = _drive(monkeypatch, repo, detached=detached)
    print(_table(("detached" if detached else "in-checkout") + " launch, guard removed",
                 spawns, repo))
    unexposed = [seam for seam, s in spawns.items() if s.hooks != ["PreToolUse", "SessionStart"]]
    assert unexposed == []


def test_another_pr_checked_out_in_the_primary_never_reaches_a_worktree_spawn(monkeypatch, tmp_path):
    """Claude Code also reads ``settings.local.json`` from the canonical git root —
    for a linked worktree, the PRIMARY checkout, which the guard never touches. The
    primary here has another PR checked out whose commit tracks a hostile
    ``settings.local.json``. No spawn loads it: every spawn loads user and project
    settings only."""
    repo = make_pr_repo(tmp_path, {}, {"src/app.py": "x = 2\n"})
    git(repo.primary, "checkout", "-q", "-b", "pr-8")
    write(repo.primary, LOCAL_SETTINGS, hostile(repo.markers))
    git(repo.primary, "add", "-f", LOCAL_SETTINGS)
    git(repo.primary, "commit", "-qm", "pr 8")
    # the exposure is real: a spawn loading every source would pick it up
    assert seen_settings(str(repo.wt), ["claude"])[ROOT_LOCAL_SETTINGS]["hooks"]
    spawns, _, _ = _drive(monkeypatch, repo, detached=True)
    exposed = [seam for seam, s in spawns.items()
               if s.hooks or s.argv[s.argv.index("--setting-sources") + 1] != "user,project"]
    assert exposed == []
    assert markers_present(repo.markers) == []


# ── A3 / A4: a legitimate committed settings file keeps its base-trusted keys ─────

def _legit(command: str) -> dict:
    return {
        "model": "sonnet",
        "permissions": {"allow": ["Bash(npm run lint)", "Bash(npm run test:*)"],
                        "deny": ["Read(./secrets/**)"]},
        "hooks": command_hook("PostToolUse", command, "Edit|Write"),
    }


def _check_py(markers) -> str:
    return f"import pathlib\npathlib.Path({str(markers / 'HOOK_RAN_check')!r}).touch()\n"


def test_legit_repo_keeps_every_key_live_and_is_never_rewritten(monkeypatch, tmp_path):
    """A3: base and head carry the same settings (a project-dir hook, permissions,
    model); the PR changes an unrelated file. The fetch fails, yet the merge-base
    resolves from the existing refs/remotes/origin/main; every key is live in all
    six spawns, and the file is never rewritten."""
    markers = tmp_path / "markers"
    settings = _legit('python3 "$CLAUDE_PROJECT_DIR"/tools/check.py')
    repo = make_pr_repo(tmp_path, {SETTINGS: settings, "tools/check.py": _check_py(markers),
                                   "src/app.py": "x = 1\n"},
                        {"src/app.py": "x = 2\n"})
    path = repo.wt / SETTINGS
    before, st = path.read_bytes(), os.stat(path)
    spawns, stub, _ = _drive(monkeypatch, repo, detached=False)
    assert stub.fetches and all(f.returncode == 128 for f in stub.fetches)
    assert "transport 'https' not allowed" in stub.fetches[0].stderr
    for seam, s in spawns.items():
        assert s.settings[SETTINGS] == settings, f"{seam}: {s.settings}"
        assert s.raw[SETTINGS] == before, seam
    after = os.stat(path)
    assert (after.st_ino, after.st_mtime_ns) == (st.st_ino, st.st_mtime_ns)
    assert path.read_bytes() == before and repo.status() == ""
    state = os.environ[claude_settings_guard.STATE_DIR_ENV]
    assert not any(n.endswith(".json") for n in (os.listdir(state) if os.path.isdir(state) else []))
    assert markers_present(repo.markers) == []


# Every spelling of "this file in the checkout" C3(b) names.
SPELLINGS = (
    'python3 "$CLAUDE_PROJECT_DIR"/tools/check.py',
    'python3 "${CLAUDE_PROJECT_DIR}"/tools/check.py',
    "python3 ${CLAUDE_PROJECT_DIR}/tools/check.py",
    "python3 $CLAUDE_PROJECT_DIR/tools/check.py",
    'python3 "$CLAUDE_PROJECT_DIR/tools/check.py"',
    "python3 ./tools/check.py",
)


@pytest.mark.parametrize("rewritten", [False, True], ids=["unchanged", "rewritten"])
@pytest.mark.parametrize("command", SPELLINGS)
def test_a_hook_naming_a_pr_rewritten_file_is_stripped(monkeypatch, tmp_path, command, rewritten):
    """A4: as A3, but the PR rewrites tools/check.py — the hook is stripped in every
    spawn while the other keys stay live. The unchanged case pins each spelling to
    the file itself: a spelling resolved to nothing, or to the whole checkout,
    would lose the hook even when the file is untouched."""
    markers = tmp_path / "markers"
    settings = _legit(command)
    head = ({"tools/check.py": _check_py(markers) + "print('rewritten by the PR')\n"}
            if rewritten else {"src/app.py": "x = 2\n"})
    repo = make_pr_repo(tmp_path, {SETTINGS: settings, "tools/check.py": _check_py(markers),
                                   "src/app.py": "x = 1\n"}, head)
    before = repo.settings_bytes()
    spawns, _, _ = _drive(monkeypatch, repo, detached=False)
    for seam, s in spawns.items():
        live = s.settings[SETTINGS]
        assert live["model"] == "sonnet" and live["permissions"] == settings["permissions"], seam
        if rewritten:
            assert "hooks" not in live, f"{seam} kept a hook naming a PR-rewritten file"
        else:
            assert live["hooks"] == settings["hooks"], f"{seam} lost a base-identical hook"
    assert repo.settings_bytes() == before and repo.status() == ""
    assert markers_present(repo.markers) == []


# ── A9: the resolver never mistakes a PR branch named origin/main for the base ────

def test_resolver_ignores_a_pr_branch_named_like_the_base(monkeypatch, tmp_path):
    """A9: the PR branch is named ``origin/main`` (so ``refs/heads/origin/main``
    exists, and git prefers it for the short name). The resolver still returns the
    real merge-base, and a PR-changed hook is stripped."""
    markers = tmp_path / "markers"
    settings = _legit('python3 "$CLAUDE_PROJECT_DIR"/tools/check.py')
    repo = make_pr_repo(tmp_path, {SETTINGS: settings, "tools/check.py": _check_py(markers)},
                        {"tools/check.py": "print('changed by the PR')\n"}, branch="origin/main")
    assert git(repo.wt, "rev-parse", "origin/main").strip() == repo.head  # the trap is armed
    stub = ClaudeStub(real_run=subprocess.run)
    for k, v in OFFLINE_GIT.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(subprocess, "run", stub)
    resolver = merge.PullRequestBase("7", "o/r")
    assert resolver(str(repo.wt)) == repo.base
    claude_settings_guard.install_base_resolver(resolver)
    with claude_settings_guard.window(str(repo.wt)):
        view = stub(["claude", "-p", "x"], cwd=str(repo.wt), text=True)
    assert view.returncode == 0
    live = stub.spawns[-1].settings[SETTINGS]
    assert "hooks" not in live and live["model"] == "sonnet"


def test_resolver_never_falls_back_to_a_pr_branch_named_like_the_tracking_ref(monkeypatch, tmp_path):
    """With ``refs/remotes/origin/main`` absent, git's name lookup would resolve
    that name to ``refs/heads/refs/remotes/origin/main`` — a branch the PR can
    create. The resolver reads the tracking ref exactly, so there is no base."""
    repo = make_pr_repo(tmp_path, {}, {}, branch="refs/remotes/origin/main")
    git(repo.primary, "update-ref", "-d", "refs/remotes/origin/main")
    assert git(repo.wt, "rev-parse", "refs/remotes/origin/main").strip() == repo.head
    stub = ClaudeStub(real_run=subprocess.run)
    for k, v in OFFLINE_GIT.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(subprocess, "run", stub)
    resolver = merge.PullRequestBase("7", "o/r")
    assert resolver(str(repo.wt)) is None
    assert resolver.last_error == "refs/remotes/origin/main does not exist"


def test_resolver_matches_the_remote_on_the_prs_own_host(monkeypatch, tmp_path):
    """``--repo o/r`` names no host; a mirror of ``o/r`` on another host is listed
    first. The PR's own URL carries the host, so the GitHub remote is the one used."""
    repo = make_pr_repo(tmp_path, {}, {})
    git(repo.primary, "remote", "rename", "origin", "upstream")
    git(repo.primary, "remote", "add", "a-mirror", "https://gitea.example.net/o/r.git")
    git(repo.primary, "update-ref", "refs/remotes/a-mirror/main", repo.head)
    assert git(repo.wt, "remote", "-v").splitlines()[0].startswith("a-mirror")
    stub = ClaudeStub(real_run=subprocess.run)
    for k, v in OFFLINE_GIT.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(subprocess, "run", stub)
    assert merge.PullRequestBase("7", "o/r")(str(repo.wt)) == repo.base


@pytest.mark.parametrize("url", ["git@github-work:o/r.git", "ssh://git@ssh.github.com:443/o/r.git"])
def test_resolver_accepts_a_remote_behind_an_ssh_alias(monkeypatch, tmp_path, url):
    """``--repo o/r`` names no host, so a remote reached through an SSH host alias
    (several GitHub accounts) or ssh.github.com:443 still matches."""
    repo = make_pr_repo(tmp_path, {}, {})
    git(repo.primary, "remote", "set-url", "origin", url)
    stub = ClaudeStub(real_run=subprocess.run)
    for k, v in OFFLINE_GIT.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(subprocess, "run", stub)
    assert merge.PullRequestBase("7", "o/r")(str(repo.wt)) == repo.base


def test_resolver_caches_asks_the_pr_repo_and_fetches_only_the_base(monkeypatch, tmp_path):
    repo = make_pr_repo(tmp_path, {}, {})
    stub = ClaudeStub(real_run=subprocess.run)
    for k, v in OFFLINE_GIT.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(subprocess, "run", stub)
    resolver = merge.PullRequestBase("7", "o/r")
    assert resolver(str(repo.wt)) == repo.base and resolver(str(repo.wt)) == repo.base
    assert stub.gh_calls == [["gh", "pr", "view", "7", "--json", "baseRefName,url", "-R", "o/r"]]
    assert len(stub.fetches) == 1
    assert stub.fetches[0].args[-2:] == ["origin", "+refs/heads/main:refs/remotes/origin/main"]


def test_resolver_refuses_to_guess_between_hosts(monkeypatch, tmp_path):
    repo = make_pr_repo(tmp_path, {}, {})
    git(repo.primary, "remote", "set-url", "origin", "git@alias-one:o/r.git")
    git(repo.primary, "remote", "add", "mirror", "https://gitea.example.net/o/r.git")
    stub = ClaudeStub(real_run=subprocess.run)
    monkeypatch.setattr(subprocess, "run", stub)
    resolver = merge.PullRequestBase("7", "o/r")
    assert resolver(str(repo.wt)) is None
    assert resolver.last_error == "several git remotes on different hosts point at o/r"


def test_resolver_takes_the_repo_from_the_pr_url_when_none_is_given(monkeypatch, tmp_path):
    repo = make_pr_repo(tmp_path, {}, {})
    stub = ClaudeStub(real_run=subprocess.run)
    for k, v in OFFLINE_GIT.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(subprocess, "run", stub)
    assert merge.PullRequestBase("7", None)(str(repo.wt)) == repo.base
    assert stub.gh_calls[-1] == ["gh", "pr", "view", "7", "--json", "baseRefName,url"]


def test_resolver_fails_closed_without_a_matching_remote_and_backs_off(monkeypatch, tmp_path):
    repo = make_pr_repo(tmp_path, {}, {})
    stub = ClaudeStub(real_run=subprocess.run,
                      pr_view={"baseRefName": "main", "url": "https://github.com/x/other/pull/7"})
    monkeypatch.setattr(subprocess, "run", stub)
    now = [1000.0]
    resolver = merge.PullRequestBase("7", None, clock=lambda: now[0])
    assert resolver(str(repo.wt)) is None
    assert "no configured git remote points at https://github.com/x/other" in resolver.last_error
    assert resolver(str(repo.wt)) is None and len(stub.gh_calls) == 1  # backing off: no call
    now[0] += 31
    assert resolver(str(repo.wt)) is None and len(stub.gh_calls) == 2  # retried on schedule
    now[0] += 31
    assert resolver(str(repo.wt)) is None and len(stub.gh_calls) == 2  # next delay is 60 s


def test_resolver_needs_the_fully_qualified_tracking_ref(monkeypatch, tmp_path):
    """Only an unresolved merge-base fails: no ``refs/remotes/origin/main`` (and a
    fetch that cannot make one) means no base, never a local ``main``."""
    repo = make_pr_repo(tmp_path, {}, {})
    git(repo.primary, "update-ref", "-d", "refs/remotes/origin/main")
    stub = ClaudeStub(real_run=subprocess.run)
    for k, v in OFFLINE_GIT.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(subprocess, "run", stub)
    resolver = merge.PullRequestBase("7", "o/r")
    assert resolver(str(repo.wt)) is None
    assert resolver.last_error == "refs/remotes/origin/main does not exist"


# ── A16: a hook naming the checkout root is stripped, even on an empty PR ─────────

def _root_forms(markers):
    t = touch(markers, "root")
    return {
        "bare-project-dir": f"cd $CLAUDE_PROJECT_DIR && {t}",
        "braced-project-dir": f'cd "${{CLAUDE_PROJECT_DIR}}" && {t}',
        "dot": f"cd . && {t}",
        "dot-slash": f"cd ./ && {t}",
        "root-glob": f'for f in "$CLAUDE_PROJECT_DIR"/*.sh; do :; done; {t}',
    }


@pytest.mark.parametrize("form", ["bare-project-dir", "braced-project-dir", "dot",
                                  "dot-slash", "root-glob"])
def test_a_hook_naming_the_checkout_root_is_stripped_on_an_empty_pr(monkeypatch, tmp_path, form):
    """A16: base and head carry the identical hook, the PR's only commit is empty
    and nothing untracked is added — the tree equals base, so only the root rule
    can strip it. The hook is stripped in all six spawns while the other keys stay
    live, the fixer still runs, and the checkout is left exactly as found."""
    markers = tmp_path / "markers"
    settings = {
        "model": "sonnet",
        "permissions": {"allow": ["Bash(npm run lint)"]},
        "hooks": command_hook("PreToolUse", _root_forms(markers)[form], "Bash"),
    }
    repo = make_pr_repo(tmp_path, {SETTINGS: settings}, {})
    assert git(repo.wt, "diff", "--name-only", repo.base, "HEAD") == ""
    before, flags = repo.settings_bytes(), repo.flags()
    spawns, stub, out = _drive(monkeypatch, repo, detached=False)
    for seam, s in spawns.items():
        live = s.settings[SETTINGS]
        assert "hooks" not in live, f"{seam} kept a hook naming the checkout root"
        assert live["model"] == "sonnet" and live["permissions"] == settings["permissions"], seam
    assert spawns[SEAMS[5]].argv  # the fixer's claude spawn happened: nothing refused
    assert "could not be made safe" not in out
    assert repo.settings_bytes() == before
    assert repo.status() == ""
    assert repo.flags() == flags
    assert markers_present(repo.markers) == []
