"""The default business-question answer window is 24 hours.

``ANSWER_TIMEOUT`` is read from ``BUDDHI_BIZQ_ANSWER_TIMEOUT`` at import time,
so each test reloads the module under the environment it sets, and the module
is reloaded once more under the real environment afterwards.
"""

import importlib

import pytest

from buddhi_review import escalation_wait

_ENV = "BUDDHI_BIZQ_ANSWER_TIMEOUT"


@pytest.fixture(autouse=True)
def _reload_under_real_env(monkeypatch):
    yield
    monkeypatch.undo()
    importlib.reload(escalation_wait)


def _reload(monkeypatch, value=None):
    if value is None:
        monkeypatch.delenv(_ENV, raising=False)
    else:
        monkeypatch.setenv(_ENV, value)
    return importlib.reload(escalation_wait)


def test_default_answer_window_is_24_hours(monkeypatch):
    assert _reload(monkeypatch).ANSWER_TIMEOUT == 86400.0


def test_env_overrides_the_default(monkeypatch):
    assert _reload(monkeypatch, "3600").ANSWER_TIMEOUT == 3600.0


def test_unparseable_env_falls_back_to_the_default(monkeypatch):
    assert _reload(monkeypatch, "soon").ANSWER_TIMEOUT == 86400.0
