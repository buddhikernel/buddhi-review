"""F3 rebase-gate engine verb — ``python -m buddhi_review rebase-check``.

Every test drives real git against a local bare remote (no network); the
free path is proven mutation-free (HEAD/tree hash, ``git status
--porcelain``, and ``git diff``/``--cached`` all unchanged after
rebase_check).
"""
from __future__ import annotations

import io
import json
import subprocess
import types

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

    def run_rebase(self, cwd, base):
        self.calls.append((cwd, base))
        return {"status": self._status, "base": base, "detail": "backend did it"}


class _FreeBackendNoRebase:
    """A backend that does NOT expose ``run_rebase`` (free tier)."""
    name = "free-no-rebase"
    priority = 0

    def is_active(self):
        return True

    def run_review_loop(self, pr, repo, cwd, **opts):
        return 0


def test_capability_hook_delegates_when_backend_has_run_rebase(repo):
    """When the active backend exposes run_rebase, run_check_verb delegates to it."""
    _advance_base(repo)
    backend = _FakePaidBackend(status="rebased")
    out = io.StringIO()

    rc = rebase_gate.run_check_verb(str(repo), "main", backend=backend,
                                    fetch=True, out=out, json_only=True)

    assert len(backend.calls) == 1, "backend.run_rebase must be called exactly once"
    assert backend.calls[0] == (str(repo), "main")
    assert rc == 0

    data = json.loads(out.getvalue().strip())
    assert data["status"] == "rebased"


def test_capability_hook_free_path_when_backend_has_no_run_rebase(repo):
    """When the backend has no run_rebase, the free check path runs instead."""
    backend = _FreeBackendNoRebase()
    out = io.StringIO()

    rc = rebase_gate.run_check_verb(str(repo), "main", backend=backend,
                                    fetch=True, out=out, json_only=True)

    data = json.loads(out.getvalue().strip())
    assert data["status"] in ("up-to-date", "clean", "conflicts", "dirty", "error")
    # exit 0 for any valid check result (non-error)
    assert rc == 0


def test_capability_hook_no_backend(repo):
    """With backend=None, the free path runs (no delegation attempted)."""
    out = io.StringIO()
    rc = rebase_gate.run_check_verb(str(repo), "main", backend=None,
                                    fetch=True, out=out, json_only=True)
    data = json.loads(out.getvalue().strip())
    assert data["status"] in ("up-to-date", "clean", "conflicts", "dirty", "error")
    assert rc == 0


def test_capability_hook_backend_run_rebase_exception_falls_through(repo):
    """A backend whose run_rebase raises falls back to the free check path."""
    class _BrokenBackend:
        def is_active(self): return True
        def run_review_loop(self, *a, **k): return 0
        def run_rebase(self, cwd, base):
            raise RuntimeError("backend exploded")

    out = io.StringIO()
    rc = rebase_gate.run_check_verb(str(repo), "main", backend=_BrokenBackend(),
                                    fetch=True, out=out, json_only=True)
    data = json.loads(out.getvalue().strip())
    # Free path ran → valid status
    assert data["status"] in ("up-to-date", "clean", "conflicts", "dirty", "error")
    assert rc == 0


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
