"""Real git repositories and a recording ``claude`` stub for the tests of
:mod:`buddhi_review.claude_settings_guard`.

Only the ``claude`` subprocess and ``gh`` are stubbed; every git call is real.
No hook is ever run: a test hook's command only ``touch``es a marker under the
test's tmp dir, and the tests assert that no marker ever appears.
"""
import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import buddhi_review

# The package under test's own root: child processes import THIS tree, never an
# installed copy elsewhere on the machine.
PACKAGE_ROOT = str(Path(buddhi_review.__file__).resolve().parent.parent)

SETTINGS = ".claude/settings.json"
LOCAL_SETTINGS = ".claude/settings.local.json"
OFFLINE_GIT = {  # a real `git fetch` over https fails in milliseconds, offline
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "protocol.https.allow",
    "GIT_CONFIG_VALUE_0": "never",
    "GIT_CONFIG_KEY_1": "protocol.ssh.allow",
    "GIT_CONFIG_VALUE_1": "never",
}
PR_VIEW = {"baseRefName": "main", "url": "https://github.com/o/r/pull/7"}


def git(cwd, *args: str, check: bool = True, run: Callable = subprocess.run) -> str:
    r = run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
            stdin=subprocess.DEVNULL)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {r.stderr}")
    return r.stdout


def write(root, rel: str, content) -> Path:
    path = Path(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, (dict, list)):
        content = json.dumps(content, indent=2) + "\n"
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    return path


def touch(markers: Path, name: str) -> str:
    """A hook command whose only effect would be to create a marker file."""
    return f"touch {shlex.quote(str(markers / ('HOOK_RAN_' + name)))}"


def command_hook(event: str, command: str, matcher: Optional[str] = None) -> Dict:
    entry: Dict = {"hooks": [{"type": "command", "command": command}]}
    if matcher is not None:
        entry["matcher"] = matcher
    return {event: [entry]}


def hostile(markers: Path) -> Dict:
    """What §0's probe committed: a SessionStart hook (fires with no tool call)
    and a PreToolUse(Bash) hook."""
    hooks = dict(command_hook("SessionStart", touch(markers, "SessionStart")))
    hooks.update(command_hook("PreToolUse", touch(markers, "PreToolUse"), "Bash"))
    return {"hooks": hooks}


def markers_present(markers: Path) -> List[str]:
    return sorted(p.name for p in markers.iterdir()) if markers.exists() else []


@dataclass
class PrRepo:
    """A primary checkout on ``main`` (the base) plus a linked worktree on the PR
    branch — the shape the loop runs in."""
    primary: Path
    wt: Path
    base: str
    head: str
    markers: Path

    def settings_bytes(self, rel: str = SETTINGS) -> bytes:
        return (self.wt / rel).read_bytes()

    def flags(self) -> str:
        return git(self.wt, "ls-files", "-v")

    def status(self) -> str:
        return git(self.wt, "status", "--porcelain", "--untracked-files=all")


def make_pr_repo(tmp_path: Path, base_files: Dict[str, object],
                 head_files: Optional[Dict[str, object]] = None, *,
                 removed=(), branch: str = "feature") -> PrRepo:
    """Base commit on ``main`` holding ``base_files``; the PR branch (checked out in
    a linked worktree) writes ``head_files`` and deletes ``removed`` in ONE commit
    — an empty commit when it changes nothing. ``origin`` points at GitHub, and
    ``refs/remotes/origin/main`` is the base commit, made with ``update-ref``."""
    primary, wt, markers = tmp_path / "primary", tmp_path / "wt", tmp_path / "markers"
    primary.mkdir()
    markers.mkdir()
    git(primary, "init", "-q", "-b", "main")
    git(primary, "config", "user.email", "t@example.com")
    git(primary, "config", "user.name", "t")
    git(primary, "config", "commit.gpgsign", "false")
    # Hermetic: the machine's global excludes file and hooks never apply here.
    git(primary, "config", "core.excludesFile", os.devnull)
    git(primary, "config", "core.hooksPath", os.devnull)
    for rel, content in {"README.md": "base\n", **base_files}.items():
        write(primary, rel, content)
    git(primary, "add", "-A")
    git(primary, "commit", "-qm", "base")
    base = git(primary, "rev-parse", "HEAD").strip()
    git(primary, "remote", "add", "origin", "https://github.com/o/r.git")
    git(primary, "update-ref", "refs/remotes/origin/main", base)
    git(primary, "worktree", "add", "-q", "-b", branch, str(wt), "main")
    for rel, content in (head_files or {}).items():
        write(wt, rel, content)
    for rel in removed:
        (wt / rel).unlink()
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "--allow-empty", "-m", "pr")
    head = git(wt, "rev-parse", "HEAD").strip()
    return PrRepo(primary, wt, base, head, markers)


ROOT_LOCAL_SETTINGS = "<canonical git root>/" + LOCAL_SETTINGS


def _canonical_git_root(cwd: str) -> Optional[str]:
    r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return os.path.dirname(r.stdout.strip()) if r.returncode == 0 else None


def seen_settings(cwd: str, argv: Optional[List[str]] = None) -> Dict[str, Optional[dict]]:
    """What a ``claude`` started in ``cwd`` with ``argv`` would load from the
    project, read the way it reads it (following symlinks). Like Claude Code, a
    ``--setting-sources`` list decides which sources load; local settings come from
    ``cwd`` AND from the repository's canonical git root (for a linked worktree,
    the primary checkout)."""
    sources = {"user", "project", "local"}
    if argv and "--setting-sources" in argv:
        sources = set(argv[argv.index("--setting-sources") + 1].split(","))
    files = []
    if "project" in sources:
        files.append((SETTINGS, os.path.join(cwd, SETTINGS)))
    if "local" in sources:
        files.append((LOCAL_SETTINGS, os.path.join(cwd, LOCAL_SETTINGS)))
        root = _canonical_git_root(cwd) if argv is not None else None
        if root and os.path.realpath(root) != os.path.realpath(cwd):
            files.append((ROOT_LOCAL_SETTINGS, os.path.join(root, LOCAL_SETTINGS)))
    out: Dict[str, Optional[dict]] = {}
    for rel, path in files:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        try:
            obj = json.loads(data.decode("utf-8-sig"))
        except ValueError:
            obj = None
        out[rel] = obj if isinstance(obj, dict) else None
    return out


@dataclass
class Spawn:
    argv: List[str]
    cwd_kwarg: Optional[str]
    effective: str
    settings: Dict[str, Optional[dict]]
    raw: Dict[str, bytes]

    @property
    def hooks(self) -> List[str]:
        events = set()
        for obj in self.settings.values():
            events.update((obj or {}).get("hooks", {}) or {})
        return sorted(events)

    @property
    def keys(self) -> List[str]:
        keys = set()
        for obj in self.settings.values():
            keys.update(obj or {})
        return sorted(keys)

    @property
    def sandboxed(self) -> bool:
        return os.path.basename(self.argv[0]) == "sandbox-exec"


@dataclass
class ClaudeStub:
    """Stands in for ``subprocess.run``: records every ``claude`` spawn (the cwd it
    was given, the directory it would really run in, the settings it would load),
    answers ``gh pr view`` from ``pr_view``, and passes everything else — every
    git call — to the real ``subprocess.run``."""
    real_run: Callable
    pr_view: Dict = field(default_factory=lambda: dict(PR_VIEW))
    claude_stdout: str = "{}"
    during: Optional[Callable[["Spawn"], None]] = None
    spawns: List[Spawn] = field(default_factory=list)
    gh_calls: List[List[str]] = field(default_factory=list)
    fetches: List[subprocess.CompletedProcess] = field(default_factory=list)

    def __call__(self, argv, *args, **kwargs):
        argv = [str(a) for a in argv]
        names = [os.path.basename(a) for a in argv[:4]]
        if "claude" in names:
            cwd = kwargs.get("cwd")
            effective = os.path.realpath(cwd or os.getcwd())
            raw = {}
            for rel in (SETTINGS, LOCAL_SETTINGS):
                with_path = os.path.join(effective, rel)
                if os.path.isfile(with_path):
                    raw[rel] = Path(with_path).read_bytes()
            spawn = Spawn(argv, cwd, effective, seen_settings(effective, argv), raw)
            self.spawns.append(spawn)
            if self.during is not None:
                self.during(spawn)
            text = kwargs.get("text")
            out = self.claude_stdout
            return subprocess.CompletedProcess(argv, 0, out if text else out.encode(),
                                               "" if text else b"")
        if names[0] == "gh":
            self.gh_calls.append(argv)
            if argv[1:3] == ["pr", "view"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps(self.pr_view), "")
            return subprocess.CompletedProcess(argv, 1, "", "gh: not stubbed")
        result = self.real_run(argv, *args, **kwargs)
        if "git" in names and "fetch" in argv:
            self.fetches.append(result)
        return result
