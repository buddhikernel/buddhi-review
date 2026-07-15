#!/usr/bin/env python3
"""PreToolUse(Bash) entry for the git guardrail — the PLUGIN path only.

When the skills run from a ``/plugin install`` (rather than a ``pip install`` +
``~/.claude/skills`` copy), the ``buddhi_review`` package is not on the default
import path — the SessionStart hook (``ensure_install.py``) installs it into
``${CLAUDE_PLUGIN_DATA}/site``. This entry:

1. puts that install directory on ``sys.path`` so the package resolves, then
2. delegates to the real hook (:mod:`buddhi_review.git_guardrail_hook`).

If the package is STILL absent (e.g. an offline first run before the install
lands), it DEGRADES FAIL-OPEN: one clear stderr line naming the fix and exit 0 —
never a raw ``ModuleNotFoundError`` traceback surfacing on every Bash call, and
never a blocked command. The install retries on the next SessionStart.

The pip-install path never reaches this file: the skills' ``SKILL.md`` ``hooks:``
frontmatter runs it ONLY when ``$CLAUDE_PLUGIN_ROOT`` is set, and otherwise runs
``python3 -m buddhi_review.git_guardrail_hook`` unchanged. Pure stdlib.
"""
from __future__ import annotations

import os
import sys

# One line, printed to stderr, when the package cannot be imported. The exact
# substring ``pip install buddhi-review`` is asserted by the degrade test.
DEGRADE_MESSAGE = (
    "buddhi-review is not installed yet — the git guardrail is inactive for now; "
    "run: pip install buddhi-review"
)

# Printed instead of DEGRADE_MESSAGE when the import fails for a reason OTHER than
# the package being absent (e.g. a syntax error or a broken transitive import) —
# so a real regression doesn't masquerade as "not installed yet".
BROKEN_INSTALL_MESSAGE = (
    "buddhi-review failed to import (not a missing-package issue) — "
    "the git guardrail is inactive for now: {exc!r}"
)


def _prepend_data_site():
    """Prepend ``${CLAUDE_PLUGIN_DATA}/site`` (the SessionStart install target) to
    ``sys.path`` so a data-dir install is importable. No-op when the variable is
    unset or the directory does not exist yet."""
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if not data:
        return
    site = os.path.join(data, "site")
    if os.path.isdir(site) and site not in sys.path:
        sys.path.insert(0, site)


def run():
    """Delegate to the real guardrail, or degrade fail-open. Returns the process
    exit code (0 on the degrade path; whatever the real hook returns otherwise —
    also 0, since the guardrail never breaks a session)."""
    _prepend_data_site()
    try:
        from buddhi_review import git_guardrail_hook
    except ModuleNotFoundError:
        sys.stderr.write(DEGRADE_MESSAGE + "\n")
        return 0
    except Exception as exc:  # noqa: BLE001 - still fail open, but flag it distinctly
        sys.stderr.write(BROKEN_INSTALL_MESSAGE.format(exc=exc) + "\n")
        return 0
    return git_guardrail_hook.main()


if __name__ == "__main__":
    sys.exit(run())
