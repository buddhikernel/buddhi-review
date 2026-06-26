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
