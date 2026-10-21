"""The paid setup paths of pro_trial.py: ``setup --buy`` (buying from the tool),
``setup --activate`` (a setup code from the website or a teammate), ``setup
--recover`` (moving seat 1 here, or changing the recovery address), and what a plain
``setup`` finishes first (build rule 2: a held recovery code, a held ``--buy`` code,
then an install whose key is already in ``~/.netrc``).

Network-free (a FakeBridge answers for the Buddhi Review service, tests/conftest.py),
pip-free (the install runner is injected), and clock-free (the waits read a fake
monotonic clock). Every line asserted is the approved on-screen wording, word for word.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import types
from urllib.parse import parse_qs, urlparse

import pytest

from buddhi_review import pro_trial
from conftest import FakeBridge, ScriptedUi

_KEY = "PAIDKEY-0001"
_SECRET = "M" * 43
_PHRASE = "abacus abdomen zoology zoom"
_SUPPORT = "support@buddhireview.com"
_RECOVERY = ("with your recovery address (the email you first paid with, unless you changed "
             "it; if you paid with two different emails, run it once with each)")
_PAID_OK = ("✓ Buddhi Pro is active — your subscription is active. The live view is starting "
            "in the background.")
_TEAMMATE_OK = ("✓ Buddhi Review Pro is active on this computer. The live view is starting in "
                "the background.")
_CLOSED = ("Buddhi Review Pro cannot be installed into this Python right now: Buddhi Review "
           "could not be reached, no Pro release fits this Python yet, or this Python cannot "
           "install packages with pip. This command changed nothing. Try again later, or email "
           "support@buddhireview.com.")
_STOPPED = "Setup stopped. Nothing was bought, used or changed."
_FAULT = ("Buddhi Review did not accept this request. This points to a fault in this version "
          "of Buddhi Review, not in anything you did. Run `buddhi-review upgrade`, then try "
          "again. If it still fails, email support@buddhireview.com.")
_NOT_ANSWERING = ("The Buddhi Review service is not answering right now. Run the same command "
                  "again in a few minutes.")
_WAITING = ("Payment received. Finishing Buddhi Review Pro setup — up to 2 minutes. Keep this "
            "window open. Do not pay again.")


class FakeProBackend:
    name = "pro"
    priority = 100

    def is_active(self):
        return True

    def run_review_loop(self, *a, **k):  # pragma: no cover - Protocol completeness
        return 0

    def start_daemon(self, **k):
        return True


class _Clock:
    """A monotonic clock that moves ``step`` seconds every time it is read."""

    def __init__(self, step=0.0):
        self.t, self.step = 1000.0, step

    def __call__(self):
        self.t += self.step
        return self.t


def _open_here():
    from packaging import tags
    return {"status": "open", "wheels": [str(next(iter(tags.sys_tags())))]}


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv("BUDDHI_NO_UPSELL", raising=False)
    monkeypatch.setattr(pro_trial, "_release_fetch", _open_here)
    monkeypatch.setattr(pro_trial, "pro_backend_active", lambda **k: False)
    monkeypatch.setattr(pro_trial, "_mono", _Clock(0.0))


def _bridge(monkeypatch, routes):
    fake = FakeBridge(routes)
    monkeypatch.setattr(pro_trial, "_bridge_transport", fake)
    return fake


def _ok_runner(cmd, env=None):
    return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")


def _kw(**over):
    kw = dict(runner=_ok_runner, backends=[FakeProBackend()], is_active=lambda: True,
              sleep=lambda s: None, attempts=1)
    kw.update(over)
    return kw


def _state():
    path = pro_trial._state_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _set_state(**values):
    path = pro_trial._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values), encoding="utf-8")


def _accepted(**values):
    _set_state(eula={"version": "1.1", "sha256": pro_trial.EULA_SHA256,
                     "accepted_at": "2026-10-18T00:00:00Z"}, **values)


def _seat_admin():
    path = pro_trial._seat_admin_path()
    return json.loads(path.read_text()) if path.exists() else None


def _netrc_text(tmp_path):
    p = tmp_path / ".netrc"
    return p.read_text() if p.exists() else ""


def _claim_200(*, seat=1, secret=True, key=_KEY):
    data = {"subscription_id": "sub_1", "seat": seat, "generation": 1, "licence_key": key,
            "replay_until": "2026-10-18T00:15:00Z"}
    if secret:
        data.update(manage_secret=_SECRET, manage_secret_version=1)
    return 200, data


class _Closed:
    def is_open(self):
        return False


# ═══════════════════════════════ setup --buy ═══════════════════════════════════

def _buy(monkeypatch, routes, ui=None, **kw):
    fake = _bridge(monkeypatch, routes)
    ui = ui or ScriptedUi()
    rc = pro_trial.run_buy(ui, **_kw(**kw))
    return rc, ui, fake


def test_buy_pairs_opens_the_checkout_waits_and_installs(monkeypatch, tmp_path):
    _accepted()
    rc, ui, fake = _buy(monkeypatch, {
        "POST /pair": [(201, {"phrase": _PHRASE})],
        "POST /claim": [(404, {"error": "not_found"}), (404, {"error": "not_found"}),
                        (202, {"status": "pending"}, 3), _claim_200()],
    })
    assert rc == 0
    setup_hash = fake.bodies("POST /pair")[0]["setup_hash"]
    assert re.fullmatch(r"[0-9a-f]{64}", setup_hash)
    url = f"https://buddhireview.com/buy?h={setup_hash}"
    assert ui.opened == [url]
    assert ui.lines[:3] == [
        ("step", f"Your pairing words: {_PHRASE}"),
        ("info", "The checkout page shows the same four words. If it shows other words, or "
                 "none, close it without paying and run `buddhi-review setup --buy` again."),
        ("step", "Opening the checkout in your browser.")]
    assert ui.text.count("Waiting for your payment. Pay in your browser and keep this window "
                         "open; setup continues on its own.") == 1
    assert ui.text.count(_WAITING) == 1
    assert ui.lines[-1] == ("ok", _PAID_OK)
    # the code made here is the one the hash is of (its 32 raw bytes), sent only to /claim
    claim = fake.bodies("POST /claim")[0]
    raw = base64.urlsafe_b64decode(claim["setup_code"] + "=")
    assert hashlib.sha256(raw).hexdigest() == setup_hash
    assert all(b == claim for b in fake.bodies("POST /claim"))
    assert claim["setup_code"] not in url and not any(claim["setup_code"] in t for t in ui.text)
    assert _seat_admin()["entries"] == {
        "https://api.buddhireview.com sub_1": {"secret": _SECRET, "version": 1}}
    assert f"password {_KEY}" in _netrc_text(tmp_path)
    assert "buy" not in _state()                  # the key is saved: the code is dropped
    assert _state()["last_install_kind"] == "buy"
    assert fake.unexpected == []


def test_buy_shows_the_page_when_no_browser_opens(monkeypatch):
    _accepted()
    rc, ui, fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                      "POST /claim": [_claim_200()]},
                        ui=ScriptedUi(browser=False))
    url = f"https://buddhireview.com/buy?h={fake.bodies('POST /pair')[0]['setup_hash']}"
    assert ("info", f"Open this page in your browser: {url}") in ui.lines


def test_buy_draws_a_new_code_when_the_hash_is_already_paired(monkeypatch):
    _accepted()
    rc, ui, fake = _buy(monkeypatch, {
        "POST /pair": [(409, {"error": "already_paired"}), (201, {"phrase": _PHRASE})],
        "POST /claim": [_claim_200()]})
    hashes = [b["setup_hash"] for b in fake.bodies("POST /pair")]
    assert rc == 0 and len(set(hashes)) == 2
    assert ui.lines[0] == ("step", f"Your pairing words: {_PHRASE}")


@pytest.mark.parametrize("answer, line", [
    ((429, {"error": "rate_limited"}, 60), "Too many requests have been made for now. Run the "
                                           "same command again in about 1 minute."),
    ((503, {"error": "maintenance"}, 300), _NOT_ANSWERING),
    ((400, {"error": "invalid_request"}), _FAULT),
    ((201, {"phrase": "NOT A PHRASE 1"}), _NOT_ANSWERING),
])
def test_buy_stops_when_pairing_fails(monkeypatch, answer, line):
    _accepted()
    rc, ui, fake = _buy(monkeypatch, {"POST /pair": [answer]})
    assert rc == 1 and ui.lines[-1] == ("warn", line)
    assert fake.count("POST /claim") == 0 and ui.opened == []
    assert "buy" not in _state()


def test_buy_waits_through_silence_and_never_shows_the_shared_lines(monkeypatch):
    _accepted()
    rc, ui, fake = _buy(monkeypatch, {
        "POST /pair": [(201, {"phrase": _PHRASE})],
        "POST /claim": [(429, {"error": "rate_limited"}, 60), (503, {"error": "unavailable"}),
                        (0, None), (200, None), _claim_200()]})
    assert rc == 0
    assert _NOT_ANSWERING not in ui.text and not any("Too many" in t for t in ui.text)


def test_buy_stops_waiting_after_30_minutes(monkeypatch):
    _accepted()
    monkeypatch.setattr(pro_trial, "_mono", _Clock(60.0))
    rc, ui, _fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                       "POST /claim": [(404, {"error": "not_found"})]})
    assert rc == 1
    assert ui.lines[-1] == ("warn", (
        "We could not see a payment yet. If you paid, do not pay again: run `buddhi-review "
        "setup` in a few minutes. If it still cannot find your payment, run `buddhi-review "
        f"setup --recover` {_RECOVERY}, or email {_SUPPORT}. If the checkout page showed you a "
        "setup code, run `buddhi-review setup --activate` and paste it instead. If you did not "
        "pay, nothing was charged."))
    assert "buy" in _state()                      # kept: a plain setup keeps checking


def test_buy_stops_after_two_minutes_of_a_seat_being_made(monkeypatch):
    _accepted()
    monkeypatch.setattr(pro_trial, "_mono", _Clock(30.0))
    rc, ui, _fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                       "POST /claim": [(202, {"status": "pending"})]})
    assert rc == 1 and ui.lines[-1] == ("warn", (
        "Your payment was received, but setup is not finished yet. Do not pay again. Run "
        "`buddhi-review setup` again in a few minutes; it finishes this purchase."))
    assert _state()["buy"]["first_accepted_at"]


@pytest.mark.parametrize("answer, line, kept", [
    ((409, {"error": "seat_not_active"}),
     "The seat for this purchase is not active right now: the subscription may be paused, "
     "cancelled, refunded or in a payment dispute, or the seat is being moved by a recovery. "
     "Nothing was used up; run `buddhi-review setup` again in a few minutes. If this lasts, "
     "email support@buddhireview.com.", True),
    ((410, {"error": "expired_or_used"}),
     f"This purchase can no longer be finished from here. To set up this computer, run "
     f"`buddhi-review setup --recover` {_RECOVERY}.", False),
    ((410, {"error": "duplicate_purchase"}),
     "Buddhi Review found two purchases made through this computer's checkout, so it cannot "
     f"set up this computer by itself. Run `buddhi-review setup --recover` {_RECOVERY}. If you "
     "meant to buy only once, email support@buddhireview.com.", False),
    ((400, {"error": "invalid_request"}), _FAULT, True),
])
def test_buy_answers_that_end_the_wait(monkeypatch, answer, line, kept):
    _accepted()
    rc, ui, _fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                       "POST /claim": [answer]})
    assert rc == 1 and ui.lines[-1] == ("warn", line)
    assert ("buy" in _state()) is kept


def test_ctrl_c_while_buy_waits(monkeypatch):
    _accepted()
    rc, ui, _fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                       "POST /claim": [(404, {}), KeyboardInterrupt()]})
    assert rc == 1 and ui.lines[-1] == ("info", (
        "Stopped waiting. If you paid, do not pay again: run `buddhi-review setup` to finish. "
        f"If it cannot find your payment, run `buddhi-review setup --recover` {_RECOVERY}."))
    assert "buy" in _state()


def test_buy_keeps_the_code_when_the_key_cannot_be_saved(monkeypatch, tmp_path):
    _accepted()
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    rc, ui, _fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                       "POST /claim": [_claim_200()]},
                         netrc_path=blocker / ".netrc")
    assert rc == 1 and ui.lines[-1][1].startswith(f"Could not write {blocker / '.netrc'}. ")
    assert "buy" in _state()
    assert _seat_admin() is not None               # the manage secret was kept anyway


def test_buy_reports_a_seat_access_file_it_could_not_save(monkeypatch, tmp_path):
    _accepted()
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("BUDDHI_SEAT_ADMIN", str(blocker / "seat-admin.json"))
    rc, ui, _fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                       "POST /claim": [_claim_200()]})
    assert rc == 0 and ui.lines[-2] == ("ok", _PAID_OK)
    assert ui.lines[-1][1].startswith("Buddhi Review Pro is set up, but this computer could "
                                      "not save the access it needs to manage your seats (")


def test_buy_while_the_release_is_closed_changes_nothing(monkeypatch):
    fake = _bridge(monkeypatch, {})
    ui = ScriptedUi()
    assert pro_trial.run_buy(ui, release=_Closed(), **_kw()) == 1
    assert ui.lines == [("warn", _CLOSED)] and fake.calls == []


def test_buy_asks_for_the_licence_agreement_before_the_checkout(monkeypatch):
    rc, ui, fake = _buy(monkeypatch, {}, ui=ScriptedUi(yes={"Do you accept it?": [False]}))
    assert rc == 1 and fake.calls == []
    assert ui.lines[-1] == ("warn", "You did not accept the agreement, so Buddhi Review Pro was "
                                    "not installed. The free version keeps working. To install "
                                    "Pro, run the same command again and accept the agreement.")


def test_buy_off_a_terminal_without_assent_never_assumes_it(monkeypatch):
    rc, ui, fake = _buy(monkeypatch, {}, ui=ScriptedUi(interactive=False))
    assert rc == 1 and fake.calls == []
    assert ui.lines == [("warn", "Buddhi Review Pro was not installed: its licence agreement "
                                 "must be accepted in a terminal. Run the same command in a "
                                 "terminal to read and accept it.")]


def test_buy_asks_first_only_when_paid_pro_is_active_here(monkeypatch):
    _accepted(last_install_kind="buy")
    monkeypatch.setattr(pro_trial, "pro_backend_active", lambda **k: True)
    question = ("Buddhi Review Pro is already active on this computer. Buy another "
                "subscription?")
    rc, ui, fake = _buy(monkeypatch, {}, ui=ScriptedUi(yes={question: [False]}))
    assert rc == 0 and ui.defaults[question] is False
    assert ui.lines == [("info", "Nothing was changed.")] and fake.calls == []
    # a trial user buying is the normal path and is never asked
    _accepted(last_install_kind="trial")
    rc, ui, _fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                       "POST /claim": [_claim_200()]})
    assert rc == 0 and question not in ui.asked


def test_buy_checks_an_earlier_checkout_first(monkeypatch):
    _accepted(buy={"code": "C" * 43, "nonce": "N" * 43, "paired_at": pro_trial._now(),
                   "first_accepted_at": None})
    question = ("An earlier checkout from this computer has no payment Buddhi Review can see. "
                "If you paid in the last few minutes, do not pay again: run `buddhi-review "
                "setup` in a few minutes instead. If you paid earlier and Buddhi Review still "
                "cannot find your payment, run `buddhi-review setup --recover` "
                f"{_RECOVERY}, or email {_SUPPORT}. Start a new checkout?")
    rc, ui, fake = _buy(monkeypatch, {"POST /claim": [(404, {"error": "not_found"})]},
                        ui=ScriptedUi(yes={question: [False]}))
    assert rc == 0 and ui.lines == [("info", "Nothing was changed.")]
    assert fake.bodies("POST /claim") == [{"setup_code": "C" * 43, "claim_nonce": "N" * 43}]
    assert ui.defaults[question] is False and "buy" in _state()
    # Yes: the old code is dropped and a new checkout begins
    rc, ui, fake = _buy(monkeypatch, {"POST /claim": [(404, {}), _claim_200()],
                                      "POST /pair": [(201, {"phrase": _PHRASE})]},
                        ui=ScriptedUi(yes={question: [True]}))
    assert rc == 0 and fake.count("POST /pair") == 1
    assert fake.bodies("POST /claim")[1]["setup_code"] != "C" * 43


def test_buy_finishes_an_earlier_checkout_that_was_paid(monkeypatch):
    _accepted(buy={"code": "C" * 43, "nonce": "N" * 43, "paired_at": pro_trial._now()})
    rc, ui, fake = _buy(monkeypatch, {"POST /claim": [_claim_200()]})
    assert rc == 0 and fake.count("POST /pair") == 0 and ui.lines[-1] == ("ok", _PAID_OK)


def test_ctrl_c_while_pairing_leaves_no_held_code(monkeypatch):
    _accepted()
    rc, ui, _fake = _buy(monkeypatch, {"POST /pair": [KeyboardInterrupt()]})
    assert rc == 1 and ui.lines[-1] == ("info", _STOPPED)
    assert "buy" not in _state() and not pro_trial.has_held_work()


def test_buy_cannot_save_its_setup_details(monkeypatch):
    _accepted()
    real = pro_trial._save_state
    monkeypatch.setattr(pro_trial, "_save_state", lambda path, state: "Read-only file system"
                        if "buy" in state else real(path, state))
    rc, ui, fake = _buy(monkeypatch, {})
    assert rc == 1 and fake.calls == []
    assert ui.lines[-1] == ("warn", (
        f"Buddhi Review could not save its setup details in {pro_trial._state_path()} "
        "(Read-only file system), so this command changed nothing. Fix that folder's "
        "permissions, then run the same command again."))


def test_ctrl_c_before_anything_is_spent(monkeypatch):
    rc, ui, _fake = _buy(monkeypatch, {}, ui=ScriptedUi(yes={"Do you accept it?": [
        KeyboardInterrupt]}))
    assert rc == 1 and ui.lines[-1] == ("info", _STOPPED)


# ═════════════════════════════ setup --activate ════════════════════════════════

_CODE = "A" * 20 + "_" + "b" * 21 + "-"


def _activate(monkeypatch, routes, ui, **kw):
    fake = _bridge(monkeypatch, routes)
    rc = pro_trial.run_activate(ui, **_kw(**kw))
    return rc, fake


def _code_ui(code=_CODE, **kw):
    return ScriptedUi(hidden={"Paste your setup code (it stays hidden):": [code]}, **kw)


def test_a_teammates_code_sets_up_this_computer(monkeypatch, tmp_path):
    _accepted()
    ui = _code_ui()
    rc, fake = _activate(monkeypatch, {"POST /claim": [_claim_200(seat=2, secret=False)]}, ui)
    assert rc == 0 and ui.lines[-1] == ("ok", _TEAMMATE_OK)
    body = fake.bodies("POST /claim")[0]
    assert body["setup_code"] == _CODE and re.fullmatch(r"[A-Za-z0-9_-]{43}", body["claim_nonce"])
    assert _seat_admin() is None                  # a teammate's code never yields management
    assert "activate" not in _state() and f"password {_KEY}" in _netrc_text(tmp_path)


def test_the_buyers_own_code_also_brings_seat_management(monkeypatch):
    _accepted()
    ui = _code_ui()
    rc, _fake = _activate(monkeypatch, {"POST /claim": [_claim_200()]}, ui)
    assert rc == 0 and ui.lines[-1] == ("ok", _PAID_OK) and _seat_admin() is not None


def test_a_shaped_clipboard_is_offered_only_with_consent(monkeypatch):
    _accepted()
    question = f"Use the setup code in your clipboard ({_CODE[:4]}******{_CODE[-4:]})?"
    ui = ScriptedUi(clipboard=_CODE, yes={question: [True]})
    rc, fake = _activate(monkeypatch, {"POST /claim": [_claim_200(secret=False)]}, ui)
    assert rc == 0 and ui.defaults[question] is False
    assert fake.bodies("POST /claim")[0]["setup_code"] == _CODE
    # declined: the hidden prompt instead, and the clipboard never leaves the machine
    other = "Z" * 43
    ui = ScriptedUi(clipboard=_CODE, yes={question: [False]},
                    hidden={"Paste your setup code": [other]})
    rc, fake = _activate(monkeypatch, {"POST /claim": [_claim_200(secret=False)]}, ui)
    assert [b["setup_code"] for b in fake.bodies("POST /claim")] == [other]


def test_a_clipboard_that_is_not_a_setup_code_is_never_offered(monkeypatch):
    _accepted()
    ui = _code_ui(clipboard="my password hunter2")
    _activate(monkeypatch, {"POST /claim": [_claim_200(secret=False)]}, ui)
    assert not any("clipboard" in q for q in ui.asked)


@pytest.mark.parametrize("typed, line", [
    ("", "No setup code entered. Run `buddhi-review setup --activate` when you have it."),
    ("not-a-code", "That is not a setup code. Copy it again from the page that showed it, or "
                   "from the person who gave it to you."),
])
def test_an_empty_or_misshapen_code_is_never_sent(monkeypatch, typed, line):
    _accepted()
    ui = _code_ui(typed)
    rc, fake = _activate(monkeypatch, {}, ui)
    assert rc == 1 and ui.text[-1] == line and fake.calls == []


def test_activate_checks_for_two_minutes_then_stops(monkeypatch):
    _accepted()
    monkeypatch.setattr(pro_trial, "_mono", _Clock(30.0))
    ui = _code_ui()
    rc, _fake = _activate(monkeypatch, {"POST /claim": [(404, {"error": "not_found"})]}, ui)
    assert rc == 1
    assert ui.text.count("Checking your setup code. This takes up to 2 minutes; keep this "
                         "window open. If you bought Pro, do not pay again.") == 1
    assert ui.lines[-1] == ("warn", (
        "We could not find a purchase for this setup code yet. If you bought Pro, do not pay "
        "again: wait a few minutes, then run `buddhi-review setup --activate` and paste the same "
        "code; if it still cannot find your payment, run `buddhi-review setup --recover` "
        f"{_RECOVERY}, or email {_SUPPORT}. If someone gave you this code, ask them for a new "
        "one."))


def test_activate_waits_two_minutes_for_a_seat_being_made(monkeypatch):
    _accepted()
    monkeypatch.setattr(pro_trial, "_mono", _Clock(30.0))
    ui = _code_ui()
    rc, _fake = _activate(monkeypatch, {"POST /claim": [(202, {"status": "pending"})]}, ui)
    assert rc == 1
    assert ui.text.count("Your setup code was found. Finishing Buddhi Review Pro setup — up to "
                         "2 minutes. Keep this window open.") == 1
    assert ui.lines[-1] == ("warn", "Setup is not finished yet. Run `buddhi-review setup "
                                    "--activate` again in a few minutes and paste the same code. "
                                    "If you bought Pro, do not pay again.")


@pytest.mark.parametrize("answer, line, kept", [
    ((409, {"error": "seat_not_active"}),
     "This setup code's seat is not active right now: the subscription may be paused, "
     "cancelled, refunded or in a payment dispute, or the seat is being removed, replaced or "
     "moved by a recovery. The code was not used up; try again in a few minutes. If this "
     "lasts, email support@buddhireview.com, or ask the person who gave you this code.", True),
    ((410, {"error": "expired_or_used"}),
     "This setup code has expired or was already used. If you bought Pro, run `buddhi-review "
     "setup --recover`; if someone gave you this code, ask them for a new one.", False),
    ((410, {"error": "duplicate_purchase"}),
     "This setup code was used by more than one purchase, so it cannot set up this computer by "
     f"itself. If you bought Pro, run `buddhi-review setup --recover` {_RECOVERY}. If someone "
     "gave you this code, ask them for a new one.", False),
    ((429, {"error": "rate_limited"}, 3600),
     "Too many requests have been made for now. Run the same command again in about 1 hour.",
     True),
    ((503, {"error": "unavailable"}), _NOT_ANSWERING, True),
    ((413, {"error": "too_large"}), _FAULT, True),
])
def test_activate_answers_that_end_it(monkeypatch, answer, line, kept):
    _accepted()
    ui = _code_ui()
    rc, _fake = _activate(monkeypatch, {"POST /claim": [answer]}, ui)
    assert rc == 1 and ui.lines[-1] == ("warn", line)
    assert ("activate" in _state()) is kept


def test_pasting_the_same_code_again_reuses_its_nonce(monkeypatch):
    _accepted()
    _activate(monkeypatch, {"POST /claim": [(503, {"error": "unavailable"})]}, _code_ui())
    first = _state()["activate"]["nonce"]
    _rc, fake = _activate(monkeypatch, {"POST /claim": [_claim_200(secret=False)]}, _code_ui())
    assert fake.bodies("POST /claim")[0]["claim_nonce"] == first
    _accepted()
    _activate(monkeypatch, {"POST /claim": [(503, {})]}, _code_ui("Q" * 43))
    assert _state()["activate"]["nonce"] != first


def test_ctrl_c_while_activate_waits(monkeypatch):
    _accepted()
    ui = _code_ui()
    rc, _fake = _activate(monkeypatch, {"POST /claim": [(404, {}), KeyboardInterrupt()]}, ui)
    assert rc == 1 and ui.lines[-1] == ("info", (
        "Stopped waiting. To carry on, run `buddhi-review setup --activate` and paste the same "
        "code. If you bought Pro, do not pay again."))


def test_activate_off_a_terminal(monkeypatch):
    rc, fake = _activate(monkeypatch, {}, ScriptedUi(interactive=False))
    assert rc == 1 and fake.calls == []
    _accepted()
    ui = ScriptedUi(interactive=False)
    assert pro_trial.run_activate(ui, **_kw()) == 1
    assert ui.lines == [("warn", "This command needs a terminal to ask for your setup code. Run "
                                 "it in a terminal.")]


# ═══════════════════════════ setup --recover, choice 1 ═════════════════════════

_ADDRESS = "Your recovery address (the email you first paid with, unless you changed it): "
_REC_PROMPT = "Paste the recovery code (it stays hidden; press Enter if none arrived): "
_MOVE_Q = "Moving seat 1 here ends Pro on any other computer that holds it. Continue?"
_REC_CODE = "ABCD-EFGH-JKMN-PQRS"


def _recover_ui(*, choice=("1",), move=(True,), email=("me@example.com",), code=(_REC_CODE,),
                **kw):
    return ScriptedUi(answers={"": list(choice), _ADDRESS: list(email)},
                      hidden={_REC_PROMPT: list(code)}, yes={_MOVE_Q: list(move)}, **kw)


def _ready(sub, key, version=2):
    return {"subscription_id": sub, "manage_secret": f"{sub}-secret".ljust(43, "x"),
            "manage_secret_version": version,
            "seat": {"status": "ready", "seat": 1, "generation": 3, "licence_key": key}}


def _refused(sub, error):
    return {"subscription_id": sub, "manage_secret": f"{sub}-secret".ljust(43, "x"),
            "manage_secret_version": 2, "seat": {"status": "refused", "error": error}}


def _complete(*subs):
    return 200, {"status": "complete", "replay_until": "2026-10-18T00:15:00Z",
                 "subscriptions": list(subs)}


def _recover(monkeypatch, routes, ui, **kw):
    fake = _bridge(monkeypatch, routes)
    rc = pro_trial.run_recover(ui, **_kw(**kw))
    return rc, fake


def _choice_ui(ui):
    """The choice prompt is the empty string, which every prompt contains: answer it
    only when the prompt is exactly empty, so it never swallows another prompt."""
    answers = ui._answers
    choice = answers.pop("")
    ui._answers = {k: v for k, v in answers.items()}
    real = ui.ask

    def ask(prompt):
        if prompt == "":
            ui.asked.append(prompt)
            if not choice:
                raise AssertionError("choice asked again")
            return choice.pop(0)
        return real(prompt)
    ui.ask = ask
    return ui


def test_recovery_moves_seat_1_here_and_takes_seat_management(monkeypatch, tmp_path):
    _accepted(buy={"code": "C" * 43, "nonce": "N" * 43, "paired_at": pro_trial._now()})
    ui = _choice_ui(_recover_ui())
    rc, fake = _recover(monkeypatch, {
        "POST /recover/code": [(202, {"status": "sent"})],
        "POST /recover": [_complete(_ready("sub_1", _KEY))],
        "GET /seats": [(200, {"reasons": [], "seats": []})]}, ui)
    assert rc == 0
    assert ui.lines[:3] == [
        ("step", "What do you want to do?"),
        ("info", "1. Move my own Buddhi Review Pro seat to this computer (seat 1 of each of my "
                 "subscriptions)"),
        ("info", "2. Change my recovery address")]
    assert ui.defaults[_MOVE_Q] is False
    assert ("info", "If this is the recovery address of a Buddhi Review Pro purchase, a recovery "
                    "code is on its way from Buddhi Review (codes@buddhireview.com). It expires "
                    "in 30 minutes.") in ui.lines
    assert ui.text[-4:] == [
        "Subscription sub_1: seat 1 is now on this computer.",
        "Any other computer that held seat 1 of sub_1 loses Pro the next time it connects, and "
        "within 7 days if it stays offline.",
        "Seat management for sub_1 is now on this computer.",
        _PAID_OK]
    body = fake.bodies("POST /recover")[0]
    assert body["email"] == "me@example.com" and body["code"] == "ABCDEFGHJKMNPQRS"
    assert fake.calls[-1]["auth"] == f"Bearer {'sub_1-secret'.ljust(43, 'x')}"  # GET /seats
    assert _seat_admin()["entries"]["https://api.buddhireview.com sub_1"]["version"] == 2
    assert f"password {_KEY}" in _netrc_text(tmp_path)
    state = _state()
    assert "recover" not in state and state["buy"]["recovered"] is True
    assert fake.unexpected == []


def test_recovery_with_several_seats_runs_pro_under_an_active_one(monkeypatch, tmp_path):
    _accepted()
    ui = _choice_ui(_recover_ui())
    paused = lambda body, auth, query: (200, {"reasons": ["paused"] if query[
        "subscription_id"] == "sub_1" else []})
    rc, _fake = _recover(monkeypatch, {
        "POST /recover/code": [(202, {"status": "sent"})],
        "POST /recover": [_complete(_ready("sub_1", "KEY-1"), _ready("sub_2", "KEY-2"),
                                    _refused("sub_3", "superseded"),
                                    _refused("sub_4", "needs_attention"))],
        "GET /seats": paused}, ui)
    assert rc == 0
    assert ui.text[-8:] == [
        "Subscription sub_1: seat 1 is now on this computer, but the subscription is paused or "
        "in a payment dispute, so Pro does not run under it until that ends.",
        "Subscription sub_2: seat 1 is now on this computer.",
        "Subscription sub_3: a newer recovery took this seat, so it was not moved here. Run "
        "`buddhi-review setup --recover` again if this computer should have it.",
        f"Subscription sub_4: waiting for a check by Buddhi Review support, so no seat was moved "
        f"here. Email {_SUPPORT}.",
        "Any other computer that held seat 1 of sub_1, sub_2 and sub_4 loses Pro the next time it "
        "connects, and within 7 days if it stays offline.",
        "Seat management for sub_1, sub_2 and sub_4 is now on this computer.",
        "This computer runs Pro under subscription sub_2.",
        _PAID_OK]
    assert "password KEY-2" in _netrc_text(tmp_path)
    assert "https://api.buddhireview.com sub_3" not in _seat_admin()["entries"]


@pytest.mark.parametrize("error, line", [
    ("subscription_not_active", "Subscription sub_9: not active (cancelled or refunded), so no "
                                "seat was moved."),
    ("subscription_not_ready", "Subscription sub_9: not ready yet, so no seat was moved here. "
                               "Run `buddhi-review setup --recover` again in a few minutes."),
    ("expired", "Subscription sub_9: not ready yet, so no seat was moved here. Run "
                "`buddhi-review setup --recover` again in a few minutes."),
])
def test_recovery_when_no_seat_moved(monkeypatch, error, line):
    _accepted()
    ui = _choice_ui(_recover_ui())
    rc, _fake = _recover(monkeypatch, {"POST /recover/code": [(202, {"status": "sent"})],
                                       "POST /recover": [_complete(_refused("sub_9", error))]},
                         ui, runner=lambda *a, **k: pytest.fail("installed"))
    assert rc == 1 and ui.text[-4:] == [
        line,
        "Any other computer that held seat 1 of sub_9 loses Pro the next time it connects, and "
        "within 7 days if it stays offline.",
        "Seat management for sub_9 is now on this computer.",
        "No seat was moved here, so Buddhi Review Pro was not installed."]


def test_recovery_when_every_subscription_was_superseded(monkeypatch):
    _accepted()
    ui = _choice_ui(_recover_ui())
    rc, _fake = _recover(monkeypatch, {
        "POST /recover/code": [(202, {"status": "sent"})],
        "POST /recover": [_complete(_refused("sub_1", "superseded"))]}, ui)
    assert rc == 1 and ui.text[-2:] == [
        "Subscription sub_1: a newer recovery took this seat, so it was not moved here. Run "
        "`buddhi-review setup --recover` again if this computer should have it.",
        "No seat was moved here, so Buddhi Review Pro was not installed."]


def test_recovery_when_every_moved_seat_is_paused(monkeypatch, tmp_path):
    _accepted()
    ui = _choice_ui(_recover_ui())
    rc, _fake = _recover(monkeypatch, {
        "POST /recover/code": [(202, {"status": "sent"})],
        "POST /recover": [_complete(_ready("sub_1", "KEY-1"))],
        "GET /seats": [(200, {"reasons": ["chargeback"]})]},
        ui, runner=lambda *a, **k: pytest.fail("installed"))
    assert rc == 1 and ui.text[-1] == (
        "Buddhi Review Pro is not installed yet, because none of these subscriptions is active "
        "now. Once one is, run `buddhi-review setup` to finish.")
    assert "password KEY-1" in _netrc_text(tmp_path)
    assert _state()["install"]["kind"] == "recover"       # the next plain setup finishes it


_PAUSED_LINE = ("Buddhi Review Pro is not installed yet, because none of these subscriptions is "
                "active now. Once one is, run `buddhi-review setup` to finish.")


def _paused_recovery(monkeypatch):
    _accepted()
    _recover(monkeypatch, {"POST /recover/code": [(202, {"status": "sent"})],
                           "POST /recover": [_complete(_ready("sub_1", "KEY-1"))],
                           "GET /seats": [(200, {"reasons": ["paused"]})]},
             _choice_ui(_recover_ui()), runner=lambda *a, **k: pytest.fail("installed"))
    assert _state()["install"]["deferred"] == "paused"
    assert _state()["install"]["subscriptions"] == ["sub_1"]


def test_a_plain_setup_keeps_a_paused_recovery_waiting_while_it_is_still_paused(monkeypatch):
    _paused_recovery(monkeypatch)
    for _ in range(2):      # not installed, not dropped: the promised later setup still works
        ui, fake = _finish(monkeypatch, {"GET /seats": [(200, {"reasons": ["paused"]})]},
                           runner=lambda *a, **k: pytest.fail("installed while paused"))
        assert ui.text == [_PAUSED_LINE] and pro_trial.has_held_work()
        assert fake.calls[0]["auth"] == "Bearer " + "sub_1-secret".ljust(43, "x")


def test_a_plain_setup_installs_a_paused_recovery_once_it_is_active(monkeypatch):
    _paused_recovery(monkeypatch)
    ui, _fake = _finish(monkeypatch, {"GET /seats": [(200, {"reasons": []})]})
    assert ui.lines == [("ok", _PAID_OK)] and "install" not in _state()


def test_a_paused_recovery_whose_state_cannot_be_read_is_installed(monkeypatch):
    _paused_recovery(monkeypatch)
    ui, _fake = _finish(monkeypatch, {"GET /seats": [(503, {"error": "unavailable"})]})
    assert ui.lines[-1] == ("ok", _PAID_OK) and "install" not in _state()


def test_a_paused_recovery_resumes_under_the_subscription_that_became_active(monkeypatch,
                                                                             tmp_path):
    _accepted()
    _recover(monkeypatch, {"POST /recover/code": [(202, {"status": "sent"})],
                           "POST /recover": [_complete(_ready("sub_1", "KEY-1"),
                                                       _ready("sub_2", "KEY-2"))],
                           "GET /seats": [(200, {"reasons": ["paused"]}),
                                          (200, {"reasons": ["paused"]})]},
             _choice_ui(_recover_ui()), runner=lambda *a, **k: pytest.fail("installed"))
    assert "password KEY-1" in _netrc_text(tmp_path)
    assert _state()["install"]["keys"] == {"sub_1": "KEY-1", "sub_2": "KEY-2"}
    seen = []

    def runner(*a, **k):
        seen.append(_netrc_text(tmp_path))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    ui, _fake = _finish(monkeypatch, {"GET /seats": [(200, {"reasons": ["paused"]}),
                                                     (200, {"reasons": []})]}, runner=runner)
    assert ui.lines == [("ok", _PAID_OK)] and "install" not in _state()
    assert seen and "password KEY-2" in seen[0] and "KEY-1" not in seen[0]


def test_a_paused_recovery_keeps_the_saved_key_when_none_reads_active(monkeypatch, tmp_path):
    _paused_recovery(monkeypatch)
    ui, _fake = _finish(monkeypatch, {"GET /seats": [(503, {"error": "unavailable"})]})
    assert ui.lines[-1] == ("ok", _PAID_OK) and "password KEY-1" in _netrc_text(tmp_path)


def test_a_held_recovery_that_ends_paused_is_not_installed_in_the_same_run(monkeypatch):
    _accepted(recover=_held_recovery(accepted=True, replay_until=pro_trial._now() + 600))
    ui, _fake = _finish(monkeypatch, {"POST /recover": [_complete(_ready("sub_1", _KEY))],
                                      "GET /seats": [(200, {"reasons": ["paused"]})]},
                        runner=lambda *a, **k: pytest.fail("installed while paused"))
    assert ui.text[-1] == _PAUSED_LINE and ui.text.count(_PAUSED_LINE) == 1
    assert _state()["install"]["deferred"] == "paused"


def test_recovery_with_no_purchase_at_the_address(monkeypatch):
    _accepted()
    ui = _choice_ui(_recover_ui())
    rc, _fake = _recover(monkeypatch, {"POST /recover/code": [(202, {"status": "sent"})],
                                       "POST /recover": [_complete()]}, ui)
    assert rc == 1 and ui.text[-1] == ("No Buddhi Review Pro purchase was found for this "
                                       "recovery address.")


def test_recovery_follows_a_202_then_finishes(monkeypatch):
    _accepted()
    ui = _choice_ui(_recover_ui())
    pending = (202, {"status": "pending", "replay_until": "2026-10-18T00:15:00Z",
                     "subscriptions": [{"subscription_id": "sub_1", "manage_secret": _SECRET,
                                        "manage_secret_version": 3,
                                        "seat": {"status": "pending"}}]}, 3)
    rc, fake = _recover(monkeypatch, {"POST /recover/code": [(202, {"status": "sent"})],
                                      "POST /recover": [pending, pending,
                                                        _complete(_ready("sub_1", _KEY))],
                                      "GET /seats": [(200, {"reasons": []})]}, ui)
    assert rc == 0
    assert ui.text.count("Moving your seat to this computer. This takes up to 2 minutes; keep "
                         "this window open.") == 1
    assert len({json.dumps(b) for b in fake.bodies("POST /recover")}) == 1   # the same request


def test_a_recovery_still_moving_after_two_minutes(monkeypatch):
    _accepted()
    monkeypatch.setattr(pro_trial, "_mono", _Clock(30.0))
    ui = _choice_ui(_recover_ui())
    pending = (202, {"status": "pending", "replay_until": "2099-01-01T00:00:00Z",
                     "subscriptions": [{"subscription_id": "sub_1", "manage_secret": _SECRET,
                                        "manage_secret_version": 3,
                                        "seat": {"status": "pending"}}]})
    rc, _fake = _recover(monkeypatch, {"POST /recover/code": [(202, {"status": "sent"})],
                                       "POST /recover": [pending]}, ui)
    assert rc == 1 and ui.text[-1] == (
        "Your recovery was accepted, but this computer is not set up yet. Any other computer "
        "that held seat 1 of your subscriptions is already losing it. Run `buddhi-review "
        "setup` within the next few minutes to finish here; if that is too late, run "
        "`buddhi-review setup --recover` again, with a new recovery code.")
    assert _state()["recover"]["accepted"] is True
    # the rotated secret was kept from the 202 already
    assert _seat_admin()["entries"]["https://api.buddhireview.com sub_1"]["version"] == 3


@pytest.mark.parametrize("answer, line, kept", [
    ((403, {"error": "code_invalid"}),
     "That recovery code is not valid. It may be mistyped, expired (codes last 30 minutes) or "
     "replaced by a newer one, or too many wrong codes were entered for this address in the "
     "last 24 hours, and then a new code also fails until that time has passed. Run "
     "`buddhi-review setup --recover` to get a new code. If this keeps happening, email "
     "support@buddhireview.com.", False),
    ((429, {"error": "rate_limited"}, 120),
     "Too many requests have been made for now. Run the same command again in about 2 minutes.",
     False),
    ((503, {"error": "unavailable"}, 5),
     "The Buddhi Review service is not answering right now, so this computer is not set up "
     "yet. Run `buddhi-review setup` within the next few minutes to finish the move here, if "
     "it began. If your code was right, any other computer that held seat 1 of your "
     "subscriptions may already be losing it. If that run cannot finish it, run "
     "`buddhi-review setup --recover` again, with a new recovery code.", True),
])
def test_the_first_answer_to_a_recovery_code(monkeypatch, answer, line, kept):
    _accepted()
    ui = _choice_ui(_recover_ui())
    rc, _fake = _recover(monkeypatch, {"POST /recover/code": [(202, {"status": "sent"})],
                                       "POST /recover": [answer]}, ui)
    assert rc == 1 and ui.lines[-1] == ("warn", line)
    assert ("recover" in _state()) is kept


def test_ctrl_c_before_the_service_answers_a_recovery(monkeypatch):
    _accepted()
    ui = _choice_ui(_recover_ui())
    rc, _fake = _recover(monkeypatch, {"POST /recover/code": [(202, {"status": "sent"})],
                                       "POST /recover": [KeyboardInterrupt()]}, ui)
    assert rc == 1 and ui.text[-1] == (
        "Stopped before Buddhi Review answered, so this computer is not set up yet. Run "
        "`buddhi-review setup` within the next few minutes to finish the move here, if it "
        "began. If your code was right, any other computer that held seat 1 of your "
        "subscriptions may already be losing it. If that run cannot finish it, run "
        "`buddhi-review setup --recover` again, with a new recovery code.")
    assert "recover" in _state()


def test_no_recovery_code_yet_and_a_misshapen_one(monkeypatch):
    _accepted()
    ui = _choice_ui(_recover_ui(code=("ABCD-EFGH",)))
    ui._hidden[_REC_PROMPT].append("")
    rc, fake = _recover(monkeypatch, {"POST /recover/code": [(202, {"status": "sent"})]}, ui)
    assert rc == 1 and fake.count("POST /recover") == 0
    assert ui.text[-2:] == [
        "That is not a recovery code. A recovery code has 16 letters and digits, shown as "
        "XXXX-XXXX-XXXX-XXXX. Copy it again from the email.",
        "No code yet? It can take a few minutes, so check your spam folder, and check that you "
        "entered the email you first paid with (or the address you changed it to). If none "
        "arrives, run `buddhi-review setup --recover` again later, or email "
        "support@buddhireview.com and we will help."]


def test_the_choice_and_the_address_prompts(monkeypatch):
    ui = _choice_ui(_recover_ui(choice=("3", "")))
    rc, fake = _recover(monkeypatch, {}, ui)
    assert rc == 1 and ui.text[-2:] == ["Enter 1 or 2.", _STOPPED] and fake.calls == []
    _accepted()
    ui = _choice_ui(_recover_ui(move=(False,)))
    assert _recover(monkeypatch, {}, ui)[0] == 0 and ui.text[-1] == "Nothing was changed."
    ui = _choice_ui(_recover_ui(email=("nope", "")))
    rc, fake = _recover(monkeypatch, {}, ui)
    assert ui.text[-2:] == ["That doesn't look like an email address — try again.",
                            "No recovery address entered. Nothing was changed."]
    assert fake.calls == []


def test_recover_off_a_terminal(monkeypatch):
    ui = ScriptedUi(interactive=False)
    assert pro_trial.run_recover(ui, **_kw()) == 1
    assert ui.text == ["Buddhi Review Pro was not installed: its licence agreement must be "
                       "accepted in a terminal. Run the same command in a terminal to read and "
                       "accept it."]
    _accepted()
    ui = ScriptedUi(interactive=False)
    assert pro_trial.run_recover(ui, **_kw()) == 1
    assert ui.text == ["This command needs a terminal to ask for your recovery address and the "
                       "code we email you. Run it in a terminal."]


# ═══════════════════════ setup --recover, choice 2 ═════════════════════════════

_CUR = "old@example.com"
_NEW = "new@example.com"
_CUR_PROMPT = f"Paste the code sent to {_CUR} (it stays hidden; press Enter if none arrived): "
_NEW_PROMPT = f"Paste the code sent to {_NEW} (it stays hidden; press Enter if none arrived): "
_ADDR_403 = ("That code is not valid. It may be mistyped, expired (codes last 30 minutes) or "
             "replaced by a newer one, or too many wrong codes were entered for this address in "
             "the last 24 hours, and then a new code also fails until that time has passed. "
             "Start again with `buddhi-review setup --recover`.")


def _address_ui(*, current=(_CUR,), new=(_NEW,), cur_code=(_REC_CODE,),
                new_code=("WXYZ-WXYZ-WXYZ-WXYZ",), again=()):
    ui = ScriptedUi(answers={"": ["2"], "Your current recovery address: ": list(current),
                             "Your new recovery address: ": list(new)},
                    hidden={_CUR_PROMPT: list(cur_code), _NEW_PROMPT: list(new_code)},
                    yes={"Send it again?": list(again)})
    return _choice_ui(ui)


def _address_routes(**over):
    routes = {"POST /recover/address/code": [(202, {"status": "sent"})],
              "POST /recover/address/new-code": [(202, {"status": "sent"})],
              "POST /recover/address": [(200, {"status": "changed"})]}
    routes.update(over)
    return routes


def test_the_recovery_address_moves_after_both_inboxes_prove_it(monkeypatch):
    ui = _address_ui()
    rc, fake = _recover(monkeypatch, _address_routes(), ui)
    assert rc == 0
    assert ui.lines[-3:] == [
        ("info", f"If {_CUR} is the recovery address of a Buddhi Review Pro purchase, a code is "
                 "on its way to it from Buddhi Review (codes@buddhireview.com). It expires in 30 "
                 "minutes."),
        ("info", f"A second code is on its way to {_NEW}. It expires in 30 minutes."),
        ("ok", f"Your recovery address is now {_NEW}. Recovery codes now go only there.")]
    assert ui.asked[-2:] == [_CUR_PROMPT, _NEW_PROMPT]
    assert fake.bodies("POST /recover/address") == [{
        "email": _CUR, "new_email": _NEW, "current_code": "ABCDEFGHJKMNPQRS",
        "new_code": "WXYZWXYZWXYZWXYZ"}]
    assert fake.bodies("POST /recover/address/code") == [{"email": _CUR, "new_email": _NEW}]


def test_the_address_change_needs_no_release_check_and_no_assent(monkeypatch):
    monkeypatch.setattr(pro_trial, "_release_fetch", lambda: pytest.fail("release checked"))
    rc, _fake = _recover(monkeypatch, _address_routes(), _address_ui())
    assert rc == 0 and "eula" not in _state()


def test_the_new_address_must_be_a_plain_one(monkeypatch):
    ui = _address_ui(new=("o'brien@example.com", ""))
    rc, fake = _recover(monkeypatch, _address_routes(), ui)
    assert rc == 1 and fake.calls == []
    assert ui.text[-2:] == ["That new address cannot be used. Use a plain address made of "
                            "letters, digits and . _ % + - such as name@example.com.",
                            "No recovery address entered. Nothing was changed."]


def test_the_same_address_changes_nothing(monkeypatch):
    ui = _address_ui(new=(_CUR,), cur_code=())
    rc, _fake = _recover(monkeypatch, _address_routes(**{
        "POST /recover/address/code": [(400, {"error": "same_address"})]}), ui)
    assert rc == 1 and ui.text[-1] == ("The new address is the same as the current one. Nothing "
                                       "was changed.")


def test_no_code_at_the_new_address_offers_it_again(monkeypatch):
    ui = _address_ui(new_code=("", "WXYZ-WXYZ-WXYZ-WXYZ"), again=(True,))
    rc, fake = _recover(monkeypatch, _address_routes(), ui)
    assert rc == 0 and fake.count("POST /recover/address/new-code") == 2
    assert f"No code at {_NEW} yet? It can take a few minutes, so check your spam folder." in ui.text
    assert ui.defaults["Send it again?"] is False
    ui = _address_ui(new_code=("",), again=(False,))
    rc, _fake = _recover(monkeypatch, _address_routes(), ui)
    assert rc == 1 and ui.text[-1] == ("Your recovery address was not changed. Run `buddhi-review "
                                       "setup --recover` to try again.")


@pytest.mark.parametrize("route, answer, line", [
    ("POST /recover/address/new-code", (403, {"error": "code_invalid"}), _ADDR_403),
    ("POST /recover/address/new-code", (422, {"error": "send_failed"}),
     f"We could not send to {_NEW}. The codes for this change no longer work. Start again with "
     "another address."),
    ("POST /recover/address/new-code", (503, {"error": "sending_paused"}, 600),
     "Codes cannot be sent right now. Try again in about 10 minutes with `buddhi-review setup "
     "--recover`."),
    ("POST /recover/address", (403, {"error": "code_invalid"}), _ADDR_403),
])
def test_address_change_answers_that_end_it(monkeypatch, route, answer, line):
    ui = _address_ui()
    if route == "POST /recover/address/new-code":
        ui._hidden[_NEW_PROMPT].clear()
    rc, _fake = _recover(monkeypatch, _address_routes(**{route: [answer]}), ui)
    assert rc == 1 and ui.lines[-1] == ("warn", line)


def test_recovery_code_mail_paused_says_when_to_try_again(monkeypatch):
    _accepted()
    ui = _choice_ui(_recover_ui())
    rc, fake = _recover(monkeypatch, {
        "POST /recover/code": [(503, {"error": "sending_paused"}, 600)]}, ui)
    assert rc == 1 and fake.count("POST /recover/code") == 1
    assert ui.lines[-1] == ("warn", "Codes cannot be sent right now. Try again in about 10 "
                                    "minutes with `buddhi-review setup --recover`.")


@pytest.mark.parametrize("answer", [(502, None), (504, None), (0, None)])
def test_the_recovery_code_request_is_never_retried(monkeypatch, answer):
    """Each accepted request mails a new code that replaces the last: a retry after a
    lost answer would void the code already on its way."""
    _accepted()
    naps = _naps(monkeypatch)
    ui = _choice_ui(_recover_ui())
    rc, fake = _recover(monkeypatch, {"POST /recover/code": [answer]}, ui)
    assert rc == 1 and fake.count("POST /recover/code") == 1 and naps == []
    assert ui.lines[-1] == ("warn", _NOT_ANSWERING)


@pytest.mark.parametrize("route", ["POST /recover/address/code",
                                   "POST /recover/address/new-code"])
@pytest.mark.parametrize("answer", [(502, None), (504, None), (0, None)])
def test_the_address_change_code_requests_are_never_retried(monkeypatch, route, answer):
    naps = _naps(monkeypatch)
    ui = _address_ui()
    if route == "POST /recover/address/code":
        ui._hidden[_CUR_PROMPT].clear()
    ui._hidden[_NEW_PROMPT].clear()
    rc, fake = _recover(monkeypatch, _address_routes(**{route: [answer]}), ui)
    assert rc == 1 and fake.count(route) == 1 and naps == []
    assert ui.lines[-1] == ("warn", _NOT_ANSWERING)


def test_a_misshapen_address_change_code_is_never_sent(monkeypatch):
    ui = _address_ui(cur_code=("NOT-A-CODE",))
    rc, fake = _recover(monkeypatch, _address_routes(), ui)
    assert rc == 1 and ui.text[-1] == _ADDR_403
    assert fake.count("POST /recover/address/new-code") == 0


@pytest.mark.parametrize("where", ["current", "new"])
def test_ctrl_c_at_either_code_prompt(monkeypatch, where):
    ui = _address_ui(cur_code=[KeyboardInterrupt] if where == "current" else (_REC_CODE,),
                     new_code=[KeyboardInterrupt] if where == "new" else ("X",))
    rc, fake = _recover(monkeypatch, _address_routes(), ui)
    assert rc == 1 and ui.text[-1] == "Your recovery address was not changed."
    assert fake.count("POST /recover/address") == 0


# ════════════════ What a plain setup finishes first (build rule 2) ═════════════

def _held_recovery(**over):
    hold = {"email": "me@example.com", "code": "ABCDEFGHJKMNPQRS", "nonce": "N" * 43,
            "first_request_at": pro_trial._now(), "accepted": False, "replay_until": None}
    hold.update(over)
    return hold


def _finish(monkeypatch, routes, ui=None, release=None, **kw):
    fake = _bridge(monkeypatch, routes)
    ui = ui or ScriptedUi()
    pro_trial.finish_held_work(ui, release=release, **_kw(**kw))
    return ui, fake


def test_nothing_held_nothing_done(monkeypatch):
    assert not pro_trial.has_held_work()
    ui, fake = _finish(monkeypatch, {}, release=_Closed())
    assert ui.lines == [] and fake.calls == []


def test_an_accepted_recovery_is_finished(monkeypatch):
    _accepted(recover=_held_recovery(accepted=True, replay_until=pro_trial._now() + 600))
    assert pro_trial.has_held_work()
    ui, fake = _finish(monkeypatch, {"POST /recover": [_complete(_ready("sub_1", _KEY))],
                                     "GET /seats": [(200, {"reasons": []})]})
    assert ui.text[0] == "Finishing the move of your seat to this computer."
    assert ui.text.count("Finishing the move of your seat to this computer.") == 1
    assert ui.lines[-1] == ("ok", _PAID_OK) and "recover" not in _state()
    assert fake.bodies("POST /recover")[0]["claim_nonce"] == "N" * 43


def test_an_accepted_recovery_past_its_window(monkeypatch):
    _accepted(recover=_held_recovery(accepted=True, replay_until=pro_trial._now() + 600))
    ui, _fake = _finish(monkeypatch, {"POST /recover": [(403, {"error": "code_invalid"})]})
    assert ui.text[-1] == ("The move can no longer be finished from here. Run `buddhi-review "
                           "setup --recover` again, with a new recovery code.")
    assert "recover" not in _state()


def test_a_recovery_that_never_got_an_answer_is_checked(monkeypatch):
    _accepted(recover=_held_recovery())
    ui, _fake = _finish(monkeypatch, {"POST /recover": [_complete(_ready("sub_1", _KEY))],
                                      "GET /seats": [(200, {"reasons": []})]})
    assert ui.text[:2] == ["Checking whether the move of your seat to this computer began.",
                           "Finishing the move of your seat to this computer."]
    assert ui.lines[-1] == ("ok", _PAID_OK)


@pytest.mark.parametrize("answer, line, kept", [
    ((403, {"error": "code_invalid"}),
     "Buddhi Review could not confirm this recovery. The move may already have begun, and any "
     "other computer that held seat 1 of your subscriptions may already be losing it. To try "
     "again, run `buddhi-review setup --recover` with a new recovery code.", False),
    ((503, {"error": "unavailable"}),
     "The Buddhi Review service is not answering right now, so this computer is not set up "
     "yet. Run `buddhi-review setup` within the next few minutes to finish the move here, if "
     "it began. If your code was right, any other computer that held seat 1 of your "
     "subscriptions may already be losing it. If that run cannot finish it, run "
     "`buddhi-review setup --recover` again, with a new recovery code.", True),
])
def test_an_unanswered_recovery_that_cannot_be_confirmed(monkeypatch, answer, line, kept):
    _accepted(recover=_held_recovery())
    ui, _fake = _finish(monkeypatch, {"POST /recover": [answer]})
    assert ui.text[-1] == line and ("recover" in _state()) is kept


def test_a_held_recovery_expires(monkeypatch):
    _accepted(recover=_held_recovery(first_request_at=pro_trial._now() - 16 * 60))
    assert not pro_trial.has_held_work()
    ui, fake = _finish(monkeypatch, {})
    assert ui.lines == [] and fake.calls == [] and "recover" not in _state()


def _held_buy(**over):
    hold = {"code": "C" * 43, "nonce": "N" * 43, "paired_at": pro_trial._now(),
            "first_accepted_at": None, "held_404_shown": False, "recovered": False}
    hold.update(over)
    return hold


_HELD_404 = ("We could not find a payment for your earlier checkout yet. If you paid, do not "
             "pay again: later `buddhi-review setup` runs keep checking for it, without this "
             "message, for up to 6 days. If Buddhi Review Pro is still not set up after the "
             f"next one, run `buddhi-review setup --recover` {_RECOVERY}, or email {_SUPPORT}. "
             "If you did not pay, nothing was charged.")
_HELD_CHECK = "Checking for a payment from your earlier checkout on this computer."


def test_a_held_buy_code_says_404_once(monkeypatch):
    _accepted(buy=_held_buy())
    ui, _fake = _finish(monkeypatch, {"POST /claim": [(404, {"error": "not_found"})]})
    assert ui.text == [_HELD_CHECK, _HELD_404]
    ui, _fake = _finish(monkeypatch, {"POST /claim": [(404, {"error": "not_found"})]})
    assert ui.text == [_HELD_CHECK] and "buy" in _state()


def test_a_held_buy_code_is_quiet_on_silence(monkeypatch):
    _accepted(buy=_held_buy())
    ui, _fake = _finish(monkeypatch, {"POST /claim": [(429, {"error": "rate_limited"})]})
    assert ui.text == [_HELD_CHECK] and "buy" in _state()


def test_a_held_buy_code_that_was_paid_is_finished(monkeypatch):
    _accepted(buy=_held_buy())
    ui, _fake = _finish(monkeypatch, {"POST /claim": [(202, {"status": "pending"}),
                                                      _claim_200()]})
    assert ui.text == [_HELD_CHECK, _WAITING, _PAID_OK] and "buy" not in _state()


def test_ctrl_c_while_a_held_buy_code_is_checked(monkeypatch):
    _accepted(buy=_held_buy())
    ui, _fake = _finish(monkeypatch, {"POST /claim": [KeyboardInterrupt()]})
    assert ui.lines[-1] == ("info", _BUY_CTRLC) and "buy" in _state()


def test_a_held_buy_code_goes_quiet_after_a_recovery(monkeypatch):
    _accepted(buy=_held_buy(recovered=True))
    ui, _fake = _finish(monkeypatch, {"POST /claim": [(410, {"error": "expired_or_used"})]})
    assert ui.lines == [] and "buy" not in _state()


def test_a_held_buy_code_expires_after_six_days(monkeypatch):
    _accepted(buy=_held_buy(paired_at=pro_trial._now() - 145 * 3600))
    assert not pro_trial.has_held_work()
    # ...unless it was accepted within the last 72 hours
    _accepted(buy=_held_buy(paired_at=pro_trial._now() - 145 * 3600,
                            first_accepted_at=pro_trial._now() - 3600))
    assert pro_trial.has_held_work()


def test_while_the_release_is_closed_no_held_code_is_checked(monkeypatch):
    _accepted(buy=_held_buy(), recover=_held_recovery())
    ui, fake = _finish(monkeypatch, {}, release=_Closed())
    head = ("Buddhi Review Pro cannot be installed into this Python right now: Buddhi Review "
            "could not be reached, no Pro release fits this Python yet, or this Python cannot "
            "install packages with pip.")
    assert ui.text == [
        f"{head} Run `buddhi-review setup` again within the next few minutes to finish moving "
        "your seat, if the move began; if that is too late, run `buddhi-review setup --recover` "
        "again, with a new recovery code.",
        f"{head} If you paid for Buddhi Review Pro from this computer, do not pay again: run "
        "`buddhi-review setup` again later to finish. If it cannot find your payment, run "
        f"`buddhi-review setup --recover` {_RECOVERY}, or email {_SUPPORT}."]
    assert fake.calls == []


def test_an_unfinished_install_is_finished_first(monkeypatch):
    _accepted(install={"kind": "activate", "buyer": False, "at": pro_trial._now()})
    ran = []
    ui, fake = _finish(monkeypatch, {}, runner=lambda cmd, env=None: ran.append(cmd) or
                       types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert ran and ui.lines == [("ok", _TEAMMATE_OK)] and fake.calls == []
    assert "install" not in _state()


def test_an_install_that_did_not_activate_is_not_retried(monkeypatch):
    _accepted(install={"kind": "buy", "buyer": True, "at": pro_trial._now()})
    ran = []

    def runner(cmd, env=None):
        ran.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    ui, _fake = _finish(monkeypatch, {}, runner=runner, is_active=lambda: False)
    assert len(ran) == 1 and ui.lines[-1][1].startswith("Pro installed, but it did not activate")
    assert "install" not in _state() and not pro_trial.has_held_work()
    ui, _fake = _finish(monkeypatch, {}, runner=runner, is_active=lambda: False)
    assert len(ran) == 1 and ui.lines == []


def test_a_held_purchase_whose_install_fails_is_tried_once_per_run(monkeypatch):
    _accepted(buy=_held_buy())
    ran = []

    def runner(cmd, env=None):
        ran.append(cmd)
        return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")

    ui, _fake = _finish(monkeypatch, {"POST /claim": [_claim_200()]}, runner=runner)
    assert len(ran) == 1 and len([t for t in ui.text if t.startswith("Install did not")]) == 1
    assert "install" in _state()          # the next plain setup tries it again
    ui, _fake = _finish(monkeypatch, {}, runner=runner)
    assert len(ran) == 2


def test_the_order_is_recovery_then_buy_then_install(monkeypatch):
    _accepted(recover=_held_recovery(accepted=True, replay_until=pro_trial._now() + 600),
              buy=_held_buy(), install={"kind": "buy", "buyer": True})
    _ui, fake = _finish(monkeypatch, {"POST /recover": [(403, {})], "POST /claim": [(404, {})]})
    assert [c["route"] for c in fake.calls] == ["POST /recover", "POST /claim"]


# ═══════════════════ The command line: setup's three new options ═══════════════

@pytest.mark.parametrize("flag", ["--buy", "--activate", "--recover"])
def test_setup_passes_its_pro_option_to_the_wizard(monkeypatch, flag):
    from buddhi_review import cli, wizard
    seen = []
    monkeypatch.setattr(wizard, "run", lambda argv=None, **k: seen.append(argv) or 0)
    assert cli.main(["setup", flag]) == 0
    assert seen == [[flag]]


def test_setup_options_are_one_at_a_time(capsys):
    from buddhi_review import cli
    for argv in (["setup", "--buy", "--activate"], ["setup", "--repo", "o/r", "--recover"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 2
    capsys.readouterr()


def test_setup_help_names_the_new_options_in_the_approved_words():
    from buddhi_review import cli
    setup = next(a for a in cli.build_parser()._actions
                 if isinstance(a, __import__("argparse")._SubParsersAction)).choices["setup"]
    helps = {a.dest: a.help for a in setup._actions}
    assert helps["buy"] == "buy Buddhi Review Pro in your browser and set it up on this computer"
    assert helps["activate"] == ("set up Buddhi Review Pro with a setup code from the website or "
                                 "from the person who bought your seat")
    assert helps["recover"] == ("move your own Buddhi Review Pro seat to this computer, or "
                                "change your recovery address")


# ═══════════ The licence agreement on every path that ends in a key ════════════

_PATHS = {
    "buy": lambda ui: pro_trial.run_buy(ui, **_kw()),
    "activate": lambda ui: pro_trial.run_activate(ui, **_kw()),
    "recover": lambda ui: pro_trial.run_recover(ui, **_kw()),
}


def _path_ui(path, accept):
    if path == "recover":
        return _choice_ui(ScriptedUi(answers={"": ["1"]}, yes={"Do you accept it?": [accept],
                                                               _MOVE_Q: [False]}))
    return ScriptedUi(yes={"Do you accept it?": [accept]},
                      hidden={"Paste your setup code": [_CODE]})


@pytest.mark.parametrize("path", sorted(_PATHS))
def test_every_paid_path_refuses_without_assent_and_installs_nothing(monkeypatch, tmp_path,
                                                                     path):
    fake = _bridge(monkeypatch, {})
    ui = _path_ui(path, False)
    assert _PATHS[path](ui) == 1
    assert fake.calls == [] and not (tmp_path / ".netrc").exists()
    assert ui.lines[-1] == ("warn", "You did not accept the agreement, so Buddhi Review Pro was "
                                    "not installed. The free version keeps working. To install "
                                    "Pro, run the same command again and accept the agreement.")
    assert "eula" not in _state()


@pytest.mark.parametrize("path", sorted(_PATHS))
def test_every_paid_path_records_the_assent(monkeypatch, path):
    _bridge(monkeypatch, {"POST /claim": [(503, {"error": "unavailable"})]})
    _PATHS[path](_path_ui(path, True))
    assert _state()["eula"]["sha256"] == pro_trial.EULA_SHA256
    assert _state()["eula"]["version"] == "1.1"


@pytest.mark.parametrize("path", sorted(_PATHS))
def test_every_paid_path_asks_again_when_the_text_changed(monkeypatch, path):
    _set_state(eula={"version": "1.0", "sha256": "f" * 64, "accepted_at": "x"})
    _bridge(monkeypatch, {})
    ui = _path_ui(path, False)
    _PATHS[path](ui)
    assert ("info", "The Buddhi Review Pro End User Licence Agreement has changed since you "
                    "last accepted it.") in ui.lines


def test_unreadable_successes_are_never_taken_for_a_fault(monkeypatch):
    """A 200 that carries no key is an answer the tool cannot read: --buy keeps
    waiting and --activate says the service is not answering."""
    _accepted()
    rc, ui, _fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                       "POST /claim": [(200, {"seat": 1}), _claim_200()]})
    assert rc == 0 and _FAULT not in ui.text
    ui = _code_ui()
    rc, _fake = _activate(monkeypatch, {"POST /claim": [(200, {"seat": 2})]}, ui)
    assert rc == 1 and ui.lines[-1] == ("warn", _NOT_ANSWERING)


def test_held_codes_are_kept_until_their_own_expiry():
    """Each held code's hold, counted by the tool: a --buy code 144 h after its pair
    (72 h after it was first accepted, if later), an --activate code 72 h after its
    first request, an unanswered recovery code 15 minutes after its first request."""
    now = pro_trial._now()
    _accepted(buy=_held_buy(paired_at=now - 143 * 3600))
    assert pro_trial.has_held_work()
    _accepted(recover=_held_recovery(first_request_at=now - 14 * 60))
    assert pro_trial.has_held_work()
    state = {"activate": {"code": _CODE, "nonce": "N" * 43, "first_request_at": now - 71 * 3600}}
    assert not pro_trial._expire_holds(state, now) and "activate" in state
    state["activate"]["first_request_at"] = now - 73 * 3600
    assert pro_trial._expire_holds(state, now) and "activate" not in state
    state = {"recover": _held_recovery(accepted=True, replay_until=now + 60,
                                       first_request_at=now - 3600)}
    assert not pro_trial._expire_holds(state, now)           # its replay window still runs
    assert pro_trial._expire_holds(state, now + 61)


# ═════════════ Defects found by the second-model review, each pinned ═══════════

def test_buy_asks_first_when_pro_is_active_with_no_record_of_a_trial(monkeypatch):
    """An install this tool has no record of (an earlier release, a wiped cache) is
    never assumed to be a trial: the second-checkout question is asked."""
    _accepted()
    monkeypatch.setattr(pro_trial, "pro_backend_active", lambda **k: True)
    question = ("Buddhi Review Pro is already active on this computer. Buy another "
                "subscription?")
    rc, ui, fake = _buy(monkeypatch, {}, ui=ScriptedUi(yes={question: [False]}))
    assert rc == 0 and question in ui.asked and fake.calls == []


@pytest.mark.parametrize("held", ["buy", "recover", "install"])
def test_a_plain_setup_resumes_held_work_only_with_current_assent(monkeypatch, held):
    work = {"buy": _held_buy(), "recover": _held_recovery(),
            "install": {"kind": "buy", "buyer": True, "at": pro_trial._now()}}[held]
    _set_state(eula={"version": "1.0", "sha256": "e" * 64, "accepted_at": "x"}, **{held: work})
    ui, fake = _finish(monkeypatch, {}, ui=ScriptedUi(yes={"Do you accept it?": [False]}),
                       runner=lambda *a, **k: pytest.fail("installed"))
    assert fake.calls == [] and _state()[held] == work           # everything stays held
    assert ui.lines[-1] == ("warn", "You did not accept the agreement, so Buddhi Review Pro was "
                                    "not installed. The free version keeps working. To install "
                                    "Pro, run the same command again and accept the agreement.")
    ui, fake = _finish(monkeypatch, {}, ui=ScriptedUi(interactive=False))
    assert fake.calls == [] and ui.lines[-1][1].startswith(
        "Buddhi Review Pro was not installed: its licence agreement must be accepted in a "
        "terminal.")


def test_a_recovery_answered_200_keeps_its_replay_window_when_the_key_cannot_be_saved(
        monkeypatch, tmp_path):
    _accepted(recover=_held_recovery(first_request_at=pro_trial._now() - 14 * 60))
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    answer = _complete(_ready("sub_1", _KEY))
    answer[1]["replay_until"] = "2099-01-01T00:00:00Z"
    _finish(monkeypatch, {"POST /recover": [answer], "GET /seats": [(200, {"reasons": []})]},
            netrc_path=blocker / ".netrc")
    hold = _state()["recover"]
    assert hold["accepted"] is True and hold["replay_until"] > pro_trial._now() + 86400
    assert pro_trial.has_held_work()             # still held well past the 15 minutes


def test_ctrl_c_during_the_final_address_change_never_says_nothing_changed(monkeypatch):
    ui = _address_ui()
    rc, fake = _recover(monkeypatch, _address_routes(**{
        "POST /recover/address": [KeyboardInterrupt(), (200, {"status": "changed"})]}), ui)
    assert rc == 0 and fake.count("POST /recover/address") == 2
    assert ui.lines[-1] == ("ok", f"Your recovery address is now {_NEW}. Recovery codes now go "
                                  "only there.")
    assert _STOPPED not in ui.text and "Your recovery address was not changed." not in ui.text


def test_recovery_prefers_a_seat_read_as_active_over_one_it_could_not_read(monkeypatch,
                                                                            tmp_path):
    _accepted()
    ui = _choice_ui(_recover_ui())
    unread_then_active = lambda body, auth, query: (
        (503, {"error": "unavailable"}) if query["subscription_id"] == "sub_1"
        else (200, {"reasons": []}))
    rc, _fake = _recover(monkeypatch, {
        "POST /recover/code": [(202, {"status": "sent"})],
        "POST /recover": [_complete(_ready("sub_1", "KEY-1"), _ready("sub_2", "KEY-2"))],
        "GET /seats": unread_then_active}, ui)
    assert rc == 0 and "password KEY-2" in _netrc_text(tmp_path)
    assert "This computer runs Pro under subscription sub_2." in ui.text
    # neither seat is described as paused when its state could not be read
    assert "Subscription sub_1: seat 1 is now on this computer." in ui.text


def test_a_failed_state_write_keeps_the_held_code(monkeypatch):
    """The hold is dropped in the same write that records the unfinished install: when
    that write fails, the code stays held, so a later setup can still continue."""
    _accepted()
    real = pro_trial._save_state
    monkeypatch.setattr(pro_trial, "_save_state", lambda path, state: "No space left on device"
                        if "install" in state else real(path, state))
    rc, _ui, _fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                        "POST /claim": [_claim_200()]},
                          runner=lambda cmd, env=None: types.SimpleNamespace(
                              returncode=1, stdout="", stderr="boom"))
    assert rc == 1 and "buy" in _state() and pro_trial.has_held_work()


def test_after_the_payment_is_seen_silence_ends_in_the_finishing_line(monkeypatch):
    _accepted()
    monkeypatch.setattr(pro_trial, "_mono", _Clock(30.0))
    rc, ui, _fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                       "POST /claim": [(202, {"status": "pending"}),
                                                       (503, {"error": "unavailable"})]})
    assert rc == 1 and ui.lines[-1] == ("warn", (
        "Your payment was received, but setup is not finished yet. Do not pay again. Run "
        "`buddhi-review setup` again in a few minutes; it finishes this purchase."))
    assert not any("We could not see a payment yet" in t for t in ui.text)


def test_ctrl_c_during_the_install_after_a_claim_says_nothing_untrue(monkeypatch):
    """Once the claim is answered the code is used and dropped: "paste the same code"
    and "nothing was used" would both be false. Nothing is said; the install is
    recorded, and the next plain setup finishes it."""
    _accepted()

    def interrupted(cmd, env=None):
        raise KeyboardInterrupt
    ui = _code_ui()
    rc, _fake = _activate(monkeypatch, {"POST /claim": [_claim_200(secret=False)]}, ui,
                          runner=interrupted)
    assert rc == 1 and ui.lines == []
    assert _state()["install"]["kind"] == "activate" and "activate" not in _state()


# ═══════════════════ Unreadable files, pending installs, pauses, Ctrl-C ═══════════════

_REC_ACCEPTED = (
    "Your recovery was accepted, but this computer is not set up yet. Any other computer "
    "that held seat 1 of your subscriptions is already losing it. Run `buddhi-review setup` "
    "within the next few minutes to finish here; if that is too late, run `buddhi-review "
    "setup --recover` again, with a new recovery code.")
_BUY_CTRLC = ("Stopped waiting. If you paid, do not pay again: run `buddhi-review setup` to "
              f"finish. If it cannot find your payment, run `buddhi-review setup --recover` "
              f"{_RECOVERY}.")


def _naps(monkeypatch):
    naps = []
    monkeypatch.setattr(pro_trial, "_nap", naps.append)
    return naps


def test_a_seat_access_file_that_is_not_text_is_left_alone_and_never_shown(monkeypatch):
    """Its bytes may hold a manage secret: an undecodable file is 'unreadable', with a
    reason that never quotes it."""
    path = pro_trial._seat_admin_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (b'{"schema": 1, "entries": {"https://api.buddhireview.com sub_9": '
           b'{"secret": "SECRET-THAT-MUST-NOT-SHOW", "version": 1}}}\xff\xfe')
    path.write_bytes(raw)
    failure = pro_trial.save_seat_admin([("sub_1", _SECRET, 2)])
    assert failure == ("unreadable", "it is not valid JSON")
    assert path.read_bytes() == raw
    line = pro_trial._seat_admin_line(failure, installed=True)
    assert "SECRET" not in line and "\\x" not in line
    # …and through a recovery, which shows that line and still installs
    _accepted()
    ui = _choice_ui(_recover_ui())
    rc, _fake = _recover(monkeypatch, {"POST /recover/code": [(202, {"status": "sent"})],
                                       "POST /recover": [_complete(_ready("sub_1", _KEY))],
                                       "GET /seats": [(200, {"reasons": []})]}, ui)
    assert rc == 0 and ui.lines[-1] == ("warn", (
        f"The seat access file {path} could not be read (it is not valid JSON), so it was "
        "left as it is and your seat access was not saved. Move it aside, then run "
        "`buddhi-review setup --recover`."))
    assert not any("SECRET" in t for t in ui.text)


def test_buy_asks_first_when_a_paid_install_waits_over_an_old_trial_record(monkeypatch):
    """A trial computer that bought, then stopped mid-install: the paid key is in
    ~/.netrc and Pro may run under it, so the trial record no longer counts."""
    _accepted(last_install_kind="trial",
              install={"kind": "buy", "buyer": True, "at": pro_trial._now()})
    monkeypatch.setattr(pro_trial, "pro_backend_active", lambda **k: True)
    question = ("Buddhi Review Pro is already active on this computer. Buy another "
                "subscription?")
    rc, ui, fake = _buy(monkeypatch, {}, ui=ScriptedUi(yes={question: [False]}))
    assert rc == 0 and ui.lines == [("info", "Nothing was changed.")] and fake.calls == []
    # a trial install still waiting is a trial: never asked
    _accepted(last_install_kind="trial",
              install={"kind": "trial", "buyer": True, "at": pro_trial._now()})
    rc, ui, _fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                       "POST /claim": [_claim_200()]})
    assert rc == 0 and question not in ui.asked


@pytest.mark.parametrize("kind", ["buy", "activate", "recover", "an-older-kind"])
def test_no_trial_is_offered_over_an_unfinished_paid_install(kind):
    pro_trial._write_state(None, {"install": {"kind": kind, "buyer": True,
                                              "at": pro_trial._now()}})
    assert pro_trial.offer_allowed() is False
    ui = ScriptedUi()
    assert pro_trial.trial_step(ui, release=types.SimpleNamespace(is_open=lambda: True)) is False
    assert ui.lines == [] and ui.asked == []
    # an unfinished trial install does not stop the offer
    pro_trial._write_state(None, {"install": {"kind": "trial", "buyer": True,
                                              "at": pro_trial._now()}})
    assert pro_trial.offer_allowed() is True


@pytest.mark.parametrize("kind", ["buy", "activate", "recover"])
def test_no_trial_is_offered_over_a_finished_paid_install_that_reads_inactive(kind):
    """A paused or disputed subscription reads as inactive, yet its key is still the
    one in ~/.netrc: a trial key must not replace it."""
    pro_trial._write_state(None, {"last_install_kind": kind})
    assert pro_trial.offer_allowed(backends=[]) is False
    pro_trial._write_state(None, {"last_install_kind": "trial"})
    assert pro_trial.offer_allowed(backends=[]) is True


def test_a_long_retry_after_is_never_cut_short(monkeypatch):
    """Maintenance asks for 5 minutes: the tool does not retry sooner; it stops at once
    with the not-answering line."""
    _accepted()
    naps = _naps(monkeypatch)
    rc, ui, fake = _buy(monkeypatch, {"POST /pair": [(503, {"error": "maintenance"}, 300)]})
    assert rc == 1 and fake.count("POST /pair") == 1 and naps == []
    assert ui.lines[-1] == ("warn", _NOT_ANSWERING)
    # a short one is waited in full before each retry
    naps.clear()
    rc, ui, fake = _buy(monkeypatch, {"POST /pair": [(503, {"error": "unavailable"}, 7)]})
    assert fake.count("POST /pair") == 3 and naps == [7.0, 7.0]


def test_polls_wait_the_full_retry_after(monkeypatch):
    _accepted()
    naps = _naps(monkeypatch)
    rc, _ui, _fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                        "POST /claim": [(429, {"error": "rate_limited"}, 60),
                                                        _claim_200()]})
    assert rc == 0 and naps == [60.0]


def test_a_pause_past_the_end_of_the_wait_stops_with_its_line(monkeypatch):
    """Two minutes after a 202, a 300 s pause would outlast the wait: the finishing line
    at once, no pause."""
    _accepted()
    monkeypatch.setattr(pro_trial, "_mono", _Clock(1.0))     # time moves, so a wait ends
    naps = _naps(monkeypatch)
    rc, ui, _fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                       "POST /claim": [(202, {"status": "pending"}, 300)]})
    assert rc == 1 and naps == [] and ui.lines[-1] == ("warn", (
        "Your payment was received, but setup is not finished yet. Do not pay again. Run "
        "`buddhi-review setup` again in a few minutes; it finishes this purchase."))
    # before any payment is seen, the same ends in the not-seen line
    _accepted()
    rc, ui, _fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                       "POST /claim": [(503, {"error": "maintenance"}, 3600)]})
    assert rc == 1 and naps == [] and ui.lines[-1][1].startswith(
        "We could not see a payment yet.")


def test_activate_and_recovery_pauses_past_their_wait_stop_with_their_lines(monkeypatch):
    _accepted()
    monkeypatch.setattr(pro_trial, "_mono", _Clock(1.0))     # time moves, so a wait ends
    naps = _naps(monkeypatch)
    ui = _code_ui()
    rc, _fake = _activate(monkeypatch, {"POST /claim": [(202, {"status": "pending"}, 300)]}, ui)
    assert rc == 1 and naps == [] and ui.lines[-1] == ("warn", (
        "Setup is not finished yet. Run `buddhi-review setup --activate` again in a few "
        "minutes and paste the same code. If you bought Pro, do not pay again."))
    ui = _code_ui()
    rc, _fake = _activate(monkeypatch, {"POST /claim": [(404, {"error": "not_found"}, 300)]},
                          ui)
    assert rc == 1 and naps == [] and ui.lines[-1][1].startswith(
        "We could not find a purchase for this setup code yet.")
    pending = (202, {"status": "pending", "replay_until": "2099-01-01T00:00:00Z",
                     "subscriptions": [{"subscription_id": "sub_1", "manage_secret": _SECRET,
                                        "manage_secret_version": 3,
                                        "seat": {"status": "pending"}}]}, 300)
    ui = _choice_ui(_recover_ui())
    rc, fake = _recover(monkeypatch, {"POST /recover/code": [(202, {"status": "sent"})],
                                      "POST /recover": [pending]}, ui)
    assert rc == 1 and naps == [] and fake.count("POST /recover") == 1
    assert ui.text[-1] == _REC_ACCEPTED


def test_ctrl_c_while_an_earlier_checkout_is_checked_says_not_to_pay_again(monkeypatch):
    _accepted(buy={"code": "C" * 43, "nonce": "N" * 43, "paired_at": pro_trial._now()})
    rc, ui, _fake = _buy(monkeypatch, {"POST /claim": [KeyboardInterrupt()]})
    assert rc == 1 and ui.lines[-1] == ("info", _BUY_CTRLC) and _STOPPED not in ui.text
    assert "buy" in _state()


def test_ctrl_c_after_a_recovery_was_accepted_says_so(monkeypatch):
    _accepted()
    ui = _choice_ui(_recover_ui())
    rc, _fake = _recover(monkeypatch, {"POST /recover/code": [(202, {"status": "sent"})],
                                       "POST /recover": [_complete(_ready("sub_1", _KEY))],
                                       "GET /seats": [KeyboardInterrupt()]}, ui)
    assert rc == 1 and ui.text[-1] == _REC_ACCEPTED and _STOPPED not in ui.text
    assert _state()["recover"]["accepted"] is True


# ═══════════════ --buy while an earlier setup is unfinished and Pro is off ════════════

_OVER_UNFINISHED = ("An earlier Buddhi Review Pro setup on this computer may be unfinished. "
                    "To continue it, run `buddhi-review setup`. Start a new checkout anyway?")
_ALREADY_ACTIVE = ("Buddhi Review Pro is already active on this computer. Buy another "
                   "subscription?")


def _pending(kind="buy", **values):
    _set_state(install={"kind": kind, "buyer": True, "at": pro_trial._now()}, **values)


@pytest.mark.parametrize("kind", ["buy", "activate", "recover", "an-older-kind"])
def test_buy_over_an_unfinished_paid_setup_asks_first_and_a_no_changes_nothing(monkeypatch,
                                                                               kind):
    """Pro is not running and a paid key waits to be installed: a new checkout could be
    a second payment. Asked before the licence agreement and before any write; No
    changes nothing and sends nothing."""
    _pending(kind)
    path = pro_trial._state_path()
    before = path.read_bytes()
    ui = ScriptedUi(yes={_OVER_UNFINISHED: [False]})
    rc, ui, fake = _buy(monkeypatch, {}, ui=ui)
    assert rc == 0 and ui.asked == [_OVER_UNFINISHED] and ui.defaults[_OVER_UNFINISHED] is False
    assert ui.lines == [("info", "Nothing was changed.")]
    assert fake.calls == [] and path.read_bytes() == before      # no request, no write
    assert "Do you accept it?" not in ui.asked                    # asked before the agreement


def test_buy_over_an_unfinished_paid_setup_continues_on_yes(monkeypatch):
    _pending("buy", eula={"version": "1.1", "sha256": pro_trial.EULA_SHA256,
                          "accepted_at": "2026-10-18T00:00:00Z"})
    rc, ui, fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                      "POST /claim": [_claim_200()]},
                        ui=ScriptedUi(yes={_OVER_UNFINISHED: [True]}))
    assert rc == 0 and ui.asked[0] == _OVER_UNFINISHED and fake.count("POST /pair") == 1
    assert ui.lines[-1] == ("ok", _PAID_OK)


def test_buy_over_an_unfinished_setup_with_pro_running_asks_the_usual_question(monkeypatch):
    _pending("buy")
    _accepted(install={"kind": "buy", "buyer": True, "at": pro_trial._now()})
    monkeypatch.setattr(pro_trial, "pro_backend_active", lambda **k: True)
    rc, ui, fake = _buy(monkeypatch, {}, ui=ScriptedUi(yes={_ALREADY_ACTIVE: [False]}))
    assert rc == 0 and ui.asked == [_ALREADY_ACTIVE] and fake.calls == []
    assert ui.lines == [("info", "Nothing was changed.")]


@pytest.mark.parametrize("state", [{}, {"install": {"kind": "trial", "buyer": True}}],
                         ids=["nothing-unfinished", "an-unfinished-trial"])
def test_buy_with_no_unfinished_paid_setup_is_not_asked(monkeypatch, state):
    _accepted(**state)
    rc, ui, fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                      "POST /claim": [_claim_200()]})
    assert rc == 0 and _OVER_UNFINISHED not in ui.asked and fake.count("POST /pair") == 1


@pytest.mark.parametrize("answer", [EOFError, "", "maybe"],
                         ids=["end-of-input", "empty", "not-yes"])
def test_buy_over_an_unfinished_setup_off_a_terminal_takes_anything_but_yes_as_no(
        monkeypatch, answer):
    """Off a terminal the wizard's own yes/no control falls back to a plain text prompt,
    like every other yes/no question: end of input, an empty line or any answer but yes
    is No."""
    import io
    from buddhi_review import wizard
    _pending("buy")
    path = pro_trial._state_path()
    before = path.read_bytes()
    fake = _bridge(monkeypatch, {})
    monkeypatch.setattr(wizard, "_is_tty", lambda: False)
    asked, out = [], io.StringIO()

    def input_fn(prompt=""):
        asked.append(prompt)
        if isinstance(answer, type):
            raise answer
        return answer
    ui = wizard._acquisition_ui(pal=wizard._Palette(False), stream=out, input_fn=input_fn)
    assert pro_trial.run_buy(ui, **_kw()) == 0
    assert asked == [f"  {_OVER_UNFINISHED} [y/N]: "]
    assert "Nothing was changed." in out.getvalue()
    assert fake.calls == [] and path.read_bytes() == before


def test_buy_over_an_unfinished_setup_with_pro_running_never_asks_both(monkeypatch):
    """Pro running: only the usual question, and a yes to it goes straight on."""
    _accepted(install={"kind": "buy", "buyer": True, "at": pro_trial._now()})
    monkeypatch.setattr(pro_trial, "pro_backend_active", lambda **k: True)
    rc, ui, fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                      "POST /claim": [_claim_200()]},
                        ui=ScriptedUi(yes={_ALREADY_ACTIVE: [True]}))
    assert rc == 0 and ui.asked == [_ALREADY_ACTIVE] and fake.count("POST /pair") == 1


def test_ctrl_c_at_the_unfinished_setup_question_changes_nothing(monkeypatch):
    _pending("buy")
    path = pro_trial._state_path()
    before = path.read_bytes()
    rc, ui, fake = _buy(monkeypatch, {},
                        ui=ScriptedUi(yes={_OVER_UNFINISHED: [KeyboardInterrupt]}))
    assert rc == 1 and ui.lines == [("info", _STOPPED)]
    assert fake.calls == [] and path.read_bytes() == before


def test_setup_buy_from_the_command_line_asks_before_a_second_checkout(monkeypatch, capsys):
    """The whole path a user runs: `buddhi-review setup --buy` → the CLI → the wizard →
    run_buy, with the wizard's own terminal reading standard input (off a TTY here, so
    a text prompt)."""
    import io
    import sys
    from buddhi_review import cli, wizard
    _pending("buy")
    path = pro_trial._state_path()
    before = path.read_bytes()
    fake = _bridge(monkeypatch, {})
    monkeypatch.setattr(wizard, "_is_tty", lambda: False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))          # no answer: No
    rc = cli.main(["setup", "--buy"])
    out = capsys.readouterr().out
    assert rc == 0 and f"  {_OVER_UNFINISHED} [y/N]: " in out
    assert "Do you accept it?" not in out and "Nothing was changed." in out
    assert fake.calls == [] and path.read_bytes() == before
    # …and a typed yes goes on to the licence agreement, which off a terminal stops with
    # its own approved line
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    assert cli.main(["setup", "--buy"]) == 1
    out = capsys.readouterr().out
    assert out.index(f"{_OVER_UNFINISHED} [y/N]: ") < out.index(
        "Buddhi Review Pro was not installed: its licence agreement must be accepted in a "
        "terminal. Run the same command in a terminal to read and accept it.")
    assert fake.calls == []


# ═══════════════════ --buy over a held recovery (still within its hold) ═══════════════════



def _accepted_recovery():
    return _held_recovery(accepted=True, replay_until=pro_trial._now() + 600)


@pytest.mark.parametrize("recover", ["accepted", "not-yet-answered"])
def test_buy_over_a_held_recovery_asks_first_and_a_no_changes_nothing(monkeypatch, recover):
    """A recovery the service accepted (or has not answered yet, inside its 15
    minutes) is finished by a plain setup; a new checkout over it could be a second
    payment."""
    _set_state(recover=_accepted_recovery() if recover == "accepted" else _held_recovery())
    path = pro_trial._state_path()
    before = path.read_bytes()
    rc, ui, fake = _buy(monkeypatch, {}, ui=ScriptedUi(yes={_OVER_UNFINISHED: [False]}))
    assert rc == 0 and ui.asked == [_OVER_UNFINISHED] and ui.defaults[_OVER_UNFINISHED] is False
    assert ui.lines == [("info", "Nothing was changed.")]
    assert fake.calls == [] and path.read_bytes() == before
    assert "Do you accept it?" not in ui.asked


def test_buy_over_a_held_recovery_continues_on_yes(monkeypatch):
    _accepted(recover=_accepted_recovery())
    rc, ui, fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                      "POST /claim": [_claim_200()]},
                        ui=ScriptedUi(yes={_OVER_UNFINISHED: [True]}))
    assert rc == 0 and ui.asked[0] == _OVER_UNFINISHED and fake.count("POST /pair") == 1


def test_buy_over_a_recovery_past_its_hold_is_not_asked(monkeypatch):
    _accepted(recover=_held_recovery(accepted=True, replay_until=pro_trial._now() - 1))
    rc, ui, fake = _buy(monkeypatch, {"POST /pair": [(201, {"phrase": _PHRASE})],
                                      "POST /claim": [_claim_200()]})
    assert rc == 0 and _OVER_UNFINISHED not in ui.asked and fake.count("POST /pair") == 1


def test_buy_over_a_held_recovery_during_a_trial_asks_the_unfinished_question(monkeypatch):
    """Pro running under a trial is never asked the second-subscription question, so a
    held recovery still gets the unfinished-setup one."""
    _accepted(last_install_kind="trial", recover=_accepted_recovery())
    monkeypatch.setattr(pro_trial, "pro_backend_active", lambda **k: True)
    rc, ui, fake = _buy(monkeypatch, {}, ui=ScriptedUi(yes={_OVER_UNFINISHED: [False]}))
    assert rc == 0 and ui.asked == [_OVER_UNFINISHED] and fake.calls == []


def test_held_codes_expire_with_one_rule():
    """Every held code expires by one rule, the one the unfinished-setup question reads
    for a held recovery: a --buy code 144 h after its pair (72 h after it was first
    accepted, if later), an --activate code 72 h after its first request, a recovery
    code at its replay deadline (15 minutes after its first request while unanswered);
    a record that holds no code is expired. An expired code is dropped; a live one is
    kept."""
    now = pro_trial._now()
    cases = [
        ("buy", {"code": "C" * 43, "paired_at": now - 143 * 3600}, True),
        ("buy", {"code": "C" * 43, "paired_at": now - 144 * 3600}, False),
        ("buy", {"code": "C" * 43, "paired_at": now - 200 * 3600,
                 "first_accepted_at": now - 71 * 3600}, True),
        ("buy", {"code": "C" * 43, "paired_at": now - 200 * 3600,
                 "first_accepted_at": now - 72 * 3600}, False),
        ("activate", {"code": "A" * 43, "first_request_at": now - 71 * 3600}, True),
        ("activate", {"code": "A" * 43, "first_request_at": now - 72 * 3600}, False),
        ("recover", _held_recovery(accepted=True, replay_until=now + 1), True),
        ("recover", _held_recovery(accepted=True, replay_until=now), False),
        ("recover", _held_recovery(first_request_at=now - 14 * 60), True),
        ("recover", _held_recovery(first_request_at=now - 15 * 60), False),
        ("recover", _held_recovery(code=None), False),
        ("buy", {"paired_at": now}, False),
    ]
    for kind, hold, alive in cases:
        assert pro_trial._hold_expired(kind, hold, now) is (not alive), (kind, hold)
        state = {kind: hold, "eula": {"version": "1.1"}}
        assert pro_trial._expire_holds(state, now) is (not alive), (kind, hold)
        assert (kind in state) is alive and "eula" in state
        if kind == "recover":
            assert pro_trial._recovery_unfinished({kind: hold}, now) is alive


def test_setup_buy_from_the_command_line_asks_over_a_held_recovery(monkeypatch, capsys):
    import io
    import sys
    from buddhi_review import cli, wizard
    monkeypatch.setattr(wizard, "_is_tty", lambda: False)
    _set_state(recover=_accepted_recovery())
    path = pro_trial._state_path()
    before = path.read_bytes()
    fake = _bridge(monkeypatch, {})
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(["setup", "--buy"]) == 0
    out = capsys.readouterr().out
    assert f"  {_OVER_UNFINISHED} [y/N]: " in out and "Nothing was changed." in out
    assert "Do you accept it?" not in out
    assert fake.calls == [] and path.read_bytes() == before
    # …and a typed yes goes on to the licence agreement (off a terminal: its own line)
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    assert cli.main(["setup", "--buy"]) == 1
    out = capsys.readouterr().out
    assert out.index(f"{_OVER_UNFINISHED} [y/N]: ") < out.index(
        "Buddhi Review Pro was not installed: its licence agreement must be accepted in a "
        "terminal.")
    assert fake.calls == []
