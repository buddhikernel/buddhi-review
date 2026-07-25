"""Versioned managed-file sync — the ``buddhi-managed-version`` marker parsing, the
``needs_update`` policy, the shipped-template registry, and the wizard's
``_offer_update_managed_file`` helper (the in-place update PR for an OUTDATED file).

This is the mechanism that delivers a newer bundled workflow — e.g. the auth-failure
guard — to a repo whose installed copy predates it, instead of the old
"present by name = done" check that silently skipped a stale file.
"""
from __future__ import annotations

import base64
import io
import types

import pytest

from buddhi_review import managed_files, wizard
from conftest import _yn_bridge


def _R(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ── marker parsing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("# buddhi-managed-version: 1\nname: x\n", 1),
    ("name: x\n#buddhi-managed-version:42\n", 42),
    ("   #   buddhi-managed-version:   7   \n", 7),          # tolerant whitespace
    ("# BUDDHI-MANAGED-VERSION: 3\n", 3),                    # case-insensitive
    ("no marker here\n", None),
    ("# buddhi-managed-version: notanint\n", None),
    ("# buddhi-managed-version:\n", None),                   # missing number
    ("", None),
    (None, None),
])
def test_file_version_parsing(text, expected):
    assert managed_files.file_version(text) == expected


def test_marker_must_be_its_own_line_not_inline_after_code():
    # A marker buried mid-line (not a standalone comment) is NOT a managed-version line.
    assert managed_files.file_version("name: x  # buddhi-managed-version: 9\n") is None


# ── needs_update policy ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("installed,shipped,expected", [
    (None, 1, True),     # legacy/unversioned installed → outdated
    (0, 1, True),
    (1, 2, True),
    (1, 1, False),       # current → no offer
    (2, 1, False),       # installed NEWER than shipped → no offer
    (1, None, False),    # unknown shipped → never claim 'newer'
    (None, None, False),
])
def test_needs_update(installed, shipped, expected):
    assert managed_files.needs_update(installed, shipped) is expected


# ── the shipped registry: every managed file carries a marker ───────────────────────

def test_every_managed_file_is_bundled_and_versioned():
    """Each registered file ships in the package AND carries a parseable marker — so a
    careless edit that drops the marker (which would silently disable the update
    offer) fails here instead of in a user's repo."""
    assert managed_files.MANAGED_FILES, "registry must not be empty"
    for spec in managed_files.MANAGED_FILES:
        template = spec["template"]
        assert template.is_file(), f"{spec['name']} template missing: {template}"
        v = managed_files.shipped_version(template)
        assert isinstance(v, int) and v >= 1, (
            f"{spec['name']} must carry a buddhi-managed-version >= 1 (got {v!r})"
        )
        assert spec["dest"].endswith(spec["name"]), spec


def test_claude_workflow_is_registered():
    names = {s["name"] for s in managed_files.MANAGED_FILES}
    assert "claude-code-review.yml" in names
    assert "tests-ready-for-ci.yml" in names


# ── _offer_update_managed_file: the in-place update PR ──────────────────────────────

def _claude_spec():
    return next(s for s in managed_files.MANAGED_FILES
                if s["name"] == "claude-code-review.yml")


def _update_router(*, head_sha="cafe", pr_url="https://github.com/o/r/pull/5",
                   put_rc=0, pr_rc=0, blob_sha="blob123"):
    """A run() covering the server-side update-PR calls: head SHA, branch create, the
    existing-blob SHA probe (so the PUT is an UPDATE), the PUT, and the PR create."""
    def run(argv, **kw):
        joined = " ".join(argv)
        if argv[:2] == ["gh", "pr"]:
            return _R(returncode=pr_rc, stdout=(pr_url + "\n") if pr_rc == 0 else "")
        if argv[:2] == ["gh", "api"]:
            if "-X" in argv and "PUT" in argv:
                return _R(returncode=put_rc)
            if "/git/ref/heads/" in joined and "--jq" in argv:
                return _R(returncode=0, stdout=head_sha + "\n")
            if argv[2].endswith("/git/refs"):
                return _R(returncode=0)
            if "contents/" in joined and "--jq" in argv and ".sha" in argv:
                return _R(returncode=0, stdout=blob_sha + "\n")  # file exists → update
        return _R()
    return run


def _offer_update(installed_text, *, is_tty, monkeypatch, run=None, accept=True,
                  cfg_path=None, pending_ci_prs=None):
    monkeypatch.setattr(wizard, "_is_tty", lambda: is_tty)
    if is_tty:
        monkeypatch.setattr(wizard, "single_select", _yn_bridge)
    buf = io.StringIO()
    calls = []

    def rec(argv, **kw):
        calls.append(list(argv))
        return (run or _update_router())(argv, **kw)

    result = wizard._offer_update_managed_file(
        "o/r", "main", _claude_spec(), installed_text,
        run=rec, pal=wizard._Palette(False), stream=buf,
        input_fn=lambda prompt="": "y" if accept else "n",
        sleep=lambda s: None,  # never a real sleep — the retry backoff seam
        cfg_path=cfg_path, pending_ci_prs=pending_ci_prs)
    return result, buf.getvalue(), calls


def test_update_offered_when_installed_is_legacy_unversioned(monkeypatch):
    """The buddhi-review case: an installed workflow with NO marker is older than the
    bundled (versioned) template → an update PR is opened on the dedicated update
    branch, and the muted git-revert reassurance is shown."""
    result, out, calls = _offer_update("name: stale workflow\n", is_tty=True,
                                       monkeypatch=monkeypatch)
    assert result == "pr"
    put = next(c for c in calls if "-X" in c and "PUT" in c)
    assert any(a.startswith("branch=buddhi/update-claude-code-review-v") for a in put)
    # An UPDATE supplies the existing blob SHA (a PUT over an existing file 422s without it).
    assert any(a.startswith("sha=") for a in put), "update PUT must carry the blob sha"
    pr = next(c for c in calls if c[:2] == ["gh", "pr"])
    assert pr[pr.index("--head") + 1].startswith("buddhi/update-claude-code-review-v")
    assert "revert the PR" in out


def test_no_update_when_installed_is_current(monkeypatch):
    """An installed copy already at the shipped version is left alone — no PR."""
    shipped = managed_files.shipped_version(_claude_spec()["template"])
    installed = f"# buddhi-managed-version: {shipped}\nname: x\n"
    result, out, calls = _offer_update(installed, is_tty=True, monkeypatch=monkeypatch)
    assert result is None
    assert not any(c[:2] == ["gh", "pr"] for c in calls)


def test_outdated_but_declined_opens_no_pr(monkeypatch):
    result, out, calls = _offer_update("legacy\n", is_tty=True, monkeypatch=monkeypatch,
                                       accept=False)
    assert result is None
    assert not any(c[:2] == ["gh", "pr"] for c in calls)


def test_non_tty_outdated_defers_with_guidance(monkeypatch):
    result, out, calls = _offer_update("legacy\n", is_tty=False, monkeypatch=monkeypatch)
    assert result is None
    assert not any(c[:2] == ["gh", "pr"] for c in calls)
    assert "Re-run setup in a terminal" in out


# ── the update PR must actually get CI when the repo is label-gated (#94) ────────
# _offer_update_managed_file opens the claude-code-review.yml update PR on a
# `buddhi/update-<slug>-v<n>` branch. On a repo that defers CI to `ready-for-ci`,
# that PR carried no label, so the suite never ran on it — which is how #94 (itself
# a claude-code-review.yml update PR) merged unexercised. The wizard now attaches
# the label, but ONLY when the user has explicitly opted this repo into label-gated
# CI (default OFF), so a normal every-push-CI repo gets no stray label.

def _offer_update_lgc(installed_text, *, monkeypatch, label_gated, run=None):
    monkeypatch.setattr(wizard.config, "label_gated_ci", lambda cfg, repo=None: label_gated)
    return _offer_update(installed_text, is_tty=True, monkeypatch=monkeypatch, run=run)


def test_update_pr_gets_ready_for_ci_when_repo_is_label_gated(monkeypatch):
    result, out, calls = _offer_update_lgc("legacy\n", monkeypatch=monkeypatch,
                                           label_gated=True)
    assert result == "pr"
    edits = [c for c in calls if c[:3] == ["gh", "pr", "edit"]]
    assert len(edits) == 1, calls
    assert "--add-label" in edits[0] and "ready-for-ci" in edits[0]
    assert "-R" in edits[0] and "o/r" in edits[0]
    # self-bootstrapping so --add-label can't 404 on a repo that never had the label
    assert any(c[:3] == ["gh", "label", "create"] and "ready-for-ci" in c
               for c in calls), calls


def test_label_attached_after_create_never_at_create_time(monkeypatch):
    """The label-gated workflow fires on the `labeled` event; a label applied at
    creation does not reliably emit one, so it must be a separate edit AFTER the
    PR exists."""
    result, out, calls = _offer_update_lgc("legacy\n", monkeypatch=monkeypatch,
                                           label_gated=True)
    create_i = next(i for i, c in enumerate(calls) if c[:3] == ["gh", "pr", "create"])
    edit_i = next(i for i, c in enumerate(calls) if c[:3] == ["gh", "pr", "edit"])
    assert create_i < edit_i
    assert "--label" not in calls[create_i]


def test_no_label_when_repo_not_opted_into_label_gated_ci(monkeypatch):
    """Default OFF: a repo whose CI runs on every push must get no stray label."""
    result, out, calls = _offer_update_lgc("legacy\n", monkeypatch=monkeypatch,
                                           label_gated=False)
    assert result == "pr"                                  # the update PR still opens
    assert not [c for c in calls if c[:3] == ["gh", "pr", "edit"]]
    assert not [c for c in calls if c[:3] == ["gh", "label", "create"]]


def test_label_add_failure_never_breaks_the_update(monkeypatch):
    """A raising label add leaves the update PR intact — the PR is open, the wizard
    already reported it; the label is best-effort. The user is TOLD, though: the warn
    row is the only signal that the update may merge without CI having run."""
    def run(argv, **kw):
        if list(argv)[:3] == ["gh", "pr", "edit"]:
            raise OSError("boom")
        return _update_router()(argv, **kw)
    result, out, calls = _offer_update_lgc("legacy\n", monkeypatch=monkeypatch,
                                           label_gated=True, run=run)
    assert result == "pr"
    assert "ready-for-ci" in out and "by hand" in out


def test_label_add_nonzero_exit_warns_and_keeps_the_update_pr(monkeypatch):
    """The likelier real-world failure is not an exception but ``gh pr edit`` exiting
    NON-ZERO (no auth, no `pull_requests: write`) for a REAL, resolvable PR ref — a
    different branch of the helper than the raising one. Same degradation: the
    update PR stands, the user is warned, and — because the ref IS resolvable — the
    hand-run hint is a command that actually runs: ``gh pr edit <ref> --add-label
    ready-for-ci -R <repo>``."""
    base = _update_router()  # default pr_url → a real, resolvable ref

    def run(argv, **kw):
        if list(argv)[:3] == ["gh", "pr", "edit"]:
            return _R(returncode=1)
        return base(argv, **kw)

    result, out, calls = _offer_update_lgc("legacy\n", monkeypatch=monkeypatch,
                                           label_gated=True, run=run)
    assert result == "pr"
    assert "ready-for-ci" in out and "by hand" in out
    edits = [c for c in calls if c[:3] == ["gh", "pr", "edit"]]
    assert len(edits) == wizard._LABEL_ADD_ATTEMPTS, edits   # retried, then gave up
    assert "https://github.com/o/r/pull/5" in edits[0]
    assert ("gh pr edit https://github.com/o/r/pull/5 --add-label ready-for-ci "
            "-R o/r") in out


def test_label_add_skips_retries_when_pr_ref_is_unresolvable_sentinel(monkeypatch):
    """``_create_file_pr`` falls back to the ``(PR opened)`` sentinel when `gh pr
    create` succeeds but reports no PR number/URL — not a ref ``gh pr edit`` could
    ever resolve. The attach must recognize this and skip the doomed retries/backoff
    entirely (no attempts, no real ``time.sleep`` burned in front of a waiting user),
    and the warning must name the repo instead of printing an unrunnable
    ``gh pr edit (PR opened) ...`` hint."""
    base = _update_router(pr_url="")  # empty create stdout → ref is the sentinel

    result, out, calls = _offer_update_lgc("legacy\n", monkeypatch=monkeypatch,
                                           label_gated=True, run=base)
    assert result == "pr"
    edits = [c for c in calls if c[:3] == ["gh", "pr", "edit"]]
    assert not edits, edits   # no doomed gh pr edit attempts against an unusable ref
    assert "ready-for-ci" in out and "by hand" in out
    assert "gh pr edit (PR opened)" not in out   # never an unrunnable hand-run hint
    assert "gh pr edit <PR#> --add-label ready-for-ci -R o/r" in out
    # The best-effort `gh label create` still ran, even though the ref was
    # unusable — otherwise the hand-run hint above names a label that doesn't
    # exist yet on a repo that never carried it before.
    creates = [c for c in calls if c[:3] == ["gh", "label", "create"]]
    assert creates, calls


# ── _attach_ready_for_ci: direct unit coverage of the helper itself ─────────────────

def test_attach_ready_for_ci_backoff_uses_injected_sleep(monkeypatch):
    """Mirrors buddhi_review.merge._attach_ready_for_ci: the label ADD is retried
    with linear backoff through the injected sleep seam (never a real time.sleep)
    so a transient gh/GitHub blip doesn't leave the PR unlabeled."""
    monkeypatch.setattr(wizard.config, "label_gated_ci", lambda cfg, repo=None: True)
    slept = []
    calls = {"n": 0}

    def run(argv, **kw):
        if argv[:3] == ["gh", "pr", "edit"]:
            calls["n"] += 1
            return _R(returncode=0 if calls["n"] >= 3 else 1)
        return _R()

    ok = wizard._attach_ready_for_ci("o/r", "5", run=run, sleep=slept.append)
    assert ok is True
    assert slept == [2.0, 4.0]  # backoff_s * attempt for the two failed tries


def test_attach_ready_for_ci_bootstraps_label_even_for_unresolvable_sentinel_ref(monkeypatch):
    """The `_NO_PR_REF` sentinel skips the doomed `gh pr edit` retries (there is no
    ref to resolve), but the best-effort `gh label create` must still run before
    that early return — it's the only thing that guarantees `ready-for-ci` exists
    on a repo that never carried it, which is what the caller's hand-run hint
    (`gh pr edit <PR#> --add-label ready-for-ci -R {repo}`) assumes."""
    monkeypatch.setattr(wizard.config, "label_gated_ci", lambda cfg, repo=None: True)
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        return _R()

    ok = wizard._attach_ready_for_ci("o/r", wizard._NO_PR_REF, run=run,
                                     sleep=lambda s: None)
    assert ok is False
    assert calls == [["gh", "label", "create", "ready-for-ci", "--color",
                      "cccccc", "-R", "o/r"]]   # created, no gh pr edit attempted


def test_attach_ready_for_ci_gives_up_after_all_attempts_fail(monkeypatch):
    monkeypatch.setattr(wizard.config, "label_gated_ci", lambda cfg, repo=None: True)

    def run(argv, **kw):
        if argv[:3] == ["gh", "pr", "edit"]:
            return _R(returncode=1)
        return _R()

    ok = wizard._attach_ready_for_ci("o/r", "5", run=run, sleep=lambda s: None)
    assert ok is False


def test_attach_ready_for_ci_config_read_failure_falls_through_to_attach(monkeypatch):
    """A broken config read must NOT default to "no label needed" (which would
    silently suppress the caller's warning on an actually-label-gated repo) — it
    falls through and attempts the attach, so the return value reflects whether the
    label really landed."""
    monkeypatch.setattr(
        wizard.config, "label_gated_ci",
        lambda cfg, repo=None: (_ for _ in ()).throw(RuntimeError("boom")))
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "edit"]:
            return _R(returncode=0)
        return _R()

    ok = wizard._attach_ready_for_ci("o/r", "5", run=run, sleep=lambda s: None)
    assert ok is True
    assert any(c[:3] == ["gh", "pr", "edit"] for c in calls)


def test_attach_ready_for_ci_config_read_failure_and_attach_failure_returns_false(monkeypatch):
    """Same broken-config path, but the attach itself fails — the caller must see
    False (and warn), not a blanket True that hides the failure."""
    monkeypatch.setattr(
        wizard.config, "label_gated_ci",
        lambda cfg, repo=None: (_ for _ in ()).throw(RuntimeError("boom")))

    def run(argv, **kw):
        if argv[:3] == ["gh", "pr", "edit"]:
            return _R(returncode=1)
        return _R()

    ok = wizard._attach_ready_for_ci("o/r", "5", run=run, sleep=lambda s: None,
                                     attempts=1)
    assert ok is False


# ── the gating decision itself, against a REAL config file (no label_gated_ci stub) ──
# The stubbed tests above pin the wiring; these pin the DECISION. With
# config.label_gated_ci patched out, config_path(), the .exists() guard, load_config
# and the per-repo-vs-global resolution are never exercised — a mutation to
# `label_gated_ci(cfg)` (dropping the repo argument) or to a different config file
# would keep every stubbed test green while silently un-labelling a repo that opted
# in per-repo. These write a real config and leave the resolver alone.

def _real_config(tmp_path, monkeypatch, text, *, name="config.yaml"):
    """Write a real config and point $BUDDHI_CONFIG (what config.config_path() reads)
    at it, overriding the autouse hermetic fixture's absent-file default."""
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    monkeypatch.setenv("BUDDHI_CONFIG", str(p))
    return p


def test_real_config_per_repo_optin_under_global_off_gets_the_label(monkeypatch, tmp_path):
    """The per-repo value WINS over a global false — the resolution the wizard's own
    label-gated-CI step writes. A `label_gated_ci(cfg)` that drops the repo argument
    reads the global false and leaves this PR unlabeled (and so untested)."""
    _real_config(tmp_path, monkeypatch,
                 "label_gated_ci: false\nrepos:\n  o/r:\n    label_gated_ci: true\n")
    result, out, calls = _offer_update("legacy\n", is_tty=True, monkeypatch=monkeypatch)
    assert result == "pr"
    edits = [c for c in calls if c[:3] == ["gh", "pr", "edit"]]
    assert len(edits) == 1, calls
    assert "--add-label" in edits[0] and "ready-for-ci" in edits[0]


def test_real_config_global_optin_with_no_repo_entry_gets_the_label(monkeypatch, tmp_path):
    """The global flag is the fallback when the repo has no entry of its own."""
    _real_config(tmp_path, monkeypatch, "label_gated_ci: true\n")
    result, out, calls = _offer_update("legacy\n", is_tty=True, monkeypatch=monkeypatch)
    assert result == "pr"
    assert [c for c in calls if c[:3] == ["gh", "pr", "edit"]], calls


def test_real_config_per_repo_off_under_global_on_gets_no_label(monkeypatch, tmp_path):
    """The other direction of the same resolution: an explicit per-repo OFF shadows a
    global ON, so this repo keeps its every-push CI and gets no stray label."""
    _real_config(tmp_path, monkeypatch,
                 "label_gated_ci: true\nrepos:\n  o/r:\n    label_gated_ci: false\n")
    result, out, calls = _offer_update("legacy\n", is_tty=True, monkeypatch=monkeypatch)
    assert result == "pr"
    assert not [c for c in calls if c[:3] == ["gh", "pr", "edit"]], calls


@pytest.mark.parametrize("text", ["", "plan: free\n"])
def test_real_config_empty_or_silent_defaults_off(monkeypatch, tmp_path, text):
    """DEFAULT OFF: a config that says nothing about label-gated CI attaches nothing."""
    _real_config(tmp_path, monkeypatch, text)
    result, out, calls = _offer_update("legacy\n", is_tty=True, monkeypatch=monkeypatch)
    assert result == "pr"
    assert not [c for c in calls if c[:3] == ["gh", "pr", "edit"]], calls


def test_real_config_absent_file_defaults_off(monkeypatch, tmp_path):
    """No config on disk at all (a first run) — the .exists() guard short-circuits to
    the empty-config default, and nothing is labeled."""
    monkeypatch.setenv("BUDDHI_CONFIG", str(tmp_path / "never-written.yaml"))
    result, out, calls = _offer_update("legacy\n", is_tty=True, monkeypatch=monkeypatch)
    assert result == "pr"
    assert not [c for c in calls if c[:3] == ["gh", "pr", "edit"]], calls


# ── a config that EXISTS but can't be read must fail closed, not "no label needed" ──
# config.load_config swallows a syntax error / non-mapping document into the SAME `{}`
# an absent or genuinely empty file returns, so `label_gated_ci({}, repo)` reads False
# either way. _attach_ready_for_ci must tell these apart via load_config_checked's
# `readable` flag — an absent file legitimately means "no label needed", but a present,
# unreadable one must never be read as an opt-out.

def test_real_config_corrupt_yaml_fails_closed(monkeypatch, tmp_path):
    """A real YAML syntax error (not a mocked exception) on disk — `load_config` itself
    swallows it into `{}`, so the fix must be in how `_attach_ready_for_ci` reads that
    `{}`, not in `load_config`'s own error handling."""
    p = _real_config(tmp_path, monkeypatch, "label_gated_ci: [true\n")  # unbalanced flow seq
    assert wizard.config.load_config(p) == {}, "sanity: load_config swallows the parse error"
    result, out, calls = _offer_update("legacy\n", is_tty=True, monkeypatch=monkeypatch)
    assert result == "pr"
    assert [c for c in calls if c[:3] == ["gh", "pr", "edit"]], calls


def test_real_config_non_mapping_document_fails_closed(monkeypatch, tmp_path):
    """A config file that parses cleanly but to something other than a mapping (e.g. a
    bare YAML list) can never carry a genuine `label_gated_ci` key either — just as
    unreadable as a syntax error for this decision, and must fail closed the same way."""
    _real_config(tmp_path, monkeypatch, "- just\n- a\n- list\n")
    result, out, calls = _offer_update("legacy\n", is_tty=True, monkeypatch=monkeypatch)
    assert result == "pr"
    assert [c for c in calls if c[:3] == ["gh", "pr", "edit"]], calls


# ── load_config_checked: the helper itself, distinguishing the two `{}`s ────────────

def test_load_config_checked_absent_file_is_readable_empty(tmp_path):
    cfg, readable = wizard.config.load_config_checked(tmp_path / "never-written.yaml")
    assert (cfg, readable) == ({}, True)


def test_load_config_checked_genuinely_empty_file_is_readable_empty(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("", encoding="utf-8")
    assert wizard.config.load_config_checked(p) == ({}, True)


def test_load_config_checked_valid_config_is_readable(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("label_gated_ci: true\n", encoding="utf-8")
    cfg, readable = wizard.config.load_config_checked(p)
    assert readable is True
    assert cfg == {"label_gated_ci": True}


def test_load_config_checked_corrupt_yaml_is_unreadable(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("label_gated_ci: [true\n", encoding="utf-8")
    cfg, readable = wizard.config.load_config_checked(p)
    assert (cfg, readable) == ({}, False)


def test_load_config_checked_non_mapping_document_is_unreadable(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert wizard.config.load_config_checked(p) == ({}, False)


@pytest.mark.parametrize("body", ["[]\n", "{}\n", "false\n", "0\n", "''\n"])
def test_load_config_checked_falsy_non_mapping_document_is_unreadable(tmp_path, body):
    """A FALSY non-mapping document (``[]``, ``false``, ``0``, ``''``) is the same class
    of malformed config as the non-empty list above — it must land on the unreadable
    side, not be normalised to a readable ``{}``. ``{}`` itself IS a mapping and stays
    readable."""
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    expected = ({}, True) if body.strip() == "{}" else ({}, False)
    assert wizard.config.load_config_checked(p) == expected


def test_load_config_checked_explicit_null_document_is_readable(tmp_path):
    """``null`` is legitimately-absent content, not garbage — same as an empty file."""
    p = tmp_path / "config.yaml"
    p.write_text("null\n", encoding="utf-8")
    assert wizard.config.load_config_checked(p) == ({}, True)


# ── one run, ONE config: the decision follows the run's resolved cfg_path ───────────
# setup_interactive resolves `cfg_path = config_path or config.config_path()` and
# confirm_repo_interactive takes it as a parameter; every other read + write in a run
# goes through that value. The label decision must too, or an injected path (the
# `buddhi-review setup` config seam the tests use) would read one file while the same
# run's opt-in is written to another.

def test_injected_cfg_path_decides_the_label_not_the_ambient_env(monkeypatch, tmp_path):
    """$BUDDHI_CONFIG says OFF, the run's own config says ON → the label lands."""
    _real_config(tmp_path, monkeypatch, "label_gated_ci: false\n", name="ambient.yaml")
    run_cfg = tmp_path / "run-config.yaml"
    run_cfg.write_text("repos:\n  o/r:\n    label_gated_ci: true\n", encoding="utf-8")
    result, out, calls = _offer_update("legacy\n", is_tty=True, monkeypatch=monkeypatch,
                                       cfg_path=run_cfg)
    assert result == "pr"
    assert [c for c in calls if c[:3] == ["gh", "pr", "edit"]], calls


def test_injected_cfg_path_off_wins_over_an_ambient_optin(monkeypatch, tmp_path):
    """The mirror image: the ambient env config would attach, the run's config says
    OFF → no label. Proves the injected path is READ, not merely accepted."""
    _real_config(tmp_path, monkeypatch, "label_gated_ci: true\n", name="ambient.yaml")
    run_cfg = tmp_path / "run-config.yaml"
    run_cfg.write_text("label_gated_ci: false\n", encoding="utf-8")
    result, out, calls = _offer_update("legacy\n", is_tty=True, monkeypatch=monkeypatch,
                                       cfg_path=run_cfg)
    assert result == "pr"
    assert not [c for c in calls if c[:3] == ["gh", "pr", "edit"]], calls


# ── the opt-in made in THIS run counts, not just what was on disk when it started ───
# step_reviewers (which opens the update PR) runs BEFORE the label-gated-CI step and
# before the config is written, in both entry points. Deciding the attach from the
# persisted flag alone therefore misses the run that turns label-gated CI on: on a
# repo whose gate workflow is ALREADY live on the default branch, that update PR
# merges unexercised — #94 again. So the attach is DEFERRED to the caller, which
# flushes it once the answer is in.

def _flush(pr_refs, *, opted_in, run, cfg_path=None):
    buf = io.StringIO()
    wizard._flush_pending_ci_labels("o/r", pr_refs, opted_in=opted_in, run=run,
                                    pal=wizard._Palette(False), stream=buf,
                                    cfg_path=cfg_path, sleep=lambda s: None)
    return buf.getvalue()


def _label_recorder(*, edit_rc=0):
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        if list(argv)[:3] == ["gh", "pr", "edit"]:
            return _R(returncode=edit_rc)
        return _R()

    return run, calls


def test_update_pr_label_is_deferred_when_the_caller_asks_later(monkeypatch, tmp_path):
    """With a `pending_ci_prs` sink the update PR is opened and its ref handed back
    UNLABELED — no attach yet, because the opt-in question has not been asked."""
    _real_config(tmp_path, monkeypatch, "label_gated_ci: true\n")  # would attach inline
    pending = []
    result, out, calls = _offer_update("legacy\n", is_tty=True, monkeypatch=monkeypatch,
                                       pending_ci_prs=pending)
    assert result == "pr"
    assert pending == ["https://github.com/o/r/pull/5"]
    assert not [c for c in calls if c[:3] == ["gh", "pr", "edit"]], calls


def test_flush_attaches_on_this_runs_optin_with_nothing_on_disk_yet(monkeypatch, tmp_path):
    """The #94 shape: the gate workflow is live on the default branch, the opt-in is
    not persisted yet (this run is about to write it). The in-run answer alone must
    put the label on the PR."""
    monkeypatch.setenv("BUDDHI_CONFIG", str(tmp_path / "not-written-yet.yaml"))
    run, calls = _label_recorder()
    out = _flush(["https://github.com/o/r/pull/5"], opted_in=True, run=run)
    edits = [c for c in calls if c[:3] == ["gh", "pr", "edit"]]
    assert len(edits) == 1, calls
    assert "--add-label" in edits[0] and "ready-for-ci" in edits[0]
    assert "https://github.com/o/r/pull/5" in edits[0]
    assert any(c[:3] == ["gh", "label", "create"] for c in calls), calls
    assert out == ""


def test_flush_still_honours_the_persisted_flag_when_this_run_says_no(monkeypatch, tmp_path):
    """A run that answers "off" does NOT veto an earlier opt-in: the gate workflow can
    already be live on the default branch, and that PR still needs the label to get
    CI. The in-run answer can only turn the attach ON."""
    _real_config(tmp_path, monkeypatch, "repos:\n  o/r:\n    label_gated_ci: true\n")
    run, calls = _label_recorder()
    _flush(["5"], opted_in=False, run=run)
    assert [c for c in calls if c[:3] == ["gh", "pr", "edit"]], calls


def test_flush_attaches_nothing_when_neither_source_opted_in(monkeypatch, tmp_path):
    """Neither this run nor the config asked for label-gated CI → no stray label."""
    _real_config(tmp_path, monkeypatch, "label_gated_ci: false\n")
    run, calls = _label_recorder()
    _flush(["5"], opted_in=False, run=run)
    assert not [c for c in calls if c[:3] == ["gh", "pr", "edit"]], calls
    assert not [c for c in calls if c[:3] == ["gh", "label", "create"]], calls


@pytest.mark.parametrize("text", ["label_gated_ci: [true\n", "- just\n- a\n- list\n"])
def test_flush_explicit_no_beats_an_unreadable_config(monkeypatch, tmp_path, text):
    """The three-valued `opted_in` must not collapse False into None. The config
    EXISTS but can't be parsed, so `load_config_checked` reports readable=False — the
    fail-closed fallthrough that an unreadable config normally triggers. Here the user
    ALSO answered "no" seconds ago, and that direct answer outranks a file that
    wouldn't parse: no stray label on a repo the user explicitly declined. Only
    `opted_in is None` (no signal at all) may fall through to the attach."""
    p = _real_config(tmp_path, monkeypatch, text)
    assert wizard.config.load_config_checked(p)[1] is False, "sanity: config is unreadable"
    run, calls = _label_recorder()
    out = _flush(["5"], opted_in=False, run=run)
    assert not [c for c in calls if c[:3] == ["gh", "pr", "edit"]], calls
    assert not [c for c in calls if c[:3] == ["gh", "label", "create"]], calls
    assert out == "", out   # "no label needed" is not a failed attach — no warning


def test_flush_no_signal_still_falls_through_on_an_unreadable_config(monkeypatch, tmp_path):
    """The other half of the same distinction: with `opted_in=None` there is no in-run
    answer to honour, so an unreadable config keeps failing CLOSED and attempts the
    attach rather than silently masking a label-gated repo."""
    _real_config(tmp_path, monkeypatch, "label_gated_ci: [true\n")
    run, calls = _label_recorder()
    assert wizard._attach_ready_for_ci("o/r", "5", run=run, sleep=lambda s: None,
                                       opted_in=None) is True
    assert [c for c in calls if c[:3] == ["gh", "pr", "edit"]], calls


def test_flush_warns_once_per_pr_when_the_attach_fails(monkeypatch, tmp_path):
    """A failed deferred attach warns exactly as the inline one does — the user's only
    signal that the update PR may merge without CI."""
    monkeypatch.setenv("BUDDHI_CONFIG", str(tmp_path / "absent.yaml"))
    run, calls = _label_recorder(edit_rc=1)
    out = _flush(["5", "6"], opted_in=True, run=run)
    assert out.count("ready-for-ci") == 4 and out.count("by hand") == 2, out
    assert "to 5 —" in out and "to 6 —" in out, out
    assert "gh pr edit 5 --add-label ready-for-ci" in out, out
    assert "gh pr edit 6 --add-label ready-for-ci" in out, out


def test_flush_with_nothing_pending_touches_no_gh_at_all(monkeypatch):
    """The common case — no managed file was outdated — must shell out to nothing."""
    run, calls = _label_recorder()
    assert _flush([], opted_in=True, run=run) == ""
    assert calls == []


# ── end-to-end: the per-repo confirm labels the update PR it opened earlier ─────────

def _e2e_router(*, default="main", legacy=b"name: legacy claude workflow\n"):
    """A run() for a whole confirm_repo_interactive pass with a Claude-only fleet: the
    claude workflow is PRESENT on the default branch but unversioned (→ outdated, so
    the update PR is offered), and every gh call the update path makes succeeds."""
    calls = []
    installed_b64 = base64.b64encode(legacy).decode()

    def run(argv, cwd=None, timeout=30, input=None):
        argv = list(argv)
        calls.append(argv)
        joined = " ".join(argv)
        if argv[:3] == ["gh", "auth", "status"]:
            return _R(returncode=1)          # no gh auth → skip the secret walkthrough
        if argv[:2] == ["git", "-C"]:
            return _R(returncode=1)          # cwd is passed explicitly
        if argv[:3] == ["gh", "repo", "view"]:
            return _R(returncode=0, stdout=default + "\n")
        if argv[:2] == ["gh", "pr"]:
            return _R(returncode=0, stdout="https://github.com/o/r/pull/7\n")
        if argv[:2] == ["gh", "api"]:
            if "-X" in argv and "PUT" in argv:
                return _R(returncode=0)
            if "claude-code-review.yml" in joined and ".content" in argv:
                return _R(returncode=0, stdout=installed_b64 + "\n")
            if "/git/ref/heads/" in joined and "--jq" in argv:
                return _R(returncode=0, stdout="cafe\n")
            if argv[2].endswith("/git/refs"):
                return _R(returncode=0)
            if "contents/" in joined and ".sha" in argv:
                return _R(returncode=0, stdout="blob123\n")
        return _R()

    return run, calls


def _drive_confirm_with_update(monkeypatch, tmp_path, *, lgc_on):
    """confirm_repo_interactive over o/r: Claude confirmed installed, the outdated
    workflow updated by PR, and label-gated CI answered ``lgc_on`` — the answer the
    reviewer step could not see when it opened that PR."""
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "single_select", _yn_bridge)   # for _ask_yes_no
    # The gate installer has its own suite (tests/test_wizard_ready_for_ci.py); this
    # test is about the label on the UPDATE PR, so keep it out of the router.
    monkeypatch.setattr(wizard, "_offer_install_ready_for_ci", lambda *a, **k: None)
    run, calls = _e2e_router()

    def ss(prompt, options, *, preselect=0, **kw):
        for key, idx in {"reviewer is installed": 1,
                         "Auto-merge default for": 0,
                         "Label-gated CI default for": 1 if lgc_on else 0,
                         "Confirm: enable label-gated CI": 1}.items():
            if key in prompt:
                return idx
        return preselect

    buf = io.StringIO()
    cfg_path = tmp_path / "config.yaml"          # nothing persisted yet — a first run
    rc = wizard.confirm_repo_interactive(
        "o/r", str(tmp_path), run=run, spawn_command=lambda *a, **k: None,
        getpass_fn=lambda *a: "", pal=wizard._Palette(False), stream=buf,
        cfg_path=cfg_path, multi_select=lambda *a, **k: {3},   # claude
        single_select=ss, input_fn=lambda prompt="": "y")
    return rc, buf.getvalue(), calls, cfg_path


def test_confirm_run_labels_the_update_pr_it_opened_before_the_optin(monkeypatch, tmp_path):
    """#94, round 2: the reviewer step opens the claude-code-review.yml update PR
    BEFORE the label-gated-CI step is asked and before anything is written to disk.
    Opting in seconds later must still get the label onto that PR — otherwise, on a
    repo whose gate workflow is already live, the update merges with CI never run."""
    rc, out, calls, cfg_path = _drive_confirm_with_update(monkeypatch, tmp_path,
                                                          lgc_on=True)
    assert rc == 0
    assert wizard.config.label_gated_ci(wizard.config.load_config(cfg_path), "o/r") is True
    creates = [c for c in calls if c[:3] == ["gh", "pr", "create"]]
    edits = [c for c in calls if c[:3] == ["gh", "pr", "edit"]]
    assert len(creates) == 1, calls          # the update PR was opened
    assert len(edits) == 1, calls            # …and then labeled
    assert "--add-label" in edits[0] and "ready-for-ci" in edits[0]
    assert calls.index(creates[0]) < calls.index(edits[0])


def test_confirm_run_without_the_optin_leaves_the_update_pr_unlabeled(monkeypatch, tmp_path):
    """The same run, label-gated CI declined and nothing on disk → the update PR is
    still opened, and no stray label is added to a repo whose CI runs on every push."""
    rc, out, calls, cfg_path = _drive_confirm_with_update(monkeypatch, tmp_path,
                                                          lgc_on=False)
    assert rc == 0
    assert wizard.config.label_gated_ci(wizard.config.load_config(cfg_path), "o/r") is False
    assert [c for c in calls if c[:3] == ["gh", "pr", "create"]], calls
    assert not [c for c in calls if c[:3] == ["gh", "pr", "edit"]], calls
