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
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence

from buddhi.stage0.conditioning import RawItem

from buddhi_review.loop import Comment

COMMENTS_JSON_ENV = "BUDDHI_REVIEW_COMMENTS_JSON"
# Reactions seam — a JSON list of raw reaction dicts (same shape gh returns)
# short-circuits the gh call, mirroring COMMENTS_JSON_ENV.
REACTIONS_JSON_ENV = "BUDDHI_REVIEW_REACTIONS_JSON"
# Review-threads seam — a JSON list of raw ``reviewThreads`` node dicts (same
# shape GitHub's GraphQL returns) short-circuits the gh call, like the two above.
THREADS_JSON_ENV = "BUDDHI_REVIEW_THREADS_JSON"
_GH_TIMEOUT = 60


@dataclass(frozen=True)
class Reaction:
    """One reaction on the PR body (issues/<pr>/reactions). ``content`` is the
    GitHub reaction name (``+1``, ``eyes``, …); ``source`` is the reacting login.
    Only bot-authored reactions are ever constructed — a human's +1 must never
    read as a reviewer sign-off."""
    id: str
    content: str
    source: str


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
    # updated_at (when the payload carries it) lets an EDITED comment prove an
    # errored-reviewer comeback by its edit time; absent → None.
    updated = raw.get("updated_at")
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
        updated_at=str(updated) if isinstance(updated, str) and updated else None,
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
    run: Callable[..., "subprocess.CompletedProcess[str]"] = _default_run,
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


def fetch_pr_diff_lines(
    pr: str,
    *,
    repo: Optional[str] = None,
    cwd: Optional[str] = None,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = _default_run,
) -> Optional[int]:
    """Best-effort PR diff size (additions + deletions) via ``gh pr view``, used to
    auto-size the review→fix round budget. Returns ``None`` on ANY failure —
    missing ``gh``, a non-zero exit, unparseable JSON, or missing/ill-typed
    counts — so the caller falls back to the default budget (fail-soft)."""
    argv = ["gh", "pr", "view", str(pr), "--json", "additions,deletions"]
    if repo:
        argv += ["-R", repo]
    try:
        proc = run(argv, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    try:
        data = json.loads((getattr(proc, "stdout", "") or "").strip() or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    additions = data.get("additions")
    deletions = data.get("deletions")
    if not isinstance(additions, int) or not isinstance(deletions, int):
        return None
    return additions + deletions


def _reaction_from_raw(raw: dict) -> Optional[Reaction]:
    """Map one raw reaction dict → :class:`Reaction`, or None when it is
    malformed OR not bot-authored. GitHub reserves the ``[bot]`` login suffix for
    Apps (a human cannot register it), and some App accounts report
    ``type == "User"`` (verified for the Codex connector), so the suffix is the
    reliable bot signal; ``type == "Bot"`` is accepted as a secondary one. Every
    non-bot reaction is dropped here so a human's +1 can never reach the fold."""
    rid = raw.get("id")
    content = raw.get("content")
    if rid is None or not isinstance(content, str) or not content:
        return None
    user = raw.get("user") or {}
    if not isinstance(user, dict):
        return None
    login = str(user.get("login") or "")
    is_bot = login.endswith("[bot]") or user.get("type") == "Bot"
    if not login or not is_bot:
        return None
    return Reaction(id=str(rid), content=content, source=login)


def fetch_reactions(
    pr: str,
    *,
    repo: Optional[str] = None,
    cwd: Optional[str] = None,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = _default_run,
) -> List[Reaction]:
    """Fetch the reactions on the PR body (``issues/<pr>/reactions``) as
    :class:`Reaction` objects, keeping only bot-authored ones. A Codex-style
    reviewer signals "reviewed, no issues" with a bare ``+1`` reaction and no
    comment, so this is the only place that signal is observable. Same two seams
    as :func:`fetch_comments`: ``BUDDHI_REVIEW_REACTIONS_JSON`` and an injectable
    ``run``. Network errors raise — the caller decides how to degrade."""
    seeded = os.environ.get(REACTIONS_JSON_ENV)
    if seeded is not None:
        raws = _parse_payload(seeded)
        return [r for r in map(_reaction_from_raw, raws) if r is not None]

    repo_path = repo or "{owner}/{repo}"
    endpoint = f"repos/{repo_path}/issues/{pr}/reactions"
    try:
        proc = run(["gh", "api", "--paginate", endpoint], cwd=cwd)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"failed to execute 'gh' CLI: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise RuntimeError(
            f"gh api {endpoint} failed (rc={proc.returncode}): {detail}"
        )
    reactions: List[Reaction] = []
    seen = set()
    try:
        for raw in _decode_concatenated(proc.stdout):
            r = _reaction_from_raw(raw)
            if r is not None and r.id not in seen:
                seen.add(r.id)
                reactions.append(r)
    except ValueError as exc:
        raise RuntimeError(
            f"failed to parse gh api response for {endpoint}: {exc}") from exc
    return reactions


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


@dataclass(frozen=True)
class ReviewThread:
    """One GitHub review thread on the PR. ``id`` is the GraphQL node id needed
    to resolve it; ``is_resolved`` is GitHub's own resolved flag; and
    ``root_comment_id`` is the databaseId (stringified) of the thread's FIRST
    comment — the key that ties a thread back to a :class:`Comment` the loop
    ingested and handled (a :class:`Comment` built from ``pulls/<pr>/comments``
    carries the same stringified databaseId as ``id``). ``root_comment_id`` is
    None when the thread carries no comments (never expected in practice —
    tolerated so a malformed node can never crash the read)."""
    id: str
    is_resolved: bool
    root_comment_id: Optional[str]


def _review_thread_from_raw(raw: dict) -> Optional[ReviewThread]:
    """Map one raw ``reviewThreads`` node → :class:`ReviewThread`, or None when
    it has no usable GraphQL node id."""
    tid = raw.get("id")
    if not isinstance(tid, str) or not tid:
        return None
    comments = raw.get("comments")
    nodes = comments.get("nodes") if isinstance(comments, dict) else None
    root: Optional[str] = None
    if isinstance(nodes, list) and nodes:
        first = nodes[0]
        if isinstance(first, dict) and first.get("databaseId") is not None:
            root = str(first["databaseId"])
    return ReviewThread(id=tid, is_resolved=bool(raw.get("isResolved")),
                        root_comment_id=root)


_REVIEW_THREADS_QUERY = """query($owner: String!, $name: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          comments(first: 1) { nodes { databaseId } }
        }
      }
    }
  }
}"""

_RESOLVE_REVIEW_THREAD_MUTATION = """mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { isResolved }
  }
}"""


def fetch_review_threads(
    pr: str,
    *,
    repo: Optional[str] = None,
    cwd: Optional[str] = None,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = _default_run,
) -> List[ReviewThread]:
    """Fetch every review thread on the PR via ``gh api graphql``, paginating past
    GitHub's 100-node page limit. Returns one :class:`ReviewThread` per thread so
    a caller can tell which are still unresolved and match each back to the
    comment that opened it. Same two seams as :func:`fetch_reactions`:
    ``BUDDHI_REVIEW_THREADS_JSON`` (a JSON list of raw thread nodes) short-circuits
    the network, and ``run`` is injectable. A ``gh`` / GraphQL / parse failure
    RAISES ``RuntimeError`` — the caller decides how to degrade (the pre-merge
    thread gate fails soft on it, never wedging a mergeable PR). ``reviewThreads``
    are the inline-comment threads only; the general PR conversation is not a
    thread and never appears here."""
    seeded = os.environ.get(THREADS_JSON_ENV)
    if seeded is not None:
        raws = _parse_payload(seeded)
        return [t for t in map(_review_thread_from_raw, raws) if t is not None]

    if not repo or "/" not in repo:
        # GraphQL needs explicit owner/name variables — gh does not substitute the
        # REST ``{owner}/{repo}`` placeholder into GraphQL args. Signal the caller.
        raise RuntimeError("fetch_review_threads requires an explicit owner/repo")
    owner, name = repo.split("/", 1)

    threads: List[ReviewThread] = []
    cursor: Optional[str] = None
    while True:
        argv = ["gh", "api", "graphql",
                "-f", f"query={_REVIEW_THREADS_QUERY}",
                "-f", f"owner={owner}", "-f", f"name={name}",
                "-F", f"pr={pr}"]
        if cursor:
            argv += ["-f", f"cursor={cursor}"]
        try:
            proc = run(argv, cwd=cwd)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"failed to execute 'gh' CLI: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise RuntimeError(
                f"gh api graphql reviewThreads failed (rc={proc.returncode}): {detail}")
        try:
            data = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(
                f"failed to parse gh api graphql reviewThreads response: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(
                f"unexpected JSON type from gh api graphql reviewThreads: {type(data).__name__}")
        if data and data.get("errors"):
            raise RuntimeError(
                f"gh api graphql reviewThreads returned errors: {data['errors']}")
        rt = ((((data or {}).get("data") or {}).get("repository") or {})
              .get("pullRequest") or {}).get("reviewThreads") or {}
        for node in (rt.get("nodes") or []):
            if isinstance(node, dict):
                t = _review_thread_from_raw(node)
                if t is not None:
                    threads.append(t)
        page_info = rt.get("pageInfo") or {}
        if page_info.get("hasNextPage") and page_info.get("endCursor"):
            cursor = page_info["endCursor"]
        else:
            break
    return threads


def resolve_review_thread(
    thread_id: str,
    *,
    cwd: Optional[str] = None,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = _default_run,
) -> bool:
    """Mark ONE review thread resolved via the ``resolveReviewThread`` GraphQL
    mutation. Returns True on success, False on any ``gh`` error — best-effort by
    design: the pre-merge gate RE-QUERIES thread state afterwards and treats that
    re-query, not this return value, as the source of truth. Under the
    ``BUDDHI_REVIEW_THREADS_JSON`` seam there is no live GitHub to mutate, so the
    seeded reader payload stays authoritative and this is a no-op success."""
    if os.environ.get(THREADS_JSON_ENV) is not None:
        return True
    argv = ["gh", "api", "graphql",
            "-f", f"query={_RESOLVE_REVIEW_THREAD_MUTATION}",
            "-f", f"threadId={thread_id}"]
    try:
        proc = run(argv, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def ingest_source(
    pr: str,
    *,
    repo: Optional[str] = None,
    cwd: Optional[str] = None,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = _default_run,
) -> Callable[[], Iterable[RawItem]]:
    """An ``ingest_source`` for ``ReviewAdapter`` — the pre-classification raw
    stream for the kernel's ``ingest`` verb (classification enriches later)."""

    def _source() -> Iterable[RawItem]:
        return tuple(
            RawItem(id=c.id, payload=c.text, source=c.source)
            for c in fetch_comments(pr, repo=repo, cwd=cwd, run=run)
        )

    return _source
