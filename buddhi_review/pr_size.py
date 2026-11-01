"""The measured size of the whole proposed pull request, for protection checks.

When a pull request lists protections, every protection verification is told how
big the PR would be if the candidate fix were kept: additions, deletions and their
sum, from the PR's comparison base to the EXACT tree the round's commit would hold.

* **The tree** is projected (:func:`project_retained_tree`) by running the real
  staging decision — :func:`buddhi_review.commit_push.stage_plan` and its
  executor — against a private copy of the index. The live index is never staged,
  reset or rewritten; whatever staging would withhold is withheld here too, and
  nothing else is.
* **The base** is the PR's named base branch, resolved by repository identity
  (:class:`BaseTip`, the same discipline as the settings guard's
  :class:`buddhi_review.merge.PullRequestBase`), then the UNIQUE merge-base with
  ``HEAD``. Re-resolved for every measurement.
* **The count** is one ``git diff --numstat -M <base> <tree>``. A pure rename adds
  nothing; an empty complete diff is a genuine zero.

Any doubt — an incomplete status read, a failed step, a binary entry, malformed
output, an ambiguous or missing base — makes the whole measurement UNAVAILABLE,
with a reason and no numbers. A failure is never reported as zero.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from buddhi_review import commit_push, merge

Run = Callable[..., "subprocess.CompletedProcess[str]"]

_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_NUMSTAT_RE = re.compile(r"^(\d+|-)\t(\d+|-)\t.+$")


class Unavailable(Exception):
    """A measurement step that cannot produce trustworthy evidence."""


@dataclass(frozen=True)
class PrSize:
    """One measurement. ``available`` False carries a ``reason`` and no numbers."""
    available: bool
    reason: Optional[str] = None
    base_branch: Optional[str] = None
    comparison_base: Optional[str] = None
    additions: Optional[int] = None
    deletions: Optional[int] = None
    tree: Optional[str] = None

    @property
    def total(self) -> Optional[int]:
        if not self.available or self.additions is None or self.deletions is None:
            return None
        return self.additions + self.deletions

    def render(self) -> str:
        """The console form shown after a verifier verdict."""
        if not self.available:
            return f"UNAVAILABLE ({self.reason}); no numbers, and not zero"
        return (f"+{self.additions} -{self.deletions} = {self.total} changed lines "
                f"against base branch `{self.base_branch}` "
                f"(comparison base {str(self.comparison_base)[:7]})")

    def prompt_text(self) -> str:
        """The evidence the verifier reads."""
        if not self.available:
            return f"UNAVAILABLE ({self.reason}); no numbers, and not zero"
        return (f"base branch: {self.base_branch}\n"
                f"comparison base commit: {self.comparison_base}\n"
                f"additions: {self.additions}\n"
                f"deletions: {self.deletions}\n"
                f"total changed lines: {self.total}")


def unavailable(reason: str) -> PrSize:
    return PrSize(available=False, reason=reason)


def _out(run: Run, argv, cwd: str, what: str) -> str:
    try:
        r = run(list(argv), cwd=cwd)
    except subprocess.TimeoutExpired:
        raise Unavailable(f"{what} timed out")
    except (subprocess.SubprocessError, OSError, UnicodeDecodeError) as exc:
        raise Unavailable(f"{what} could not run ({type(exc).__name__})")
    if getattr(r, "returncode", 1) != 0:
        raise Unavailable(f"{what} failed")
    return getattr(r, "stdout", "") or ""


def project_retained_tree(cwd: str, *, run: Run = commit_push._default_run) -> dict:
    """``{available, tree, reason}``: the tree the round's commit would hold right
    now, built by the shared staging plan in a scratch copy of the index."""
    made = commit_push.scratch_run(cwd, run=run)
    if made is None:
        return {"available": False, "tree": None,
                "reason": "the index could not be copied for the projection"}
    scratch, srun = made
    try:
        plan = commit_push.stage_plan(cwd, run=srun)
        if not plan.complete:          # an un-normalized plan is never complete
            return {"available": False, "tree": None,
                    "reason": plan.reason or "the status read was incomplete"}
        added = commit_push.execute_stage_plan(cwd, plan, run=srun)
        if getattr(added, "returncode", 1) != 0:
            return {"available": False, "tree": None,
                    "reason": "the staging projection failed"}
        tree = _out(srun, ["git", "write-tree"], cwd, "git write-tree").strip()
        if not _SHA_RE.fullmatch(tree):
            return {"available": False, "tree": None,
                    "reason": "git write-tree returned no tree"}
        return {"available": True, "tree": tree, "reason": None}
    except Unavailable as exc:
        return {"available": False, "tree": None, "reason": str(exc)}
    except subprocess.TimeoutExpired:
        return {"available": False, "tree": None, "reason": "a staging step timed out"}
    except (subprocess.SubprocessError, OSError, TypeError, UnicodeDecodeError) as exc:
        return {"available": False, "tree": None,
                "reason": f"a staging step failed ({type(exc).__name__})"}
    finally:
        try:
            os.unlink(scratch)
        except OSError:
            pass


def numstat_totals(base: str, tree: str, *, cwd: str,
                   run: Run = commit_push._default_run) -> Tuple[int, int]:
    """``(additions, deletions)`` from ``base`` to ``tree`` with rename detection,
    or :class:`Unavailable` for any binary entry or unparseable line."""
    text = _out(run, ["git", "diff", "--no-ext-diff", "--no-textconv", "--numstat",
                      "-M", base, tree, "--"], cwd, "git diff --numstat")
    adds = dels = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        m = _NUMSTAT_RE.match(line)
        if m is None:
            raise Unavailable("unreadable numstat output")
        if m.group(1) == "-" or m.group(2) == "-":
            raise Unavailable("a binary file has no line count")
        adds += int(m.group(1))
        dels += int(m.group(2))
    return adds, dels


class BaseTip(merge.PullRequestBase):
    """The PR's base branch and its tip, by the same identity discipline the
    settings guard uses: the remote whose URL names the PR's repository, the base
    branch fetched by its full ref, an unfetchable tip trusted only when it equals
    GitHub's own ``baseRefOid``. Never cached — each call re-resolves."""

    def tip(self, checkout: str) -> Tuple[str, str]:
        argv = ["gh", "pr", "view", self.pr, "--json", "baseRefName,baseRefOid,url"]
        if self.repo:
            argv += ["-R", self.repo]
        try:
            data = json.loads(self._output(argv, checkout, "gh pr view"))
        except merge._Unresolved as exc:
            raise Unavailable(str(exc))
        except ValueError:
            raise Unavailable("gh pr view did not return JSON")
        if not isinstance(data, dict):
            raise Unavailable("gh pr view did not return a PR")
        base = data.get("baseRefName")
        if (not isinstance(base, str) or not merge._REF_NAME_RE.fullmatch(base)
                or base.startswith("-") or ".." in base):
            raise Unavailable("the PR names no usable base branch")
        m = merge._PR_URL_RE.match(str(data.get("url") or ""))
        from_url = m.group(1) if m else ""
        repo = self.repo or from_url
        if not repo:
            raise Unavailable("cannot tell which repository the PR belongs to")
        try:
            remote = self._remote(repo, merge._host(from_url), checkout)
        except merge._Unresolved as exc:
            raise Unavailable(str(exc))
        if not merge._REF_NAME_RE.fullmatch(remote) or remote.startswith("-"):
            raise Unavailable(f"no usable git remote points at {repo}")
        tracking = f"refs/remotes/{remote}/{base}"
        try:
            fetched = self._run(["env", "GIT_TERMINAL_PROMPT=0", "git", "fetch",
                                 "--no-tags", "--quiet", remote,
                                 f"+refs/heads/{base}:{tracking}"], cwd=checkout)
            fetch_ok = getattr(fetched, "returncode", 1) == 0
        except (subprocess.SubprocessError, OSError):
            fetch_ok = False
        try:
            tip = self._output(["git", "show-ref", "--verify", "--hash", tracking],
                               checkout, "git show-ref").strip()
        except merge._Unresolved:
            raise Unavailable(f"{tracking} does not exist")
        if not _SHA_RE.fullmatch(tip):
            raise Unavailable(f"{tracking} does not exist")
        if not fetch_ok:
            current = data.get("baseRefOid")
            if not isinstance(current, str) or tip != current:
                raise Unavailable(f"could not fetch {base}, and {tracking} is not "
                                  f"the PR's current base")
        return base, tip


def comparison_base(cwd: str, tip: str, *, run: Run = commit_push._default_run) -> str:
    """The ONE merge-base of ``HEAD`` and ``tip``; none or several is unavailable."""
    found = [ln.strip() for ln in
             _out(run, ["git", "merge-base", "--all", "HEAD", tip], cwd,
                  "git merge-base").splitlines() if ln.strip()]
    if len(found) != 1 or not _SHA_RE.fullmatch(found[0]):
        raise Unavailable("no merge-base with the base branch" if not found
                          else "the merge-base with the base branch is ambiguous")
    return found[0]


def measure_pr_size(
    cwd: str,
    *,
    pr=None,
    repo: Optional[str] = None,
    run: Run = commit_push._default_run,
    base_tip: Optional[Callable[[str], Tuple[str, str]]] = None,
) -> PrSize:
    """Measure the whole proposed PR once, right now. ``base_tip(cwd)`` returns
    ``(base_branch, tip_sha)`` (default: :class:`BaseTip` over ``gh``)."""
    try:
        if base_tip is None:
            if pr is None:
                raise Unavailable("no pull request to measure against")
            base_tip = BaseTip(pr, repo, run=run).tip
        branch, tip = base_tip(cwd)
        base = comparison_base(cwd, tip, run=run)
        projected = project_retained_tree(cwd, run=run)
        if not projected["available"]:
            raise Unavailable(projected["reason"] or "the retained tree is unavailable")
        adds, dels = numstat_totals(base, projected["tree"], cwd=cwd, run=run)
    except Unavailable as exc:
        return unavailable(str(exc))
    except Exception as exc:  # a measurement never takes the fix path down with it
        return unavailable(f"measurement failed ({type(exc).__name__})")
    return PrSize(available=True, base_branch=branch, comparison_base=base,
                  additions=adds, deletions=dels, tree=projected["tree"])
