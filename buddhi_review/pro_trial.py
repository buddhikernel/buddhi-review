"""pro_trial.py — the free tool's Buddhi Review Pro ACQUISITION plumbing.

This is the ONE sanctioned licence-ACQUISITION module in the free tree: every path
that gets this computer a Buddhi Review Pro licence key routes here, and here ONLY.
None of them makes a key, and no person ever sees, types or pastes one: a key comes
machine to machine from the Buddhi Review service (:data:`BRIDGE_ORIGIN`), goes into
``~/.netrc`` (0600) for pip, and the installed paid package activates this computer.

  * **the trial** (the setup wizard's trial step) — an email, a 6-digit code the
    service emails (``POST /trial/code``), then ``POST /trial`` with that code and a
    claim nonce kept in this module's 0600 state;
  * **buying from the tool** (``setup --buy``) — a 32-byte setup code made here, its
    hash registered (``POST /pair``), the checkout opened in the browser, then
    ``POST /claim`` polled with a claim nonce until the payment's seat has a key;
  * **a setup code from the website or a teammate** (``setup --activate``) — the code
    read at a hidden prompt (or, with consent, from the clipboard), then
    ``POST /claim``;
  * **recovery** (``setup --recover``) — a 16-character code emailed to the buyer's
    recovery address (``POST /recover/code``), then ``POST /recover``, which moves
    seat 1 of each subscription here and rotates its manage secret (kept in
    ``~/.config/buddhi/seat-admin.json``); and the recovery address's own change,
    proven at both inboxes;
  * **the install tail** they share — the index credential in ``~/.netrc``, ``pip
    install`` from the licence-gated index, and the paid daemon started through
    backend discovery.

Nothing here runs until a public route says the paid package is published with a
wheel this interpreter can install (:class:`ReleaseCheck`; closed on any doubt), and
no key is used before the user has accepted the Buddhi Review Pro End User Licence
Agreement in a terminal (:func:`_eula_gate`).

Every line these paths show is the approved wording of the purchase-to-activation
design's on-screen set, word for word.

⛔ **ACQUISITION ONLY — zero entitlement ENFORCEMENT.** Nothing here checks an
entitlement at runtime — no cryptographic verification, no runtime gate, no expiry
math; the compiled paid package checks itself. ``tests/test_oss_purity.py`` asserts
this module holds acquisition calls only, and the publish gate allowlists it for
the acquisition vocabulary (the licence index host + the ``buddhi-review-pro``
package name it installs) while still scanning it for every other paid surface.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import inspect
import json
import math
import os
import platform
import re
import secrets
import shlex
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from buddhi_review import netrc_writer

# ── Where things are (all NON-secret) ─────────────────────────────────────────────
# The Buddhi Review service every acquisition request goes to, and nothing else.
BRIDGE_ORIGIN = "https://api.buddhireview.com"
# The licence-gated private index (the key is the index password) + the pip package.
# The account slug is overridable so a QA account can be pointed at without an edit.
_ACCOUNT = os.environ.get("BUDDHI_KEYGEN_ACCOUNT", "buddhi")
_INDEX_HOST = "pypi.pkg.keygen.sh"
_INDEX_LOGIN = "license"
_PACKAGE = "buddhi-review-pro"

# The commercial site's checkout page and the support mailbox. The commercial domain
# is buddhireview.com; the open-source kernel's site is never a place to buy.
# tests/test_commercial_links.py fails the build if a shipped file names the kernel's
# site, or if a shipped email address is off that domain.
CHECKOUT_URL = "https://buddhireview.com/buy"
SUPPORT_EMAIL = "support@buddhireview.com"
_CODES_SENDER = "codes@buddhireview.com"

# The Buddhi Review Pro End User Licence Agreement the user accepts before any key is
# used: its version, its effective date, where it is published, and the SHA-256 of
# its signed text. A change of text is a new SHA-256, and assent is asked again.
EULA_VERSION = "1.1"
EULA_EFFECTIVE = "4 October 2026"
EULA_URL = "https://buddhireview.com/eula"
EULA_SHA256 = "a7e6899db2e9024f34b67cfd085a6f6ef8b4ddc44a8ec301ed144715cf68a2cc"

# ── Approved wording used by more than one path ──────────────────────────────────
OFFER_ROW = ("Try Buddhi Review Pro — automation that carries a run like this to the "
             "finish on its own.")
OFFER_QUESTION = "Start or continue your free 14-day Pro trial?"
UPGRADE_POINTER = ("To buy Pro, run `buddhi-review setup --buy`. If you have a setup code, "
                   "run `buddhi-review setup --activate`. To move your own seat from another "
                   "computer to this one, run `buddhi-review setup --recover`.")
_RECOVERY_PHRASE = ("with your recovery address (the email you first paid with, unless you "
                    "changed it; if you paid with two different emails, run it once with each)")
_STOPPED = "Setup stopped. Nothing was bought, used or changed."
_NOTHING_CHANGED = "Nothing was changed."
_NOT_ANSWERING = ("The Buddhi Review service is not answering right now. Run the same "
                  "command again in a few minutes.")
_FAULT = ("Buddhi Review did not accept this request. This points to a fault in this "
          "version of Buddhi Review, not in anything you did. Run `buddhi-review upgrade`, "
          f"then try again. If it still fails, email {SUPPORT_EMAIL}.")
_CLOSED_HEAD = ("Buddhi Review Pro cannot be installed into this Python right now: Buddhi "
                "Review could not be reached, no Pro release fits this Python yet, or this "
                "Python cannot install packages with pip.")
_CLOSED = (f"{_CLOSED_HEAD} This command changed nothing. Try again later, or email "
           f"{SUPPORT_EMAIL}.")
_WAITING = ("Payment received. Finishing Buddhi Review Pro setup — up to 2 minutes. Keep "
            "this window open. Do not pay again.")
_EXPIRED_OR_USED = ("This setup code has expired or was already used. If you bought Pro, run "
                    "`buddhi-review setup --recover`; if someone gave you this code, ask them "
                    "for a new one.")
_EMAIL_RETRY = "That doesn't look like an email address — try again."
_NO_RECOVERY_CODE = ("No code yet? It can take a few minutes, so check your spam folder, and "
                     "check that you entered the email you first paid with (or the address you "
                     "changed it to). If none arrives, run `buddhi-review setup --recover` "
                     f"again later, or email {SUPPORT_EMAIL} and we will help.")


def _too_many(seconds: Optional[float]) -> str:
    return ("Too many requests have been made for now. Run the same command again in "
            f"about {_time_phrase(seconds)}.")


def _unsaved(path: Path, why: str) -> str:
    return (f"Buddhi Review could not save its setup details in {path} ({why}), so this "
            "command changed nothing. Fix that folder's permissions, then run the same "
            "command again.")


def _time_phrase(seconds: Optional[float]) -> str:
    """An answer's ``Retry-After`` rounded up and written *1 minute*, *<n> minutes*,
    *1 hour* or *<n> hours*; *a few minutes* when the answer has none."""
    if seconds is None:
        return "a few minutes"
    minutes = max(1, math.ceil(seconds / 60))
    if minutes < 60:
        return "1 minute" if minutes == 1 else f"{minutes} minutes"
    hours = math.ceil(seconds / 3600)
    return "1 hour" if hours == 1 else f"{hours} hours"


def _join_ids(ids: List[str]) -> str:
    if len(ids) <= 1:
        return "".join(ids)
    return f"{', '.join(ids[:-1])} and {ids[-1]}"


# ── Clocks (seams the tests replace) ─────────────────────────────────────────────
_now: Callable[[], float] = time.time          # wall clock: what the state file records
_mono: Callable[[], float] = time.monotonic     # how long a wait has lasted
_nap: Callable[[float], None] = time.sleep


# ════════════════════════════ The bridge client ════════════════════════════════
# Plain JSON over HTTPS to BRIDGE_ORIGIN only: no redirect is followed, every request
# is bounded in time and size, and every failure becomes an answer the paths below
# read (status 0 = no answer), never a traceback. ``_bridge_transport`` is the one
# seam: ``callable(method, url, body, auth) -> (status, data, retry_after)``, where
# ``body`` is a JSON-able dict or None and ``auth`` a full Authorization value or
# None. The test suite points it at a fake bridge, so no test reaches the network.

_BRIDGE_TIMEOUT_S = 35.0          # the service holds /recover and the seat routes up to 25 s
_BRIDGE_WALL_S = _BRIDGE_TIMEOUT_S + 10.0
_BRIDGE_MAX_BYTES = 64 * 1024
_bridge_transport: Optional[Callable] = None


class BridgeAnswer:
    """One answer from the service: ``status`` (0 when there was none), the JSON
    object it carried (or None), and its ``Retry-After`` in seconds (or None)."""

    __slots__ = ("status", "data", "retry_after")

    def __init__(self, status: int, data: Optional[dict], retry_after: Optional[float]):
        self.status, self.data, self.retry_after = status, data, retry_after

    @property
    def error(self) -> str:
        return str(self.data.get("error") or "") if isinstance(self.data, dict) else ""

    def __repr__(self):  # pragma: no cover - debug aid
        return f"BridgeAnswer({self.status}, {self.error or '-'})"


def _seconds(value) -> Optional[float]:
    try:
        s = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return s if math.isfinite(s) and s >= 0 else None


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the answer must come from the route itself, so any
    3xx surfaces as an ``HTTPError`` and reads as closed (or as no answer)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _https_once(method: str, url: str, body: Optional[dict], auth: Optional[str]):
    opener = urllib.request.build_opener(
        _RefuseRedirects, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    headers = {"Accept": "application/json", "User-Agent": "buddhi-review-setup"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if auth:
        headers["Authorization"] = auth
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = opener.open(req, timeout=_BRIDGE_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        resp = exc
    except Exception:
        return 0, None, None
    try:
        with resp:
            status = int(getattr(resp, "status", None) or resp.getcode() or 0)
            retry = resp.headers.get("Retry-After") if resp.headers is not None else None
            raw = resp.read(_BRIDGE_MAX_BYTES + 1)
    except Exception:
        return 0, None, None
    if len(raw) > _BRIDGE_MAX_BYTES:
        return status, None, retry
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else None
    except Exception:
        payload = None
    return status, payload, retry


def _https_json(method: str, url: str, body: Optional[dict], auth: Optional[str]):
    """One request, bounded by a wall-clock cap (urllib's own timeout bounds each
    socket operation, never DNS or a trickling body). A Ctrl-C while it waits still
    reaches the caller."""
    box: list = [None]

    def _work() -> None:
        box[0] = _https_once(method, url, body, auth)

    worker = threading.Thread(target=_work, name="buddhi-bridge-request", daemon=True)
    try:
        worker.start()
    except Exception:
        return 0, None, None
    worker.join(_BRIDGE_WALL_S)
    return box[0] if box[0] is not None else (0, None, None)


def _bridge_call(method: str, path: str, body: Optional[dict] = None, *,
                 auth: Optional[str] = None, query: Optional[dict] = None) -> BridgeAnswer:
    url = f"{BRIDGE_ORIGIN}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    fn = _bridge_transport or _https_json
    try:
        status, data, retry = fn(method, url, body, auth)
        status = int(status or 0)
    except Exception:          # a misbehaving transport is no answer, never a raise
        return BridgeAnswer(0, None, None)
    if 300 <= status < 400:    # a redirect is never followed: no answer
        return BridgeAnswer(0, None, None)
    return BridgeAnswer(status, data if isinstance(data, dict) else None, _seconds(retry))


def _no_answer(a: BridgeAnswer) -> bool:
    """Any 5xx (trial mail paused is an answer, not this), a network error, or a
    success the tool cannot read."""
    if a.status == 0:
        return True
    if a.status >= 500:
        return a.error != "sending_paused"
    return 200 <= a.status < 300 and a.data is None


_NO_ANSWER_TRIES = 3
_RETRY_MAX_S = 10.0    # the longest pause the tool's own retries of one request wait


def _call_retrying(method: str, path: str, body: Optional[dict] = None, *,
                   auth: Optional[str] = None, query: Optional[dict] = None) -> BridgeAnswer:
    """The tool's own retries: the same request (the same claim nonce) up to three
    times while there is no answer, each after the answer's Retry-After (3 s without
    one). A longer Retry-After (maintenance asks for 5 minutes) is never cut short:
    the answer stands at once, and the caller says to run the command again later."""
    answer = _bridge_call(method, path, body, auth=auth, query=query)
    for _ in range(_NO_ANSWER_TRIES - 1):
        if not _no_answer(answer):
            break
        pause = max(answer.retry_after or 3.0, 1.0)
        if pause > _RETRY_MAX_S:
            break
        _nap(pause)
        answer = _bridge_call(method, path, body, auth=auth, query=query)
    return answer


def _poll_pause(answer: BridgeAnswer, remaining: float) -> bool:
    """The pause before the next poll: the answer's Retry-After in full, never under
    3 s (the service asks for no more than one claim every 3 s). False, without
    waiting, when that pause would run past the ``remaining`` seconds of the wait:
    the caller then stops with its line for the end of that wait."""
    pause = max(answer.retry_after or 3.0, 3.0)
    if pause > remaining:
        return False
    _nap(pause)
    return True


# ════════════════════════ Shapes, checked before any request ═══════════════════
# The bridge's own grammars, so a 400 from it means a fault, never a typo.

_ATEXT = r"[a-z0-9!#$%&'*+/=?^_`{|}~-]"
_LOCAL_RE = re.compile(rf"^{_ATEXT}+(\.{_ATEXT}+)*$")
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_TOP_LABEL_RE = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")
_PLAIN_LOCAL_RE = re.compile(r"^[A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*$")
_PLAIN_DOMAIN_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:[A-Za-z]{2,63}|[Xx][Nn]--[A-Za-z0-9](?:[A-Za-z0-9-]{0,57}[A-Za-z0-9])?)$")
_SETUP_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_TRIAL_CODE_RE = re.compile(r"^[0-9]{6}$")
# Crockford base32: 0-9 and A-Z without I, L, O and U; I and L read as 1, O as 0.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_PHRASE_RE = re.compile(r"^[a-z]+(?:[ -][a-z]+)*$")


def _domain_ok(domain: str) -> bool:
    if len(domain) < 3 or len(domain) > 253:
        return False
    labels = domain.split(".")
    if len(labels) < 2:
        return False
    return all(_LABEL_RE.match(lb) for lb in labels) and bool(_TOP_LABEL_RE.match(labels[-1]))


def _address_parts(raw: str) -> Optional[Tuple[str, str]]:
    text = (raw or "").strip().lower()
    if len(text) < 3 or len(text) > 254:
        return None
    at = text.find("@")
    if at <= 0 or at != text.rfind("@"):
        return None
    local, domain = text[:at], text[at + 1:]
    if len(local) > 64 or not _LOCAL_RE.match(local) or not _domain_ok(domain):
        return None
    return local, domain


def valid_email(email: str) -> bool:
    """Whether ``email`` is an address the service's trial routes accept (its own
    grammar, normalisation included): a plain ASCII dot-atom address of at most 254
    characters whose local part is still an address once a ``+tag`` (and, at gmail.com
    and googlemail.com, its dots) is dropped."""
    parts = _address_parts(email)
    if parts is None:
        return False
    local, domain = parts
    if "+" in local:
        local = local[:local.index("+")]
    if domain in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
    return bool(local) and bool(_LOCAL_RE.match(local))


def valid_recovery_address(email: str) -> bool:
    """The service's wider grammar for a CURRENT recovery address: any address a
    purchase can have pinned."""
    return _address_parts(email) is not None


def valid_new_recovery_address(email: str) -> bool:
    """The service's stricter grammar for a NEW recovery address: letters, digits and
    ``. _ % + -`` in the local part (with a letter or digit), a plain DNS domain, ASCII
    only, 254 characters at most."""
    if not isinstance(email, str) or len(email) > 254 or not email.isascii():
        return False
    at = email.find("@")
    if at < 1 or at != email.rfind("@"):
        return False
    local, domain = email[:at], email[at + 1:]
    return (len(local) <= 64 and bool(_PLAIN_LOCAL_RE.match(local))
            and bool(re.search(r"[A-Za-z0-9]", local)) and bool(_PLAIN_DOMAIN_RE.match(domain)))


def _recovery_code(raw: str) -> Optional[str]:
    """The 16 Crockford symbols of a recovery or address-change code as typed (hyphens
    and spaces optional, any case, ``I``/``L`` read as 1 and ``O`` as 0), or None."""
    text = "".join((raw or "").split()).replace("-", "").upper()
    text = text.translate(str.maketrans({"I": "1", "L": "1", "O": "0"}))
    if len(text) != 16 or any(ch not in _CROCKFORD for ch in text):
        return None
    return text


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _new_nonce() -> str:
    """A claim nonce: 32 random bytes in the setup-code format (43 characters)."""
    return _b64url(secrets.token_bytes(32))


def _new_setup_code() -> Tuple[str, str]:
    """A fresh setup code and its hash: 32 random bytes as unpadded base64url, and the
    lowercase hex SHA-256 of those 32 raw bytes (never of the text)."""
    raw = secrets.token_bytes(32)
    return _b64url(raw), hashlib.sha256(raw).hexdigest()


def _iso_epoch(value) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _utc_iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ═══════════════════════ The trial's machine fingerprint ═══════════════════════
# The service keeps one trial per email AND one per computer. This is how the free
# tool names the computer: the SHA-256 (64 lowercase hex, the service's format) of a
# label and the operating system's own machine identifier — the platform UUID on
# macOS, /etc/machine-id (or D-Bus's copy) on Linux, MachineGuid on Windows. Where
# none can be read, a random identifier made once and kept in this module's 0600
# state stands in, so the value is stable for as long as that file is. It is this
# package's own derivation for the trial's uniqueness check; nothing compares it
# with the paid package's activation fingerprint.

_FINGERPRINT_LABEL = "buddhi-review trial fingerprint v1"


def _os_machine_id() -> str:
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                 capture_output=True, text=True, timeout=5)
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out.stdout or "")
            return m.group(1).strip() if m else ""
        if sys.platform.startswith("linux"):
            for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                try:
                    value = Path(p).read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                if value:
                    return value
            return ""
        if sys.platform == "win32":
            import winreg  # type: ignore[import-not-found]
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography") as k:
                return str(winreg.QueryValueEx(k, "MachineGuid")[0]).strip()
    except Exception:
        return ""
    return ""


_machine_id: Callable[[], str] = _os_machine_id


def trial_fingerprint(*, state_path: Optional[Path] = None) -> str:
    base = ""
    try:
        base = (_machine_id() or "").strip()
    except Exception:
        base = ""
    if not base:
        state = _read_state(state_path)
        base = state.get("machine_id") if isinstance(state.get("machine_id"), str) else ""
        if not base:
            base = secrets.token_hex(32)
            state["machine_id"] = base
            _write_state(state_path, state)
    return hashlib.sha256(f"{_FINGERPRINT_LABEL}|{base}".encode("utf-8")).hexdigest()


# ═════════════════════════════ The terminal seam ═══════════════════════════════

class Ui:
    """How the acquisition paths talk to the person: ``row(kind, text)`` prints one
    line (``kind`` is ``ok`` · ``info`` · ``warn`` · ``bad`` · ``step``); ``ask`` reads
    a visible answer and ``ask_hidden`` one that is not echoed (both may raise
    ``EOFError`` or ``KeyboardInterrupt``); ``yes_no(question, default)`` asks with
    the wizard's own yes/no control; ``interactive`` is whether stdin and stdout are a
    terminal; ``open_browser(url)`` opens a page and says whether it could; and
    ``read_clipboard()`` returns the clipboard's text, or ''."""

    def __init__(self, *, row: Callable[[str, str], None], ask: Callable[[str], str],
                 ask_hidden: Callable[[str], str],
                 yes_no: Callable[[str, bool], bool], interactive: bool,
                 open_browser: Optional[Callable[[str], bool]] = None,
                 read_clipboard: Optional[Callable[[], str]] = None):
        self.row, self.ask, self.ask_hidden, self.yes_no = row, ask, ask_hidden, yes_no
        self.interactive = interactive
        self.open_browser = open_browser or _open_in_browser
        self.read_clipboard = read_clipboard or _read_clipboard

    @classmethod
    def terminal(cls) -> "Ui":
        def _row(kind: str, text: str) -> None:
            glyph = {"ok": "✓", "warn": "⚠", "bad": "✗", "step": "▸", "info": "·"}.get(kind, " ")
            print(f"  {glyph} {text}")

        def _yes_no(question: str, default: bool) -> bool:
            suffix = "[Y/n]" if default else "[y/N]"
            try:
                raw = input(f"  {question} {suffix}: ").strip().lower()
            except EOFError:
                return default
            return default if not raw else raw in ("y", "yes")

        interactive = False
        try:
            interactive = sys.stdin.isatty() and sys.stdout.isatty()
        except (AttributeError, ValueError, OSError):
            pass
        return cls(row=_row, ask=lambda p: input(f"  {p}"),
                   ask_hidden=lambda p: _hidden_paste_input(f"  {p}"),
                   yes_no=_yes_no, interactive=interactive)


def _open_in_browser(url: str) -> bool:
    try:
        import webbrowser
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def _read_clipboard(reader: Optional[Callable] = None) -> str:
    """Best-effort clipboard read. Catches EVERYTHING (ImportError / RuntimeError /
    OSError / bare Exception) and returns "" on any failure — a headless / SSH host
    with no clipboard tool must fall straight through to a manual paste, never hang
    or raise. The ``reader`` seam keeps tests off the real clipboard."""
    if reader is not None:
        try:
            return (reader() or "").strip()
        except Exception:
            return ""
    for cmd in (["pbpaste"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "-b"]):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except Exception:
            continue
    return ""


def _mask_key(candidate: str) -> str:
    """A recognisable but non-disclosing preview for the consent prompt: the first and
    last four characters, never enough to spill a mis-copied secret into the
    scrollback."""
    if len(candidate) <= 8:
        return "*" * len(candidate)
    return f"{candidate[:4]}{'*' * 6}{candidate[-4:]}"


def _hidden_paste_input(prompt: str) -> str:
    """Read a pasted code WITHOUT echoing it (the default ``Ui``'s hidden reader; the
    wizard injects its own wrapped-paste-safe one)."""
    import getpass
    import warnings
    try:
        with warnings.catch_warnings():
            # Python's getpass falls back to an ECHOING read after this warning; a code
            # must never reach the scrollback, so that fallback reads nothing instead.
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except Exception:
        return ""


# ═══════════════════════════ The 0600 state file ══════════════════════════════
# One file holds what the acquisition paths must remember between runs:
#   * the trial offer's gating (BUDDHI_NO_UPSELL suppresses; an active Pro backend
#     suppresses; a durable No sticks; the shared upsell cap bounds re-offers) and
#     when a trial became active on this computer (``trial_active_since``);
#   * the claim nonces and the codes the tool holds: a ``--buy`` setup code
#     (``buy``), a setup code pasted into ``--activate`` (``activate``), a recovery
#     code (``recover``) and the trial's nonce (``trial_nonce``);
#   * an install whose key is already in ``~/.netrc`` but did not finish
#     (``install``), which the next plain ``setup`` finishes first;
#   * the licence agreement's assent (``eula``) and, where the operating system names
#     no machine, the trial fingerprint's stand-in (``machine_id``).

_TRIAL_DAYS = 14
_BUY_PAIR_HOLD_S = 144 * 3600       # a --buy code: 144 h after its /pair ...
_BUY_ACCEPTED_HOLD_S = 72 * 3600    # ... or 72 h after it was first accepted, whichever is later
_ACTIVATE_HOLD_S = 72 * 3600
_RECOVER_UNANSWERED_HOLD_S = 15 * 60
# An earlier release kept a registration's throwaway password here; it is never
# written again, and dropped whenever the file is.
_LEGACY_KEYS = ("pending_email", "pending_password", "pending_user_id")


def _state_path() -> Path:
    override = os.environ.get("BUDDHI_TRIAL_STATE")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".cache" / "buddhi" / "pro_trial.json"


def _read_state(path: Optional[Path]) -> dict:
    p = path or _state_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Optional[Path], state: dict) -> Optional[str]:
    """Atomically replace the state file, 0600 from its first byte. Returns None when
    it was saved, else the operating system's reason it could not be.

    The temp file gets an unpredictable name via ``tempfile.mkstemp`` (opened O_EXCL,
    so a pre-created file or symlink at a guessed name is refused rather than
    truncated through — ``BUDDHI_TRIAL_STATE`` can point at a shared directory), is
    0600 before a byte is written (the file holds claim nonces and held codes), and is
    unlinked in the ``finally`` so a failing ``os.replace`` never strands a copy."""
    p = path or _state_path()
    for key in _LEGACY_KEYS:
        state.pop(key, None)
    tmp: Optional[Path] = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f"{p.name}.", suffix=".tmp", dir=str(p.parent))
        tmp = Path(tmp_name)
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(state))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        tmp = None                  # replaced: the path is the state file now, not a leftover
        os.chmod(p, 0o600)
        return None
    except OSError as exc:
        return exc.strerror or str(exc)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _write_state(path: Optional[Path], state: dict) -> None:
    """:func:`_save_state` where a failure must not stop anything (the offer's
    counters): a read-only or full disk never crashes setup."""
    _save_state(path, state)


def _held(state: dict, kind: str) -> Optional[dict]:
    value = state.get(kind)
    return value if isinstance(value, dict) else None


def _num(value) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _hold_expired(kind: str, hold: dict, now: float) -> bool:
    """Whether a held code (``buy``, ``activate`` or ``recover``) is past its hold,
    counted by the tool (the service's own ``410`` is final whatever this count says),
    or is not a code at all."""
    if not isinstance(hold.get("code"), str):
        return True
    if kind == "buy":
        paired = _num(hold.get("paired_at")) or 0.0
        accepted = _num(hold.get("first_accepted_at"))
        until = max(paired + _BUY_PAIR_HOLD_S,
                    (accepted + _BUY_ACCEPTED_HOLD_S) if accepted is not None else 0.0)
    elif kind == "activate":
        until = (_num(hold.get("first_request_at")) or 0.0) + _ACTIVATE_HOLD_S
    else:
        until = _num(hold.get("replay_until"))
        if until is None:
            until = (_num(hold.get("first_request_at")) or 0.0) + _RECOVER_UNANSWERED_HOLD_S
    return now >= until


def _expire_holds(state: dict, now: float) -> bool:
    """Drop every held code past its hold. Returns True when one was dropped."""
    dropped = False
    for kind in ("buy", "activate", "recover"):
        hold = _held(state, kind)
        if hold is not None and _hold_expired(kind, hold, now):
            state.pop(kind, None)
            dropped = True
    return dropped


def _recovery_unfinished(state: dict, now: float) -> bool:
    """A recovery code is held and still within its hold (the service accepted it, or
    has not answered it yet): a plain ``setup`` finishes or checks it."""
    rec = _held(state, "recover")
    return rec is not None and not _hold_expired("recover", rec, now)


def _update_state(state_path: Optional[Path], **changes) -> Optional[str]:
    """Re-read the state, apply ``changes`` (a None value removes the key) and save."""
    state = _read_state(state_path)
    for key, value in changes.items():
        if value is None:
            state.pop(key, None)
        else:
            state[key] = value
    return _save_state(state_path, state)


def _edit_hold(state_path: Optional[Path], kind: str, **fields) -> None:
    """Change fields of one held code, if it is still held (fail-soft)."""
    state = _read_state(state_path)
    hold = _held(state, kind)
    if hold is None:
        return
    hold.update(fields)
    _write_state(state_path, state)


def _drop_hold(state_path: Optional[Path], kind: str) -> None:
    state = _read_state(state_path)
    if kind in state:
        state.pop(kind, None)
        _write_state(state_path, state)


# ═════════════════════ The licence agreement, before any key ═══════════════════

_EULA_ASK = ("To use Buddhi Review Pro, you must accept the Buddhi Review Pro End User "
             f"Licence Agreement, Version {EULA_VERSION}, effective {EULA_EFFECTIVE}: {EULA_URL}")
_EULA_CHANGED = ("The Buddhi Review Pro End User Licence Agreement has changed since you last "
                 "accepted it.")
_EULA_NO_TERMINAL = ("Buddhi Review Pro was not installed: its licence agreement must be "
                     "accepted in a terminal. Run the same command in a terminal to read and "
                     "accept it.")
_EULA_REFUSED = ("You did not accept the agreement, so Buddhi Review Pro was not installed. The "
                 "free version keeps working. To install Pro, run the same command again and "
                 "accept the agreement.")


def _eula_current(state: dict) -> bool:
    record = state.get("eula")
    return isinstance(record, dict) and record.get("sha256") == EULA_SHA256


def _eula_gate(ui: Ui, state_path: Optional[Path]) -> Tuple[str, str]:
    """Ask for assent to the licence agreement unless this text was accepted already.
    Returns ``("ok", "")``, ``("refused", "")``, ``("no_terminal", "")`` or
    ``("unsaved", why)``. Assent is recorded once — the version, the signed text's
    SHA-256 and the UTC time — and asked again only when that SHA-256 changes. A run
    that is not in a terminal never assumes it, and the default answer is No."""
    state = _read_state(state_path)
    if _eula_current(state):
        return "ok", ""
    if not ui.interactive:
        return "no_terminal", ""
    if isinstance(state.get("eula"), dict):
        ui.row("info", _EULA_CHANGED)
    ui.row("step", _EULA_ASK)
    if not ui.yes_no("Do you accept it?", False):
        return "refused", ""
    state = _read_state(state_path)
    state["eula"] = {"version": EULA_VERSION, "sha256": EULA_SHA256,
                     "accepted_at": _utc_iso(_now())}
    why = _save_state(state_path, state)
    return ("unsaved", why) if why else ("ok", "")


def _paid_eula(ui: Ui, state_path: Optional[Path]) -> bool:
    """The gate on ``--buy``, ``--activate`` and ``--recover``: True to go on; else its
    line is shown."""
    verdict, why = _eula_gate(ui, state_path)
    if verdict == "ok":
        return True
    ui.row("warn", {"refused": _EULA_REFUSED, "no_terminal": _EULA_NO_TERMINAL}.get(
        verdict) or _unsaved(state_path or _state_path(), why))
    return False


# ═══════════════════════ seat-admin.json: who may manage seats ═════════════════
# The manage secret of each subscription this computer manages, written ONLY here
# (the paid ``seats`` command only reads it): ``~/.config/buddhi/seat-admin.json``,
# mode 0600, schema ``{"schema": 1, "entries": {"<bridge origin> <subscription id>":
# {"secret": "…", "version": n}}}``, replaced atomically and merge-preserving (a
# temp file, fsync, rename; every other entry kept; an entry is never replaced by
# an older version of its secret). A file that cannot be read is left as it is.

SEAT_ADMIN_SCHEMA = 1


def _seat_admin_path() -> Path:
    override = os.environ.get("BUDDHI_SEAT_ADMIN")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".config" / "buddhi" / "seat-admin.json"


def save_seat_admin(entries: List[Tuple[str, str, int]]) -> Optional[Tuple[str, str]]:
    """Merge ``(subscription_id, secret, version)`` entries into the seat-access file.
    Returns None when saved, ``("unreadable", why)`` when the existing file could not
    be read (it is left as it is), or ``("unwritable", why)``."""
    path = _seat_admin_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):   # absent: nothing to keep
        current: Dict[str, Any] = {"schema": SEAT_ADMIN_SCHEMA, "entries": {}}
    except OSError as exc:
        return "unreadable", exc.strerror or str(exc)
    except UnicodeDecodeError:     # not text; its bytes (maybe secrets) are never shown
        return "unreadable", "it is not valid JSON"
    else:
        try:
            current = json.loads(raw)
        except ValueError:
            return "unreadable", "it is not valid JSON"
        schema = current.get("schema") if isinstance(current, dict) else None
        if not isinstance(schema, int) or isinstance(schema, bool) \
                or not isinstance(current.get("entries"), dict):
            return "unreadable", "it is not a seat access file"
        if schema > SEAT_ADMIN_SCHEMA:
            return "unreadable", "it was written by a newer version of Buddhi Review"
    merged = dict(current["entries"])
    for sub, secret, version in entries:
        key = f"{BRIDGE_ORIGIN} {sub}"
        old = merged.get(key)
        old_version = old.get("version") if isinstance(old, dict) else None
        if isinstance(old_version, int) and old_version > version:
            continue
        merged[key] = {"secret": secret, "version": version}
    body = json.dumps({"schema": SEAT_ADMIN_SCHEMA, "entries": merged}, indent=2) + "\n"
    tmp: Optional[Path] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
        tmp = Path(tmp_name)
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
        os.chmod(path, 0o600)
        return None
    except OSError as exc:
        return "unwritable", exc.strerror or str(exc)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _seat_admin_secret(sub_id: str) -> Optional[str]:
    """The manage secret this computer holds for ``sub_id``, or None when the
    seat-access file, or its entry for this subscription, cannot be read."""
    try:
        current = json.loads(_seat_admin_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    entries = current.get("entries") if isinstance(current, dict) else None
    entry = entries.get(f"{BRIDGE_ORIGIN} {sub_id}") if isinstance(entries, dict) else None
    secret = entry.get("secret") if isinstance(entry, dict) else None
    return secret if isinstance(secret, str) and secret else None


def _seat_admin_line(failure: Tuple[str, str], *, installed: bool) -> str:
    kind, why = failure
    path = _seat_admin_path()
    if kind == "unreadable":
        return (f"The seat access file {path} could not be read ({why}), so it was left as it "
                "is and your seat access was not saved. Move it aside, then run "
                "`buddhi-review setup --recover`.")
    if installed:
        return ("Buddhi Review Pro is set up, but this computer could not save the access it "
                f"needs to manage your seats ({path}: {why}). Pro keeps working here. After you "
                "fix that, run `buddhi-review setup --recover` to get that access again.")
    return ("This computer could not save the access it needs to manage your seats "
            f"({path}: {why}), so no computer can manage them now. After you fix that, run "
            "`buddhi-review setup --recover` to get that access again.")


# ══════════════════════ The index credential, pip and the daemon ═════════════════

def write_index_credential(key: str, *, path: Optional[Path] = None) -> Tuple[bool, str]:
    """Write the key as the private-index credential in ``~/.netrc`` (host login is
    the literal ``license``; the password is the key). Delegates the merge-preserving
    0600 write to the generic :mod:`netrc_writer`. Returns ``(ok, action)``."""
    return netrc_writer.upsert(_INDEX_HOST, _INDEX_LOGIN, key, path=path)


def _index_url() -> str:
    """The PLAIN license-gated index URL — NO embedded credential. pip authenticates
    from ``~/.netrc`` (written first), so the key never appears in the process argv /
    ``ps`` output, only in the 0600 file."""
    return f"https://{_INDEX_HOST}/{_ACCOUNT}/simple"


def pip_install(*, runner: Optional[Callable] = None,
                netrc_path: Optional[Path] = None) -> Tuple[bool, int, str]:
    """``pip install buddhi-review-pro`` from the license-gated index. Returns
    ``(ok, returncode, output)``. The index credential is NOT on the command line —
    pip reads it from the netrc file written just before this call — so the key is
    never exposed in argv. The ``runner`` seam keeps this network-free under test.

    ``netrc_path`` is exported as ``NETRC`` in the subprocess environment: pip's own
    netrc lookup (vendored requests' ``get_netrc_auth``) honors the ``NETRC`` env var
    and only falls back to ``~/.netrc`` when it is unset, so a custom write location
    (``BUDDHI_NETRC`` or an explicit override) would otherwise be invisible to pip.

    ``--no-input`` (pip ≥ 21.1) is what keeps a credential failure FAST: setup runs on an
    inherited TTY, so an ignored / malformed netrc or a 401 from the index would
    otherwise leave pip blocking on its interactive user/password prompt — the wizard
    would simply look hung until the 600 s timeout instead of falling into the
    ``pip_failed`` / ``index_403`` paths below."""
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", _PACKAGE,
           "--index-url", _index_url(), "--no-input"]
    env = {**os.environ, "NETRC": str(netrc_path)} if netrc_path is not None else None
    run = runner or (lambda c, env=None: subprocess.run(c, capture_output=True, text=True,
                                                         timeout=600, env=env))
    try:
        proc = run(cmd, env=env) if _runner_accepts_env(run) else run(cmd)
    except Exception as exc:
        return False, 1, f"{type(exc).__name__}: {exc}"
    rc = getattr(proc, "returncode", 1)
    output = f"{getattr(proc, 'stdout', '') or ''}\n{getattr(proc, 'stderr', '') or ''}"
    return rc == 0, rc, output


def _runner_accepts_env(run: Callable) -> bool:
    """Whether ``run`` will take an ``env=`` keyword — the default subprocess runner
    and any production wrapper that wants the computed ``NETRC`` env do; the legacy
    single-arg fakes in the test suite (``lambda cmd: ...``) do not, and must keep
    receiving just ``cmd`` unchanged."""
    try:
        params = inspect.signature(run).parameters
    except (TypeError, ValueError):
        return False
    return any(p.name == "env" or p.kind == inspect.Parameter.VAR_KEYWORD
              for p in params.values())


def _looks_like_403(returncode: int, output: str) -> bool:
    low = (output or "").lower()
    return returncode != 0 and ("403" in low or "forbidden" in low)


# ════════════════════════ Backend discovery (daemon + gate) ════════════════════
# Everything here goes THROUGH FREE-1's discovery — the free tree never imports the
# Pro package (§E.2); it only holds the discovered backend object and duck-types.

def pro_backend_active(*, backends=None) -> bool:
    """True iff an installed non-free backend reports itself active — the same
    check the front door uses. Reuses the upsell eligibility gate."""
    from buddhi_review import upsell
    return upsell.paid_backend_active(backends)


def _select_installed_backend(candidates, *, free_name: str):
    """The freshly-installed non-free backend to kick, regardless of whether it
    already reports itself active. Unlike :func:`backends.select_backend` (which
    filters to already-active candidates — the right call for routing a review
    loop), a backend fresh off ``pip install`` legitimately reports inactive until
    its OWN ``start_daemon`` performs first activation; requiring active-already
    here would mean it is never selected, so its daemon never starts and it can
    never become active in the first place."""
    from buddhi_review import backends as _b
    non_free = [b for b in candidates if getattr(b, "name", None) != free_name]
    if not non_free:
        return None
    non_free.sort(key=_b._safe_priority, reverse=True)
    return non_free[0]


def start_daemon(*, backends=None) -> bool:
    """Start the Pro live-view daemon THROUGH the discovered backend (never importing
    the Pro package). Selects the freshly-installed non-free backend — NOT through
    :func:`backends.select_backend`, whose active-only filter would exclude a
    backend that has not yet run its own first-activation daemon start — and
    duck-types its ``start_daemon``; a backend without one, or any error, is a
    best-effort no-op. Returns True iff a daemon was reported ready."""
    from buddhi_review import backends as _b
    candidates = backends if backends is not None else _b.discover_backends()
    backend = _select_installed_backend(candidates, free_name=_b.FreeBackend.name)
    if backend is None:
        return False
    starter = getattr(backend, "start_daemon", None)
    if not callable(starter):
        return False
    try:
        return bool(starter())
    except Exception:
        return False


def _await_active(*, backends=None, is_active=None, attempts: int = 20,
                  sleep: Optional[Callable] = None) -> bool:
    """Poll briefly for the freshly-installed backend to report active (its own
    first run activates this machine). Best-effort; every seam is injectable."""
    import time
    check = is_active or (lambda: pro_backend_active(backends=backends))
    napper = sleep or time.sleep
    for i in range(max(1, attempts)):
        try:
            if check():
                return True
        except Exception:
            pass
        if i < attempts - 1:
            try:
                napper(0.25)
            except Exception:
                break
    return False


# ═════════════════════ Is the paid release published yet? ══════════════════════
# The trial ends in a pip install from the private index, so it is offered only
# once there is a published wheel this machine can install. A public route answers
# that, with no credential and no request data:
#
#   200 {"status": "open", "wheels": ["<interpreter>-<abi>-<platform>", ...]}
#   200 {"status": "closed"}
#
# It counts as OPEN only for that first shape — a 200 ``application/json`` object
# whose ``wheels`` is a non-empty list of strings — AND when at least one listed tag
# suits the interpreter the install runs, which pip can install into. Everything
# else is closed: a timeout, a DNS or TLS failure, a redirect, any other status (the
# edge's 429, a 5xx), a body that is not JSON or is too large, an unknown status, a
# missing or empty wheel list, a tag that does not parse, an interpreter with no pip
# or one PEP 668 marks externally managed. One request per setup run (a :class:`ReleaseCheck`
# carries it from the wizard's early prefetch to the offer and into
# :func:`start_trial`); no retry, no cache, and nothing about the answer is written.

RELEASE_STATUS_URL = "https://api.buddhireview.com/release-status"
# A cold answer takes about 2 s and the route can hold a queued request for about
# 6 s more, so an 8 s wall-clock cap waits for every real answer.
_RELEASE_TIMEOUT_S = 8.0
_RELEASE_MAX_BYTES = 64 * 1024
# The one seam every check reads the answer through: a callable returning the
# route's JSON object (or None), or None for the HTTPS client below. The test suite
# points it at a stub so no test ever calls the route.
_release_fetch: Optional[Callable[[], Optional[dict]]] = None
# The outcome tag :func:`start_trial` returns while the release is closed. It carries
# no message: the wizard stays silent, as it does off a TTY or past the offer cap.
RELEASE_CLOSED = "release_closed"


def _fetch_release_answer(url: Optional[str] = None,
                          timeout: float = _RELEASE_TIMEOUT_S) -> Optional[dict]:
    """One GET to the release route → the JSON object it answered, or None for
    anything but a 200 ``application/json`` object within the size cap (a redirect,
    any other status, another content type, an oversized or non-JSON body, a network
    or TLS error). Never raises.

    ``timeout`` is urllib's per-socket-operation timeout; it does not bound DNS
    resolution or the whole read, so the hard wall-clock cap is
    :class:`ReleaseCheck`'s. ``url`` is a test seam: the route is always
    :data:`RELEASE_STATUS_URL`."""
    opener = urllib.request.build_opener(
        _RefuseRedirects, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    req = urllib.request.Request(url or RELEASE_STATUS_URL, method="GET",
                                 headers={"Accept": "application/json",
                                          "User-Agent": "buddhi-review-setup"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            if resp.status != 200 or resp.headers.get_content_type() != "application/json":
                return None
            raw = resp.read(_RELEASE_MAX_BYTES + 1)
    except Exception:
        return None
    if len(raw) > _RELEASE_MAX_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


# Run BY the interpreter :func:`pip_install` runs (``sys.executable``): who it is, and
# whether ``-m pip install`` can install into it at all — pip is importable there, and
# it is not a PEP 668 externally-managed interpreter outside a virtual environment
# (pip refuses those without an override this module never passes).
_INSTALL_PROBE = (
    "import importlib.util, os, platform, sys, sysconfig\n"
    "managed = sys.prefix == sys.base_prefix and os.path.isfile(\n"
    "    os.path.join(sysconfig.get_path('stdlib'), 'EXTERNALLY-MANAGED'))\n"
    "print(sys.hexversion, sys.implementation.cache_tag, getattr(sys, 'abiflags', '') or '-',\n"
    "      platform.machine() or '-', importlib.util.find_spec('pip') is not None and not managed)\n"
)
_INSTALL_PROBE_TIMEOUT_S = 5.0
# How long :class:`ReleaseCheck` waits for the local half (the probe above plus the
# in-process tag computation), counted from :meth:`ReleaseCheck.start`. It runs
# alongside the request, never inside the route's cap, and fits within that cap.
_LOCAL_CHECK_TIMEOUT_S = _INSTALL_PROBE_TIMEOUT_S + 2.0


def _install_identity() -> List[str]:
    """What :data:`_INSTALL_PROBE` prints when it runs in this very interpreter and
    pip can install into it."""
    return [str(sys.hexversion), str(sys.implementation.cache_tag),
            getattr(sys, "abiflags", "") or "-", platform.machine() or "-", "True"]


def _install_target_ok(*, timeout: float = _INSTALL_PROBE_TIMEOUT_S) -> bool:
    """Whether ``sys.executable`` — the interpreter :func:`pip_install` runs — is this
    running interpreter, so its wheel tags can be computed here, in process, AND can
    ``pip install`` into its environment. Asked of that interpreter itself, once, so a
    virtual environment's own path still counts as this interpreter. False on any
    doubt: no interpreter path, a different interpreter (an override such as
    ``PYTHONEXECUTABLE`` can point ``sys.executable`` elsewhere), no pip (a ``uv``
    tool environment ships none), a PEP 668 externally-managed interpreter, or a
    probe that fails or hangs."""
    if not sys.executable:
        return False
    try:
        proc = subprocess.run([sys.executable, "-c", _INSTALL_PROBE], capture_output=True,
                              text=True, timeout=timeout)
    except Exception:
        return False
    return proc.returncode == 0 and (proc.stdout or "").split() == _install_identity()


def _interpreter_tags() -> frozenset:
    """The wheel tags the install's interpreter accepts: computed in process, since
    :func:`_install_target_ok` has confirmed ``sys.executable`` is this interpreter —
    or none at all when it is not, or when pip could not install into it."""
    if not _install_target_ok():
        return frozenset()
    from packaging import tags
    return frozenset(tags.sys_tags())


def _release_open(answer, supported: Optional[frozenset] = None) -> bool:
    """Whether a route answer offers a published wheel this interpreter can install:
    ``status`` is exactly ``open``, ``wheels`` is a non-empty list of strings, every
    entry parses as a wheel tag (compressed sets such as ``py2.py3-none-any``
    expand), and at least one tag is one :func:`_interpreter_tags` accepts. A tag
    that does not parse makes the whole answer closed. ``supported`` is those tags
    when the caller has already computed them (:class:`ReleaseCheck` does, alongside
    the request)."""
    if not isinstance(answer, dict) or answer.get("status") != "open":
        return False
    wheels = answer.get("wheels")
    if not (isinstance(wheels, list) and wheels and all(isinstance(w, str) for w in wheels)):
        return False
    try:
        from packaging.tags import parse_tag
        offered: set = set()
        for wheel in wheels:
            offered |= parse_tag(wheel)
        if supported is None:
            supported = _interpreter_tags()
    except Exception:
        return False
    return not offered.isdisjoint(supported)


class ReleaseCheck:
    """One setup run's answer to "is the paid release published, with a wheel for
    this interpreter?" — at most ONE request, made on a background thread so the
    wizard can ask early and use the answer at the end.

    :meth:`start` sends the request (once). The answer counts only if it is complete
    within the wall-clock cap counted from :meth:`start` (the background-thread
    pattern of ``update_banner._bounded_fetch``: urllib's own timeout does not bound
    DNS resolution or a trickling body). :meth:`is_open` waits for it, never past the
    cap, then freezes the answer: one that lands after the cap is closed for the rest
    of the run, whenever it is first read — and one that landed in time is used even
    when it is read long after (the wizard asks at its start and reads at its end).
    The local half — whether this interpreter can install a listed wheel — runs on
    its own thread from :meth:`start`, alongside the request and outside its cap, so
    a slow interpreter probe can never turn an in-time ``open`` into closed. It has
    its own bound (:data:`_LOCAL_CHECK_TIMEOUT_S`), and one that does not finish is
    closed. ``fetch`` and ``timeout`` are test seams."""

    def __init__(self, *, fetch: Optional[Callable[[], Optional[dict]]] = None,
                 timeout: float = _RELEASE_TIMEOUT_S):
        self._fetch = fetch
        self._timeout = timeout
        self._thread: Optional[threading.Thread] = None
        self._probe: Optional[threading.Thread] = None
        self._deadline = 0.0
        self._local_deadline = 0.0
        self._result: list = [None]
        self._tags: List[Optional[frozenset]] = [None]
        self._finished: List[Optional[float]] = [None]
        self._answer: Optional[bool] = None

    def start(self) -> "ReleaseCheck":
        if self._thread is not None or self._answer is not None:
            return self
        fetch = self._fetch or _release_fetch or _fetch_release_answer
        result, finished, tags = self._result, self._finished, self._tags

        def _probe() -> None:
            try:
                tags[0] = _interpreter_tags()
            except Exception:
                tags[0] = frozenset()

        def _worker() -> None:
            try:
                answer = fetch()
            except Exception:
                answer = None
            result[0] = answer
            finished[0] = time.monotonic()

        probe = threading.Thread(target=_probe, name="buddhi-release-probe", daemon=True)
        thread = threading.Thread(target=_worker, name="buddhi-release-check", daemon=True)
        now = time.monotonic()
        self._deadline = now + self._timeout
        self._local_deadline = now + _LOCAL_CHECK_TIMEOUT_S
        try:
            probe.start()
            thread.start()
        except Exception:  # no thread to ask on: closed
            self._answer = False
            return self
        self._probe = probe
        self._thread = thread
        return self

    def is_open(self) -> bool:
        if self._answer is None:
            self.start()
        if self._answer is None:
            self._thread.join(max(0.0, self._deadline - time.monotonic()))
            done = self._finished[0]
            in_time = (not self._thread.is_alive() and done is not None
                       and done <= self._deadline)
            supported = None
            if in_time:
                self._probe.join(max(0.0, self._local_deadline - time.monotonic()))
                supported = None if self._probe.is_alive() else self._tags[0]
            try:
                self._answer = (supported is not None
                                and _release_open(self._result[0], supported))
            except Exception:
                self._answer = False
        return self._answer


# ═══════════════════════════════ Result type ═══════════════════════════════════

class TrialOutcome:
    """The structured result of one acquisition step. ``ok`` is the headline;
    ``status`` is a stable machine tag (for tests); ``message`` is the last line the
    step showed (or '' for a silent one)."""

    __slots__ = ("ok", "status", "message")

    def __init__(self, ok: bool, status: str, message: str):
        self.ok, self.status, self.message = ok, status, message

    def __repr__(self):  # pragma: no cover - debug aid
        return f"TrialOutcome(ok={self.ok!r}, status={self.status!r})"


# ════════════════════════════ The install tail ═════════════════════════════════
# Shared by every path once it holds a key: write the index credential → pip install
# → start the daemon → confirm activation → one line. Every failure after the
# credential is written leaves ``~/.netrc`` intact and records the install as
# unfinished, so the next plain ``setup`` finishes it (which keeps the kept lines'
# "re-run setup" true). ``kind`` is ``trial``, ``buy``, ``activate`` or ``recover``;
# ``buyer`` is whether a manage secret came back (a teammate's code has none);
# ``last_offer`` selects the trial's last-offer lines.

def _pip_index_command(netrc: Path) -> str:
    return (f"NETRC={shlex.quote(str(netrc))} {shlex.quote(sys.executable)} -m pip install "
            f"--upgrade {_PACKAGE} --index-url {shlex.quote(_index_url())} --no-input")


def _netrc_failed_line(netrc: Path, kind: str, last_offer: bool) -> str:
    if kind == "trial":
        if last_offer:
            return (f"Could not write `{netrc}`. Check the file's permissions. For help "
                    f"continuing trial setup, email {SUPPORT_EMAIL}.")
        return (f"Could not write `{netrc}`. Check the file's permissions, then run "
                "`buddhi-review setup` again with the same email to continue the trial if "
                "it is still live.")
    if kind == "activate":
        return (f"Could not write {netrc}. Check the file's permissions, then run "
                "`buddhi-review setup --activate` within the next few minutes and paste the "
                "same code. If that is too late and you bought Pro, run `buddhi-review setup "
                f"--recover` {_RECOVERY_PHRASE}; if someone gave you this code, ask them for a "
                "new one.")
    return (f"Could not write {netrc}. Check the file's permissions, then run `buddhi-review "
            "setup` again within the next few minutes to finish. If that is too late, run "
            f"`buddhi-review setup --recover` {_RECOVERY_PHRASE}.")


def _success_line(kind: str, buyer: bool) -> str:
    if kind == "trial":
        return ("✓ Buddhi Review Pro is active — your free trial is live. The live view is "
                "starting in the background.")
    if buyer:
        return ("✓ Buddhi Pro is active — your subscription is active. The live view is "
                "starting in the background.")
    return ("✓ Buddhi Review Pro is active on this computer. The live view is starting in "
            "the background.")


def _install_and_activate(netrc: Path, *, backends=None, runner=None, is_active=None,
                          sleep=None, attempts=20) -> Tuple[bool, int, str, bool]:
    """The ONE place the activation order lives: today the paid package is pip-installed
    with the key already in ``netrc``, and its own first daemon start activates this
    computer. Should the licence index ever refuse a licence that has no activated
    machine yet, this function alone changes to activate first and install after.
    Returns ``(installed, returncode, pip_output, active)``."""
    installed, rc, output = pip_install(runner=runner, netrc_path=netrc)
    if not installed:
        return False, rc, output, False
    start_daemon(backends=backends)
    active = _await_active(backends=backends, is_active=is_active, sleep=sleep,
                           attempts=attempts)
    return True, rc, output, active


def _install_tail(netrc: Path, *, kind: str, buyer: bool, last_offer: bool, backends=None,
                  runner=None, is_active=None, sleep=None, attempts=20,
                  state_path: Optional[Path] = None) -> TrialOutcome:
    """pip install → daemon → activation (:func:`_install_and_activate`), the key
    already in ``netrc``."""
    installed, rc, output, active = _install_and_activate(
        netrc, backends=backends, runner=runner, is_active=is_active, sleep=sleep,
        attempts=attempts)
    if not installed:
        if _looks_like_403(rc, output):
            if kind == "trial" and last_offer:
                return TrialOutcome(False, "index_403",
                                    "The licence index refused the install (403). Your key may "
                                    "not be active yet. Wait a moment, then run "
                                    f"{_pip_index_command(netrc)}. If it still fails, email "
                                    f"{SUPPORT_EMAIL}.")
            return TrialOutcome(False, "index_403",
                                "The license index refused the install (403). Your key may not "
                                "be active yet — wait a moment and re-run setup.")
        return TrialOutcome(False, "pip_failed",
                            f"Install did not complete — your license is set up and {netrc} "
                            f"is intact, so just re-run: {_pip_index_command(netrc)}")
    if active:
        state = _read_state(state_path)
        state.pop("install", None)
        state["last_install_kind"] = kind
        if kind == "trial":
            # The first start counts (build rule 12): a reinstall that continues the trial
            # within its 14 days never moves it later.
            if _num(state.get("trial_active_since")) is None:
                state["trial_active_since"] = _now()
            state.pop("trial_nonce", None)
        _write_state(state_path, state)
        return TrialOutcome(True, "active", _success_line(kind, buyer))
    # Installed, but this computer did not activate: one calm line + a pointer. The
    # unfinished-install record is dropped, so a later plain ``setup`` does not re-run
    # pip and repeat this line on every run (the cause is often permanent: a trial
    # already used here, or a key active elsewhere).
    state = _read_state(state_path)
    if state.pop("install", None) is not None:
        _write_state(state_path, state)
    if kind == "trial":
        return TrialOutcome(False, "not_activated",
                            "Pro installed, but it did not activate on this machine — it may "
                            f"have already been used for a trial. {UPGRADE_POINTER}")
    return TrialOutcome(False, "not_activated",
                        "Pro installed, but it did not activate on this machine — your key may "
                        f"already be active elsewhere. Contact {SUPPORT_EMAIL}.")


def _finish_install(key: str, *, kind: str = "trial", buyer: bool = True,
                    last_offer: bool = False, drop_hold: Optional[str] = None,
                    install: bool = True, paused_subs: Sequence[str] = (), backends=None,
                    runner=None, netrc_path=None, is_active=None, sleep=None, attempts=20,
                    state_path: Optional[Path] = None) -> TrialOutcome:
    """Write ``key`` as the index credential, record the install as unfinished, then
    run :func:`_install_tail` (unless ``install`` is False: the key is saved for a
    later plain ``setup`` to finish, and the record notes it waits on ``paused_subs``
    being paused, which that ``setup`` reads again before it installs). The held code named by ``drop_hold`` is dropped
    in the SAME state write that records the unfinished install, once the key is in
    ``~/.netrc`` — so a write that fails keeps the code, and some record always
    remains for a later ``setup`` to continue from."""
    netrc = Path(netrc_path) if netrc_path else netrc_writer.default_path()
    ok, action = write_index_credential(key, path=netrc)
    if not ok:
        return TrialOutcome(False, "netrc_failed", _netrc_failed_line(netrc, kind, last_offer))
    state = _read_state(state_path)
    state["install"] = {"kind": kind, "buyer": bool(buyer), "at": _now()}
    if not install:
        state["install"].update(deferred="paused", subscriptions=list(paused_subs))
    if drop_hold:
        state.pop(drop_hold, None)
    _write_state(state_path, state)
    if action == "appended-unparsed":
        if kind == "trial" and last_offer:
            return TrialOutcome(False, "netrc_unparsed",
                                f"{netrc} has an entry that could not be safely updated (it shares "
                                "a line with another entry), so your new credential was appended "
                                f"instead. Clean up {netrc} by hand. For help continuing trial "
                                f"setup, email {SUPPORT_EMAIL}.")
        return TrialOutcome(False, "netrc_unparsed",
                            f"{netrc} has an entry that could not be safely updated (it shares a "
                            "line with another entry) — your new credential was appended "
                            f"instead. Please clean up {netrc} by hand, then re-run setup.")
    if not install:
        return TrialOutcome(False, "saved", "")
    return _install_tail(netrc, kind=kind, buyer=buyer, last_offer=last_offer,
                         backends=backends, runner=runner, is_active=is_active, sleep=sleep,
                         attempts=attempts, state_path=state_path)


def _show(ui: Ui, outcome: TrialOutcome) -> TrialOutcome:
    if outcome.message:
        ui.row("ok" if outcome.ok else "warn", outcome.message)
    return outcome


# ═══════════════════════════════════ The trial ═════════════════════════════════
# The setup wizard's trial step. While another offer remains (build rule 1: an
# attempt that ends without a No to the offer does not start the gap between
# offers, so the next setup offers it again), a line that stops the attempt says to
# try again; on the last offer it says nothing of a later one, and a user who cannot
# finish is sent to support.

RELEASE_CLOSED = "release_closed"

_TRIAL_STOPPED = "Trial setup stopped. To try again, run `buddhi-review setup`."
_TRIAL_STOPPED_LAST = "Trial setup stopped."
_TRIAL_ENDED = ("Your Buddhi Review Pro trial has ended. The free version is still "
                "available.")


def _trial_stopped(last_offer: bool) -> str:
    return _TRIAL_STOPPED_LAST if last_offer else _TRIAL_STOPPED


def _trial_answer_line(a: BridgeAnswer, last_offer: bool) -> str:
    """The line for a trial request's answer that ends the attempt (a 429, trial
    mail paused, no answer, or one retrying cannot fix)."""
    if a.status == 429:
        if last_offer:
            return ("This trial setup could not finish because too many requests have been "
                    "made for now.")
        return ("Too many requests have been made for now. Try again in about "
                f"{_time_phrase(a.retry_after)} with `buddhi-review setup`.")
    if a.status == 503 and a.error == "sending_paused":
        if last_offer:
            return "Trial codes cannot be sent right now, so this trial setup could not finish."
        return ("Trial codes cannot be sent right now. Try again in about "
                f"{_time_phrase(a.retry_after)} with `buddhi-review setup`.")
    if _no_answer(a):
        if last_offer:
            return ("The Buddhi Review service is not answering right now, so this trial setup "
                    "could not finish.")
        return ("The Buddhi Review service is not answering right now. Try again in a few "
                "minutes with `buddhi-review setup`.")
    if last_offer:
        return ("Buddhi Review did not accept this request. This points to a fault in this "
                "version of Buddhi Review, not in anything you did. Run `buddhi-review "
                f"upgrade`, and for help continuing trial setup, email {SUPPORT_EMAIL}.")
    return _FAULT


def _trial_unsaved(path: Path, why: str, last_offer: bool) -> str:
    if last_offer:
        return (f"Buddhi Review could not save its trial details in {path} ({why}), so this "
                "trial setup could not finish. Fix that folder's permissions. For help "
                f"continuing trial setup, email {SUPPORT_EMAIL}.")
    return (f"Buddhi Review could not save its trial details in {path} ({why}), so this "
            "trial setup could not finish. Fix that folder's permissions, then run "
            "`buddhi-review setup` again.")


def _trial_refused(last_offer: bool) -> str:
    if last_offer:
        return ("You did not accept the agreement, so trial setup stopped. The free version "
                "keeps working.")
    return ("You did not accept the agreement, so trial setup stopped. The free version keeps "
            "working. To start the trial, run `buddhi-review setup` again and accept the "
            "agreement.")


def _no_new_code(ui: Ui, last_offer: bool) -> TrialOutcome:
    line = _trial_stopped(last_offer)
    ui.row("info", line)
    ui.row("step", UPGRADE_POINTER)
    return TrialOutcome(False, "no_new_code", line)


def start_trial(email: str, *, release: Optional["ReleaseCheck"] = None, ui: Optional[Ui] = None,
                last_offer: bool = False, backends=None, runner=None, netrc_path=None,
                is_active=None, sleep=None, attempts=20,
                state_path: Optional[Path] = None) -> TrialOutcome:
    """Start (or continue) this computer's trial for ``email``: the licence agreement,
    a code emailed by the service, the code at a prompt, then the key and the install.
    Every line goes through ``ui``; the outcome's ``status`` says how it ended
    (``send_failed`` asks the caller for another address).

    Nothing happens unless the paid release is published with a wheel for this
    interpreter: ``release`` is the setup run's :class:`ReleaseCheck` (a fresh one —
    one request — when not given). While it is closed there is nothing to install, so
    no request is made and the outcome is :data:`RELEASE_CLOSED`, with no message.

    Ctrl-C or end of input anywhere in it propagates to :func:`trial_step`, which
    shows the trial's stop line."""
    if not (release or ReleaseCheck()).is_open():
        return TrialOutcome(False, RELEASE_CLOSED, "")
    if not valid_email(email):
        return TrialOutcome(False, "bad_email", "")
    ui = ui or Ui.terminal()
    path = state_path or _state_path()
    verdict, why = _eula_gate(ui, state_path)
    if verdict != "ok":
        line = {"refused": _trial_refused(last_offer), "no_terminal": _EULA_NO_TERMINAL}.get(
            verdict) or _trial_unsaved(path, why, last_offer)
        ui.row("warn", line)
        return TrialOutcome(False, f"eula_{verdict}", line)
    state = _read_state(state_path)
    nonce = state.get("trial_nonce")
    if not (isinstance(nonce, str) and _SETUP_CODE_RE.match(nonce)):
        nonce = _new_nonce()
        state["trial_nonce"] = nonce
        why = _save_state(state_path, state)
        if why:
            line = _trial_unsaved(path, why, last_offer)
            ui.row("warn", line)
            return TrialOutcome(False, "unsaved", line)
    fingerprint = trial_fingerprint(state_path=state_path)

    while True:  # one pass per code the service is asked to send
        sent = _bridge_call("POST", "/trial/code", {"email": email, "fingerprint": fingerprint})
        if sent.status == 422 and sent.error == "send_failed":
            line = "We could not send to that address. Check it and try again."
            ui.row("warn", line)
            return TrialOutcome(False, "send_failed", line)
        if sent.status != 202 or _no_answer(sent):
            line = _trial_answer_line(sent, last_offer)
            ui.row("warn", line)
            return TrialOutcome(False, "code_request_failed", line)
        ui.row("info", "If this address can start a trial on this computer, a 6-digit code is "
                       f"on its way from Buddhi Review ({_CODES_SENDER}). It expires in 10 "
                       "minutes.")
        while True:  # one pass per code typed
            # Hidden: the emailed code authenticates the trial claim, so it must not
            # reach the scrollback.
            code = "".join((ui.ask_hidden("Enter the 6-digit code (press Enter if none "
                                          "arrived): ") or "").split())
            if not code:
                ui.row("info", "No code yet? It can take a few minutes, so check your spam "
                               "folder. If an email says this address or this computer has "
                               "already used its trial, no code will come.")
                if ui.yes_no("Send a new code?", False):
                    break
                return _no_new_code(ui, last_offer)
            if not _TRIAL_CODE_RE.match(code):
                ui.row("warn", "That is not a trial code. A trial code has 6 digits.")
                continue
            answer = _call_retrying("POST", "/trial", {"email": email, "fingerprint": fingerprint,
                                                       "code": code, "claim_nonce": nonce})
            key = answer.data.get("licence_key") if answer.data else None
            if answer.status == 200 and isinstance(key, str) and key:
                return _show(ui, _finish_install(
                    key, kind="trial", last_offer=last_offer, backends=backends, runner=runner,
                    netrc_path=netrc_path, is_active=is_active, sleep=sleep, attempts=attempts,
                    state_path=state_path))
            if answer.status == 403:
                ui.row("warn", "That code is not valid. It may be mistyped or expired, or too "
                               "many wrong codes were entered for this address in the last 24 "
                               "hours, and then a new code also fails until that time has "
                               "passed. Ask for a new one.")
                if ui.yes_no("Send a new code?", True):
                    break
                return _no_new_code(ui, last_offer)
            if answer.status == 410 and answer.error == "trial_ended":
                # Recorded like a No to the offer: this computer is not offered the trial
                # again (build rule 11). Nothing installs.
                state = _read_state(state_path)
                state["declined"] = True
                state.pop("trial_nonce", None)
                _write_state(state_path, state)
                ui.row("info", _TRIAL_ENDED)
                ui.row("step", UPGRADE_POINTER)
                return TrialOutcome(False, "trial_ended", _TRIAL_ENDED)
            if _no_answer(answer) or answer.status == 200:   # a 200 without a key: unreadable
                line = (f"The trial setup could not finish. For help continuing trial setup, "
                        f"email {SUPPORT_EMAIL}." if last_offer else
                        "The trial setup could not finish. To try again, run `buddhi-review "
                        "setup` in a few minutes, using the same email. If a trial was started "
                        "and is still live, setup will try to continue it.")
            else:
                line = _trial_answer_line(answer, last_offer)
            ui.row("warn", line)
            return TrialOutcome(False, "trial_failed", line)


def _prompt_trial_email(ui: Ui) -> Optional[str]:
    """Ask for the trial email and check its shape, at most three tries. Returns a valid
    address, or None when the user gives up or enters nothing (no line: it ends the
    attempt quietly)."""
    for _ in range(3):
        email = (ui.ask("Email to set up your trial: ") or "").strip()
        if not email:
            return None
        if valid_email(email):
            return email
        ui.row("warn", _EMAIL_RETRY)
    return None


def trial_step(ui: Ui, *, release: Optional["ReleaseCheck"] = None, backends=None, runner=None,
               netrc_path=None, is_active=None, sleep=None, attempts=20,
               state_path: Optional[Path] = None) -> bool:
    """The setup wizard's trial step: the offer, shown only in a terminal, while the
    offer's gating allows it (:func:`offer_allowed`) and once the paid release is
    published with a wheel for this interpreter (``release``, the setup run's
    :class:`ReleaseCheck`). Until then it is silent and spends no offer.

    A No is a durable decline. Anything else that ends the attempt — an empty email,
    no code, a refused agreement, a failed request, Ctrl-C — leaves the next setup
    free to offer again (build rule 1), within the offer cap. Returns True when the
    user stopped setup with Ctrl-C or end of input here."""
    if not ui.interactive:
        return False
    if not offer_allowed(backends=backends, state_path=state_path):
        return False
    release = release or ReleaseCheck()
    if not release.is_open():
        return False
    last_offer = record_offer_shown(state_path=state_path)
    try:
        ui.row("step", OFFER_ROW)
        if not ui.yes_no(OFFER_QUESTION, True):
            record_declined(state_path=state_path)
            return False
        _waive_offer_gap(state_path)
        while True:
            email = _prompt_trial_email(ui)
            if email is None:
                return False
            outcome = start_trial(email, release=release, ui=ui, last_offer=last_offer,
                                  backends=backends, runner=runner, netrc_path=netrc_path,
                                  is_active=is_active, sleep=sleep, attempts=attempts,
                                  state_path=state_path)
            if outcome.status != "send_failed":
                return False
    except (KeyboardInterrupt, EOFError):
        _waive_offer_gap(state_path)
        ui.row("info", _trial_stopped(last_offer))
        return True


# ═══════════════════════ Buying from the tool: setup --buy ═════════════════════

_BUY_WAIT_S = 30 * 60          # how long --buy waits for the payment
_SEAT_WAIT_S = 2 * 60          # how long a seat may stay "being made" (202) before the tool stops
_PAIR_TRIES = 3

_BUY_WAIT_LINE = ("Waiting for your payment. Pay in your browser and keep this window open; "
                  "setup continues on its own.")
_BUY_NOT_SEEN = ("We could not see a payment yet. If you paid, do not pay again: run "
                 "`buddhi-review setup` in a few minutes. If it still cannot find your payment, "
                 f"run `buddhi-review setup --recover` {_RECOVERY_PHRASE}, or email "
                 f"{SUPPORT_EMAIL}. If the checkout page showed you a setup code, run "
                 "`buddhi-review setup --activate` and paste it instead. If you did not pay, "
                 "nothing was charged.")
_BUY_LONG = ("Your payment was received, but setup is not finished yet. Do not pay again. Run "
             "`buddhi-review setup` again in a few minutes; it finishes this purchase.")
_BUY_CTRLC = ("Stopped waiting. If you paid, do not pay again: run `buddhi-review setup` to "
              "finish. If it cannot find your payment, run `buddhi-review setup --recover` "
              f"{_RECOVERY_PHRASE}.")
_BUY_409 = ("The seat for this purchase is not active right now: the subscription may be "
            "paused, cancelled, refunded or in a payment dispute, or the seat is being moved by "
            "a recovery. Nothing was used up; run `buddhi-review setup` again in a few minutes. "
            f"If this lasts, email {SUPPORT_EMAIL}.")
_BUY_410_DUP = ("Buddhi Review found two purchases made through this computer's checkout, so "
                "it cannot set up this computer by itself. Run `buddhi-review setup --recover` "
                f"{_RECOVERY_PHRASE}. If you meant to buy only once, email {SUPPORT_EMAIL}.")
_BUY_410_USED = ("This purchase can no longer be finished from here. To set up this computer, "
                 f"run `buddhi-review setup --recover` {_RECOVERY_PHRASE}.")
_BUY_NEW_CHECKOUT = ("An earlier checkout from this computer has no payment Buddhi Review can "
                     "see. If you paid in the last few minutes, do not pay again: run "
                     "`buddhi-review setup` in a few minutes instead. If you paid earlier and "
                     "Buddhi Review still cannot find your payment, run `buddhi-review setup "
                     f"--recover` {_RECOVERY_PHRASE}, or email {SUPPORT_EMAIL}. Start a new "
                     "checkout?")
_BUY_OVER_UNFINISHED = ("An earlier Buddhi Review Pro setup on this computer may be unfinished. "
                        "To continue it, run `buddhi-review setup`. Start a new checkout anyway?")
_HELD_CHECK = "Checking for a payment from your earlier checkout on this computer."
_HELD_404 = ("We could not find a payment for your earlier checkout yet. If you paid, do not "
             "pay again: later `buddhi-review setup` runs keep checking for it, without this "
             "message, for up to 6 days. If Buddhi Review Pro is still not set up after the "
             f"next one, run `buddhi-review setup --recover` {_RECOVERY_PHRASE}, or email "
             f"{SUPPORT_EMAIL}. If you did not pay, nothing was charged.")
_PAID_KINDS = ("buy", "activate", "recover")


def _claimed_key(a: BridgeAnswer) -> Optional[str]:
    key = a.data.get("licence_key") if a.status == 200 and a.data else None
    return key if isinstance(key, str) and key else None


def _manage_secret(data: dict) -> Optional[Tuple[str, str, int]]:
    sub, secret = data.get("subscription_id"), data.get("manage_secret")
    version = data.get("manage_secret_version")
    if isinstance(sub, str) and sub and isinstance(secret, str) and secret \
            and isinstance(version, int) and not isinstance(version, bool):
        return sub, secret, version
    return None


def _install_claimed(ui: Ui, a: BridgeAnswer, *, kind: str, hold: str, opts: dict) -> int:
    """A ``200`` from ``/claim``: keep the manage secret (seat 1's first claim), write
    the key, install. The held code is dropped once the key is in ``~/.netrc``."""
    secret = _manage_secret(a.data)
    failure = save_seat_admin([secret]) if secret else None
    try:
        outcome = _show(ui, _finish_install(
            _claimed_key(a), kind=kind, buyer=secret is not None, drop_hold=hold, **opts))
    except KeyboardInterrupt:
        # The key is saved and the install recorded as unfinished: the next plain
        # setup finishes it. No line here would be true for every stopping point.
        return 1
    if failure:
        ui.row("warn", _seat_admin_line(failure, installed=outcome.ok))
    return 0 if outcome.ok else 1


def _buy_answer_line(a: BridgeAnswer) -> Optional[str]:
    """The line for a /claim answer that ends a --buy wait, or None to keep waiting."""
    if a.status == 409 and a.error == "seat_not_active":
        return _BUY_409
    if a.status == 410 and a.error == "duplicate_purchase":
        return _BUY_410_DUP
    if a.status == 410:
        return _BUY_410_USED
    return None


def _await_buy(ui: Ui, hold: dict, opts: dict, *, wait_s: float,
               first: Optional[BridgeAnswer] = None) -> int:
    """Poll ``/claim`` with the held ``--buy`` code until the payment's seat has a
    key. A ``404``, a ``429``, any ``5xx``, an unreadable answer or a network error
    keeps it waiting (never the shared lines, whose "run the same command again" would
    open a second checkout); it stops after ``wait_s``, or 2 minutes after the first
    ``202``."""
    state_path = opts.get("state_path")
    body = {"setup_code": hold["code"], "claim_nonce": hold["nonce"]}
    started, seen_202 = _mono(), None
    said_wait = said_waiting = False
    answer = first
    try:
        while True:
            if answer is None:
                answer = _bridge_call("POST", "/claim", body)
            if _claimed_key(answer):
                _edit_hold(state_path, "buy", first_accepted_at=hold.get("first_accepted_at")
                           or _now())
                return _install_claimed(ui, answer, kind="buy", hold="buy", opts=opts)
            if answer.status == 202 and answer.data is not None:
                if not hold.get("first_accepted_at"):
                    hold["first_accepted_at"] = _now()
                    _edit_hold(state_path, "buy", first_accepted_at=hold["first_accepted_at"])
                if not said_waiting:
                    ui.row("info", _WAITING)
                    said_waiting = True
                seen_202 = seen_202 if seen_202 is not None else _mono()
                if _mono() - seen_202 >= _SEAT_WAIT_S:
                    ui.row("warn", _BUY_LONG)
                    return 1
            elif answer.status in (200, 404, 429) or _no_answer(answer):
                if seen_202 is not None:
                    # The payment was seen: silence now ends in the finishing line.
                    if _mono() - seen_202 >= _SEAT_WAIT_S:
                        ui.row("warn", _BUY_LONG)
                        return 1
                else:
                    if not said_wait:
                        ui.row("info", _BUY_WAIT_LINE)
                        said_wait = True
                    if _mono() - started >= wait_s:
                        ui.row("warn", _BUY_NOT_SEEN)
                        return 1
            else:
                line = _buy_answer_line(answer)
                if line is None:
                    ui.row("warn", _FAULT)
                    return 1
                if answer.status == 410:
                    _drop_hold(state_path, "buy")
                ui.row("warn", line)
                return 1
            if seen_202 is not None:
                remaining, end_line = _SEAT_WAIT_S - (_mono() - seen_202), _BUY_LONG
            else:
                remaining, end_line = wait_s - (_mono() - started), _BUY_NOT_SEEN
            if not _poll_pause(answer, remaining):
                ui.row("warn", end_line)
                return 1
            answer = None
    except KeyboardInterrupt:
        ui.row("info", _BUY_CTRLC)
        return 1


def _paid_install_pending(state: dict) -> bool:
    """A paid key (bought, activated or recovered) is in ``~/.netrc``, its install not
    finished yet. A record of an unknown kind is finished as a purchase, so it counts."""
    record = state.get("install")
    return isinstance(record, dict) and record.get("kind") != "trial"


def _paid_install_active(state: dict, backends) -> bool:
    """Pro is active here and not known to be a trial: asked before a second checkout.
    Only a trial this tool started is known as one; an install it has no record of (an
    earlier release, a wiped cache) is asked about, never assumed to be a trial; nor is
    one whose paid key is still waiting to be installed over the trial."""
    known_trial = state.get("last_install_kind") == "trial" and not _paid_install_pending(state)
    return not known_trial and pro_backend_active(backends=backends)


def _run_buy(ui: Ui, release: "ReleaseCheck", opts: dict) -> int:
    state_path = opts.get("state_path")
    if not release.is_open():
        ui.row("warn", _CLOSED)
        return 1
    state = _read_state(state_path)
    if _paid_install_active(state, opts.get("backends")):
        if not ui.yes_no("Buddhi Review Pro is already active on this computer. Buy another "
                         "subscription?", False):
            ui.row("info", _NOTHING_CHANGED)
            return 0
    elif _paid_install_pending(state) or _recovery_unfinished(state, _now()):
        # A paid key waits to be installed, or a recovery to finish, and Pro is not
        # running: a plain setup finishes it, so a new checkout is asked about first
        # (before the agreement or any write).
        if not ui.yes_no(_BUY_OVER_UNFINISHED, False):
            ui.row("info", _NOTHING_CHANGED)
            return 0
    if not _paid_eula(ui, state_path):
        return 1
    state = _read_state(state_path)
    if _expire_holds(state, _now()):
        _write_state(state_path, state)
    hold = _held(state, "buy")
    if hold is not None:
        # An earlier checkout from this computer is checked first. It may be paid, so a
        # Ctrl-C here says not to pay again.
        try:
            first = _bridge_call("POST", "/claim", {"setup_code": hold["code"],
                                                    "claim_nonce": hold["nonce"]})
        except KeyboardInterrupt:
            ui.row("info", _BUY_CTRLC)
            return 1
        if first.status == 404:
            if not ui.yes_no(_BUY_NEW_CHECKOUT, False):
                ui.row("info", _NOTHING_CHANGED)
                return 0
            _drop_hold(state_path, "buy")
        elif first.status == 429:
            ui.row("warn", _too_many(first.retry_after))
            return 1
        elif _no_answer(first):
            ui.row("warn", _NOT_ANSWERING)
            return 1
        elif _claimed_key(first) or first.status == 202 or _buy_answer_line(first):
            return _await_buy(ui, hold, opts, wait_s=_BUY_WAIT_S, first=first)
        else:
            ui.row("warn", _FAULT)
            return 1
    path = state_path or _state_path()
    for _ in range(_PAIR_TRIES):
        code, setup_hash = _new_setup_code()
        hold = {"code": code, "nonce": _new_nonce(), "paired_at": _now(),
                "first_accepted_at": None, "held_404_shown": False, "recovered": False}
        why = _update_state(state_path, buy=hold)
        if why:
            ui.row("warn", _unsaved(path, why))
            return 1
        try:
            paired = _call_retrying("POST", "/pair", {"setup_hash": setup_hash})
        except KeyboardInterrupt:
            # No pairing words were shown and no checkout opened: drop the new hold so
            # the "Nothing was bought, used or changed" line stays true.
            _drop_hold(state_path, "buy")
            raise
        phrase = paired.data.get("phrase") if paired.data else None
        if paired.status in (200, 201) and isinstance(phrase, str) and _PHRASE_RE.match(phrase):
            break
        _drop_hold(state_path, "buy")
        if paired.status == 409 and paired.error == "already_paired":
            continue              # draw a new code and pair again, with no line
        ui.row("warn", _too_many(paired.retry_after) if paired.status == 429
               else _NOT_ANSWERING if _no_answer(paired) or paired.status in (200, 201)
               else _FAULT)
        return 1
    else:
        ui.row("warn", _FAULT)
        return 1
    ui.row("step", f"Your pairing words: {phrase}")
    ui.row("info", "The checkout page shows the same four words. If it shows other words, or "
                   "none, close it without paying and run `buddhi-review setup --buy` again.")
    url = f"{CHECKOUT_URL}?{urllib.parse.urlencode({'h': setup_hash})}"
    ui.row("step", "Opening the checkout in your browser.")
    if not ui.open_browser(url):
        ui.row("info", f"Open this page in your browser: {url}")
    return _await_buy(ui, hold, opts, wait_s=_BUY_WAIT_S)


# ═════════════ A setup code from the website or a teammate: --activate ═════════

_ACT_CHECKING = ("Checking your setup code. This takes up to 2 minutes; keep this window open. "
                 "If you bought Pro, do not pay again.")
_ACT_202 = ("Your setup code was found. Finishing Buddhi Review Pro setup — up to 2 minutes. "
            "Keep this window open.")
_ACT_NOT_FOUND = ("We could not find a purchase for this setup code yet. If you bought Pro, do "
                  "not pay again: wait a few minutes, then run `buddhi-review setup --activate` "
                  "and paste the same code; if it still cannot find your payment, run "
                  f"`buddhi-review setup --recover` {_RECOVERY_PHRASE}, or email "
                  f"{SUPPORT_EMAIL}. If someone gave you this code, ask them for a new one.")
_ACT_LONG = ("Setup is not finished yet. Run `buddhi-review setup --activate` again in a few "
             "minutes and paste the same code. If you bought Pro, do not pay again.")
_ACT_CTRLC = ("Stopped waiting. To carry on, run `buddhi-review setup --activate` and paste the "
              "same code. If you bought Pro, do not pay again.")
_ACT_409 = ("This setup code's seat is not active right now: the subscription may be paused, "
            "cancelled, refunded or in a payment dispute, or the seat is being removed, "
            "replaced or moved by a recovery. The code was not used up; try again in a few "
            f"minutes. If this lasts, email {SUPPORT_EMAIL}, or ask the person who gave you "
            "this code.")
_ACT_410_DUP = ("This setup code was used by more than one purchase, so it cannot set up this "
                "computer by itself. If you bought Pro, run `buddhi-review setup --recover` "
                f"{_RECOVERY_PHRASE}. If someone gave you this code, ask them for a new one.")


def _read_setup_code(ui: Ui) -> str:
    """The setup code: offered from the clipboard first, only with the user's consent
    (default No; nothing leaves the machine to find out), else read at the hidden
    prompt."""
    clip = ""
    try:
        clip = (ui.read_clipboard() or "").strip()
    except Exception:
        clip = ""
    if clip and _SETUP_CODE_RE.match(clip) and ui.yes_no(
            f"Use the setup code in your clipboard ({_mask_key(clip)})?", False):
        return clip
    return "".join((ui.ask_hidden("Paste your setup code (it stays hidden):") or "").split())


def _run_activate(ui: Ui, release: "ReleaseCheck", opts: dict) -> int:
    state_path = opts.get("state_path")
    if not release.is_open():
        ui.row("warn", _CLOSED)
        return 1
    if not ui.interactive and _eula_current(_read_state(state_path)):
        ui.row("warn", "This command needs a terminal to ask for your setup code. Run it in a "
                       "terminal.")
        return 1
    if not _paid_eula(ui, state_path):
        return 1
    code = _read_setup_code(ui)
    if not code:
        ui.row("info", "No setup code entered. Run `buddhi-review setup --activate` when you "
                       "have it.")
        return 1
    if not _SETUP_CODE_RE.match(code):
        ui.row("warn", "That is not a setup code. Copy it again from the page that showed it, "
                       "or from the person who gave it to you.")
        return 1
    state = _read_state(state_path)
    if _expire_holds(state, _now()):
        _write_state(state_path, state)
    hold = _held(state, "activate")
    if hold is None or hold.get("code") != code:
        hold = {"code": code, "nonce": _new_nonce(), "first_request_at": _now()}
    why = _update_state(state_path, activate=hold)
    if why:
        ui.row("warn", _unsaved(state_path or _state_path(), why))
        return 1
    body = {"setup_code": code, "claim_nonce": hold["nonce"]}
    seen_404 = seen_202 = None
    try:
        while True:
            answer = _call_retrying("POST", "/claim", body)
            if _claimed_key(answer):
                return _install_claimed(ui, answer, kind="activate", hold="activate", opts=opts)
            if answer.status == 404:
                if seen_404 is None:
                    ui.row("info", _ACT_CHECKING)
                    seen_404 = _mono()
                elif _mono() - seen_404 >= _SEAT_WAIT_S:
                    ui.row("warn", _ACT_NOT_FOUND)
                    return 1
            elif answer.status == 202 and answer.data is not None:
                if seen_202 is None:
                    ui.row("info", _ACT_202)
                    seen_202 = _mono()
                elif _mono() - seen_202 >= _SEAT_WAIT_S:
                    ui.row("warn", _ACT_LONG)
                    return 1
            else:
                if answer.status == 409 and answer.error == "seat_not_active":
                    line = _ACT_409
                elif answer.status == 410:
                    _drop_hold(state_path, "activate")
                    line = _ACT_410_DUP if answer.error == "duplicate_purchase" else _EXPIRED_OR_USED
                elif answer.status == 429:
                    line = _too_many(answer.retry_after)
                elif _no_answer(answer) or answer.status == 200:   # 200 without a key
                    line = _NOT_ANSWERING
                else:
                    line = _FAULT
                ui.row("warn", line)
                return 1
            if answer.status == 404:
                remaining, end_line = _SEAT_WAIT_S - (_mono() - seen_404), _ACT_NOT_FOUND
            else:
                remaining, end_line = _SEAT_WAIT_S - (_mono() - seen_202), _ACT_LONG
            if not _poll_pause(answer, remaining):
                ui.row("warn", end_line)
                return 1
    except KeyboardInterrupt:
        ui.row("info", _ACT_CTRLC)
        return 1


# ═══════════════ Recovering on a new computer: setup --recover ═════════════════

_REC_403 = ("That recovery code is not valid. It may be mistyped, expired (codes last 30 "
            "minutes) or replaced by a newer one, or too many wrong codes were entered for "
            "this address in the last 24 hours, and then a new code also fails until that "
            "time has passed. Run `buddhi-review setup --recover` to get a new code. If this "
            f"keeps happening, email {SUPPORT_EMAIL}.")
_REC_MOVING = ("Moving your seat to this computer. This takes up to 2 minutes; keep this "
               "window open.")
_REC_ACCEPTED = ("Your recovery was accepted, but this computer is not set up yet. Any other "
                 "computer that held seat 1 of your subscriptions is already losing it. Run "
                 "`buddhi-review setup` within the next few minutes to finish here; if that is "
                 "too late, run `buddhi-review setup --recover` again, with a new recovery code.")
_REC_TAIL = ("If your code was right, any other computer that held seat 1 of your "
             "subscriptions may already be losing it. If that run cannot finish it, run "
             "`buddhi-review setup --recover` again, with a new recovery code.")
_REC_NO_ANSWER = ("The Buddhi Review service is not answering right now, so this computer is "
                  "not set up yet. Run `buddhi-review setup` within the next few minutes to "
                  f"finish the move here, if it began. {_REC_TAIL}")
_REC_CTRLC_BEFORE = ("Stopped before Buddhi Review answered, so this computer is not set up "
                     "yet. Run `buddhi-review setup` within the next few minutes to finish the "
                     f"move here, if it began. {_REC_TAIL}")
_REC_FINISHING = "Finishing the move of your seat to this computer."
_REC_CHECKING = "Checking whether the move of your seat to this computer began."
_REC_NEVER_BEGAN = ("Buddhi Review could not confirm this recovery. The move may already have "
                    "begun, and any other computer that held seat 1 of your subscriptions may "
                    "already be losing it. To try again, run `buddhi-review setup --recover` "
                    "with a new recovery code.")
_REC_TOO_LATE = ("The move can no longer be finished from here. Run `buddhi-review setup "
                 "--recover` again, with a new recovery code.")
_REFUSED_LINES = {
    "subscription_not_active": "Subscription {id}: not active (cancelled or refunded), so no "
                               "seat was moved.",
    "needs_attention": "Subscription {id}: waiting for a check by Buddhi Review support, so no "
                       f"seat was moved here. Email {SUPPORT_EMAIL}.",
    "subscription_not_ready": "Subscription {id}: not ready yet, so no seat was moved here. Run "
                              "`buddhi-review setup --recover` again in a few minutes.",
    "superseded": "Subscription {id}: a newer recovery took this seat, so it was not moved "
                  "here. Run `buddhi-review setup --recover` again if this computer should "
                  "have it.",
}
_REFUSED_LINES["expired"] = _REFUSED_LINES["subscription_not_ready"]


def _recovery_body(hold: dict) -> dict:
    return {"email": hold["email"], "code": hold["code"], "claim_nonce": hold["nonce"]}


def _recovery_subscriptions(data: Optional[dict]) -> Optional[List[dict]]:
    subs = data.get("subscriptions") if isinstance(data, dict) else None
    if not isinstance(subs, list) or not all(
            isinstance(s, dict) and isinstance(s.get("subscription_id"), str)
            and isinstance(s.get("seat"), dict) for s in subs):
        return None
    return subs


def _keep_recovery_secrets(data: Optional[dict]) -> Tuple[List[str], Optional[Tuple[str, str]]]:
    """Save the rotated manage secrets an answer carries (every subscription but a
    superseded one). Returns the subscriptions saved for and the save's failure."""
    entries = [m for m in (_manage_secret(s) for s in _recovery_subscriptions(data) or []
                           if (s.get("seat") or {}).get("error") != "superseded") if m]
    if not entries:
        return [], None
    return [e[0] for e in entries], save_seat_admin(entries)


def _subscription_paused(sub_id: str, secret: Optional[str]) -> Optional[bool]:
    """Whether the subscription's seats are suspended (paused or in a payment
    dispute), as ``GET /seats`` reads with the new manage secret: True, False, or None
    when it cannot be read (never taken for either)."""
    if not secret:
        return None
    a = _call_retrying("GET", "/seats", query={"subscription_id": sub_id},
                       auth=f"Bearer {secret}")
    reasons = a.data.get("reasons") if a.status == 200 and a.data else None
    if not isinstance(reasons, list):
        return None
    return len(reasons) > 0


def _recovery_complete(ui: Ui, data: dict, opts: dict) -> int:
    """A ``200`` from ``/recover``: one line per subscription, the seat-access file,
    then the key of one moved seat into ``~/.netrc`` and the install."""
    state_path = opts.get("state_path")
    subs = _recovery_subscriptions(data) or []
    if not subs:
        _drop_hold(state_path, "recover")
        ui.row("info", "No Buddhi Review Pro purchase was found for this recovery address.")
        return 1
    try:
        return _recovery_seats(ui, data, subs, opts)
    except KeyboardInterrupt:
        # The service has moved the seats; the held code, accepted, lets the next plain
        # setup finish here.
        ui.row("info", _REC_ACCEPTED)
        return 1


_PAUSED_NOT_INSTALLED = ("Buddhi Review Pro is not installed yet, because none of these "
                         "subscriptions is active now. Once one is, run `buddhi-review setup` "
                         "to finish.")


def _still_paused(record: dict) -> bool:
    """Whether an install deferred because every moved subscription was paused should
    stay deferred: every one of them still reads as paused. A subscription that cannot
    be read is never taken as paused, so the install goes ahead as it would have."""
    subs = record.get("subscriptions")
    if not isinstance(subs, list) or not subs:
        return False
    return all(isinstance(s, str) and _subscription_paused(s, _seat_admin_secret(s)) is True
               for s in subs)


def _recovery_seats(ui: Ui, data: dict, subs: list, opts: dict) -> int:
    state_path = opts.get("state_path")
    saved_ids, failure = _keep_recovery_secrets(data)
    moved: List[Tuple[str, str, bool]] = []
    kept: List[str] = []
    for sub in subs:
        sub_id, seat = sub["subscription_id"], sub["seat"]
        key = seat.get("licence_key")
        if seat.get("status") == "ready" and isinstance(key, str) and key:
            secret = sub.get("manage_secret") if isinstance(sub.get("manage_secret"), str) else None
            paused = _subscription_paused(sub_id, secret)
            moved.append((sub_id, key, paused))
            kept.append(sub_id)
            ui.row("ok", f"Subscription {sub_id}: seat 1 is now on this computer, but the "
                         "subscription is paused or in a payment dispute, so Pro does not run "
                         "under it until that ends." if paused is True else
                   f"Subscription {sub_id}: seat 1 is now on this computer.")
            continue
        error = seat.get("error") if isinstance(seat.get("error"), str) else ""
        if error != "superseded":
            kept.append(sub_id)
        line = _REFUSED_LINES.get(error, _REFUSED_LINES["subscription_not_ready"])
        ui.row("info", line.format(id=sub_id))
    if kept:
        ui.row("info", f"Any other computer that held seat 1 of {_join_ids(kept)} loses Pro the "
                       "next time it connects, and within 7 days if it stays offline.")
        if saved_ids and failure is None:
            ui.row("info", f"Seat management for {_join_ids(saved_ids)} is now on this computer.")
    # A held --buy code goes quiet from now on: this computer runs Pro from a recovery.
    _edit_hold(state_path, "buy", recovered=True)
    if not moved:
        _drop_hold(state_path, "recover")
        ui.row("info", "No seat was moved here, so Buddhi Review Pro was not installed.")
        if failure:
            ui.row("warn", _seat_admin_line(failure, installed=False))
        return 1
    # Build rule 7: the first moved seat whose subscription is read with no reasons,
    # else the first moved seat. Pro is installed unless every one is read as paused.
    active = [m for m in moved if m[2] is False]
    chosen = (active or moved)[0]
    if len(moved) > 1:
        ui.row("info", f"This computer runs Pro under subscription {chosen[0]}.")
    try:
        outcome = _show(ui, _finish_install(
            chosen[1], kind="recover", buyer=True,
            install=not all(m[2] is True for m in moved),
            paused_subs=[m[0] for m in moved], drop_hold="recover", **opts))
    except KeyboardInterrupt:
        return 1    # the key is saved; the next plain setup finishes the install
    if outcome.status == "saved":
        ui.row("info", _PAUSED_NOT_INSTALLED)
    if failure:
        ui.row("warn", _seat_admin_line(failure, installed=outcome.ok))
    return 0 if outcome.ok else 1


def _await_recovery(ui: Ui, hold: dict, opts: dict, *, answered: bool,
                    plain: bool = False) -> int:
    """Send ``/recover`` (the same request, the same nonce) and follow it to the end.
    ``answered`` is whether the service has accepted this code before."""
    state_path = opts.get("state_path")
    body = _recovery_body(hold)
    try:
        answer = _call_retrying("POST", "/recover", body)
    except KeyboardInterrupt:
        ui.row("info", _REC_ACCEPTED if answered else _REC_CTRLC_BEFORE)
        return 1
    if answer.status == 403:
        _drop_hold(state_path, "recover")
        ui.row("warn", _REC_TOO_LATE if answered else _REC_NEVER_BEGAN if plain else _REC_403)
        return 1
    if answer.status == 429:
        if not plain:
            _drop_hold(state_path, "recover")
        ui.row("warn", _too_many(answer.retry_after))
        return 1
    if _no_answer(answer) or (answer.status in (200, 202)
                              and _recovery_subscriptions(answer.data) is None):
        ui.row("warn", _REC_ACCEPTED if answered else _REC_NO_ANSWER)
        return 1
    if answer.status not in (200, 202):
        _drop_hold(state_path, "recover")
        ui.row("warn", _FAULT)
        return 1
    if plain and not answered:
        ui.row("info", _REC_FINISHING)
    # Accepted (200 or 202): the code is held until the service's own replay deadline,
    # so a later plain setup can still finish whatever this run cannot.
    hold["accepted"] = True
    hold["replay_until"] = _iso_epoch(answer.data.get("replay_until")) or (_now() + 15 * 60)
    _edit_hold(state_path, "recover", accepted=True, replay_until=hold["replay_until"])
    if answer.status == 200:
        return _recovery_complete(ui, answer.data, opts)
    # 202: the manage secrets are already in it, so they are kept now.
    _keep_recovery_secrets(answer.data)
    ui.row("info", _REC_MOVING)
    started = _mono()
    try:
        while _mono() - started < _SEAT_WAIT_S:
            if not _poll_pause(answer, _SEAT_WAIT_S - (_mono() - started)):
                break
            answer = _bridge_call("POST", "/recover", body)
            if answer.status == 200 and _recovery_subscriptions(answer.data) is not None:
                return _recovery_complete(ui, answer.data, opts)
            if answer.status == 403:
                _drop_hold(state_path, "recover")
                ui.row("warn", _REC_TOO_LATE)
                return 1
    except KeyboardInterrupt:
        pass
    ui.row("info", _REC_ACCEPTED)
    return 1


def _ask_address(ui: Ui, prompt: str, check: Callable[[str], bool], bad_line: str) -> Optional[str]:
    while True:
        value = (ui.ask(prompt) or "").strip()
        if not value:
            ui.row("info", "No recovery address entered. Nothing was changed.")
            return None
        if check(value):
            return value
        ui.row("warn", bad_line)


def _recover_seats(ui: Ui, release: "ReleaseCheck", opts: dict) -> int:
    state_path = opts.get("state_path")
    if not release.is_open():
        ui.row("warn", _CLOSED)
        return 1
    if not _paid_eula(ui, state_path):
        return 1
    if not ui.yes_no("Moving seat 1 here ends Pro on any other computer that holds it. "
                     "Continue?", False):
        ui.row("info", _NOTHING_CHANGED)
        return 0
    email = _ask_address(ui, "Your recovery address (the email you first paid with, unless you "
                             "changed it): ", valid_recovery_address, _EMAIL_RETRY)
    if email is None:
        return 1
    sent = _call_retrying("POST", "/recover/code", {"email": email})
    if sent.status != 202 or sent.data is None:
        ui.row("warn", _too_many(sent.retry_after) if sent.status == 429
               else _NOT_ANSWERING if _no_answer(sent) else _FAULT)
        return 1
    ui.row("info", "If this is the recovery address of a Buddhi Review Pro purchase, a recovery "
                   f"code is on its way from Buddhi Review ({_CODES_SENDER}). It expires in 30 "
                   "minutes.")
    while True:
        raw = ui.ask_hidden("Paste the recovery code (it stays hidden; press Enter if none "
                            "arrived): ")
        if not "".join((raw or "").split()):
            ui.row("info", _NO_RECOVERY_CODE)
            return 1
        code = _recovery_code(raw)
        if code is not None:
            break
        ui.row("warn", "That is not a recovery code. A recovery code has 16 letters and digits, "
                       "shown as XXXX-XXXX-XXXX-XXXX. Copy it again from the email.")
    hold = {"email": email, "code": code, "nonce": _new_nonce(), "first_request_at": _now(),
            "accepted": False, "replay_until": None}
    why = _update_state(state_path, recover=hold)
    if why:
        ui.row("warn", _unsaved(state_path or _state_path(), why))
        return 1
    return _await_recovery(ui, hold, opts, answered=False)


# ── Choice 2: changing the recovery address (no release check, nothing installs) ──

_ADDR_403 = ("That code is not valid. It may be mistyped, expired (codes last 30 minutes) or "
             "replaced by a newer one, or too many wrong codes were entered for this address in "
             "the last 24 hours, and then a new code also fails until that time has passed. "
             "Start again with `buddhi-review setup --recover`.")
_ADDR_UNCHANGED = "Your recovery address was not changed."


def _address_answer_line(a: BridgeAnswer, new: str) -> str:
    if a.status == 403:
        return _ADDR_403
    if a.status == 422 and a.error == "send_failed":
        return (f"We could not send to {new}. The codes for this change no longer work. Start "
                "again with another address.")
    if a.status == 503 and a.error == "sending_paused":
        return ("Codes cannot be sent right now. Try again in about "
                f"{_time_phrase(a.retry_after)} with `buddhi-review setup --recover`.")
    if a.status == 429:
        return _too_many(a.retry_after)
    if _no_answer(a):
        return _NOT_ANSWERING
    return _FAULT


def _change_address(ui: Ui) -> int:
    current = _ask_address(ui, "Your current recovery address: ", valid_recovery_address,
                           _EMAIL_RETRY)
    if current is None:
        return 1
    new = _ask_address(ui, "Your new recovery address: ", valid_new_recovery_address,
                       "That new address cannot be used. Use a plain address made of letters, "
                       "digits and . _ % + - such as name@example.com.")
    if new is None:
        return 1
    first = _call_retrying("POST", "/recover/address/code", {"email": current, "new_email": new})
    if first.status == 400 and first.error == "same_address":
        ui.row("info", "The new address is the same as the current one. Nothing was changed.")
        return 1
    if first.status != 202 or first.data is None:
        ui.row("warn", _address_answer_line(first, new))
        return 1
    ui.row("info", f"If {current} is the recovery address of a Buddhi Review Pro purchase, a code "
                   f"is on its way to it from Buddhi Review ({_CODES_SENDER}). It expires in 30 "
                   "minutes.")
    try:
        raw = ui.ask_hidden(f"Paste the code sent to {current} (it stays hidden; press Enter if "
                            "none arrived): ")
        if not "".join((raw or "").split()):
            ui.row("info", _NO_RECOVERY_CODE)
            return 1
        current_code = _recovery_code(raw)
        if current_code is None:
            ui.row("warn", _ADDR_403)
            return 1
        while True:  # step 2: the code to the new address (asked again on request)
            second = _call_retrying("POST", "/recover/address/new-code",
                                    {"email": current, "new_email": new,
                                     "current_code": current_code})
            if second.status != 202 or second.data is None:
                ui.row("warn", _address_answer_line(second, new))
                return 1
            ui.row("info", f"A second code is on its way to {new}. It expires in 30 minutes.")
            raw = ui.ask_hidden(f"Paste the code sent to {new} (it stays hidden; press Enter if "
                                "none arrived): ")
            if "".join((raw or "").split()):
                break
            ui.row("info", f"No code at {new} yet? It can take a few minutes, so check your spam "
                           "folder.")
            if not ui.yes_no("Send it again?", False):
                ui.row("info", "Your recovery address was not changed. Run `buddhi-review setup "
                               "--recover` to try again.")
                return 1
        new_code = _recovery_code(raw)
        if new_code is None:
            ui.row("warn", _ADDR_403)
            return 1
    except (KeyboardInterrupt, EOFError):
        ui.row("info", _ADDR_UNCHANGED)
        return 1
    final = {"email": current, "new_email": new, "current_code": current_code,
             "new_code": new_code}
    try:
        done = _call_retrying("POST", "/recover/address", final)
    except KeyboardInterrupt:
        # The change may already be made: never say it was not. The same two codes
        # within 15 minutes get the same answer, so ask once more and report that.
        try:
            done = _call_retrying("POST", "/recover/address", final)
        except KeyboardInterrupt:
            return 1
    if done.status == 200 and done.data is not None:
        ui.row("ok", f"Your recovery address is now {new}. Recovery codes now go only there.")
        return 0
    ui.row("warn", _address_answer_line(done, new))
    return 1


def _run_recover(ui: Ui, release: Optional["ReleaseCheck"], opts: dict) -> int:
    if not ui.interactive:
        state = _read_state(opts.get("state_path"))
        ui.row("warn", "This command needs a terminal to ask for your recovery address and the "
                       "code we email you. Run it in a terminal." if _eula_current(state)
               else _EULA_NO_TERMINAL)
        return 1
    ui.row("step", "What do you want to do?")
    ui.row("info", "1. Move my own Buddhi Review Pro seat to this computer (seat 1 of each of "
                   "my subscriptions)")
    ui.row("info", "2. Change my recovery address")
    while True:
        choice = (ui.ask("") or "").strip()
        if not choice:
            ui.row("info", _STOPPED)
            return 1
        if choice in ("1", "2"):
            break
        ui.row("warn", "Enter 1 or 2.")
    if choice == "2":
        return _change_address(ui)
    return _recover_seats(ui, release or ReleaseCheck(), opts)


# ══════════════════════════ The entry points setup calls ═══════════════════════

def _opts(backends, runner, netrc_path, is_active, sleep, attempts, state_path) -> dict:
    return {"backends": backends, "runner": runner, "netrc_path": netrc_path,
            "is_active": is_active, "sleep": sleep, "attempts": attempts,
            "state_path": state_path}


def _entry(fn: Callable[..., int], ui: Ui, release, opts: dict) -> int:
    """Run one setup path. Ctrl-C or end of input that no step answered itself comes
    before anything is spent."""
    try:
        return fn(ui, release, opts)
    except (KeyboardInterrupt, EOFError):
        ui.row("info", _STOPPED)
        return 1


def run_buy(ui: Ui, *, release: Optional["ReleaseCheck"] = None, backends=None, runner=None,
            netrc_path=None, is_active=None, sleep=None, attempts=20,
            state_path: Optional[Path] = None) -> int:
    """``buddhi-review setup --buy``: pair a new setup code, open the checkout, wait for
    the payment, install. Returns the exit code."""
    return _entry(_run_buy, ui, release or ReleaseCheck(),
                  _opts(backends, runner, netrc_path, is_active, sleep, attempts, state_path))


def run_activate(ui: Ui, *, release: Optional["ReleaseCheck"] = None, backends=None,
                 runner=None, netrc_path=None, is_active=None, sleep=None, attempts=20,
                 state_path: Optional[Path] = None) -> int:
    """``buddhi-review setup --activate``: a setup code from the website or a teammate."""
    return _entry(_run_activate, ui, release or ReleaseCheck(),
                  _opts(backends, runner, netrc_path, is_active, sleep, attempts, state_path))


def run_recover(ui: Ui, *, release: Optional["ReleaseCheck"] = None, backends=None,
                runner=None, netrc_path=None, is_active=None, sleep=None, attempts=20,
                state_path: Optional[Path] = None) -> int:
    """``buddhi-review setup --recover``: move seat 1 of each of the buyer's
    subscriptions here, or change the recovery address."""
    return _entry(_run_recover, ui, release,
                  _opts(backends, runner, netrc_path, is_active, sleep, attempts, state_path))


# ════════════════ What a plain setup finishes first (build rule 2) ═════════════
# Order: the release check (closed: one line per held code, and no held code is
# checked), then a held recovery code, then a held --buy code, then an install whose
# key is already in ~/.netrc, then the wizard.

def has_held_work(*, state_path: Optional[Path] = None) -> bool:
    state = _read_state(state_path)
    _expire_holds(state, _now())
    return any(isinstance(state.get(k), dict) for k in ("recover", "buy", "install"))


def _check_held_buy(ui: Ui, hold: dict, opts: dict) -> None:
    state_path = opts.get("state_path")
    quiet = bool(hold.get("recovered"))
    if not quiet:
        ui.row("info", _HELD_CHECK)
    answer = _bridge_call("POST", "/claim", {"setup_code": hold["code"],
                                             "claim_nonce": hold["nonce"]})
    if _claimed_key(answer) or (answer.status == 202 and answer.data is not None):
        _await_buy(ui, hold, opts, wait_s=0, first=answer)
        return
    if answer.status == 404:
        if not hold.get("held_404_shown"):
            ui.row("info", _HELD_404)
            _edit_hold(state_path, "buy", held_404_shown=True)
        return
    if answer.status == 429 or _no_answer(answer):
        return
    line = _buy_answer_line(answer)
    if answer.status == 410:
        _drop_hold(state_path, "buy")
    if not quiet:
        ui.row("warn", line or _FAULT)


def _finish_unfinished_install(ui: Ui, record: dict, opts: dict) -> None:
    # A recovery whose every subscription was paused saved the key without installing:
    # while they still are, installing would only fail to activate (and drop this
    # record), so the record is kept for the setup the user was told to run.
    if record.get("deferred") == "paused" and _still_paused(record):
        ui.row("info", _PAUSED_NOT_INSTALLED)
        return
    kind = record.get("kind") if record.get("kind") in ("trial",) + _PAID_KINDS else "buy"
    state = _read_state(opts.get("state_path"))
    from buddhi_review import upsell
    shown = state.get("shown_count") if isinstance(state.get("shown_count"), int) else 0
    netrc = Path(opts["netrc_path"]) if opts.get("netrc_path") else netrc_writer.default_path()
    _show(ui, _install_tail(netrc, kind=kind, buyer=bool(record.get("buyer", True)),
                            last_offer=shown >= upsell._max_shows(),
                            **{k: v for k, v in opts.items() if k != "netrc_path"}))


def finish_held_work(ui: Ui, *, release: Optional["ReleaseCheck"] = None, backends=None,
                     runner=None, netrc_path=None, is_active=None, sleep=None, attempts=20,
                     state_path: Optional[Path] = None) -> None:
    """A plain ``setup``'s first work: finish what an earlier run left held. Never
    raises on a Ctrl-C it can answer itself; setup carries on to the wizard."""
    opts = _opts(backends, runner, netrc_path, is_active, sleep, attempts, state_path)
    state = _read_state(state_path)
    if _expire_holds(state, _now()):
        _write_state(state_path, state)
    recover, buy, record = _held(state, "recover"), _held(state, "buy"), _held(state, "install")
    if recover is None and buy is None and record is None:
        return
    if not (release or ReleaseCheck()).is_open():
        if recover is not None:
            ui.row("warn", f"{_CLOSED_HEAD} Run `buddhi-review setup` again within the next few "
                           "minutes to finish moving your seat, if the move began; if that is "
                           "too late, run `buddhi-review setup --recover` again, with a new "
                           "recovery code.")
        if buy is not None:
            ui.row("warn", f"{_CLOSED_HEAD} If you paid for Buddhi Review Pro from this computer, "
                           "do not pay again: run `buddhi-review setup` again later to finish. If "
                           f"it cannot find your payment, run `buddhi-review setup --recover` "
                           f"{_RECOVERY_PHRASE}, or email {SUPPORT_EMAIL}.")
        return
    # Every held code and unfinished install ends in a key being used: the licence
    # agreement as it reads now must be accepted first. Refused, everything stays held.
    if not _paid_eula(ui, state_path):
        return
    if recover is not None:
        answered = bool(recover.get("accepted"))
        ui.row("info", _REC_FINISHING if answered else _REC_CHECKING)
        _await_recovery(ui, recover, opts, answered=answered, plain=True)
    state = _read_state(state_path)
    buy = _held(state, "buy")
    if buy is not None:
        _check_held_buy(ui, buy, opts)
    # Only an install left by an EARLIER run: one the recovery or the purchase above
    # wrote has already been tried (or deliberately deferred) in this run.
    state = _read_state(state_path)
    if record is not None and _held(state, "install") == record:
        _finish_unfinished_install(ui, record, opts)


# ════════════════════ The trial offer's gating (the same state file) ═══════════
# The OSS upsell conventions: BUDDHI_NO_UPSELL suppresses; an active Pro backend
# suppresses; a durable No sticks; and the shared upsell cap bounds how many times it
# is shown (5 by default) and how soon after the last showing (24 h). Build rule 1:
# an attempt that ends without a No does not start that gap. Build rule 12: once 14
# days have passed since a trial became active on this computer, the offer is never
# shown here again.

def offer_allowed(*, backends=None, now=None, state_path=None) -> bool:
    """True iff the wizard may show the trial offer."""
    from buddhi_review import upsell
    if upsell.upsell_suppressed():
        return False
    if pro_backend_active(backends=backends):
        return False
    state = _read_state(state_path)
    if state.get("declined") is True:
        return False
    if _paid_install_pending(state):     # a trial key would replace the paid one
        return False
    now = now if now is not None else _now()
    started = _num(state.get("trial_active_since"))
    if started is not None and now - started >= _TRIAL_DAYS * 86400:
        return False
    shown = state.get("shown_count", 0)
    shown = shown if isinstance(shown, int) else 0
    if shown >= upsell._max_shows():
        return False
    last = state.get("last_shown")
    if isinstance(last, (int, float)) and (now - last) < upsell._min_interval_seconds():
        return False
    return True


def record_offer_shown(*, now=None, state_path=None) -> bool:
    """Count one showing of the offer. Returns True when it is the last one the cap
    allows (the last offer)."""
    from buddhi_review import upsell
    state = _read_state(state_path)
    state["shown_count"] = (state.get("shown_count", 0)
                            if isinstance(state.get("shown_count"), int) else 0) + 1
    state["last_shown"] = now if now is not None else _now()
    _write_state(state_path, state)
    return state["shown_count"] >= upsell._max_shows()


def _waive_offer_gap(state_path: Optional[Path]) -> None:
    """Build rule 1: this showing ended without a No, so the next setup may offer the
    trial again at once (it still counted toward the cap)."""
    state = _read_state(state_path)
    if "last_shown" in state:
        state.pop("last_shown", None)
        _write_state(state_path, state)


def record_declined(*, state_path=None) -> None:
    """Durably record 'don't offer the trial again' (honours the upsell durable-
    dismiss convention)."""
    state = _read_state(state_path)
    state["declined"] = True
    _write_state(state_path, state)
