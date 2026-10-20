"""Unit tests for pro_trial.py — the free tool's Buddhi Review Pro ACQUISITION plumbing.

Network-free (every request to the Buddhi Review service goes through a FakeBridge,
tests/conftest.py) and pip-free (the install runner is injected). This file covers the
pieces every setup path shares — the service client, the shapes checked before any
request, the trial's machine fingerprint, the 0600 state, the licence agreement's
gate, the seat-access file and the install tail — and the trial end to end, with its
offer gating. The other paths (``setup --buy``, ``--activate``, ``--recover``, and what
a plain setup finishes first) are tests/test_pro_setup_paths.py's.

Every line asserted here is the approved on-screen wording, word for word.

This file names the pro package it installs by design, so it is allowlisted in
tests/test_oss_purity.py's _VOCAB_SCAFFOLDING.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import json
import os
import re
import stat
import threading
import time
import types

import pytest

from buddhi_review import pro_trial
from conftest import FakeBridge, ScriptedUi

_KEY = "TRIALKEY-ABC-123"
_SUPPORT = "support@buddhireview.com"


# ── Shared fakes ──────────────────────────────────────────────────────────────

class FakeProBackend:
    name = "pro"
    priority = 100

    def __init__(self, active=True):
        self._active = active
        self.started = 0

    def is_active(self):
        return self._active

    def run_review_loop(self, *a, **k):  # pragma: no cover - Protocol completeness
        return 0

    def start_daemon(self, **k):
        self.started += 1
        return True


def _open_here():
    """A release answer that is open, with a wheel tag this interpreter accepts."""
    from packaging import tags
    return {"status": "open", "wheels": [str(next(iter(tags.sys_tags())))]}


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv("BUDDHI_NO_UPSELL", raising=False)
    monkeypatch.setenv("BUDDHI_TRIAL_STATE", str(tmp_path / "trial.json"))
    # These tests drive the acquisition itself, so the release is published here; the
    # closed answer and the gate around it are tests/test_pro_release_gate.py's.
    monkeypatch.setattr(pro_trial, "_release_fetch", _open_here)
    # Not on Pro: deterministic whatever happens to be installed in this environment.
    monkeypatch.setattr(pro_trial, "pro_backend_active", lambda **k: False)


def _bridge(monkeypatch, routes):
    fake = FakeBridge(routes)
    monkeypatch.setattr(pro_trial, "_bridge_transport", fake)
    return fake


def _ok_runner(cmd, env=None):
    return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")


def _install_kw(**over):
    kw = dict(runner=_ok_runner, backends=[FakeProBackend(active=True)],
              is_active=lambda: True, sleep=lambda s: None, attempts=1)
    kw.update(over)
    return kw


def _state():
    path = pro_trial._state_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _set_state(**values):
    path = pro_trial._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values), encoding="utf-8")


def _eula_accepted():
    return {"version": "1.1", "sha256": pro_trial.EULA_SHA256,
            "accepted_at": "2026-10-18T00:00:00Z"}


# ── Shapes checked before any request ─────────────────────────────────────────

@pytest.mark.parametrize("seconds, phrase", [
    (None, "a few minutes"), (0, "1 minute"), (1, "1 minute"), (60, "1 minute"),
    (61, "2 minutes"), (59 * 60, "59 minutes"), (3599, "1 hour"), (3600, "1 hour"),
    (3601, "2 hours"), (7200, "2 hours")])
def test_time_phrase_rounds_up_and_says_a_few_minutes_without_retry_after(seconds, phrase):
    assert pro_trial._time_phrase(seconds) == phrase


@pytest.mark.parametrize("email", ["a@b.co", "Ann.Lee+x@GMail.com", "o'brien@example.com",
                                   "  me@example.com  ", "a@b.c0"])
def test_trial_email_accepts_what_the_service_accepts(email):
    assert pro_trial.valid_email(email)


@pytest.mark.parametrize("email", ["nope", "a@b", "+tag@example.com", "a.@example.com",
                                   "a..b@example.com", "a@@example.com", "a b@example.com",
                                   "a@-x.com", "a@x.1com", "x" * 65 + "@example.com",
                                   ".@gmail.com"])
def test_trial_email_refuses_what_the_service_refuses(email):
    assert not pro_trial.valid_email(email)


def test_new_recovery_address_uses_the_stricter_grammar():
    assert pro_trial.valid_new_recovery_address("name@example.com")
    assert pro_trial.valid_new_recovery_address("a_b%c+d-e@x.xn--p1ai")
    for bad in ("o'brien@example.com", "a@b.c0", " name@example.com", "-@example.com",
                "name@example", "näme@example.com", "a@b@c.com"):
        assert not pro_trial.valid_new_recovery_address(bad), bad
    # every address the stricter grammar takes, the current-address grammar takes too
    assert pro_trial.valid_recovery_address("name@example.com")
    assert pro_trial.valid_recovery_address("o'brien@example.com")


@pytest.mark.parametrize("typed, code", [
    ("abcd-efgh-jkmn-pqrs", "ABCDEFGHJKMNPQRS"),
    ("ABCD EFGH JKMN PQRS", "ABCDEFGHJKMNPQRS"),
    ("ilo0-ILO0-1234-5678", "1100110012345678"),
])
def test_recovery_code_is_read_as_crockford(typed, code):
    assert pro_trial._recovery_code(typed) == code


@pytest.mark.parametrize("typed", ["ABCD-EFGH-JKMN-PQRU", "ABCD-EFGH-JKMN-PQR", "", "ABCD!EFGHJKMNPQRS"])
def test_a_recovery_code_of_the_wrong_shape_is_refused(typed):
    assert pro_trial._recovery_code(typed) is None


def test_a_setup_code_is_32_random_bytes_and_its_hash_is_of_the_raw_bytes():
    code, setup_hash = pro_trial._new_setup_code()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", code)
    raw = base64.urlsafe_b64decode(code + "=")
    assert len(raw) == 32
    assert setup_hash == hashlib.sha256(raw).hexdigest()
    assert setup_hash != hashlib.sha256(code.encode()).hexdigest()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", pro_trial._new_nonce())


# ── The service client ─────────────────────────────────────────────────────────

@contextlib.contextmanager
def _server(handler_fn):
    """A local HTTP server; ``handler_fn(request_handler)`` answers each request."""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _any(self):
            length = int(self.headers.get("Content-Length") or 0)
            seen.append({"method": self.command, "path": self.path,
                         "headers": dict(self.headers),
                         "body": self.rfile.read(length) if length else b""})
            handler_fn(self)

        do_GET = do_POST = _any

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


def _reply(status, body=b"", headers=None):
    def handler(h):
        h.send_response(status)
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
    return handler


@pytest.fixture
def _direct(monkeypatch):
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")


def test_the_client_sends_json_with_the_secret_only_in_the_header(_direct):
    with _server(_reply(202, b'{"status":"pending"}', {"Retry-After": "3"})) as (url, seen):
        status, data, retry = pro_trial._https_once("POST", f"{url}/claim", {"a": 1},
                                                    "Bearer s3cret")
    assert (status, data, pro_trial._seconds(retry)) == (202, {"status": "pending"}, 3.0)
    assert seen[0]["method"] == "POST" and seen[0]["path"] == "/claim"
    assert json.loads(seen[0]["body"]) == {"a": 1}
    assert seen[0]["headers"]["Content-Type"] == "application/json"
    assert seen[0]["headers"]["Authorization"] == "Bearer s3cret"
    assert "s3cret" not in seen[0]["path"]


def test_the_client_reads_an_error_answer_body(_direct):
    with _server(_reply(409, b'{"error":"seat_not_active"}')) as (url, _seen):
        status, data, _ = pro_trial._https_once("POST", f"{url}/claim", {}, None)
    assert (status, data) == (409, {"error": "seat_not_active"})


@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_the_client_never_follows_a_redirect(_direct, code, monkeypatch):
    with _server(_reply(code, b"", {"Location": "https://elsewhere.example/"})) as (url, seen):
        status, _data, _ = pro_trial._https_once("POST", f"{url}/claim", {}, None)
        assert status == code and len(seen) == 1
        monkeypatch.setattr(pro_trial, "_bridge_transport",
                            lambda m, u, b, a: pro_trial._https_once(m, f"{url}/claim", b, a))
        answer = pro_trial._bridge_call("POST", "/claim", {})
    assert answer.status == 0                     # a redirect is no answer


def test_the_client_caps_the_answer_size(_direct):
    big = json.dumps({"x": "y" * (pro_trial._BRIDGE_MAX_BYTES + 10)}).encode()
    with _server(_reply(200, big)) as (url, _seen):
        status, data, _ = pro_trial._https_once("GET", f"{url}/seats", None, None)
    assert status == 200 and data is None


def test_a_network_failure_is_no_answer_never_a_raise():
    status, data, retry = pro_trial._https_once("POST", "http://127.0.0.1:9/claim", {}, None)
    assert (status, data, retry) == (0, None, None)


def test_the_request_wall_clock_is_bounded(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(pro_trial, "_https_once",
                        lambda *a: release.wait(5) or (200, {}, None))
    monkeypatch.setattr(pro_trial, "_BRIDGE_WALL_S", 0.2)
    started = time.monotonic()
    assert pro_trial._https_json("POST", "https://api.buddhireview.com/claim", {}, None) == (
        0, None, None)
    assert time.monotonic() - started < 2
    release.set()


@pytest.mark.parametrize("status", ["not-a-number", None])
def test_a_misbehaving_transport_is_no_answer(monkeypatch, status):
    monkeypatch.setattr(pro_trial, "_bridge_transport", lambda *a: (status, {"x": 1}, None))
    assert pro_trial._bridge_call("POST", "/claim", {}).status == 0
    monkeypatch.setattr(pro_trial, "_bridge_transport",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert pro_trial._bridge_call("POST", "/claim", {}).status == 0


def test_requests_go_only_to_the_service_over_https(monkeypatch):
    fake = _bridge(monkeypatch, {"GET /seats": [(200, {"seats": []})]})
    pro_trial._bridge_call("GET", "/seats", query={"subscription_id": "sub_1"},
                           auth="Bearer x")
    assert fake.unexpected == []
    assert fake.calls[0]["query"] == {"subscription_id": "sub_1"}
    assert pro_trial.BRIDGE_ORIGIN == "https://api.buddhireview.com"


def test_no_answer_is_retried_with_the_same_request_then_reported(monkeypatch):
    fake = _bridge(monkeypatch, {"POST /trial": [(503, {"error": "unavailable"}, 5),
                                                 (0, None), (500, None)]})
    answer = pro_trial._call_retrying("POST", "/trial", {"claim_nonce": "n"})
    assert answer.status == 500 and fake.count("POST /trial") == pro_trial._NO_ANSWER_TRIES
    assert all(b == {"claim_nonce": "n"} for b in fake.bodies("POST /trial"))


def test_an_answer_stops_the_retries(monkeypatch):
    fake = _bridge(monkeypatch, {"POST /trial": [(0, None), (403, {"error": "code_invalid"})]})
    assert pro_trial._call_retrying("POST", "/trial", {}).status == 403
    assert fake.count("POST /trial") == 2


def test_trial_mail_paused_is_an_answer_not_a_silence():
    paused = pro_trial.BridgeAnswer(503, {"error": "sending_paused"}, 60)
    assert not pro_trial._no_answer(paused)
    assert pro_trial._no_answer(pro_trial.BridgeAnswer(503, {"error": "maintenance"}, 300))
    assert pro_trial._no_answer(pro_trial.BridgeAnswer(200, None, None))


# ── The trial's machine fingerprint ───────────────────────────────────────────

def test_the_fingerprint_is_64_hex_from_the_machine_id(monkeypatch):
    monkeypatch.setattr(pro_trial, "_machine_id", lambda: "MACHINE-UUID-1")
    fp = pro_trial.trial_fingerprint()
    assert re.fullmatch(r"[0-9a-f]{64}", fp)
    assert fp == hashlib.sha256(b"buddhi-review trial fingerprint v1|MACHINE-UUID-1").hexdigest()
    assert fp == pro_trial.trial_fingerprint()
    monkeypatch.setattr(pro_trial, "_machine_id", lambda: "MACHINE-UUID-2")
    assert pro_trial.trial_fingerprint() != fp


def test_without_a_machine_id_a_kept_random_one_stands_in(monkeypatch):
    monkeypatch.setattr(pro_trial, "_machine_id", lambda: "")
    first = pro_trial.trial_fingerprint()
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert pro_trial.trial_fingerprint() == first           # kept in the 0600 state
    assert re.fullmatch(r"[0-9a-f]{64}", _state()["machine_id"])


# ── The 0600 state ────────────────────────────────────────────────────────────

def test_write_state_leaves_no_temp_file_behind(tmp_path):
    p = tmp_path / "s" / "trial.json"
    pro_trial._write_state(p, {"shown_count": 1})
    assert json.loads(p.read_text()) == {"shown_count": 1}
    assert [x.name for x in p.parent.iterdir()] == ["trial.json"]
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_write_state_removes_the_temp_file_when_replace_fails(tmp_path, monkeypatch):
    p = tmp_path / "trial.json"

    def boom(*a, **k):
        raise OSError("replace failed")
    monkeypatch.setattr(pro_trial.os, "replace", boom)
    assert pro_trial._save_state(p, {"trial_nonce": "n"}) == "replace failed"
    assert list(tmp_path.iterdir()) == []


def test_save_state_reports_why_it_could_not_save(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    why = pro_trial._save_state(blocker / "trial.json", {"a": 1})
    assert why                                   # the operating system's reason


def test_an_older_releases_pending_password_is_dropped_on_the_next_write(tmp_path):
    p = tmp_path / "trial.json"
    p.write_text(json.dumps({"pending_email": "a@b.co", "pending_password": "pw",
                             "pending_user_id": "u", "shown_count": 2}))
    pro_trial._write_state(p, pro_trial._read_state(p))
    assert json.loads(p.read_text()) == {"shown_count": 2}


# ── The licence agreement, before any key ─────────────────────────────────────

_EULA_LINE = ("To use Buddhi Review Pro, you must accept the Buddhi Review Pro End User Licence "
              "Agreement, Version 1.1, effective 4 October 2026: https://buddhireview.com/eula")


def test_assent_is_asked_with_the_approved_sentence_and_recorded(monkeypatch):
    monkeypatch.setattr(pro_trial, "_now", lambda: 1_760_000_000.0)
    ui = ScriptedUi(yes={"Do you accept it?": [True]})
    assert pro_trial._eula_gate(ui, None) == ("ok", "")
    assert ui.lines == [("step", _EULA_LINE)]
    assert ui.defaults["Do you accept it?"] is False        # default No
    assert _state()["eula"] == {"version": "1.1", "sha256": pro_trial.EULA_SHA256,
                                "accepted_at": "2025-10-09T08:53:20Z"}
    # recorded once: not asked again
    assert pro_trial._eula_gate(ScriptedUi(), None) == ("ok", "")


def test_a_refusal_records_nothing():
    assert pro_trial._eula_gate(ScriptedUi(yes={"Do you accept it?": [False]}), None) == (
        "refused", "")
    assert "eula" not in _state()


def test_a_changed_text_is_asked_again_with_its_notice():
    _set_state(eula={"version": "1.0", "sha256": "0" * 64, "accepted_at": "x"})
    ui = ScriptedUi(yes={"Do you accept it?": [True]})
    assert pro_trial._eula_gate(ui, None)[0] == "ok"
    assert ui.text == ["The Buddhi Review Pro End User Licence Agreement has changed since you "
                       "last accepted it.", _EULA_LINE]
    assert _state()["eula"]["sha256"] == pro_trial.EULA_SHA256


def test_a_run_outside_a_terminal_never_assumes_assent():
    ui = ScriptedUi(interactive=False)
    assert pro_trial._eula_gate(ui, None) == ("no_terminal", "")
    assert ui.asked == [] and "eula" not in _state()


def test_an_assent_that_cannot_be_saved_is_reported(monkeypatch):
    monkeypatch.setattr(pro_trial, "_save_state", lambda path, state: "Permission denied")
    verdict = pro_trial._eula_gate(ScriptedUi(yes={"Do you accept it?": [True]}), None)
    assert verdict == ("unsaved", "Permission denied")


# ── seat-admin.json ───────────────────────────────────────────────────────────

def _seat_admin():
    return json.loads(pro_trial._seat_admin_path().read_text())


def test_the_seat_access_file_is_written_to_the_schema_0600():
    assert pro_trial.save_seat_admin([("sub_1", "S" * 43, 1)]) is None
    path = pro_trial._seat_admin_path()
    assert _seat_admin() == {"schema": 1, "entries": {
        "https://api.buddhireview.com sub_1": {"secret": "S" * 43, "version": 1}}}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir() if p.name.startswith("seat-admin")] == [
        "seat-admin.json"]


def test_the_seat_access_file_keeps_other_entries_and_never_goes_back_a_version():
    pro_trial.save_seat_admin([("sub_1", "A" * 43, 2), ("sub_2", "B" * 43, 1)])
    pro_trial.save_seat_admin([("sub_1", "OLD", 1), ("sub_3", "C" * 43, 4)])
    entries = _seat_admin()["entries"]
    assert entries["https://api.buddhireview.com sub_1"] == {"secret": "A" * 43, "version": 2}
    assert entries["https://api.buddhireview.com sub_2"]["version"] == 1
    assert entries["https://api.buddhireview.com sub_3"]["version"] == 4
    pro_trial.save_seat_admin([("sub_1", "N" * 43, 3)])
    assert _seat_admin()["entries"]["https://api.buddhireview.com sub_1"]["version"] == 3


@pytest.mark.parametrize("content, why", [
    ("{not json", "it is not valid JSON"),
    ('{"schema": "1", "entries": {}}', "it is not a seat access file"),
    ('{"schema": 2, "entries": {}}', "it was written by a newer version of Buddhi Review"),
])
def test_a_seat_access_file_that_cannot_be_read_is_left_as_it_is(content, why):
    path = pro_trial._seat_admin_path()
    path.write_text(content)
    assert pro_trial.save_seat_admin([("sub_1", "S" * 43, 1)]) == ("unreadable", why)
    assert path.read_text() == content


def test_an_unwritable_seat_access_folder_is_reported(monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("BUDDHI_SEAT_ADMIN", str(blocker / "seat-admin.json"))
    kind, why = pro_trial.save_seat_admin([("sub_1", "S" * 43, 1)])
    assert kind == "unwritable" and why


def test_the_seat_access_lines():
    path = pro_trial._seat_admin_path()
    assert pro_trial._seat_admin_line(("unwritable", "Permission denied"), installed=True) == (
        "Buddhi Review Pro is set up, but this computer could not save the access it needs to "
        f"manage your seats ({path}: Permission denied). Pro keeps working here. After you fix "
        "that, run `buddhi-review setup --recover` to get that access again.")
    assert pro_trial._seat_admin_line(("unwritable", "Permission denied"), installed=False) == (
        "This computer could not save the access it needs to manage your seats "
        f"({path}: Permission denied), so no computer can manage them now. After you fix that, "
        "run `buddhi-review setup --recover` to get that access again.")
    assert pro_trial._seat_admin_line(("unreadable", "bad"), installed=True) == (
        f"The seat access file {path} could not be read (bad), so it was left as it is and your "
        "seat access was not saved. Move it aside, then run `buddhi-review setup --recover`.")


# ── The install tail ──────────────────────────────────────────────────────────

def test_write_index_credential_writes_netrc(tmp_path):
    p = tmp_path / ".netrc"
    ok, action = pro_trial.write_index_credential("KEY-XYZ", path=p)
    assert ok and action in ("created", "updated")
    txt = p.read_text(encoding="utf-8")
    assert "machine pypi.pkg.keygen.sh" in txt
    assert "login license" in txt and "password KEY-XYZ" in txt
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_pip_install_command_targets_package_and_plain_index_no_key():
    captured = {}

    def runner(cmd):
        captured["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
    ok, rc, _out = pro_trial.pip_install(runner=runner)
    assert ok and rc == 0
    cmd = captured["cmd"]
    assert "buddhi-review-pro" in cmd                  # the package it installs
    # the index URL carries NO embedded credential (pip reads ~/.netrc)
    idx = cmd[cmd.index("--index-url") + 1]
    assert "@" not in idx and idx.endswith("/simple")


def test_pip_install_disables_interactive_prompts():
    """Setup runs on an inherited TTY: without --no-input a rejected/missing netrc makes
    pip block on its user/password prompt and setup just looks hung until the 600 s
    timeout, instead of failing fast into pip_failed / index_403."""
    captured = {}

    def runner(cmd):
        captured["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
    pro_trial.pip_install(runner=runner)
    assert "--no-input" in captured["cmd"]


def test_pip_install_403_detected():
    def runner(cmd):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="HTTP error 403 Forbidden")
    ok, rc, out = pro_trial.pip_install(runner=runner)
    assert not ok and pro_trial._looks_like_403(rc, out)


def test_pip_install_passes_netrc_env_to_a_runner_that_accepts_it(tmp_path):
    captured = {}

    def runner(cmd, env=None):
        captured["env"] = env
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
    netrc_path = tmp_path / "custom.netrc"
    pro_trial.pip_install(runner=runner, netrc_path=netrc_path)
    assert captured["env"]["NETRC"] == str(netrc_path)


def test_pip_install_legacy_single_arg_runner_is_still_called_with_just_cmd(tmp_path):
    captured = {}

    def runner(cmd):
        captured["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
    ok, rc, _out = pro_trial.pip_install(runner=runner, netrc_path=tmp_path / "custom.netrc")
    assert ok and rc == 0
    assert captured["cmd"][0:2] == [pro_trial.sys.executable, "-m"]


def test_start_daemon_via_discovered_backend():
    fake = FakeProBackend(active=True)
    assert pro_trial.start_daemon(backends=[fake]) is True
    assert fake.started == 1


def test_start_daemon_targets_a_freshly_installed_backend_that_is_not_yet_active():
    fake = FakeProBackend(active=False)
    assert pro_trial.start_daemon(backends=[fake]) is True
    assert fake.started == 1


def test_start_daemon_no_method_is_noop():
    from buddhi_review import backends as _b
    assert pro_trial.start_daemon(backends=[_b.FreeBackend()]) is False


@pytest.mark.parametrize("kind, buyer, line", [
    ("trial", True, "✓ Buddhi Review Pro is active — your free trial is live. The live view is "
                    "starting in the background."),
    ("buy", True, "✓ Buddhi Pro is active — your subscription is active. The live view is "
                  "starting in the background."),
    ("recover", True, "✓ Buddhi Pro is active — your subscription is active. The live view is "
                      "starting in the background."),
    ("activate", False, "✓ Buddhi Review Pro is active on this computer. The live view is "
                        "starting in the background."),
])
def test_the_success_line_names_what_became_active(kind, buyer, line):
    out = pro_trial._finish_install(_KEY, kind=kind, buyer=buyer, **_install_kw())
    assert (out.ok, out.status, out.message) == (True, "active", line)
    state = _state()
    assert "install" not in state and state["last_install_kind"] == kind
    assert ("trial_active_since" in state) is (kind == "trial")


def test_an_unfinished_install_is_recorded_until_it_succeeds():
    out = pro_trial._finish_install(_KEY, kind="buy", **_install_kw(
        runner=lambda cmd, env=None: types.SimpleNamespace(returncode=1, stdout="",
                                                           stderr="boom")))
    assert out.status == "pip_failed"
    assert _state()["install"]["kind"] == "buy"
    assert pro_trial.has_held_work()


def test_the_kept_pip_failure_line_names_the_exact_command(tmp_path):
    netrc = tmp_path / "my netrc"
    out = pro_trial._finish_install(_KEY, netrc_path=netrc, **_install_kw(
        runner=lambda cmd, env=None: types.SimpleNamespace(returncode=1, stdout="", stderr="x")))
    q = pro_trial.shlex.quote
    assert out.message == (
        f"Install did not complete — your license is set up and {netrc} is intact, so just "
        f"re-run: NETRC={q(str(netrc))} {q(pro_trial.sys.executable)} -m pip install --upgrade "
        f"buddhi-review-pro --index-url {q(pro_trial._index_url())} --no-input")


_403 = lambda cmd, env=None: types.SimpleNamespace(returncode=1, stdout="", stderr="403 Forbidden")


def test_the_kept_index_refusal_line_and_the_trials_last_offer_variant(tmp_path):
    kept = pro_trial._finish_install(_KEY, kind="trial", **_install_kw(runner=_403))
    assert kept.message == ("The license index refused the install (403). Your key may not be "
                            "active yet — wait a moment and re-run setup.")
    netrc = tmp_path / ".netrc"
    last = pro_trial._finish_install(_KEY, kind="trial", last_offer=True, netrc_path=netrc,
                                     **_install_kw(runner=_403))
    q = pro_trial.shlex.quote
    assert last.message == (
        "The licence index refused the install (403). Your key may not be active yet. Wait a "
        f"moment, then run NETRC={q(str(netrc))} {q(pro_trial.sys.executable)} -m pip install "
        f"--upgrade buddhi-review-pro --index-url {q(pro_trial._index_url())} --no-input. If it "
        f"still fails, email {_SUPPORT}.")
    paid = pro_trial._finish_install(_KEY, kind="buy", last_offer=True, **_install_kw(runner=_403))
    assert paid.message == kept.message


def test_the_kept_unsafe_entry_line_and_the_trials_last_offer_variant(tmp_path):
    netrc = tmp_path / ".netrc"
    netrc.write_text("machine pypi.pkg.keygen.sh login license password OLD machine x.y login a\n")
    os.chmod(netrc, 0o600)
    kept = pro_trial._finish_install(_KEY, kind="trial", netrc_path=netrc, **_install_kw())
    assert kept.message == (
        f"{netrc} has an entry that could not be safely updated (it shares a line with another "
        "entry) — your new credential was appended instead. Please clean up "
        f"{netrc} by hand, then re-run setup.")
    last = pro_trial._finish_install(_KEY, kind="trial", last_offer=True, netrc_path=netrc,
                                     **_install_kw())
    assert last.message == (
        f"{netrc} has an entry that could not be safely updated (it shares a line with another "
        f"entry), so your new credential was appended instead. Clean up {netrc} by hand. For "
        f"help continuing trial setup, email {_SUPPORT}.")


@pytest.mark.parametrize("kind, last, line", [
    ("trial", False, "Could not write `{n}`. Check the file's permissions, then run "
                     "`buddhi-review setup` again with the same email to continue the trial if "
                     "it is still live."),
    ("trial", True, "Could not write `{n}`. Check the file's permissions. For help continuing "
                    "trial setup, email support@buddhireview.com."),
    ("buy", False, "Could not write {n}. Check the file's permissions, then run `buddhi-review "
                   "setup` again within the next few minutes to finish. If that is too late, "
                   "run `buddhi-review setup --recover` with your recovery address (the email "
                   "you first paid with, unless you changed it; if you paid with two different "
                   "emails, run it once with each)."),
    ("recover", False, "Could not write {n}. Check the file's permissions, then run "
                       "`buddhi-review setup` again within the next few minutes to finish. If "
                       "that is too late, run `buddhi-review setup --recover` with your recovery "
                       "address (the email you first paid with, unless you changed it; if you "
                       "paid with two different emails, run it once with each)."),
    ("activate", False, "Could not write {n}. Check the file's permissions, then run "
                        "`buddhi-review setup --activate` within the next few minutes and paste "
                        "the same code. If that is too late and you bought Pro, run "
                        "`buddhi-review setup --recover` with your recovery address (the email "
                        "you first paid with, unless you changed it; if you paid with two "
                        "different emails, run it once with each); if someone gave you this "
                        "code, ask them for a new one."),
])
def test_a_netrc_that_cannot_be_written(tmp_path, kind, last, line):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    netrc = blocker / ".netrc"
    out = pro_trial._finish_install(_KEY, kind=kind, last_offer=last, netrc_path=netrc,
                                    **_install_kw(runner=lambda *a, **k: pytest.fail("pip ran")))
    assert (out.status, out.message) == ("netrc_failed", line.format(n=netrc))
    assert "install" not in _state()             # no key saved: nothing to finish later


def test_not_activated_lines():
    kw = _install_kw(is_active=lambda: False)
    trial = pro_trial._finish_install(_KEY, kind="trial", **kw)
    assert trial.message == (
        "Pro installed, but it did not activate on this machine — it may have already been "
        "used for a trial. To buy Pro, run `buddhi-review setup --buy`. If you have a setup "
        "code, run `buddhi-review setup --activate`. To move your own seat from another "
        "computer to this one, run `buddhi-review setup --recover`.")
    paid = pro_trial._finish_install(_KEY, kind="buy", **kw)
    assert paid.message == ("Pro installed, but it did not activate on this machine — your key "
                            f"may already be active elsewhere. Contact {_SUPPORT}.")


def test_a_key_is_never_printed_nor_world_readable(tmp_path, monkeypatch):
    netrc = tmp_path / ".netrc"
    lines = []
    for runner in (_ok_runner, _403,
                   lambda cmd, env=None: types.SimpleNamespace(returncode=1, stdout="",
                                                               stderr="x")):
        lines.append(pro_trial._finish_install(_KEY, netrc_path=netrc,
                                               **_install_kw(runner=runner)).message)
    assert all(_KEY not in line for line in lines)
    assert stat.S_IMODE(os.stat(netrc).st_mode) == 0o600
    assert _KEY not in pro_trial._state_path().read_text()


# ── The trial, end to end ─────────────────────────────────────────────────────

_SENT = "If this address can start a trial on this computer, a 6-digit code is on its way " \
        "from Buddhi Review (codes@buddhireview.com). It expires in 10 minutes."
_UPGRADE = ("To buy Pro, run `buddhi-review setup --buy`. If you have a setup code, run "
            "`buddhi-review setup --activate`. To move your own seat from another computer to "
            "this one, run `buddhi-review setup --recover`.")
_OFFER = ("Try Buddhi Review Pro — automation that carries a run like this to the finish on "
          "its own.")
_QUESTION = "Start or continue your free 14-day Pro trial?"
_CODE_PROMPT = "Enter the 6-digit code (press Enter if none arrived): "


def _trial_ui(*, codes=("123456",), email=("me@example.com",), accept=(True,), yes=None,
              offer=(True,)):
    questions = {_QUESTION: list(offer), "Do you accept it?": list(accept)}
    questions.update(yes or {})
    return ScriptedUi(answers={"Email to set up your trial": list(email),
                               "Enter the 6-digit code": list(codes)}, yes=questions)


def _trial_bridge(monkeypatch, *, code=((202, {"status": "sent"}),),
                  trial=((200, {"licence_key": _KEY, "replay_until": "2026-10-18T00:15:00Z"}),)):
    return _bridge(monkeypatch, {"POST /trial/code": list(code), "POST /trial": list(trial)})


def _step(ui, **kw):
    return pro_trial.trial_step(ui, **_install_kw(**kw))


def test_the_trial_runs_from_the_offer_to_an_active_pro(monkeypatch, tmp_path):
    fake = _trial_bridge(monkeypatch)
    ui = _trial_ui()
    assert _step(ui) is False
    assert ui.lines[0] == ("step", _OFFER)
    assert ui.asked[0] == _QUESTION and ui.defaults[_QUESTION] is True
    assert ("step", _EULA_LINE) in ui.lines
    assert ("info", _SENT) in ui.lines
    assert ui.lines[-1] == ("ok", "✓ Buddhi Review Pro is active — your free trial is live. "
                                  "The live view is starting in the background.")
    fp = pro_trial.trial_fingerprint()
    assert fake.bodies("POST /trial/code") == [{"email": "me@example.com", "fingerprint": fp}]
    body = fake.bodies("POST /trial")[0]
    assert body["code"] == "123456" and body["fingerprint"] == fp
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", body["claim_nonce"])
    assert fake.unexpected == []
    state = _state()
    assert state["shown_count"] == 1 and "trial_nonce" not in state
    assert isinstance(state["trial_active_since"], float)
    assert "password TRIALKEY-ABC-123" in (tmp_path / ".netrc").read_text()


def test_the_licence_agreement_comes_before_any_request(monkeypatch):
    fake = _trial_bridge(monkeypatch)
    ui = _trial_ui(accept=(False,))
    _step(ui)
    assert fake.calls == []
    assert ui.lines[-1] == ("warn", "You did not accept the agreement, so trial setup stopped. "
                                    "The free version keeps working. To start the trial, run "
                                    "`buddhi-review setup` again and accept the agreement.")
    assert pro_trial.offer_allowed()             # build rule 1: offered again at once


def test_on_the_last_offer_the_refusal_points_nowhere_further(monkeypatch):
    _set_state(shown_count=4)
    _trial_bridge(monkeypatch)
    ui = _trial_ui(accept=(False,))
    _step(ui)
    assert ui.lines[-1] == ("warn", "You did not accept the agreement, so trial setup stopped. "
                                    "The free version keeps working.")
    assert not pro_trial.offer_allowed()         # the cap: five offers


def test_a_no_to_the_offer_is_a_durable_decline(monkeypatch):
    fake = _trial_bridge(monkeypatch)
    ui = _trial_ui(offer=(False,))
    assert _step(ui) is False
    assert fake.calls == [] and _state()["declined"] is True
    assert not pro_trial.offer_allowed()


def test_an_empty_email_ends_the_attempt_quietly_and_waives_the_gap(monkeypatch):
    fake = _trial_bridge(monkeypatch)
    ui = _trial_ui(email=("",))
    assert _step(ui) is False
    assert fake.calls == [] and ui.lines == [("step", _OFFER)]
    assert "last_shown" not in _state() and pro_trial.offer_allowed()


def test_an_address_the_service_would_refuse_is_asked_again(monkeypatch):
    _trial_bridge(monkeypatch)
    ui = _trial_ui(email=("+tag@example.com", "nope", "a@b"))
    _step(ui)
    assert ui.text.count("That doesn't look like an email address — try again.") == 3


def test_an_address_that_cannot_take_mail_is_asked_for_again(monkeypatch):
    fake = _trial_bridge(monkeypatch, code=((422, {"error": "send_failed"}),
                                            (202, {"status": "sent"})))
    ui = _trial_ui(email=("bad@example.com", "good@example.com"))
    _step(ui)
    assert ("warn", "We could not send to that address. Check it and try again.") in ui.lines
    assert [b["email"] for b in fake.bodies("POST /trial/code")] == [
        "bad@example.com", "good@example.com"]
    assert ui.lines[-1][0] == "ok"


@pytest.mark.parametrize("answer, line, last_line", [
    ((429, {"error": "rate_limited"}, 120),
     "Too many requests have been made for now. Try again in about 2 minutes with "
     "`buddhi-review setup`.",
     "This trial setup could not finish because too many requests have been made for now."),
    ((503, {"error": "sending_paused"}, 3 * 3600),
     "Trial codes cannot be sent right now. Try again in about 3 hours with "
     "`buddhi-review setup`.",
     "Trial codes cannot be sent right now, so this trial setup could not finish."),
    ((503, {"error": "unavailable"}, 5),
     "The Buddhi Review service is not answering right now. Try again in a few minutes with "
     "`buddhi-review setup`.",
     "The Buddhi Review service is not answering right now, so this trial setup could not "
     "finish."),
    ((400, {"error": "invalid_request"}),
     "Buddhi Review did not accept this request. This points to a fault in this version of "
     "Buddhi Review, not in anything you did. Run `buddhi-review upgrade`, then try again. If "
     "it still fails, email support@buddhireview.com.",
     "Buddhi Review did not accept this request. This points to a fault in this version of "
     "Buddhi Review, not in anything you did. Run `buddhi-review upgrade`, and for help "
     "continuing trial setup, email support@buddhireview.com."),
])
@pytest.mark.parametrize("last", [False, True], ids=["another-offer", "last-offer"])
def test_a_code_request_that_cannot_go_out(monkeypatch, answer, line, last_line, last):
    if last:
        _set_state(shown_count=4)
    fake = _trial_bridge(monkeypatch, code=(answer,))
    ui = _trial_ui(codes=())
    _step(ui)
    assert ui.lines[-1] == ("warn", last_line if last else line)
    assert fake.count("POST /trial") == 0


def test_a_code_of_the_wrong_shape_is_asked_for_again(monkeypatch):
    fake = _trial_bridge(monkeypatch)
    ui = _trial_ui(codes=("12345", "abcdef", "123 456"))
    _step(ui)
    assert ui.text.count("That is not a trial code. A trial code has 6 digits.") == 2
    assert fake.bodies("POST /trial")[0]["code"] == "123456"


def test_no_code_yet_offers_a_new_one_default_no(monkeypatch):
    fake = _trial_bridge(monkeypatch)
    ui = _trial_ui(codes=("", "123456"), yes={"Send a new code?": [True]})
    _step(ui)
    assert ("info", "No code yet? It can take a few minutes, so check your spam folder. If an "
                    "email says this address or this computer has already used its trial, no "
                    "code will come.") in ui.lines
    assert ui.defaults["Send a new code?"] is False
    assert fake.count("POST /trial/code") == 2 and ui.lines[-1][0] == "ok"


@pytest.mark.parametrize("last, line", [
    (False, "Trial setup stopped. To try again, run `buddhi-review setup`."),
    (True, "Trial setup stopped."),
])
def test_no_new_code_wanted_stops_then_points_at_the_paid_paths(monkeypatch, last, line):
    if last:
        _set_state(shown_count=4)
    _trial_bridge(monkeypatch)
    ui = _trial_ui(codes=("",), yes={"Send a new code?": [False]})
    _step(ui)
    assert ui.lines[-2:] == [("info", line), ("step", _UPGRADE)]


def test_a_code_that_does_not_work_offers_a_new_one_default_yes(monkeypatch):
    fake = _trial_bridge(monkeypatch, trial=((403, {"error": "code_invalid"}),
                                             (200, {"licence_key": _KEY})))
    ui = _trial_ui(codes=("111111", "222222"), yes={"Send a new code?": [True]})
    _step(ui)
    assert ("warn", "That code is not valid. It may be mistyped or expired, or too many wrong "
                    "codes were entered for this address in the last 24 hours, and then a new "
                    "code also fails until that time has passed. Ask for a new one.") in ui.lines
    assert ui.defaults["Send a new code?"] is True
    assert fake.count("POST /trial/code") == 2 and ui.lines[-1][0] == "ok"


def test_an_ended_trial_installs_nothing_and_is_never_offered_again(monkeypatch, tmp_path):
    _trial_bridge(monkeypatch, trial=((410, {"error": "trial_ended"}),))
    ui = _trial_ui()
    _step(ui, runner=lambda *a, **k: pytest.fail("installed"))
    assert ui.lines[-2:] == [("info", "Your Buddhi Review Pro trial has ended. The free version "
                                      "is still available."), ("step", _UPGRADE)]
    assert _state()["declined"] is True and "trial_nonce" not in _state()
    assert not (tmp_path / ".netrc").exists()
    assert not pro_trial.offer_allowed()


@pytest.mark.parametrize("last, line", [
    (False, "The trial setup could not finish. To try again, run `buddhi-review setup` in a few "
            "minutes, using the same email. If a trial was started and is still live, setup "
            "will try to continue it."),
    (True, "The trial setup could not finish. For help continuing trial setup, email "
           "support@buddhireview.com."),
])
def test_no_answer_from_trial_after_retries_with_the_same_nonce(monkeypatch, last, line):
    if last:
        _set_state(shown_count=4)
    fake = _trial_bridge(monkeypatch, trial=((503, {"error": "unavailable"}, 5),))
    ui = _trial_ui()
    _step(ui)
    assert ui.lines[-1] == ("warn", line)
    nonces = {b["claim_nonce"] for b in fake.bodies("POST /trial")}
    assert fake.count("POST /trial") == pro_trial._NO_ANSWER_TRIES and len(nonces) == 1
    assert _state()["trial_nonce"] in nonces      # kept for the next attempt


def test_the_trial_nonce_is_kept_across_attempts(monkeypatch):
    fake = _trial_bridge(monkeypatch, trial=((503, {"error": "unavailable"}),
                                             (503, {"error": "unavailable"}),
                                             (503, {"error": "unavailable"}),
                                             (200, {"licence_key": _KEY})))
    _step(_trial_ui())
    _step(_trial_ui())
    assert len({b["claim_nonce"] for b in fake.bodies("POST /trial")}) == 1


@pytest.mark.parametrize("where", ["question", "email", "code", "in-flight"])
@pytest.mark.parametrize("last", [False, True], ids=["another-offer", "last-offer"])
def test_ctrl_c_anywhere_in_the_trial_step(monkeypatch, where, last):
    if last:
        _set_state(shown_count=4)
    trial = [KeyboardInterrupt()] if where == "in-flight" else [(200, {"licence_key": _KEY})]
    _trial_bridge(monkeypatch, trial=trial)
    ui = _trial_ui(offer=[KeyboardInterrupt] if where == "question" else (True,),
                   email=[EOFError] if where == "email" else ("me@example.com",),
                   codes=[KeyboardInterrupt] if where == "code" else ("123456",))
    assert _step(ui) is True
    assert ui.lines[-1] == ("info", "Trial setup stopped." if last else
                            "Trial setup stopped. To try again, run `buddhi-review setup`.")
    assert "last_shown" not in _state()          # nothing started the gap


@pytest.mark.parametrize("last", [False, True], ids=["another-offer", "last-offer"])
def test_trial_details_that_cannot_be_saved(monkeypatch, last):
    if last:
        _set_state(shown_count=4, eula=_eula_accepted())
    else:
        _set_state(eula=_eula_accepted())
    real = pro_trial._save_state
    monkeypatch.setattr(pro_trial, "_save_state", lambda path, state: "Permission denied"
                        if "trial_nonce" in state else real(path, state))
    fake = _trial_bridge(monkeypatch)
    ui = _trial_ui(accept=())
    _step(ui)
    path = pro_trial._state_path()
    assert ui.lines[-1] == ("warn", (
        f"Buddhi Review could not save its trial details in {path} (Permission denied), so "
        "this trial setup could not finish. Fix that folder's permissions. For help continuing "
        "trial setup, email support@buddhireview.com.") if last else (
        f"Buddhi Review could not save its trial details in {path} (Permission denied), so "
        "this trial setup could not finish. Fix that folder's permissions, then run "
        "`buddhi-review setup` again."))
    assert fake.calls == []


def test_the_last_offer_is_the_fifth(monkeypatch):
    for shown in range(4):
        _set_state(shown_count=shown)
        assert pro_trial.record_offer_shown() is False
    _set_state(shown_count=4)
    assert pro_trial.record_offer_shown() is True


# ── The offer's gating ────────────────────────────────────────────────────────

def test_offer_allowed_default():
    assert pro_trial.offer_allowed()


def test_offer_suppressed_by_no_upsell(monkeypatch):
    monkeypatch.setenv("BUDDHI_NO_UPSELL", "1")
    assert not pro_trial.offer_allowed()


def test_offer_suppressed_when_pro_active(monkeypatch):
    monkeypatch.setattr(pro_trial, "pro_backend_active", lambda **k: True)
    assert not pro_trial.offer_allowed()


def test_offer_suppressed_after_durable_decline():
    pro_trial.record_declined()
    assert not pro_trial.offer_allowed()


def test_offer_frequency_cap_and_gap():
    now = time.time()
    _set_state(shown_count=1, last_shown=now)
    assert not pro_trial.offer_allowed(now=now + 60)          # the 24 h gap
    assert pro_trial.offer_allowed(now=now + 25 * 3600)
    _set_state(shown_count=5)
    assert not pro_trial.offer_allowed()


def test_an_attempt_without_a_no_is_offered_again_on_the_very_next_setup(monkeypatch):
    """Build rule 1: the attempt counted toward the cap, but no gap."""
    _trial_bridge(monkeypatch, trial=((403, {"error": "code_invalid"}),))
    _step(_trial_ui(yes={"Send a new code?": [False]}))
    assert _state()["shown_count"] == 1
    assert pro_trial.offer_allowed(now=time.time() + 1)


def test_fourteen_days_after_a_trial_became_active_it_is_never_offered_here_again():
    now = time.time()
    _set_state(trial_active_since=now - 13 * 86400)
    assert pro_trial.offer_allowed(now=now)                   # a reinstall mid-trial
    assert not pro_trial.offer_allowed(now=now + 86400)


def test_the_trial_start_is_recorded_only_when_the_success_line_shows(monkeypatch):
    _trial_bridge(monkeypatch)
    _step(_trial_ui(), runner=_403)
    assert "trial_active_since" not in _state()
    assert _state()["install"]["kind"] == "trial"             # the next setup finishes it


def test_off_a_terminal_the_step_is_silent(monkeypatch):
    fake = _trial_bridge(monkeypatch)
    ui = ScriptedUi(interactive=False)
    assert _step(ui) is False
    assert ui.lines == [] and fake.calls == [] and _state() == {}


def test_a_trial_answer_without_a_key_is_no_answer(monkeypatch):
    _trial_bridge(monkeypatch, trial=((200, {"replay_until": "x"}),))
    ui = _trial_ui()
    _step(ui, runner=lambda *a, **k: pytest.fail("installed"))
    assert ui.lines[-1] == ("warn", "The trial setup could not finish. To try again, run "
                                    "`buddhi-review setup` in a few minutes, using the same "
                                    "email. If a trial was started and is still live, setup will "
                                    "try to continue it.")


def test_the_activation_order_lives_in_one_function(monkeypatch):
    """Probe P1 decides whether activation must come before the install; until then
    the order is today's — install, then the paid package's own first start activates
    — and it is decided in exactly one place."""
    order = []
    monkeypatch.setattr(pro_trial, "pip_install",
                        lambda **k: order.append("install") or (True, 0, ""))
    monkeypatch.setattr(pro_trial, "start_daemon", lambda **k: order.append("activate"))
    monkeypatch.setattr(pro_trial, "_await_active", lambda **k: True)
    assert pro_trial._install_and_activate(pro_trial._state_path()) == (True, 0, "", True)
    assert order == ["install", "activate"]
    out = pro_trial._finish_install(_KEY, kind="buy")
    assert out.ok and order == ["install", "activate"] * 2


def test_a_continued_trial_keeps_its_first_start(monkeypatch):
    """Build rule 12 counts 14 days from the FIRST time a trial became active here: a
    reinstall that continues it on day 13 must not restart the count."""
    first = time.time() - 13 * 86400
    _set_state(trial_active_since=first)
    out = pro_trial._finish_install(_KEY, kind="trial", **_install_kw())
    assert out.ok and _state()["trial_active_since"] == first
    assert not pro_trial.offer_allowed(now=first + 15 * 86400)
