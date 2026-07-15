"""Tests for the Claude Code plugin manifest (``.claude-plugin/plugin.json``).

The repo ships as a community-marketplace plugin (FREE-6). This guard locks the
manifest's shape against the live schema's requirements — a valid, immutable
``name``, an explicit ``version`` that ``/plugin update`` keys on, the metadata a
marketplace listing needs, and a ``skills`` path that actually resolves to the two
shipped skills — plus keeps the manifest and its bootstrap scripts publish-clean
(no paid/private surface). See ``buddhi_review/git_guardrail_hook.py`` and
``scripts/`` for the hook wiring.
"""
import json
import re
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_MANIFEST = _REPO / ".claude-plugin" / "plugin.json"
_SCRIPTS = _REPO / "scripts"

sys.path.insert(0, str(_REPO / "tools"))
import publish_gate as g  # noqa: E402

_REPO_URL = "https://github.com/buddhikernel/buddhi-review"


@pytest.fixture(scope="module")
def manifest():
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def test_manifest_exists_and_is_valid_json():
    assert _MANIFEST.is_file(), ".claude-plugin/plugin.json must exist at the repo root"
    json.loads(_MANIFEST.read_text(encoding="utf-8"))  # raises on invalid JSON


def test_name_is_immutable_kebab_case(manifest):
    # `name` is the ONLY required field and is IMMUTABLE once published — it keys
    # enabledPlugins and the /plugin UI. Kebab-case, no spaces.
    assert manifest.get("name") == "buddhi-review"
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", manifest["name"]), \
        "name must be kebab-case with no spaces"


def test_version_is_present_and_semver_shaped(manifest):
    # An explicit version pins the plugin so /plugin update only fires on a bump;
    # without it Claude Code falls back to the commit SHA (every commit = update).
    version = manifest.get("version")
    assert isinstance(version, str) and version, "an explicit version is required"
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"version must be semver: {version!r}"


def test_required_marketplace_metadata_present(manifest):
    assert manifest.get("description"), "a description is required for the listing"
    assert manifest.get("license") == "MIT"
    assert isinstance(manifest.get("keywords"), list) and manifest["keywords"], \
        "keywords must be a non-empty array (a string is a load error)"
    author = manifest.get("author")
    assert isinstance(author, dict) and author.get("name"), "author.name is required"


def test_homepage_and_repository_point_at_the_github_repo(manifest):
    assert manifest.get("homepage") == _REPO_URL
    assert manifest.get("repository") == _REPO_URL


def test_skills_path_resolves_to_both_shipped_skills(manifest):
    skills = manifest.get("skills")
    assert isinstance(skills, str), "skills must be a path string pointing at the skill dir"
    skills_dir = (_REPO / skills).resolve()
    assert skills_dir.is_dir(), f"skills path does not resolve to a directory: {skills}"
    for skill in ("open-pr", "review-pr"):
        assert (skills_dir / skill / "SKILL.md").is_file(), \
            f"skills path must contain {skill}/SKILL.md"
    # Must NOT move/duplicate the existing skill files: the path IS the in-tree dir.
    assert skills_dir == (_REPO / "buddhi_review" / "skills").resolve()


def test_sessionstart_hook_runs_the_install_bootstrap(manifest):
    hooks = manifest.get("hooks") or {}
    sessionstart = hooks.get("SessionStart")
    assert isinstance(sessionstart, list) and sessionstart, \
        "manifest must declare a SessionStart hook to ensure buddhi_review is importable"
    commands = [h.get("command", "") for entry in sessionstart
                for h in (entry.get("hooks") or [])
                if h.get("type") == "command"]
    assert any("ensure_install.py" in c for c in commands), \
        "SessionStart hook must run scripts/ensure_install.py"
    assert any("${CLAUDE_PLUGIN_ROOT}" in c for c in commands), \
        "SessionStart command must resolve the script under ${CLAUDE_PLUGIN_ROOT}"
    assert (_SCRIPTS / "ensure_install.py").is_file(), \
        "the SessionStart bootstrap script must exist"


def test_only_documented_top_level_fields(manifest):
    # Keep the manifest to the documented schema so `claude plugin validate
    # --strict` (warnings-as-errors) stays green — an unrecognized field trips it.
    allowed = {
        "$schema", "name", "displayName", "version", "description", "author",
        "homepage", "repository", "license", "keywords", "skills", "commands",
        "agents", "hooks", "mcpServers", "outputStyles", "lspServers",
        "experimental", "dependencies", "userConfig", "channels", "defaultEnabled",
    }
    unknown = set(manifest) - allowed
    assert not unknown, f"unrecognized top-level manifest fields: {sorted(unknown)}"


# ── Purity: the manifest + bootstrap scripts ship in the public plugin ──────────
def _plugin_surface_files():
    files = [_MANIFEST]
    files += sorted(_SCRIPTS.glob("*.py"))
    return files


@pytest.mark.parametrize("path", _plugin_surface_files(),
                         ids=lambda p: str(p.relative_to(_REPO)))
def test_plugin_files_are_publish_clean(path):
    """The plugin manifest and its bootstrap scripts carry no paid/private surface
    (paid product names, the author path / owner handle, the company handle)."""
    hits = g.scan_paid_and_publish(path.read_text(encoding="utf-8"))
    assert hits == [], f"{path.relative_to(_REPO)}: {hits}"
