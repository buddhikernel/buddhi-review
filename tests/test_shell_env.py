"""shell_env.py — the sanctioned shell-rc secret writer (GH_TOKEN escape hatch)."""
import os
import stat

import pytest

from buddhi_review import shell_env


def test_format_export_posix_and_fish():
    assert shell_env.format_export("GH_TOKEN", "abc", "posix") == "export GH_TOKEN=abc"
    # A value with a space is shell-quoted.
    assert shell_env.format_export("GH_TOKEN", "a b", "posix") == "export GH_TOKEN='a b'"
    assert shell_env.format_export("GH_TOKEN", "abc", "fish") == 'set -gx GH_TOKEN "abc"'


def test_upsert_writes_block_and_is_0600(tmp_path):
    rc = tmp_path / ".zshenv"
    rc.write_text("# my prior content\nexport FOO=1\n", encoding="utf-8")
    ok, path = shell_env.upsert({"GH_TOKEN": "tok123"}, rc_path=str(rc))
    assert ok
    text = rc.read_text(encoding="utf-8")
    assert shell_env.MARKER in text
    assert "export GH_TOKEN=tok123" in text
    # Pre-existing content is preserved.
    assert "export FOO=1" in text
    # Secret file is mode 0600.
    assert stat.S_IMODE(os.stat(rc).st_mode) == 0o600
    assert shell_env.present(str(rc)) == {"GH_TOKEN"}


def test_upsert_idempotent_no_duplicate_block(tmp_path):
    rc = tmp_path / ".zshenv"
    shell_env.upsert({"GH_TOKEN": "one"}, rc_path=str(rc))
    shell_env.upsert({"GH_TOKEN": "two"}, rc_path=str(rc))
    text = rc.read_text(encoding="utf-8")
    assert text.count(shell_env.MARKER) == 1
    assert "export GH_TOKEN=two" in text
    assert "export GH_TOKEN=one" not in text


def test_upsert_empty_value_removes_export(tmp_path):
    rc = tmp_path / ".zshenv"
    shell_env.upsert({"GH_TOKEN": "tok"}, rc_path=str(rc))
    shell_env.upsert({"GH_TOKEN": ""}, rc_path=str(rc))
    text = rc.read_text(encoding="utf-8")
    # With no managed exports left, the block (and marker) is gone entirely.
    assert shell_env.MARKER not in text
    assert shell_env.present(str(rc)) == set()


def test_upsert_ignores_names_outside_write_order(tmp_path):
    rc = tmp_path / ".zshenv"
    ok, _ = shell_env.upsert({"SOMETHING_ELSE": "x", "GH_TOKEN": "tok"}, rc_path=str(rc))
    assert ok
    text = rc.read_text(encoding="utf-8")
    assert "SOMETHING_ELSE" not in text
    assert "export GH_TOKEN=tok" in text


def test_strip_block_round_trips(tmp_path):
    rc = tmp_path / ".profile"
    rc.write_text("export KEEP=1\n", encoding="utf-8")
    shell_env.upsert({"GH_TOKEN": "tok"}, rc_path=str(rc))
    stripped = shell_env.strip_block(rc.read_text(encoding="utf-8"))
    assert "export KEEP=1" in stripped
    assert "GH_TOKEN" not in stripped


def test_windows_setx_path_via_injected_runner():
    calls = []

    class _R:
        returncode = 0

    def runner(argv):
        calls.append(argv)
        return _R()

    ok, _ = shell_env._upsert_windows({"GH_TOKEN": "tok"}, also_env=False, runner=runner)
    assert ok
    assert calls and calls[0][:2] == ["setx", "GH_TOKEN"]


def test_managed_block_does_not_capture_gh_token_siblings(tmp_path):
    """The managed-prefix match is anchored to the exact name, so a user's own
    GH_TOKEN-prefixed export is preserved across a re-write."""
    rc = tmp_path / ".zshenv"
    rc.write_text("export GH_TOKEN_BACKUP=keepme\n", encoding="utf-8")
    shell_env.upsert({"GH_TOKEN": "tok"}, rc_path=str(rc))
    text = rc.read_text(encoding="utf-8")
    assert "export GH_TOKEN_BACKUP=keepme" in text  # sibling untouched
    assert "export GH_TOKEN=tok" in text
    # Removing the managed token leaves the sibling intact.
    shell_env.upsert({"GH_TOKEN": ""}, rc_path=str(rc))
    assert "export GH_TOKEN_BACKUP=keepme" in rc.read_text(encoding="utf-8")


def test_no_paid_secret_names_in_source():
    """OSS purity: the free shell_env must not reference Telegram/billing secrets
    (those are paid surface). Only GH_TOKEN is managed."""
    src = (shell_env.__file__)
    with open(src, encoding="utf-8") as f:
        text = f.read()
    for forbidden in ("TELEGRAM", "BILLING", "GEMINI_API_KEY"):
        assert forbidden not in text, f"{forbidden} leaked into free shell_env"
    assert shell_env.WRITE_ORDER == ("GH_TOKEN",)
