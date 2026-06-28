"""Parity guard: the bundled Claude reviewer workflow stays the hardened one.

The setup flow installs ``skills/review-pr/references/claude-code-review.yml``
into a user's repo as their ``.github/workflows/claude-code-review.yml``. That
template is the canonical hardened reviewer, and several of its lines are
load-bearing — if any drifts, a one-click install ships a reviewer that silently
stalls or under-reviews. These tests pin the load-bearing contract so a careless
edit fails in CI instead of in a user's repo.

The single most important coupling: the clean-review sentinel the workflow tells
Claude to POST must be exactly what the round driver's clean-review detector
(:data:`buddhi_review.detectors.CLEAN_REVIEW_PATTERNS`) recognises. Editing one
side without the other would leave a clean review unrecognised — the loop would
wait on Claude until it timed out. The coupling test reads the sentinel straight
out of the workflow and runs it through the real detector, so the two can never
drift apart unnoticed.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from buddhi_review import detectors

TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "buddhi_review" / "skills" / "review-pr" / "references"
    / "claude-code-review.yml"
)


def _workflow_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _action_step() -> dict:
    """The claude-code-action step, indexed by its ``uses:`` (not position) so
    reordering steps can't silently make this read the wrong block."""
    doc = yaml.safe_load(_workflow_text())
    steps = doc["jobs"]["review"]["steps"]
    step = next(
        (s for s in steps if "claude-code-action" in (s.get("uses") or "")), None
    )
    assert step is not None, "workflow missing the claude-code-action step"
    return step


def _prompt() -> str:
    return _action_step()["with"]["prompt"]


def _claude_args() -> str:
    return _action_step()["with"]["claude_args"]


def _model(claude_args: str) -> str:
    m = re.search(r"--model\s+(\S+)", claude_args)
    assert m, f"claude_args missing a --model flag: {claude_args!r}"
    return m.group(1)


def _allowed_tools(claude_args: str) -> list[str]:
    m = re.search(r'--allowedTools\s+"([^"]*)"', claude_args)
    assert m, f"claude_args missing --allowedTools: {claude_args!r}"
    return [t.strip() for t in m.group(1).split(",") if t.strip()]


def _checkout_step() -> dict:
    """The actions/checkout step, indexed by its ``uses:`` so the assertion reads
    the LIVE step value, not raw file text a commented-out line could satisfy."""
    doc = yaml.safe_load(_workflow_text())
    steps = doc["jobs"]["review"]["steps"]
    step = next(
        (s for s in steps if (s.get("uses") or "").startswith("actions/checkout")),
        None,
    )
    assert step is not None, "workflow missing the actions/checkout step"
    return step


def _posted_sentinel(prompt: str) -> str:
    """The exact ``--body`` value the prompt tells Claude to POST as the clean
    sentinel — the string the detector must recognise. Asserts there is EXACTLY
    one ``--body`` in the prompt so a future illustrative second one can't
    silently shadow the real sentinel this extraction binds to."""
    bodies = re.findall(r'--body "([^"]+)"', prompt)
    assert len(bodies) == 1, (
        f"expected exactly one --body sentinel in the prompt, found {bodies}"
    )
    return bodies[0]


def test_template_exists() -> None:
    assert TEMPLATE.is_file(), f"bundled workflow template missing: {TEMPLATE}"


def test_template_is_valid_yaml() -> None:
    doc = yaml.safe_load(_workflow_text())
    assert isinstance(doc, dict) and "jobs" in doc


def test_checkout_is_v4_with_pr_head_ref() -> None:
    """checkout@v4 with the PR-head ``ref:`` override, so issue_comment runs see
    the actual changed files rather than stale base-branch contents. Read from
    the PARSED step so a commented-out ``@v4`` line can't satisfy it."""
    step = _checkout_step()
    assert step["uses"] == "actions/checkout@v4", step["uses"]
    ref = (step.get("with") or {}).get("ref", "")
    assert ref.startswith("refs/pull/"), f"checkout ref is not the PR head: {ref!r}"
    # Belt-and-suspenders: no stale @v3 lingers anywhere (live step or comment).
    assert "actions/checkout@v3" not in _workflow_text(), "stale checkout@v3 present"


def test_model_is_the_opus_alias_not_a_pinned_literal() -> None:
    """``--model opus`` uses the ALIAS, which auto-resolves to the latest Opus,
    so the reviewer tracks new Opus releases without a workflow edit. Guard
    against a regression to a pinned ``claude-opus-4-*`` literal."""
    args = _claude_args()
    assert _model(args) == "opus", _model(args)
    # Intentionally scoped to `claude-opus-4` (the current Opus family literal).
    # A broader `claude-\d` would false-positive on legacy names like `claude-3-opus`
    # that could appear in unrelated args; the regression risk is specifically
    # someone pinning the Claude-4 Opus model id instead of the `opus` alias.
    assert not re.search(r"claude-opus-4", args), (
        "claude_args pins a versioned Opus literal — use the `opus` alias"
    )


def test_allowed_tools_carry_the_load_bearing_channels() -> None:
    """The ``--allowedTools`` allowlist is required — without it the SDK denies
    every GitHub tool call and posts zero comments. The inline-comment MCP tool
    is the only finding channel the loop consumes, and ``gh pr comment`` is how
    the clean sentinel gets POSTED; both must be present."""
    tools = _allowed_tools(_claude_args())
    assert tools, "allowedTools list is empty"
    assert "mcp__github_inline_comment__create_inline_comment" in tools, (
        "lost the inline-comment MCP tool — the only finding channel the loop reads"
    )
    assert any(t.startswith("Bash(gh pr comment") for t in tools), (
        "lost Bash(gh pr comment:*) — Claude can no longer POST the clean sentinel"
    )
    # AskUserQuestion stays disallowed: in a headless runner it is a silent
    # give-up (the action exits success with zero comments).
    assert '--disallowedTools "AskUserQuestion"' in _claude_args()


def test_prompt_forces_an_actual_posted_sentinel_not_narration() -> None:
    """The clean case must instruct Claude to RUN ``gh pr comment`` as its final
    action and warn that an unposted (merely narrated) sentinel is invisible —
    otherwise a clean re-review stalls the loop until it times out."""
    prompt = _prompt()
    assert "gh pr comment" in prompt, "prompt never tells Claude to run gh pr comment"
    assert _posted_sentinel(prompt) == "No issues found.", _posted_sentinel(prompt)
    # The mandatory-POST framing and the keep-the-anchor literal.
    assert "you MUST post the clean" in prompt
    assert "exactly this line on its own: No issues found." in prompt
    # The clean RE-review case is explicitly covered (nothing-to-flag still posts).
    assert "INCLUDING a re-review" in prompt


def test_posted_sentinel_matches_the_clean_review_detector() -> None:
    """No silent drift: the sentinel the workflow tells Claude to POST is exactly
    what the round driver's clean-review detector flips to voluntarily-done. If
    someone edits one side without the other, this fails before a shipped reviewer
    starts emitting a clean signal the loop can't read (or vice versa)."""
    sentinel = _posted_sentinel(_prompt())
    # Tier-1 deterministic detector: the posted sentinel reads as a clean review.
    assert detectors.is_clean_review(sentinel), (
        f"workflow sentinel {sentinel!r} is NOT recognised as clean by the detector"
    )
    # And it matches the load-bearing sentinel pattern specifically (pattern [0]).
    assert re.search(detectors.CLEAN_REVIEW_PATTERNS[0], sentinel, re.IGNORECASE), (
        f"workflow sentinel {sentinel!r} no longer matches CLEAN_REVIEW_PATTERNS[0]"
    )


def test_workflow_stays_publish_clean() -> None:
    """The bundled template ships in an OSS package — it must not leak private
    paths, account handles, or internal reference markers."""
    text = _workflow_text()
    for needle in ("manasvi", "m-s-21", "/Users/", "buddhi/", "buddhi-claude"):
        assert needle not in text, f"private reference {needle!r} leaked into the template"


# ---------------------------------------------------------------------------
# F9: the auth-failure post-step guard (turns a silent 401 RED, not green)
# ---------------------------------------------------------------------------
_GUARD_NAME_FRAGMENT = "authentication error"


def _guard_step() -> dict:
    """The auth-failure post-step, indexed by its ``name:`` so a reorder can't
    silently read the wrong block."""
    doc = yaml.safe_load(_workflow_text())
    steps = doc["jobs"]["review"]["steps"]
    step = next(
        (s for s in steps if _GUARD_NAME_FRAGMENT in (s.get("name") or "")), None
    )
    assert step is not None, "workflow missing the auth-failure guard post-step"
    return step


def test_action_step_carries_a_stable_id() -> None:
    """The action step needs a stable ``id`` so the guard can read its
    ``execution_file`` output. (Reordering steps can't break the coupling.)"""
    assert _action_step().get("id") == "claude_review", _action_step().get("id")


def test_guard_step_present_and_runs_always() -> None:
    """The post-step must run on ``always()`` — the action concludes ``success``
    on a 401, so a ``success()``-gated step would be skipped and the silent
    failure would stay invisible. It reads the action's ``execution_file``."""
    guard = _guard_step()
    assert str(guard.get("if")).strip() == "always()"
    env = guard.get("env") or {}
    assert "execution_file" in (env.get("CLAUDE_EXECUTION_FILE") or ""), (
        "guard must read the action's execution_file output"
    )
    assert "steps.claude_review.outputs.execution_file" in env["CLAUDE_EXECUTION_FILE"]
    assert isinstance(guard.get("run"), str) and guard["run"].strip()


def test_guard_fails_only_on_the_auth_signature_not_a_clean_or_plain_failure() -> None:
    """The run-script must fail (``exit 1``) ONLY on the token-invalid auth
    signature AND only when there is no clean-success result — so a clean review
    (is_error:false) or an ordinary non-auth failure is never turned red."""
    run = _guard_step()["run"]
    # Matches the auth signature (mirrors the detector).
    assert "invalid bearer token" in run
    assert "authentication_error" in run
    # The is_error:false clean-success guard is present (condition 2): a clean
    # review never fires even if the phrase is quoted in the reviewed diff.
    assert "is_error == false" in run
    assert "exit 1" in run
    # Fail-safe toward green when the tooling/output is absent — never a false red.
    assert "command -v jq" in run
    assert run.count("exit 0") >= 2  # missing-file path AND no-failure path


def test_guard_is_self_contained_and_publish_clean() -> None:
    """The template is copied verbatim into third-party repos, so the guard
    script must reference no repo file and leak no private/paid identifier."""
    run = _guard_step()["run"]
    for forbidden in ("tools/", "python3 ", "../", "review_loop"):
        assert forbidden not in run, f"guard script references {forbidden!r}"
    for needle in ("manasvi", "m-s-21", "/Users/", "buddhi/", "buddhi-claude"):
        assert needle not in run, f"private reference {needle!r} leaked into the guard"


def test_credential_inputs_unchanged_and_runs_on_standard_runner() -> None:
    """F9 only ADDS detection — it must not weaken the dual-credential inputs and
    must keep the free standard runner."""
    with_ = _action_step()["with"]
    assert with_.get("claude_code_oauth_token") == "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"
    assert with_.get("anthropic_api_key") == "${{ secrets.ANTHROPIC_API_KEY }}"
    doc = yaml.safe_load(_workflow_text())
    assert doc["jobs"]["review"]["runs-on"] == "ubuntu-latest"
