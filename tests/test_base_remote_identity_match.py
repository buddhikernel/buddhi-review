"""Tests for base-remote identity matching (owner/repo + host).

:func:`merge._remote_for_repo` identifies which local git remote hosts the
PR's base repository by parsing both sides down to an identity — lowercase
``owner/repo`` via :func:`merge._owner_repo` plus host via :func:`merge._host`
— so a remote configured in ANY URL form (scp-style SSH, ssh://, https, with
or without ``.git``) matches, and an Enterprise-host remote for the same
owner/repo is never mistaken for the github.com one.

This file is the OSS twin of the reference tree's identity-match tests; the
regexes are byte-identical across the trees, so it pins the same behavioral
surface here:

1. ``_owner_repo`` / ``_host`` across every git URL form (ports included).
2. ``_remote_for_repo``'s identity match + host-disambiguation guard.
"""
from __future__ import annotations

import subprocess

from buddhi_review import merge


class TestOwnerRepo:
    def test_scp_style_ssh(self):
        assert merge._owner_repo("git@github.com:owner/repo.git") == "owner/repo"

    def test_ssh_protocol_form(self):
        assert merge._owner_repo("ssh://git@github.com/owner/repo.git") == "owner/repo"

    def test_https(self):
        assert merge._owner_repo("https://github.com/owner/repo") == "owner/repo"

    def test_https_dot_git(self):
        assert merge._owner_repo("https://github.com/owner/repo.git") == "owner/repo"

    def test_trailing_slash(self):
        assert merge._owner_repo("https://github.com/owner/repo/") == "owner/repo"

    def test_case_is_normalised(self):
        assert merge._owner_repo("git@github.com:Owner/Repo.git") == "owner/repo"

    def test_bare_owner_repo(self):
        assert merge._owner_repo("owner/repo") == "owner/repo"

    def test_gh_host_owner_repo_form(self):
        # gh's [HOST/]OWNER/REPO argument form (GitHub Enterprise).
        assert merge._owner_repo("ghe.example.com/owner/repo") == "owner/repo"

    def test_garbage_is_none(self):
        assert merge._owner_repo("") is None
        assert merge._owner_repo("no-slash-here") is None


class TestHost:
    def test_scp_style_ssh(self):
        assert merge._host("git@github.com:owner/repo.git") == "github.com"

    def test_ssh_protocol_form(self):
        assert merge._host("ssh://git@github.com/owner/repo.git") == "github.com"

    def test_https(self):
        assert merge._host("https://github.com/owner/repo") == "github.com"

    def test_enterprise_host(self):
        assert merge._host("ghe.example.com/owner/repo") == "ghe.example.com"

    def test_bare_owner_repo_has_no_host(self):
        assert merge._host("owner/repo") is None

    def test_host_case_is_normalised(self):
        assert merge._host("https://GitHub.COM/owner/repo") == "github.com"

    def test_ssh_protocol_form_with_port(self):
        assert merge._host("ssh://git@github.com:443/owner/repo.git") == "github.com"

    def test_https_with_port(self):
        assert merge._host("https://github.com:8443/owner/repo") == "github.com"

    def test_enterprise_host_with_port(self):
        assert merge._host(
            "https://ghe.example.com:8443/owner/repo.git"
        ) == "ghe.example.com"


# ---- _remote_for_repo: identity match + host guard ---------------------------

def _remotes_run(stdout):
    """A fake ``run`` seam answering ``git remote -v`` with ``stdout``."""
    def run(argv, *, cwd=None, timeout=None):
        assert argv == ["git", "remote", "-v"], f"unexpected call: {argv}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")
    return run


class TestRemoteForRepo:
    def test_matches_any_url_form(self):
        """An ssh://-form remote (which a whole-URL compare would miss) still
        matches the base repo by parsed identity."""
        run = _remotes_run(
            "origin\tssh://git@github.com/owner/repo.git (fetch)\n"
            "origin\tssh://git@github.com/owner/repo.git (push)\n"
        )
        assert merge._remote_for_repo("owner/repo", cwd="/x", run=run) == "origin"

    def test_prefers_identity_over_name(self):
        """In a fork checkout, the remote whose URL is the BASE repo wins,
        regardless of remote names or ordering."""
        run = _remotes_run(
            "origin\tgit@github.com:contributor/repo.git (fetch)\n"
            "origin\tgit@github.com:contributor/repo.git (push)\n"
            "upstream\tgit@github.com:owner/repo.git (fetch)\n"
            "upstream\tgit@github.com:owner/repo.git (push)\n"
        )
        assert merge._remote_for_repo("owner/repo", cwd="/x", run=run) == "upstream"

    def test_host_guard_rejects_wrong_host(self):
        """An Enterprise-host remote for the same owner/repo must not satisfy a
        github.com target (and vice versa)."""
        run = _remotes_run(
            "ghe\tgit@ghe.example.com:owner/repo.git (fetch)\n"
            "ghe\tgit@ghe.example.com:owner/repo.git (push)\n"
        )
        assert merge._remote_for_repo(
            "github.com/owner/repo", cwd="/x", run=run) is None

    def test_no_match_returns_none(self):
        run = _remotes_run(
            "origin\tgit@github.com:someone/else.git (fetch)\n"
            "origin\tgit@github.com:someone/else.git (push)\n"
        )
        assert merge._remote_for_repo("owner/repo", cwd="/x", run=run) is None
