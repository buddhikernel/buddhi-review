"""Command-line entrypoint for the skill: ``buddhi-review`` / ``python -m buddhi_review``.

Subcommands:
  ``self-check`` — run the kernel-driven pipeline on built-in fixtures (no network,
                   no ``claude``); proves the kernel decides + is the post-install
                   health check. Exits non-zero on any deviation.
  ``review-pr``  — the review loop over an open PR: real ``gh`` comment ingest →
                   classify → kernel decision → act (snapshot/rollback fix-apply with
                   the safety floor, console escalation + answer poll, opt-in
                   squash-merge), driven by the multi-round quiescence loop with
                   clean-review detection and per-round re-request handling.
  ``open-pr``    — create a PR from local work, then launch the loop: resolve repo →
                   git decision tree (commit/branch/push) → gh pr create → launch the
                   review adapter detached (PR URL is the last stdout line).
  ``setup``      — the interactive onboarding wizard (plan, repo, reviewer fleet).
  ``status``     — print per-repo setup status as JSON (``repo_confirmed`` /
                   ``has_global_default``) for the SKILL.md gate to shell out to.
  ``install-skills`` — copy the bundled ``/review-pr`` + ``/open-pr`` skills into Claude
                   Code's skills dir, provenance-safe: idempotent, skips unmodified/current
                   files, refuses to clobber a user edit without ``--force``.
  ``upgrade``    — upgrade this installation the way it was installed (editable checkout
                   / pipx / uv tool / venv), then re-sync the skills. An OS-managed or
                   unidentifiable interpreter is told what to run, never written to.

Any other command word is not one of ours: it is routed to a separately-installed
backend that CLAIMS it (which runs it), or — the normal free-only state, and equally
a plain typo — answered with a one-shot upgrade notice and exit 2 (never a half-run).
See :func:`_dispatch_unclaimed_command`.

Answers come from the terminal.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from typing import Callable, List, Optional, TextIO

from buddhi_review import __version__, gh_ingest, model_call, round_driver, update_banner, upsell
from buddhi_review.actuators import default_fix_dispatch
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.backends import launch_review_loop, select_command_backend
from buddhi_review.config import (
    active_reviewers,
    auto_merge as resolve_auto_merge,
    has_global_default,
    load_config,
    notifier_channel,
    plan,
    repo_entry,
)
from buddhi_review.fix_apply import VERIFY_MODES
from buddhi_review.loop import Comment, process_comments
from buddhi_review.notifier import ConsoleNotifier
from buddhi_review.seams import ConsoleEscalation

# label → its representative self-check fixture text and the disposition we expect
# the kernel to reach: the same label must always yield the same disposition.
_SELF_CHECK = [
    ("SUBSTANTIVE", "this null check is missing", "fix"),
    ("COSMETIC", "rename this variable for clarity", "fix"),
    ("OUTDATED", "this refers to code that no longer exists", "skip"),
    ("INVALID", "this suggestion is simply wrong", "skip"),
    ("BUSINESS_QUESTION", "should we drop this column?", "escalate"),
    ("PR_DESCRIPTION", "the PR body is out of date", "fix"),  # → PR-body rewriter
    ("__GARBAGE__", "force a classifier failure", "escalate"),  # → CLASSIFICATION_FAILED
]


def _self_check(argv: Optional[List[str]] = None) -> int:
    print(f"buddhi_review {__version__} — kernel-driven self-check\n")

    def runner(prompt: str) -> str:
        # Stub model: echo back the label embedded in the fixture text. Garbage text
        # produces no parseable label → the loop yields CLASSIFICATION_FAILED.
        for label, text, _ in _SELF_CHECK:
            if text in prompt and label != "__GARBAGE__":
                return json.dumps({"label": label, "reason": "self-check"})
        return "the model said something unparseable"

    comments = [
        Comment(id=f"c{i}", text=text, source=f"reviewer-{i}")
        for i, (_, text, _) in enumerate(_SELF_CHECK)
    ]
    adapter = ReviewAdapter()
    results = process_comments(comments, adapter=adapter, classify_runner=runner)
    # Self-check leaves no answer files behind in the temp dir.
    for ask in adapter.escalation.delivered:
        adapter.escalation.notifier.clear(ConsoleEscalation.to_channel_ask(ask))

    ok = True
    for (label, _text, expected), r in zip(_SELF_CHECK, results):
        got = r.disposition
        flag = "ok " if got == expected else "FAIL"
        if got != expected:
            ok = False
        shown = r.classification.label
        print(f"  [{flag}] {shown:20} kernel={r.kernel_status:13} disposition={got:14} (want {expected})")

    print("\nSELF-CHECK " + ("OK — the kernel decided every disposition." if ok else "FAILED."))
    return 0 if ok else 1


def _effective_auto_merge(args: argparse.Namespace, cfg: dict) -> bool:
    """The auto-merge decision for this run, as a concrete bool.

    Tri-state resolution, fail-closed: an explicit ``--auto-merge`` /
    ``--no-auto-merge`` flag (``True`` / ``False``) always wins; an UNSET flag
    (``None``) falls back to the repo's configured ``repos[<repo>].auto_merge``,
    else off. There is deliberately no global-default tier (the merge is opt-in per
    repo). Shared by the ``review-pr`` front door and the ``run-loop`` engine so the
    two never drift, and so a genuinely-unset run with no per-repo config resolves
    to ``False`` (never auto-merges a PR the operator did not opt into)."""
    if args.auto_merge is not None:
        return args.auto_merge
    return resolve_auto_merge(cfg, args.repo)


def _review_pr(args: argparse.Namespace) -> int:
    """The ``review-pr`` front door: route the loop launch through the backend
    dispatcher, then return immediately. With nothing extra installed this runs the
    free engine (``run-loop`` detached via ``launch-review.sh``); a separately
    installed, active backend would take over the same command. The backend prints
    its own "where to watch" line after the choice is made."""
    cwd = args.cwd or os.getcwd()
    # A muted, non-blocking one-liner naming any available Buddhi / workflow update.
    # Decoration → stderr (the front door's stdout carries the launch's own output);
    # fully fail-open so it never blocks or delays the launch.
    update_banner.maybe_emit_update_banner(cwd=cwd, stream=sys.stderr)
    # Resolve auto-merge to a concrete bool BEFORE the backend hand-off, so the
    # resolved value reaches a separately-installed PRO backend as a definite bool
    # via the argv seam (an unset None would otherwise let per-repo config be lost
    # at the seam). Resolved again in run-loop for a directly-invoked engine.
    # Only load config when the flag is unset AND a repo is given: an explicit
    # --auto-merge/--no-auto-merge never consults cfg, and repo_entry() returns
    # None for repo=None regardless of cfg — so both cases would pay for I/O and
    # a possible missing-config warning for a value that's never used.
    cfg = load_config() if args.auto_merge is None and args.repo else {}
    effective_auto_merge = _effective_auto_merge(args, cfg)
    return launch_review_loop(
        args.pr, args.repo, cwd,
        auto_merge=effective_auto_merge,
        verify_fixes=args.verify_fixes,
        max_rounds=args.max_rounds,
        test_failure_mode=args.test_failure_mode,
        fix_pr_description=args.fix_pr_description,
        rr=args.rr,
        rr_active=args.rr_active,
        rr_none=args.rr_none,
    )


def _run_loop(args: argparse.Namespace) -> int:
    """The in-process free review engine (run detached by ``launch-review.sh``).

    This is the free backend's loop body: the launch preflight gates + the kernel
    round driver. It is invoked as ``python -m buddhi_review run-loop`` from the
    launcher, never directly by a user."""
    cfg = load_config()
    # pr/repo are carried so the console answer file lands at
    # review-answer-<repo>-PR<pr>-<ask>.md — keyed per (repo, PR) like the log.
    notifier = ConsoleNotifier(pr=args.pr, repo=args.repo)
    notifier.startup_log()
    print(f"plan: {plan(cfg)} · reviewers: {', '.join(active_reviewers(cfg, args.repo))} · channel: {notifier_channel(cfg)}")

    cwd = args.cwd or os.getcwd()
    # Launch preflight gates (console). (1) Refuse the repo's PRIMARY
    # checkout while it sits on the PR branch — fixers must run in a dedicated
    # worktree so an uncommitted edit can never strand on the default branch.
    # (2) Fail closed on a repo with no confirmed reviewer fleet and no global
    # default to fall back to (an unconfirmed repo WITH a default proceeds on it).
    if round_driver.refuse_primary_checkout(args.pr, args.repo, cwd):
        return 2
    round_driver.enforce_repo_confirmation_gate(args.repo, cfg)
    # Auto-size the round budget from the PR diff when neither --max-rounds nor
    # BUDDHI_MAX_ROUNDS is set (best-effort; a fetch failure falls back to the
    # default budget, with a one-line stderr warning so the fallback is never
    # silent). resolve_max_rounds inside RoundDriver consumes diff_lines.
    diff_lines = None
    if args.max_rounds is None and round_driver._env_max_rounds() is None:
        diff_lines = gh_ingest.fetch_pr_diff_lines(args.pr, repo=args.repo, cwd=cwd)
        if diff_lines is None:
            print(f"[setup] could not measure PR #{args.pr} diff size — falling "
                  f"back to --max-rounds={round_driver.MAX_ROUNDS_FALLBACK}",
                  file=sys.stderr)
    # Resolve the plan ONCE and thread it into every model call, so a per-comment
    # call never re-reads config.yaml (and never re-warns a config-less user).
    plan_name = plan(cfg)
    adapter = ReviewAdapter(
        ingest_source=gh_ingest.ingest_source(args.pr, repo=args.repo, cwd=cwd),
        escalation=ConsoleEscalation(notifier=notifier),
    )
    # Every deterministic model call is role-sized: explicit effort per role,
    # MCP isolation, [1m] only on a >160K-token prompt.
    driver = round_driver.RoundDriver(
        args.pr,
        repo=args.repo,
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        # cwd pins the classifier subprocess to the target repo so its escalation
        # criteria ("running inside the repository … consult the docs") hold even
        # when review-pr is launched detached with --cwd from another checkout.
        classify_runner=model_call.text_runner("classifier", plan=plan_name, cwd=cwd),
        clean_llm=lambda prompt: model_call.run_model_json(
            prompt, role="clean-review-detector", plan=plan_name),
        quota_llm=lambda prompt: model_call.run_model_json(
            prompt, role="quota-detector", plan=plan_name),
        fix_dispatch=default_fix_dispatch(
            cwd=cwd,
            plan=plan_name,
            verify_runner=model_call.text_runner("fix-verify", plan=plan_name),
            verify_mode=args.verify_fixes,
            # A PR_DESCRIPTION comment rewrites the PR body in place (on by
            # default); the rewriter model is cwd-pinned like the classifier.
            pr=args.pr,
            repo=args.repo,
            fix_pr_description=args.fix_pr_description,
            rewrite_runner=model_call.text_runner(
                "pr-description-rewriter", plan=plan_name, cwd=cwd),
        ),
        max_rounds=args.max_rounds,
        diff_lines=diff_lines,
        # Same tri-state resolution as the review-pr front door (flag > per-repo
        # config > off), applied again here so a directly-invoked run-loop (or a
        # front door that forwarded an unset None) still honors the config.
        auto_merge=_effective_auto_merge(args, cfg),
        rr=args.rr,
        rr_active=args.rr_active,
        rr_none=args.rr_none,
        test_gate=(args.test_failure_mode != "off"),
    )
    try:
        outcome = driver.run()
    except RuntimeError as exc:
        print(f"review-pr: the round loop failed — {exc}", file=sys.stderr)
        return 1
    print(f"\nreview-pr PR #{args.pr}: {outcome.status} after {outcome.rounds} round(s)"
          + (" — landed (merged)" if outcome.merged else ""))
    # A transient, contextual upgrade nudge — shown only when this free run handed
    # work back to a human AND no active paid backend is present (all gating inside).
    upsell.maybe_emit_run_end_nudge(outcome.status)
    return 0 if outcome.status == "clean" else 1


def _open_pr(args: argparse.Namespace) -> int:
    from buddhi_review import open_pr
    # A muted, non-blocking one-liner naming any available Buddhi / workflow update.
    # Emitted to stderr so the actuator's stdout URL contract (PR URL is the last
    # stdout line) is never touched; fully fail-open so the launch is unaffected.
    update_banner.maybe_emit_update_banner(cwd=args.cwd or os.getcwd(), stream=sys.stderr)
    if not args.title:
        print("open-pr: --title is required.", file=sys.stderr)
        return 2
    return open_pr.actuate(
        repo=args.repo,
        cwd=args.cwd,
        base=args.base,
        title=args.title,
        body=args.body or "",
        branch=args.branch,
        branch_prefix=args.branch_prefix,
        no_loop=args.no_loop,
        max_rounds=args.max_rounds,
    )


def _setup(args: argparse.Namespace) -> int:
    from buddhi_review import wizard
    argv = ["--repo", args.repo] if getattr(args, "repo", None) else None
    return wizard.run(argv=argv)


def _status(args: argparse.Namespace) -> int:
    """Print the per-repo setup status as JSON for the SKILL.md gate to shell out
    to: whether ``--repo`` has a CONFIRMED reviewer fleet (a ``repos[<repo>]``
    entry) and whether a global default exists to fall back to. Pure read — one
    config load, no network, no loop. JSON is the only thing on stdout (any
    config-missing warning goes to stderr)."""
    cfg = load_config()
    print(json.dumps({
        "repo_confirmed": repo_entry(cfg, args.repo) is not None,
        "has_global_default": has_global_default(cfg),
    }))
    return 0


def _install_skills(args: argparse.Namespace) -> int:
    """Install (or update / uninstall) the bundled ``/review-pr`` + ``/open-pr`` skills
    into Claude Code's skills directory, provenance-safe. Idempotent and safe to re-run:
    it skips unmodified/current files and refuses to clobber a user edit unless
    ``--force`` is given. Prints a per-file summary + the target path; exits non-zero
    only on a real error (a preserved CONFLICT is an expected, safe outcome → exit 0)."""
    from buddhi_review import skill_install

    try:
        summary = skill_install.install_skills(
            force=args.force, dry_run=args.dry_run, uninstall=args.uninstall,
        )
    except skill_install.SkillInstallError as exc:
        print(f"install-skills: {exc}", file=sys.stderr)
        return 1

    verb = "Uninstalling" if summary.uninstall else "Installing"
    prefix = "[dry-run] " if summary.dry_run else ""
    arrow = "from" if summary.uninstall else "→"
    print(f"{prefix}{verb} Buddhi skills {arrow} {summary.target_root}\n")

    # Per-file lines, grouped by skill, in a stable order.
    glyph = {
        skill_install.INSTALL: "✓ installed",
        skill_install.UPDATE: "✓ updated",
        skill_install.NOOP: "· unchanged",
        skill_install.REMOVED: "✓ removed",
        skill_install.CONFLICT: "⚠ conflict",
        skill_install.ERROR: "✗ error",
    }
    last_skill = None
    for f in summary.files:
        if f.skill != last_skill:
            print(f"  {f.skill}")
            last_skill = f.skill
        label = glyph.get(f.action, f.action)
        name = f.rel or "(directory)"
        line = f"    {label:14} {name}"
        if f.detail:
            line += f"  — {f.detail}"
        print(line)

    counts = summary.counts()
    # (action, singular, plural) — "installed"/"updated"/"unchanged"/"removed" read fine
    # at any count; only the noun forms need pluralising.
    order = [
        (skill_install.INSTALL, "installed", "installed"),
        (skill_install.UPDATE, "updated", "updated"),
        (skill_install.NOOP, "unchanged", "unchanged"),
        (skill_install.REMOVED, "removed", "removed"),
        (skill_install.CONFLICT, "conflict", "conflicts"),
        (skill_install.ERROR, "error", "errors"),
    ]
    parts = [f"{counts[a]} {sing if counts[a] == 1 else plur}"
             for a, sing, plur in order if counts.get(a)]
    print(f"\nSummary: {', '.join(parts) if parts else 'nothing to do'}.")
    if not summary.dry_run:
        print(f"Provenance: {summary.sidecar_path}")
    if any(f.action == skill_install.CONFLICT for f in summary.files):
        print("Conflicts were left untouched. Re-run with --force to overwrite them "
              "(each is backed up to a .bak-<timestamp> sidecar first).", file=sys.stderr)
    if not summary.uninstall and not summary.dry_run and counts.get(skill_install.INSTALL, 0) + counts.get(skill_install.UPDATE, 0):
        print("\nRestart Claude Code so it loads the skills.")
    return 1 if summary.had_error else 0


# ── upgrade ────────────────────────────────────────────────────────────────────────
# What ``upgrade`` re-execs into once the package on disk has been replaced. Spelled as
# plain module-level strings so the post-upgrade path is assembled from constants alone
# — see the torn-state note in :func:`_upgrade`.
#
# The obvious spelling, ``-m buddhi_review.cli``, is WRONG here: ``-m`` puts the current
# working directory on ``sys.path`` ahead of everything else, which the console script
# that launched ``upgrade`` never did. Upgrading while cwd'd into any directory that
# happens to contain a ``buddhi_review/`` folder — any clone of this repo, any worktree —
# would silently re-sync the skills from THAT tree and stamp its version into the
# installed files, reporting success. ``-c`` prepends the cwd the same way, so the
# payload drops ``sys.path[0]`` before importing anything: the re-sync then resolves the
# package exactly as the console script would. (``-P`` / ``PYTHONSAFEPATH`` do this
# natively but only from 3.11; this package supports 3.9.)
_RESYNC_PATH_PREAMBLE = "import sys; sys.path.pop(0); "
_RESYNC_CODE = (_RESYNC_PATH_PREAMBLE
                + "from buddhi_review.cli import main; sys.exit(main(sys.argv[1:]))")


def _upgrade_check(plan, *, out: TextIO,
                   latest_fn: Optional[Callable[[], Optional[str]]] = None) -> int:
    """``upgrade --check``: say whether a newer release exists, and change nothing.

    Unlike the deliberately-muted launch banner, this prints an explicit verdict even
    when the install is current — the user asked a direct question, so silence would
    read as a failure. It reuses the banner's own TTL-cached check (one shared cache,
    one network behaviour) and distinguishes "current" from "could not tell", which a
    banner never has to."""
    print(f"buddhi-review {__version__} — install method: {plan.method}", file=out)
    if update_banner.update_check_disabled():
        print("Update checks are switched off by BUDDHI_NO_UPDATE_CHECK, so PyPI was "
              "not contacted and there is no verdict to report.", file=out)
        return 0
    latest = (latest_fn or update_banner.latest_release)()
    if latest is None:
        print("Could not determine the latest release (offline, or PyPI was "
              "unreachable). Nothing was changed.", file=out)
    elif update_banner.update_available(__version__, latest):
        print(f"↑ buddhi-review {latest} is available (you have {__version__}).\n"
              f"    Run: buddhi-review upgrade", file=out)
    else:
        print(f"buddhi-review {__version__} is current (latest release: {latest}).",
              file=out)
    return 0


def _upgrade(args: argparse.Namespace, *,
             execer: Optional[Callable[[str, List[str]], None]] = None,
             runner: Optional[Callable[..., int]] = None,
             latest_fn: Optional[Callable[[], Optional[str]]] = None,
             out: Optional[TextIO] = None,
             err: Optional[TextIO] = None) -> int:
    """Upgrade this installation the way it was installed, then re-sync the skills.

    THE SAFETY GATE LIVES HERE, NOT IN AN UPDATER. Install-method detection and the
    notify-only decision both run BEFORE any discovered updater is selected, so no
    installed package can route this command into pip-installing against an
    interpreter the operating system owns. A notify-only outcome prints the exact
    command the user should run themselves and exits SUCCESS — refusing to act is the
    correct result here, not an error.

    ``--check`` and ``--dry-run`` are independent, both strictly read-only, and
    ``--check`` wins when both are given. ``runner`` / ``execer`` / ``latest_fn`` are
    injectable so the whole path is unit-testable without ever installing or exec'ing.
    """
    from buddhi_review import updaters

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    plan = updaters.detect_install_method()

    if args.check:
        return _upgrade_check(plan, out=out, latest_fn=latest_fn)

    print(f"buddhi-review {__version__} — install method: {plan.method}", file=out)
    print(plan.reason, file=out)

    if plan.notify_only:
        print("\nRun this yourself to upgrade:", file=out)
        for line in plan.manual:
            print(f"    {line}", file=out)
        return 0

    if args.dry_run:
        print("\n[dry-run] would run:", file=out)
        for step in plan.steps:
            print(f"    {step.display()}", file=out)
        print(f"    {shlex.join([sys.executable, '-c', _RESYNC_CODE, 'install-skills'])}",
              file=out)
        print("\n[dry-run] nothing was run.", file=out)
        return 0

    # ── TORN-STATE GUARD ────────────────────────────────────────────────────────────
    # The upgrade below replaces this package's files on disk while this very process
    # is running against the OLD ones — and the package root resolves its public names
    # through a lazy PEP-562 ``__getattr__``, so ANY buddhi_review attribute access
    # after that point can import a FRESH module against already-loaded stale siblings
    # and produce a half-new module graph. So every value the tail needs is bound to a
    # plain local FIRST, and between the upgrade returning and the exec there are ZERO
    # buddhi_review imports and zero attribute reads on the package.
    python = sys.executable
    resync_argv = [python, "-c", _RESYNC_CODE, "install-skills"]
    resync_display = shlex.join(resync_argv)
    execv = execer if execer is not None else os.execv
    manual = list(plan.manual)

    updater = updaters.select_updater(updaters.discover_updaters())
    outcome = updaters.perform_update(updater, plan, runner=runner, out=out, err=err)
    returncode, upgraded, message = outcome.returncode, outcome.upgraded, outcome.message

    # ↓↓↓ nothing below this line may touch the buddhi_review package ↓↓↓
    if not upgraded:
        # Either a hard failure (non-zero → say so, exit non-zero) or a step that left
        # everything exactly as it was (zero → the notify-only outcome, with the manual
        # commands). Neither re-syncs: nothing was installed, so there is nothing new
        # to sync, and a re-sync after a failed upgrade would only muddy the report.
        if message:
            print(message, file=out if returncode == 0 else err)
        if returncode == 0:
            for line in manual:
                print(f"    {line}", file=out)
        return returncode

    # An "already up to date" package manager also lands here (it exits 0), and that is
    # deliberate: re-syncing the skills for the installed version is the documented
    # repair path, so it runs whether or not the version actually moved.
    print("\nUpgrade finished (an already-current install counts as done). "
          "Re-syncing the bundled skills…", file=out)
    try:
        execv(python, resync_argv)
    except Exception as exc:
        print(f"upgrade: could not hand off to the skill re-sync ({exc}). The upgrade "
              f"itself succeeded — finish it by running:\n    {resync_display}", file=err)
        return 1
    return 0  # unreachable once execv replaces the process; kept for the injected seam


def _add_loop_args(p: argparse.ArgumentParser) -> None:
    """The review-loop flags, shared by ``review-pr`` (the front door) and
    ``run-loop`` (the detached engine) so they never drift."""
    p.add_argument("pr")
    p.add_argument("--repo")
    p.add_argument("--cwd")
    # Tri-state: unset (None) → fall back to the repo's configured auto_merge
    # (repos[<repo>].auto_merge), else off; an explicit --auto-merge /
    # --no-auto-merge always wins. Default MUST stay None, never False — a
    # concrete False would make the per-repo config unreachable (the merge is
    # opt-in, so the config can only ever turn it ON).
    p.add_argument("--auto-merge", action=argparse.BooleanOptionalAction, default=None,
                   help="squash-merge + delete branch on a clean pass (default: "
                        "unset → the repo's configured auto_merge, else off)")
    p.add_argument("--verify-fixes", choices=VERIFY_MODES, default="auto",
                   help="pre-commit fix verification (tripwire always forces it)")
    # A PR_DESCRIPTION comment auto-rewrites the PR body in place (default: on).
    # Off leaves the body untouched and logs a skip for a manual update.
    p.add_argument("--fix-pr-description", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="rewrite the PR body to address a PR-description comment "
                        "(default: on; --no-fix-pr-description leaves it for a human)")
    p.add_argument("--max-rounds", type=int, default=None,
                   help="maximum review→fix rounds (default: BUDDHI_MAX_ROUNDS env → diff auto-size → 10)")
    # Test-failure handling is escalate-only: on a red gate the handler asks the
    # human. It never edits or reverts your tests.
    # BUDDHI_TEST_FAILURE_MODE seeds the default (see env-vars.md). An invalid value
    # falls back to "escalate" so the documented "invalid → escalate" contract holds
    # and the detached run-loop never receives an out-of-choices value.
    _test_failure_default = os.environ.get("BUDDHI_TEST_FAILURE_MODE", "escalate")
    if _test_failure_default not in ("escalate", "off"):
        _test_failure_default = "escalate"
    p.add_argument("--test-failure-mode", choices=("escalate", "off"),
                   default=_test_failure_default,
                   help="escalate: run the test gate, ask on red (default); "
                        "off: skip the gate (loud ⊘ notice)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--rr", action="store_true",
                   help="round 1: re-request EVERY enabled reviewer; clears the soft "
                        "exclusions (voluntarily-done + polish), keeps the hard ones")
    g.add_argument("--rr-active", action="store_true",
                   help="round 1: only still-active reviewers; exit clean if none. "
                        "The RESTART flag: a reviewer that already approved this PR "
                        "is skipped, and the polish-only verdicts of the killed run "
                        "are restored when the PR's HEAD is unchanged — so no "
                        "reviewer whose verdict is already in hand is re-asked")
    g.add_argument("--rr-none", action="store_true",
                   help="summon NO reviewers: fix/resolve the comments already on "
                        "the PR and merge on a clean exit (if --auto-merge is "
                        "enabled), even with zero reviews. "
                        "The one explicit way to lift the never-merge-unreviewed "
                        "block (zero reviewers is then deliberate, not an "
                        "accidentally-silent fleet)")


def _add_base_remote_args(sp: argparse.ArgumentParser) -> None:
    """Base-remote selection, shared by ``rebase-check`` and ``rebase``.

    Without these, a fork checkout (origin = the contributor's fork, PR base on
    upstream) resolves the base to the fork's own stale copy of the branch and
    the gate reports ``up-to-date`` when ``upstream/<base>`` is ahead."""
    sp.add_argument("--repo", default=None,
                    help="owner/repo hosting the base branch; its matching git "
                         "remote is used (fork checkouts: pass the upstream repo)")
    sp.add_argument("--remote", default=None,
                    help="git remote hosting the base branch; overrides --repo "
                         "(default: branch.<base>.remote, else origin)")


def _detect_rebase_base(args: argparse.Namespace) -> str:
    cwd = args.cwd or os.getcwd()
    if args.base:
        return args.base
    # Auto-detect base if not supplied (mirrors open_pr.detect_base).
    from buddhi_review.open_pr import detect_base, _default_run as _opr_run
    try:
        return detect_base(cwd, _opr_run)
    except Exception:
        return "main"


def _rebase_check(args: argparse.Namespace) -> int:
    """The ``rebase-check`` verb: report rebase state as JSON + guidance.

    Strictly check-only, on every tier — never mutates. The paid-capability
    action verb is the separate ``rebase`` subcommand (see ``_rebase``)."""
    from buddhi_review import rebase_gate

    cwd = args.cwd or os.getcwd()
    base = _detect_rebase_base(args)

    return rebase_gate.run_check_verb(
        cwd, base,
        fetch=not args.no_fetch,
        json_only=args.json_only,
        repo=getattr(args, "repo", None),
        remote=getattr(args, "remote", None),
    )


def _rebase(args: argparse.Namespace) -> int:
    """The ``rebase`` verb: the paid-capability ACTION verb.

    On free tier (no active backend exposing ``run_rebase``), this prints
    the same manual guidance as ``rebase-check`` and declines to mutate the
    repo itself. On paid tier, it delegates the actual rebase to the backend."""
    from buddhi_review import rebase_gate
    from buddhi_review.backends import discover_backends, select_backend

    cwd = args.cwd or os.getcwd()
    base = _detect_rebase_base(args)

    # Capability hook: resolve the active backend so a paid ``run_rebase`` can
    # take over the action. The free FreeBackend has no ``run_rebase``, so it
    # is silently treated as free-tier inside ``run_rebase_verb``.
    try:
        backend = select_backend(discover_backends())
    except Exception:
        backend = None

    return rebase_gate.run_rebase_verb(
        cwd, base,
        fetch=not args.no_fetch,
        backend=backend,
        json_only=args.json_only,
        repo=getattr(args, "repo", None),
        remote=getattr(args, "remote", None),
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="buddhi-review", description="Buddhi PR-review skill.")
    p.add_argument("--version", action="version", version=f"buddhi_review {__version__}")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("self-check", help="run the kernel-driven pipeline on built-in fixtures")

    rp = sub.add_parser("review-pr", help="launch the review loop on an open PR")
    _add_loop_args(rp)

    # Internal: the detached free engine the launcher runs (not a user command).
    rl = sub.add_parser("run-loop",
                        help="internal: run the review loop in this process "
                             "(invoked by launch-review.sh; users call review-pr)")
    _add_loop_args(rl)

    cp = sub.add_parser("open-pr", help="create a PR then run the loop")
    cp.add_argument("--repo")
    cp.add_argument("--cwd")
    # base defaults to None → the actuator detects origin/HEAD, then a local
    # main/master, then "main".
    cp.add_argument("--base", default=None)
    cp.add_argument("--title", help="PR title (also the commit subject); required")
    cp.add_argument("--body", default="")
    cp.add_argument("--branch", help="branch name to create when on the base branch "
                                     "(default: <prefix>/<slug-from-title>)")
    cp.add_argument("--branch-prefix", default="feat",
                    help="branch prefix when deriving a name (feat/fix/refactor; default feat)")
    cp.add_argument("--max-rounds", type=int, default=None,
                    help="maximum review→fix rounds (default: BUDDHI_MAX_ROUNDS env → diff auto-size → 10)")
    cp.add_argument("--no-loop", action="store_true",
                    help="create the PR but skip launching the review loop")

    # S2 — the free rebase-gate verb (check-only, never mutates).
    rc = sub.add_parser("rebase-check",
                        help="report whether the branch needs a rebase (JSON + guidance)")
    rc.add_argument("--cwd", help="repo directory (default: cwd)")
    rc.add_argument("--base", default=None,
                    help="base branch to check against (default: auto-detect origin/HEAD)")
    _add_base_remote_args(rc)
    rc.add_argument("--no-fetch", action="store_true",
                    help="skip git fetch (use local tracking refs; may be stale)")
    rc.add_argument("--json-only", action="store_true",
                    help="print only the JSON result, no guidance text")

    # S2 — the rebase action verb (paid-capability hook; free tier declines
    # to mutate and prints the same manual guidance as rebase-check).
    rb = sub.add_parser("rebase",
                        help="rebase onto the base branch (paid tier); free "
                             "tier prints manual guidance and does not mutate")
    rb.add_argument("--cwd", help="repo directory (default: cwd)")
    rb.add_argument("--base", default=None,
                    help="base branch to rebase onto (default: auto-detect origin/HEAD)")
    _add_base_remote_args(rb)
    rb.add_argument("--no-fetch", action="store_true",
                    help="skip git fetch (use local tracking refs; may be stale)")
    rb.add_argument("--json-only", action="store_true",
                    help="print only the JSON result, no guidance text")

    sp = sub.add_parser("setup", help="interactive onboarding wizard")
    sp.add_argument("--repo", help="pre-bind this owner/repo (per-repo confirm mode)")

    stp = sub.add_parser("status", help="print per-repo setup status as JSON (for the skill gate)")
    stp.add_argument("--repo", required=True, help="owner/repo to report on")

    isk = sub.add_parser("install-skills",
                         help="install the bundled /review-pr + /open-pr skills into "
                              "Claude Code (idempotent; safe to re-run after an upgrade)")
    isk.add_argument("--dry-run", action="store_true",
                     help="show every per-file action but write nothing")
    isk.add_argument("--force", action="store_true",
                     help="overwrite user-modified/foreign files (each backed up to "
                          ".bak-<timestamp> first); the only way to clobber a conflict")
    isk.add_argument("--uninstall", action="store_true",
                     help="remove the installed skills (unmodified ones; add --force to "
                          "remove modified/foreign ones and a stale legacy create-pr)")

    upg = sub.add_parser("upgrade",
                         help="upgrade buddhi-review the way it was installed, then "
                              "re-sync the skills (an OS-managed Python is told what "
                              "to run, never written to)")
    upg.add_argument("--dry-run", action="store_true",
                     help="report the detected install method and the exact command "
                          "that would run, and change nothing")
    upg.add_argument("--check", action="store_true",
                     help="report whether a newer release is available and exit; "
                          "never upgrades (wins over --dry-run)")
    return p


# ── Unclaimed-command fallback seam ───────────────────────────────────────────────
# argparse's subparsers raise SystemExit(2) on an unknown subcommand BEFORE any
# dispatch runs, so a command word that is not one of our own free subcommands has to
# be intercepted in main() ahead of parse_args. Such a command may be claimed by a
# separately-installed, active backend (which runs it), or answered with the notice
# below — the normal free-only outcome, and equally what a plain typo gets.

# The upgrade notice (approved verbatim 2026-07-12, gate H1) printed when no installed
# backend claims the command. The free tree ships NO list of non-free command names,
# so a command whose paid access has lapsed and one that never existed are
# indistinguishable here; the wording is deliberately true for BOTH and asserts
# nothing about whether the command is real. ``{command}`` is echoed from the runtime
# invocation — no command name is ever hard-coded. This is a functional "why did
# nothing happen?" answer, so it is exempt from BUDDHI_NO_UPSELL and any nudge
# frequency cap (execution-plan §B2a / §E item 9c).
_UNCLAIMED_COMMAND_NOTICE = (
    "The '{command}' command is not included in this free installation.\n"
    "If you have a Buddhi licence, renew or reactivate it and run the command again.\n"
    "To get a licence: https://buddhikernel.com"
)


def _display_command(command: str) -> str:
    """Escape non-printable characters in ``command`` before it goes into a
    printed notice — a raw control character (e.g. an ANSI/OSC escape) would
    otherwise be interpreted by the terminal, mangling output or forging a
    clickable link. The unescaped ``command`` is still what gets passed to a
    claiming backend; only the displayed copy is sanitized."""
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in command)


def _known_commands(parser: argparse.ArgumentParser) -> frozenset:
    """The free subcommand names this parser defines — read straight off the
    subparsers action, so a newly-added free command is covered with no second list
    to keep in sync."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return frozenset(action.choices)
    return frozenset()


def _split_command(argv: List[str]) -> tuple[Optional[str], List[str]]:
    """Split ``argv`` into ``(command, trailing)`` for the pre-parse fallback seam:
    ``command`` is ``argv[0]`` (the subcommand word) and ``trailing`` is everything
    after it, forwarded verbatim to a claiming backend. Returns ``(None, [])`` when
    there is no candidate to dispatch — an empty argv, a leading ``-h`` / ``--help``
    / ``--version`` that argparse itself answers, or any other leading option (this
    parser has no global options that take a value, so a leading ``-`` token is
    always argparse's to reject, never a command word)."""
    if not argv:
        return None, []
    tok = argv[0]
    if tok in ("-h", "--help", "--version"):
        return None, []
    if not tok.startswith("-"):
        return tok, list(argv[1:])
    return None, []


def _dispatch_unclaimed_command(command: str, trailing: List[str], *,
                                backends: Optional[List] = None,
                                stream: Optional[TextIO] = None) -> int:
    """Route a non-free command through the front door.

    A separately-installed backend may CLAIM the command via the optional
    ``claimed_commands`` hook (never part of the Backend Protocol); the highest-
    priority active claimant runs it, receiving the command name and the trailing
    argv verbatim and unparsed. With no active claimant the front door prints the
    upgrade notice and exits 2 — it never half-runs a command it does not own.
    ``backends`` / ``stream`` are injectable for tests.
    """
    backend = select_command_backend(command, backends=backends)
    out = stream if stream is not None else sys.stderr
    if backend is not None:
        try:
            return backend.run_command(command, trailing)
        except Exception as exc:  # an installed backend must never crash the free front door
            print(f"⚠ backend {getattr(backend, 'name', repr(backend))!r} failed "
                  f"running {command!r} ({exc!r})", file=out)
            return 1
    print(_UNCLAIMED_COMMAND_NOTICE.format(command=_display_command(command)), file=out)
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    # Intercept a non-free command BEFORE argparse (which would SystemExit(2) on it):
    # a claiming backend runs it, else it gets the upgrade notice.
    command, trailing = _split_command(raw_argv)
    if command is not None and command not in _known_commands(parser):
        return _dispatch_unclaimed_command(command, trailing)

    args = parser.parse_args(raw_argv)
    if args.command == "self-check":
        return _self_check()
    if args.command == "review-pr":
        return _review_pr(args)
    if args.command == "run-loop":
        return _run_loop(args)
    if args.command == "open-pr":
        return _open_pr(args)
    if args.command == "rebase-check":
        return _rebase_check(args)
    if args.command == "rebase":
        return _rebase(args)
    if args.command == "setup":
        return _setup(args)
    if args.command == "status":
        return _status(args)
    if args.command == "install-skills":
        return _install_skills(args)
    if args.command == "upgrade":
        return _upgrade(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
