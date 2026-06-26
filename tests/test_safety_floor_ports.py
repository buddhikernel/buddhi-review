"""Safety-floor parity ports (build-spec §5 manifest) — the A5 tripwire cases not
already pinned in ``test_fix_apply.py`` / ``test_fix_apply_hardening.py``, plus the
#294 tmp-isolation harness-hygiene guard.

The §5 safety floor (A1 empirical-verify golden, A4 verify CONFIRM/REJECT/fail-open,
the classifier-handoff Phase-1 byte-identical golden, and the clean-review +
``No issues found.`` sentinel coupling) is already pinned in this suite — see
``test_fix_apply.py`` (A1 golden, handoff golden, A4 gating), ``test_fix_apply_hardening.py``
(A4 stdout verdicts, A5 ``*_FLAGS`` reason), and ``test_detectors.py`` (sentinel
coupling). This file closes the remaining named A5 cases so the whole §5 manifest
is covered, and adds the harness-hygiene guard.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from buddhi_review import fix_apply, notifier
from buddhi_review.fix_apply import diff_tripwire


# ===========================================================================
# A5 — dangerous-change tripwire: the remaining named reference cases
# ===========================================================================

def test_a5_whole_test_file_deletion_trips():
    # A fixer that deletes a whole test file ("+++ /dev/null", every line a "-def
    # test_…" removal) trips the "removes a test" predicate — a fix can never
    # delete a test to make a point pass.
    diff = (
        "--- a/tests/test_widget.py\n"
        "+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n"
        "-def test_widget_renders():\n"
        "-    assert render() == 'ok'\n"
    )
    reason = diff_tripwire(diff)
    assert reason is not None
    assert "removes a test" in reason


def test_a5_empty_or_unavailable_diff_never_trips():
    # An empty / None / placeholder diff has no +/- content, so it can never trip.
    for d in ("", None, "(round diff unavailable)", "(round diff empty)\n"):
        assert diff_tripwire(d) is None, f"unexpected trip on {d!r}"


def test_a5_outside_threshold_is_env_tunable(monkeypatch):
    # The default sprawl threshold reads BUDDHI_FIX_TRIPWIRE_OUTSIDE_LINES (the
    # single source the fixer reads), floored at 1. Pin the env seam directly so
    # the test does not depend on import-time evaluation.
    assert fix_apply._env_int("BUDDHI_FIX_TRIPWIRE_OUTSIDE_LINES", 40, floor=1) == 40

    monkeypatch.setenv("BUDDHI_FIX_TRIPWIRE_OUTSIDE_LINES", "5")
    assert fix_apply._env_int("BUDDHI_FIX_TRIPWIRE_OUTSIDE_LINES", 40, floor=1) == 5
    monkeypatch.setenv("BUDDHI_FIX_TRIPWIRE_OUTSIDE_LINES", "garbage")
    assert fix_apply._env_int("BUDDHI_FIX_TRIPWIRE_OUTSIDE_LINES", 40, floor=1) == 40
    monkeypatch.setenv("BUDDHI_FIX_TRIPWIRE_OUTSIDE_LINES", "0")  # below the floor
    assert fix_apply._env_int("BUDDHI_FIX_TRIPWIRE_OUTSIDE_LINES", 40, floor=1) == 1


def test_a5_unknown_commented_path_skips_the_outside_condition():
    # With no commented file given, the "lines outside the commented region" check
    # is skipped entirely — only the structural predicates (flags / assertions /
    # tests) can trip. A large benign diff in one file does NOT trip.
    diff = "+++ b/app/big.py\n" + "".join(f"+    line_{i} = {i}\n" for i in range(200))
    assert diff_tripwire(diff) is None              # no commented_files → no outside check
    assert diff_tripwire(diff, commented_files=("app/other.py",),
                         outside_limit=40) is not None  # now the sprawl trips


# ===========================================================================
# #294 — tmp-isolation harness hygiene
# ===========================================================================

def test_answer_file_honours_the_tmp_seam_not_a_hardcoded_path(tmp_path, monkeypatch):
    # The console answer-file path is resolved through BUDDHI_REVIEW_TMP, so the
    # suite never writes to a shared temp location and is portable. #294: pytest's
    # tmp_path is /var/folders on macOS and /tmp on Linux CI, so a test must NEVER
    # assert a "/tmp/…" prefix — it asserts isolation UNDER the configured seam.
    monkeypatch.setenv("BUDDHI_REVIEW_TMP", str(tmp_path))
    p = notifier._answer_path("c1")
    assert p == tmp_path / "review-answer-local-c1.md"
    assert Path(p).resolve().is_relative_to(tmp_path.resolve())  # under the seam dir


def test_answer_file_round_trip_writes_only_under_the_seam(tmp_path, monkeypatch):
    # Sending an ask creates exactly one answer file, and it lands under the seam
    # dir — never in the process-wide tempdir.
    monkeypatch.setenv("BUDDHI_REVIEW_TMP", str(tmp_path))
    n = notifier.ConsoleNotifier()
    ask = notifier.Ask(id="hyg1", question="Proceed?", options=["Yes", "No"],
                       recommended_index=0)
    n.send(ask)
    written = list(tmp_path.glob("review-answer-*.md"))
    assert written == [tmp_path / "review-answer-local-hyg1.md"]
    # nothing leaked into the system tempdir under this id
    assert not (Path(tempfile.gettempdir()) / "review-answer-local-hyg1.md").exists()
