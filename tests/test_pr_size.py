"""The whole-PR size measurement: the projected retained tree is exactly what the
round's staging would commit (and the live index is never touched), the base is
resolved by repository identity, and any doubt is UNAVAILABLE — never a number.
Real git in a temp repo; ``gh`` and ``git fetch`` are faked, nothing leaves it."""
import json
import os
import shutil
import subprocess

import pytest

from buddhi_review import commit_push, pr_size


def _git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env=env).stdout


def _write(root, rel, text="x = 1\n"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _write(root, "src/app.py", "".join(f"line {i}\n" for i in range(10)))
    _write(root, "src/util.py", "def util():\n    return 1\n")
    _write(root, "build/existing.py", "SOURCE = True\n")
    _write(root, "node_modules/vendored.js", "module.exports = 1;\n")
    _write(root, "tools/build", "#!/bin/sh\necho build\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _index_bytes(root):
    path = _git(root, "rev-parse", "--git-path", "index").strip()
    with open(os.path.join(root, path), "rb") as f:
        return f.read()


def _tree_paths(root, tree):
    return set(_git(root, "ls-tree", "-r", "--name-only", tree).split())


def _noop(*a, **k):
    return ""


# Each scenario edits the worktree the way a fixer (or a runner) leaves it; the
# expected sets pin what the original staging guard commits and withholds.
def _ordinary(r):
    _write(r, "src/app.py", "changed\n")


def _residue(r):
    _write(r, "notes/unrelated.md", "eligible residue\n")


def _prestaged_dropping(r):
    _write(r, "src/app.py.bak", "backup\n")
    _git(r, "add", "src/app.py.bak")


def _prestaged_runner(r):
    _write(r, "node_modules/left-pad/index.js", "x\n")
    _git(r, "add", "node_modules/left-pad/index.js")


def _staged_rename_into_dropping(r):
    _git(r, "mv", "src/util.py", "src/util.py.bak")


def _paired_backup(r):
    os.rename(r / "src/app.py", r / "src/app.py.bak")


def _paired_runner_move(r):
    (r / "build/out").mkdir(parents=True)
    os.rename(r / "src/util.py", r / "build/out/util.py")


def _tracked_runner_edit_and_delete(r):
    _write(r, "node_modules/vendored.js", "patched\n")
    _write(r, "src/app.py", "also\n")


def _tracked_runner_delete(r):
    os.remove(r / "node_modules/vendored.js")


def _source_child(r):
    _write(r, "build/new_rule.py", "NEW = 1\n")


def _index_cannot_invent_source(r):
    _write(r, "build/gen/artifact.js", "generated\n")
    _git(r, "add", "-N", "build/gen/artifact.js")


def _runner_replaces_tracked_file(r):
    os.remove(r / "tools/build")
    _write(r, "tools/build/out.o", "binary-ish\n")


def _prestaged_runner_content(r):
    # a tracked runner entry staged by the fixer and left alone since: the commit
    # carries the STAGED version (neither HEAD's nor a later worktree edit)
    _write(r, "node_modules/vendored.js", "staged version\n")
    _git(r, "add", "node_modules/vendored.js")


def _staged_runner_deletion(r):
    _git(r, "rm", "-q", "node_modules/vendored.js")


def _source_child_beside_backup(r):
    _write(r, "build/new.py", "real\n")
    _write(r, "build/new.py.bak", "stays out\n")


def _rename_away_and_copy_into_runner(r):
    # the deleted source pairs with its runner copy only in the normalized index
    _write(r, "node_modules/pkg/util.py", "def util():\n    return 1\n")
    _git(r, "mv", "src/util.py", "src/other.bak")


# An INDEPENDENT oracle: for each scenario, the plain git commands that give the
# index the round's commit must hold (the guard's documented decision, spelled
# out by hand), applied to a copy of the same repository.
ORACLE = {
    "ordinary_edit": [["add", "src/app.py"]],
    "eligible_residue": [["add", "notes/unrelated.md"]],
    "prestaged_dropping": [["reset", "-q", "--", "src/app.py.bak"]],
    "prestaged_runner": [["reset", "-q", "--", "node_modules/left-pad/index.js"]],
    "staged_rename_normalized": [["reset", "-q", "--", "src/util.py", "src/util.py.bak"]],
    "paired_backup_hold": [],
    "paired_runner_hold": [],
    "tracked_runner_recovery": [["add", "node_modules/vendored.js", "src/app.py"]],
    "tracked_runner_deletion": [["rm", "-q", "--cached", "node_modules/vendored.js"]],
    "source_child_recovery": [["add", "build/new_rule.py"]],
    "index_cannot_invent_source": [["rm", "-q", "--cached", "build/gen/artifact.js"]],
    "runner_replaced_tracked_file": [["rm", "-q", "--cached", "tools/build"]],
    "prestaged_runner_content": [],
    "staged_runner_deletion": [],
    "source_child_beside_backup": [["add", "build/new.py"]],
    "rename_away_and_runner_copy": [["reset", "-q", "--", "src/util.py", "src/other.bak"]],
}

SCENARIOS = {
    "ordinary_edit": (_ordinary, {"src/app.py"}, set()),
    "eligible_residue": (_residue, {"notes/unrelated.md"}, set()),
    "prestaged_dropping": (_prestaged_dropping, set(), {"src/app.py.bak"}),
    "prestaged_runner": (_prestaged_runner, set(), {"node_modules/left-pad/index.js"}),
    "staged_rename_normalized": (_staged_rename_into_dropping, {"src/util.py"}, {"src/util.py.bak"}),
    "paired_backup_hold": (_paired_backup, {"src/app.py"}, {"src/app.py.bak"}),
    "paired_runner_hold": (_paired_runner_move, {"src/util.py"}, {"build/out/util.py"}),
    "tracked_runner_recovery": (_tracked_runner_edit_and_delete, {"node_modules/vendored.js"}, set()),
    "tracked_runner_deletion": (_tracked_runner_delete, set(), {"node_modules/vendored.js"}),
    "source_child_recovery": (_source_child, {"build/new_rule.py"}, set()),
    "index_cannot_invent_source": (_index_cannot_invent_source, set(), {"build/gen/artifact.js"}),
    "runner_replaced_tracked_file": (_runner_replaces_tracked_file, set(), {"tools/build", "tools/build/out.o"}),
    "prestaged_runner_content": (_prestaged_runner_content, {"node_modules/vendored.js"}, set()),
    "staged_runner_deletion": (_staged_runner_deletion, set(), {"node_modules/vendored.js"}),
    "source_child_beside_backup": (_source_child_beside_backup, {"build/new.py"}, {"build/new.py.bak"}),
    "rename_away_and_runner_copy": (_rename_away_and_copy_into_runner, {"src/util.py"},
                                    {"src/other.bak", "node_modules/pkg/util.py"}),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_retained_tree_measurement(repo, name, tmp_path):
    mutate, kept, withheld = SCENARIOS[name]
    head_tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    mutate(repo)
    oracle = tmp_path / "oracle"
    shutil.copytree(repo, oracle, symlinks=True)
    for op in ORACLE[name]:
        _git(oracle, *op)
    expected_tree = _git(oracle, "write-tree").strip()
    before_index = _index_bytes(repo)
    before_status = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    projected = pr_size.project_retained_tree(str(repo))
    # the live index and the visible status are exactly as the fixer left them
    assert _index_bytes(repo) == before_index
    assert _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all") == before_status
    assert projected["available"], projected
    # …and the projection is the tree the real staging then builds
    assert commit_push._stage_all(str(repo), notice=_noop).returncode == 0
    staged_tree = _git(repo, "write-tree").strip()
    assert projected["tree"] == staged_tree == expected_tree
    paths = _tree_paths(repo, staged_tree)
    assert kept <= paths and not (withheld & paths), (sorted(paths), kept, withheld)
    if name != "eligible_residue" and not kept and not withheld:
        assert staged_tree == head_tree
    if name == "prestaged_runner_content":
        assert _git(repo, "show", f"{staged_tree}:node_modules/vendored.js") == "staged version\n"


@pytest.mark.parametrize("failure", ["exit", "partial", "missing_rename_origin", "timeout",
                                     "late_exit", "late_partial"])
def test_incomplete_status_is_unavailable_with_a_plain_git_runner(repo, failure):
    """A runner that sees each git command exactly as issued (``git status …``) and
    forwards every keyword to ``subprocess.run``: the scratch index travels in the
    environment, so a status failure at either view is still the one it sees."""
    (_staged_rename_into_dropping if failure.startswith("late_") else _ordinary)(repo)
    before = _index_bytes(repo)
    reads = []

    def run(argv, *, cwd=None, **kwargs):
        if argv[:2] == ["git", "status"]:
            reads.append(kwargs.get("env", {}).get("GIT_INDEX_FILE"))
            if not (failure.startswith("late_") and len(reads) == 1):
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(argv, 1)
                out = {"exit": " M src.py\0", "late_exit": " M src.py\0", "partial": " M a\0broken\0",
                       "late_partial": " M a\0broken\0", "missing_rename_origin": "R  renamed.py\0"}[failure]
                return subprocess.CompletedProcess(argv, 1 if failure.endswith("exit") else 0, out, "x")
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=15, **kwargs)
    result = pr_size.project_retained_tree(str(repo), run=run)
    assert result["available"] is False and result["tree"] is None and result["reason"]
    assert _index_bytes(repo) == before
    # every view read a SCRATCH index, never the live one
    assert reads and all(r and os.path.basename(r).startswith("buddhi-index-") for r in reads)
    if failure.startswith("late_"):
        assert len(reads) >= 2 and reads[0] != reads[1]


def test_retained_tree_with_no_change_is_head(repo):
    projected = pr_size.project_retained_tree(str(repo))
    assert projected["tree"] == _git(repo, "rev-parse", "HEAD^{tree}").strip()


def _failing(match, how="rc"):
    real = commit_push._default_run

    def run(argv, **kw):
        if match(list(argv)):
            if how == "timeout":
                raise subprocess.TimeoutExpired(argv, 1)
            if how == "malformed":
                return subprocess.CompletedProcess(argv, 0, "R  only.bak\0", "")
            return subprocess.CompletedProcess(argv, 128, "", "boom")
        return real(argv, **kw)
    return run


def _is_status(argv):
    return "status" in argv and "--porcelain" in argv


@pytest.mark.parametrize("case", ["initial_status_fails", "initial_status_timeout",
                                  "initial_status_malformed", "normalized_status_fails",
                                  "normalized_status_timeout", "write_tree_fails"])
def test_incomplete_status_is_unavailable(repo, case):
    def runner():
        if case.startswith("normalized"):
            calls = {"n": 0}

            def second_status(argv):
                if _is_status(argv):
                    calls["n"] += 1
                    return calls["n"] == 2   # the view AFTER the rename un-staging
                return False
            return _failing(second_status, "timeout" if case.endswith("timeout") else "rc")
        if case == "write_tree_fails":
            return _failing(lambda a: "write-tree" in a)
        how = {"initial_status_fails": "rc", "initial_status_timeout": "timeout",
               "initial_status_malformed": "malformed"}[case]
        return _failing(_is_status, how)

    (_staged_rename_into_dropping if case.startswith("normalized") else _ordinary)(repo)
    before = _index_bytes(repo)
    projected = pr_size.project_retained_tree(str(repo), run=runner())
    assert projected == {"available": False, "tree": None, "reason": projected["reason"]}
    assert projected["reason"]
    assert _index_bytes(repo) == before
    size = pr_size.measure_pr_size(str(repo), run=runner(),
                                   base_tip=lambda cwd: ("main", _git(repo, "rev-parse", "HEAD").strip()))
    assert not size.available and size.additions is None and size.deletions is None
    assert size.total is None and "not zero" in size.render()


@pytest.mark.parametrize("mutate,read", [
    (_source_child, "ls-tree"),            # which build/ dirs hold committed source
    (_paired_runner_move, "ls-files"),     # the moved file's index blob
    (_paired_runner_move, "hash-object"),  # the candidate's content identity
    (_paired_runner_move, "cat-file"),     # the moved file's size
    (_paired_runner_move, "rev-parse"),    # the repository top
])
def test_a_failed_plan_read_makes_the_size_unavailable(repo, mutate, read):
    # Staging falls back to its safe answer on these failures; a count may not.
    mutate(repo)
    real = commit_push._default_run

    def run(argv, **kw):
        if read in argv and not (read == "rev-parse" and "--show-toplevel" not in argv):
            return subprocess.CompletedProcess(argv, 128, "", "boom")
        return real(argv, **kw)
    projected = pr_size.project_retained_tree(str(repo), run=run)
    assert projected["available"] is False and projected["reason"]
    size = pr_size.measure_pr_size(str(repo), run=run,
                                   base_tip=lambda cwd: ("main", _git(repo, "rev-parse", "HEAD").strip()))
    assert not size.available and size.total is None


def test_projection_of_a_repo_with_no_index_yet(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, "first.py", "x\n")
    projected = pr_size.project_retained_tree(str(root))
    assert projected["available"] and _tree_paths(root, projected["tree"]) == {"first.py"}
    assert not os.path.exists(root / ".git" / "index")      # the live repo still has none


def test_a_failed_scratch_normalization_falls_back_to_the_original_staging(repo, tmp_path):
    # The rename un-staging cannot be evaluated in the scratch index: staging
    # normalizes the live index and re-plans, exactly as it always did.
    _staged_rename_into_dropping(repo)
    oracle = tmp_path / "oracle"
    shutil.copytree(repo, oracle, symlinks=True)
    for op in ORACLE["staged_rename_normalized"]:
        _git(oracle, *op)
    real = commit_push._default_run

    def run(argv, **kw):
        if "reset" in argv and "GIT_INDEX_FILE" in (kw.get("env") or {}):
            return subprocess.CompletedProcess(argv, 1, "", "scratch reset refused")
        return real(argv, **kw)
    plan = commit_push.stage_plan(str(repo), run=run)
    assert not plan.normalized and not plan.complete
    assert commit_push._stage_all(str(repo), run=run, notice=_noop).returncode == 0
    assert _git(repo, "write-tree").strip() == _git(oracle, "write-tree").strip()
    assert pr_size.project_retained_tree(str(repo), run=run)["available"] is False


def test_a_failed_projection_step_is_unavailable(repo):
    _ordinary(repo)
    real = commit_push._default_run

    def run(argv, **kw):
        if argv[:3] == ["git", "add", "-A"]:
            return subprocess.CompletedProcess(argv, 1, "", "add failed")
        return real(argv, **kw)
    assert pr_size.project_retained_tree(str(repo), run=run) == {
        "available": False, "tree": None, "reason": "the staging projection failed"}


@pytest.mark.parametrize("output", ["garbage line\n", "1\t2\n", "x\ty\tpath\n"])
def test_malformed_numstat_is_unavailable(output):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, output, "")
    with pytest.raises(pr_size.Unavailable):
        pr_size.numstat_totals("a" * 40, "b" * 40, cwd=".", run=run)
    assert pr_size.numstat_totals("a" * 40, "b" * 40, cwd=".", run=lambda a, **k: subprocess.CompletedProcess(
        a, 0, "3\t1\tx.py\n0\t0\ty.py\n", "")) == (3, 1)


def test_scratch_index_keeps_the_live_index_timestamp(repo):
    _ordinary(repo)
    path = commit_push.scratch_index(str(repo))
    try:
        live = os.stat(repo / _git(repo, "rev-parse", "--git-path", "index").strip())
        assert os.stat(path).st_mtime_ns == live.st_mtime_ns
    finally:
        os.unlink(path)


def test_scratch_index_removes_its_file_when_the_copy_fails(repo, tmp_path, monkeypatch):
    _ordinary(repo)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(commit_push.tempfile, "tempdir", str(scratch))

    def failing_utime(*a, **k):
        raise OSError("utime failed")
    monkeypatch.setattr(commit_push.os, "utime", failing_utime)
    assert commit_push.scratch_index(str(repo)) is None
    assert list(scratch.glob("buddhi-index-*.idx")) == []


def test_scratch_index_is_none_when_mkstemp_fails(repo, monkeypatch):
    def failing_mkstemp(*a, **k):
        raise OSError("no temp dir")
    monkeypatch.setattr(commit_push.tempfile, "mkstemp", failing_mkstemp)
    assert commit_push.scratch_index(str(repo)) is None


def test_a_runner_that_drops_the_environment_never_reaches_the_live_index(repo):
    _staged_rename_into_dropping(repo)
    before = _index_bytes(repo)
    real = commit_push._default_run
    calls = []

    def strict(argv, *, cwd=None, timeout=None):           # takes no env at all
        calls.append(list(argv))
        return real(argv, cwd=cwd)

    def dropping(argv, **kw):                               # accepts env, ignores it
        return real(argv, cwd=kw.get("cwd"))
    assert pr_size.project_retained_tree(str(repo), run=strict)["available"] is False
    assert pr_size.project_retained_tree(str(repo), run=dropping)["available"] is False
    assert _index_bytes(repo) == before
    # and staging itself still stages exactly as before with the strict runner
    assert commit_push._stage_all(str(repo), run=strict, notice=_noop).returncode == 0
    assert "src/util.py.bak" not in _tree_paths(repo, _git(repo, "write-tree").strip())


def test_staging_itself_keeps_its_fallback_on_a_failed_status(repo):
    # Staging is unchanged by the completeness evidence: a failed read still stages
    # with the fixed runner globs (fail closed), it just cannot be counted.
    _ordinary(repo)
    plan = commit_push.stage_plan(str(repo), run=_failing(_is_status))
    assert not plan.complete and plan.entries == [] and plan.reason


# ── the whole-PR count ──────────────────────────────────────────────────────

@pytest.fixture
def pr_repo(tmp_path):
    """main has a base commit; the PR branch adds committed lines on top."""
    root = tmp_path / "pr"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _write(root, "a.py", "".join(f"a{i}\n" for i in range(20)))
    _write(root, "logo.bin", "x")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    return root


def _commit_lines(root, rel, n, tag):
    p = root / rel
    p.write_text((p.read_text() if p.exists() else "") + "".join(f"{tag}{i}\n" for i in range(n)))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", f"+{n}")


def _main(root):
    return lambda cwd: ("main", _git(root, "rev-parse", "main").strip())


@pytest.mark.parametrize("committed,candidate,total", [(6, 2, 8), (9, 2, 11)])
def test_whole_pr_size_counts_committed_plus_candidate(pr_repo, committed, candidate, total):
    _commit_lines(pr_repo, "b.py", committed, "c")
    _write(pr_repo, "b.py", (pr_repo / "b.py").read_text() + "".join(f"n{i}\n" for i in range(candidate)))
    size = pr_size.measure_pr_size(str(pr_repo), base_tip=_main(pr_repo))
    assert size.available and (size.additions, size.deletions, size.total) == (total, 0, total)
    assert size.base_branch == "main"
    assert size.comparison_base == _git(pr_repo, "rev-parse", "main").strip()
    assert size.render() == (f"+{total} -0 = {total} changed lines against base branch `main` "
                             f"(comparison base {size.comparison_base[:7]})")
    assert f"total changed lines: {total}" in size.prompt_text()


def test_whole_pr_size_renames_deletions_new_files_and_residue(pr_repo):
    _git(pr_repo, "mv", "a.py", "renamed.py")
    _git(pr_repo, "commit", "-qm", "pure rename")
    assert pr_size.measure_pr_size(str(pr_repo), base_tip=_main(pr_repo)).total == 0
    # an edited rename counts its textual change; a new file and unrelated residue
    # that staging would keep count too; an editor dropping does not
    p = pr_repo / "renamed.py"
    p.write_text(p.read_text().replace("a3\n", "A3\n"))
    _write(pr_repo, "new.py", "one\ntwo\n")
    _write(pr_repo, "residue.md", "r\n")
    _write(pr_repo, "new.py.bak", "never\ncounted\nhere\n")
    size = pr_size.measure_pr_size(str(pr_repo), base_tip=_main(pr_repo))
    assert (size.additions, size.deletions) == (1 + 2 + 1, 1)
    # a cancelled change counts nothing
    p.write_text(p.read_text().replace("A3\n", "a3\n"))
    for f in ("new.py", "residue.md", "new.py.bak"):
        os.remove(pr_repo / f)
    assert pr_size.measure_pr_size(str(pr_repo), base_tip=_main(pr_repo)).total == 0


def test_whole_pr_size_unavailable_cases(pr_repo, tmp_path):
    # binary content has no line count: the WHOLE total is unavailable
    (pr_repo / "logo.bin").write_bytes(b"\x00\x01\x02")
    s = pr_size.measure_pr_size(str(pr_repo), base_tip=_main(pr_repo))
    assert not s.available and "binary" in s.reason and s.total is None
    (pr_repo / "logo.bin").write_text("x")
    # no PR and no base resolver
    assert not pr_size.measure_pr_size(str(pr_repo)).available
    # a base the branch shares no history with
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "config", "user.email", "t@example.com")
    _git(other, "config", "user.name", "t")
    _write(other, "z", "z\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "unrelated")
    _git(pr_repo, "fetch", "-q", str(other), "HEAD:refs/heads/unrelated")
    s = pr_size.measure_pr_size(str(pr_repo), base_tip=lambda c: ("unrelated", _git(pr_repo, "rev-parse", "unrelated").strip()))
    assert not s.available and "merge-base" in s.reason
    # a base resolver that raises is unavailable, never zero
    def boom(cwd):
        raise RuntimeError("x")
    s = pr_size.measure_pr_size(str(pr_repo), base_tip=boom)
    assert not s.available and s.render().startswith("UNAVAILABLE (")


def test_whole_pr_size_ambiguous_merge_base_is_unavailable(pr_repo):
    # criss-cross: two merge-bases between feature and main
    _commit_lines(pr_repo, "f.py", 1, "f")
    _git(pr_repo, "checkout", "-q", "main")
    _commit_lines(pr_repo, "m.py", 1, "m")
    m1 = _git(pr_repo, "rev-parse", "HEAD").strip()
    _git(pr_repo, "checkout", "-q", "feature")
    f1 = _git(pr_repo, "rev-parse", "HEAD").strip()
    _git(pr_repo, "merge", "-q", "--no-edit", m1)
    _git(pr_repo, "checkout", "-q", "main")
    _git(pr_repo, "merge", "-q", "--no-edit", f1)
    _git(pr_repo, "checkout", "-q", "feature")
    s = pr_size.measure_pr_size(str(pr_repo), base_tip=_main(pr_repo))
    assert not s.available and "ambiguous" in s.reason


def _gh_and_fetch(root, view, *, fetch_ok=False):
    real = commit_push._default_run
    seen = []

    def run(argv, **kw):
        argv = list(argv)
        seen.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(view), "")
        if "fetch" in argv:
            return subprocess.CompletedProcess(argv, 0 if fetch_ok else 1, "", "offline")
        return real(argv, **kw)
    return run, seen


def test_base_resolved_by_repository_identity(pr_repo):
    main = _git(pr_repo, "rev-parse", "main").strip()
    _git(pr_repo, "remote", "add", "upstream", "https://github.com/o/r.git")
    _git(pr_repo, "remote", "add", "mirror", "https://github.com/someone/fork.git")
    _git(pr_repo, "update-ref", "refs/remotes/upstream/main", main)
    _commit_lines(pr_repo, "b.py", 3, "c")
    # the local main and a mirror's main have moved on (they now hold the PR's
    # lines): either one as the base would count 0, the identity-matched remote 3
    feature = _git(pr_repo, "rev-parse", "HEAD").strip()
    _git(pr_repo, "update-ref", "refs/heads/main", feature)
    _git(pr_repo, "update-ref", "refs/remotes/mirror/main", feature)
    view = {"baseRefName": "main", "baseRefOid": main, "url": "https://github.com/o/r/pull/7"}
    run, seen = _gh_and_fetch(pr_repo, view)
    size = pr_size.measure_pr_size(str(pr_repo), pr="7", repo="o/r", run=run)
    assert size.available and size.total == 3 and size.comparison_base == main
    assert any("upstream" in a and "fetch" in a for a in seen)
    # unfetchable and stale: the tracking ref no longer equals GitHub's base commit
    stale = dict(view, baseRefOid="f" * 40)
    run, _ = _gh_and_fetch(pr_repo, stale)
    s = pr_size.measure_pr_size(str(pr_repo), pr="7", repo="o/r", run=run)
    assert not s.available and "not the PR's current base" in s.reason
    # no remote names the PR's repository
    run, _ = _gh_and_fetch(pr_repo, dict(view, url="https://github.com/x/y/pull/7"))
    s = pr_size.measure_pr_size(str(pr_repo), pr="7", repo=None, run=run)
    assert not s.available and "x/y" in s.reason
    # an unusable base branch name, even one a tracking ref exists for
    _git(pr_repo, "update-ref", "refs/remotes/upstream/-x", main)
    run, _ = _gh_and_fetch(pr_repo, dict(view, baseRefName="-x"))
    s = pr_size.measure_pr_size(str(pr_repo), pr="7", repo="o/r", run=run)
    assert not s.available and "base branch" in s.reason
