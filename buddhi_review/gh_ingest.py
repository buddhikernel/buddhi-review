"""Real ``gh`` comment ingest — the substrate-specific ``ingest()`` source.

Reads the open PR's reviewer comments (raw ``gh api`` payloads) and yields them
as :class:`buddhi_review.loop.Comment` objects / kernel ``RawItem`` streams. The
SDK exposes no work-queue, so this is the adapter's own substrate I/O (the
``ingest()`` verb).

Two seams keep it network-free under test:

* ``BUDDHI_REVIEW_COMMENTS_JSON`` — when set, its JSON payload (a list of raw
  comment dicts) is used verbatim and ``gh`` is never invoked.
* ``run=`` — the subprocess runner is injectable.

The per-line review comments (``pulls/<pr>/comments``), the top-level review
bodies (``pulls/<pr>/reviews``), and the general PR conversation comments
(``issues/<pr>/comments``) are all ingested; empty bodies are dropped.
``issues/<pr>/comments`` is the channel ``gh pr comment`` posts to — it carries
the ``"No issues found."`` clean sentinel that the round driver detects.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Callable, Iterable, List, Optional, Sequence

from buddhi.stage0.conditioning import RawItem

from buddhi_review.loop import Comment

COMMENTS_JSON_ENV = "BUDDHI_REVIEW_COMMENTS_JSON"
_GH_TIMEOUT = 60


def _default_run(argv: Sequence[str], *, cwd: Optional[str] = None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=_GH_TIMEOUT,
        stdin=subprocess.DEVNULL, cwd=cwd,
    )


def _comment_from_raw(raw: dict, *, from_issue_channel: Optional[bool] = None) -> Optional[Comment]:
    body = raw.get("body")
    if not isinstance(body, str) or not body.strip():
        return None
    cid = raw.get("id")
    if cid is None:
        return None
    user = raw.get("user") or {}
    login = user.get("login") if isinstance(user, dict) else None
    path = raw.get("path")
    diff_hunk = raw.get("diff_hunk")
    # pulls/<pr>/comments carries created_at; pulls/<pr>/reviews carries
    # submitted_at. Either drives the strictly-newer errored comeback.
    created = raw.get("created_at") or raw.get("submitted_at")
    # The loop passes from_issue_channel explicitly per endpoint. The seeded
    # (BUDDHI_REVIEW_COMMENTS_JSON) path has no endpoint, so infer it from the
    # payload: an issues/<pr>/comments object carries `issue_url` and lacks the
    # inline `path`/`diff_hunk`; review/inline objects never carry `issue_url`.
    if from_issue_channel is None:
        from_issue_channel = bool(raw.get("issue_url")) and not path and not diff_hunk
    return Comment(
        id=str(cid),
        text=body,
        source=str(login or "reviewer"),
        path=str(path) if isinstance(path, str) and path else None,
        diff_hunk=str(diff_hunk) if isinstance(diff_hunk, str) and diff_hunk else None,
        created_at=str(created) if isinstance(created, str) and created else None,
        from_issue_channel=from_issue_channel,
    )


def _parse_payload(text: str) -> List[dict]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, dict):  # tolerate a single-object payload
        data = [data]
    return [d for d in data if isinstance(d, dict)]


def fetch_comments(
    pr: str,
    *,
    repo: Optional[str] = None,
    cwd: Optional[str] = None,
    run: Callable[[Sequence[str], Optional[str]], "subprocess.CompletedProcess[str]"] = _default_run,
) -> List[Comment]:
    """Fetch the PR's reviewer comments + review bodies as ``Comment`` objects.

    ``repo`` is ``owner/repo``; when omitted, ``gh`` infers it from the cwd's
    ``origin`` (the ``{owner}/{repo}`` placeholder form). Network errors raise —
    the caller decides whether a failed ingest stops the run.
    """
    seeded = os.environ.get(COMMENTS_JSON_ENV)
    if seeded is not None:
        raws = _parse_payload(seeded)
        return [c for c in (map(_comment_from_raw, raws)) if c is not None]

    repo_path = repo or "{owner}/{repo}"
    comments: List[Comment] = []
    seen = set()
    for endpoint, from_issue_channel in (
        (f"repos/{repo_path}/pulls/{pr}/comments", False),
        (f"repos/{repo_path}/pulls/{pr}/reviews", False),
        # General PR conversation comments — the channel `gh pr comment` posts to.
        # The clean sentinel ("No issues found.") lands here, not on pulls/.../comments.
        # Tagged from_issue_channel so the round driver scans it for the sentinel
        # + signals only and never routes its chatter to the fixer (findings are
        # required to be INLINE per claude-code-review.yml).
        (f"repos/{repo_path}/issues/{pr}/comments", True),
    ):
        try:
            proc = run(["gh", "api", "--paginate", endpoint], cwd=cwd)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"failed to execute 'gh' CLI: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise RuntimeError(
                f"gh api {endpoint} failed (rc={proc.returncode}): {detail}"
            )
        # --paginate can emit one JSON array per page back-to-back; json.loads
        # only takes the first. Decode page-by-page with raw_decode.
        try:
            for raw in _decode_concatenated(proc.stdout):
                c = _comment_from_raw(raw, from_issue_channel=from_issue_channel)
                if c is not None and c.id not in seen:
                    seen.add(c.id)
                    comments.append(c)
        except ValueError as exc:
            raise RuntimeError(f"failed to parse gh api response for {endpoint}: {exc}") from exc
    return comments


def _decode_concatenated(text: str) -> Iterable[dict]:
    """Yield dicts from one or more concatenated JSON arrays/objects."""
    decoder = json.JSONDecoder()
    idx, n = 0, len(text)
    while idx < n:
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Failed to decode concatenated JSON at index {idx}: {exc}") from exc
        idx = end
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    yield item
        elif isinstance(obj, dict):
            yield obj


def ingest_source(
    pr: str,
    *,
    repo: Optional[str] = None,
    cwd: Optional[str] = None,
    run: Callable[[Sequence[str], Optional[str]], "subprocess.CompletedProcess[str]"] = _default_run,
) -> Callable[[], Iterable[RawItem]]:
    """An ``ingest_source`` for ``ReviewAdapter`` — the pre-classification raw
    stream for the kernel's ``ingest`` verb (classification enriches later)."""

    def _source() -> Iterable[RawItem]:
        return tuple(
            RawItem(id=c.id, payload=c.text, source=c.source)
            for c in fetch_comments(pr, repo=repo, cwd=cwd, run=run)
        )

    return _source
