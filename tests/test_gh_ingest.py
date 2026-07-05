"""The real-``gh`` ingest: env seam, runner injection, payload mapping."""
import json
import subprocess

import pytest

from buddhi_review import gh_ingest


def _proc(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def test_env_seam_short_circuits_gh(monkeypatch):
    monkeypatch.setenv(
        gh_ingest.COMMENTS_JSON_ENV,
        json.dumps([
            {"id": 11, "body": "fix the null check", "user": {"login": "copilot"}},
            {"id": 12, "body": "", "user": {"login": "gemini"}},  # empty → dropped
        ]),
    )
    def explode(argv):  # gh must never be invoked when the seam is set
        raise AssertionError("gh was called")
    comments = gh_ingest.fetch_comments("5", run=explode)
    assert [(c.id, c.text, c.source) for c in comments] == [("11", "fix the null check", "copilot")]


def test_env_seam_garbage_is_empty(monkeypatch):
    monkeypatch.setenv(gh_ingest.COMMENTS_JSON_ENV, "not json")
    assert gh_ingest.fetch_comments("5", run=lambda argv: _proc()) == []


def test_gh_endpoints_and_mapping(monkeypatch):
    monkeypatch.delenv(gh_ingest.COMMENTS_JSON_ENV, raising=False)
    calls = []

    def fake_run(argv, *, cwd=None):
        calls.append(argv)
        if argv[-1].endswith("pulls/7/comments"):
            return _proc(json.dumps([{"id": 1, "body": "a", "user": {"login": "codex"}}]))
        if argv[-1].endswith("pulls/7/reviews"):
            return _proc(json.dumps([{"id": 2, "body": "b", "user": {"login": "claude"}}]))
        # issues/{pr}/comments — the `gh pr comment` channel
        return _proc(json.dumps([{"id": 3, "body": "c", "user": {"login": "gemini"}}]))

    comments = gh_ingest.fetch_comments("7", repo="o/r", run=fake_run)
    assert [c.id for c in comments] == ["1", "2", "3"]
    assert calls[0][:3] == ["gh", "api", "--paginate"]
    assert calls[0][-1] == "repos/o/r/pulls/7/comments"
    assert calls[1][-1] == "repos/o/r/pulls/7/reviews"
    assert calls[2][-1] == "repos/o/r/issues/7/comments"


def test_clean_sentinel_via_issue_comment(monkeypatch):
    """gh pr comment posts to issues/{pr}/comments; round driver must see the sentinel."""
    monkeypatch.delenv(gh_ingest.COMMENTS_JSON_ENV, raising=False)

    def fake_run(argv, *, cwd=None):
        if argv[-1].endswith("issues/7/comments"):
            return _proc(json.dumps([
                {"id": 99, "body": "No issues found.", "user": {"login": "claude[bot]"},
                 "created_at": "2024-01-01T00:00:00Z"},
            ]))
        return _proc("[]")

    comments = gh_ingest.fetch_comments("7", repo="o/r", run=fake_run)
    assert len(comments) == 1
    assert comments[0].text == "No issues found."
    assert comments[0].source == "claude[bot]"


def test_only_issue_channel_is_tagged(monkeypatch):
    """Inline + review-body comments are NOT from the issue channel; only the
    issues/<pr>/comments endpoint is tagged so the round driver knows to scan it
    for the sentinel/signals alone (findings come inline per the workflow)."""
    monkeypatch.delenv(gh_ingest.COMMENTS_JSON_ENV, raising=False)

    def fake_run(argv, *, cwd=None):
        if argv[-1].endswith("pulls/7/comments"):
            return _proc(json.dumps([{"id": 1, "body": "inline finding",
                                      "user": {"login": "copilot"}, "path": "a.py",
                                      "diff_hunk": "@@"}]))
        if argv[-1].endswith("pulls/7/reviews"):
            return _proc(json.dumps([{"id": 2, "body": "review body",
                                      "user": {"login": "claude"}}]))
        return _proc(json.dumps([{"id": 3, "body": "top-level chatter",
                                  "user": {"login": "gemini"}}]))

    by_id = {c.id: c for c in gh_ingest.fetch_comments("7", repo="o/r", run=fake_run)}
    assert by_id["1"].from_issue_channel is False
    assert by_id["2"].from_issue_channel is False
    assert by_id["3"].from_issue_channel is True


def test_seeded_path_infers_issue_channel_from_payload(monkeypatch):
    """The BUDDHI_REVIEW_COMMENTS_JSON seam has no endpoint, so the issue channel
    is inferred from the payload: `issue_url` present + no inline path/diff_hunk."""
    monkeypatch.setenv(
        gh_ingest.COMMENTS_JSON_ENV,
        json.dumps([
            {"id": 1, "body": "inline", "user": {"login": "claude"},
             "path": "a.py", "diff_hunk": "@@"},
            {"id": 2, "body": "conversation", "user": {"login": "claude"},
             "issue_url": "https://api.github.com/repos/o/r/issues/7"},
        ]),
    )
    by_id = {c.id: c for c in gh_ingest.fetch_comments("7", repo="o/r")}
    assert by_id["1"].from_issue_channel is False
    assert by_id["2"].from_issue_channel is True


def test_gh_failure_raises(monkeypatch):
    monkeypatch.delenv(gh_ingest.COMMENTS_JSON_ENV, raising=False)
    with pytest.raises(RuntimeError):
        gh_ingest.fetch_comments("7", repo="o/r", run=lambda argv, **kwargs: _proc(rc=1, stderr="boom"))


def test_paginated_concatenated_arrays(monkeypatch):
    monkeypatch.delenv(gh_ingest.COMMENTS_JSON_ENV, raising=False)
    pages = (
        json.dumps([{"id": 1, "body": "x", "user": {"login": "a"}}])
        + json.dumps([{"id": 2, "body": "y", "user": {"login": "b"}}])
    )

    def fake_run(argv, *, cwd=None):
        return _proc(pages if "comments" in argv[-1] else "[]")

    comments = gh_ingest.fetch_comments("9", repo="o/r", run=fake_run)
    assert [c.id for c in comments] == ["1", "2"]


def test_duplicate_ids_deduped(monkeypatch):
    monkeypatch.delenv(gh_ingest.COMMENTS_JSON_ENV, raising=False)

    def fake_run(argv, *, cwd=None):
        return _proc(json.dumps([{"id": 3, "body": "same", "user": {"login": "a"}}]))

    comments = gh_ingest.fetch_comments("9", repo="o/r", run=fake_run)
    assert len(comments) == 1  # appears in both endpoints, ingested once


def test_ingest_source_yields_raw_items(monkeypatch):
    monkeypatch.setenv(
        gh_ingest.COMMENTS_JSON_ENV,
        json.dumps([{"id": 4, "body": "claim", "user": {"login": "gemini"}}]),
    )
    items = tuple(gh_ingest.ingest_source("5")())
    assert len(items) == 1
    assert items[0].id == "4"
    assert items[0].payload == "claim"
    assert items[0].source == "gemini"


def test_updated_at_is_read_into_the_comment(monkeypatch):
    """The errored-comeback candidate reads updated_at (edit time) before
    created_at, so the ingest must thread updated_at through."""
    monkeypatch.setenv(
        gh_ingest.COMMENTS_JSON_ENV,
        json.dumps([
            {"id": 1, "body": "edited finding", "user": {"login": "claude"},
             "created_at": "2026-06-10T00:00:00Z",
             "updated_at": "2026-06-10T00:10:00Z"},
            {"id": 2, "body": "never edited", "user": {"login": "claude"},
             "created_at": "2026-06-10T00:00:00Z"},
        ]),
    )
    by_id = {c.id: c for c in gh_ingest.fetch_comments("7", repo="o/r")}
    assert by_id["1"].updated_at == "2026-06-10T00:10:00Z"
    assert by_id["1"].created_at == "2026-06-10T00:00:00Z"
    assert by_id["2"].updated_at is None  # absent → None


# --------------------------------------------------------------- diff-size probe

def test_fetch_pr_diff_lines_sums_additions_and_deletions():
    def fake_run(argv, *, cwd=None):
        assert argv[:4] == ["gh", "pr", "view", "7"]
        assert "additions,deletions" in argv
        assert argv[-2:] == ["-R", "o/r"]
        return _proc(json.dumps({"additions": 120, "deletions": 30}))

    assert gh_ingest.fetch_pr_diff_lines("7", repo="o/r", run=fake_run) == 150


def test_fetch_pr_diff_lines_no_repo_omits_flag():
    def fake_run(argv, *, cwd=None):
        assert "-R" not in argv
        return _proc(json.dumps({"additions": 1, "deletions": 1}))

    assert gh_ingest.fetch_pr_diff_lines("7", run=fake_run) == 2


def test_fetch_pr_diff_lines_returns_none_on_failure():
    # non-zero exit, unparseable JSON, missing/ill-typed counts, and a raising run
    # all fail soft to None so the caller uses the default budget.
    assert gh_ingest.fetch_pr_diff_lines("7", run=lambda a, **k: _proc(rc=1)) is None
    assert gh_ingest.fetch_pr_diff_lines("7", run=lambda a, **k: _proc("not json")) is None
    assert gh_ingest.fetch_pr_diff_lines(
        "7", run=lambda a, **k: _proc(json.dumps({"additions": 5}))) is None
    assert gh_ingest.fetch_pr_diff_lines(
        "7", run=lambda a, **k: _proc(json.dumps({"additions": "x", "deletions": 1}))) is None

    def boom(argv, *, cwd=None):
        raise OSError("gh missing")
    assert gh_ingest.fetch_pr_diff_lines("7", run=boom) is None


# ------------------------------------------------------------------- reactions


def test_reactions_env_seam_short_circuits_gh(monkeypatch):
    monkeypatch.setenv(
        gh_ingest.REACTIONS_JSON_ENV,
        json.dumps([
            {"id": 1, "content": "+1", "user": {"login": "chatgpt-codex-connector[bot]",
                                                 "type": "User"}},
            {"id": 2, "content": "eyes", "user": {"login": "copilot[bot]", "type": "Bot"}},
        ]),
    )

    def explode(argv, **kw):  # gh must never be invoked when the seam is set
        raise AssertionError("gh was called")

    reactions = gh_ingest.fetch_reactions("5", run=explode)
    assert [(r.id, r.content, r.source) for r in reactions] == [
        ("1", "+1", "chatgpt-codex-connector[bot]"),
        ("2", "eyes", "copilot[bot]"),
    ]


def test_reactions_drop_non_bot_authors(monkeypatch):
    # A human's +1 must never reach the fold — only a "[bot]" login OR type=="Bot"
    # is trusted. A login that merely CONTAINS a bot alias (john-copilot) is a
    # human and is dropped.
    monkeypatch.setenv(
        gh_ingest.REACTIONS_JSON_ENV,
        json.dumps([
            {"id": 1, "content": "+1", "user": {"login": "octocat", "type": "User"}},
            {"id": 2, "content": "+1", "user": {"login": "john-copilot", "type": "User"}},
            {"id": 3, "content": "+1", "user": {"login": "claude[bot]", "type": "Bot"}},
        ]),
    )
    reactions = gh_ingest.fetch_reactions("5")
    assert [r.source for r in reactions] == ["claude[bot]"]


def test_reactions_gh_endpoint_and_dedup(monkeypatch):
    monkeypatch.delenv(gh_ingest.REACTIONS_JSON_ENV, raising=False)
    calls = []

    def fake_run(argv, *, cwd=None):
        calls.append(argv)
        # concatenated pages with a duplicate id across pages
        return _proc(
            json.dumps([{"id": 9, "content": "+1", "user": {"login": "codex[bot]"}}])
            + json.dumps([{"id": 9, "content": "+1", "user": {"login": "codex[bot]"}}]))

    reactions = gh_ingest.fetch_reactions("7", repo="o/r", run=fake_run)
    assert calls[0][:3] == ["gh", "api", "--paginate"]
    assert calls[0][-1] == "repos/o/r/issues/7/reactions"
    assert [r.id for r in reactions] == ["9"]  # deduped across pages


def test_reactions_malformed_dropped(monkeypatch):
    monkeypatch.setenv(
        gh_ingest.REACTIONS_JSON_ENV,
        json.dumps([
            {"content": "+1", "user": {"login": "codex[bot]"}},   # no id
            {"id": 2, "user": {"login": "codex[bot]"}},           # no content
            {"id": 3, "content": "+1"},                            # no user
            {"id": 4, "content": "+1", "user": {"login": "codex[bot]"}},  # ok
        ]),
    )
    reactions = gh_ingest.fetch_reactions("5")
    assert [r.id for r in reactions] == ["4"]


def test_reactions_gh_failure_raises(monkeypatch):
    monkeypatch.delenv(gh_ingest.REACTIONS_JSON_ENV, raising=False)
    with pytest.raises(RuntimeError):
        gh_ingest.fetch_reactions("7", repo="o/r",
                                  run=lambda argv, **kw: _proc(rc=1, stderr="boom"))
