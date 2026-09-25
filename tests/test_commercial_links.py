"""Every link the free package shows a prospective or paying user goes to ONE host.

The commercial site is buddhireview.com. On 2026-09-20 three modules were found
sending users to buddhikernel.com — the open-source kernel's site, which sells
nothing — from the unclaimed-command notice (``cli.py``), the checkout pointer
(``pro_trial.py``) and the upgrade nudge (``upsell.py``), each with its own
hard-coded literal, so nothing noticed when the commercial domain changed. These
tests close that class across every shipped file, not only the Python modules: no
shipped file may name the kernel site, the three surfaces must agree on one host,
every shipped email address is on that host, and no copy may promise that a
licence key is emailed (nothing emails one).
"""
import re
from pathlib import Path
from urllib.parse import urlparse

import buddhi_review
from buddhi_review import cli, pro_trial, upsell

_PKG = Path(buddhi_review.__file__).resolve().parent
_COMMERCIAL_HOST = "buddhireview.com"
_KERNEL_SITE = "buddhikernel" + ".com"   # split so this file does not trip its own scan
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def _shipped_text_files():
    for p in sorted(_PKG.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        try:
            yield p, p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue


def test_no_shipped_file_names_the_kernel_site():
    offenders = [p.relative_to(_PKG).as_posix()
                 for p, text in _shipped_text_files() if _KERNEL_SITE in text]
    assert offenders == [], f"these shipped files still send users to {_KERNEL_SITE}: {offenders}"


def test_every_commercial_surface_points_at_the_same_host():
    assert upsell.DOMAIN == _COMMERCIAL_HOST
    assert urlparse(upsell._URL).hostname == _COMMERCIAL_HOST
    assert urlparse(pro_trial.CHECKOUT_URL).hostname == _COMMERCIAL_HOST
    notice_urls = [w for w in cli._UNCLAIMED_COMMAND_NOTICE.split() if w.startswith("https://")]
    assert notice_urls and all(urlparse(u).hostname == _COMMERCIAL_HOST for u in notice_urls)
    # the checkout pointer and the unclaimed-command notice send the buyer to the same page
    assert pro_trial.CHECKOUT_URL in cli._UNCLAIMED_COMMAND_NOTICE


def test_every_shipped_email_address_is_on_the_commercial_host():
    assert pro_trial.SUPPORT_EMAIL.endswith("@" + _COMMERCIAL_HOST)
    offenders = sorted({(p.relative_to(_PKG).as_posix(), m.group(0))
                        for p, text in _shipped_text_files()
                        for m in _EMAIL.finditer(text)
                        if m.group(1).lower() != _COMMERCIAL_HOST})
    assert offenders == [], f"shipped email addresses off {_COMMERCIAL_HOST}: {offenders}"


def test_no_shipped_copy_promises_an_emailed_key():
    offenders = [p.relative_to(_PKG).as_posix()
                 for p, text in _shipped_text_files()
                 if re.search(r"emails? your (Pro )?key", text, re.IGNORECASE)]
    assert offenders == [], f"these shipped files promise an emailed key: {offenders}"


def test_the_subscribe_pointer_sends_new_buyers_to_checkout_and_subscribers_to_support():
    pointer = pro_trial._convert_pointer()
    assert pro_trial.CHECKOUT_URL in pointer
    assert pro_trial.SUPPORT_EMAIL in pointer
