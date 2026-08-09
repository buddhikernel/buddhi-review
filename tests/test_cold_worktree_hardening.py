"""Cold-worktree hardening: the two guards that keep a worktree whose gate has
never run from poisoning the round.

1. **Runner-dropping ignore hygiene** — a fix commit never sweeps a dependency-
   install / build / cache / coverage artifact (`node_modules/`, `target/`,
   `build/`, `bin/Debug/`, `.coverage`, …) into the customer's PR, and the
   exclusion costs a FIXED number of git pathspecs no matter how many files the
   tree holds (a cold `node_modules` is thousands of untracked files —
   enumerating them per file would blow git's argv). Only UNTRACKED artifacts are
   held back: a fixer's edit to a TRACKED file under a runner dir must still reach
   the commit, and a real `src/bin/` is never swept.
2. **Per-runner gate timeout** — `BUDDHI_TEST_GATE_TIMEOUT_SECS_<RUNNER>` lifts
   ONE runner's ceiling; with no per-runner var set the gate is byte-identical to
   the global-only behaviour (no extra pre-run runner detection either).
"""
import os
import shutil
import subprocess

import pytest

from buddhi_review import commit_push, test_runner


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _write(root, rel, text="x = 1\n"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _rec_notice(store):
    def notice(action, detail="", *, status="do", hint=None):
        store.append((action, detail, status))
        return ""
    return notice


def _staged(cwd):
    """The paths in the index that differ from HEAD, as a set. ``-z`` for VERBATIM
    names — plain output C-quotes any path holding a backslash or a non-ASCII byte.
    A staged RENAME is reported by its DESTINATION only (git's rename detection),
    so a test about a rename's two halves asserts on the resulting commit instead."""
    r = subprocess.run(["git", "diff", "--cached", "--name-only", "-z"], cwd=cwd,
                       capture_output=True, text=True, check=True)
    return {p for p in r.stdout.split("\0") if p}


@pytest.fixture
def git_repo(tmp_path):
    """A minimal git worktree with one committed file."""
    _write(tmp_path, "seed.py")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


# ── 1. Runner-dropping classification ────────────────────────────────────────────

_RUNNER_PATHS = [
    "node_modules/left-pad/index.js",
    "packages/web/node_modules/react/index.js",   # workspace, nested
    "target/debug/app",
    "pkg/__pycache__/mod.cpython-311.pyc",
    ".pytest_cache/v/cache/lastfailed",
    "build/classes/Main.class",
    ".gradle/7.6/fileHashes.bin",
    "deps/plug/mix.exs",
    "_build/dev/lib/app.beam",
    "coverage/lcov-report/index.html",
    "htmlcov/index.html",
    ".nyc_output/out.json",
    ".tox/py311/bin/python",
    ".nox/tests/bin/python",
    "src/App/bin/Debug/net8.0/App.dll",           # dotnet, under a config child
    "src/App/obj/Release/project.assets.json",
    ".coverage",
    "sub/coverage.xml",
    "js/lcov.info",
]

# Legitimate source paths that must NEVER be classified as runner droppings.
_RUNNER_DECOYS = [
    "src/bin/main.rs",            # a Rust multi-binary tree — bare `bin/`
    "src/bin/tool/helper.rs",
    "bin/release-notes.sh",       # a repo's own scripts dir
    "src/obj/loader.c",           # bare `obj/`
    "app/builder.py",             # `build` as a SUBSTRING, not a segment
    "app/rebuild/x.py",
    "targets/list.txt",
    "docs/coverage-policy.md",
    "src/node_modules_shim.js",
    "lcov.info.md",
    "seed.py",
]


@pytest.mark.parametrize("path", _RUNNER_PATHS)
def test_runner_dropping_matched_at_any_depth(path):
    assert commit_push._is_runner_dropping(path) is True


@pytest.mark.parametrize("path", _RUNNER_DECOYS)
def test_legitimate_paths_are_not_runner_droppings(path):
    assert commit_push._is_runner_dropping(path) is False


def test_dotnet_bin_obj_only_under_a_build_config_child():
    """A bare `bin/`/`obj/` segment collides with real source layouts, so dotnet
    output counts ONLY under a Debug/Release child."""
    for d in ("bin", "obj"):
        for cfg in ("Debug", "Release"):
            assert commit_push._is_runner_dropping(f"App/{d}/{cfg}/App.dll") is True
            assert commit_push._is_runner_dropping(f"App/{d}/{cfg}") is True
        assert commit_push._is_runner_dropping(f"App/{d}/Custom/App.dll") is False
        assert commit_push._is_runner_dropping(f"App/{d}/main.rs") is False


def test_runner_dropping_handles_edge_paths():
    assert commit_push._is_runner_dropping("") is False
    assert commit_push._is_runner_dropping(None) is False
    assert commit_push._is_runner_dropping("node_modules/") is True   # porcelain dir form
    assert commit_push._is_runner_dropping("/node_modules/x") is True


def test_a_literal_backslash_is_not_a_separator(git_repo):
    """REGRESSION. `\\` must NOT be normalized to `/`: porcelain emits `/` on every
    platform (Windows included) and `_runner_globs` emits `/`-only globs, so a file
    whose BASENAME literally contains backslashes can never be matched by the git
    exclude layer. Claiming it in the Python predicate alone made the guard report
    the file as excluded while it rode into the commit."""
    weird = "bin\\Debug\\App.dll"
    assert commit_push._is_runner_dropping(weird) is False
    assert commit_push._is_runner_dropping("pkg\\__pycache__\\m.pyc") is False

    # The two layers must AGREE on a real repo: whatever the predicate claims is
    # exactly what the git exclude pathspecs actually hold back.
    _write(git_repo, weird, "dll\n")
    _write(git_repo, "node_modules/real.js", "// dep\n")
    _write(git_repo, "fix.py", "z = 3\n")
    notices = []
    assert commit_push._stage_all(str(git_repo), notice=_rec_notice(notices)).returncode == 0
    staged = _staged(git_repo)
    assert "node_modules/real.js" not in staged
    assert weird in staged                       # git cannot exclude it → it commits
    runner_notes = [n for n in notices if "runner artifact" in n[1]]
    assert weird not in (runner_notes[0][1] if runner_notes else "")   # and we don't lie


# ── 1b. The exclusion is count-INDEPENDENT (argv safety) ─────────────────────────


def test_runner_exclude_pathspec_set_is_fixed_and_small():
    """The pathspec set is derived from the pattern constants alone — nothing about
    a worktree can grow it."""
    specs = commit_push._runner_exclude_pathspecs()
    assert specs == commit_push._runner_exclude_pathspecs()   # deterministic
    expected = (2 * len(commit_push._RUNNER_DROPPING_DIRS)
                + len(commit_push._RUNNER_DROPPING_PARENT_DIRS)   # contents glob only
                + 2 * len(commit_push._RUNNER_DROPPING_DOTNET_DIRS)
                * len(commit_push._RUNNER_DROPPING_DOTNET_CONFIGS)
                + len(commit_push._RUNNER_DROPPING_FILES))
    assert len(specs) == expected
    assert all(s.startswith(":(top,exclude,glob)") for s in specs)
    assert sum(len(s) for s in specs) < 4096          # nowhere near an argv limit


@pytest.mark.parametrize("n_files", [5, 400])
def test_stage_all_argv_does_not_scale_with_planted_tree_size(git_repo, n_files):
    """THE argv guard: a cold worktree's thousands of untracked runner files must
    cost the same, fixed pathspec count as a handful. Asserted on the RECORDED
    `git add` argv, at two tree sizes, so a regression to per-file enumeration
    fails here."""
    for i in range(n_files):
        _write(git_repo, f"node_modules/pkg{i}/index.js", f"// {i}\n")
    _write(git_repo, "real.py", "y = 2\n")

    calls = []
    real_run = commit_push._default_run

    def run(argv, **kw):
        calls.append(list(argv))
        return real_run(argv, **kw)

    add = commit_push._stage_all(str(git_repo), run=run, notice=_rec_notice([]))
    assert add.returncode == 0
    adds = [c for c in calls if c[:3] == ["git", "add", "-A"]]
    assert adds, calls
    argv = adds[0]
    # The argv holds the fixed exclude set (+ `:/`) and NOTHING per planted file.
    assert len(argv) == 5 + len(commit_push._runner_exclude_pathspecs())
    assert not any("pkg0/index.js" in a for a in argv)
    assert sum(len(a) for a in argv) < 8192
    # ...and the outcome is right: the real edit staged, no artifact swept in.
    assert _staged(git_repo) == {"real.py"}


# ── 1c. Staging behaviour on a real repo ─────────────────────────────────────────


def test_nested_artifacts_are_held_back_at_the_GIT_layer(git_repo):
    """REGRESSION. The any-depth promise has to be pinned on the git EXCLUDE layer,
    not only on the Python predicate — the globs are what actually hold files back.
    Reverting `_runner_globs`' `**/` prefix to a root-anchored glob passes every
    predicate test while four NESTED artifacts ride into the customer's PR."""
    _write(git_repo, "packages/web/node_modules/left-pad/index.js", "// dep\n")
    _write(git_repo, "packages/api/target/debug/blob.bin", "bin\n")
    _write(git_repo, "sub/dir/deep/htmlcov/index.html", "<html>\n")
    _write(git_repo, "svc/one/__pycache__/m.cpython-311.pyc", "pyc\n")
    _write(git_repo, "src/app.js", "the fix\n")

    assert commit_push._stage_all(str(git_repo), notice=_rec_notice([])).returncode == 0
    assert _staged(git_repo) == {"src/app.js"}
    _git(git_repo, "commit", "-qm", "fix")
    tree = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=git_repo,
                          capture_output=True, text=True, check=True).stdout
    for nested in ("node_modules", "target/", "htmlcov", "__pycache__"):
        assert nested not in tree, tree


def test_a_git_add_spawn_failure_returns_nonzero_instead_of_crashing(git_repo):
    """REGRESSION. The OSError guard on the main `git add -A` was untested — without
    it an E2BIG on a pathological tree raises out of the round instead of handing
    back a return code the caller can read."""
    _write(git_repo, "node_modules/left-pad/index.js", "// dep\n")
    _write(git_repo, "fix.py", "z = 3\n")
    real_run = commit_push._default_run

    def run(argv, **kw):
        if argv[:3] == ["git", "add", "-A"] and "--" in argv:
            raise OSError(7, "Argument list too long")
        return real_run(argv, **kw)

    add = commit_push._stage_all(str(git_repo), run=run, notice=_rec_notice([]))
    assert add.returncode != 0
    assert "Argument list too long" in (add.stderr or "")


def test_the_unstage_reset_argv_is_batched_too(git_repo):
    """REGRESSION. Only the recovery list was argv-bounded; the un-stage RESET list
    grows with the number of pre-staged artifacts just as fast. Unbatched this
    produces a quarter-megabyte argv."""
    n = 2500
    for i in range(n):
        _write(git_repo, f"node_modules/pkg{i}/a-fairly-long-artifact-name-{i}.js", "//\n")
    _write(git_repo, "fix.py", "z = 3\n")
    _git(git_repo, "add", "-A")                       # the fixer pre-staged them all

    calls = []
    real_run = commit_push._default_run

    def run(argv, **kw):
        calls.append(list(argv))
        return real_run(argv, **kw)

    add = commit_push._stage_all(str(git_repo), run=run, notice=_rec_notice([]))
    assert add.returncode == 0
    assert _staged(git_repo) == {"fix.py"}
    resets = [c for c in calls if c[:2] == ["git", "reset"]]
    assert resets
    for argv in calls:
        assert sum(len(a.encode("utf-8")) + 1 for a in argv) <= \
            commit_push._PATHSPEC_ARGV_BUDGET + 4096, argv[:4]


def test_a_huge_per_file_exclude_list_falls_back_instead_of_blowing_argv(git_repo):
    """REGRESSION. The main `git add -A -- :/ <excludes>` is the one argv that cannot
    be split — every exclude must share one pathspec set. Its per-file half grows
    with the worktree, and appending the fixed runner globs pushed a band of trees
    that used to stage cleanly over the OS limit. Past the budget it now falls back
    to add-with-globs + a batched un-stage, which reaches the identical end state."""
    long_name = "d" * 180
    for i in range(700):
        _write(git_repo, f"s/{long_name}{i:04d}.bak", "backup\n")
    _write(git_repo, "node_modules/left-pad/index.js", "// dep\n")
    _write(git_repo, "README.md", "THE REAL FIX\n")

    calls = []
    real_run = commit_push._default_run

    def run(argv, **kw):
        calls.append(list(argv))
        return real_run(argv, **kw)

    add = commit_push._stage_all(str(git_repo), run=run, notice=_rec_notice([]))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"README.md"}      # droppings + artifact both held out
    for argv in calls:
        assert sum(len(a.encode("utf-8")) + 1 for a in argv) <= \
            commit_push._PATHSPEC_ARGV_BUDGET + 4096, argv[:4]


def test_a_tracked_only_runner_change_announces_nothing(git_repo):
    """REGRESSION. The notice must never claim it withheld a file that is in fact in
    the commit. Dropping the `_new_to_head` filter on the held-back list makes the
    log contradict the commit — the worst possible failure for an audit trail."""
    _write(git_repo, "node_modules/vendored/x.js", "// v1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "vendored")
    _write(git_repo, "node_modules/vendored/x.js", "// v2 the fix\n")

    notices = []
    assert commit_push._stage_all(str(git_repo), notice=_rec_notice(notices)).returncode == 0
    assert _staged(git_repo) == {"node_modules/vendored/x.js"}   # it IS committed
    assert not [n for n in notices if "runner artifact" in n[1]], notices


def test_untracked_runner_artifacts_are_held_back_and_announced(git_repo):
    _write(git_repo, "node_modules/left-pad/index.js", "// dep\n")
    _write(git_repo, "target/debug/app", "bin\n")
    _write(git_repo, "src/App/bin/Debug/App.dll", "dll\n")
    _write(git_repo, ".coverage", "cov\n")
    _write(git_repo, "fix.py", "z = 3\n")

    notices = []
    add = commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert add.returncode == 0
    assert _staged(git_repo) == {"fix.py"}
    runner_notes = [n for n in notices if "runner artifact" in n[1]]
    assert len(runner_notes) == 1
    assert runner_notes[0][2] == "skip"


def test_tracked_file_under_a_runner_dir_is_still_committed(git_repo):
    """A vendored/committed file under a runner dir is a REAL tracked file — a
    fixer's edit to it must reach the commit, never be held back."""
    _write(git_repo, "node_modules/vendored/patch.js", "// v1\n")
    _write(git_repo, "build/keep.txt", "v1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "vendor")

    _write(git_repo, "node_modules/vendored/patch.js", "// v2 — the fixer's edit\n")
    _write(git_repo, "build/keep.txt", "v2\n")
    _write(git_repo, "node_modules/left-pad/index.js", "// fresh install\n")  # untracked

    add = commit_push._stage_all(str(git_repo), notice=_rec_notice([]))
    assert add.returncode == 0
    assert _staged(git_repo) == {"node_modules/vendored/patch.js", "build/keep.txt"}


def test_tracked_file_under_a_runner_dir_deletion_is_committed(git_repo):
    _write(git_repo, "node_modules/vendored/patch.js", "// v1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "vendor")
    (git_repo / "node_modules/vendored/patch.js").unlink()

    add = commit_push._stage_all(str(git_repo), notice=_rec_notice([]))
    assert add.returncode == 0
    assert _staged(git_repo) == {"node_modules/vendored/patch.js"}


def test_src_bin_is_never_swept(git_repo):
    """A Rust `src/bin/` multi-binary tree is source, not dotnet output."""
    _write(git_repo, "src/bin/main.rs", "fn main() {}\n")
    _write(git_repo, "src/bin/tool.rs", "fn main() {}\n")
    _write(git_repo, "bin/release.sh", "#!/bin/sh\n")

    add = commit_push._stage_all(str(git_repo), notice=_rec_notice([]))
    assert add.returncode == 0
    assert _staged(git_repo) == {"src/bin/main.rs", "src/bin/tool.rs",
                                 "bin/release.sh"}


def test_prestaged_runner_artifact_is_unstaged(git_repo):
    """A prior `git add -A` that already staged an untracked node_modules must not
    survive — an exclude pathspec alone never un-stages."""
    _write(git_repo, "node_modules/left-pad/index.js", "// dep\n")
    _write(git_repo, "fix.py", "z = 3\n")
    _git(git_repo, "add", "-A")
    assert "node_modules/left-pad/index.js" in _staged(git_repo)

    add = commit_push._stage_all(str(git_repo), notice=_rec_notice([]))
    assert add.returncode == 0
    assert _staged(git_repo) == {"fix.py"}


def test_clean_repo_still_takes_the_bare_add_short_circuit(git_repo):
    """No droppings of EITHER kind → a plain `git add -A`, byte-identical to a
    no-guard fix commit (no `:/`, no exclude pathspecs)."""
    _write(git_repo, "fix.py", "z = 3\n")
    calls = []
    real_run = commit_push._default_run

    def run(argv, **kw):
        calls.append(list(argv))
        return real_run(argv, **kw)

    add = commit_push._stage_all(str(git_repo), run=run, notice=_rec_notice([]))
    assert add.returncode == 0
    assert ["git", "add", "-A"] in calls
    assert not any(len(c) > 3 and c[:3] == ["git", "add", "-A"] for c in calls)
    assert _staged(git_repo) == {"fix.py"}


def test_editor_dropping_guard_still_holds_with_runner_artifacts_present(git_repo):
    """The pre-existing editor/backup guard is preserved alongside the new one."""
    _write(git_repo, "foo.bak", "backup\n")
    _write(git_repo, "node_modules/left-pad/index.js", "// dep\n")
    _write(git_repo, "fix.py", "z = 3\n")

    notices = []
    add = commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert add.returncode == 0
    assert _staged(git_repo) == {"fix.py"}
    assert any("editor/backup dropping" in n[1] for n in notices)
    assert any("runner artifact" in n[1] for n in notices)


# ── 1d. Regressions the adversarial pass proved reachable ────────────────────────


def test_a_tracked_path_replaced_by_an_artifact_dir_never_leaks_its_subtree(git_repo):
    """REGRESSION (proven to reach a real remote). HEAD tracks a FILE named `build`
    (a build script); the round deletes it and the runner drops a `build/` output
    tree. The per-file recovery names `build`, which is now a DIRECTORY — with
    `git add -A` that pathspec recursively staged every untracked artifact under it,
    while the [auto] line told the operator they were excluded."""
    _write(git_repo, "build", "#!/bin/sh\nmake\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "build script")

    (git_repo / "build").unlink()
    _write(git_repo, "build/a.o", "obj\n")
    _write(git_repo, "build/CMakeFiles/huge.bin", "big\n")
    _write(git_repo, "real.txt", "the fix\n")

    assert commit_push._stage_all(str(git_repo), notice=_rec_notice([])).returncode == 0
    assert _staged(git_repo) == {"build", "real.txt"}   # the DELETION, not the tree
    _git(git_repo, "commit", "-qm", "fix")
    r = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=git_repo,
                       capture_output=True, text=True, check=True)
    assert "build/a.o" not in r.stdout
    assert "build/CMakeFiles/huge.bin" not in r.stdout


def test_a_tracked_symlink_replaced_by_an_install_tree_never_leaks(git_repo):
    """REGRESSION, same class: the common monorepo `node_modules` SYMLINK that a
    real install replaces with a directory."""
    (git_repo / "vendor").mkdir()
    _write(git_repo, "vendor/keep.js", "// v\n")
    os.symlink("vendor", git_repo / "node_modules")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "symlinked deps")

    (git_repo / "node_modules").unlink()
    _write(git_repo, "node_modules/left-pad/index.js", "// dep\n")
    _write(git_repo, "node_modules/.bin/x", "bin\n")
    _write(git_repo, "real.txt", "the fix\n")

    assert commit_push._stage_all(str(git_repo), notice=_rec_notice([])).returncode == 0
    assert _staged(git_repo) == {"node_modules", "real.txt"}


def test_a_staged_rename_under_a_runner_dir_keeps_both_halves(git_repo):
    """REGRESSION. A glob-based un-stage of pre-staged runner content also decomposed
    a staged RENAME, and the per-file recovery only ever sees porcelain's rename
    DESTINATION — so the origin's deletion never reached the commit and the
    renamed-away file was resurrected."""
    _write(git_repo, "build/old.txt", "PRECIOUS SOURCE\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "vendored")

    _git(git_repo, "mv", "build/old.txt", "build/new.txt")
    _write(git_repo, "node_modules/fresh/a.js", "// dep\n")
    _git(git_repo, "add", "node_modules/fresh/a.js")      # a fixer's own `git add -A`
    _write(git_repo, "real.txt", "the fix\n")

    assert commit_push._stage_all(str(git_repo), notice=_rec_notice([])).returncode == 0
    _git(git_repo, "commit", "-qm", "fix")
    r = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=git_repo,
                       capture_output=True, text=True, check=True)
    assert "build/old.txt" not in r.stdout          # NOT resurrected
    assert "build/new.txt" in r.stdout
    assert "node_modules/fresh/a.js" not in r.stdout


def test_a_risky_delete_pair_under_a_runner_dir_is_not_re_staged(git_repo):
    """REGRESSION (silent data loss). A backup-then-replace rewrite that failed
    mid-way, under a runner dir: the guard announced it was holding the deletion
    back, then the recovery re-staged it — committing the file's removal while its
    only surviving copy sat in the excluded, uncommitted `.bak`."""
    _write(git_repo, "build/src.py", "PRECIOUS = 1\n")
    _write(git_repo, "keep.txt", "k\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "vendored")

    (git_repo / "build/src.py").rename(git_repo / "build/src.py.bak")

    notices = []
    commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert "build/src.py" not in _staged(git_repo)
    assert any(n[2] == "stop" and "build/src.py" in n[1] for n in notices)


def test_per_file_recovery_argv_is_batched_under_arg_max(git_repo):
    """REGRESSION (E2BIG). A runner that reinstalls a VENDORED, tracked tree makes
    the per-file recovery list scale with the file count. Unbatched, ~10k paths
    raised `OSError: Argument list too long` straight out of the round."""
    n = 3000
    for i in range(n):
        _write(git_repo, f"node_modules/pkg{i}/a-fairly-long-vendored-path-{i}.js", "//\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "vendored")
    shutil.rmtree(str(git_repo / "node_modules"))
    _write(git_repo, "real.txt", "the fix\n")

    calls = []
    real_run = commit_push._default_run

    def run(argv, **kw):
        calls.append(list(argv))
        return real_run(argv, **kw)

    add = commit_push._stage_all(str(git_repo), run=run, notice=_rec_notice([]))
    assert add.returncode == 0
    for argv in calls:
        assert sum(len(a) + 1 for a in argv) < 200_000, argv[:4]
    assert len(_staged(git_repo)) == n + 1        # every deletion + the fix
    assert any(c[:3] == ["git", "add", "-u"] for c in calls)


def test_a_fixer_staged_deletion_under_a_runner_dir_does_not_abort_the_round(git_repo):
    """REGRESSION (round-killer). A fixer that deletes a stale committed artifact
    under `build/` and stages it itself leaves `D ` — no index entry, no worktree
    file. Recovering that path made `git add` exit 128 (`pathspec did not match any
    files`), so `_stage_all` returned non-zero and the WHOLE round became an
    `error` that never shipped the fix. It also re-reproduced on every later round."""
    _write(git_repo, "build/stale.txt", "old\n")
    _write(git_repo, "src/main.py", "def f():\n    return 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "seed2")

    _git(git_repo, "rm", "-q", "build/stale.txt")            # the fixer stages it
    _write(git_repo, "src/main.py", "def f():\n    return 2\n")
    _git(git_repo, "add", "-A")

    add = commit_push._stage_all(str(git_repo), notice=_rec_notice([]))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"build/stale.txt", "src/main.py"}


def test_an_intent_to_add_runner_artifact_never_leaks(git_repo):
    """REGRESSION. `git add -N` creates a real INDEX ENTRY whose staged column is a
    SPACE (` A`), so it escaped the un-stage — and the per-file recovery's `git add
    -u` then filled that stub in with the artifact's FULL content (proven to reach a
    remote), while the guard logged it as excluded."""
    _write(git_repo, "build", "#!/bin/sh\nmake\n")
    _write(git_repo, "app.py", "v = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "build script")

    (git_repo / "build").unlink()
    _write(git_repo, "build/artifact.js", "RUNNER-ARTIFACT-SECRET\n")
    _write(git_repo, "build/sub/deep.min.js", "DEEP\n")
    _write(git_repo, "app.py", "v = 2\n")
    _git(git_repo, "add", "-N", "build/artifact.js", "build/sub/deep.min.js")

    add = commit_push._stage_all(str(git_repo), notice=_rec_notice([]))
    assert add.returncode == 0, getattr(add, "stderr", "")
    _git(git_repo, "commit", "-qm", "fix")
    r = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=git_repo,
                       capture_output=True, text=True, check=True)
    assert "build/artifact.js" not in r.stdout
    assert "build/sub/deep.min.js" not in r.stdout
    assert "app.py" in r.stdout


def test_batching_counts_utf8_bytes_not_characters():
    """REGRESSION. The kernel counts the ENCODED argv — measuring characters
    under-counted a 4-byte-UTF-8 tree by up to 4x, straight into E2BIG."""
    wide = "𝔫" * 500                       # 500 chars, 2000 UTF-8 bytes
    specs = [f":(top,literal){wide}/{i}" for i in range(200)]
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    commit_push._run_batched(run, ["git", "reset", "--"], specs, cwd=".")
    assert len(calls) > 1                   # it actually split
    for argv in calls:
        assert sum(len(a.encode("utf-8")) + 1 for a in argv) <= \
            commit_push._PATHSPEC_ARGV_BUDGET + 2048


def test_a_spawn_failure_returns_nonzero_instead_of_crashing(git_repo):
    """The staging guard's caller reads a RETURN CODE — an OSError from a git spawn
    must become a non-zero result, never kill the round."""
    _write(git_repo, "node_modules/vendored/x.js", "// v1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "vendored")
    _write(git_repo, "node_modules/vendored/x.js", "// v2\n")
    _write(git_repo, "node_modules/fresh/y.js", "// dep\n")

    real_run = commit_push._default_run

    def run(argv, **kw):
        if argv[:3] == ["git", "add", "-u"]:
            raise OSError(7, "Argument list too long")
        return real_run(argv, **kw)

    add = commit_push._stage_all(str(git_repo), run=run, notice=_rec_notice([]))
    assert add.returncode != 0


def test_held_back_set_covers_new_runner_artifacts_only():
    entries = [("??", "node_modules/left-pad/index.js"), ("??", "foo.bak"),
               ("M ", "node_modules/vendored/patch.js"), ("??", "fix.py")]
    assert commit_push._held_back_new_artifacts(entries) == {
        "node_modules/left-pad/index.js", "foo.bak"}


def test_clean_tree_tripwire_ignores_cold_gate_artifacts(git_repo, capsys):
    """A round whose only worktree residue is a cold-gate artifact is a legitimate
    no-op, not "a fixer wrote outside the committed set"."""
    _write(git_repo, "node_modules/left-pad/index.js", "// dep\n")
    notices = []
    commit_push._assert_clean_after_commit(str(git_repo), notice=_rec_notice(notices))
    assert notices == []

    _write(git_repo, "lost.py", "orphan = 1\n")
    commit_push._assert_clean_after_commit(str(git_repo), notice=_rec_notice(notices))
    assert len(notices) == 1
    assert "lost.py" in notices[0][1]
    assert "node_modules" not in notices[0][1]


# ── 1e. A file NAMED like a runner dir is source, not an artifact ────────────────


# `build`, `target`, `deps` and `coverage` are perfectly ordinary FILE names in
# Go/C/Make repos. A git pathspec matches paths, not types, so the bare `**/build`
# glob withheld `scripts/build` from the fix commit exactly as it withheld the
# `build/` output tree — and the tripwire, reading the same predicate, hid it.
_RUNNER_NAMED_FILES = [
    "scripts/build", "build", "tools/deps", "deps",
    "coverage", "bin/coverage", "target", "make/target",
]


@pytest.mark.parametrize("path", _RUNNER_NAMED_FILES)
def test_a_file_named_like_a_runner_dir_is_not_a_dropping(path):
    """REGRESSION. The LAST segment must never match a file-name-shaped runner dir
    — only a path UNDER one is an artifact."""
    assert commit_push._is_runner_dropping(path) is False
    assert commit_push._is_runner_dropping(f"{path}/out.o") is True   # …under it, yes


def test_a_file_named_like_a_runner_dir_reaches_the_commit(git_repo):
    """REGRESSION (silent no-op round). A reviewer asks the fixer to add
    `scripts/build`; the glob held it out, `_held_back_new_artifacts` hid it from the
    tripwire, and the round reported `pushed` with the fix missing — so the next
    round saw the comment unaddressed and looped."""
    _write(git_repo, "scripts/build", "#!/bin/sh\nmake all\n")
    _write(git_repo, "coverage", "#!/bin/sh\ngo test -cover\n")
    _write(git_repo, "build/out.o", "OBJ\n")            # the real artifact
    _write(git_repo, "node_modules/left-pad/index.js", "// dep\n")

    notices = []
    add = commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"scripts/build", "coverage"}
    # …and the tripwire still sees the real fix as committed, not as residue.
    _git(git_repo, "commit", "-qm", "fix")
    tripwire = []
    commit_push._assert_clean_after_commit(str(git_repo), notice=_rec_notice(tripwire))
    assert tripwire == []


# ── 1e-bis. A NEW file beside COMMITTED source in a `build/` is source ───────────


def test_a_new_file_beside_committed_source_is_not_an_artifact(git_repo):
    """REGRESSION (silent no-op round). A fixer asked to add `build/new_rule.py`
    next to a committed `build/existing.py` had it classified as runner output: the
    glob held it out, `_held_back_new_artifacts` hid it from the tripwire, and the
    round reported `pushed` with the fix missing."""
    _write(git_repo, "build/existing.py", "x = 1\n")
    _write(git_repo, "build/scripts/deploy.sh", "#!/bin/sh\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "build scripts")

    _write(git_repo, "build/new_rule.py", "def rule(): pass\n")       # the fix
    _write(git_repo, "build/scripts/rollback.sh", "#!/bin/sh\n")      # …one dir down
    _write(git_repo, "build/classes/Main.class", "CAFEBABE\n")        # REAL output
    _write(git_repo, "build/out.o", "OBJ\n")                          # …beside source
    _write(git_repo, "node_modules/left-pad/index.js", "// dep\n")

    notices = []
    add = commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert add.returncode == 0, getattr(add, "stderr", "")
    # `build/out.o` sits DIRECTLY in a committed-source dir, so it is source by the
    # same evidence — the discriminator is the directory, never the file name.
    assert _staged(git_repo) == {"build/new_rule.py", "build/scripts/rollback.sh",
                                 "build/out.o"}
    _git(git_repo, "commit", "-qm", "fix")
    tripwire = []
    commit_push._assert_clean_after_commit(str(git_repo), notice=_rec_notice(tripwire))
    assert tripwire == []


def test_a_fresh_output_subtree_under_a_committed_build_dir_stays_out(git_repo):
    """THE limit of the escape hatch. Committed source in `build/scripts` proves
    `build/scripts` is source — never `build`, and never Gradle's own fresh
    subtrees. Widening it to the whole `build/` tree walks every artifact into the
    customer's PR."""
    _write(git_repo, "build/scripts/deploy.sh", "#!/bin/sh\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "build scripts")

    _write(git_repo, "build/classes/java/Main.class", "CAFEBABE\n")
    _write(git_repo, "build/libs/app.jar", "JAR\n")
    _write(git_repo, "build/report.html", "<html>\n")   # directly in `build/` — no
    _write(git_repo, "real.py", "the fix\n")            #   committed sibling there

    add = commit_push._stage_all(str(git_repo), notice=_rec_notice([]))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"real.py"}


def test_the_source_dir_evidence_comes_from_HEAD_not_the_index(git_repo):
    """REGRESSION (artifact leak). The index is writable by the very actors this
    guard defends against: a fixer's `git add -N build/artifact.js` (or a blanket
    `git add -A` over a cold tree) would otherwise manufacture its own proof that
    `build/` is a source dir and walk the whole output tree into the PR."""
    _write(git_repo, "app.py", "v = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "seed2")

    _write(git_repo, "build/artifact.js", "RUNNER-ARTIFACT-SECRET\n")
    _write(git_repo, "build/sub/deep.min.js", "DEEP\n")
    _git(git_repo, "add", "-N", "build/artifact.js")     # the faked evidence
    _git(git_repo, "add", "build/sub/deep.min.js")       # …and a full one
    _write(git_repo, "app.py", "v = 2\n")

    entries = commit_push._status_entries(str(git_repo))
    assert commit_push._tracked_source_dirs(str(git_repo), entries) == set()
    add = commit_push._stage_all(str(git_repo), notice=_rec_notice([]))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"app.py"}


def test_an_unambiguous_artifact_inside_a_committed_build_dir_stays_out(git_repo):
    """No repo evidence can make `node_modules`, `__pycache__` or a coverage file
    source — the escape hatch reaches ONLY the file-name-shaped dirs."""
    _write(git_repo, "build/existing.py", "x = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "build source")

    _write(git_repo, "build/node_modules/left-pad/index.js", "// dep\n")
    _write(git_repo, "build/__pycache__/m.cpython-311.pyc", "pyc\n")
    _write(git_repo, "build/.coverage", "cov\n")
    _write(git_repo, "build/keep.py", "the fix\n")

    add = commit_push._stage_all(str(git_repo), notice=_rec_notice([]))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"build/keep.py"}


def test_a_nested_untracked_repo_in_a_committed_build_dir_is_never_staged(git_repo):
    """`--untracked-files=all` expands every untracked directory EXCEPT a nested git
    repo, which stays a `?? build/vendored/` entry. Staging that pathspec records a
    stray gitlink, so the escape hatch never applies to a directory entry."""
    _write(git_repo, "build/existing.py", "x = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "build source")

    _write(git_repo, "build/vendored/thing.py", "y = 2\n")
    _git(git_repo / "build/vendored", "init", "-q", ".")
    _write(git_repo, "build/new_rule.py", "the fix\n")

    entries = commit_push._status_entries(str(git_repo))
    assert ("??", "build/vendored/") in entries, entries
    assert commit_push._is_runner_dropping(
        "build/vendored/", source_dirs={"build"}) is True
    add = commit_push._stage_all(str(git_repo), notice=_rec_notice([]))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"build/new_rule.py"}


def test_source_dirs_never_overturns_a_hard_classification():
    """Unit-level twin of the two tests above: `source_dirs` reaches the ambiguous
    rule and nothing else."""
    dirs = {"build", "build/sub", "src/target", "coverage", "deps"}
    for path in ("build/new_rule.py", "build/sub/x.py", "src/target/y.rs",
                 "coverage/report.md", "deps/vendored.ex"):
        assert commit_push._is_runner_dropping(path) is True
        assert commit_push._is_runner_dropping(path, source_dirs=dirs) is False
    for path in ("build/node_modules/x.js", "build/__pycache__/m.pyc",
                 "build/.coverage", "build/lcov.info", "build/vendored/"):
        assert commit_push._is_runner_dropping(path, source_dirs=dirs) is True
    # ...and a dir whose evidence is one level up is untouched.
    assert commit_push._is_runner_dropping(
        "build/classes/Main.class", source_dirs=dirs) is True


def test_a_committed_file_named_build_is_not_evidence_of_a_source_dir(git_repo):
    """REGRESSION. HEAD tracks a FILE named `build`; the runner replaces it with an
    output tree. Reading that blob as "`build/` holds committed content" would hand
    the whole subtree the source escape hatch."""
    _write(git_repo, "build", "#!/bin/sh\nmake\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "build script")

    (git_repo / "build").unlink()
    _write(git_repo, "build/a.o", "obj\n")
    _write(git_repo, "real.txt", "the fix\n")

    entries = commit_push._status_entries(str(git_repo))
    assert commit_push._tracked_source_dirs(str(git_repo), entries) == set()
    assert commit_push._stage_all(str(git_repo), notice=_rec_notice([])).returncode == 0
    assert _staged(git_repo) == {"build", "real.txt"}       # the DELETION, not the tree


def test_a_failed_source_dir_probe_holds_everything_back(git_repo):
    """Best-effort: a git failure inside the probe must fall back to the
    pre-existing (withhold) behaviour, never fail the round."""
    _write(git_repo, "build/existing.py", "x = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "build source")
    _write(git_repo, "build/new_rule.py", "the fix\n")

    real_run = commit_push._default_run

    def run(argv, **kw):
        if argv[:2] == ["git", "ls-tree"]:
            raise OSError(7, "Argument list too long")
        return real_run(argv, **kw)

    add = commit_push._stage_all(str(git_repo), run=run, notice=_rec_notice([]))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == set()


def test_the_source_dir_probe_costs_one_pathspec_per_ambiguous_root(git_repo):
    """The probe must not reintroduce per-file argv growth: one `git ls-tree`
    pathspec per ambiguous ROOT dir, whatever the planted tree's size."""
    _write(git_repo, "build/existing.py", "x = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "build source")
    for i in range(400):
        _write(git_repo, f"build/classes/pkg{i}/Main{i}.class", f"// {i}\n")

    calls = []
    real_run = commit_push._default_run

    def run(argv, **kw):
        calls.append(list(argv))
        return real_run(argv, **kw)

    add = commit_push._stage_all(str(git_repo), run=run, notice=_rec_notice([]))
    assert add.returncode == 0
    trees = [c for c in calls if c[:2] == ["git", "ls-tree"]]
    assert trees, calls
    for argv in trees:
        assert len([a for a in argv if a.startswith(":(top,")]) == 1, argv
    assert _staged(git_repo) == set()          # all 400 are real output


def test_source_dir_rescue_works_when_cwd_is_a_subdirectory(git_repo):
    """REGRESSION. `git ls-tree` prints paths relative to (and limited to) `cwd`
    unless `--full-tree` is passed — `:(top,literal)` anchors the MATCH to the repo
    root, not the output. `--cwd` is a documented CLI option, so invoking the round
    from a subdirectory turned the listing into `../`-prefixed paths that could
    never intersect the root-relative `parents` dict: the rescue silently failed and
    a fixer's new file beside committed `build/` source was held back exactly like
    real runner output."""
    _write(git_repo, "build/existing.py", "x = 1\n")
    _write(git_repo, "sub/placeholder.py", "y = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "build source and a subdir")

    _write(git_repo, "build/new_rule.py", "the fix\n")
    sub_cwd = str(git_repo / "sub")

    entries = commit_push._status_entries(sub_cwd)
    assert commit_push._tracked_source_dirs(sub_cwd, entries) == {"build"}

    add = commit_push._stage_all(sub_cwd, notice=_rec_notice([]))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"build/new_rule.py"}


def test_the_held_back_set_drops_a_committed_source_dir_child():
    entries = [("??", "build/new_rule.py"), ("??", "build/classes/Main.class"),
               ("??", "node_modules/x.js"), ("??", "build/new_rule.py.bak")]
    assert commit_push._held_back_new_artifacts(entries) == {
        "build/new_rule.py", "build/classes/Main.class", "node_modules/x.js",
        "build/new_rule.py.bak"}
    assert commit_push._held_back_new_artifacts(entries, source_dirs={"build"}) == {
        "build/classes/Main.class", "node_modules/x.js", "build/new_rule.py.bak"}


def test_the_tripwire_reports_a_lost_source_dir_child(git_repo):
    """The `at minimum` half of the same defect: if the file somehow does NOT reach
    the commit, the tripwire must name it rather than filter it out as an artifact."""
    _write(git_repo, "build/existing.py", "x = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "build source")

    _write(git_repo, "build/new_rule.py", "the fix\n")   # never staged
    _write(git_repo, "build/classes/Main.class", "CAFEBABE\n")

    notices = []
    commit_push._assert_clean_after_commit(str(git_repo), notice=_rec_notice(notices))
    assert len(notices) == 1, notices
    assert "build/new_rule.py" in notices[0][1]
    assert "build/classes/Main.class" not in notices[0][1]


def test_the_unambiguous_names_keep_their_bare_glob():
    """The bare form is load-bearing for a tracked `node_modules` SYMLINK a real
    install replaces — those names are never a plausible source file."""
    globs = commit_push._runner_globs()
    for d in commit_push._RUNNER_DROPPING_DIRS:
        assert f"**/{d}" in globs and f"**/{d}/**" in globs
    for d in commit_push._RUNNER_DROPPING_PARENT_DIRS:
        assert f"**/{d}" not in globs and f"**/{d}/**" in globs


# ── 1f. An UNSTAGED rename INTO a runner dir keeps both halves ───────────────────


def test_an_unstaged_rename_into_a_runner_dir_holds_both_halves(git_repo):
    """REGRESSION (silent data loss). `mv src/old.py build/new.py` reaches the guard
    as `D src/old.py` + `?? build/new.py` — git does NO rename detection when the
    destination has no index entry. The glob held the destination back while
    `git add -A` staged the source's DELETION, and `_held_back_new_artifacts` then
    hid the destination from the tripwire: the PR dropped the file outright."""
    _write(git_repo, "src/old.py", "PRECIOUS = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "source")

    (git_repo / "build").mkdir()
    (git_repo / "src/old.py").rename(git_repo / "build/new.py")
    _write(git_repo, "real.txt", "the fix\n")

    notices = []
    add = commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert add.returncode == 0, getattr(add, "stderr", "")
    # The deletion is NOT committed alone — both halves stay as residue.
    assert _staged(git_repo) == {"real.txt"}
    assert any(n[2] == "stop" and "src/old.py" in n[1] for n in notices), notices


def test_an_unstaged_rename_into_a_runner_dir_pairs_from_a_subdirectory_cwd(git_repo):
    """REGRESSION. `git ls-files` prints paths relative to `cwd` unless `--full-name`
    is given — `:(top,literal)` only anchors the MATCH, not the output. `deleted`
    comes from root-relative porcelain, so without `--full-name` the two path spaces
    only coincided when `cwd` was the repo root: from a subdirectory the `../`-
    prefixed `ls-files` keys could never match, the pair went undetected, and the
    source's deletion was staged alone while the runner glob held its destination
    back — the exact silent file-loss this guard exists to prevent."""
    _write(git_repo, "src/old.py", "PRECIOUS = 1\n")
    _write(git_repo, "sub/placeholder.py", "y = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "source and a subdir")

    (git_repo / "build").mkdir()
    (git_repo / "src/old.py").rename(git_repo / "build/new.py")
    _write(git_repo, "real.txt", "the fix\n")
    sub_cwd = str(git_repo / "sub")

    entries = commit_push._status_entries(sub_cwd)
    assert commit_push._renamed_into_runner_sources(sub_cwd, entries) == {
        "src/old.py"}

    notices = []
    add = commit_push._stage_all(sub_cwd, notice=_rec_notice(notices))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"real.txt"}
    assert any(n[2] == "stop" and "src/old.py" in n[1] for n in notices), notices


def test_a_move_into_a_runner_dir_holds_both_halves_after_git_add_u(git_repo):
    """REGRESSION (silent data loss). The SAME move as above, from a fixer that ran
    `git add -u` (or `git rm`) after it: porcelain reports the deletion in the INDEX
    column (`D `), not the worktree one (` D`). Reading only the worktree column let
    this shape past the pair probe, so `git add -A` committed the deletion while the
    glob held the destination back and the tripwire hid it."""
    _write(git_repo, "src/old.py", "PRECIOUS = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "source")

    (git_repo / "build").mkdir()
    (git_repo / "src/old.py").rename(git_repo / "build/new.py")
    _git(git_repo, "add", "-u")                  # the deletion is now STAGED
    _write(git_repo, "real.txt", "the fix\n")

    entries = commit_push._status_entries(str(git_repo))
    assert ("D ", "src/old.py") in entries, entries
    assert commit_push._renamed_into_runner_sources(str(git_repo), entries) == {
        "src/old.py"}

    notices = []
    add = commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert add.returncode == 0, getattr(add, "stderr", "")
    # The staged deletion is un-staged again — only the real fix commits.
    assert _staged(git_repo) == {"real.txt"}
    assert any(n[2] == "stop" and "src/old.py" in n[1] for n in notices), notices

    # …and the still-deleted source is residue the tripwire names, so the round
    # cannot report success over a file it dropped.
    _git(git_repo, "commit", "-qm", "fix")
    tripwire = []
    commit_push._assert_clean_after_commit(str(git_repo), notice=_rec_notice(tripwire))
    assert len(tripwire) == 1, tripwire
    assert "src/old.py" in tripwire[0][1]
    assert "build/new.py" not in tripwire[0][1]   # that half IS deliberately withheld


def test_a_staged_move_into_a_runner_dir_pairs_from_a_subdirectory_cwd(git_repo):
    """REGRESSION, same class as the `ls-files` case above but for the STAGED-
    deletion half: `git ls-tree HEAD` also prints `cwd`-relative paths unless
    `--full-tree` is given, so the same `../`-prefixed-keys failure applied to a
    fixer's `git add -u`/`git rm` after the move when the round ran from a
    subdirectory."""
    _write(git_repo, "src/old.py", "PRECIOUS = 1\n")
    _write(git_repo, "sub/placeholder.py", "y = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "source and a subdir")

    (git_repo / "build").mkdir()
    (git_repo / "src/old.py").rename(git_repo / "build/new.py")
    _git(git_repo, "add", "-u")                  # the deletion is now STAGED
    _write(git_repo, "real.txt", "the fix\n")
    sub_cwd = str(git_repo / "sub")

    entries = commit_push._status_entries(sub_cwd)
    assert ("D ", "src/old.py") in entries, entries
    assert commit_push._renamed_into_runner_sources(sub_cwd, entries) == {
        "src/old.py"}

    notices = []
    add = commit_push._stage_all(sub_cwd, notice=_rec_notice(notices))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"real.txt"}
    assert any(n[2] == "stop" and "src/old.py" in n[1] for n in notices), notices


def test_a_staged_symlink_move_into_a_runner_dir_holds_both_halves(git_repo):
    """The symlink route reads the deleted entry's MODE, which for a staged deletion
    comes from HEAD rather than the index — a `120000` source must still pair."""
    _write(git_repo, "src/real.txt", "the pointed-at file\n")
    os.symlink("real.txt", git_repo / "src/link")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "source")

    (git_repo / "build").mkdir()
    shutil.move(str(git_repo / "src/link"), str(git_repo / "build/link"))
    _git(git_repo, "add", "-u")
    _write(git_repo, "real.txt", "the fix\n")

    entries = commit_push._status_entries(str(git_repo))
    assert commit_push._renamed_into_runner_sources(str(git_repo), entries) == {
        "src/link"}
    notices = []
    add = commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"real.txt"}
    assert any(n[2] == "stop" and "src/link" in n[1] for n in notices), notices


def test_a_staged_deletion_with_no_moved_content_still_commits(git_repo):
    """The widened deletion predicate must not hold back an ORDINARY staged
    deletion: pairing is still by CONTENT, so a `git rm`-ed file beside an unrelated
    artifact commits its deletion exactly as before."""
    _write(git_repo, "src/gone.py", "TO BE DELETED\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "source")

    _git(git_repo, "rm", "-q", "src/gone.py")
    _write(git_repo, "build/artifact.js", "SOMETHING ELSE ENTIRELY\n")
    _write(git_repo, "real.txt", "the fix\n")

    entries = commit_push._status_entries(str(git_repo))
    assert commit_push._renamed_into_runner_sources(str(git_repo), entries) == set()
    notices = []
    add = commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"src/gone.py", "real.txt"}
    assert not [n for n in notices if n[2] == "stop"], notices


def test_a_staged_move_inside_a_committed_build_dir_lands_whole(git_repo):
    """`source_dirs` still overrules the widened predicate: a staged move INSIDE the
    repo's own committed `build/` is an ordinary rename, not a pair to withhold."""
    _write(git_repo, "build/old.sh", "echo PRECIOUS\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "build scripts")

    (git_repo / "build/old.sh").rename(git_repo / "build/new.sh")
    _git(git_repo, "add", "-u")

    notices = []
    add = commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"build/new.sh"}
    assert not [n for n in notices if n[2] == "stop"], notices


def test_a_rename_inside_a_repos_own_committed_build_dir_lands_whole(git_repo):
    """Same defect, the likelier shape: a repo whose `build/` is real COMMITTED
    source. The source half is itself runner-classified, so the per-file RECOVERY
    would have staged its deletion even though the destination stayed held back.

    Both halves now reach the commit — `build/` holding committed source makes the
    destination ordinary source too (`_tracked_source_dirs`), so this is a plain
    rename, not a pair to withhold and hand to a human. The invariant under test is
    unchanged and stronger: the deletion is NEVER committed without its content."""
    _write(git_repo, "build/old.sh", "echo PRECIOUS\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "build scripts")

    (git_repo / "build/old.sh").rename(git_repo / "build/new.sh")

    notices = []
    add = commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"build/new.sh"}     # git renders the pair as R100
    _git(git_repo, "commit", "-qm", "fix")
    tree = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=git_repo,
                          capture_output=True, text=True, check=True).stdout
    assert "build/new.sh" in tree and "build/old.sh" not in tree, tree
    assert not [n for n in notices if n[2] == "stop"], notices


def test_a_symlink_renamed_into_a_runner_dir_holds_both_halves(git_repo):
    """REGRESSION (silent data loss). The rename probe was regular-files-only, so
    `mv src/link build/link` staged a bare `D src/link` while the glob held the new
    symlink back and the tripwire hid it — the PR dropped the link outright."""
    _write(git_repo, "src/real.txt", "the pointed-at file\n")
    os.symlink("real.txt", git_repo / "src/link")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "source")

    (git_repo / "build").mkdir()
    shutil.move(str(git_repo / "src/link"), str(git_repo / "build/link"))
    _write(git_repo, "real.txt", "the fix\n")

    notices = []
    add = commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"real.txt"}
    assert any(n[2] == "stop" and "src/link" in n[1] for n in notices), notices


def test_git_hash_object_is_never_trusted_for_a_symlink(git_repo):
    """THE reason the symlink route exists. `git hash-object build/link` returns the
    hash of what the link POINTS AT, so routing symlinks through the regular probe
    does not merely miss the pair — here it would invent one, pairing the deleted
    `src/payload.txt` with a symlink that is not its content at all."""
    _write(git_repo, "src/payload.txt", "the pointed-at file\n")
    _write(git_repo, "src/other.txt", "the pointed-at file\n")   # same bytes
    os.symlink("other.txt", git_repo / "src/link")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "source")

    (git_repo / "src/payload.txt").unlink()          # an ORDINARY deletion
    (git_repo / "build").mkdir()
    shutil.move(str(git_repo / "src/link"), str(git_repo / "build/link"))

    entries = commit_push._status_entries(str(git_repo))
    # `build/link` -> `other.txt`, whose bytes equal the deleted payload's, so a
    # dereferencing hash pairs them. Reading the LINK pairs only the real move.
    assert commit_push._renamed_into_runner_sources(str(git_repo), entries) == {
        "src/link"}


def test_a_deleted_regular_file_is_not_paired_with_a_new_symlink(git_repo):
    """The `120000` mode gate: a deleted regular file whose contents happen to spell
    a path must never read as a moved symlink."""
    _write(git_repo, "src/pathlike.txt", "real.txt")   # no newline — 8 bytes
    _write(git_repo, "real.txt", "target\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "source")

    (git_repo / "src/pathlike.txt").unlink()
    (git_repo / "build").mkdir()
    os.symlink("real.txt", git_repo / "build/link")

    entries = commit_push._status_entries(str(git_repo))
    assert commit_push._renamed_into_runner_sources(str(git_repo), entries) == set()
    add = commit_push._stage_all(str(git_repo), notice=_rec_notice([]))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"src/pathlike.txt"}   # an ordinary deletion, committed


def test_the_symlink_route_never_reads_a_size_mismatched_blob(git_repo):
    """Same cost guard as the regular route: a `cat-file -p` only for a blob whose
    length matches a destination link's target."""
    _write(git_repo, "src/big.txt", "a considerably longer file than any link\n")
    os.symlink("elsewhere.txt", git_repo / "src/link")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "source")

    (git_repo / "src/big.txt").unlink()               # deleted, but far too long
    (git_repo / "src/link").unlink()
    (git_repo / "build").mkdir()
    os.symlink("elsewhere.txt", git_repo / "build/link")

    calls = []
    real_run = commit_push._default_run

    def run(argv, **kw):
        calls.append(list(argv))
        return real_run(argv, **kw)

    entries = commit_push._status_entries(str(git_repo), run=run)
    assert commit_push._renamed_into_runner_sources(
        str(git_repo), entries, run=run) == {"src/link"}
    reads = [c for c in calls if c[:3] == ["git", "cat-file", "-p"]]
    assert len(reads) == 1, calls          # the link blob only, never big.txt's


def test_a_failed_symlink_blob_read_leaves_the_guard_unchanged(git_repo):
    """Best-effort: a git failure inside the symlink route must never fail the
    round — it falls back to the regular route's answer."""
    _write(git_repo, "src/real.txt", "the pointed-at file\n")
    os.symlink("real.txt", git_repo / "src/link")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "source")

    (git_repo / "build").mkdir()
    shutil.move(str(git_repo / "src/link"), str(git_repo / "build/link"))

    real_run = commit_push._default_run

    def run(argv, **kw):
        if argv[:3] == ["git", "cat-file", "-p"]:
            raise OSError(7, "Argument list too long")
        return real_run(argv, **kw)

    entries = commit_push._status_entries(str(git_repo), run=run)
    assert commit_push._renamed_into_runner_sources(
        str(git_repo), entries, run=run) == set()
    add = commit_push._stage_all(str(git_repo), run=run, notice=_rec_notice([]))
    assert add.returncode == 0, getattr(add, "stderr", "")


def test_an_unrelated_deletion_beside_an_artifact_still_commits(git_repo):
    """The pairing is by CONTENT, not by co-occurrence: a fixer's ordinary deletion
    in a cold worktree must still reach the PR."""
    _write(git_repo, "src/gone.py", "OLD = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "source")

    (git_repo / "src/gone.py").unlink()
    _write(git_repo, "node_modules/left-pad/index.js", "// dep\n")
    _write(git_repo, "build/out.o", "OBJ\n")
    _write(git_repo, "real.txt", "the fix\n")

    notices = []
    add = commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"src/gone.py", "real.txt"}
    assert not [n for n in notices if "moved INTO" in n[1]], notices


def test_an_empty_deletion_is_never_paired_with_an_empty_artifact(git_repo):
    """A 0-byte blob is not an identity. EVERY empty file hashes to `e69de29…`, so a
    deleted empty source (`__init__.py`, `py.typed`, `.gitkeep`) sitting beside any
    empty untracked artifact (cargo's 0-byte `target/debug/.cargo-lock`) would pair
    on that shared sha and hold a real deletion out of the PR — a silent no-op round
    the tripwire then reports as `pushed`. The size gate must skip the degenerate
    size entirely: no hash probe runs, and the deletion commits like any other."""
    _write(git_repo, "src/pkg/__init__.py", "")
    _write(git_repo, "src/pkg/mod.py", "OLD = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "source")

    (git_repo / "src/pkg/__init__.py").unlink()          # an ORDINARY deletion
    _write(git_repo, "target/debug/.cargo-lock", "")     # cargo's 0-byte dropping
    _write(git_repo, "real.txt", "the fix\n")

    calls = []
    real_run = commit_push._default_run

    def run(argv, **kw):
        calls.append(list(argv))
        return real_run(argv, **kw)

    entries = commit_push._status_entries(str(git_repo), run=run)
    assert commit_push._renamed_into_runner_sources(
        str(git_repo), entries, run=run) == set()
    assert not [c for c in calls if c[:2] == ["git", "hash-object"]], calls

    notices = []
    add = commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"src/pkg/__init__.py", "real.txt"}
    assert not [n for n in notices if "moved INTO" in n[1]], notices


def test_a_non_empty_move_still_pairs_beside_an_empty_deletion(git_repo):
    """The 0-byte skip is surgical, not a disabling: with BOTH an empty deletion and
    a real move into a runner dir in one tree, the empty one is ignored and the real
    pair is still caught and held back."""
    _write(git_repo, "src/pkg/py.typed", "")
    _write(git_repo, "src/old.py", "PRECIOUS = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "source")

    (git_repo / "src/pkg/py.typed").unlink()
    (git_repo / "target").mkdir()
    (git_repo / "src/old.py").rename(git_repo / "target/new.py")
    _write(git_repo, "target/debug/.cargo-lock", "")

    entries = commit_push._status_entries(str(git_repo))
    assert commit_push._renamed_into_runner_sources(
        str(git_repo), entries) == {"src/old.py"}

    notices = []
    add = commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert add.returncode == 0, getattr(add, "stderr", "")
    assert _staged(git_repo) == {"src/pkg/py.typed"}     # the real pair held back
    assert any(n[2] == "stop" and "src/old.py" in n[1] for n in notices), notices


def test_the_move_probe_never_hashes_a_size_mismatched_tree(git_repo):
    """THE cost guard: a cold `node_modules` must be stat'd, never READ. Only a
    candidate whose size already matches a deleted blob is handed to
    `git hash-object`."""
    _write(git_repo, "src/gone.py", "OLD = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "source")

    (git_repo / "src/gone.py").unlink()
    for i in range(300):
        _write(git_repo, f"node_modules/pkg{i}/index.js", f"// a distinctly longer {i}\n")

    calls = []
    real_run = commit_push._default_run

    def run(argv, **kw):
        calls.append(list(argv))
        return real_run(argv, **kw)

    add = commit_push._stage_all(str(git_repo), run=run, notice=_rec_notice([]))
    assert add.returncode == 0
    assert _staged(git_repo) == {"src/gone.py"}
    assert not [c for c in calls if c[:2] == ["git", "hash-object"]], calls
    for argv in calls:
        assert sum(len(a.encode("utf-8")) + 1 for a in argv) <= \
            commit_push._PATHSPEC_ARGV_BUDGET + 4096, argv[:4]


def test_a_failed_move_probe_leaves_the_guard_unchanged(git_repo):
    """Best-effort: a git failure inside the probe must never fail the round — the
    guard falls back to exactly its pre-probe behaviour."""
    _write(git_repo, "src/old.py", "PRECIOUS = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "source")

    (git_repo / "build").mkdir()
    (git_repo / "src/old.py").rename(git_repo / "build/new.py")
    real_run = commit_push._default_run

    def run(argv, **kw):
        if argv[:2] == ["git", "hash-object"]:
            raise OSError(7, "Argument list too long")
        return real_run(argv, **kw)

    add = commit_push._stage_all(str(git_repo), run=run, notice=_rec_notice([]))
    assert add.returncode == 0, getattr(add, "stderr", "")


# ── 1g. The exit rebase tolerates exactly what the guard withheld ────────────────


def _rebase_repo(tmp_path):
    """A branch pushed to a local `origin` whose base has advanced — the shape
    `exit_rebase` is called on after a manual-landing outcome."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work = tmp_path / "work"
    _write(work, "seed.py")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "seed")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "main")
    _git(work, "checkout", "-q", "-b", "feature")
    _write(work, "feat.py", "f = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "feat")
    _git(work, "push", "-q", "-u", "origin", "feature")
    # main advances behind us, so a real rebase has something to do.
    _git(work, "checkout", "-q", "main")
    _write(work, "other.py", "o = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "other")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-q", "feature")
    return work


def test_exit_rebase_tolerates_the_artifacts_the_guard_withheld(tmp_path):
    """REGRESSION. A cold-worktree round ENDS with the runner artifacts the staging
    guard intentionally withheld still untracked. `exit_rebase` rejected any nonempty
    porcelain, so the automatic base rebase skipped on exactly the runs this
    hardening targets — leaving the PR stranded behind its base."""
    work = _rebase_repo(tmp_path)
    _write(work, "node_modules/left-pad/index.js", "// dep\n")
    _write(work, "build/out.o", "OBJ\n")
    _write(work, "stale.bak", "backup\n")

    status, detail = commit_push.exit_rebase(
        str(work), base="main", notice=_rec_notice([]))
    assert status == "rebased", detail


def test_exit_rebase_skips_on_an_uncommitted_child_of_a_committed_build_dir(tmp_path):
    """The precondition reads the SAME tracked-source-dir answer the staging guard
    stages by. Without it the two part ways: a fixer's new file beside committed
    source in a `build/` would read as a withheld artifact and the rebase would run
    straight over the uncommitted fix."""
    work = _rebase_repo(tmp_path)
    _write(work, "build/existing.py", "x = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "build source")
    _git(work, "push", "-q", "origin", "feature")

    # A fresh output subtree under the same `build/` is still tolerated…
    _write(work, "build/classes/Main.class", "CAFEBABE\n")
    status, detail = commit_push.exit_rebase(
        str(work), base="main", notice=_rec_notice([]))
    assert status == "rebased", detail

    _write(work, "build/new_rule.py", "the fix\n")
    status, detail = commit_push.exit_rebase(
        str(work), base="main", notice=_rec_notice([]))
    assert status == "skipped" and "uncommitted changes" in detail


def test_exit_rebase_still_skips_on_a_genuinely_dirty_tree(tmp_path):
    """The relaxation is EXACTLY the held-back set: a real uncommitted edit, and the
    held-back half of a risky-delete pair (a tracked deletion — which would make the
    rebase itself fail), must both keep skipping."""
    work = _rebase_repo(tmp_path)
    _write(work, "node_modules/left-pad/index.js", "// dep\n")
    _write(work, "feat.py", "f = 2\n")                    # a real uncommitted edit
    status, detail = commit_push.exit_rebase(
        str(work), base="main", notice=_rec_notice([]))
    assert status == "skipped" and "uncommitted changes" in detail

    _git(work, "checkout", "--", "feat.py")
    (work / "feat.py").rename(work / "feat.py.bak")        # D feat.py + ?? feat.py.bak
    status, detail = commit_push.exit_rebase(
        str(work), base="main", notice=_rec_notice([]))
    assert status == "skipped" and "uncommitted changes" in detail


# ── 2. Per-runner gate timeout ───────────────────────────────────────────────────


def test_per_runner_timeout_override_is_honoured(monkeypatch):
    monkeypatch.delenv("BUDDHI_TEST_GATE_TIMEOUT_SECS", raising=False)
    monkeypatch.setenv("BUDDHI_TEST_GATE_TIMEOUT_SECS_GRADLE", "1800")
    default = commit_push._TEST_TIMEOUT_DEFAULT
    assert commit_push._test_gate_timeout(test_runner.GRADLE) == 1800
    assert commit_push._test_gate_timeout(test_runner.PYTEST) == default  # untouched
    assert commit_push._test_gate_timeout() == default
    assert commit_push._per_runner_timeout_configured() is True


def test_per_runner_env_key_sanitizes_non_alnum(monkeypatch):
    monkeypatch.setenv("BUDDHI_TEST_GATE_TIMEOUT_SECS_NODE_TEST", "900")
    assert commit_push._test_gate_timeout(test_runner.NODE_TEST) == 900


def test_per_runner_override_layers_over_a_custom_global(monkeypatch):
    monkeypatch.setenv("BUDDHI_TEST_GATE_TIMEOUT_SECS", "120")
    monkeypatch.setenv("BUDDHI_TEST_GATE_TIMEOUT_SECS_CARGO", "900")
    assert commit_push._test_gate_timeout(test_runner.CARGO) == 900
    assert commit_push._test_gate_timeout(test_runner.PYTEST) == 120


@pytest.mark.parametrize("raw", ["", "abc", "1e3"])
def test_per_runner_garbage_falls_back_to_the_global(monkeypatch, raw):
    monkeypatch.setenv("BUDDHI_TEST_GATE_TIMEOUT_SECS", "300")
    monkeypatch.setenv("BUDDHI_TEST_GATE_TIMEOUT_SECS_PYTEST", raw)
    assert commit_push._test_gate_timeout(test_runner.PYTEST) == 300


def test_per_runner_value_is_clamped_to_one_second(monkeypatch):
    monkeypatch.setenv("BUDDHI_TEST_GATE_TIMEOUT_SECS_PYTEST", "-5")
    assert commit_push._test_gate_timeout(test_runner.PYTEST) == 1


def test_unset_per_runner_is_byte_identical_to_today(monkeypatch):
    """No per-runner var anywhere → the global is used AND no pre-run
    `detect_runner` call is made (the cheap guard keeps the call order today's)."""
    for k in list(os.environ):
        if k.startswith("BUDDHI_TEST_GATE_TIMEOUT_SECS_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("BUDDHI_TEST_GATE_TIMEOUT_SECS", raising=False)
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "pytest -q")
    assert commit_push._per_runner_timeout_configured() is False

    detects = []
    real_detect = test_runner.detect_runner

    def spy(cwd, cmd):
        detects.append(cmd)
        return real_detect(cwd, cmd)

    monkeypatch.setattr(test_runner, "detect_runner", spy)
    seen = {}

    def run(argv, **kw):
        seen["timeout"] = kw.get("timeout")
        return subprocess.CompletedProcess(argv, 0, "1 passed\n", "")

    status, _ = commit_push.run_test_gate(".", run=run, notice=lambda *a, **k: "")
    assert status == "green"
    assert seen["timeout"] == commit_push._TEST_TIMEOUT_DEFAULT
    assert len(detects) == 1          # POST-run detection only, exactly as before


def test_gate_applies_the_per_runner_timeout(monkeypatch):
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "pytest -q")
    monkeypatch.setenv("BUDDHI_TEST_GATE_TIMEOUT_SECS_PYTEST", "42")
    seen = {}

    def run(argv, **kw):
        seen["timeout"] = kw.get("timeout")
        return subprocess.CompletedProcess(argv, 0, "1 passed\n", "")

    status, _ = commit_push.run_test_gate(".", run=run, notice=lambda *a, **k: "")
    assert status == "green"
    assert seen["timeout"] == 42


def test_a_detect_runner_explosion_never_breaks_the_gate(monkeypatch):
    monkeypatch.delenv("BUDDHI_TEST_GATE_TIMEOUT_SECS", raising=False)
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "pytest -q")
    monkeypatch.setenv("BUDDHI_TEST_GATE_TIMEOUT_SECS_PYTEST", "42")
    calls = {"n": 0}
    real_detect = test_runner.detect_runner

    def boom(cwd, cmd):
        calls["n"] += 1
        if calls["n"] == 1:                       # only the PRE-run resolution
            raise RuntimeError("detection bug")
        return real_detect(cwd, cmd)

    monkeypatch.setattr(test_runner, "detect_runner", boom)
    seen = {}

    def run(argv, **kw):
        seen["timeout"] = kw.get("timeout")
        return subprocess.CompletedProcess(argv, 0, "1 passed\n", "")

    status, _ = commit_push.run_test_gate(".", run=run, notice=lambda *a, **k: "")
    assert status == "green"
    assert seen["timeout"] == commit_push._TEST_TIMEOUT_DEFAULT   # fell back

