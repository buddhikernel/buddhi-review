"""The canonical Buddhi config location, the legacy-file migration, and the config lock.

``config.config_path()`` resolves ``~/.config/buddhi/config.yaml`` (never
``$XDG_CONFIG_HOME``) and, before handing the path out, merges a legacy
``~/.config/review-loop/config.yaml`` into it at FILE level, then renames the legacy
file to ``config.yaml.migrated-<UTC timestamp>``. Every read-modify-write of the
config runs under ONE inter-process lock beside the file.

Every test here deletes ``BUDDHI_CONFIG`` (the suite-wide conftest pins it) and points
``HOME`` at a sandbox under ``tmp_path``; nothing touches the real ``~/.config`` or
``~/.cache``. Subprocess tests run the package from THIS checkout (``PYTHONPATH``).

Also here: the setup wizard's promotion of the bound repo's ``auto_merge`` /
``label_gated_ci`` to the top level, and the proof that a repo without its own
``label_gated_ci`` resolves exactly as it did before the promotion existed.
"""
import contextlib
import errno
import io
import json
import os
import re
import site
import stat
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest
import yaml

import buddhi_review
from buddhi_review import commit_push, config, plan_profile, wizard
from buddhi_review.loop import Comment

REPO = "acme/widgets"
FLEET = ["copilot", "codex"]
ROOT = Path(buddhi_review.__file__).resolve().parents[1]
BACKUP_RE = re.compile(r"^config\.yaml\.migrated-\d{8}T\d{6}Z(-\d+)?$")

# A full legacy config, shaped like the one earlier setup runs wrote.
LEGACY = {
    "plan": "max-5x",
    "active_reviewers": FLEET,
    "auto_on_open": {"copilot": True, "codex": True},
    "notifications": "console",
    "repo": REPO,
    "cwd": "/work/widgets",
    "repos": {REPO: {"active_reviewers": FLEET,
                     "auto_on_open": {"copilot": True, "codex": True},
                     "auto_merge": False, "label_gated_ci": False,
                     "test_command": "make test"}},
}


# ── Sandbox helpers ─────────────────────────────────────────────────────────────

@pytest.fixture
def home(monkeypatch, tmp_path):
    """A sandbox HOME with BUDDHI_CONFIG / XDG unset and a fresh once-per-process
    message memory, so each test sees its own migration lines."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("BUDDHI_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(config, "_reported", set())
    monkeypatch.setattr(config, "_deferred", set())
    return h


def _canonical(h):
    return h / ".config" / "buddhi" / "config.yaml"


def _legacy(h):
    return h / ".config" / "review-loop" / "config.yaml"


def _put(path, data, *, mtime_ns=None, raw=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None else yaml.safe_dump(data, sort_keys=False),
                    encoding="utf-8")
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _backups(h):
    d = h / ".config" / "review-loop"
    return sorted(p for p in d.iterdir() if p.name.startswith("config.yaml.migrated-")) \
        if d.is_dir() else []


def _tree(d):
    """{relative path: bytes} for every regular file under ``d``."""
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def _env(h, **extra):
    """Subprocess env: sandbox HOME, this checkout first on the import path, and no
    config override. The child keeps THIS interpreter's user site-packages: Python
    finds them through HOME, so a sandboxed HOME would hide every dependency
    installed with ``pip install --user`` (as CI does when the system site-packages
    are not writable) unless ``PYTHONUSERBASE`` pins where they are."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("BUDDHI_CONFIG", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR",
                        "BUDDHI_TEST_COMMAND")}
    env.setdefault("PYTHONUSERBASE", site.getuserbase())
    env["HOME"] = str(h)
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["BUDDHI_NO_UPDATE_CHECK"] = "1"
    env.update(extra)
    return env


def _cli(h, *args, timeout=60):
    return subprocess.run([sys.executable, "-m", "buddhi_review", *args], env=_env(h),
                          cwd=str(h), capture_output=True, text=True, timeout=timeout)


# ── Clause 1: one canonical file ────────────────────────────────────────────────

def test_default_path_is_the_canonical_location(home):
    assert config.config_path() == _canonical(home)
    # Nothing to migrate → nothing is created (not even the folder or a lock file).
    assert not (home / ".config").exists()


def test_xdg_config_home_is_ignored(home, monkeypatch, tmp_path):
    xdg = tmp_path / "xdg"
    _put(xdg / "buddhi" / "config.yaml", {"plan": "pro"})
    _put(xdg / "review-loop" / "config.yaml", {"plan": "pro"})
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert config.config_path() == _canonical(home)
    assert config.load_config() == {}
    # The XDG-located legacy file is not ours to migrate.
    assert (xdg / "review-loop" / "config.yaml").exists()
    assert not _canonical(home).exists()


def test_buddhi_config_override_wins_and_skips_the_migration(home, monkeypatch, tmp_path, capsys):
    _put(_legacy(home), LEGACY)
    before = _legacy(home).read_bytes()
    override = tmp_path / "elsewhere.yaml"
    monkeypatch.setenv("BUDDHI_CONFIG", str(override))
    assert config.config_path() == override
    assert _legacy(home).read_bytes() == before
    assert not _canonical(home).exists()
    assert _backups(home) == []
    assert capsys.readouterr().err == ""


# ── Clause 2: migration at file level ───────────────────────────────────────────

def test_legacy_only_is_migrated_and_backed_up(home, capsys):
    _put(_legacy(home), None, raw="# my notes\n" + yaml.safe_dump(LEGACY, sort_keys=False))
    original = _legacy(home).read_bytes()
    p = config.config_path()
    assert p == _canonical(home)
    assert _load(p) == LEGACY
    assert p.read_bytes() == original  # a byte-for-byte copy, comments included
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert not _legacy(home).exists()
    (backup,) = _backups(home)
    assert BACKUP_RE.match(backup.name)
    assert backup.read_bytes() == original
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600
    out, err = capsys.readouterr()
    assert out == ""
    lines = err.strip().splitlines()
    assert len(lines) == 1
    assert str(_canonical(home)) in lines[0] and str(backup) in lines[0]
    assert "No settings conflicted" in lines[0]


def test_backup_is_0600_even_when_the_legacy_file_was_world_readable(home):
    _put(_legacy(home), LEGACY)
    os.chmod(_legacy(home), 0o644)
    config.config_path()
    (backup,) = _backups(home)
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600


def test_second_resolve_is_a_noop(home, capsys):
    _put(_legacy(home), LEGACY)
    config.config_path()
    capsys.readouterr()
    snapshot = _canonical(home).read_bytes()
    mtime = os.stat(_canonical(home)).st_mtime_ns
    config.config_path()
    config.load_config()
    assert capsys.readouterr().err == ""
    assert _canonical(home).read_bytes() == snapshot
    assert os.stat(_canonical(home)).st_mtime_ns == mtime
    assert len(_backups(home)) == 1


def test_canonical_only_is_a_noop(home, capsys):
    _put(_canonical(home), {"plan": "pro", "known_repos": [REPO]})
    before = _canonical(home).read_bytes()
    assert config.config_path() == _canonical(home)
    assert _canonical(home).read_bytes() == before
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("shape", ["sidecar", "empty-folder"])
def test_migrates_when_the_canonical_folder_already_exists(home, shape, capsys):
    folder = home / ".config" / "buddhi"
    folder.mkdir(parents=True)
    if shape == "sidecar":
        (folder / "installed-skills.json").write_text('{"schema": 1, "files": {}}\n')
    before = _tree(folder)
    _put(_legacy(home), LEGACY)
    config.config_path()
    assert _load(_canonical(home)) == LEGACY
    after = _tree(folder)
    after.pop("config.yaml")
    after.pop("config.yaml.lock", None)
    assert after == before  # the sidecar (or nothing) — untouched
    assert "Config moved" in capsys.readouterr().err


def test_no_other_file_in_either_folder_is_touched(home):
    legacy_dir = home / ".config" / "review-loop"
    canon_dir = home / ".config" / "buddhi"
    _put(_legacy(home), LEGACY)
    (legacy_dir / "preferences.yaml").write_text("bias: {}\n")
    (legacy_dir / "notes.txt").write_text("keep me\n")
    canon_dir.mkdir(parents=True)
    (canon_dir / "installed-skills.json").write_text("{}\n")
    (canon_dir / "preferences.yaml").write_text("other: 1\n")
    legacy_before = {k: v for k, v in _tree(legacy_dir).items() if k != "config.yaml"}
    canon_before = _tree(canon_dir)
    config.config_path()
    legacy_after = {k: v for k, v in _tree(legacy_dir).items()
                    if not k.startswith("config.yaml.migrated-")}
    canon_after = {k: v for k, v in _tree(canon_dir).items()
                   if k not in ("config.yaml", "config.yaml.lock")}
    assert legacy_after == legacy_before
    assert canon_after == canon_before


@pytest.mark.parametrize("link", ["folder", "file"])
def test_a_legacy_path_linked_to_the_canonical_file_is_left_alone(home, link, capsys):
    """A compatibility symlink (the old folder → the new one, or the old file → the
    new file) makes both paths one file; renaming it would move the live config."""
    _put(_canonical(home), LEGACY)
    before = _canonical(home).read_bytes()
    if link == "folder":
        (home / ".config" / "review-loop").symlink_to(home / ".config" / "buddhi")
    else:
        (home / ".config" / "review-loop").mkdir()
        _legacy(home).symlink_to(_canonical(home))
    for _ in range(2):
        assert config.config_path() == _canonical(home)
    assert _canonical(home).read_bytes() == before
    assert not any(p.name.startswith("config.yaml.migrated-")
                   for p in (home / ".config" / "buddhi").iterdir())
    assert capsys.readouterr().err == ""


def test_a_symlinked_legacy_file_is_migrated_and_its_target_kept(home, tmp_path):
    """A legacy file that is a link into a dotfiles checkout: its settings move, the
    link itself becomes the backup, and the dotfiles target is not modified."""
    target = _put(tmp_path / "dotfiles" / "review.yaml", LEGACY)
    os.chmod(target, 0o644)
    before = (target.read_bytes(), stat.S_IMODE(os.stat(target).st_mode))
    (home / ".config" / "review-loop").mkdir(parents=True)
    _legacy(home).symlink_to(target)
    config.config_path()
    assert _load(_canonical(home)) == LEGACY
    (backup,) = _backups(home)
    assert backup.is_symlink() and os.readlink(backup) == str(target)
    assert (target.read_bytes(), stat.S_IMODE(os.stat(target).st_mode)) == before


def test_every_exists_check_sees_the_migrated_file(home, tmp_path):
    """The five callers that test ``.exists()`` BEFORE loading all resolve through
    ``config_path()``, so the legacy settings are in place before they look."""
    _put(_legacy(home), LEGACY)
    # commit_push: `config.load_config() if config.config_path().exists() else {}`.
    assert commit_push.resolve_test_command(str(tmp_path), REPO) == ["make", "test"]
    assert config.config_path().exists()


def test_set_repo_keys_on_the_default_path_keeps_the_legacy_settings(home):
    _put(_legacy(home), LEGACY)
    assert config.set_repo_keys("other/repo", {"active_reviewers": ["claude"]}) is True
    cfg = _load(_canonical(home))
    assert config.repo_entry(cfg, REPO) is not None
    assert config.repo_entry(cfg, "other/repo") == {"active_reviewers": ["claude"]}
    assert cfg["plan"] == "max-5x"


def test_write_global_default_on_the_default_path_keeps_the_legacy_settings(home):
    _put(_legacy(home), LEGACY)
    assert wizard._write_global_default(["claude"], {"claude": False}, config.config_path())
    cfg = _load(_canonical(home))
    assert cfg["active_reviewers"] == ["claude"]
    assert config.repo_entry(cfg, REPO)["test_command"] == "make test"


# ── Clause 3: both files present → lossless merge ───────────────────────────────

def test_both_present_merges_losslessly(home, capsys):
    t = time.time_ns()
    _put(_canonical(home), {"known_repos": ["other/repo"], "test_command": "pytest -x",
                            "repos": {"Acme/Widgets": {"auto_merge": True},
                                      "zeta/app": {"active_reviewers": ["claude"]}}},
         mtime_ns=t)
    _put(_legacy(home), {**LEGACY, "known_repos": ["acme/widgets", "other/repo"]},
         mtime_ns=t - 10**9)
    config.config_path()
    cfg = _load(_canonical(home))
    # Keys only one side held all survive.
    assert cfg["test_command"] == "pytest -x"
    assert cfg["plan"] == "max-5x" and cfg["active_reviewers"] == FLEET
    # known_repos is the union (canonical order first).
    assert cfg["known_repos"] == ["other/repo", "acme/widgets"]
    # repos merges per repo (case-insensitively, keeping the canonical spelling) …
    assert set(cfg["repos"]) == {"Acme/Widgets", "zeta/app"}
    entry = cfg["repos"]["Acme/Widgets"]
    # … and within a repo per key: the canonical side's auto_merge conflicts with the
    # legacy False; the canonical file is newer, so its True is kept.
    assert entry["auto_merge"] is True
    assert entry["test_command"] == "make test"
    assert entry["active_reviewers"] == FLEET
    assert len(_backups(home)) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert "1 setting(s) conflicted" in err[0] and "repos.Acme/Widgets.auto_merge" in err[0]
    assert str(_backups(home)[0]) in err[0]


@pytest.mark.parametrize("newer,expect", [("legacy", "pro"), ("canonical", "max-20x"),
                                          ("tie", "max-20x")])
def test_conflicts_take_the_newer_file_and_the_canonical_on_a_tie(home, newer, expect, capsys):
    t = time.time_ns()
    legacy_t = {"legacy": t, "canonical": t - 10**9, "tie": t}[newer]
    _put(_canonical(home), {"plan": "max-20x", "repos": {REPO: {"label_gated_ci": False}}},
         mtime_ns=t if newer != "legacy" else t - 10**9)
    _put(_legacy(home), {"plan": "pro", "repos": {REPO: {"label_gated_ci": True}}},
         mtime_ns=legacy_t)
    config.config_path()
    cfg = _load(_canonical(home))
    assert cfg["plan"] == expect
    assert cfg["repos"][REPO]["label_gated_ci"] is (expect == "pro")
    err = capsys.readouterr().err
    assert "2 setting(s) conflicted" in err and "plan" in err


def test_partial_canonical_known_repos_only_adopts_the_legacy_settings(home):
    """The file a loop writes before its gate on a machine never set up for the
    canonical location holds only ``known_repos``; it must end up with everything."""
    _put(_legacy(home), LEGACY, mtime_ns=time.time_ns() - 10**9)
    _put(_canonical(home), {"known_repos": [REPO]})
    config.config_path()
    cfg = _load(_canonical(home))
    assert cfg["known_repos"] == [REPO]
    assert {k: v for k, v in cfg.items() if k != "known_repos"} == LEGACY
    assert config.repo_entry(cfg, REPO) is not None and config.has_global_default(cfg)


@pytest.mark.parametrize("newer", ["legacy", "canonical"])
@pytest.mark.parametrize("canonical_side,legacy_side,expect", [
    # A value every reader treats as absent never overrides a real setting.
    ({"repos": None}, {"repos": LEGACY["repos"]}, {"repos": LEGACY["repos"]}),
    ({"repos": LEGACY["repos"]}, {"repos": "garbage"}, {"repos": LEGACY["repos"]}),
    ({"repos": {REPO: None}}, {"repos": LEGACY["repos"]}, {"repos": LEGACY["repos"]}),
    ({"repos": {REPO: {"auto_merge": None}}}, {"repos": {REPO: {"auto_merge": True}}},
     {"repos": {REPO: {"auto_merge": True}}}),
    ({"plan": None, "active_reviewers": None}, {"plan": "pro", "active_reviewers": FLEET},
     {"plan": "pro", "active_reviewers": FLEET}),
    ({"known_repos": {"a/b": 1}}, {"known_repos": [REPO]}, {"known_repos": [REPO]}),
    ({"plan": "pro"}, {"plan": None}, {"plan": "pro"}),
    ({"plan": "pro"}, {"plan": ""}, {"plan": "pro"}),
    ({"plan": ""}, {"plan": "pro"}, {"plan": "pro"}),
])
def test_a_null_or_malformed_side_never_overrides_a_real_setting(home, capsys, newer,
                                                                 canonical_side, legacy_side,
                                                                 expect):
    t = time.time_ns()
    _put(_canonical(home), canonical_side, mtime_ns=t if newer == "canonical" else t - 10**9)
    _put(_legacy(home), legacy_side, mtime_ns=t if newer == "legacy" else t - 10**9)
    config.config_path()
    cfg = _load(_canonical(home))
    for key, value in expect.items():
        assert cfg[key] == value
    assert "No settings conflicted" in capsys.readouterr().err


@pytest.mark.parametrize("canonical_repos", [{"zeta/app": {"auto_merge": True}},
                                             {"ACME/widgets": {"test_command": "make"}}],
                         ids=["other-repo", "same-repo"])
def test_case_variant_duplicates_inside_the_legacy_file_are_not_collapsed(home, capsys,
                                                                          canonical_repos):
    """Readers take the FIRST case-variant entry for a repo; the merge must not fold
    a later variant into it (which would change what the repo resolves to)."""
    legacy = {"repos": {"Acme/Widgets": {"auto_merge": True, "label_gated_ci": False},
                        "acme/widgets": {"auto_merge": False, "label_gated_ci": True}}}
    t = time.time_ns()
    _put(_canonical(home), {"repos": canonical_repos}, mtime_ns=t - 10**9)
    _put(_legacy(home), legacy, mtime_ns=t)  # newer: a folded variant would win
    config.config_path()
    cfg = _load(_canonical(home))
    assert config.auto_merge(cfg, REPO) is config.auto_merge(legacy, REPO) is True
    assert config.label_gated_ci(cfg, REPO) is config.label_gated_ci(legacy, REPO) is False
    assert "No settings conflicted" in capsys.readouterr().err


@pytest.mark.parametrize("raw,configured", [("# setup ran; nothing chosen yet\n", True),
                                            ("{}\n", True), ("", False)])
def test_the_skill_gate_answers_the_same_before_and_after_the_move(home, raw, configured):
    """The Step 0 gate of both skills, run by bash exactly as the SKILL.md states it:
    a legacy-only machine is never told it is unconfigured by the move itself, and an
    empty or comment-only legacy file still reaches the canonical location."""
    lines = {Path(buddhi_review.__file__).parent.joinpath("skills", s, "SKILL.md")
             .read_text(encoding="utf-8").split("```bash\n", 2)[1].split("\n", 1)[0]
             for s in ("review-pr", "open-pr")}
    (gate,) = lines  # both skills carry the identical gate line
    _put(_legacy(home), None, raw=raw)

    def ask():
        r = subprocess.run(["bash", "-c", gate], env=_env(home), capture_output=True, text=True)
        return r.stdout.strip()

    want = "configured" if configured else "unconfigured"
    assert ask() == want
    config.config_path()
    assert _canonical(home).read_bytes() == raw.encode()
    assert ask() == want


def test_a_write_to_the_legacy_location_during_the_move_is_not_lost(home, monkeypatch):
    """A process of an earlier release takes no lock. Its write, landing after the
    legacy file was read, must reach the canonical file — and, being the newest
    write, win its conflicts — never only the backup."""
    _put(_legacy(home), LEGACY, mtime_ns=time.time_ns() - 2 * 10**9)
    _put(_canonical(home), {"known_repos": [REPO]}, mtime_ns=time.time_ns() - 10**9)
    real = config._read_config_file
    late = {**LEGACY, "plan": "pro",
            "repos": {**LEGACY["repos"], "late/repo": {"auto_merge": True}}}
    reads = []

    def read_then_old_release_writes(path):
        got = real(path)
        if Path(path).parent == _legacy(home).parent:
            reads.append(Path(path).name)
            if len(reads) == 2:  # the read under the lock (the first is the pre-check)
                tmp = _legacy(home).with_name("earlier-release.tmp")
                tmp.write_text(yaml.safe_dump(late), encoding="utf-8")
                os.replace(tmp, _legacy(home))  # the earlier release's own atomic write
        return got

    monkeypatch.setattr(config, "_read_config_file", read_then_old_release_writes)
    assert config.migrate_legacy_config() == "migrated"
    cfg = _load(_canonical(home))
    assert config.repo_entry(cfg, "late/repo") == {"auto_merge": True}
    assert cfg["plan"] == "pro"
    assert not _legacy(home).exists() and len(_backups(home)) == 1
    assert _load(_backups(home)[0]) == late


_CHILD_READ_DURING_MOVE = r"""
import os, sys, time, pathlib
from buddhi_review import config
ready, go, role, signal = sys.argv[1:5]
if role == "migrate":
    _orig = config._merge_into_canonical
    def slow(*a, **k):
        pathlib.Path(signal).touch()   # inside the move, before the canonical write
        time.sleep(0.5)
        return _orig(*a, **k)
    config._merge_into_canonical = slow
pathlib.Path(ready).touch()
while not os.path.exists(go):
    time.sleep(0.002)
if role == "migrate":
    config.config_path()
else:
    deadline = time.monotonic() + 20
    while not os.path.exists(signal):
        assert time.monotonic() < deadline, "the move never started"
        time.sleep(0.002)
    cfg = config.load_config()
    print(cfg.get("plan"), config.repo_entry(cfg, "acme/widgets") is not None)
"""


def test_a_reader_during_the_move_sees_the_settings(home, tmp_path):
    """While one process moves the settings, another that resolves the config must
    not read a canonical file that lacks them (it waits for the move instead)."""
    _put(_legacy(home), LEGACY)
    signal = tmp_path / "moving"
    results = _race(home, tmp_path, _CHILD_READ_DURING_MOVE,
                    [["migrate", str(signal)], ["read", str(signal)]])
    assert results[1][0].strip() == "max-5x True"


def test_an_interrupted_move_is_finished_by_the_next_resolve(home, monkeypatch):
    """A process stopped after writing the canonical file but before retiring the
    legacy name leaves the legacy file in place; the next resolve completes."""
    _put(_legacy(home), LEGACY)
    real_rename = os.rename

    def interrupted(src, dst, *a, **k):
        if str(src) == str(_legacy(home)):
            raise KeyboardInterrupt
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(config.os, "rename", interrupted)
    with pytest.raises(KeyboardInterrupt):
        config.config_path()
    monkeypatch.setattr(config.os, "rename", real_rename)
    assert _legacy(home).exists() and _load(_canonical(home)) == LEGACY
    config.config_path()
    assert not _legacy(home).exists() and len(_backups(home)) == 1
    assert _load(_canonical(home)) == LEGACY


def _atomic_legacy_write(home, data, name="earlier-release.tmp"):
    """An earlier release's own write: a temp file replaced over the legacy name."""
    tmp = _legacy(home).with_name(name)
    tmp.write_text(yaml.safe_dump(data), encoding="utf-8")
    os.replace(tmp, _legacy(home))


def test_earlier_release_writes_on_both_sides_of_the_rename_are_all_kept(home, monkeypatch):
    """One write lands after the legacy file was read and before it is renamed (so
    the moved file holds it); another lands after the rename (a fresh legacy file).
    The first reaches the canonical file in this move, the second in the next."""
    _put(_legacy(home), {"plan": "max-20x", "repos": {"leg/one": {"auto_merge": False}}})
    real_rename = os.rename
    first = {"plan": "max-20x", "repos": {"leg/one": {"auto_merge": False},
                                          "old/a": {"auto_merge": True}}}

    def racing(src, dst, *a, **k):
        if str(src) == str(_legacy(home)) and ".migrated-" in str(dst):
            _atomic_legacy_write(home, first)                       # before the rename
            real_rename(src, dst, *a, **k)
            _atomic_legacy_write(home, {"repos": {"old/b": {"auto_merge": True}}})  # after
            return None
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(config.os, "rename", racing)
    assert config.migrate_legacy_config() == "migrated"
    monkeypatch.setattr(config.os, "rename", real_rename)
    cfg = _load(_canonical(home))
    assert {"leg/one", "old/a"} <= set(cfg["repos"])
    assert _load(_legacy(home)) == {"repos": {"old/b": {"auto_merge": True}}}
    config.config_path()
    cfg = _load(_canonical(home))
    assert {"leg/one", "old/a", "old/b"} <= set(cfg["repos"])
    assert not _legacy(home).exists() and len(_backups(home)) == 2


def _catch_up_fails(home, monkeypatch, failure):
    """Migrate while an earlier release rewrites the legacy file just before the
    rename (adding ``old/a``), and the catch-up write of that newer write fails."""
    _put(_canonical(home), {"plan": "pro"}, mtime_ns=time.time_ns() - 10**9)
    _put(_legacy(home), {"repos": {"leg/one": {"auto_merge": False}}})
    real_rename, real_write = os.rename, wizard.write_config
    writes = []

    def racing(src, dst, *a, **k):
        if str(src) == str(_legacy(home)) and ".migrated-" in str(dst):
            _atomic_legacy_write(home, {"repos": {"leg/one": {"auto_merge": False},
                                                  "old/a": {"auto_merge": True}}})
        return real_rename(src, dst, *a, **k)

    def second_write_fails(cfg, path):
        writes.append(1)
        if len(writes) == 2:  # the catch-up merge
            if failure == "refused":
                return False
            raise KeyboardInterrupt
        return real_write(cfg, path)

    monkeypatch.setattr(config.os, "rename", racing)
    monkeypatch.setattr(wizard, "write_config", second_write_fails)
    if failure == "refused":
        assert config.migrate_legacy_config() == "error"
    else:
        with pytest.raises(KeyboardInterrupt):
            config.migrate_legacy_config()
    monkeypatch.setattr(config.os, "rename", real_rename)
    monkeypatch.setattr(wizard, "write_config", real_write)
    assert not _legacy(home).exists() and len(_backups(home)) == 1
    assert "old/a" not in _load(_canonical(home))["repos"]
    assert "old/a" in _load(_backups(home)[0])["repos"]


@pytest.mark.parametrize("failure", ["refused", "interrupted"])
def test_a_failed_catch_up_after_the_rename_is_retried_from_the_backup(home, monkeypatch,
                                                                       capsys, failure):
    """The legacy name is already gone when the catch-up merge fails, so the record
    naming the backup is what brings the newer write over on the next resolve."""
    _catch_up_fails(home, monkeypatch, failure)
    record = _canonical(home).with_name("config.yaml.legacy-merged")
    assert _load(record)["backup"] == _backups(home)[0].name
    capsys.readouterr()
    assert config.migrate_legacy_config() == "migrated"
    cfg = _load(_canonical(home))
    assert {"leg/one", "old/a"} <= set(cfg["repos"]) and cfg["plan"] == "pro"
    assert f"kept as {_backups(home)[0]}" in capsys.readouterr().err
    assert not record.exists() and len(_backups(home)) == 1
    assert config.migrate_legacy_config() == "absent"


def test_an_unreadable_recovery_record_is_kept_for_the_next_catch_up(
        home, monkeypatch, capsys):
    """The legacy name is already gone, so an unreadable recovery record must not
    be mistaken for an absent record and deleted: it is the only pointer to the
    backup whose later write still needs to be merged."""
    _catch_up_fails(home, monkeypatch, "refused")
    record = _canonical(home).with_name("config.yaml.legacy-merged")
    backup = _backups(home)[0]
    real_read = config._read_config_file

    def record_read_fails(path):
        if Path(path) == record:
            return None, b"", "unreadable", None
        return real_read(path)

    monkeypatch.setattr(config, "_read_config_file", record_read_fails)
    capsys.readouterr()
    assert config.migrate_legacy_config() == "unreadable"
    assert record.exists()
    assert f"Could not read the old config file {record} (unreadable)" in capsys.readouterr().err
    monkeypatch.setattr(config, "_read_config_file", real_read)
    assert config.migrate_legacy_config() == "migrated"
    assert "old/a" in _load(_canonical(home))["repos"]
    assert backup.exists() and not record.exists()


@pytest.mark.parametrize("failure", ["refused", "interrupted"])
@pytest.mark.parametrize("canonical", ["untouched", "edited"])
@pytest.mark.parametrize("start", ["merged", "copied"])
def test_a_retried_catch_up_judges_a_changed_setting_against_the_file_the_user_left(
        home, monkeypatch, failure, canonical, start):
    """An earlier release changes an EXISTING setting after the legacy file was read
    and before the migration writes the canonical file, and the catch-up of that
    write fails. The retry judges the conflict against the canonical file as the user
    left it — so the newer legacy value wins — never against the migration's own
    write: neither a merge (``merged``) nor a byte copy made because there was no
    canonical file yet (``copied``). Once the user edits the canonical file after the
    migration, that edit is the newer one, and the canonical value stays."""
    t = time.time_ns()
    if start == "merged":
        _put(_canonical(home), {"plan": "pro"}, mtime_ns=t - 2 * 10**9)
    _put(_legacy(home), {"repos": {"leg/one": {"auto_merge": False}}}, mtime_ns=t - 3 * 10**9)
    real_copy, real_write = config._write_bytes_atomic, wizard.write_config
    raced = []

    def old_release_writes(path):
        if not raced and Path(path) == _canonical(home):  # the migration's first write
            raced.append(1)
            _atomic_legacy_write(home, {"repos": {"leg/one": {"auto_merge": True}}})
            os.utime(_legacy(home), ns=(t - 10**9, t - 10**9))  # after C0, before this write

    def copy(path, raw):
        old_release_writes(path)
        return real_copy(path, raw)

    def write(cfg, path):
        if raced:  # the catch-up merge
            if failure == "refused":
                return False
            raise KeyboardInterrupt
        old_release_writes(path)
        return real_write(cfg, path)

    monkeypatch.setattr(config, "_write_bytes_atomic", copy)
    monkeypatch.setattr(wizard, "write_config", write)
    if failure == "refused":
        assert config.migrate_legacy_config() == "error"
    else:
        with pytest.raises(KeyboardInterrupt):
            config.migrate_legacy_config()
    monkeypatch.setattr(config, "_write_bytes_atomic", real_copy)
    monkeypatch.setattr(wizard, "write_config", real_write)
    assert raced and len(_backups(home)) == 1
    assert config.repo_entry(_load(_canonical(home)), "leg/one") == {"auto_merge": False}
    if canonical == "edited":
        # The user edits the canonical file directly (its path named, so this write
        # does not itself resolve the config and run the retry).
        assert config.set_repo_keys("other/repo", {"auto_merge": True}, _canonical(home)) is True
    assert config.migrate_legacy_config() == "migrated"
    want = canonical == "untouched"
    assert config.repo_entry(_load(_canonical(home)), "leg/one") == {"auto_merge": want}
    assert not _canonical(home).with_name("config.yaml.legacy-merged").exists()


@pytest.mark.parametrize("failure", ["stat", "read"])
def test_an_unreadable_backup_keeps_the_record_until_the_catch_up_can_run(home, monkeypatch,
                                                                          capsys, failure):
    """A backup that cannot be stat'ed or read is not "nothing to catch up": the
    newer write an earlier release renamed into it would be lost if the record were
    dropped. Both the catch-up after the rename and the retry from the record keep
    the record, and the write arrives once the backup can be read."""
    _put(_canonical(home), {"plan": "pro"}, mtime_ns=time.time_ns() - 10**9)
    _put(_legacy(home), {"repos": {"leg/one": {"auto_merge": False}}})
    real_rename, real_stat, real_read = os.rename, os.stat, config._read_config_file

    def racing(src, dst, *a, **k):
        if str(src) == str(_legacy(home)) and ".migrated-" in str(dst):
            _atomic_legacy_write(home, {"repos": {"leg/one": {"auto_merge": False},
                                                  "old/a": {"auto_merge": True}}})
        return real_rename(src, dst, *a, **k)

    def stat_fails(path, *a, **k):
        if ".migrated-" in str(path):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_stat(path, *a, **k)

    def read_fails(path):
        if ".migrated-" in str(path):
            return None, b"", "unreadable", None
        return real_read(path)

    monkeypatch.setattr(config.os, "rename", racing)
    if failure == "stat":
        monkeypatch.setattr(config.os, "stat", stat_fails)
    else:
        monkeypatch.setattr(config, "_read_config_file", read_fails)
    assert config.migrate_legacy_config() == "unreadable"
    monkeypatch.setattr(config.os, "rename", real_rename)
    record = _canonical(home).with_name("config.yaml.legacy-merged")
    backup = _backups(home)[0]
    assert _load(record)["backup"] == backup.name
    assert f"Could not read the old config file {backup} (unreadable)" in capsys.readouterr().err
    assert config.migrate_legacy_config() == "unreadable"  # the retry keeps it too
    assert _load(record)["backup"] == backup.name
    assert "old/a" not in _load(_canonical(home))["repos"]
    monkeypatch.setattr(config.os, "stat", real_stat)
    monkeypatch.setattr(config, "_read_config_file", real_read)
    assert config.migrate_legacy_config() == "migrated"
    assert {"leg/one", "old/a"} <= set(_load(_canonical(home))["repos"])
    assert not record.exists()


def test_a_backup_that_cannot_be_probed_keeps_the_record_until_it_can(home, monkeypatch,
                                                                      capsys):
    """An I/O error while probing the backup a record names is no evidence it is
    gone. The legacy name is already retired, so dropping the record would lose the
    newer write the backup holds for good: the backup is reported, the record kept,
    and the write arrives once the error clears."""
    _catch_up_fails(home, monkeypatch, "refused")
    record = _canonical(home).with_name("config.yaml.legacy-merged")
    backup = _backups(home)[0]
    real_lstat, real_stat = os.lstat, os.stat

    def io_error(real):
        def probe(path, *a, **k):
            if str(path) == str(backup):
                raise OSError(errno.EIO, os.strerror(errno.EIO), str(path))
            return real(path, *a, **k)
        return probe

    monkeypatch.setattr(config.os, "lstat", io_error(real_lstat))
    monkeypatch.setattr(config.os, "stat", io_error(real_stat))
    capsys.readouterr()
    assert config.migrate_legacy_config() == "unreadable"
    assert f"Could not read the old config file {backup} (unreadable)" in capsys.readouterr().err
    monkeypatch.setattr(config.os, "lstat", real_lstat)
    monkeypatch.setattr(config.os, "stat", real_stat)
    assert _load(record)["backup"] == backup.name
    assert "old/a" not in _load(_canonical(home))["repos"]
    assert config.migrate_legacy_config() == "migrated"
    assert {"leg/one", "old/a"} <= set(_load(_canonical(home))["repos"])
    assert not record.exists()


def test_a_retried_catch_up_never_reads_or_overwrites_a_fresh_legacy_file(home, monkeypatch):
    """A legacy file an earlier release creates after the failed catch-up is left to
    its own migration: both its settings and the backup's reach the canonical file,
    and the first backup is never overwritten."""
    _catch_up_fails(home, monkeypatch, "refused")
    first = _backups(home)[0]
    first_bytes = first.read_bytes()
    _atomic_legacy_write(home, {"repos": {"old/b": {"auto_merge": True}}})
    config.config_path()
    cfg = _load(_canonical(home))
    assert {"leg/one", "old/a", "old/b"} <= set(cfg["repos"])
    assert not _legacy(home).exists() and len(_backups(home)) == 2
    assert first.read_bytes() == first_bytes


def test_the_legacy_name_is_kept_while_the_record_naming_the_backup_cannot_be_written(
        home, monkeypatch, capsys):
    """The record naming the backup cannot be written, and at that moment an earlier
    release rewrites the legacy file (changing ``leg/one`` and adding ``old/a``) with a
    timestamp after the canonical file as the user left it but before the
    migration's own write. Renamed anyway, the legacy file would carry that write
    into a backup that no later resolve could find once the catch-up failed. So the
    name stays, the merge's record (and the baseline it holds) survives, and the next
    resolve merges what changed — judged against that baseline — and finishes."""
    t = time.time_ns()
    _put(_canonical(home), {"plan": "pro"}, mtime_ns=t - 2 * 10**9)
    _put(_legacy(home), {"repos": {"leg/one": {"auto_merge": False}}}, mtime_ns=t - 3 * 10**9)
    record = _canonical(home).with_name("config.yaml.legacy-merged")
    real_replace, real_write = os.replace, wizard.write_config
    writes = []

    def backup_record_fails(src, dst, *a, **k):
        if str(dst) == str(record) and b"backup:" in Path(src).read_bytes():
            _atomic_legacy_write(home, {"repos": {"leg/one": {"auto_merge": True},
                                                  "old/a": {"auto_merge": True}}})
            os.utime(_legacy(home), ns=(t - 10**9, t - 10**9))
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), str(dst))
        return real_replace(src, dst, *a, **k)

    def catch_up_refused(cfg, path):
        writes.append(1)
        return len(writes) == 1 and real_write(cfg, path)

    monkeypatch.setattr(config.os, "replace", backup_record_fails)
    monkeypatch.setattr(wizard, "write_config", catch_up_refused)
    assert config.migrate_legacy_config() == "error"
    monkeypatch.setattr(config.os, "replace", real_replace)
    monkeypatch.setattr(wizard, "write_config", real_write)
    assert _legacy(home).exists() and _backups(home) == []
    assert "old/a" in _load(_legacy(home))["repos"]
    kept = _load(record)
    assert "backup" not in kept and kept["canonical_before"] == t - 2 * 10**9
    assert capsys.readouterr().err.strip() == (
        f"Warning: Settings from {_legacy(home)} were merged into {_canonical(home)}, but the "
        f"old file could not be renamed ({os.strerror(errno.ENOSPC)}). No settings "
        f"conflicted. It will not be merged again unless it changes.")
    assert config.migrate_legacy_config() == "migrated"
    cfg = _load(_canonical(home))
    assert cfg["plan"] == "pro" and {"leg/one", "old/a"} <= set(cfg["repos"])
    assert config.repo_entry(cfg, "leg/one") == {"auto_merge": True}
    assert not _legacy(home).exists() and len(_backups(home)) == 1
    assert not record.exists()


def test_a_record_that_cannot_be_written_at_all_keeps_the_legacy_name(home, capsys):
    """No record can be written (a folder sits at its path), so neither config is
    changed. Once a record can be written, the move completes."""
    _put(_legacy(home), LEGACY)
    record = _canonical(home).with_name("config.yaml.legacy-merged")
    record.mkdir(parents=True)
    assert config.migrate_legacy_config() == "error"
    assert not _canonical(home).exists()
    assert _legacy(home).exists() and _backups(home) == []
    assert "writing the file failed" in capsys.readouterr().err
    record.rmdir()
    assert config.migrate_legacy_config() == "migrated"
    assert _load(_canonical(home)) == LEGACY
    assert not _legacy(home).exists() and len(_backups(home)) == 1
    assert not record.exists()


def test_an_interruption_after_the_canonical_replace_retries_a_late_legacy_edit(
        home, monkeypatch):
    """A legacy write after the migration read but before its canonical replace is
    recovered even if execution stops after that replace and before completion is
    recorded. The migration's newer mtime must not defeat the late legacy value."""
    t = time.time_ns()
    _put(_canonical(home), {"plan": "pro"}, mtime_ns=t - 3 * 10**9)
    _put(_legacy(home), {"plan": "max-20x"}, mtime_ns=t - 2 * 10**9)
    record = _canonical(home).with_name("config.yaml.legacy-merged")
    real_replace = os.replace

    def replace_then_stop(src, dst, *args, **kwargs):
        if str(dst) == str(_canonical(home)):
            _atomic_legacy_write(home, {"plan": "max-5x"})
            os.utime(_legacy(home), ns=(t - 10**9, t - 10**9))
            real_replace(src, dst, *args, **kwargs)
            raise KeyboardInterrupt
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(config.os, "replace", replace_then_stop)
    with pytest.raises(KeyboardInterrupt):
        config.migrate_legacy_config()
    monkeypatch.setattr(config.os, "replace", real_replace)
    assert _load(record)["state"] == "pending"
    assert _load(_canonical(home))["plan"] == "max-20x"
    assert config.migrate_legacy_config() == "migrated"
    assert _load(_canonical(home))["plan"] == "max-5x"
    assert not record.exists()


def test_a_failed_pending_record_write_cannot_lose_a_late_legacy_edit(
        home, monkeypatch):
    """The recovery baseline must exist before the canonical replacement. If its
    write fails while an earlier release edits the legacy file, the canonical file
    stays untouched and the next run imports that edit."""
    t = time.time_ns()
    _put(_canonical(home), {"plan": "pro"}, mtime_ns=t - 2 * 10**9)
    _put(_legacy(home), {"plan": "max-20x"}, mtime_ns=t - 3 * 10**9)
    record = _canonical(home).with_name("config.yaml.legacy-merged")
    real_replace = os.replace

    def record_fails(src, dst, *args, **kwargs):
        if str(dst) == str(record):
            _atomic_legacy_write(home, {"plan": "max-5x"})
            os.utime(_legacy(home), ns=(t - 10**9, t - 10**9))
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(config.os, "replace", record_fails)
    assert config.migrate_legacy_config() == "error"
    monkeypatch.setattr(config.os, "replace", real_replace)
    assert _load(_canonical(home)) == {"plan": "pro"}
    assert _load(_legacy(home)) == {"plan": "max-5x"}
    assert config.migrate_legacy_config() == "migrated"
    assert _load(_canonical(home))["plan"] == "max-5x"


def test_a_record_naming_a_backup_outside_the_legacy_folder_is_ignored(home, tmp_path):
    """Only a ``.migrated-`` name beside the legacy file is ever caught up from."""
    _put(_canonical(home), {"plan": "pro"})
    stray = _put(tmp_path / "config.yaml.migrated-x", {"plan": "max-20x"})
    record = _canonical(home).with_name("config.yaml.legacy-merged")
    _put(record, {"identity": [0, 0, 0, 0], "conflicts": [], "legacy": {},
                  "backup": str(stray)})
    assert config.migrate_legacy_config() == "absent"
    assert _load(_canonical(home)) == {"plan": "pro"} and not record.exists()


def test_a_run_stopped_after_the_write_reports_its_conflicts_on_the_next_run(home, monkeypatch,
                                                                             capsys):
    t = time.time_ns()
    _put(_canonical(home), {"plan": "pro"}, mtime_ns=t - 10**9)
    _put(_legacy(home), {"plan": "max-20x"}, mtime_ns=t)
    real_rename = os.rename

    def stopped(src, dst, *a, **k):
        if str(src) == str(_legacy(home)):
            raise KeyboardInterrupt
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(config.os, "rename", stopped)
    with pytest.raises(KeyboardInterrupt):
        config.config_path()
    monkeypatch.setattr(config.os, "rename", real_rename)
    assert _load(_canonical(home))["plan"] == "max-20x"
    capsys.readouterr()
    config.config_path()
    err = capsys.readouterr().err
    assert "1 setting(s) conflicted" in err and "plan" in err
    assert not _canonical(home).with_name("config.yaml.legacy-merged").exists()


def test_a_linked_canonical_path_still_lets_a_newer_legacy_write_win(home, monkeypatch):
    _put(_legacy(home), {"plan": "max-5x"})
    _canonical(home).parent.mkdir(parents=True)
    _canonical(home).symlink_to(_legacy(home))
    real = config._read_config_file
    reads = []

    def read_then_old_release_writes(path):
        got = real(path)
        if Path(path) == _legacy(home):
            reads.append(1)
            if len(reads) == 2:  # the read under the lock
                _atomic_legacy_write(home, {"plan": "pro"})
        return got

    monkeypatch.setattr(config, "_read_config_file", read_then_old_release_writes)
    assert config.migrate_legacy_config() == "migrated"
    assert _load(_canonical(home))["plan"] == "pro"
    assert not _canonical(home).is_symlink()


def test_a_refused_rename_merges_only_what_a_later_legacy_edit_changed(home):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    _put(_legacy(home), LEGACY, mtime_ns=time.time_ns() - 10**9)
    folder = _legacy(home).parent
    os.chmod(folder, 0o500)
    try:
        config.config_path()
        # The user changes the plan in the canonical file …
        assert config.set_repo_keys(REPO, {"auto_merge": True}) is True
        cfg = _load(_canonical(home))
        cfg["plan"] = "pro"
        _put(_canonical(home), cfg)
        # … and later edits the (still present) legacy file in place: only the key
        # that edit changed is carried over; the user's own changes stay.
        edited = {**LEGACY, "test_command": "tox"}
        _legacy(home).write_text(yaml.safe_dump(edited), encoding="utf-8")
        config.config_path()
        cfg = _load(_canonical(home))
        assert cfg["test_command"] == "tox"
        assert cfg["plan"] == "pro" and cfg["repos"][REPO]["auto_merge"] is True
    finally:
        os.chmod(folder, 0o700)


def _edit_after_the_merge(home, monkeypatch, path, edited):
    """Migrate, while an earlier release rewrites the legacy file as ``edited``
    after it was merged: just before the rename, so the catch-up merges the edit
    from the backup (``catch-up``); or after a refused rename, so the next resolve
    merges it from the legacy file (``retry``). Both merge only what it changed."""
    raw = yaml.safe_dump(edited, sort_keys=False)
    if path == "catch-up":
        real_rename = os.rename

        def racing(src, dst, *a, **k):
            if str(src) == str(_legacy(home)) and ".migrated-" in str(dst):
                tmp = _legacy(home).with_name("earlier-release.tmp")
                tmp.write_text(raw, encoding="utf-8")
                os.replace(tmp, _legacy(home))
            return real_rename(src, dst, *a, **k)

        monkeypatch.setattr(config.os, "rename", racing)
        assert config.migrate_legacy_config() == "migrated"
        monkeypatch.setattr(config.os, "rename", real_rename)
        assert _load(_backups(home)[0]) == edited
        return
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    folder = _legacy(home).parent
    os.chmod(folder, 0o500)
    try:
        assert config.migrate_legacy_config() == "error"  # the rename is refused
        _legacy(home).write_text(raw, encoding="utf-8")
        config.migrate_legacy_config()
    finally:
        os.chmod(folder, 0o700)
    assert _load(_legacy(home)) == edited


@pytest.mark.parametrize("path", ["catch-up", "retry"])
def test_a_later_edit_of_a_shadowed_case_variant_is_never_promoted(home, monkeypatch, path):
    """Readers use a repo's FIRST case-variant entry. An edit that changes only the
    entry behind it changes nothing a reader of the legacy file sees, so it must not
    reach the entry readers use in the canonical file either."""
    t = time.time_ns()
    _put(_canonical(home), {"plan": "pro"}, mtime_ns=t - 2 * 10**9)
    _put(_legacy(home), {"repos": {"O/R": {"auto_merge": False}, "o/r": {"auto_merge": False}}},
         mtime_ns=t - 10**9)
    edited = {"repos": {"O/R": {"auto_merge": False}, "o/r": {"auto_merge": True}}}
    _edit_after_the_merge(home, monkeypatch, path, edited)
    assert config.auto_merge(_load(_canonical(home)), "o/r") is config.auto_merge(edited, "o/r") \
        is False


@pytest.mark.parametrize("path", ["catch-up", "retry"])
def test_a_later_edit_that_only_changes_a_repos_case_replays_nothing(home, monkeypatch, path):
    """An edit that only changes the case of a repo's key changes no setting, so the
    unchanged legacy values are not merged again over the canonical file's own."""
    t = time.time_ns()
    _put(_canonical(home), {"repos": {"o/r": {"auto_merge": True}}}, mtime_ns=t - 10**9)
    _put(_legacy(home), {"repos": {"o/r": {"auto_merge": False}}}, mtime_ns=t - 2 * 10**9)
    _edit_after_the_merge(home, monkeypatch, path, {"repos": {"O/R": {"auto_merge": False}}})
    assert _load(_canonical(home)) == {"repos": {"o/r": {"auto_merge": True}}}


def test_the_record_is_not_trusted_once_the_canonical_file_is_gone(home):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    _put(_legacy(home), LEGACY)
    folder = _legacy(home).parent
    os.chmod(folder, 0o500)
    try:
        config.config_path()
        _canonical(home).unlink()
        config.config_path()
        assert _load(_canonical(home)) == LEGACY
    finally:
        os.chmod(folder, 0o700)


def test_the_backup_of_a_hard_linked_legacy_file_keeps_the_other_links_mode(home, tmp_path):
    other = _put(tmp_path / "dotfiles" / "review.yaml", LEGACY)
    os.chmod(other, 0o644)
    (home / ".config" / "review-loop").mkdir(parents=True)
    os.link(other, _legacy(home))
    config.config_path()
    assert stat.S_IMODE(os.stat(other).st_mode) == 0o644
    assert _load(_canonical(home)) == LEGACY


@pytest.mark.parametrize("stop", ["after-the-rename", "catch-up-refused", "catch-up-interrupted"])
def test_a_resumed_recovery_leaves_the_backup_0600(home, monkeypatch, stop):
    """A world-readable legacy file whose move stops once it is renamed — right
    after the rename, or in a failed catch-up of a newer write the rename carried
    along — leaves the record naming the backup. A failed catch-up never leaves the
    backup readable by others, and the resolve that finishes the recovery makes it
    0600 before it drops the record."""
    _put(_canonical(home), {"plan": "pro"}, mtime_ns=time.time_ns() - 10**9)
    _put(_legacy(home), {"repos": {"leg/one": {"auto_merge": False}}})
    os.chmod(_legacy(home), 0o644)
    real_rename, real_write = os.rename, wizard.write_config
    writes = []

    def rename(src, dst, *a, **k):
        if str(src) == str(_legacy(home)) and ".migrated-" in str(dst):
            if stop != "after-the-rename":  # an earlier release writes just before it
                _atomic_legacy_write(home, {"repos": {"leg/one": {"auto_merge": False},
                                                      "old/a": {"auto_merge": True}}})
                os.chmod(_legacy(home), 0o644)
            real_rename(src, dst, *a, **k)
            if stop == "after-the-rename":
                raise KeyboardInterrupt
            return None
        return real_rename(src, dst, *a, **k)

    def write(cfg, path):
        writes.append(1)
        if len(writes) == 2:  # the catch-up merge
            if stop == "catch-up-refused":
                return False
            raise KeyboardInterrupt
        return real_write(cfg, path)

    monkeypatch.setattr(config.os, "rename", rename)
    monkeypatch.setattr(wizard, "write_config", write)
    if stop == "catch-up-refused":
        assert config.migrate_legacy_config() == "error"
    else:
        with pytest.raises(KeyboardInterrupt):
            config.migrate_legacy_config()
    monkeypatch.setattr(config.os, "rename", real_rename)
    monkeypatch.setattr(wizard, "write_config", real_write)
    (backup,) = _backups(home)
    record = _canonical(home).with_name("config.yaml.legacy-merged")
    assert _load(record)["backup"] == backup.name
    stopped_before_it = 0o644 if stop == "after-the-rename" else 0o600
    assert stat.S_IMODE(os.stat(backup).st_mode) == stopped_before_it
    config.migrate_legacy_config()
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600
    assert not record.exists()
    if stop != "after-the-rename":
        assert "old/a" in _load(_canonical(home))["repos"]


def test_a_resumed_recovery_keeps_a_hard_linked_files_other_names_mode(home, tmp_path,
                                                                       monkeypatch):
    other = _put(tmp_path / "dotfiles" / "review.yaml", LEGACY)
    os.chmod(other, 0o644)
    (home / ".config" / "review-loop").mkdir(parents=True)
    os.link(other, _legacy(home))
    real_rename = os.rename

    def stopped_after_the_rename(src, dst, *a, **k):
        real_rename(src, dst, *a, **k)
        if str(src) == str(_legacy(home)):
            raise KeyboardInterrupt

    monkeypatch.setattr(config.os, "rename", stopped_after_the_rename)
    with pytest.raises(KeyboardInterrupt):
        config.migrate_legacy_config()
    monkeypatch.setattr(config.os, "rename", real_rename)
    config.migrate_legacy_config()
    assert stat.S_IMODE(os.stat(other).st_mode) == 0o644
    assert not _canonical(home).with_name("config.yaml.legacy-merged").exists()


@pytest.mark.parametrize("shape", ["folder-link", "file-link"])
def test_a_stow_style_canonical_link_is_left_alone(home, tmp_path, shape):
    """The canonical file is itself a symlink into a dotfiles checkout, and the
    legacy path reaches that same ENTRY (a symlinked legacy folder, or a legacy
    symlink to the canonical path): one file, nothing to move, nothing to cut."""
    target = _put(tmp_path / "dotfiles" / "buddhi.yaml", LEGACY)
    _canonical(home).parent.mkdir(parents=True)
    _canonical(home).symlink_to(target)
    if shape == "folder-link":
        (home / ".config" / "review-loop").symlink_to(home / ".config" / "buddhi")
    else:
        (home / ".config" / "review-loop").mkdir()
        _legacy(home).symlink_to(_canonical(home))
    assert config.migrate_legacy_config() == "absent"
    assert _canonical(home).is_symlink() and _load(_canonical(home)) == LEGACY


@pytest.mark.parametrize("canonical_repos,legacy_repos,expect", [
    # a null spelling in the canonical file is replaced, and never ahead of the
    # entry readers use first
    ({"Acme/Widgets": None}, {"acme/widgets": {"active_reviewers": ["codex"]},
                              "Acme/Widgets": {"active_reviewers": []}}, ("codex",)),
    # the same entries in a different order: the newer file's first entry wins
    ({"Acme/Widgets": {"active_reviewers": ["copilot"]},
      "acme/widgets": {"active_reviewers": ["codex"]}},
     {"acme/widgets": {"active_reviewers": ["codex"]},
      "Acme/Widgets": {"active_reviewers": ["copilot"]}}, ("codex",)),
])
def test_the_entry_readers_use_first_follows_the_winning_file(home, canonical_repos,
                                                             legacy_repos, expect):
    t = time.time_ns()
    _put(_canonical(home), {"repos": canonical_repos}, mtime_ns=t - 10**9)
    _put(_legacy(home), {"repos": legacy_repos}, mtime_ns=t)
    config.config_path()
    assert config.active_reviewers(_load(_canonical(home)), REPO) == expect


def test_the_skill_gate_never_answers_unconfigured_during_the_move(home):
    """Run the shipped gate line with a ``test`` that performs the whole move just
    before its SECOND check: at no point may a machine with settings read as
    unconfigured."""
    line = Path(buddhi_review.__file__).parent.joinpath(
        "skills", "review-pr", "SKILL.md").read_text(encoding="utf-8").split(
        "```bash\n", 2)[1].split("\n", 1)[0]
    _put(_legacy(home), LEGACY)
    mark = home / "moved"
    shim = ('test() { if [ -e "$MARK" ]; then :; elif [ -n "$SEEN" ]; then touch "$MARK"; '
            'python3 -c "from buddhi_review import config; config.config_path()" 2>/dev/null; '
            'else SEEN=1; fi; builtin test "$@"; }; ')
    r = subprocess.run(["bash", "-c", shim + line], env=_env(home, MARK=str(mark)),
                       capture_output=True, text=True)
    assert r.stdout.strip() == "configured"


@pytest.mark.parametrize("failure", ["refused", "interrupted"])
def test_a_canonical_write_that_did_not_land_is_merged_again(home, monkeypatch, failure):
    """A failed or interrupted canonical write must not be taken for a finished
    merge: the next resolve merges again, and only then is the legacy name retired."""
    t = time.time_ns()
    _put(_canonical(home), {"plan": "max-20x"}, mtime_ns=t - 10**9)
    _put(_legacy(home), LEGACY, mtime_ns=t)
    real = wizard.write_config

    def fails_once(cfg, path):
        monkeypatch.setattr(wizard, "write_config", real)
        if failure == "refused":
            return False
        raise KeyboardInterrupt

    monkeypatch.setattr(wizard, "write_config", fails_once)
    if failure == "refused":
        config.config_path()
    else:
        with pytest.raises(KeyboardInterrupt):
            config.config_path()
    assert _load(_canonical(home)) == {"plan": "max-20x"} and _legacy(home).exists()
    config.config_path()
    cfg = _load(_canonical(home))
    assert cfg["plan"] == "max-5x" and config.repo_entry(cfg, REPO) is not None
    assert not _legacy(home).exists()


@pytest.mark.parametrize("where", ["top-level", "per-repo"])
def test_a_blank_test_command_never_overrides_a_real_one(home, capsys, where):
    t = time.time_ns()
    if where == "top-level":
        _put(_canonical(home), {"test_command": "  "}, mtime_ns=t)
        _put(_legacy(home), {"test_command": "make test"}, mtime_ns=t - 10**9)
    else:
        _put(_canonical(home), {"repos": {REPO: {"test_command": ""}}}, mtime_ns=t)
        _put(_legacy(home), {"repos": {REPO: {"test_command": "make test"}}},
             mtime_ns=t - 10**9)
    config.config_path()
    assert config.test_command(_load(_canonical(home)), REPO) == "make test"
    assert "No settings conflicted" in capsys.readouterr().err


@pytest.mark.parametrize("falsy", [False, 0, [], {}])
@pytest.mark.parametrize("where", ["top-level", "per-repo"])
def test_a_falsy_test_command_never_overrides_a_real_one(home, capsys, where, falsy):
    t = time.time_ns()
    if where == "top-level":
        _put(_canonical(home), {"test_command": falsy}, mtime_ns=t)
        _put(_legacy(home), {"test_command": "make test"}, mtime_ns=t - 10**9)
    else:
        _put(_canonical(home), {"repos": {REPO: {"test_command": falsy}}}, mtime_ns=t)
        _put(_legacy(home), {"repos": {REPO: {"test_command": "make test"}}},
             mtime_ns=t - 10**9)
    config.config_path()
    assert config.test_command(_load(_canonical(home)), REPO) == "make test"
    assert "No settings conflicted" in capsys.readouterr().err


def test_a_record_left_after_the_rename_is_dropped_not_reused(home, monkeypatch):
    """A run stopped after the rename leaves its record behind. It describes a
    retired file, so a legacy file an earlier release writes later is merged on
    its own terms (the newer file wins), not as a change against that record."""
    t = time.time_ns()
    _put(_canonical(home), {"plan": "pro"}, mtime_ns=t - 3 * 10**9)
    _put(_legacy(home), {"plan": "max-20x"}, mtime_ns=t - 2 * 10**9)
    real_drop = config._drop_record
    monkeypatch.setattr(config, "_drop_record", lambda canonical: None)  # stopped here
    config.config_path()
    monkeypatch.setattr(config, "_drop_record", real_drop)
    record = _canonical(home).with_name("config.yaml.legacy-merged")
    assert record.exists()
    cfg = _load(_canonical(home))
    cfg["plan"] = "pro"  # the user sets the plan back afterwards
    _put(_canonical(home), cfg, mtime_ns=t - 10**9)
    _put(_legacy(home), {"plan": "max-20x"}, mtime_ns=t)  # an earlier release writes again
    config.config_path()
    assert _load(_canonical(home))["plan"] == "max-20x"  # the newer file wins
    assert not record.exists()


def test_a_legacy_file_moved_away_mid_look_is_not_reported(home, monkeypatch, capsys):
    """Another process finished the move between this one's existence check and its
    first read: that is "nothing to do", not an unreadable file."""
    _put(_legacy(home), LEGACY)
    real = config._read_config_file

    def moved_first(path):
        if Path(path) == _legacy(home) and _legacy(home).exists():
            os.rename(_legacy(home), _legacy(home).with_name("config.yaml.migrated-by-other"))
        return real(path)

    monkeypatch.setattr(config, "_read_config_file", moved_first)
    assert config.migrate_legacy_config() == "absent"
    assert capsys.readouterr().err == ""


def test_a_refused_rename_still_brings_the_settings_over_once(home, capsys):
    """A legacy folder that refuses the rename: the settings still reach the
    canonical file, and a key the user removes afterwards stays removed (the same
    bytes are not merged again). Once the folder allows it, the move completes."""
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    _put(_legacy(home), LEGACY)
    folder = _legacy(home).parent
    os.chmod(folder, 0o500)
    try:
        config.config_path()
        assert _load(_canonical(home)) == LEGACY
        assert config.set_repo_keys(REPO, {"test_command": None}) is True
        for _ in range(2):
            config.config_path()
            assert "test_command" not in _load(_canonical(home))["repos"][REPO]
        assert _legacy(home).exists() and _backups(home) == []
        assert "could not be renamed" in capsys.readouterr().err
    finally:
        os.chmod(folder, 0o700)
    config.config_path()
    assert not _legacy(home).exists() and len(_backups(home)) == 1
    assert "test_command" not in _load(_canonical(home))["repos"][REPO]
    assert not _canonical(home).with_name("config.yaml.legacy-merged").exists()


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_a_canonical_path_linked_to_the_legacy_file_becomes_its_own_file(home, link):
    """The canonical path links to the legacy file: the first write would split
    them, and the stale legacy twin would later be merged back over the user's
    edits. The move gives the canonical path its own file and retires the legacy."""
    _put(_legacy(home), LEGACY)
    _canonical(home).parent.mkdir(parents=True)
    if link == "symlink":
        _canonical(home).symlink_to(_legacy(home))
    else:
        os.link(_legacy(home), _canonical(home))
    config.config_path()
    assert not _canonical(home).is_symlink() and os.stat(_canonical(home)).st_nlink == 1
    assert _load(_canonical(home)) == LEGACY
    assert not _legacy(home).exists() and len(_backups(home)) == 1
    assert config.set_repo_keys(REPO, {"test_command": None}) is True
    config.config_path()
    assert "test_command" not in _load(_canonical(home))["repos"][REPO]


@pytest.mark.parametrize("newer", ["legacy", "canonical"])
def test_one_and_true_are_different_settings(home, newer, capsys):
    """Readers accept only a real bool, so ``1`` is not ``true``: the two are a
    conflict, and the result resolves as the file that wins resolved alone."""
    t = time.time_ns()
    canonical = {"label_gated_ci": True, "repos": {"o/r": {"label_gated_ci": True}}}
    legacy = {"label_gated_ci": 1, "repos": {"o/r": {"label_gated_ci": 1}}}
    _put(_canonical(home), canonical, mtime_ns=t if newer == "canonical" else t - 10**9)
    _put(_legacy(home), legacy, mtime_ns=t if newer == "legacy" else t - 10**9)
    config.config_path()
    cfg = _load(_canonical(home))
    winner = legacy if newer == "legacy" else canonical
    # The per-repo value is a real conflict: ``1`` is not ``true``.
    assert config.label_gated_ci(cfg, "o/r") is config.label_gated_ci(winner, "o/r")
    # A top-level ``1`` is a value the reader treats as unset, so it never replaces
    # the valid global ``true`` even from the newer file.
    assert config.label_gated_ci(cfg, "other/repo") is True
    assert "1 setting(s) conflicted" in capsys.readouterr().err


@pytest.mark.parametrize("side", ["legacy", "canonical"])
def test_a_null_case_variant_never_takes_a_repos_place(home, side):
    """Readers skip a non-mapping entry, so a null case-variant listed first must
    not capture or shadow the real entry for that repo."""
    t = time.time_ns()
    if side == "legacy":
        canonical = {"repos": {"o/r": {"active_reviewers": ["claude"]}}}
        legacy = {"repos": {"O/R": None, "o/r": {"label_gated_ci": True,
                                                 "test_command": "make test"}}}
    else:
        canonical = {"repos": {"O/R": None, "o/r": {"active_reviewers": ["claude"]}}}
        legacy = {"repos": {"o/r": {"label_gated_ci": True, "test_command": "make test"}}}
    _put(_canonical(home), canonical, mtime_ns=t - 10**9)
    _put(_legacy(home), legacy, mtime_ns=t)
    config.config_path()
    cfg = _load(_canonical(home))
    assert config.active_reviewers(cfg, "o/r") == ("claude",)
    assert config.label_gated_ci(cfg, "o/r") is True
    assert config.test_command(cfg, "o/r") == "make test"


def test_a_per_repo_null_that_shadows_the_global_is_a_setting(home, capsys):
    """A present per-repo ``label_gated_ci: null`` shadows the global (it resolves
    to the default), so in a merge it is a value: the newer file's null wins."""
    t = time.time_ns()
    _put(_canonical(home), {"repos": {"o/r": {"label_gated_ci": True}}}, mtime_ns=t - 10**9)
    _put(_legacy(home), {"label_gated_ci": True, "repos": {"o/r": {"label_gated_ci": None}}},
         mtime_ns=t)
    config.config_path()
    cfg = _load(_canonical(home))
    assert config.label_gated_ci(cfg, "o/r") is False
    assert "repos.o/r.label_gated_ci" in capsys.readouterr().err


def test_an_unsearchable_canonical_folder_fails_open(home):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    folder = home / ".config" / "buddhi"
    folder.mkdir(parents=True)
    os.chmod(folder, 0o000)
    try:
        assert config.load_config() == {}
        assert config.load_config_checked() == ({}, False)
        r = _cli(home, "status", "--repo", REPO)
    finally:
        os.chmod(folder, 0o700)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"repo_confirmed": False, "has_global_default": False}


def test_merge_function_contract():
    canonical = {"a": 1, "repos": {"x/y": {"k": 1}}, "known_repos": ["p/q"]}
    legacy = {"a": 2, "b": 3, "repos": {"X/Y": {"k": 2, "j": 5}, "m/n": {}},
              "known_repos": ["p/q", "r/s"]}
    merged, conflicts = config.merge_config_files(canonical, legacy, legacy_wins=False)
    assert merged == {"a": 1, "b": 3, "known_repos": ["p/q", "r/s"],
                      "repos": {"x/y": {"k": 1, "j": 5}, "m/n": {}}}
    assert sorted(conflicts) == ["a", "repos.x/y.k"]
    merged, _ = config.merge_config_files(canonical, legacy, legacy_wins=True)
    assert merged["a"] == 2 and merged["repos"]["x/y"] == {"k": 2, "j": 5}
    # Neither input was mutated.
    assert canonical == {"a": 1, "repos": {"x/y": {"k": 1}}, "known_repos": ["p/q"]}


@pytest.mark.parametrize("unset", ["", None, 5, []])
def test_a_plan_the_readers_treat_as_unset_never_overrides_a_real_one(monkeypatch, unset):
    """``plan()`` and ``plan_profile.active_plan()`` read a plan that is not a
    non-empty string as the default plan, so in a merge it yields to a real plan on
    the other side, even from the newer file, instead of changing the plan."""
    monkeypatch.delenv("BUDDHI_LOOP_PLAN", raising=False)
    for legacy_wins in (True, False):
        for canonical, legacy in (({"plan": "pro"}, {"plan": unset}),
                                  ({"plan": unset}, {"plan": "pro"})):
            merged, conflicts = config.merge_config_files(canonical, legacy,
                                                          legacy_wins=legacy_wins)
            assert merged == {"plan": "pro"} and conflicts == []
            assert config.plan(merged) == plan_profile.active_plan(merged) == "pro"


@pytest.mark.parametrize("key, good, bad", [
    ("active_reviewers", ["copilot"], "copilot"),
    ("auto_on_open", {"claude": True}, ["claude"]),
    ("label_gated_ci", True, "no"),
])
def test_a_malformed_reader_setting_never_overrides_a_valid_one(key, good, bad):
    """``active_reviewers()`` reads only a list, ``auto_on_open()`` only a mapping and
    ``label_gated_ci()`` only a real bool, so a malformed value of the same key in the
    newer file yields to the valid value on the other side instead of replacing it."""
    for legacy_wins in (True, False):
        for canonical, legacy in (({key: good}, {key: bad}), ({key: bad}, {key: good})):
            merged, conflicts = config.merge_config_files(canonical, legacy,
                                                          legacy_wins=legacy_wins)
            assert _same_setting(merged[key], good) and conflicts == []


def _same_setting(a, b):
    return type(a) is type(b) and a == b


# ── Unreadable legacy file → fail open ──────────────────────────────────────────

@pytest.mark.parametrize("kind", ["invalid-yaml", "not-a-mapping", "permission", "fifo"])
def test_unreadable_legacy_is_left_untouched_and_reported_once(home, kind, capsys):
    legacy = _legacy(home)
    legacy.parent.mkdir(parents=True)
    if kind == "invalid-yaml":
        legacy.write_text("plan: [unclosed\n")
    elif kind == "not-a-mapping":
        legacy.write_text("- just\n- a list\n")
    elif kind == "permission":
        if os.geteuid() == 0:
            pytest.skip("root reads a 000 file")
        legacy.write_text("plan: pro\n")
        os.chmod(legacy, 0o000)
    else:
        os.mkfifo(legacy)
    before = os.lstat(legacy)
    for _ in range(3):
        assert config.config_path() == _canonical(home)
    assert config.load_config() == {}
    after = os.lstat(legacy)
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == \
        (before.st_ino, before.st_mode, before.st_mtime_ns)
    assert not (home / ".config" / "buddhi").exists()  # nothing created, not even a lock
    assert _backups(home) == []
    out, err = capsys.readouterr()
    assert out == ""
    warn = [ln for ln in err.splitlines() if "Could not read the old config file" in ln]
    assert len(warn) == 1 and str(legacy) in warn[0]
    reason = {"invalid-yaml": "not valid YAML", "not-a-mapping": "not a YAML mapping",
              "permission": "unreadable", "fifo": "not a regular file"}[kind]
    assert warn[0] == (f"Warning: Could not read the old config file {legacy} ({reason}). "
                       f"It was left unchanged. Settings are read from {_canonical(home)}.")
    if kind == "permission":
        os.chmod(legacy, 0o600)


def test_a_legacy_folder_that_cannot_be_searched_reports_the_approved_reason(home, capsys):
    """The lstat of the legacy path itself fails (its folder lacks the search bit):
    the user sees the approved word ``unreadable``, never an OS error string."""
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    _put(_legacy(home), LEGACY)
    folder = _legacy(home).parent
    os.chmod(folder, 0o000)
    try:
        assert config.config_path() == _canonical(home)
    finally:
        os.chmod(folder, 0o700)
    assert capsys.readouterr().err.strip() == (
        f"Warning: Could not read the old config file {_legacy(home)} (unreadable). "
        f"It was left unchanged. Settings are read from {_canonical(home)}.")
    assert _load(_legacy(home)) == LEGACY


def test_an_error_while_reading_reports_the_approved_reason(home, monkeypatch, capsys):
    """The file opens but the read itself fails (an I/O error on a failing disk):
    the user still sees the approved word ``unreadable``."""
    _put(_legacy(home), LEGACY)
    real_fdopen = os.fdopen

    class Failing:
        def __init__(self, f):
            self._f = f
        def __enter__(self):
            return self
        def __exit__(self, *a):
            self._f.close()
        def read(self):
            raise OSError(5, "Input/output error")

    with monkeypatch.context() as m:
        m.setattr(config.os, "fdopen", lambda fd, *a, **k: Failing(real_fdopen(fd, *a, **k)))
        assert config.config_path() == _canonical(home)
    assert capsys.readouterr().err.strip() == (
        f"Warning: Could not read the old config file {_legacy(home)} (unreadable). "
        f"It was left unchanged. Settings are read from {_canonical(home)}.")


def test_a_canonical_file_that_cannot_be_read_reports_the_approved_reason(home, capsys):
    if os.geteuid() == 0:
        pytest.skip("root reads a 000 file")
    _put(_canonical(home), {"plan": "pro"})
    _put(_legacy(home), LEGACY)
    os.chmod(_canonical(home), 0o000)
    try:
        assert config.config_path() == _canonical(home)
    finally:
        os.chmod(_canonical(home), 0o600)
    assert capsys.readouterr().err.strip() == (
        f"Warning: Could not move settings from {_legacy(home)} to {_canonical(home)} because "
        f"config.yaml could not be read: unreadable. Both files were left unchanged. "
        f"Settings are read from {_canonical(home)}.")
    assert _load(_legacy(home)) == LEGACY and _load(_canonical(home)) == {"plan": "pro"}


def test_a_copied_legacy_file_is_recorded_so_a_refused_rename_never_re_merges_it(home):
    """No canonical file yet, so the legacy file is byte-copied; its rename is then
    refused (a read-only legacy folder). The user deletes a repo from the canonical
    file. The next run must NOT merge the same legacy bytes again and bring it back."""
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    _put(_legacy(home), LEGACY)
    folder = _legacy(home).parent
    os.chmod(folder, 0o500)
    try:
        config.config_path()
        assert _load(_canonical(home)) == LEGACY
        cfg = _load(_canonical(home))
        del cfg["repos"][REPO]
        _put(_canonical(home), cfg)
        config.config_path()
        assert REPO not in _load(_canonical(home))["repos"]
        assert _legacy(home).exists()
    finally:
        os.chmod(folder, 0o700)


def test_unreadable_canonical_leaves_both_files_and_fails_open(home, capsys):
    _put(_canonical(home), None, raw="plan: [unclosed\n")
    _put(_legacy(home), LEGACY)
    canon_before, legacy_before = _canonical(home).read_bytes(), _legacy(home).read_bytes()
    assert config.config_path() == _canonical(home)
    assert _canonical(home).read_bytes() == canon_before
    assert _legacy(home).read_bytes() == legacy_before
    assert "Both files were left unchanged" in capsys.readouterr().err


@pytest.mark.parametrize("canonical_exists", [False, True])
def test_a_failed_write_leaves_the_legacy_file_and_fails_open(home, monkeypatch, capsys,
                                                             canonical_exists):
    _put(_legacy(home), LEGACY)
    before = _legacy(home).read_bytes()
    if canonical_exists:
        _put(_canonical(home), {"known_repos": [REPO]})
    monkeypatch.setattr(wizard, "write_config", lambda cfg, path: False)
    monkeypatch.setattr(config, "_write_bytes_atomic", lambda path, raw: False)
    assert config.config_path() == _canonical(home)
    assert _legacy(home).read_bytes() == before and _backups(home) == []
    assert "because writing the file failed" in capsys.readouterr().err


def test_a_migration_crash_never_escapes_the_resolver(home, monkeypatch, capsys):
    _put(_legacy(home), LEGACY)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(config, "_migrate_locked", boom)
    assert config.config_path() == _canonical(home)
    err = capsys.readouterr().err
    assert "Could not move settings" in err and "disk on fire" in err


# ── Clause 4: stdout stays JSON-only on the status verb ─────────────────────────

def test_status_verb_migrates_with_json_only_stdout(home):
    _put(_legacy(home), LEGACY)
    r = _cli(home, "status", "--repo", REPO)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"repo_confirmed": True, "has_global_default": True}
    assert r.stdout.count("\n") == 1
    assert "Config moved" in r.stderr
    assert _load(_canonical(home)) == LEGACY


# ── Clause 6: the lock ──────────────────────────────────────────────────────────

def test_lock_file_sits_beside_the_config_at_0600(home):
    p = _canonical(home)
    with config.config_lock(p):
        pass
    lock = p.with_name("config.yaml.lock")
    assert lock.exists()
    assert stat.S_IMODE(os.stat(lock).st_mode) == 0o600


def test_lock_is_reentrant_within_a_thread(home, capsys):
    p = _canonical(home)
    start = time.monotonic()
    with config.config_lock(p, timeout=0.5):
        with config.config_lock(p, timeout=0.5):
            assert config.set_repo_keys(REPO, {"auto_merge": True}, p)
    assert time.monotonic() - start < 0.4
    assert capsys.readouterr().err == ""
    assert _load(p)["repos"][REPO]["auto_merge"] is True


def test_timeout_covers_another_thread_holding_the_in_process_lock(home, capsys):
    p = _canonical(home)
    held = threading.Event()
    release = threading.Event()

    def hold():
        with config.config_lock(p, timeout=1) as may_write:
            assert may_write is True
            held.set()
            assert release.wait(2)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(1)
    try:
        start = time.monotonic()
        with config.config_lock(p, timeout=0.05) as may_write:
            waited = time.monotonic() - start
        assert may_write is False
        assert 0.03 <= waited < 0.15
        assert holder.is_alive()
        assert "Another thread has held the config lock" in capsys.readouterr().err
    finally:
        release.set()
        holder.join(2)
    assert not holder.is_alive()
    with config.config_lock(p, timeout=0.05) as may_write:
        assert may_write is True


_HOLD_LOCK = r"""
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
open(sys.argv[2], "w").close()
time.sleep(float(sys.argv[3]))
"""


def _wait_for(path, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.005)


def test_a_held_lock_is_waited_on_then_failed_open(home, tmp_path, capsys):
    p = _canonical(home)
    p.parent.mkdir(parents=True)
    held = tmp_path / "held"
    holder = subprocess.Popen([sys.executable, "-c", _HOLD_LOCK, str(p) + ".lock",
                               str(held), "3"])
    try:
        _wait_for(held)
        start = time.monotonic()
        with config.config_lock(p, timeout=0.3) as may_write:
            waited = time.monotonic() - start
        assert may_write is False
        assert 0.25 <= waited < 2.5
        assert "has held the config lock" in capsys.readouterr().err
    finally:
        holder.kill()
        holder.wait()


def _hold_lock(p, tmp_path):
    """A separate process holding ``p``'s config lock for 30 s, started and held."""
    held = tmp_path / "held"
    holder = subprocess.Popen([sys.executable, "-c", _HOLD_LOCK, str(p) + ".lock",
                               str(held), "30"])
    try:
        _wait_for(held)
    except BaseException:
        holder.kill()
        holder.wait()
        raise
    return holder


def test_a_writer_that_gives_up_on_the_lock_writes_nothing(home, tmp_path, monkeypatch, capsys):
    """The holder may be mid read-modify-write: a write made without the lock would
    be overwritten by its stale snapshot, so every writer refuses instead."""
    p = _put(_canonical(home), {"plan": "pro"})
    before = p.read_bytes()
    monkeypatch.setattr(config, "LOCK_TIMEOUT_S", 0.3)
    holder = _hold_lock(p, tmp_path)
    try:
        assert config.set_repo_keys(REPO, {"auto_merge": True}, p) is False
        assert wizard.write_config({"plan": "max-5x"}, p) is False
        assert wizard._write_global_default(FLEET, {"copilot": True}, p,
                                            auto_merge=False) is False
        # One bounded wait per hold: the writers inside it refuse without waiting again.
        start = time.monotonic()
        with config.config_lock(p) as may_write:
            waited = time.monotonic() - start
            assert may_write is False
            assert config.set_repo_keys(REPO, {"auto_merge": True}, p) is False
            assert wizard.write_config({"plan": "max-5x"}, p) is False
        assert time.monotonic() - start < waited + config.LOCK_TIMEOUT_S
        assert p.read_bytes() == before
        assert sorted(q.name for q in p.parent.iterdir()) == ["config.yaml", "config.yaml.lock"]
        assert capsys.readouterr().err.count("has held the config lock") == 4
    finally:
        holder.kill()
        holder.wait()
    # Once the holder is gone the next write takes the lock and lands.
    assert config.set_repo_keys(REPO, {"auto_merge": True}, p) is True
    assert _load(p) == {"plan": "pro", "repos": {REPO: {"auto_merge": True}}}


@pytest.mark.parametrize("argv", [None, ["--repo", REPO]], ids=["full", "confirm"])
def test_setup_that_gives_up_on_the_lock_says_so(home, tmp_path, monkeypatch, capsys, argv):
    """Setup cannot save while another process holds the config lock past the
    timeout. Its failure line names the lock as a possible cause, and the lock line
    says the config was not changed."""
    _stub_wizard(monkeypatch)
    p = _put(_canonical(home), {"plan": "pro"})
    before = p.read_bytes()
    monkeypatch.setattr(config, "LOCK_TIMEOUT_S", 0.3)
    holder = _hold_lock(p, tmp_path)
    try:
        out = io.StringIO()
        rc = wizard.run(argv=argv, run=_fake_run, which=lambda x: None,
                        single_select=_answers(auto_merge_on=True, lgc_on=False),
                        multi_select=lambda *a, **k: set(), getpass_fn=lambda *a: "",
                        spawn_command=lambda *a, **k: {"spawned": False},
                        input_fn=lambda *a: "", stream=out)
    finally:
        holder.kill()
        holder.wait()
    assert rc == 1
    assert (f"Could not write {p} — check the path's permissions, or whether another "
            f"process holds the config lock") in out.getvalue()
    # (The full line at the default 10 s is pinned by the stuck-holder test.)
    err = capsys.readouterr().err
    assert f"Warning: Another process has held the config lock {p}.lock for more than " in err
    assert ("The config was not changed. Run the command again once that process has "
            "finished.") in err
    assert p.read_bytes() == before


def test_a_migration_that_gives_up_on_the_lock_is_deferred(home, tmp_path, monkeypatch, capsys):
    """The migration never runs without the lock. This process reads the canonical
    file as it is and does not wait again on its later resolves (every
    ``load_config()`` resolves the path); the next process migrates."""
    _put(_legacy(home), LEGACY, mtime_ns=time.time_ns() - 10**9)
    canonical = _put(_canonical(home), {"known_repos": [REPO]})
    monkeypatch.setattr(config, "LOCK_TIMEOUT_S", 0.3)
    holder = _hold_lock(canonical, tmp_path)
    try:
        before = _tree(home / ".config")
        assert config.migrate_legacy_config() == "deferred"
        start = time.monotonic()
        for _ in range(3):
            assert config.config_path() == canonical
        assert config.load_config() == {"known_repos": [REPO]}
        assert config.migrate_legacy_config() == "deferred"
        assert time.monotonic() - start < config.LOCK_TIMEOUT_S
        assert _tree(home / ".config") == before
        err = capsys.readouterr().err
        assert err.count("has held the config lock") == 1
        assert "Config moved" not in err and "Could not" not in err
    finally:
        holder.kill()
        holder.wait()
    monkeypatch.setattr(config, "_deferred", set())  # a fresh process
    assert config.migrate_legacy_config() == "migrated"
    assert _load(canonical) == {"known_repos": [REPO], **LEGACY}
    assert not _legacy(home).exists() and len(_backups(home)) == 1


@pytest.mark.parametrize("argv", [None, ["--repo", REPO]], ids=["full", "confirm"])
def test_setup_reports_a_failed_write_while_another_process_holds_the_lock(
        home, tmp_path, monkeypatch, argv):
    _stub_wizard(monkeypatch)
    p = _put(_canonical(home), {"plan": "pro"})
    before = p.read_bytes()
    monkeypatch.setattr(config, "LOCK_TIMEOUT_S", 0.3)
    holder = _hold_lock(p, tmp_path)
    try:
        assert _run_wizard(argv) == 1
        assert p.read_bytes() == before
    finally:
        holder.kill()
        holder.wait()


_CHILD_WRITER = r"""
import os, sys, time, pathlib
from buddhi_review import config
ready, go, cfg, tag, n, delay = sys.argv[1:7]
_orig = config.load_config
def slow_load(path=None):
    data = _orig(path)
    time.sleep(float(delay))   # widen the read-modify-write window
    return data
config.load_config = slow_load
pathlib.Path(ready).touch()
while not os.path.exists(go):
    time.sleep(0.002)
for i in range(int(n)):
    assert config.set_repo_keys(f"{tag}/repo{i}", {"active_reviewers": ["claude"]},
                                pathlib.Path(cfg))
"""


def _race(h, tmp_path, script, argv_per_child):
    go = tmp_path / "go"
    procs, readies = [], []
    try:
        for i, argv in enumerate(argv_per_child):
            ready = tmp_path / f"ready{i}"
            readies.append(ready)
            procs.append(subprocess.Popen(
                [sys.executable, "-c", script, str(ready), str(go), *argv],
                env=_env(h), cwd=str(h), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True))
        for ready in readies:
            _wait_for(ready)
        go.touch()
        results = [p.communicate(timeout=60) for p in procs]
        for p, (out, err) in zip(procs, results):
            assert p.returncode == 0, err
        return results
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        for p in procs:
            p.communicate()


def test_two_concurrent_writers_lose_no_setting(home, tmp_path):
    cfg = _put(_canonical(home), {"plan": "pro"})
    _race(home, tmp_path, _CHILD_WRITER,
          [[str(cfg), "alpha", "4", "0.05"], [str(cfg), "beta", "4", "0.05"]])
    data = _load(cfg)
    assert data["plan"] == "pro"
    assert set(data["repos"]) == {f"{t}/repo{i}" for t in ("alpha", "beta") for i in range(4)}


_CHILD_RESOLVE = r"""
import os, sys, time, pathlib
from buddhi_review import config
ready, go = sys.argv[1:3]
_orig = config._read_config_file
def slow(path):
    got = _orig(path)
    time.sleep(0.1)   # widen the migration's read-merge-write window
    return got
config._read_config_file = slow
pathlib.Path(ready).touch()
while not os.path.exists(go):
    time.sleep(0.002)
print(config.config_path())
"""


def test_two_concurrent_migrations_move_the_file_exactly_once(home, tmp_path):
    _put(_legacy(home), LEGACY, mtime_ns=time.time_ns() - 10**9)
    _put(_canonical(home), {"known_repos": [REPO]})
    results = _race(home, tmp_path, _CHILD_RESOLVE, [[], []])
    assert all(out.strip() == str(_canonical(home)) for out, _ in results)
    cfg = _load(_canonical(home))
    assert cfg["known_repos"] == [REPO]
    assert {k: v for k, v in cfg.items() if k != "known_repos"} == LEGACY
    assert len(_backups(home)) == 1 and not _legacy(home).exists()
    stderr = "".join(err for _, err in results)
    assert stderr.count("Config moved") == 1
    assert "Warning" not in stderr


_CHILD_MIGRATE_OR_WRITE = r"""
import os, sys, time, pathlib
from buddhi_review import config
ready, go, role, cfg, signal = sys.argv[1:6]
if role == "migrate":
    _orig = config._read_config_file
    def slow(path):
        got = _orig(path)
        if pathlib.Path(path) == pathlib.Path(cfg):
            pathlib.Path(signal).touch()   # the canonical file has just been read
            time.sleep(0.3)
        return got
    config._read_config_file = slow
pathlib.Path(ready).touch()
while not os.path.exists(go):
    time.sleep(0.002)
if role == "migrate":
    config.config_path()
else:
    # Write exactly inside the other process's read-merge-write window.
    deadline = time.monotonic() + 20
    while not os.path.exists(signal):
        assert time.monotonic() < deadline, "the migration never read the canonical file"
        time.sleep(0.002)
    assert config.set_repo_keys("late/writer", {"auto_merge": True}, pathlib.Path(cfg))
"""


def test_a_writer_racing_a_migration_loses_nothing(home, tmp_path):
    _put(_legacy(home), LEGACY, mtime_ns=time.time_ns() - 10**9)
    cfg = _put(_canonical(home), {"known_repos": [REPO]})
    signal = tmp_path / "canonical-read"
    _race(home, tmp_path, _CHILD_MIGRATE_OR_WRITE,
          [["migrate", str(cfg), str(signal)], ["write", str(cfg), str(signal)]])
    data = _load(cfg)
    assert data["repos"]["late/writer"] == {"auto_merge": True}
    assert data["repos"][REPO] == LEGACY["repos"][REPO]
    assert data["plan"] == "max-5x" and data["known_repos"] == [REPO]


# ── The real sequence: install-skills + setup + status, both orders ──────────────

def _wizard_write_path(h):
    """The wizard's own persist path, exactly as ``wizard.run`` composes it."""
    path = config.config_path()
    new = wizard.build_config("max-5x", REPO, str(h / "widgets"), FLEET,
                              {"copilot": True, "codex": True})
    with config.config_lock(path):
        current = config.load_config(path) if path.exists() else {}
        assert wizard.write_config(wizard.merge_preserving(current, new), path)
        assert config.set_repo_keys(REPO, {"active_reviewers": FLEET,
                                           "auto_on_open": {"copilot": True, "codex": True},
                                           "auto_merge": False, "label_gated_ci": False},
                                    path)


@pytest.mark.parametrize("order", ["install-then-setup", "setup-then-install"])
def test_real_sequence_reports_the_repo_confirmed(home, order):
    steps = [lambda: _cli(home, "install-skills"), lambda: _wizard_write_path(home)]
    for step in (steps if order == "install-then-setup" else reversed(steps)):
        r = step()
        if r is not None:
            assert r.returncode == 0, r.stderr
    assert (home / ".config" / "buddhi" / "installed-skills.json").exists()
    r = _cli(home, "status", "--repo", REPO)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"repo_confirmed": True, "has_global_default": True}
    cfg = _load(_canonical(home))
    assert cfg["active_reviewers"] == FLEET
    assert not _legacy(home).exists()


@pytest.mark.parametrize("order", ["legacy-then-install", "install-then-legacy"])
def test_a_machine_set_up_by_an_earlier_release_is_confirmed(home, order):
    """The reproduced split: an earlier release's setup left the legacy file, and
    install-skills created the canonical folder (its sidecar) before or after it."""
    if order == "install-then-legacy":
        assert _cli(home, "install-skills").returncode == 0
        _put(_legacy(home), LEGACY)
    else:
        _put(_legacy(home), LEGACY)
        assert _cli(home, "install-skills").returncode == 0
    r = _cli(home, "status", "--repo", REPO)
    assert json.loads(r.stdout) == {"repo_confirmed": True, "has_global_default": True}
    assert _load(_canonical(home)) == LEGACY
    assert len(_backups(home)) == 1


# ── PRO-42: the setup wizard promotes the two landing globals ──────────────────

def _answers(*, auto_merge_on, lgc_on):
    def single_select(prompt, options, *, preselect=0, **kw):
        if prompt.startswith("  Auto-merge default for"):
            return 1 if auto_merge_on else 0
        if prompt.startswith("  Label-gated CI default for"):
            return 1 if lgc_on else 0
        if prompt.startswith("  Confirm: enable label-gated CI"):
            return 1 if lgc_on else 0
        return preselect
    return single_select


def _fake_run(argv, cwd=None, timeout=30, input=None):
    R = types.SimpleNamespace
    if argv[:2] == ["gh", "--version"]:
        return R(returncode=0, stdout="gh version 2.90.0 (2026-01-01)")
    if argv[:2] == ["git", "-C"] and "remote" in argv:
        return R(returncode=0, stdout="git@github.com:acme/widgets.git\n")
    if argv[:2] == ["git", "-C"] and "--show-toplevel" in argv:
        return R(returncode=0, stdout="/work/widgets\n")
    return R(returncode=0, stdout="")


def _stub_wizard(monkeypatch):
    monkeypatch.setattr(wizard, "step_reviewers",
                        lambda *a, **k: (list(FLEET), {"copilot": True, "codex": True}))
    monkeypatch.setattr(wizard, "_offer_install_ready_for_ci", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "step_pro_trial", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "step_repo_test_command", lambda *a, **k: None)


def _run_wizard(argv=None, *, auto_merge_on=False, lgc_on=True):
    return wizard.run(argv=argv, run=_fake_run, which=lambda x: None,
                      single_select=_answers(auto_merge_on=auto_merge_on, lgc_on=lgc_on),
                      multi_select=lambda *a, **k: set(), getpass_fn=lambda *a: "",
                      spawn_command=lambda *a, **k: {"spawned": False},
                      input_fn=lambda *a: "", stream=io.StringIO())


@pytest.mark.parametrize("am,lgc", [(False, True), (True, False), (True, True)])
def test_full_wizard_run_writes_the_two_globals_to_the_canonical_file(home, monkeypatch, am, lgc):
    """The top-level keys EXIST after a full setup (for a separately installed
    backend's per-repo choice check) and always hold False, whatever the bound repo
    answered."""
    _stub_wizard(monkeypatch)
    assert _run_wizard(auto_merge_on=am, lgc_on=lgc) == 0
    cfg = _load(_canonical(home))
    assert cfg["auto_merge"] is False and cfg["label_gated_ci"] is False
    # The per-repo entry carries the explicit answers.
    assert cfg["repos"][REPO]["auto_merge"] is am
    assert cfg["repos"][REPO]["label_gated_ci"] is lgc
    assert cfg["active_reviewers"] == FLEET


def test_full_wizard_run_keeps_a_write_made_during_its_prompts(home, monkeypatch):
    """The end-of-run write re-reads the file under the lock, so a write another
    process made while the user was answering prompts is kept."""
    _stub_wizard(monkeypatch)
    real = wizard.step_repo_auto_merge

    def meanwhile(*a, **k):
        config.set_repo_keys("zeta/app", {"active_reviewers": ["claude"]}, _canonical(home))
        return real(*a, **k)

    monkeypatch.setattr(wizard, "step_repo_auto_merge", meanwhile)
    assert _run_wizard() == 0
    cfg = _load(_canonical(home))
    assert config.repo_entry(cfg, "zeta/app") == {"active_reviewers": ["claude"]}
    assert config.repo_entry(cfg, REPO) is not None


def test_first_per_repo_confirm_promotes_the_two_globals(home, monkeypatch):
    _stub_wizard(monkeypatch)
    assert _run_wizard(["--repo", REPO], auto_merge_on=True, lgc_on=False) == 0
    cfg = _load(_canonical(home))
    assert cfg["active_reviewers"] == FLEET  # first setup → promoted fleet
    assert cfg["auto_merge"] is False and cfg["label_gated_ci"] is False
    assert cfg["repos"][REPO]["auto_merge"] is True


def test_later_per_repo_confirm_leaves_the_established_globals(home, monkeypatch):
    _stub_wizard(monkeypatch)
    _put(_canonical(home), {"active_reviewers": ["claude"], "auto_merge": False,
                            "label_gated_ci": False})
    assert _run_wizard(["--repo", "zeta/app"], auto_merge_on=True, lgc_on=True) == 0
    cfg = _load(_canonical(home))
    assert cfg["active_reviewers"] == ["claude"]
    assert cfg["auto_merge"] is False and cfg["label_gated_ci"] is False
    assert cfg["repos"]["zeta/app"]["label_gated_ci"] is True


def test_later_per_repo_confirm_arms_missing_choice_globals(home, monkeypatch):
    """A migrated reviewer fleet keeps its values while confirmation adds only the
    missing top-level choice keys, always False."""
    _stub_wizard(monkeypatch)
    _put(_canonical(home), {"active_reviewers": ["claude"],
                            "auto_on_open": {"claude": True}})
    assert _run_wizard(["--repo", "zeta/app"], auto_merge_on=True, lgc_on=True) == 0
    cfg = _load(_canonical(home))
    assert cfg["active_reviewers"] == ["claude"]
    assert cfg["auto_on_open"] == {"claude": True}
    assert cfg["auto_merge"] is False and cfg["label_gated_ci"] is False
    assert cfg["repos"]["zeta/app"]["auto_merge"] is True
    assert cfg["repos"]["zeta/app"]["label_gated_ci"] is True


@pytest.mark.parametrize("argv", [None, ["--repo", REPO]], ids=["full", "confirm"])
def test_setup_leaves_a_hand_set_global_label_gated_ci_alone(home, monkeypatch, argv):
    """A user who set a top-level label_gated_ci by hand keeps it — value and
    inheritance — however setup answers for the bound repo."""
    _stub_wizard(monkeypatch)
    _put(_canonical(home), {"label_gated_ci": True,
                            "repos": {"old/repo": {"auto_merge": True}}})
    assert _run_wizard(argv, auto_merge_on=False, lgc_on=False) == 0
    cfg = _load(_canonical(home))
    assert cfg["label_gated_ci"] is True
    assert config.label_gated_ci(cfg, "old/repo") is True
    assert config.label_gated_ci(cfg, "other/x") is True
    assert cfg["auto_merge"] is False  # the other key was still established


def test_a_global_default_set_during_a_first_confirm_is_kept(home, monkeypatch):
    """Two first-time confirms overlap: the one that finishes second must not
    replace the global default the first one established while it was prompting."""
    _stub_wizard(monkeypatch)
    real = wizard.step_repo_auto_merge

    def meanwhile(*a, **k):
        wizard._write_global_default(["claude"], {"claude": False}, _canonical(home),
                                     auto_merge=False, label_gated_ci=False)
        return real(*a, **k)

    monkeypatch.setattr(wizard, "step_repo_auto_merge", meanwhile)
    assert _run_wizard(["--repo", REPO]) == 0
    cfg = _load(_canonical(home))
    assert cfg["active_reviewers"] == ["claude"] and cfg["auto_merge"] is False
    assert cfg["repos"][REPO]["active_reviewers"] == FLEET


_DEFAULT_TIMEOUT_WAIT = r"""
import sys, time
from buddhi_review import config
start = time.monotonic()
with config.config_lock(__import__("pathlib").Path(sys.argv[1])):   # the DEFAULT timeout
    pass
print(round(time.monotonic() - start, 2))
"""


def test_a_stuck_lock_holder_is_waited_on_for_ten_seconds_by_default(home, tmp_path):
    """The default bound is 10 s: a stuck holder must never hang ``status``, which
    the skills run before every launch. A fresh process takes the lock with no
    explicit timeout while another process holds it for far longer."""
    p = _canonical(home)
    p.parent.mkdir(parents=True)
    held = tmp_path / "held"
    holder = subprocess.Popen([sys.executable, "-c", _HOLD_LOCK, str(p) + ".lock",
                               str(held), "60"])
    try:
        _wait_for(held)
        waiter = subprocess.Popen([sys.executable, "-c", _DEFAULT_TIMEOUT_WAIT, str(p)],
                                  env=_env(home), cwd=str(home), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        try:
            out, err = waiter.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            waiter.kill()
            waiter.communicate()
            raise AssertionError("config_lock did not give up within 25 s on a stuck holder")
        assert waiter.returncode == 0, err
        assert 9.5 <= float(out.strip()) <= 20
        assert err.strip() == (
            f"Warning: Another process has held the config lock {p}.lock for more than "
            f"10 seconds. The config was not changed. Run the command again once that "
            f"process has finished.")
    finally:
        holder.kill()
        holder.wait()
    assert config.LOCK_TIMEOUT_S == 10.0


@pytest.mark.parametrize("newer", ["legacy", "canonical"])
@pytest.mark.parametrize("raw", ["", "# setup ran; nothing chosen\n", "{}\n", "null\n"],
                         ids=["empty", "comment-only", "braces", "null"])
def test_an_empty_legacy_file_never_replaces_an_existing_canonical_file(home, raw, newer):
    """A legacy file that holds no settings beside a canonical file that does: the
    canonical settings stay, the legacy file is retired, whichever is newer."""
    t = time.time_ns()
    _put(_canonical(home), LEGACY, mtime_ns=t if newer == "canonical" else t - 10**9)
    _put(_legacy(home), None, raw=raw, mtime_ns=t if newer == "legacy" else t - 10**9)
    config.config_path()
    assert _load(_canonical(home)) == LEGACY
    assert not _legacy(home).exists() and len(_backups(home)) == 1
    assert _backups(home)[0].read_bytes() == raw.encode()


def test_full_wizard_run_re_reads_the_file_under_the_lock(home, monkeypatch):
    """A write that lands AFTER the end-of-run lock is requested and BEFORE the file
    is re-read is kept: the re-read happens under the lock, not before it."""
    _stub_wizard(monkeypatch)
    real_lock = config.config_lock
    state = {"injected": False}

    @contextlib.contextmanager
    def lock_then_write(path, **kw):
        with real_lock(path, **kw) as may_write:
            if not state["injected"]:
                state["injected"] = True
                config.set_repo_keys("zeta/app", {"active_reviewers": ["claude"]}, Path(path))
            yield may_write

    monkeypatch.setattr(config, "config_lock", lock_then_write)
    assert _run_wizard() == 0
    cfg = _load(_canonical(home))
    assert config.repo_entry(cfg, "zeta/app") == {"active_reviewers": ["claude"]}
    assert config.repo_entry(cfg, REPO) is not None


def test_a_setup_with_no_bound_repo_writes_no_top_level_choice_keys(home, monkeypatch):
    """No repo bound means the auto-merge / label-gated-CI questions were never
    asked, so no top-level key may be established (those keys arm the paid
    per-repo gates)."""
    _stub_wizard(monkeypatch)

    def no_remote(argv, cwd=None, timeout=30, input=None):
        if argv[:2] == ["git", "-C"] and "remote" in argv:
            return types.SimpleNamespace(returncode=1, stdout="")
        return _fake_run(argv, cwd=cwd, timeout=timeout, input=input)

    rc = wizard.run(argv=None, run=no_remote, which=lambda x: None,
                    single_select=_answers(auto_merge_on=True, lgc_on=True),
                    multi_select=lambda *a, **k: set(), getpass_fn=lambda *a: "",
                    spawn_command=lambda *a, **k: {"spawned": False},
                    input_fn=lambda *a: "", stream=io.StringIO())
    assert rc == 0
    cfg = _load(_canonical(home))
    assert "repo" not in cfg and "repos" not in cfg
    assert "auto_merge" not in cfg and "label_gated_ci" not in cfg
    assert cfg["active_reviewers"] == FLEET


def test_a_lock_file_that_cannot_be_opened_refuses_writes(home, monkeypatch, capsys):
    """A failed open does not prove there is no holder (the lock file's permissions
    can change while another process keeps its flock), so every writer refuses and
    the open failure is reported in its own words, not as a timeout."""
    p = _put(_canonical(home), {"plan": "pro"})
    before = p.read_bytes()
    real_open = os.open

    def refuse_lock_open(path, *a, **kw):
        if str(path).endswith(".lock"):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr(config.os, "open", refuse_lock_open)
    with config.config_lock(p) as may_write:
        assert may_write is False
    assert config.set_repo_keys(REPO, {"auto_merge": True}, p) is False
    assert wizard.write_config({"plan": "max-5x"}, p) is False
    assert p.read_bytes() == before
    err = capsys.readouterr().err
    assert err.count("Could not open the config lock file") == 3
    assert "has held the config lock" not in err


def _flock_failing_with(monkeypatch, code, times=None):
    """Make ``fcntl.flock`` raise ``OSError(code)`` (``times`` times, else always)."""
    calls = {"n": 0}

    def flaky(fd, op):
        calls["n"] += 1
        if times is None or calls["n"] <= times:
            raise OSError(code, os.strerror(code))

    monkeypatch.setattr(config.fcntl, "flock", flaky)
    return calls


def test_an_unexpected_flock_error_refuses_writes(home, monkeypatch, capsys):
    """ENOLCK (lock resources exhausted) takes no lock and proves no absence of a
    holder, so writers refuse instead of overwriting another process's update."""
    p = _put(_canonical(home), {"plan": "pro"})
    before = p.read_bytes()
    _flock_failing_with(monkeypatch, errno.ENOLCK)
    with config.config_lock(p) as may_write:
        assert may_write is False
    assert config.set_repo_keys(REPO, {"auto_merge": True}, p) is False
    assert wizard.write_config({"plan": "max-5x"}, p) is False
    assert p.read_bytes() == before
    assert "Could not take the config lock" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["ENOTSUP", "EOPNOTSUPP", "ENOSYS"])
def test_a_file_system_without_flock_still_lets_writers_go_on(home, monkeypatch, name):
    p = _put(_canonical(home), {"plan": "pro"})
    _flock_failing_with(monkeypatch, getattr(errno, name))
    with config.config_lock(p) as may_write:
        assert may_write is True


def test_an_eintr_is_retried_within_the_deadline(home, monkeypatch):
    p = _put(_canonical(home), {"plan": "pro"})
    calls = _flock_failing_with(monkeypatch, errno.EINTR, times=2)
    with config.config_lock(p, timeout=5) as may_write:
        assert may_write is True
        assert calls["n"] == 3  # two interrupted attempts, then the one that took it


def test_an_endless_eintr_cannot_outlast_the_deadline(home, monkeypatch, capsys):
    p = _put(_canonical(home), {"plan": "pro"})
    _flock_failing_with(monkeypatch, errno.EINTR)
    with config.config_lock(p, timeout=0.2) as may_write:
        assert may_write is False
    assert "has held the config lock" in capsys.readouterr().err


def test_an_interrupt_while_waiting_for_the_lock_leaks_no_fd(home, tmp_path, monkeypatch):
    p = _canonical(home)
    p.parent.mkdir(parents=True)
    held = tmp_path / "held"
    holder = subprocess.Popen([sys.executable, "-c", _HOLD_LOCK, str(p) + ".lock",
                               str(held), "5"])
    try:
        _wait_for(held)
        fds = len(os.listdir("/dev/fd"))

        def interrupt(_s):
            raise KeyboardInterrupt

        with monkeypatch.context() as m:
            m.setattr(config.time, "sleep", interrupt)
            with pytest.raises(KeyboardInterrupt):
                with config.config_lock(p, timeout=5):
                    pass
        assert len(os.listdir("/dev/fd")) == fds
    finally:
        holder.kill()
        holder.wait()


# ── PRO-42 safety: an unconfirmed repo resolves exactly as before ───────────────
# The config a fresh full setup writes for acme/widgets with label-gated CI ON, and
# the same config WITHOUT the established top-level keys (what setup wrote before).

PROMOTED = {
    "plan": "max-5x", "active_reviewers": ["claude"], "auto_on_open": {"claude": False},
    "notifications": "console", "auto_merge": False, "label_gated_ci": False,
    "repos": {REPO: {"active_reviewers": ["claude"], "auto_on_open": {"claude": False},
                     "auto_merge": True, "label_gated_ci": True}},
}
BEFORE = {k: v for k, v in PROMOTED.items() if k not in ("auto_merge", "label_gated_ci")}


@pytest.mark.parametrize("repo", ["o/r", "other/repo", None])
def test_resolvers_are_unchanged_for_a_repo_without_its_own_value(repo):
    for cfg in (PROMOTED, BEFORE):
        assert config.label_gated_ci(cfg, repo) is False
        assert config.auto_merge(cfg, repo) is False
    # A confirmed repo that has no label_gated_ci key of its own, too.
    confirmed_no_key = {**PROMOTED, "repos": {"o/r": {"active_reviewers": ["claude"]}}}
    assert config.label_gated_ci(confirmed_no_key, "o/r") is False
    # The bound repo keeps its explicit answer.
    assert config.label_gated_ci(PROMOTED, REPO) is True


def test_plan_profile_resolver_is_unchanged(tmp_path, monkeypatch):
    for cfg in (PROMOTED, BEFORE):
        path = _put(tmp_path / "c.yaml", cfg)
        monkeypatch.setenv("BUDDHI_CONFIG", str(path))
        assert plan_profile.label_gated_ci("other/repo") is False
        assert plan_profile.label_gated_ci() is False
        assert plan_profile.label_gated_ci(REPO) is True


@pytest.mark.parametrize("cfg", [PROMOTED, BEFORE], ids=["promoted", "before"])
def test_the_merge_loop_takes_the_plain_path_for_an_unconfirmed_repo(cfg):
    """round_driver's pre-merge fork on ``label_gated_ci(self.cfg, self.repo)``:
    the harness repo ``o/r`` is unconfirmed, so it merges on the non-label path —
    no ``ready-for-ci`` attach and no CI poll — with or without the promoted global."""
    from test_round_driver import make_driver
    timeline = [(0, Comment(id="a", text="No issues found.", source="claude[bot]"))]
    driver, _, gh = make_driver(timeline, cfg=cfg, auto_merge=True)
    outcome = driver.run()
    assert outcome.merged is True
    assert gh.matching("--add-label", "ready-for-ci") == []
    assert gh.matching("pr", "checks") == []


@pytest.mark.parametrize("cfg", [PROMOTED, BEFORE], ids=["promoted", "before"])
def test_the_wizard_ready_for_ci_attach_is_unchanged_for_an_unconfirmed_repo(cfg, tmp_path):
    path = _put(tmp_path / "c.yaml", cfg)
    calls = []

    def run(argv, **k):
        calls.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    for opted_in in (False, None):
        assert wizard._attach_ready_for_ci("other/repo", "https://github.com/other/repo/pull/1",
                                           run=run, sleep=lambda s: None,
                                           opted_in=opted_in, cfg_path=path) is True
    assert calls == []  # no label created or added


@pytest.mark.parametrize("cfg", [PROMOTED, BEFORE], ids=["promoted", "before"])
def test_the_wizard_default_for_a_new_repo_is_unchanged(cfg, home, monkeypatch):
    """Both per-repo steps preselect from the resolvers; a repo being confirmed for
    the first time sees the built-in defaults whether or not a global was promoted."""
    _stub_wizard(monkeypatch)
    _put(_canonical(home), cfg)
    seen = {}

    def spy(name, real):
        def wrapped(repo, current_default, **k):
            seen[name] = current_default
            return real(repo, current_default, **k)
        return wrapped

    monkeypatch.setattr(wizard, "step_repo_auto_merge",
                        spy("auto_merge", wizard.step_repo_auto_merge))
    monkeypatch.setattr(wizard, "step_repo_label_gated_ci",
                        spy("label_gated_ci", wizard.step_repo_label_gated_ci))
    assert _run_wizard(["--repo", "zeta/app"], lgc_on=False) == 0
    assert seen == {"auto_merge": False, "label_gated_ci": False}


def test_a_hand_set_global_is_still_inherited():
    """A top-level value keeps today's inheritance (tests/test_f1_perrepo_write_status.py
    pins the rest); setup only ever establishes False, which inherits as off."""
    assert config.label_gated_ci({"label_gated_ci": True}, "any/repo") is True
    assert config.label_gated_ci({"label_gated_ci": False}, "any/repo") is False


def test_establish_global_defaults_writes_false_only_for_what_was_asked():
    assert config.establish_global_defaults({"plan": "pro"}) == {"plan": "pro"}
    assert config.establish_global_defaults({}, auto_merge=True, label_gated_ci=True) == \
        {"auto_merge": False, "label_gated_ci": False}
    assert config.establish_global_defaults({}, label_gated_ci=False) == {"label_gated_ci": False}
    # A value already there (set by hand) is left exactly as it is.
    hand = {"label_gated_ci": True, "auto_merge": True}
    assert config.establish_global_defaults(hand, auto_merge=False, label_gated_ci=False) == hand
