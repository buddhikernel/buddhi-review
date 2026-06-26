"""The deterministic pre-commit dangerous-Unicode cleanup in fix_apply.

The diff parser and the cleanup run FOR REAL against tmp files (no model, no
network); the apply_fix integration runs against a real temp git repo, so the
SOURCE-only / surgical / per-file-reverify / byte-exact-rollback contract is
exercised end-to-end.
"""
import os
import subprocess

import pytest

from buddhi_review import fix_apply
from buddhi_review.fix_apply import (
    _is_test_path,
    _safe_repo_path,
    added_lines_by_file,
    apply_fix,
    deterministic_unicode_cleanup,
)

NBSP = chr(0xA0)
# NBSP-as-indentation is a real Python SyntaxError that normalization cleanly fixes.
_NBSP_SRC = "def f():\n" + NBSP + "   return 1\n"


# ── added_lines_by_file: unified-diff parsing ────────────────────────────────

def test_added_lines_single_file():
    diff = (
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -1,3 +1,4 @@\n"
        " ctx0\n"
        "-removed\n"
        "+added_a\n"
        "+added_b\n"
        " ctx1\n"
    )
    # new-file numbering: line1 ctx, line2 added_a, line3 added_b, line4 ctx
    assert added_lines_by_file(diff) == {"mod.py": {2, 3}}


def test_added_lines_multi_file_and_deletion():
    # realistic git output: each file section opens with a `diff --git` boundary
    diff = (
        "diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1 +1,2 @@\n ctx\n+new_a\n"
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\nindex 3..0\n"
        "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"
        "diff --git a/b.py b/b.py\nindex 4..5 100644\n--- a/b.py\n+++ b/b.py\n"
        "@@ -1 +1 @@\n-x\n+new_b\n"
    )
    assert added_lines_by_file(diff) == {"a.py": {2}, "b.py": {1}}   # deletion contributes nothing


def test_added_lines_untracked_all_added():
    # the shape git diff --no-index /dev/null <new> produces for an untracked file
    diff = ("diff --git a/new.py b/new.py\nnew file mode 100644\nindex 0..1\n"
            "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+line1\n+line2\n")
    assert added_lines_by_file(diff) == {"new.py": {1, 2}}


def test_added_lines_empty_and_metadata_only():
    assert added_lines_by_file("") == {}
    assert added_lines_by_file("diff --git a/x b/x\nindex 111..222 100644\n") == {}


def test_added_lines_counts_diff_like_content_as_content():
    # A fixer edits a file that embeds patch text. The added lines' CONTENT begins with
    # ++ / @@ — git emits them as +++ / +@@. They must be counted as content for the
    # current file, NEVER mis-read as a new-file header (the diff-of-a-diff trap).
    diff = (
        "diff --git a/fixture.py b/fixture.py\nindex 1..2 100644\n"
        "--- a/fixture.py\n+++ b/fixture.py\n@@ -1,1 +1,4 @@\n"
        " PATCH = r'''\n"
        "+++ b/zzz.py\n"          # content '++ b/zzz.py' — old code switched to a phantom file
        "+@@ -9 +9 @@\n"          # content '+@@ -9 +9 @@'
        "+normal added line\n"
    )
    assert added_lines_by_file(diff) == {"fixture.py": {2, 3, 4}}


def test_added_lines_space_and_quoted_paths():
    # git appends a TAB after a space-containing path; quotepath=true wraps a path in
    # quotes. Both must map back to the clean on-disk path.
    space = ("diff --git a/my file.py b/my file.py\nindex 1..2 100644\n"
             "--- a/my file.py\t\n+++ b/my file.py\t\n@@ -1 +1,2 @@\n a\n+b\n")
    assert added_lines_by_file(space) == {"my file.py": {2}}
    quoted = ('diff --git "a/x y.py" "b/x y.py"\nindex 1..2 100644\n'
              '--- "a/x y.py"\n+++ "b/x y.py"\n@@ -1 +1,2 @@\n a\n+b\n')
    assert added_lines_by_file(quoted) == {"x y.py": {2}}


# ── path helpers ─────────────────────────────────────────────────────────────

def test_is_test_path():
    assert _is_test_path("tests/test_x.py")
    assert _is_test_path("pkg/conftest.py")
    assert _is_test_path("a/b_test.py")
    assert _is_test_path("a/tests/util.py")
    assert not _is_test_path("pkg/module.py")
    assert not _is_test_path("")


def test_safe_repo_path_confines_to_worktree(tmp_path):
    inside = _safe_repo_path(str(tmp_path), "sub/mod.py")
    assert inside and inside.startswith(os.path.realpath(str(tmp_path)) + os.sep)
    assert _safe_repo_path(str(tmp_path), "../escape.py") is None
    assert _safe_repo_path(str(tmp_path), "") is None


# ── deterministic_unicode_cleanup: the contract ──────────────────────────────

def test_cleanup_fixes_broken_source_file(tmp_path):
    p = tmp_path / "mod.py"
    p.write_text(_NBSP_SRC, encoding="utf-8")
    files, chars = deterministic_unicode_cleanup(str(tmp_path), {"mod.py": {1, 2}})
    assert (files, chars) == (1, 1)
    assert p.read_text(encoding="utf-8") == "def f():\n    return 1\n"


def test_cleanup_clean_file_is_not_a_candidate(tmp_path):
    # valid Python with a legit curly apostrophe in a comment → not broken → untouched
    p = tmp_path / "mod.py"
    p.write_text("x = 1  # it" + chr(0x2019) + "s fine\n", encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    assert deterministic_unicode_cleanup(str(tmp_path), {"mod.py": {1}}) == (0, 0)
    assert p.read_text(encoding="utf-8") == before


def test_cleanup_excludes_test_files(tmp_path):
    (tmp_path / "tests").mkdir()
    p = tmp_path / "tests" / "test_x.py"
    p.write_text(_NBSP_SRC, encoding="utf-8")
    assert deterministic_unicode_cleanup(str(tmp_path), {"tests/test_x.py": {1, 2}}) == (0, 0)
    assert p.read_text(encoding="utf-8") == _NBSP_SRC   # the test fixture is untouched


def test_cleanup_rolls_back_when_normalize_does_not_fix(tmp_path):
    # A smart-quote DELIMITER + an in-string apostrophe: straightening the delimiter
    # also straightens the apostrophe → the string is STILL broken → rolled back.
    lq, rq, apo = chr(0x2018), chr(0x2019), chr(0x2019)
    src = "X = " + lq + "it" + apo + "s" + rq + "\n"
    p = tmp_path / "m.py"
    p.write_text(src, encoding="utf-8")
    assert deterministic_unicode_cleanup(str(tmp_path), {"m.py": {1}}) == (0, 0)
    assert p.read_text(encoding="utf-8") == src   # restored byte-for-byte


def test_cleanup_surgical_to_changed_lines(tmp_path):
    # line 1 (UNCHANGED): a legit display string with a curly apostrophe.
    # line 2 (changed): an NBSP-broken assignment. Only line 2 is normalized, so the
    # legit typographic glyph on line 1 survives.
    glyph = chr(0x2019)
    p = tmp_path / "mod.py"
    p.write_text("MSG = 'all" + glyph + "good'\n" + "x =" + NBSP + "1\n", encoding="utf-8")
    files, chars = deterministic_unicode_cleanup(str(tmp_path), {"mod.py": {2}})
    assert files == 1
    txt = p.read_text(encoding="utf-8")
    assert glyph in txt and NBSP not in txt


def test_cleanup_preserves_legit_glyph_on_a_changed_line(tmp_path):
    # line 1 (a CHANGED line) carries a LEGITIMATE curly apostrophe inside a valid
    # string; line 3 (also changed) has an NBSP-broken indentation. Only the NBSP is
    # load-bearing for the fix, so the legitimate apostrophe must survive — the cleanup
    # never rewrites valid prose even when it shares a changed line set with a break.
    import ast
    glyph = chr(0x2019)
    p = tmp_path / "mod.py"
    p.write_text('GREETING = "the user' + glyph + 's name"\n'
                 "def f():\n" + NBSP + "   return 1\n", encoding="utf-8")
    files, chars = deterministic_unicode_cleanup(str(tmp_path), {"mod.py": {1, 2, 3}})
    assert (files, chars) == (1, 1)        # ONLY the NBSP substitution was kept
    txt = p.read_text(encoding="utf-8")
    assert glyph in txt                    # the legitimate apostrophe SURVIVED
    assert NBSP not in txt                 # the break was fixed
    ast.parse(txt)                         # and the file now parses


def test_cleanup_minimizes_same_line(tmp_path):
    # a legitimate apostrophe (in a comment) and the NBSP break sit on the SAME changed
    # line — codepoint-level minimization keeps only the NBSP; the comment glyph stays.
    glyph = chr(0x2019)
    p = tmp_path / "mod.py"
    p.write_text("if x:" + NBSP + "pass  # it" + glyph + "s fine\n", encoding="utf-8")
    files, chars = deterministic_unicode_cleanup(str(tmp_path), {"mod.py": {1}})
    assert (files, chars) == (1, 1)
    txt = p.read_text(encoding="utf-8")
    assert glyph in txt and NBSP not in txt


def test_cleanup_per_file_independence(tmp_path):
    # file A is Unicode-broken (fixable); file B is broken for a NON-Unicode reason.
    # A is cleaned and kept; B is left exactly as-is (its break flows to the gate).
    a = tmp_path / "a.py"
    a.write_text(_NBSP_SRC, encoding="utf-8")
    b = tmp_path / "b.py"
    b.write_text("def g(:\n    pass\n", encoding="utf-8")
    files, chars = deterministic_unicode_cleanup(str(tmp_path), {"a.py": {1, 2}, "b.py": {1}})
    assert files == 1   # only A
    assert a.read_text(encoding="utf-8") == "def f():\n    return 1\n"
    assert b.read_text(encoding="utf-8") == "def g(:\n    pass\n"   # untouched


def test_cleanup_skips_symlink(tmp_path):
    real = tmp_path / "real.py"
    real.write_text(_NBSP_SRC, encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(real)
    assert deterministic_unicode_cleanup(str(tmp_path), {"link.py": {1, 2}}) == (0, 0)
    assert link.is_symlink()                                   # link preserved
    assert real.read_text(encoding="utf-8") == _NBSP_SRC       # target NOT rewritten via the link


def test_cleanup_preserves_crlf(tmp_path):
    p = tmp_path / "mod.py"
    p.write_bytes(("x =" + NBSP + "1\r\ny = 2\r\n").encode("utf-8"))
    files, chars = deterministic_unicode_cleanup(str(tmp_path), {"mod.py": {1}})
    assert files == 1
    raw = p.read_bytes()
    assert b"\r\n" in raw and b"\xc2\xa0" not in raw           # CRLF kept, NBSP fixed


def test_cleanup_missing_file_is_noop(tmp_path):
    assert deterministic_unicode_cleanup(str(tmp_path), {"gone.py": {1}}) == (0, 0)


# ── apply_fix integration (real git repo) ────────────────────────────────────

@pytest.fixture
def repo(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "tracked.py").write_text("original = 1\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    return tmp_path


def test_apply_fix_cleans_dangerous_unicode_before_commit(repo):
    def fixer(prompt, *, model, effort, timeout, cwd):
        (repo / "mod.py").write_text(_NBSP_SRC, encoding="utf-8")
        return 0, "done"
    out = apply_fix("claim", cwd=str(repo), runner=fixer, retries=0)
    assert out.status == "applied"
    assert (repo / "mod.py").read_text(encoding="utf-8") == "def f():\n    return 1\n"
    assert NBSP not in out.diff   # the recomputed diff reflects the cleaned file


def test_apply_fix_cleans_under_noprefix_git_config(repo):
    # a user with diff.noprefix / diff.mnemonicPrefix set in their git config must still
    # get the cleanup — _attempt_diff forces the canonical a/ b/ header form so the diff
    # parser can map the changed lines regardless of the operator's config.
    for key in ("diff.noprefix", "diff.mnemonicPrefix"):
        subprocess.run(["git", "config", key, "true"], cwd=repo, check=True, capture_output=True)

    def fixer(prompt, *, model, effort, timeout, cwd):
        (repo / "mod.py").write_text(_NBSP_SRC, encoding="utf-8")
        return 0, "done"
    out = apply_fix("claim", cwd=str(repo), runner=fixer, retries=0)
    assert out.status == "applied"
    assert (repo / "mod.py").read_text(encoding="utf-8") == "def f():\n    return 1\n"


def test_apply_fix_unicode_cleanup_kill_switch(repo, monkeypatch):
    monkeypatch.setenv("BUDDHI_DETERMINISTIC_UNICODE_REMEDY", "0")

    def fixer(prompt, *, model, effort, timeout, cwd):
        (repo / "mod.py").write_text(_NBSP_SRC, encoding="utf-8")
        return 0, "done"
    out = apply_fix("claim", cwd=str(repo), runner=fixer, retries=0)
    assert out.status == "applied"
    assert NBSP in (repo / "mod.py").read_text(encoding="utf-8")   # cleanup disabled → left broken


def test_apply_fix_leaves_clean_unicode_untouched(repo):
    # A legitimate em-dash in a comment is NOT dangerous → never rewritten, and the
    # file is valid so it is not even a candidate.
    body = "x = 1  # an em " + chr(0x2014) + " dash, intentional\n"

    def fixer(prompt, *, model, effort, timeout, cwd):
        (repo / "mod.py").write_text(body, encoding="utf-8")
        return 0, "done"
    out = apply_fix("claim", cwd=str(repo), runner=fixer, retries=0)
    assert out.status == "applied"
    assert (repo / "mod.py").read_text(encoding="utf-8") == body
