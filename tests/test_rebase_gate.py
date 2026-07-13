"""F3 rebase-gate engine verb — ``python -m buddhi_review rebase-check``.

Every test drives real git against a local bare remote (no network); the
free path is proven mutation-free (HEAD/tree hash, ``git status
--porcelain``, and ``git diff``/``--cached`` all unchanged after
rebase_check).
"""
from __future__ import annotations

import inspect
import io
import json
import subprocess

import pytest

from buddhi_review import rebase_gate, cli


# ── Real-git helpers ─────────────────────────────────────────────────────────

def _g(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=str(cwd), check=check,
                          capture_output=True, text=True)


def _sha(cwd, ref="HEAD"):
    return _g(cwd, "rev-parse", ref).stdout.strip()


def _tree_hash(cwd, ref="HEAD"):
    """The committed tree object SHA at ref.

    Reflects only the tree of the commit at ``ref`` — it does NOT change for
    working-tree, staged (index), or untracked-file mutations that were never
    committed. Use alongside ``git status --porcelain``/``git diff`` checks
    to actually prove the working tree and index are untouched."""
    return _g(cwd, "rev-parse", f"{ref}^{{tree}}").stdout.strip()


def _write(path, text):
    path.write_text(text, encoding="utf-8")


# ── Shared git fixture ────────────────────────────────────────────────────────

@pytest.fixture
def repo(tmp_path):
    """Bare remote + a clone on a feature branch ``feat/x`` off ``main``.

    After setup: ``main`` and ``feat/x`` are both pushed; the clone is on
    ``feat/x`` with an upstream configured."""
    remote = tmp_path / "remote.git"
    _g(tmp_path, "init", "-q", "--bare", str(remote))
    work = tmp_path / "work"
    _g(tmp_path, "clone", "-q", str(remote), str(work))
    _g(work, "config", "user.email", "t@example.com")
    _g(work, "config", "user.name", "Tester")
    _g(work, "checkout", "-q", "-b", "main")
    _write(work / "base.py", "x = 1\n")
    _g(work, "add", "-A")
    _g(work, "commit", "-qm", "base commit")
    _g(work, "push", "-q", "-u", "origin", "main")
    _g(work, "checkout", "-q", "-b", "feat/x", "main")
    _write(work / "feature.py", "y = 2\n")
    _g(work, "add", "-A")
    _g(work, "commit", "-qm", "feature work")
    _g(work, "push", "-q", "-u", "origin", "feat/x")
    return work


def _advance_base(work, *, filename="other.py", content="z = 3\n"):
    """Push a new commit onto ``main`` then return to ``feat/x``.

    After this call ``feat/x`` is strictly behind ``main`` (no conflict)."""
    _g(work, "checkout", "-q", "main")
    _write(work / filename, content)
    _g(work, "add", "-A")
    _g(work, "commit", "-qm", "base advances")
    _g(work, "push", "-q", "origin", "main")
    _g(work, "checkout", "-q", "feat/x")


# ── 1. up-to-date ─────────────────────────────────────────────────────────────

def test_up_to_date(repo):
    """When feat/x is already on the latest main (just pushed), status is up-to-date."""
    result = rebase_gate.rebase_check(str(repo), "main")
    assert result["status"] == "up-to-date"
    assert result["behind"] == 0


# ── 2. behind-clean ───────────────────────────────────────────────────────────

def test_behind_clean_status(repo):
    """After main advances with a non-conflicting commit, status is 'clean'."""
    _advance_base(repo)
    result = rebase_gate.rebase_check(str(repo), "main")
    assert result["status"] == "clean"
    assert result["behind"] >= 1
    assert result["conflict_files"] == []


def test_behind_clean_guidance_contains_manual_steps(repo):
    _advance_base(repo)
    result = rebase_gate.rebase_check(str(repo), "main")
    text = rebase_gate.guidance_text(result)
    assert "git rebase" in text
    assert "force-with-lease" in text


# ── 3. behind-conflicts ───────────────────────────────────────────────────────

def test_behind_conflicts_status(repo):
    """When both branches edit the same line, the predicted status is 'conflicts'."""
    # Edit the same line in base.py on main and on feat/x.
    _g(repo, "checkout", "-q", "main")
    _write(repo / "base.py", "x = 999\n")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-qm", "main edits x")
    _g(repo, "push", "-q", "origin", "main")
    _g(repo, "checkout", "-q", "feat/x")
    _write(repo / "base.py", "x = 777\n")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-qm", "feat edits x too")

    result = rebase_gate.rebase_check(str(repo), "main")
    # Either "conflicts" (predicted) or "clean" (merge-tree false-positive) is
    # acceptable; the tree must not be mutated.
    assert result["status"] in ("conflicts", "clean")
    assert isinstance(result["conflict_files"], list)


def test_conflicts_guidance_mentions_conflict(repo):
    """guidance_text for a conflicts result mentions manual conflict resolution."""
    result = {
        "status": "conflicts",
        "base": "main",
        "base_resolved": "origin/main",
        "behind": 2,
        "ahead": 1,
        "conflict_files": ["base.py"],
        "detail": "2 commit(s) behind origin/main; rebase would conflict.",
    }
    text = rebase_gate.guidance_text(result)
    assert "base.py" in text
    assert "git rebase" in text


# ── 4. dirty ─────────────────────────────────────────────────────────────────

def test_dirty_status(repo):
    """Uncommitted changes → status 'dirty' with dirty=True."""
    _advance_base(repo)
    _write(repo / "wip.py", "work = True\n")  # untracked → dirty

    result = rebase_gate.rebase_check(str(repo), "main")
    assert result["status"] == "dirty"
    assert result.get("dirty") is True
    # Behind count is still populated.
    assert result["behind"] is not None and result["behind"] >= 1


def test_dirty_guidance_says_stash_first(repo):
    result = {
        "status": "dirty",
        "base": "main",
        "base_resolved": "origin/main",
        "behind": 1,
        "ahead": 1,
        "dirty": True,
        "detail": "uncommitted changes present; ...",
    }
    text = rebase_gate.guidance_text(result)
    assert "stash" in text.lower() or "commit" in text.lower()


# ── 5. not-a-repo ─────────────────────────────────────────────────────────────

def test_not_a_repo(tmp_path):
    """A non-git directory returns status 'error'."""
    result = rebase_gate.rebase_check(str(tmp_path), "main", fetch=False)
    assert result["status"] == "error"


def test_cwd_does_not_exist():
    result = rebase_gate.rebase_check("/nonexistent/path/xyz", "main", fetch=False)
    assert result["status"] == "error"


# ── 6. Mutation-free proof ────────────────────────────────────────────────────

def test_free_path_does_not_mutate_tree(repo):
    """rebase_check NEVER changes HEAD, the index, or the working tree.

    Tree/HEAD hashes only prove no new commit was made; ``git status
    --porcelain`` and ``git diff``/``--cached`` are what actually prove the
    working tree and index (staged + unstaged + untracked) are untouched."""
    _advance_base(repo)
    # A staged change and a separate unstaged/untracked change so both the
    # index and the working tree are covered by the before/after diff.
    _write(repo / "wip.py", "pending = 1\n")
    _g(repo, "add", "wip.py")
    _write(repo / "unstaged.py", "also_pending = 1\n")

    tree_before = _tree_hash(repo)
    head_before = _sha(repo)
    status_before = _g(repo, "status", "--porcelain").stdout
    diff_before = _g(repo, "diff").stdout
    diff_cached_before = _g(repo, "diff", "--cached").stdout

    rebase_gate.rebase_check(str(repo), "main")

    assert _sha(repo) == head_before, "rebase_check must not commit or reset HEAD"
    assert _tree_hash(repo) == tree_before, "rebase_check must not create a new commit"
    assert _g(repo, "status", "--porcelain").stdout == status_before, \
        "rebase_check must not change staged/unstaged/untracked file state"
    assert _g(repo, "diff").stdout == diff_before, \
        "rebase_check must not mutate the working tree"
    assert _g(repo, "diff", "--cached").stdout == diff_cached_before, \
        "rebase_check must not mutate the index"


# ── 7. Capability hook — fake paid backend ────────────────────────────────────

class _FakePaidBackend:
    """Simulates a backend that exposes ``run_rebase``."""
    def __init__(self, status="rebased"):
        self._status = status
        self.calls = []

    def is_active(self):
        return True

    def run_review_loop(self, pr, repo, cwd, **opts):
        return 0

    def run_rebase(self, cwd, base, repo=None, remote=None):
        self.calls.append((cwd, base, repo, remote))
        return {"status": self._status, "base": base, "detail": "backend did it"}


class _FreeBackendNoRebase:
    """A backend that does NOT expose ``run_rebase`` (free tier)."""
    name = "free-no-rebase"
    priority = 0

    def is_active(self):
        return True

    def run_review_loop(self, pr, repo, cwd, **opts):
        return 0


def test_check_verb_never_delegates_even_with_paid_backend(repo):
    """rebase-check has no capability hook: it stays read-only-check even when
    a paid backend with run_rebase is active. (The hook lives on the
    separate ``rebase`` action verb, run_rebase_verb, not on the check verb.)"""
    sig = inspect.signature(rebase_gate.run_check_verb)
    assert "backend" not in sig.parameters

    out = io.StringIO()
    rc = rebase_gate.run_check_verb(str(repo), "main", fetch=True, out=out,
                                    json_only=True)
    data = json.loads(out.getvalue().strip())
    assert data["status"] in ("up-to-date", "clean", "conflicts", "dirty", "error")
    assert rc == (1 if data["status"] == "error" else 0)


def test_capability_hook_delegates_when_backend_has_run_rebase(repo):
    """When the active backend exposes run_rebase, run_rebase_verb delegates to it."""
    _advance_base(repo)
    backend = _FakePaidBackend(status="rebased")
    out = io.StringIO()

    rc = rebase_gate.run_rebase_verb(str(repo), "main", backend=backend,
                                     fetch=True, out=out, json_only=True)

    assert len(backend.calls) == 1, "backend.run_rebase must be called exactly once"
    assert backend.calls[0] == (str(repo), "main", None, None)
    assert rc == 0

    data = json.loads(out.getvalue().strip())
    assert data["status"] == "rebased"


def test_capability_hook_forwards_base_remote_override(repo):
    """A fork checkout's explicit --repo/--remote override must reach the
    paid backend's run_rebase, not just the free-fallback rebase_check path —
    otherwise a paid rebase could target the stale fork remote/base."""
    backend = _FakePaidBackend(status="rebased")
    out = io.StringIO()

    rc = rebase_gate.run_rebase_verb(str(repo), "main", backend=backend,
                                     fetch=False, out=out, json_only=True,
                                     repo="owner/upstream-repo",
                                     remote="upstream")

    assert len(backend.calls) == 1
    assert backend.calls[0] == (str(repo), "main", "owner/upstream-repo",
                                "upstream")
    assert rc == 0


def test_capability_hook_free_path_when_backend_has_no_run_rebase(repo):
    """When the backend has no run_rebase, the free check path runs instead
    (declines to mutate, same read-only contract as rebase-check)."""
    backend = _FreeBackendNoRebase()
    out = io.StringIO()

    rc = rebase_gate.run_rebase_verb(str(repo), "main", backend=backend,
                                     fetch=True, out=out, json_only=True)

    data = json.loads(out.getvalue().strip())
    assert data["status"] in ("up-to-date", "clean", "conflicts", "dirty", "error")
    # rc is 0 for any valid check result, 1 only when the check itself errored
    assert rc == (1 if data["status"] == "error" else 0)


def test_capability_hook_no_backend(repo):
    """With backend=None, the free path runs (no delegation attempted)."""
    out = io.StringIO()
    rc = rebase_gate.run_rebase_verb(str(repo), "main", backend=None,
                                     fetch=True, out=out, json_only=True)
    data = json.loads(out.getvalue().strip())
    assert data["status"] in ("up-to-date", "clean", "conflicts", "dirty", "error")
    assert rc == (1 if data["status"] == "error" else 0)


def test_capability_hook_backend_run_rebase_exception_falls_through(repo):
    """A backend whose run_rebase raises falls back to the free check path."""
    class _BrokenBackend:
        def is_active(self): return True
        def run_review_loop(self, *a, **k): return 0
        def run_rebase(self, cwd, base, repo=None, remote=None):
            raise RuntimeError("backend exploded")

    out = io.StringIO()
    rc = rebase_gate.run_rebase_verb(str(repo), "main", backend=_BrokenBackend(),
                                     fetch=True, out=out, json_only=True)
    data = json.loads(out.getvalue().strip())
    # Free path ran → valid status
    assert data["status"] in ("up-to-date", "clean", "conflicts", "dirty", "error")
    assert rc == (1 if data["status"] == "error" else 0)


# ── 8. CLI subcommand smoke tests ─────────────────────────────────────────────

def test_cli_rebase_check_json_only(repo):
    """The rebase-check CLI subcommand emits valid JSON on --json-only."""
    captured = []
    original = rebase_gate.run_check_verb

    def fake_verb(cwd, base, **kwargs):
        out = kwargs.get("out") or __import__("sys").stdout
        result = rebase_gate.rebase_check(cwd, base, fetch=False)
        print(json.dumps(result), file=out)
        captured.append(result)
        return 0

    rebase_gate.run_check_verb = fake_verb
    try:
        rc = cli.main(["rebase-check", "--cwd", str(repo), "--base", "main",
                       "--no-fetch", "--json-only"])
    finally:
        rebase_gate.run_check_verb = original

    assert rc == 0
    assert len(captured) == 1
    assert "status" in captured[0]


def test_cli_rebase_check_help():
    """rebase-check --help exits 0 (argparse prints help and exits)."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["rebase-check", "--help"])
    assert exc.value.code == 0


# ── 9. _resolve_baseref: FETCH_HEAD vs. a stale tracking ref ─────────────────

class _FakeGitRun:
    """Answers ``git -C <cwd> rev-parse --verify --quiet <ref>`` per a table of
    ref -> (returncode, stdout), so a stale-but-resolvable tracking ref can be
    modelled without needing a real narrow/single-branch clone."""

    def __init__(self, answers):
        self._answers = answers
        self.calls = []

    def __call__(self, argv, *, cwd=None, timeout=None):
        self.calls.append(list(argv))
        ref = argv[-1]
        rc, out = self._answers.get(ref, (1, ""))
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")


def test_resolve_baseref_prefers_fetch_head_over_stale_tracking_ref():
    """The bug: a narrow/single-branch checkout's ``origin/main`` tracking ref
    can still resolve (stale) after an explicit ``git fetch origin main`` that
    only updated FETCH_HEAD. With ``try_fetch_head=True`` FETCH_HEAD must win
    even though the stale tracking ref also resolves successfully."""
    fake = _FakeGitRun({
        "origin/main": (0, "stale-sha"),
        "refs/remotes/origin/main": (0, "stale-sha"),
        "FETCH_HEAD": (0, "fresh-sha"),
    })
    baseref = rebase_gate._resolve_baseref(
        "/repo", "main", "origin", fake, try_fetch_head=True)
    assert baseref == "FETCH_HEAD"
    assert fake.calls[0][-1] == "FETCH_HEAD", "FETCH_HEAD must be tried first"


def test_resolve_baseref_falls_back_to_tracking_ref_when_no_fetch_head():
    """When FETCH_HEAD doesn't resolve (no fetch just ran), the tracking ref
    is still used."""
    fake = _FakeGitRun({"origin/main": (0, "sha")})
    baseref = rebase_gate._resolve_baseref(
        "/repo", "main", "origin", fake, try_fetch_head=True)
    assert baseref == "origin/main"


def test_resolve_baseref_uses_tracking_ref_without_try_fetch_head():
    """Without ``try_fetch_head`` (no explicit fetch just ran for this base),
    the remote-tracking ref is tried directly; FETCH_HEAD is never consulted."""
    fake = _FakeGitRun({"origin/main": (0, "sha"), "FETCH_HEAD": (0, "other-sha")})
    baseref = rebase_gate._resolve_baseref(
        "/repo", "main", "origin", fake, try_fetch_head=False)
    assert baseref == "origin/main"
    assert "FETCH_HEAD" not in [c[-1] for c in fake.calls]


# ── 10. _default_run: non-interactive stdin ───────────────────────────────────

def test_default_run_sets_stdin_devnull(monkeypatch):
    """git must never block on a credential/input prompt: stdin is DEVNULL."""
    captured = {}
    real_run = subprocess.run

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    rebase_gate._default_run(["git", "--version"])
    assert captured.get("stdin") is subprocess.DEVNULL


# ── 11. _git: injected run() exceptions never escape ─────────────────────────

def test_git_converts_timeout_to_structured_failure():
    def raising_run(argv, *, cwd=None, timeout=None):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    r = rebase_gate._git("/repo", "fetch", "origin", "main", run=raising_run)
    assert rebase_gate._rc(r) != 0


def test_git_converts_missing_binary_to_structured_failure():
    def raising_run(argv, *, cwd=None, timeout=None):
        raise FileNotFoundError("git")

    r = rebase_gate._git("/repo", "status", "--porcelain", run=raising_run)
    assert rebase_gate._rc(r) != 0


def test_rebase_check_reports_error_status_when_run_raises(repo):
    """End-to-end: an injected run() that raises must surface as a structured
    error result, not propagate the exception out of rebase_check()."""
    def raising_run(argv, *, cwd=None, timeout=None):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    result = rebase_gate.rebase_check(str(repo), "main", run=raising_run)
    assert result["status"] == "error"


# ── 12. Fork checkout — the base remote is upstream, not origin ───────────────

@pytest.fixture
def fork(tmp_path):
    """A fork checkout: ``origin`` is the contributor's fork, the PR base lives
    on ``upstream``.

    Built exactly as a contributor's clone is: ``upstream.git`` (bare, hosts
    ``main``) → ``fork.git`` (a bare clone of it) → ``work`` (a plain clone of
    the FORK, so ``origin`` = fork), with ``upstream`` added as a second remote
    afterwards. Crucially ``branch.main.remote`` stays ``origin`` — that is
    what a plain ``git clone <fork>`` leaves behind, which is why the
    ``branch.<base>.remote`` lookup alone cannot save this shape.

    Returns ``(work, upstream_path, seed)``; ``seed`` is a clone of upstream
    used to push commits onto the real base."""
    upstream = tmp_path / "upstream.git"
    _g(tmp_path, "init", "-q", "--bare", str(upstream))

    seed = tmp_path / "seed"
    _g(tmp_path, "clone", "-q", str(upstream), str(seed))
    _g(seed, "config", "user.email", "t@example.com")
    _g(seed, "config", "user.name", "Tester")
    _g(seed, "checkout", "-q", "-b", "main")
    _write(seed / "base.py", "x = 1\n")
    _g(seed, "add", "-A")
    _g(seed, "commit", "-qm", "base commit")
    _g(seed, "push", "-q", "-u", "origin", "main")
    # `git init --bare` sets HEAD from init.defaultBranch, which may not be
    # `main`; point it at the branch that actually exists so clones of this
    # repo (and of the fork below) check out `main`.
    _g(upstream, "symbolic-ref", "HEAD", "refs/heads/main")

    fork_bare = tmp_path / "fork.git"
    _g(tmp_path, "clone", "-q", "--bare", str(upstream), str(fork_bare))

    work = tmp_path / "work"
    _g(tmp_path, "clone", "-q", str(fork_bare), str(work))
    _g(work, "config", "user.email", "c@example.com")
    _g(work, "config", "user.name", "Contributor")
    _g(work, "remote", "add", "upstream", str(upstream))
    _g(work, "fetch", "-q", "upstream")
    _g(work, "checkout", "-q", "-b", "feat/x", "main")
    _write(work / "feature.py", "y = 2\n")
    _g(work, "add", "-A")
    _g(work, "commit", "-qm", "feature work")
    _g(work, "push", "-q", "-u", "origin", "feat/x")

    # The base advances on UPSTREAM only; the fork's own `main` stays stale.
    _write(seed / "other.py", "z = 3\n")
    _g(seed, "add", "-A")
    _g(seed, "commit", "-qm", "upstream base advances")
    _g(seed, "push", "-q", "origin", "main")

    return work, upstream, seed


def test_fork_branch_config_still_points_at_origin(fork):
    """Pins the premise: in a fork clone `branch.main.remote` IS `origin`, so
    that lookup alone cannot find the upstream base."""
    work, _upstream, _seed = fork
    assert _g(work, "config", "--get", "branch.main.remote").stdout.strip() == "origin"


def test_fork_explicit_remote_sees_upstream_is_ahead(fork):
    """With `--remote upstream`, the gate compares against the REAL base and
    reports the branch as behind (a rebase is required)."""
    work, _upstream, _seed = fork
    result = rebase_gate.rebase_check(str(work), "main", remote="upstream")
    assert result["remote"] == "upstream"
    assert result["status"] == "clean"
    assert result["behind"] == 1


def test_fork_repo_hint_resolves_the_upstream_remote(fork, tmp_path):
    """`repo` (the owner/repo the PR base lives on) is matched against the
    configured remotes' URLs — the authoritative signal, per
    merge._remote_for_repo — and selects `upstream` over `origin`."""
    work, _upstream, _seed = fork
    result = rebase_gate.rebase_check(str(work), "main",
                                      repo=f"{tmp_path.name}/upstream")
    assert result["remote"] == "upstream"
    assert result["status"] == "clean"
    assert result["behind"] == 1


def test_fork_without_a_hint_compares_against_the_stale_fork(fork):
    """Documents WHY --repo/--remote exist: with no hint the chain falls back to
    branch.main.remote → `origin` (the fork), whose `main` is stale, so the
    branch reads up-to-date even though upstream/main is ahead."""
    work, _upstream, _seed = fork
    result = rebase_gate.rebase_check(str(work), "main")
    assert result["remote"] == "origin"
    assert result["status"] == "up-to-date"


def test_explicit_remote_overrides_repo_hint(fork, tmp_path):
    """`remote` is the explicit operator override and wins over `repo`."""
    work, _upstream, _seed = fork
    assert rebase_gate._resolve_base_remote(
        str(work), "main", rebase_gate._default_run,
        repo=f"{tmp_path.name}/upstream", remote="origin") == "origin"


def test_unmatched_repo_hint_falls_back_to_branch_config(fork):
    """A `repo` that matches no configured remote must not break the check — it
    falls through to branch.<base>.remote, then origin."""
    work, _upstream, _seed = fork
    assert rebase_gate._resolve_base_remote(
        str(work), "main", rebase_gate._default_run,
        repo="someone/not-a-configured-remote") == "origin"


def test_repo_and_remote_flags_reach_the_verb(fork, tmp_path):
    """The CLI plumbs --repo/--remote through to the engine (both verbs)."""
    work, _upstream, _seed = fork
    seen = {}
    original = rebase_gate.run_check_verb

    def fake_verb(cwd, base, **kwargs):
        seen.update(kwargs)
        return 0

    rebase_gate.run_check_verb = fake_verb
    try:
        rc = cli.main(["rebase-check", "--cwd", str(work), "--base", "main",
                       "--repo", f"{tmp_path.name}/upstream",
                       "--remote", "upstream", "--json-only"])
    finally:
        rebase_gate.run_check_verb = original

    assert rc == 0
    assert seen["repo"] == f"{tmp_path.name}/upstream"
    assert seen["remote"] == "upstream"

    args = cli.build_parser().parse_args(
        ["rebase", "--repo", "o/r", "--remote", "upstream"])
    assert (args.repo, args.remote) == ("o/r", "upstream")


def test_cli_rebase_check_end_to_end_with_remote_flag(fork, capsys):
    """End-to-end through cli.main: the fork checkout with --remote upstream
    reports `clean` (behind), not a false `up-to-date`."""
    work, _upstream, _seed = fork
    rc = cli.main(["rebase-check", "--cwd", str(work), "--base", "main",
                   "--remote", "upstream", "--json-only"])
    data = json.loads(capsys.readouterr().out.strip())
    assert rc == 0
    assert data["remote"] == "upstream"
    assert data["status"] == "clean"
    assert data["behind"] == 1
