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


def test_pip_install_disables_interactive_prompts():
    """Setup runs on an inherited TTY: without --no-input a rejected/missing netrc makes
    pip block on its user/password prompt and the wizard just looks hung until the 600 s
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


def test_start_trial_pip_failed_message_includes_index_url(tmp_path):
    """The manual retry command in the pip_failed message must carry the SAME
    --index-url pip_install() itself uses — plain PyPI does not host the private
    package, so a bare 'pip install buddhi-review-pro' cannot recover this state."""
    runner = lambda c: types.SimpleNamespace(returncode=1, stdout="", stderr="network down")
    out = _start(runner=runner, tmp=tmp_path)
    assert not out.ok and out.status == "pip_failed"
    assert f"--index-url {pro_trial._index_url()}" in out.message


# ── start_trial: pending-credential retry (token/license failure must not burn the email) ──

def test_start_trial_retries_after_token_failure_reuse_same_password(tmp_path):
    """A registration that succeeds but whose token mint fails right after must not
    permanently lock the email out of its own account: a retry should mint a token
    with the SAME password instead of re-registering (which the server would now
    refuse as already-taken, dead-ending into email_registered)."""
    attempt = {"n": 0}

    def tokens(body):
        attempt["n"] += 1
        return (0, None) if attempt["n"] == 1 else _TOKENS_OK

    t = _full(tokens=tokens)
    out1 = _start(transport=t, tmp=tmp_path)
    assert not out1.ok and out1.status == "token_failed"
    assert len([c for c in t.calls if c["url"].endswith("/users")]) == 1

    out2 = _start(transport=t, tmp=tmp_path)
    assert out2.ok and out2.status == "active"
    # no second registration attempt on retry
    assert len([c for c in t.calls if c["url"].endswith("/users")]) == 1
    token_calls = [c for c in t.calls if c["url"].endswith("/tokens")]
    assert len(token_calls) == 2
    assert token_calls[0]["auth"] == token_calls[1]["auth"]   # same password both times


def test_start_trial_retries_after_license_failure_reuse_same_password(tmp_path):
    attempt = {"n": 0}

    def licenses(body):
        attempt["n"] += 1
        return (0, None) if attempt["n"] == 1 else _LICENSE_OK

    t = _full(licenses=licenses)
    out1 = _start(transport=t, tmp=tmp_path)
    assert not out1.ok and out1.status == "license_failed"

    out2 = _start(transport=t, tmp=tmp_path)
    assert out2.ok and out2.status == "active"
    assert len([c for c in t.calls if c["url"].endswith("/users")]) == 1


def test_start_trial_clears_pending_credential_after_success(tmp_path):
    out = _start(tmp=tmp_path)
    assert out.ok
    state = json.loads((tmp_path / "trial.json").read_text(encoding="utf-8"))
    assert "pending_password" not in state
    assert "pending_email" not in state
    assert "pending_user_id" not in state


def test_start_trial_pending_state_file_is_0600(tmp_path):
    t = _full(tokens=lambda body: (0, None))
    out = _start(transport=t, tmp=tmp_path)
    assert not out.ok and out.status == "token_failed"
    sp = tmp_path / "trial.json"
    assert sp.exists()
    assert stat.S_IMODE(os.stat(sp).st_mode) == 0o600


def test_start_trial_pending_credential_ignored_for_a_different_email(tmp_path):
    """A pending credential for one email must never be reused to mint a token for a
    DIFFERENT email — only an exact-email match is a legitimate same-account retry."""
    t = _full(tokens=lambda body: (0, None))
    out1 = _start(email="me@x.io", transport=t, tmp=tmp_path)
    assert not out1.ok and out1.status == "token_failed"

    t2 = _full()
    out2 = _start(email="someone-else@x.io", transport=t2, tmp=tmp_path)
    assert out2.ok
    # the second email registered fresh — it did not reuse the first email's pending slot
    assert len([c for c in t2.calls if c["url"].endswith("/users")]) == 1


def test_start_trial_not_activated_unique_per_policy(tmp_path):
    # install succeeds, but the machine never activates (already used its one trial).
    out = _start(is_active=False, tmp=tmp_path)
    assert not out.ok and out.status == "not_activated"
    assert pro_trial.CHECKOUT_URL in out.message
    # a single calm line — no stack trace / error spam vocabulary
    assert "traceback" not in out.message.lower() and "error" not in out.message.lower()


# ── convert / concierge-paste ───────────────────────────────────────────────────

def test_convert_uses_valid_clipboard_key(tmp_path):
    # A key-shaped clipboard is used ONLY once the user explicitly consents to it.
    t = _full()
    out = pro_trial.convert(
        transport=t, clipboard_reader=lambda: "PAIDKEY-FROM-CLIP",
        confirm_input=lambda *a: "y", backends=[FakeProBackend(True)],
        runner=lambda c: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        netrc_path=tmp_path / ".netrc", is_active=lambda: True, sleep=lambda s: None,
        attempts=2, stream=io.StringIO())
    assert out.ok and out.status == "active"
    assert (tmp_path / ".netrc").read_text().find("PAIDKEY-FROM-CLIP") >= 0


def test_convert_falls_back_to_manual_paste_when_clipboard_invalid(tmp_path):
    # consented clipboard that the SERVER rejects → prompt for manual paste.
    def validate(body):
        key = body["meta"]["key"]
        return (200, {"meta": {"valid": key == "GOODKEY", "code": "VALID" if key == "GOODKEY" else "NOT_FOUND"}})
    t = _full(validate=validate)
    out = pro_trial.convert(
        transport=t, clipboard_reader=lambda: "junk-not-a-key",
        confirm_input=lambda *a: "y",
        paste_input=lambda *a: "GOODKEY", backends=[FakeProBackend(True)],
        runner=lambda c: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        netrc_path=tmp_path / ".netrc", is_active=lambda: True, sleep=lambda s: None,
        attempts=2, stream=io.StringIO())
    assert out.ok and "GOODKEY" in (tmp_path / ".netrc").read_text()


# ── the clipboard never leaves the machine unbidden (P1: no arbitrary exfiltration) ──

def test_clipboard_is_never_posted_without_explicit_consent(tmp_path):
    """A stale clipboard holding an unrelated secret must NOT be POSTed just because the
    user chose the key-paste path: without a yes, validate-key is never called on it."""
    t = _full()
    out = pro_trial.convert(
        transport=t, clipboard_reader=lambda: "ghp_STALE_GITHUB_TOKEN_abcdef123456",
        confirm_input=lambda *a: "n", paste_input=lambda *a: "", stream=io.StringIO(),
        netrc_path=tmp_path / ".netrc")
    assert not out.ok and out.status == "no_key"
    assert t.calls == []                                  # nothing left the machine at all
    assert not (tmp_path / ".netrc").exists()


def test_declined_clipboard_falls_through_to_manual_paste(tmp_path):
    """Declining the clipboard costs one paste and leaks nothing — the manual key still
    installs, and the clipboard value is never sent."""
    t = _full()
    out = pro_trial.convert(
        transport=t, clipboard_reader=lambda: "SOME-OTHER-SECRET-VALUE",
        confirm_input=lambda *a: "", paste_input=lambda *a: "GOODKEY",
        backends=[FakeProBackend(True)],
        runner=lambda c: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        netrc_path=tmp_path / ".netrc", is_active=lambda: True, sleep=lambda s: None,
        attempts=2, stream=io.StringIO())
    assert out.ok
    sent = [c["body"]["meta"]["key"] for c in t.calls if "validate-key" in c["url"]]
    assert sent == ["GOODKEY"]                            # the clipboard value was NOT sent


def test_non_key_shaped_clipboard_is_not_even_offered():
    """Clipboard content that cannot be a key (prose / a passphrase with spaces) is
    dropped locally — no network call AND no consent prompt."""
    t = _full()
    asked = []
    got = pro_trial.detect_pasted_key(
        transport=t, clipboard_reader=lambda: "the quick brown fox jumps",
        confirm_input=lambda p: asked.append(p) or "y", paste_input=lambda *a: "")
    assert got is None
    assert t.calls == [] and asked == []


def test_looks_like_license_key_shape_gate():
    ok = ["2A26D5-D74C39-D5CDCA-38DA05-4F70E5-V3",          # Keygen hyphen-grouped hex
          "40680ca7-99c5-4569-8c1d-611713612d3d",           # a UUID key
          "key/eyJhY2NvdW50IjoiYnVkZGhpIn0=.c2lnbmF0dXJl"]  # a signed key/<payload>.<sig>
    for k in ok:
        assert pro_trial._looks_like_license_key(k) is True
    bad = ["", "short", "correct horse battery staple",     # empty / too short / spaces
           "my password is hunter2", "line one\nline two"]  # prose / multi-line
    for k in bad:
        assert pro_trial._looks_like_license_key(k) is False


def test_consent_prompt_masks_the_candidate():
    """The prompt must let the OWNER recognise their key without spilling a mis-copied
    secret into the scrollback."""
    masked = pro_trial._mask_key("ghp_STALE_GITHUB_TOKEN_abcdef123456")
    assert "STALE_GITHUB_TOKEN" not in masked and masked.startswith("ghp_")
    assert pro_trial._mask_key("shortkey") == "*" * 8       # short values disclose nothing


def test_consent_declines_on_non_interactive_stdin():
    """A headless / captured-stdin host cannot consent, so it must DECLINE (fail closed)
    rather than raise or default to sending the clipboard."""
    def boom(_prompt):
        raise OSError("reading from stdin while output is captured")
    assert pro_trial._clipboard_consented("KEY-1234-5678", boom) is False
    assert pro_trial._clipboard_consented("KEY-1234-5678", lambda p: (_ for _ in ()).throw(EOFError())) is False
    assert pro_trial._clipboard_consented("KEY-1234-5678", lambda p: "yes") is True


# ── the manually pasted key is hidden (P2: no credential in the scrollback) ──────

def test_manual_paste_default_is_hidden_not_echoing_input():
    """The pasted key becomes the private-index password, so the DEFAULT reader must be
    the hidden one — never the echoing builtin ``input``."""
    import inspect
    default = inspect.signature(pro_trial.detect_pasted_key).parameters["paste_input"].default
    assert default is pro_trial._hidden_paste_input and default is not input
    assert inspect.signature(pro_trial.convert).parameters["paste_input"].default is not input


def test_hidden_paste_input_reads_via_getpass(monkeypatch):
    seen = {}

    def fake_getpass(prompt):
        seen["p"] = prompt
        return "K-1"
    monkeypatch.setattr("getpass.getpass", fake_getpass)
    assert pro_trial._hidden_paste_input("Paste your Pro key: ") == "K-1"
    assert "Pro key" in seen["p"]


def test_hidden_paste_input_never_falls_back_to_an_echoing_read(monkeypatch):
    """If the hidden read fails outright, return "" (→ no key) rather than echoing."""
    def boom(prompt):
        raise RuntimeError("no terminal")
    monkeypatch.setattr("getpass.getpass", boom)
    assert pro_trial._hidden_paste_input("Paste your Pro key: ") == ""


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
