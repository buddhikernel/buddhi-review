"""F2 — the wizard's provisioning engine: owner-type detection, the server-side
branch+PR file installer, safe (repo-default / org-confirmed) secret scoping, and
the org-aware + dual-credential existence check.

These guard the security-sensitive surface ported from the reference wizard: the
secret never goes org-wide without an explicit confirmation + an org-admin check +
a repo-scope fallback, the org-aware existence check can't false-positive or miss
an org-set secret, and the installer always targets the default branch."""
import io
import types

import pytest

from buddhi_review import wizard


def _R(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _recorder(router):
    """Wrap a routing function as an injectable `run`, recording every argv."""
    calls = []

    def run(argv, cwd=None, timeout=30, input=None):
        calls.append({"argv": list(argv), "input": input})
        return router(list(argv), input)

    return run, calls


def _has(argv, frag):
    """Whether any argv token contains `frag` — robust to `--paginate` and other
    flags shifting the API path off a fixed index."""
    return any(frag in tok for tok in argv)


def _startswith(argv, frag):
    return any(tok.startswith(frag) for tok in argv)


# ── _owner_type ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("owner,reply,rc,expect", [
    ("acme/widgets", "Organization\n", 0, "Organization"),
    ("acme", "Organization\n", 0, "Organization"),
    ("alice/widgets", "User\n", 0, "User"),
    ("acme", "", 1, None),               # gh error
    ("acme", "Bot\n", 0, None),          # unexpected value
    ("", "", 0, None),                   # no owner
])
def test_owner_type(owner, reply, rc, expect):
    def router(argv, _inp):
        assert argv[:2] == ["gh", "api"]
        # the repo half must be dropped — only the bare login is queried
        assert argv[2] == f"users/{str(owner).split('/')[0]}"
        return _R(returncode=rc, stdout=reply)
    run, _ = _recorder(router)
    assert wizard._owner_type(owner, run=run) == expect


def test_owner_type_malformed_login_never_calls_gh():
    called = {"n": 0}

    def router(argv, _inp):
        called["n"] += 1
        return _R()
    run, _ = _recorder(router)
    # a login with a space / shell metachar is rejected before any gh call
    assert wizard._owner_type("bad owner!", run=run) is None
    assert called["n"] == 0


# ── _create_file_pr (server-side installer) ─────────────────────────────────────────

def _installer_router(*, head_rc=0, head_sha="deadbeef", ref_rc=0,
                      branch_probe_rc=0, file_probe_rc=1, file_sha="",
                      put_rc=0, pr_rc=0, pr_url="https://github.com/acme/widgets/pull/7"):
    def router(argv, _inp):
        if argv[:2] == ["gh", "pr"]:
            return _R(returncode=pr_rc, stdout=(pr_url + "\n") if pr_rc == 0 else "")
        if argv[:2] == ["gh", "api"]:
            if "-X" in argv and "PUT" in argv:
                return _R(returncode=put_rc)
            if argv[2].endswith("/git/refs"):
                return _R(returncode=ref_rc)
            if "/git/ref/heads/" in argv[2] and "--jq" in argv:
                return _R(returncode=head_rc, stdout=(head_sha + "\n") if head_rc == 0 else "")
            if "/git/ref/heads/" in argv[2]:           # branch existence probe
                return _R(returncode=branch_probe_rc)
            if "/contents/" in argv[2] and "--jq" in argv:  # recovery file-sha probe
                return _R(returncode=file_probe_rc, stdout=(file_sha + "\n") if file_probe_rc == 0 else "")
        return _R()
    return router


def test_create_file_pr_happy_path_targets_default_branch():
    run, calls = _recorder(_installer_router())
    ok, detail = wizard._create_file_pr(
        "acme/widgets", "main", ".github/workflows/claude-code-review.yml",
        "name: x\n", "msg", "title", "buddhi/add-claude-review-workflow", run=run)
    assert ok is True
    assert detail == "https://github.com/acme/widgets/pull/7"

    # ref is created off the head SHA, on the requested fresh branch
    ref = next(c["argv"] for c in calls if c["argv"][:2] == ["gh", "api"]
               and c["argv"][2].endswith("/git/refs"))
    assert "ref=refs/heads/buddhi/add-claude-review-workflow" in ref
    assert "sha=deadbeef" in ref

    # the PUT writes the requested path, on the fresh branch (not the default)
    put = next(c["argv"] for c in calls if "-X" in c["argv"] and "PUT" in c["argv"])
    assert put[4] == "repos/acme/widgets/contents/.github/workflows/claude-code-review.yml"
    assert "branch=buddhi/add-claude-review-workflow" in put

    # the PR always opens base=DEFAULT, head=the fresh branch — never the user's branch
    pr = next(c["argv"] for c in calls if c["argv"][:2] == ["gh", "pr"])
    assert pr[pr.index("--base") + 1] == "main"
    assert pr[pr.index("--head") + 1] == "buddhi/add-claude-review-workflow"
    # a fresh write carries no blob-sha update arg
    assert not any(tok.startswith("sha=") for tok in put)


def test_create_file_pr_recovery_updates_existing_file():
    # branch already exists from a partial run; the file is already committed → the
    # PUT must carry the existing blob sha (else GitHub 422s on the update).
    run, calls = _recorder(_installer_router(ref_rc=1, branch_probe_rc=0,
                                             file_probe_rc=0, file_sha="blobsha123"))
    ok, _ = wizard._create_file_pr(
        "acme/widgets", "main", ".github/workflows/claude-code-review.yml",
        "name: x\n", "msg", "title", "buddhi/add-claude-review-workflow", run=run)
    assert ok is True
    put = next(c["argv"] for c in calls if "-X" in c["argv"] and "PUT" in c["argv"])
    assert "sha=blobsha123" in put


def test_create_file_pr_branch_create_fails_and_absent():
    run, _ = _recorder(_installer_router(ref_rc=1, branch_probe_rc=1))
    ok, detail = wizard._create_file_pr(
        "acme/widgets", "main", "p", "c", "m", "t", "b", run=run)
    assert ok is False
    assert "couldn't create branch" in detail


@pytest.mark.parametrize("kwargs,frag", [
    (dict(head_rc=1), "default branch head"),
    (dict(put_rc=1), "couldn't write the file"),
    (dict(pr_rc=1), "opening the PR failed"),
])
def test_create_file_pr_failure_modes(kwargs, frag):
    run, _ = _recorder(_installer_router(**kwargs))
    ok, detail = wizard._create_file_pr(
        "acme/widgets", "main", "p", "c", "m", "t", "b", run=run)
    assert ok is False
    assert frag in detail


def test_create_file_pr_recovery_path_defaults_to_path():
    # When the branch already exists, the file probed for its blob sha is `path`
    # itself (recovery_path defaults to path) — not some other file.
    seen = {"probe": None}

    def router(argv, _inp):
        if argv[:2] == ["gh", "api"] and "/contents/" in argv[2] and "--jq" in argv:
            seen["probe"] = argv[2]
            return _R(returncode=0, stdout="s\n")
        return _installer_router(ref_rc=1, branch_probe_rc=0, file_probe_rc=0,
                                 file_sha="s")(argv, _inp)
    run, _ = _recorder(router)
    wizard._create_file_pr("acme/widgets", "main", "dir/file.yml", "c", "m", "t",
                           "b", run=run)
    assert seen["probe"].startswith("repos/acme/widgets/contents/dir/file.yml")


# ── secret scoping (_set_secret_scoped / _set_gh_secret) ────────────────────────────

def _secret_router(*, role="admin", org_set_rc=0, repo_set_rc=0, existing_repos=None):
    def router(argv, _inp):
        if argv[:2] == ["gh", "api"] and _startswith(argv, "user/memberships/orgs/"):
            return _R(returncode=0, stdout=role + "\n")
        if argv[:2] == ["gh", "api"] and _has(argv, "/repositories"):
            return _R(returncode=0, stdout="\n".join(existing_repos or []) + "\n")
        if argv[:3] == ["gh", "secret", "set"]:
            return _R(returncode=org_set_rc if "--org" in argv else repo_set_rc,
                      stderr="denied" if (org_set_rc and "--org" in argv) else "")
        return _R()
    return router


def test_set_secret_scoped_default_is_repo():
    run, calls = _recorder(_secret_router())
    ok, _, scope = wizard._set_secret_scoped("acme/widgets", "TOK", "val",
                                             prefer_org=False, run=run)
    assert ok is True and scope == "repo"
    sets = [c["argv"] for c in calls if c["argv"][:3] == ["gh", "secret", "set"]]
    assert sets == [["gh", "secret", "set", "TOK", "--repo", "acme/widgets"]]
    # the value goes via stdin, never on argv
    setcall = next(c for c in calls if c["argv"][:3] == ["gh", "secret", "set"])
    assert setcall["input"] == "val"
    assert "val" not in setcall["argv"]


def test_set_secret_scoped_org_optin_admin():
    run, calls = _recorder(_secret_router(role="admin", existing_repos=["other"]))
    ok, _, scope = wizard._set_secret_scoped("acme/widgets", "TOK", "val",
                                             prefer_org=True, run=run)
    assert ok is True and scope == "org"
    org_set = next(c["argv"] for c in calls if c["argv"][:3] == ["gh", "secret", "set"])
    assert "--org" in org_set and org_set[org_set.index("--org") + 1] == "acme"
    assert org_set[org_set.index("--visibility") + 1] == "selected"
    # the current repo is UNIONED into the existing selected list, not replacing it
    repos = org_set[org_set.index("--repos") + 1].split(",")
    assert set(repos) == {"other", "widgets"}


def test_set_secret_scoped_non_admin_falls_back_to_repo():
    run, calls = _recorder(_secret_router(role="member"))
    ok, _, scope = wizard._set_secret_scoped("acme/widgets", "TOK", "val",
                                             prefer_org=True, run=run)
    assert ok is True and scope == "repo"
    # CRITICAL: a non-admin must never trigger an org-wide set
    assert not any("--org" in c["argv"] for c in calls
                   if c["argv"][:3] == ["gh", "secret", "set"])


def test_set_secret_scoped_org_set_failure_falls_back_to_repo():
    run, calls = _recorder(_secret_router(role="admin", org_set_rc=1))
    ok, _, scope = wizard._set_secret_scoped("acme/widgets", "TOK", "val",
                                             prefer_org=True, run=run)
    assert ok is True and scope == "repo"
    sets = [c["argv"] for c in calls if c["argv"][:3] == ["gh", "secret", "set"]]
    assert any("--org" in s for s in sets)   # org attempt happened
    assert any("--repo" in s for s in sets)  # …then repo fallback


# ── org-aware + dual-credential existence check (_gh_secret_exists) ──────────────────

def _exists_router(*, owner_type="User", repo_secrets=(), org_secrets=None):
    def router(argv, _inp):
        if argv[:2] == ["gh", "api"] and _startswith(argv, "users/"):
            return _R(returncode=0, stdout=owner_type + "\n")
        if argv[:3] == ["gh", "secret", "list"]:
            return _R(returncode=0, stdout="\n".join(f"{s}\t2026-01-01" for s in repo_secrets))
        if argv[:2] == ["gh", "api"] and _has(argv, "organization-secrets"):
            if org_secrets is None:
                return _R(returncode=1)
            return _R(returncode=0, stdout="\n".join(org_secrets))
        return _R()
    return router


def test_gh_secret_exists_repo_level():
    run, _ = _recorder(_exists_router(repo_secrets=["CLAUDE_CODE_OAUTH_TOKEN"]))
    assert wizard._gh_secret_exists("acme/widgets", "CLAUDE_CODE_OAUTH_TOKEN", run=run) is True
    run2, _ = _recorder(_exists_router(repo_secrets=[]))
    assert wizard._gh_secret_exists("acme/widgets", "CLAUDE_CODE_OAUTH_TOKEN", run=run2) is False


def test_gh_secret_exists_org_level_found():
    # The secret is set org-wide (shared to this repo) — absent from --repo list but
    # present in the per-repo organization-secrets endpoint. Must be found.
    run, _ = _recorder(_exists_router(owner_type="Organization", repo_secrets=[],
                                      org_secrets=["CLAUDE_CODE_OAUTH_TOKEN"]))
    assert wizard._gh_secret_exists("acme/widgets", "CLAUDE_CODE_OAUTH_TOKEN", run=run) is True


def test_gh_secret_exists_no_substring_false_positive():
    run, _ = _recorder(_exists_router(repo_secrets=["CLAUDE_CODE_OAUTH_TOKEN_OLD"]))
    assert wizard._gh_secret_exists("acme/widgets", "CLAUDE_CODE_OAUTH_TOKEN", run=run) is False


def test_gh_secret_exists_unknown_when_both_checks_fail():
    def router(argv, _inp):
        if argv[:2] == ["gh", "api"] and argv[2].startswith("users/"):
            return _R(returncode=0, stdout="Organization\n")
        return _R(returncode=1)  # repo list AND org endpoint both error
    run, _ = _recorder(router)
    assert wizard._gh_secret_exists("acme/widgets", "CLAUDE_CODE_OAUTH_TOKEN", run=run) is None


# ── dual-credential gate in _set_claude_secret ──────────────────────────────────────

@pytest.mark.parametrize("repo_secrets,expect", [
    (["CLAUDE_CODE_OAUTH_TOKEN"], "present"),          # OAuth only
    (["ANTHROPIC_API_KEY"], "present"),                # ANTHROPIC only
    (["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"], "present"),  # both
])
def test_set_claude_secret_dual_credential_present(monkeypatch, repo_secrets, expect):
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, _ = _recorder(_exists_router(owner_type="User", repo_secrets=repo_secrets))
    pal, buf = wizard._Palette(False), io.StringIO()
    status = wizard._set_claude_secret(
        "alice/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "", pal=pal, stream=buf)
    assert status == expect


def test_set_claude_secret_neither_credential_proceeds(monkeypatch):
    # Neither present → the wizard proceeds to offer minting (blank token → skipped).
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, _ = _recorder(_exists_router(owner_type="User", repo_secrets=[]))
    pal, buf = wizard._Palette(False), io.StringIO()
    status = wizard._set_claude_secret(
        "alice/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "", pal=pal, stream=buf)
    assert status == "skipped"   # offered, but a blank token aborts the set


def test_set_claude_secret_anthropic_present_skips_org_secret_set(monkeypatch):
    # A user who already has ANTHROPIC_API_KEY must NOT be re-prompted / re-set.
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, calls = _recorder(_exists_router(owner_type="Organization", repo_secrets=[],
                                          org_secrets=["ANTHROPIC_API_KEY"]))
    pal, buf = wizard._Palette(False), io.StringIO()
    status = wizard._set_claude_secret(
        "acme/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "tok", pal=pal, stream=buf)
    assert status == "present"
    assert not any(c["argv"][:3] == ["gh", "secret", "set"] for c in calls)


# ── _set_claude_secret org opt-in (blast-radius confirmation) ───────────────────────

def _full_secret_router(*, owner_type="Organization", role="admin",
                        repo_secrets=(), repo_set_rc=0, org_set_rc=0):
    def router(argv, _inp):
        if argv[:2] == ["gh", "api"] and _startswith(argv, "users/"):
            return _R(returncode=0, stdout=owner_type + "\n")
        if argv[:2] == ["gh", "api"] and _startswith(argv, "user/memberships/orgs/"):
            return _R(returncode=0, stdout=role + "\n")
        if argv[:3] == ["gh", "secret", "list"]:
            return _R(returncode=0, stdout="\n".join(f"{s}\t2026" for s in repo_secrets))
        if argv[:2] == ["gh", "api"] and _has(argv, "organization-secrets"):
            return _R(returncode=0, stdout="")
        if argv[:2] == ["gh", "api"] and _has(argv, "/repositories"):
            return _R(returncode=0, stdout="")
        if argv[:3] == ["gh", "secret", "set"]:
            return _R(returncode=org_set_rc if "--org" in argv else repo_set_rc)
        return _R()
    return router


def test_set_claude_secret_org_optin_declined_stays_repo(monkeypatch):
    # CLAIM #1 guard: on an org repo, choosing "this repo only" (idx 0) must never
    # set an org-wide secret.
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, calls = _recorder(_full_secret_router())
    pal, buf = wizard._Palette(False), io.StringIO()
    status = wizard._set_claude_secret(
        "acme/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "tok", pal=pal, stream=buf,
        single_select=lambda *a, **k: 0)   # "this repository only"
    assert status == "set"
    sets = [c["argv"] for c in calls if c["argv"][:3] == ["gh", "secret", "set"]]
    assert sets and all("--org" not in s for s in sets)


def test_set_claude_secret_org_optin_confirmed_goes_org(monkeypatch):
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, calls = _recorder(_full_secret_router())
    pal, buf = wizard._Palette(False), io.StringIO()
    status = wizard._set_claude_secret(
        "acme/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "tok", pal=pal, stream=buf,
        single_select=lambda *a, **k: 1)   # "org-wide, scoped to this repo"
    assert status == "set"
    assert any("--org" in c["argv"] for c in calls
              if c["argv"][:3] == ["gh", "secret", "set"])


def test_set_claude_secret_personal_repo_never_offers_org(monkeypatch):
    # A personal (User) account must never see the org opt-in selector at all.
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, _ = _recorder(_full_secret_router(owner_type="User"))
    pal, buf = wizard._Palette(False), io.StringIO()
    selector_calls = {"n": 0}

    def ss(*a, **k):
        selector_calls["n"] += 1
        return 0
    status = wizard._set_claude_secret(
        "alice/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "tok", pal=pal, stream=buf, single_select=ss)
    assert status == "set"
    assert selector_calls["n"] == 0


# ── _offer_install_claude_workflow (server-side vs local) ───────────────────────────

def test_offer_install_feature_branch_opens_server_side_pr(monkeypatch, tmp_path):
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)

    def router(argv, _inp):
        if argv[:2] == ["git", "-C"] and "rev-parse" in argv:
            return _R(returncode=0, stdout="feature/x\n")     # current branch
        if argv[:3] == ["gh", "repo", "view"]:
            return _R(returncode=0, stdout="main\n")          # default branch
        # installer calls succeed
        return _installer_router()(argv, _inp)
    run, calls = _recorder(router)
    pal, buf = wizard._Palette(False), io.StringIO()
    result = wizard._offer_install_claude_workflow(
        "acme/widgets", str(tmp_path), run=run, pal=pal, stream=buf,
        input_fn=lambda *a: "")    # accept the default-True PR offer
    assert result == "pr"
    pr = next(c["argv"] for c in calls if c["argv"][:2] == ["gh", "pr"])
    assert pr[pr.index("--base") + 1] == "main"
    # nothing was written into the local feature checkout
    assert not (tmp_path / ".github" / "workflows" / "claude-code-review.yml").exists()


def test_offer_install_default_branch_writes_local(monkeypatch, tmp_path):
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)

    def router(argv, _inp):
        if argv[:2] == ["git", "-C"] and "rev-parse" in argv:
            return _R(returncode=0, stdout="main\n")          # on the default branch
        if argv[:3] == ["gh", "repo", "view"]:
            return _R(returncode=0, stdout="main\n")
        return _R()
    run, calls = _recorder(router)
    pal, buf = wizard._Palette(False), io.StringIO()
    result = wizard._offer_install_claude_workflow(
        "acme/widgets", str(tmp_path), run=run, pal=pal, stream=buf,
        input_fn=lambda *a: "")    # accept the default-True local-write offer
    assert result is True
    assert (tmp_path / ".github" / "workflows" / "claude-code-review.yml").exists()
    # on the default branch there is no need to open a PR
    assert not any(c["argv"][:2] == ["gh", "pr"] for c in calls)


# ── P7 #1 — the re-check prompt phrasing ────────────────────────────────────────────

def test_claude_recheck_prompt_is_clear_and_names_the_file():
    p = wizard._CLAUDE_RECHECK_PROMPT
    assert "claude-code-review.yml" in p
    assert "CLAUDE_CODE_OAUTH_TOKEN" in p
    # the two facts are split into a committed-on-default-branch clause + a
    # secret-set clause, then a single confirm — not one mashed-together question
    assert "default branch" in p
    assert p.strip().endswith("?")
