"""The ``.claude`` settings guard, window by window.

Every repository here is a real git repository; every git call is real. A
``claude`` spawn is simulated by reading, inside a guard window, what a ``claude``
started there would load (``settings_guard_support.seen_settings``), or by the
recording ``ClaudeStub`` when a real spawn path is driven. No hook ever runs: a
test hook only ``touch``es a marker under the test's tmp dir, and every test that
could run one asserts the marker is absent.
"""
import io
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from buddhi_review import claude_settings_guard as guard
from buddhi_review import cli, fix_apply, model_call
from buddhi_review.actuators import act_on_result
from buddhi_review.classify import Classification
from buddhi_review.loop import Comment, CommentResult
from settings_guard_support import (
    LOCAL_SETTINGS,
    PACKAGE_ROOT,
    SETTINGS,
    ClaudeStub,
    command_hook,
    git,
    hostile,
    make_pr_repo,
    markers_present,
    seen_settings,
    touch,
    write,
)


def _spawn(cwd):
    """One guarded "spawn": what a claude started in ``cwd`` would load."""
    with guard.window(cwd):
        return seen_settings(os.path.realpath(cwd or os.getcwd()))


def _hooks(view):
    events = set()
    for obj in view.values():
        events.update((obj or {}).get("hooks", {}) or {})
    return sorted(events)


def _state_files():
    d = os.environ[guard.STATE_DIR_ENV]
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _saved_copies():
    """Originals the guard saved beside the journal (their only copy)."""
    return sorted(f for f in _state_files() if ".settings" in f)


def _saved_bytes():
    return [(Path(os.environ[guard.STATE_DIR_ENV]) / f).read_bytes() for f in _saved_copies()]


def _snapshot(root: Path):
    """Bytes, inode and mtime of every file under ``root`` — to prove nothing
    outside the checkout is ever written."""
    out = {}
    for p in sorted(root.rglob("*")):
        st = os.lstat(p)
        out[str(p)] = (p.read_bytes() if p.is_file() and not p.is_symlink() else None,
                       st.st_ino, st.st_mtime_ns)
    return out


# ── what a settings value names (C3) ──────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    'python3 "$CLAUDE_PROJECT_DIR"/tools/check.py',
    'python3 "${CLAUDE_PROJECT_DIR}"/tools/check.py',
    "python3 ${CLAUDE_PROJECT_DIR}/tools/check.py",
    "python3 $CLAUDE_PROJECT_DIR/tools/check.py",
    'python3 "$CLAUDE_PROJECT_DIR/tools/check.py"',
    "python3 ./tools/check.py",
    "python3 tools/check.py",
    "python3 $PWD/tools/check.py",
    'python3 "$(pwd)"/tools/check.py',
    "python3 ~+/tools/check.py",
    "bash -c 'python3 $CLAUDE_PROJECT_DIR/tools/check.py'",
    "sh -c \"python3 './tools/check.py' --flag\"",
])
def test_every_spelling_of_a_checkout_path_names_that_file(command):
    named = guard.named_paths(command)
    assert named.unsafe is None
    assert (named.paths | named.bare) >= {"tools/check.py"}
    # an interpreter imports from its script's directory, so that is named too
    assert named.trees | named.bare_trees == {"tools"}


@pytest.mark.parametrize("command,path", [
    ('sh $"./hooks/x.sh"', "hooks/x.sh"),                      # bash's locale-quoted string
    ("sh '.''/hooks/x.sh'", "hooks/x.sh"),                     # adjacent quoted parts join
    ('sh .""/hooks/x.sh', "hooks/x.sh"),
    ("sh ./hooks/x\\\n.sh", "hooks/x.sh"),                     # backslash-newline
    ("sh \\./hooks/x.sh", "hooks/x.sh"),
    ("eval \"sh ./hooks/x.sh\"", "hooks/x.sh"),
    ("echo ./x.sh | xargs sh", "x.sh"),
    ("sh <<EOF\n./x.sh\nEOF", "x.sh"),                         # a here-document
    ("sh <<< ./x.sh", "x.sh"),                                  # a here-string
    ("source <(cat ./x.sh)", "x.sh"),                           # process substitution
    ("A=./hooks/x.sh; sh $A", "hooks/x.sh"),                    # indirection through a variable
    ("export BASH_ENV=./x.sh", "x.sh"),
    ("NODE_OPTIONS='--require ./x.js' node y", "x.js"),
    ("node -r./tools/x.js y", "tools/x.js"),                                # a value glued to a short option
    ("perl -I./lib tools/lint.pl", "lib"),
    ("pytest -c./tools/pytest.ini", "tools/pytest.ini"),
    ("node --require=./tools/x.js y", "tools/x.js"),
    ("node -e \"require('./tools/x.js')\"", "tools/x.js"),
    ("sh -c 'sh \"$0\"' ./hooks/x.sh", "hooks/x.sh"),
    ("sh '`pwd`/x.sh'", "x.sh"),                                # quoted, re-read by a shell
    ('[ -f "$CLAUDE_PROJECT_DIR/hooks/x.sh" ] && sh "$CLAUDE_PROJECT_DIR/hooks/x.sh"', "hooks/x.sh"),
    ("{ sh ./hooks/x.sh; }", "hooks/x.sh"),
    ("sh ./tools/../hooks/x.sh", "tools/../hooks/x.sh"),       # ``..`` is resolved on disk
])
def test_shell_forms_that_reach_a_file_name_it(command, path):
    named = guard.named_paths(command)
    assert named.unsafe is None
    assert path in (named.paths | named.bare)


@pytest.mark.parametrize("command,path", [
    ('sh "lint hook.sh"', "lint hook.sh"),                      # a quoted name with a space
    ("sh lint\\ hook.sh", "lint hook.sh"),
    ("python3 'Dev Tools/check.py'", "Dev Tools/check.py"),
    ('sh "run(1).sh"', "run(1).sh"),
    ('sh "R&D.sh"', "R&D.sh"),
    ('sh "$CLAUDE_PROJECT_DIR/hooks/tools hook.sh"', "hooks/tools hook.sh"),
    ("sh ~+0/hooks/x.sh", "hooks/x.sh"),                       # ~+0 and ~0 are $PWD too
    ("sh ~0/hooks/x.sh", "hooks/x.sh"),
    ("python3 -c \"exec(open(r'tools/x.py').read())\"", "tools/x.py"),  # a Python r'' string
    ("python3 -c \"runpy.run_path(f'tools/x.py')\"", "tools/x.py"),
    ("env -S 'sh\\_./hooks/x.sh'", "hooks/x.sh"),              # env -S's \\_ separator
    ("java @jvm.opts -jar tools/fmt.jar", "jvm.opts"),          # an argument file
    (". ./scripts/env.sh && true", "scripts/env.sh"),           # the . builtin
])
def test_round_two_shell_forms_name_their_file(command, path):
    named = guard.named_paths(command)
    assert named.unsafe is None
    assert path in (named.paths | named.bare)


@pytest.mark.parametrize("command,why", [
    ("export PYTHONPATH=$PYTHONPATH:$CLAUDE_PROJECT_DIR/src; python3 x.py", "checkout root"),
    ('export PYTHONPATH="${PYTHONPATH}:src"', "checkout root"),
    ("export PYTHONPATH+=:", "checkout root"),
    ("PATH+=: make", "checkout root"),
    ("sh ~-/x.sh", "directory stack"),                          # where the last cd came from
    ("sh ~1/x.sh", "directory stack"),
    ("cd tools && sh $OLDPWD/x.sh", "cannot be parsed"),
    ("sh ./tools/*/../../x.sh", "climbs back out"),              # .. after a glob
    ("sh ./tools/{..,x}/x.sh", "cannot follow"),                # .. inside a brace expansion
    ("python3 ./check.py", "imports from the checkout root"),   # sys.path[0] is the root
])
def test_round_two_forms_that_reach_unknowable_places_are_unsafe(command, why):
    assert why in (guard.named_paths(command).unsafe or "")


def test_a_search_path_through_path_itself_is_not_the_root():
    assert guard.named_paths("export PATH=$PATH:$CLAUDE_PROJECT_DIR/bin").unsafe is None
    assert guard.named_paths("PATH+=:/opt/bin make").unsafe is None


@pytest.mark.parametrize("command", [
    "git diff --name-only | awk '{print $1}'",   # a {…} that is not a brace expansion
    "find tools -name '*.py' -exec true {} +",
])
def test_ordinary_shell_programs_are_not_mistaken_for_the_root(command):
    assert guard.named_paths(command).unsafe is None


def test_env_values_are_also_read_literally():
    """No shell reads an ``env`` value: ``~/lib`` is a directory named ``~`` inside
    the checkout, and ``shell env.sh`` is one file name."""
    named = guard.named_paths({"PYTHONPATH": "~/lib", "BASH_ENV": "shell env.sh"}, key="env")
    assert {"~/lib", "shell env.sh"} <= named.bare


def test_home_relative_paths_that_reach_the_checkout_are_named(tmp_path, monkeypatch):
    checkout = tmp_path / "home" / "work" / "wt"
    (checkout / "tools").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for command in ("sh ~/work/wt/x.sh", 'sh "$HOME"/work/wt/x.sh', "sh ${HOME}/work/wt/x.sh"):
        assert guard.named_paths(command, str(checkout)).paths == {"x.sh"}, command
    alias = tmp_path / "alias-to-tools"
    os.symlink(checkout / "tools", alias)
    assert guard.named_paths(f"python3 {alias}/check.py", str(checkout)).paths == {"tools/check.py"}


def test_a_module_added_next_to_a_python_hook_is_caught(tmp_path):
    """The documented hook shape ``python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/x.py``
    imports from its own directory first: a PR that adds ``.claude/hooks/json.py``
    there would replace the standard library's ``json``. The hook is held back."""
    markers = tmp_path / "markers"
    hook = 'python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/check.py'
    base = {SETTINGS: {"hooks": command_hook("PostToolUse", hook, "Edit")},
            ".claude/hooks/check.py": "import json\n"}
    repo = make_pr_repo(tmp_path, base, {"src/app.py": "x = 1\n"})
    guard.install_base_resolver(lambda checkout: repo.base)
    assert _hooks(_spawn(str(repo.wt))) == ["PostToolUse"]     # nothing near it changed: live
    write(repo.wt, ".claude/hooks/json.py", touch(markers, "json") + "\n")
    assert _hooks(_spawn(str(repo.wt))) == []
    assert markers_present(markers) == []


def test_a_literal_tilde_directory_in_an_env_search_path_is_caught(tmp_path):
    markers = tmp_path / "markers"
    base = {SETTINGS: {"env": {"PYTHONPATH": "~/lib"}}}
    repo = make_pr_repo(tmp_path, base, {"~/lib/sitecustomize.py": touch(markers, "site") + "\n"})
    guard.install_base_resolver(lambda checkout: repo.base)
    assert "env" not in _spawn(str(repo.wt))[SETTINGS]


def test_a_value_nested_too_deeply_is_unsafe_not_a_crash():
    value = "sh ./x.sh"
    for _ in range(5000):
        value = [value]
    assert guard.named_paths(value).unsafe == "it is nested too deeply to check"
    assert guard._parse(("{\"a\": " + "[" * 5000 + "]" * 5000 + "}").encode()) is None


@pytest.mark.parametrize("command,tree", [
    ('for f in "$CLAUDE_PROJECT_DIR"/hooks.d/*.sh; do sh "$f"; done', "hooks.d"),
    ("sh ./hooks.d/*.sh", "hooks.d"),
    ("sh hooks.d/*.sh", "hooks.d"),
    ('sh "$CLAUDE_PROJECT_DIR"/hooks.d/$NAME.sh', "hooks.d"),
])
def test_a_glob_or_variable_names_the_directory_before_it(command, tree):
    named = guard.named_paths(command)
    assert named.unsafe is None
    assert tree in (named.trees | named.bare_trees)


@pytest.mark.parametrize("command", [
    "cd $CLAUDE_PROJECT_DIR && npm test",
    'cd "${CLAUDE_PROJECT_DIR}" && npm test',
    "cd . && make",
    "cd ./ && make",
    'for f in "$CLAUDE_PROJECT_DIR"/*.sh; do sh "$f"; done',
    "for f in *.sh; do sh $f; done",
    "$CLAUDE_PROJECT_DIR/",
    "find . -name x",
    "sh ./hooks.{d,e}/run.sh",   # a brace expansion at the first component could be anything
])
def test_a_value_naming_the_checkout_root_is_unsafe(command):
    assert guard.named_paths(command).unsafe == "it names the checkout root"


@pytest.mark.parametrize("command,why", [
    ('echo "unbalanced', "cannot be parsed"),
    ("python3 $(whoami)/x.py", "cannot be parsed"),
    ("python3 ${CLAUDE_PROJECT_DIR:-.}/x.py", "cannot be parsed"),
    ("python3 $'\\x2e/x.py'", "cannot be parsed"),
    ("python3 foo$CLAUDE_PROJECT_DIR/x.py", "cannot be parsed"),
    ("python3 ../sibling/x.py", "outside the checkout"),
    ("python3 $CLAUDE_PROJECT_DIR/../sibling/x.py", "outside the checkout"),
])
def test_what_cannot_be_followed_is_unsafe(command, why):
    assert why in (guard.named_paths(command).unsafe or "")


def test_permission_rules_and_hook_matchers_are_not_commands():
    perms = {"allow": ["Bash(npm run test:*)", "Read(./src/**)"], "deny": ["Bash(rm *)"],
             "defaultMode": "acceptEdits"}
    assert guard.named_paths(perms, key="permissions") == guard.named_paths({}, key="permissions")
    hooks = command_hook("PreToolUse", "echo hi", "*")
    named = guard.named_paths(hooks, key="hooks")
    assert named.unsafe is None and named.bare == {"echo", "hi"}
    # the same strings anywhere else ARE scanned
    assert guard.named_paths({"x": "Bash(rm *)"}, key="statusLine").unsafe


def test_opaque_variables_and_outside_paths_name_nothing():
    named = guard.named_paths('npx prettier --write "$CLAUDE_FILE_PATHS" $HOME/bin/x /usr/bin/env')
    assert named.unsafe is None and not named.paths and not named.trees


# ── round three: the value scanner ───────────────────────────────────────────────

S = '"$CLAUDE_PROJECT_DIR"/.claude/hooks/check.py'


@pytest.mark.parametrize("command", [
    f"if [ -f {S} ]; then python3 {S}; fi",               # after a reserved word
    f"timeout 30 python3 {S}",                             # behind wrappers
    f"env -i python3 {S}",
    f"nice -n 10 python3 {S}",
    f"uv run {S}",
    f"python3 -W ignore {S}",                              # past an option's argument
    f'"$PYTHON" {S}',                                      # an interpreter named by a variable
])
def test_an_interpreter_anywhere_names_its_scripts_directory(command):
    named = guard.named_paths(command)
    assert named.unsafe is None
    assert ".claude/hooks" in named.trees


@pytest.mark.parametrize("command", [
    'HOOKS="$CLAUDE_PROJECT_DIR/.claude/hooks"; . "$HOOKS/../lib/common.sh"',
    "D=tools; sh \"$D/../x.sh\"",
    'sh "$CLAUDE_PROJECT_DIR"/sub/.?/x.sh',                 # .? and .* can match ..
    'for f in "$CLAUDE_PROJECT_DIR"/sub/.*/x.sh; do sh "$f"; done',
    'sh "$CLAUDE_PROJECT_DIR"/sub/.{.,}/x.sh',
])
def test_climbing_out_through_a_variable_or_a_dot_glob_is_unsafe(command):
    assert guard.named_paths(command).unsafe


@pytest.mark.parametrize("command", [
    "python3 -c \"import json,sys; p=json.load(sys.stdin)['tool_input']['file_path']; "
    "sys.exit(2 if any(x in p for x in ['.env', '.git/']) else 0)\"",
    "node -e \"const [a] = process.argv; console.log(a)\"",
    'case "$f" in *.py) black "$f";; *.js) prettier "$f";; esac',
    'case "$f" in\n  *.py) ruff format "$f" ;;\n  *.js) prettier -w "$f" ;;\nesac',
    'case "$f" in *.py|*.pyi) black "$f";; esac',
    '[[ "$f" == *.py ]] && black "$f"',
    "if [ -f .venv/bin/activate ]; then . .venv/bin/activate; fi",
    "echo done # it's formatted",
])
def test_ordinary_hooks_are_not_misread_as_naming_the_root(command):
    assert guard.named_paths(command).unsafe is None, guard.named_paths(command).unsafe


def test_a_prompt_hook_and_additional_directories_are_not_commands():
    hooks = {"Stop": [{"hooks": [{"type": "prompt", "prompt": "Don't stop yet; check it's done."}]}]}
    assert guard.named_paths(hooks, key="hooks").unsafe is None
    perms = {"deny": ["Read(./.env)"], "additionalDirectories": ["../shared-docs"]}
    assert guard.named_paths(perms, key="permissions").unsafe is None


def test_a_path_holding_a_nul_is_unsafe_not_a_crash():
    assert guard.named_paths("cat ~\x00/notes").unsafe
    assert guard.named_paths("cat ~\ud800/notes").unsafe


def _hook_repo(tmp_path, command, files, head=None):
    base = {SETTINGS: {"hooks": command_hook("SessionStart", command)}, **files}
    repo = make_pr_repo(tmp_path, base, head or {"docs/guide.md": "x\n"})
    guard.install_base_resolver(lambda checkout: repo.base)
    return repo


def test_a_script_run_by_its_shebang_names_its_directory(tmp_path):
    """The documented hook ``"$CLAUDE_PROJECT_DIR"/.claude/hooks/check.py``, run by
    its ``#!/usr/bin/env python3`` line: a module the PR adds beside it would
    replace the standard library's, so the hook is held back."""
    repo = _hook_repo(tmp_path, S, {".claude/hooks/check.py": "#!/usr/bin/env python3\nimport json\n"})
    os.chmod(repo.wt / ".claude/hooks/check.py", 0o755)
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    write(repo.wt, ".claude/hooks/json.py", "x = 1\n")
    assert _hooks(_spawn(str(repo.wt))) == []


def test_cd_into_a_checked_directory_resolves_what_follows_there(tmp_path):
    command = 'cd "$CLAUDE_PROJECT_DIR"/.claude/hooks && ./format.sh'
    repo = _hook_repo(tmp_path, command, {".claude/hooks/format.sh": "true\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    write(repo.wt, ".claude/hooks/format.sh", "echo changed\n")
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_protective_hook_that_mentions_git_stays_live(tmp_path):
    repo = _hook_repo(tmp_path, "grep -q ORIG_HEAD .git/HEAD && exit 2", {})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]


def test_an_untracked_bytecode_cache_does_not_strip_a_python_hook(tmp_path):
    """A hook's own run writes ``__pycache__/*.pyc`` beside its helper module: that
    untracked cache is not a change. A cache the PR commits still is."""
    command = f"python3 {S}"
    repo = _hook_repo(tmp_path, command, {".claude/hooks/check.py": "import helper\n",
                                         ".claude/hooks/helper.py": "x = 1\n"})
    write(repo.wt, ".claude/hooks/__pycache__/helper.cpython-311.pyc", b"\x00cache")
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    git(repo.wt, "add", "-f", ".claude/hooks/__pycache__/helper.cpython-311.pyc")
    git(repo.wt, "commit", "-qm", "commit a cache")
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_named_file_the_base_branch_lacks_is_never_trusted(tmp_path):
    """C3(c): an optional script base does not have, which the PR adds."""
    repo = _hook_repo(tmp_path, "[ -f ./local-hook.sh ] && sh ./local-hook.sh", {})
    assert _hooks(_spawn(str(repo.wt))) == []              # absent at base: never trusted
    write(repo.wt, "local-hook.sh", touch(tmp_path / "markers", "local") + "\n")
    assert _hooks(_spawn(str(repo.wt))) == []              # ... not even once it exists


# ── A5: a directory hook (C3a) ───────────────────────────────────────────────────

DIR_HOOK = 'for f in "$CLAUDE_PROJECT_DIR"/hooks.d/*.sh; do sh "$f"; done'


def _dir_hook_repo(tmp_path, head=None, removed=()):
    markers = tmp_path / "markers"
    base = {
        SETTINGS: {"model": "sonnet", "hooks": command_hook("SessionStart", DIR_HOOK)},
        "hooks.d/a.sh": touch(markers, "a") + "\n",
        "hooks.d/b.sh": touch(markers, "b") + "\n",
        ".gitignore": "*.local.sh\n",
    }
    repo = make_pr_repo(tmp_path, base, head or {"src/app.py": "x = 2\n"}, removed=removed)
    guard.install_base_resolver(lambda checkout: repo.base)
    return repo


@pytest.mark.parametrize("case", ["adds", "modifies", "removes", "untracked", "ignored", "symlink"])
def test_a_directory_hook_is_stripped_when_the_directory_changes(tmp_path, case):
    """A5: base-identical ``for f in "$CLAUDE_PROJECT_DIR"/hooks.d/*.sh`` hook. The
    PR adds, modifies or removes a file there — or a file appears untracked,
    ignored, or as a symlink base lacks — and the hook is held back."""
    markers = tmp_path / "markers"
    head, removed = None, ()
    if case == "adds":
        head = {"hooks.d/evil.sh": touch(markers, "evil") + "\n"}
    elif case == "modifies":
        head = {"hooks.d/a.sh": touch(markers, "a2") + "\n"}
    elif case == "removes":
        removed = ("hooks.d/a.sh",)
    repo = _dir_hook_repo(tmp_path, head, removed)
    if case == "untracked":
        write(repo.wt, "hooks.d/c.sh", touch(markers, "c") + "\n")
    elif case == "ignored":
        write(repo.wt, "hooks.d/c.local.sh", touch(markers, "c") + "\n")
        assert "c.local.sh" not in repo.status()
    elif case == "symlink":
        os.symlink(str(tmp_path / "elsewhere.sh"), repo.wt / "hooks.d" / "c.sh")
    before = repo.settings_bytes()
    view = _spawn(str(repo.wt))
    assert _hooks(view) == [] and view[SETTINGS]["model"] == "sonnet"
    assert repo.settings_bytes() == before
    assert markers_present(repo.markers) == []


def test_a_directory_hook_stays_live_when_the_directory_is_untouched(tmp_path):
    """A5 control: the PR leaves ``hooks.d/`` alone, so the hook stays live and the
    settings file is never rewritten."""
    repo = _dir_hook_repo(tmp_path)
    path = repo.wt / SETTINGS
    st = os.stat(path)
    view = _spawn(str(repo.wt))
    assert _hooks(view) == ["SessionStart"]
    assert (os.stat(path).st_ino, os.stat(path).st_mtime_ns) == (st.st_ino, st.st_mtime_ns)
    assert not any(f.endswith(".json") for f in _state_files())


def test_a_base_identical_symlink_is_followed_to_what_it_points_at(tmp_path):
    """A symlink base also has is trusted only as far as its target: a PR that
    changes the file a ``hooks.d`` symlink points at still loses the hook."""
    markers = tmp_path / "markers"
    base = {
        SETTINGS: {"hooks": command_hook("SessionStart", DIR_HOOK)},
        "scripts/real.sh": touch(markers, "real") + "\n",
    }
    repo = make_pr_repo(tmp_path, base, {})
    (repo.wt / "hooks.d").mkdir()
    os.symlink("../scripts/real.sh", repo.wt / "hooks.d" / "a.sh")
    git(repo.wt, "add", "-A")
    git(repo.wt, "commit", "-qm", "link")
    new_base = git(repo.wt, "rev-parse", "HEAD").strip()
    guard.install_base_resolver(lambda checkout: new_base)
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]  # link and target both base
    write(repo.wt, "scripts/real.sh", touch(markers, "changed") + "\n")
    assert _hooks(_spawn(str(repo.wt))) == []


# ── A6: kill → restore ───────────────────────────────────────────────────────────

_CHILD = """
import sys, time
from buddhi_review import claude_settings_guard as guard
with guard.window(sys.argv[1]):
    if len(sys.argv) > 3:  # the run edits a held file, then is killed
        open(sys.argv[3], "wb").write(open(sys.argv[4], "rb").read())
    open(sys.argv[2], "w").write("ready")
    time.sleep(120)
"""


def _child_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = PACKAGE_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _kill_mid_window(repo, tmp_path, edit=None):
    """A child enters a window on ``repo`` (first writing ``edit = (rel, bytes)``
    into the held file, as a run would) and is SIGKILLed inside it."""
    ready = tmp_path / "ready"
    argv = [sys.executable, "-c", _CHILD, str(repo.wt), str(ready)]
    if edit is not None:
        payload = tmp_path / "run-edit.bin"
        payload.write_bytes(edit[1])
        argv += [str(repo.wt / edit[0]), str(payload)]
    proc = subprocess.Popen(argv, env=_child_env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 30
    while not ready.exists():
        assert proc.poll() is None, proc.stderr.read().decode()
        assert time.monotonic() < deadline, "the child never entered its window"
        time.sleep(0.02)
    journal = guard._journal_path(str(repo.wt))
    assert _mode(journal) == 0o600
    assert _mode(os.path.dirname(journal)) == 0o700
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    proc.stderr.close()
    return journal


def _hostile_tracked(tmp_path):
    return make_pr_repo(tmp_path, {}, {SETTINGS: hostile(tmp_path / "markers")}), SETTINGS


def _hostile_untracked_local(tmp_path):
    repo = make_pr_repo(tmp_path, {".gitignore": ".claude/settings.local.json\n"}, {})
    path = write(repo.wt, LOCAL_SETTINGS, {
        "env": {"ANTHROPIC_API_KEY": "sk-local-secret"},
        **hostile(tmp_path / "markers"),
    })
    os.chmod(path, 0o600)
    return repo, LOCAL_SETTINGS


def _heal_by_next_spawn(repo):
    view = _spawn(str(repo.wt))
    assert _hooks(view) == []


def _heal_at_loop_entry(repo):
    class _Driver:
        def __init__(self, *a, **k):
            pass

        def run(self):
            return SimpleNamespace(status="clean", rounds=1, merged=False)

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(cli.round_driver, "RoundDriver", _Driver)
        mp.setattr(cli.round_driver, "refuse_primary_checkout", lambda *a, **k: None)
        mp.setattr(cli.round_driver, "enforce_repo_confirmation_gate", lambda *a, **k: None)
        mp.setattr(cli, "ConsoleNotifier",
                   lambda *a, **k: SimpleNamespace(startup_log=lambda: None))
        mp.setattr(cli.upsell, "maybe_emit_run_end_nudge", lambda *a, **k: None)
        args = cli.build_parser().parse_args(
            ["run-loop", "7", "--repo", "o/r", "--max-rounds", "3", "--cwd", str(repo.wt)])
        with redirect_stdout(io.StringIO()):
            assert cli._run_loop(args) == 0
    finally:
        mp.undo()


@pytest.mark.parametrize("heal", [_heal_by_next_spawn, _heal_at_loop_entry],
                         ids=["next-spawn", "loop-entry"])
@pytest.mark.parametrize("make", [_hostile_tracked, _hostile_untracked_local],
                         ids=["tracked-settings", "untracked-0600-local"])
def test_a_killed_window_is_restored_exactly(tmp_path, capfd, make, heal):
    """A6: a child enters the guard, signals ready, and is SIGKILLed. Before
    recovery the file is still scrubbed; the next guarded spawn and the loop-entry
    heal each restore bytes, mode and index flags on their own; git status is
    clean; the journal is gone, and was 0o600 while it existed."""
    repo, rel = make(tmp_path)
    path = repo.wt / rel
    before, mode, flags = path.read_bytes(), _mode(path), repo.flags()
    journal = _kill_mid_window(repo, tmp_path)
    assert os.path.exists(journal)
    assert _hooks(seen_settings(str(repo.wt))) == []          # still scrubbed
    assert path.read_bytes() != before
    heal(repo)
    assert path.read_bytes() == before
    assert _mode(path) == mode
    assert repo.flags() == flags
    assert repo.status() == ""
    assert not os.path.exists(journal)
    assert [f for f in _state_files() if not f.endswith(".lock")] == []
    assert "after an interrupted claude run" in capfd.readouterr().err
    assert markers_present(repo.markers) == []


# ── C4: an exception raised inside the spawn still restores ──────────────────────

@pytest.mark.parametrize("raised,surfaces", [
    (subprocess.TimeoutExpired("claude", 1), subprocess.TimeoutExpired),
    (KeyboardInterrupt(), KeyboardInterrupt),
    (FileNotFoundError("claude"), RuntimeError),   # a model call's launch failure
], ids=["timeout", "interrupt", "launch-failure"])
def test_an_exception_inside_the_spawn_restores_at_once(tmp_path, monkeypatch, raised, surfaces):
    """The restore runs as the exception leaves the window, not at the next spawn:
    bytes, mode and flags are back and no journal remains, whether the spawn timed
    out, was interrupted, or failed to start."""
    repo, rel = _hostile_tracked(tmp_path)
    before, flags = repo.settings_bytes(), repo.flags()

    def boom(spawn):
        raise raised

    monkeypatch.setattr(subprocess, "run", ClaudeStub(real_run=subprocess.run, during=boom))
    with pytest.raises(surfaces):
        model_call.run_model_text("p", role="classifier", cwd=str(repo.wt))
    assert repo.settings_bytes() == before and repo.flags() == flags and repo.status() == ""
    assert not any(f.endswith(".json") for f in _state_files())


def test_a_fixer_timeout_restores_before_its_retry(tmp_path, monkeypatch):
    repo, rel = _hostile_tracked(tmp_path)
    before, flags = repo.settings_bytes(), repo.flags()
    views = []

    def timeout(spawn):
        views.append((spawn.hooks, repo.flags()))
        raise subprocess.TimeoutExpired("claude", 1)

    monkeypatch.setattr(fix_apply, "maybe_sandbox", lambda argv, cwd: list(argv))
    monkeypatch.setattr(subprocess, "run", ClaudeStub(real_run=subprocess.run, during=timeout))
    outcome = fix_apply.apply_fix("fix it", cwd=str(repo.wt), model="sonnet", effort="low",
                                  retries=1)
    assert outcome.status == "transient-failed" and len(views) == 2
    assert all(hooks == [] for hooks, _ in views)
    assert repo.settings_bytes() == before and repo.flags() == flags and repo.status() == ""
    assert not any(f.endswith(".json") for f in _state_files())


def test_two_spellings_of_one_checkout_share_one_lock_and_journal(tmp_path):
    """On a case-insensitive filesystem another letter case names the same
    checkout: it must find the same journal (and take the same lock)."""
    repo, rel = _hostile_tracked(tmp_path)
    other = str(repo.wt)[:-2] + str(repo.wt)[-2:].upper()
    if not os.path.isdir(other) or os.path.samefile(other, repo.wt) is False:
        pytest.skip("the filesystem is case-sensitive")
    assert guard._journal_path(other) == guard._journal_path(str(repo.wt))
    before = repo.settings_bytes()
    _kill_mid_window(repo, tmp_path)
    assert guard.recover(other) is True
    assert repo.settings_bytes() == before and repo.status() == ""


# ── A7: a user's own skip-worktree override survives (C6) ────────────────────────

@pytest.mark.parametrize("edit", ["allowlisted", "adds-a-hook"])
def test_a_user_skip_worktree_override_survives_repeated_spawns(tmp_path, edit):
    """A7: a tracked settings file the user marked ``--skip-worktree`` and edited
    locally keeps its bytes and its bit through three guarded spawns; when the
    edit adds a hook base lacks, a scrub and restore happen and no spawn sees it."""
    repo = make_pr_repo(tmp_path, {SETTINGS: {"model": "sonnet"}}, {})
    guard.install_base_resolver(lambda checkout: repo.base)
    git(repo.wt, "update-index", "--skip-worktree", SETTINGS)
    local = {"model": "opus", "theme": "dark"}
    if edit == "adds-a-hook":
        local.update(hostile(tmp_path / "markers"))
    path = write(repo.wt, SETTINGS, local)
    before = path.read_bytes()
    for _ in range(3):
        view = _spawn(str(repo.wt))
        assert _hooks(view) == [] and view[SETTINGS]["model"] == "opus"
        assert path.read_bytes() == before
        assert git(repo.wt, "ls-files", "-v", SETTINGS).startswith("S ")
    assert repo.status() == ""
    assert markers_present(repo.markers) == []


# ── A8: two processes on one checkout ────────────────────────────────────────────

_SLOW_CHILD = """
import json, os, sys, time
from buddhi_review import claude_settings_guard as guard
_write = os.write
def _one_byte_at_a_time(fd, data):
    # any write in place would be caught half-done by the poller
    n = _write(fd, bytes(memoryview(data)[:1]))
    time.sleep(0.0005)
    return n
os.write = _one_byte_at_a_time
with guard.window(sys.argv[1]):
    start = time.time()
    time.sleep(0.5)          # the "spawn"
    end = time.time()
open(sys.argv[2], "w").write(json.dumps([start, end]))
"""


def test_two_processes_never_interleave_and_the_path_is_never_torn(tmp_path):
    """A8: two processes each hold a window with a slow spawn while a poller reads
    the settings path in a tight loop. The windows never overlap, the poller never
    sees the path absent or unparseable, and the final state is byte-identical."""
    repo, rel = _hostile_tracked(tmp_path)
    path = repo.wt / rel
    before, flags = path.read_bytes(), repo.flags()
    seen = {"reads": 0, "absent": 0, "torn": 0}
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                seen["absent"] += 1
                continue
            try:
                ok = isinstance(json.loads(data), dict)
            except ValueError:
                ok = False
            seen["reads"] += 1
            seen["torn"] += not ok

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    logs = [tmp_path / "a.json", tmp_path / "b.json"]
    procs = [subprocess.Popen([sys.executable, "-c", _SLOW_CHILD, str(repo.wt), str(log)],
                              env=_child_env(), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL) for log in logs]
    try:
        for p in procs:
            assert p.wait(timeout=45) == 0
    finally:
        stop.set()
        poller.join()
    (s1, e1), (s2, e2) = sorted(json.loads(log.read_text()) for log in logs)
    assert e1 <= s2, "the two windows overlapped"
    assert seen["reads"] > 100
    assert seen["absent"] == 0 and seen["torn"] == 0, seen
    assert path.read_bytes() == before and repo.flags() == flags and repo.status() == ""
    assert markers_present(repo.markers) == []


# ── A10: no base commit known ────────────────────────────────────────────────────

@pytest.mark.parametrize("resolver,why", [
    (None, "no base resolver is installed"),
    (lambda checkout: None, "the base resolver found none"),
    (lambda checkout: 1 / 0, "the base resolver failed: division by zero"),
])
def test_with_no_base_only_allowlisted_keys_survive(tmp_path, capfd, resolver, why):
    """A10: with no resolver, or one that cannot answer, a base-identical hook and
    base-identical permissions are held back too — only the audited display keys
    survive — with ONE log line, and no crash."""
    settings = {"model": "sonnet", "theme": "dark", "permissions": {"allow": ["Bash(ls)"]},
                "hooks": command_hook("SessionStart", touch(tmp_path / "markers", "base"))}
    repo = make_pr_repo(tmp_path, {SETTINGS: settings}, {})
    guard.install_base_resolver(resolver)
    capfd.readouterr()
    for _ in range(2):
        view = _spawn(str(repo.wt))
        assert view[SETTINGS] == {"model": "sonnet", "theme": "dark"}
    err = capfd.readouterr().err
    lines = [ln for ln in err.splitlines() if "base commit is unknown" in ln]
    assert len(lines) == 1 and why in lines[0], err
    assert repo.status() == ""


# ── A11 / C10: the hot path is free ──────────────────────────────────────────────

class _Counter:
    def __init__(self, real):
        self.real, self.calls = real, []

    def __call__(self, argv, *a, **kw):
        self.calls.append([str(x) for x in argv])
        if os.path.basename(str(argv[0])) == "claude":
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        return self.real(argv, *a, **kw)


def test_a_checkout_with_no_claude_entry_costs_no_subprocess_and_no_write(tmp_path, monkeypatch):
    """A11: no ``.claude`` entry → the guard runs zero subprocesses and writes
    nothing (its state dir is never even created); a model spawn and a fixer spawn
    each run exactly their one ``claude``."""
    counter = _Counter(subprocess.run)
    monkeypatch.setattr(subprocess, "run", counter)
    with guard.window(str(tmp_path)):
        pass
    assert counter.calls == []
    model_call._make_default_spawn(str(tmp_path))(["claude", "-p", "x"], None, 5)
    monkeypatch.setattr(fix_apply, "maybe_sandbox", lambda argv, cwd: list(argv))
    fix_apply.default_fixer_runner("p", model="sonnet", effort="low", timeout=5, cwd=str(tmp_path))
    assert [c[0] for c in counter.calls] == ["claude", "claude"]
    assert not os.path.exists(os.environ[guard.STATE_DIR_ENV])


def test_an_allowlisted_only_file_is_never_touched(tmp_path, monkeypatch):
    """C10: a file holding only allowlisted keys costs no subprocess, no rewrite,
    no index change and no journal."""
    repo = make_pr_repo(tmp_path, {}, {SETTINGS: {"model": "opus", "theme": "dark",
                                                  "verbose": True}})
    path = repo.wt / SETTINGS
    st, flags = os.stat(path), repo.flags()
    counter = _Counter(subprocess.run)
    monkeypatch.setattr(subprocess, "run", counter)
    view = _spawn(str(repo.wt))
    assert counter.calls == []
    assert view[SETTINGS] == {"model": "opus", "theme": "dark", "verbose": True}
    assert (os.stat(path).st_ino, os.stat(path).st_mtime_ns) == (st.st_ino, st.st_mtime_ns)
    assert repo.flags() == flags
    assert not any(f.endswith(".json") for f in _state_files())


def test_a_base_trusted_file_is_read_but_never_written(tmp_path, monkeypatch):
    """C10: base-trusted keys cost git reads only — no rewrite, no index change,
    no journal."""
    settings = {"hooks": command_hook("SessionStart", touch(tmp_path / "markers", "x"))}
    repo = make_pr_repo(tmp_path, {SETTINGS: settings}, {})
    guard.install_base_resolver(lambda checkout: repo.base)
    path = repo.wt / SETTINGS
    st, flags = os.stat(path), repo.flags()
    counter = _Counter(subprocess.run)
    monkeypatch.setattr(subprocess, "run", counter)
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    assert all(c[:2] != ["git", "update-index"] for c in counter.calls)
    assert (os.stat(path).st_ino, os.stat(path).st_mtime_ns) == (st.st_ino, st.st_mtime_ns)
    assert repo.flags() == flags
    assert not any(f.endswith(".json") for f in _state_files())


# ── A12: a falsy cwd is the process's own directory, never skipped (C8) ──────────

@pytest.mark.parametrize("falsy", [None, ""])
def test_a_falsy_cwd_guards_the_directory_the_child_inherits(tmp_path, monkeypatch, falsy):
    """A12: called with no cwd from inside a hostile checkout, the guard scrubs
    and restores that checkout — it is never skipped."""
    monkeypatch.delenv(guard.FALLBACK_CWD_ENV)  # this test pins the real fallback
    repo, rel = _hostile_tracked(tmp_path)
    before = repo.settings_bytes()
    monkeypatch.chdir(repo.wt)
    assert _hooks(_spawn(falsy)) == []
    assert repo.settings_bytes() == before and repo.status() == ""
    stub = ClaudeStub(real_run=subprocess.run)
    monkeypatch.setattr(subprocess, "run", stub)
    model_call.run_model_text("p", role="classifier")        # the cwd-less default spawn
    assert stub.spawns[-1].cwd_kwarg is None
    assert stub.spawns[-1].effective == os.path.realpath(repo.wt)
    assert stub.spawns[-1].hooks == []
    assert markers_present(repo.markers) == []


# ── A13: nothing outside the checkout is ever written ────────────────────────────

def test_a_committed_claude_symlink_is_removed_for_the_spawn_never_written_through(tmp_path):
    outside = tmp_path / "outside"
    write(outside, "settings.json", hostile(tmp_path / "markers"))
    repo = make_pr_repo(tmp_path, {}, {})
    os.symlink(str(outside), repo.wt / ".claude")
    git(repo.wt, "add", ".claude")
    git(repo.wt, "commit", "-qm", "link .claude outside")
    flags, snap = repo.flags(), _snapshot(outside)
    with guard.window(str(repo.wt)):
        assert not os.path.lexists(repo.wt / ".claude")
        assert repo.status() == ""                         # skip-worktree hides the gap
    assert os.readlink(repo.wt / ".claude") == str(outside)
    assert _snapshot(outside) == snap
    assert repo.flags() == flags and repo.status() == ""


def test_a_hardlinked_settings_file_never_writes_the_outside_file(tmp_path):
    repo, rel = _hostile_tracked(tmp_path)
    before = repo.settings_bytes()
    outside = tmp_path / "outside" / "settings.json"
    outside.parent.mkdir()
    outside.write_bytes(before)
    (repo.wt / rel).unlink()
    os.link(outside, repo.wt / rel)
    snap = _snapshot(outside.parent)
    assert _hooks(_spawn(str(repo.wt))) == []
    assert _snapshot(outside.parent) == snap
    assert repo.settings_bytes() == before and repo.status() == ""


def test_a_symlinked_settings_file_is_removed_for_the_spawn(tmp_path):
    outside = tmp_path / "outside" / "settings.json"
    write(outside.parent, "settings.json", hostile(tmp_path / "markers"))
    repo = make_pr_repo(tmp_path, {}, {})
    (repo.wt / ".claude").mkdir()
    os.symlink(str(outside), repo.wt / SETTINGS)
    git(repo.wt, "add", "-A")
    git(repo.wt, "commit", "-qm", "link settings outside")
    snap = _snapshot(outside.parent)
    with guard.window(str(repo.wt)):
        assert not os.path.lexists(repo.wt / SETTINGS)
    assert os.readlink(repo.wt / SETTINGS) == str(outside)
    assert _snapshot(outside.parent) == snap and repo.status() == ""


def test_a_pr_committed_file_named_like_a_temp_file_is_never_deleted(tmp_path):
    """Recovery removes only the temp names of the interrupted window's own random
    token, so a PR that commits look-alike files loses none of them."""
    repo = make_pr_repo(tmp_path, {}, {
        SETTINGS: hostile(tmp_path / "markers"),
        ".claude/.settings.json.0123456789abcdef.guard-tmp": "tracked\n",
        "..claude.0123456789abcdef.guard-tmp": "tracked\n",
    })
    _kill_mid_window(repo, tmp_path)
    assert guard.recover(str(repo.wt)) is True
    assert repo.status() == ""


def test_a_dotdot_through_a_pr_planted_symlink_is_checked_where_it_really_leads(tmp_path):
    """``..`` is resolved on disk: a base-identical hook that climbs through a
    directory the PR turned into a symlink reaches a PR file, and is held back."""
    markers = tmp_path / "markers"
    hook = 'python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/../../scripts/lint.py'
    base = {SETTINGS: {"hooks": command_hook("SessionStart", hook)},
            ".claude/hooks/keep.txt": "x\n", "scripts/lint.py": touch(markers, "lint") + "\n"}
    repo = make_pr_repo(tmp_path, base, {"evil/deep/keep.txt": "x\n",
                                         "evil/scripts/lint.py": touch(markers, "evil") + "\n"},
                        removed=(".claude/hooks/keep.txt",))
    guard.install_base_resolver(lambda checkout: repo.base)
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]  # a real directory: live
    os.rmdir(repo.wt / ".claude" / "hooks")
    os.symlink("../evil/deep", repo.wt / ".claude" / "hooks")
    assert _hooks(_spawn(str(repo.wt))) == []
    assert markers_present(markers) == []


def test_a_dotdot_inside_a_base_identical_symlink_is_followed_on_disk(tmp_path):
    """Base holds ``tools/check.py -> impl/../real_check.py``; the PR turns
    ``tools/impl`` into a symlink elsewhere. The link and its lexical target are
    unchanged, but the kernel now reaches a PR file, so the hook is held back."""
    markers = tmp_path / "markers"
    hook = 'python3 "$CLAUDE_PROJECT_DIR"/tools/check.py'
    base = {SETTINGS: {"hooks": command_hook("SessionStart", hook)},
            "tools/impl/keep.txt": "x\n", "tools/real_check.py": touch(markers, "real") + "\n"}
    repo = make_pr_repo(tmp_path, base, {})
    os.symlink("impl/../real_check.py", repo.wt / "tools" / "check.py")
    git(repo.wt, "add", "-A")
    git(repo.wt, "commit", "-qm", "link")
    trusted = git(repo.wt, "rev-parse", "HEAD").strip()
    guard.install_base_resolver(lambda checkout: trusted)
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    write(repo.wt, "evil/deep/keep.txt", "x\n")
    write(repo.wt, "evil/real_check.py", touch(markers, "evil") + "\n")
    shutil.rmtree(repo.wt / "tools" / "impl")
    os.symlink("../evil/deep", repo.wt / "tools" / "impl")
    assert _hooks(_spawn(str(repo.wt))) == []
    assert markers_present(markers) == []


def test_an_absolute_path_spelled_another_way_still_names_the_checkout_file(tmp_path):
    """An absolute path that reaches the checkout through an alias — a symlinked
    parent here, another letter case on a case-insensitive filesystem — is
    recognised by file identity, not by its spelling."""
    markers = tmp_path / "markers"
    alias = tmp_path / "alias"
    hook = f"python3 {alias}/tools/check.py"
    base = {SETTINGS: {"hooks": command_hook("SessionStart", hook)},
            "tools/check.py": touch(markers, "check") + "\n"}
    repo = make_pr_repo(tmp_path, base, {"tools/check.py": touch(markers, "changed") + "\n"})
    os.symlink(str(repo.wt), alias)
    guard.install_base_resolver(lambda checkout: repo.base)
    assert guard.named_paths(hook, str(repo.wt)).paths == {"tools/check.py"}
    assert _hooks(_spawn(str(repo.wt))) == []
    assert markers_present(markers) == []


@pytest.mark.parametrize("value", [":/usr/bin:/bin", "/usr/bin::/bin", "/usr/bin:", ""])
def test_a_search_path_with_an_empty_element_names_the_checkout_root(value):
    """An empty element of ``PATH`` (or ``PYTHONPATH`` …) is the current directory:
    a base-identical ``env`` would make a bare ``python3`` in any hook run a file the
    PR added at the checkout root."""
    assert guard.named_paths({"PATH": value}, key="env").unsafe == "it names the checkout root"
    assert guard.named_paths(f"PATH={value} python3 x", key="hooks").unsafe == \
        "it names the checkout root"
    assert guard.named_paths({"PATH": "/usr/bin:/bin"}, key="env").unsafe is None


def test_a_claude_dir_swapped_for_a_symlink_mid_spawn_is_never_written_through(tmp_path, capfd):
    """A spawn that swaps ``.claude/`` for a symlink to a directory outside the
    checkout: the restore never writes through it."""
    outside = tmp_path / "outside"
    write(outside, "settings.json", {"model": "x"})
    repo, rel = _hostile_tracked(tmp_path)
    snap = _snapshot(outside)
    with guard.window(str(repo.wt)):
        shutil.rmtree(repo.wt / ".claude")
        os.symlink(str(outside), repo.wt / ".claude")
    assert _snapshot(outside) == snap
    assert ".claude was replaced while settings were held back" in capfd.readouterr().err
    assert not any(f.endswith(".json") for f in _state_files())


def test_after_a_crash_a_claude_symlink_is_never_written_through(tmp_path, capfd):
    """The window holding an untracked local file is killed; ``.claude`` is then
    replaced by a symlink into the checkout. Recovery never writes the user's
    local settings through it (``git add -A`` would pick them up there): their
    original is saved beside the journal instead, owner-only, and named."""
    repo, rel = _hostile_untracked_local(tmp_path)
    before = (repo.wt / rel).read_bytes()
    _kill_mid_window(repo, tmp_path)
    shutil.rmtree(repo.wt / ".claude")
    (repo.wt / "docs").mkdir()
    os.symlink("docs", repo.wt / ".claude")
    assert guard.recover(str(repo.wt)) is True
    assert os.listdir(repo.wt / "docs") == []
    assert _saved_bytes() == [before]
    saved = Path(os.environ[guard.STATE_DIR_ENV]) / _saved_copies()[0]
    assert _mode(saved) == 0o600 and str(saved) in capfd.readouterr().err
    assert markers_present(repo.markers) == []


def test_an_edited_local_file_gets_the_keys_it_did_not_restate_back(tmp_path, capfd):
    """An untracked local file's bytes exist only in the journal. A run that edits
    it keeps its edit — the key it re-stated is its own — and the key the guard
    held back that the run did not re-state (the user's secret) is merged back,
    at the file's own mode. Nothing needs saving aside."""
    repo = make_pr_repo(tmp_path, {".gitignore": ".claude/settings.local.json\n"}, {})
    rules = [f"Bash(tool{i})" for i in range(40)]
    path = write(repo.wt, LOCAL_SETTINGS, {"permissions": {"allow": rules}, "env": {"K": "secret"}})
    os.chmod(path, 0o600)
    with guard.window(str(repo.wt)):
        write(repo.wt, LOCAL_SETTINGS, {"permissions": {"allow": ["Bash(new)"]}})
    assert json.loads(path.read_text()) == {"permissions": {"allow": ["Bash(new)"]}, "env": {"K": "secret"}}
    assert _mode(path) == 0o600 and _saved_copies() == []
    assert ("kept the edit made to this file while settings were held back, and put back env"
            in capfd.readouterr().err)
    assert not any(f.endswith(".json") for f in _state_files())


_TERM_CHILD = """
import subprocess, sys
from buddhi_review import claude_settings_guard as guard
with guard.window(sys.argv[1]):
    subprocess.run([sys.executable, "-c",
                    "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); "
                    "time.sleep(120)", sys.argv[2]])
"""


def test_a_sigterm_mid_spawn_kills_the_child_and_restores(tmp_path):
    """``kill <pid>`` of the loop mid-spawn: the spawn's child is killed with it and
    the settings are put back, instead of the child running on while a restarted
    loop puts the PR's settings back underneath it."""
    repo, rel = _hostile_tracked(tmp_path)
    before, flags = repo.settings_bytes(), repo.flags()
    pidfile = tmp_path / "child.pid"
    proc = subprocess.Popen([sys.executable, "-c", _TERM_CHILD, str(repo.wt), str(pidfile)],
                            env=_child_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 30
    while not pidfile.exists() or not pidfile.read_text():
        assert proc.poll() is None and time.monotonic() < deadline
        time.sleep(0.02)
    child = int(pidfile.read_text())
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 128 + signal.SIGTERM
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    assert repo.settings_bytes() == before and repo.flags() == flags and repo.status() == ""
    assert not any(f.endswith(".json") for f in _state_files())


def test_a_journal_temp_left_by_a_kill_is_swept_on_the_next_entry(tmp_path):
    repo, rel = _hostile_tracked(tmp_path)
    state = Path(os.environ[guard.STATE_DIR_ENV])
    state.mkdir(mode=0o700)
    leftover = state / f".{guard._key(str(repo.wt))}.json.0123456789abcdef.guard-tmp"
    leftover.write_text("{a crashed journal write}")
    _spawn(str(repo.wt))
    assert not leftover.exists()


_KILL_AT_RESTORE = """
import os, signal, sys, time
from buddhi_review import claude_settings_guard as guard
calls = []
_replace = os.replace
def replace(*a, **k):
    calls.append(a)
    if len(calls) == 3:   # journal, scrub, then the restore's own write
        os.kill(os.getpid(), signal.SIGKILL)
    return _replace(*a, **k)
os.replace = replace
with guard.window(sys.argv[1]):
    pass
"""


def test_a_kill_during_the_restore_leaves_no_temp_file_behind(tmp_path):
    """Killed between writing the restore's temp file (the original bytes — a
    local secret, for a local file) and renaming it: the next entry removes the
    temp and finishes the restore; nothing named ``*.guard-tmp`` survives."""
    repo, rel = _hostile_untracked_local(tmp_path)
    path = repo.wt / rel
    before = path.read_bytes()
    proc = subprocess.run([sys.executable, "-c", _KILL_AT_RESTORE, str(repo.wt)],
                          env=_child_env(), capture_output=True)
    assert proc.returncode == -signal.SIGKILL
    assert any(n.endswith(".guard-tmp") for n in os.listdir(repo.wt / ".claude"))
    assert guard.recover(str(repo.wt)) is True
    assert path.read_bytes() == before and _mode(path) == 0o600
    assert not [p for p in repo.wt.rglob("*.guard-tmp")]
    assert repo.status() == ""


def test_every_byte_is_written_at_or_below_the_original_mode(tmp_path, monkeypatch):
    """C5: the scrub, the journal and the restore write every byte into a file
    already at its final, owner-only mode — never wider, not even briefly."""
    repo, rel = _hostile_untracked_local(tmp_path)
    real = os.write
    modes = []

    def spy(fd, data):
        modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real(fd, data)

    monkeypatch.setattr(os, "write", spy)
    _spawn(str(repo.wt))
    monkeypatch.undo()
    assert modes and all(m & ~0o600 == 0 for m in modes), [oct(m) for m in modes]


def test_the_restore_never_clears_the_users_own_skip_worktree_bit(tmp_path):
    """A7, widened: the user marked ``settings.json`` skip-worktree (a local
    override), and the PR commits a hostile ``settings.local.json`` that the guard
    marks for the window. Only the guard's own bit is cleared afterwards."""
    repo = make_pr_repo(tmp_path, {SETTINGS: {"model": "sonnet"}},
                        {LOCAL_SETTINGS: hostile(tmp_path / "markers")})
    git(repo.wt, "update-index", "--skip-worktree", SETTINGS)
    write(repo.wt, SETTINGS, {"model": "opus"})
    view = _spawn(str(repo.wt))
    assert _hooks(view) == []
    assert git(repo.wt, "ls-files", "-v", SETTINGS).startswith("S ")
    assert git(repo.wt, "ls-files", "-v", LOCAL_SETTINGS).startswith("H ")


def test_a_journal_for_another_checkout_is_never_replayed(tmp_path):
    repo, rel = _hostile_tracked(tmp_path)
    journal = Path(guard._journal_path(str(repo.wt)))
    journal.parent.mkdir(parents=True, mode=0o700)
    journal.write_text(json.dumps({"version": 1, "checkout": "/somewhere/else",
                                   "token": "0123456789abcdef", "entries": []}))
    assert guard.recover(str(repo.wt)) is False
    with pytest.raises(guard.SettingsGuardRefusal, match="belongs to another checkout"):
        _spawn(str(repo.wt))


def test_a_state_dir_open_to_others_is_narrowed_before_a_journal_is_written(tmp_path):
    repo, rel = _hostile_untracked_local(tmp_path)
    state = Path(os.environ[guard.STATE_DIR_ENV])
    state.mkdir()
    os.chmod(state, 0o755)
    with guard.window(str(repo.wt)):
        assert _mode(state) == 0o700
        assert _mode(guard._journal_path(str(repo.wt))) == 0o600


# ── round two: what the journal is the only copy of is never dropped ─────────────

@pytest.mark.parametrize("replacement", ["symlink", "directory", "deleted", "claude-removed"])
def test_an_untracked_file_a_spawn_replaces_is_saved_not_lost(tmp_path, replacement):
    """An untracked local file a spawn deletes, or replaces with a symlink or a
    directory, or whose ``.claude`` it removes: nothing is written into the
    checkout, no later spawn is refused, and the original is saved."""
    repo, rel = _hostile_untracked_local(tmp_path)
    path = repo.wt / rel
    before = path.read_bytes()
    with guard.window(str(repo.wt)):
        if replacement == "claude-removed":
            shutil.rmtree(repo.wt / ".claude")
        else:
            path.unlink()
            if replacement == "symlink":
                os.symlink(str(tmp_path / "elsewhere.json"), path)
            elif replacement == "directory":
                path.mkdir()
    assert _saved_bytes() == [before]
    assert not os.path.isfile(path) or os.path.islink(path)
    _spawn(str(repo.wt))                                  # the next spawn is not refused


def test_a_local_override_the_spawn_deletes_is_saved(tmp_path):
    """A tracked file with a local-only edit (the user's ``--skip-worktree``
    override): git does not hold those bytes, so a spawn deleting the file does
    not lose them — they are saved."""
    repo = make_pr_repo(tmp_path, {SETTINGS: {"model": "sonnet"}}, {})
    guard.install_base_resolver(lambda checkout: repo.base)
    git(repo.wt, "update-index", "--skip-worktree", SETTINGS)
    path = write(repo.wt, SETTINGS, {"model": "opus", "env": {"ANTHROPIC_API_KEY": "sk-local"}})
    before = path.read_bytes()
    with guard.window(str(repo.wt)):
        path.unlink()
    assert not path.exists() and _saved_bytes() == [before]
    assert git(repo.wt, "ls-files", "-v", SETTINGS).startswith("S ")


def test_a_widened_claude_dir_is_narrowed_before_a_local_secret_goes_back(tmp_path):
    """``.claude`` at 0o700 keeps an untracked 0o644 local file private. A spawn
    opens ``.claude`` to 0o755; the file goes back only into a directory no wider
    than it was."""
    repo, rel = _hostile_untracked_local(tmp_path)
    path = repo.wt / rel
    os.chmod(path, 0o644)
    os.chmod(repo.wt / ".claude", 0o700)
    before = path.read_bytes()
    with guard.window(str(repo.wt)):
        os.chmod(repo.wt / ".claude", 0o755)
    assert _mode(repo.wt / ".claude") == 0o700
    assert path.read_bytes() == before and _mode(path) == 0o644


def test_a_removed_claude_dir_saves_every_untracked_file_whole(tmp_path):
    repo = make_pr_repo(tmp_path, {".gitignore": ".claude/\n"}, {})
    one = write(repo.wt, SETTINGS, {"model": "opus", **hostile(tmp_path / "markers")})
    two = write(repo.wt, LOCAL_SETTINGS, {"theme": "dark", "env": {"K": "secret"}})
    os.chmod(two, 0o600)
    before = sorted([one.read_bytes(), two.read_bytes()])
    with guard.window(str(repo.wt)):
        shutil.rmtree(repo.wt / ".claude")
    assert sorted(_saved_bytes()) == before and not (repo.wt / ".claude").exists()


def test_a_held_local_secret_is_never_merged_into_a_file_git_now_tracks(tmp_path):
    """An untracked local file holding a secret is held back; during the window
    the spawn pulls a commit that now TRACKS that path. Nothing is merged into the
    tracked file (the next ``git add -A`` would publish the secret): it keeps the
    commit's bytes, and the user's original is saved."""
    repo, rel = _hostile_untracked_local(tmp_path)
    before = (repo.wt / rel).read_bytes()
    theirs = json.dumps({"model": "sonnet"}).encode()
    with guard.window(str(repo.wt)):
        (repo.wt / rel).write_bytes(theirs)
        git(repo.wt, "add", "-f", rel)
        git(repo.wt, "commit", "-qm", "track the local file")
    assert (repo.wt / rel).read_bytes() == theirs
    assert b"secret" not in (repo.wt / rel).read_bytes()
    assert _saved_bytes() == [before]


def test_a_lone_surrogate_from_a_pr_neither_crashes_nor_leaks(tmp_path, monkeypatch, capfd):
    """A PR's ``"\\ud800"`` escape in a kept value cannot be encoded back to
    UTF-8. It must neither end the run nor put settings bytes into a message."""
    raw = b'{"model": "\\ud800", "env": {"K": "secret"}}'
    repo = make_pr_repo(tmp_path, {}, {SETTINGS: raw})
    stub = ClaudeStub(real_run=subprocess.run)
    monkeypatch.setattr(subprocess, "run", stub)
    model_call.run_model_text("p", role="classifier", cwd=str(repo.wt))
    assert stub.spawns and stub.spawns[-1].hooks == [] and "env" not in stub.spawns[-1].keys
    assert repo.settings_bytes() == raw and repo.status() == ""
    assert "secret" not in capfd.readouterr().err


def test_a_corrupt_file_in_the_state_dir_never_breaks_loop_entry(tmp_path):
    repo, rel = _hostile_tracked(tmp_path)
    state = Path(os.environ[guard.STATE_DIR_ENV])
    state.mkdir(mode=0o700)
    (state / ("e" * 32 + ".json")).write_text("[" * 200000 + "]" * 200000)
    os.mkfifo(state / ("d" * 32 + ".json"))
    assert guard.recover(str(repo.wt)) is True
    assert _hooks(_spawn(str(repo.wt))) == []


def test_recovery_says_restored_only_for_what_it_restored(tmp_path, capfd):
    repo, rel = _hostile_tracked(tmp_path)
    _kill_mid_window(repo, tmp_path)
    (repo.wt / rel).unlink()                           # the user deletes it after the crash
    capfd.readouterr()
    assert guard.recover(str(repo.wt)) is True
    err = capfd.readouterr().err
    assert "after an interrupted claude run" not in err
    assert "the file was deleted while settings were held back from it" in err


def test_a_journal_is_never_replayed_into_a_different_tracked_file(tmp_path, capfd):
    """A window on one PR is killed; the worktree is then recreated at the same
    path for another branch. Recovery must not write the first PR's hooks into the
    second PR's settings file, where the next ``git add -A`` would commit them."""
    repo, rel = _hostile_tracked(tmp_path)
    _kill_mid_window(repo, tmp_path)
    git(repo.primary, "worktree", "remove", "--force", str(repo.wt))
    git(repo.primary, "branch", "-q", "other", "main")
    git(repo.primary, "worktree", "add", "-q", str(repo.wt), "other")
    write(repo.wt, SETTINGS, {"model": "haiku"})
    git(repo.wt, "add", "-A")
    git(repo.wt, "commit", "-qm", "other")
    theirs = repo.settings_bytes()
    assert guard.recover(str(repo.wt)) is True
    assert repo.settings_bytes() == theirs and repo.status() == ""
    assert "git now holds a different .claude/settings.json" in capfd.readouterr().err


def test_a_spawn_re_stating_a_key_of_a_git_held_file_wins_outright(tmp_path):
    """C6: when git holds the original, a re-stated held key is the spawn's —
    a fixer asked to drop a hook the PR added can do so."""
    markers = tmp_path / "markers"
    base = {"hooks": command_hook("PostToolUse", touch(markers, "lint"), "Edit")}
    pr = {"hooks": {**base["hooks"], **command_hook("SessionStart", touch(markers, "evil"))}}
    repo = make_pr_repo(tmp_path, {SETTINGS: base}, {SETTINGS: pr})
    guard.install_base_resolver(lambda checkout: repo.base)
    with guard.window(str(repo.wt)):
        write(repo.wt, SETTINGS, base)                      # the fixer restores base's hooks
    assert json.loads(repo.settings_bytes()) == base


def test_json_nested_past_the_limit_is_unparseable_at_any_stack_depth():
    def nested(n):
        return ("{\"a\": " + "[" * (n - 1) + "]" * (n - 1) + "}").encode()
    assert guard._parse(nested(guard._MAX_JSON_DEPTH)) is not None
    assert guard._parse(nested(guard._MAX_JSON_DEPTH + 1)) is None

    def deep(k):
        return guard._parse(nested(guard._MAX_JSON_DEPTH + 1)) if k == 0 else deep(k - 1)
    assert deep(900) is None                                # the same answer, deep in a stack


def test_non_path_variables_with_colons_stay_live():
    env = {"RUST_LOG": "my_crate::db=debug", "DISPLAY": ":0", "NO_PROXY": "localhost,::1"}
    assert guard.named_paths(env, key="env").unsafe is None
    assert guard.named_paths("RUST_LOG=app::db=debug cargo check").unsafe is None


def test_a_value_glued_to_a_cluster_of_short_options_is_named():
    assert "lib" in guard.named_paths("perl -wI./lib tools/x.pl").paths
    assert "lib" in guard.named_paths("perl -wIlib tools/x.pl").bare


_TERM_DURING_RESTORE = """
import os, signal, sys, time
from buddhi_review import claude_settings_guard as guard
real = guard._put_back
def slow(*a, **k):
    open(sys.argv[2], "w").write("restoring")
    time.sleep(1.0)
    return real(*a, **k)
guard._put_back = slow
with guard.window(sys.argv[1]):
    pass
sys.exit(3)
"""


def test_a_sigterm_during_the_restore_waits_for_it(tmp_path):
    repo, rel = _hostile_tracked(tmp_path)
    before, flags = repo.settings_bytes(), repo.flags()
    marker = tmp_path / "restoring"
    proc = subprocess.Popen([sys.executable, "-c", _TERM_DURING_RESTORE, str(repo.wt), str(marker)],
                            env=_child_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 30
    while not marker.exists():
        assert proc.poll() is None and time.monotonic() < deadline
        time.sleep(0.02)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 128 + signal.SIGTERM   # delivered once the restore was done
    assert repo.settings_bytes() == before and repo.flags() == flags
    assert not any(f.endswith(".json") for f in _state_files())


def test_a_window_leaves_sigterm_handling_and_the_signal_mask_as_it_found_them(tmp_path):
    repo, rel = _hostile_tracked(tmp_path)
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        _spawn(str(repo.wt))
        assert signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, set())
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    _spawn(str(repo.wt))
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


def test_loop_entry_sweeps_a_journal_temp_and_names_an_orphaned_journal(tmp_path, capfd):
    repo, rel = _hostile_tracked(tmp_path)
    state = Path(os.environ[guard.STATE_DIR_ENV])
    state.mkdir(mode=0o700)
    leftover = state / f".{guard._key(str(repo.wt))}.json.0123456789abcdef.guard-tmp"
    leftover.write_text("{a crashed journal write}")
    orphan = state / ("f" * 32 + ".json")
    orphan.write_text(json.dumps({"version": 1, "checkout": str(tmp_path / "gone")}))
    assert guard.recover(str(repo.wt)) is True
    assert not leftover.exists() and orphan.exists()
    assert f"{orphan} holds .claude settings held back from {tmp_path / 'gone'}" in \
        capfd.readouterr().err


def test_when_only_the_index_flag_was_owed_a_later_edit_is_left_alone(tmp_path, monkeypatch):
    """The content was restored but clearing the skip-worktree flag failed: the
    journal owes only the flag, so a later edit to the file is never re-merged."""
    repo, rel = _hostile_tracked(tmp_path)
    real = guard._update_index

    def flaky(checkout, flag, paths):
        if flag == "--no-skip-worktree":
            raise OSError("index.lock held")
        return real(checkout, flag, paths)

    monkeypatch.setattr(guard, "_update_index", flaky)
    _spawn(str(repo.wt))
    monkeypatch.setattr(guard, "_update_index", real)
    edited = write(repo.wt, SETTINGS, {"model": "opus"}).read_bytes()  # the user removes the hooks
    assert guard.recover(str(repo.wt)) is True
    assert repo.settings_bytes() == edited
    assert git(repo.wt, "ls-files", "-v", SETTINGS).startswith("H ")


# ── round three: contract clauses pinned by execution ────────────────────────────

def test_the_allowlist_is_exactly_the_audited_set():
    assert guard.INERT_KEYS == frozenset({
        "alwaysThinkingEnabled", "disabledMcpjsonServers",
        "includeCoAuthoredBy", "messageIdleNotifThresholdMs", "model", "preferredNotifChannel",
        "spinnerTipsEnabled", "syntaxHighlightingDisabled", "theme", "verbose",
    })


def test_the_journal_lives_in_the_durable_cache_dir_by_default(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.delenv(guard.STATE_DIR_ENV)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert guard.state_dir() == str(tmp_path / ".cache" / "buddhi" / "settings-guard")
    assert not guard.state_dir().startswith(tempfile.gettempdir())


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores directory permissions")
def test_a_journal_that_cannot_be_written_refuses_the_spawn(tmp_path, monkeypatch):
    repo, rel = _hostile_tracked(tmp_path)
    before = repo.settings_bytes()
    stub = ClaudeStub(real_run=subprocess.run)
    monkeypatch.setattr(subprocess, "run", stub)
    state = Path(os.environ[guard.STATE_DIR_ENV])
    state.mkdir(mode=0o700)
    (state / (guard._key(str(repo.wt)) + ".lock")).touch(mode=0o600)
    os.chmod(state, 0o500)
    try:
        with pytest.raises(guard.SettingsGuardRefusal, match="cannot record the original"):
            with guard.window(str(repo.wt)):
                stub(["claude", "-p", "x"], cwd=str(repo.wt), text=True)
    finally:
        os.chmod(state, 0o700)
    assert stub.spawns == [] and repo.settings_bytes() == before and repo.status() == ""


def test_a_scrub_that_does_not_read_back_refuses_the_spawn(tmp_path, monkeypatch):
    repo, rel = _hostile_tracked(tmp_path)
    before, flags = repo.settings_bytes(), repo.flags()
    real = guard._Dir.write

    def lost(self, name, data, mode, token, full=False):
        if name == "settings.json" and data != before:
            return  # a write that silently never lands
        return real(self, name, data, mode, token, full=full)

    monkeypatch.setattr(guard._Dir, "write", lost)
    with pytest.raises(guard.SettingsGuardRefusal, match="did not read back as written"):
        _spawn(str(repo.wt))
    assert repo.settings_bytes() == before and repo.flags() == flags


_NESTED_CHILD = """
import sys
from buddhi_review import claude_settings_guard as guard
from settings_guard_support import seen_settings
with guard.window(sys.argv[1]):
    with guard.window(sys.argv[1]):
        hooks = (seen_settings(sys.argv[1]).get(".claude/settings.json") or {}).get("hooks")
print("hooks:", sorted(hooks or {}))
"""


def test_a_nested_window_on_the_same_checkout_does_not_deadlock(tmp_path):
    """Run in a child with a deadline, so a window that blocks on its own lock
    fails this test instead of hanging the suite."""
    repo, rel = _hostile_tracked(tmp_path)
    before = repo.settings_bytes()
    env = _child_env()
    env["PYTHONPATH"] += os.pathsep + str(Path(__file__).parent)
    try:
        r = subprocess.run([sys.executable, "-c", _NESTED_CHILD, str(repo.wt)], env=env,
                           capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        pytest.fail("a nested window on the same checkout deadlocked")
    assert r.returncode == 0, r.stderr
    assert "hooks: []" in r.stdout
    assert repo.settings_bytes() == before


_HOLD = """
import json, sys, time
from buddhi_review import claude_settings_guard as guard
from settings_guard_support import seen_settings
with guard.window(sys.argv[1]):
    open(sys.argv[2], "w").write("ready")
    seen = []
    for _ in range(40):
        seen.append(sorted((seen_settings(sys.argv[1]).get(".claude/settings.json") or {}).get("hooks", {})))
        time.sleep(0.05)
    open(sys.argv[3], "w").write(json.dumps(seen))
"""


@pytest.mark.parametrize("second", ["recover", "window"])
def test_another_process_never_replays_a_live_window(tmp_path, second):
    """C9(i): while one process holds a window, another's loop-entry recovery or
    window waits for it — never mistaking the live journal for a crash, which would
    put the PR's hooks back under the running spawn."""
    repo, rel = _hostile_tracked(tmp_path)
    ready, log = tmp_path / "ready", tmp_path / "seen.json"
    env = _child_env()
    env["PYTHONPATH"] += os.pathsep + str(Path(__file__).parent)
    proc = subprocess.Popen([sys.executable, "-c", _HOLD, str(repo.wt), str(ready), str(log)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 30
    while not ready.exists():
        assert proc.poll() is None and time.monotonic() < deadline
        time.sleep(0.02)
    started = time.monotonic()
    if second == "recover":
        assert guard.recover(str(repo.wt)) is True
    else:
        _spawn(str(repo.wt))
    waited = time.monotonic() - started
    assert proc.wait(timeout=30) == 0
    assert all(hooks == [] for hooks in json.loads(log.read_text()))
    assert waited > 1.0                                    # it blocked until the window closed


def test_an_unexpected_error_during_the_restore_is_reported_not_raised(tmp_path, monkeypatch):
    repo, rel = _hostile_tracked(tmp_path)

    def broken(*a, **k):
        raise TypeError("a guard bug")

    monkeypatch.setattr(guard, "_put_back_file", broken)
    stub = ClaudeStub(real_run=subprocess.run, claude_stdout="answer")
    monkeypatch.setattr(subprocess, "run", stub)
    assert model_call.run_model_text("p", role="classifier", cwd=str(repo.wt)) == "answer"
    assert os.path.exists(guard._journal_path(str(repo.wt)))
    monkeypatch.undo()
    assert guard.recover(str(repo.wt)) is True             # the journal replays


@pytest.mark.skipif(not os.path.exists("/System/Library") and sys.platform != "darwin",
                    reason="needs a case-insensitive filesystem")
def test_a_case_variant_settings_path_is_marked_skip_worktree(tmp_path):
    repo = make_pr_repo(tmp_path, {}, {".Claude/Settings.json": hostile(tmp_path / "markers")})
    if not (repo.wt / ".claude" / "settings.json").exists():
        pytest.skip("the filesystem is case-sensitive")
    flags = repo.flags()
    with guard.window(str(repo.wt)):
        assert repo.status() == ""
        assert git(repo.wt, "ls-files", "-v", ".Claude/Settings.json").startswith("S ")
    assert repo.flags() == flags and repo.status() == ""


# ── A14: fail closed (C7) ────────────────────────────────────────────────────────

@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores directory permissions")
def test_a_read_only_claude_dir_refuses_every_spawn(tmp_path, monkeypatch, capfd):
    """A14: a hostile file in a read-only ``.claude/`` cannot be made inert, so no
    ``claude`` starts: the fixer escalates, naming the refusal, without retries;
    a model call fails to launch; the checkout is left as found."""
    repo, rel = _hostile_tracked(tmp_path)
    before, flags = repo.settings_bytes(), repo.flags()
    claude_dir = repo.wt / ".claude"
    os.chmod(claude_dir, 0o555)
    stub = ClaudeStub(real_run=subprocess.run)
    monkeypatch.setattr(subprocess, "run", stub)
    try:
        with pytest.raises(guard.SettingsGuardRefusal):
            _spawn(str(repo.wt))
        outcome = fix_apply.apply_fix("this null check is missing", cwd=str(repo.wt),
                                      model="sonnet", effort="low", retries=3)
        with pytest.raises(RuntimeError, match="failed to launch claude"):
            model_call.run_model_text("p", role="classifier", cwd=str(repo.wt))
        delivered = []
        adapter = SimpleNamespace(escalation=SimpleNamespace(
            delivered=delivered, notifier=SimpleNamespace(send=lambda ask: None)))
        result = CommentResult("c1", Classification(label="SUBSTANTIVE"), "decided", "fix")
        action = act_on_result(Comment(id="c1", text="fix it"), result, adapter=adapter,
                               fix_dispatch=lambda c, r: outcome)
    finally:
        os.chmod(claude_dir, 0o755)
    assert stub.spawns == []
    assert outcome.status == "transient-failed" and outcome.attempts == 1
    assert outcome.detail.startswith(f"fixer spawn failed: the .claude settings in {repo.wt} "
                                     f"could not be made safe: cannot rewrite {rel}")
    assert action.final == "escalated" and delivered[0].detail == outcome.detail
    assert "[settings-guard] not starting claude in" in capfd.readouterr().err
    assert repo.settings_bytes() == before and repo.flags() == flags and repo.status() == ""
    assert not any(f.endswith(".json") for f in _state_files())


def test_an_unreadable_journal_refuses_until_it_is_dealt_with(tmp_path, capfd):
    repo, rel = _hostile_tracked(tmp_path)
    journal = Path(guard._journal_path(str(repo.wt)))
    journal.parent.mkdir(parents=True, mode=0o700)
    journal.write_text("{not json")
    assert guard.recover(str(repo.wt)) is False
    with pytest.raises(guard.SettingsGuardRefusal, match="is unreadable"):
        _spawn(str(repo.wt))
    assert "claude will not start there until they are restored" in capfd.readouterr().err


# ── A15: a spawn that edits the settings file keeps its edit (C6) ─────────────────

def test_a_spawn_edit_is_kept_and_held_back_keys_come_back(tmp_path, capfd):
    """A15: the spawn changes ``model`` and deletes a base-trusted hook it could
    see; afterwards its ``model`` wins, the key the guard held back comes back, and
    the deleted visible hook stays deleted. Mode and index flags are exact."""
    markers = tmp_path / "markers"
    base = {"model": "sonnet", "hooks": command_hook("PostToolUse", touch(markers, "base"), "Edit")}
    pr = dict(base, statusLine={"type": "command", "command": touch(markers, "status")})
    repo = make_pr_repo(tmp_path, {SETTINGS: base}, {SETTINGS: pr})
    guard.install_base_resolver(lambda checkout: repo.base)
    path = repo.wt / SETTINGS
    os.chmod(path, 0o640)
    flags = repo.flags()
    with guard.window(str(repo.wt)):
        visible = json.loads(path.read_text())
        assert visible == base                               # statusLine held back
        write(repo.wt, SETTINGS, {"model": "opus"})          # the spawn's own edit
    assert json.loads(path.read_text()) == {"model": "opus", "statusLine": pr["statusLine"]}
    assert _mode(path) == 0o640 and repo.flags() == flags
    assert ("kept the edit made to this file while settings were held back, and put back "
            "statusLine") in capfd.readouterr().err
    assert markers_present(markers) == []


def test_a_spawn_restating_a_held_key_wins(tmp_path):
    markers = tmp_path / "markers"
    repo = make_pr_repo(tmp_path, {}, {SETTINGS: {"model": "sonnet", "env": {"A": "1"},
                                                  "apiKeyHelper": touch(markers, "k")}})
    path = repo.wt / SETTINGS
    with guard.window(str(repo.wt)):
        write(repo.wt, SETTINGS, {"env": {"A": "2"}})
    assert json.loads(path.read_text()) == {"env": {"A": "2"}, "apiKeyHelper": touch(markers, "k")}


@pytest.mark.parametrize("how", ["file", "claude-dir", "into-a-directory"])
def test_a_spawn_that_removes_the_settings_keeps_its_change_and_never_jams(tmp_path, capfd, how):
    """A spawn that deletes the settings file (or all of ``.claude/``), or puts a
    directory in its place, keeps that change: the held-back keys are not
    resurrected into a file its author removed — the next commit would carry them
    again — and the journal does not jam every later spawn."""
    repo, rel = _hostile_tracked(tmp_path)
    path = repo.wt / rel
    with guard.window(str(repo.wt)):
        if how == "file":
            path.unlink()
        elif how == "claude-dir":
            shutil.rmtree(repo.wt / ".claude")
        else:
            path.unlink()
            path.mkdir()
    assert not path.is_file()
    assert "they (hooks) were not put back" in capfd.readouterr().err
    assert not any(f.endswith(".json") for f in _state_files())
    view = _spawn(str(repo.wt))                           # the next spawn is not refused
    assert _hooks(view) == []
    assert markers_present(repo.markers) == []


@pytest.mark.parametrize("original", [b'["not", "an", "object"]\n', b'{"hooks": {"SessionStart": '])
def test_a_non_object_original_wins_and_the_dropped_edit_is_announced(tmp_path, capfd, original):
    """A15, the non-object case: the guard writes ``{}``; a spawn that edits it
    cannot be merged into the PR's original, so the PR's bytes are restored and the
    drop is announced. Without an edit the restore is byte-identical."""
    repo = make_pr_repo(tmp_path, {}, {SETTINGS: original})
    path = repo.wt / SETTINGS
    with guard.window(str(repo.wt)):
        assert json.loads(path.read_text()) == {}
    assert path.read_bytes() == original
    capfd.readouterr()
    with guard.window(str(repo.wt)):
        write(repo.wt, SETTINGS, {"model": "opus"})
    assert path.read_bytes() == original and repo.status() == ""
    assert "could not be merged with the original" in capfd.readouterr().err


# ── parsing: what counts as one JSON object ──────────────────────────────────────

@pytest.mark.parametrize("raw,keeps", [
    (b'\xef\xbb\xbf{"model": "opus"}', {"model": "opus"}),                  # BOM tolerated
    (b'{"model": "opus", "model": "haiku"}', {}),                            # duplicate key
    (b'{"model": NaN}', {}),                                                 # not JSON
    (b"   \n", None),                                                        # blank: nothing to hold
])
def test_strict_parsing(tmp_path, raw, keeps):
    repo = make_pr_repo(tmp_path, {}, {SETTINGS: raw})
    view = _spawn(str(repo.wt))
    if keeps is None:
        assert (repo.wt / SETTINGS).read_bytes() == raw and view[SETTINGS] is None
    else:
        assert view[SETTINGS] == keeps
    assert repo.settings_bytes() == raw


def test_disable_all_hooks_must_match_base(tmp_path):
    """``disableAllHooks`` is not allowlisted: a PR-supplied ``true`` would switch
    off the base-trusted hooks the guard keeps live."""
    base = {"hooks": command_hook("SessionStart", touch(tmp_path / "markers", "b"))}
    repo = make_pr_repo(tmp_path, {SETTINGS: base}, {SETTINGS: dict(base, disableAllHooks=True)})
    guard.install_base_resolver(lambda checkout: repo.base)
    view = _spawn(str(repo.wt))
    assert "disableAllHooks" not in view[SETTINGS] and _hooks(view) == ["SessionStart"]


def test_the_containment_survives_a_reload_of_the_guard(tmp_path):
    import importlib
    state = os.environ[guard.STATE_DIR_ENV]
    reloaded = importlib.reload(guard)
    assert reloaded.state_dir() == state
    assert reloaded._checkout(None) == os.path.abspath(os.environ[guard.FALLBACK_CWD_ENV])


# ── round four: where a cd leaves the shell, code that runs a shell, patterns ────

@pytest.mark.parametrize("command", [
    "(cd sub && true) && sh scripts/check.sh",       # the subshell's cd is undone
    "pushd sub && popd && sh scripts/check.sh",
    "cd sub; cd -; sh scripts/check.sh",
    "cd missing; sh scripts/check.sh",               # a cd that fails
    "cd sub || true; sh scripts/check.sh",
    "cd /tmp; sh scripts/check.sh",
    "cd sub >/dev/null && sh scripts/check.sh",      # a redirect is not where cd goes
    "cd sub 2>/dev/null && sh scripts/check.sh",
    "cd sub && true; cd - >/dev/null && sh scripts/check.sh",
    "true | cd sub && sh scripts/check.sh",          # cd in a pipeline changes nothing
    "env cd sub && sh scripts/check.sh",
    "! cd missing && sh scripts/check.sh",
    "pushd -n sub && sh scripts/check.sh",
    'cd "" && sh scripts/check.sh',
])
def test_a_path_is_checked_wherever_a_cd_may_have_left_the_shell(tmp_path, command):
    """A ``cd`` only adds places to check. However the shell reads it — undone by a
    subshell, a ``popd`` or a ``cd -``, failed, or never run in this shell — a
    relative path is still checked from the checkout root."""
    repo = _hook_repo(tmp_path, command, {"scripts/check.sh": "true\n", "sub/keep.txt": "x\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    write(repo.wt, "scripts/check.sh", "echo changed\n")
    assert _hooks(_spawn(str(repo.wt))) == []


@pytest.mark.parametrize("command", [
    "cd sub && sh ../scripts/check.sh",               # climbs out of a cd directory
    "cd sub >/dev/null && sh ../scripts/check.sh",
    "mkdir -p build && cd build && ../configure",     # a directory the hook creates
    "cd link && cd ../c && sh x.sh",                  # cd reads .. by name
    "cd sub && sh .*/x.sh",                           # .* can match ..
    "echo sub | xargs -I{} sh -c 'cd {} && sh run.sh'",
    'cd "$SOMEWHERE" && sh run.sh',
    "cd - && sh run.sh",                              # the directory before the hook ran
    "cd -- -dir && sh run.sh",
])
def test_a_climb_after_a_cd_or_a_cd_nobody_can_follow_is_unsafe(command):
    assert guard.named_paths(command).unsafe, command


def test_a_cd_above_the_checkout_is_unsafe(tmp_path):
    checkout = tmp_path / "wt"
    checkout.mkdir()
    assert guard.named_paths(f"cd {tmp_path} && sh wt/x.sh", str(checkout)).unsafe
    assert guard.named_paths("cd / && sh x.sh", str(checkout)).unsafe
    assert guard.named_paths("cd /tmp && sh x.sh", str(checkout)).unsafe is None


def test_cd_reads_dotdot_by_name_so_the_named_directory_is_walked(tmp_path):
    """``cd link/../c`` enters ``c`` whatever ``link`` points at, so ``c`` is what
    must be unchanged — not the directory the symlink's parent holds."""
    repo = _hook_repo(tmp_path, "cd link/../c && sh x.sh",
                      {"a/b/keep.txt": "x\n", "a/c/x.sh": "true\n", "c/x.sh": "true\n"})
    os.symlink("a/b", repo.wt / "link")
    git(repo.wt, "add", "link")
    git(repo.wt, "commit", "-qm", "link")
    guard.install_base_resolver(lambda checkout: git(repo.wt, "rev-parse", "HEAD").strip())
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    write(repo.wt, "c/x.sh", "echo changed\n")
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_path_climbing_out_of_git_is_never_exempt(tmp_path):
    repo = _hook_repo(tmp_path, "sh .git/../x.sh", {"x.sh": "true\n"})
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_directory_literal_in_interpreter_code_is_walked_not_opened(tmp_path):
    command = ("python3 -c \"import sys; p = sys.argv[-1]; "
               "sys.exit(2 if p.startswith('docs/') else 0)\"")
    repo = _hook_repo(tmp_path, command, {"docs/guide.md": "x\n"}, {"README.md": "changed\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    write(repo.wt, "docs/new.md", "y\n")
    assert _hooks(_spawn(str(repo.wt))) == []


@pytest.mark.parametrize("command", [
    "python3 -c \"import os; os.system('sh tools/x.sh')\"",
    "perl -e 'system(\"sh tools/x.sh\")'",
    "node -e \"require('child_process').execSync('sh tools/x.sh')\"",
])
def test_a_command_line_inside_interpreter_code_is_checked(tmp_path, command):
    repo = _hook_repo(tmp_path, command, {"tools/x.sh": "true\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    write(repo.wt, "tools/x.sh", "echo changed\n")
    assert _hooks(_spawn(str(repo.wt))) == []


@pytest.mark.parametrize("command", [
    'case "$1" in $(sh scripts/check.sh)) true ;; esac',
    'case "$1" in `sh scripts/check.sh`) true ;; esac',
    '[[ "$1" == $(sh scripts/check.sh) ]] && true',
])
def test_a_command_substitution_in_a_pattern_is_unsafe(command):
    """A pattern is matched, not opened, but the shell expands it first: a command
    substitution in it runs."""
    assert guard.named_paths(command).unsafe


def test_a_multi_line_case_reads_its_patterns_as_patterns():
    named = guard.named_paths('case "$f" in\n  docs) make html ;;\n  *) true ;;\nesac')
    assert named.unsafe is None
    assert "esac" not in named.bare and "docs" not in named.bare


# ── round four: the index moves under a held file ─────────────────────────────────

def test_a_spawn_untracking_the_settings_leaves_the_original_not_the_scrub(tmp_path):
    """A spawn runs ``git rm --sparse --cached`` on the PR's settings file. What the
    guard wrote must not stay behind for the next ``git add -A`` to commit: the
    file gets its original bytes back, which git still has."""
    repo, rel = _hostile_tracked(tmp_path)
    before = repo.settings_bytes()
    with guard.window(str(repo.wt)):
        git(repo.wt, "rm", "-q", "--sparse", "--cached", rel)
    assert repo.settings_bytes() == before
    git(repo.wt, "add", "-A")
    assert git(repo.wt, "diff", "--cached", "--stat") == ""
    assert _saved_copies() == []


def test_a_local_edit_a_spawn_untracks_gets_its_original_back(tmp_path):
    """A tracked file with an unstaged local edit; a spawn runs ``git rm --sparse
    --cached``. The file is left as it was on disk before — never the guard's
    scrub, which the next ``git add -A`` would stage."""
    repo = make_pr_repo(tmp_path, {SETTINGS: {"model": "sonnet"}}, {})
    guard.install_base_resolver(lambda checkout: repo.base)
    path = write(repo.wt, SETTINGS, '{\n    "model": "sonnet",\n    "env": {"K": "local"}\n}\n')
    before = path.read_bytes()
    with guard.window(str(repo.wt)):
        git(repo.wt, "rm", "-q", "--sparse", "--cached", SETTINGS)
    assert path.read_bytes() == before


def test_a_staged_original_a_spawn_unstages_is_saved(tmp_path, capfd):
    """The user staged an edit to the settings file; a spawn runs ``git reset``.
    The staged bytes were the only copy: the file now matches what git holds, and
    the staged original is saved and named."""
    committed = '{\n    "model": "sonnet"\n}\n'           # not how the guard would write it
    repo = make_pr_repo(tmp_path, {SETTINGS: committed}, {})
    guard.install_base_resolver(lambda checkout: repo.base)
    path = write(repo.wt, SETTINGS, {"model": "sonnet", "env": {"K": "staged-secret"}})
    git(repo.wt, "add", SETTINGS)
    before = path.read_bytes()
    with guard.window(str(repo.wt)):
        git(repo.wt, "reset", "-q")
    assert path.read_text() == committed and repo.status() == ""
    assert _saved_bytes() == [before]
    assert "so the file now matches it; its earlier bytes were saved to" in capfd.readouterr().err


# ── round four: restore edge cases ───────────────────────────────────────────────

@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores directory permissions")
def test_a_journal_that_cannot_be_removed_never_fails_a_finished_spawn(tmp_path, capfd):
    repo, rel = _hostile_tracked(tmp_path)
    before, flags = repo.settings_bytes(), repo.flags()
    state = Path(os.environ[guard.STATE_DIR_ENV])
    try:
        with guard.window(str(repo.wt)):
            os.chmod(state, 0o500)
    finally:
        os.chmod(state, 0o700)
    assert repo.settings_bytes() == before and repo.flags() == flags
    assert "could not be removed" in capfd.readouterr().err
    assert _hooks(_spawn(str(repo.wt))) == []                # the replay changes nothing
    assert repo.settings_bytes() == before and repo.status() == ""
    assert not os.path.exists(guard._journal_path(str(repo.wt)))


def test_a_rollback_names_the_original_it_saved(tmp_path, monkeypatch, capfd):
    """Another writer lands its bytes between the scrub's write and its read-back:
    the spawn is refused, and the local original it replaced is saved and named."""
    repo, rel = _hostile_untracked_local(tmp_path)
    before = (repo.wt / rel).read_bytes()
    theirs = b'{"permissions": {"allow": ["Bash(npm test)"]'      # not one JSON object
    real = guard._Dir.write

    def racing(self, name, data, mode, token, full=False):
        if name == "settings.local.json" and data != before:
            data = theirs
        return real(self, name, data, mode, token, full=full)

    monkeypatch.setattr(guard._Dir, "write", racing)
    with pytest.raises(guard.SettingsGuardRefusal, match="did not read back as written"):
        _spawn(str(repo.wt))
    monkeypatch.setattr(guard._Dir, "write", real)
    assert _saved_bytes() == [before]
    saved = Path(os.environ[guard.STATE_DIR_ENV]) / _saved_copies()[0]
    assert str(saved) in capfd.readouterr().err


def test_a_saved_copy_temp_left_by_a_kill_is_swept(tmp_path):
    """A kill while an original was being saved leaves a temp in the state dir
    under the journal's token; the replay must sweep it rather than collide with
    it and jam the checkout."""
    repo, rel = _hostile_untracked_local(tmp_path)
    before = (repo.wt / rel).read_bytes()
    journal = _kill_mid_window(repo, tmp_path)
    write(repo.wt, rel, "not json\n")                        # the killed spawn's edit
    token = json.loads(Path(journal).read_text())["token"]
    state = Path(os.environ[guard.STATE_DIR_ENV])
    for kind in (*guard.SETTINGS_FILES, guard._DIR_LINK):
        name = f"{guard._key(str(repo.wt))}.{token}.{kind}"
        (state / guard._tmp_name(name, token)).write_bytes(b"partial")
    assert guard.recover(str(repo.wt)) is True
    assert _saved_bytes() == [before]
    assert not [f for f in _state_files() if f.endswith(guard._TMP_SUFFIX)]
    _spawn(str(repo.wt))                                      # the next spawn is not refused


def test_a_hard_link_made_during_the_window_is_replaced_not_chmodded(tmp_path):
    repo, rel = _hostile_tracked(tmp_path)
    path = repo.wt / rel
    before, mode = path.read_bytes(), _mode(path)
    outside = tmp_path / "outside.json"
    outside.write_bytes(before)
    os.chmod(outside, 0o600)
    with guard.window(str(repo.wt)):
        path.unlink()
        os.link(outside, path)
    assert _mode(outside) == 0o600
    assert path.read_bytes() == before and _mode(path) == mode
    assert os.stat(path).st_ino != os.stat(outside).st_ino


def test_a_spawn_edit_that_is_not_json_is_kept_when_git_holds_the_original(tmp_path, capfd):
    """C6: the spawn's edit is kept. When it is not one JSON object there is
    nothing to merge the held keys into; they stay out (git still has them)."""
    repo, rel = _hostile_tracked(tmp_path)
    edit = b'{"model": "opus",}\n'
    with guard.window(str(repo.wt)):
        write(repo.wt, rel, edit)
    assert repo.settings_bytes() == edit
    assert ("kept the edit made to this file while settings were held back; it is not one "
            "JSON object, so hooks were not put back") in capfd.readouterr().err
    assert markers_present(repo.markers) == []


# ── round six: what an interpreter imports and runs ──────────────────────────────

_JSON_HOOK = ("python3 -c \"import json,sys; p=json.load(sys.stdin)['tool_input']['file_path']; "
              "sys.exit(2 if p.endswith('.lock') else 0)\"")


@pytest.mark.parametrize("command", [
    _JSON_HOOK,
    "python3 - <<'PY'\nimport json\nPY",
    "python3 <<< 'import json'",
    "python3 -m json.tool",
])
def test_a_module_the_pr_adds_at_the_root_holds_back_a_python_program_run_there(tmp_path, command):
    """``-c``, ``-m`` and a program on stdin put the checkout root first on Python's
    path: a ``json.py`` the PR adds there replaces the standard library's."""
    repo = _hook_repo(tmp_path, command, {}, {"README.md": "changed\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    write(repo.wt, "json.py", "x = 1\n")
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_package_python_code_imports_by_name_is_walked(tmp_path):
    repo = _hook_repo(tmp_path, "python3 -c 'from tools.lint import main; main()'",
                      {"tools/lint.py": "def main(): pass\n"}, {"README.md": "changed\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    write(repo.wt, "tools/lint.py", "def main(): print(1)\n")
    assert _hooks(_spawn(str(repo.wt))) == []


def test_an_isolated_python_program_does_not_import_from_the_root(tmp_path):
    repo = _hook_repo(tmp_path, 'python3 -I -c "import json"', {}, {"json.py": "x = 1\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]


_HOOKS = '"$CLAUDE_PROJECT_DIR"/.claude/hooks'
_PY = {".claude/hooks/check.py": "#!/usr/bin/env python3\nimport helper\n"}


@pytest.mark.parametrize("command,files", [
    (f"timeout 30 {_HOOKS}/check.py", _PY),                 # run through a wrapper
    (f"nice -n 5 {_HOOKS}/check.py", _PY),
    (f"env -u NOT_SET {_HOOKS}/check.py", _PY),
    (f"ruby -W {_HOOKS}/check.rb", {".claude/hooks/check.rb": "require_relative 'helper'\n"}),
    (f"node --disable-warning ExperimentalWarning {_HOOKS}/check.js",
     {".claude/hooks/check.js": "require('./helper')\n"}),
    (f"perl -p {_HOOKS}/check.pl", {".claude/hooks/check.pl": "use FindBin;\n"}),
    ("node -e \"require('./.claude/hooks/check.js')\"", {".claude/hooks/check.js": "require('./helper')\n"}),
    ("node -r./.claude/hooks/check.js -e '0'", {".claude/hooks/check.js": "require('./helper')\n"}),
    ("node -r ./.claude/hooks/check.js \"$CLAUDE_PROJECT_DIR\"/tools/run.js",
     {".claude/hooks/check.js": "require('./helper')\n", "tools/run.js": "1\n"}),
])
def test_a_script_an_interpreter_may_run_names_its_directory(tmp_path, command, files):
    """Whatever the options around it, a script an interpreter may run (or a module
    it preloads) imports from its own directory: a sibling the PR changes holds
    the hook back."""
    repo = _hook_repo(tmp_path, command, {**files, ".claude/hooks/helper.txt": "1\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    write(repo.wt, ".claude/hooks/helper.txt", "2\n")
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_here_string_program_is_read_as_a_command_line(tmp_path):
    repo = _hook_repo(tmp_path, "bash <<< 'cd .claude/hooks && ./check.sh'",
                      {".claude/hooks/check.sh": "true\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    write(repo.wt, ".claude/hooks/check.sh", "echo changed\n")
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_module_node_options_preloads_names_its_directory(tmp_path):
    base = {SETTINGS: {"env": {"NODE_OPTIONS": "--require ./.claude/hooks/setup.js"}},
            ".claude/hooks/setup.js": "require('./helper')\n", ".claude/hooks/helper.js": "1\n"}
    repo = make_pr_repo(tmp_path, base, {"docs/guide.md": "x\n"})
    guard.install_base_resolver(lambda checkout: repo.base)
    assert "env" in _spawn(str(repo.wt))[SETTINGS]
    write(repo.wt, ".claude/hooks/helper.js", "2\n")
    assert "env" not in _spawn(str(repo.wt))[SETTINGS]


# ── a run's edit to a file only the journal holds is merged too ──────────────────

def _local_override(tmp_path, kind):
    """The user's own settings, which only the journal will hold: (i) an untracked
    0o600 ``settings.local.json``; (ii) a tracked ``settings.json`` with a local edit
    the user keeps out of git with ``--skip-worktree``. Each holds the user's
    ``model``, an ``env`` entry and a hook the base lacks."""
    markers = tmp_path / "markers"
    mine = {"model": "sonnet", "env": {"ANTHROPIC_API_KEY": "sk-local"},
            "hooks": command_hook("SessionStart", touch(markers, "mine"))}
    if kind == "untracked-local":
        repo = make_pr_repo(tmp_path, {".gitignore": ".claude/settings.local.json\n"}, {})
        rel = LOCAL_SETTINGS
        os.chmod(write(repo.wt, rel, mine), 0o600)
    else:
        repo = make_pr_repo(tmp_path, {SETTINGS: {"model": "sonnet"}}, {})
        rel = SETTINGS
        git(repo.wt, "update-index", "--skip-worktree", rel)
        write(repo.wt, rel, mine)
    guard.install_base_resolver(lambda checkout: repo.base)
    return repo, rel, mine


@pytest.mark.parametrize("route", ["window", "recover-after-kill"])
@pytest.mark.parametrize("kind", ["untracked-local", "skip-worktree-override"])
def test_a_run_edit_to_the_users_own_settings_gets_their_held_keys_back(tmp_path, capfd, kind, route):
    """A15 for a file whose original only the journal holds: the run changes
    ``model``; afterwards the file holds the run's ``model`` plus the user's ``env``
    and hook, at its own mode, with the user's ``--skip-worktree`` bit still set
    and ``git status`` clean — through a real window, and through ``recover()``
    after a window killed right after the run's edit."""
    repo, rel, mine = _local_override(tmp_path, kind)
    path = repo.wt / rel
    mode, flags = _mode(path), repo.flags()
    edit = dict(mine, model="opus")
    if route == "window":
        with guard.window(str(repo.wt)):
            assert _hooks(seen_settings(str(repo.wt))) == []
            write(repo.wt, rel, {"model": "opus"})
    else:
        _kill_mid_window(repo, tmp_path, edit=(rel, json.dumps({"model": "opus"}).encode()))
        assert guard.recover(str(repo.wt)) is True
    assert json.loads(path.read_text()) == edit
    assert _mode(path) == mode and repo.flags() == flags and repo.status() == ""
    if kind == "skip-worktree-override":
        assert git(repo.wt, "ls-files", "-v", rel).startswith("S ")
    else:
        assert mode == 0o600
    assert _saved_copies() == [] and not any(f.endswith(".json") for f in _state_files())
    assert "put back env, hooks" in capfd.readouterr().err
    assert markers_present(repo.markers) == []


@pytest.mark.parametrize("route", ["window", "recover-after-kill"])
@pytest.mark.parametrize("bytes_left", [b"not json\n", b'["not", "an", "object"]\n'],
                         ids=["invalid-json", "not-an-object"])
def test_a_run_that_leaves_no_json_object_in_a_local_file_keeps_its_bytes(
        tmp_path, capfd, bytes_left, route):
    """Nothing to merge into: the run's bytes stay, and the original — which
    nothing but the journal holds — is saved, owner-only, and named."""
    repo, rel = _hostile_untracked_local(tmp_path)
    path = repo.wt / rel
    before = path.read_bytes()
    if route == "window":
        with guard.window(str(repo.wt)):
            path.write_bytes(bytes_left)
    else:
        _kill_mid_window(repo, tmp_path, edit=(rel, bytes_left))
        assert guard.recover(str(repo.wt)) is True
    assert path.read_bytes() == bytes_left
    assert _saved_bytes() == [before]
    saved = Path(os.environ[guard.STATE_DIR_ENV]) / _saved_copies()[0]
    assert _mode(saved) == 0o600 and str(saved) in capfd.readouterr().err
    assert markers_present(repo.markers) == []


def test_a_local_original_that_is_not_one_object_wins_over_a_run_edit(tmp_path, capfd):
    """C6's own exception holds for a file only the journal has: an original that
    is not one JSON object has nothing to merge into, so its bytes come back, at
    its mode, and the dropped edit is announced."""
    repo = make_pr_repo(tmp_path, {".gitignore": ".claude/settings.local.json\n"}, {})
    original = b'["the", "user", "keeps", "a", "list"]\n'
    path = write(repo.wt, LOCAL_SETTINGS, original)
    os.chmod(path, 0o600)
    with guard.window(str(repo.wt)):
        write(repo.wt, LOCAL_SETTINGS, {"model": "opus"})
    assert path.read_bytes() == original and _mode(path) == 0o600
    assert "could not be merged with the original" in capfd.readouterr().err


# ── a pull request cannot change how long Claude Code keeps the user's history ──

@pytest.mark.parametrize("pr_value,live", [(1, False), (30, True)], ids=["changed", "as-base"])
def test_a_pr_cannot_change_how_long_claude_keeps_history(tmp_path, pr_value, live):
    """``cleanupPeriodDays`` drives a sweep that deletes transcripts and other
    history under ``~/.claude``: a value the PR changed is held back; the base's
    own value stays."""
    repo = make_pr_repo(tmp_path, {SETTINGS: {"model": "sonnet", "cleanupPeriodDays": 30}},
                        {SETTINGS: {"model": "sonnet", "cleanupPeriodDays": pr_value}})
    guard.install_base_resolver(lambda checkout: repo.base)
    view = _spawn(str(repo.wt))
    assert ("cleanupPeriodDays" in view[SETTINGS]) is live
    assert view[SETTINGS]["model"] == "sonnet"


# ── every hold-back and refusal decision has a test ───────────────────────────────

def test_a_hook_the_pr_changed_is_held_back_even_when_it_names_no_file(tmp_path, capfd):
    markers = tmp_path / "markers"
    repo = make_pr_repo(tmp_path, {SETTINGS: {"hooks": command_hook("SessionStart", touch(markers, "base"))}},
                        {SETTINGS: {"hooks": command_hook("SessionStart", touch(markers, "pr"))}})
    guard.install_base_resolver(lambda checkout: repo.base)
    assert _hooks(_spawn(str(repo.wt))) == []
    assert "hooks (it differs from the base branch)" in capfd.readouterr().err
    assert markers_present(markers) == []


def test_an_unexpected_error_while_checking_refuses_the_spawn(tmp_path, monkeypatch):
    repo, rel = _hostile_tracked(tmp_path)
    before = repo.settings_bytes()

    def broken(data):
        raise IndexError("a guard bug")

    monkeypatch.setattr(guard, "_parse", broken)
    stub = ClaudeStub(real_run=subprocess.run)
    monkeypatch.setattr(subprocess, "run", stub)
    with pytest.raises(RuntimeError, match=r"could not be checked \(an unexpected IndexError\)"):
        model_call.run_model_text("p", role="classifier", cwd=str(repo.wt))
    assert stub.spawns == [] and repo.settings_bytes() == before and repo.status() == ""
    assert markers_present(repo.markers) == []


@pytest.mark.parametrize("failure", ["no-directory-descriptors", "unreadable-settings"])
def test_settings_that_cannot_be_read_refuse_the_spawn(tmp_path, monkeypatch, failure):
    """A platform without directory descriptors (native Windows) or a settings
    file the guard cannot read: the spawn does not happen."""
    repo, rel = _hostile_tracked(tmp_path)
    if failure == "no-directory-descriptors":
        monkeypatch.setattr(os, "supports_dir_fd", {f for f in os.supports_dir_fd if f is not os.open})
    else:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root reads any file")
        os.chmod(repo.wt / rel, 0o000)
    stub = ClaudeStub(real_run=subprocess.run)
    monkeypatch.setattr(subprocess, "run", stub)
    try:
        with pytest.raises(RuntimeError, match="cannot read its .claude settings"):
            model_call.run_model_text("p", role="classifier", cwd=str(repo.wt))
    finally:
        os.chmod(repo.wt / rel, 0o644)
    assert stub.spawns == [] and markers_present(repo.markers) == []


def test_a_root_script_run_by_bare_name_is_held_back_when_the_pr_adds_a_module(tmp_path, capfd):
    """``python3 check.py`` imports from the checkout root: a ``json.py`` the PR
    adds there would be imported first."""
    repo = _hook_repo(tmp_path, "python3 check.py", {"check.py": "import json\n"},
                      head={"json.py": "x = 1\n"})
    assert _hooks(_spawn(str(repo.wt))) == []
    assert "its interpreter imports from the checkout root" in capfd.readouterr().err


def test_a_symlink_base_does_not_have_is_never_followed(tmp_path):
    """Base holds ``tools/check.py`` as a regular file; the PR swaps it for a
    symlink to ``tools/deploy.py``, itself unchanged. Base has no such link, so the
    link is not followed and the hook is held back."""
    repo = _hook_repo(tmp_path, 'python3 "$CLAUDE_PROJECT_DIR"/tools/check.py',
                      {"tools/check.py": "print('check')\n", "tools/deploy.py": "print('deploy')\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    (repo.wt / "tools/check.py").unlink()
    os.symlink("deploy.py", repo.wt / "tools/check.py")
    git(repo.wt, "add", "-A")
    git(repo.wt, "commit", "-qm", "check is deploy now")
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_journal_is_replayed_even_when_claude_dir_is_gone(tmp_path, capfd):
    """The no-``.claude`` fast path still notices a journal an interrupted window
    left: the next guarded spawn replays it, so the user's local original is saved
    and named rather than left only in the journal."""
    repo, rel = _hostile_untracked_local(tmp_path)
    before = (repo.wt / rel).read_bytes()
    journal = _kill_mid_window(repo, tmp_path)
    shutil.rmtree(repo.wt / ".claude")
    capfd.readouterr()
    _spawn(str(repo.wt))
    assert not os.path.exists(journal)
    assert _saved_bytes() == [before]
    assert "its original, which exists nowhere else, was saved to" in capfd.readouterr().err


# ── a journal that fails its integrity checks is never replayed ──────────────────

def _tamper(record, how):
    entry = record["entries"][0]
    if how == "not-an-object":
        return [record]
    if how == "another-format-version":
        record["version"] = 99
    elif how == "token-not-sixteen-hex-digits":
        record["token"] = "../../../../escape"
    elif how == "entries-not-a-list":
        record["entries"] = {}
    elif how == "one-path-twice":
        record["entries"] = [entry, dict(entry)]
    elif how == "entry-not-an-object":
        record["entries"] = ["settings.local.json"]
    elif how == "kind-not-a-settings-file":
        entry["kind"] = "../escape.json"
    elif how == "flag-not-a-boolean":
        entry["link"] = "false"
    elif how == "mode-out-of-range":
        entry["mode"] = -1
    elif how == "index-blob-not-text":
        entry["index_sha"] = None
    elif how == "index-paths-not-a-list":
        entry["index_set"] = ".claude/settings.local.json"
    elif how == "claude-dir-entry-not-a-link":
        entry.update(kind="claude-dir", link=False)
    elif how == "original-not-strict-base64":
        entry["original"] += "!"            # a lenient decoder would skip the stray character
    return record


@pytest.mark.parametrize("how", [
    "not-an-object", "another-format-version", "token-not-sixteen-hex-digits", "entries-not-a-list",
    "one-path-twice", "entry-not-an-object", "kind-not-a-settings-file", "flag-not-a-boolean",
    "mode-out-of-range", "index-blob-not-text", "index-paths-not-a-list",
    "claude-dir-entry-not-a-link", "original-not-strict-base64", "nested-past-the-parser",
])
def test_a_journal_that_fails_its_checks_is_never_replayed(tmp_path, capfd, how):
    """A journal is replayed only when every field is what this guard writes: a
    malformed or tampered one is reported, kept for a hand check, and every spawn
    there is refused — nothing is written from it into the checkout."""
    repo, rel = _hostile_untracked_local(tmp_path)
    journal = Path(_kill_mid_window(repo, tmp_path))
    scrubbed = (repo.wt / rel).read_bytes()
    if how == "nested-past-the-parser":
        journal.write_text("[" * 100_000 + "]" * 100_000)
    else:
        journal.write_text(json.dumps(_tamper(json.loads(journal.read_text()), how)))
    tampered = journal.read_bytes()
    capfd.readouterr()
    assert guard.recover(str(repo.wt)) is False
    assert "is unreadable" in capfd.readouterr().err
    with pytest.raises(guard.SettingsGuardRefusal, match="is unreadable"):
        _spawn(str(repo.wt))
    assert (repo.wt / rel).read_bytes() == scrubbed and journal.read_bytes() == tampered
    assert not (tmp_path / "escape.json").exists() and not (repo.wt / "escape.json").exists()


# ── the lock, the state dir and a window inside a window ─────────────────────────

def test_a_lock_that_cannot_be_taken_refuses_the_spawn(tmp_path, monkeypatch, capfd):
    import fcntl
    repo, rel = _hostile_untracked_local(tmp_path)
    journal = _kill_mid_window(repo, tmp_path)

    def no_lock(fd, op):
        raise OSError(37, "No locks available")

    monkeypatch.setattr(fcntl, "flock", no_lock)
    assert guard.recover(str(repo.wt)) is False
    assert "cannot lock the checkout" in capfd.readouterr().err
    with pytest.raises(guard.SettingsGuardRefusal, match="cannot lock the checkout"):
        _spawn(str(repo.wt))
    assert os.path.exists(journal)


@pytest.mark.parametrize("state", ["a-symlink", "another-users"])
def test_a_state_dir_that_is_not_the_users_own_refuses_the_spawn(tmp_path, monkeypatch, state):
    """The journal can hold a local secret: its directory must be a real directory
    this user owns, or no spawn starts."""
    repo, rel = _hostile_untracked_local(tmp_path)
    path = Path(os.environ[guard.STATE_DIR_ENV])
    if state == "a-symlink":
        real = tmp_path / "elsewhere"
        real.mkdir(mode=0o700)
        os.symlink(real, path)
        reason = "is not a directory"
    else:
        path.mkdir(mode=0o700)
        uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: uid + 1)
        reason = "belongs to another user"
    with pytest.raises(guard.SettingsGuardRefusal, match=reason):
        _spawn(str(repo.wt))


def test_a_window_inside_a_live_window_never_replays_its_journal(tmp_path):
    """A nested window, or a loop-entry recovery, on a checkout whose window is
    already open in this process is not a crash to recover from: the PR's settings
    stay held back for the outer spawn until it ends."""
    repo, rel = _hostile_tracked(tmp_path)
    before = repo.settings_bytes()
    with guard.window(str(repo.wt)):
        with guard.window(str(repo.wt)):
            assert _hooks(seen_settings(str(repo.wt))) == []
        assert _hooks(seen_settings(str(repo.wt))) == []
        assert guard.recover(str(repo.wt)) is True
        assert _hooks(seen_settings(str(repo.wt))) == []
    assert repo.settings_bytes() == before and repo.status() == ""


# ── git that cannot answer ───────────────────────────────────────────────────────

def test_an_index_git_cannot_read_refuses_the_spawn(tmp_path):
    """Whether a settings file is tracked decides how it is held back (it is
    marked skip-worktree so a fixer's ``git add`` cannot stage the scrub): when
    git cannot read the index, the spawn is refused instead of guessing."""
    repo, rel = _hostile_tracked(tmp_path)
    before = repo.settings_bytes()
    index = Path(repo.wt, git(repo.wt, "rev-parse", "--git-path", "index").strip())
    index.write_bytes(b"not an index")
    with pytest.raises(guard.SettingsGuardRefusal, match="cannot read the git index"):
        _spawn(str(repo.wt))
    assert repo.settings_bytes() == before


@pytest.mark.parametrize("where", ["not-a-git-checkout", "no-git-installed"])
def test_settings_are_held_back_where_git_is_not_there(tmp_path, monkeypatch, where):
    """Outside a git work tree, or with no git at all, nothing is tracked: the
    settings are still held back for the spawn and put back after."""
    if where == "not-a-git-checkout":
        checkout = tmp_path / "plain"
        checkout.mkdir()
    else:
        repo, rel = _hostile_tracked(tmp_path)
        checkout = repo.wt

        def no_git(cwd, args):
            raise FileNotFoundError("git")

        monkeypatch.setattr(guard, "_git_index", no_git)
    path = write(checkout, SETTINGS, hostile(tmp_path / "markers")) if where == "not-a-git-checkout" \
        else checkout / SETTINGS
    before = path.read_bytes()
    with guard.window(str(checkout)):
        assert json.loads(path.read_text()) == {}
    assert path.read_bytes() == before


def test_a_resolver_answer_that_is_not_a_commit_id_is_not_trusted(tmp_path, capfd):
    """Only a full commit id counts as the base: an answer like ``HEAD`` would make
    the PR its own base and trust everything it committed."""
    settings = {"model": "sonnet", "hooks": command_hook("SessionStart", touch(tmp_path / "markers", "b"))}
    repo = make_pr_repo(tmp_path, {}, {SETTINGS: settings})
    guard.install_base_resolver(lambda checkout: "HEAD")
    view = _spawn(str(repo.wt))
    assert view[SETTINGS] == {"model": "sonnet"}
    assert "base commit is unknown" in capfd.readouterr().err


def test_a_base_settings_path_that_is_a_symlink_is_not_read_as_settings(tmp_path):
    """Base commits ``.claude/settings.json`` as a symlink whose target text happens
    to be JSON; the PR replaces it with a regular file holding that text. Base has
    no settings FILE there, so nothing in the PR's file is base-trusted."""
    text = json.dumps({"hooks": command_hook("SessionStart", touch(tmp_path / "markers", "x"))})
    repo = make_pr_repo(tmp_path, {}, {})
    (repo.primary / ".claude").mkdir()
    os.symlink(text, repo.primary / SETTINGS)
    git(repo.primary, "add", "-A")
    git(repo.primary, "commit", "-qm", "a symlink at the settings path")
    base = git(repo.primary, "rev-parse", "HEAD").strip()
    git(repo.wt, "merge", "-q", "--no-edit", "main")
    (repo.wt / SETTINGS).unlink()
    write(repo.wt, SETTINGS, text)
    git(repo.wt, "add", "-A")
    git(repo.wt, "commit", "-qm", "a regular file now")
    guard.install_base_resolver(lambda checkout: base)
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_base_branch_git_cannot_read_holds_settings_back_without_refusing(tmp_path, monkeypatch):
    settings = {"model": "sonnet", "hooks": command_hook("SessionStart", touch(tmp_path / "markers", "b"))}
    repo = make_pr_repo(tmp_path, {SETTINGS: settings}, {})
    guard.install_base_resolver(lambda checkout: repo.base)

    def unreadable(self, rel):
        raise OSError("git ls-tree failed: fatal: bad object")

    monkeypatch.setattr(guard._Base, "entry", unreadable)
    assert _spawn(str(repo.wt))[SETTINGS] == {"model": "sonnet"}


# ── a symlinked settings file that changes between the plan and the scrub ────────

def _symlinked_settings(tmp_path):
    repo = make_pr_repo(tmp_path, {}, {})
    outside = write(tmp_path, "outside/settings.json", hostile(tmp_path / "markers"))
    (repo.wt / ".claude").mkdir()
    os.symlink(outside, repo.wt / SETTINGS)
    return repo, outside


def test_a_symlink_that_cannot_be_removed_refuses_the_spawn(tmp_path, monkeypatch):
    repo, outside = _symlinked_settings(tmp_path)
    monkeypatch.setattr(guard._Dir, "unlink", lambda self, name: None)
    with pytest.raises(guard.SettingsGuardRefusal, match="is still present"):
        _spawn(str(repo.wt))


@pytest.mark.parametrize("swap", ["replaced-by-a-file", "already-gone"])
def test_a_symlink_that_changed_since_the_plan(tmp_path, monkeypatch, swap):
    """Between the plan and the scrub the symlinked settings file is replaced by a
    regular file (the spawn is refused, and that file is left alone) or removed
    (there is nothing left to hold back: the spawn goes ahead)."""
    repo, outside = _symlinked_settings(tmp_path)
    real_plan = guard._plan

    def racing_plan(checkout):
        held = real_plan(checkout)
        (repo.wt / SETTINGS).unlink()
        if swap == "replaced-by-a-file":
            write(repo.wt, SETTINGS, {"model": "opus"})
        return held

    monkeypatch.setattr(guard, "_plan", racing_plan)
    if swap == "replaced-by-a-file":
        with pytest.raises(guard.SettingsGuardRefusal, match="is still present"):
            _spawn(str(repo.wt))
        assert json.loads((repo.wt / SETTINGS).read_text()) == {"model": "opus"}
    else:
        assert _hooks(_spawn(str(repo.wt))) == []
        assert os.readlink(repo.wt / SETTINGS) == str(outside)


def test_an_unexpected_error_while_holding_back_refuses_the_spawn(tmp_path, monkeypatch):
    repo, rel = _hostile_tracked(tmp_path)
    before = repo.settings_bytes()

    def broken(checkout, held, token):
        raise TypeError("a guard bug")

    monkeypatch.setattr(guard, "_write_journal", broken)
    with pytest.raises(guard.SettingsGuardRefusal, match=r"could not be held back \(an unexpected TypeError\)"):
        _spawn(str(repo.wt))
    assert repo.settings_bytes() == before and repo.status() == ""


# ── where the guard looks, and loop entry ────────────────────────────────────────

def test_the_falsy_cwd_redirect_works_only_under_the_test_runner(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    assert guard._checkout(None) == os.path.abspath(os.getcwd())
    assert guard._checkout("") == os.path.abspath(os.getcwd())


def test_a_working_directory_that_is_gone_refuses_the_spawn(tmp_path, monkeypatch):
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    monkeypatch.delenv(guard.FALLBACK_CWD_ENV)
    with pytest.raises(guard.SettingsGuardRefusal, match="the current directory"):
        with guard.window(None):
            pass
    assert guard.recover(None) is False


def test_loop_entry_with_nothing_pending_costs_nothing(tmp_path):
    repo, rel = _hostile_tracked(tmp_path)
    assert guard.recover(str(repo.wt)) is True
    assert not os.path.exists(os.environ[guard.STATE_DIR_ENV])


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores directory permissions")
def test_a_replay_that_cannot_put_settings_back_refuses_until_it_can(tmp_path, capfd):
    """A crash left a local file held back, and its ``.claude`` is now read-only:
    the replay cannot finish, so loop entry reports it and every spawn there is
    refused — the journal, the only copy of the original, is never overwritten —
    until the directory is writable again."""
    repo, rel = _hostile_untracked_local(tmp_path)
    before = (repo.wt / rel).read_bytes()
    journal = Path(_kill_mid_window(repo, tmp_path))
    owed = journal.read_bytes()
    os.chmod(repo.wt / ".claude", 0o555)
    try:
        assert guard.recover(str(repo.wt)) is False
        with pytest.raises(guard.SettingsGuardRefusal, match="could not be put back"):
            _spawn(str(repo.wt))
        assert json.loads(journal.read_text())["entries"] == json.loads(owed)["entries"]
    finally:
        os.chmod(repo.wt / ".claude", 0o755)
    assert guard.recover(str(repo.wt)) is True
    assert (repo.wt / rel).read_bytes() == before


# ── every word that can reach a file is read ──────────────────────────────────────

def _everything(named):
    return named.paths | named.trees | named.bare | named.bare_trees


@pytest.mark.parametrize("command,field,path", [
    ("python3 -X dev tools/run", "bare_trees", "tools"),           # an option's argument is not the script
    ("python3 tools/run", "bare_trees", "tools"),                  # a script with no suffix
    ("python3 -m tools.lint", "root_imports", "tools"),            # a module, imported from the root
    ('"$PERL" -p tools/fix.pl', "bare_trees", "tools"),            # an option read as code may be the script
    ("bash <<< '$CLAUDE_PROJECT_DIR/tools/x.sh'", "paths", "tools/x.sh"),  # an inner shell expands it
    ("bash -c '$CLAUDE_PROJECT_DIR/tools/x.sh'", "paths", "tools/x.sh"),
    ('case "$1" in a) true ;; esac; python3 tools/x.py', "bare_trees", "tools"),  # after esac
    ("echo case $x in; python3 tools/x.py", "bare_trees", "tools"),  # 'case' as an argument
    ("[[ -n $x ]] && make CC = ./tools/cc.sh", "paths", "tools/cc.sh"),  # '=' after a test has ended
    ("make CC = ./tools/cc.sh", "paths", "tools/cc.sh"),           # '=' outside a test
    ('case "$1" in lint) ./tools/lint.sh ;; esac', "paths", "tools/lint.sh"),  # a case arm's command
    ("bash lint,all.sh", "bare", "lint,all.sh"),                   # a name that holds a separator
    ("curl -fsS https://x/#a && python3 tools/x.py", "bare_trees", "tools"),  # '#' inside a word
    ("./tools/x.sh>out.log", "paths", "tools/x.sh"),               # a word glued to a redirect
])
def test_every_word_that_can_reach_a_file_is_read(command, field, path):
    named = guard.named_paths(command)
    assert named.unsafe is None
    assert path in getattr(named, field), _everything(named)


def test_a_permissions_string_that_is_not_a_rule_is_read_as_a_command():
    """Only the rule lists are patterns; any other string under ``permissions`` —
    one a future release adds — may run."""
    named = guard.named_paths({"helperCommand": "./tools/x.sh"}, key="permissions")
    assert "tools/x.sh" in named.paths


def test_a_command_nested_past_the_limit_is_unsafe():
    command = "true"
    for _ in range(8):
        command = "bash -c " + shlex.quote(command)
    assert "nested too deeply" in (guard.named_paths(command).unsafe or "")


def test_a_command_that_changes_directory_too_often_is_unsafe():
    assert "changes directory too often" in (
        guard.named_paths("cd a; cd b; cd c; cd d; cd e; ./x.sh").unsafe or "")


def test_a_bare_cd_to_a_home_above_the_checkout_is_unsafe(tmp_path, monkeypatch):
    checkout = tmp_path / "wt"
    checkout.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    assert guard.named_paths("cd; ./x.sh", str(checkout)).unsafe == (
        "it changes into a directory above the checkout")


@pytest.mark.parametrize("command,why", [
    ("cd tools && python3 sub/../x.py", "its interpreter imports from the checkout root"),  # a climb after a cd
    ('pushd "$X" && ./run.sh', "it changes into a directory this guard cannot know"),
    ('cd "$X"/sub && make', "it changes into a directory this guard cannot know"),  # $X is not $HOME
])
def test_where_a_cd_leaves_the_shell_is_never_guessed(tmp_path, monkeypatch, command, why):
    checkout = tmp_path / "wt"
    checkout.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert guard.named_paths(command, str(checkout)).unsafe == why


@pytest.mark.parametrize("value,key,why", [
    ('"$CLAUDE_PROJECT_DIR"-old/x.sh', None,
     "it cannot be parsed (the project directory is glued to other text)"),
    ("cd sub && ruff check .", None, "it names the checkout root"),   # '.' where a cd may not have happened
    ("cd sub && ls ./", None, "it names the checkout root"),
    ("LD_PRELOAD=:x make", None, "it names the checkout root"),       # an empty search-path element
    ({"PYTHONPATH": "$PYTHONPATH:lib"}, "env", "it names the checkout root"),  # an element that may be empty
])
def test_a_value_that_may_name_the_checkout_root_is_unsafe(value, key, why):
    assert guard.named_paths(value, key=key).unsafe == why


def test_an_env_path_is_read_exactly_as_written():
    assert "my lib" in guard.named_paths({"PYTHONPATH": "./my lib"}, key="env").paths


def test_a_symlink_at_the_lock_path_is_never_followed(tmp_path):
    repo, rel = _hostile_tracked(tmp_path)
    state = Path(os.environ[guard.STATE_DIR_ENV])
    state.mkdir(mode=0o700)
    target = tmp_path / "lock-target"
    os.symlink(target, state / (guard._key(str(repo.wt)) + ".lock"))
    with pytest.raises(guard.SettingsGuardRefusal, match="cannot lock the checkout"):
        _spawn(str(repo.wt))
    assert not target.exists()


def test_a_claude_entry_that_is_a_plain_file_is_left_alone(tmp_path):
    """A regular file named ``.claude`` holds no settings Claude Code reads: there
    is nothing to hold back and nothing to refuse."""
    repo = make_pr_repo(tmp_path, {}, {".claude": "not a directory\n"})
    _spawn(str(repo.wt))
    assert (repo.wt / ".claude").read_text() == "not a directory\n"


def test_a_file_of_display_keys_only_never_asks_for_the_base(tmp_path):
    """C10: nothing in a file of allowlisted keys needs the base commit, so the
    resolver (``gh`` and ``git`` in a real run) is never consulted for it."""
    repo = make_pr_repo(tmp_path, {}, {SETTINGS: {"model": "opus", "theme": "dark"}})
    asked = []
    guard.install_base_resolver(lambda checkout: asked.append(checkout) or repo.base)
    assert _spawn(str(repo.wt))[SETTINGS] == {"model": "opus", "theme": "dark"}
    assert asked == []


# ── the dependency checker, decision by decision ─────────────────────────────────

def test_with_no_base_each_held_key_says_why(tmp_path, capfd):
    repo = make_pr_repo(tmp_path, {}, {SETTINGS: hostile(tmp_path / "markers")})
    _spawn(str(repo.wt))
    assert "hooks (no base commit to compare with)" in capfd.readouterr().err


def test_a_named_file_the_pr_deleted_is_not_mistaken_for_a_plain_word(tmp_path):
    """Base has ``tools/check.sh``; the PR deletes it. The hook names a file the
    run itself could write back before the hook fires, so it is held back."""
    repo = _hook_repo(tmp_path, "sh tools/check.sh", {"tools/check.sh": "true\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    shutil.rmtree(repo.wt / "tools")
    git(repo.wt, "add", "-A")
    git(repo.wt, "commit", "-qm", "drop tools")
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_root_script_run_by_its_shebang_says_it_imports_from_the_root(tmp_path, capfd):
    repo = _hook_repo(tmp_path, '"$CLAUDE_PROJECT_DIR"/check.py',
                      {"check.py": "#!/usr/bin/env python3\nimport json\n"})
    assert _hooks(_spawn(str(repo.wt))) == []
    assert "hooks (its interpreter imports from the checkout root)" in capfd.readouterr().err


def test_a_glob_inside_git_metadata_is_not_a_checkout_dependency(tmp_path):
    repo = _hook_repo(tmp_path, "ls .git/hooks/* >/dev/null 2>&1; true", {})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]


def test_files_that_cannot_be_compared_hold_the_key_back_without_refusing(tmp_path, monkeypatch, capfd):
    repo = _hook_repo(tmp_path, "sh tools/check.sh", {"tools/check.sh": "true\n"})

    def unreadable(self, rel, hops):
        raise OSError("Input/output error")

    monkeypatch.setattr(guard._Checker, "_same", unreadable)
    assert _hooks(_spawn(str(repo.wt))) == []
    assert "its files could not be compared with the base branch" in capfd.readouterr().err


@pytest.mark.parametrize("changed,live", [
    ("pkg/__init__.py", False),          # a package Python finds at the root before its own
    ("my-script.py", True),              # not a name Python can import
], ids=["package-init", "not-importable"])
def test_what_a_python_program_run_at_the_root_can_import(tmp_path, changed, live):
    repo = _hook_repo(tmp_path, 'python3 -c "import json"',
                      {"pkg/__init__.py": "x = 1\n", "my-script.py": "print(1)\n"})
    write(repo.wt, changed, "print('changed')\n")
    assert _hooks(_spawn(str(repo.wt))) == (["SessionStart"] if live else [])


@pytest.mark.parametrize("extra,live", [
    (".DS_Store", True),                       # Finder's own file: not a change
    ("helper.pyc", False),                     # a sourceless module Python would import
    ("__pycache__/notes.txt", False),          # only bytecode is the cache
], ids=["ds-store", "pyc-beside-the-script", "non-bytecode-in-pycache"])
def test_which_untracked_files_in_a_walked_directory_count(tmp_path, extra, live):
    command = 'python3 "$CLAUDE_PROJECT_DIR"/tools/check.py'
    repo = _hook_repo(tmp_path, command, {"tools/check.py": "import helper\n", "tools/helper.py": "x = 1\n"})
    write(repo.wt, f"tools/{extra}", b"\x00")
    assert _hooks(_spawn(str(repo.wt))) == (["SessionStart"] if live else [])


@pytest.mark.parametrize("first_line,live", [
    ("# python3 is required", True),            # a comment, not a shebang
    ("#!/usr/bin/env -S python3 -u", False),    # env -S and an option before the interpreter
], ids=["comment", "env-dash-s"])
def test_only_a_real_shebang_names_the_interpreter(tmp_path, first_line, live):
    repo = _hook_repo(tmp_path, '"$CLAUDE_PROJECT_DIR"/tools/run',
                      {"tools/run": first_line + "\necho run\n", "tools/notes.txt": "x\n"})
    write(repo.wt, "tools/notes.txt", "changed\n")
    assert _hooks(_spawn(str(repo.wt))) == (["SessionStart"] if live else [])


def test_a_bare_shebang_names_no_interpreter_and_refuses_nothing(tmp_path):
    repo = _hook_repo(tmp_path, '"$CLAUDE_PROJECT_DIR"/tools/run', {"tools/run": "#!\necho run\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]


def test_a_named_directory_the_pr_added_is_held_back_without_refusing(tmp_path):
    repo = _hook_repo(tmp_path, "ls tools/ >/dev/null", {}, head={"tools/new.sh": "true\n"})
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_regular_file_holding_a_base_symlinks_target_is_not_that_symlink_when_run(tmp_path):
    """The file a hook runs (no directory of it is walked): base has ``tools/check.sh``
    as a symlink to ``real.sh``; the PR makes it a regular file whose bytes are the
    link's target text."""
    repo = _hook_repo(tmp_path, "sh tools/check.sh", {"tools/real.sh": "true\n"})
    _relink(repo, "tools/check.sh", "real.sh", in_base=True)
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    (repo.wt / "tools/check.sh").unlink()
    (repo.wt / "tools/check.sh").write_bytes(b"real.sh")
    git(repo.wt, "add", "-A")
    git(repo.wt, "commit", "-qm", "a file now")
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_regular_file_holding_a_base_symlinks_target_is_not_that_symlink(tmp_path):
    """Base has ``tools/check.py`` as a symlink to ``real.py``; the PR swaps it for a
    regular file whose bytes are the link's target text. A file is not a link."""
    repo = _hook_repo(tmp_path, 'python3 "$CLAUDE_PROJECT_DIR"/tools/check.py', {"tools/real.py": "x = 1\n"})
    os.symlink("real.py", repo.primary / "tools/check.py")
    git(repo.primary, "add", "-A")
    git(repo.primary, "commit", "-qm", "link")
    base = git(repo.primary, "rev-parse", "HEAD").strip()
    git(repo.wt, "merge", "-q", "--no-edit", "main")
    (repo.wt / "tools/check.py").unlink()
    (repo.wt / "tools/check.py").write_bytes(b"real.py")
    git(repo.wt, "add", "-A")
    git(repo.wt, "commit", "-qm", "a file now")
    guard.install_base_resolver(lambda checkout: base)
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_named_file_too_big_to_compare_is_held_back(tmp_path, monkeypatch):
    repo = _hook_repo(tmp_path, "sh tools/check.sh", {"tools/check.sh": "true # padding padding\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    monkeypatch.setattr(guard, "_MAX_WALK_BYTES", 10)
    assert _hooks(_spawn(str(repo.wt))) == []


# ── symlinks along a named path ──────────────────────────────────────────────────

def _relink(repo, rel, target, *, in_base=False):
    """Put a symlink at ``rel`` — in the PR (a commit on the PR branch), or at base
    (a commit on main that the PR then merges, returning the new base)."""
    where = repo.primary if in_base else repo.wt
    if os.path.lexists(where / rel):
        os.unlink(where / rel)
    (where / rel).parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, where / rel)
    git(where, "add", "-A")
    git(where, "commit", "-qm", f"link {rel}")
    if in_base:
        base = git(repo.primary, "rev-parse", "HEAD").strip()
        git(repo.wt, "merge", "-q", "--no-edit", "main")
        guard.install_base_resolver(lambda checkout: base)


def test_a_symlink_the_pr_added_is_held_back_without_refusing(tmp_path):
    repo = _hook_repo(tmp_path, "sh tools/link.sh", {"tools/real.sh": "true\n"})
    _relink(repo, "tools/link.sh", "real.sh")
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_symlink_that_replaced_a_file_holding_its_target_text_is_held_back(tmp_path):
    """Base holds ``tools/check.sh`` as a FILE whose bytes are ``real.sh``; the PR
    makes it a symlink to ``real.sh``. A link is not that file."""
    repo = _hook_repo(tmp_path, "sh tools/check.sh", {"tools/check.sh": "real.sh", "tools/real.sh": "true\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    _relink(repo, "tools/check.sh", "real.sh")
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_symlink_cycle_is_held_back_without_refusing(tmp_path):
    repo = _hook_repo(tmp_path, "sh tools/a", {"tools/keep.txt": "x\n"})
    (repo.primary / "tools/a").symlink_to("b")
    _relink(repo, "tools/b", "a", in_base=True)
    assert _hooks(_spawn(str(repo.wt))) == []


def test_an_absolute_symlink_is_not_followed_into_the_checkout(tmp_path):
    """Base links ``tools/tool`` to ``/opt/x``; the checkout happens to hold a
    base-identical ``tools/opt/x``. The absolute target is outside the checkout,
    not that file, so it is never compared as if it were."""
    repo = _hook_repo(tmp_path, "sh tools/tool", {"tools/opt/x": "true\n"})
    _relink(repo, "tools/tool", "/opt/x", in_base=True)
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_base_symlink_the_pr_retargets_is_held_back(tmp_path):
    """Base links ``tools/run.sh`` to ``check.sh``; the PR points it at
    ``cleanup.sh``, another file base already has. The script that would run is
    not the one base meant."""
    repo = _hook_repo(tmp_path, "sh tools/run.sh", {"tools/check.sh": "true\n", "tools/cleanup.sh": "true\n"})
    _relink(repo, "tools/run.sh", "check.sh", in_base=True)
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    _relink(repo, "tools/run.sh", "cleanup.sh")
    assert _hooks(_spawn(str(repo.wt))) == []


# ── walking a directory a value names ────────────────────────────────────────────

def test_a_fifo_in_a_walked_directory_holds_the_key_back_without_refusing(tmp_path):
    """A run can leave a FIFO or socket where a hook looks: the directory is then
    not what base has — held back, not compared as if the FIFO were absent, and
    never a refusal."""
    repo = _hook_repo(tmp_path, 'python3 "$CLAUDE_PROJECT_DIR"/tools/check.py', {"tools/check.py": "x = 1\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    os.mkfifo(repo.wt / "tools/pipe")
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_regular_file_holding_a_base_symlinks_target_inside_a_walked_directory(tmp_path):
    repo = _hook_repo(tmp_path, 'python3 "$CLAUDE_PROJECT_DIR"/tools/check.py',
                      {"tools/check.py": "x = 1\n", "tools/real.py": "y = 2\n"})
    _relink(repo, "tools/link.py", "real.py", in_base=True)
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    (repo.wt / "tools/link.py").unlink()
    (repo.wt / "tools/link.py").write_bytes(b"real.py")
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_cache_file_is_not_dropped_when_git_cannot_say_it_is_untracked(tmp_path):
    """Only an UNTRACKED ``__pycache__`` file is a hook's own leftover; when git
    cannot read the index, a committed one cannot be told apart, so the directory
    is not trusted."""
    repo = _hook_repo(tmp_path, 'python3 "$CLAUDE_PROJECT_DIR"/tools/check.py', {"tools/check.py": "x = 1\n"},
                      head={"tools/__pycache__/check.cpython-311.pyc": "crafted"})
    index = Path(repo.wt, git(repo.wt, "rev-parse", "--git-path", "index").strip())
    index.write_bytes(b"not an index")
    checker = guard._Checker(str(repo.wt), guard._Base(str(repo.wt), repo.base))
    same, why = checker.unchanged("hooks", command_hook("SessionStart", 'python3 "$CLAUDE_PROJECT_DIR"/tools/check.py'))
    assert not same and "could not be compared" in why


def test_a_directory_too_large_to_walk_is_held_back(tmp_path, monkeypatch):
    repo = _hook_repo(tmp_path, 'python3 "$CLAUDE_PROJECT_DIR"/tools/check.py',
                      {"tools/check.py": "x = 1\n", "tools/a.py": "a = 1\n", "tools/b.py": "b = 1\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    monkeypatch.setattr(guard, "_MAX_WALK_FILES", 2)
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_repository_with_sha256_object_ids_keeps_base_trusted_hooks(tmp_path):
    """Blob ids are computed in the repository's own hash: in a SHA-256 repository
    an unchanged file still matches base."""
    primary, wt = tmp_path / "primary", tmp_path / "wt"
    primary.mkdir()
    git(primary, "init", "-q", "-b", "main", "--object-format=sha256")
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false"),
                 ("core.excludesFile", os.devnull), ("core.hooksPath", os.devnull)):
        git(primary, "config", k, v)
    write(primary, SETTINGS, {"hooks": command_hook("SessionStart", "sh tools/check.sh")})
    write(primary, "tools/check.sh", "true\n")
    git(primary, "add", "-A")
    git(primary, "commit", "-qm", "base")
    base = git(primary, "rev-parse", "HEAD").strip()
    git(primary, "worktree", "add", "-q", "-b", "feature", str(wt), "main")
    write(wt, "docs/guide.md", "x\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "pr")
    guard.install_base_resolver(lambda checkout: base)
    assert len(base) == 64 and _hooks(_spawn(str(wt))) == ["SessionStart"]


# ── reading shell the way a shell does ───────────────────────────────────────────

@pytest.mark.parametrize("value,why", [
    ("echo x \\", "it cannot be parsed (trailing backslash)"),
    ("echo 'oops", "it cannot be parsed (unbalanced single quote)"),
    ("echo ${oops", "it cannot be parsed (unterminated expansion)"),
])
def test_shell_that_cannot_be_split_is_unsafe(value, why):
    assert guard.named_paths(value).unsafe == why


@pytest.mark.parametrize("value", [
    "sh -c 'echo it`s fine'",               # an inner shell would reject this, running nothing
    'echo "it\'s $((1+2)) ok"',             # arithmetic in a word read piece by piece
])
def test_a_quoted_word_the_inner_reading_cannot_follow_is_read_piece_by_piece(value):
    assert guard.named_paths(value).unsafe is None


def test_paths_in_a_quoted_command_the_inner_reading_cannot_split_are_still_named():
    assert "tools/x.sh" in guard.named_paths('sh -c "echo it\'s ./tools/x.sh"').paths


def test_a_python_flag_cluster_ending_in_c_is_code():
    named = guard.named_paths("python3 -uc 'import foo'")
    assert named.imports_root and named.root_imports == {"foo"}


@pytest.mark.parametrize("command", [
    "cd /tmp && ./x.sh",          # cd outside the checkout, then a file there
    "pushd tools && popd && ./x.sh",
])
def test_a_relative_word_after_a_cd_counts_at_the_root_only_where_it_exists(tmp_path, command):
    repo = _hook_repo(tmp_path, command, {"tools/keep.txt": "x\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]


@pytest.mark.parametrize("command", [
    'cd "" && true',                       # cd "" stays where it is
    "pushd tools && popd && true",         # popd goes back, not home
    "cd ~+/tools && true",                 # ~+ is the current directory
])
def test_a_cd_that_never_leaves_the_checkout_is_not_a_cd_home(tmp_path, monkeypatch, command):
    checkout = tmp_path / "wt"
    (checkout / "tools").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert guard.named_paths(command, str(checkout)).unsafe is None


# ── a script a hook names, however it names it ───────────────────────────────────

# Every way a hook's shell can spell ``tools/...``: the six project-directory
# spellings, the same for ``PWD``, and a bare relative path.
_SPELLINGS = (
    '"$CLAUDE_PROJECT_DIR"/', '"${CLAUDE_PROJECT_DIR}"/', "${CLAUDE_PROJECT_DIR}/",
    "$CLAUDE_PROJECT_DIR/", '"$CLAUDE_PROJECT_DIR/', "./",
    '"$PWD"/', '"${PWD}"/', "${PWD}/", "$PWD/", '"$PWD/', "",
)


def _spell(spelling, path):
    """``path`` (relative to the checkout) in one of :data:`_SPELLINGS`."""
    return spelling + path + ('"' if spelling.endswith(('_DIR/', 'PWD/')) and spelling[0] == '"' else "")


# The script directory every family's cells load from, and the sibling module each
# family's script loads (the file a PR changes).
_TOOLS = {
    "tools/run": "import helper\n",
    "tools/x.py": "import helper\n", "tools/helper.py": "x = 1\n",
    "tools/x.js": "require('./helper')\n", "tools/helper.js": "module.exports = 1\n",
    "tools/x.rb": "require_relative 'helper'\n", "tools/helper.rb": "X = 1\n",
    "tools/x.pl": "use FindBin; use lib $FindBin::Bin; require 'helper.pl';\n",
    "tools/helper.pl": "1;\n",
}
_SIBLING = {"python3": "tools/helper.py", "node": "tools/helper.js", "ruby": "tools/helper.rb",
            "perl": "tools/helper.pl", '"$PY"': "tools/helper.py"}


@pytest.fixture(scope="module")
def _tools_repo(tmp_path_factory):
    return make_pr_repo(tmp_path_factory.mktemp("tools"), _TOOLS, {})


def _live(repo, command):
    checker = guard._Checker(str(repo.wt), guard._Base(str(repo.wt), repo.base))
    return checker.unchanged("hooks", command_hook("SessionStart", command))[0]


def _assert_sibling_holds(repo, command, sibling):
    """The hook is live while the script's directory is base's, and held back once
    the PR changes a module beside the script."""
    path = repo.wt / sibling
    before = path.read_bytes()
    assert _live(repo, command), f"held back with nothing changed: {command}"
    try:
        path.write_bytes(before + b"\n# the pull request's code\n")
        assert not _live(repo, command), f"live after {sibling} changed: {command}"
    finally:
        path.write_bytes(before)


@pytest.mark.parametrize("command", [
    'SCRIPT=./tools/x.py; python3 "$SCRIPT"',
    'SCRIPT=./tools/x.py && python3 "$SCRIPT"',
    'export SCRIPT=tools/x.py; python3 "$SCRIPT"',
    'DIR=./tools; python3 "$DIR"/x.py',
    'DIR=./tools; SCRIPT=$DIR/run; python3 "$SCRIPT"',
    'SCRIPT="$CLAUDE_PROJECT_DIR"/tools/run; python3 "$SCRIPT"',
    'for f in tools/x.py; do python3 "$f"; done',
    "sh -c 'python3 \"$1\"' _ tools/run",
])
def test_a_script_named_by_a_variable_names_its_directory(_tools_repo, command):
    """``SCRIPT=./tools/x.py; python3 "$SCRIPT"``: the interpreter runs the value the
    hook assigned, and imports from its directory — so the PR changing a module
    beside an unchanged script holds the hook back."""
    _assert_sibling_holds(_tools_repo, command, "tools/helper.py")


@pytest.mark.parametrize("command", [
    'python3 "$SCRIPT"',                                   # from the environment
    'node "$1"',
    'true && SCRIPT=./tools/x.py; python3 "$SCRIPT"',      # an assignment that may not run
    'if true; then SCRIPT=./tools/x.py; fi; python3 "$SCRIPT"',
    'SCRIPT=./tools/x.py | cat; python3 "$SCRIPT"',       # set in a subshell
    'SCRIPT=./tools/x.py python3 "$SCRIPT"',              # a prefix: expanded before it applies
    'SCRIPT=./tools/x.py; . tools/env.sh; python3 "$SCRIPT"',  # a sourced script may reset it
    'read SCRIPT; python3 "$SCRIPT"',
    'SCRIPT+=x.py; python3 "$SCRIPT"',
    "SCRIPT=-c; python3 \"$SCRIPT\" 'import helper'",      # an option, not a script
    "SCRIPT='tools/x.py tools/y.py'; python3 $SCRIPT",    # split into words
    'for f in $FILES; do python3 "$f"; done',
    'for f; do python3 "$f"; done',
    "echo tools/x.py | xargs sh -c 'python3 \"$0\"'",      # the operand comes from stdin
    "sh -c 'python3 \"$1\"' _ \"$SCRIPT\"",
])
def test_a_script_named_by_a_variable_of_unknown_value_is_unsafe(command):
    assert guard.named_paths(command).unsafe == "it runs a script a variable names"


@pytest.mark.parametrize("command", [
    'python3 tools/x.py "$CLAUDE_FILE_PATHS"',
    'npx prettier --write "$CLAUDE_FILE_PATHS"',
    "sh -c 'echo \"$1\"' _ \"$CLAUDE_FILE_PATHS\"",
    'for f in "$CLAUDE_PROJECT_DIR"/hooks.d/*.sh; do sh "$f"; done',
])
def test_a_variable_that_is_not_the_script_stays_safe(command):
    assert guard.named_paths(command).unsafe is None


@pytest.mark.parametrize("command", [
    "node -e \"require('$CLAUDE_PROJECT_DIR/tools/x.js')\"",
    "node -e \"require('${CLAUDE_PROJECT_DIR}/tools/x.js')\"",
    "node -e \"require('$PWD/tools/x.js')\"",
    "node -p \"require('$CLAUDE_PROJECT_DIR/tools/x.js')\"",
])
def test_a_script_node_code_loads_by_the_project_directory_names_its_directory(tmp_path, command):
    """The shell expands the path in ``node -e`` code before node runs, so
    ``require('$CLAUDE_PROJECT_DIR/tools/x.js')`` loads ``tools/x.js`` exactly as
    ``require('./tools/x.js')`` does — and ``x.js`` loads its sibling."""
    repo = _hook_repo(tmp_path, command, {"tools/x.js": "require('./helper.js');\n",
                                         "tools/helper.js": "1\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    write(repo.wt, "tools/helper.js", touch(repo.markers, "pr") + "\n")
    assert _hooks(_spawn(str(repo.wt))) == []


@pytest.mark.parametrize("command", [
    'python3 -uW ignore "$CLAUDE_PROJECT_DIR"/tools/run',
    'python3 -bX dev "$CLAUDE_PROJECT_DIR"/tools/run',
    'python3 -Wd "$CLAUDE_PROJECT_DIR"/tools/run',
])
def test_a_python_script_after_a_cluster_ending_in_an_option_names_its_directory(tmp_path, command):
    """``-uW ignore`` is ``-u`` then ``-W ignore``: ``ignore`` is ``-W``'s argument
    and ``tools/run`` the script, which imports from its own directory."""
    repo = _hook_repo(tmp_path, command, {"tools/run": "import helper\n", "tools/helper.py": "x = 1\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    write(repo.wt, "tools/helper.py", "x = 2\n")
    assert _hooks(_spawn(str(repo.wt))) == []


@pytest.mark.parametrize("command", [
    "ruby -e \"require './tools/x.rb'\"",
    "ruby -e \"load '$CLAUDE_PROJECT_DIR/tools/x.rb'\"",
])
def test_a_script_ruby_code_loads_names_its_directory(tmp_path, command):
    repo = _hook_repo(tmp_path, command, {"tools/x.rb": "require_relative 'helper'\n",
                                         "tools/helper.rb": "X = 1\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    write(repo.wt, "tools/helper.rb", "X = 2\n")
    assert _hooks(_spawn(str(repo.wt))) == []


# (interpreter, options before the script): every option that takes an argument,
# alone and closing a Python cluster, and the options that look like one but take
# none in that family.
_AFTER_OPTIONS = [
    ("python3", "-W ignore"), ("python3", "-X dev"), ("python3", "-uW ignore"),
    ("python3", "-bX dev"), ("python3", "-Wd"), ("python3", '-W"$MODE"'), ("python3", "-I"),
    ("node", "-r dotenv/config"), ("node", "--require dotenv/config"),
    ("node", "--import dotenv/config"), ("node", "--loader ts-node/esm"),
    ("node", "--experimental-loader ts-node/esm"), ("node", "-C dev"),
    ("node", "--conditions dev"),
    ("ruby", "-I vendor"), ("ruby", "-r json"), ("ruby", "-C /"), ("ruby", "-X /"), ("ruby", "-W"),
    ("perl", "-I vendor"), ("perl", "-W"), ("perl", "-X"), ("perl", "-C"),
    ('"$PY"', "-W ignore"), ('"$PY"', "-X dev"), ('"$PY"', "-uW ignore"), ('"$PY"', "-bX dev"),
    ('"$PY"', "-Wd"), ('"$PY"', "-I"), ('"$PY"', "-I vendor"), ('"$PY"', "-r dotenv/config"),
    ('"$PY"', "-W"), ('"$PY"', "-X"), ('"$PY"', "-C"),
]


@pytest.mark.parametrize("spelling", _SPELLINGS, ids=lambda s: s or "bare")
@pytest.mark.parametrize("interp,options", _AFTER_OPTIONS, ids=lambda v: v)
def test_a_script_after_any_option_names_its_directory(_tools_repo, interp, options, spelling):
    """The script after any option cluster — an option's argument skipped, a flag
    not mistaken for one — imports from its directory, however its path is spelled
    (``tools/run`` has no suffix, so only its position says it is the script)."""
    command = f"{interp} {options} {_spell(spelling, 'tools/run')}"
    _assert_sibling_holds(_tools_repo, command, _SIBLING[interp])


@pytest.mark.parametrize("command", [
    "node --require=./tools/x.js -e '0'",
    "node --require=tools/x.js -e '0'",
    "node --import=./tools/x.js -e '0'",
    'node --require="$CLAUDE_PROJECT_DIR"/tools/x.js -e \'0\'',
    "node -r./tools/x.js -e '0'",
    "node -rtools/x.js -e '0'",
])
def test_a_script_glued_to_an_option_by_a_separator_names_its_directory(_tools_repo, command):
    """Splitting ``--require=./tools/x.js`` at ``=`` must not lose that the path
    half is a script: ``x.js`` loads its sibling exactly as it does when the option
    and the script are two words, so the PR changing ``tools/helper.js`` holds the
    hook. The same holds for a script glued to a short option (``-r./tools/x.js``)."""
    _assert_sibling_holds(_tools_repo, command, "tools/helper.js")


# Shell and PHP scripts that load a sibling by their own location, and the same run
# by a ``#!`` line or, with none, by the hook's shell (an executable file).
_SHELL_TOOLS = {
    "check.sh": '. "$(dirname "$0")/lib.sh"\n', "lib.sh": "true\n",
    "tools/check.sh": '. "$(dirname "$0")/lib.sh"\n', "tools/lib.sh": "true\n",
    "tools/check": '. "$(dirname "$0")/lib.sh"\n',  # no suffix: only its position says it is run
    "tools/shebang-sh": '#!/bin/sh\n. "$(dirname "$0")/lib.sh"\n',
    "tools/shebang-bash": '#!/usr/bin/env bash\nsource "${BASH_SOURCE%/*}/lib.sh"\n',
    "tools/no-shebang": '. "$(dirname "$0")/lib.sh"\n',
    "tools/check.php": "<?php require __DIR__ . '/helper.php';\n", "tools/helper.php": "<?php\n",
    "tools/shebang-php": "#!/usr/bin/env php\n<?php require 'helper.php';\n",
}


@pytest.fixture(scope="module")
def _shell_repo(tmp_path_factory):
    repo = make_pr_repo(tmp_path_factory.mktemp("shell"), _SHELL_TOOLS, {})
    for rel in ("tools/shebang-sh", "tools/shebang-bash", "tools/no-shebang", "tools/shebang-php"):
        os.chmod(repo.wt / rel, 0o755)
    return repo


@pytest.mark.parametrize("command", [
    "sh tools/check.sh",
    "bash ./tools/check.sh",
    'zsh "$CLAUDE_PROJECT_DIR"/tools/check.sh',
    "dash ${CLAUDE_PROJECT_DIR}/tools/check.sh",
    'ksh "$PWD/tools/check.sh"',
    "bash -e tools/check.sh",
    "bash -o pipefail tools/check.sh",
    "bash --norc tools/check.sh",
    "bash -euo pipefail tools/check",          # -o's argument is not the script
    "bash +x tools/check",
    "bash +O extglob tools/check",
    "fish -C 'set x 1' tools/check",
    "timeout 30 bash tools/check.sh",
    "busybox sh tools/check.sh",
    ". ./tools/check.sh",                    # the . builtin reads it into the hook's shell
    "source tools/check.sh",
    "bash -c '. ./tools/check.sh'",           # a -c command line is read as a command
    "bash -ec 'sh tools/check.sh'",
    "sh -c 'sh \"$0\"' ./tools/check.sh",
    "\"$SHELL\" -c '. tools/check.sh'",        # an interpreter that may be a shell
    "$SHELL -c 'sh tools/check'",
    "./tools/shebang-sh",                     # a #! shell
    '"$CLAUDE_PROJECT_DIR"/tools/shebang-bash',
    "./tools/no-shebang",                     # executable, no #!: the hook's shell reads it
    "cd tools && ./no-shebang",
])
def test_a_shell_script_names_its_directory(_shell_repo, command):
    """An unchanged shell script that sources a sibling by its own location
    (``. "$(dirname "$0")/lib.sh"``): the PR changing the sibling holds the hook
    back, whichever shell runs it and however it is started."""
    _assert_sibling_holds(_shell_repo, command, "tools/lib.sh")


@pytest.mark.parametrize("command", [
    "sh ./check.sh", "bash check.sh", '. "$CLAUDE_PROJECT_DIR"/check.sh', "source ./check.sh",
])
def test_a_shell_script_at_the_root_is_held_back(_shell_repo, command):
    """Its directory is the checkout root, which a PR always changes."""
    assert not _live(_shell_repo, command)


@pytest.mark.parametrize("command", [
    "php tools/check.php",
    "php -f tools/check.php",
    "php8.2 -n tools/check.php",
    "timeout 30 php tools/check.php",
    '"$PHP" tools/check.php',
    "php -r \"require 'tools/check.php';\"",
    "./tools/shebang-php",
])
def test_a_php_program_is_held_back(_shell_repo, command):
    """PHP's default include path starts with the working directory, the checkout
    root: ``require 'helper.php'`` in an unchanged script finds a file the PR adds
    at the root before the script's own sibling."""
    assert not _live(_shell_repo, command)
    if not command.startswith("./"):
        assert "imports from the checkout root" in guard.named_paths(command).unsafe


@pytest.mark.parametrize("command", ["bash -c 'echo hi'", "bash -lc 'git status'", "sh -ec true"])
def test_a_shell_command_line_is_not_taken_for_a_script(command):
    assert guard.named_paths(command).unsafe is None


def test_a_file_a_hook_only_reads_does_not_name_its_directory(_shell_repo):
    """A file with no ``#!`` line and no execute bit cannot be run directly: ``cat``
    reading it depends on the file alone."""
    path = _shell_repo.wt / "tools/check.sh"
    before = path.read_bytes()
    try:
        path.write_bytes(before + b"# the pull request's code\n")
        assert _live(_shell_repo, "cat ./tools/lib.sh")
    finally:
        path.write_bytes(before)


# Scripts a hook reaches through symlinks base also has. Python's ``sys.path[0]`` and
# Node's ``require`` resolve the link and import from the directory of the file it
# names (``tools/``), not the link's own (``hooks/``); a shell's ``$0`` keeps the
# link's.
_LINKED_FILES = {
    "tools/check.py": "#!/usr/bin/env python3\nimport helper\n", "tools/helper.py": "x = 1\n",
    "tools/x.js": "require('./helper')\n", "tools/helper.js": "module.exports = 1\n",
    "tools/run": '. "$(dirname "$0")/lib.sh"\n', "hooks/lib.sh": "true\n",
    "lib/deep.py": "import helper\n", "lib/helper.py": "x = 1\n",
    "top.py": "#!/usr/bin/env python3\nimport helper\n",
}
_LINKS = {
    "hooks/check.py": "../tools/check.py",
    "hooks/x.js": "../tools/x.js",
    "hooks/run": "../tools/run",              # executable, no #!: the hook's shell reads it
    "hooks.d/a.py": "../tools/check.py",
    "tools/deep.py": "../lib/deep.py",
    "chain/deep.py": "../tools/deep.py",      # a link to a link
    "rooted/top.py": "../top.py",             # a link to a script at the checkout root
}


@pytest.fixture(scope="module")
def _linked_repo(tmp_path_factory):
    repo = make_pr_repo(tmp_path_factory.mktemp("linked"), _LINKED_FILES, {})
    for rel in ("tools/check.py", "tools/run", "top.py"):
        os.chmod(repo.primary / rel, 0o755)
    for rel, target in _LINKS.items():
        (repo.primary / rel).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, repo.primary / rel)
    git(repo.primary, "add", "-A")
    git(repo.primary, "commit", "-qm", "links")
    git(repo.wt, "merge", "-q", "--no-edit", "main")
    repo.base = git(repo.primary, "rev-parse", "HEAD").strip()
    return repo


@pytest.mark.parametrize("command,sibling", [
    ("python3 ./hooks/check.py", "tools/helper.py"),
    ("python3 hooks/check.py", "tools/helper.py"),
    ('python3 "$CLAUDE_PROJECT_DIR"/hooks/check.py', "tools/helper.py"),
    ("./hooks/check.py", "tools/helper.py"),                 # run by its #! line
    ('"$CLAUDE_PROJECT_DIR"/hooks/check.py', "tools/helper.py"),
    ("timeout 30 ./hooks/check.py", "tools/helper.py"),
    ("node ./hooks/x.js", "tools/helper.js"),
    ("node -e \"require('./hooks/x')\"", "tools/helper.js"),
    ("python3 ./chain/deep.py", "lib/helper.py"),            # through a chain of links
    ("./hooks/run", "hooks/lib.sh"),                         # the link's own directory, run directly
    ("python3 ./hooks.d/*.py", "tools/helper.py"),           # a link a glob reaches
    ('for f in ./hooks.d/*.py; do python3 "$f"; done', "tools/helper.py"),
])
def test_a_script_run_through_a_base_symlink_names_the_directory_it_resolves_to(
        _linked_repo, command, sibling):
    """``hooks/check.py`` is base's own symlink to ``../tools/check.py``, unchanged,
    and the PR changes only ``tools/helper.py``: the interpreter imports it from the
    directory the link resolves to, so the hook is held back — however the script
    is started."""
    _assert_sibling_holds(_linked_repo, command, sibling)


@pytest.mark.parametrize("command", ["python3 ./rooted/top.py", "./rooted/top.py"])
def test_a_script_linked_to_the_checkout_root_is_held_back(_linked_repo, command):
    """The link resolves to a script at the checkout root, which a PR always changes."""
    checker = guard._Checker(str(_linked_repo.wt), guard._Base(str(_linked_repo.wt), _linked_repo.base))
    live, why = checker.unchanged("hooks", command_hook("SessionStart", command))
    assert not live and why == "its interpreter imports from the checkout root"


def test_a_link_a_hook_only_reads_does_not_name_where_it_leads(_linked_repo):
    """A linked file with no ``#!`` line and no execute bit cannot be run directly:
    ``cat`` reading it depends on the file alone, not on what sits beside its
    target."""
    path = _linked_repo.wt / "tools/helper.js"
    before = path.read_bytes()
    try:
        path.write_bytes(before + b"// the pull request's code\n")
        assert _live(_linked_repo, "cat ./hooks/x.js")
    finally:
        path.write_bytes(before)


# (interpreter and option, code before the path, code after it, suffix): every
# inline-code option and loader, the option alone, ending a cluster, and with the
# code glued to it (``-e'…'``, marked by a trailing ``|``).
_INLINE = [
    ("node -e", 'require("', '")', ".js"), ("node --eval", 'require("', '")', ".js"),
    ("node -p", 'require("', '")', ".js"), ("node --print", 'require("', '")', ".js"),
    ("node -pe", 'require("', '")', ".js"),
    ("ruby -e", 'require "', '"', ".rb"), ("ruby -e", 'require_relative "', '"', ".rb"),
    ("ruby -e", 'load "', '"', ".rb"), ("ruby -we", 'require "', '"', ".rb"),
    ("ruby -we", 'require_relative "', '"', ".rb"), ("ruby -we", 'load "', '"', ".rb"),
    ("ruby -e|", 'require "', '"', ".rb"), ("ruby -e|", 'require_relative "', '"', ".rb"),
    ("ruby -e|", 'load "', '"', ".rb"),
    ("perl -e", 'do "', '"', ".pl"), ("perl -e", 'require "', '"', ".pl"),
    ("perl -E", 'do "', '"', ".pl"), ("perl -E", 'require "', '"', ".pl"),
    ("perl -we", 'do "', '"', ".pl"), ("perl -we", 'require "', '"', ".pl"),
    ("perl -e|", 'do "', '"', ".pl"), ("perl -e|", 'require "', '"', ".pl"),
    ("python3 -c", 'exec(open("', '").read())', ".py"), ("python3 -uc", 'exec(open("', '").read())', ".py"),
    ("python3 -c|", 'exec(open("', '").read())', ".py"),
    ('"$RUN" -e', 'require("', '")', ".js"), ('"$RUN" -c', 'exec(open("', '").read())', ".py"),
    ('"$RUN" -E', 'do "', '"', ".pl"), ('"$RUN" -we', 'require_relative "', '"', ".rb"),
]
_INLINE_SIBLING = {".js": "tools/helper.js", ".rb": "tools/helper.rb", ".pl": "tools/helper.pl",
                   ".py": "tools/helper.py"}


@pytest.mark.parametrize("spelling", _SPELLINGS, ids=lambda s: s or "bare")
@pytest.mark.parametrize("module", ["x{}", "x"], ids=["suffix", "no-suffix"])
@pytest.mark.parametrize("option,before,after,suffix", _INLINE,
                         ids=[f"{o} {b}" for o, b, _, _ in _INLINE])
def test_a_script_inline_code_loads_names_its_directory(_tools_repo, option, before, after, suffix,
                                                        module, spelling):
    """A file inline code hands a loader is code, with or without a suffix, and its
    directory is a dependency, however the hook's shell spells its path."""
    path = "tools/" + module.format(suffix)
    code = f"'{before}'{_spell(spelling, path)}'{after}'"
    command = f"{option[:-1]}{code}" if option.endswith("|") else f"{option} {code}"
    if option == '"$RUN" -c' and module == "x":
        # ``$RUN`` may be a shell, whose ``-c`` line names ``tools/x`` itself — a
        # file base does not hold, which a PR may add.
        assert not _live(_tools_repo, command)
        return
    _assert_sibling_holds(_tools_repo, command, _INLINE_SIBLING[suffix])


@pytest.mark.parametrize("option", ["python3 -c", "python3 -uc", "python3 -c|"])
@pytest.mark.parametrize("code", ["import tools.x", "from tools import x", "from tools.x import y"])
def test_a_package_python_code_imports_by_name_is_walked_whatever_the_option(_tools_repo, option, code):
    command = f"{option[:-1]}'{code}'" if option.endswith("|") else f"{option} '{code}'"
    _assert_sibling_holds(_tools_repo, command, "tools/helper.py")


@pytest.mark.parametrize("code", [
    "import importlib; importlib.import_module('tools.x')",
    "from importlib import import_module; import_module('tools.x')",
    "__import__('tools.x')",
    "__import__('tools', fromlist=['x'])",
    "import importlib; importlib.import_module('.x', package='tools')",
    "import runpy; runpy.run_module('tools.x')",
    "import pkgutil; pkgutil.resolve_name('tools.x:y')",
    "exec('import tools.x')",
])
def test_a_package_python_code_imports_at_run_time_is_walked(_tools_repo, code):
    """A module named to a call that imports it at run time is walked like one an
    ``import`` statement names: the PR rewriting it holds the hook back."""
    _assert_sibling_holds(_tools_repo, f'python3 -c "{code}"', "tools/x.py")


@pytest.mark.parametrize("code", [
    "import importlib, sys; importlib.import_module(sys.argv[1])",   # a name read at run time
    "import importlib; importlib.import_module('tools.' + 'x')",     # a name built by the code
    "import importlib; p = 'tools'; importlib.import_module(f'{p}.x')",
    "import importlib; importlib.import_module('.x', package=__name__)",  # relative to an unknown package
    "__import__('$MOD')",                                             # a name the shell fills in
])
def test_python_code_importing_a_name_it_does_not_spell_out_is_held_back(_tools_repo, code):
    named = guard.named_paths(f'python3 -c "{code}"')
    assert named.unsafe == "it imports a module by a name this guard cannot read"
    assert not _live(_tools_repo, f'python3 -c "{code}"')


def test_an_isolated_python_program_may_import_a_name_it_does_not_spell_out():
    """``-I`` keeps the checkout root off the program's path: nothing it imports
    by name comes from the checkout."""
    assert guard.named_paths('python3 -I -c "import importlib, sys; importlib.import_module(sys.argv[1])"').unsafe is None


@pytest.mark.parametrize("command,sibling", [
    ("node -e \"require(process.env.CLAUDE_PROJECT_DIR + '/tools/x')\"", "tools/helper.js"),
    ("node -e \"require(process.cwd() + '/tools/x.js')\"", "tools/helper.js"),
    ("ruby -e \"require \\\"#{ENV['CLAUDE_PROJECT_DIR']}/tools/x\\\"\"", "tools/helper.rb"),
    ("perl -e 'do $ENV{CLAUDE_PROJECT_DIR} . \"/tools/x.pl\"'", "tools/helper.pl"),
    ("python3 -c \"import os; exec(open(os.environ['CLAUDE_PROJECT_DIR'] + '/tools/x.py').read())\"",
     "tools/helper.py"),
    ("python3 -c \"import os; exec(open(f'{os.getcwd()}/tools/x.py').read())\"", "tools/helper.py"),
])
def test_a_script_code_joins_onto_the_checkout_path_names_its_directory(_tools_repo, command, sibling):
    """Code that builds the path itself — the project directory from its
    environment, the working directory — joined with a literal ``/tools/x``: the
    literal names the checkout's ``tools/x``."""
    _assert_sibling_holds(_tools_repo, command, sibling)


def test_an_isolated_python_program_still_has_its_code_read(tmp_path):
    """``-I`` is a flag to Python, not an option taking the next word: the ``-c``
    after it still starts code, and a file that code runs, changed by the PR, holds
    the hook back."""
    command = "python3 -I -c \"exec(open(r'tools/x.py').read())\""
    repo = _hook_repo(tmp_path, command, {"tools/x.py": "x = 1\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    write(repo.wt, "tools/x.py", "x = 2\n")
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_rollback_merges_the_held_keys_into_a_racing_writers_object(tmp_path, monkeypatch, capfd):
    """Another writer lands one JSON object between the scrub's write and its
    read-back: the spawn is refused, and the put-back keeps that writer's keys and
    puts back the held ones it did not re-state — at the file's own mode."""
    repo, rel = _hostile_untracked_local(tmp_path)
    path = repo.wt / rel
    original = json.loads(path.read_bytes())
    theirs = {"permissions": {"allow": ["Bash(npm test)"]}}
    real = guard._Dir.write
    raced = []

    def racing(self, name, data, mode, token, full=False):
        if name == "settings.local.json" and not raced:
            raced.append(data)
            data = json.dumps(theirs).encode()
        return real(self, name, data, mode, token, full=full)

    monkeypatch.setattr(guard._Dir, "write", racing)
    with pytest.raises(guard.SettingsGuardRefusal, match="did not read back as written"):
        _spawn(str(repo.wt))
    monkeypatch.setattr(guard._Dir, "write", real)
    assert raced, "the scrub never wrote the file"
    merged = {**theirs, "env": original["env"], "hooks": original["hooks"]}
    assert path.read_bytes() == guard._dump(merged)
    assert _mode(path) == 0o600
    assert _saved_bytes() == []
    assert "kept the edit made to this file while settings were held back, and put back" \
        in capfd.readouterr().err
    assert markers_present(repo.markers) == []


_FIFO_CHILD = """
import os, shutil, sys
from buddhi_review import claude_settings_guard as guard
with guard.window(sys.argv[1]):
    shutil.rmtree(os.path.join(sys.argv[1], ".claude"))
    os.mkfifo(os.path.join(sys.argv[1], ".claude"))
"""


def test_a_claude_dir_swapped_for_a_fifo_mid_spawn_never_blocks_the_put_back(tmp_path):
    """A run that leaves a FIFO where ``.claude/`` was: the put-back never opens it
    as a directory (which would wait for a writer forever) — it finishes, and says
    ``.claude`` was replaced."""
    repo, _ = _hostile_tracked(tmp_path)
    r = subprocess.run([sys.executable, "-c", _FIFO_CHILD, str(repo.wt)], env=_child_env(),
                       capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    assert ".claude was replaced while settings were held back" in r.stderr


@pytest.mark.parametrize("command", ["cd sub && sh ./tools/.*", "cd sub && cat tools/.?"])
def test_a_dot_glob_read_after_a_cd_may_climb_to_the_checkout_root(tmp_path, command):
    """``.*`` / ``.?`` can match ``..``: after ``cd sub``, ``./tools/.*`` may be read
    from the root as well as from ``sub``, and ``tools/..`` is the root itself."""
    repo = _hook_repo(tmp_path, command, {"sub/a.txt": "a\n"}, head={"README.md": "changed\n"})
    assert _hooks(_spawn(str(repo.wt))) == []


def test_code_glued_to_its_option_and_starting_with_an_expansion_is_read(_tools_repo):
    _assert_sibling_holds(_tools_repo, 'ruby -e"$PRELUDE"\'; load "./tools/x.rb"\'', "tools/helper.rb")


def test_a_module_named_through_a_variable_reaches_into_its_directory(_tools_repo):
    _assert_sibling_holds(_tools_repo, "node -e \"require('./tools/$NAME')\"", "tools/helper.js")


def test_a_module_a_loader_finds_at_the_checkout_root_holds_the_hook_back(tmp_path):
    """``require_relative 'helper'`` loads ``helper.rb`` from the checkout root,
    whose every module the PR controls; a module no root file provides
    (``require('fs')``) names nothing."""
    repo = _hook_repo(tmp_path, "ruby -e \"require_relative 'helper'\"", {"helper.rb": "X = 1\n"})
    assert _hooks(_spawn(str(repo.wt))) == []
    (tmp_path / "fs").mkdir()
    repo = _hook_repo(tmp_path / "fs", "node -e \"require('fs')\"", {"helper.rb": "X = 1\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]


def test_a_variable_interpreter_given_I_may_still_import_from_the_root(tmp_path):
    """``-I`` isolates only Python: run as ``"$PY" -Ic``, the program may still
    import from the checkout root, so a module the PR adds there holds it back."""
    repo = _hook_repo(tmp_path, "\"$PY\" -Ic 'import json'", {}, {"README.md": "changed\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    write(repo.wt, "json.py", "x = 1\n")
    assert _hooks(_spawn(str(repo.wt))) == []


@pytest.mark.parametrize("command", ["node -e \"require('/usr/lib/node_modules/x')\"",
                                     "python3 -c \"print(open('/etc/hosts').read())\""])
def test_an_absolute_path_in_code_that_the_checkout_lacks_names_nothing(tmp_path, command):
    repo = _hook_repo(tmp_path, command, {"tools/x.py": "x = 1\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]


_DECIDE_CHILD = """
import sys
from buddhi_review import claude_settings_guard as guard
hook = {"SessionStart": [{"hooks": [{"type": "command", "command": sys.argv[3]}]}]}
print(guard._Checker(sys.argv[1], guard._Base(sys.argv[1], sys.argv[2])).unchanged("hooks", hook))
"""


def test_a_fifo_at_a_path_a_hook_names_is_held_back_without_waiting(tmp_path):
    """A run can leave a FIFO where a hook names a file: reading it for a ``#!``
    line must not wait for a writer. The check finishes, and holds the hook back."""
    repo = _hook_repo(tmp_path, "cat tools/pipe", {"tools/x.txt": "x\n"})
    os.mkfifo(repo.wt / "tools/pipe")
    r = subprocess.run([sys.executable, "-c", _DECIDE_CHILD, str(repo.wt), repo.base, "cat tools/pipe"],
                       env=_child_env(), capture_output=True, text=True, timeout=20,
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("(False, ") and "tools/pipe" in r.stdout


# ── a Node program resolves a package name above its code ───────────────────────

_FMT = '"$CLAUDE_PROJECT_DIR"/.claude/hooks/fmt.js'
_PR_PACKAGE = "module.exports = 1  // the pull request's code\n"


def _commit(repo, files, message="the PR's package"):
    for rel, content in files.items():
        write(repo.wt, rel, content)
    git(repo.wt, "add", "-A")
    git(repo.wt, "commit", "-qm", message)


@pytest.mark.parametrize("value,key,node", [
    ("node x.js", None, True), ("bun x.ts", None, True), ("deno run x.ts", None, True),
    ("tsx x.ts", None, True), ("ts-node x.ts", None, True),
    ('"$RUN" x.js', None, True), ("uv run x", None, True),       # an interpreter of unknown family
    ("python3 x.py", None, False), ("ruby x.rb", None, False), ("perl x.pl", None, False),
    ("sh x.sh", None, False),
    ({"NODE_OPTIONS": "--require pkg"}, "env", True), ({"NODE_ENV": "production"}, "env", False),
])
def test_a_value_that_may_run_a_node_program_says_so(value, key, node):
    assert guard.named_paths(value, key=key).node is node


@pytest.mark.parametrize("command", [
    f"node {_FMT}", f"bun {_FMT}", f"tsx {_FMT}", f'"$NODE" {_FMT}',
    _FMT,                                                        # run by its #!/usr/bin/env node line
])
@pytest.mark.parametrize("added", [
    {"node_modules/prettier/index.js": _PR_PACKAGE},
    {".claude/node_modules/prettier/index.js": _PR_PACKAGE},
    {"package.json": json.dumps({"name": "prettier", "exports": "./evil.js"}), "evil.js": _PR_PACKAGE},
], ids=["root-node_modules", "claude-node_modules", "package-json-self-reference"])
def test_a_package_a_node_script_requires_is_resolved_above_its_directory(tmp_path, command, added):
    """``fmt.js`` requires ``prettier`` by name. Node finds it in ``node_modules/``
    in any directory above the script, or through the nearest ``package.json`` that
    names itself ``prettier``: a PR that adds either, leaving the hook and the
    script's directory as base has them, holds the hook back."""
    repo = _hook_repo(tmp_path, command,
                      {".claude/hooks/fmt.js": "#!/usr/bin/env node\nrequire('prettier')\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    _commit(repo, added)
    assert _hooks(_spawn(str(repo.wt))) == []
    assert markers_present(repo.markers) == []


@pytest.mark.parametrize("command,added", [
    ("node -e \"require('prettier')\"", "node_modules/prettier/index.js"),
    ("node -p \"require('prettier')\"", "node_modules/prettier/index.js"),
    (f"node -r prettier {_FMT}", "node_modules/prettier/index.js"),
    ("node --import prettier -e 1", "node_modules/prettier/index.js"),
    ("cd \"$CLAUDE_PROJECT_DIR\"/.claude/hooks && node -e \"require('prettier')\"",
     ".claude/node_modules/prettier/index.js"),
])
def test_a_package_node_code_or_a_preload_names_is_resolved_from_where_it_runs(tmp_path, command, added):
    """Inline code and a preload resolve a package name from the working directory
    upward — the checkout root, or where a ``cd`` left the shell."""
    repo = _hook_repo(tmp_path, command, {".claude/hooks/fmt.js": "1\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    _commit(repo, {added: _PR_PACKAGE})
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_package_node_options_preloads_is_resolved_from_the_checkout_root(tmp_path):
    """``NODE_OPTIONS=--require prettier`` names a package, not a path: a
    ``node_modules/prettier`` a run leaves in the checkout, which base does not
    have, holds the env back."""
    base = {SETTINGS: {"env": {"NODE_OPTIONS": "--require prettier"}}}
    repo = make_pr_repo(tmp_path, base, {"docs/guide.md": "x\n"})
    guard.install_base_resolver(lambda checkout: repo.base)
    assert "env" in _spawn(str(repo.wt))[SETTINGS]
    write(repo.wt, "node_modules/prettier/index.js", _PR_PACKAGE)
    assert "env" not in _spawn(str(repo.wt))[SETTINGS]


def test_a_node_hook_stays_live_while_the_packages_are_base_s(tmp_path):
    """Base's own ``package.json`` and committed ``node_modules`` keep a Node hook
    live; a PR's ``imports`` map in that ``package.json`` holds it back."""
    files = {".claude/hooks/fmt.js": "require('prettier')\n", "package.json": '{"name": "app"}\n',
             "node_modules/prettier/index.js": "module.exports = 1\n"}
    repo = _hook_repo(tmp_path, f"node {_FMT}", files)
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]
    _commit(repo, {"package.json": '{"name": "app", "imports": {"#fmt": "./evil.js"}}\n',
                   "evil.js": _PR_PACKAGE})
    assert _hooks(_spawn(str(repo.wt))) == []


def test_a_python_hook_is_not_held_back_by_a_node_package(tmp_path):
    repo = _hook_repo(tmp_path, f"python3 {S}", {".claude/hooks/check.py": "import json\n"},
                      {"node_modules/prettier/index.js": _PR_PACKAGE, "package.json": "{}\n"})
    assert _hooks(_spawn(str(repo.wt))) == ["SessionStart"]


def test_a_node_hook_in_a_checkout_below_the_repository_top_is_held_back(tmp_path):
    """From a checkout below the repository's top, Node climbs on into the
    repository's own directories, which the PR controls too and this guard cannot
    compare. A Python hook there is unaffected."""
    repo = make_pr_repo(tmp_path, {"app/.claude/hooks/fmt.js": "require('prettier')\n",
                                   "app/.claude/hooks/check.py": "import json\n"}, {})
    app = str(repo.wt / "app")
    checker = guard._Checker(app, guard._Base(app, repo.base))
    live, why = checker.unchanged("hooks", command_hook("SessionStart", f"node {_FMT}"))
    assert not live and "above the checkout" in why
    assert checker.unchanged("hooks", command_hook("SessionStart", f"python3 {S}")) == (True, "")
