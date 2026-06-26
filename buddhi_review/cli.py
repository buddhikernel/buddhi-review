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
  ``create-pr``  — create a PR from local work, then launch the loop: resolve repo →
                   git decision tree (commit/branch/push) → gh pr create → launch the
                   review adapter detached (PR URL is the last stdout line).
  ``setup``      — the interactive onboarding wizard (plan, repo, reviewer fleet).

Answers come from the terminal.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from buddhi_review import __version__, gh_ingest, model_call, round_driver, upsell
from buddhi_review.actuators import default_fix_dispatch
from buddhi_review.adapter import ReviewAdapter
from buddhi_review.backends import launch_review_loop
from buddhi_review.config import active_reviewers, load_config, notifier_channel, plan
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
    ("PR_DESCRIPTION", "the PR body is out of date", "escalate"),
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


def _review_pr(args: argparse.Namespace) -> int:
    """The ``review-pr`` front door: route the loop launch through the backend
    dispatcher, then return immediately. With nothing extra installed this runs the
    free engine (``run-loop`` detached via ``launch-review.sh``); a separately
    installed, active backend would take over the same command. The backend prints
    its own "where to watch" line after the choice is made."""
    cwd = args.cwd or os.getcwd()
    return launch_review_loop(
        args.pr, args.repo, cwd,
        auto_merge=args.auto_merge,
        verify_fixes=args.verify_fixes,
        max_rounds=args.max_rounds,
        test_failure_mode=args.test_failure_mode,
        rr=args.rr,
        rr_active=args.rr_active,
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
        fix_dispatch=default_fix_dispatch(
            cwd=cwd,
            plan=plan_name,
            verify_runner=model_call.text_runner("fix-verify", plan=plan_name),
            verify_mode=args.verify_fixes,
        ),
        max_rounds=args.max_rounds,
        auto_merge=args.auto_merge,
        rr=args.rr,
        rr_active=args.rr_active,
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


def _create_pr(args: argparse.Namespace) -> int:
    from buddhi_review import create_pr
    if not args.title:
        print("create-pr: --title is required.", file=sys.stderr)
        return 2
    return create_pr.actuate(
        repo=args.repo,
        cwd=args.cwd,
        base=args.base,
        title=args.title,
        body=args.body or "",
        branch=args.branch,
        branch_prefix=args.branch_prefix,
        no_loop=args.no_loop,
    )


def _setup(args: argparse.Namespace) -> int:
    from buddhi_review import wizard
    argv = ["--repo", args.repo] if getattr(args, "repo", None) else None
    return wizard.run(argv=argv)


def _add_loop_args(p: argparse.ArgumentParser) -> None:
    """The review-loop flags, shared by ``review-pr`` (the front door) and
    ``run-loop`` (the detached engine) so they never drift."""
    p.add_argument("pr")
    p.add_argument("--repo")
    p.add_argument("--cwd")
    # Default is NO auto-merge; the merge is opt-in.
    p.add_argument("--auto-merge", action=argparse.BooleanOptionalAction, default=False,
                   help="squash-merge + delete branch on a clean pass (default: off)")
    p.add_argument("--verify-fixes", choices=VERIFY_MODES, default="auto",
                   help="pre-commit fix verification (tripwire always forces it)")
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
                   help="round 1: re-request EVERY enabled reviewer (never clears exclusions)")
    g.add_argument("--rr-active", action="store_true",
                   help="round 1: only still-active reviewers; exit clean if none")


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

    cp = sub.add_parser("create-pr", help="create a PR then run the loop")
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
    cp.add_argument("--no-loop", action="store_true",
                    help="create the PR but skip launching the review loop")

    sp = sub.add_parser("setup", help="interactive onboarding wizard")
    sp.add_argument("--repo", help="pre-bind this owner/repo (per-repo confirm mode)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "self-check":
        return _self_check()
    if args.command == "review-pr":
        return _review_pr(args)
    if args.command == "run-loop":
        return _run_loop(args)
    if args.command == "create-pr":
        return _create_pr(args)
    if args.command == "setup":
        return _setup(args)
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
