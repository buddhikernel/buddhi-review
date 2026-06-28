"""F6 — the SKILL.md per-repo unconfigured-repo gate (both shipped skills).

Both ``create-pr`` and ``review-pr`` must carry the SAME per-repo reviewer
confirmation gate: after the repo is resolved, shell out to F1's status reader
(``python -m buddhi_review status --repo <repo>``), and on an UNCONFIRMED repo
ask the user ONCE — either launch the terminal wizard for that repo or fall back
to global defaults. It must NEVER walk the user through reviewer/auto-on-open/
label-CI selection inline in the session (the standing "no deterministic setup in
the Claude Code session" rule). A CONFIRMED repo must draw no prompt at all.

This is the FREE-skill twin of the monolith skills' Step 1.1 (P5). The two files
have no automated drift-guard apart from this test, so it asserts the gate is
present, single-ask, and consistent across both.
"""
from pathlib import Path

import pytest
import yaml

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "buddhi_review" / "skills"
_SKILLS = ("create-pr", "review-pr")

# The header the gate ships under in both files, and the next section header that
# bounds it (so a marker matched elsewhere in the file can never pass the scan).
_GATE_HEADER = "### 1.1 Per-repo reviewer confirmation gate"
_NEXT_SECTION = {
    "create-pr": "### 2. Pre-launch rebase gate",
    "review-pr": "### 2. Select which PR to review",
}


def _text(skill):
    return (_SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter(skill):
    text = _text(skill)
    assert text.startswith("---\n"), f"{skill}: SKILL.md must open with YAML frontmatter"
    end = text.index("\n---", 4)
    return yaml.safe_load(text[4:end])


def _section(skill, header, *, until):
    """Return the ``header`` section up to the next ``until`` header, with all
    whitespace runs collapsed to single spaces so a prose marker that line-wraps
    in the source (``**Do\\n  NOT configure…``) still matches as one substring.
    The shell one-liners the gate checks for are single-spaced already, so the
    flattening leaves them intact."""
    text = _text(skill)
    start = text.index(header)
    end = text.index(until, start)
    return " ".join(text[start:end].split())


def _gate(skill):
    return _section(skill, _GATE_HEADER, until=_NEXT_SECTION[skill])


def _rules(skill):
    return _section(skill, "## Critical behaviour rules", until="## Arguments")


# ── (a) frontmatter stays valid ─────────────────────────────────────────────────

@pytest.mark.parametrize("skill", _SKILLS)
def test_frontmatter_still_valid(skill):
    fm = _frontmatter(skill)
    assert fm.get("name") == skill
    assert isinstance(fm.get("description"), str) and fm["description"]
    tools = fm.get("allowed-tools") or []
    # The gate cannot ask without AskUserQuestion; shelling out needs Bash.
    assert "Bash" in tools
    assert "AskUserQuestion" in tools


# ── (b) the gate is present and behaves correctly in BOTH files ──────────────────

@pytest.mark.parametrize("skill", _SKILLS)
def test_gate_section_present(skill):
    assert _GATE_HEADER in _text(skill), f"{skill}: per-repo gate section missing"


@pytest.mark.parametrize("skill", _SKILLS)
def test_gate_checks_via_f1_status_cli(skill):
    gate = _gate(skill)
    # Shells out to F1's status reader (JSON), not an inline config walk.
    assert "python3 -m buddhi_review status --repo" in gate
    assert "repo_confirmed" in gate
    assert "has_global_default" in gate


@pytest.mark.parametrize("skill", _SKILLS)
def test_gate_is_single_ask(skill):
    gate = _gate(skill)
    assert "AskUserQuestion" in gate
    assert "ask ONCE" in gate


@pytest.mark.parametrize("skill", _SKILLS)
def test_unconfirmed_launches_wizard_for_this_repo(skill):
    gate = _gate(skill)
    # Option 1 launches the terminal wizard bound to THIS repo (per-repo confirm
    # mode), never an inline setup.
    assert "launch-setup.sh" in gate
    assert 'bash "$SETUP" --repo "$OWNER_REPO"' in gate


@pytest.mark.parametrize("skill", _SKILLS)
def test_unconfirmed_offers_global_defaults_fallback(skill):
    gate = _gate(skill)
    # Option 2 proceeds on the global default fleet without writing a per-repo entry.
    assert "Use global defaults" in gate
    assert "will refuse to launch" in gate  # names the no-default fail-closed consequence


@pytest.mark.parametrize("skill", _SKILLS)
def test_gate_never_configures_reviewers_in_session(skill):
    gate = _gate(skill)
    # The standing rule: no deterministic setup in the session.
    assert "Do NOT configure reviewers in this session" in gate
    assert "never configures reviewers itself" in gate


@pytest.mark.parametrize("skill", _SKILLS)
def test_confirmed_repo_draws_no_prompt(skill):
    gate = _gate(skill)
    # The `repo_confirmed: true` branch proceeds silently — no question.
    assert "**`true`** — proceed silently" in gate


# ── (c) the critical-behaviour rules name the new gate ───────────────────────────

@pytest.mark.parametrize("skill", _SKILLS)
def test_rules_list_the_perrepo_gate(skill):
    rules = _rules(skill)
    assert "Step 1.1" in rules
    assert "per-repo reviewer" in rules


# ── (d) the two skills stay consistent (no drift) ────────────────────────────────

def test_gate_consistent_across_both_skills():
    invariants = (
        "python3 -m buddhi_review status --repo",
        "repo_confirmed",
        "has_global_default",
        "AskUserQuestion",
        "ask ONCE",
        'bash "$SETUP" --repo "$OWNER_REPO"',
        "Use global defaults",
        "Do NOT configure reviewers in this session",
        "**`true`** — proceed silently",
    )
    gates = {skill: _gate(skill) for skill in _SKILLS}
    for marker in invariants:
        for skill, gate in gates.items():
            assert marker in gate, f"{skill}: gate missing shared invariant {marker!r}"
