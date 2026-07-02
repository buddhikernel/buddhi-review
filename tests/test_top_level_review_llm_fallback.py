"""Top-level review bodies get the SAME conservative LLM clean-fallback as
inline comments.

A reviewer usually posts its "I'm done" summary as a top-level review body
(``pulls/<pr>/reviews``), not an inline comment (``pulls/<pr>/comments``). Both
surfaces are ingested through the one ``fetch_comments`` source and flow through
the one ``_classify_signal`` → ``detect_clean_review(..., llm_json=clean_llm)``
path — so a clean verdict only the model can confirm promotes the bot to
voluntarily-done regardless of which surface it arrived on. These tests pin that
the review-body surface is ingested AND that the round driver consults the LLM
seam on it.
"""
import json
import subprocess

from buddhi_review import detectors, gh_ingest
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.loop import Comment
from buddhi_review.round_driver import RoundDriver, RoundTimes
from buddhi_review.seams import ConsoleEscalation

# A clean approval the deterministic regex tier does NOT recognise — only the
# LLM tier can confirm it. Used to prove the seam is actually consulted.
LLM_ONLY_CLEAN = "Reviewed the whole change end to end; it all reads correctly and is ready."

CLAUDE_ONLY = {"active_reviewers": ["claude"], "auto_on_open": {"claude": False}}


# ---------------------------------------------------------------------------
# Ingest: a top-level review body becomes a Comment that reaches clean detection
# ---------------------------------------------------------------------------

def test_review_body_is_ingested_and_reaches_clean_detection(monkeypatch):
    # Shape of a pulls/<pr>/reviews entry: carries submitted_at, no `path`.
    raw = json.dumps([{
        "id": 9001,
        "body": LLM_ONLY_CLEAN,
        "user": {"login": "gemini-code-assist[bot]"},
        "submitted_at": "2026-06-21T00:00:00Z",
    }])
    monkeypatch.setenv(gh_ingest.COMMENTS_JSON_ENV, raw)

    comments = gh_ingest.fetch_comments("7", repo="o/r")
    assert len(comments) == 1
    c = comments[0]
    assert c.path is None                      # top-level review body, not inline
    assert c.created_at == "2026-06-21T00:00:00Z"   # submitted_at → created_at
    assert detectors.bot_for_login(c.source) == "gemini"

    # Tier 1 misses this wording; the LLM seam confirms it — same seam the
    # inline-comment path uses.
    assert not detectors.is_clean_review(c.text)
    calls = []
    assert detectors.detect_clean_review(
        c.text, llm_json=lambda p: calls.append(p) or {"clean": True})
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Round driver: a review body promotes the bot to done via the LLM seam
# ---------------------------------------------------------------------------

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


def _gh(argv, *, cwd=None, timeout=None):
    out = " M x.py\n" if argv[:3] == ["git", "status", "--porcelain"] else ""
    return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


def test_round_driver_consults_llm_seam_on_a_review_body():
    # A top-level review body (no path, carries submitted_at→created_at) whose
    # clean status only the LLM can determine.
    review = Comment(id="rv1", text=LLM_ONLY_CLEAN, source="claude[bot]",
                     path=None, created_at="2026-06-21T00:00:00Z")
    clock = _Clock()
    llm_calls = []

    def clean_llm(prompt):
        llm_calls.append(prompt)
        return {"clean": True}

    adapter = ReviewAdapter(escalation=ConsoleEscalation(notifier=_Notifier()))
    driver = RoundDriver(
        "7", repo="o/r", cwd="/nonexistent", cfg=CLAUDE_ONLY, adapter=adapter,
        classify_runner=lambda prompt: json.dumps({"label": "INVALID", "reason": "t"}),
        clean_llm=clean_llm,
        fetch=lambda pr, repo=None, cwd=None: [review],
        gh_run=_gh, clock=clock, sleep=clock.sleep, notice=lambda *a, **k: "",
        # register_delay=0 isolates the definitive-signal timing from the
        # post-summon register delay (which this test isn't exercising).
        times=RoundTimes(quiescence=60, poll_interval=30, min_bot_wait=420,
                         idle_timeout=900, max_wait_total=1800, register_delay=0),
    )

    outcome = driver.run()

    assert outcome.status == "clean" and outcome.rounds == 1
    assert "claude" in driver.done          # promoted to voluntarily-done...
    assert len(llm_calls) >= 1              # ...by the conservative LLM seam
    assert clock.t < 60                     # a definitive signal — no silence wait
