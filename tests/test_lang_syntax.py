"""Unit tests for lang_syntax — the language-keyed syntax checker."""
import pytest

from buddhi_review import lang_syntax as L


def test_python_clean_and_broken():
    assert L.check_python("x = 1\n", "ok.py").ok
    bad = L.check_python("def f(:\n", "bad.py")
    assert not bad.ok and not bad.skipped and bad.path == "bad.py" and bad.detail


def test_json_clean_and_broken():
    assert L.check_json('{"a": 1}', "ok.json").ok
    assert not L.check_json("{a: 1}", "bad.json").ok


def test_yaml_if_available():
    r = L.check_yaml("a: 1\nb: 2\n", "ok.yml")
    if r.skipped:
        pytest.skip("PyYAML not installed")
    assert r.ok
    assert not L.check_yaml("a: [1, 2\n", "bad.yml").ok


def test_toml_if_available():
    r = L.check_toml("a = 1\n", "ok.toml")
    if r.skipped:
        pytest.skip("tomllib not available")
    assert r.ok
    assert not L.check_toml("a = \n", "bad.toml").ok


def test_javascript_node_absent_is_skipped(monkeypatch):
    monkeypatch.setattr(L, "_find_node", lambda: None)
    r = L.check_javascript("var x = ;", "x.js")
    assert r.skipped and r.ok            # skipped is NOT a verdict (never a false fail)
    assert L.first_error([r]) is None


@pytest.mark.skipif(not L.available_languages()["javascript"], reason="node absent")
def test_javascript_present():
    assert L.check_javascript("var x = 1;", "ok.js").ok
    assert not L.check_javascript("var x = ;", "bad.js").ok


def test_node_finder_honours_env(monkeypatch, tmp_path):
    fake = tmp_path / "node"
    fake.write_text("#!/bin/sh\nexit 0\n")
    import os
    os.chmod(fake, 0o755)
    monkeypatch.setenv("NODE_BIN", str(fake))
    assert L._find_node() == str(fake)
    monkeypatch.setenv("NODE_BIN", str(tmp_path / "missing"))
    # a non-existent NODE_BIN falls through to PATH discovery (never raises)
    assert L._find_node() != str(tmp_path / "missing")


def test_language_routing(tmp_path):
    assert L.language_for("a.py") == "python"
    assert L.language_for("a.mjs") == "javascript"
    assert L.language_for("a.json") == "json"
    assert L.language_for("a.yaml") == "yaml"
    assert L.language_for("a.sh") == "shell"
    assert L.language_for("a.toml") == "toml"
    assert L.language_for("a.txt") is None        # unrecognized → never checked
    sh = tmp_path / "runme"                        # extensionless shell shebang
    sh.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    assert L.language_for(str(sh)) == "shell"


def test_shebang_interpreter_allowlist(tmp_path):
    def mk(name, shebang):
        p = tmp_path / name
        p.write_text(shebang + "\necho hi\n", encoding="utf-8")
        return str(p)
    assert L.language_for(mk("a", "#!/bin/sh")) == "shell"
    assert L.language_for(mk("b", "#!/usr/bin/env bash")) == "shell"
    assert L.language_for(mk("c", "#!/bin/zsh")) == "shell"
    # an interpreter merely CONTAINING "sh" is NOT routed to bash -n (no false fail)
    assert L.language_for(mk("d", "#!/usr/bin/env fish")) is None
    assert L.language_for(mk("e", "#!/usr/bin/env python3")) is None
    assert L.language_for(mk("f", "no shebang here")) is None


def test_check_file_python_with_and_without_embedded_js(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("X = 1\n", encoding="utf-8")
    assert [r.lang for r in L.check_file(str(p), "m.py")] == ["python"]  # zero JS → only python row

    p2 = tmp_path / "n.py"
    p2.write_text("A_JS = 'var x = 1;'\n", encoding="utf-8")
    rows = L.check_file(str(p2), "n.py")
    assert {r.lang for r in rows} == {"python", "javascript"}
    assert any(r.unit == "A_JS" for r in rows)


def test_check_file_python_syntax_error_yields_python_fail():
    rows = L.check_file("/x.py", "x.py", source="def f(:\n_JS_LEAK = 'x'\n")
    assert L.first_error(rows) is not None and L.first_error(rows).lang == "python"


def test_embedded_js_fold_and_runtime_skip():
    assert L.embedded_js_constants("/x.py", source="A_JS = 'a' + 'b'\n") == {"A_JS": "ab"}
    assert L.embedded_js_constants("/x.py", source="B_JS = 'x' + dumps(y) + 'z'\n") == {}
    # nested (in a function) is still discovered (ast.walk scope)
    nested = L.embedded_js_constants("/x.py", source="def f():\n    C_JS = 'z'\n    return C_JS\n")
    assert nested == {"C_JS": "z"}


def test_missing_file_is_skipped_not_error():
    rows = L.check_file("/no/such/file.py", "file.py")
    assert len(rows) == 1 and rows[0].skipped and rows[0].ok
    assert L.first_error(rows) is None


def test_first_error_ignores_skips():
    skipped = L.SyntaxResult("a.js", "javascript", None, True, True, "")
    real = L.SyntaxResult("b.py", "python", None, False, False, "1:1 boom")
    assert L.first_error([skipped]) is None
    assert L.first_error([skipped, real]) is real
    assert L.first_error([real, skipped]) is real


def test_node_check_never_raises_on_surrogate_text():
    # check_file reads with surrogateescape; the temp write must too, so a lone
    # surrogate degrades to a result, never a propagating UnicodeEncodeError.
    r = L.check_javascript("var x = 1;" + chr(0xDC80), "x.js")
    assert isinstance(r, L.SyntaxResult)   # returned, not raised


def test_embedded_js_annotated_assignment_discovered():
    assert L.embedded_js_constants("/x.py", source="A_JS: str = 'var x=1;'\n") == {"A_JS": "var x=1;"}
