"""Plan→role→model/effort resolution + the one model-JSON skeleton."""
import json
import subprocess

import pytest

from buddhi_review import model_call, plan_profile


# ---------------------------------------------------------------------------
# plan_profile — the free role table (effort is plan-independent)
# ---------------------------------------------------------------------------

def test_free_role_table():
    assert plan_profile.model_for("classifier", "max-5x") == "sonnet"
    assert plan_profile.effort_for("classifier") == "medium"
    for role in ("clean-review-detector", "quota-detector", "fix-verify", "fix-cosmetic"):
        assert plan_profile.effort_for(role) == "low"
    assert plan_profile.model_for("fix-substantive", "max-5x") == "sonnet"
    assert plan_profile.effort_for("fix-substantive") == "high"


def test_unknown_role_falls_back():
    assert plan_profile.model_for("no-such-role", "max-5x") == "sonnet"
    assert plan_profile.effort_for("no-such-role") == plan_profile.DEFAULT_EFFORT


def test_pro_plan_downmaps_opus():
    assert plan_profile.tier_model("opus", "pro") == "sonnet"
    assert plan_profile.tier_model("opus", "max-5x") == "opus"
    assert plan_profile.tier_model("haiku", "pro") == "haiku"
    assert plan_profile.tier_model("opus", "unknown-plan") == "opus"  # pass-through


def test_env_plan_wins(monkeypatch):
    monkeypatch.setenv("BUDDHI_LOOP_PLAN", "pro")
    assert plan_profile.active_plan({"plan": "max-5x"}) == "pro"
    monkeypatch.delenv("BUDDHI_LOOP_PLAN")
    assert plan_profile.active_plan({"plan": "max-20x"}) == "max-20x"
    assert plan_profile.active_plan({}) == "max-5x"


def test_profiles_ship_with_the_package():
    assert plan_profile.PROFILES_PATH.exists()
    assert plan_profile.PROFILES_PATH.parent.name == "buddhi_review"


def test_long_context_threshold_boundary():
    at_threshold = "x" * (plan_profile.LONG_CONTEXT_TOKEN_THRESHOLD * 4)
    assert not plan_profile.needs_long_context(at_threshold)      # exactly 160K → no
    assert plan_profile.needs_long_context(at_threshold + "xxxx")  # 160K+1 → yes
    assert plan_profile.long_context_model("sonnet") == "sonnet[1m]"
    assert plan_profile.long_context_model("sonnet[1m]") == "sonnet[1m]"  # idempotent


# ---------------------------------------------------------------------------
# model_call — argv contract, retry, [1m] escalation, stdin mode
# ---------------------------------------------------------------------------

def _cp(argv, rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr)


def test_argv_contract_small_prompt():
    argv, stdin_text = model_call.build_argv("p", model="sonnet", effort="medium")
    assert argv == [
        "claude", "--model", "sonnet", "--effort", "medium",
        "--no-session-persistence", "--strict-mcp-config", "-p", "p",
    ]
    assert stdin_text is None


def test_argv_stdin_mode_for_oversized_prompt():
    big = "x" * (model_call.STDIN_THRESHOLD + 1)
    argv, stdin_text = model_call.build_argv(big, model="sonnet", effort="low")
    assert argv[-1] == "--print" and "-p" not in argv
    assert stdin_text == big


def test_resolve_model_escalates_1m_only_past_threshold():
    small = "x"
    big = "x" * (plan_profile.LONG_CONTEXT_TOKEN_THRESHOLD * 4 + 4)
    assert model_call.resolve_model(small, role="classifier", plan="max-5x") == "sonnet"
    assert model_call.resolve_model(big, role="classifier", plan="max-5x") == "sonnet[1m]"


def test_run_model_json_retries_then_succeeds():
    outputs = ["not json at all", json.dumps({"label": "COSMETIC"})]
    spawns, sleeps = [], []
    def spawn(argv, stdin_text, timeout):
        spawns.append(argv)
        return _cp(argv, 0, stdout=outputs[len(spawns) - 1])
    obj = model_call.run_model_json(
        "p", role="classifier", plan="max-5x", spawn=spawn,
        retries=1, sleep=sleeps.append,
    )
    assert obj == {"label": "COSMETIC"}
    assert len(spawns) == 2
    assert sleeps == [model_call.RETRY_DELAY]


def test_run_model_json_none_after_exhausted_retries():
    spawns = []
    def spawn(argv, stdin_text, timeout):
        spawns.append(argv)
        return _cp(argv, 1, stderr="boom")
    obj = model_call.run_model_json(
        "p", role="classifier", spawn=spawn, retries=2, sleep=lambda s: None,
    )
    assert obj is None
    assert len(spawns) == 3  # retries+1, same model every time


def test_text_runner_raises_so_callers_own_retry_policy():
    runner = model_call.text_runner(
        "fix-verify", plan="max-5x",
        spawn=lambda argv, stdin_text, timeout: _cp(argv, 1, stderr="down"),
    )
    with pytest.raises(RuntimeError):
        runner("p")
    ok = model_call.text_runner(
        "fix-verify", plan="max-5x",
        spawn=lambda argv, stdin_text, timeout: _cp(argv, 0, stdout="OUT"),
    )
    assert ok("p") == "OUT"


def test_classifier_cwd_pins_subprocess_to_target_repo(monkeypatch):
    # Detached launch (review-pr --cwd from another checkout): the classifier
    # subprocess must run IN the target repo, not inherit the launcher's cwd, so
    # its "running inside the repository" escalation criteria actually hold.
    seen = {}

    def fake_run(argv, **kwargs):
        seen["cwd"] = kwargs.get("cwd")
        return _cp(argv, 0, stdout="{}")

    monkeypatch.setattr(model_call.subprocess, "run", fake_run)
    model_call.text_runner("classifier", plan="max-5x", cwd="/target/repo")("p")
    assert seen["cwd"] == "/target/repo"
    # no cwd → inherit the process cwd (the in-checkout launch — unchanged behaviour)
    seen.clear()
    model_call.text_runner("classifier", plan="max-5x")("p")
    assert seen["cwd"] is None


def test_role_effort_rides_argv():
    seen = {}
    def spawn(argv, stdin_text, timeout):
        seen["argv"] = argv
        return _cp(argv, 0, stdout="{}")
    model_call.run_model_json("p", role="clean-review-detector", plan="max-5x",
                              spawn=spawn, retries=0, sleep=lambda s: None)
    i = seen["argv"].index("--effort")
    assert seen["argv"][i + 1] == "low"
    assert seen["argv"][seen["argv"].index("--model") + 1] == "haiku"
