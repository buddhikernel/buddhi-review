"""Unit tests for netrc_writer.py — the generic merge-preserving ~/.netrc writer.

Every test writes to a tmp_path file via the ``path=`` arg, so the real ~/.netrc is
never touched. Covers create / in-place update / idempotency / byte-for-byte
preservation of other stanzas / single-line-form replacement / the tangled-line
fallback / 0600 permissions.
"""
from __future__ import annotations

import os
import stat

from buddhi_review import netrc_writer as nw


def _read(p):
    return p.read_text(encoding="utf-8")


def test_create_in_absent_file(tmp_path):
    p = tmp_path / ".netrc"
    ok, action = nw.upsert("index.example", "user", "SECRETKEY", path=p)
    assert ok and action == "created"
    assert "machine index.example" in _read(p)
    assert "password SECRETKEY" in _read(p)


def test_mode_is_0600(tmp_path):
    p = tmp_path / ".netrc"
    nw.upsert("index.example", "user", "K", path=p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_update_in_place_preserves_other_stanzas(tmp_path):
    p = tmp_path / ".netrc"
    p.write_text(
        "machine github.com login ghuser password ghtok\n"
        "machine index.example\n  login old\n  password OLDKEY\n"
        "machine gitlab.com login gl password gltok\n", encoding="utf-8")
    ok, action = nw.upsert("index.example", "license", "NEWKEY", path=p)
    assert ok and action == "updated"
    out = _read(p)
    # our entry updated
    assert "password NEWKEY" in out and "OLDKEY" not in out and "login license" in out
    # every OTHER entry preserved byte-for-byte
    assert "machine github.com login ghuser password ghtok\n" in out
    assert "machine gitlab.com login gl password gltok\n" in out


def test_idempotent_no_duplicate(tmp_path):
    p = tmp_path / ".netrc"
    nw.upsert("index.example", "license", "K1", path=p)
    nw.upsert("index.example", "license", "K2", path=p)
    out = _read(p)
    assert out.count("machine index.example") == 1     # replaced, not appended
    assert "password K2" in out and "K1" not in out


def test_single_line_form_replaced(tmp_path):
    p = tmp_path / ".netrc"
    p.write_text("machine index.example login old password OLDKEY\n"
                 "machine other.host login o password ot\n", encoding="utf-8")
    ok, action = nw.upsert("index.example", "license", "NEWKEY", path=p)
    assert ok and action == "updated"
    out = _read(p)
    assert "OLDKEY" not in out and "password NEWKEY" in out
    assert "machine other.host login o password ot\n" in out   # sibling untouched


def test_create_appends_when_host_absent(tmp_path):
    p = tmp_path / ".netrc"
    p.write_text("machine github.com login g password t\n", encoding="utf-8")
    ok, action = nw.upsert("index.example", "license", "K", path=p)
    assert ok and action == "created"
    out = _read(p)
    assert "machine github.com login g password t\n" in out
    assert "machine index.example" in out


def test_tangled_line_is_appended_not_spliced(tmp_path):
    p = tmp_path / ".netrc"
    # A single physical line packing two stanzas — cannot be spliced safely.
    p.write_text("machine index.example login a machine other.host login b\n", encoding="utf-8")
    ok, action = nw.upsert("index.example", "license", "NEWKEY", path=p)
    assert ok and action == "appended-unparsed"
    out = _read(p)
    # the tangled line is left intact (other.host's creds are not destroyed)…
    assert "machine index.example login a machine other.host login b\n" in out
    # …and our fresh stanza is appended
    assert out.rstrip().endswith("password NEWKEY")


def test_no_trailing_newline_still_separates(tmp_path):
    p = tmp_path / ".netrc"
    p.write_text("machine github.com login g password t", encoding="utf-8")  # no newline
    nw.upsert("index.example", "license", "K", path=p)
    out = _read(p)
    assert "password t\nmachine index.example" in out   # a separator was inserted


def test_unreadable_existing_file_is_not_clobbered(tmp_path):
    p = tmp_path / ".netrc"
    original = "machine github.com login ghuser password ghtok\n"
    p.write_text(original, encoding="utf-8")
    p.chmod(0o000)
    try:
        if os.access(p, os.R_OK):
            return  # running as root or on a platform that ignores chmod(0)
        ok, action = nw.upsert("index.example", "license", "NEWKEY", path=p)
    finally:
        p.chmod(0o600)
    assert ok is False and action == "read-error"
    assert _read(p) == original   # the unreadable file was left untouched


def test_undecodable_existing_file_is_not_clobbered(tmp_path):
    p = tmp_path / ".netrc"
    p.write_bytes(b"machine github.com login ghuser password \xff\xfe\n")
    ok, action = nw.upsert("index.example", "license", "NEWKEY", path=p)
    assert ok is False and action == "read-error"
    assert p.read_bytes() == b"machine github.com login ghuser password \xff\xfe\n"
