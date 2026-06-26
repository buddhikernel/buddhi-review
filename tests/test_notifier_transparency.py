"""Console notifier round-trip, the ⚙ [auto] transparency contract, and config."""
from __future__ import annotations

from buddhi_review import config, tmp_paths
from buddhi_review.notifier import Ask, ConsoleNotifier
from buddhi_review.transparency import automation_notice


def test_console_answer_file_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDHI_REVIEW_TMP", str(tmp_path))
    n = ConsoleNotifier()
    ask = Ask(id="c1", question="Drop the column?", options=["Apply", "Skip"], recommended_index=0)
    n.send(ask)
    assert n.read_answer(ask) is None  # nothing typed yet
    # A bare notifier (no PR/repo) lands at review-answer-local-<ask>.md.
    f = tmp_path / tmp_paths.answer_name_for_ask("c1")
    assert f.name == "review-answer-local-c1.md"
    text = f.read_text()
    # the user types an answer after the '>' line
    f.write_text(text.replace("> ", "> 2"))
    assert n.read_answer(ask) == "2"


def test_answer_file_carries_repo_and_pr_when_known(tmp_path, monkeypatch):
    # A notifier built with pr/repo (the production path, threaded from cli.py)
    # writes review-answer-<repo>-PR<pr>-<ask>.md, so two loops with the same
    # fixed-id ask (e.g. "test-gate") on the same PR number across repos never
    # collide — the same per-(repo,PR) keying the log uses.
    monkeypatch.setenv("BUDDHI_REVIEW_TMP", str(tmp_path))
    n = ConsoleNotifier(pr="9", repo="acme/demo")
    ask = Ask(id="test-gate", question="Red gate?", options=["Push", "Stop"], recommended_index=1)
    n.send(ask)
    f = tmp_path / "review-answer-demo-PR9-test-gate.md"
    assert f.exists()
    f.write_text(f.read_text().replace("> ", "> 1"))
    assert n.read_answer(ask) == "1"
    n.clear(ask)
    assert not f.exists()


def test_console_is_the_only_free_channel():
    assert ConsoleNotifier().name == "console"
    assert config.notifier_channel({"notifications": "telegram"}) == "console"  # any non-console channel falls back to console


def test_automation_notice_format_and_greppable(capsys):
    body = automation_notice("squash-merge", "PR #12 clean", status="do", hint="disable: --no-auto-merge")
    out = capsys.readouterr().out
    assert "[auto]" in out  # greppable tag
    assert body == "⚙ [auto] squash-merge — PR #12 clean   (disable: --no-auto-merge)"


def test_automation_notice_glyph_per_status(capsys):
    glyphs = {
        "do": "⚙",
        "done": "✓",
        "skip": "⊘",
        "fallback": "⚠",
        "stop": "✗",
    }
    for status, glyph in glyphs.items():
        body = automation_notice("act", status=status)
        assert body.startswith(f"{glyph} [auto] act")


def test_no_colour_when_not_a_tty(capsys):
    # capsys replaces stdout with a non-tty buffer → no ANSI escape in the output.
    automation_notice("merge")
    out = capsys.readouterr().out
    assert "\033[" not in out


def test_config_defaults_and_auto_on_open():
    assert config.plan({}) == config.DEFAULT_PLAN
    assert config.active_reviewers({}) == config.DEFAULT_REVIEWERS
    assert config.auto_on_open({}, "claude") is False  # summoned in round 1
    assert config.auto_on_open({}, "copilot") is True  # auto-comments on open
    assert config.auto_on_open({"auto_on_open": {"claude": True}}, "claude") is True
