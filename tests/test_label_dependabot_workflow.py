"""Static-analysis guard for .github/workflows/label-dependabot.yml (F5).

This workflow closes a specific hole: the full pytest + publish-gate suite is
label-gated (tests-ready-for-ci.yml runs only when the `ready-for-ci` label is
present), and that label is normally added at merge time by the review-loop
automation. Dependabot PRs never pass through that automation, so without this
workflow the full suite never runs on them and a broken action bump merges
unexercised (how #61's template drift landed silently).

Every assertion below pins one load-bearing line whose loss would silently
re-open that hole:
  * the PR-author gate (`github.event.pull_request.user.login ==
    'dependabot[bot]'`) — without it the job would try to label EVERY PR, or
    (if dropped) would never scope to Dependabot. It reads the PR's author
    rather than `github.actor` (whoever triggered the event) so a human
    reopening a Dependabot PR still gets it labeled;
  * the label is added with a GitHub App installation token, NOT the built-in
    `GITHUB_TOKEN` — GitHub's recursion guard suppresses workflow runs from
    events a `GITHUB_TOKEN` creates, so a `GITHUB_TOKEN`-added label would land
    but tests-ready-for-ci.yml would never fire (the suite would silently never
    run). The App token is minted via `actions/create-github-app-token`, the
    same pattern release-please.yml uses so its Release triggers publish.yml;
  * the `pull_request_target` trigger on `opened`/`reopened` — NOT plain
    `pull_request`: a Dependabot-triggered `pull_request` run is sandboxed to
    Dependabot secrets only (no Actions secrets), so `secrets.RELEASE_PLEASE_APP_*`
    would be empty and the App-token step would fail on the exact
    `opened`-by-Dependabot case this workflow targets. `pull_request_target` runs
    in the base-repo context where those Actions secrets are visible (safe here
    because the job checks out no PR code and only runs `gh pr edit`);
  * the label added is exactly `ready-for-ci` (the gate label) — a typo would
    silently defeat the whole point;
  * the only marketplace action (`actions/create-github-app-token`) is pinned to
    a full commit SHA, never a `@v<digit>` tag — SHA-pinning is the repo's
    supply-chain posture (release-please.yml pins the same way).

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


def test_triggers_on_pull_request_target_opened_reopened() -> None:
    """The label has to land the moment the PR appears, so the full suite gates
    the merge. `opened` covers the first push; `reopened` covers a Dependabot
    recreate/rebase that reopens the PR.

    The trigger is `pull_request_target`, NOT plain `pull_request`: a
    Dependabot-triggered `pull_request` run only sees Dependabot secrets, so the
    RELEASE_PLEASE_APP_* Actions secrets used to mint the App token would be empty
    and the step would fail on the very case this workflow exists for.
    `pull_request_target` runs in the base-repo context where those Actions
    secrets are available. Assert plain `pull_request` is NOT the trigger so a
    regression back to it (which silently re-opens the hole) is caught."""
    on = _on_block(_doc())
    assert isinstance(on, dict) and "pull_request_target" in on, on
    assert "pull_request" not in on, (
        "trigger regressed to plain `pull_request` — Dependabot runs can't see the "
        f"RELEASE_PLEASE_APP_* Actions secrets, so the App-token step fails: {on}"
    )
    types = on["pull_request_target"]["types"]
    assert "opened" in types and "reopened" in types, types


def test_job_gated_on_dependabot_pr_author() -> None:
    """Only Dependabot's own PRs are auto-labeled here — every other author's PR
    is labeled by the review loop at merge time. Gated on the PR author
    (`github.event.pull_request.user.login`), not `github.actor`, so a human
    reopening a Dependabot PR doesn't skip the label. Read from the PARSED `if:`
    so a commented-out line can't satisfy it."""
    cond = str(_job(_doc()).get("if", "")).strip()
    assert cond == "github.event.pull_request.user.login == 'dependabot[bot]'", cond


def test_github_token_pinned_to_least_privilege() -> None:
    """The App token (not GITHUB_TOKEN) performs the label write, so GITHUB_TOKEN
    needs no scopes at all. The job pins it to least privilege with an empty
    `permissions: {}` block. A stray write scope here would be unused privilege —
    and a `pull-requests: write` grant in particular would signal a regression to
    the old, broken GITHUB_TOKEN-labels-the-PR design."""
    perms = _job(_doc()).get("permissions")
    assert perms == {}, f"expected least-privilege `permissions: {{}}`, got {perms!r}"


def test_mints_github_app_token() -> None:
    """The label must be added with a GitHub App installation token, minted via
    `actions/create-github-app-token`, so the `labeled` event is NOT attributed
    to GITHUB_TOKEN (whose events the recursion guard suppresses). The action is
    SHA-pinned and wired to the existing RELEASE_PLEASE_APP_* secrets."""
    steps = _job(_doc())["steps"]
    minters = [
        s for s in steps
        if str(s.get("uses", "")).startswith("actions/create-github-app-token@")
    ]
    assert len(minters) == 1, f"expected exactly one app-token minting step: {steps}"
    minter = minters[0]
    assert minter.get("id") == "app-token", minter
    with_ = minter.get("with", {})
    assert with_.get("app-id") == "${{ secrets.RELEASE_PLEASE_APP_ID }}", with_
    assert with_.get("private-key") == "${{ secrets.RELEASE_PLEASE_APP_KEY }}", with_


def test_label_added_with_app_token_not_github_token() -> None:
    """The load-bearing invariant: the `gh pr edit` step authenticates with the
    App token (`steps.app-token.outputs.token`), NOT `secrets.GITHUB_TOKEN`. A
    GITHUB_TOKEN here would land the label but never trigger tests-ready-for-ci.yml
    (recursion guard), silently re-opening the exact hole this workflow closes."""
    steps = _job(_doc())["steps"]
    label_steps = [s for s in steps if "gh pr edit" in str(s.get("run", ""))]
    assert len(label_steps) == 1, f"expected exactly one labeling step: {steps}"
    gh_token = str(label_steps[0].get("env", {}).get("GH_TOKEN", ""))
    assert "steps.app-token.outputs.token" in gh_token, gh_token
    assert "secrets.GITHUB_TOKEN" not in gh_token, (
        "labeling must NOT use GITHUB_TOKEN — its `labeled` event is suppressed "
        f"by GitHub's recursion guard: {gh_token!r}"
    )


def test_adds_exactly_the_ready_for_ci_gate_label() -> None:
    """The label added must be exactly `ready-for-ci` — the label
    tests-ready-for-ci.yml gates on. A typo here would silently never fire the
    suite. Assert against the parsed run-step so the check reads the live value."""
    steps = _job(_doc())["steps"]
    run = " ".join(s.get("run", "") for s in steps)
    assert "gh pr edit" in run, "job must add the label via the built-in gh CLI"
    assert "--add-label ready-for-ci" in run, run


def test_no_tag_pinned_action_version_literal() -> None:
    """Any marketplace action must be pinned to a full commit SHA, never a
    `@v<digit>` tag — a mutable tag is a supply-chain hazard. The `# vX.Y.Z`
    version lives in a trailing comment (not preceded by `@`), so a real `@v<digit>`
    literal would mean an action was tag-pinned and must be converted to a SHA."""
    assert re.search(r"@v\d", _text()) is None, "action is tag-pinned; use a full SHA"


def test_only_action_is_the_sha_pinned_app_token() -> None:
    """The workflow's ONLY third-party action is `actions/create-github-app-token`
    (needed to mint the non-GITHUB_TOKEN credential), and it must be SHA-pinned.
    The labeling itself is still done inline with the built-in `gh` CLI, so no
    other `uses:` step should appear."""
    steps = _job(_doc())["steps"]
    uses = [str(s["uses"]) for s in steps if "uses" in s]
    assert len(uses) == 1, f"expected exactly one `uses:` step, found: {uses}"
    action = uses[0]
    assert action.startswith("actions/create-github-app-token@"), action
    ref = action.split("@", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{40}", ref), f"app-token action must be SHA-pinned: {ref}"
