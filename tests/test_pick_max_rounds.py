"""Diff-size → round-budget scaling (uncapped) + the budget resolver.

`pick_max_rounds` scales the review→fix round budget with the size of the PR
diff: +1 round per doubling of changed lines, floored at 2 and with NO upper
cap, so a larger change earns proportionally more rounds.

Bucket schedule (floor 2, +1 per doubling, no ceiling):
    <25            → 2   (one fix round + one confirmation round)
    25 – <50       → 2
    50 – <100      → 3
    100 – <200     → 4
    200 – <400     → 5
    400 – <800     → 6
    800 – <1600    → 7
    1600 – <3200   → 8
    3200 – <6400   → 9
    6400 – <12800  → 10
    12800 – <25600 → 11
    …              (keeps climbing; a 1M-line diff → 17)
"""
from __future__ import annotations

import math

import pytest

from buddhi_review import round_driver
from buddhi_review.round_driver import pick_max_rounds, resolve_max_rounds


@pytest.mark.parametrize(
    "lines,expected",
    [
        (0, 2), (24, 2), (25, 2), (49, 2),
        (50, 3), (99, 3),
        (100, 4), (199, 4),
        (200, 5), (399, 5),
        (400, 6), (799, 6),
        (800, 7), (1599, 7),
        (1600, 8), (3199, 8),
        (3200, 9), (6399, 9),
        (6400, 10), (12799, 10),
        # No upper cap: the budget keeps climbing past 10 as the diff grows.
        (12800, 11),
        (25600, 12),
        (51200, 13),
        (100_000, 13),
        (1_000_000, 17),
    ],
)
def test_bucket_boundaries(lines, expected):
    assert pick_max_rounds(lines) == expected


def test_no_upper_cap_strictly_increasing_past_ten():
    # Proof the size→rounds scaling is uncapped: it keeps rising past the old
    # ceiling of 10 across successive doublings.
    assert pick_max_rounds(6_400) == 10
    assert pick_max_rounds(12_800) == 11
    assert pick_max_rounds(25_600) == 12
    assert pick_max_rounds(204_800) > 10


def test_floor_is_two_for_small_or_negative_or_nan():
    for lines in (0, 1, 24, -100, float("nan")):
        assert pick_max_rounds(lines) == 2


def test_negative_infinity_returns_floor():
    assert pick_max_rounds(float("-inf")) == 2


def test_positive_infinity_maps_to_high_backstop_not_crash():
    # +inf can't be a real diff size; with the cap removed it must map to a high
    # defensive backstop rather than crash on floor(log2(inf)).
    assert pick_max_rounds(math.inf) == 100


def test_huge_int_overflow_maps_to_backstop():
    # A diff size so large it overflows float coercion is treated like +inf, not
    # silently collapsed to the floor.
    assert pick_max_rounds(10 ** 400) == 100


def test_unparseable_size_returns_floor():
    assert pick_max_rounds("not a number") == 2
    assert pick_max_rounds(None) == 2


# --------------------------------------------------------------- resolver order


def test_explicit_value_wins_over_env_and_diff(monkeypatch):
    monkeypatch.setenv("BUDDHI_MAX_ROUNDS", "4")
    assert resolve_max_rounds(7, diff_lines=100_000) == 7


def test_explicit_is_floored_to_one(monkeypatch):
    monkeypatch.delenv("BUDDHI_MAX_ROUNDS", raising=False)
    assert resolve_max_rounds(0) == 1
    assert resolve_max_rounds(-3) == 1


def test_env_used_when_no_explicit(monkeypatch):
    monkeypatch.setenv("BUDDHI_MAX_ROUNDS", "6")
    assert resolve_max_rounds(None, diff_lines=100_000) == 6


def test_env_invalid_falls_through_to_diff(monkeypatch):
    monkeypatch.setenv("BUDDHI_MAX_ROUNDS", "not-an-int")
    assert resolve_max_rounds(None, diff_lines=800) == pick_max_rounds(800) == 7


def test_diff_auto_size_when_no_explicit_or_env(monkeypatch):
    monkeypatch.delenv("BUDDHI_MAX_ROUNDS", raising=False)
    assert resolve_max_rounds(None, diff_lines=6400) == 10


def test_fallback_when_diff_size_unknown(monkeypatch):
    monkeypatch.delenv("BUDDHI_MAX_ROUNDS", raising=False)
    assert resolve_max_rounds(None, diff_lines=None) == round_driver.MAX_ROUNDS_FALLBACK == 10
