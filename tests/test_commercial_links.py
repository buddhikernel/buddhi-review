"""Every link the free package shows a prospective or paying user goes to ONE host.

The commercial site is buddhireview.com. On 2026-09-20 three modules were found
sending users to buddhikernel.com — the open-source kernel's site, which sells
nothing — from the unclaimed-command notice (``cli.py``), the checkout pointer
(``pro_trial.py``) and the upgrade nudge (``upsell.py``), each with its own
hard-coded literal, so nothing noticed when the commercial domain changed. These
tests close that class across every shipped file, not only the Python modules: no
shipped file may name the kernel site, every surface must agree on one host,
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
    assert urlparse(pro_trial.EULA_URL).hostname == _COMMERCIAL_HOST
    # the service every acquisition request goes to is the commercial host's API
    assert urlparse(pro_trial.BRIDGE_ORIGIN).hostname == f"api.{_COMMERCIAL_HOST}"
    assert urlparse(pro_trial.BRIDGE_ORIGIN).scheme == "https"
    # any link the unclaimed-command notice shows is on that host too (today it shows
    # none: it sends the buyer to `setup --buy`, which opens the checkout itself)
    notice_urls = [w for w in cli._UNCLAIMED_COMMAND_NOTICE.split() if w.startswith("https://")]
    assert all(urlparse(u).hostname == _COMMERCIAL_HOST for u in notice_urls)


def test_the_unclaimed_command_notice_sends_the_buyer_to_setup_buy():
    assert cli._UNCLAIMED_COMMAND_NOTICE.splitlines()[-1] == (
        "To get a licence, run: buddhi-review setup --buy")


# RFC 2606's reserved example domain: an address on it is a shape shown to the user
# ("such as name@example.com"), never a mailbox anyone is sent to.
_EXAMPLE_HOST = "example.com"


def test_every_shipped_email_address_is_on_the_commercial_host():
    assert pro_trial.SUPPORT_EMAIL.endswith("@" + _COMMERCIAL_HOST)
    offenders = sorted({(p.relative_to(_PKG).as_posix(), m.group(0))
                        for p, text in _shipped_text_files()
                        for m in _EMAIL.finditer(text)
                        if m.group(1).lower() not in (_COMMERCIAL_HOST, _EXAMPLE_HOST)})
    assert offenders == [], f"shipped email addresses off {_COMMERCIAL_HOST}: {offenders}"


def test_no_shipped_copy_promises_an_emailed_key():
    offenders = [p.relative_to(_PKG).as_posix()
                 for p, text in _shipped_text_files()
                 if re.search(r"emails? your (Pro )?key", text, re.IGNORECASE)]
    assert offenders == [], f"these shipped files promise an emailed key: {offenders}"


def test_the_upgrade_pointer_sends_buyers_to_the_tools_own_paths():
    """The pointer every message that sent a user to the checkout page now shows: buy
    from the tool, activate a setup code, or move your own seat — no page to visit."""
    pointer = pro_trial.UPGRADE_POINTER
    for path in ("setup --buy", "setup --activate", "setup --recover"):
        assert f"buddhi-review {path}" in pointer
    assert "https://" not in pointer
