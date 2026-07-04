"""SKILL.md frontmatter audit (build-spec §9.2).

Both shipped skills must use the flat positional ``arguments:`` form (a list of
``$name`` strings), with the human usage hint in ``argument-hint:`` — NOT the old
custom-command ``{name, description, required}`` object form.
"""
from pathlib import Path

import pytest
import yaml

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "buddhi_review" / "skills"
_SKILLS = ("review-pr", "open-pr")


def _frontmatter(skill_name):
    text = (_SKILLS_DIR / skill_name / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{skill_name}: SKILL.md must open with YAML frontmatter"
    end = text.index("\n---", 4)
    return yaml.safe_load(text[4:end])


@pytest.mark.parametrize("skill", _SKILLS)
def test_arguments_are_flat_positional(skill):
    fm = _frontmatter(skill)
    args = fm.get("arguments")
    assert isinstance(args, list) and args, f"{skill}: arguments must be a non-empty list"
    for a in args:
        # Flat positional form: a string like "$pr". NOT an object.
        assert isinstance(a, str), f"{skill}: argument {a!r} must be a flat $name string, not an object"
        assert a.startswith("$"), f"{skill}: argument {a!r} must be a $name positional"


@pytest.mark.parametrize("skill", _SKILLS)
def test_has_argument_hint_and_core_fields(skill):
    fm = _frontmatter(skill)
    assert isinstance(fm.get("argument-hint"), str) and fm["argument-hint"], \
        f"{skill}: argument-hint must carry the human usage hint"
    assert fm.get("name") == skill
    assert isinstance(fm.get("description"), str) and fm["description"]
    assert "Bash" in (fm.get("allowed-tools") or [])


def test_review_pr_positional_args():
    fm = _frontmatter("review-pr")
    assert fm["arguments"] == ["$pr", "$repo"]


def test_open_pr_positional_args():
    fm = _frontmatter("open-pr")
    assert fm["arguments"] == ["$repo"]
