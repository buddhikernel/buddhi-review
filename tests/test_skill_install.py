"""Provenance-safe skill installer — the full 3-way state matrix, the never-clobber
safe default, ``--force`` / ``--dry-run`` / ``--uninstall``, idempotency across a second
registered transform, the CLI subcommand, and a doc-swap guard.

Every test points ``HOME`` / ``CLAUDE_CONFIG_DIR`` / ``XDG_CONFIG_HOME`` at a tmp tree so
nothing here ever touches the real ``~/.claude`` or ``~/.config``.
"""
from __future__ import annotations

import json
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
