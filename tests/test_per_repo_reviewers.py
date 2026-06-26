"""Per-repo reviewer resolution — the data layer.

Two layers:

* :mod:`buddhi_review.config` owns the ``repos:`` schema and the resolution order
  (a confirmed ``repos[<owner/repo>]`` entry → the top-level global default → the
  built-in defaults). These functions take a plain ``cfg`` dict, so the whole
  resolution lattice is testable with zero I/O.
* :mod:`buddhi_review.plan_profile` exposes the named resolvers the round loop
  consumes: ``configured_bots`` / ``auto_on_open_for`` / ``auto_on_open_map``
  / ``repo_confirmed`` / ``has_global_default``. They supply the loaded config.

The headline invariant: **``repo=None`` resolves to the global-default fleet** —
callers (``round_driver``/``cli``) that pass no repo get the global default.
"""
from buddhi_review import config, plan_profile

REPO = "octocat/Hello-World"


# ---------------------------------------------------------------------------
# config.norm_repo / repos_map / repo_entry — the schema primitives
# ---------------------------------------------------------------------------

def test_norm_repo_lowercases_and_strips():
    assert config.norm_repo("Octocat/Hello-World") == "octocat/hello-world"
    assert config.norm_repo("  Owner/Repo  ") == "owner/repo"


def test_norm_repo_empty_is_none():
    assert config.norm_repo(None) is None
    assert config.norm_repo("") is None
    assert config.norm_repo("   ") is None


def test_repos_map_tolerates_absent_or_malformed():
    assert config.repos_map({}) == {}
    assert config.repos_map({"repos": None}) == {}
    assert config.repos_map({"repos": ["not", "a", "map"]}) == {}
    m = {"repos": {REPO: {"active_reviewers": ["claude"]}}}
    assert config.repos_map(m) == {REPO: {"active_reviewers": ["claude"]}}


def test_repo_entry_case_insensitive_lookup():
    cfg = {"repos": {"Octocat/Hello-World": {"active_reviewers": ["claude"]}}}
    assert config.repo_entry(cfg, "octocat/hello-world") == {"active_reviewers": ["claude"]}
    assert config.repo_entry(cfg, "OCTOCAT/HELLO-WORLD") == {"active_reviewers": ["claude"]}


def test_repo_entry_none_when_missing_or_none_repo():
    cfg = {"repos": {REPO: {"active_reviewers": ["claude"]}}}
    assert config.repo_entry(cfg, None) is None
    assert config.repo_entry(cfg, "other/repo") is None
    assert config.repo_entry({}, REPO) is None


def test_repo_entry_ignores_non_dict_entry():
    cfg = {"repos": {REPO: ["claude"]}}  # malformed: list, not a mapping
    assert config.repo_entry(cfg, REPO) is None


def test_repo_entry_empty_fleet_still_confirms():
    cfg = {"repos": {REPO: {"active_reviewers": []}}}
    assert config.repo_entry(cfg, REPO) == {"active_reviewers": []}


# ---------------------------------------------------------------------------
# config.has_global_default
# ---------------------------------------------------------------------------

def test_has_global_default():
    assert config.has_global_default({"active_reviewers": ["copilot"]}) is True
    assert config.has_global_default({"active_reviewers": []}) is True  # empty list still set
    assert config.has_global_default({}) is False
    assert config.has_global_default({"active_reviewers": "copilot"}) is False  # non-list


# ---------------------------------------------------------------------------
# config.active_reviewers(cfg, repo) — resolution order
# ---------------------------------------------------------------------------

def test_active_reviewers_repo_none_byte_identical():
    # No repo, no config → built-in four-bot set (the default).
    assert config.active_reviewers({}) == config.DEFAULT_REVIEWERS
    assert config.active_reviewers({}, None) == config.DEFAULT_REVIEWERS
    # No repo, a global default → that fleet, verbatim.
    cfg = {"active_reviewers": ["copilot", "claude"]}
    assert config.active_reviewers(cfg) == ("copilot", "claude")
    assert config.active_reviewers(cfg, None) == ("copilot", "claude")


def test_active_reviewers_per_repo_wins_over_global():
    cfg = {
        "active_reviewers": ["copilot", "gemini", "codex", "claude"],
        "repos": {REPO: {"active_reviewers": ["claude"]}},
    }
    assert config.active_reviewers(cfg, REPO) == ("claude",)
    # A different (unconfirmed) repo falls back to the global default.
    assert config.active_reviewers(cfg, "other/repo") == ("copilot", "gemini", "codex", "claude")


def test_active_reviewers_per_repo_empty_list_honoured():
    cfg = {"active_reviewers": ["copilot"], "repos": {REPO: {"active_reviewers": []}}}
    assert config.active_reviewers(cfg, REPO) == ()


def test_active_reviewers_entry_without_key_falls_back_to_global():
    # The repo is confirmed (entry exists) but carries no active_reviewers key →
    # the global default fleet applies for this repo.
    cfg = {"active_reviewers": ["copilot"], "repos": {REPO: {"auto_on_open": {"copilot": False}}}}
    assert config.active_reviewers(cfg, REPO) == ("copilot",)


def test_active_reviewers_per_repo_malformed_value_uses_builtin_not_global():
    # Per-repo active_reviewers present but non-list → built-in defaults (the key's
    # presence shadows the global default; a malformed value never silently reuses
    # the global fleet).
    cfg = {"active_reviewers": ["copilot"], "repos": {REPO: {"active_reviewers": None}}}
    assert config.active_reviewers(cfg, REPO) == config.DEFAULT_REVIEWERS


def test_active_reviewers_case_insensitive_repo():
    cfg = {"repos": {"octocat/hello-world": {"active_reviewers": ["gemini"]}}}
    assert config.active_reviewers(cfg, "Octocat/Hello-World") == ("gemini",)


# ---------------------------------------------------------------------------
# config.auto_on_open(cfg, bot, repo) — resolution order
# ---------------------------------------------------------------------------

def test_auto_on_open_repo_none_byte_identical():
    # Defaults: claude summoned (False), the GitHub-App bots auto-comment (True).
    assert config.auto_on_open({}, "claude") is False
    assert config.auto_on_open({}, "claude", None) is False
    assert config.auto_on_open({}, "copilot") is True
    assert config.auto_on_open({}, "unknown-bot") is True  # unknown → True
    cfg = {"auto_on_open": {"copilot": False}}
    assert config.auto_on_open(cfg, "copilot") is False
    assert config.auto_on_open(cfg, "copilot", None) is False


def test_auto_on_open_per_repo_block_wins():
    cfg = {
        "auto_on_open": {"copilot": True},
        "repos": {REPO: {"auto_on_open": {"copilot": False, "claude": True}}},
    }
    assert config.auto_on_open(cfg, "copilot", REPO) is False
    assert config.auto_on_open(cfg, "claude", REPO) is True
    # A bot absent from the per-repo block falls to DEFAULT_AUTO_ON_OPEN.
    assert config.auto_on_open(cfg, "gemini", REPO) is True


def test_auto_on_open_entry_without_key_falls_back_to_global():
    cfg = {
        "auto_on_open": {"copilot": False},
        "repos": {REPO: {"active_reviewers": ["copilot"]}},  # no auto_on_open key
    }
    assert config.auto_on_open(cfg, "copilot", REPO) is False  # top-level applies


def test_auto_on_open_per_repo_malformed_block_shadows_global():
    # A per-repo auto_on_open key present but non-dict shadows the top-level block
    # (every bot then falls to its per-bot default — not the global override).
    cfg = {
        "auto_on_open": {"copilot": False},
        "repos": {REPO: {"auto_on_open": None}},
    }
    assert config.auto_on_open(cfg, "copilot", REPO) is True  # DEFAULT, not the global False


def test_auto_on_open_case_insensitive_repo():
    cfg = {"repos": {"octocat/hello-world": {"auto_on_open": {"claude": True}}}}
    assert config.auto_on_open(cfg, "claude", "Octocat/Hello-World") is True


# ---------------------------------------------------------------------------
# plan_profile resolvers — the named API the loop consumes
# ---------------------------------------------------------------------------

def _patch_cfg(monkeypatch, cfg):
    """Pin the config the plan_profile resolvers read (no file/network I/O)."""
    monkeypatch.setattr(plan_profile, "load_config", lambda: cfg)


def test_configured_bots_resolves_per_repo(monkeypatch):
    _patch_cfg(monkeypatch, {
        "active_reviewers": ["copilot", "gemini", "codex", "claude"],
        "repos": {REPO: {"active_reviewers": ["claude", "copilot"]}},
    })
    assert plan_profile.configured_bots(REPO) == ["claude", "copilot"]
    assert plan_profile.configured_bots("other/repo") == ["copilot", "gemini", "codex", "claude"]


def test_configured_bots_repo_none_matches_global_default(monkeypatch):
    cfg = {"active_reviewers": ["copilot", "claude"]}
    _patch_cfg(monkeypatch, cfg)
    # Same content the loop reads via config.active_reviewers.
    assert plan_profile.configured_bots(None) == list(config.active_reviewers(cfg))
    assert plan_profile.configured_bots() == ["copilot", "claude"]


def test_configured_bots_default_fleet_when_unconfigured(monkeypatch):
    _patch_cfg(monkeypatch, {})
    assert plan_profile.configured_bots() == list(config.DEFAULT_REVIEWERS)


def test_auto_on_open_for_resolves_per_repo(monkeypatch):
    _patch_cfg(monkeypatch, {
        "auto_on_open": {"claude": False},
        "repos": {REPO: {"auto_on_open": {"claude": True}}},
    })
    assert plan_profile.auto_on_open_for("claude", REPO) is True
    assert plan_profile.auto_on_open_for("claude", "other/repo") is False
    assert plan_profile.auto_on_open_for("claude") is False  # repo=None → global


def test_auto_on_open_map_only_enabled_reviewers(monkeypatch):
    _patch_cfg(monkeypatch, {
        "repos": {REPO: {
            "active_reviewers": ["claude", "copilot"],
            "auto_on_open": {"claude": True},  # copilot falls to default True
        }},
    })
    assert plan_profile.auto_on_open_map(REPO) == {"claude": True, "copilot": True}


def test_auto_on_open_map_default_fleet(monkeypatch):
    _patch_cfg(monkeypatch, {})
    assert plan_profile.auto_on_open_map() == {
        "copilot": True, "gemini": True, "codex": True, "claude": False,
    }


def test_repo_confirmed(monkeypatch):
    _patch_cfg(monkeypatch, {"repos": {REPO: {"active_reviewers": []}}})
    assert plan_profile.repo_confirmed(REPO) is True       # presence is the marker
    assert plan_profile.repo_confirmed("other/repo") is False
    assert plan_profile.repo_confirmed(None) is False


def test_has_global_default_resolver(monkeypatch):
    _patch_cfg(monkeypatch, {"active_reviewers": ["copilot"]})
    assert plan_profile.has_global_default() is True
    _patch_cfg(monkeypatch, {})
    assert plan_profile.has_global_default() is False


def test_resolvers_read_config_through_real_load(monkeypatch, tmp_path):
    # End-to-end through the real config loader via the BUDDHI_CONFIG seam —
    # proves the resolvers are wired to load_config(), not just monkeypatched.
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "active_reviewers: [copilot, claude]\n"
        "repos:\n"
        f"  {REPO}:\n"
        "    active_reviewers: [claude]\n"
        "    auto_on_open:\n"
        "      claude: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BUDDHI_CONFIG", str(cfg_file))
    assert plan_profile.configured_bots(REPO) == ["claude"]
    assert plan_profile.configured_bots() == ["copilot", "claude"]
    assert plan_profile.auto_on_open_for("claude", REPO) is True
    assert plan_profile.repo_confirmed(REPO) is True
    assert plan_profile.has_global_default() is True
