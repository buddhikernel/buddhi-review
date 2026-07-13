"""pro_trial.py — the setup wizard's Pro-trial ACQUISITION plumbing (PRO-6).

This is the ONE sanctioned license-ACQUISITION module in the free tree
(execution-plan §E.9(a)): the setup wizard's first-run "try Pro free for 14 days"
offer routes here, and here ONLY. It ACQUIRES a trial the server-less, cardless,
zero-paste way and then hands off to the installed Pro wheel:

  1. **open registration** — ``POST /users`` on the *unprotected* Keygen account
     (no token), with the buyer's email;
  2. **a user token** — ``POST /tokens`` (HTTP Basic, the email + a locally-generated
     password), which the unprotected account hands back;
  3. **the trial license, created client-side** — ``POST /licenses`` (Bearer the
     user token) under the *unprotected trial policy*. The policy — NOT this code —
     stamps the 14-day window server-side; this module sends NO expiry and could not
     make one stick if it tried (Part 3 Q1);
  4. **the private-index credential** — the returned key is written to ``~/.netrc``
     (merge-preserving, 0600) so ``pip`` can pull the wheel;
  5. **pip install buddhi-review-pro** from the license-gated index;
  6. **daemon start THROUGH backend discovery** — never importing the Pro package,
     only the discovered backend object (§E.2).

It also carries the **convert / re-subscribe** path: print the checkout, then
concierge-paste the emailed paid key (consent-gated clipboard-detect → hidden manual
paste), validate it with the public tokenless ``validate-key`` action, and install.

⛔ **ACQUISITION ONLY — zero entitlement ENFORCEMENT.** Nothing here does runtime
entitlement checking — no cryptographic verification, no runtime entitlement gate,
no expiry math — the compiled wheel checks itself (§E.1/§E.3).
``tests/test_oss_purity.py`` asserts this module holds creation / registration /
validation calls only, and the publish gate allowlists it for the acquisition
vocabulary (the Keygen host + the ``buddhi-review-pro`` package name it installs)
while still scanning it for every other paid surface.

Endpoints / payloads verified against keygen.sh/docs/api (users · tokens · licenses ·
licenses/actions/validate-key · engines/pypi · authentication) 2026-07-12.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import secrets
import shlex
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Tuple

from buddhi_review import netrc_writer

# ── Keygen coordinates (all NON-secret — public in the index URL by design) ───────
# The account slug + the unprotected trial policy id (MP-1). Overridable via env so
# a QA / self-hosted account can be pointed at without editing source.
_ACCOUNT = os.environ.get("BUDDHI_KEYGEN_ACCOUNT", "buddhi")
_API_ROOT = os.environ.get("BUDDHI_KEYGEN_API_ROOT", "https://api.keygen.sh/v1/accounts")
_TRIAL_POLICY_ID = os.environ.get(
    "BUDDHI_TRIAL_POLICY_ID", "40680ca7-99c5-4569-8c1d-611713612d3d")

# The license-gated private index (the key is the index password) + the pip package.
_INDEX_HOST = "pypi.pkg.keygen.sh"
_INDEX_LOGIN = "license"
_PACKAGE = "buddhi-review-pro"

# The subscribe / re-subscribe destination. TODO(MP-2): swap for the live Paddle
# hosted-checkout URL once the buddhikernel.com #buy-link carries it (today it still
# points at the site's contact section, so the bare site is the correct target).
CHECKOUT_URL = "https://buddhikernel.com"

_VND = "application/vnd.api+json"
_HTTP_TIMEOUT_S = 20


def _api_base() -> str:
    return f"{_API_ROOT.rstrip('/')}/{_ACCOUNT}"


# ════════════════════════════ HTTP transport seam ══════════════════════════════
# One injectable seam so the whole flow is network-free under test. A transport is
# ``callable(method, url, auth, body) -> (status, data)`` where ``auth`` is a full
# Authorization header value (or None) and ``data`` is the parsed JSON dict / None.

def _default_transport(method: str, url: str, auth: Optional[str], body):
    headers = {"Accept": _VND}
    data_bytes = None
    if body is not None:
        headers["Content-Type"] = _VND
        data_bytes = json.dumps(body).encode("utf-8")
    if auth:
        headers["Authorization"] = auth
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S, context=ctx) as resp:
            return resp.getcode(), _parse_json(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            payload = _parse_json(exc.read())
        except Exception:
            payload = None
        return exc.code, payload
    except Exception:
        return 0, None


def _parse_json(raw):
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except Exception:
        return None


def _request(method, path, *, auth=None, body=None, transport=None) -> Tuple[int, Optional[dict]]:
    url = f"{_api_base()}{path}"
    fn = transport or _default_transport
    try:
        status, data = fn(method, url, auth, body)
        status = int(status or 0)          # inside the guard: a non-numeric status
    except Exception:                      # from a misbehaving transport is fail-open,
        return 0, None                     # never a raise
    return status, data if isinstance(data, dict) else None


def _ok(status: int) -> bool:
    return 200 <= status < 300


def _basic_auth(email: str, password: str) -> str:
    import base64
    token = base64.b64encode(f"{email}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _first_error_code(data) -> str:
    if isinstance(data, dict):
        for err in data.get("errors") or []:
            if isinstance(err, dict) and err.get("code"):
                return str(err["code"]).upper()
    return ""


# ═══════════════════════════ Acquisition primitives ════════════════════════════

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match((email or "").strip()))


def _gen_password() -> str:
    """A throwaway password used ONLY to mint the user token; never shown to the
    user. The unprotected account requires a credential pair to issue a user token,
    so we generate a strong random one. If registration succeeds but a later step
    (token, license, or the netrc/pip/activation install tail) fails, it is kept as
    a short-lived local retry-state credential (0600, see
    ``_save_pending_credential``) — cleared only once the install actually succeeds
    (see ``start_trial``) — so a same-email retry is not permanently locked out of
    the account it just opened."""
    return secrets.token_urlsafe(24)


def register_user(email: str, password: str, *, transport=None) -> Tuple[int, Optional[str], str]:
    """Open registration on the unprotected account (``POST /users``, NO auth).
    Returns ``(status, user_id, error_code)``. ``user_id`` is None on failure;
    ``error_code`` carries Keygen's first error code (e.g. an email-taken code)."""
    body = {"data": {"type": "users", "attributes": {"email": email, "password": password}}}
    status, data = _request("POST", "/users", body=body, transport=transport)
    if _ok(status) and isinstance(data, dict):
        d = data.get("data")
        if isinstance(d, dict) and d.get("id"):
            return status, d["id"], ""
    return status, None, _first_error_code(data)


def mint_user_token(email: str, password: str, *, transport=None) -> Optional[str]:
    """Exchange the email + password for a user token (``POST /tokens``, HTTP Basic).
    Returns the token string, or None."""
    status, data = _request("POST", "/tokens", auth=_basic_auth(email, password),
                            transport=transport)
    if _ok(status) and isinstance(data, dict):
        d = data.get("data")
        if isinstance(d, dict):
            tok = (d.get("attributes") or {}).get("token")
            if isinstance(tok, str) and tok:
                return tok
    return None


def create_trial_license(user_token: str, user_id: str, *, transport=None) -> Optional[dict]:
    """Create the trial license client-side under the unprotected trial policy
    (``POST /licenses``, Bearer the user token). Sends NO expiry — the policy stamps
    the 14-day window server-side (Q1). Returns the license ``attributes`` dict (the
    key is at ``["key"]``, the server-stamped end date at ``["expiry"]``), or None."""
    body = {"data": {"type": "licenses", "relationships": {
        "policy": {"data": {"type": "policies", "id": _TRIAL_POLICY_ID}},
        "owner": {"data": {"type": "users", "id": user_id}},
    }}}
    status, data = _request("POST", "/licenses", auth=f"Bearer {user_token}",
                            body=body, transport=transport)
    if _ok(status) and isinstance(data, dict):
        d = data.get("data")
        if isinstance(d, dict) and isinstance(d.get("attributes"), dict):
            attrs = d["attributes"]
            if attrs.get("key"):
                return attrs
    return None


def validate_key(key: str, *, transport=None) -> Tuple[bool, str]:
    """Validate a license key via the PUBLIC tokenless ``validate-key`` action
    (``POST /licenses/actions/validate-key``, NO auth — the key IS the proof). Used
    on the convert path to confirm a pasted paid key before installing. Returns the
    server's verdict verbatim as ``(valid, code)`` (code e.g. ``VALID`` /
    ``SUSPENDED`` / ``EXPIRED`` / ``NO_MACHINE``). Callers decide what to DO with a
    given code — see :func:`_key_installable`."""
    status, data = _request("POST", "/licenses/actions/validate-key",
                            body={"meta": {"key": key}}, transport=transport)
    if _ok(status) and isinstance(data, dict):
        meta = data.get("meta") or {}
        return bool(meta.get("valid")), str(meta.get("code") or "")
    return False, ""


# The codes that mean "this key is REAL — it just has no activation on it yet". A
# license whose policy limits it to N machines validates ``false`` while it has none:
# ``NO_MACHINE`` (the node-locked case) / ``NO_MACHINES`` (the floating case). A key
# Keygen emailed minutes ago is in exactly that state — the Pro wheel's own first run
# is what activates this box — so requiring ``valid=True`` here would bounce every
# freshly-bought key before ``_finish_install`` could write ~/.netrc and let the wheel
# do it, i.e. nobody could ever convert from the wizard. Accepting them is NOT an
# entitlement decision (still zero enforcement here, §E.1/§E.3): a key that is not
# actually entitled is refused by the private index (→ the ``index_403`` path), and
# the wheel checks itself regardless. Every genuinely dead verdict — SUSPENDED,
# EXPIRED, BANNED, NOT_FOUND, … — stays rejected.
_ACTIVATION_PENDING_CODES = frozenset({"NO_MACHINE", "NO_MACHINES"})


def _key_installable(valid: bool, code: str) -> bool:
    """Is this validate-key verdict good enough to go install against? True for a
    plain ``valid`` key and for the not-yet-activated codes above — nothing else."""
    return bool(valid) or (code or "").strip().upper() in _ACTIVATION_PENDING_CODES


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


# ═══════════════════════════════ Result type ═══════════════════════════════════

class TrialOutcome:
    """The structured result of a trial / convert attempt. ``ok`` is the headline;
    ``status`` is a stable machine tag (for tests); ``message`` is the user-facing
    line the wizard prints."""

    __slots__ = ("ok", "status", "message")

    def __init__(self, ok: bool, status: str, message: str):
        self.ok, self.status, self.message = ok, status, message

    def __repr__(self):  # pragma: no cover - debug aid
        return f"TrialOutcome(ok={self.ok!r}, status={self.status!r})"


def _convert_pointer() -> str:
    return (f"Already subscribed? Subscribe or paste your Pro key from {CHECKOUT_URL} "
            "and re-run setup.")


def _expiry_phrase(attrs: Optional[dict]) -> str:
    """A friendly 'runs to <date>' phrase from the server-stamped end date, or the
    plain '14-day' fallback. This DISPLAYS the server value — it does no date math."""
    expiry = (attrs or {}).get("expiry") if isinstance(attrs, dict) else None
    if isinstance(expiry, str) and len(expiry) >= 10:
        return f"your 14-day trial runs to {expiry[:10]}"
    return "your 14-day trial is live"


# ═══════════════════════════════ Orchestration ═════════════════════════════════

def _finish_install(key: str, *, attrs=None, is_trial=True, backends=None, runner=None,
                    netrc_path=None, is_active=None, sleep=None, attempts=20) -> TrialOutcome:
    """Shared tail for both the trial and the convert paths: write the index
    credential → pip install → start the daemon → confirm activation → message.
    Every failure leaves ``~/.netrc`` intact (the key is valid; the user can retry
    the install later). ``is_trial`` selects trial-vs-paid wording for the success
    and not-activated messages — ``convert()`` passes a paid key with no ``attrs``,
    so the messaging must not default to trial language."""
    resolved_netrc = netrc_path or netrc_writer.default_path()
    ok, action = write_index_credential(key, path=resolved_netrc)
    if not ok:
        return TrialOutcome(False, "netrc_failed",
                            f"Could not write {resolved_netrc} — check the file's permissions "
                            "and re-run.")
    if action == "appended-unparsed":
        return TrialOutcome(False, "netrc_unparsed",
                            f"{resolved_netrc} has an entry that could not be safely updated (it "
                            "shares a line with another entry) — your new credential was appended "
                            f"instead. Please clean up {resolved_netrc} by hand, then re-run setup.")

    installed, rc, output = pip_install(runner=runner, netrc_path=resolved_netrc)
    if not installed:
        if _looks_like_403(rc, output):
            return TrialOutcome(False, "index_403",
                                "The license index refused the install (403). Your key may not be "
                                "active yet — wait a moment and re-run setup.")
        return TrialOutcome(False, "pip_failed",
                            f"Install did not complete — your license is set up and {resolved_netrc} "
                            f"is intact, so just re-run: NETRC={shlex.quote(str(resolved_netrc))} "
                            f"{shlex.quote(sys.executable)} -m pip install --upgrade {_PACKAGE} "
                            f"--index-url {shlex.quote(_index_url())} --no-input")

    start_daemon(backends=backends)
    if _await_active(backends=backends, is_active=is_active, sleep=sleep, attempts=attempts):
        status_phrase = _expiry_phrase(attrs) if is_trial else "your subscription is active"
        return TrialOutcome(True, "active",
                            f"✓ Buddhi Pro is active — {status_phrase}. "
                            "The live view is starting in the background.")
    # Installed but the machine did not activate. On the trial path this is most
    # often this machine already used its one trial (the policy allows one per
    # machine); on the convert path the paid key may simply be active on another
    # machine already. One calm line + the appropriate pointer; no error spam.
    if is_trial:
        return TrialOutcome(False, "not_activated",
                            "Pro installed, but it did not activate on this machine — it may have "
                            f"already been used for a trial. {_convert_pointer()}")
    return TrialOutcome(False, "not_activated",
                        "Pro installed, but it did not activate on this machine — your key may "
                        f"already be active elsewhere. Contact support via {CHECKOUT_URL}.")


def start_trial(email: str, *, transport=None, backends=None, runner=None,
                netrc_path=None, is_active=None, sleep=None, attempts=20,
                state_path=None) -> TrialOutcome:
    """Run the full server-less trial acquisition for ``email``. Returns a
    :class:`TrialOutcome`; the wizard prints ``.message``. Fully seam-injectable so
    the path is unit-testable without network, pip, or a real backend.

    A registration that succeeds but fails before a license is minted (the token or
    license call errors — e.g. a transient network hiccup) saves its password as a
    short-lived pending credential (see ``_pending_credential``). A later retry for
    the SAME email reuses it to mint a token directly, instead of re-registering —
    which the server would now refuse as already-taken, with no password on this
    end to answer it."""
    if not valid_email(email):
        return TrialOutcome(False, "bad_email", "That doesn't look like an email address.")

    pending = _pending_credential(state_path)
    if pending is not None and pending[0] == email:
        password, user_id = pending[1], pending[2]
    else:
        password = _gen_password()
        status, user_id, code = register_user(email, password, transport=transport)
        if user_id is None:
            # An already-registered email cannot be continued tokenlessly (we do not hold
            # the existing user's password), so route them to the convert path.
            if status in (409, 422) or "TAKEN" in code or "CONFLICT" in code:
                return TrialOutcome(False, "email_registered",
                                    "That email already has a Buddhi account. "
                                    + _convert_pointer())
            return TrialOutcome(False, "register_failed",
                                "Could not start the trial (registration failed) — try again later.")
        _save_pending_credential(email, password, user_id, state_path=state_path)

    token = mint_user_token(email, password, transport=transport)
    if not token:
        return TrialOutcome(False, "token_failed",
                            "Could not start the trial (sign-in failed) — try again later.")

    attrs = create_trial_license(token, user_id, transport=transport)
    if not attrs:
        return TrialOutcome(False, "license_failed",
                            "Could not create the trial license — try again later.")

    # Cleared only once the install actually succeeds — a netrc/pip/activation
    # failure here still leaves the license minted, and the pending credential is
    # what lets a same-email retry mint a fresh token+license directly instead of
    # re-registering (which the server would now refuse as already-taken, with no
    # emailed key to fall back on since this is a trial, not a paid subscription).
    outcome = _finish_install(attrs["key"], attrs=attrs, backends=backends, runner=runner,
                              netrc_path=netrc_path, is_active=is_active, sleep=sleep,
                              attempts=attempts)
    if outcome.ok:
        _clear_pending_credential(state_path=state_path)
    return outcome


# ── Convert / re-subscribe (concierge-paste) ─────────────────────────────────────

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


# A key is ONE opaque whitespace-free token: every Keygen key format — hyphen-grouped
# hex, a UUID, or a signed ``key/<payload>.<signature>`` — is a single run of these
# characters. Prose, a URL, a wrapped paragraph or a passphrase with spaces is
# definitively NOT a key, and must never leave the machine to find that out.
_KEY_SHAPE_RE = re.compile(r"^[A-Za-z0-9._/+=-]{12,4096}$")


def _looks_like_license_key(candidate: str) -> bool:
    """A NARROW *shape* gate on clipboard content — never a semantic one (the server
    stays the only authority on whether a key is real). A false negative is harmless:
    the caller falls straight through to the manual-paste prompt."""
    return bool(_KEY_SHAPE_RE.match(candidate or ""))


def _mask_key(candidate: str) -> str:
    """A recognisable but non-disclosing preview for the consent prompt: enough for the
    owner to recognise their own key, never enough to spill a mis-copied secret into
    the scrollback."""
    if len(candidate) <= 8:
        return "*" * len(candidate)
    return f"{candidate[:4]}{'*' * 6}{candidate[-4:]}"


def _clipboard_consented(candidate: str, confirm_input: Callable) -> bool:
    """Ask BEFORE the clipboard leaves the machine. Defaults to NO: a non-interactive
    host (EOF / no usable stdin) or any answer other than an explicit yes declines, and
    the caller falls back to the manual paste."""
    try:
        answer = (confirm_input(
            f"Use the Pro key in your clipboard ({_mask_key(candidate)})? [y/N]: ") or "")
    except (EOFError, KeyboardInterrupt, OSError):
        return False
    return answer.strip().lower() in ("y", "yes")


def _hidden_paste_input(prompt: str) -> str:
    """Read the pasted key WITHOUT echoing it. The key IS the private-index password
    (it goes straight into ~/.netrc), so a plain ``input`` would leave a live credential
    in the terminal scrollback of every SSH / headless / manual-paste session. The
    wizard injects its own wrapped-paste-safe hidden reader over this default."""
    import getpass
    try:
        return getpass.getpass(prompt)
    except Exception:
        return ""


def detect_pasted_key(*, transport=None, clipboard_reader=None,
                      paste_input: Callable = _hidden_paste_input,
                      confirm_input: Callable = input) -> Optional[str]:
    """Concierge-paste: OFFER a key-shaped clipboard candidate and, only with the user's
    explicit consent, validate it; else prompt for a hidden manual paste and validate
    that. Returns the validated key or None.

    The clipboard is NEVER transmitted unprompted. A stale clipboard routinely holds an
    unrelated password / API token — itself key-shaped — so a candidate must BOTH match
    the narrow key shape AND be explicitly confirmed before it is POSTed to validate-key.
    Everything else falls through to the manual paste, so declining costs one paste and
    leaks nothing. Validation uses the public tokenless validate-key action (allowed at
    acquisition — it is proof-of-possession, not lease enforcement) and accepts the
    not-yet-activated verdicts too (:func:`_key_installable`), because a just-bought key
    has no activation on it until the wheel this very flow installs puts one there."""
    clip = _read_clipboard(clipboard_reader)
    if clip and _looks_like_license_key(clip) and _clipboard_consented(clip, confirm_input):
        if _key_installable(*validate_key(clip, transport=transport)):
            return clip
    try:
        raw = (paste_input("Paste your Pro key (input hidden): ") or "").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw or not _looks_like_license_key(raw):
        return None
    return raw if _key_installable(*validate_key(raw, transport=transport)) else None


def convert(*, transport=None, clipboard_reader=None,
            paste_input: Callable = _hidden_paste_input, confirm_input: Callable = input,
            backends=None, runner=None, netrc_path=None, is_active=None, sleep=None,
            attempts=20, stream=None) -> TrialOutcome:
    """The convert / re-subscribe path: point at the checkout, concierge-paste the
    emailed paid key (consent-gated clipboard detect, then a HIDDEN manual paste;
    validated tokenlessly), then write ~/.netrc + install + daemon. Never mints a paid
    license itself and never holds a privileged token — open registration +
    validate-key only."""
    out = stream or sys.stdout
    print(f"Subscribe or re-subscribe at {CHECKOUT_URL} — Keygen emails your Pro key.",
          file=out)
    key = detect_pasted_key(transport=transport, clipboard_reader=clipboard_reader,
                            paste_input=paste_input, confirm_input=confirm_input)
    if not key:
        return TrialOutcome(False, "no_key",
                            "No valid key entered — re-run setup once you have your Pro key.")
    return _finish_install(key, is_trial=False, backends=backends, runner=runner,
                           netrc_path=netrc_path, is_active=is_active, sleep=sleep,
                           attempts=attempts)


# ═════════ Local state file (offer gating + pending-credential retry) ══════════
# Reuses the OSS upsell conventions for the first-run offer: BUDDHI_NO_UPSELL
# suppresses; an active Pro backend suppresses (re-run suppression); a durable
# decline sticks; and a frequency cap (the shared upsell env knobs) bounds
# re-offers on later setups. The SAME file also holds a short-lived
# pending-credential slot (see ``_pending_credential`` below) so a registration
# that never reached a minted license can be retried without dead-ending into
# "email already registered". The state lives in this module's own file so
# neither concern leaks into the shared upsell counters' schema.

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


def _write_state(path: Optional[Path], state: dict) -> None:
    """Atomic write. Locked to 0600 because this file may hold the pending-
    credential's throwaway password (see ``_save_pending_credential``), not just
    non-secret offer counters. The temp file is created via ``os.open`` with mode
    0600 from its first byte — unlike ``Path.write_text``, which creates at the
    umask-derived default (often 0644) and would briefly expose the password
    before the post-replace chmod below catches up.

    The write is fail-soft (a read-only / full disk must never crash setup), but it
    never LEAVES the password behind: the temp file gets an unpredictable name via
    ``tempfile.mkstemp`` (opened O_EXCL under the hood, so a pre-created file or
    symlink at a guessed name is refused rather than truncated-through — relevant
    because ``BUDDHI_TRIAL_STATE`` can point at a shared directory) and is unlinked
    in the ``finally`` — without which a failing ``os.replace`` would strand a stray
    ``pro_trial.json.tmp*`` holding ``pending_password`` for backups and later
    readers to pick up, long after the state file itself was cleared."""
    p = path or _state_path()
    tmp: Optional[Path] = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{p.name}.", suffix=".tmp", dir=str(p.parent)
        )
        tmp = Path(tmp_name)
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(state))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        tmp = None                  # replaced: the path is the state file now, not a leftover
        os.chmod(p, 0o600)
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _pending_from_state(state: dict) -> Optional[Tuple[str, str, str]]:
    """``_pending_credential`` against an ALREADY-read state dict, so a caller that
    holds the state (``offer_allowed``) does not re-read the file to ask."""
    email = state.get("pending_email")
    password = state.get("pending_password")
    user_id = state.get("pending_user_id")
    if (isinstance(email, str) and email and isinstance(password, str) and password
            and isinstance(user_id, str) and user_id):
        return email, password, user_id
    return None


def _pending_credential(state_path: Optional[Path]) -> Optional[Tuple[str, str, str]]:
    """The ``(email, password, user_id)`` saved by a registration that succeeded but
    did not reach a minted license. Returns None if there is no pending attempt —
    the caller only reuses it when it matches the email being (re)started."""
    return _pending_from_state(_read_state(state_path))


def _save_pending_credential(email: str, password: str, user_id: str, *,
                             state_path: Optional[Path] = None) -> None:
    state = _read_state(state_path)
    state["pending_email"] = email
    state["pending_password"] = password
    state["pending_user_id"] = user_id
    _write_state(state_path, state)


def _clear_pending_credential(*, state_path: Optional[Path] = None) -> None:
    state = _read_state(state_path)
    state.pop("pending_email", None)
    state.pop("pending_password", None)
    state.pop("pending_user_id", None)
    _write_state(state_path, state)


def offer_allowed(*, backends=None, now=None, state_path=None) -> bool:
    """True iff the wizard may show the first-run trial offer: not suppressed
    (BUDDHI_NO_UPSELL), not already on Pro, not durably declined, and within the
    shared upsell frequency cap. Reuses upsell's suppression + cap knobs.

    ONE carve-out: the min-interval leg of the cap is waived while a pending
    credential is on disk. The offer is the ONLY door to ``start_trial`` (the wizard
    shows it, then calls), and the shown-stamp is written when the offer is shown —
    i.e. just before the attempt that failed and left the pending credential. So
    applying the interval to that state would hide the offer for the full window
    (24 h by default) and make the retry slot unreachable for exactly as long as it
    is worth anything, stranding a half-opened account behind an "email already
    registered" wall. Suppression, an active Pro backend, a durable decline and the
    max-shows ceiling all still apply — the retry is bounded, not unlimited."""
    from buddhi_review import upsell
    import time
    if upsell.upsell_suppressed():
        return False
    if pro_backend_active(backends=backends):
        return False
    state = _read_state(state_path)
    if state.get("declined") is True:
        return False
    now = now if now is not None else time.time()
    shown = state.get("shown_count", 0)
    shown = shown if isinstance(shown, int) else 0
    if shown >= upsell._max_shows():
        return False
    if _pending_from_state(state) is not None:
        return True
    last = state.get("last_shown")
    if isinstance(last, (int, float)) and (now - last) < upsell._min_interval_seconds():
        return False
    return True


def record_offer_shown(*, now=None, state_path=None) -> None:
    import time
    state = _read_state(state_path)
    state["shown_count"] = (state.get("shown_count", 0)
                            if isinstance(state.get("shown_count"), int) else 0) + 1
    state["last_shown"] = now if now is not None else time.time()
    _write_state(state_path, state)


def record_declined(*, state_path=None) -> None:
    """Durably record 'don't offer the trial again' (honours the upsell durable-
    dismiss convention)."""
    state = _read_state(state_path)
    state["declined"] = True
    _write_state(state_path, state)
