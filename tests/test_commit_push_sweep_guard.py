"""The fix-commit sweep guard: a fixer's editor/backup droppings (`foo.bak`,
`main.py~`, `.#lock`, `*.swp`/`*.swo`, `.DS_Store`) must NEVER ride the per-round
`git add -A` into the PR, while every legitimate change still stages. Regression
cover for the reference-tree incident where two `.bak` files reached a repo's
`main`.
"""
import subprocess

import pytest

from buddhi_review import commit_push


# One filename per guarded pattern — root level and one nested a directory down —
# plus decoys that MUST survive (a real source file whose name merely contains a
# dropping token). Kept as data so every test exercises the full pattern set.
_ROOT_DROPPINGS = ["foo.bak", "main.py~", "patch.orig", ".#lock",
                   ".main.py.swp", ".main.py.swo", ".DS_Store"]
_NESTED_DROPPINGS = ["pkg/mod.bak", "pkg/mod.py~", "pkg/mod.orig", "pkg/.#lock2",
                     "pkg/.mod.py.swp", "pkg/.mod.py.swo", "pkg/.DS_Store"]
_DECOYS = ["bakery.py", "swap.py", "original.py", "notes.orig.md"]  # NOT droppings


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _write(root, rel, text="x = 1\n"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _rec_notice(store):
    def notice(action, detail="", *, status="do", hint=None):
        store.append((action, detail, status))
        return ""
    return notice


@pytest.fixture
def git_repo(tmp_path):
    """A minimal git worktree with one committed file (the unit-test substrate)."""
    _write(tmp_path, "seed.py")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


@pytest.fixture
def repo(tmp_path):
    """A clone with an upstream so `commit_and_push`'s push works for real
    (mirrors the fixture in test_commit_push.py)."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (work / "f.py").write_text("x = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    _git(work, "push", "-q", "-u", "origin", "HEAD")
    return work


# ── _is_dropping: basename match, never a substring ────────────────────────────

@pytest.mark.parametrize("path", _ROOT_DROPPINGS + _NESTED_DROPPINGS)
def test_is_dropping_matches_every_pattern_at_any_depth(path):
    assert commit_push._is_dropping(path) is True


@pytest.mark.parametrize("path", _DECOYS + ["pkg/bakery.py", "a/b/real.py"])
def test_is_dropping_never_flags_a_real_source_file(path):
    # MUTATION: a substring match (`".bak" in name`) or dropping `fnmatchcase`
    # for a case-folded glob would wrongly flag `bakery.py` / `swap.py` here.
    assert commit_push._is_dropping(path) is False


def test_is_dropping_handles_collapsed_untracked_dir_marker():
    # Porcelain renders a wholly-untracked dir as `dir/`; its basename is "" after
    # the split unless the trailing slash is stripped first. Must NOT crash / match.
    assert commit_push._is_dropping("pkg/") is False


# ── _new_to_head: only a path with NO HEAD blob is a "new" dropping ────────────

@pytest.mark.parametrize("xy", ["??", "A ", " A", "AM", "AD"])
def test_new_to_head_true_for_untracked_or_freshly_added(xy):
    assert commit_push._new_to_head(xy) is True


@pytest.mark.parametrize("xy", [" M", " D", "M ", "D ", "R ", "C ", "MM"])
def test_new_to_head_false_for_paths_with_head_history(xy):
    # A tracked path's modification/deletion already exists in HEAD — the sweep
    # guard must not treat it as a "new" dropping to hold out of the commit.
    assert commit_push._new_to_head(xy) is False


# ── _detect_droppings: enumerates droppings at any depth, incl. brand-new dirs ──

def test_detect_droppings_finds_all_patterns_including_new_dirs(git_repo):
    for rel in _ROOT_DROPPINGS + _NESTED_DROPPINGS + _DECOYS:
        _write(git_repo, rel)
    _write(git_repo, "real.py")
    found = set(commit_push._detect_droppings(str(git_repo)))
    # Every dropping, root and nested (nested ones live in a brand-new `pkg/` dir —
    # only `--untracked-files=all` un-collapses them).
    assert found == set(_ROOT_DROPPINGS + _NESTED_DROPPINGS)
    # MUTATION: drop `--untracked-files=all` and the nested set vanishes.
    assert all(d not in found for d in _DECOYS + ["real.py"])


def test_detect_droppings_fail_open_on_status_error():
    def run(argv, *, cwd=None, timeout=None):
        return subprocess.CompletedProcess(list(argv), 128, "", "fatal: boom")
    assert commit_push._detect_droppings("x", run=run) == []


def test_detect_droppings_fail_open_on_decode_error():
    def run(argv, *, cwd=None, timeout=None):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    assert commit_push._detect_droppings("x", run=run) == []


def test_detect_droppings_survives_renames_and_special_char_names(git_repo):
    # A staged rename must not be mistaken for a dropping, and a dropping whose name
    # carries a quote / space-arrow (inherited from an oddly-named source file, which
    # porcelain would otherwise C-quote) must be caught by its VERBATIM name — the
    # `-z` parse is what makes both true.
    (git_repo / "seed.py").rename(git_repo / "seed_renamed.py")
    _git(git_repo, "add", "-A")  # stages the rename
    weird = 'a" -> b.bak'        # quote + literal " -> "
    _write(git_repo, weird)
    _write(git_repo, "real.py")
    found = commit_push._detect_droppings(str(git_repo))
    assert weird in found
    assert "seed_renamed.py" not in found and "seed.py" not in found
    assert "real.py" not in found


def test_detect_droppings_does_not_misread_a_rename_origin_field():
    # Synthetic `-z` porcelain: a rename whose ORIGIN name would itself look like a
    # dropping if misparsed. The origin field must be consumed, not classified.
    payload = "R  new.py\0old.foo.bak\0?? junk.bak\0"

    def run(argv, *, cwd=None, timeout=None):
        assert "-z" in argv and "--untracked-files=all" in argv
        return subprocess.CompletedProcess(list(argv), 0, payload, "")

    # MUTATION: drop the rename field-skip and `old.foo.bak` is misparsed into a
    # phantom `d.foo.bak` dropping alongside the real one.
    assert commit_push._detect_droppings("x", run=run) == ["junk.bak"]


def test_detect_droppings_excludes_only_new_droppings_not_tracked_ones(git_repo):
    # A dropping that was already committed and is now deleted/modified in the
    # worktree has HEAD history — it must NOT be classified as an exclude-worthy
    # dropping, or its deletion would never reach the commit (it would look
    # forever "staged for removal" but excluded from every `git add -A`).
    _write(git_repo, "tracked.bak")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "tracked dropping")
    (git_repo / "tracked.bak").unlink()
    _write(git_repo, "fresh.bak")  # a brand-new dropping, untracked
    found = commit_push._detect_droppings(str(git_repo))
    assert "fresh.bak" in found
    assert "tracked.bak" not in found


# ── _backup_source / _risky_delete_pairs: paired delete+backup detection ───────

@pytest.mark.parametrize("path,expected", [
    ("src.py.bak", "src.py"),
    ("src.py~", "src.py"),
    ("src.py.orig", "src.py"),
    (".DS_Store", None),   # no deterministic source-name relationship
    (".#lock", None),
    (".main.py.swp", None),
    (".main.py.swo", None),
])
def test_backup_source_pairs_only_deterministic_suffixes(path, expected):
    assert commit_push._backup_source(path) == expected


def test_risky_delete_pairs_pairs_a_deleted_source_with_its_fresh_backup():
    entries = [(" D", "src.py"), ("??", "src.py.bak")]
    assert commit_push._risky_delete_pairs(entries) == {"src.py"}


def test_risky_delete_pairs_ignores_an_unpaired_backup():
    entries = [("??", "orphan.bak")]
    assert commit_push._risky_delete_pairs(entries) == set()


def test_risky_delete_pairs_ignores_an_unpaired_delete():
    entries = [(" D", "gone.py")]
    assert commit_push._risky_delete_pairs(entries) == set()


def test_risky_delete_pairs_ignores_a_tracked_droppings_own_deletion():
    # A previously-committed dropping's own deletion (`_new_to_head` false on
    # the dropping itself) must not be mistaken for a "fresh backup" pairing.
    entries = [("D ", "old.bak")]
    assert commit_push._risky_delete_pairs(entries) == set()


def test_risky_delete_pairs_does_not_guess_non_deterministic_droppings():
    # `.DS_Store`/`.#*`/`.swp`/`.swo` carry no source-name relationship — even
    # with a same-round deletion present, no pairing is guessed.
    entries = [(" D", "notes"), ("??", ".DS_Store")]
    assert commit_push._risky_delete_pairs(entries) == set()


def test_stage_all_unstages_an_already_staged_dropping(git_repo):
    # A fixer that itself `git add`-ed a dropping: the exclude pathspec alone would
    # leave the staged copy in the index (git add never removes). The de-stage must
    # pull it back out so it never rides the commit — and the log stays truthful.
    _write(git_repo, "real.py")
    _write(git_repo, "sneaky.bak")
    _git(git_repo, "add", "sneaky.bak")  # already in the index before the guard runs
    notices = []
    out = commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert out.returncode == 0
    staged = set(subprocess.run(["git", "diff", "--cached", "--name-only"],
                                cwd=git_repo, capture_output=True, text=True)
                 .stdout.split())
    assert "real.py" in staged
    assert "sneaky.bak" not in staged  # de-staged, never reaches the commit
    assert any(a == "stage" and s == "skip" for a, _d, s in notices)  # truthful log


def test_stage_all_reset_uses_literal_pathspec(git_repo):
    # `git reset -- <path>` parses its argument as a PATHSPEC, not a raw path — a
    # dropping name carrying glob metacharacters (`[`/`]`/`*`/`?`) could otherwise
    # unstage the WRONG path. The reset must wrap each dropping in `:(top,literal)`,
    # mirroring the `:(top,exclude,literal)` form already used for the `git add`
    # exclude below it.
    _write(git_repo, "odd[1].bak")
    _git(git_repo, "add", "odd[1].bak")  # already staged, like the sibling test above
    calls = []
    real = commit_push._default_run

    def run(argv, *, cwd=None, timeout=commit_push._GIT_TIMEOUT):
        calls.append(list(argv))
        return real(argv, cwd=cwd, timeout=timeout)

    out = commit_push._stage_all(str(git_repo), run=run, notice=_rec_notice([]))
    assert out.returncode == 0
    reset_call = next(c for c in calls if c[:2] == ["git", "reset"])
    assert ":(top,literal)odd[1].bak" in reset_call
    staged = set(subprocess.run(["git", "diff", "--cached", "--name-only"],
                                cwd=git_repo, capture_output=True, text=True)
                 .stdout.split())
    assert "odd[1].bak" not in staged  # still correctly de-staged


# ── _stage_all: excludes droppings, keeps legit adds, logs once ────────────────

def test_stage_all_no_droppings_is_a_plain_add(git_repo):
    _write(git_repo, "real.py")
    notices = []
    calls = []
    real = commit_push._default_run

    def run(argv, *, cwd=None, timeout=commit_push._GIT_TIMEOUT):
        calls.append(list(argv))
        return real(argv, cwd=cwd, timeout=timeout)

    out = commit_push._stage_all(str(git_repo), run=run, notice=_rec_notice(notices))
    assert out.returncode == 0
    # No droppings → the byte-identical plain `git add -A`, no exclude pathspec.
    assert ["git", "add", "-A"] in calls
    assert not any("(exclude" in " ".join(c) for c in calls)
    assert notices == []  # nothing excluded → no log line
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                            cwd=git_repo, capture_output=True, text=True).stdout
    assert "real.py" in staged


def test_stage_all_excludes_droppings_keeps_real_files_and_logs(git_repo):
    for rel in _ROOT_DROPPINGS + _NESTED_DROPPINGS + _DECOYS:
        _write(git_repo, rel)
    _write(git_repo, "pkg/impl.py")  # a real file in the SAME brand-new dir
    notices = []
    out = commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert out.returncode == 0
    staged = set(subprocess.run(["git", "diff", "--cached", "--name-only"],
                                cwd=git_repo, capture_output=True, text=True)
                 .stdout.split())
    # Real files staged (including the one nested beside a dropping); no dropping.
    assert "pkg/impl.py" in staged
    assert set(_DECOYS).issubset(staged)
    assert staged.isdisjoint(_ROOT_DROPPINGS + _NESTED_DROPPINGS)
    # Exactly one dim `[auto]` line, naming the count.
    stage_lines = [n for n in notices if n[0] == "stage"]
    assert len(stage_lines) == 1
    action, detail, status = stage_lines[0]
    assert status == "skip"
    assert str(len(_ROOT_DROPPINGS + _NESTED_DROPPINGS)) in detail


def test_stage_all_holds_back_a_deleted_source_paired_with_its_backup(git_repo):
    # The reported scenario: a fixer/editor moves `src.py` to `src.py.bak` and
    # fails before recreating `src.py`. Excluding only the backup would still
    # stage (and commit) the real deletion — both must be held out of staging.
    _write(git_repo, "src.py", "original\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "seed src.py")
    (git_repo / "src.py").rename(git_repo / "src.py.bak")
    _write(git_repo, "real.py")  # a genuine, unrelated edit this round
    notices = []
    out = commit_push._stage_all(str(git_repo), notice=_rec_notice(notices))
    assert out.returncode == 0
    staged = set(subprocess.run(["git", "diff", "--cached", "--name-only"],
                                cwd=git_repo, capture_output=True, text=True)
                 .stdout.split())
    assert "real.py" in staged           # the unrelated edit still lands
    assert "src.py" not in staged        # the deletion is held back, not staged
    assert "src.py.bak" not in staged
    status = subprocess.run(["git", "status", "--porcelain"], cwd=git_repo,
                            capture_output=True, text=True).stdout
    assert "src.py" in status and "src.py.bak" in status  # both still pending
    stop_lines = [n for n in notices if n[0] == "stage" and n[2] == "stop"]
    assert len(stop_lines) == 1
    assert "src.py" in stop_lines[0][1]


def test_stage_all_fail_open_when_status_probe_errors():
    calls = []

    def run(argv, *, cwd=None, timeout=None):
        argv = list(argv)
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 128, "", "boom")
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    notices = []
    out = commit_push._stage_all("x", run=run, notice=_rec_notice(notices))
    assert out.returncode == 0
    assert calls == [["git", "add", "-A"]]  # fell back to the plain add
    assert notices == []


# ── End-to-end through commit_and_push: droppings never reach the commit ───────

def _committed_files(repo):
    return set(subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"],
                              cwd=repo, capture_output=True, text=True).stdout.split())


def test_droppings_never_ride_the_fix_commit(monkeypatch, repo):
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -c pass")  # green gate
    for rel in _ROOT_DROPPINGS + _NESTED_DROPPINGS + _DECOYS:
        _write(repo, rel)
    _write(repo, "pkg/impl.py")
    out = commit_push.commit_and_push(str(repo), message="fix: round 1",
                                      notice=lambda *a, **k: "")
    assert out == "pushed"
    tracked = _committed_files(repo)
    # The real edits landed; the decoys landed; not a single dropping did.
    assert "pkg/impl.py" in tracked
    assert set(_DECOYS).issubset(tracked)          # bakery.py & friends survive
    assert tracked.isdisjoint(_ROOT_DROPPINGS + _NESTED_DROPPINGS)


def test_intent_to_add_dropping_never_rides_the_fix_commit(monkeypatch, repo):
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -c pass")
    _write(repo, "real.py")
    _write(repo, "sneaky.bak")
    _git(repo, "add", "-N", "sneaky.bak")
    out = commit_push.commit_and_push(str(repo), message="fix: round 1",
                                      notice=lambda *a, **k: "")
    assert out == "pushed"
    tracked = _committed_files(repo)
    assert "real.py" in tracked
    assert "sneaky.bak" not in tracked


def test_deleted_tracked_dropping_reaches_the_commit(monkeypatch, repo):
    # A fixer's deletion of a PREVIOUSLY COMMITTED dropping is a real change Git
    # is supposed to record — the sweep guard must not silently keep it alive by
    # excluding it from every subsequent `git add -A`.
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -c pass")
    _write(repo, "tracked.orig")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "tracked dropping")
    _git(repo, "push", "-q")
    (repo / "tracked.orig").unlink()
    out = commit_push.commit_and_push(str(repo), message="fix: drop the .orig",
                                      notice=lambda *a, **k: "")
    assert out == "pushed"
    assert "tracked.orig" not in _committed_files(repo)  # the deletion landed


def test_delete_backup_pair_never_lands_the_deletion_end_to_end(monkeypatch, repo):
    # End-to-end through `commit_and_push`: an unrelated real edit still ships,
    # but the paired delete+backup never lands a lossy deletion on the PR.
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -c pass")  # green gate
    _write(repo, "src.py", "original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed src.py")
    _git(repo, "push", "-q")
    (repo / "src.py").rename(repo / "src.py.bak")
    _write(repo, "unrelated.py")
    notices = []
    out = commit_push.commit_and_push(str(repo), message="fix: round 1",
                                      notice=_rec_notice(notices))
    assert out == "pushed"
    tracked = _committed_files(repo)
    assert "unrelated.py" in tracked  # the unrelated edit still ships
    assert "src.py" in tracked        # the deletion did NOT land
    assert "src.py.bak" not in tracked
    assert any(a == "stage" and s == "stop" for a, _d, s in notices)


def test_exclusion_log_fires_through_commit_and_push(monkeypatch, repo):
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -c pass")
    _write(repo, "real.py")
    _write(repo, "junk.bak")
    notices = []
    out = commit_push.commit_and_push(str(repo), message="m",
                                      notice=_rec_notice(notices))
    assert out == "pushed"
    assert any(a == "stage" and s == "skip" for a, _d, s in notices)


def test_no_droppings_no_stage_log(monkeypatch, repo):
    monkeypatch.setenv("BUDDHI_TEST_COMMAND", "python3 -c pass")
    _write(repo, "real.py")
    notices = []
    out = commit_push.commit_and_push(str(repo), message="m",
                                      notice=_rec_notice(notices))
    assert out == "pushed"
    assert not any(a == "stage" for a, _d, _s in notices)  # log only when excluded


# ── The residue tripwire must not mistake excluded droppings for lost edits ─────

def test_residue_tripwire_ignores_droppings_but_still_flags_real_residue(git_repo):
    # A dropping beside a tracked file is how it shows up in the real post-commit
    # tree (porcelain lists it individually, not under a collapsed new dir).
    _write(git_repo, "pkg/keep.py")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "pkg")
    # Only-dropping residue → the "edits are not on the PR" alarm stays silent.
    _write(git_repo, "leftover.bak")   # root dropping
    _write(git_repo, "pkg/x.py~")      # nested dropping (pkg/ is now tracked)
    notices = []
    commit_push._assert_clean_after_commit(str(git_repo), notice=_rec_notice(notices))
    assert not any(a == "fix-residue tripwire" for a, _d, _s in notices)
    # A genuinely-lost NON-dropping edit still trips it.
    _write(git_repo, "lost_real_edit.py")
    notices2 = []
    commit_push._assert_clean_after_commit(str(git_repo), notice=_rec_notice(notices2))
    assert any(a == "fix-residue tripwire" and s == "fallback"
               for a, _d, s in notices2)


def test_residue_tripwire_ignores_a_dropping_alone_in_a_brand_new_dir(git_repo):
    # The collapsed-dir trap: a dropping that is the SOLE file in a wholly-untracked
    # new dir shows as `newdir/` under default porcelain (basename evades the
    # filter). `--untracked-files=all` un-collapses it so the alarm stays silent.
    _write(git_repo, "brand_new/only.swp")
    notices = []
    commit_push._assert_clean_after_commit(str(git_repo), notice=_rec_notice(notices))
    assert not any(a == "fix-residue tripwire" for a, _d, _s in notices)
    # But a real lost edit alone in a new dir STILL trips it.
    _write(git_repo, "another_new/lost.py")
    notices2 = []
    commit_push._assert_clean_after_commit(str(git_repo), notice=_rec_notice(notices2))
    assert any(a == "fix-residue tripwire" and s == "fallback"
               for a, _d, s in notices2)


def test_residue_tripwire_recognizes_a_dropping_with_special_characters(git_repo):
    # Without `-z`, porcelain C-quotes/escapes a path holding a quote or a literal
    # " -> " (and renders a rename as a single `old -> new` line) — either would
    # make `_is_dropping` fail to recognize the dropping and wrongly fire the
    # alarm. `-z` yields it VERBATIM, matching `_detect_droppings`'s parsing, so
    # the alarm correctly stays silent.
    weird = 'a" -> b.bak'
    _write(git_repo, weird)
    notices = []
    commit_push._assert_clean_after_commit(str(git_repo), notice=_rec_notice(notices))
    assert not any(a == "fix-residue tripwire" for a, _d, _s in notices)


def test_residue_tripwire_still_flags_a_lost_edit_with_special_characters(git_repo):
    weird = 'a" -> b.py'  # NOT a dropping — a genuinely lost real edit
    _write(git_repo, weird)
    notices = []
    commit_push._assert_clean_after_commit(str(git_repo), notice=_rec_notice(notices))
    assert any(a == "fix-residue tripwire" and s == "fallback" and weird in d
               for a, d, s in notices)
