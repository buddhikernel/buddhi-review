"""The trial offer's release gate: the setup wizard offers (and ``start_trial`` starts)
the 14-day trial only once a public route says the paid release is published, with a
wheel this interpreter can install.

The route answers ``200 {"status": "open", "wheels": [<tag triple>, ...]}`` or
``200 {"status": "closed"}``. Only the first shape, with a wheel tag this interpreter
accepts, is open; everything else — a timeout, a network or TLS error, a redirect,
any other status, another content type, a malformed or oversized body, an unknown
status, an empty wheel list, a tag that does not parse — is closed.

The suite-wide conftest stubs the answer closed. Here the PRODUCTION HTTP client is
driven against a local server (127.0.0.1; the URL injected only through the client's
test seam), and the wizard / ``start_trial`` gates are driven through the same seam
the conftest uses.
"""
from __future__ import annotations

import contextlib
import functools
import http.server
import inspect
import io
import json
import os
import platform
import socket
import sys
import sysconfig
import threading
import time
import types

import pytest
from packaging import tags

from buddhi_review import pro_trial, wizard
from conftest import _yn_bridge

_HERE = str(next(iter(tags.sys_tags())))          # a tag this interpreter accepts
# The real "can sys.executable pip-install here" probe; the conftest answers it yes for
# every test, so the tests of the probe itself call this saved original.
_REAL_INSTALL_TARGET_OK = pro_trial._install_target_ok
_OPEN = {"status": "open", "wheels": [_HERE]}
_JSON = {"Content-Type": "application/json"}


def _body(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _foreign_tags():
    """Tags for other platforms that this interpreter does not accept."""
    candidates = ["cp313-cp313-win_amd64", "cp312-cp312-win32",
                  "cp312-cp312-macosx_11_0_arm64", "cp311-cp311-manylinux_2_17_x86_64",
                  "cp310-cp310-musllinux_1_2_aarch64"]
    here = frozenset(tags.sys_tags())
    foreign = [t for t in candidates if not (tags.parse_tag(t) & here)]
    assert len(foreign) >= 3
    return foreign


@pytest.fixture(autouse=True)
def _direct(monkeypatch):
    """Talk to the local server directly even if the developer has a proxy set."""
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.delenv("BUDDHI_NO_UPSELL", raising=False)


@contextlib.contextmanager
def _route(table):
    """A local stand-in for the release route. ``table`` maps a path to
    ``(status, headers, body, delay_s)``; every requested path is recorded."""
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server's naming
            hits.append(self.path)
            status, headers, body, delay = table.get(self.path, (404, {}, b"", 0))
            if delay:
                time.sleep(delay)
            try:
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass  # the client stopped waiting

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", hits
    finally:
        server.shutdown()
        server.server_close()


def _check(url, **kw):
    """A ReleaseCheck over the PRODUCTION client, pointed at ``url``."""
    return pro_trial.ReleaseCheck(
        fetch=functools.partial(pro_trial._fetch_release_answer, url=url), **kw)


# ── The production HTTP client, against a local server ──────────────────────────

def test_an_open_answer_with_a_wheel_for_this_interpreter_is_open():
    with _route({"/rs": (200, _JSON, _body(_OPEN), 0)}) as (base, hits):
        assert pro_trial._fetch_release_answer(url=base + "/rs") == _OPEN
        assert _check(base + "/rs").is_open() is True
    assert hits == ["/rs", "/rs"]


_CLOSED_ANSWERS = {
    "closed": (200, _JSON, _body({"status": "closed"})),
    "closed-with-wheels": (200, _JSON, _body({"status": "closed", "wheels": [_HERE]})),
    "open-empty-wheels": (200, _JSON, _body({"status": "open", "wheels": []})),
    "open-no-wheels": (200, _JSON, _body({"status": "open"})),
    "open-wheels-not-a-list": (200, _JSON, _body({"status": "open", "wheels": _HERE})),
    # An object of tag strings iterates like a list of them; only a list is the answer.
    "open-wheels-an-object": (200, _JSON, _body({"status": "open", "wheels": {_HERE: True}})),
    "open-non-string-wheel": (200, _JSON, _body({"status": "open", "wheels": [_HERE, 3]})),
    "unknown-status": (200, _JSON, _body({"status": "published", "wheels": [_HERE]})),
    "status-case": (200, _JSON, _body({"status": "OPEN", "wheels": [_HERE]})),
    "malformed-json": (200, _JSON, b'{"status": "open", "wheels": ['),
    "json-not-an-object": (200, _JSON, _body([_OPEN])),
    "not-utf8": (200, _JSON, b'\xff\xfe{"status": "open"}'),
    "empty-body": (200, _JSON, b""),
    "429-rate-limited": (429, _JSON, _body(_OPEN)),
    "500": (500, _JSON, _body(_OPEN)),
    "503": (503, _JSON, _body(_OPEN)),
    "201-not-200": (201, _JSON, _body(_OPEN)),
    "404": (404, _JSON, _body(_OPEN)),
    "text-html": (200, {"Content-Type": "text/html"}, _body(_OPEN)),
    "text-plain": (200, {"Content-Type": "text/plain; charset=utf-8"}, _body(_OPEN)),
    "no-content-type": (200, {}, _body(_OPEN)),
    "json-lookalike-type": (200, {"Content-Type": "application/json-seq"}, _body(_OPEN)),
}


# The answers the HTTP client itself rejects (it returns None, not the body).
_NOT_AN_ANSWER = {"malformed-json", "json-not-an-object", "not-utf8", "empty-body",
                  "429-rate-limited", "500", "503", "201-not-200", "404", "text-html",
                  "text-plain", "no-content-type", "json-lookalike-type"}


@pytest.mark.parametrize("name", sorted(_CLOSED_ANSWERS))
def test_every_other_answer_is_closed(name):
    status, headers, body = _CLOSED_ANSWERS[name]
    with _route({"/rs": (status, headers, body, 0)}) as (base, hits):
        assert _check(base + "/rs").is_open() is False
        if name in _NOT_AN_ANSWER:
            assert pro_trial._fetch_release_answer(url=base + "/rs") is None
    assert hits == (["/rs", "/rs"] if name in _NOT_AN_ANSWER else ["/rs"])


def test_a_charset_parameter_on_the_json_type_is_still_json():
    headers = {"Content-Type": "application/json; charset=utf-8"}
    with _route({"/rs": (200, headers, _body(_OPEN), 0)}) as (base, _):
        assert _check(base + "/rs").is_open() is True


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_a_redirect_is_never_followed(code):
    """Any 3xx is closed, and its target is never even requested — even when the
    target would answer open."""
    with _route({"/rs": (code, {"Location": "/elsewhere"}, b"", 0),
                 "/elsewhere": (200, _JSON, _body(_OPEN), 0)}) as (base, hits):
        assert pro_trial._fetch_release_answer(url=base + "/rs") is None
        assert _check(base + "/rs").is_open() is False
    assert hits == ["/rs", "/rs"]


def test_the_body_is_size_capped():
    """An answer up to the cap is read; one byte more is closed, however valid."""
    cap = pro_trial._RELEASE_MAX_BYTES
    exact = _body(_OPEN)
    exact = exact + b" " * (cap - len(exact))
    over = exact + b" "
    assert len(exact) == cap
    with _route({"/exact": (200, _JSON, exact, 0),
                 "/over": (200, _JSON, over, 0)}) as (base, _):
        assert _check(base + "/exact").is_open() is True
        assert _check(base + "/over").is_open() is False


@contextlib.contextmanager
def _plain_http_on_a_tls_port():
    """A TCP server that answers every connection with a plain-HTTP open answer and
    closes — whatever the client sends, so an https:// client's handshake fails at
    once instead of waiting on a timeout."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.2)
    stop = threading.Event()
    reply = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n" + _body(_OPEN))

    def serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            with conn:
                try:
                    conn.sendall(reply)
                except OSError:
                    pass
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{srv.getsockname()[1]}/rs"
    finally:
        stop.set()
        thread.join(2)
        srv.close()


def test_a_tls_failure_is_closed():
    with _plain_http_on_a_tls_port() as url:
        began = time.monotonic()
        assert pro_trial._fetch_release_answer(url=url) is None
        assert _check(url).is_open() is False
        assert time.monotonic() - began < 5


def test_a_dns_failure_is_closed(monkeypatch):
    looked_up = []

    def no_dns(host, *a, **k):
        looked_up.append(host)
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    assert pro_trial._fetch_release_answer() is None
    monkeypatch.setattr(pro_trial, "_release_fetch", None)
    assert pro_trial.ReleaseCheck().is_open() is False
    assert looked_up and set(looked_up) == {"api.buddhireview.com"}


def test_a_slow_answer_past_the_cap_is_closed_without_waiting_for_it():
    with _route({"/rs": (200, _JSON, _body(_OPEN), 2.0)}) as (base, hits):
        began = time.monotonic()
        assert _check(base + "/rs", timeout=0.3).is_open() is False
        assert time.monotonic() - began < 1.5
    assert hits == ["/rs"]


def test_an_answer_inside_the_cap_is_used():
    with _route({"/rs": (200, _JSON, _body(_OPEN), 0.3)}) as (base, _):
        assert _check(base + "/rs", timeout=5.0).is_open() is True


def test_the_production_route_and_cap_are_fixed():
    assert pro_trial.RELEASE_STATUS_URL == "https://api.buddhireview.com/release-status"
    assert pro_trial._RELEASE_TIMEOUT_S == 8.0
    params = inspect.signature(pro_trial._fetch_release_answer).parameters
    assert params["url"].default is None and params["timeout"].default == 8.0
    assert inspect.signature(pro_trial.ReleaseCheck).parameters["timeout"].default == 8.0


def test_the_default_check_goes_through_the_production_client(monkeypatch):
    """With the seam unset, a plain ReleaseCheck() asks RELEASE_STATUS_URL with the
    real client — here pointed at the local server — with one GET."""
    with _route({"/release-status": (200, _JSON, _body(_OPEN), 0)}) as (base, hits):
        monkeypatch.setattr(pro_trial, "_release_fetch", None)
        monkeypatch.setattr(pro_trial, "RELEASE_STATUS_URL", base + "/release-status")
        check = pro_trial.ReleaseCheck()
        assert check.is_open() is True
        assert check.is_open() is True
    assert hits == ["/release-status"]


# ── ReleaseCheck: one request, a wall-clock cap, a frozen answer ────────────────

def _counting(answer=_OPEN):
    calls = []

    def fetch():
        calls.append(1)
        return answer
    fetch.calls = calls
    return fetch


def test_a_check_makes_exactly_one_request():
    fetch = _counting()
    check = pro_trial.ReleaseCheck(fetch=fetch)
    check.start()
    check.start()
    assert [check.is_open() for _ in range(3)] == [True, True, True]
    assert len(fetch.calls) == 1


def test_an_answer_that_lands_after_the_cap_is_closed_even_when_read_later():
    """The wizard asks at its start and reads at its end: an answer that took longer
    than the cap is closed however late it is first read."""
    def slow():
        time.sleep(0.3)
        return _OPEN
    check = pro_trial.ReleaseCheck(fetch=slow, timeout=0.1).start()
    time.sleep(0.6)                                   # the answer has landed, late
    assert check._thread is not None and not check._thread.is_alive()
    assert check.is_open() is False


def test_an_answer_that_landed_in_time_is_used_when_read_much_later():
    check = pro_trial.ReleaseCheck(fetch=_counting(), timeout=0.2).start()
    time.sleep(0.5)                                   # read well past the cap
    assert check.is_open() is True


def test_an_answer_that_lands_after_the_cap_never_reopens_the_check():
    release = threading.Event()
    finished = threading.Event()

    def late():
        release.wait(5)
        finished.set()
        return _OPEN
    check = pro_trial.ReleaseCheck(fetch=late, timeout=0.1)
    assert check.is_open() is False
    release.set()
    assert finished.wait(5)
    time.sleep(0.05)
    assert check.is_open() is False


def test_a_fetch_that_raises_is_closed_quietly(monkeypatch):
    """Closed, and no thread traceback lands on the user's terminal mid-setup."""
    escaped = []
    monkeypatch.setattr(threading, "excepthook", escaped.append)

    def boom():
        raise RuntimeError("no")
    assert pro_trial.ReleaseCheck(fetch=boom).is_open() is False
    assert escaped == []


def test_no_thread_to_ask_on_is_closed(monkeypatch):
    fetch = _counting()

    def cannot_start(self):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    assert pro_trial.ReleaseCheck(fetch=fetch).is_open() is False
    assert fetch.calls == []


# ── The wheel match: this interpreter's tags, via packaging ─────────────────────

def test_a_matching_wheel_tag_offers():
    assert pro_trial._release_open({"status": "open", "wheels": [_HERE]}) is True
    mixed = _foreign_tags() + [_HERE]
    assert pro_trial._release_open({"status": "open", "wheels": mixed}) is True


def test_a_compressed_tag_set_is_expanded():
    """A wheel tag may compress several platforms (``a.b``); one of them matching is
    enough, which a plain string comparison would miss."""
    here = next(iter(tags.sys_tags()))
    compressed = f"{here.interpreter}-{here.abi}-win_arm64.{here.platform}"
    assert compressed not in {str(t) for t in tags.sys_tags()}
    assert pro_trial._release_open({"status": "open", "wheels": [compressed]}) is True


def test_only_foreign_wheel_tags_do_not_offer():
    assert pro_trial._release_open({"status": "open", "wheels": _foreign_tags()}) is False


@pytest.mark.parametrize("bad", ["cp312_cp312_any", "", "cp312-cp312", "a-b-c-d"])
def test_a_malformed_wheel_tag_does_not_offer(bad):
    assert pro_trial._release_open({"status": "open", "wheels": [bad]}) is False
    # Fail closed: one unparseable entry closes the whole answer.
    assert pro_trial._release_open({"status": "open", "wheels": [_HERE, bad]}) is False


def test_no_interpreter_path_offers_nothing(monkeypatch):
    monkeypatch.setattr(pro_trial, "_install_target_ok", _REAL_INSTALL_TARGET_OK)
    monkeypatch.setattr(sys, "executable", "")
    assert pro_trial._release_open(_OPEN) is False


# ── The interpreter pip_install runs: this one, and able to pip-install ─────────

def _identity_line(can_install="True", **over):
    parts = {"hexversion": str(sys.hexversion), "cache_tag": sys.implementation.cache_tag,
             "abiflags": getattr(sys, "abiflags", "") or "-",
             "machine": platform.machine() or "-", "ok": can_install}
    parts.update(over)
    return " ".join(parts.values())


def _fake_interpreter(tmp_path, line, *, code=0, sleep=0):
    """A stand-in for sys.executable that prints ``line`` whatever it is asked."""
    exe = tmp_path / "python-stand-in"
    exe.write_text(f"#!/bin/sh\nsleep {sleep}\necho '{line}'\nexit {code}\n", encoding="utf-8")
    exe.chmod(0o755)
    return str(exe)


def test_the_probe_answers_for_this_interpreter():
    """Run for real against the interpreter running these tests: yes exactly when pip
    is importable here and this is not an externally-managed system interpreter."""
    import importlib.util
    managed = sys.prefix == sys.base_prefix and os.path.isfile(
        os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED"))
    expected = importlib.util.find_spec("pip") is not None and not managed
    assert _REAL_INSTALL_TARGET_OK() is expected


_posix_only = pytest.mark.skipif(sys.platform == "win32", reason="a /bin/sh stand-in")


@_posix_only
@pytest.mark.parametrize("case,line,code,ok", [
    ("this-interpreter-can-install", _identity_line(), 0, True),
    ("no-pip-or-externally-managed", _identity_line("False"), 0, False),
    ("another-python-version", _identity_line(hexversion="50331888"), 0, False),
    ("another-abi", _identity_line(abiflags="t"), 0, False),
    ("another-machine", _identity_line(machine="sparc64"), 0, False),
    ("probe-failed", _identity_line(), 1, False),
    ("garbage", "Python is not configured", 0, False),
])
def test_the_probe_trusts_only_this_interpreter_able_to_install(monkeypatch, tmp_path,
                                                               case, line, code, ok):
    monkeypatch.setattr(sys, "executable", _fake_interpreter(tmp_path, line, code=code))
    assert _REAL_INSTALL_TARGET_OK() is ok
    monkeypatch.setattr(pro_trial, "_install_target_ok", _REAL_INSTALL_TARGET_OK)
    assert pro_trial._release_open(_OPEN) is ok


@_posix_only
@pytest.mark.parametrize("printed,ok", [("t", True), ("-", False)])
def test_the_probe_compares_the_abi_flags(monkeypatch, tmp_path, printed, ok):
    """On a free-threaded build (ABI flags ``t``) only an interpreter with the same
    flags is this one; a regular build of the same version is not."""
    monkeypatch.setattr(sys, "abiflags", "t", raising=False)
    line = _identity_line(abiflags=printed)
    monkeypatch.setattr(sys, "executable", _fake_interpreter(tmp_path, line))
    assert _REAL_INSTALL_TARGET_OK() is ok


@_posix_only
def test_a_probe_that_hangs_is_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "executable", _fake_interpreter(tmp_path, _identity_line(), sleep=5))
    began = time.monotonic()
    assert _REAL_INSTALL_TARGET_OK(timeout=0.3) is False
    assert time.monotonic() - began < 3


def test_a_missing_interpreter_is_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "no-such-python"))
    assert _REAL_INSTALL_TARGET_OK() is False


def _run_probe(monkeypatch, capsys, tmp_path, *, venv, marker, pip):
    """Run the probe's own source in this process with the environment faked."""
    import importlib.util
    stdlib = tmp_path / "stdlib"
    stdlib.mkdir()
    if marker:
        (stdlib / "EXTERNALLY-MANAGED").write_text("[externally-managed]\n", encoding="utf-8")
    real_get_path = sysconfig.get_path
    monkeypatch.setattr(sysconfig, "get_path",
                        lambda name, *a, **k: str(stdlib) if name == "stdlib"
                        else real_get_path(name, *a, **k))
    monkeypatch.setattr(sys, "base_prefix", sys.prefix if not venv else sys.prefix + "-base")
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **k: (real_find_spec(name, *a, **k)
                                               if name != "pip" or pip else None))
    capsys.readouterr()
    exec(pro_trial._INSTALL_PROBE, {})
    return capsys.readouterr().out.split()[-1]


@pytest.mark.parametrize("venv,marker,pip,ok", [
    (False, False, True, "True"),     # a plain interpreter with pip
    (False, True, True, "False"),     # externally managed (PEP 668): pip refuses
    (True, True, True, "True"),       # a venv on an externally-managed base is fine
    (False, False, False, "False"),   # no pip (a uv tool environment)
    (True, False, False, "False"),    # a venv made without pip
])
def test_the_probe_reads_pip_and_pep668_as_pip_would(monkeypatch, capsys, tmp_path,
                                                     venv, marker, pip, ok):
    import importlib.util
    if importlib.util.find_spec("pip") is None and pip:
        pytest.skip("pip is not importable in this test interpreter")
    assert _run_probe(monkeypatch, capsys, tmp_path, venv=venv, marker=marker, pip=pip) == ok


def test_without_packaging_nothing_is_offered(monkeypatch):
    monkeypatch.setitem(sys.modules, "packaging.tags", None)
    assert pro_trial._release_open(_OPEN) is False


# ── The wizard's offer: gated after the TTY + offer checks, before the counters ──

def _state(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _state_path():
    return pro_trial._state_path()


def _never(prompt=""):
    raise AssertionError(f"no prompt expected, got {prompt!r}")


@pytest.fixture
def _tty(monkeypatch):
    monkeypatch.setattr(wizard, "_is_tty", lambda: True)
    monkeypatch.setattr(wizard, "single_select", _yn_bridge)
    # Not on Pro: deterministic whatever happens to be installed in this environment.
    monkeypatch.setattr(pro_trial, "pro_backend_active", lambda **k: False)


def test_a_closed_answer_shows_nothing_and_spends_no_offer(monkeypatch, _tty):
    path = _state_path()
    path.write_text(json.dumps({"shown_count": 1, "last_shown": 0}), encoding="utf-8")
    before = path.read_bytes()
    fetch = _counting({"status": "closed"})
    monkeypatch.setattr(pro_trial, "_release_fetch", fetch)
    monkeypatch.setattr(pro_trial, "start_trial", lambda *a, **k: pytest.fail("started"))
    buf = io.StringIO()
    wizard.step_pro_trial("acme/widgets", pal=wizard._Palette(False), stream=buf,
                          input_fn=_never)
    assert buf.getvalue() == ""
    assert len(fetch.calls) == 1
    assert path.read_bytes() == before        # shown_count / last_shown untouched


def test_an_open_answer_shows_the_offer_and_counts_it(monkeypatch, _tty):
    fetch = _counting()
    monkeypatch.setattr(pro_trial, "_release_fetch", fetch)
    buf = io.StringIO()
    wizard.step_pro_trial("acme/widgets", pal=wizard._Palette(False), stream=buf,
                          input_fn=lambda prompt="": "n")
    assert wizard._PRO_TRIAL_OFFER in buf.getvalue()
    state = _state(_state_path())
    assert state["shown_count"] == 1 and isinstance(state["last_shown"], (int, float))
    assert state["declined"] is True
    assert len(fetch.calls) == 1


_GATED = {
    "suppressed": lambda mp, path: mp.setenv("BUDDHI_NO_UPSELL", "1"),
    "declined": lambda mp, path: path.write_text(json.dumps({"declined": True})),
    "capped": lambda mp, path: path.write_text(json.dumps({"shown_count": 5})),
    "recently-shown": lambda mp, path: path.write_text(
        json.dumps({"shown_count": 1, "last_shown": time.time()})),
    "already-pro": lambda mp, path: mp.setattr(pro_trial, "pro_backend_active",
                                               lambda **k: True),
    "off-tty": lambda mp, path: mp.setattr(wizard, "_is_tty", lambda: False),
}


@pytest.mark.parametrize("who", sorted(_GATED))
def test_a_gated_user_never_contacts_the_route(monkeypatch, _tty, who):
    _GATED[who](monkeypatch, _state_path())
    fetch = _counting()
    monkeypatch.setattr(pro_trial, "_release_fetch", fetch)
    buf = io.StringIO()
    wizard.step_pro_trial("acme/widgets", pal=wizard._Palette(False), stream=buf,
                          input_fn=_never)
    assert fetch.calls == []
    assert buf.getvalue() == ""


# ── The full wizard: one request, prefetched early, reused by start_trial ────────

def _setup_run(calls):
    R = types.SimpleNamespace

    def run(argv, cwd=None, timeout=30, input=None):
        calls.append(("run", list(argv)))
        if argv[:2] == ["gh", "--version"]:
            return R(returncode=0, stdout="gh version 2.90.0 (2026-01-01)")
        if argv[:2] == ["git", "-C"] and "remote" in argv:
            return R(returncode=0, stdout="git@github.com:acme/widgets.git\n")
        if argv and (argv[0] == "claude" or str(argv[0]).endswith("claude")):
            return R(returncode=0, stdout="pong")
        return R(returncode=0, stdout="")
    return run


def _full_run(monkeypatch, tmp_path, *, answers, calls):
    monkeypatch.setattr(wizard, "_set_claude_secret", lambda *a, **k: "present")
    monkeypatch.setattr(wizard, "_offer_update_managed_file", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_offer_gh_token", lambda *a, **k: None)

    def input_fn(prompt=""):
        for key, value in answers.items():
            if key in prompt:
                calls.append(("prompt", key))
                return value
        return ""
    buf = io.StringIO()
    rc = wizard.run(
        config_path=tmp_path / "config.yaml", run=_setup_run(calls),
        which=lambda x: "/bin/claude" if x == "claude" else None,
        single_select=lambda prompt, options, *, preselect=0, **kw: preselect,
        multi_select=lambda *a, **k: set(),
        getpass_fn=lambda *a: "", spawn_command=lambda *a, **k: {"spawned": False},
        input_fn=input_fn, stream=buf)
    assert rc == 0
    return buf.getvalue()


def _record_start(monkeypatch, calls):
    real_start = pro_trial.ReleaseCheck.start

    def start(self):
        if self._thread is None and self._answer is None:
            calls.append(("release-check", None))
        return real_start(self)
    monkeypatch.setattr(pro_trial.ReleaseCheck, "start", start)


def test_a_full_run_asks_once_early_and_start_trial_reuses_the_answer(
        monkeypatch, tmp_path, _tty):
    calls = []
    _record_start(monkeypatch, calls)
    fetch = _counting()
    monkeypatch.setattr(pro_trial, "_release_fetch", fetch)
    keygen = []

    def transport(method, url, auth, body):
        keygen.append(url)
        return 409, {"errors": [{"code": "EMAIL_TAKEN"}]}
    monkeypatch.setattr(pro_trial, "_default_transport", transport)
    out = _full_run(monkeypatch, tmp_path, calls=calls, answers={
        "Start your free 14-day Pro trial": "y",
        "Email to set up your trial": "me@example.com",
        "Already have a Pro key to paste": "n",
        "Review an open PR now": "n",
    })
    assert len(fetch.calls) == 1                      # one request for the whole run
    assert calls[0] == ("release-check", None)        # started before the first step
    assert [c for c in calls if c[0] == "release-check"] == [("release-check", None)]
    assert wizard._PRO_TRIAL_OFFER in out
    assert keygen and keygen[0].endswith("/users")    # the open answer let it register
    assert ("prompt", "Already have a Pro key to paste") in calls


def test_a_full_run_while_closed_offers_nothing(monkeypatch, tmp_path, _tty):
    calls = []
    fetch = _counting({"status": "closed"})
    monkeypatch.setattr(pro_trial, "_release_fetch", fetch)
    out = _full_run(monkeypatch, tmp_path, calls=calls, answers={
        "Start your free 14-day Pro trial": "y", "Review an open PR now": "n"})
    assert wizard._PRO_TRIAL_OFFER not in out
    assert ("prompt", "Start your free 14-day Pro trial") not in calls
    assert len(fetch.calls) == 1
    assert _state(_state_path()) is None              # no offer counted


@pytest.mark.parametrize("who", ["suppressed", "declined", "capped", "already-pro", "off-tty"])
def test_a_full_run_for_a_gated_user_never_contacts_the_route(monkeypatch, tmp_path,
                                                              _tty, who):
    _GATED[who](monkeypatch, _state_path())
    calls = []
    _record_start(monkeypatch, calls)
    fetch = _counting()
    monkeypatch.setattr(pro_trial, "_release_fetch", fetch)
    _full_run(monkeypatch, tmp_path, calls=calls, answers={"Review an open PR now": "n"})
    assert fetch.calls == []
    assert ("release-check", None) not in calls


# ── start_trial's own guard: first, before the pending-credential retry ─────────

class _Closed:
    def is_open(self):
        return False


@pytest.mark.parametrize("pending", [False, True], ids=["fresh", "pending-credential"])
def test_start_trial_while_closed_mints_nothing(tmp_path, pending):
    state = tmp_path / "trial.json"
    if pending:
        state.write_text(json.dumps({"pending_email": "me@example.com",
                                     "pending_password": "pw",
                                     "pending_user_id": "user-1"}), encoding="utf-8")
    before = state.read_bytes() if pending else None
    keygen, installs = [], []
    netrc = tmp_path / ".netrc"
    outcome = pro_trial.start_trial(
        "me@example.com", release=_Closed(), state_path=state, netrc_path=netrc,
        transport=lambda *a: keygen.append(a) or (201, {}),
        runner=lambda cmd, env=None: installs.append(cmd))
    assert (outcome.ok, outcome.status, outcome.message) == (False, pro_trial.RELEASE_CLOSED, "")
    assert keygen == [] and installs == []
    assert not netrc.exists()
    if pending:
        assert state.read_bytes() == before      # the pending credential is untouched
    else:
        assert not state.exists()


def test_start_trial_checks_the_release_before_anything_else():
    """The guard is the first statement: even an invalid email gets the silent closed
    outcome, not a message."""
    outcome = pro_trial.start_trial("not-an-email", release=_Closed())
    assert outcome.status == pro_trial.RELEASE_CLOSED


def test_start_trial_called_directly_asks_once(monkeypatch, tmp_path):
    fetch = _counting({"status": "closed"})
    monkeypatch.setattr(pro_trial, "_release_fetch", fetch)
    outcome = pro_trial.start_trial("me@example.com", state_path=tmp_path / "s.json",
                                    transport=lambda *a: pytest.fail("contacted keygen"))
    assert outcome.status == pro_trial.RELEASE_CLOSED
    assert len(fetch.calls) == 1


def test_start_trial_reuses_the_run_answer(monkeypatch, tmp_path):
    fetch = _counting()
    monkeypatch.setattr(pro_trial, "_release_fetch", fetch)
    check = pro_trial.ReleaseCheck().start()
    assert check.is_open() is True
    outcome = pro_trial.start_trial(
        "me@example.com", release=check, state_path=tmp_path / "s.json",
        transport=lambda *a: (409, {"errors": [{"code": "EMAIL_TAKEN"}]}))
    assert outcome.status == "email_registered"
    assert len(fetch.calls) == 1


# ── The paid-key prompt is behind the same gate (deliberately) ──────────────────

class _OpenThenClosed:
    """Open for the offer, closed by the time start_trial asks: pins the guard INSIDE
    start_trial, independent of the step's own check."""

    def __init__(self):
        self.asked = 0

    def is_open(self):
        self.asked += 1
        return self.asked == 1


def test_the_paid_key_prompt_is_hidden_while_closed(monkeypatch, _tty):
    """The paste prompt follows start_trial's "email already registered" outcome, so
    the release guard at the top of start_trial hides it while closed: no account is
    contacted, no paste is asked for, convert never runs, and nothing is printed."""
    keygen = []
    monkeypatch.setattr(pro_trial, "_default_transport",
                        lambda *a: keygen.append(a) or (409, {"errors": [{"code": "TAKEN"}]}))
    monkeypatch.setattr(pro_trial, "convert", lambda **k: pytest.fail("convert ran"))
    asked = []

    def input_fn(prompt=""):
        asked.append(prompt)
        assert "Pro key" not in prompt
        return "me@example.com" if "Email" in prompt else "y"
    release = _OpenThenClosed()
    buf = io.StringIO()
    wizard.step_pro_trial("acme/widgets", pal=wizard._Palette(False), stream=buf,
                          input_fn=input_fn, release=release)
    assert release.asked == 2                    # the step, then start_trial
    assert keygen == []
    assert not any("Pro key" in p for p in asked)
    # The offer line is all that was printed: no outcome row after it.
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1 and "Try Buddhi Pro free for 14 days" in lines[0]


def test_the_paid_key_prompt_is_hidden_when_the_step_is_closed(monkeypatch, _tty):
    monkeypatch.setattr(pro_trial, "convert", lambda **k: pytest.fail("convert ran"))
    monkeypatch.setattr(pro_trial, "_default_transport",
                        lambda *a: pytest.fail("contacted keygen"))
    buf = io.StringIO()
    wizard.step_pro_trial("acme/widgets", pal=wizard._Palette(False), stream=buf,
                          input_fn=_never, release=_Closed())
    assert buf.getvalue() == ""
