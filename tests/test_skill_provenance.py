"""Unit tests for the skill content-transform / provenance seam.

Covers the public API F2 (install-skills) and F3b (interpreter stamping) build on:
version read, version-stamp idempotency, post-transform hash stability, registry
ordering, and the "register a new transform WITHOUT editing the module" contract.
"""
import hashlib
import re

import pytest

import buddhi_review
from buddhi_review import skill_provenance as sp

SAMPLE_SKILL = """\
---
name: review-pr
description: A sample skill.
allowed-tools:
  - Bash
---

# Body

Some skill body text.
"""


@pytest.fixture(autouse=True)
def _restore_registry():
    """Keep the module-global transform registry pristine across tests that
    register their own transforms."""
    snapshot = list(sp._REGISTRY)
    yield
    sp._REGISTRY[:] = snapshot


# ── package_version ─────────────────────────────────────────────────────────────
def test_package_version_is_single_sourced():
    assert sp.package_version() == buddhi_review.__version__
    # Asserted by SemVer shape, not pinned to a literal, so an automated release bump
    # (release-please) never turns this test red.
    assert re.fullmatch(r"\d+\.\d+\.\d+", sp.package_version())


# ── version-stamp transform ─────────────────────────────────────────────────────
def test_stamp_adds_version_key_to_frontmatter():
    out = sp.apply_transforms(SAMPLE_SKILL, ctx={"version": "1.2.3"})
    assert f"{sp.VERSION_STAMP_KEY}: 1.2.3" in out
    # stamp lands inside the frontmatter block, not the body
    head, _, _ = out.partition("\n---\n")
    assert f"{sp.VERSION_STAMP_KEY}: 1.2.3" in head
    # the original body is preserved verbatim
    assert "Some skill body text." in out
    assert out.endswith("\n")


def test_stamp_is_idempotent():
    once = sp.apply_transforms(SAMPLE_SKILL, ctx={"version": "1.2.3"})
    twice = sp.apply_transforms(once, ctx={"version": "1.2.3"})
    assert once == twice
    # exactly one stamp line, never duplicated
    assert twice.count(sp.VERSION_STAMP_KEY) == 1


def test_stamp_updates_existing_key_in_place():
    first = sp.apply_transforms(SAMPLE_SKILL, ctx={"version": "1.0.0"})
    second = sp.apply_transforms(first, ctx={"version": "2.0.0"})
    assert f"{sp.VERSION_STAMP_KEY}: 2.0.0" in second
    assert "1.0.0" not in second
    assert second.count(sp.VERSION_STAMP_KEY) == 1


def test_stamp_falls_back_to_package_version():
    out = sp.apply_transforms(SAMPLE_SKILL)
    assert f"{sp.VERSION_STAMP_KEY}: {sp.package_version()}" in out


def test_stamp_leaves_content_without_frontmatter_unchanged():
    no_fm = "# Just a heading\n\nNo frontmatter here.\n"
    assert sp.apply_transforms(no_fm, ctx={"version": "1.2.3"}) == no_fm


def test_stamp_leaves_unterminated_frontmatter_unchanged():
    unterminated = "---\nname: x\ndescription: y\n"  # no closing delimiter
    assert sp.apply_transforms(unterminated, ctx={"version": "1.2.3"}) == unterminated


def test_stamp_preserves_crlf_line_endings():
    crlf = "---\r\nname: x\r\ndescription: y\r\n---\r\nbody\r\n"
    out = sp.apply_transforms(crlf, ctx={"version": "1.2.3"})
    # the inserted stamp line is CRLF-terminated like its neighbours, so the
    # post-transform bytes never carry a lone-LF line a later EOL normalisation
    # could flip (which would make content_hash falsely flag the file as modified).
    assert f"{sp.VERSION_STAMP_KEY}: 1.2.3\r\n" in out
    assert out.replace("\r\n", "").count("\n") == 0  # no bare LF anywhere
    assert sp.apply_transforms(out, ctx={"version": "1.2.3"}) == out  # idempotent


def test_stamp_handles_bom_prefixed_frontmatter():
    bom = chr(0xFEFF)
    src = bom + "---\nname: x\n---\nbody\n"
    out = sp.apply_transforms(src, ctx={"version": "1.2.3"})
    assert out.startswith(bom)  # BOM carried through
    assert f"{sp.VERSION_STAMP_KEY}: 1.2.3" in out  # frontmatter beneath the BOM is stamped
    assert out.count(sp.VERSION_STAMP_KEY) == 1
    assert sp.apply_transforms(out, ctx={"version": "1.2.3"}) == out  # idempotent


# ── content_hash ────────────────────────────────────────────────────────────────
def test_content_hash_is_stable_and_correct():
    text = "hello world"
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert sp.content_hash(text) == expected
    assert sp.content_hash(text) == sp.content_hash(text)


def test_content_hash_changes_with_content():
    assert sp.content_hash("a") != sp.content_hash("b")


def test_content_hash_is_eol_invariant():
    # A pure CRLF/CR↔LF conversion (no content change) hashes identically, so a
    # managed file an editor re-wrote to LF is never falsely flagged "modified".
    assert sp.content_hash("a\r\nb\r\n") == sp.content_hash("a\nb\n") == sp.content_hash("a\rb\r")
    # ...but a genuine content edit at the same EOL still changes the hash.
    assert sp.content_hash("a\nb\n") != sp.content_hash("a\nc\n")


def test_post_transform_hash_is_stable_across_restamp():
    once = sp.apply_transforms(SAMPLE_SKILL, ctx={"version": "1.2.3"})
    twice = sp.apply_transforms(once, ctx={"version": "1.2.3"})
    # re-stamping does not change the post-transform bytes → hash is unchanged,
    # so a stamped file never later looks "modified".
    assert sp.content_hash(once) == sp.content_hash(twice)


# ── registry: ordering + extensibility ──────────────────────────────────────────
def test_version_stamp_is_registered_by_default():
    assert "version-stamp" in {t.name for t in sp.registered_transforms()}


def test_registry_runs_transforms_in_order():
    log: list[str] = []

    def make(tag):
        def _t(text, ctx):
            log.append(tag)
            return text + f"<{tag}>"
        return _t

    sp.register_transform(make("late"), name="late", order=200)
    sp.register_transform(make("early"), name="early", order=10)

    out = sp.apply_transforms("X")
    # lower order runs first regardless of registration order
    assert log == ["early", "late"]
    assert out.index("<early>") < out.index("<late>")


def test_equal_order_preserves_registration_order():
    log: list[str] = []

    def make(tag):
        return lambda text, ctx: (log.append(tag) or text)

    sp.register_transform(make("first"), name="first", order=300)
    sp.register_transform(make("second"), name="second", order=300)
    sp.apply_transforms("X")
    assert log[-2:] == ["first", "second"]


def test_new_transform_can_be_registered_without_editing_module():
    @sp.register_transform(name="exclaim", order=500)
    def _exclaim(text, ctx):
        return text + "!"

    out = sp.apply_transforms("done")
    assert out.endswith("!")
    assert "exclaim" in {t.name for t in sp.registered_transforms()}


def test_unregister_transform_removes_it():
    sp.register_transform(lambda text, ctx: text + "?", name="q", order=400)
    assert "q" in {t.name for t in sp.registered_transforms()}
    assert sp.unregister_transform("q") is True
    assert "q" not in {t.name for t in sp.registered_transforms()}
    assert sp.unregister_transform("q") is False


def test_ctx_is_isolated_per_apply_call():
    seen: list[dict] = []

    def _spy(text, ctx):
        seen.append(dict(ctx))
        return text

    sp.register_transform(_spy, name="spy", order=50)
    sp.apply_transforms("X", ctx={"version": "9.9.9"})
    assert seen and seen[0].get("version") == "9.9.9"
