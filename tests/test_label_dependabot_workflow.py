"""Static-analysis guard for .github/workflows/label-dependabot.yml (F5).

This workflow closes a specific hole: the full pytest + publish-gate suite is
label-gated (tests-ready-for-ci.yml runs only when the `ready-for-ci` label is
present), and that label is normally added at merge time by the review-loop
automation. Dependabot PRs never pass through that automation, so without this
workflow the full suite never runs on them and a broken action bump merges
unexercised (how #61's template drift landed silently).

Every assertion below pins one load-bearing line whose loss would silently
re-open that hole:
  * the actor gate (`github.actor == 'dependabot[bot]'`) — without it the job
    would try to label EVERY PR, or (if dropped) would never scope to Dependabot;
  * the explicit `permissions: pull-requests: write` — a Dependabot-triggered
    run gets a READ-ONLY token by default, so without this block `gh pr edit`
    fails and the PR is never labeled;
  * the `pull_request` trigger on `opened`/`reopened`;
  * the label added is exactly `ready-for-ci` (the gate label) — a typo would
    silently defeat the whole point;
  * NO `@v<digit>` action-version literal — the job uses the built-in `gh` CLI,
    not a marketplace action, so no pinned version should ever appear.

The workflow's own publish-cleanliness (no private paths/handles) is already
covered by ``tests/test_oss_purity.py``, which scans every
``.github/workflows/*.yml`` — so this module deliberately does NOT re-assert it
(re-spelling the forbidden vocabulary here would only force the file into that
guard's scaffolding allowlist for no added coverage).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).resolve().parent.parent
    / ".github" / "workflows" / "label-dependabot.yml"
)


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _doc() -> dict:
    return yaml.safe_load(_text())


def _on_block(doc: dict):
    """The `on:` block. PyYAML (YAML 1.1) parses the bare ``on`` key as the
    boolean ``True``, so fall back to that key."""
    return doc.get("on", doc.get(True))


def _job(doc: dict) -> dict:
    jobs = doc["jobs"]
    assert len(jobs) == 1, f"expected exactly one job, found {list(jobs)}"
    return next(iter(jobs.values()))


def test_workflow_exists() -> None:
    assert WORKFLOW.is_file(), f"missing workflow: {WORKFLOW}"


def test_parses_as_yaml() -> None:
    doc = _doc()
    assert isinstance(doc, dict) and "jobs" in doc


def test_triggers_on_pull_request_opened_reopened() -> None:
    """The label has to land the moment the PR appears, so the full suite gates
    the merge. `opened` covers the first push; `reopened` covers a Dependabot
    recreate/rebase that reopens the PR."""
    on = _on_block(_doc())
    assert isinstance(on, dict) and "pull_request" in on, on
    types = on["pull_request"]["types"]
    assert "opened" in types and "reopened" in types, types


def test_job_gated_on_dependabot_actor() -> None:
    """Only Dependabot's own PRs are auto-labeled here — every other actor's PR
    is labeled by the review loop at merge time. Read from the PARSED `if:` so a
    commented-out line can't satisfy it."""
    cond = str(_job(_doc()).get("if", "")).strip()
    assert cond == "github.actor == 'dependabot[bot]'", cond


def test_job_declares_pull_requests_write_permission() -> None:
    """A Dependabot-triggered `pull_request` run gets a READ-ONLY GITHUB_TOKEN by
    default (GitHub treats it like a fork PR). Without an explicit
    `pull-requests: write`, `gh pr edit --add-label` fails and the PR is never
    labeled — the exact hole this workflow exists to close. Least privilege: this
    is the only scope granted."""
    perms = _job(_doc()).get("permissions")
    assert isinstance(perms, dict), f"job has no permissions block: {perms!r}"
    assert perms.get("pull-requests") == "write", perms


def test_adds_exactly_the_ready_for_ci_gate_label() -> None:
    """The label added must be exactly `ready-for-ci` — the label
    tests-ready-for-ci.yml gates on. A typo here would silently never fire the
    suite. Assert against the parsed run-step so the check reads the live value."""
    steps = _job(_doc())["steps"]
    run = " ".join(s.get("run", "") for s in steps)
    assert "gh pr edit" in run, "job must add the label via the built-in gh CLI"
    assert "--add-label ready-for-ci" in run, run


def test_no_pinned_action_version_literal() -> None:
    """The job uses the built-in `gh` CLI, not a marketplace action, so no
    `uses: …@v<digit>` pin should ever appear (a version literal here would mean
    someone reintroduced a marketplace action)."""
    assert re.search(r"@v\d", _text()) is None, "unexpected @v<digit> action pin"


def test_uses_no_marketplace_action() -> None:
    """No `uses:` step at all — the labeling is done inline with `gh`, keeping the
    workflow free of any third-party action (and thus of any version to pin)."""
    steps = _job(_doc())["steps"]
    assert all("uses" not in s for s in steps), (
        "a marketplace action crept in; label via the built-in gh CLI instead"
    )
