---
title: Contributing
---

# Contributing

Thanks for helping improve `buddhi-review`. It is a small adapter that re-homes the
public [Buddhi kernel](https://github.com/buddhikernel/buddhi) onto the GitHub
PR-review substrate; please keep changes focused and well-tested.

## Development setup

```bash
git clone https://github.com/buddhikernel/buddhi-review
cd buddhi-review
pip install -e ".[test]"
python3 -m pytest -q
```

The suite runs offline: `tests/conftest.py` shims the `gh` and Claude seams, so no
network or `claude` CLI is needed. Add a test for any logic change.

Before proposing a packaging change, run the publish-readiness gate, which builds the
sdist + wheel and scans the published bytes:

```bash
python tools/publish_gate.py scan
python tools/publish_gate.py publish --check
```

## Invariants to preserve

These hold the line between the free skill and anything paid; a change that breaks one
will be sent back:

- **No license / lease / activation logic in the package code.** The free skill never
  checks an entitlement; it always runs. Keep it that way, and never add code that
  gates a feature behind a key or a remote check.
- **The two reserved kernel cells stay as interfaces.** The out-of-band resolution
  seam delegates to an injected hook only (it never autonomously detects, reconciles,
  or skips a resolution), and the conditioning stage is an identity pass-through (it
  never rewrites an item in place). Do not implement logic inside either.
- **The bundled `claude-code-review.yml` ships verbatim.** Its prompt emits a literal
  `No issues found.` line on a clean review, and the loop's clean-review detector is
  coupled to that exact string. Change one only by changing both in lockstep.
- **The published wheel is pure Python** with no compiled extension and no private
  identifiers. The publish gate enforces this; do not work around it.

## Pull requests

- Match the style of the surrounding code: the comment density, naming, and idiom.
- Keep the diff scoped to one change; explain the why in the PR body.
- CI runs the test suite and the publish gate on every PR. Both must be green.

By contributing you agree your work is licensed under the MIT license (see
[LICENSE](LICENSE)).
