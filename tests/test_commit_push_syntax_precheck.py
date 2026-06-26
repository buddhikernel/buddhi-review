"""The shift-left ADVISORY syntax pre-check in commit_and_push.

It names a fixer-introduced syntax error before the (possibly minutes-long) test
gate, and NEVER blocks the commit/push — it only informs.
"""
import subprocess

import pytest

from buddhi_review import commit_push
from buddhi_review.commit_push import (
    _advisory_syntax_precheck,
    _changed_paths_from_porcelain,
)


def _collector():
    rec = []

    def notice(action, detail="", *, status="do", hint=None):
        rec.append((action, detail, status))
        return f"{action} {detail}"
    return rec, notice


# ── porcelain parsing ────────────────────────────────────────────────────────

def test_changed_paths_from_porcelain():
    porcelain = (
        " M src/mod.py\n"
        "?? new_file.py\n"
        "A  added.py\n"
        "R  old.py -> renamed.py\n"
        " D deleted.py\n"
    )
    paths = _changed_paths_from_porcelain(porcelain)
    assert "src/mod.py" in paths
    assert "new_file.py" in paths
    assert "added.py" in paths
    assert "renamed.py" in paths and "old.py" not in paths   # rename → the new name
    assert "deleted.py" in paths                              # filtered later by isfile


def test_changed_paths_from_porcelain_empty():
    assert _changed_paths_from_porcelain("") == []
    assert _changed_paths_from_porcelain("   ") == []


# ── the advisory check (direct) ──────────────────────────────────────────────

def test_advisory_flags_broken_changed_file(tmp_path):
    (tmp_path / "mod.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    rec, notice = _collector()
    out = _advisory_syntax_precheck(str(tmp_path), " M mod.py\n", notice=notice)
    assert out is not None
    assert rec and rec[0][0] == "syntax pre-check" and rec[0][2] == "do"
    assert "syntax error in" in rec[0][1] and "mod.py" in rec[0][1]


def test_advisory_silent_on_clean_files(tmp_path):
    (tmp_path / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    rec, notice = _collector()
    assert _advisory_syntax_precheck(str(tmp_path), " M mod.py\n", notice=notice) is None
    assert rec == []


def test_advisory_ignores_missing_and_unrecognized(tmp_path):
    # a deleted/missing path and a .txt file are not a syntax error → no false alarm
    (tmp_path / "notes.txt").write_text("def f(:\n", encoding="utf-8")
    rec, notice = _collector()
    porcelain = " D gone.py\n M notes.txt\n"
    assert _advisory_syntax_precheck(str(tmp_path), porcelain, notice=notice) is None
    assert rec == []


def test_advisory_never_raises_on_garbage(tmp_path):
    rec, notice = _collector()
    # malformed porcelain / odd input must degrade silently
    assert _advisory_syntax_precheck(str(tmp_path), "garbage no status", notice=notice) is None


# ── integration: advisory runs but NEVER blocks the push ─────────────────────

@pytest.fixture
def repo(tmp_path):
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


def test_advisory_does_not_block_a_broken_file(monkeypatch, repo):
    """A syntactically-broken changed file is FLAGGED but the commit/push still
    proceeds when the gate is green — the pre-check is advisory, never a gate."""
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -c pass")   # green gate
    (repo / "broken.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    rec, notice = _collector()
    out = commit_push.commit_and_push(str(repo), message="fix: round 1", notice=notice)
    assert out == "pushed"   # NOT blocked by the advisory
    assert any(a == "syntax pre-check" and s == "do" for a, _d, s in rec)
