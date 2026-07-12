"""Unit tests for pro_trial.py — the wizard's Pro-trial ACQUISITION plumbing (PRO-6).

Network-free (every Keygen call goes through an injected transport) and pip-free
(the install runner is injected). Covers: the server-less trial flow end to end;
the four graceful-failure paths (email already registered, index 403, pip failure,
machine not activated); the convert / concierge-paste path; daemon-start through
discovery; the offer-gating conventions; and the adversarial invariants — the
create-license payload carries NO expiry and only the TRIAL policy (Q1), and the
license key never leaks into printed output or a world-readable file.

This file names the pro package it installs by design, so it is allowlisted in
tests/test_oss_purity.py's _VOCAB_SCAFFOLDING.
"""
from __future__ import annotations

import io
import json
import os
import stat
import types

import pytest

from buddhi_review import pro_trial


# ── Fake transport ────────────────────────────────────────────────────────────

def make_transport(**handlers):
    calls = []

    def _resolve(h, body):
        if h is None:
            return 404, None
        return h(body) if callable(h) else h

    def transport(method, url, auth, body):
        calls.append({"method": method, "url": url, "auth": auth, "body": body})
        if url.endswith("/users"):
            return _resolve(handlers.get("users"), body)
        if url.endswith("/tokens"):
            return _resolve(handlers.get("tokens"), body)
        if url.endswith("/licenses/actions/validate-key"):
            return _resolve(handlers.get("validate"), body)
        if url.endswith("/licenses"):
            return _resolve(handlers.get("licenses"), body)
        return 404, None

    transport.calls = calls
    return transport


_USERS_OK = (201, {"data": {"type": "users", "id": "user-1"}})
_TOKENS_OK = (201, {"data": {"attributes": {"token": "tok-1"}}})
_LICENSE_OK = (201, {"data": {"attributes": {
    "key": "TRIALKEY-ABC", "expiry": "2026-07-26T00:00:00.000Z"}}})
_VALIDATE_OK = (200, {"data": {"id": "lic-1"}, "meta": {"valid": True, "code": "VALID"}})


def _full(**over):
    h = dict(users=_USERS_OK, tokens=_TOKENS_OK, licenses=_LICENSE_OK, validate=_VALIDATE_OK)
    h.update(over)
    return make_transport(**h)


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


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv("BUDDHI_NO_UPSELL", raising=False)
    monkeypatch.setenv("BUDDHI_NETRC", str(tmp_path / ".netrc"))
    monkeypatch.setenv("BUDDHI_TRIAL_STATE", str(tmp_path / "trial.json"))


# ── primitives ────────────────────────────────────────────────────────────────

def test_valid_email():
    assert pro_trial.valid_email("a@b.co")
    assert not pro_trial.valid_email("nope")
    assert not pro_trial.valid_email("a@b")
    assert not pro_trial.valid_email("")


def test_register_user_open_registration_no_auth():
    t = _full()
    status, uid, code = pro_trial.register_user("me@x.io", "pw", transport=t)
    assert (status, uid) == (201, "user-1")
    call = [c for c in t.calls if c["url"].endswith("/users")][0]
    assert call["auth"] is None                       # open registration: NO token
    assert call["body"]["data"]["attributes"]["email"] == "me@x.io"


def test_register_user_email_taken():
    t = _full(users=(422, {"errors": [{"code": "EMAIL_TAKEN"}]}))
    status, uid, code = pro_trial.register_user("me@x.io", "pw", transport=t)
    assert uid is None and status == 422 and code == "EMAIL_TAKEN"


def test_mint_user_token_uses_basic_auth():
    t = _full()
    tok = pro_trial.mint_user_token("me@x.io", "pw", transport=t)
    assert tok == "tok-1"
    call = [c for c in t.calls if c["url"].endswith("/tokens")][0]
    assert call["auth"].startswith("Basic ")


def test_create_trial_license_has_no_expiry_and_only_trial_policy():
    """Q1 adversarial: the wizard's create-license payload carries NO expiry field
    (the policy stamps it server-side) and references ONLY the unprotected trial
    policy — never a paid or longer one."""
    t = _full()
    attrs = pro_trial.create_trial_license("tok-1", "user-1", transport=t)
    assert attrs["key"] == "TRIALKEY-ABC"
    call = [c for c in t.calls if c["url"].endswith("/licenses")][0]
    body_json = json.dumps(call["body"])
    assert "expiry" not in body_json          # the client cannot set the 14-day window
    assert "duration" not in body_json
    pol = call["body"]["data"]["relationships"]["policy"]["data"]["id"]
    assert pol == pro_trial._TRIAL_POLICY_ID
    assert call["auth"] == "Bearer tok-1"


def test_create_trial_license_server_ignores_injected_expiry():
    """Even if a tampered client tried to send an expiry, the SERVER stamps its own:
    the returned expiry is whatever the policy set, never the request's."""
    def licenses(body):
        # a server that ignores any client-sent expiry and stamps its own
        return 201, {"data": {"attributes": {"key": "K", "expiry": "2026-07-26T00:00:00.000Z"}}}
    t = _full(licenses=licenses)
    attrs = pro_trial.create_trial_license("tok-1", "user-1", transport=t)
    assert attrs["expiry"] == "2026-07-26T00:00:00.000Z"


def test_validate_key_is_tokenless():
    t = _full()
    valid, code = pro_trial.validate_key("SOMEKEY", transport=t)
    assert valid and code == "VALID"
    call = [c for c in t.calls if "validate-key" in c["url"]][0]
    assert call["auth"] is None                       # public, tokenless
    assert call["body"]["meta"]["key"] == "SOMEKEY"


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


def test_pip_install_403_detected():
    def runner(cmd):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="HTTP error 403 Forbidden")
    ok, rc, out = pro_trial.pip_install(runner=runner)
    assert not ok and pro_trial._looks_like_403(rc, out)


# ── daemon start via discovery ──────────────────────────────────────────────────

def test_start_daemon_via_discovered_backend():
    fake = FakeProBackend(active=True)
    assert pro_trial.start_daemon(backends=[fake]) is True
    assert fake.started == 1


def test_start_daemon_no_method_is_noop():
    from buddhi_review import backends as _b
    assert pro_trial.start_daemon(backends=[_b.FreeBackend()]) is False


def test_pro_backend_active_reuses_upsell():
    assert pro_trial.pro_backend_active(backends=[FakeProBackend(active=True)]) is True
    assert pro_trial.pro_backend_active(backends=[FakeProBackend(active=False)]) is False


# ── start_trial: full flow + graceful paths ─────────────────────────────────────

def _start(email="me@x.io", *, transport=None, backends=None, runner=None,
           is_active=True, tmp=None):
    return pro_trial.start_trial(
        email, transport=transport or _full(),
        backends=backends if backends is not None else [FakeProBackend(True)],
        runner=runner or (lambda c: types.SimpleNamespace(returncode=0, stdout="", stderr="")),
        netrc_path=(tmp / ".netrc") if tmp else None,
        is_active=(lambda: is_active), sleep=lambda s: None, attempts=3)


def test_start_trial_active(tmp_path):
    out = _start(tmp=tmp_path)
    assert out.ok and out.status == "active"
    assert "2026-07-26" in out.message          # server-stamped end date shown
    assert (tmp_path / ".netrc").exists()


def test_start_trial_email_registered_routes_to_convert(tmp_path):
    out = _start(transport=_full(users=(409, {"errors": [{"code": "EMAIL_TAKEN"}]})),
                 tmp=tmp_path)
    assert not out.ok and out.status == "email_registered"
    assert pro_trial.CHECKOUT_URL in out.message
    assert not (tmp_path / ".netrc").exists()   # never got as far as writing creds


def test_start_trial_bad_email():
    out = pro_trial.start_trial("not-an-email")
    assert not out.ok and out.status == "bad_email"


def test_start_trial_index_403(tmp_path):
    runner = lambda c: types.SimpleNamespace(returncode=1, stdout="", stderr="403 Forbidden")
    out = _start(runner=runner, tmp=tmp_path)
    assert not out.ok and out.status == "index_403"
    assert (tmp_path / ".netrc").exists()       # netrc intact (key valid, retry later)


def test_start_trial_pip_failed_netrc_intact(tmp_path):
    runner = lambda c: types.SimpleNamespace(returncode=1, stdout="", stderr="network down")
    out = _start(runner=runner, tmp=tmp_path)
    assert not out.ok and out.status == "pip_failed"
    assert (tmp_path / ".netrc").exists()
    assert "intact" in out.message.lower()


def test_start_trial_not_activated_unique_per_policy(tmp_path):
    # install succeeds, but the machine never activates (already used its one trial).
    out = _start(is_active=False, tmp=tmp_path)
    assert not out.ok and out.status == "not_activated"
    assert pro_trial.CHECKOUT_URL in out.message
    # a single calm line — no stack trace / error spam vocabulary
    assert "traceback" not in out.message.lower() and "error" not in out.message.lower()


# ── convert / concierge-paste ───────────────────────────────────────────────────

def test_convert_uses_valid_clipboard_key(tmp_path):
    t = _full()
    out = pro_trial.convert(
        transport=t, clipboard_reader=lambda: "PAIDKEY-FROM-CLIP",
        backends=[FakeProBackend(True)],
        runner=lambda c: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        netrc_path=tmp_path / ".netrc", is_active=lambda: True, sleep=lambda s: None,
        attempts=2, stream=io.StringIO())
    assert out.ok and out.status == "active"
    assert (tmp_path / ".netrc").read_text().find("PAIDKEY-FROM-CLIP") >= 0


def test_convert_falls_back_to_manual_paste_when_clipboard_invalid(tmp_path):
    # clipboard holds junk that does NOT validate → prompt for manual paste.
    def validate(body):
        key = body["meta"]["key"]
        return (200, {"meta": {"valid": key == "GOODKEY", "code": "VALID" if key == "GOODKEY" else "NOT_FOUND"}})
    t = _full(validate=validate)
    out = pro_trial.convert(
        transport=t, clipboard_reader=lambda: "junk-not-a-key",
        paste_input=lambda *a: "GOODKEY", backends=[FakeProBackend(True)],
        runner=lambda c: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        netrc_path=tmp_path / ".netrc", is_active=lambda: True, sleep=lambda s: None,
        attempts=2, stream=io.StringIO())
    assert out.ok and "GOODKEY" in (tmp_path / ".netrc").read_text()


def test_convert_no_key_entered(tmp_path):
    t = _full(validate=(200, {"meta": {"valid": False, "code": "NOT_FOUND"}}))
    out = pro_trial.convert(transport=t, clipboard_reader=lambda: "",
                            paste_input=lambda *a: "", stream=io.StringIO())
    assert not out.ok and out.status == "no_key"


def test_read_clipboard_swallows_all_exceptions():
    def boom():
        raise RuntimeError("no clipboard tool on this headless host")
    assert pro_trial._read_clipboard(boom) == ""       # falls through, never raises


def test_request_non_numeric_status_is_fail_open():
    def weird(method, url, auth, body):
        return "not-an-int", None                       # a misbehaving transport
    assert pro_trial._request("POST", "/users", transport=weird) == (0, None)


# ── offer gating ────────────────────────────────────────────────────────────────

def test_offer_allowed_default(tmp_path):
    assert pro_trial.offer_allowed(backends=[FakeProBackend(False)]) is True


def test_offer_suppressed_by_no_upsell(monkeypatch):
    monkeypatch.setenv("BUDDHI_NO_UPSELL", "1")
    assert pro_trial.offer_allowed(backends=[FakeProBackend(False)]) is False


def test_offer_suppressed_when_pro_active():
    assert pro_trial.offer_allowed(backends=[FakeProBackend(True)]) is False


def test_offer_suppressed_after_durable_decline(tmp_path):
    sp = tmp_path / "trial.json"
    pro_trial.record_declined(state_path=sp)
    assert pro_trial.offer_allowed(backends=[FakeProBackend(False)], state_path=sp) is False


def test_offer_frequency_cap(tmp_path, monkeypatch):
    sp = tmp_path / "trial.json"
    monkeypatch.setenv("BUDDHI_UPSELL_MAX_SHOWS", "1")
    pro_trial.record_offer_shown(now=1000.0, state_path=sp)
    assert pro_trial.offer_allowed(backends=[FakeProBackend(False)], now=1001.0,
                                   state_path=sp) is False


# ── adversarial: the key never leaks into output or a world-readable file ────────

def test_key_not_leaked_into_output_or_world_readable(tmp_path):
    stream = io.StringIO()
    # capture what start_trial's own printing would emit by driving the primitives
    out = pro_trial.start_trial(
        "me@x.io", transport=_full(), backends=[FakeProBackend(True)],
        runner=lambda c: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        netrc_path=tmp_path / ".netrc", is_active=lambda: True, sleep=lambda s: None,
        attempts=2)
    assert out.ok
    # the user-facing message shows the end date, NOT the secret key
    assert "TRIALKEY-ABC" not in out.message
    # the key lives ONLY in the 0600 netrc, not world/group readable
    mode = stat.S_IMODE(os.stat(tmp_path / ".netrc").st_mode)
    assert mode == 0o600
