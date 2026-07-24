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


@pytest.mark.parametrize("bad", ["\x00", "\ud800"])
def test_unusable_sidecar_key_outcome_is_printable(env, bad):
    """Not raising inside :mod:`skill_install` isn't enough on its own: the CLI prints
    ``f.skill``/``f.rel`` straight to stdout, so a lone surrogate (or NUL) surviving into
    those fields crashes the run one layer up with ``UnicodeEncodeError`` even though
    ``install_skills`` itself returned cleanly. Every outcome's ``skill``/``rel`` must
    round-trip through UTF-8 unchanged."""
    skill_install.install_skills()
    key = f"{env.target}/open{bad}-pr/SKILL.md"
    _record(env, key, content_hash("x\n"))

    summary = skill_install.install_skills()
    matches = [f for f in summary.files if bad in str(f.path)]
    assert matches
    for f in matches:
        f.skill.encode("utf-8")  # must not raise UnicodeEncodeError
        f.rel.encode("utf-8")


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
    """An overlong path makes ``os.lstat`` fail ENAMETOOLONG — which ``Path.exists()`` /
    ``is_symlink()`` re-raise rather than swallow. The run must still report the record
    instead of dying with a bare traceback.

    ENAMETOOLONG is a limit on the path given to the syscall, NOT proof of absence (a file
    reached relatively from a deep working directory exists while its absolute path cannot
    be stat'ed), so the record is reported as an error and KEPT, never retired."""
    skill_install.install_skills()
    key = f"{env.target}/open-pr/{'x' * 5000}"
    _record(env, key, content_hash("x\n"))

    summary = skill_install.install_skills(**kwargs)  # must not raise
    outs = [f for f in summary.files if str(f.path) == key]
    assert len(outs) == 1 and outs[0].action == skill_install.ERROR
    assert "record kept" in outs[0].detail
    assert key in json.loads(env.sidecar.read_text())["files"]  # never silently retired


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


@pytest.mark.parametrize("kwargs", _ALL_MODES, ids=_MODE_IDS)
def test_unreadable_record_keeps_its_provenance(env, kwargs):
    """When a probe cannot tell what is at a recorded path, the record is KEPT and the
    failure reported — dropping it would orphan a file that may still be on disk. Both
    record loops must behave this way, so every mode is exercised (``--uninstall`` reaches
    :func:`_uninstall`'s loop, the rest reach the prune in :func:`_install`)."""
    skill_install.install_skills()
    stale_dir = env.target / "open-pr" / "old"
    stale_dir.mkdir()
    stale = stale_dir / "gone.md"
    stale.write_text("still here\n", encoding="utf-8")
    _record(env, str(stale), content_hash("still here\n"))
    stale_dir.chmod(0o000)  # no search bit → lstat fails EACCES
    try:
        summary = skill_install.install_skills(**kwargs)  # must not raise
        outs = [f for f in summary.files if f.path == stale]
        assert len(outs) == 1 and outs[0].action == skill_install.ERROR
        assert "could not inspect it — record kept" in outs[0].detail
        assert str(stale) in json.loads(env.sidecar.read_text())["files"]
    finally:
        stale_dir.chmod(0o700)
    assert stale.read_text(encoding="utf-8") == "still here\n"


@pytest.mark.parametrize("uninstall", [False, True], ids=["install", "uninstall"])
def test_symlink_record_backup_failure_is_reported_not_raised(env, uninstall):
    """``--force`` on a recorded SYMLINK moves the link aside, which needs write permission
    on its parent. A read-only skill dir must yield an ERROR outcome — the same treatment
    the regular-file branch beside it already gives — never a raise."""
    skill_install.install_skills()
    legacy = env.target / "legacy-skill"
    legacy.mkdir()
    link = legacy / "NOTES.md"
    link.symlink_to("/etc/hosts")
    _record(env, str(link), content_hash("x\n"))
    legacy.chmod(0o500)  # searchable, NOT writable → _backup's os.replace fails EACCES
    try:
        summary = skill_install.install_skills(force=True, uninstall=uninstall)  # no raise
        outs = [f for f in summary.files if f.path == link]
        assert len(outs) == 1 and outs[0].action == skill_install.ERROR
        assert str(link) in json.loads(env.sidecar.read_text())["files"]  # record kept
    finally:
        legacy.chmod(0o700)
    assert link.is_symlink()                       # the link itself is untouched
    assert not list(legacy.glob("NOTES.md.bak-*"))


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


# ── The BUNDLED-tree loop: hostile on-disk state never raises and never blocks ────
#
# The record loops above are probed through ``_lstat_probe``; these cover the loop that
# walks the BUNDLED tree (``_decide_action`` / ``_handle_legacy_dirs``), which used to probe
# through pathlib and so re-raised EACCES and blocked on a FIFO.

def _review_pr_file_count() -> int:
    return len([1 for skill, _rel, _src in _src_files() if skill == "review-pr"])


@pytest.mark.parametrize("kwargs", _ALL_MODES, ids=_MODE_IDS)
def test_unreadable_skill_dir_never_raises_and_sibling_is_still_processed(env, kwargs):
    """An installed skill dir with no search bit made every pathlib probe in the bundled
    loop raise PermissionError straight out of ``install_skills`` — exit 1, no summary, and
    the healthy skill beside it never processed. Now it is one ERROR outcome and the run
    continues; the sibling assertion is the one that proves it."""
    skill_install.install_skills()
    (env.target / "open-pr").chmod(0o000)
    try:
        summary = skill_install.install_skills(**kwargs)  # must not raise
        assert isinstance(summary, skill_install.InstallSummary)
        blocked = [f for f in summary.files if f.skill == "open-pr"]
        assert blocked and all(f.action == skill_install.ERROR for f in blocked)
        assert all("could not inspect" in f.detail for f in blocked)
        assert summary.had_error  # the CLI exits 1 — the run genuinely could not tell
        # THE POINT: the healthy sibling was processed to completion regardless.
        healthy = [f for f in summary.files if f.skill == "review-pr"]
        assert len(healthy) == _review_pr_file_count()
        assert not any(f.action == skill_install.ERROR for f in healthy)
    finally:
        (env.target / "open-pr").chmod(0o700)
    # Nothing was written into (or moved out of) the unreadable directory.
    assert (env.target / "open-pr" / "SKILL.md").exists()
    assert not list((env.target / "open-pr").glob("*.bak-*"))


@pytest.mark.parametrize("kwargs", _ALL_MODES, ids=_MODE_IDS)
def test_unsearchable_skills_root_completes_in_every_mode(env, kwargs):
    """``_handle_legacy_dirs`` probed with ``exists()``/``is_symlink()``, so an unsearchable
    skills ROOT aborted the run there too — after the per-file loop had already done its
    work, leaving a bare traceback instead of the summary that reports it."""
    skill_install.install_skills()
    legacy = env.target / "create-pr"
    legacy.mkdir()
    (legacy / "SKILL.md").write_text("stale legacy skill\n", encoding="utf-8")
    env.target.chmod(0o600)  # readable, NOT searchable
    try:
        summary = skill_install.install_skills(**kwargs)  # must not raise
        assert isinstance(summary, skill_install.InstallSummary)
        legacy_outs = [f for f in summary.files if f.skill == "create-pr"]
        assert len(legacy_outs) == 1 and legacy_outs[0].action == skill_install.ERROR
        assert "could not tell whether a legacy create-pr directory is there" in legacy_outs[0].detail
    finally:
        env.target.chmod(0o700)
    assert (legacy / "SKILL.md").read_text(encoding="utf-8") == "stale legacy skill\n"
    assert not list(env.target.parent.glob("create-pr.bak-*"))  # never acted on blind


def test_unsearchable_root_does_not_assert_a_legacy_dir_that_is_absent(env):
    """The normal modern state has NO ``create-pr`` at all. An unsearchable root still
    cannot see that, so the message must not name the directory as a thing that is there —
    asserting something the user cannot find is the exact defect class this step removes."""
    skill_install.install_skills()
    assert not (env.target / "create-pr").exists()
    env.target.chmod(0o600)
    try:
        summary = skill_install.install_skills()
        legacy_outs = [f for f in summary.files if f.skill == "create-pr"]
        assert len(legacy_outs) == 1
        detail = legacy_outs[0].detail
        assert "could not tell whether" in detail
        assert "left untouched" in detail
    finally:
        env.target.chmod(0o700)


@pytest.mark.parametrize("kwargs", _ALL_MODES, ids=_MODE_IDS)
def test_fifo_at_a_bundled_destination_completes_without_hanging(env, kwargs):
    """THE WORST ONE: a FIFO at a BUNDLED destination needs no sidecar record at all, and
    ``_hash_on_disk``'s ``read_text`` on it blocked forever with no writer — plain,
    ``--force`` AND ``--dry-run`` alike. A read-only, report-only mode must never block."""
    skill_install.install_skills()
    dest = env.target / "open-pr" / "SKILL.md"
    dest.unlink()
    os.mkfifo(dest)

    with _no_blocking(5):  # must not block AND must not raise
        summary = skill_install.install_skills(**kwargs)
    outs = [f for f in summary.files if f.path == dest]
    assert len(outs) == 1
    if kwargs == {"force": True}:
        # --force is the one escape hatch, and it resolves the FIFO the same way it
        # resolves a symlink: the thing is MOVED aside (never opened) and a real file is
        # written in its place, so a FIFO-blocked install is not unfixable forever.
        assert outs[0].action == skill_install.UPDATE
        assert dest.is_file() and not dest.is_fifo()
        baks = list(dest.parent.glob("SKILL.md.bak-*"))
        assert len(baks) == 1 and baks[0].is_fifo()
    else:
        assert outs[0].action == skill_install.CONFLICT
        assert dest.is_fifo()                                  # never opened, never moved
        assert not list(dest.parent.glob("SKILL.md.bak-*"))


def _bind_unix_socket(path: Path):
    """Bind an AF_UNIX socket AT ``path``, or return ``None`` if this platform will not.
    Bound from inside the directory so the ~104-byte ``sun_path`` limit applies to the
    short relative name rather than the long tmp path."""
    import socket as _socket

    cwd = os.getcwd()
    try:
        os.chdir(path.parent)
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.bind(path.name)
        return sock
    except (AttributeError, OSError):
        return None
    finally:
        os.chdir(cwd)


@pytest.mark.parametrize("kind", ["fifo", "socket", "directory"])
def test_non_regular_file_at_a_bundled_destination_is_a_conflict(env, kind):
    """Anything that is not a proven REGULAR file is decided from the stat alone and is
    never opened. The detail naming the kind is the proof: a verdict reached by trying to
    READ the thing reports the generic "user-modified or foreign" reason instead."""
    skill_install.install_skills()
    dest = env.target / "open-pr" / "SKILL.md"
    dest.unlink()
    sock = None
    if kind == "fifo":
        os.mkfifo(dest)
        expected = "a FIFO where a regular file belongs"
    elif kind == "socket":
        sock = _bind_unix_socket(dest)
        if sock is None:
            pytest.skip("this platform cannot bind an AF_UNIX socket here")
        expected = "a socket where a regular file belongs"
    else:
        dest.mkdir()
        expected = "a directory where a regular file belongs"

    try:
        with _no_blocking(5):
            summary = skill_install.install_skills()
        outs = [f for f in summary.files if f.path == dest]
        assert len(outs) == 1 and outs[0].action == skill_install.CONFLICT
        assert expected in outs[0].detail
        assert not summary.had_error                    # a preserved conflict is not an error
        assert not list(dest.parent.glob("SKILL.md.bak-*"))
        assert os.path.lexists(dest)                    # left exactly as it was
    finally:
        if sock is not None:
            sock.close()


def test_hash_on_disk_never_opens_a_non_regular_file(env):
    """The structural half of the guarantee: even called directly on a FIFO — i.e. if a
    destination stopped being a regular file between the probe and the open — this returns
    rather than blocking, and it refuses to read THROUGH a symlink."""
    env.target.mkdir(parents=True)
    fifo = env.target / "pipe"
    os.mkfifo(fifo)
    real = env.target / "real.md"
    real.write_text("hello\n", encoding="utf-8")
    link = env.target / "link.md"
    link.symlink_to(real)

    with _no_blocking(5):
        assert skill_install._hash_on_disk(fifo) is None
    assert skill_install._hash_on_disk(link) is None            # never read through a link
    assert skill_install._hash_on_disk(real) == content_hash("hello\n")


# ── EACCES on an ancestor is could-not-tell, NOT "is a symlink" ───────────────────

def test_eacces_ancestor_is_not_blamed_on_a_symlink_in_the_bundled_loop(env):
    """``chmod 000`` on the review-pr skill dir hits BOTH ancestor branches at once:
    ``references/env-vars.md`` fails while inspecting its PARENT, ``SKILL.md`` on the final
    component. Neither may claim a symlink — there is none to remove."""
    skill_install.install_skills()
    (env.target / "review-pr").chmod(0o000)
    try:
        summary = skill_install.install_skills()
        outs = {f.rel: f for f in summary.files if f.skill == "review-pr"}
        assert outs["references/env-vars.md"].action == skill_install.ERROR
        assert "could not inspect a parent directory" in outs["references/env-vars.md"].detail
        assert outs["SKILL.md"].action == skill_install.ERROR
        assert "could not inspect it" in outs["SKILL.md"].detail
        assert not any("symlink" in f.detail for f in summary.files)
        assert summary.had_error
    finally:
        (env.target / "review-pr").chmod(0o700)


@pytest.mark.parametrize("kwargs", _ALL_MODES, ids=_MODE_IDS)
def test_eacces_ancestor_matches_the_final_component_in_the_record_loops(env, kwargs):
    """A recorded file under an unreadable ancestor was reported as ``a parent directory is
    a symlink — remove the symlinked directory manually`` at exit 0, while the SAME errno on
    the final component was an ERROR at exit 1. One condition, two contradictory answers,
    and a user sent hunting for a symlink that does not exist. Both now read alike."""
    skill_install.install_skills()
    # (a) the errno one level UP: the record's PARENT directory is unreadable.
    ghost = env.target / "ghost"
    (ghost / "sub").mkdir(parents=True)
    up = ghost / "sub" / "gone.md"
    up.write_text("still here\n", encoding="utf-8")
    _record(env, str(up), content_hash("still here\n"))
    # (b) the SAME errno on the FINAL component — the control this used to disagree with.
    deep = env.target / "spooky" / "deep"
    deep.mkdir(parents=True)
    final = deep / "gone.md"
    final.write_text("still here\n", encoding="utf-8")
    _record(env, str(final), content_hash("still here\n"))
    ghost.chmod(0o000)
    deep.chmod(0o000)
    try:
        summary = skill_install.install_skills(**kwargs)
        got = {f.path: f for f in summary.files}
        assert got[up].action == got[final].action == skill_install.ERROR
        assert "could not inspect it — record kept" in got[up].detail
        assert "could not inspect it — record kept" in got[final].detail
        assert "symlink" not in got[up].detail
        assert summary.had_error  # same exit code for the same condition
        recs = json.loads(env.sidecar.read_text())["files"]
        assert str(up) in recs and str(final) in recs  # neither record is retired
    finally:
        ghost.chmod(0o700)
        deep.chmod(0o700)
    assert up.read_text(encoding="utf-8") == "still here\n"      # nothing acted on blind
    assert final.read_text(encoding="utf-8") == "still here\n"


@pytest.mark.parametrize("uninstall", [False, True], ids=["prune", "uninstall"])
def test_unreadable_ancestor_beats_a_clean_destination_probe(env, monkeypatch, uninstall):
    """Both record loops probe the destination FIRST and its ancestors second, and take
    could-not-tell from EITHER. Resolving the destination traverses every ancestor, so a
    real permission state cannot make the two disagree — the divergence is faked here
    because what it guards is severe: with the ancestor's error dropped, the record falls
    through to the ours-unmodified branch and the file is UNLINKED, no backup, on the
    strength of a directory we could not read."""
    skill_install.install_skills()
    stale = env.target / "open-pr" / "gone.md"        # recorded, ours-unmodified, unbundled
    stale.write_text("dropped upstream\n", encoding="utf-8")
    _record(env, str(stale), content_hash("dropped upstream\n"))

    real_probe = skill_install._lstat_probe
    blinded = env.target / "open-pr"

    def _blind_one_ancestor(path):
        if Path(path) == blinded:
            return None, "[Errno 13] Permission denied (simulated)"
        return real_probe(path)

    monkeypatch.setattr(skill_install, "_lstat_probe", _blind_one_ancestor)
    summary = skill_install.install_skills(uninstall=uninstall)
    outs = [f for f in summary.files if f.path == stale]
    assert len(outs) == 1 and outs[0].action == skill_install.ERROR
    assert "could not inspect it — record kept" in outs[0].detail
    assert stale.read_text(encoding="utf-8") == "dropped upstream\n"   # never removed
    assert str(stale) in json.loads(env.sidecar.read_text())["files"]  # never retired


@pytest.mark.parametrize("kwargs", [{"force": True}, {"uninstall": True, "force": True}],
                         ids=["prune", "uninstall"])
def test_real_symlinked_ancestor_still_says_symlink_and_is_never_written_through(env, kwargs):
    """The other half: a REAL symlinked ancestor keeps the symlink verdict, and even
    ``--force`` never unlinks or backs up through it."""
    skill_install.install_skills()
    outside = env.tmp / "outside_stale"
    outside.mkdir()
    victim = outside / "gone.md"
    victim.write_text("PRECIOUS outside content\n", encoding="utf-8")
    (env.target / "stale").symlink_to(outside, target_is_directory=True)
    recorded = env.target / "stale" / "gone.md"
    _record(env, str(recorded), content_hash("PRECIOUS outside content\n"))

    summary = skill_install.install_skills(**kwargs)
    outs = [f for f in summary.files if f.path == recorded]
    assert len(outs) == 1 and outs[0].action == skill_install.CONFLICT
    assert "a parent directory is a symlink" in outs[0].detail
    assert victim.read_text(encoding="utf-8") == "PRECIOUS outside content\n"
    assert not list(outside.glob("gone.md.bak-*"))
    assert (env.target / "stale").is_symlink()


def test_symlink_loop_ancestor_is_still_named_as_a_symlink(env):
    """A self-referential skill dir makes the nested ``references/`` component fail ELOOP,
    so a walk that stopped at the first unreadable ancestor would report "could not inspect
    a parent directory" — when the link causing it sits one level up and IS nameable. The
    walk remembers the error and keeps going, so the actionable answer survives."""
    env.target.mkdir(parents=True)
    loop = env.target / "review-pr"
    loop.symlink_to(loop, target_is_directory=True)  # points at itself

    with _no_blocking(5):
        summary = skill_install.install_skills(force=True)
    outs = {f.rel: f for f in summary.files if f.skill == "review-pr"}
    assert outs["references/env-vars.md"].action == skill_install.CONFLICT
    assert "a parent directory is a symlink" in outs["references/env-vars.md"].detail
    assert loop.is_symlink()                                  # never written through
    assert not list(env.target.glob("review-pr.bak-*"))
    assert (env.target / "open-pr" / "SKILL.md").exists()      # the sibling still installs


def test_dry_run_writes_nothing_across_the_hostile_paths(env):
    """``--dry-run`` is documented to write nothing and merely report — and it is the mode a
    hang or a raise hurt most. Every new path at once, with nothing on disk changed."""
    skill_install.install_skills()
    fifo = env.target / "open-pr" / "SKILL.md"          # a FIFO at a bundled destination
    fifo.unlink()
    os.mkfifo(fifo)
    as_dir = env.target / "review-pr" / "SKILL.md"      # a directory where a file belongs
    as_dir.unlink()
    as_dir.mkdir()
    refs = env.target / "review-pr" / "references"      # its file becomes uninspectable
    legacy = env.target / "create-pr"                   # a stale legacy skill dir
    legacy.mkdir()
    (legacy / "SKILL.md").write_text("stale legacy skill\n", encoding="utf-8")
    ghost = env.target / "ghost"                        # a record under an unreadable dir
    (ghost / "sub").mkdir(parents=True)
    (ghost / "sub" / "gone.md").write_text("still here\n", encoding="utf-8")
    _record(env, str(ghost / "sub" / "gone.md"), content_hash("still here\n"))
    refs.chmod(0o000)
    ghost.chmod(0o000)
    before = json.loads(env.sidecar.read_text())
    try:
        with _no_blocking(5):
            summary = skill_install.install_skills(dry_run=True, force=True)
        assert isinstance(summary, skill_install.InstallSummary)
    finally:
        refs.chmod(0o700)
        ghost.chmod(0o700)
    assert fifo.is_fifo()                                        # not opened, not replaced
    assert as_dir.is_dir() and not any(as_dir.iterdir())         # not moved aside, not written
    assert (refs / "env-vars.md").exists()
    assert legacy.is_dir() and (legacy / "SKILL.md").exists()
    assert (ghost / "sub" / "gone.md").read_text(encoding="utf-8") == "still here\n"
    assert json.loads(env.sidecar.read_text()) == before         # sidecar untouched
    assert not list(env.target.rglob("*.bak-*"))                 # nothing moved aside
    assert not list(env.target.parent.glob("create-pr.bak-*"))


# ── Unreadable config dir: the sidecar read fails SAFE, never raises ──────────────

@pytest.mark.parametrize("kwargs", _ALL_MODES, ids=_MODE_IDS)
def test_sidecar_that_is_a_fifo_never_blocks(env, kwargs):
    """The sidecar sits at a fixed, predictable path in the user's own config dir, and it
    was read with a plain ``read_text`` — so a FIFO there blocked EVERY mode forever, before
    a single skill file was even looked at. It now reads as empty (the documented fail-safe:
    nothing is provably ours, so nothing is clobbered)."""
    skill_install.install_skills()
    dest = env.target / "review-pr" / "SKILL.md"
    installed = dest.read_text(encoding="utf-8")
    env.sidecar.unlink()
    os.mkfifo(env.sidecar)

    with _no_blocking(5):  # must not block AND must not raise
        summary = skill_install.install_skills(**kwargs)
    assert isinstance(summary, skill_install.InstallSummary)
    assert dest.read_text(encoding="utf-8") == installed  # no record ⇒ nothing clobbered
    assert not list(dest.parent.glob("SKILL.md.bak-*"))
    if kwargs in ({}, {"force": True}):
        # A real install rewrites the sidecar, so the corrupt FIFO heals itself.
        assert env.sidecar.is_file() and not env.sidecar.is_fifo()
    else:
        assert env.sidecar.is_fifo()  # --dry-run / --uninstall wrote nothing


def test_symlinked_sidecar_is_still_followed(env):
    """A sidecar SYMLINKED into a dotfiles repo is a legitimate setup, so — unlike a skill
    destination, where following a link is the very thing we refuse — the sidecar read
    follows links. UPDATE rather than CONFLICT is the proof that the record behind the link
    was actually read."""
    skill_install.install_skills()
    dest = env.target / "open-pr" / "SKILL.md"
    prior = "old managed content\n"
    dest.write_text(prior, encoding="utf-8")
    sc = json.loads(env.sidecar.read_text())
    sc["files"][str(dest)] = {"version": "0.0.1", "hash": content_hash(prior)}
    env.sidecar.write_text(json.dumps(sc), encoding="utf-8")
    real = env.tmp / "dotfiles-installed-skills.json"
    env.sidecar.rename(real)
    env.sidecar.symlink_to(real)

    summary = skill_install.install_skills()
    assert _actions(summary)[("open-pr", "SKILL.md")] == skill_install.UPDATE


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
