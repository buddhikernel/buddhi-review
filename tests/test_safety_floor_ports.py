"""Safety-floor parity ports (build-spec §5 manifest) — the A5 tripwire cases not
already pinned in ``test_fix_apply.py`` / ``test_fix_apply_hardening.py``, plus the
#294 tmp-isolation harness-hygiene guard.

The §5 safety floor (A1 empirical-verify golden, A4 verify CONFIRM/REJECT/fail-open,
the classifier-handoff Phase-1 byte-identical golden, and the clean-review +
``No issues found.`` sentinel coupling) is already pinned in this suite — see
``test_fix_apply.py`` (A1 golden, handoff golden, A4 gating), ``test_fix_apply_hardening.py``
(A4 stdout verdicts, A5 ``*_FLAGS`` reason), and ``test_detectors.py`` (sentinel
coupling). This file closes the remaining named A5 cases so the whole §5 manifest
is covered, and adds the harness-hygiene guard.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from buddhi_review import fix_apply, notifier
from buddhi_review.fix_apply import diff_tripwire


# ===========================================================================
# A5 — dangerous-change tripwire: the remaining named reference cases
# ===========================================================================

def test_a5_whole_test_file_deletion_trips():
    # A fixer that deletes a whole test file ("+++ /dev/null", every line a "-def
    # test_…" removal) trips the "removes a test" predicate — a fix can never
    # delete a test to make a point pass.
    diff = (
        "--- a/tests/test_widget.py\n"
        "+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n"
        "-def test_widget_renders():\n"
        "-    assert render() == 'ok'\n"
    )
    reason = diff_tripwire(diff)
    assert reason is not None
    assert "removes a test" in reason


def test_a5_empty_or_unavailable_diff_never_trips():
    # An empty / None / placeholder diff has no +/- content, so it can never trip.
    for d in ("", None, "(round diff unavailable)", "(round diff empty)\n"):
        assert diff_tripwire(d) is None, f"unexpected trip on {d!r}"


def test_a5_outside_threshold_is_env_tunable(monkeypatch):
    # The default sprawl threshold reads BUDDHI_FIX_TRIPWIRE_OUTSIDE_LINES (the
    # single source the fixer reads), floored at 1. Pin the env seam directly so
    # the test does not depend on import-time evaluation.
    assert fix_apply._env_int("BUDDHI_FIX_TRIPWIRE_OUTSIDE_LINES", 40, floor=1) == 40

    monkeypatch.setenv("BUDDHI_FIX_TRIPWIRE_OUTSIDE_LINES", "5")
    assert fix_apply._env_int("BUDDHI_FIX_TRIPWIRE_OUTSIDE_LINES", 40, floor=1) == 5
    monkeypatch.setenv("BUDDHI_FIX_TRIPWIRE_OUTSIDE_LINES", "garbage")
    assert fix_apply._env_int("BUDDHI_FIX_TRIPWIRE_OUTSIDE_LINES", 40, floor=1) == 40
    monkeypatch.setenv("BUDDHI_FIX_TRIPWIRE_OUTSIDE_LINES", "0")  # below the floor
    assert fix_apply._env_int("BUDDHI_FIX_TRIPWIRE_OUTSIDE_LINES", 40, floor=1) == 1


def test_a5_unknown_commented_path_skips_the_outside_condition():
    # With no commented file given, the "lines outside the commented region" check
    # is skipped entirely — only the structural predicates (flags / assertions /
    # tests) can trip. A large benign diff in one file does NOT trip.
    diff = "+++ b/app/big.py\n" + "".join(f"+    line_{i} = {i}\n" for i in range(200))
    assert diff_tripwire(diff) is None              # no commented_files → no outside check
    assert diff_tripwire(diff, commented_files=("app/other.py",),
                         outside_limit=40) is not None  # now the sprawl trips


# ===========================================================================
# #294 — tmp-isolation harness hygiene
# ===========================================================================

def test_answer_file_honours_the_tmp_seam_not_a_hardcoded_path(tmp_path, monkeypatch):
    # The console answer-file path is resolved through BUDDHI_REVIEW_TMP, so the
    # suite never writes to a shared temp location and is portable. #294: pytest's
    # tmp_path is /var/folders on macOS and /tmp on Linux CI, so a test must NEVER
    # assert a "/tmp/…" prefix — it asserts isolation UNDER the configured seam.
    monkeypatch.setenv("BUDDHI_REVIEW_TMP", str(tmp_path))
    p = notifier._answer_path("c1")
    assert p == tmp_path / "review-answer-local-c1.md"
    assert Path(p).resolve().is_relative_to(tmp_path.resolve())  # under the seam dir


def test_answer_file_round_trip_writes_only_under_the_seam(tmp_path, monkeypatch):
    # Sending an ask creates exactly one answer file, and it lands under the seam
    # dir — never in the process-wide tempdir.
    monkeypatch.setenv("BUDDHI_REVIEW_TMP", str(tmp_path))
    n = notifier.ConsoleNotifier()
    ask = notifier.Ask(id="hyg1", question="Proceed?", options=["Yes", "No"],
                       recommended_index=0)
    n.send(ask)
    written = list(tmp_path.glob("review-answer-*.md"))
    assert written == [tmp_path / "review-answer-local-hyg1.md"]
    # nothing leaked into the system tempdir under this id
    assert not (Path(tempfile.gettempdir()) / "review-answer-local-hyg1.md").exists()


# ===========================================================================
# A5 hardening — the post-image marker-span index (the wide-construct closer)
# and the trip-aware verify-diff composer
# ===========================================================================

def _spans(text):
    return fix_apply._tripwire_marker_spans(text)


def test_span_wide_tuple():
    text = "A = 1\nX_FLAGS = (\n" + "    'a',\n" * 50 + ")\nB = 2\n"
    assert _spans(text) == [(2, 53)]


def test_span_single_line_constant():
    assert _spans("X_FLAGS = ('a', 'b')\n") == [(1, 1)]


def test_span_isolation_dict():
    assert _spans("SANDBOX_ISOLATION = {\n    'a': 1,\n}\n") == [(1, 3)]


def test_span_annotated_constant():
    # The annotation's own balanced brackets must not end the span early.
    assert _spans("X_FLAGS: Tuple[str, ...] = (\n    'a',\n)\n") == [(1, 3)]


def test_span_brackets_inside_strings_do_not_derail():
    text = "X_FLAGS = (\n    '--filter=(a',\n    \"} ] )\",\n)\n"
    assert _spans(text) == [(1, 4)]


def test_span_comment_brackets_ignored():
    text = "X_FLAGS = (\n    'a',  # opens ( ( ( but is a comment\n)\n"
    assert _spans(text) == [(1, 3)]


def test_span_triple_quoted_brackets_ignored():
    text = ('X_FLAGS = (\n    """doc ( [ {\n    still string )\n    """,\n'
            '    "a",\n)\n')
    assert _spans(text) == [(1, 6)]


def test_span_prose_isolation_mention_opens_no_span():
    # Assignment shape is required — a comment/prose mention is not a span.
    assert _spans("# We rely on SNAPSHOT ISOLATION (see ADR-14\nplain = 1\n") == []


def test_span_backslash_continuation_extends():
    assert _spans("X_FLAGS = \\\n    (\n    'a',\n)\nB = 1\n") == [(1, 4)]


def test_span_annotated_backslash_continuation_extends():
    # Annotation brackets (Tuple[str, ...]) must not end the span early when
    # the value opens on the next line via backslash continuation.
    assert _spans("X_FLAGS: Tuple[str, ...] = \\\n    (\n    'a',\n)\nB = 1\n") == [(1, 4)]


def test_span_unterminated_construct_spans_to_eof():
    text = "X_FLAGS = (\n    'a',\n    'b',\n"
    s = _spans(text)
    assert s[0][0] == 1
    assert s[0][1] == len(text.split("\n"))


def test_span_pep701_nested_fstring_shortens_documented_residual():
    # KNOWN residual: a PEP-701 same-quote nested f-string makes the lexer
    # treat post-close characters as code, so an unbalanced `)` inside the
    # format expression can end the span early. This test PINS the limitation
    # (the per-hunk scan floor still applies); it is not desired behavior.
    text = ('RUN_FLAGS = (f"{q("a)b")}",\n'
            "    'x',\n"
            ")\n")
    s = _spans(text)
    assert s[0][0] == 1
    assert s[0][1] < 3   # closes before the construct's real end


def _deep_hunk(path="pkg/consts.py", start=100):
    # A hunk deep inside a wide construct: no marker in context or @@ header.
    return (f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n+++ b/{path}\n"
            f"@@ -{start},3 +{start},3 @@\n"
            "     '--opt98',\n"
            "-    '--opt99',\n"
            "+    '--opt99', '--available-tools', 'write',\n"
            "     '--opt100',\n")


def test_a5_wide_tuple_edit_trips_with_spans():
    spans = {"pkg/consts.py": ((1, 202),)}
    reason = diff_tripwire(_deep_hunk(), marker_spans=spans)
    assert reason is not None
    assert "edits a *_FLAGS / ISOLATION constant" in reason


def test_a5_wide_tuple_edit_without_spans_documents_the_old_miss():
    # Without the span index the same hunk does NOT trip — this pins the exact
    # hole the index closes.
    assert diff_tripwire(_deep_hunk()) is None


def test_a5_edit_outside_any_span_does_not_trip():
    spans = {"pkg/consts.py": ((1, 20),)}
    assert diff_tripwire(_deep_hunk(start=500), marker_spans=spans) is None


def test_a5_deletion_inside_span_trips():
    d = ("diff --git a/pkg/consts.py b/pkg/consts.py\n"
         "--- a/pkg/consts.py\n+++ b/pkg/consts.py\n"
         "@@ -50,3 +50,2 @@\n"
         "     '--opt48',\n"
         "-    '--opt49',\n"
         "     '--opt50',\n")
    spans = {"pkg/consts.py": ((1, 202),)}
    reason = diff_tripwire(d, marker_spans=spans)
    assert reason is not None
    assert "edits a *_FLAGS / ISOLATION constant" in reason


def test_a5_blank_context_line_advances_the_line_counter():
    # suppressBlankEmpty-style diffs emit "" for a blank context line; it must
    # still consume a new-file line or span membership drifts.
    d = ("diff --git a/pkg/consts.py b/pkg/consts.py\n"
         "--- a/pkg/consts.py\n+++ b/pkg/consts.py\n"
         "@@ -8,4 +8,4 @@\n"
         " ctx\n"
         "\n"
         "-old\n"
         "+new\n")
    assert diff_tripwire(d, marker_spans={"pkg/consts.py": ((10, 10),)}) is not None


def test_a5_flags_use_site_edit_trips_without_definition_anchor():
    # The widened marker: a *_FLAGS USE (no `=`/`:`) on a changed line.
    d = ("diff --git a/runner.py b/runner.py\n"
         "--- a/runner.py\n+++ b/runner.py\n"
         "@@ -10,1 +10,2 @@\n"
         " ctx\n"
         "+argv += list(BASE_FLAGS)\n")
    assert diff_tripwire(d) is not None


def test_spans_for_diff_collects_postimage(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "c.py").write_text("X_FLAGS = (\n    'a',\n)\n")
    d = ("diff --git a/pkg/c.py b/pkg/c.py\n"
         "--- a/pkg/c.py\n+++ b/pkg/c.py\n@@ -1 +1 @@\n-a\n+b\n")
    spans = fix_apply._tripwire_spans_for_diff(d, str(tmp_path))
    assert spans == {"pkg/c.py": ((1, 3),)}


def test_spans_for_diff_skips_non_python_missing_and_traversal(tmp_path):
    (tmp_path / "c.sql").write_text("-- ISOLATION (\nselect 1;\n")
    d = ""
    for p in ("c.sql", "gone.py", "../../etc/pw.py"):
        d += (f"diff --git a/{p} b/{p}\n"
              f"--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-a\n+b\n")
    assert fix_apply._tripwire_spans_for_diff(d, str(tmp_path)) == {}


_FLAGS_HUNK = ("diff --git a/n.py b/n.py\n"
               "--- a/n.py\n+++ b/n.py\n"
               "@@ -59,3 +59,4 @@ COPILOT_TOOL_ISOLATION_FLAGS = (\n"
               '     "--a",\n'
               '+    "--available-tools", "write",\n'
               " )\n")


def _big_benign(n_bytes):
    line = "+" + "x" * 98 + "\n"
    header = ("diff --git a/big.txt b/big.txt\n--- a/big.txt\n"
              "+++ b/big.txt\n@@ -0,0 +1,999 @@\n")
    return header + line * (n_bytes // len(line) + 1)


def test_compose_small_diff_verbatim():
    d = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n"
    assert fix_apply._compose_verify_diff(d, True) == d


def test_compose_flagged_hunk_past_the_cap_rides_first():
    # The past-the-cap blind spot, closed: the dangerous hunk sits past 60KB
    # of benign churn, yet the verify artifact leads with it.
    out = fix_apply._compose_verify_diff(_big_benign(70000) + _FLAGS_HUNK, True)
    assert out.startswith(fix_apply._VERIFY_DIFF_NOTE)
    assert '"--available-tools", "write",' in out
    assert (len(out.encode())
            <= fix_apply._ATTEMPT_DIFF_MAX_BYTES
            + len(fix_apply._DIFF_TRUNCATED_SENTINEL.encode()))


def test_compose_tripped_but_no_flagged_hunks_falls_back_to_cap():
    # An outside-region-only or scan-budget force has no specific hunk.
    out = fix_apply._compose_verify_diff(_big_benign(70000), True)
    assert "NOTE:" not in out
    assert "[diff truncated]" in out


# ===========================================================================
# A5 hardening — adversarial-verify pinning tests (span-lexer escapes,
# in-hunk header guard, untracked-junk filtering)
# ===========================================================================

def test_span_escaped_quote_in_triple_quoted_element_keeps_span_long():
    # A `\"""` inside a triple-quoted element is an ESCAPED quote, not a
    # terminator — misreading it closed the span early (fail short) and
    # reopened the wide-construct hole for ordinary docstring-style elements.
    text = ('SOME_FLAGS = (\n'
            '    """usage: pass \\""" ) as a literal""",\n'
            "    'a',\n"
            "    'b',\n"
            ")\n")
    assert _spans(text) == [(1, 5)]


def test_span_continued_single_quote_string_keeps_span_long():
    # A backslash-continued single-quote string legally survives the newline;
    # force-closing it at EOL made brackets inside the real string count as
    # code (fail short).
    text = ("X_FLAGS = ('ab\\\n"
            ") still string',\n"
            "    'c',\n"
            ")\n")
    assert _spans(text) == [(1, 4)]


def test_span_bracketless_continuation_chain_spans_the_statement():
    text = ("Y_FLAGS = PART_0 \\\n"
            "    + PART_1 \\\n"
            "    + PART_2\n"
            "Z = 1\n")
    assert _spans(text) == [(1, 3)]


def test_a5_plus_plus_body_line_does_not_wipe_spans():
    # An ADDED body line starting `++ ` yields a raw `+++ …` line; inside a
    # hunk it is BODY, not a file header — misreading it wiped the span index
    # for every later hunk of the file.
    d = ("diff --git a/mod.py b/mod.py\n"
         "--- a/mod.py\n+++ b/mod.py\n"
         "@@ -3,3 +3,4 @@\n"
         " docs\n"
         "+++ bump\n"
         " more\n"
         "@@ -100,3 +101,3 @@\n"
         "     '--opt98',\n"
         "-    '--opt99',\n"
         "+    '--opt99', '--available-tools', 'write',\n"
         "     '--opt100',\n")
    reason = diff_tripwire(d, marker_spans={"mod.py": ((7, 209),)})
    assert reason is not None
    assert "edits a *_FLAGS / ISOLATION constant" in reason


def test_spans_for_diff_handles_space_path_tab(tmp_path):
    # Git appends a TAB to the `+++ b/<path>` header when the path contains a
    # space; the collector and the walker must both normalize it or the span
    # index silently skips the file.
    (tmp_path / "my tool.py").write_text("SPACE_FLAGS = (\n    'a',\n)\n")
    d = ("diff --git a/my tool.py b/my tool.py\n"
         "--- a/my tool.py\t\n+++ b/my tool.py\t\n@@ -1 +1 @@\n-a\n+b\n")
    spans = fix_apply._tripwire_spans_for_diff(d, str(tmp_path))
    assert spans == {"my tool.py": ((1, 3),)}
    # And the walker resolves the same key: an edit inside the span trips.
    d2 = ("diff --git a/my tool.py b/my tool.py\n"
          "--- a/my tool.py\t\n+++ b/my tool.py\t\n@@ -2,1 +2,1 @@\n"
          "-    'a',\n+    'a', '--available-tools',\n")
    assert diff_tripwire(d2, marker_spans=spans) is not None


def test_attempt_diff_preexisting_unchanged_untracked_filtered(repo_sf):
    repo, git = repo_sf
    (repo / "junk.log").write_text("j\n" * 1000)
    snap = fix_apply.snapshot_worktree(str(repo))
    assert snap is not None and "junk.log" in snap[1]
    (repo / "tracked.py").write_text("X2 = 1\n")
    import unittest.mock as um
    with um.patch.object(fix_apply, "_SCAN_CHUNK_MAX_BYTES", 500):
        diff, truncated = fix_apply._attempt_diff(str(repo), snap[0] or "HEAD",
                                                  snap[1])
    assert not truncated
    assert "junk.log" not in diff
    assert "X2 = 1" in diff


def test_attempt_diff_preexisting_untracked_modified_still_rides(repo_sf):
    repo, git = repo_sf
    (repo / "notes.py").write_text("A = 1\n")
    snap = fix_apply.snapshot_worktree(str(repo))
    (repo / "notes.py").write_text("EVIL_FLAGS = ('--x',)\n")
    diff, truncated = fix_apply._attempt_diff(str(repo), snap[0] or "HEAD",
                                              snap[1])
    assert not truncated
    assert "EVIL_FLAGS" in diff


def test_attempt_diff_ls_files_failure_fails_closed(repo_sf, monkeypatch):
    # If the untracked files cannot even be enumerated, a fixer-created
    # dangerous new file could sit unscanned — that must surface as
    # scan_truncated (forced verify), never a silent fail-open.
    repo, git = repo_sf
    (repo / "tracked.py").write_text("X2 = 1\n")
    real_git = fix_apply._git

    def fake_git(cwd, *args, **kw):
        if "ls-files" in args:
            raise subprocess.TimeoutExpired(["git", *args], 1)
        return real_git(cwd, *args, **kw)

    monkeypatch.setattr(fix_apply, "_git", fake_git)
    diff, truncated = fix_apply._attempt_diff(str(repo), "HEAD")
    assert truncated
    assert "X2 = 1" in diff   # the tracked diff is preserved


@pytest.fixture
def repo_sf(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True,
                       capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "tracked.py").write_text("original\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    return tmp_path, git
