"""F2 — the wizard's provisioning engine: owner-type detection, the server-side
branch+PR file installer, safe (repo-default / org-confirmed) secret scoping, and
the org-aware + dual-credential existence check.

These guard the security-sensitive surface ported from the reference wizard: the
secret never goes org-wide without an explicit confirmation + an org-admin check +
a repo-scope fallback, the org-aware existence check can't false-positive or miss
an org-set secret, and the installer always targets the default branch."""
import io
import os
import subprocess
import types

import pytest

from buddhi_review import wizard
from conftest import _yn_bridge


def _R(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# The token-mint flow now opens with a consent gate (a `_ask_yes_no` rendered as a
# single_select on a forced TTY) before it spawns `claude setup-token`. In tests
# that must REACH the paste, the injected selector answers that consent prompt Yes
# (index 0) and routes the org-scope prompt to `org_idx`. The two prompts are
# distinguished by text: the org selector's prompt contains "organization".
def _consenting_select(org_idx=0):
    def ss(prompt, options, **kw):
        if "organization" in (prompt or ""):
            return org_idx
        return 0   # consent prompt → Yes
    return ss


def _recorder(router):
    """Wrap a routing function as an injectable `run`, recording every argv.

    Accepts the full seam signature including the F10 ``env`` kwarg so a call site
    that threads an isolated credential env (the token validator) does not break the
    fake (`_default_run(argv, *, timeout, input, env)`)."""
    calls = []

    def run(argv, cwd=None, timeout=30, input=None, env=None):
        calls.append({"argv": list(argv), "input": input, "env": env})
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


def test_create_file_pr_returns_existing_pr_when_create_fails_on_duplicate():
    """A re-run whose branch already has an OPEN PR: the branch push succeeds but
    `gh pr create` fails on the duplicate. Instead of a scary failure, detect the
    existing PR (`gh pr view <branch>`) and return it — idempotent, like create-pr.sh."""
    def router(argv, _inp):
        if argv[:3] == ["gh", "pr", "create"]:
            return _R(returncode=1, stdout="")                       # duplicate → fails
        if argv[:3] == ["gh", "pr", "view"]:
            return _R(returncode=0, stdout="https://github.com/acme/widgets/pull/20\n")
        if argv[:2] == ["gh", "api"]:
            if "-X" in argv and "PUT" in argv:
                return _R(returncode=0)
            if argv[2].endswith("/git/refs"):
                return _R(returncode=0)
            if "/git/ref/heads/" in argv[2] and "--jq" in argv:
                return _R(returncode=0, stdout="deadbeef\n")
            if "/contents/" in argv[2] and "--jq" in argv:
                return _R(returncode=1)
        return _R()
    run, calls = _recorder(router)
    ok, detail = wizard._create_file_pr(
        "acme/widgets", "main", "p", "c", "m", "t", "buddhi/update-x", run=run)
    assert ok is True
    assert detail == "https://github.com/acme/widgets/pull/20"
    # it queried the existing PR for THIS branch (not some other selector)
    view = next(c["argv"] for c in calls if c["argv"][:3] == ["gh", "pr", "view"])
    assert "buddhi/update-x" in view


def test_create_file_pr_still_fails_when_create_fails_and_no_existing_pr():
    """gh pr create fails AND no PR exists for the branch → still a real failure."""
    def router(argv, _inp):
        if argv[:3] == ["gh", "pr", "create"]:
            return _R(returncode=1, stdout="")
        if argv[:3] == ["gh", "pr", "view"]:
            return _R(returncode=1, stdout="")                       # no PR for the branch
        if argv[:2] == ["gh", "api"]:
            if "-X" in argv and "PUT" in argv:
                return _R(returncode=0)
            if argv[2].endswith("/git/refs"):
                return _R(returncode=0)
            if "/git/ref/heads/" in argv[2] and "--jq" in argv:
                return _R(returncode=0, stdout="deadbeef\n")
            if "/contents/" in argv[2] and "--jq" in argv:
                return _R(returncode=1)
        return _R()
    run, _ = _recorder(router)
    ok, detail = wizard._create_file_pr(
        "acme/widgets", "main", "p", "c", "m", "t", "b", run=run)
    assert ok is False
    assert "opening the PR failed" in detail


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
    # Neither present → the wizard proceeds to offer minting. Consent Yes reaches the
    # paste; a blank token there routes to the by-hand instructions ("deferred").
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, _ = _recorder(_exists_router(owner_type="User", repo_secrets=[]))
    pal, buf = wizard._Palette(False), io.StringIO()
    status = wizard._set_claude_secret(
        "alice/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "", pal=pal, stream=buf,
        single_select=_consenting_select())
    assert status == "skipped"   # offered + consented, but a blank token aborts the set


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
        single_select=_consenting_select(org_idx=0),   # consent Yes; "this repo only"
        validate_fn=lambda *a, **k: ("valid", ""))   # F10: skip the real isolated ping
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
        single_select=_consenting_select(org_idx=1),   # consent Yes; "org-wide"
        validate_fn=lambda *a, **k: ("valid", ""))   # F10: skip the real isolated ping
    assert status == "set"
    assert any("--org" in c["argv"] for c in calls
              if c["argv"][:3] == ["gh", "secret", "set"])


def test_set_claude_secret_personal_repo_never_offers_org(monkeypatch):
    # A personal (User) account must never see the org opt-in selector at all. The
    # consent gate DOES render (a single_select on a TTY), so count only org prompts.
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, _ = _recorder(_full_secret_router(owner_type="User"))
    pal, buf = wizard._Palette(False), io.StringIO()
    org_selector_calls = {"n": 0}

    def ss(prompt, options, **kw):
        if "organization" in (prompt or ""):
            org_selector_calls["n"] += 1
        return 0   # consent → Yes; org (never reached) → repo-only
    status = wizard._set_claude_secret(
        "alice/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "tok", pal=pal, stream=buf, single_select=ss,
        validate_fn=lambda *a, **k: ("valid", ""))   # F10: skip the real isolated ping
    assert status == "set"
    assert org_selector_calls["n"] == 0


# ── _offer_install_claude_workflow (server-side vs local) ───────────────────────────

def test_offer_install_feature_branch_opens_server_side_pr(monkeypatch, tmp_path):
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "single_select", _yn_bridge)

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
    monkeypatch.setattr(wizard, "single_select", _yn_bridge)

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


# ── F10 Part A — _validate_claude_token (verify a candidate BEFORE storing) ──────────

def _validating_run(returncode=0, stdout="", stderr=""):
    """A fake `run` for the token validator that CAPTURES the env + cwd it was
    invoked with (the load-bearing isolation surface), then returns a canned result.
    Accepts the exact seam call the validator makes: ``run(argv, timeout=…, env=…)``."""
    cap = {}

    def run(argv, *, timeout=None, env=None, input=None):
        cap["argv"] = list(argv)
        cap["env"] = dict(env or {})
        cap["cwd"] = os.getcwd()
        cfg = (env or {}).get("CLAUDE_CONFIG_DIR")
        cap["cwd_is_config_dir"] = bool(cfg) and cfg == os.getcwd()
        cap["cwd_existed"] = os.path.isdir(os.getcwd())
        return _R(returncode=returncode, stdout=stdout, stderr=stderr)

    return run, cap


def test_validate_claude_token_valid_on_clean_ping():
    run, _ = _validating_run(returncode=0)
    state, _detail = wizard._validate_claude_token(
        "oat-CANDIDATE", run=run, which=lambda _b: "/usr/bin/claude")
    assert state == "valid"


@pytest.mark.parametrize("stderr", [
    "401 Invalid bearer token",
    "authentication_failed: 401",
    "authentication_error: token rejected",
    "Error: 401 Unauthorized",
    "Your token has expired",
])
def test_validate_claude_token_invalid_on_auth_signature(stderr):
    run, _ = _validating_run(returncode=1, stderr=stderr)
    state, _detail = wizard._validate_claude_token(
        "oat-bad", run=run, which=lambda _b: "/usr/bin/claude")
    assert state == "invalid"


def test_validate_claude_token_unknown_without_binary():
    # No claude on PATH → cannot test → "unknown" (never blocks setup); run not called.
    called = {"n": 0}

    def run(*a, **k):
        called["n"] += 1
        return _R()
    state, _detail = wizard._validate_claude_token(
        "oat", run=run, which=lambda _b: None)
    assert state == "unknown"
    assert called["n"] == 0


def test_validate_claude_token_unknown_on_timeout():
    def run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=25)
    state, _detail = wizard._validate_claude_token(
        "oat", run=run, which=lambda _b: "/usr/bin/claude")
    assert state == "unknown"


def test_validate_claude_token_strips_wrapped_whitespace():
    """A token copied from a wrapped / small terminal window carries an internal
    newline (+ indent space); a real sk-ant-oat token has none. The validator strips
    ALL whitespace before the ping, so the intended token is what gets tested — the
    env carries the reconstructed token, no whitespace."""
    run, cap = _validating_run(returncode=0)
    wrapped = "sk-ant-oat01-Q1bZ...c0\n SddG...wAA"  # newline + leading space from the wrap
    state, _detail = wizard._validate_claude_token(
        wrapped, run=run, which=lambda _b: "/usr/bin/claude")
    assert state == "valid"
    assert cap["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-Q1bZ...c0SddG...wAA"
    assert not any(ch.isspace() for ch in cap["env"]["CLAUDE_CODE_OAUTH_TOKEN"])


def test_validate_claude_token_unknown_on_ambiguous_failure():
    # Non-zero exit but NO auth signature → "unknown", never "invalid".
    run, _ = _validating_run(returncode=1, stderr="network unreachable")
    state, _detail = wizard._validate_claude_token(
        "oat", run=run, which=lambda _b: "/usr/bin/claude")
    assert state == "unknown"


def test_validate_claude_token_full_isolation(monkeypatch):
    # ADVERSARIAL CLAIM #1 guard: the candidate must be the ONLY credential the ping
    # can use — every higher-precedence ENV cred popped AND the apiKeyHelper (#4)
    # neutralised via an empty CLAUDE_CONFIG_DIR + cwd, and NEVER `--bare` (which
    # ignores CLAUDE_CODE_OAUTH_TOKEN → would test the local login).
    candidate = "oat-CANDIDATE-VALUE-do-not-leak"
    creds = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
             "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
    for c in creds:
        monkeypatch.setenv(c, "SHOULD-BE-POPPED")
    orig_cwd = os.getcwd()
    run, cap = _validating_run(returncode=0)
    state, _detail = wizard._validate_claude_token(
        candidate, run=run, which=lambda _b: "/usr/bin/claude")
    assert state == "valid"
    # the candidate IS set, and is the ONLY claude credential in the child env
    assert cap["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == candidate
    for c in creds:
        assert c not in cap["env"], f"{c} was not popped from the validation env"
    # settings-FILE isolation: CLAUDE_CONFIG_DIR is an empty dir AND cwd is that dir
    assert "CLAUDE_CONFIG_DIR" in cap["env"]
    assert cap["cwd_is_config_dir"] is True
    assert cap["cwd_existed"] is True
    # the cheap real round-trip — `-p ping --model haiku`, NEVER `--bare`
    assert "-p" in cap["argv"] and "ping" in cap["argv"]
    assert "--model" in cap["argv"] and "haiku" in cap["argv"]
    assert "--bare" not in cap["argv"]
    # ADVERSARIAL CLAIM #3 guard: the token NEVER appears on argv (only in env)
    assert all(candidate not in tok for tok in cap["argv"])
    # the process cwd is restored after the probe, and the tempdir is cleaned up
    assert os.getcwd() == orig_cwd
    assert not os.path.isdir(cap["env"]["CLAUDE_CONFIG_DIR"])


# ── F10 Part A wired into _set_claude_secret (validate BEFORE the store) ──────────────

def test_set_claude_secret_stores_only_after_valid(monkeypatch):
    # A "valid" verdict → the token is piped to `gh secret set` on STDIN, never argv.
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, calls = _recorder(_full_secret_router(owner_type="User"))
    pal, buf = wizard._Palette(False), io.StringIO()
    status = wizard._set_claude_secret(
        "alice/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "oat-GOOD", pal=pal, stream=buf,
        single_select=_consenting_select(),
        validate_fn=lambda token, **k: ("valid", ""))
    assert status == "set"
    sets = [c for c in calls if c["argv"][:3] == ["gh", "secret", "set"]]
    assert sets and sets[0]["input"] == "oat-GOOD"
    assert all("oat-GOOD" not in tok for c in calls for tok in c["argv"])


def test_set_claude_secret_consent_declined_defers_without_spawning(monkeypatch):
    # The consent gate (new): declining "Set it now via `claude setup-token`?" must
    # NOT spawn the token window nor prompt for a paste — it routes to the by-hand
    # instructions ("skipped").
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, calls = _recorder(_full_secret_router(owner_type="User"))
    pal, buf = wizard._Palette(False), io.StringIO()
    spawned = {"n": 0}
    getpass_calls = {"n": 0}

    def spawn(*a, **k):
        spawned["n"] += 1
        return None

    def gp(*a):
        getpass_calls["n"] += 1
        return "oat"
    status = wizard._set_claude_secret(
        "alice/widgets", run=run, spawn_command=spawn,
        getpass_fn=gp, pal=pal, stream=buf,
        single_select=lambda *a, **k: 1,   # consent → No (index 1)
        validate_fn=lambda token, **k: ("valid", ""))
    assert status == "skipped"
    assert spawned["n"] == 0 and getpass_calls["n"] == 0
    assert not any(c["argv"][:3] == ["gh", "secret", "set"] for c in calls)
    # the by-hand header + copy-paste commands are shown
    out = buf.getvalue()
    assert "by hand" in out and "claude setup-token" in out


def test_set_claude_secret_invalid_shows_validator_detail(monkeypatch):
    # The grown validator returns (state, detail); on "invalid" the one-line
    # diagnostic is surfaced as a dim note beneath the warn.
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, _ = _recorder(_full_secret_router(owner_type="User"))
    pal, buf = wizard._Palette(False), io.StringIO()
    status = wizard._set_claude_secret(
        "alice/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "oat-BAD", pal=pal, stream=buf,
        single_select=_consenting_select(),
        validate_fn=lambda token, **k: ("invalid", "The token was rejected."))
    assert status == "failed"
    assert "The token was rejected." in buf.getvalue()


def test_set_claude_secret_invalid_token_never_stored(monkeypatch):
    # ADVERSARIAL CLAIM #1/#3 guard: an "invalid" verdict re-prompts (bounded ~3) and
    # the invalid token NEVER reaches `gh secret set`.
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, calls = _recorder(_full_secret_router(owner_type="User"))
    pal, buf = wizard._Palette(False), io.StringIO()
    attempts = {"n": 0}

    def vf(token, **k):
        attempts["n"] += 1
        return ("invalid", "")
    status = wizard._set_claude_secret(
        "alice/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "oat-BAD", pal=pal, stream=buf,
        single_select=_consenting_select(), validate_fn=vf)
    assert status == "failed"   # 3× invalid exhausts the paste; routes to by-hand
    assert attempts["n"] == 3
    assert not any(c["argv"][:3] == ["gh", "secret", "set"] for c in calls)


def test_set_claude_secret_invalid_then_valid_stores(monkeypatch):
    # Re-prompt recovery: a rejected paste, then an accepted one → stored.
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, calls = _recorder(_full_secret_router(owner_type="User"))
    pal, buf = wizard._Palette(False), io.StringIO()
    verdicts = iter([("invalid", ""), ("valid", "")])
    status = wizard._set_claude_secret(
        "alice/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "oat", pal=pal, stream=buf,
        single_select=_consenting_select(),
        validate_fn=lambda token, **k: next(verdicts))
    assert status == "set"
    assert any(c["argv"][:3] == ["gh", "secret", "set"] for c in calls)


def test_set_claude_secret_unknown_token_stored_with_warning(monkeypatch):
    # "unknown" (no binary / inconclusive) → store anyway so a transient check never
    # blocks setup, but warn that it is unverified.
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, calls = _recorder(_full_secret_router(owner_type="User"))
    pal, buf = wizard._Palette(False), io.StringIO()
    status = wizard._set_claude_secret(
        "alice/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "oat", pal=pal, stream=buf,
        single_select=_consenting_select(),
        validate_fn=lambda token, **k: ("unknown", ""))
    assert status == "set"
    assert "unverified" in buf.getvalue()
    assert any(c["argv"][:3] == ["gh", "secret", "set"] for c in calls)


def test_set_claude_secret_validator_crash_fails_safe(monkeypatch):
    # ADVERSARIAL CLAIM #1 guard: a validator that RAISES is a bug, not transient →
    # fail SAFE (never store on it).
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, calls = _recorder(_full_secret_router(owner_type="User"))
    pal, buf = wizard._Palette(False), io.StringIO()

    def boom(token, **k):
        raise RuntimeError("validator bug")
    status = wizard._set_claude_secret(
        "alice/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "oat", pal=pal, stream=buf,
        single_select=_consenting_select(), validate_fn=boom)
    assert status == "failed"   # a crashing validator fails safe → by-hand
    assert not any(c["argv"][:3] == ["gh", "secret", "set"] for c in calls)


# ── F10 Part B — re-mint a stored-but-broken token (the live buddhi-review fix) ───────

def test_set_claude_secret_present_and_401_enters_remint(monkeypatch):
    # The stored OAuth token is failing live (probe → True) → DON'T skip; enter the
    # re-mint flow. Consent Yes reaches the paste; a blank paste there routes to the
    # by-hand instructions ("deferred", NOT "present"), and the operator is told the
    # reviews are failing.
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, _ = _recorder(_exists_router(owner_type="User",
                                      repo_secrets=["CLAUDE_CODE_OAUTH_TOKEN"]))
    pal, buf = wizard._Palette(False), io.StringIO()
    probe_calls = {"n": 0}

    def probe(repo, **k):
        probe_calls["n"] += 1
        assert repo == "alice/widgets"
        return True
    status = wizard._set_claude_secret(
        "alice/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "", pal=pal, stream=buf, auth_probe=probe,
        single_select=_consenting_select())
    assert status == "skipped"         # entered the mint flow; a blank paste → by-hand
    assert probe_calls["n"] == 1
    out = buf.getvalue()
    assert "failing" in out and "re-mint" in out


def test_set_claude_secret_present_and_clean_stays_present(monkeypatch):
    # ADVERSARIAL CLAIM #2 guard: a stored token that works (probe → False) keeps the
    # skip — NO mint, NO paste, NO re-mint of a working token.
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, calls = _recorder(_exists_router(owner_type="User",
                                          repo_secrets=["CLAUDE_CODE_OAUTH_TOKEN"]))
    pal, buf = wizard._Palette(False), io.StringIO()
    getpass_calls = {"n": 0}

    def gp(*a):
        getpass_calls["n"] += 1
        return "tok"
    status = wizard._set_claude_secret(
        "alice/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=gp, pal=pal, stream=buf, auth_probe=lambda repo, **k: False)
    assert status == "present"
    assert getpass_calls["n"] == 0
    assert not any(c["argv"][:3] == ["gh", "secret", "set"] for c in calls)


def test_set_claude_secret_present_and_probe_cant_tell_stays_present(monkeypatch):
    # "couldn't tell" maps to False → keep the skip (never blind-re-mint on uncertainty).
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, _ = _recorder(_exists_router(owner_type="User",
                                      repo_secrets=["CLAUDE_CODE_OAUTH_TOKEN"]))
    pal, buf = wizard._Palette(False), io.StringIO()
    status = wizard._set_claude_secret(
        "alice/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "tok", pal=pal, stream=buf,
        auth_probe=lambda repo, **k: False)
    assert status == "present"


def test_set_claude_secret_anthropic_present_never_probes(monkeypatch):
    # ADVERSARIAL CLAIM #2 guard: a working ANTHROPIC_API_KEY backs the repo → keep
    # the skip WITHOUT probing (the probe only knows the OAuth token's 401).
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    run, _ = _recorder(_exists_router(owner_type="User",
                                      repo_secrets=["ANTHROPIC_API_KEY"]))
    pal, buf = wizard._Palette(False), io.StringIO()
    probe_calls = {"n": 0}

    def probe(repo, **k):
        probe_calls["n"] += 1
        return True
    status = wizard._set_claude_secret(
        "alice/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "tok", pal=pal, stream=buf, auth_probe=probe)
    assert status == "present"
    assert probe_calls["n"] == 0


def test_set_claude_secret_probe_raising_seam_doesnt_crash(monkeypatch):
    # ADVERSARIAL CLAIM #2 guard: the REAL probe (auth_probe=None) over a gh seam that
    # RAISES is best-effort — returns False ("couldn't tell"), the skip is kept, no crash.
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)

    def router(argv, _inp):
        if argv[:2] == ["gh", "api"] and _startswith(argv, "users/"):
            return _R(returncode=0, stdout="User\n")
        if argv[:3] == ["gh", "secret", "list"]:
            return _R(returncode=0, stdout="CLAUDE_CODE_OAUTH_TOKEN\t2026-01-01")
        if argv[:3] in (["gh", "run", "list"], ["gh", "run", "view"]):
            raise OSError("gh missing")
        return _R()
    run, _ = _recorder(router)
    pal, buf = wizard._Palette(False), io.StringIO()
    status = wizard._set_claude_secret(
        "alice/widgets", run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "tok", pal=pal, stream=buf)   # auth_probe=None → real probe
    assert status == "present"


# ── F11 — _offer_gh_token: verify-before-store the GH_TOKEN escape hatch ──────────────
# A pasted GH_TOKEN goes to the SHELL RC (not a re-mintable GitHub secret), so a wrong
# value silently shadows a later `gh auth login` and breaks every gh call. This path
# probes the token with a real `gh api user` (the pasted token FORCED into the child
# env) BEFORE storing, fails CLOSED on any failure, and re-prompts once then skips.

def _gh_probe_run(*results):
    """Fake `run` for the GH_TOKEN probe. Returns each queued result in order (a _R,
    or a BaseException to raise), recording the argv + env of every call so a test can
    assert the pasted token was threaded via env and never on argv. A depleted queue
    defaults to a clean success."""
    queue = list(results)
    calls = []

    def run(argv, cwd=None, timeout=30, input=None, env=None):
        calls.append({"argv": list(argv), "env": dict(env or {})})
        outcome = queue.pop(0) if queue else _R(returncode=0, stdout="octocat\n")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return run, calls


def _capture_upsert(monkeypatch, ok=True):
    """Capture `shell_env.upsert` so a test can assert WHETHER and WITH WHAT a token
    was persisted, without touching a real rc file."""
    seen = {"called": False, "mapping": None}

    def fake_upsert(mapping, **kwargs):
        seen["called"] = True
        seen["mapping"] = dict(mapping)
        seen["kwargs"] = kwargs
        return (ok, "/home/u/.zshrc")

    monkeypatch.setattr(wizard.shell_env, "upsert", fake_upsert)
    return seen


def _getpass_seq(*values):
    it = iter(values)
    return lambda *a, **k: next(it)


def test_offer_gh_token_stores_after_valid_probe(monkeypatch):
    # A valid probe (gh api user → login, exit 0) → the token is persisted via upsert,
    # the ✓ row names the authenticated login, and the probe authenticated as the
    # PASTED token in env (never on argv).
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "single_select", _yn_bridge)
    run, calls = _gh_probe_run(_R(returncode=0, stdout="octocat\n"))
    seen = _capture_upsert(monkeypatch)
    pal, buf = wizard._Palette(False), io.StringIO()
    wizard._offer_gh_token(run=run, getpass_fn=lambda *a: "ghp_PASTED", pal=pal,
                           stream=buf, input_fn=lambda *a: "y")
    assert seen["called"] and seen["mapping"] == {"GH_TOKEN": "ghp_PASTED"}
    assert len(calls) == 1
    assert calls[0]["argv"] == ["gh", "api", "user", "--jq", ".login"]
    assert calls[0]["env"]["GH_TOKEN"] == "ghp_PASTED"            # pasted token in env
    assert all("ghp_PASTED" not in tok for tok in calls[0]["argv"])  # never on argv
    out = buf.getvalue()
    assert "octocat" in out and "GH_TOKEN written" in out


def test_offer_gh_token_invalid_never_stored_bounded_reprompt(monkeypatch):
    # ADVERSARIAL guard: a 401 on BOTH the paste and the single re-prompt → the token
    # is NEVER stored, guidance is shown, and getpass is called exactly twice (bounded
    # re-prompt, then skip — never an unbounded loop).
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "single_select", _yn_bridge)
    run, calls = _gh_probe_run(_R(returncode=1, stderr="gh: Bad credentials (HTTP 401)"),
                               _R(returncode=1, stderr="gh: Bad credentials (HTTP 401)"))
    seen = _capture_upsert(monkeypatch)
    pal, buf = wizard._Palette(False), io.StringIO()
    getpass_calls = {"n": 0}

    def gp(*a):
        getpass_calls["n"] += 1
        return "ghp_BAD"
    wizard._offer_gh_token(run=run, getpass_fn=gp, pal=pal, stream=buf,
                           input_fn=lambda *a: "y")
    assert seen["called"] is False                 # nothing persisted on any path
    assert getpass_calls["n"] == 2                 # one paste + one bounded re-prompt
    assert len(calls) == 2                         # probed twice, no third attempt
    out = buf.getvalue()
    assert "didn't authenticate" in out and "HTTP 401" in out
    assert "gh auth login" in out                  # guidance to the alternative


def test_offer_gh_token_invalid_then_valid_stores(monkeypatch):
    # Re-prompt recovery: a rejected paste, then an accepted one → stored on the second.
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "single_select", _yn_bridge)
    run, calls = _gh_probe_run(_R(returncode=1, stderr="HTTP 401"),
                               _R(returncode=0, stdout="octocat\n"))
    seen = _capture_upsert(monkeypatch)
    pal, buf = wizard._Palette(False), io.StringIO()
    wizard._offer_gh_token(run=run, getpass_fn=_getpass_seq("ghp_BAD", "ghp_GOOD"),
                           pal=pal, stream=buf, input_fn=lambda *a: "y")
    assert seen["called"] and seen["mapping"] == {"GH_TOKEN": "ghp_GOOD"}
    assert len(calls) == 2 and calls[1]["env"]["GH_TOKEN"] == "ghp_GOOD"


def test_offer_gh_token_network_failure_not_stored(monkeypatch):
    # A spawn / network failure (run raises) → not verifiable → fail CLOSED: not stored,
    # guidance shown. The rc-written token must never be persisted unverified.
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "single_select", _yn_bridge)
    run, calls = _gh_probe_run(OSError("gh: network unreachable"),
                               OSError("gh: network unreachable"))
    seen = _capture_upsert(monkeypatch)
    pal, buf = wizard._Palette(False), io.StringIO()
    wizard._offer_gh_token(run=run, getpass_fn=lambda *a: "ghp_X", pal=pal, stream=buf,
                           input_fn=lambda *a: "y")
    assert seen["called"] is False
    assert len(calls) == 2                       # re-prompted once, then skipped
    assert "didn't authenticate" in buf.getvalue()


def test_offer_gh_token_blank_paste_skips(monkeypatch):
    # A blank paste → skip immediately: NO probe, NO store (the existing early-return).
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "single_select", _yn_bridge)
    run, calls = _gh_probe_run()
    seen = _capture_upsert(monkeypatch)
    pal, buf = wizard._Palette(False), io.StringIO()
    wizard._offer_gh_token(run=run, getpass_fn=lambda *a: "", pal=pal, stream=buf,
                           input_fn=lambda *a: "y")
    assert calls == [] and seen["called"] is False


def test_offer_gh_token_non_tty_noop(monkeypatch):
    # Non-TTY → early no-op (unchanged): no prompt, no probe, no store.
    monkeypatch.setattr(wizard, "_is_tty", lambda: False)
    run, calls = _gh_probe_run()
    seen = _capture_upsert(monkeypatch)
    pal, buf = wizard._Palette(False), io.StringIO()
    getpass_calls = {"n": 0}
    wizard._offer_gh_token(run=run,
                           getpass_fn=lambda *a: getpass_calls.__setitem__("n", 1) or "x",
                           pal=pal, stream=buf, input_fn=lambda *a: "y")
    assert calls == [] and seen["called"] is False and getpass_calls["n"] == 0


def test_offer_gh_token_declined_noop(monkeypatch):
    # The user declines the GH_TOKEN prompt → no probe, no store.
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "single_select", _yn_bridge)
    run, calls = _gh_probe_run()
    seen = _capture_upsert(monkeypatch)
    pal, buf = wizard._Palette(False), io.StringIO()
    wizard._offer_gh_token(run=run, getpass_fn=lambda *a: "ghp_X", pal=pal, stream=buf,
                           input_fn=lambda *a: "n")
    assert calls == [] and seen["called"] is False


def test_offer_gh_token_probe_forces_pasted_token_over_ambient(monkeypatch):
    # ADVERSARIAL guard for the false-positive concern. A fake `run` cannot exercise
    # gh's real GH_TOKEN-over-GITHUB_TOKEN precedence, so this asserts the CODE's half
    # of that contract: even with a different ambient GITHUB_TOKEN present, the child
    # env carries GH_TOKEN == the PASTED value (which gh resolves ahead of the ambient
    # GITHUB_TOKEN, per its documented precedence — that resolution lives in gh, not
    # here). If the code stopped forcing GH_TOKEN, this fails.
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "single_select", _yn_bridge)
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-WRONG")
    run, calls = _gh_probe_run(_R(returncode=0, stdout="octocat\n"))
    _capture_upsert(monkeypatch)
    pal, buf = wizard._Palette(False), io.StringIO()
    wizard._offer_gh_token(run=run, getpass_fn=lambda *a: "ghp_PASTED", pal=pal,
                           stream=buf, input_fn=lambda *a: "y")
    assert calls[0]["env"]["GH_TOKEN"] == "ghp_PASTED"       # pasted forced as GH_TOKEN
    assert calls[0]["env"]["GITHUB_TOKEN"] == "ambient-WRONG"  # ambient left intact; gh ranks GH_TOKEN first
    assert all("ghp_PASTED" not in tok for tok in calls[0]["argv"])


def test_offer_gh_token_value_never_logged(monkeypatch):
    # The token value must never be echoed into the console (getpass hides input; the
    # code prints the login + rc path, never the token) — on BOTH success and failure.
    secret = "ghp_SUPER_SECRET_VALUE"
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "single_select", _yn_bridge)
    run_ok, _ = _gh_probe_run(_R(returncode=0, stdout="octocat\n"))
    _capture_upsert(monkeypatch)
    pal, buf = wizard._Palette(False), io.StringIO()
    wizard._offer_gh_token(run=run_ok, getpass_fn=lambda *a: secret, pal=pal,
                           stream=buf, input_fn=lambda *a: "y")
    run_bad, _ = _gh_probe_run(_R(returncode=1, stderr="HTTP 401"),
                               _R(returncode=1, stderr="HTTP 401"))
    wizard._offer_gh_token(run=run_bad, getpass_fn=lambda *a: secret, pal=pal,
                           stream=buf, input_fn=lambda *a: "y")
    assert secret not in buf.getvalue()


def test_probe_gh_token_returns_login_and_errors():
    # The probe contract directly: (ok, login, error). Success → (True, login, ""); a
    # failure surfaces the first error line; an exit-0-but-empty-login is NOT ok.
    run_ok, calls = _gh_probe_run(_R(returncode=0, stdout="octocat\n"))
    assert wizard._probe_gh_token("ghp_X", run=run_ok) == (True, "octocat", "")
    assert calls[0]["env"]["GH_TOKEN"] == "ghp_X"
    run_bad, _ = _gh_probe_run(_R(returncode=1, stderr="line1\nline2"))
    assert wizard._probe_gh_token("ghp_X", run=run_bad) == (False, "", "line1")
    run_empty, _ = _gh_probe_run(_R(returncode=0, stdout="   \n"))
    assert wizard._probe_gh_token("ghp_X", run=run_empty)[0] is False
