"""Provenance-safe skill installer — the full 3-way state matrix, the never-clobber
safe default, ``--force`` / ``--dry-run`` / ``--uninstall``, idempotency across a second
registered transform, the CLI subcommand, and a doc-swap guard.

Every test points ``HOME`` / ``CLAUDE_CONFIG_DIR`` / ``XDG_CONFIG_HOME`` at a tmp tree so
nothing here ever touches the real ``~/.claude`` or ``~/.config``.
"""
from __future__ import annotations

import json
import os
import signal
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from buddhi_review import cli, skill_install
from buddhi_review.skill_provenance import (
    apply_transforms,
    content_hash,
    package_version,
    register_transform,
    unregister_transform,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Redirect every filesystem root the installer touches into ``tmp_path`` and hand
    back the resolved target/sidecar paths."""
    home = tmp_path / "home"
    cfg = tmp_path / "claude_cfg"
    xdg = tmp_path / "xdg"
    for d in (home, cfg, xdg):
        d.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return SimpleNamespace(
        tmp=tmp_path,
        home=home,
        target=cfg / "skills",
        sidecar=xdg / "buddhi" / "installed-skills.json",
    )


def _src_files():
    """(skill, rel, abs source path) for every bundled skill file."""
    root = skill_install.bundled_skills_root()
    out = []
    for skill in skill_install._skill_dirs(root):
        for f in skill_install._skill_files(root / skill):
            out.append((skill, f.relative_to(root / skill).as_posix(), f))
    return out


def _expected_hash(src: Path) -> str:
    return content_hash(apply_transforms(src.read_text(encoding="utf-8"),
                                         ctx={"version": package_version()}))


def _actions(summary):
    return {(f.skill, f.rel): f.action for f in summary.files}


# ── Path resolution ───────────────────────────────────────────────────────────────

def test_target_root_prefers_claude_config_dir(env):
    assert skill_install.target_root() == env.target


def test_target_root_falls_back_to_home(env, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert skill_install.target_root() == env.home / ".claude" / "skills"


def test_sidecar_honours_xdg_then_home(env, monkeypatch):
    assert skill_install.sidecar_path() == env.sidecar
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert skill_install.sidecar_path() == env.home / ".config" / "buddhi" / "installed-skills.json"


# ── 3-way matrix ──────────────────────────────────────────────────────────────────

def test_absent_installs(env):
    summary = skill_install.install_skills()
    assert not summary.had_error
    assert set(_actions(summary).values()) == {skill_install.INSTALL}
    # Every file is on disk, byte-equal to the post-transform source, and the version
    # stamp is present on the frontmatter files.
    for skill, rel, src in _src_files():
        dest = env.target / skill / rel
        assert dest.exists()
        assert content_hash(dest.read_text(encoding="utf-8")) == _expected_hash(src)
    assert env.sidecar.exists()
    recs = json.loads(env.sidecar.read_text())["files"]
    assert len(recs) == len(_src_files())


def test_current_is_noop(env):
    skill_install.install_skills()
    summary = skill_install.install_skills()
    assert set(_actions(summary).values()) == {skill_install.NOOP}


def test_prior_ours_updates(env):
    skill_install.install_skills()
    dest = env.target / "open-pr" / "SKILL.md"
    prior = "old managed content\n"
    dest.write_text(prior, encoding="utf-8")
    sc = json.loads(env.sidecar.read_text())
    sc["files"][str(dest)] = {"version": "0.0.1", "hash": content_hash(prior)}
    env.sidecar.write_text(json.dumps(sc), encoding="utf-8")

    summary = skill_install.install_skills()
    assert _actions(summary)[("open-pr", "SKILL.md")] == skill_install.UPDATE
    # The stale-but-ours file is replaced with the current bundled content …
    src = skill_install.bundled_skills_root() / "open-pr" / "SKILL.md"
    assert content_hash(dest.read_text(encoding="utf-8")) == _expected_hash(src)
    # … and its record is refreshed to the current version.
    assert json.loads(env.sidecar.read_text())["files"][str(dest)]["version"] == package_version()


def test_modified_is_conflict_left_untouched(env):
    skill_install.install_skills()
    dest = env.target / "review-pr" / "SKILL.md"
    edited = dest.read_text(encoding="utf-8") + "\n# my own edit\n"
    dest.write_text(edited, encoding="utf-8")

    summary = skill_install.install_skills()
    assert _actions(summary)[("review-pr", "SKILL.md")] == skill_install.CONFLICT
    assert dest.read_text(encoding="utf-8") == edited  # untouched
    assert not summary.had_error


def test_foreign_is_conflict(env):
    # A file we never recorded (empty sidecar) sitting where a skill file belongs.
    dest = env.target / "open-pr" / "SKILL.md"
    dest.parent.mkdir(parents=True)
    dest.write_text("someone else's file\n", encoding="utf-8")

    summary = skill_install.install_skills()
    assert _actions(summary)[("open-pr", "SKILL.md")] == skill_install.CONFLICT
    assert dest.read_text(encoding="utf-8") == "someone else's file\n"


def test_legacy_manual_copy_is_adopted(env):
    """A raw ``cp -R`` copy left by the pre-F2 README (unstamped bytes, no sidecar record)
    is provably unmodified, so a plain re-run adopts it: stamped, recorded, no --force."""
    root = skill_install.bundled_skills_root()
    for skill, rel, src in _src_files():
        dest = env.target / skill / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")  # raw, unstamped

    summary = skill_install.install_skills()
    assert not summary.had_error
    # Stamped files are adopted via UPDATE; a file the transform leaves alone (no YAML
    # frontmatter) is already byte-current, so it lands as NOOP — recorded either way.
    assert set(_actions(summary).values()) <= {skill_install.UPDATE, skill_install.NOOP}
    assert _actions(summary)[("review-pr", "SKILL.md")] == skill_install.UPDATE
    recs = json.loads(env.sidecar.read_text())["files"]
    for skill, rel, src in _src_files():
        dest = env.target / skill / rel
        assert content_hash(dest.read_text(encoding="utf-8")) == _expected_hash(src)
        assert recs[str(dest)] == {"version": package_version(), "hash": _expected_hash(src)}
        assert not list(dest.parent.glob(f"{dest.name}.bak-*"))  # adoption is not a clobber
    # And the adopted tree reads back as a clean NOOP on the next run.
    assert set(_actions(skill_install.install_skills()).values()) == {skill_install.NOOP}
    assert root.is_dir()


def test_edited_legacy_copy_still_conflicts(env):
    """Adoption is byte-exact: a legacy copy the user then EDITED is still a CONFLICT."""
    src = skill_install.bundled_skills_root() / "review-pr" / "SKILL.md"
    dest = env.target / "review-pr" / "SKILL.md"
    dest.parent.mkdir(parents=True)
    edited = src.read_text(encoding="utf-8") + "\n# my own edit\n"
    dest.write_text(edited, encoding="utf-8")

    summary = skill_install.install_skills()
    assert _actions(summary)[("review-pr", "SKILL.md")] == skill_install.CONFLICT
    assert dest.read_text(encoding="utf-8") == edited  # untouched


def test_symlink_is_conflict_not_followed(env):
    outside = env.tmp / "secret.txt"
    outside.write_text("do not touch\n", encoding="utf-8")
    dest = env.target / "open-pr" / "SKILL.md"
    dest.parent.mkdir(parents=True)
    dest.symlink_to(outside)

    summary = skill_install.install_skills()
    assert _actions(summary)[("open-pr", "SKILL.md")] == skill_install.CONFLICT
    assert dest.is_symlink()                      # link intact
    assert outside.read_text(encoding="utf-8") == "do not touch\n"  # target untouched


def test_symlinked_skill_dir_is_conflict(env):
    # A whole skill DIRECTORY replaced by a symlink must not be written through.
    elsewhere = env.tmp / "evil"
    elsewhere.mkdir()
    env.target.mkdir(parents=True)
    (env.target / "open-pr").symlink_to(elsewhere, target_is_directory=True)

    summary = skill_install.install_skills()
    assert _actions(summary)[("open-pr", "SKILL.md")] == skill_install.CONFLICT
    assert not (elsewhere / "SKILL.md").exists()  # nothing written into the link target


def test_force_refuses_write_through_symlinked_skill_dir(env):
    # Adversarial: a symlinked skill DIR whose target holds a file colliding with a bundled
    # name. --force must NOT follow the link and clobber it (nor drop a .bak inside it).
    outside = env.tmp / "outside_open_pr"
    outside.mkdir()
    victim = outside / "SKILL.md"
    victim.write_text("PRECIOUS outside content\n", encoding="utf-8")
    env.target.mkdir(parents=True)
    (env.target / "open-pr").symlink_to(outside, target_is_directory=True)

    summary = skill_install.install_skills(force=True)
    assert _actions(summary)[("open-pr", "SKILL.md")] == skill_install.CONFLICT
    assert victim.read_text(encoding="utf-8") == "PRECIOUS outside content\n"  # untouched
    assert not list(outside.glob("SKILL.md.bak-*"))        # no backup landed in the target
    assert (env.target / "open-pr").is_symlink()           # the link itself is intact


def test_force_refuses_write_through_symlinked_references_dir(env):
    # Same, but a NESTED references/ dir is the symlink; the sibling SKILL.md still installs.
    outside = env.tmp / "outside_refs"
    outside.mkdir()
    victim = outside / "env-vars.md"
    victim.write_text("PRECIOUS refs\n", encoding="utf-8")
    (env.target / "review-pr").mkdir(parents=True)
    (env.target / "review-pr" / "references").symlink_to(outside, target_is_directory=True)

    acts = _actions(skill_install.install_skills(force=True))
    assert acts[("review-pr", "references/env-vars.md")] == skill_install.CONFLICT
    assert victim.read_text(encoding="utf-8") == "PRECIOUS refs\n"
    assert not list(outside.glob("*.bak-*"))
    assert acts[("review-pr", "SKILL.md")] == skill_install.INSTALL  # not under the link


# ── Clobber policy: safe default vs --force ────────────────────────────────────────

def test_non_tty_default_refuses_to_clobber(env, monkeypatch):
    # force is the sole clobber signal; there is no prompt path, so a non-TTY run with
    # the default is inherently safe.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    skill_install.install_skills()
    dest = env.target / "review-pr" / "SKILL.md"
    dest.write_text("edited in a pipe\n", encoding="utf-8")

    summary = skill_install.install_skills()  # default force=False
    assert _actions(summary)[("review-pr", "SKILL.md")] == skill_install.CONFLICT
    assert dest.read_text(encoding="utf-8") == "edited in a pipe\n"


def test_force_overwrites_conflict_and_backs_up(env):
    skill_install.install_skills()
    dest = env.target / "review-pr" / "SKILL.md"
    dest.write_text("hand edited\n", encoding="utf-8")

    summary = skill_install.install_skills(force=True)
    assert _actions(summary)[("review-pr", "SKILL.md")] == skill_install.UPDATE
    src = skill_install.bundled_skills_root() / "review-pr" / "SKILL.md"
    assert content_hash(dest.read_text(encoding="utf-8")) == _expected_hash(src)
    baks = list(dest.parent.glob("SKILL.md.bak-*"))
    assert len(baks) == 1 and baks[0].read_text(encoding="utf-8") == "hand edited\n"


def test_force_replaces_symlink_without_writing_through(env):
    outside = env.tmp / "target.txt"
    outside.write_text("protected\n", encoding="utf-8")
    dest = env.target / "open-pr" / "SKILL.md"
    dest.parent.mkdir(parents=True)
    dest.symlink_to(outside)

    summary = skill_install.install_skills(force=True)
    assert _actions(summary)[("open-pr", "SKILL.md")] == skill_install.UPDATE
    assert not dest.is_symlink()                              # now a real file
    assert outside.read_text(encoding="utf-8") == "protected\n"  # target never written through
    baks = list(dest.parent.glob("SKILL.md.bak-*"))
    assert len(baks) == 1 and baks[0].is_symlink()           # the link itself was backed up


# ── --dry-run writes nothing ──────────────────────────────────────────────────────

def test_dry_run_writes_nothing(env):
    summary = skill_install.install_skills(dry_run=True)
    assert set(_actions(summary).values()) == {skill_install.INSTALL}
    assert not env.target.exists()
    assert not env.sidecar.exists()


def test_dry_run_conflict_creates_no_backup(env):
    skill_install.install_skills()
    dest = env.target / "review-pr" / "SKILL.md"
    dest.write_text("edited\n", encoding="utf-8")

    skill_install.install_skills(force=True, dry_run=True)
    assert dest.read_text(encoding="utf-8") == "edited\n"      # unchanged
    assert not list(dest.parent.glob("SKILL.md.bak-*"))       # no backup written


# ── Idempotency & hash round-trip (incl. a second transform) ──────────────────────

def test_install_twice_is_all_noop(env):
    skill_install.install_skills()
    second = skill_install.install_skills()
    # Proves the POST-transform hash round-trips: a freshly written file is never
    # flagged modified on the next run.
    assert all(f.action == skill_install.NOOP for f in second.files)
    assert not any(f.action == skill_install.CONFLICT for f in second.files)


def test_hash_correct_with_a_second_transform(env):
    """A second registered transform must not break the round-trip — the recorded hash
    is over the post-ALL-transforms bytes, never the raw source."""
    def _extra(text, ctx):
        return text + "\n<!-- extra transform -->\n" if text.startswith("---") else text

    register_transform(_extra, name="test-extra", order=200)
    try:
        first = skill_install.install_skills()
        assert set(_actions(first).values()) == {skill_install.INSTALL}
        # The written SKILL.md carries the extra transform's output …
        skillmd = env.target / "open-pr" / "SKILL.md"
        assert "<!-- extra transform -->" in skillmd.read_text(encoding="utf-8")
        # … and the next run is still a clean NOOP (hash matched post-both-transforms).
        second = skill_install.install_skills()
        assert set(_actions(second).values()) == {skill_install.NOOP}
    finally:
        unregister_transform("test-extra")


# ── Sidecar keying by absolute path (no cross-root collision) ─────────────────────

def test_two_config_roots_do_not_collide(env, monkeypatch):
    skill_install.install_skills()  # into env.target (root A)
    root_b = env.tmp / "cfg_b"
    root_b.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root_b))

    summary = skill_install.install_skills()  # into root B/skills
    assert set(_actions(summary).values()) == {skill_install.INSTALL}  # not NOOP
    recs = json.loads(env.sidecar.read_text())["files"]
    a_keys = [k for k in recs if str(env.target) in k]
    b_keys = [k for k in recs if str(root_b) in k]
    assert a_keys and b_keys                       # both roots recorded, keyed by abs path
    assert len(recs) == 2 * len(_src_files())


# ── Uninstall ─────────────────────────────────────────────────────────────────────

def test_uninstall_removes_ours_and_prunes_sidecar(env):
    skill_install.install_skills()
    summary = skill_install.install_skills(uninstall=True)
    assert set(_actions(summary).values()) == {skill_install.REMOVED}
    assert not (env.target / "review-pr").exists()
    assert not (env.target / "open-pr").exists()
    assert json.loads(env.sidecar.read_text())["files"] == {}


def test_uninstall_leaves_modified_file(env):
    skill_install.install_skills()
    dest = env.target / "review-pr" / "SKILL.md"
    dest.write_text("customised\n", encoding="utf-8")

    summary = skill_install.install_skills(uninstall=True)
    acts = _actions(summary)
    assert acts[("review-pr", "SKILL.md")] == skill_install.CONFLICT
    assert dest.read_text(encoding="utf-8") == "customised\n"  # preserved
    # The other (unmodified) review-pr files were still removed.
    assert acts[("review-pr", "references/env-vars.md")] == skill_install.REMOVED


def test_uninstall_refuses_write_through_symlinked_ancestor(env):
    # Adversarial: after install, the skill dir is swapped for a symlink to an outside dir
    # holding a colliding file. Even --force uninstall must not unlink/back-up through it.
    skill_install.install_skills()
    import shutil
    real = env.target / "open-pr"
    outside = env.tmp / "outside2"
    outside.mkdir()
    victim = outside / "SKILL.md"
    victim.write_text("outside file to protect\n", encoding="utf-8")
    shutil.rmtree(real)
    real.symlink_to(outside, target_is_directory=True)

    summary = skill_install.install_skills(uninstall=True, force=True)
    op = [f for f in summary.files if f.skill == "open-pr"]
    assert op and op[0].action == skill_install.CONFLICT
    assert victim.exists()
    assert victim.read_text(encoding="utf-8") == "outside file to protect\n"
    assert not list(outside.glob("SKILL.md.bak-*"))


def test_force_uninstall_removes_modified_with_backup(env):
    skill_install.install_skills()
    dest = env.target / "review-pr" / "SKILL.md"
    dest.write_text("customised\n", encoding="utf-8")

    summary = skill_install.install_skills(uninstall=True, force=True)
    assert _actions(summary)[("review-pr", "SKILL.md")] == skill_install.REMOVED
    baks = list((env.target / "review-pr").glob("SKILL.md.bak-*"))
    assert len(baks) == 1 and baks[0].read_text(encoding="utf-8") == "customised\n"


def test_uninstall_leaves_legacy_createpr_without_force(env):
    skill_install.install_skills()
    legacy = env.target / "create-pr"
    legacy.mkdir()
    (legacy / "SKILL.md").write_text("stale legacy skill\n", encoding="utf-8")

    summary = skill_install.install_skills(uninstall=True)
    legacy_outcomes = [f for f in summary.files if f.skill == "create-pr"]
    assert legacy_outcomes and legacy_outcomes[0].action == skill_install.CONFLICT
    assert legacy.exists()  # left in place without --force


def test_force_uninstall_removes_legacy_createpr_with_backup(env):
    skill_install.install_skills()
    legacy = env.target / "create-pr"
    legacy.mkdir()
    (legacy / "SKILL.md").write_text("stale legacy skill\n", encoding="utf-8")

    summary = skill_install.install_skills(uninstall=True, force=True)
    legacy_outcomes = [f for f in summary.files if f.skill == "create-pr"]
    assert legacy_outcomes and legacy_outcomes[0].action == skill_install.REMOVED
    assert not legacy.exists()
    # Backed up OUTSIDE the skills root, not as a sibling — a sibling dir would still
    # contain SKILL.md under the root Claude Code scans and stay discoverable there.
    assert not list(env.target.glob("create-pr.bak-*"))
    assert list(env.target.parent.glob("create-pr.bak-*"))


def test_install_reports_legacy_createpr_without_force(env):
    # The documented upgrade path is a plain re-run of install-skills (no --uninstall).
    # A stale create-pr dir left by the old manual snippet must surface there too.
    skill_install.install_skills()
    legacy = env.target / "create-pr"
    legacy.mkdir()
    (legacy / "SKILL.md").write_text("stale legacy skill\n", encoding="utf-8")

    summary = skill_install.install_skills()
    legacy_outcomes = [f for f in summary.files if f.skill == "create-pr"]
    assert legacy_outcomes and legacy_outcomes[0].action == skill_install.CONFLICT
    assert legacy.exists()  # left in place without --force
    assert (env.target / "open-pr" / "SKILL.md").exists()  # current skills still installed


def test_force_install_removes_legacy_createpr_with_backup(env):
    skill_install.install_skills()
    legacy = env.target / "create-pr"
    legacy.mkdir()
    (legacy / "SKILL.md").write_text("stale legacy skill\n", encoding="utf-8")

    summary = skill_install.install_skills(force=True)
    legacy_outcomes = [f for f in summary.files if f.skill == "create-pr"]
    assert legacy_outcomes and legacy_outcomes[0].action == skill_install.REMOVED
    assert not legacy.exists()
    # Backed up OUTSIDE the skills root, not as a sibling — a sibling dir would still
    # contain SKILL.md under the root Claude Code scans and stay discoverable there.
    assert not list(env.target.glob("create-pr.bak-*"))
    assert list(env.target.parent.glob("create-pr.bak-*"))


# ── Per-file error isolation (atomic; one failure doesn't block the rest) ─────────

def test_write_error_is_isolated_not_fatal(env):
    # Put a regular FILE where the open-pr skill DIR belongs → its SKILL.md write fails,
    # but review-pr still installs cleanly.
    env.target.mkdir(parents=True)
    (env.target / "open-pr").write_text("i am a file, not a dir\n", encoding="utf-8")

    summary = skill_install.install_skills()
    acts = _actions(summary)
    assert acts[("open-pr", "SKILL.md")] == skill_install.ERROR
    assert acts[("review-pr", "SKILL.md")] == skill_install.INSTALL
    assert summary.had_error
    assert (env.target / "review-pr" / "SKILL.md").exists()  # safe files still written


def test_sidecar_write_failure_is_reported_not_raised(env):
    # $XDG_CONFIG_HOME/buddhi is an existing FILE (not a dir) — _write_sidecar's
    # mkdir(parents=True) can never succeed. Files must still install; the sidecar
    # failure must come back as an ERROR outcome, never an uncaught exception.
    env.sidecar.parent.parent.mkdir(parents=True, exist_ok=True)
    env.sidecar.parent.write_text("not the buddhi config dir\n", encoding="utf-8")

    summary = skill_install.install_skills()  # must not raise
    prov = [f for f in summary.files if f.skill == "(provenance)"]
    assert prov and prov[0].action == skill_install.ERROR
    assert summary.had_error
    assert (env.target / "review-pr" / "SKILL.md").exists()  # files installed regardless
    assert not env.sidecar.exists()  # provenance genuinely was not recorded


# ── No-longer-bundled prune (and what it must NEVER touch) ───────────────────────

def _record(env, key: str, hash_: str) -> None:
    """Add one raw sidecar record under ``key`` (a string, so non-canonical spellings
    survive — ``Path`` would collapse them before they ever reach the file)."""
    sc = json.loads(env.sidecar.read_text())
    sc["files"][key] = {"version": package_version(), "hash": hash_}
    env.sidecar.write_text(json.dumps(sc), encoding="utf-8")


def test_stale_record_is_pruned_when_ours_unmodified(env):
    """The genuine prune still works: a recorded file whose source is no longer bundled
    and whose bytes are ours-unmodified is removed and its record dropped."""
    skill_install.install_skills()
    stale = env.target / "open-pr" / "gone.md"
    stale.write_text("dropped upstream\n", encoding="utf-8")
    _record(env, str(stale), content_hash("dropped upstream\n"))

    summary = skill_install.install_skills()
    assert _actions(summary)[("open-pr", "gone.md")] == skill_install.REMOVED
    assert not stale.exists()
    assert str(stale) not in json.loads(env.sidecar.read_text())["files"]


def test_stale_record_modified_is_conflict_then_removed_with_force(env):
    """…and a MODIFIED no-longer-bundled file is preserved as a CONFLICT until --force,
    which backs it up first."""
    skill_install.install_skills()
    stale = env.target / "open-pr" / "gone.md"
    stale.write_text("user edited this\n", encoding="utf-8")
    _record(env, str(stale), content_hash("the bytes we once wrote\n"))

    summary = skill_install.install_skills()
    assert _actions(summary)[("open-pr", "gone.md")] == skill_install.CONFLICT
    assert stale.read_text(encoding="utf-8") == "user edited this\n"

    summary = skill_install.install_skills(force=True)
    assert _actions(summary)[("open-pr", "gone.md")] == skill_install.REMOVED
    assert not stale.exists()
    baks = list((env.target / "open-pr").glob("gone.md.bak-*"))
    assert len(baks) == 1 and baks[0].read_text(encoding="utf-8") == "user edited this\n"


# Non-canonical spellings of one currently-bundled destination. Each normalizes back onto
# ``<root>/open-pr/SKILL.md`` but is a DIFFERENT string, so a raw-string "still bundled?"
# check would let the prune act on a file the install loop simultaneously keeps.
_ALIAS_SPELLINGS = ("{root}/open-pr/./SKILL.md",
                    "{root}/open-pr//SKILL.md",
                    "{root}/open-pr/references/../SKILL.md",
                    "{root}/./open-pr/SKILL.md")


@pytest.mark.parametrize("spelling", _ALIAS_SPELLINGS)
def test_noncanonical_key_never_prunes_a_bundled_file(env, spelling):
    """DATA LOSS GUARD: a sidecar key that is a non-canonical spelling of a CURRENTLY
    bundled file must never enter the prune — a plain (force=False) re-run leaves the
    file on disk, with its content and its canonical record intact."""
    skill_install.install_skills()
    dest = env.target / "open-pr" / "SKILL.md"
    installed = dest.read_text(encoding="utf-8")
    # The alias carries the file's REAL current hash — i.e. it would match
    # ``ours_unmodified`` and be unlink()ed, with no backup, by an unfixed prune.
    _record(env, spelling.format(root=env.target), content_hash(installed))

    summary = skill_install.install_skills()
    assert dest.exists()                                        # the whole point
    assert dest.read_text(encoding="utf-8") == installed        # byte-identical
    assert not any(f.action == skill_install.REMOVED for f in summary.files)
    assert not any("no longer bundled" in f.detail for f in summary.files)
    # Exactly ONE verdict for that destination — never a keep from the install loop plus a
    # contradictory prune verdict for the same file in the same run.
    assert len([f for f in summary.files if f.path == dest]) == 1
    assert json.loads(env.sidecar.read_text())["files"][str(dest)]["hash"] == content_hash(installed)


@pytest.mark.parametrize("spelling", _ALIAS_SPELLINGS)
def test_noncanonical_key_never_prunes_a_user_modified_bundled_file(env, spelling):
    """Same alias, but the on-disk file is USER-MODIFIED: it stays a preserved CONFLICT
    (the install loop's verdict) and is never removed or backed up by the prune."""
    skill_install.install_skills()
    dest = env.target / "open-pr" / "SKILL.md"
    recorded = json.loads(env.sidecar.read_text())["files"][str(dest)]["hash"]
    dest.write_text("my own edit\n", encoding="utf-8")
    _record(env, spelling.format(root=env.target), recorded)

    summary = skill_install.install_skills()
    assert _actions(summary)[("open-pr", "SKILL.md")] == skill_install.CONFLICT
    assert dest.read_text(encoding="utf-8") == "my own edit\n"   # untouched
    assert not any(f.action == skill_install.REMOVED for f in summary.files)
    assert not list(dest.parent.glob("SKILL.md.bak-*"))          # nothing moved aside
    assert len([f for f in summary.files if f.path == dest]) == 1  # one verdict, not two


def test_hardlinked_alias_key_never_prunes_a_bundled_file(env):
    """A key that is a HARD LINK to a bundled file is a spelling no amount of lexical
    normalizing reveals — the prune must still refuse it (filesystem-identity guard)."""
    skill_install.install_skills()
    dest = env.target / "open-pr" / "SKILL.md"
    installed = dest.read_text(encoding="utf-8")
    alias = env.target / "open-pr" / "alias.md"
    os.link(dest, alias)  # same inode, second name
    _record(env, str(alias), content_hash(installed))

    summary = skill_install.install_skills()
    assert dest.exists() and dest.read_text(encoding="utf-8") == installed
    # The alias is left alone too: the prune cannot tell an alias-of-a-live-file from the
    # live file itself, and leaving a stale name is always the safe side of that call.
    assert alias.exists()
    assert not any(f.action == skill_install.REMOVED for f in summary.files)


def test_case_variant_key_never_prunes_a_bundled_file(env):
    """On a case-INSENSITIVE filesystem (macOS APFS by default) ``…/OPEN-PR/SKILL.md`` is a
    different STRING but the SAME file — normalization cannot tell, so identity must."""
    skill_install.install_skills()
    dest = env.target / "open-pr" / "SKILL.md"
    upper = env.target / "OPEN-PR" / "SKILL.md"
    if not upper.exists():
        pytest.skip("case-sensitive filesystem — the case-variant alias is a different file")
    installed = dest.read_text(encoding="utf-8")
    _record(env, str(upper), content_hash(installed))

    summary = skill_install.install_skills()
    assert dest.exists()                                     # NOT deleted out from under us
    assert dest.read_text(encoding="utf-8") == installed
    assert not any(f.action == skill_install.REMOVED for f in summary.files)
    assert not any("no longer bundled" in f.detail for f in summary.files)


@pytest.mark.parametrize("uninstall", [False, True])
def test_sidecar_key_naming_a_directory_is_never_moved_aside(env, uninstall):
    """A record naming a DIRECTORY is corruption. Even under --force it must stay a
    CONFLICT: backing it up would move a live skill dir (plus the user's own files in it)
    to a ``.bak-<ts>`` sibling that Claude Code still scans as a skill."""
    skill_install.install_skills()
    skill_dir = env.target / "open-pr"
    (skill_dir / "my-notes.md").write_text("my own notes\n", encoding="utf-8")
    _record(env, str(skill_dir), content_hash("whatever\n"))

    summary = skill_install.install_skills(force=True, uninstall=uninstall)
    assert skill_dir.is_dir()                                    # still a live skill dir
    assert (skill_dir / "my-notes.md").read_text(encoding="utf-8") == "my own notes\n"
    assert not list(env.target.glob("open-pr.bak-*"))            # nothing moved aside
    assert not list(env.target.parent.glob("open-pr.bak-*"))
    dir_outcomes = [f for f in summary.files if f.path == skill_dir]
    assert dir_outcomes and all(f.action == skill_install.CONFLICT for f in dir_outcomes)
    # The dead record is retired, so the same CONFLICT does not recur on every future run.
    assert str(skill_dir) not in json.loads(env.sidecar.read_text())["files"]
    later = skill_install.install_skills(force=True, uninstall=uninstall)
    assert not any(f.path == skill_dir for f in later.files)


def test_directory_record_is_reported_but_kept_under_dry_run(env):
    """--dry-run writes nothing — including the sidecar — so the dead directory record is
    reported and still there afterwards."""
    skill_install.install_skills()
    skill_dir = env.target / "open-pr"
    _record(env, str(skill_dir), content_hash("whatever\n"))

    summary = skill_install.install_skills(force=True, dry_run=True)
    assert [f.action for f in summary.files if f.path == skill_dir] == [skill_install.CONFLICT]
    assert str(skill_dir) in json.loads(env.sidecar.read_text())["files"]


def test_deeply_nested_sidecar_reads_as_empty(env):
    """A structurally corrupt sidecar makes ``json.loads`` raise RecursionError, which is
    neither OSError nor ValueError — it must still degrade to an empty read."""
    env.sidecar.parent.mkdir(parents=True, exist_ok=True)
    env.sidecar.write_text("[" * 20000 + "]" * 20000, encoding="utf-8")

    assert skill_install._load_sidecar(env.sidecar) == {}
    summary = skill_install.install_skills()  # must not raise
    assert (env.target / "open-pr" / "SKILL.md").exists()
    assert set(_actions(summary).values()) == {skill_install.INSTALL}


@pytest.mark.parametrize("bad", ["\x00", "\ud800"])
def test_unusable_sidecar_key_never_raises(env, bad):
    """A sidecar key is untrusted input. An embedded NUL (or a lone surrogate) makes a raw
    ``os.lstat`` raise ValueError where pathlib swallows it — every mode must still return
    a summary rather than abort the run with a bare traceback."""
    skill_install.install_skills()
    _record(env, f"{env.target}/open{bad}-pr/SKILL.md", content_hash("x\n"))

    for kwargs in ({}, {"dry_run": True}, {"force": True}):
        summary = skill_install.install_skills(**kwargs)   # must not raise
        assert isinstance(summary, skill_install.InstallSummary)
    assert (env.target / "open-pr" / "SKILL.md").exists()  # the real tree is untouched
    assert isinstance(skill_install.install_skills(uninstall=True), skill_install.InstallSummary)


def test_sidecar_key_equal_to_root_is_not_actionable(env):
    """A corrupt record naming the skills ROOT itself is rejected outright — otherwise
    --force would back up (i.e. move away) the ENTIRE skills tree in one step."""
    skill_install.install_skills()
    _record(env, str(env.target), content_hash("whatever\n"))

    summary = skill_install.install_skills(force=True)
    assert env.target.is_dir()
    assert (env.target / "open-pr" / "SKILL.md").exists()
    assert not list(env.target.parent.glob(f"{env.target.name}.bak-*"))
    assert not any(f.path == env.target for f in summary.files)

    # Same on the uninstall path, where the record loop is the only gate.
    summary = skill_install.install_skills(uninstall=True, force=True)
    assert env.target.is_dir()
    assert not list(env.target.parent.glob(f"{env.target.name}.bak-*"))
    assert not any(f.path == env.target for f in summary.files)


# Every mode, so a key that upsets a filesystem probe cannot abort ANY of them — least of
# all --dry-run, which is documented to write nothing and merely report.
_ALL_MODES = [{}, {"force": True}, {"dry_run": True}, {"uninstall": True}]
_MODE_IDS = ["plain", "force", "dry-run", "uninstall"]


@pytest.mark.parametrize("kwargs", _ALL_MODES, ids=_MODE_IDS)
def test_overlong_sidecar_key_completes_normally(env, kwargs):
    """An overlong final component makes ``os.lstat`` fail ENAMETOOLONG — which
    ``Path.exists()`` / ``is_symlink()`` re-raise rather than swallow. The run must still
    report the record instead of dying with a bare traceback."""
    skill_install.install_skills()
    key = f"{env.target}/open-pr/{'x' * 5000}"
    _record(env, key, content_hash("x\n"))

    summary = skill_install.install_skills(**kwargs)  # must not raise
    outs = [f for f in summary.files if str(f.path) == key]
    assert len(outs) == 1 and outs[0].action == skill_install.NOOP  # provably absent
    assert not summary.had_error


class _Blocked(BaseException):
    """Raised by :func:`_no_blocking`. A BaseException on purpose: the code under test
    swallows ``OSError``, and the root conftest breaker raises ``TimeoutError`` — an
    OSError subclass — so a hang there is caught by ``_hash_on_disk`` and silently becomes
    a SLOW PASS instead of a failure. This one cannot be absorbed."""


@contextmanager
def _no_blocking(seconds: int = 5):
    """Fail — do not merely delay — if the wrapped call blocks. It also replaces the outer
    per-test breaker for its duration, restoring the previous handler on the way out."""
    def _fire(signum, frame):
        raise _Blocked(f"install_skills blocked for more than {seconds}s")

    old = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


@pytest.mark.parametrize("kwargs", _ALL_MODES, ids=_MODE_IDS)
def test_fifo_record_completes_without_hanging(env, kwargs):
    """A record naming a FIFO must never be opened: reading one with no writer blocks the
    whole run forever. It is not a regular file, so it is reported and left alone."""
    skill_install.install_skills()
    fifo = env.target / "open-pr" / "pipe.md"
    os.mkfifo(fifo)
    _record(env, str(fifo), content_hash("x\n"))

    with _no_blocking(5):  # must not raise AND must not block
        summary = skill_install.install_skills(**kwargs)
    assert [f.action for f in summary.files if f.path == fifo] == [skill_install.CONFLICT]
    assert fifo.is_fifo()          # never opened, never moved aside
    assert not list(fifo.parent.glob("pipe.md.bak-*"))


def test_unreadable_record_keeps_its_provenance(env):
    """When a probe cannot tell what is at a recorded path, the record is KEPT and the
    failure reported — dropping it would orphan a file that may still be on disk."""
    skill_install.install_skills()
    stale_dir = env.target / "open-pr" / "old"
    stale_dir.mkdir()
    stale = stale_dir / "gone.md"
    stale.write_text("still here\n", encoding="utf-8")
    _record(env, str(stale), content_hash("still here\n"))
    stale_dir.chmod(0o000)  # no search bit → lstat fails EACCES
    try:
        summary = skill_install.install_skills()  # must not raise
        outs = [f for f in summary.files if f.path == stale]
        assert len(outs) == 1 and outs[0].action == skill_install.ERROR
        assert str(stale) in json.loads(env.sidecar.read_text())["files"]
    finally:
        stale_dir.chmod(0o700)
    assert stale.read_text(encoding="utf-8") == "still here\n"


def test_dry_run_alias_key_is_skipped_lexically(env):
    """The LEXICAL half of the prune's skip is load-bearing exactly where the destination
    has no inode yet: in a --dry-run first install nothing is on disk, so the (dev, ino)
    identity guard has nothing to match on and only the normalized-string compare keeps a
    currently-bundled destination out of the prune. Without it the same run reports two
    contradictory verdicts for one file — install, and 'no longer bundled'."""
    env.sidecar.parent.mkdir(parents=True, exist_ok=True)
    env.sidecar.write_text(json.dumps({"schema": 1, "files": {
        f"{env.target}/open-pr/./SKILL.md": {"version": "0.0.1", "hash": content_hash("x\n")},
    }}), encoding="utf-8")
    dest = env.target / "open-pr" / "SKILL.md"

    summary = skill_install.install_skills(dry_run=True)
    assert [f.action for f in summary.files if f.path == dest] == [skill_install.INSTALL]
    assert not any("no longer bundled" in f.detail for f in summary.files)
    assert not env.target.exists()  # --dry-run still wrote nothing


# ── Unreadable config dir: the sidecar read fails SAFE, never raises ──────────────

def test_load_sidecar_on_unsearchable_parent_returns_empty(env):
    """``Path.exists()`` itself raises PermissionError when the sidecar's parent dir has
    no search bit; that must read as an empty (=> nothing is ours => nothing clobbered)
    sidecar, not abort the run with a bare traceback."""
    env.sidecar.parent.mkdir(parents=True, exist_ok=True)
    env.sidecar.write_text('{"schema": 1, "files": {}}', encoding="utf-8")
    env.sidecar.parent.chmod(0o000)
    try:
        assert skill_install._load_sidecar(env.sidecar) == {}
        # And a whole install still completes (files installed, no exception).
        summary = skill_install.install_skills()
        assert (env.target / "open-pr" / "SKILL.md").exists()
        assert set(_actions(summary).values()) >= {skill_install.INSTALL}
    finally:
        env.sidecar.parent.chmod(0o700)  # so tmp_path cleanup can recurse


# ── CLI subcommand ────────────────────────────────────────────────────────────────

def test_cli_install_skills_returns_zero(env, capsys):
    rc = cli.main(["install-skills"])
    assert rc == 0
    out = capsys.readouterr().out
    assert str(env.target) in out
    assert "installed" in out


def test_cli_conflict_exit_zero(env, capsys):
    cli.main(["install-skills"])
    (env.target / "review-pr" / "SKILL.md").write_text("edit\n", encoding="utf-8")
    rc = cli.main(["install-skills"])
    assert rc == 0  # a preserved conflict is a safe, expected outcome → exit 0


def test_cli_error_exit_nonzero(env):
    env.target.mkdir(parents=True)
    (env.target / "open-pr").write_text("blocking file\n", encoding="utf-8")
    assert cli.main(["install-skills"]) == 1


def test_cli_dry_run_and_uninstall(env, capsys):
    assert cli.main(["install-skills", "--dry-run"]) == 0
    assert not env.target.exists()
    capsys.readouterr()
    assert cli.main(["install-skills"]) == 0
    capsys.readouterr()
    assert cli.main(["install-skills", "--uninstall"]) == 0
    assert not (env.target / "review-pr").exists()


# ── Doc-swap guard ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("doc", ["README.md", "GETTING_STARTED.md"])
def test_docs_swapped_to_install_skills(doc):
    text = (REPO_ROOT / doc).read_text(encoding="utf-8")
    assert "rm -rf ~/.claude/skills" not in text
    assert "buddhi-review install-skills" in text
    # The stale "re-run after every upgrade / it copies rather than links" caveat is gone.
    assert "Re-run this block" not in text
