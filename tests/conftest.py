"""Suite-wide hermeticity shim for the PR-intent seam.

The fix-verify pass can consult the PR's own title + body via ``gh pr view`` to
catch a fix that undoes deliberate work. Pin every test to the network-free empty
seam (``BUDDHI_REVIEW_PR_INTENT_JSON``) so the suite never shells out to the real
``gh`` binary — including the pre-existing ``apply_fix`` verify-path tests, which
reach the live fetch without setting the seam themselves. A test that wants a
specific intent sets the seam in its own body; one exercising the ``gh`` fallback
deletes the seam and injects its own runner.
"""
import pytest

from buddhi_review import fix_apply


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
def _hermetic_pr_intent(monkeypatch):
    fix_apply.reset_pr_intent()
    monkeypatch.setenv(fix_apply.PR_INTENT_JSON_ENV, '{"title": "", "body": ""}')
    yield
    fix_apply.reset_pr_intent()
