"""Round-summary box table + canonical reviewer order + honest skip-reason logging.

All console-only: the table aligns, the reviewer order is the same everywhere
(Claude → Copilot → Codex → Gemini), and a reviewer dropped from a round's
expected set is logged with the reason WHY.
"""
from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stdout

from buddhi_review import detectors, round_driver
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.fix_apply import FixOutcome
from buddhi_review.loop import Comment
from buddhi_review.round_driver import (
    RoundDriver,
    RoundTimes,
    _canonical,
    _display_width,
    _pad_cell,
    _render_round_table,
    _strip_cell_emoji,
)
from buddhi_review.seams import ConsoleEscalation


# --------------------------------------------------------------- pure helpers


def test_strip_cell_emoji_removes_color_emoji_vs16_zwj():
    # color emoji + VS16 + ZWJ family sequence all go; surrounding text stays.
    # Trailing emoji are stripped clean; a leading emoji's indent is preserved
    # (the helper only rstrips + collapses interior gaps, never leading space).
    assert _strip_cell_emoji("done ✅️") == "done"
    assert _strip_cell_emoji("approved 👍") == "approved"
    assert _strip_cell_emoji("family 👩‍👩‍👧") == "family"  # ZWJ sequence stripped
    assert _strip_cell_emoji("waiting ⏳") == "waiting"
    # no emoji codepoint survives even when the glyph leads the cell
    assert all(ord(c) < 0x2300 for c in _strip_cell_emoji("👩‍👩‍👧 family"))


def test_strip_cell_emoji_keeps_cjk_and_plain_text():
    assert _strip_cell_emoji("レビュー") == "レビュー"   # CJK is legitimate width, kept
    assert _strip_cell_emoji("PR too large") == "PR too large"
    assert _strip_cell_emoji("a 👍 b") == "a b"          # interior gap collapsed


def test_strip_cell_emoji_preserves_blank_cell():
    assert _strip_cell_emoji("") == ""
    assert _strip_cell_emoji("   ") == "   "


def test_display_width_counts_cjk_as_two():
    assert _display_width("abc") == 3
    assert _display_width("レビュー") == 8       # 4 wide glyphs × 2
    assert _display_width("á") == 1        # combining accent adds 0


def test_pad_cell_pads_and_truncates():
    assert _pad_cell("hi", 5) == "hi   "
    assert _display_width(_pad_cell("レビュー", 10)) == 10   # padded to display width
    truncated = _pad_cell("a very long status text", 8)
    assert _display_width(truncated) == 8
    assert truncated.endswith("…")


def test_canonical_order_is_claude_copilot_codex_gemini():
    assert _canonical(["gemini", "codex", "copilot", "claude"]) == \
        ["claude", "copilot", "codex", "gemini"]
    # unknown reviewers keep their order, after the known four
    assert _canonical(["mystery", "gemini", "claude"]) == ["claude", "gemini", "mystery"]
    # dedup contract: a malformed config that lists a reviewer twice must NOT
    # double-render / double-log it (active_reviewers passes duplicates through).
    assert _canonical(["claude", "claude", "gemini", "gemini"]) == ["claude", "gemini"]
    assert _canonical(["mystery", "mystery", "claude"]) == ["claude", "mystery"]  # unknowns dedup too


# --------------------------------------------------------------- table render


def _box_lines(text):
    return [ln for ln in text.splitlines() if ln and ln[0] in "┌├└│"]


def _render_to_string(round_no, max_rounds, rows):
    buf = io.StringIO()
    with redirect_stdout(buf):
        _render_round_table(round_no, max_rounds, rows)
    return buf.getvalue()


def _row(label, **counts):
    base = {k: 0 for k in round_driver._TABLE_COUNT_KEYS}
    base.update(counts)
    base["label"] = label
    base.setdefault("status", "active")
    return base


def test_round_table_is_rectangular():
    rows = [
        _row("Claude", posted=3, sub=2, cosm=1, status="reviewed"),
        _row("Copilot", status="quota"),
        _row("Codex", posted=1, inval=1, status="PR too large"),
        _row("Gemini", status="active"),
    ]
    out = _render_to_string(2, 10, rows)
    widths = {_display_width(ln) for ln in _box_lines(out)}
    assert len(widths) == 1, f"box not rectangular: {widths}"


def test_round_table_stays_rectangular_with_emoji_status():
    # a status cell carrying an emoji must NOT break the box (emoji is stripped)
    rows = [_row("Gemini", status="done 🎉"), _row("Claude", status="active")]
    out = _render_to_string(1, 5, rows)
    widths = {_display_width(ln) for ln in _box_lines(out)}
    assert len(widths) == 1
    assert "🎉" not in out


def _total_cells(out):
    """Positionally-parsed TOTAL-row cells (index 0 == 'TOTAL', then one per column)."""
    total_line = [ln for ln in out.splitlines() if "TOTAL" in ln][0]
    return [c.strip() for c in total_line.split("│")[1:-1]]


def test_round_table_total_row_sums_counts():
    # Distinct totals (posted=5, subst=3, cosm=2) parsed BY COLUMN POSITION, so a
    # column swap or an off-by-one in one column is provably caught (a whole-line
    # substring check would pass on any permutation of the same multiset).
    rows = [
        _row("Claude", posted=2, sub=2),
        _row("Codex", posted=3, sub=1, cosm=2),
    ]
    cells = _total_cells(_render_to_string(1, 10, rows))
    # column order: Bot, Posted, Subst, Cosm, PR-d, Outd, Inval, Biz, Fail, Status
    assert cells[0] == "TOTAL"
    assert cells[1] == "5"   # posted
    assert cells[2] == "3"   # subst
    assert cells[3] == "2"   # cosm
    assert cells[4:9] == ["0", "0", "0", "0", "0"]  # prdesc/outd/inval/biz/fail
    assert cells[9] == ""    # status has no total


def test_round_table_all_zero_totals():
    cells = _total_cells(_render_to_string(1, 10, [_row("Claude"), _row("Gemini")]))
    assert cells[1:9] == ["0"] * 8


def test_round_table_header_and_title_present():
    out = _render_to_string(4, 8, [_row("Claude")])
    assert "Round 4 of 8 summary" in out
    for header in ("Bot", "Posted", "Subst", "Status"):
        assert header in out


# -------------------------------------------------- driver: rows + skip log


CFG = {"active_reviewers": ["copilot", "gemini", "codex", "claude"],
       "auto_on_open": {"copilot": True, "gemini": True, "codex": True, "claude": False}}


def _bare_driver(cfg=None):
    # No run() — just a constructed driver whose state we set directly.
    return RoundDriver("7", repo="o/r", cfg=cfg if cfg is not None else CFG,
                       classify_runner=lambda p: "{}", clean_llm=None)


def test_round_table_rows_tally_labels_into_columns():
    d = _bare_driver()
    actionable = [
        Comment(id="a", text="x", source="claude[bot]"),
        Comment(id="b", text="y", source="claude[bot]"),
        Comment(id="c", text="z", source="codex[bot]"),
    ]

    class R:
        def __init__(self, label):
            self.classification = type("C", (), {"label": label})()

    results = [R("SUBSTANTIVE"), R("COSMETIC"), R("INVALID")]
    rows = {r["bot_key"]: r for r in d._round_table_rows(actionable, results)}
    assert [r for r in rows] == ["claude", "copilot", "codex", "gemini"]  # canonical order
    assert rows["claude"]["posted"] == 2
    assert rows["claude"]["sub"] == 1 and rows["claude"]["cosm"] == 1
    assert rows["codex"]["posted"] == 1 and rows["codex"]["inval"] == 1
    assert rows["copilot"]["posted"] == 0


def test_log_skipped_names_the_reason_per_bot():
    d = _bare_driver()
    # claude: clean/done · copilot: quota · codex: errored · gemini: still expected
    d.done.add("claude")
    d._bot_state("claude").signal = detectors.SIGNAL_CLEAN
    d.store.exclude_quota("copilot")
    d._bot_state("copilot").signal = detectors.SIGNAL_QUOTA
    d.store.exclude_errored("codex")
    d._bot_state("codex").signal = detectors.SIGNAL_ERRORED

    expected = d.expected_bots()  # only gemini remains
    assert expected == ["gemini"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        d._log_skipped(expected)
    out = buf.getvalue()
    assert "skipping claude: voluntarily done (LGTM)" in out
    assert "skipping copilot: quota exhausted" in out
    assert "skipping codex: errored" in out
    assert "skipping gemini" not in out  # still expected — never logged as skipped


def test_pr_too_large_skip_reason():
    d = _bare_driver(cfg={"active_reviewers": ["claude", "gemini"],
                          "auto_on_open": {"claude": False, "gemini": True}})
    d.store.exclude_pr_too_large("claude")
    d._bot_state("claude").signal = detectors.SIGNAL_PR_TOO_LARGE
    buf = io.StringIO()
    with redirect_stdout(buf):
        d._log_skipped(d.expected_bots())
    assert "skipping claude: PR too large" in buf.getvalue()


def test_store_excluded_without_signal_reads_as_excluded_not_unexpected():
    # The defensive fallthrough: a bot the store excludes but whose in-memory
    # BotState carries no signal must read "excluded", never "not expected".
    d = _bare_driver()
    d.store.exclude_permanent("copilot")  # store-level exclusion, signal stays None
    assert d._skip_key("copilot") == "excluded"
    assert "copilot" not in d.expected_bots()
    buf = io.StringIO()
    with redirect_stdout(buf):
        d._log_skipped(d.expected_bots())
    out = buf.getvalue()
    assert "skipping copilot: excluded" in out
    assert "skipping copilot: not expected" not in out


def test_bot_status_text_per_cause():
    d = _bare_driver()
    # clean signal — an EXPLICIT all-clear (sentinel / LGTM) is "Approved 👍"
    d.done.add("claude")
    d._bot_state("claude").signal = detectors.SIGNAL_CLEAN
    assert d._bot_status_text("claude") == "Approved 👍"
    # quota / errored
    d._bot_state("copilot").signal = detectors.SIGNAL_QUOTA
    d.store.exclude_quota("copilot")
    assert d._bot_status_text("copilot") == "Quota exhausted ⚠️"
    d._bot_state("codex").signal = detectors.SIGNAL_ERRORED
    d.store.exclude_errored("codex")
    assert d._bot_status_text("codex") == "Could not review ❌"
    # store-excluded with no signal → "excluded"
    d.store.exclude_permanent("gemini")
    assert d._bot_status_text("gemini") == "excluded"


# ─────────────────────────────────────────────────────────────────────
# Round-scoping of "Could not review ❌" (round-2 mislabel incident).
# The label describes an EVENT (this round's attempt errored), not a
# persistent state: in a later round the errored bot is deliberately NOT
# re-summoned (expected_bots subtracts the errored exclusion), so its row
# must read "Not requested 🙅" like any other bot the round skipped — the
# incident run showed Copilot as "Could not review" in a round-2 table
# where it was never asked.
# ─────────────────────────────────────────────────────────────────────


def _errored_driver():
    d = _bare_driver()
    d._bot_state("copilot").signal = detectors.SIGNAL_ERRORED
    d.store.exclude_errored("copilot")
    return d


def test_errored_label_renders_in_the_round_the_bot_was_expected():
    # The error round: the bot is still in the round-START expected set (it
    # was computed before the mid-round exclusion landed).
    d = _errored_driver()
    assert d._bot_status_text(
        "copilot", expected=["claude", "copilot", "codex", "gemini"]
    ) == "Could not review ❌"


def test_errored_label_falls_to_not_requested_when_round_skipped_the_bot():
    # Round 2+: not expected, posted nothing — the bot simply was not asked
    # this round.
    d = _errored_driver()
    assert d._bot_status_text(
        "copilot", expected=["codex"]
    ) == "Not requested 🙅"


def test_errored_label_kept_when_bot_posted_despite_not_being_asked():
    # An unsolicited late posting that did NOT prove recovery (a genuine
    # classified comment would have retracted the exclusion via the comeback
    # before the table renders) keeps the honest error label.
    d = _errored_driver()
    assert d._bot_status_text(
        "copilot", expected=["codex"], posted=1) == "Could not review ❌"


def test_errored_label_kept_when_seen_this_round_without_a_classified_comment():
    # The "seen this round" arm: a bot
    # not in this round's expected set that still contributed something
    # non-actionable this round (last_seen set, e.g. a re-posted error
    # placeholder — seen but posted==0) keeps "Could not review ❌", not
    # "Not requested 🙅". last_seen is the per-round stamp (_wait_for_quiescence
    # resets it each round), so this only fires in a round the bot actually
    # acted in.
    d = _errored_driver()
    d._bot_state("copilot").last_seen = 123.0   # contributed this round
    assert d._bot_status_text(
        "copilot", expected=["codex"], posted=0) == "Could not review ❌"


def test_errored_label_unscoped_without_round_context():
    # A caller with no round context (expected=None) keeps the label — the
    # scoping engages only on the render path, which always passes the set.
    d = _errored_driver()
    assert d._bot_status_text("copilot") == "Could not review ❌"


def test_round_table_row_shows_not_requested_for_skipped_errored_bot():
    # Table-level integration: the round-2 card. Copilot is errored-excluded
    # from round 1, absent from round 2's expected set, and posted nothing —
    # its row must be "Not requested 🙅", byte-identical to every other
    # skipped bot's row.
    d = _errored_driver()
    rows = {r["bot_key"]: r for r in d._round_table_rows([], [], expected=["codex"])}
    assert rows["copilot"]["status"] == "Not requested 🙅"


def test_bot_status_text_split_approved_vs_no_findings_vs_no_change():
    # The done-for-the-run labels split three ways on HOW the reviewer got
    # there: explicit all-clear → Approved; the zero-findings promotion (in
    # `done` with NO clean signal) → Reviewed — no findings; a dismissed-
    # substantive demotion → Reviewed — no change.
    d = _bare_driver()
    d._bot_state("claude").signal = detectors.SIGNAL_CLEAN
    assert d._bot_status_text("claude") == "Approved 👍"
    d.done.add("copilot")                       # promotion path: no signal
    assert d._bot_status_text("copilot") == "Reviewed — no findings ✓"
    d.reviewed_no_change.add("codex")           # dismissed-substantive demotion
    assert d._bot_status_text("codex") == "Reviewed — no change ✓"
    d.polishing.add("gemini")                   # cosmetic-only demotion
    assert d._bot_status_text("gemini") == "Polish-only 🧹"


def test_approved_label_is_sticky_across_a_later_hard_signal():
    # A hard placeholder (quota / errored / PR-too-large) arriving AFTER an
    # explicit all-clear overwrites the mutable BotState.signal — the sticky
    # `approved` set must keep the sign-off's label either way round, never
    # demoting it to the promotion's "Reviewed — no findings ✓".
    d = _bare_driver()
    # clean first, quota after (signal overwritten by the placeholder)
    d.done.add("claude")
    d.approved.add("claude")
    d._bot_state("claude").signal = detectors.SIGNAL_QUOTA
    d.store.exclude_quota("claude")
    assert d._bot_status_text("claude") == "Approved 👍"
    # quota first, clean after (signal ends CLEAN) — same label
    d.done.add("copilot")
    d.approved.add("copilot")
    d._bot_state("copilot").signal = detectors.SIGNAL_CLEAN
    d.store.exclude_quota("copilot")
    assert d._bot_status_text("copilot") == "Approved 👍"


def test_bot_status_text_active_vs_no_review_posted():
    d = _bare_driver()
    # Expected yet never seen this round → posted no review (the pre-split
    # "silent" state, folded into the same label as a silent drop).
    assert d._bot_status_text("claude") == "No review posted 🔇"
    d._bot_state("claude").last_seen = 12.0                  # contributed this round
    assert d._bot_status_text("claude") == "Active ✅"
    # A silent DROP renders the identical label (the old "silent (dropped)").
    d.silent_dropped.add("gemini")
    assert d._bot_status_text("gemini") == "No review posted 🔇"


def test_round_table_rows_capitalize_unknown_reviewer_label():
    d = _bare_driver(cfg={"active_reviewers": ["mystery"], "auto_on_open": {"mystery": True}})
    rows = d._round_table_rows([], [])
    # full-roster view: the built-in four render first (canonical order), then
    # the enabled unknown reviewer keeps its row after them
    assert [r["bot_key"] for r in rows] == \
        ["claude", "copilot", "codex", "gemini", "mystery"]
    assert rows[-1]["label"] == "Mystery"  # _REVIEWER_LABEL fallback to capitalize()


def test_round_table_rows_cover_full_roster_with_not_requested():
    # A configured-but-inactive built-in reviewer still gets a row — labelled
    # "Not requested 🙅" — while the enabled-but-quiet reviewers keep their
    # existing "No review posted 🔇" label, and the order stays canonical.
    d = _bare_driver(cfg={"active_reviewers": ["claude", "codex"],
                          "auto_on_open": {"claude": False, "codex": True}})
    rows = d._round_table_rows([], [])
    assert [r["bot_key"] for r in rows] == ["claude", "copilot", "codex", "gemini"]
    by = {r["bot_key"]: r for r in rows}
    assert by["copilot"]["status"] == "Not requested 🙅"
    assert by["gemini"]["status"] == "Not requested 🙅"
    assert by["copilot"]["posted"] == 0 and by["gemini"]["posted"] == 0
    assert by["claude"]["status"] == "No review posted 🔇"
    assert by["codex"]["status"] == "No review posted 🔇"


def test_skip_key_and_long_form_for_not_requested_bot():
    d = _bare_driver(cfg={"active_reviewers": ["claude"],
                          "auto_on_open": {"claude": False}})
    assert d._skip_key("gemini") == "not-requested"
    assert d._bot_status_text("gemini") == "Not requested 🙅"
    # every skip key carries an honest long form
    assert "not requested" in round_driver._SKIP_LONG["not-requested"]
    # distinct from the repo-gate label: an operator-off reviewer (removed from
    # the fleet) reads "Not requested 🙅", never the repo-gate "Not configured
    # (repo) 🔧". Under the per-reviewer model a non-Claude reviewer is never
    # gate-excluded, so it could not read "Not configured" here regardless — only
    # an absent Claude workflow earns that badge.
    assert d._bot_status_text("gemini") != round_driver._STATUS_NOT_CONFIGURED


def test_not_requested_is_lowest_priority_real_state_wins():
    # A not-summoned reviewer that engaged anyway is never masked as
    # not-requested: an explicit all-clear renders "Approved 👍", and mere
    # activity this round renders the same "Active ✅" an expected bot earns.
    d = _bare_driver(cfg={"active_reviewers": ["claude"],
                          "auto_on_open": {"claude": False}})
    d.done.add("gemini")
    d.approved.add("gemini")
    d._bot_state("gemini").signal = detectors.SIGNAL_CLEAN
    assert d._bot_status_text("gemini") == "Approved 👍"
    d._bot_state("codex").last_seen = 12.0
    assert d._bot_status_text("codex") == "Active ✅"


def test_round_start_reset_clears_stale_stamp_for_not_requested_bot():
    # A not-summoned bot that posted in an EARLIER round must fall back to
    # "Not requested 🙅" in a later round it sat out — the round-start reset
    # covers every tracked reviewer, not just the expected set.
    d = _bare_driver(cfg={"active_reviewers": ["claude"],
                          "auto_on_open": {"claude": False}})
    d.fetch = lambda pr, repo=None, cwd=None: []
    d._bot_state("gemini").last_seen = 5.0          # engaged in a prior round
    d._wait_for_quiescence([], 0.0)                 # a round it sits out
    assert d._bot_status_text("gemini") == "Not requested 🙅"


# ------------------------------------------------- driver: end-to-end console


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _Notifier:
    name = "console"

    def startup_log(self):
        pass

    def send(self, ask):
        pass

    def read_answer(self, ask):
        return None

    def clear(self, ask):
        pass


class _Gh:
    def __call__(self, argv, *, cwd=None, timeout=None):
        out = " M x.py\n" if argv[:3] == ["git", "status", "--porcelain"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


def _run_capture(timeline, cfg, **kw):
    clock = _Clock()

    def fetch(pr, repo=None, cwd=None):
        return [c for t, c in timeline if t <= clock.t]

    adapter = ReviewAdapter(escalation=ConsoleEscalation(notifier=_Notifier()))
    kw.setdefault("gh_run", _Gh())
    kw.setdefault("preflight", False)  # timeline comments arrive during the round
    d = RoundDriver(
        "7", repo="o/r", cwd="/nonexistent", cfg=cfg, adapter=adapter,
        classify_runner=lambda p: json.dumps({"label": "SUBSTANTIVE", "reason": "t"}),
        fix_dispatch=lambda c, r: FixOutcome(status="applied"),
        fetch=fetch, clock=clock, sleep=clock.sleep,
        notice=lambda *a, **k: "",
        # register_delay=0 keeps these console-render tests on the tight timing
        # they were written for (the post-summon register delay is exercised in
        # test_round_driver).
        times=RoundTimes(quiescence=60, poll_interval=30, min_bot_wait=420,
                         idle_timeout=900, max_wait_total=1800, register_delay=0),
        answer_waiter=lambda esc, **k: {}, **kw,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        outcome = d.run()
    return outcome, buf.getvalue(), d


def test_expecting_line_is_canonical_and_table_renders_each_round():
    # codex hits quota in round 1 (gemini comes in clean); claude's substantive fix
    # earns a round 2, whose skip-log names codex's quota exclusion + canonical order.
    timeline = [
        (0, Comment(id="a", text="Rate limit exceeded for this model.", source="codex[bot]")),
        (0, Comment(id="b", text="this null check is missing", source="claude[bot]")),
        (0, Comment(id="g", text="No issues found.", source="gemini-code-assist[bot]")),
        (90, Comment(id="c", text="No issues found.", source="claude[bot]")),
    ]
    cfg = {"active_reviewers": ["gemini", "codex", "claude"],
           "auto_on_open": {"gemini": True, "codex": True, "claude": False}}
    outcome, out, _ = _run_capture(timeline, cfg, max_rounds=3)
    assert outcome.status == "clean"
    # the expecting line lists reviewers in canonical order (claude before codex/gemini)
    r1 = [ln for ln in out.splitlines() if "Round 1 of" in ln and "expecting" in ln][0]
    assert "expecting: claude, codex, gemini" in r1
    # a round-summary table was printed
    assert "summary" in out and "┌" in out and "TOTAL" in out
    # honest skip-reason logging fired once codex was excluded
    assert "skipping codex: quota exhausted" in out


def test_full_roster_table_summons_only_the_active_set():
    # With a two-bot fleet the console table still renders all four built-in
    # reviewers — the inactive two as "Not requested 🙅" (the 🙅 stripped for the
    # monospace box, so the cell reads "Not requested") — while the expecting
    # line, the summons, and the polling stay on the active set only.
    calls = []

    class _RecGh(_Gh):
        def __call__(self, argv, *, cwd=None, timeout=None):
            calls.append(list(argv))
            return super().__call__(argv, cwd=cwd, timeout=timeout)

    timeline = [
        (0, Comment(id="a", text="No issues found.", source="claude[bot]")),
        (0, Comment(id="b", text="No issues found.", source="codex[bot]")),
    ]
    cfg = {"active_reviewers": ["claude", "codex"],
           "auto_on_open": {"claude": False, "codex": True}}
    outcome, out, _ = _run_capture(timeline, cfg, max_rounds=3, gh_run=_RecGh())
    assert outcome.status == "clean"
    r1 = [ln for ln in out.splitlines() if "Round 1 of" in ln and "expecting" in ln][0]
    assert "expecting: claude, codex" in r1
    assert "copilot" not in r1 and "gemini" not in r1
    # all four built-ins have a table row; the two outside the fleet read
    # "Not requested" (the canonical "Not requested 🙅" with its 🙅 stripped for
    # the box, like every other status glyph)
    for label in ("Claude", "Copilot", "Codex", "Gemini"):
        assert any(ln.startswith("│") and label in ln for ln in out.splitlines())
    not_req_box_lines = [ln for ln in out.splitlines() if ln.startswith("│") and "Not requested" in ln]
    assert len(not_req_box_lines) == 2
    assert all("🙅" not in ln for ln in not_req_box_lines)
    # no summon ever targeted a not-requested bot: neither gemini's trigger
    # comment nor copilot's review-request API call was posted. The positive
    # control first — claude's round-1 summon MUST be visible through the same
    # recorded seam, or the negative assertions below prove nothing.
    bodies = [" ".join(argv) for argv in calls]
    assert any(round_driver.TRIGGER_COMMENTS["claude"] in b for b in bodies)
    assert not any(round_driver.TRIGGER_COMMENTS["gemini"] in b for b in bodies)
    assert not any("requested_reviewers" in b for b in bodies)


def test_max_rounds_auto_size_seam_via_diff_lines():
    # max_rounds=None auto-sizes from the diff (uncapped); explicit still wins.
    d_auto = _bare_driver()
    d_auto = RoundDriver("7", cfg=CFG, classify_runner=lambda p: "{}",
                         max_rounds=None, diff_lines=6400)
    assert d_auto.max_rounds == round_driver.pick_max_rounds(6400) == 10

    d_big = RoundDriver("7", cfg=CFG, classify_runner=lambda p: "{}",
                        max_rounds=None, diff_lines=204_800)
    assert d_big.max_rounds > 10  # no artificial cap

    d_explicit = RoundDriver("7", cfg=CFG, classify_runner=lambda p: "{}",
                             max_rounds=3, diff_lines=204_800)
    assert d_explicit.max_rounds == 3  # explicit wins over diff auto-size

    # diff_lines=None (the diff-size probe failed) → the default fallback budget.
    d_fallback = RoundDriver("7", cfg=CFG, classify_runner=lambda p: "{}",
                             max_rounds=None, diff_lines=None)
    assert d_fallback.max_rounds == round_driver.MAX_ROUNDS_FALLBACK == 10


# ---------------------------------------------------------------------------
# #50 — per-round silent-reviewer prerequisite guidance note (dim [reviewer-silent])
# ---------------------------------------------------------------------------
from test_round_driver import CLAUDE_ONLY, make_driver  # noqa: E402


def test_silent_guidance_note_for_summoned_reviewer(capsys):
    # An auto_on_open=false reviewer the loop summoned that then posts nothing this
    # round → a dim [reviewer-silent] prerequisite note, forking on auto_on_open=false.
    driver, _, _ = make_driver([], cfg=CLAUDE_ONLY)
    driver.run()
    out = capsys.readouterr().out
    assert "[reviewer-silent]" in out
    assert "summoned this round" in out            # the auto_on_open=false wording
    assert "Claude" in out


def test_silent_guidance_note_for_auto_on_open_reviewer(capsys):
    # An auto_on_open=true reviewer the loop does NOT summon that stays silent →
    # the note forks to the "review on PR open" wording instead.
    cfg = {"active_reviewers": ["gemini"], "auto_on_open": {"gemini": True}}
    driver, _, _ = make_driver([], cfg=cfg)
    driver.run()
    out = capsys.readouterr().out
    assert "[reviewer-silent]" in out
    assert "review on PR open" in out              # the auto_on_open=true wording


def test_silent_guidance_note_is_once_per_run(capsys):
    # The note is emitted at most once per bot per run (the _silent_noted guard).
    driver, _, _ = make_driver([], cfg=CLAUDE_ONLY)
    driver.requested_ever.add("claude")            # genuinely summoned → expected
    driver._emit_silent_reviewer_guidance(["claude"])
    driver._emit_silent_reviewer_guidance(["claude"])
    out = capsys.readouterr().out
    assert out.count("[reviewer-silent]") == 1
    assert "claude" in driver._silent_noted


def test_silent_guidance_skips_a_reviewer_that_responded(capsys):
    driver, _, _ = make_driver([], cfg=CLAUDE_ONLY)
    driver.requested_ever.add("claude")
    driver._bot_state("claude").last_seen = 0.0    # responded this round
    driver._emit_silent_reviewer_guidance(["claude"])
    assert "[reviewer-silent]" not in capsys.readouterr().out


def test_silent_guidance_skips_an_unsummonable_reviewer(capsys):
    # Never summoned + auto_on_open=false → the loop could not have expected a
    # review, so it is not nagged.
    driver, _, _ = make_driver([], cfg=CLAUDE_ONLY)
    driver._emit_silent_reviewer_guidance(["claude"])   # requested_ever empty
    assert "[reviewer-silent]" not in capsys.readouterr().out


def test_silent_guidance_skips_a_known_excluded_reviewer(capsys):
    # A reviewer excluded for a known cause (quota) is not a setup gap → no note.
    driver, _, _ = make_driver([], cfg=CLAUDE_ONLY)
    driver.requested_ever.add("claude")
    driver.store.exclude_quota("claude")
    driver._emit_silent_reviewer_guidance(["claude"])
    assert "[reviewer-silent]" not in capsys.readouterr().out
