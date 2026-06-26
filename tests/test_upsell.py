"""The in-run contextual upgrade nudge (PRO-7 / execution-plan §D4 + §E.9).

Proves the whole gate: shown ONLY when the free skill runs without an active paid
backend, contextual to what the run did, frequency-capped, dismissible, suppressible
by ``BUDDHI_NO_UPSELL``, never naming a paid mechanism, and never touching the
network (the only side effect is a local state file pointed at ``tmp_path``).
"""
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from buddhi_review import backends, upsell

# The forbidden-vocabulary gate, imported the same way the OSS-purity suite does.
_PUBLIC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PUBLIC / "tools"))
import publish_gate as g  # noqa: E402


# ── Test doubles + hermetic env ───────────────────────────────────────────────────

class _Fake:
    """A registered backend test double (mirrors test_backends_seam.FakeBackend)."""

    def __init__(self, *, active=True, name="fake"):
        self._active = active
        self.name = name
        self.priority = 0

    def is_active(self):
        return self._active

    def run_review_loop(self, pr, repo, cwd, **opts):  # pragma: no cover
        return 0


class _Broken:
    name = "broken"
    priority = 0

    def is_active(self):
        raise RuntimeError("boom")

    def run_review_loop(self, pr, repo, cwd, **opts):  # pragma: no cover
        raise RuntimeError("boom")


_FREE_ONLY = lambda: [backends.FreeBackend()]  # noqa: E731


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """No ambient upsell env leaks into a test; colour off by default."""
    for var in ("BUDDHI_NO_UPSELL", "BUDDHI_UPSELL_DISMISS", "BUDDHI_UPSELL_STATE",
                "BUDDHI_UPSELL_MIN_INTERVAL_HOURS", "BUDDHI_UPSELL_MAX_SHOWS",
                "BUDDHI_LOOP_NO_COLOR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    yield


def _emit(status, tmp_path, **kw):
    """Run the orchestrator into a captured buffer with a tmp state file + free-only
    backends + a fixed clock, returning ``(text_or_None, output, state_dict)``."""
    buf = io.StringIO()
    path = kw.pop("state_path", tmp_path / "upsell.json")
    text = upsell.maybe_emit_run_end_nudge(
        status,
        stream=buf,
        backends=kw.pop("backends", _FREE_ONLY()),
        now=kw.pop("now", 1_000_000.0),
        state_path=path,
        **kw,
    )
    state = json.loads(path.read_text()) if path.exists() else {}
    return text, buf.getvalue(), state


# ── format_nudge: the contextual benefit copy ─────────────────────────────────────

def test_format_nudge_is_contextual_per_status():
    needs = upsell.format_nudge("needs-human")
    rounds = upsell.format_nudge("max-rounds")
    assert needs and rounds and needs != rounds
    # Each names a concrete benefit + the Cmd-clickable domain + the silence hint.
    for line in (needs, rounds):
        assert line.startswith("↑ Upgrade to ")
        assert "https://buddhikernel.com" in line
        assert "BUDDHI_NO_UPSELL=1" in line


def test_format_nudge_skips_non_handback_statuses():
    # A clean merge needed no help; an operator-chosen stop is a deliberate halt.
    assert upsell.format_nudge("clean") is None
    assert upsell.format_nudge("stopped") is None
    assert upsell.format_nudge("anything-else") is None


# ── OSS purity: the one permitted paid reference names no mechanism ───────────────

def test_nudge_copy_contains_no_forbidden_term():
    for status in ("needs-human", "max-rounds"):
        line = upsell.format_nudge(status)
        assert g.scan_paid_and_publish(line) == [], line
        assert g.scan_entitlement(line) == [], line


def test_upsell_module_source_is_publish_clean():
    src = (_PUBLIC / "buddhi_review" / "upsell.py").read_text(encoding="utf-8")
    assert g.scan_paid_and_publish(src) == []
    assert g.scan_entitlement(src) == []


# ── Eligibility: only without an active paid backend ──────────────────────────────

def test_shown_when_no_active_paid_backend(tmp_path):
    text, out, state = _emit("needs-human", tmp_path)
    assert text is not None
    assert "https://buddhikernel.com" in out
    assert state["shown_count"] == 1 and state["last_shown"] == 1_000_000.0


def test_suppressed_when_a_paid_backend_is_active(tmp_path):
    backs = [backends.FreeBackend(), _Fake(active=True, name="paid")]
    text, out, _ = _emit("needs-human", tmp_path, backends=backs)
    assert text is None and out == ""


def test_an_inactive_paid_backend_does_not_suppress(tmp_path):
    backs = [backends.FreeBackend(), _Fake(active=False, name="paid")]
    text, _, _ = _emit("needs-human", tmp_path, backends=backs)
    assert text is not None


def test_paid_backend_active_helper():
    assert upsell.paid_backend_active([backends.FreeBackend()]) is False
    assert upsell.paid_backend_active([backends.FreeBackend(), _Fake(name="x")]) is True
    # A backend that errors answering is inactive — never breaks the free skill.
    assert upsell.paid_backend_active([backends.FreeBackend(), _Broken()]) is False


def test_paid_backend_active_uses_default_discovery(monkeypatch):
    """With no list injected, eligibility runs the real FREE-1 discovery seam."""
    monkeypatch.setattr(backends, "discover_backends", lambda: [backends.FreeBackend()])
    assert upsell.paid_backend_active() is False
    monkeypatch.setattr(backends, "discover_backends",
                        lambda: [backends.FreeBackend(), _Fake(name="paid")])
    assert upsell.paid_backend_active() is True


# ── Suppression (BUDDHI_NO_UPSELL) ────────────────────────────────────────────────

def test_suppressed_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDHI_NO_UPSELL", "1")
    text, out, state = _emit("needs-human", tmp_path)
    assert text is None and out == "" and state == {}


def test_upsell_suppressed_helper(monkeypatch):
    monkeypatch.delenv("BUDDHI_NO_UPSELL", raising=False)
    assert upsell.upsell_suppressed() is False
    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv("BUDDHI_NO_UPSELL", truthy)
        assert upsell.upsell_suppressed() is True


def test_wizard_delegates_to_the_one_suppress_helper(monkeypatch):
    """The wizard teasers and the in-run nudge share a single suppression switch."""
    from buddhi_review import wizard
    monkeypatch.setenv("BUDDHI_NO_UPSELL", "1")
    assert wizard._upsell_suppressed() is True
    monkeypatch.setenv("BUDDHI_NO_UPSELL", "0")
    assert wizard._upsell_suppressed() is False


# ── Frequency cap ─────────────────────────────────────────────────────────────────

def test_frequency_capped_by_min_interval(tmp_path):
    path = tmp_path / "s.json"
    # First show records the timestamp.
    t1, _, _ = _emit("needs-human", tmp_path, state_path=path, now=1_000_000.0)
    assert t1 is not None
    # 1h later — inside the 24h default window — stays silent.
    t2, out2, _ = _emit("needs-human", tmp_path, state_path=path, now=1_000_000.0 + 3600)
    assert t2 is None and out2 == ""
    # >24h later — shows again.
    t3, _, state = _emit("needs-human", tmp_path, state_path=path, now=1_000_000.0 + 90_000)
    assert t3 is not None and state["shown_count"] == 2


def test_frequency_capped_by_lifetime_max(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDHI_UPSELL_MIN_INTERVAL_HOURS", "0")  # interval never blocks
    monkeypatch.setenv("BUDDHI_UPSELL_MAX_SHOWS", "2")
    path = tmp_path / "s.json"
    assert _emit("needs-human", tmp_path, state_path=path, now=1.0)[0] is not None
    assert _emit("needs-human", tmp_path, state_path=path, now=2.0)[0] is not None
    # Third call is over the lifetime cap.
    text, out, state = _emit("needs-human", tmp_path, state_path=path, now=3.0)
    assert text is None and out == "" and state["shown_count"] == 2


def test_interval_overridable(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDHI_UPSELL_MIN_INTERVAL_HOURS", "1")
    path = tmp_path / "s.json"
    assert _emit("needs-human", tmp_path, state_path=path, now=0.0)[0] is not None
    # 90min later (> the 1h override) shows again.
    assert _emit("needs-human", tmp_path, state_path=path, now=5400.0)[0] is not None


# ── Dismissal (durable, survives across runs) ─────────────────────────────────────

def test_dismiss_env_records_durable_flag_and_stays_off(tmp_path, monkeypatch):
    path = tmp_path / "s.json"
    monkeypatch.setenv("BUDDHI_UPSELL_DISMISS", "1")
    text, out, state = _emit("needs-human", tmp_path, state_path=path)
    assert text is None and out == "" and state["dismissed"] is True
    # A later run WITHOUT the env still stays silent — the flag persisted.
    monkeypatch.delenv("BUDDHI_UPSELL_DISMISS", raising=False)
    text2, out2, _ = _emit("needs-human", tmp_path, state_path=path, now=2_000_000.0)
    assert text2 is None and out2 == ""


def test_dismissed_state_suppresses(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"dismissed": True}))
    text, out, _ = _emit("needs-human", tmp_path, state_path=path)
    assert text is None and out == ""


# ── State file robustness (never crashes, never phones home) ──────────────────────

def test_corrupt_state_file_is_tolerated(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("}{ not json")
    text, _, state = _emit("needs-human", tmp_path, state_path=path)
    assert text is not None and state["shown_count"] == 1


def test_unwritable_state_dir_does_not_block_the_nudge(tmp_path, monkeypatch):
    # A write failure must never swallow the nudge or raise.
    monkeypatch.setattr(upsell, "_write_state", lambda *a, **k: None)
    text, out, _ = _emit("needs-human", tmp_path)
    assert text is not None and "buddhikernel.com" in out


def test_default_state_path_is_local_cache(monkeypatch):
    monkeypatch.delenv("BUDDHI_UPSELL_STATE", raising=False)
    p = upsell._state_path()
    assert p.name == "upsell.json" and ".cache" in p.parts


# ── Rendering: transient, dim, Cmd-clickable ──────────────────────────────────────

def test_colour_emitted_on_a_tty(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)

    class _TTY(io.StringIO):
        def isatty(self):
            return True

    buf = _TTY()
    upsell.maybe_emit_run_end_nudge(
        "needs-human", stream=buf, backends=_FREE_ONLY(), now=1.0,
        state_path=tmp_path / "s.json",
    )
    out = buf.getvalue()
    assert "\033[2m" in out and "buddhikernel.com" in out  # dim, transient


# ── End-to-end wiring through cli._run_loop ───────────────────────────────────────

def _drive_run_loop(monkeypatch, status, tmp_path):
    """Drive the REAL ``cli._run_loop`` with a stubbed driver returning ``status``,
    capturing stdout. Hermetic: tmp state file, free-only eligibility, no colour.
    Mirrors the gate-wiring stubs in test_launch_gates_round1_summon."""
    from buddhi_review import cli

    monkeypatch.setenv("BUDDHI_UPSELL_STATE", str(tmp_path / "cli-upsell.json"))
    monkeypatch.setattr(cli.round_driver, "refuse_primary_checkout", lambda *a, **k: None)
    monkeypatch.setattr(cli.round_driver, "enforce_repo_confirmation_gate", lambda *a, **k: None)
    # Isolate the wiring assertion from the host's installed backends.
    monkeypatch.setattr(cli.upsell, "paid_backend_active", lambda *a, **k: False)

    class _Outcome:
        def __init__(self):
            self.status = status
            self.rounds = 1
            self.merged = (status == "clean")

    class _Driver:
        def __init__(self, *a, **k):
            pass

        def run(self):
            return _Outcome()

    monkeypatch.setattr(cli.round_driver, "RoundDriver", _Driver)
    args = cli.build_parser().parse_args(["run-loop", "7", "--repo", "o/r", "--cwd", "/x"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli._run_loop(args)
    return rc, buf.getvalue()


def test_cli_run_loop_emits_nudge_on_handback(monkeypatch, tmp_path):
    rc, out = _drive_run_loop(monkeypatch, "needs-human", tmp_path)
    assert rc == 1  # a non-clean exit
    assert "↑ Upgrade to" in out and "https://buddhikernel.com" in out


def test_cli_run_loop_no_nudge_on_clean_merge(monkeypatch, tmp_path):
    rc, out = _drive_run_loop(monkeypatch, "clean", tmp_path)
    assert rc == 0
    assert "buddhikernel.com" not in out  # a clean merge needed no help


def test_cli_run_loop_nudge_respects_suppression(monkeypatch, tmp_path):
    monkeypatch.setenv("BUDDHI_NO_UPSELL", "1")
    rc, out = _drive_run_loop(monkeypatch, "needs-human", tmp_path)
    assert rc == 1 and "buddhikernel.com" not in out
