"""Workflow-detected Claude usage-limit rail (claude-review-unavailable-v1).

claude-code-review.yml (managed-version 2) posts a github-actions[bot] marker
comment when its Claude run died on a usage limit — a state that otherwise reads
as plain reviewer silence and burns the full poll window. The free RoundDriver
acts on ``type=rate_limited`` only (releases claude from the wait + a timed
comeback once the reset passes); ``type=credits_exhausted`` is a paid-tier
concept it logs and ignores.

The most important guarantee is SAFETY: a rate-limited claude is RELEASED from
the wait but posts no review, so it must never satisfy the never-merge-unreviewed
gate — an auto-merge fleet of only claude must refuse to merge when claude was
rate-limited. That holds by construction (the release never touches
``reviewed_ever``), and the flagship test pins it end-to-end.
"""
from datetime import datetime, timezone

from buddhi_review import detectors
from buddhi_review.loop import Comment

from test_round_driver import make_driver, CLAUDE_ONLY

UTC = timezone.utc
RESET = datetime(2026, 7, 4, 13, 0, 0, tzinfo=UTC)
RESET_EPOCH = int(RESET.timestamp())
BEFORE_RESET = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
_MIN = datetime.min.replace(tzinfo=UTC)

RATE_LIMITED_BODY = (
    f"<!-- claude-review-unavailable-v1 type=rate_limited resets_at={RESET_EPOCH} -->\n"
    "⏳ Claude review unavailable — the Claude subscription usage window is "
    "exhausted, so this run posted no review.")
CREDITS_BODY = (
    "<!-- claude-review-unavailable-v1 type=credits_exhausted -->\n"
    "⏳ Claude review unavailable — the API credit balance is exhausted.")


def _marker(text=RATE_LIMITED_BODY, source="github-actions[bot]", cid="m"):
    return Comment(id=cid, text=text, source=source, from_issue_channel=True,
                   created_at="2026-07-04T10:47:00Z")


# ── SAFETY (the flagship): a released rate-limited claude never merges ────────

def test_rate_limited_claude_does_not_satisfy_the_merge_gate():
    """claude-only fleet, --auto-merge on, the round's only comment is a
    rate_limited marker: claude is released (round closes fast) but NEVER
    genuinely reviewed, so the never-merge-unreviewed gate must block the merge.
    Mutation this catches: routing the release through reviewed_ever."""
    driver, clock, gh = make_driver([(0, _marker())], cfg=CLAUDE_ONLY,
                                    auto_merge=True,
                                    wall_clock=lambda: BEFORE_RESET)
    outcome = driver.run()
    assert outcome.merged is False, "must not merge — claude never reviewed"
    assert "claude" not in driver.reviewed_ever
    assert gh.matching("gh", "merge", "--squash") == []
    # ...and claude WAS released (recorded rate-limited), not merely silent.
    assert "claude" in driver._rate_limited_until


# ── Marker handling ──────────────────────────────────────────────────────────

def _fresh_driver(**kw):
    driver, _clock, _gh = make_driver([], cfg=CLAUDE_ONLY, **kw)
    return driver


def test_rate_limited_marker_records_reset_and_releases_and_excludes():
    driver = _fresh_driver(wall_clock=lambda: BEFORE_RESET)
    driver._scan_unavailable_markers([_marker()])
    assert driver._rate_limited_until["claude"] == RESET
    # released from the wait (signal set → _quiesced short-circuits)...
    assert driver._bot_state("claude").signal == detectors.SIGNAL_RATE_LIMITED
    # ...and excluded from re-request while rate-limited.
    assert "claude" not in driver.expected_bots()


def test_unknown_reset_degrades_to_next_round_retry():
    driver = _fresh_driver()
    body = "<!-- claude-review-unavailable-v1 type=rate_limited resets_at=0 -->\n⏳"
    driver._scan_unavailable_markers([_marker(text=body)])
    assert driver._rate_limited_until["claude"] == _MIN


def test_out_of_range_epoch_does_not_crash():
    driver = _fresh_driver()
    body = ("<!-- claude-review-unavailable-v1 type=rate_limited "
            "resets_at=1751900000000 -->\n⏳")  # ms-scale
    driver._scan_unavailable_markers([_marker(text=body)])
    assert driver._rate_limited_until["claude"] == _MIN  # degraded, no crash


def test_credits_marker_is_ignored_no_billing_mode():
    driver = _fresh_driver()
    driver._scan_unavailable_markers([_marker(text=CREDITS_BODY)])
    assert driver._rate_limited_until == {}
    assert driver._bot_state("claude").signal is None


def test_spoofed_author_is_rejected():
    driver = _fresh_driver()
    driver._scan_unavailable_markers([_marker(source="some-user")])
    assert driver._rate_limited_until == {}


# ── Timed comeback ───────────────────────────────────────────────────────────

def test_comeback_pops_only_after_reset():
    before = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    after = datetime(2026, 7, 4, 13, 0, 1, tzinfo=UTC)
    driver = _fresh_driver(wall_clock=lambda: before)
    driver._rate_limited_until["claude"] = RESET
    driver._bot_state("claude").signal = detectors.SIGNAL_RATE_LIMITED
    driver._apply_rate_limit_comeback()
    assert "claude" in driver._rate_limited_until       # not yet
    assert "claude" not in driver.expected_bots()
    driver.wall_clock = lambda: after
    driver._apply_rate_limit_comeback()
    assert driver._rate_limited_until == {}             # popped
    assert driver._bot_state("claude").signal is None   # active again
    assert "claude" in driver.expected_bots()


def test_unknown_reset_comes_back_on_first_boundary():
    driver = _fresh_driver(wall_clock=lambda: datetime(2000, 1, 1, tzinfo=UTC))
    driver._rate_limited_until["claude"] = _MIN
    driver._apply_rate_limit_comeback()
    assert driver._rate_limited_until == {}


# ── Stale marker (resets_at already in the past) ────────────────────────────

def test_stale_marker_is_ignored():
    """A marker whose resets_at epoch is already past must NOT quiesce the
    round — it should be skipped so a real in-flight review isn't cut short."""
    after_reset = datetime(2026, 7, 4, 14, 0, tzinfo=UTC)  # 1 hour after RESET
    driver = _fresh_driver(wall_clock=lambda: after_reset)
    driver._scan_unavailable_markers([_marker()])
    assert driver._rate_limited_until == {}
    assert driver._bot_state("claude").signal is None


def test_comeback_clears_silent_dropped():
    """After a rate-limited round, claude ends up in silent_dropped.
    The comeback must clear it so expected_bots re-admits claude."""
    before = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    after = datetime(2026, 7, 4, 13, 0, 1, tzinfo=UTC)
    driver = _fresh_driver(wall_clock=lambda: before)
    driver._rate_limited_until["claude"] = RESET
    driver._bot_state("claude").signal = detectors.SIGNAL_RATE_LIMITED
    driver.silent_dropped.add("claude")   # populated by _record_round_attendance
    driver.silent_rounds["claude"] = 1
    driver.wall_clock = lambda: after
    driver._apply_rate_limit_comeback()
    assert "claude" not in driver.silent_dropped
    assert driver.silent_rounds.get("claude", 0) == 0
    assert "claude" in driver.expected_bots()


# ── Label ────────────────────────────────────────────────────────────────────

def test_rate_limited_label():
    driver = _fresh_driver(wall_clock=lambda: BEFORE_RESET)
    driver._scan_unavailable_markers([_marker()])
    assert driver._bot_status_text("claude") == "Rate-limited ⏳"
    assert driver._skip_key("claude") == "rate-limited"
