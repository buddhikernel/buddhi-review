"""The four-verb adapter contract + the concrete seams."""
from __future__ import annotations

from buddhi.adapter import Adapter, Budget
from buddhi.stage0.conditioning import RawItem

from types import SimpleNamespace

from buddhi_review.adapter import ReviewAdapter
from buddhi_review.seams import ReviewRouter, ReviewStore, SignaledOOBSource


def test_review_adapter_satisfies_the_kernel_contract():
    assert isinstance(ReviewAdapter(), Adapter)  # runtime-checkable four-verb Protocol


def test_ingest_yields_the_injected_source():
    items = (RawItem(id="a", payload="x"), RawItem(id="b", payload="y"))
    adapter = ReviewAdapter(ingest_source=lambda: items)
    assert tuple(adapter.ingest()) == items


def test_detect_resolved_is_signaled_only():
    handled = {"b"}
    adapter = ReviewAdapter(oob_source=SignaledOOBSource(signal=lambda it: it.id in handled))
    assert adapter.detect_resolved(RawItem(id="a", payload="x")) is False
    assert adapter.detect_resolved(RawItem(id="b", payload="y")) is True


def test_two_tier_exclusion_lattice_and_errored_comeback():
    store = ReviewStore()
    # errored → transient → retractable (the comeback)
    store.exclude_errored("gemini")
    assert store.is_excluded("gemini")
    store.errored_comeback("gemini")
    assert not store.is_excluded("gemini")
    # quota → permanent → a transient retraction must NOT cross into it
    store.exclude_quota("copilot")
    assert store.is_excluded("copilot")
    store.errored_comeback("copilot")  # no-op on the permanent tier
    assert store.is_excluded("copilot"), "a quota cap must survive an errored comeback"


def test_review_router_recommends_canonical_tier_aliases():
    """recommend() maps stakes to a canonical tier ALIAS (opus/sonnet/haiku) paired
    with the role effort — never the non-canonical legacy ``claude-{effort}`` form."""
    router = ReviewRouter()
    high = router.recommend(SimpleNamespace(stakes=0.9))
    mid = router.recommend(SimpleNamespace(stakes=0.5))
    low = router.recommend(SimpleNamespace(stakes=0.1))
    assert (high.model, high.effort) == ("opus", "high")
    assert (mid.model, mid.effort) == ("sonnet", "medium")
    assert (low.model, low.effort) == ("haiku", "low")
    # Band boundaries resolve to the higher tier (>= thresholds).
    assert router.recommend(SimpleNamespace(stakes=0.7)).model == "opus"
    assert router.recommend(SimpleNamespace(stakes=0.4)).model == "sonnet"
    # Guard against a regression back to the non-canonical "claude-{effort}" string.
    assert not high.model.startswith("claude-")
    # Defensive checks for None or missing stakes
    assert router.recommend(None).model == "haiku"
    assert router.recommend(SimpleNamespace()).model == "haiku"


def test_run_embedded_returns_a_policy_result():
    adapter = ReviewAdapter()
    raw = RawItem(id="c", payload="missing null check", model_confidence=0.9, changes=("substantive",))
    outcome = adapter.run_embedded(raw, Budget(rounds_remaining=3, max_rounds=3))
    assert outcome.item_id == "c"
    assert outcome.status  # a non-empty kernel disposition
