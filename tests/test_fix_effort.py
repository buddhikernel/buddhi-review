"""A real-flaw fix runs fast by default, and spends more effort only when the
classifier can name why — no ``claude`` binary, no network.

Covers: the SUBSTANTIVE default (Opus at medium, also for a missing or malformed
pick), every other label left as before, the named-criterion rule for picks above
medium, the guidance the classifier is sent, the effort vocabulary's timeouts,
the older-CLI degrade at both ``--effort`` boundaries, the dispatch boundary, and
the plan table."""
from __future__ import annotations

import io
import json
import os

import pytest

from buddhi_review import actuators, classify, fix_apply, model_call, plan_profile, policy
from buddhi_review.classify import (
    Classification,
    build_prompt,
    classification_failed,
    classify_comment,
    parse_classification,
)
from buddhi_review.loop import Comment, CommentResult


def _parse(obj):
    """Parse one classifier reply. A parse that raises is a defect of its own, so
    it is reported as a failed assertion naming the input."""
    try:
        return parse_classification(json.dumps(obj) if isinstance(obj, dict) else obj)
    except Exception as exc:
        raise AssertionError(f"parsing {obj!r} raised {exc!r}") from exc


# ── The SUBSTANTIVE default ─────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    {"label": "SUBSTANTIVE"},                      # JSON, no model or effort
    {"label": "SUBSTANTIVE", "model": "", "effort": ""},
    "SUBSTANTIVE|claude|||null deref on empty input",   # legacy pipe, empty fields
    "the model rambled, but this is SUBSTANTIVE",       # last-resort priority scan
])
def test_a_missing_substantive_pick_runs_on_opus_at_medium(raw):
    c = _parse(raw)
    assert (c.label, c.model, c.effort) == ("SUBSTANTIVE", "opus", "medium")
    # The default is medium itself, not a higher default that the hold lowers
    # (which would announce a pick the classifier never made).
    assert c.held_effort == "" and c.effort_criterion == ""


@pytest.mark.parametrize("model, effort, want", [
    ("gpt-5.6-terra", "banana", ("opus", "medium")),   # another vendor's model, no such effort
    ("Opus", "HIGH", ("opus", "medium")),              # folded; high without a criterion is held
    ("SONNET", "Low", ("sonnet", "low")),              # a valid pick in any case is kept
    ("haiku", "medium", ("haiku", "medium")),
])
def test_a_malformed_substantive_pick_falls_back_and_a_valid_one_is_kept(model, effort, want):
    c = _parse({"label": "SUBSTANTIVE", "model": model, "effort": effort, "reason": "npe"})
    assert (c.model, c.effort) == want


def test_every_other_label_keeps_the_old_fallback_and_its_pick():
    assert _parse({"label": "COSMETIC"}).tuple4 == ("COSMETIC", "claude", "sonnet", "high")
    assert _parse("this is COSMETIC").tuple4 == ("COSMETIC", "claude", "sonnet", "high")
    # A non-SUBSTANTIVE pick is used exactly as the classifier gave it: no
    # criterion rule, no vocabulary check, no case folding.
    c = _parse({"label": "COSMETIC", "model": "Haiku", "effort": "xhigh", "reason": "nit"})
    assert (c.model, c.effort, c.held_effort) == ("Haiku", "xhigh", "")
    assert _parse({"label": "COSMETIC", "effort": "banana"}).effort == "banana"
    bq = _parse({"label": "BUSINESS_QUESTION", "model": "opus", "effort": "max"})
    assert (bq.model, bq.effort, bq.held_effort) == ("opus", "max", "")
    failed = classification_failed()
    assert (failed.model, failed.effort) == ("sonnet", "high")
    assert classify.label_fix_default("COSMETIC") == ("sonnet", "high")
    assert classify.label_fix_default("SUBSTANTIVE") == ("opus", "medium")


# ── Above medium only on a named criterion ──────────────────────────────────────

@pytest.mark.parametrize("effort", ["high", "xhigh", "max"])
@pytest.mark.parametrize("reason", [
    "needs careful analysis of the parser",
    "non-trivial change to the retry loop",
    "security-relevant: token handling",
])
def test_a_pick_above_medium_that_names_no_criterion_runs_at_medium(effort, reason):
    c = _parse({"label": "SUBSTANTIVE", "model": "opus", "effort": effort,
                "effort_criterion": "none", "reason": reason})
    assert c.effort == "medium" and c.held_effort == effort
    assert c.reason == reason


@pytest.mark.parametrize("name", [n for n, _ in policy.EFFORT_CRITERIA])
@pytest.mark.parametrize("effort", ["high", "xhigh", "max"])
def test_a_named_criterion_keeps_the_pick_and_leads_the_reason(name, effort):
    c = _parse({"label": "SUBSTANTIVE", "model": "opus", "effort": effort,
                "effort_criterion": name, "reason": "two writers race on the lock file"})
    assert c.effort == effort and c.held_effort == ""
    assert c.reason == f"{name}: two writers race on the lock file"
    # Named at the start of the reason, with no field (a legacy pipe line).
    piped = parse_classification(f"SUBSTANTIVE|claude|opus|{effort}|{name}: see the hunk")
    assert piped.effort == effort and piped.reason == f"{name}: see the hunk"


@pytest.mark.parametrize("criterion", [
    "multi-modules", "multi-module-ish", "multi_module_ish", "subtle-correctnessish",
    "prior-attempt-failedx", "deeply-non-obvious-ish", "Multi Modules: three files",
])
def test_a_longer_word_is_not_the_criterion(criterion):
    c = _parse({"label": "SUBSTANTIVE", "effort": "xhigh", "effort_criterion": criterion,
                "reason": criterion})
    assert c.effort == "medium" and c.held_effort == "xhigh"


@pytest.mark.parametrize("spelled", [
    "Multi Module", "multi_module", "MULTI-MODULE", "**multi-module**: three files",
    "- subtle correctness", "`deeply_non_obvious`", "Prior Attempt Failed",
    "multi  module", "_multi-module: three files", "[multi-module] three files",
    "(multi-module) three files", '"multi-module"', "'multi-module'",
])
def test_a_spelled_out_criterion_counts(spelled):
    c = _parse({"label": "SUBSTANTIVE", "effort": "high", "effort_criterion": spelled,
                "reason": "see the hunk"})
    assert c.effort == "high" and c.held_effort == ""


@pytest.mark.parametrize("criterion, reason", [
    ("none", "the fix is not multi-module, it is a local fix"),
    ("none (no multi-module change)", "a local fix"),
    ("", "a local fix, not subtle-correctness"),
])
def test_the_criterion_must_open_the_text(criterion, reason):
    # A criterion named later in the sentence names nothing.
    c = _parse({"label": "SUBSTANTIVE", "effort": "max", "effort_criterion": criterion,
                "reason": reason})
    assert c.effort == "medium" and c.held_effort == "max"


@pytest.mark.parametrize("effort", ["low", "medium"])
def test_low_and_medium_are_never_held(effort):
    c = _parse({"label": "SUBSTANTIVE", "model": "opus", "effort": effort, "reason": "typo"})
    assert c.effort == effort and c.held_effort == ""


def test_the_hold_reads_a_classification_directly():
    c = classify.hold_effort_to_evidence(
        Classification(label="SUBSTANTIVE", model="opus", effort="max", reason="x"))
    assert (c.effort, c.held_effort) == ("medium", "max")
    kept = classify.hold_effort_to_evidence(
        Classification(label="SUBSTANTIVE", effort="max", effort_criterion="multi-module",
                       reason="x"))
    assert (kept.effort, kept.reason) == ("max", "multi-module: x")
    # An effort outside the vocabulary is not "above medium": the hold leaves it.
    odd = classify.hold_effort_to_evidence(Classification(label="SUBSTANTIVE", effort="banana"))
    assert (odd.effort, odd.held_effort) == ("banana", "")


def test_the_field_wins_over_the_reason_and_the_reason_gets_its_canonical_name():
    c = _parse({"label": "SUBSTANTIVE", "effort": "high", "effort_criterion": "multi-module",
                "reason": "subtle-correctness: two writers"})
    assert c.reason == "multi-module: subtle-correctness: two writers"
    c = _parse({"label": "SUBSTANTIVE", "effort": "high", "effort_criterion": "Multi Module",
                "reason": "see the hunk"})
    assert c.reason == "multi-module: see the hunk"
    # The field wins even when it names a later criterion than the reason does.
    c = _parse({"label": "SUBSTANTIVE", "effort": "high",
                "effort_criterion": "subtle-correctness", "reason": "multi-module: two writers"})
    assert c.reason == "subtle-correctness: multi-module: two writers"
    assert classify.named_effort_criterion("prior-attempt-failed", "multi-module: x") \
        == "prior-attempt-failed"


@pytest.mark.parametrize("criterion, stored, effort", [
    (1, "1", "medium"),
    (True, "True", "medium"),
    (["multi-module"], "['multi-module']", "high"),
])
def test_a_criterion_that_is_not_a_string_is_read_as_text(criterion, stored, effort):
    c = _parse({"label": "SUBSTANTIVE", "effort": "high", "effort_criterion": criterion,
                "reason": "x"})
    assert (c.effort_criterion, c.effort) == (stored, effort)


def test_the_prefixed_reason_stays_within_120_chars():
    c = _parse({"label": "SUBSTANTIVE", "effort": "high", "effort_criterion": "multi-module",
                "reason": "x" * 120})
    assert len(c.reason) == 120 and c.reason.startswith("multi-module: x")


def test_the_named_criterion_is_kept_trimmed_and_absent_means_empty():
    c = _parse({"label": "SUBSTANTIVE", "effort": "high", "effort_criterion": " multi-module ",
                "reason": "x"})
    assert c.effort_criterion == "multi-module" and c.effort == "high"
    assert _parse({"label": "SUBSTANTIVE"}).effort_criterion == ""


def test_the_criterion_match_is_literal_and_reads_any_text():
    class Falsy:
        def __bool__(self):
            return False

        def __str__(self):
            return "multi-module"

    assert classify.leads_with_criterion(Falsy(), "multi-module") is False
    assert classify.leads_with_criterion(None, "multi-module") is False
    assert classify.leads_with_criterion(42, "multi-module") is False
    assert classify.leads_with_criterion("axb", "a.b") is False      # names are escaped
    assert classify.named_effort_criterion(None, "multi-module: x") == "multi-module"


# ── The guidance the classifier is sent ─────────────────────────────────────────

# The exact wording the classifier reads about the fixer's model and effort. It is
# product text that is signed off as a whole, so it is pinned as a whole: any
# edit to it must be deliberate and must update this copy.
_GUIDANCE = (
    'FIX MODEL AND EFFORT — the fixer is Claude Code, so bot is "claude"; '
    "model ∈ {haiku, sonnet, opus}; effort ∈ {low, medium, high, xhigh, max}.\n"
    "For a SUBSTANTIVE comment pick opus — the latest Opus fixes most real flaws at "
    "medium effort.\n"
    "WHY MEDIUM: a fast fix that reviewers from other vendors then check, iterated over "
    "a few quick rounds, beats one long attempt by a single model — those reviewers make "
    "different mistakes from the fixer, so each round catches what the last one missed, "
    "sooner and at lower cost. Spend more effort only where you can name why.\n"
    "EFFORT for a SUBSTANTIVE comment:\n"
    "  low    = trivial edit — a typo, a rename, an obvious one-line correction.\n"
    "  medium = the DEFAULT for every other SUBSTANTIVE fix, including a real bug that "
    "needs a careful read of the code.\n"
    "  high, xhigh, max = ONLY when one of these named criteria holds. Put its name in "
    '"effort_criterion" and begin "reason" with it; a pick above medium that names none '
    "runs at medium:\n"
    "    multi-module — the fix must change several modules or systems together.\n"
    "    subtle-correctness — concurrency, security or state correctness where a "
    "plausible-looking fix is easily wrong: a race, a check-then-act gap, or a bypass the "
    "comment shows has several routes. A single missing case with a local fix is medium, "
    "however severe its consequence.\n"
    "    prior-attempt-failed — an earlier attempt at this same issue failed: a PRIOR "
    "ATTEMPT line shows it, or the comment itself says a fix did not hold (it was "
    "rejected, it broke a test, or the reviewer is re-raising the issue).\n"
    "    deeply-non-obvious — concrete evidence in the comment or its diff hunk shows a "
    "misleading symptom: the reported behaviour is separated from its cause by a "
    "transformation, numerical boundary, grammar ambiguity, or performance interaction, "
    "and the comment identifies why the obvious local correction would be wrong. Naming "
    "an algorithmic, numerical, parsing, or performance issue without that evidence is "
    "not enough.\n"
    "  Among those: high = one criterion holds; xhigh = the change touches several systems "
    "at once or more than one criterion holds; max = a systemic redesign or a deeply "
    "non-obvious bug hunt.\n"
    '  "Needs careful analysis", "non-trivial", "complex", "security-relevant" or a severe '
    "consequence alone is NOT a criterion — that is what medium is for. A PRIOR ATTEMPT "
    "line is the loop's own record of a failed attempt; without one, do not assume an "
    "earlier attempt failed unless the comment itself says so.\n"
    "For every other label, pick the model and effort that match the comment.\n"
    'effort_criterion is "none" unless a SUBSTANTIVE pick is above medium.\n'
)


def test_the_effort_guidance_is_exactly_the_signed_off_wording():
    try:
        got = classify.effort_guidance()
    except Exception as exc:
        raise AssertionError(f"effort_guidance raised {exc!r}") from exc
    assert got == _GUIDANCE


def test_the_classifier_is_sent_the_guidance_above_the_fence():
    sent = []

    def runner(prompt):
        sent.append(prompt)
        return '{"label": "SUBSTANTIVE", "model": "opus", "effort": "medium", "reason": "x"}'

    classify_comment("please fix the null check", runner=runner, path="a.py",
                     diff_hunk="@@ -1,1 +1,1 @@\n-a\n+b")
    prompt = sent[0]
    fence = prompt.index("<<")
    assert _GUIDANCE in prompt[:fence]
    assert ('Reply with ONE JSON object: '
            '{"label","bot","model","effort","effort_criterion","severity","reason"}.') in prompt[:fence]
    assert prompt.index("please fix the null check") > fence
    assert classify.PRIOR_ATTEMPT_LINE not in prompt     # no earlier attempt was passed


def test_the_criteria_list_names_all_four_in_order():
    assert policy.effort_criteria_text().startswith("    multi-module — ")
    names = [ln.split(" — ")[0].strip()
             for ln in policy.effort_criteria_text().splitlines() if ln.strip()]
    assert names == ["multi-module", "subtle-correctness", "prior-attempt-failed",
                     "deeply-non-obvious"]
    assert [n for n, _ in policy.EFFORT_CRITERIA] == names
    assert "WHY MEDIUM" in build_prompt("x")


# ── The effort vocabulary ───────────────────────────────────────────────────────

def test_xhigh_and_max_get_their_own_time_budget(tmp_path):
    import subprocess as sp
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "t"]):
        sp.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    sp.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    sp.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True, capture_output=True)
    seen = {}

    def fixer(prompt, *, model, effort, timeout, cwd):
        seen[effort] = timeout
        return 0, "SKIP: nothing to change"

    for effort in ("xhigh", "max"):
        fix_apply.apply_fix("c", cwd=str(tmp_path), model="opus", effort=effort,
                            runner=fixer, retries=0)
    assert seen == {"xhigh": 1200, "max": 1800}
    assert fix_apply.EFFORT_TIMEOUTS["xhigh"] == 1200
    assert fix_apply.EFFORT_TIMEOUTS["max"] == 1800


def test_the_kernel_effort_taxonomy_has_no_ceiling_below_max():
    from types import SimpleNamespace

    from buddhi.decisions.effort_model import IterationBudget, decide_spend

    try:
        pack = policy.review_policy_pack()
    except Exception as exc:
        raise AssertionError(f"the review policy pack could not be built: {exc!r}") from exc
    tax = pack.effort_taxonomy
    assert tax.levels == ("low", "medium", "high", "xhigh", "max")
    assert tax.ceiling == "max" and tax.clamp("max") == "max"
    for effort in ("xhigh", "max"):
        router = SimpleNamespace(recommend=lambda item, e=effort: SimpleNamespace(
            model="x", effort=e, rationale=""))
        spend = decide_spend(None, pack, router, IterationBudget(5, 5))
        assert (spend.effort, spend.model) == (effort, "opus")


# ── Older-CLI degrade at the --effort boundary ──────────────────────────────────

_OLD_HELP = "  --effort <level>   Effort level for the session (low, medium, high)\n"
_NEW_HELP = "  --effort <level>   Effort level (low, medium, high, xhigh, max)\n"
_THREE = frozenset({"low", "medium", "high"})
_ALL = frozenset({"low", "medium", "high", "xhigh", "max"})


class _Help:
    def __init__(self, stdout, stderr=""):
        self.stdout, self.stderr, self.returncode = stdout, stderr, 0


@pytest.fixture
def fresh_probe(monkeypatch):
    monkeypatch.setattr(model_call, "_supported_efforts_cache", None)
    monkeypatch.setattr(model_call, "_degrade_logged", False)


def _with_help(monkeypatch, text):
    calls = []
    real_probe = model_call.probe_supported_efforts

    def probe():
        calls.append(1)
        return real_probe(run=lambda *a, **k: _Help(text))

    monkeypatch.setattr(model_call, "probe_supported_efforts", probe)
    return calls


def _probe(run):
    """Run the probe with ``run``; a probe that raises is a defect, reported as a
    failed assertion (it must always fail open instead)."""
    try:
        return model_call.probe_supported_efforts(run=run)
    except Exception as exc:
        raise AssertionError(f"the probe raised {exc!r}") from exc


@pytest.mark.parametrize("help_text, want", [
    (_OLD_HELP, _THREE),
    (_NEW_HELP, _ALL),
    # The current CLI wraps the list onto the line after the option.
    ("  --effort <level>        Effort level for the current session\n"
     "                          (low, medium, high, xhigh, max)\n"
     "  --fallback-model <m>    Fallback model (default: sonnet)\n", _ALL),
    ("  --effort <level>        Effort level for the current session\n"
     "                          (low, medium, high)\n", _THREE),
    ("  --EFFORT <LEVEL>  Effort (Low, Medium, High)\n", _THREE),
    ('  --effort <level>  Effort (choices: "low", "medium", "high")\n', _THREE),
    ("  --effort <level>  Effort (low, medium, high, auto)\n", _THREE),
    ("  --effortless-mode   (on, off)\n  --effort <level>  Effort (low, medium, high)\n", _THREE),
    ("  --effort <level>  Effort () ( ) (low, medium, high)\n", _THREE),
    # The option's first list that names low, medium and high is read; its text ends at a
    # blank line.
    ("  --effort <level>  Effort (default: high) (low, medium, high)\n", _THREE),
    ("  --effort <level>  Effort (low, medium, high) (xhigh)\n", _THREE),
    ("  --effort <level>  Effort level (low, medium, high)\n\nCommands:\n"
     "  doctor  Check health (max detail)\n", _THREE),
    ("  --effort <level>  Effort level\n\nCommands:\n"
     "  doctor  Check health (low, medium, high)\n", _ALL),
    # The option's own text ends at the next option: a later option's list is
    # never read as the effort list, and a "--" that starts no option is not one.
    ("  --effort <level>  Effort (low, medium, high)\n  --mode <m>  Mode (fast, xhigh, max)\n",
     _THREE),
    ("  --effort <level>  Effort level\n                    -- (low, medium, high)\n", _THREE),
    # A hyphenated word inside the option's own text does not end it.
    ("  --effort <level>  Effort level for a non-interactive run (low, medium, high)\n", _THREE),
    # Only the --effort option is read, not an earlier one that mentions effort.
    ("  --model <m>  Model for effort runs (default: sonnet)\n"
     "  --effort <level>  Effort (low, medium, high)\n", _THREE),
    # Only parenthesised lists are read, each on its own.
    ("  --effort <level>  Effort (low, medium, high); max is not supported (see docs)\n",
     _THREE),
    # Fail open: no list, a default note, or a list that is not effort levels.
    ("  --effort <level>  Effort level\n  --model <model>  Model (default: sonnet)\n", _ALL),
    ("  --effort <level>  Effort level\n  --thinking <t>  Thinking (low, medium, high)\n", _ALL),
    ("  --effort <level>  Effort level\n  -t <t>  Thinking (low, medium, high)\n", _ALL),
    ("  --effort <level>  Effort level (default: high)\n", _ALL),
    ("  --effort <level>  Effort: low, medium or high\n", _ALL),
    ("  --effort <level>  Effort (medium, high)\n", _ALL),
    ("  --effort <level>  Effort (low, high)\n", _ALL),
    ("  --effort <level>  Effort (low, medium)\n", _ALL),
    ("  --effort <level>  Effort (auto)\n", _ALL),
    ("no such flag", _ALL),
    ("", _ALL),
])
def test_the_probe_reads_the_effort_option_and_fails_open(help_text, want):
    assert _probe(lambda *a, **k: _Help(help_text)) == want


class _Falsy(str):
    """A falsy stream value whose text is still readable."""

    def __bool__(self):
        return False


def test_the_probe_reads_both_streams_as_separate_lines():
    assert _probe(lambda *a, **k: _Help("", stderr=_OLD_HELP)) == _THREE
    assert _probe(lambda *a, **k: _Help(None, stderr=_OLD_HELP)) == _THREE

    class OnlyStdout:
        stdout = _OLD_HELP

    class OnlyStderr:
        stderr = _OLD_HELP

    assert _probe(lambda *a, **k: OnlyStdout()) == _THREE
    assert _probe(lambda *a, **k: OnlyStderr()) == _THREE
    # A falsy stream counts as empty.
    assert _probe(lambda *a, **k: _Help(_Falsy(_OLD_HELP))) == _ALL
    assert _probe(lambda *a, **k: _Help("", stderr=_Falsy(_OLD_HELP))) == _ALL
    # stdout's last line and stderr's first line are not joined into one option.
    assert _probe(lambda *a, **k: _Help(
        "  --effort <level>  Effort level",
        stderr="  --model <m>  Model (low, medium, high)\n")) == _ALL


def test_the_probe_asks_claude_help_quietly_and_with_a_time_limit():
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, **kw)
        return _Help(_OLD_HELP)

    _probe(run)
    assert seen.get("argv") == ["claude", "--help"]
    assert {k: seen.get(k) for k in ("capture_output", "text", "timeout", "stdin")} == {
        "capture_output": True, "text": True, "timeout": 20,
        "stdin": model_call.subprocess.DEVNULL}


def test_the_probe_uses_subprocess_run_by_default(monkeypatch):
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return _Help(_OLD_HELP)

    monkeypatch.setattr(model_call.subprocess, "run", fake_run)
    assert model_call.probe_supported_efforts() == _THREE
    assert seen == [["claude", "--help"]]


@pytest.mark.parametrize("exc", [
    OSError("claude not found"),
    model_call.subprocess.TimeoutExpired(["claude", "--help"], 20),
    model_call.subprocess.SubprocessError("boom"),
    ValueError("bad argument"),
])
def test_a_failed_probe_keeps_every_level(exc):
    def boom(*a, **k):
        raise exc

    assert _probe(boom) == _ALL


def test_an_older_cli_runs_xhigh_and_max_at_high_and_says_so_once(fresh_probe, monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    calls = _with_help(monkeypatch, _OLD_HELP)
    assert model_call.degrade_effort("xhigh") == "high"
    assert model_call.degrade_effort("MAX") == "high"
    assert capsys.readouterr().out == (
        "  [effort] 'xhigh' is not offered by the installed claude CLI "
        "(it offers: low, medium, high); running at 'high'.\n")
    assert len(calls) == 1          # probed once per process


def test_the_degrade_line_is_dim_on_a_terminal(fresh_probe, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("BUDDHI_LOOP_NO_COLOR", raising=False)
    _with_help(monkeypatch, _OLD_HELP)

    class Tty(io.StringIO):
        def isatty(self):
            return True

    out = Tty()
    monkeypatch.setattr(model_call.sys, "stdout", out)
    model_call.degrade_effort("xhigh")
    assert out.getvalue() == (
        "\033[2m  [effort] 'xhigh' is not offered by the installed claude CLI "
        "(it offers: low, medium, high); running at 'high'.\033[0m\n")


def test_the_degrade_line_names_the_effort_as_given(fresh_probe, monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    _with_help(monkeypatch, _OLD_HELP)
    assert model_call.degrade_effort(" XHIGH ") == "high"
    assert "[effort] ' XHIGH ' is not offered" in capsys.readouterr().out


def test_a_current_cli_keeps_xhigh_and_max_as_given(fresh_probe, monkeypatch, capsys):
    _with_help(monkeypatch, _NEW_HELP)
    assert model_call.degrade_effort("xhigh") == "xhigh"
    assert model_call.degrade_effort("max") == "max"
    assert model_call.degrade_effort("XHIGH") == "XHIGH"
    assert capsys.readouterr().out == ""


def test_every_other_value_passes_through_and_never_probes(fresh_probe, monkeypatch, capsys):
    # Only xhigh/max are ever touched: low/medium/high and anything a non-SUBSTANTIVE
    # pick carries reach --effort exactly as before.
    calls = _with_help(monkeypatch, _OLD_HELP)
    values = ["low", "medium", "high", "Low", "Medium", "HIGH", "banana", "", None]
    try:
        got = [model_call.degrade_effort(e) for e in values]
    except Exception as exc:
        raise AssertionError(f"degrade_effort raised {exc!r}") from exc
    assert got == values
    assert calls == [] and capsys.readouterr().out == ""


def _fresh_interpreter(code):
    """Run ``code`` in a new interpreter with this tree on sys.path; return
    (stdout, stderr). The code ends with os._exit, so only output that was
    flushed survives."""
    import pathlib
    import subprocess as sp
    import sys

    root = str(pathlib.Path(__file__).resolve().parents[1])
    prog = f"import os, sys\nsys.path.insert(0, {root!r})\n{code}\n"
    env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "BUDDHI_LOOP_NO_COLOR")}
    env["NO_COLOR"] = "1"
    proc = sp.run([sys.executable, "-c", prog], capture_output=True, text=True, timeout=60,
                  env=env, cwd=root)
    return proc.stdout, proc.stderr


def test_a_new_process_probes_before_its_first_degrade_and_flushes_the_line():
    out, err = _fresh_interpreter(
        "from buddhi_review import model_call as m\n"
        "m.probe_supported_efforts = lambda: frozenset({'low', 'medium', 'high'})\n"
        "r = m.degrade_effort('xhigh')\n"
        "os.write(2, ('RESULT=' + r).encode())\n"
        "os._exit(0)")
    assert "RESULT=high" in err, err
    assert "[effort] 'xhigh' is not offered" in out


def test_the_fixer_argv_carries_the_degraded_effort(fresh_probe, monkeypatch):
    _with_help(monkeypatch, _OLD_HELP)
    seen = {}
    monkeypatch.setattr(fix_apply, "maybe_sandbox", lambda argv, cwd: list(argv))

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return _Help("ok")

    monkeypatch.setattr(fix_apply.subprocess, "run", fake_run)
    try:
        fix_apply.default_fixer_runner("p", model="opus", effort="max", timeout=9, cwd="/wt")
    except Exception as exc:
        raise AssertionError(f"the fixer runner raised {exc!r}") from exc
    argv = seen["argv"]
    assert argv[argv.index("--effort") + 1] == "high"


def test_a_role_call_carries_the_degraded_effort(fresh_probe, monkeypatch):
    _with_help(monkeypatch, _OLD_HELP)
    monkeypatch.setattr(model_call.plan_profile, "effort_for", lambda role, plan=None: "xhigh")
    seen = {}

    def spawn(argv, stdin_text, timeout):
        seen["argv"] = argv
        return _Help("{}")

    try:
        model_call.run_model_text("p", role="classifier", spawn=spawn)
    except Exception as exc:
        raise AssertionError(f"the role call raised {exc!r}") from exc
    argv = seen["argv"]
    assert argv[argv.index("--effort") + 1] == "high"


# ── The dispatch boundary ───────────────────────────────────────────────────────

def _dispatch_with(monkeypatch, classification, plan):
    seen = {}

    def fake_apply_fix(text, **kw):
        seen.update(kw)
        return fix_apply.FixOutcome(status="applied")

    monkeypatch.setattr(actuators, "apply_fix", fake_apply_fix)
    comment = Comment(id="c1", text="fix it", source="reviewer", path="a.py",
                      diff_hunk="@@ -1,1 +7,1 @@\n+x")
    result = CommentResult(comment_id="c1", classification=classification,
                           kernel_status="model", disposition="fix")
    actuators.default_fix_dispatch(cwd="/wt", plan=plan)(comment, result)
    return seen


@pytest.mark.parametrize("plan, model", [("max-5x", "opus"), ("max-20x", "opus"), ("pro", "sonnet")])
def test_the_substantive_default_reaches_the_fixer_through_the_plan(monkeypatch, plan, model):
    seen = _dispatch_with(monkeypatch, _parse({"label": "SUBSTANTIVE"}), plan)
    assert (seen["model"], seen["effort"]) == (model, "medium")


def test_a_cosmetic_default_still_reaches_the_fixer_as_before(monkeypatch):
    seen = _dispatch_with(monkeypatch, _parse({"label": "COSMETIC"}), "max-5x")
    assert (seen["model"], seen["effort"]) == ("sonnet", "high")


def test_a_held_pick_is_announced_before_the_fix(monkeypatch, capsys):
    held = _parse({"label": "SUBSTANTIVE", "effort": "xhigh", "reason": "complex"})
    seen = _dispatch_with(monkeypatch, held, "max-5x")
    assert seen["effort"] == "medium"
    assert ("[effort] a.py:7 — SUBSTANTIVE pick 'xhigh' names no criterion for "
            "spending more than medium; running at 'medium'.") in capsys.readouterr().out


def test_the_held_line_comes_before_the_fixer_runs(monkeypatch, capsys):
    def fake_apply_fix(text, **kw):
        print("FIXER-RAN")
        return fix_apply.FixOutcome(status="applied")

    monkeypatch.setattr(actuators, "apply_fix", fake_apply_fix)
    comment = Comment(id="c1", text="fix it", path="a.py", diff_hunk="@@ -1,1 +7,1 @@\n+x")
    result = CommentResult(comment_id="c1", kernel_status="model", disposition="fix",
                           classification=_parse({"label": "SUBSTANTIVE", "effort": "max",
                                                  "reason": "complex"}))
    actuators.default_fix_dispatch(cwd="/wt", plan="max-5x")(comment, result)
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["  [effort] a.py:7 — SUBSTANTIVE pick 'max' names no criterion for "
                     "spending more than medium; running at 'medium'.", "FIXER-RAN"]


def test_a_pr_description_comment_is_never_announced(monkeypatch, capsys):
    # The PR-body branch returns before the code fixer, and before the held line.
    comment = Comment(id="c1", text="reword the description")
    result = CommentResult(comment_id="c1", kernel_status="model", disposition="fix",
                           classification=Classification(label="PR_DESCRIPTION",
                                                         held_effort="xhigh"))
    out = actuators.default_fix_dispatch(cwd="/wt", plan="max-5x",
                                         fix_pr_description=False)(comment, result)
    assert out.status == "skipped" and capsys.readouterr().out == ""


def test_a_pick_that_was_not_held_is_not_announced(monkeypatch, capsys):
    _dispatch_with(monkeypatch, _parse({"label": "SUBSTANTIVE", "effort": "medium"}), "max-5x")
    assert "[effort]" not in capsys.readouterr().out


def test_the_held_line_is_returned_and_reaches_the_log_at_once():
    comment = Comment(id="c", text="t", path="a.py", diff_hunk="@@ -1,1 +7,1 @@\n+x")
    want = ("  [effort] a.py:7 — SUBSTANTIVE pick 'xhigh' names no criterion for "
            "spending more than medium; running at 'medium'.")
    assert actuators.held_effort_line(comment, "xhigh") == want
    bare = Comment(id="c", text="t")
    assert actuators.held_effort_line(bare, "max") == (
        "  [effort] ?:? — SUBSTANTIVE pick 'max' names no criterion for spending more "
        "than medium; running at 'medium'.")
    out, err = _fresh_interpreter(
        "from buddhi_review.actuators import held_effort_line\n"
        "from buddhi_review.loop import Comment\n"
        "held_effort_line(Comment(id='c', text='t', path='a.py', "
        "diff_hunk='@@ -1,1 +7,1 @@\\n+x'), 'xhigh')\n"
        "os._exit(0)")
    assert want in out, err


# ── The plan table ──────────────────────────────────────────────────────────────

def test_the_substantive_role_row_is_opus_at_medium_and_the_rest_is_unchanged():
    assert plan_profile.model_for("fix-substantive", "max-5x") == "opus"
    assert plan_profile.effort_for("fix-substantive") == "medium"
    assert plan_profile.model_for("fix-substantive", "pro") == "sonnet"   # Pro has no Opus
    assert plan_profile.model_for("classifier", "max-5x") == "sonnet"
    assert plan_profile.effort_for("classifier") == "medium"
    for role in ("fix-verify", "fix-cosmetic", "clean-review-detector", "quota-detector"):
        assert plan_profile.model_for(role, "max-5x") == "haiku"
        assert plan_profile.effort_for(role) == "low"


def test_the_setup_wizard_shows_the_fixer_model_per_plan():
    from buddhi_review import wizard

    seen = {}

    def pick(prompt, options, **kw):
        seen["options"] = dict(options)
        return 0

    wizard.step_plan({"tiers": {}}, pal=wizard._Palette(False), stream=io.StringIO(),
                     single_select=pick)
    assert seen["options"] == {"pro": "fixes run sonnet", "max-5x": "fixes run opus",
                               "max-20x": "fixes run opus"}
