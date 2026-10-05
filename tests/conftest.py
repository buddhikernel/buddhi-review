"""Suite-wide hermeticity shims: the PR-intent seam, the on-disk config, and the
``.claude`` settings guard's containment (see ``_contained_settings_guard``).

The fix-verify pass can consult the PR's own title + body via ``gh pr view`` to
catch a fix that undoes deliberate work. Pin every test to the network-free empty
seam (``BUDDHI_REVIEW_PR_INTENT_JSON``) so the suite never shells out to the real
``gh`` binary — including the pre-existing ``apply_fix`` verify-path tests, which
reach the live fetch without setting the seam themselves. A test that wants a
specific intent sets the seam in its own body; one exercising the ``gh`` fallback
deletes the seam and injects its own runner.

The test gate resolves its command through the CONFIG (``test_command``, global +
per-repo) as well as the environment, so an unpinned suite would read the
developer's real ``~/.config/review-loop/config.yaml`` and a machine with a global
``test_command`` set would flip gate assertions. Point ``BUDDHI_CONFIG`` at a
per-test tmp path (a file that does not exist → the empty-config default) and
clear ``BUDDHI_TEST_COMMAND``; a test that wants a config writes to
``tmp_path``/its own path and sets the env itself.
"""
import sys

import pytest

from buddhi_review import claude_settings_guard, fix_apply, gh_ingest, polish_state


def _log_line(stdout):
    """The single `log: <path>` line from the launcher's stdout, which now also
    carries S3 `NOTICE: ` relay lines. Returns the line WITHOUT the `log: ` prefix."""
    for ln in stdout.splitlines():
        if ln.startswith("log: "):
            return ln[len("log: "):]
    raise AssertionError(f"no `log:` line on stdout:\n{stdout}")


def _yn_bridge(prompt, options, *, preselect=0, input_fn=input, **kw):
    """Bridge single_select for _ask_yes_no on a forced TTY: reads the test's
    input_fn (which supplies 'y'/'n'/'') and maps to an option index."""
    try:
        raw = (input_fn(prompt) or "").strip().lower()
    except EOFError:
        raw = ""
    if raw in ("y", "yes", "1"):
        return 0
    if raw in ("n", "no", "2"):
        return 1
    return preselect


@pytest.fixture(autouse=True)
def _hermetic_config(monkeypatch, tmp_path):
    """Never let a test read the developer's real config or a stray
    ``BUDDHI_TEST_COMMAND``. Both feed :func:`commit_push.resolve_test_command`,
    so an unpinned suite is machine-dependent."""
    monkeypatch.setenv("BUDDHI_CONFIG", str(tmp_path / "absent-config.yaml"))
    monkeypatch.delenv("BUDDHI_TEST_COMMAND", raising=False)
    # A round driver whose gh fake reports the PR's head stamps polish verdicts
    # against it; keep those stamps in the test's own directory.
    monkeypatch.setenv(polish_state.STATE_DIR_ENV, str(tmp_path / "polish-state"))


@pytest.fixture(autouse=True)
def _contained_settings_guard(monkeypatch, tmp_path):
    """Keep the ``.claude`` settings guard off the contributor's own checkout.

    Its journal and lock live under this test's tmp dir, and a falsy ``cwd`` (a
    model call given none inherits the process's working directory, which is the
    checkout the suite runs from and may hold the contributor's own
    ``.claude/settings.local.json``) resolves to a per-test path instead. Both are
    environment variables the guard reads at call time, so the containment
    survives ``importlib.reload`` of the guard and of any module importing it. A
    test pinning the falsy-cwd contract deletes the fallback itself.

    ``_run_loop`` installs a process-wide base resolver; it is removed after every
    test, so a later test never resolves through an earlier test's ``gh`` stub or
    the real ``gh``."""
    monkeypatch.setenv(claude_settings_guard.STATE_DIR_ENV, str(tmp_path / "settings-guard-state"))
    monkeypatch.setenv(claude_settings_guard.FALLBACK_CWD_ENV, str(tmp_path / "settings-guard-cwd"))
    yield
    sys.modules["buddhi_review.claude_settings_guard"].uninstall_base_resolver()


@pytest.fixture(autouse=True)
def _hermetic_pr_intent(monkeypatch):
    fix_apply.reset_pr_intent()
    monkeypatch.setenv(fix_apply.PR_INTENT_JSON_ENV, '{"title": "", "body": ""}')
    # The round driver reads PR-body reactions (a +1 = voluntarily-done). Pin the
    # reactions seam to empty so any driver constructed without an injected
    # reactions fetch stays network-free; a reactions test either injects its own
    # fetch or sets this env in its own body.
    monkeypatch.setenv(gh_ingest.REACTIONS_JSON_ENV, "[]")
    # The update-availability banner (update_banner) does a cached PyPI read at the
    # /review-pr and /open-pr launch surfaces. Pin it OFF suite-wide so no test ever
    # phones home; the dedicated tests/test_update_banner.py re-enables it and injects
    # a fetcher, and any test that wants the banner sets the env in its own body.
    monkeypatch.setenv("BUDDHI_NO_UPDATE_CHECK", "1")
    yield
    fix_apply.reset_pr_intent()
