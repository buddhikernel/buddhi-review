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


def _offer_update(installed_text, *, is_tty, monkeypatch, run=None, accept=True):
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
        input_fn=lambda prompt="": "y" if accept else "n")
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
    """A failed/raising label add leaves the update PR intact — the PR is open, the
    wizard already reported it; the label is best-effort."""
    def run(argv, **kw):
        if list(argv)[:3] == ["gh", "pr", "edit"]:
            raise OSError("boom")
        return _update_router()(argv, **kw)
    result, out, calls = _offer_update_lgc("legacy\n", monkeypatch=monkeypatch,
                                           label_gated=True, run=run)
    assert result == "pr"
