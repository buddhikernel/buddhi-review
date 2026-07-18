"""The pre-merge review-thread gate.

A clean exit is not merge-ready until GitHub confirms zero unresolved review
threads. The gate first resolves the threads THIS run genuinely handled (a fix
landed, or the finding was dismissed as outdated / invalid / already-fixed /
already-converged), then re-confirms; a thread the loop never touched — a human's
still-open thread — or one it could not finish — a fix the verify pass rejected —
keeps the PR un-merge-ready and is NEVER auto-resolved.

Three layers, all network-free on a fake clock:
  1. The ``gh_ingest`` reader/resolver (``fetch_review_threads`` +
     ``resolve_review_thread``): env-seam, injected ``run``, pagination, errors.
  2. ``RoundDriver._thread_gate_ok`` directly — seeded actions + a stateful
     ``FakeThreads`` — for every branch (own / foreign / fail-soft / re-query).
  3. End-to-end through ``RoundDriver.run`` — the gate's placement AFTER the
     SAFETY gate and its effect on the merge / hand-back.
"""
import json
import subprocess

import pytest

from buddhi_review import gh_ingest, round_driver
from buddhi_review.actuators import ActionResult
from buddhi_review.loop import Comment
from test_round_driver import CLAUDE_ONLY, FakeThreads, make_driver


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _thread(tid, root, resolved=False):
    return gh_ingest.ReviewThread(id=tid, is_resolved=resolved, root_comment_id=root)


def _graphql_page(nodes, *, has_next=False, cursor=None):
    """A ``gh api graphql`` reviewThreads response page as gh would emit it."""
    return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": nodes,
    }}}}})


def _node(tid, db_id, resolved=False, replies=()):
    nodes = ([{"databaseId": db_id}] if db_id is not None else [])
    nodes += [{"databaseId": r} for r in replies]
    return {"id": tid, "isResolved": resolved, "comments": {"nodes": nodes}}


def _driver_with_gate(actions, ft, *, inline_ids=None, **kw):
    """A driver whose thread-gate seams are ``ft`` and whose ``actions`` +
    ``_handled_inline_ids`` are seeded — for calling ``_thread_gate_ok`` in
    isolation. ``inline_ids`` defaults to every resolving action's id (i.e. the
    seeded actions are treated as inline review comments); pass it explicitly to
    model a handled comment that is NOT a thread root (a review-body id)."""
    driver, _clock, _gh = make_driver(
        [], cfg=CLAUDE_ONLY, threads_fetch=ft.fetch, resolve_thread=ft.resolve, **kw)
    driver.actions = list(actions)
    if inline_ids is None:
        inline_ids = {a.comment_id for a in actions
                      if a.final in round_driver._RESOLVED_FINALS}
    driver._handled_inline_ids = set(inline_ids)
    return driver


def _fixed(comment_id):
    return ActionResult(comment_id, "fix", "fixed", "ok")


# ===========================================================================
# 1. gh_ingest reader / resolver
# ===========================================================================

def test_reader_env_seam_parses_raw_nodes(monkeypatch):
    payload = [_node("PRRT_1", 111, resolved=False),
               _node("PRRT_2", 222, resolved=True),
               _node("PRRT_3", None)]  # a thread with no comments → root None
    monkeypatch.setenv(gh_ingest.THREADS_JSON_ENV, json.dumps(payload))
    threads = gh_ingest.fetch_review_threads("7", repo="o/r")
    assert [(t.id, t.is_resolved, t.root_comment_id) for t in threads] == [
        ("PRRT_1", False, "111"),
        ("PRRT_2", True, "222"),
        ("PRRT_3", False, None),
    ]
    # comment_ids exposes every comment; a single-comment thread holds just the root.
    assert [t.comment_ids for t in threads] == [
        frozenset({"111"}), frozenset({"222"}), frozenset(),
    ]


def test_reader_exposes_all_comment_ids_including_replies(monkeypatch):
    # A resolved thread with a root (222) and two replies (333, 444): comment_ids
    # holds every id so a caller can recognise a REPLY, not just the root. The root
    # stays first for the thread gate's back-compat matching.
    payload = [_node("PRRT_2", 222, resolved=True, replies=[333, 444])]
    monkeypatch.setenv(gh_ingest.THREADS_JSON_ENV, json.dumps(payload))
    (t,) = gh_ingest.fetch_review_threads("7", repo="o/r")
    assert t.root_comment_id == "222"
    assert t.comment_ids == frozenset({"222", "333", "444"})


def test_reader_paginates_across_pages():
    # gh returns page 1 (hasNextPage) then page 2; the reader must concatenate.
    pages = [
        _graphql_page([_node("PRRT_1", 111)], has_next=True, cursor="C1"),
        _graphql_page([_node("PRRT_2", 222, resolved=True)], has_next=False),
    ]
    calls = []

    def run(argv, *, cwd=None):
        calls.append(argv)
        # page 2 is the call that carries the cursor
        idx = 1 if any(a.startswith("cursor=") for a in argv) else 0
        return subprocess.CompletedProcess(argv, 0, stdout=pages[idx], stderr="")

    threads = gh_ingest.fetch_review_threads("7", repo="o/r", run=run)
    assert [t.id for t in threads] == ["PRRT_1", "PRRT_2"]
    assert len(calls) == 2                       # exactly two pages fetched
    assert any(a == "cursor=C1" for a in calls[1])  # the cursor was threaded through


def test_reader_raises_on_gh_error():
    def run(argv, *, cwd=None):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
    with pytest.raises(RuntimeError):
        gh_ingest.fetch_review_threads("7", repo="o/r", run=run)


def test_reader_raises_on_spawn_failure():
    def run(argv, *, cwd=None):
        raise OSError("gh not found")
    with pytest.raises(RuntimeError):
        gh_ingest.fetch_review_threads("7", repo="o/r", run=run)


def test_reader_raises_on_unparseable_json():
    def run(argv, *, cwd=None):
        return subprocess.CompletedProcess(argv, 0, stdout="not json", stderr="")
    with pytest.raises(RuntimeError):
        gh_ingest.fetch_review_threads("7", repo="o/r", run=run)


def test_reader_requires_explicit_repo():
    # GraphQL needs owner/name variables — the REST {owner}/{repo} placeholder is
    # not substituted, so a missing/degenerate repo raises (caller fails soft).
    with pytest.raises(RuntimeError):
        gh_ingest.fetch_review_threads("7", repo=None,
                                       run=lambda *a, **k: pytest.fail("should not spawn"))
    with pytest.raises(RuntimeError):
        gh_ingest.fetch_review_threads("7", repo="nolslash",
                                       run=lambda *a, **k: pytest.fail("should not spawn"))


def test_resolver_returns_true_on_success_false_on_error():
    ok_calls = []

    def ok(argv, *, cwd=None):
        ok_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    assert gh_ingest.resolve_review_thread("PRRT_1", run=ok) is True
    assert any(a.startswith("threadId=PRRT_1") for a in ok_calls[0])

    def bad(argv, *, cwd=None):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="nope")
    assert gh_ingest.resolve_review_thread("PRRT_1", run=bad) is False

    def boom(argv, *, cwd=None):
        raise OSError("gh not found")
    assert gh_ingest.resolve_review_thread("PRRT_1", run=boom) is False


def test_resolver_is_noop_success_under_env_seam(monkeypatch):
    monkeypatch.setenv(gh_ingest.THREADS_JSON_ENV, "[]")
    # Under the seam there is no live GitHub to mutate — never spawn gh.
    assert gh_ingest.resolve_review_thread(
        "PRRT_1", run=lambda *a, **k: pytest.fail("should not spawn")) is True


# ===========================================================================
# 2. _thread_gate_ok — every branch, in isolation
# ===========================================================================

def test_gate_passes_when_no_unresolved_threads():
    ft = FakeThreads([_thread("PRRT_1", "c1", resolved=True)])
    driver = _driver_with_gate([_fixed("c1")], ft)
    assert driver._thread_gate_ok() is True
    assert ft.resolved == []             # already resolved → nothing to do


def test_gate_resolves_own_thread_then_passes():
    # (b) the round's OWN handled thread is resolved, then the re-query is clean.
    ft = FakeThreads([_thread("PRRT_1", "c1", resolved=False)])
    driver = _driver_with_gate([_fixed("c1")], ft)
    assert driver._thread_gate_ok() is True
    assert ft.resolved == ["PRRT_1"]     # resolved exactly the run's own thread
    assert ft.fetches == 2               # first read + one confirming re-query


def test_gate_blocks_and_never_resolves_foreign_human_thread():
    # (a)+(d) a thread the loop never touched (a human's) blocks the merge AND is
    # never auto-resolved — the whole point of the gate.
    ft = FakeThreads([_thread("PRRT_human", "human99", resolved=False)])
    driver = _driver_with_gate([_fixed("c1")], ft)   # loop handled c1, not human99
    assert driver._thread_gate_ok() is False
    assert ft.resolved == []             # the human thread is NEVER resolved
    assert ft.fetches == 1               # blocked on the first read; no re-query


def test_gate_blocks_on_foreign_even_while_resolving_own():
    # A mix: the own thread is resolved, but a foreign one still blocks.
    ft = FakeThreads([_thread("PRRT_own", "c1", resolved=False),
                      _thread("PRRT_human", "human99", resolved=False)])
    driver = _driver_with_gate([_fixed("c1")], ft)
    assert driver._thread_gate_ok() is False
    assert ft.resolved == ["PRRT_own"]   # own resolved; foreign left untouched
    assert "human99" not in ft.resolved


@pytest.mark.parametrize("final", ["rejected", "escalated", "deferred"])
def test_gate_never_resolves_a_non_finished_disposition(final):
    # A rejected fix / escalation / deferral is NOT a genuine "done" — its thread
    # must not be resolved, and it keeps the PR un-merge-ready.
    ft = FakeThreads([_thread("PRRT_1", "c1", resolved=False)])
    driver = _driver_with_gate([ActionResult("c1", "fix", final, "x")], ft)
    assert driver._thread_gate_ok() is False
    assert ft.resolved == []


def test_gate_blocks_when_resolve_mutation_silently_fails():
    # We tried to resolve our own thread but the mutation failed; the re-query
    # still shows it unresolved → block honestly (do not merge on a phantom).
    ft = FakeThreads([_thread("PRRT_1", "c1", resolved=False)])
    ft.resolve_fail = True
    driver = _driver_with_gate([_fixed("c1")], ft)
    assert driver._thread_gate_ok() is False
    assert ft.resolved == ["PRRT_1"]     # attempted, but it did not land
    assert ft.fetches == 2               # first read + the confirming re-query


def test_gate_ignores_non_inline_handled_id_colliding_with_thread_root():
    # A review-BODY comment the loop handled carries an id from a DIFFERENT GitHub
    # namespace than review-thread roots. Even if that id numerically collides with
    # a human inline thread's root, it must NOT resolve that thread — the handled
    # set is scoped to inline ids, so the human thread stays foreign and blocks.
    ft = FakeThreads([_thread("PRRT_human", "2500000000", resolved=False)])
    # The action is handled (fixed) but its id is a review-body id → NOT inline.
    driver = _driver_with_gate(
        [_fixed("2500000000")], ft, inline_ids=set())   # nothing inline this run
    assert driver._thread_gate_ok() is False
    assert ft.resolved == []                            # the human thread untouched


def test_gate_fails_soft_on_reader_error():
    # (c) a transient gh / GraphQL read error is NOT an unresolved thread — proceed.
    ft = FakeThreads([_thread("PRRT_human", "human99", resolved=False)])
    ft.fail = True
    driver = _driver_with_gate([], ft)
    assert driver._thread_gate_ok() is True
    assert ft.resolved == []


def test_gate_blocks_on_missing_repo_config_error():
    # A missing/invalid repo raises RepoNotConfiguredError from
    # gh_ingest.fetch_review_threads — that is a non-transient config error, NOT
    # a transient gh blip, so the gate must block (return False) rather than fail-soft.
    class MissingRepoFetch:
        resolved = []
        def fetch(self, pr, repo=None, cwd=None):
            raise gh_ingest.RepoNotConfiguredError(
                "fetch_review_threads requires an explicit owner/repo")
        def resolve(self, thread_id, cwd=None):
            return True

    driver = _driver_with_gate([], MissingRepoFetch())
    assert driver._thread_gate_ok() is False
    assert driver._thread_gate_block_reason == (
        "could not check review threads (no owner/repo configured)")


def test_gate_fails_soft_on_requery_error():
    # The first read succeeds and only own threads are unresolved; a transient
    # error at the confirming re-query must not wedge the (already-resolved) PR.
    class RequeryBoom(FakeThreads):
        def fetch(self, pr, repo=None, cwd=None):
            self.fetches += 1
            if self.fetches >= 2:            # fail ONLY on the re-query
                raise RuntimeError("graphql blip")
            return super().fetch(pr, repo=repo, cwd=cwd)

    ft = RequeryBoom([_thread("PRRT_1", "c1", resolved=False)])
    driver = _driver_with_gate([_fixed("c1")], ft)
    assert driver._thread_gate_ok() is True
    assert ft.resolved == ["PRRT_1"]         # own thread was still resolved


def test_gate_blocks_on_resolve_fail_plus_requery_error():
    # Compound failure: the resolve mutation reported failure (returned False) AND
    # the confirming re-query errors. The thread's true state is unconfirmed, so the
    # gate must NOT fail-open into a merge — it blocks and hands back.
    class RequeryBoom(FakeThreads):
        def fetch(self, pr, repo=None, cwd=None):
            self.fetches += 1
            if self.fetches >= 2:            # fail ONLY on the re-query
                raise RuntimeError("graphql blip")
            return super().fetch(pr, repo=repo, cwd=cwd)

    ft = RequeryBoom([_thread("PRRT_1", "c1", resolved=False)])
    ft.resolve_fail = True                   # resolve() returns False (silent fail)
    driver = _driver_with_gate([_fixed("c1")], ft)
    assert driver._thread_gate_ok() is False
    assert ft.resolved == ["PRRT_1"]         # attempted, but it reported failure


def test_gate_matches_all_resolved_finals():
    # Every genuine-done disposition resolves its own thread.
    for final in ("fixed", "skipped", "skipped-invalid",
                  "skipped-already-fixed", "already-resolved"):
        ft = FakeThreads([_thread("PRRT_1", "c1", resolved=False)])
        driver = _driver_with_gate([ActionResult("c1", "fix", final, "")], ft)
        assert driver._thread_gate_ok() is True, final
        assert ft.resolved == ["PRRT_1"], final


# ===========================================================================
# 3. End-to-end through RoundDriver.run
# ===========================================================================

def test_e2e_own_thread_resolved_then_merges():
    # (b) a reviewer's INVALID inline comment is dismissed (final "skipped"); its
    # thread is the run's own, gets resolved, and the PR merges. The comment is
    # path-anchored (an inline comment — the only kind that is a thread root).
    timeline = [(0, Comment(id="c1", text="this line is off",
                            source="claude[bot]", path="app.py"))]
    ft = FakeThreads([_thread("PRRT_1", "c1", resolved=False)])
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, auto_merge=True,
        threads_fetch=ft.fetch, resolve_thread=ft.resolve)
    outcome = driver.run()
    assert outcome.status == "clean"
    assert outcome.merged is True
    assert gh.matching("gh", "merge", "--squash")
    assert ft.resolved == ["PRRT_1"]


def test_e2e_unresolved_human_thread_blocks_and_hands_back(capsys):
    # (a) a genuine clean approval (so the SAFETY gate passes) but a human review
    # thread is still open → NOT merge-ready; hand back with the honest reason.
    timeline = [(0, Comment(id="s", text="No issues found.", source="claude[bot]"))]
    ft = FakeThreads([_thread("PRRT_human", "human99", resolved=False)])
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, auto_merge=True,
        threads_fetch=ft.fetch, resolve_thread=ft.resolve)
    outcome = driver.run()
    assert outcome.status == "clean"
    assert outcome.merged is False
    assert gh.matching("gh", "merge", "--squash") == []   # never merged
    assert ft.resolved == []                              # human thread untouched
    assert driver._thread_gate_block_reason == "a review thread is still unresolved"
    # the honest hand-back reason surfaces this, not "ready to merge"
    assert driver._handback_caution_reason(outcome) == "a review thread is still unresolved"


def test_e2e_thread_gate_runs_only_after_safety_gate():
    # (e) a PR NObody reviewed blocks at the SAFETY gate; the thread gate must
    # never even be consulted (else it could merge an unreviewed PR).
    ft = FakeThreads([_thread("PRRT_1", "c1", resolved=False)])
    driver, clock, gh = make_driver(
        [], cfg=CLAUDE_ONLY, auto_merge=True,
        threads_fetch=ft.fetch, resolve_thread=ft.resolve)
    outcome = driver.run()
    assert outcome.status == "clean" and outcome.merged is False
    assert ft.fetches == 0               # the thread gate was never reached
    assert ft.resolved == []


def test_e2e_reader_error_fails_soft_and_merges():
    # (c) a real review happened; the thread reader errors transiently → the gate
    # fails soft and the merge still proceeds (a blip must not wedge a good PR).
    timeline = [(0, Comment(id="s", text="No issues found.", source="claude[bot]"))]
    ft = FakeThreads([_thread("PRRT_1", "c1", resolved=False)])
    ft.fail = True
    driver, clock, gh = make_driver(
        timeline, cfg=CLAUDE_ONLY, auto_merge=True,
        threads_fetch=ft.fetch, resolve_thread=ft.resolve)
    outcome = driver.run()
    assert outcome.merged is True
    assert gh.matching("gh", "merge", "--squash")
