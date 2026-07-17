"""F6 — the multi-checkout candidate-selection gate in both shipped skills.

The engine enumerates (``worktree_target list``); the SKILL.md renders. This guards the
wiring between the two: both skills must shell out to the enumerator, pass the two
auto-select flags, and handle EVERY ``present.mode`` the engine can emit — a mode the
skill does not handle is a checkout the user silently loses.

The gate is tier-neutral by construction: the engine decides whether to ask, so a user
with one candidate is never prompted and a user with several is never railroaded into
the wrong checkout. No tier vocabulary may appear in the gate text.
"""
from pathlib import Path

import pytest

from buddhi_review import worktree_target as wt

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "buddhi_review" / "skills"
_SKILLS = ("open-pr", "review-pr")

# The section each skill's gate ships under, and the header that bounds it.
_GATE = {
    "open-pr": ("### 1.5 Select which checkout to open the PR from",
                "### 2. Pre-launch rebase gate"),
    "review-pr": ("### 2. Select which PR to review",
                  "### 2.5 Pre-launch rebase gate"),
}

# Every mode build_report can emit. A skill that omits one silently drops candidates.
_MODES = ("none", "single", "caller", "two", "many")


def _text(skill):
    return (_SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")


def _gate(skill):
    """The gate section, whitespace-collapsed so a marker that line-wraps in the
    Markdown source still matches as one substring."""
    header, until = _GATE[skill]
    text = _text(skill)
    start = text.index(header)
    end = text.index(until, start)
    return " ".join(text[start:end].split())


# ── (a) both skills call the enumerator, with both auto-select flags ─────────
@pytest.mark.parametrize("skill", _SKILLS)
def test_gate_section_present(skill):
    assert _GATE[skill][0] in _text(skill), f"{skill}: candidate-selection gate missing"


@pytest.mark.parametrize("skill", _SKILLS)
def test_gate_shells_out_to_the_enumerator(skill):
    gate = _gate(skill)
    assert "python3 -m buddhi_review.worktree_target list" in gate
    assert f"--command {skill}" in gate
    assert '--cwd "$CWD"' in gate
    assert '--repo "$OWNER_REPO"' in gate


@pytest.mark.parametrize("skill", _SKILLS)
def test_gate_passes_both_auto_select_flags(skill):
    """Without these the engine cannot recognise the session's own checkout and would
    ask a question whose answer is unambiguous."""
    gate = _gate(skill)
    assert '--caller-cwd "$PWD"' in gate
    assert '--session-id "$CLAUDE_CODE_SESSION_ID"' in gate


# ── (b) every mode the engine can emit is handled ────────────────────────────
@pytest.mark.parametrize("skill", _SKILLS)
@pytest.mark.parametrize("mode", _MODES)
def test_gate_handles_every_present_mode(skill, mode):
    assert f"`{mode}`" in _gate(skill), f"{skill}: present.mode {mode!r} is unhandled"


def test_modes_asserted_here_match_the_engine():
    """Drift guard: if the engine grows a mode, this test fails until the skills (and
    the row above) handle it — the modes cannot silently diverge from the contract."""
    import inspect
    src = inspect.getsource(wt._build_present)
    emitted = {line.split('"mode": "')[1].split('"')[0]
               for line in src.splitlines() if '"mode": "' in line}
    assert emitted == set(_MODES)


# ── (c) the no-ask paths stay silent; the ask paths ask ──────────────────────
@pytest.mark.parametrize("skill", _SKILLS)
def test_single_and_caller_auto_select_without_asking(skill):
    gate = _gate(skill)
    assert "do **not** ask" in gate.lower() or "no question" in gate.lower()
    # The caller short-circuit prints exactly one line naming the auto-target.
    assert "Auto-selected this session's worktree" in gate


@pytest.mark.parametrize("skill", _SKILLS)
def test_two_and_many_ask_with_the_enumerated_options(skill):
    gate = _gate(skill)
    assert "AskUserQuestion" in gate
    assert "present.options[]" in gate
    assert "sanctioned gate" in gate


@pytest.mark.parametrize("skill", _SKILLS)
def test_free_text_other_is_offered_only_in_many_mode(skill):
    gate = _gate(skill)
    assert "present.free_input" in gate
    assert "Other" in gate


@pytest.mark.parametrize("skill", _SKILLS)
def test_all_option_fans_out_over_every_candidate(skill):
    gate = _gate(skill)
    assert '"all"' in gate
    # Each iteration must re-bind its own target — carrying the first candidate's
    # values into the next would run the loop on the wrong checkout.
    assert "re-bind" in gate or "re-binding" in gate


@pytest.mark.parametrize("skill", _SKILLS)
def test_gate_stops_on_an_engine_error_rather_than_assuming_nothing_to_do(skill):
    gate = _gate(skill)
    assert '{"status": "error"' in gate
    assert "STOP" in gate


# ── (d) the operator-facing question (the shared cross-skill wording) ────────
def test_open_pr_asks_the_canonical_which_checkout_question():
    assert "Which checkout should I open the PR from?" in _gate("open-pr")


def test_open_pr_none_mode_exits_with_the_canonical_no_op_message():
    assert "No changes to commit" in _gate("open-pr")


def test_review_pr_none_mode_exits_with_the_canonical_no_op_message():
    assert "No open PR found in" in _gate("review-pr")


def test_review_pr_flags_a_pr_that_is_not_checked_out_anywhere():
    """The engine can enumerate an open PR with ``path: null``; the loop cannot review
    one (it does not check the PR out), so the skill must not launch on it."""
    gate = _gate("review-pr")
    assert "pr-only" in gate
    assert "not checked out in any worktree" in gate


# ── (e) the rules list the new gate; it stays tier-neutral ───────────────────
def test_open_pr_rules_name_the_target_selection_gate():
    rules = _text("open-pr").split("## Arguments")[0]
    assert "target-selection" in rules
    assert "Step 1.5" in rules


def test_review_pr_rules_name_the_pr_selection_gate():
    rules = _text("review-pr").split("## Arguments")[0]
    assert "PR-selection" in rules


@pytest.mark.parametrize("skill", _SKILLS)
def test_gate_carries_no_tier_vocabulary(skill):
    """The skill is a tier-neutral instruction sheet — the ENGINE decides the ask."""
    gate = _gate(skill).lower()
    for token in ("pro", "paid", "licence", "license", "upgrade", "free tier"):
        assert f" {token} " not in gate, f"{skill}: tier vocabulary {token!r} in the gate"
