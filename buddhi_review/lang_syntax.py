"""Language-keyed SYNTAX checking.

Given a file, run the cheapest correct syntax check for its language: Python via
compile(), JavaScript via ``node --check``, JSON via json, YAML via yaml.safe_load,
shell via ``bash -n``, TOML via tomllib. PLUS the special case that motivated this:
a Python file may embed browser JavaScript in a ``*_JS = "..."`` module constant --
valid Python, but the JS can be broken (e.g. a curly quote used as a string
delimiter), and only ``node --check`` catches it. So check_file() on a ``.py`` emits
one Python result AND one JS result per statically-extractable ``*_JS`` constant.

Design rules (load-bearing):
- NEVER raises. A checker whose external TOOL is absent returns ``skipped=True``
  (NOT a verdict); a missing/unreadable file is ``skipped=True`` too. first_error()
  ignores skips, so a node-less host degrades to "Python-only + the test suite",
  never a false fail.
- The embedded-JS extractor is DISCOVERY-based (an ast.walk over every ``*_JS`` string
  assignment, module-level OR nested), folds ``+``-chains of string literals, and
  yields NOTHING for a constant whose value isn't a static string (e.g.
  ``"..." + json.dumps(..)``; the fold returns None on ANY non-literal operand --
  Name, Call, Attribute, f-string, ...). It is the single source of truth: the
  embedded-JS guard test imports it.
"""
from __future__ import annotations

import ast
import glob
import json
import os
import shutil
import subprocess
import tempfile
from typing import Dict, List, NamedTuple, Optional

_SUBPROCESS_TIMEOUT = 30
EMBEDDED_JS_SUFFIX = "_JS"
# Avoids redundant subprocess spawns when the same JS text is checked many times (e.g.
# the deterministic_unicode_cleanup minimization loop calling _file_text_ok up to 200×
# per file while only the Python portions change, leaving _JS constants identical).
_NODE_CHECK_CACHE: Dict = {}


def _node_version_key(path: str):
    """Sort key for an nvm node path so the newest installed version wins."""
    m = path.split(os.sep)
    for part in m:
        if part.startswith("v") and part[1:].replace(".", "").isdigit():
            try:
                return tuple(int(x) for x in part[1:].split("."))
            except ValueError:
                break
    return (0,)


def _find_node() -> Optional[str]:
    """Resolve a Node.js executable for ``node --check``; None when absent (the JS
    checker then SKIPS -- never a false fail). Honours ``NODE_BIN``, then PATH, then a
    few common install dirs, then the newest nvm-installed version."""
    env = os.environ.get("NODE_BIN")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    found = shutil.which("node")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/node", "/usr/local/bin/node",
                 os.path.join(os.path.expanduser("~"), ".local", "bin", "node")):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    nvm = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")),
                 key=_node_version_key, reverse=True)
    for cand in nvm:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


class SyntaxResult(NamedTuple):
    path: str            # file path, or "<path>::<CONST>" for an embedded JS unit
    lang: str            # python|javascript|json|yaml|shell|toml|unknown
    unit: Optional[str]  # None for a whole file; the constant name for an embedded JS unit
    ok: bool             # True = parsed clean (meaningless when skipped)
    skipped: bool        # True = checker tool / file absent -- NOT a verdict
    detail: str          # "line:col message" on failure; "" otherwise


def _ok(path, lang, unit=None):
    return SyntaxResult(path, lang, unit, True, False, "")


def _fail(path, lang, detail, unit=None):
    return SyntaxResult(path, lang, unit, False, False, str(detail)[:300])


def _skip(path, lang, unit=None):
    return SyntaxResult(path, lang, unit, True, True, "")


# ── embedded-JS extraction (single source of truth) ──────────────────────────
def _static_str(node) -> Optional[str]:
    """The decoded string of an AST node when it is a string literal OR a ``+``-chain
    of string literals; None if any operand is non-literal (Name/Call/Attribute/...)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_str(node.left)
        if left is None:
            return None
        right = _static_str(node.right)
        if right is None:
            return None
        return left + right
    return None


def embedded_js_constants(py_path: str, *, source: Optional[str] = None) -> Dict[str, str]:
    """Every ``*_JS`` string constant in a Python file as ``{name: js_text}``. AST-walk
    (catches module-level and nested assignments); folds ``+``-chains of string
    literals; a constant whose value isn't a static string yields no entry. Returns
    ``{}`` on a read/parse failure (the caller's python check reports that)."""
    try:
        if source is None:
            with open(py_path, "r", encoding="utf-8", errors="surrogateescape") as f:
                source = f.read()
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return {}
    out: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value   # `_FOO_JS: str = "..."`
        else:
            continue
        val = None
        for tgt in targets:
            if isinstance(tgt, ast.Name) and tgt.id.endswith(EMBEDDED_JS_SUFFIX):
                if val is None:
                    val = _static_str(value)
                if val is not None:
                    out[tgt.id] = val
    return out


# ── per-language checkers (each: (text, label[, unit]) -> SyntaxResult) ───────
def check_python(text, label):
    try:
        compile(text, label, "exec")
        return _ok(label, "python")
    except SyntaxError as e:
        return _fail(label, "python", "%s:%s %s" % (e.lineno or "?", e.offset or "?", e.msg))
    except Exception as e:   # e.g. source containing null bytes, MemoryError, etc.
        return _fail(label, "python", str(e))


def _node_check(text, label, unit=None):
    node = _find_node()
    if not node:
        return _skip(label, "javascript", unit)
    cache_key = (text, node, label, unit)
    if cache_key in _NODE_CHECK_CACHE:
        return _NODE_CHECK_CACHE[cache_key]
    tmp = None
    fd = -1
    try:
        fd, tmp = tempfile.mkstemp(suffix=".js")
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
            fd = -1  # fdopen took ownership; don't double-close in finally
            f.write(text)
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=_SUBPROCESS_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        res = _skip(label, "javascript", unit)
        _NODE_CHECK_CACHE[cache_key] = res
        return res
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    if r.returncode == 0:
        res = _ok(label, "javascript", unit)
        _NODE_CHECK_CACHE[cache_key] = res
        return res
    err = (r.stderr or r.stdout or "").strip()
    lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
    err_line = next((ln for ln in lines if "Error" in ln), lines[0] if lines else "syntax error")
    # Node emits "<tmp_path>:<line>" as its first stderr line; rewrite the temp path to
    # `label` (which is "relpath::CONST" for embedded JS) so the operator can map the
    # error back to the source constant without decoding a cryptic /tmp path.
    # Match on basename because Node may resolve symlinks (e.g. /var/... vs /private/var/...
    # on macOS), so a full-path replace would leave a dangling path prefix.
    bname = os.path.basename(tmp) if tmp else ""
    loc_line = next((ln for ln in lines if bname and bname in ln), None)
    if loc_line and bname:
        idx = loc_line.find(bname)
        loc_line = label + loc_line[idx + len(bname):]
        detail = "%s: %s" % (loc_line, err_line)
    else:
        detail = err_line
    res = _fail(label, "javascript", detail, unit)
    _NODE_CHECK_CACHE[cache_key] = res
    return res


def check_javascript(text, label):
    return _node_check(text, label)


def check_json(text, label):
    try:
        json.loads(text)
        return _ok(label, "json")
    except ValueError as e:
        return _fail(label, "json", str(e))


def check_yaml(text, label):
    try:
        import yaml
    except ImportError:
        return _skip(label, "yaml")
    try:
        for _ in yaml.parse(text):
            pass
        return _ok(label, "yaml")
    except Exception as e:               # yaml.YAMLError + any loader error
        first = (str(e).splitlines() or ["yaml error"])[0]
        return _fail(label, "yaml", first)


def check_shell(text, label):
    bash = shutil.which("bash")
    if not bash:
        return _skip(label, "shell")
    try:
        r = subprocess.run([bash, "-n"], input=text, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=_SUBPROCESS_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return _skip(label, "shell")
    if r.returncode == 0:
        return _ok(label, "shell")
    err = (r.stderr or r.stdout or "").strip()
    return _fail(label, "shell", err.splitlines()[0] if err else "syntax error")


def check_toml(text, label):
    try:
        import tomllib
    except ImportError:
        return _skip(label, "toml")
    try:
        tomllib.loads(text)
        return _ok(label, "toml")
    except Exception as e:
        return _fail(label, "toml", str(e))


# ── registry + routing ───────────────────────────────────────────────────────
_LANG_CHECKER = {
    "python": check_python, "javascript": check_javascript, "json": check_json,
    "yaml": check_yaml, "shell": check_shell, "toml": check_toml,
}
_EXT_LANG = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".json": "json",
    ".yml": "yaml", ".yaml": "yaml",
    ".sh": "shell", ".bash": "shell",
    ".toml": "toml",
}


# POSIX-shell interpreters bash -n can soundly check. A SUBSTRING match on "sh"
# would wrongly route fish/tcsh (and any "sh"-containing name) to bash -n and
# emit a false syntax failure, so the interpreter BASENAME is matched exactly.
_SHELL_INTERPRETERS = {"sh", "bash", "dash", "ksh", "zsh", "ash"}


def _shebang_interpreter(first_line: str) -> Optional[str]:
    """The interpreter basename from a ``#!`` line (resolving a leading ``env``), or
    None. ``#!/bin/bash`` → ``bash``; ``#!/usr/bin/env zsh`` → ``zsh``."""
    if not first_line.startswith("#!"):
        return None
    for tok in first_line[2:].replace("\t", " ").split():
        if tok.startswith("-"):
            continue   # skip env flags like -S in `#!/usr/bin/env -S bash`
        base = tok.rsplit("/", 1)[-1]
        if base == "env":
            continue   # `#!/usr/bin/env <interp>` → the interpreter is the next token
        return base
    return None


def language_for(path: str) -> Optional[str]:
    """The language key for a path by extension (or a shell ``#!`` shebang for an
    extensionless file), else None -- an unrecognized file is never checked and never
    a failure."""
    ext = os.path.splitext(path)[1].lower()
    lang = _EXT_LANG.get(ext)
    if lang:
        return lang
    if ext == "":
        try:
            with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
                first = f.readline(1024)
        except OSError:
            return None
        if _shebang_interpreter(first) in _SHELL_INTERPRETERS:
            return "shell"
    return None


def available_languages() -> Dict[str, bool]:
    """{lang: toolchain_present} -- which checkers can actually run here."""
    avail = {"python": True, "json": True}
    avail["javascript"] = _find_node() is not None
    avail["shell"] = shutil.which("bash") is not None
    try:
        import yaml  # noqa: F401
        avail["yaml"] = True
    except ImportError:
        avail["yaml"] = False
    try:
        import tomllib  # noqa: F401
        avail["toml"] = True
    except ImportError:
        avail["toml"] = False
    return avail


def check_file(abspath: str, relpath: Optional[str] = None, *,
               source: Optional[str] = None) -> List[SyntaxResult]:
    """Syntax-check ONE file. Returns a list of SyntaxResult: one for the file in its
    own language (when recognized) PLUS one JavaScript result per statically-
    extractable ``*_JS`` constant when the file is Python (a ``.py`` with zero such
    constants yields exactly the single python row). A missing/unreadable file yields a
    single skipped result. Never raises."""
    label = relpath or abspath
    lang = language_for(abspath)
    if source is None:
        try:
            with open(abspath, "r", encoding="utf-8", errors="surrogateescape") as f:
                source = f.read()
        except OSError:
            return [_skip(label, lang or "unknown")]
    results: List[SyntaxResult] = []
    if lang in _LANG_CHECKER:
        results.append(_LANG_CHECKER[lang](source, label))
    if lang == "python":
        for name, js in embedded_js_constants(abspath, source=source).items():
            results.append(_node_check(js, "%s::%s" % (label, name), unit=name))
    return results


def check_paths(abspaths, *, repo_root: Optional[str] = None) -> List[SyntaxResult]:
    """check_file over many paths; flattened. A non-existent path yields a single
    skipped result (ignored by first_error)."""
    out: List[SyntaxResult] = []
    for p in abspaths:
        rel = None
        if repo_root:
            try:
                rel = os.path.relpath(p, repo_root)
            except ValueError:
                rel = None
        out.extend(check_file(p, rel))
    return out


def first_error(results) -> Optional[SyntaxResult]:
    """The first REAL syntax error (ok=False AND not skipped), or None."""
    for r in results:
        if not r.ok and not r.skipped:
            return r
    return None


def file_has_syntax_error(abspath: str, relpath: Optional[str] = None,
                          *, source: Optional[str] = None) -> Optional[SyntaxResult]:
    """The first real syntax error in ONE file, or None. Convenience for the
    deterministic-Unicode cleanup's per-file gate (normalize only files that currently
    fail; re-verify they now pass)."""
    return first_error(check_file(abspath, relpath, source=source))
