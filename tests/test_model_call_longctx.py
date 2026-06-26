"""The ``[1m]`` long-context escalation `[model]` line.

A prompt-driven escalation to the ``[1m]`` selector is a per-call DECISION, not
config, so it logs one dim ``[model]`` line. The line names only the single
long-context model chosen for the call.
"""
import io

from buddhi_review import model_call, plan_profile


def _big_prompt() -> str:
    # 2 tokens past the 160K-token threshold (~4 chars/token) → escalation.
    return "x" * (plan_profile.LONG_CONTEXT_TOKEN_THRESHOLD * plan_profile._CHARS_PER_TOKEN + 8)


def test_resolve_model_emits_model_line_on_escalation(capsys):
    model = model_call.resolve_model(_big_prompt(), role="classifier", plan="max-5x")
    out = capsys.readouterr().out
    assert model == "sonnet[1m]"
    assert "[model] classifier: large prompt" in out
    assert "sonnet[1m]" in out
    assert "K tokens)" in out


def test_resolve_model_silent_for_small_prompt(capsys):
    model = model_call.resolve_model("a small prompt", role="classifier", plan="max-5x")
    out = capsys.readouterr().out
    assert model == "sonnet"
    assert out == ""  # no escalation → no line


def test_emit_long_context_returns_uncoloured_body():
    body = model_call._emit_long_context("classifier", "x" * 8000, "sonnet[1m]")
    assert body.startswith("  [model] classifier: large prompt")
    assert "sonnet[1m]" in body
    assert "\033" not in body  # the returned (asserted-on) body is never coloured


def test_token_estimate_in_line_matches_estimator(capsys):
    prompt = _big_prompt()
    model_call.resolve_model(prompt, role="quota-detector", plan="max-5x")
    out = capsys.readouterr().out
    expected_k = plan_profile.estimated_tokens(prompt) // 1000
    assert f"(≈{expected_k}K tokens)" in out


def test_dim_enabled_respects_no_color(monkeypatch):
    class _TTY(io.StringIO):
        def isatty(self):
            return True
    tty = _TTY()
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("BUDDHI_LOOP_NO_COLOR", raising=False)
    assert model_call._dim_enabled(tty) is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert model_call._dim_enabled(tty) is False
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("BUDDHI_LOOP_NO_COLOR", "1")
    assert model_call._dim_enabled(tty) is False
    assert model_call._dim_enabled(io.StringIO()) is False  # non-tty never coloured


def test_long_context_line_under_no_color_has_no_ansi(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    model_call.resolve_model(_big_prompt(), role="classifier", plan="max-5x")
    out = capsys.readouterr().out
    assert "[model]" in out and "\033[" not in out
