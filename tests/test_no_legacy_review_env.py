"""Grep-guard: the legacy ``REVIEW_*`` env-var prefix can never sneak back.

The product is Buddhi; every product environment variable uses the ``BUDDHI_*``
prefix (hard cut — there is no ``REVIEW_*`` fallback). This test scans the
package's own source — Python, shell, Markdown and config text — for any
standalone ``REVIEW_<NAME>`` token and fails if one appears that is not on the
small, explicit allow-list below.

Allow-list (the ONLY ``REVIEW_`` tokens that are deliberately NOT env vars):
  - ``REVIEW_BODY_WITH_FOOTER`` / ``REVIEW_BODY_AMBIGUOUS_WITH_FOOTER`` —
    review-body text fixtures in the detector tests, plain module-level
    constants, never an environment variable.

A negative-lookbehind on a word character means ``BUDDHI_REVIEW_*`` (e.g.
``BUDDHI_REVIEW_TMP``), ``CLEAN_REVIEW_PATTERNS`` and ``_REVIEW_FEEDBACK_RE``
are not flagged — only a token that *starts* with ``REVIEW_`` matches.
"""
import os
import re
from pathlib import Path

PUBLIC_ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

# REVIEW_<NAME> tokens that are intentionally NOT product env vars.
ALLOWLIST = {
    "REVIEW_BODY_WITH_FOOTER",
    "REVIEW_BODY_AMBIGUOUS_WITH_FOOTER",
}

# A standalone REVIEW_<NAME> token: not preceded by a word character, so
# BUDDHI_REVIEW_* / CODE_REVIEW_* / _REVIEW_* never match.
_REVIEW_TOKEN = re.compile(r"(?<![A-Za-z0-9_])(REVIEW_[A-Z0-9_]+)")

# The env-access forms specifically — os.environ/getenv/setenv reads in Python
# and ``${REVIEW_...}`` expansions in shell; a second, intent-aligned assertion.
_ENV_ACCESS = re.compile(
    r"""(?:os\.environ(?:\.get|\.pop|\.setdefault)?|\benviron(?:\.get|\.pop|\.setdefault)?|"""
    r"""\bgetenv|\bsetenv|\bdelenv|monkeypatch\.(?:set|del)env)\s*\(\s*['"](REVIEW_[A-Z0-9_]+)"""
    r"""|os\.environ\[\s*['"](REVIEW_[A-Z0-9_]+)"""
    r"""|environ\[\s*['"](REVIEW_[A-Z0-9_]+)"""
    r"""|\$\{?(REVIEW_[A-Z0-9_]+)"""
)

SKIP_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".venv",
    "venv",
    "env",
    ".tox",
    "build",
    "dist",
}
SCAN_SUFFIXES = {".py", ".sh", ".md", ".yml", ".yaml", ".toml", ".txt"}


def _sources():
    for root, dirs, files in os.walk(PUBLIC_ROOT):
        # prune in-place so os.walk never descends into skipped trees
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        root_path = Path(root)
        for fname in files:
            p = root_path / fname
            if p.suffix not in SCAN_SUFFIXES:
                continue
            if p.resolve() == SELF:
                continue
            yield p


def test_no_legacy_review_env_token():
    """No standalone REVIEW_<NAME> survives in public sources outside the allow-list."""
    offenders = {}
    for p in _sources():
        try:
            src = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        bad = {t for t in _REVIEW_TOKEN.findall(src) if t not in ALLOWLIST}
        if bad:
            offenders[str(p.relative_to(PUBLIC_ROOT))] = sorted(bad)
    assert not offenders, (
        "Legacy REVIEW_* env var(s) reintroduced — rename to BUDDHI_* "
        f"(hard cut, no fallback):\n{offenders}"
    )


def test_no_legacy_review_env_access():
    """No os.environ/getenv/setenv read (Python) or ${...} expansion (shell) of a REVIEW_ name."""
    offenders = {}
    for p in _sources():
        try:
            src = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        bad = set()
        for m in _ENV_ACCESS.finditer(src):
            name = m.group(1) or m.group(2) or m.group(3) or m.group(4)
            if name and name not in ALLOWLIST:
                bad.add(name)
        if bad:
            offenders[str(p.relative_to(PUBLIC_ROOT))] = sorted(bad)
    assert not offenders, (
        f"Legacy REVIEW_* env var ACCESS reintroduced — use BUDDHI_*:\n{offenders}"
    )
