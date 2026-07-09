"""buddhi_review — the free, MIT-licensed Claude Code PR-review skill on the buddhi kernel.

Drives the kernel's seven decisions over a stream of PR review comments:
the kernel decides the coarse disposition per comment (act / ask-a-human / skip /
stop under a bounded interrupt budget); this adapter supplies the substrate I/O
(read comments from ``gh``, classify with ``claude -p``, apply fixes, escalate via
the **console** answer-file channel).

Escalations go to the console answer-file channel: a pending question is written to
an editable file you answer from the terminal.
"""
from __future__ import annotations

# Single source of truth for the package version. pyproject.toml reads this string
# literal via setuptools' ``dynamic = ["version"]`` (``attr = buddhi_review.__version__``),
# which extracts it by AST WITHOUT importing the package — so it MUST stay a plain
# top-level string literal (never computed). ``skill_provenance.package_version()``
# returns it, and the version-stamp transform records it into installed skills.
# The trailing ``# x-release-please-version`` marker lets release-please rewrite the
# version in place on a release; it is a comment, so the AST literal is unaffected.
__version__ = "0.2.1"  # x-release-please-version

__all__ = [
    "Classification",
    "classify_comment",
    "parse_classification",
    "automation_notice",
    "Backend",
    "launch_review_loop",
    "discover_backends",
    "__version__",
]

# Lazy re-exports (PEP 562). Importing the package root must NOT eagerly pull in
# the classify/transparency chain, which imports the buddhi kernel. That keeps the
# stdlib-only entry points — notably the PreToolUse git-guardrail hook, run as
# ``python3 -m buddhi_review.git_guardrail_hook`` (which imports the package root
# first) — importable and genuinely fail-open even when the kernel is absent or a
# different interpreter is resolved. The public names below still resolve on first
# attribute access, so ``from buddhi_review import Classification`` is unchanged.
# The FREE-1 backend front door (``Backend`` / ``discover_backends`` /
# ``launch_review_loop``) is re-exported the same lazy way: ``backends`` is
# stdlib-only today, but routing it through ``_LAZY`` keeps the package root's
# import side-effect-free regardless of what a backend later pulls in.
_LAZY = {
    "Classification": "buddhi_review.classify",
    "classify_comment": "buddhi_review.classify",
    "parse_classification": "buddhi_review.classify",
    "automation_notice": "buddhi_review.transparency",
    "Backend": "buddhi_review.backends",
    "discover_backends": "buddhi_review.backends",
    "launch_review_loop": "buddhi_review.backends",
}


def __getattr__(name):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
