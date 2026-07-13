"""F1 — the unclaimed-command fallback seam + the upgrade notice (OSS front door).

A command word that is not one of the skill's own free subcommands is intercepted
in ``cli.main`` BEFORE argparse (which would ``SystemExit(2)`` on it). It is routed
to a separately-installed backend that CLAIMS it via the optional ``claimed_commands``
hook — or, with no active claimant (the normal free-only state, and equally a plain
typo), answered with the approved one-shot upgrade notice and exit 2. This suite pins:

  * a known free command is unaffected (never reaches the fallback);
  * an unknown command with no backend / an inactive backend → notice + exit 2;
  * an unknown command with an active claimant → the trailing argv is forwarded
    verbatim and unparsed to ``run_command(name, argv)``;
  * the notice echoes the runtime command string and passes the publish gate;
  * the notice is exempt from ``BUDDHI_NO_UPSELL``.
"""
import io
import sys
from pathlib import Path

import pytest

from buddhi_review import backends, cli

# The forbidden-vocabulary gate, imported exactly as the OSS-purity suite does.
_PUBLIC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PUBLIC / "tools"))
import publish_gate as g  # noqa: E402


# ── Test doubles ──────────────────────────────────────────────────────────────────

class ClaimingBackend:
    """A backend that claims one or more non-free commands (the pro-wheel shape)."""

    def __init__(self, *, commands=("review-batch",), active=True, priority=0,
                 name="claimer", rec=None):
        self._commands = tuple(commands)
        self._active = active
        self.priority = priority
        self.name = name
        self.rec = rec if rec is not None else []

    def is_active(self):
        return self._active

    def claimed_commands(self):
        return self._commands

    def run_command(self, name, argv):
        self.rec.append((name, list(argv)))
        return 0

    def run_review_loop(self, pr, repo, cwd, **opts):  # completeness (shared commands)
        return 0


class ClaimsButCannotRun:
    """Claims a command but exposes NO ``run_command`` — must NOT be selected."""

    name = "half"
    priority = 100

    def is_active(self):
        return True

    def claimed_commands(self):
        return ("review-batch",)

    def run_review_loop(self, pr, repo, cwd, **opts):  # pragma: no cover
        return 0


class BoomOnActive:
    name = "boom-active"
    priority = 100

    def is_active(self):
        raise RuntimeError("boom")

    def claimed_commands(self):
        return ("review-batch",)

    def run_command(self, name, argv):  # pragma: no cover
        return 0

    def run_review_loop(self, pr, repo, cwd, **opts):  # pragma: no cover
        return 0


class BoomOnClaim:
    name = "boom-claim"
    priority = 100

    def is_active(self):
        return True

    def claimed_commands(self):
        raise RuntimeError("boom")

    def run_command(self, name, argv):  # pragma: no cover
        return 0

    def run_review_loop(self, pr, repo, cwd, **opts):  # pragma: no cover
        return 0


_FREE = backends.FreeBackend


# ── _split_command: the pre-parse positional split ─────────────────────────────────

@pytest.mark.parametrize("argv, expected", [
    ([], (None, [])),
    (["--version"], (None, [])),
    (["-h"], (None, [])),
    (["--help"], (None, [])),
    (["--help", "review-batch"], (None, [])),        # -h/--help/--version win
    (["review-batch"], ("review-batch", [])),
    (["review-batch", "--repo", "x"], ("review-batch", ["--repo", "x"])),
    (["review-pr", "7", "--rr"], ("review-pr", ["7", "--rr"])),  # known cmds split too
    (["bogus", "--a", "b", "c"], ("bogus", ["--a", "b", "c"])),
])
def test_split_command(argv, expected):
    assert cli._split_command(argv) == expected


def test_split_command_returns_a_fresh_trailing_list():
    argv = ["review-batch", "--repo", "x"]
    _, trailing = cli._split_command(argv)
    trailing.append("mutated")
    assert argv == ["review-batch", "--repo", "x"]  # caller's argv is not aliased


# ── _known_commands: the single source of truth ────────────────────────────────────

def test_known_commands_reads_off_the_parser():
    known = cli._known_commands(cli.build_parser())
    assert {"self-check", "review-pr", "run-loop", "open-pr", "setup", "status"} <= known
    assert "review-batch" not in known  # a pro-only command is NOT a free subcommand


# ── select_command_backend: claim + active + runnable ──────────────────────────────

def test_select_returns_active_claimant():
    claimer = ClaimingBackend(active=True)
    got = backends.select_command_backend("review-batch", backends=[claimer, _FREE()])
    assert got is claimer


def test_select_skips_inactive_claimant():
    assert backends.select_command_backend(
        "review-batch", backends=[ClaimingBackend(active=False), _FREE()]) is None


def test_select_none_when_free_only_claims_nothing():
    # FreeBackend has no claimed_commands hook at all → claims nothing.
    assert backends.select_command_backend("review-batch", backends=[_FREE()]) is None


def test_select_none_when_command_not_claimed():
    claimer = ClaimingBackend(commands=("other-cmd",), active=True)
    assert backends.select_command_backend("review-batch", backends=[claimer]) is None


def test_select_prefers_highest_priority_claimant():
    lo = ClaimingBackend(active=True, priority=1, name="lo")
    hi = ClaimingBackend(active=True, priority=50, name="hi")
    assert backends.select_command_backend("review-batch", backends=[lo, hi]) is hi


def test_select_rejects_claimant_without_run_command():
    # Claims + active but no run_command → treated as not claiming (never selected).
    assert backends.select_command_backend(
        "review-batch", backends=[ClaimsButCannotRun()]) is None


def test_select_survives_a_backend_that_errors_on_is_active():
    claimer = ClaimingBackend(active=True, priority=1, name="ok")
    got = backends.select_command_backend("review-batch", backends=[BoomOnActive(), claimer])
    assert got is claimer  # the erroring one is skipped, the good one still wins


def test_select_survives_a_backend_that_errors_on_claimed_commands():
    claimer = ClaimingBackend(active=True, priority=1, name="ok")
    got = backends.select_command_backend("review-batch", backends=[BoomOnClaim(), claimer])
    assert got is claimer


# ── _dispatch_unclaimed_command: forward vs notice ─────────────────────────────────

def test_active_claimant_gets_the_trailing_argv_verbatim():
    rec = []
    claimer = ClaimingBackend(active=True, rec=rec)
    trailing = ["--repo", "o/r", "--dry-run", "positional"]
    rc = cli._dispatch_unclaimed_command("review-batch", trailing, backends=[claimer, _FREE()])
    assert rc == 0
    assert rec == [("review-batch", ["--repo", "o/r", "--dry-run", "positional"])]


def test_no_claimant_prints_notice_and_exits_2():
    buf = io.StringIO()
    rc = cli._dispatch_unclaimed_command("review-batch", ["--repo", "x"],
                                         backends=[_FREE()], stream=buf)
    assert rc == 2
    out = buf.getvalue()
    assert out.rstrip("\n") == cli._UNCLAIMED_COMMAND_NOTICE.format(command="review-batch")
    assert "review-batch" in out
    assert "https://buddhikernel.com" in out


def test_inactive_backend_falls_through_to_the_notice():
    buf = io.StringIO()
    rc = cli._dispatch_unclaimed_command("review-batch", [],
                                         backends=[ClaimingBackend(active=False)], stream=buf)
    assert rc == 2
    assert "not included in this free installation" in buf.getvalue()


def test_notice_echoes_a_typod_command_verbatim():
    # The free tree ships no paid-command list, so a typo and a lapsed command are
    # indistinguishable; the notice must echo whatever was typed, not a fixed name.
    buf = io.StringIO()
    cli._dispatch_unclaimed_command("reveiw-batch", [], backends=[_FREE()], stream=buf)
    assert "'reveiw-batch'" in buf.getvalue()


def test_notice_is_exempt_from_no_upsell(monkeypatch):
    # Unlike the in-run nudge, the notice is a functional answer — BUDDHI_NO_UPSELL
    # must NOT suppress it (the user would otherwise stare at silent inaction).
    monkeypatch.setenv("BUDDHI_NO_UPSELL", "1")
    buf = io.StringIO()
    rc = cli._dispatch_unclaimed_command("review-batch", [], backends=[_FREE()], stream=buf)
    assert rc == 2
    assert "not included in this free installation" in buf.getvalue()


# ── main(): end-to-end wiring through the front door ───────────────────────────────

def test_main_routes_unknown_command_to_active_claimant(monkeypatch):
    rec = []
    claimer = ClaimingBackend(active=True, priority=100, rec=rec)
    monkeypatch.setattr(backends, "discover_backends", lambda **k: [claimer, _FREE()])
    rc = cli.main(["review-batch", "extra", "--flag", "val"])
    assert rc == 0
    assert rec == [("review-batch", ["extra", "--flag", "val"])]


def test_main_unknown_command_no_backend_prints_notice(monkeypatch, capsys):
    monkeypatch.setattr(backends, "discover_backends", lambda **k: [_FREE()])
    rc = cli.main(["review-batch", "--repo", "x"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.rstrip("\n") == cli._UNCLAIMED_COMMAND_NOTICE.format(command="review-batch")


def test_main_known_command_never_reaches_the_fallback(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "_dispatch_unclaimed_command",
                        lambda *a, **k: called.append((a, k)) or 99)
    monkeypatch.setattr(cli, "_self_check", lambda: 0)
    assert cli.main(["self-check"]) == 0
    assert called == []  # a known subcommand bypasses the seam entirely


def test_main_no_args_prints_help_not_the_fallback(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(cli, "_dispatch_unclaimed_command",
                        lambda *a, **k: called.append(1) or 99)
    assert cli.main([]) == 0
    assert called == []
    assert "usage:" in capsys.readouterr().out.lower()


def test_main_version_defers_to_argparse_not_the_fallback(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "_dispatch_unclaimed_command",
                        lambda *a, **k: called.append(1) or 99)
    with pytest.raises(SystemExit):     # argparse prints the version and exits
        cli.main(["--version"])
    assert called == []


# ── The notice passes the publish gate (vocabulary assertion) ──────────────────────

def test_notice_text_is_publish_clean():
    rendered = cli._UNCLAIMED_COMMAND_NOTICE.format(command="review-batch")
    assert g.scan_paid_and_publish(rendered) == [], rendered
    assert g.scan_entitlement(rendered) == [], rendered


def test_notice_hardcodes_no_command_name():
    # The template carries only the {command} placeholder — the OSS tree ships no
    # paid command name (echoing only the runtime string keeps §E's no-enumeration
    # rule intact).
    assert "{command}" in cli._UNCLAIMED_COMMAND_NOTICE
    assert "review-batch" not in cli._UNCLAIMED_COMMAND_NOTICE


def test_cli_module_source_is_publish_clean():
    src = Path(cli.__file__).read_text(encoding="utf-8")
    assert g.scan_paid_and_publish(src) == []
    assert g.scan_entitlement(src) == []
