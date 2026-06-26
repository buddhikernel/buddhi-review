"""Publish-clean /tmp filename helper for the free buddhi-review skill.

The single source of the on-disk filename FORMAT so the launcher's log, the
macOS click-to-tail helper, and the console answer file all carry the repo name
*and* the PR number. Without the repo name two repos that happen to share a PR
number write the same per-PR log and stomp each other; embedding ``<repo>`` makes
every artifact addressable per (repo, PR).

This is a tiny, dependency-free port of ONLY the three filename kinds the free
skill writes — ``log``, ``tailcmd``, ``answer`` — matching the reference
convention exactly:

    log:     buddhi-<repo>-PR<pr>.log
    tailcmd: review-tail-<repo>-PR<pr>.command
    answer:  review-answer-<repo>-PR<pr>.md

``<repo>`` is the part after the slash in ``owner/repo`` (or ``"local"`` when the
repo is unknown); the PR number is always last and ``PR``-prefixed.

By design it returns FILENAMES, not full paths, and resolves NO base directory:
each caller keeps its own base-dir logic (the launcher's ``/tmp`` symlink-hijack
guard; the notifier's ``BUDDHI_REVIEW_TMP`` seam), so this helper only fixes the
basename and never disturbs where the artifacts land.
"""
from __future__ import annotations


def repo_short(repo=None):
    """``owner/repo`` -> ``repo``; a slash-free value passes through unchanged;
    a falsy value -> ``"local"``. Idempotent, so a caller may pass either the full
    ``owner/repo`` or an already-short name and get the identical result."""
    if not repo:
        return "local"
    # Normalize backslashes to forward slashes for cross-platform safety
    normalized = str(repo).replace("\\", "/")
    stripped = normalized.strip("/")
    val = stripped.split("/")[-1] if stripped else "local"
    # Sanitize to prevent path traversal or invalid filename characters
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in val)
    return safe if safe else "local"


def sanitize_pr(pr):
    """Strip non-digit characters from a PR identifier (CWE-22 guard).

    PR numbers are always positive integers; any traversal sequence ('../../') or
    unexpected string becomes empty, which callers treat as "no PR"."""
    if pr is None:
        return ""
    return "".join(c for c in str(pr) if c.isdigit())


def log_name(pr, repo=None):
    """The per-PR review-loop log filename: ``buddhi-<repo>-PR<pr>.log``."""
    return f"buddhi-{repo_short(repo)}-PR{sanitize_pr(pr)}.log"


def tailcmd_name(pr, repo=None):
    """The macOS click-to-tail helper filename: ``review-tail-<repo>-PR<pr>.command``."""
    return f"review-tail-{repo_short(repo)}-PR{sanitize_pr(pr)}.command"


def answer_name(pr, repo=None):
    """The console answer-file filename, per PR: ``review-answer-<repo>-PR<pr>.md``.

    This is the canonical per-PR name. The free console rail can have more than one
    ask pending at once, so the notifier keys each ask off :func:`answer_name_for_ask`
    (this PR-keyed stem plus the ask id); this bare form is that stem's building block."""
    return f"review-answer-{repo_short(repo)}-PR{sanitize_pr(pr)}.md"


def sanitize_ask_id(ask_id):
    """Coerce an ask id to a filename-safe token: alphanumerics and ``-``/``_`` are
    kept, every other character becomes ``_``. Empty/None -> ``""``."""
    val = str(ask_id) if ask_id is not None else ""
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in val)


def answer_name_for_ask(ask_id, pr=None, repo=None):
    """The console answer filename for ONE ask: ``review-answer-<repo>-PR<pr>-<ask>.md``.

    The free console escalation rail can have several asks pending in a single round
    (one per failed-fix comment, plus the fixed-id ``test-gate``), so each ask needs
    its own file; the sanitized ask id keeps them apart, while ``<repo>`` and ``<pr>``
    keep two loops (different repo and/or PR) from colliding on a shared id. When no
    PR is known (a decision-only / self-check fallback that owns no PR), the PR
    segment is omitted so a stable filename is still produced — never a crash."""
    rs = repo_short(repo)
    safe = sanitize_ask_id(ask_id) or "ask"
    safe_pr = sanitize_pr(pr)
    pr_seg = f"-PR{safe_pr}" if safe_pr else ""
    return f"review-answer-{rs}{pr_seg}-{safe}.md"
