"""F1 — the per-repo write path, the ``label_gated_ci`` reader, and the status CLI.

Three foundations the rest of the FREE lane consumes:

* :func:`buddhi_review.config.set_repo_keys` — the per-repo CONFIRMATION writer.
  It deep-merges into ``repos[<owner/repo>]`` and round-trips through the wizard's
  atomic, sibling-preserving ``write_config``. The headline invariant: writing one
  repo leaves **every other repo and every unknown key intact**.
* :func:`buddhi_review.config.label_gated_ci` (+ the
  :func:`buddhi_review.plan_profile.label_gated_ci` resolver) — per-repo → global →
  default(off), mirroring the ``active_reviewers`` resolution order.
* ``python -m buddhi_review status --repo <owner/repo>`` — JSON
  ``{repo_confirmed, has_global_default}`` for F6's SKILL.md gate to shell out to.
"""
import json
import os
import stat

from buddhi_review import cli, config, plan_profile

REPO = "octocat/Hello-World"
OTHER = "acme/widgets"


# ---------------------------------------------------------------------------
# config.set_repo_keys — the per-repo writer
# ---------------------------------------------------------------------------

def _read(path):
    """Reload the persisted config straight from disk (the real loader)."""
    return config.load_config(path)


def test_set_repo_keys_writes_entry_and_confirms_repo(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    assert config.set_repo_keys(REPO, {"active_reviewers": ["claude"]}, cfg_path) is True
    assert cfg_path.exists()
    cfg = _read(cfg_path)
    # The entry exists (presence == confirmed) and carries the written keys.
    assert config.repo_entry(cfg, REPO) == {"active_reviewers": ["claude"]}
    assert config.active_reviewers(cfg, REPO) == ("claude",)


def test_set_repo_keys_atomic_write_is_0600(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    config.set_repo_keys(REPO, {"label_gated_ci": True}, cfg_path)
    mode = stat.S_IMODE(os.stat(cfg_path).st_mode)
    assert mode == 0o600


def test_set_repo_keys_leaves_sibling_repos_and_unknown_top_keys_intact(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    # Seed a config with a sibling repo, a global default, and a hand-added key the
    # free writer knows nothing about.
    config.set_repo_keys(OTHER, {"active_reviewers": ["copilot", "gemini"]}, cfg_path)
    seeded = _read(cfg_path)
    seeded["plan"] = "max-5x"
    seeded["active_reviewers"] = ["copilot", "claude"]
    seeded["a_hand_added_key"] = {"keep": "me"}
    from buddhi_review.wizard import write_config
    write_config(seeded, cfg_path)

    # Now write a DIFFERENT repo.
    assert config.set_repo_keys(REPO, {"label_gated_ci": True}, cfg_path) is True
    cfg = _read(cfg_path)
    # Sibling repo untouched.
    assert config.repo_entry(cfg, OTHER) == {"active_reviewers": ["copilot", "gemini"]}
    # Unknown top-level keys + the global default survive.
    assert cfg["plan"] == "max-5x"
    assert cfg["active_reviewers"] == ["copilot", "claude"]
    assert cfg["a_hand_added_key"] == {"keep": "me"}
    # The new repo got its entry.
    assert config.label_gated_ci(cfg, REPO) is True


def test_set_repo_keys_deep_merges_into_existing_entry(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    config.set_repo_keys(REPO, {
        "active_reviewers": ["claude", "copilot"],
        "auto_on_open": {"copilot": True},
        "a_future_repo_key": 7,  # an unknown per-repo key
    }, cfg_path)
    # Re-confirm the SAME repo with overlapping + new keys.
    assert config.set_repo_keys(REPO, {
        "label_gated_ci": True,
        "auto_on_open": {"claude": True},  # nested dict → deep-merge, not replace
    }, cfg_path) is True
    entry = config.repo_entry(_read(cfg_path), REPO)
    # Nested auto_on_open merged key-by-key (copilot kept, claude added).
    assert entry["auto_on_open"] == {"copilot": True, "claude": True}
    # The first write's keys (incl. the unknown per-repo key) survive.
    assert entry["active_reviewers"] == ["claude", "copilot"]
    assert entry["a_future_repo_key"] == 7
    assert entry["label_gated_ci"] is True


def test_set_repo_keys_replaces_list_values_wholesale(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    config.set_repo_keys(REPO, {"active_reviewers": ["copilot", "gemini", "codex"]}, cfg_path)
    config.set_repo_keys(REPO, {"active_reviewers": ["claude"]}, cfg_path)
    # Lists replace (never append/merge) — the fleet is the second write's exactly.
    assert config.active_reviewers(_read(cfg_path), REPO) == ("claude",)


def test_set_repo_keys_updates_in_place_case_insensitively(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    config.set_repo_keys("Octocat/Hello-World", {"active_reviewers": ["claude"]}, cfg_path)
    # Re-confirm under a different case — must update the SAME key, not add a sibling.
    config.set_repo_keys("octocat/hello-world", {"label_gated_ci": True}, cfg_path)
    cfg = _read(cfg_path)
    assert len(config.repos_map(cfg)) == 1  # exactly one entry, no duplicate
    entry = config.repo_entry(cfg, REPO)
    assert entry["active_reviewers"] == ["claude"]
    assert entry["label_gated_ci"] is True


def test_set_repo_keys_creates_repos_block_on_fresh_config(tmp_path):
    cfg_path = tmp_path / "config.yaml"  # does not exist yet
    assert not cfg_path.exists()
    assert config.set_repo_keys(REPO, {"active_reviewers": ["claude"]}, cfg_path) is True
    cfg = _read(cfg_path)
    assert isinstance(cfg.get("repos"), dict)
    # Stored under the normalised (lowercased) key, still found case-insensitively.
    assert config.repo_entry(cfg, "OCTOCAT/HELLO-WORLD") is not None


def test_set_repo_keys_rejects_bad_input_without_writing(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    assert config.set_repo_keys(None, {"label_gated_ci": True}, cfg_path) is False
    assert config.set_repo_keys("", {"label_gated_ci": True}, cfg_path) is False
    assert config.set_repo_keys("   ", {"label_gated_ci": True}, cfg_path) is False
    assert config.set_repo_keys(REPO, "not-a-dict", cfg_path) is False  # type: ignore[arg-type]
    # Nothing was written for any rejected call.
    assert not cfg_path.exists()


def test_set_repo_keys_honours_buddhi_config_seam(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    monkeypatch.setenv("BUDDHI_CONFIG", str(cfg_path))
    # No explicit path → resolves through config_path()'s BUDDHI_CONFIG override.
    assert config.set_repo_keys(REPO, {"label_gated_ci": True}) is True
    assert config.label_gated_ci(_read(cfg_path), REPO) is True


# ---------------------------------------------------------------------------
# config.label_gated_ci(cfg, repo) — resolution order
# ---------------------------------------------------------------------------

def test_label_gated_ci_defaults_off():
    assert config.label_gated_ci({}) is False
    assert config.label_gated_ci({}, None) is False
    assert config.label_gated_ci({}, REPO) is False


def test_label_gated_ci_global_applies_without_per_repo():
    cfg = {"label_gated_ci": True}
    assert config.label_gated_ci(cfg) is True
    assert config.label_gated_ci(cfg, None) is True
    assert config.label_gated_ci(cfg, "any/repo") is True  # unconfirmed → global


def test_label_gated_ci_per_repo_wins_over_global():
    cfg = {"label_gated_ci": False, "repos": {REPO: {"label_gated_ci": True}}}
    assert config.label_gated_ci(cfg, REPO) is True
    # A different (unconfirmed) repo falls back to the global flag.
    assert config.label_gated_ci(cfg, "other/repo") is False


def test_label_gated_ci_per_repo_false_overrides_global_true():
    cfg = {"label_gated_ci": True, "repos": {REPO: {"label_gated_ci": False}}}
    assert config.label_gated_ci(cfg, REPO) is False


def test_label_gated_ci_entry_without_key_falls_back_to_global():
    cfg = {"label_gated_ci": True, "repos": {REPO: {"active_reviewers": ["claude"]}}}
    assert config.label_gated_ci(cfg, REPO) is True  # no per-repo key → global


def test_label_gated_ci_per_repo_malformed_value_uses_default_not_global():
    # Per-repo key present but non-bool → the default (off); its presence shadows
    # the global flag (mirrors active_reviewers' malformed-value contract).
    cfg = {"label_gated_ci": True, "repos": {REPO: {"label_gated_ci": "yes"}}}
    assert config.label_gated_ci(cfg, REPO) is False


def test_label_gated_ci_case_insensitive_repo():
    cfg = {"repos": {"octocat/hello-world": {"label_gated_ci": True}}}
    assert config.label_gated_ci(cfg, "Octocat/Hello-World") is True


def test_plan_profile_label_gated_ci_resolver_wired_through_load(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "label_gated_ci: false\n"
        "repos:\n"
        f"  {REPO}:\n"
        "    label_gated_ci: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BUDDHI_CONFIG", str(cfg_file))
    assert plan_profile.label_gated_ci(REPO) is True
    assert plan_profile.label_gated_ci("other/repo") is False  # unconfirmed → global
    assert plan_profile.label_gated_ci() is False              # repo=None → global


# ---------------------------------------------------------------------------
# `status --repo` CLI — JSON for the SKILL.md gate
# ---------------------------------------------------------------------------

def _status_json(capsys, repo):
    rc = cli.main(["status", "--repo", repo])
    out = capsys.readouterr().out  # JSON on stdout; any config warning is on stderr
    return rc, json.loads(out.strip())


def test_status_confirmed_repo(tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.yaml"
    config.set_repo_keys(REPO, {"active_reviewers": ["claude"]}, cfg_file)
    monkeypatch.setenv("BUDDHI_CONFIG", str(cfg_file))
    rc, data = _status_json(capsys, REPO)
    assert rc == 0
    assert data == {"repo_confirmed": True, "has_global_default": False}


def test_status_unconfirmed_repo_with_global_default(tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("active_reviewers: [copilot, claude]\n", encoding="utf-8")
    monkeypatch.setenv("BUDDHI_CONFIG", str(cfg_file))
    rc, data = _status_json(capsys, REPO)
    assert rc == 0
    assert data == {"repo_confirmed": False, "has_global_default": True}


def test_status_unconfirmed_repo_no_global_default(tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("plan: max-5x\n", encoding="utf-8")
    monkeypatch.setenv("BUDDHI_CONFIG", str(cfg_file))
    rc, data = _status_json(capsys, REPO)
    assert rc == 0
    assert data == {"repo_confirmed": False, "has_global_default": False}


def test_status_repo_confirmed_case_insensitive(tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.yaml"
    config.set_repo_keys("octocat/hello-world", {"active_reviewers": []}, cfg_file)
    monkeypatch.setenv("BUDDHI_CONFIG", str(cfg_file))
    _, data = _status_json(capsys, "OCTOCAT/HELLO-WORLD")
    assert data["repo_confirmed"] is True  # presence is the marker, empty fleet counts


def test_status_requires_repo(capsys):
    # argparse enforces --repo; omitting it exits non-zero (usage error).
    import pytest
    with pytest.raises(SystemExit):
        cli.main(["status"])
