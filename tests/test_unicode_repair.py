"""Unit tests for unicode_repair — the deterministic dangerous-Unicode normalizer.
Codepoints are built via chr() so this file stays pure-ASCII + verifiable."""
from buddhi_review import unicode_repair as U


def _norm(s):
    return U.normalize_text(s)[0]


def test_smart_quotes_to_ascii():
    assert _norm("x = " + chr(0x201C) + "hi" + chr(0x201D)) == 'x = "hi"'
    assert _norm("x = " + chr(0x2018) + "hi" + chr(0x2019)) == "x = 'hi'"
    assert _norm(chr(0x201E) + chr(0x201F)) == '""'   # low/high double
    assert _norm(chr(0x201A) + chr(0x201B)) == "''"   # low/high single


def test_classes_reported_in_order():
    _, reps = U.normalize_text(chr(0x2019) + chr(0xA0) + chr(0x200B) + chr(0x2028))
    assert [r.cls for r in reps] == ["smart_single", "nbsp", "zero_width", "line_sep"]


def test_nbsp_and_exotic_spaces_to_space():
    for cp in (0xA0, 0x2003, 0x202F, 0x205F, 0x3000):
        assert _norm("a" + chr(cp) + "b") == "a b"


def test_zero_width_deleted():
    for cp in (0x200B, 0x200C, 0x200D, 0x2060):
        assert _norm("a" + chr(cp) + "b") == "ab"


def test_leading_bom_deleted_and_midfile_too():
    assert _norm(chr(0xFEFF) + "x") == "x"
    out, reps = U.normalize_text(chr(0xFEFF) + "a" + chr(0xFEFF) + "b")
    assert out == "ab"
    assert [r.cls for r in reps] == ["bom", "zero_width"]   # leading=bom, mid=zero_width


def test_line_and_paragraph_separator_to_newline():
    assert _norm("a" + chr(0x2028) + "b") == "a\nb"
    assert _norm("a" + chr(0x2029) + "b") == "a\nb"


def test_unsafe_classes_left_untouched():
    # em-dash, en-dash, minus, ellipsis, Cyrillic homoglyph, fullwidth, bidi override:
    # all NOT syntax-breaking-by-construction / require intent — never auto-rewritten.
    for cp in (0x2014, 0x2013, 0x2212, 0x2026, 0x0430, 0xFF08, 0x202E):
        s = "a" + chr(cp) + "b"
        out, reps = U.normalize_text(s)
        assert out == s and reps == [], "codepoint U+%04X must be left untouched" % cp


def test_idempotent():
    s = "x=" + chr(0x2019) + "a" + chr(0x2019) + chr(0xA0) + chr(0x200B)
    once = _norm(s)
    assert _norm(once) == once


def test_scan_flags_bidi_without_rewriting():
    s = "a" + chr(0x202E) + "b"
    reps = U.scan_text(s)
    assert len(reps) == 1 and reps[0].cls == "bidi" and reps[0].repl is None
    assert U.normalize_text(s)[0] == s   # scan/normalize never strip bidi


def test_has_dangerous_unicode():
    assert U.has_dangerous_unicode("x" + chr(0x2019))
    assert not U.has_dangerous_unicode("plain ascii")
    assert not U.has_dangerous_unicode("em " + chr(0x2014) + " dash")  # em-dash is not dangerous


def test_normalize_code_file_writes_only_when_changed(tmp_path):
    dirty = tmp_path / "f.py"
    dirty.write_text("x = " + chr(0x2019) + "a" + chr(0x2019) + "\n", encoding="utf-8")
    reps = U.normalize_code_file(str(dirty))
    assert reps and dirty.read_text(encoding="utf-8") == "x = 'a'\n"

    clean = tmp_path / "c.py"
    clean.write_text("x = 'a'\n", encoding="utf-8")
    assert U.normalize_code_file(str(clean)) == []        # no change → no write
    assert not any(f.name.startswith(".urepair-") for f in tmp_path.iterdir())  # no temp residue


def test_normalize_code_file_missing_path_is_noop():
    assert U.normalize_code_file("/no/such/file.py") == []


def test_normalize_code_file_preserves_crlf(tmp_path):
    p = tmp_path / "f.py"
    p.write_bytes(("a =" + chr(0xA0) + "1\r\nb = 2\r\n").encode("utf-8"))
    U.normalize_code_file(str(p))
    raw = p.read_bytes()
    assert b"\r\n" in raw and b"\xc2\xa0" not in raw   # CRLF kept, NBSP fixed


def test_normalize_code_file_skips_symlink(tmp_path):
    target = tmp_path / "real.py"
    target.write_text("x = " + chr(0x2019) + "a" + chr(0x2019) + "\n", encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(target)
    assert U.normalize_code_file(str(link)) == []     # symlink left alone
    assert link.is_symlink()                          # not replaced by a regular file


def test_normalize_only_given_lines(tmp_path):
    out, reps = U._normalize_selected_lines(
        "a" + chr(0x2019) + "\nb" + chr(0x2019) + "\n", {2})
    assert out == "a" + chr(0x2019) + "\nb'\n"        # line 1 untouched, line 2 fixed
    assert len(reps) == 1


# ── position-level edits (selected_line_edits / apply_edits) ─────────────────

def test_selected_line_edits_scoped_to_lines():
    text = "a" + chr(0x2019) + "\nb" + chr(0xA0) + "c\n"   # line1 U+2019, line2 NBSP
    assert len(U.selected_line_edits(text, {2})) == 1     # only line 2's NBSP
    assert U.apply_edits(text, U.selected_line_edits(text, {2})) == \
        "a" + chr(0x2019) + "\nb c\n"                     # line 1 untouched
    both = U.selected_line_edits(text, {1, 2})
    assert len(both) == 2
    assert U.apply_edits(text, both) == "a'\nb c\n"
    # dropping the NBSP edit leaves the NBSP (the subset the caller chose)
    nbsp = [e for e in both if e[1] == " "][0]
    assert U.apply_edits(text, [e for e in both if e != nbsp]) == "a'\nb" + chr(0xA0) + "c\n"


def test_apply_edits_deletes_and_is_order_safe():
    text = "a" + chr(0x200B) + "b" + chr(0x2019) + "c"    # ZWSP (delete), U+2019 (→ ')
    assert U.apply_edits(text, U.selected_line_edits(text, {1})) == "ab'c"


def test_apply_edits_empty_and_out_of_range():
    assert U.apply_edits("abc", []) == "abc"
    assert U.apply_edits("abc", [(99, "X")]) == "abc"     # out-of-range index ignored


def test_overwrite_atomic_preserves_mode(tmp_path):
    import os
    p = tmp_path / "f.py"
    p.write_text("old\n", encoding="utf-8")
    os.chmod(p, 0o640)
    assert U.overwrite_atomic(str(p), "new\n") is True
    assert p.read_text(encoding="utf-8") == "new\n"
    assert (os.stat(p).st_mode & 0o777) == 0o640          # mode preserved
    assert not any(f.name.startswith(".urepair-") for f in tmp_path.iterdir())  # no residue


def test_overwrite_atomic_skips_symlink(tmp_path):
    real = tmp_path / "real.py"
    real.write_text("x\n", encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(real)
    assert U.overwrite_atomic(str(link), "y\n") is False
    assert real.read_text(encoding="utf-8") == "x\n"      # target untouched
    assert link.is_symlink()
