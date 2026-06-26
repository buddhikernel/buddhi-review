"""Classifier parsing + dispatch — no ``claude`` binary, no network."""
from __future__ import annotations

from buddhi_review.classify import (
    CLASSIFICATION_FAILED,
    Classification,
    classify_comment,
    parse_classification,
)


def test_json_first():
    c = parse_classification('{"label":"SUBSTANTIVE","bot":"claude","model":"sonnet","effort":"high","reason":"npe"}')
    assert c is not None
    assert c.tuple4 == ("SUBSTANTIVE", "claude", "sonnet", "high")
    assert c.reason == "npe"


def test_json_in_code_fence():
    c = parse_classification('```json\n{"label":"COSMETIC"}\n```')
    assert c is not None and c.label == "COSMETIC"


def test_legacy_pipe_fallback():
    c = parse_classification("BUSINESS_QUESTION|claude|opus|high|should we drop it")
    assert c is not None
    assert c.label == "BUSINESS_QUESTION"
    assert c.bot == "claude" and c.model == "opus" and c.effort == "high"


def test_decoy_label_outside_six_is_absent():
    # A JSON object whose label is not one of the six must be treated as absent,
    # falling through to the priority scan (which finds nothing here).
    assert parse_classification('{"label":"DELETE_EVERYTHING"}') is None


def test_priority_scan_last_resort():
    c = parse_classification("the model rambled but mentioned this is INVALID somewhere")
    assert c is not None and c.label == "INVALID"


def test_reason_clamped_to_120():
    c = parse_classification('{"label":"SUBSTANTIVE","reason":"' + "x" * 500 + '"}')
    assert c is not None and len(c.reason) == 120


def test_classify_comment_failure_yields_classification_failed():
    # A runner that always returns garbage → CLASSIFICATION_FAILED after retries.
    calls = {"n": 0}

    def runner(prompt: str) -> str:
        calls["n"] += 1
        return "no label here at all"

    c = classify_comment("anything", runner=runner, retries=1)
    assert c.label == CLASSIFICATION_FAILED
    assert calls["n"] == 2  # initial + 1 retry


def test_classify_comment_runner_raise_is_caught():
    def runner(prompt: str) -> str:
        raise RuntimeError("claude exploded")

    c = classify_comment("x", runner=runner, retries=0)
    assert c.label == CLASSIFICATION_FAILED


def test_classify_comment_happy_path_no_retry():
    def runner(prompt: str) -> str:
        return '{"label":"OUTDATED"}'

    c = classify_comment("x", runner=runner)
    assert c.label == "OUTDATED"
