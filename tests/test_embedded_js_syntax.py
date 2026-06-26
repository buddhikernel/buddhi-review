"""Guard for the embedded-``*_JS`` extractor + node-check path.

A Python file may carry browser JavaScript as a ``*_JS = "..."`` string constant —
valid Python, but the JS inside can be broken (a curly quote used as a string
delimiter is the classic one) and only ``node --check`` catches it, so every Python
test would pass while the shipped JS is a SyntaxError. ``lang_syntax`` extracts each
such constant and node-checks it. These tests pin the extractor (the single source of
truth) and the node-check path. The JS-checker tests are skipped only where ``node``
is unavailable; the extractor tests are pure AST and always run.
"""
from buddhi_review import lang_syntax

_NO_NODE = not lang_syntax.available_languages()["javascript"]


def test_extractor_discovers_module_nested_and_annotated():
    src = (
        "TOP_JS = 'var a = 1;'\n"
        "def build():\n"
        "    NESTED_JS = 'var b = 2;'\n"
        "    return NESTED_JS\n"
        "ANNOTATED_JS: str = 'var c = 3;'\n"
    )
    found = lang_syntax.embedded_js_constants("/x.py", source=src)
    assert found == {
        "TOP_JS": "var a = 1;",
        "NESTED_JS": "var b = 2;",
        "ANNOTATED_JS": "var c = 3;",
    }


def test_extractor_folds_string_concat_but_skips_runtime_concat():
    # A `+`-chain of string literals folds to one static string …
    assert lang_syntax.embedded_js_constants("/x.py", source="A_JS = 'a' + 'b' + 'c'\n") == {"A_JS": "abc"}
    # … but a chain with ANY non-literal operand (a json.dumps call, a Name, an
    # f-string) is NOT statically foldable, so it yields no entry (the source dict is
    # already validated by compile(), and json.dumps cannot emit a JS syntax error).
    assert lang_syntax.embedded_js_constants("/x.py", source="B_JS = '<' + dumps(x) + '>'\n") == {}
    assert lang_syntax.embedded_js_constants("/x.py", source="C_JS = PREFIX + 'tail'\n") == {}
    assert lang_syntax.embedded_js_constants("/x.py", source="D_JS = f'{x}'\n") == {}


def test_extractor_only_matches_js_suffix():
    src = "REGULAR = 'not js'\nSOME_JSON = '{}'\nX_JS = 'var y;'\n"
    assert set(lang_syntax.embedded_js_constants("/x.py", source=src)) == {"X_JS"}


def test_check_file_emits_one_js_row_per_constant():
    src = "import os\nP_JS = 'var a = 1;'\nQ_JS = 'var b = 2;'\n"
    rows = lang_syntax.check_file("/x.py", "x.py", source=src)
    assert rows[0].lang == "python"
    js_units = {r.unit for r in rows if r.lang == "javascript"}
    assert js_units == {"P_JS", "Q_JS"}


def test_clean_embedded_js_has_no_error():
    src = "OK_JS = 'function f(a){ return a + 1; }'\n"
    err = lang_syntax.first_error(lang_syntax.check_file("/x.py", "x.py", source=src))
    if _NO_NODE:
        assert err is None      # node absent → the JS row skips, never a false pass
    else:
        assert err is None      # valid JS → clean


def test_smart_quote_delimiter_in_embedded_js():
    """A curly quote written as a JS string DELIMITER inside a Python ``*_JS``
    constant: valid Python, broken JS. The smart quotes are built via chr() (never a
    ``\\u`` escape) so this file stays pure-ASCII. The JS unit is always extracted;
    node (when present) flags it; without node the row is skipped — never a false
    pass."""
    lq, rq = chr(0x2018), chr(0x2019)   # ‘ ’ as DELIMITERS — invalid JS
    src = "_X_JS = r'''a.cls = %sfm-note warn%s;'''\n" % (lq, rq)
    rows = lang_syntax.check_file("/x.py", "x.py", source=src)
    js_rows = [r for r in rows if r.lang == "javascript"]
    assert js_rows and js_rows[0].unit == "_X_JS"
    if _NO_NODE:
        assert js_rows[0].skipped is True       # no verdict, not a false pass
    else:
        assert js_rows[0].skipped is False
        assert js_rows[0].ok is False           # node catches the smart-quote delimiter


def test_broken_python_short_circuits_the_js_extraction():
    # An unparseable Python file yields the python failure; the extractor returns {}
    # on a parse error, so there is no spurious JS row.
    rows = lang_syntax.check_file("/x.py", "x.py", source="def f(:\nLEAK_JS = 'var x;'\n")
    err = lang_syntax.first_error(rows)
    assert err is not None and err.lang == "python"
