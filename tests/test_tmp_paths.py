"""The publish-clean /tmp filename helper (``buddhi_review/tmp_paths.py``).

Pins the three filename FORMATS the free skill writes — log, tailcmd, answer —
to the reference convention exactly (``<repo>`` = the part after the slash, or
``"local"``; the PR number always last and ``PR``-prefixed), plus the multi-ask
console answer variant and the ask-id sanitizer. These formats are the single
source the launcher (bash) and the notifier (python) both resolve from, so the
two can never drift.
"""
from __future__ import annotations

from buddhi_review import tmp_paths


def test_repo_short_owner_repo_bare_and_local():
    assert tmp_paths.repo_short("acme/demo") == "demo"
    assert tmp_paths.repo_short("demo") == "demo"          # already short → passthrough
    assert tmp_paths.repo_short("a/b/c") == "c"            # only the part after the LAST slash
    assert tmp_paths.repo_short("") == "local"             # empty → local
    assert tmp_paths.repo_short(None) == "local"           # None → local
    assert tmp_paths.repo_short("acme/demo/") == "demo"    # trailing slash stripped
    assert tmp_paths.repo_short("demo/") == "demo"         # already short, trailing slash stripped
    assert tmp_paths.repo_short("/") == "local"            # slash-only → empty after strip → local
    assert tmp_paths.repo_short("acme\\demo") == "demo"    # backslash normalized
    assert tmp_paths.repo_short("acme/demo..") == "demo.."  # dots allowed
    assert tmp_paths.repo_short("acme/demo?") == "demo_"   # unsafe char sanitized


def test_log_name_format():
    assert tmp_paths.log_name("123", "acme/demo") == "buddhi-demo-PR123.log"
    assert tmp_paths.log_name(7, None) == "buddhi-local-PR7.log"
    assert tmp_paths.log_name("../../../etc/passwd", None) == "buddhi-local-PR.log"


def test_tailcmd_name_format():
    assert tmp_paths.tailcmd_name("123", "acme/demo") == "review-tail-demo-PR123.command"
    assert tmp_paths.tailcmd_name(7) == "review-tail-local-PR7.command"


def test_answer_name_format_is_per_pr():
    assert tmp_paths.answer_name("123", "acme/demo") == "review-answer-demo-PR123.md"
    assert tmp_paths.answer_name(7) == "review-answer-local-PR7.md"


def test_sanitize_ask_id_keeps_safe_chars():
    assert tmp_paths.sanitize_ask_id("fix-c1") == "fix-c1"
    assert tmp_paths.sanitize_ask_id("test_gate") == "test_gate"
    assert tmp_paths.sanitize_ask_id("a/b c.d") == "a_b_c_d"   # / space . → _
    assert tmp_paths.sanitize_ask_id("") == ""
    assert tmp_paths.sanitize_ask_id(None) == ""


def test_answer_name_for_ask_with_pr_and_repo():
    # The console rail keys each ask per (repo, PR, ask) so two loops with the same
    # fixed-id ask never collide; the PR-keyed stem matches answer_name().
    assert (tmp_paths.answer_name_for_ask("test-gate", "123", "acme/demo")
            == "review-answer-demo-PR123-test-gate.md")
    assert (tmp_paths.answer_name_for_ask("fix-c1", 9, "acme/demo")
            == "review-answer-demo-PR9-fix-c1.md")


def test_answer_name_for_ask_without_pr_omits_pr_segment():
    # A decision-only / self-check fallback owns no PR → a stable name minus -PR<pr>.
    assert tmp_paths.answer_name_for_ask("c1") == "review-answer-local-c1.md"
    assert tmp_paths.answer_name_for_ask("c1", None, None) == "review-answer-local-c1.md"
    assert tmp_paths.answer_name_for_ask("c1", "", "acme/demo") == "review-answer-demo-c1.md"


def test_answer_name_for_ask_sanitizes_the_id():
    assert (tmp_paths.answer_name_for_ask("weird id/x", "5", "acme/demo")
            == "review-answer-demo-PR5-weird_id_x.md")
    assert (tmp_paths.answer_name_for_ask("test-gate", "../../../etc/passwd", "acme/demo")
            == "review-answer-demo-test-gate.md")
