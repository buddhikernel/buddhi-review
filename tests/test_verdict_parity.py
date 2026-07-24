"""The verdict-parity suite — the build-spec §5 acceptance bar.

Each fixture in ``parity_fixtures/`` is a frozen past PR (sanitized): the recorded
reviewer comments + the recorded classifier response per actionable comment + the
EXPECTED verdict. This suite replays every fixture through the REAL
classify/decide path and asserts the three parity guarantees:

  (1) **Same per-comment label** — each recorded ``classifier_response`` is run
      through the real tolerant parser (:func:`classify_comment`); the parsed
      label must equal the fixture's expected label. The recorded responses vary
      in shape on purpose (fenced JSON, plain JSON, JSON-behind-preamble, the
      legacy pipe form, label-less garbage) so the parser does real work.

  (2) **Same terminal disposition + per-comment disposition** — the fixture is
      driven through the real :class:`RoundDriver` round-loop (fake clock, no
      network, no ``claude``, no live ``gh``). The run's ``RunOutcome`` collapses
      to the §5 3-way via :func:`run_terminal_disposition`
      (``merge`` / ``escalate-to-human`` / ``stop``), and each comment's
      ``ActionResult.final`` normalizes to ``fixed`` / ``skipped-invalid`` /
      ``escalated``.

  (3) **Same set of autonomous actions** — every ``⚙ [auto]`` action the loop
      takes is captured as a ``(action, status)`` pair and compared as a SET, NOT
      byte-identical text. The driver runs with ``push=False`` so the graded set
      is the verdict-level trail (squash-merge + exclusion); the commit/push and
      test-gate mechanics are exercised in ``test_round_driver`` /
      ``test_commit_push`` and are deliberately out of the parity grading scope.

Explicitly NOT asserted: byte-identical logs, fix-commit prose, model wording.

Harness hygiene (#294): the replay never shells out — the comment fetch, the
fixer, the gh/git runner, the clock, and the escalation answer are all injected
fakes, and nothing writes to a shared ``/tmp`` path.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from buddhi_review import detectors
from buddhi_review.actuators import FixDispatch
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.classify import classify_comment
from buddhi_review.fix_apply import FixOutcome
from buddhi_review.loop import Comment
from buddhi_review.round_driver import (
    RoundDriver,
    RoundTimes,
    run_terminal_disposition,
)
from buddhi_review.seams import ConsoleEscalation

_FIXTURE_DIR = Path(__file__).resolve().parent / "parity_fixtures"
_FIXTURES = sorted(_FIXTURE_DIR.glob("*.json"))

# Comments in round R become visible to the fetch at this clock offset. 90s per
# round mirrors the proven multi-round timing in test_round_driver
# (test_fix_flow_pushes_then_next_round_goes_clean): round 1 closes ~t=60 and a
# round-2 contribution at t=90 lands inside round 2's polling window.
_ROUND_SPACING = 90

# ActionResult.final -> the §5 per-comment vocabulary. A kernel "skip" (an
# OUTDATED / INVALID comment the loop correctly declined to act on) and a fixer
# "skipped-invalid" / "skipped-already-fixed" (a genuine validity-judgment SKIP)
# all mean "not acted on, nothing to do" in the §5 3-way. A fixer "rejected" (a
# fix-verify REJECT) escalates for a human, so it collapses to "escalated".
_FINAL_TO_PARITY = {
    "fixed": "fixed",
    "skipped-invalid": "skipped-invalid",
    "skipped-already-fixed": "skipped-invalid",
    "skipped": "skipped-invalid",
    "rejected": "escalated",
    "escalated": "escalated",
}

# Publish-gate strings that must never ride a shipped fixture. The source-surface
# OSS-purity guard (tests/test_oss_purity.py) scans buddhi_review/, tests/*.py and
# the root/docs markdown but never these JSON fixtures, so this list MUST mirror
# its terms (paid-product names + publish-gate strings) AND add the
# fixture-specific company/token shapes. The drift-guard test below fails if
# test_oss_purity ever grows a term this misses. (Matching here is SUBSTRING —
# deliberately stricter than the word-boundary scanner, so e.g. "mono" also
# rejects a fixture that says "monolith"; a fixture edit that trips gets a
# human look rather than a silent pass.)
_FORBIDDEN = (
    # test_oss_purity._FORBIDDEN — paid/internal product + limitation surface.
    "telegram", "autopilot", "cockpit", "self-heal",
    "auto-rebase", "--implementer-session", "keep this session open",
    "buddhi board", "work dashboard", "buddhi-board", "work-dashboard",
    # FREE-3 widened paid module / reserved-cell identifiers (mirror publish_gate)
    # + the private reference tree's internal label ("mono").
    "dashboard_server", "telegram_status_bot", "bot_quota", "oob_resolution",
    "review_loop", "dashboard_refresh", "app1", "app2", "oob", "mono",
    "stage0", "stage-0",
    # FREE-3 paid-monolith module + namespace surface (mirror _PAID_MODULE_NAMES).
    "buddhi_pro", "buddhi-pro", "buddhikernel_pro", "buddhikernel-pro",
    "buddhi_review_pro", "buddhi-review-pro",
    "dashboard_", "status_data", "status_ipc",
    "usage_cli", "usage_snapshot", "claude_usage", "loop_ledger",
    "parent_merge_watcher", "merge_conflict_resolver", "dispatch_bridge",
    "run_multi_repo", "_admin_log", "spawn-team",
    # test_oss_purity._PUBLISH_GATE — author path / owner handle / private registry / company handle.
    "/users/manasvi", "/users/", "manasvi", "m-s-21", "project-registry", "snab",
    # fixture-specific: credential-token shapes (company name sourced from
    # BUDDHI_TEST_COMPANY_NAME env so the literal doesn't ship in the public tree).
    *([os.environ["BUDDHI_TEST_COMPANY_NAME"].lower()] if os.environ.get("BUDDHI_TEST_COMPANY_NAME") else []),
    "ghp_", "github_pat_",
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _actionable(fixture: dict):
    """Yield (comment_dict) for every comment carrying a recorded classifier
    response — i.e. every comment the loop classifies (not a single-shot signal)."""
    for rnd in fixture["rounds"]:
        for c in rnd:
            if "classifier_response" in c:
                yield c


# ---------------------------------------------------------------------------
# Network-free fakes (mirrors test_round_driver's harness)
# ---------------------------------------------------------------------------

class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class _FakeNotifier:
    name = "console"

    def __init__(self) -> None:
        self.sent = []

    def startup_log(self) -> None:
        pass

    def send(self, ask) -> None:
        self.sent.append(ask)

    def read_answer(self, ask):
        return None

    def clear(self, ask) -> None:
        pass


# The constant local HEAD this harness reports for `git rev-parse HEAD`. Every
# review a fixture's reviewer posts anchors to it, so the F2 head-aware merge gate
# resolves a stable single-commit head (staleness is out of scope for parity).
_HEAD_SHA = "headsha00000000"
# Its committer date — the F2 freshness cutoff. Fixtures carry no comment
# timestamps, so sha-less anchoring never fires here; every reviewer is credited
# through its real per-commit review above.
_HEAD_TIME = "2026-01-01T00:00:00+00:00"


class _GhRecorder:
    """Answers every gh/git/test spawn with rc=0; a dirty `git status` so a
    commit path (unused here, push=False) would have something to add;
    `git rev-parse HEAD` → :data:`_HEAD_SHA` and `git merge-base --is-ancestor`
    → satisfied, so the F2 head-aware merge gate resolves the merged head."""

    def __call__(self, argv, *, cwd=None, timeout=None):
        argv = list(argv)
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "HEAD":
            return subprocess.CompletedProcess(argv, 0, stdout=_HEAD_SHA + "\n", stderr="")
        if argv[:3] == ["git", "show", "-s"]:
            # The head's committer date — F2's freshness cutoff for sha-less signals.
            return subprocess.CompletedProcess(
                argv, 0, stdout=_HEAD_TIME + "\n", stderr="")
        if argv[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        out = " M x.py\n" if argv[:3] == ["git", "status", "--porcelain"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


def _body_keyed_runner(fixture: dict):
    """The classify ``claude -p`` seam for the whole run: return the recorded
    classifier response for whichever comment body appears in the prompt. Longest
    body first so a short body can't shadow a more specific one."""
    responses = {c["body"]: c["classifier_response"] for c in _actionable(fixture)}
    bodies = sorted(responses, key=len, reverse=True)

    def runner(prompt: str) -> str:
        for body in bodies:
            if body in prompt:
                return responses[body]
        return ""

    return runner


def _fix_dispatch(fixture: dict) -> FixDispatch:
    """The faked fixer: map each comment id to its recorded FixOutcome status
    (default ``applied``). Only kernel-``fix`` comments ever reach it."""
    outcomes = {
        c["id"]: c.get("fix_outcome", "applied") for c in _actionable(fixture)
    }

    def dispatch(comment: Comment, result) -> FixOutcome:
        return FixOutcome(status=outcomes.get(comment.id, "applied"))

    return dispatch


def _answer_waiter(answer_mode: str):
    """Drive the escalation answer per fixture:
      * none         — no pending answer (clean / fix-only runs).
      * unanswered   — every delivered ask returns None → the loop hands over.
      * operator-stop — answer "3" (Stop) to a failed-fix ask → the loop stops.
    """
    if answer_mode == "unanswered":
        return lambda esc, **k: {"_": None}
    if answer_mode == "operator-stop":
        return lambda esc, **k: {
            a.id: "3"
            for a in esc.delivered
            if str(getattr(a, "id", "")).startswith("fix-")
        }
    return lambda esc, **k: {}


def _timeline(fixture: dict):
    """Build the (t_visible, Comment) timeline: round R's comments appear at
    t = R * _ROUND_SPACING."""
    timeline = []
    for r, rnd in enumerate(fixture["rounds"]):
        t = r * _ROUND_SPACING
        for c in rnd:
            timeline.append(
                (
                    t,
                    Comment(
                        id=c["id"],
                        text=c["body"],
                        source=c["login"],
                        path=c.get("path"),
                        diff_hunk=c.get("diff_hunk"),
                        created_at=c.get("created_at"),
                        from_issue_channel=c.get("from_issue_channel", False),
                    ),
                )
            )
    return timeline


def _drive(fixture: dict):
    """Drive a fixture through the real RoundDriver; return (outcome, auto_actions
    set). Network-free: fake clock/notifier/gh, injected fetch/fixer/answer."""
    clock = _FakeClock()
    timeline = _timeline(fixture)

    def fetch(pr, repo=None, cwd=None):
        return [c for t, c in timeline if t <= clock.t]

    # F2 head-aware gate: anchor each review to the commit it was ACTUALLY posted
    # against — memoised on first sighting rather than read at fetch time — so the
    # harness can observe invariant 4 instead of re-anchoring every comment onto
    # whatever the tip happens to be when the gate runs. (This harness holds the tip
    # constant, so the memo equals _HEAD_SHA today; it is correct-by-construction if
    # a fixture ever moves the head.)
    posted_against = {}

    def reviews_fetch(pr, repo=None, cwd=None):
        return [{"user": {"login": c.source},
                 "commit_id": posted_against.setdefault(c.id, _HEAD_SHA),
                 "body": c.text, "state": "COMMENTED"}
                for t, c in timeline
                if t <= clock.t and detectors.bot_for_login(c.source) is not None]

    auto_actions = set()

    def notice(action, detail="", *, status="do", hint=None, stream=None):
        auto_actions.add((action, status))
        return ""

    adapter = ReviewAdapter(escalation=ConsoleEscalation(notifier=_FakeNotifier()))
    driver = RoundDriver(
        fixture["pr"],
        repo=fixture["repo"],
        cwd="/nonexistent",
        cfg=fixture["config"],
        adapter=adapter,
        classify_runner=_body_keyed_runner(fixture),
        fix_dispatch=_fix_dispatch(fixture),
        fetch=fetch,
        reviews_fetch=reviews_fetch,
        inline_fetch=lambda pr, repo=None, cwd=None: [],
        # Network-free thread-gate seams: no threads → the pre-merge thread gate is
        # a silent no-op, so the parity grade stays the verdict-level auto-action
        # SET (merge + exclusion), free of gh/GraphQL spawns.
        threads_fetch=lambda pr, repo=None, cwd=None: [],
        resolve_thread=lambda thread_id, cwd=None: True,
        gh_run=_GhRecorder(),
        clock=clock,
        sleep=clock.sleep,
        notice=notice,
        times=RoundTimes(quiescence=60, poll_interval=30, min_bot_wait=420,
                         idle_timeout=900, max_wait_total=1800),
        auto_merge=fixture["auto_merge"],
        # push disabled: the parity grade is the verdict-level auto-action SET
        # (merge + exclusion), free of the fake-harness commit/push/test-gate
        # artifacts (those are pinned in test_round_driver / test_commit_push).
        push=False,
        answer_waiter=_answer_waiter(fixture["answer_mode"]),
    )
    return driver.run(), auto_actions


# ---------------------------------------------------------------------------
# (1) Per-comment classification label parity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", _FIXTURES, ids=lambda p: p.stem)
def test_per_comment_label_parity(path):
    """Each recorded classifier response parses to the fixture's expected label,
    through the real tolerant parser (JSON-first → pipe → priority scan →
    CLASSIFICATION_FAILED)."""
    fixture = _load(path)
    expected = fixture["expected"]["labels"]
    seen = {}
    for c in _actionable(fixture):
        got = classify_comment(
            c["body"],
            runner=lambda prompt, _r=c["classifier_response"]: _r,
            retries=1,  # matches the round loop's process_comments default
            path=c.get("path"),
            diff_hunk=c.get("diff_hunk"),
        )
        seen[c["id"]] = got.label
    assert seen == expected, f"{path.stem}: label mismatch"


# ---------------------------------------------------------------------------
# (2) + (3) Terminal disposition + per-comment disposition + auto-action SET
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", _FIXTURES, ids=lambda p: p.stem)
def test_run_verdict_parity(path):
    """Drive the whole fixture through the real round-loop and assert the §5 bar:
    terminal disposition, per-comment disposition, and the auto-action set."""
    fixture = _load(path)
    exp = fixture["expected"]
    outcome, auto_actions = _drive(fixture)

    # (2a) terminal disposition
    assert run_terminal_disposition(outcome) == exp["terminal_disposition"], (
        f"{path.stem}: terminal {outcome.status}/merged={outcome.merged} "
        f"!= {exp['terminal_disposition']}"
    )

    # (2b) per-comment disposition (normalized to the §5 3-way)
    per_comment = {
        a.comment_id: _FINAL_TO_PARITY.get(a.final, a.final) for a in outcome.actions
    }
    assert per_comment == exp["per_comment"], f"{path.stem}: per-comment mismatch"

    # (3) the SET of (action, status) auto-action pairs
    expected_actions = {tuple(pair) for pair in exp["auto_actions"]}
    assert auto_actions == expected_actions, f"{path.stem}: auto-action set mismatch"


# ---------------------------------------------------------------------------
# Fixture hygiene — the example set ships in a PUBLIC repo
# ---------------------------------------------------------------------------

def test_fixtures_exist():
    # The §5 example set is ~10 PRs; guard against an empty/missing dir.
    assert len(_FIXTURES) >= 10, "expected the frozen example set (~10 fixtures)"


@pytest.mark.parametrize("path", _FIXTURES, ids=lambda p: p.stem)
def test_fixture_is_publish_clean(path):
    """No author path, owner handle, private repo name, registry, paid-product
    name, or token may ride a shipped fixture."""
    lower = path.read_text(encoding="utf-8").lower()
    for term in _FORBIDDEN:
        assert term not in lower, f"{path.name}: publish-gate string '{term}' leaked"


def test_fixture_guard_covers_the_whole_tree_oss_purity_list():
    """The fixture guard must stay a SUPERSET of the whole-tree OSS-purity guard —
    that guard never scans these JSON fixtures, so a term it knows but this list
    forgets would let a paid-product name or author path reach the public repo
    through a fixture edit unflagged."""
    import test_oss_purity as ossp

    required = {t.lower() for t in (*ossp._FORBIDDEN, *ossp._PUBLISH_GATE)}
    missing = required - set(_FORBIDDEN)
    assert not missing, f"fixture publish-clean guard is missing {missing}"


@pytest.mark.parametrize("path", _FIXTURES, ids=lambda p: p.stem)
def test_fixture_covers_its_own_taxonomy(path):
    """Every expected label is one of the 7-label taxonomy, and every expected
    per-comment / terminal disposition is in the §5 vocabulary — a typo in a
    fixture must fail loudly rather than silently grade against nonsense."""
    from buddhi_review.classify import CLASSIFICATION_FAILED, LABELS

    fixture = _load(path)
    exp = fixture["expected"]
    valid_labels = set(LABELS) | {CLASSIFICATION_FAILED}
    for label in exp["labels"].values():
        assert label in valid_labels, f"{path.name}: bad label {label}"
    assert exp["terminal_disposition"] in ("merge", "escalate-to-human", "stop")
    for d in exp["per_comment"].values():
        assert d in ("fixed", "skipped-invalid", "escalated"), f"{path.name}: {d}"
    # the actionable-comment set and the expected-labels set must line up exactly
    assert {c["id"] for c in _actionable(fixture)} == set(exp["labels"])


def test_taxonomy_coverage_across_the_example_set():
    """The example set as a whole must exercise the full 7-label taxonomy, all
    three terminal dispositions, and all three per-comment dispositions — so the
    suite is a real parity net, not a handful of happy paths."""
    labels, terminals, per_comment = set(), set(), set()
    for path in _FIXTURES:
        exp = _load(path)["expected"]
        labels.update(exp["labels"].values())
        terminals.add(exp["terminal_disposition"])
        per_comment.update(exp["per_comment"].values())
    assert labels == {
        "SUBSTANTIVE", "COSMETIC", "BUSINESS_QUESTION", "PR_DESCRIPTION",
        "OUTDATED", "INVALID", "CLASSIFICATION_FAILED",
    }
    assert terminals == {"merge", "escalate-to-human", "stop"}
    assert per_comment == {"fixed", "skipped-invalid", "escalated"}
