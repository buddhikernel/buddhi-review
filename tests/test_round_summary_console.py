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
    # done / clean — the one done-for-the-run label, shared by an explicit clean
    # sentinel and a summary-only genuine review (R2)
    d.done.add("claude")
    d._bot_state("claude").signal = detectors.SIGNAL_CLEAN
    assert d._bot_status_text("claude") == "reviewed — no findings"
    # quota / errored
    d._bot_state("copilot").signal = detectors.SIGNAL_QUOTA
    d.store.exclude_quota("copilot")
    assert d._bot_status_text("copilot") == "quota"
    d._bot_state("codex").signal = detectors.SIGNAL_ERRORED
    d.store.exclude_errored("codex")
    assert d._bot_status_text("codex") == "errored"
    # store-excluded with no signal → "excluded"
    d.store.exclude_permanent("gemini")
    assert d._bot_status_text("gemini") == "excluded"


def test_bot_status_text_active_vs_reviewed():
    d = _bare_driver()
    assert d._bot_status_text("claude") == "active"          # never seen
    d._bot_state("claude").last_seen = 12.0                  # contributed this round
    assert d._bot_status_text("claude") == "reviewed"


def test_round_table_rows_capitalize_unknown_reviewer_label():
    d = _bare_driver(cfg={"active_reviewers": ["mystery"], "auto_on_open": {"mystery": True}})
    rows = d._round_table_rows([], [])
    assert rows[0]["bot_key"] == "mystery"
    assert rows[0]["label"] == "Mystery"  # _REVIEWER_LABEL fallback to capitalize()


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
    d = RoundDriver(
        "7", repo="o/r", cwd="/nonexistent", cfg=cfg, adapter=adapter,
        classify_runner=lambda p: json.dumps({"label": "SUBSTANTIVE", "reason": "t"}),
        fix_dispatch=lambda c, r: FixOutcome(status="applied"),
        fetch=fetch, gh_run=_Gh(), clock=clock, sleep=clock.sleep,
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
