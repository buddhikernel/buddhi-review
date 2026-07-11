"""The skill-launch update-availability banner (update_banner).

Proves the whole contract: a muted one-line banner iff a newer buddhi-review release
and/or an OUTDATED installed Claude review workflow is detected; exactly one line;
silent when current; shows at most once per run; a network failure yields no banner
and never blocks or delays the launch (the only network is a single bounded, cached
PyPI read); and the version compare fail-closes on dev / equal / newer-local edges.
"""
import io
import json
import sys
from pathlib import Path

import pytest

from buddhi_review import cli, managed_files, update_banner

# The forbidden-vocabulary gate, imported the same way the OSS-purity suite does.
_PUBLIC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PUBLIC / "tools"))
import publish_gate as g  # noqa: E402


# ── Hermetic env ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """No ambient update-check env leaks into a test; colour off; the once-per-run
    guard reset. Deletes the suite-wide BUDDHI_NO_UPDATE_CHECK pin so THIS file's
    tests exercise the banner, then re-injects a fetcher/cache per test."""
    for var in ("BUDDHI_NO_UPDATE_CHECK", "BUDDHI_UPDATE_STATE",
                "BUDDHI_UPDATE_TTL_HOURS", "BUDDHI_UPDATE_TIMEOUT",
                "BUDDHI_LOOP_NO_COLOR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    update_banner._reset_run_guard()
    yield
    update_banner._reset_run_guard()


def _fresh_fetcher(value):
    """A fetcher returning ``value`` and recording that it was called."""
    calls = []

    def fetch():
        calls.append(True)
        return value

    fetch.calls = calls
    return fetch


def _boom_fetcher():
    """A fetcher that raises — stands in for an offline / unreachable PyPI."""
    calls = []

    def fetch():
        calls.append(True)
        raise OSError("network down")

    fetch.calls = calls
    return fetch


# ── update_available: version compare, fail-closed on the edges ─────────────────────

@pytest.mark.parametrize("current,latest,expected", [
    ("0.2.1", "0.3.0", True),        # a plain newer release
    ("0.2.1", "0.2.2", True),
    ("0.9.0", "0.10.0", True),       # numeric, not string ('0.10' > '0.9')
    ("0.2", "0.2.1", True),          # shorter current, zero-padded
    ("0.2.1", "0.2.1", False),       # equal → no banner
    ("0.2.1", "0.2.0", False),       # older latest
    ("0.3.0", "0.2.1", False),       # newer-local (running ahead of PyPI)
    ("1.0.0", "1.0", False),         # 1.0 == 1.0.0 after padding
    ("v0.2.1", "0.3.0", True),       # tolerant leading v
    ("0.2.1", "0.3.0rc1", False),    # pre-release latest → fail-closed (never push)
    ("0.2.1", "0.3.0.dev1", False),  # dev latest → fail-closed
    ("0.2.1.dev1", "0.3.0", False),  # dev/local CURRENT → never nagged
    ("0.2.1", None, False),          # unknown latest
    (None, "0.3.0", False),          # unknown current
    ("0.2.1", "", False),
    ("0.2.1", "garbage", False),
])
def test_update_available(current, latest, expected):
    assert update_banner.update_available(current, latest) is expected


# ── latest_known: cache-first, single bounded refresh, fail-open ────────────────────

def test_fresh_cache_returns_without_network(tmp_path):
    path = tmp_path / "u.json"
    path.write_text(json.dumps({"checked_at": 1000.0, "latest": "0.9.9"}))
    fetch = _fresh_fetcher("SHOULD-NOT-BE-CALLED")
    # 1h later — inside the default 24h window.
    got = update_banner.latest_known(now=1000.0 + 3600, state_path=path,
                                     fetcher=fetch, ttl_seconds=24 * 3600)
    assert got == "0.9.9"
    assert fetch.calls == []  # cache fresh → no network


def test_stale_cache_refreshes_and_persists(tmp_path):
    path = tmp_path / "u.json"
    path.write_text(json.dumps({"checked_at": 0.0, "latest": "0.1.0"}))
    fetch = _fresh_fetcher("0.5.0")
    got = update_banner.latest_known(now=1_000_000.0, state_path=path,
                                     fetcher=fetch, ttl_seconds=3600)
    assert got == "0.5.0" and fetch.calls == [True]
    saved = json.loads(path.read_text())
    assert saved["latest"] == "0.5.0" and saved["checked_at"] == 1_000_000.0


def test_empty_cache_triggers_one_fetch(tmp_path):
    path = tmp_path / "u.json"  # does not exist
    fetch = _fresh_fetcher("0.4.0")
    got = update_banner.latest_known(now=10.0, state_path=path, fetcher=fetch,
                                     ttl_seconds=3600)
    assert got == "0.4.0" and fetch.calls == [True]
    assert json.loads(path.read_text())["checked_at"] == 10.0


def test_fetch_failure_keeps_prior_value_and_stamps_check_time(tmp_path):
    path = tmp_path / "u.json"
    path.write_text(json.dumps({"checked_at": 0.0, "latest": "0.3.0"}))
    fetch = _boom_fetcher()
    got = update_banner.latest_known(now=99.0, state_path=path, fetcher=fetch,
                                     ttl_seconds=1)
    # Prior known value survives a failed refresh; checked_at is stamped so a later
    # call inside the TTL does NOT re-hit the network.
    assert got == "0.3.0" and fetch.calls == [True]
    saved = json.loads(path.read_text())
    assert saved["checked_at"] == 99.0 and saved["latest"] == "0.3.0"
    fetch2 = _boom_fetcher()
    again = update_banner.latest_known(now=99.5, state_path=path, fetcher=fetch2,
                                       ttl_seconds=1)
    assert again == "0.3.0" and fetch2.calls == []  # fresh now — no second network hit


def test_fetch_failure_with_no_prior_value_returns_none(tmp_path):
    path = tmp_path / "u.json"
    fetch = _boom_fetcher()
    got = update_banner.latest_known(now=5.0, state_path=path, fetcher=fetch,
                                     ttl_seconds=3600)
    assert got is None
    assert json.loads(path.read_text())["checked_at"] == 5.0  # stamped → bounded retries


def test_future_stamp_is_treated_as_fresh(tmp_path):
    path = tmp_path / "u.json"
    path.write_text(json.dumps({"checked_at": 10 ** 12, "latest": "7.0.0"}))
    fetch = _fresh_fetcher("SHOULD-NOT-BE-CALLED")
    got = update_banner.latest_known(now=1_700_000_000.0, state_path=path,
                                     fetcher=fetch, ttl_seconds=3600)
    assert got == "7.0.0" and fetch.calls == []


# ── _fetch_latest_from_pypi: never raises, parses info.version ──────────────────────

def test_pypi_fetch_parses_info_version(monkeypatch):
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, *a): return json.dumps({"info": {"version": "1.2.3"}}).encode()
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    assert update_banner._fetch_latest_from_pypi(1.5) == "1.2.3"


def test_pypi_fetch_failopen_on_network_error(monkeypatch):
    def _boom(*a, **k):
        raise OSError("unreachable")
    monkeypatch.setattr("urllib.request.urlopen", _boom)
    assert update_banner._fetch_latest_from_pypi(1.5) is None  # never raises


def test_pypi_fetch_failopen_on_malformed_json(monkeypatch):
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, *a): return b"}{ not json"
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    assert update_banner._fetch_latest_from_pypi(1.5) is None


def test_pypi_fetch_caps_response_size(monkeypatch):
    # The body read is size-capped (memory backstop). read(n) must be honoured.
    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=None):
            captured["n"] = n
            return json.dumps({"info": {"version": "1.0.0"}}).encode()
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    assert update_banner._fetch_latest_from_pypi(1.5) == "1.0.0"
    assert captured["n"] == update_banner._MAX_RESPONSE_BYTES


# ── _bounded_fetch: a HARD wall-clock cap over the raw fetch ─────────────────────────

def test_bounded_fetch_returns_fast_result(monkeypatch):
    monkeypatch.setattr(update_banner, "_fetch_latest_from_pypi", lambda t: "1.2.3")
    assert update_banner._bounded_fetch(1.5) == "1.2.3"


def test_bounded_fetch_failopen_on_raise(monkeypatch):
    def _boom(t):
        raise OSError("dns/read stall surrogate")
    monkeypatch.setattr(update_banner, "_fetch_latest_from_pypi", _boom)
    assert update_banner._bounded_fetch(1.5) is None


def test_bounded_fetch_caps_walltime_on_a_stall(monkeypatch):
    # A raw fetch that stalls far past the cap (simulating a hung DNS resolver / a
    # trickling body — neither of which urllib's socket timeout bounds) must NOT delay
    # the caller beyond the wall-clock cap. This is the launch-never-blocks guarantee.
    import time as _t

    def _stall(t):
        _t.sleep(5)  # would block the launch for 5s without the wall-clock cap
        return "9.9.9"

    monkeypatch.setattr(update_banner, "_fetch_latest_from_pypi", _stall)
    start = _t.monotonic()
    got = update_banner._bounded_fetch(0.3)
    elapsed = _t.monotonic() - start
    assert got is None            # gave up at the cap — fail-open, no banner
    assert elapsed < 2.0          # capped near 0.3s, nowhere near the 5s stall


# ── workflow_out_of_date: scoped to claude-code-review.yml, absent-safe ─────────────

def _shipped_claude_version():
    spec = update_banner._claude_review_spec()
    return managed_files.shipped_version(spec["template"])


def _write_workflow(repo: Path, name: str, marker):
    dst = repo / ".github" / "workflows" / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    body = "name: x\n" if marker is None else f"# buddhi-managed-version: {marker}\nname: x\n"
    dst.write_text(body, encoding="utf-8")


def test_workflow_absent_is_not_stale(tmp_path):
    assert update_banner.workflow_out_of_date(str(tmp_path)) is False


def test_workflow_none_cwd_is_not_stale():
    assert update_banner.workflow_out_of_date(None) is False


def test_workflow_legacy_unversioned_is_stale(tmp_path):
    _write_workflow(tmp_path, "claude-code-review.yml", None)  # markerless legacy
    assert update_banner.workflow_out_of_date(str(tmp_path)) is True


def test_workflow_lower_version_is_stale(tmp_path):
    _write_workflow(tmp_path, "claude-code-review.yml", _shipped_claude_version() - 1)
    assert update_banner.workflow_out_of_date(str(tmp_path)) is True


def test_workflow_current_version_is_not_stale(tmp_path):
    _write_workflow(tmp_path, "claude-code-review.yml", _shipped_claude_version())
    assert update_banner.workflow_out_of_date(str(tmp_path)) is False


def test_workflow_notice_ignores_other_managed_files(tmp_path):
    # An OUTDATED tests-ready-for-ci.yml must NOT trigger the notice — it is scoped
    # to claude-code-review.yml only. The Claude workflow here is current.
    _write_workflow(tmp_path, "claude-code-review.yml", _shipped_claude_version())
    _write_workflow(tmp_path, "tests-ready-for-ci.yml", None)  # stale, but irrelevant
    assert update_banner.workflow_out_of_date(str(tmp_path)) is False


# ── format_banner: pure, one line, names what + how ─────────────────────────────────

def test_format_banner_buddhi_only():
    line = update_banner.format_banner(buddhi_latest="0.3.0", current="0.2.1")
    assert line.startswith("↑ Update available — ")
    assert "buddhi-review 0.3.0" in line and "you have 0.2.1" in line
    assert "pip install -U buddhi-review" in line
    assert "\n" not in line


def test_format_banner_workflow_only():
    line = update_banner.format_banner(workflow_stale=True, workflow_label="Claude review workflow")
    assert "Claude review workflow is out of date" in line
    assert "re-run /review-pr setup" in line
    assert "pip install -U" not in line and "\n" not in line


def test_format_banner_both_is_one_line():
    line = update_banner.format_banner(buddhi_latest="0.3.0", current="0.2.1",
                                       workflow_stale=True)
    assert "pip install -U buddhi-review" in line
    assert "re-run /review-pr setup" in line
    assert "\n" not in line  # BOTH updates on exactly one line


def test_format_banner_none_when_nothing_updatable():
    assert update_banner.format_banner() is None
    assert update_banner.format_banner(buddhi_latest=None, workflow_stale=False) is None


# ── maybe_emit_update_banner: the orchestrator ──────────────────────────────────────

def _emit(tmp_path, **kw):
    """Run the orchestrator into a captured buffer with a tmp cache + fixed clock,
    returning ``(text_or_None, output)``. Defaults: no workflow (cwd None), pinned
    'now', a tmp state file."""
    buf = io.StringIO()
    kw.setdefault("stream", buf)
    kw.setdefault("now", 1_000_000.0)
    kw.setdefault("state_path", tmp_path / "u.json")
    kw.setdefault("current_version", "0.2.1")
    kw.setdefault("ttl_seconds", 24 * 3600)
    text = update_banner.maybe_emit_update_banner(**kw)
    return text, buf.getvalue()


def test_emits_on_buddhi_update(tmp_path):
    text, out = _emit(tmp_path, fetcher=_fresh_fetcher("0.3.0"))
    assert text is not None
    assert "buddhi-review 0.3.0" in out and "pip install -U buddhi-review" in out
    assert out.count("\n") == 1  # exactly one muted line


def test_emits_on_workflow_stale(tmp_path):
    _write_workflow(tmp_path, "claude-code-review.yml", None)
    text, out = _emit(tmp_path, cwd=str(tmp_path), fetcher=_fresh_fetcher("0.2.1"))
    assert text is not None
    assert "re-run /review-pr setup" in out
    assert "pip install -U" not in out  # buddhi is current here
    assert out.count("\n") == 1


def test_emits_both_on_one_line(tmp_path):
    _write_workflow(tmp_path, "claude-code-review.yml", None)
    text, out = _emit(tmp_path, cwd=str(tmp_path), fetcher=_fresh_fetcher("0.3.0"))
    assert "pip install -U buddhi-review" in out and "re-run /review-pr setup" in out
    assert out.count("\n") == 1  # both sources → still ONE line


def test_silent_when_everything_current(tmp_path):
    # buddhi at the running version, no workflow file → nothing to say.
    text, out = _emit(tmp_path, cwd=str(tmp_path), fetcher=_fresh_fetcher("0.2.1"))
    assert text is None and out == ""


def test_disabled_env_silences_and_skips_network(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDHI_NO_UPDATE_CHECK", "1")
    _write_workflow(tmp_path, "claude-code-review.yml", None)  # would otherwise fire
    fetch = _fresh_fetcher("0.3.0")
    text, out = _emit(tmp_path, cwd=str(tmp_path), fetcher=fetch)
    assert text is None and out == ""
    assert fetch.calls == []  # disabled → no network at all


def test_network_failure_still_shows_workflow_and_never_blocks(tmp_path):
    # PyPI unreachable (fetcher raises) → no buddhi part, but the workflow notice
    # still fires and the call returns cleanly (launch never blocked).
    _write_workflow(tmp_path, "claude-code-review.yml", None)
    text, out = _emit(tmp_path, cwd=str(tmp_path), fetcher=_boom_fetcher())
    assert text is not None
    assert "re-run /review-pr setup" in out and "pip install -U" not in out


def test_network_failure_with_no_workflow_is_silent(tmp_path):
    text, out = _emit(tmp_path, fetcher=_boom_fetcher())
    assert text is None and out == ""  # offline + current → quiet, no crash


def test_never_repeats_within_a_run(tmp_path):
    first, out1 = _emit(tmp_path, fetcher=_fresh_fetcher("0.3.0"))
    assert first is not None and out1.count("\n") == 1
    # A second call in the SAME process stays silent (once-per-run guard).
    buf2 = io.StringIO()
    second = update_banner.maybe_emit_update_banner(
        stream=buf2, now=1_000_100.0, state_path=tmp_path / "u2.json",
        current_version="0.2.1", fetcher=_fresh_fetcher("0.3.0"))
    assert second is None and buf2.getvalue() == ""


def test_once_false_allows_repeat(tmp_path):
    a, _ = _emit(tmp_path, fetcher=_fresh_fetcher("0.3.0"), once=False)
    b, out = _emit(tmp_path, fetcher=_fresh_fetcher("0.3.0"), once=False)
    assert a is not None and b is not None  # explicit opt-out of the guard


def test_check_pypi_false_skips_network(tmp_path):
    _write_workflow(tmp_path, "claude-code-review.yml", None)
    fetch = _fresh_fetcher("0.3.0")
    text, out = _emit(tmp_path, cwd=str(tmp_path), check_pypi=False, fetcher=fetch)
    assert text is not None and "re-run /review-pr setup" in out
    assert "pip install -U" not in out and fetch.calls == []  # network skipped


def test_orchestrator_is_fail_open(tmp_path, monkeypatch):
    # An unexpected error anywhere inside → no banner, no raise (launch safe).
    monkeypatch.setattr(update_banner, "latest_known",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    text, out = _emit(tmp_path, fetcher=_fresh_fetcher("0.3.0"))
    assert text is None and out == ""


def test_colour_emitted_on_a_tty(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)

    class _TTY(io.StringIO):
        def isatty(self):
            return True

    buf = _TTY()
    update_banner.maybe_emit_update_banner(
        stream=buf, now=1.0, state_path=tmp_path / "u.json",
        current_version="0.2.1", fetcher=_fresh_fetcher("0.3.0"))
    out = buf.getvalue()
    assert "\033[2m" in out and "buddhi-review 0.3.0" in out  # dim, muted


def test_default_state_path_is_local_cache(monkeypatch):
    monkeypatch.delenv("BUDDHI_UPDATE_STATE", raising=False)
    p = update_banner._state_path()
    assert p.name == "update-check.json" and ".cache" in p.parts


# ── OSS purity: the module + its banner copy ship no forbidden vocabulary ────────────

def test_update_banner_module_source_is_publish_clean():
    src = (_PUBLIC / "buddhi_review" / "update_banner.py").read_text(encoding="utf-8")
    assert g.scan_paid_and_publish(src) == []
    assert g.scan_entitlement(src) == []


def test_banner_copy_contains_no_forbidden_term():
    for line in (
        update_banner.format_banner(buddhi_latest="0.3.0", current="0.2.1"),
        update_banner.format_banner(workflow_stale=True),
        update_banner.format_banner(buddhi_latest="0.3.0", current="0.2.1", workflow_stale=True),
    ):
        assert g.scan_paid_and_publish(line) == [], line
        assert g.scan_entitlement(line) == [], line


# ── cli wiring: emitted at BOTH launch surfaces, on stderr, fail-open ────────────────

def test_review_pr_front_door_emits_banner(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli.update_banner, "maybe_emit_update_banner",
                        lambda **kw: calls.update(kw))
    monkeypatch.setattr(cli, "launch_review_loop", lambda *a, **k: 0)
    args = cli.build_parser().parse_args(["review-pr", "7", "--repo", "o/r", "--cwd", "/work"])
    rc = cli._review_pr(args)
    assert rc == 0
    assert calls.get("cwd") == "/work" and calls.get("stream") is sys.stderr


def test_open_pr_front_door_emits_banner_on_stderr(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli.update_banner, "maybe_emit_update_banner",
                        lambda **kw: calls.update(kw))
    monkeypatch.setattr("buddhi_review.open_pr.actuate", lambda **kw: 0)
    args = cli.build_parser().parse_args(
        ["open-pr", "--repo", "o/r", "--cwd", "/work", "--title", "t"])
    rc = cli._open_pr(args)
    assert rc == 0
    # The banner goes to stderr so the actuator's stdout URL contract stays untouched.
    assert calls.get("cwd") == "/work" and calls.get("stream") is sys.stderr


def test_review_pr_end_to_end_banner_to_stderr(monkeypatch, tmp_path, capsys):
    # A pre-stamped FRESH cache (far-future checked_at) carrying a much newer version
    # → the real orchestrator emits to stderr, no network, launch stubbed.
    state = tmp_path / "u.json"
    state.write_text(json.dumps({"checked_at": 10 ** 12, "latest": "99.0.0"}))
    monkeypatch.setenv("BUDDHI_UPDATE_STATE", str(state))
    monkeypatch.setattr(cli, "launch_review_loop", lambda *a, **k: 0)
    args = cli.build_parser().parse_args(["review-pr", "7", "--repo", "o/r", "--cwd", str(tmp_path)])
    rc = cli._review_pr(args)
    assert rc == 0
    captured = capsys.readouterr()
    assert "99.0.0" in captured.err and "pip install -U buddhi-review" in captured.err
    assert "99.0.0" not in captured.out  # never on stdout
