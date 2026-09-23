"""Every link the free package shows a prospective or paying user goes to ONE host.

The commercial site is buddhireview.com. On 2026-09-20 three modules were found
sending users to a different domain — the unclaimed-command notice (``cli.py``), the
checkout pointer (``pro_trial.py``) and the upgrade nudge (``upsell.py``) — each with
its own hard-coded literal, so nothing noticed when the commercial domain changed.
These two tests close that class: no module may name the retired host, and the three
surfaces must agree with each other.
"""
from pathlib import Path
from urllib.parse import urlparse

import buddhi_review
from buddhi_review import cli, pro_trial, upsell

_PKG = Path(buddhi_review.__file__).resolve().parent
_COMMERCIAL_HOST = "buddhireview.com"
_RETIRED_HOST = "buddhikernel" + ".com"   # split so this file does not trip its own scan


def test_no_module_names_the_retired_host():
    offenders = [p.relative_to(_PKG).as_posix()
                 for p in sorted(_PKG.rglob("*.py"))
                 if _RETIRED_HOST in p.read_text(encoding="utf-8")]
    assert offenders == [], f"these modules still send users to {_RETIRED_HOST}: {offenders}"


def test_every_commercial_surface_points_at_the_same_host():
    assert upsell.DOMAIN == _COMMERCIAL_HOST
    assert urlparse(upsell._URL).hostname == _COMMERCIAL_HOST
    assert urlparse(pro_trial.CHECKOUT_URL).hostname == _COMMERCIAL_HOST
    notice_urls = [w for w in cli._UNCLAIMED_COMMAND_NOTICE.split() if w.startswith("https://")]
    assert notice_urls and all(urlparse(u).hostname == _COMMERCIAL_HOST for u in notice_urls)
    # the checkout pointer and the unclaimed-command notice send the buyer to the same page
    assert pro_trial.CHECKOUT_URL in cli._UNCLAIMED_COMMAND_NOTICE
