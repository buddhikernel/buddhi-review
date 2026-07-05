"""Tests for buddhi_review.session_worktrees — the durable session → worktree
registry the git-guardrail hook writes and the /open-pr + /review-pr resolver
reads. Network-free and hermetic: every test pins the registry file to a tmp path
via ``$BUDDHI_SESSION_WORKTREES_PATH`` and passes an explicit ``now=`` so nothing
sleeps or touches the real clock.
"""
import json
import os

import pytest

from buddhi_review import session_worktrees as sw


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDHI_SESSION_WORKTREES_PATH",
                       str(tmp_path / "session-worktrees.json"))
    return tmp_path


def test_registry_path_honors_override(tmp_path, monkeypatch):
    p = str(tmp_path / "custom.json")
    monkeypatch.setenv("BUDDHI_SESSION_WORKTREES_PATH", p)
    assert sw.registry_path() == p


def test_registry_path_default_is_shared_cache(monkeypatch):
    monkeypatch.delenv("BUDDHI_SESSION_WORKTREES_PATH", raising=False)
    assert sw.registry_path() == os.path.expanduser(
        "~/.cache/buddhi/session-worktrees.json")


def test_round_trip_returns_abspath():
    assert sw.register("s1", "/Users/me/repo/.claude/worktrees/foo") is True
    assert sw.lookup("s1") == "/Users/me/repo/.claude/worktrees/foo"


def test_register_normalizes_relative_and_user_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sw.register("s2", "~/work/wt")
    assert sw.lookup("s2") == os.path.join(str(tmp_path), "work", "wt")


def test_latest_write_wins():
    sw.register("s", "/a/.claude/worktrees/first", now=1_000)
    sw.register("s", "/a/.claude/worktrees/second", now=2_000)
    assert sw.lookup("s") == "/a/.claude/worktrees/second"


def test_lookup_missing_file_returns_none():
    # No register() has run → the file does not exist yet.
    assert sw.lookup("nobody") is None


def test_lookup_unknown_session_returns_none():
    sw.register("known", "/a/.claude/worktrees/x")
    assert sw.lookup("unknown") is None


def test_corrupt_file_yields_none_not_raise(monkeypatch):
    with open(sw.registry_path(), "w", encoding="utf-8") as f:
        f.write("{ this is not json")
    assert sw.lookup("s") is None            # tolerated, no raise
    assert sw.all_entries() == []
    # …and a subsequent write recovers cleanly over the corrupt file.
    assert sw.register("s", "/a/.claude/worktrees/ok") is True
    assert sw.lookup("s") == "/a/.claude/worktrees/ok"


def test_non_object_json_yields_none(monkeypatch):
    with open(sw.registry_path(), "w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)
    assert sw.lookup("s") is None


def test_falsy_inputs_are_noops():
    assert sw.register("", "/a/.claude/worktrees/x") is False
    assert sw.register("s", "") is False
    assert sw.lookup("") is None
    assert sw.lookup(None) is None


def test_atomic_write_leaves_no_partial_file(tmp_path):
    sw.register("s", "/a/.claude/worktrees/x")
    # Only the final registry file exists; no leftover temp files in the dir.
    leftovers = [n for n in os.listdir(tmp_path)
                 if n.startswith(".session-worktrees-")]
    assert leftovers == []
    # And the file on disk is valid JSON with the documented shape.
    with open(sw.registry_path(), encoding="utf-8") as f:
        data = json.load(f)
    assert data["version"] == 1
    assert data["sessions"]["s"]["worktree"] == "/a/.claude/worktrees/x"


def test_prune_caps_at_100_most_recent():
    # 130 sessions, ascending timestamps → only the newest 100 survive.
    for i in range(130):
        sw.register(f"s{i:03d}", f"/a/.claude/worktrees/w{i}", now=1_000 + i)
    with open(sw.registry_path(), encoding="utf-8") as f:
        sessions = json.load(f)["sessions"]
    assert len(sessions) == 100
    assert "s030" in sessions and "s129" in sessions   # newest kept
    assert "s000" not in sessions and "s029" not in sessions  # oldest dropped


def test_prune_drops_entries_older_than_30_days():
    now = 100 * 24 * 3600  # day 100
    sw.register("old", "/a/.claude/worktrees/old", now=now - 31 * 24 * 3600)
    # A fresh write at `now` prunes the >30-day-old entry out of the file.
    sw.register("fresh", "/a/.claude/worktrees/fresh", now=now)
    assert sw.lookup("fresh") == "/a/.claude/worktrees/fresh"
    assert sw.lookup("old") is None


def test_repo_field_is_stored_and_exposed():
    sw.register("s", "/a/.claude/worktrees/x", repo="owner/repo", now=5)
    entries = sw.all_entries()
    assert entries == [{"worktree": "/a/.claude/worktrees/x",
                        "repo": "owner/repo", "ts": 5.0}]


def test_hostile_bigint_ts_never_raises(monkeypatch):
    # A hostile registry entry with a ts too large to convert to float must not
    # raise out of any read — the "never raises" contract holds.
    big = int("1" + "0" * 400)   # overflows float()
    with open(sw.registry_path(), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "sessions": {
            "s": {"worktree": "/a/.claude/worktrees/w", "repo": None, "ts": big}}}, f)
    # lookup does not touch ts → returns the path; all_entries sorts by ts → must
    # tolerate the overflow and still return the entry (ts folded to 0.0).
    assert sw.lookup("s") == "/a/.claude/worktrees/w"
    entries = sw.all_entries()
    assert [e["worktree"] for e in entries] == ["/a/.claude/worktrees/w"]
    assert entries[0]["ts"] == 0.0
    # and a subsequent register (which prunes over the bad ts) also stays safe.
    assert sw.register("t", "/a/.claude/worktrees/t", now=5) is True


def test_all_entries_newest_first():
    sw.register("a", "/a/.claude/worktrees/a", now=10)
    sw.register("b", "/a/.claude/worktrees/b", now=30)
    sw.register("c", "/a/.claude/worktrees/c", now=20)
    got = [e["worktree"] for e in sw.all_entries()]
    assert got == ["/a/.claude/worktrees/b",
                   "/a/.claude/worktrees/c",
                   "/a/.claude/worktrees/a"]
