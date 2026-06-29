# buddhi-review

A **free, MIT-licensed PR review-and-fix loop for Claude Code**, built on the public
[Buddhi kernel](https://github.com/buddhikernel/buddhi). It fans out your review
bots, classifies every comment, applies the fixes, re-reviews until the PR is clean,
and merges it when you opt in.

**Buddhi lands your PRs.** Think of a pull request as a flight: it takes off, flies
the review rounds, and comes in clean, ready to land (merge) on the base branch.
Along the way Buddhi reads the review comments left by your bots, classifies each
one, and lets the kernel decide what to do with it: apply a fix, ask you a question,
skip it, or defer it when the day's interrupt budget is spent. You answer any
questions right in your terminal.

New here? **[Getting started](https://github.com/buddhikernel/buddhi-review/blob/main/GETTING_STARTED.md)**
walks you from `pip install` through your first reviewed PR.

## What it is

Buddhi splits a PR review into two halves: the decision, and the I/O around it. The
[Buddhi kernel](https://github.com/buddhikernel/buddhi) makes the decision — for each
review comment it decides whether to fix it, ask you, skip it, or defer it.
`buddhi-review` is the **adapter** that does everything around that decision on the
GitHub side: it reads the comments off your PR, hands each one to the kernel, and
carries out whatever the kernel returns. Concretely, for each comment the loop:

1. **Classifies** it into one of six labels: `SUBSTANTIVE`, `COSMETIC`,
   `BUSINESS_QUESTION`, `PR_DESCRIPTION`, `OUTDATED`, `INVALID`. If the classifier
   can't produce a usable label, the comment becomes a synthetic
   `CLASSIFICATION_FAILED` and is escalated to you instead of being dropped — so a
   comment is never silently lost just because it could not be classified.
2. **Maps** the label onto a kernel work item and runs it through the kernel's
   seven decisions.
3. **Acts** on the kernel's disposition:

   | Kernel disposition | What happens |
   |---|---|
   | fix | dispatch a fixer (SUBSTANTIVE / COSMETIC) |
   | escalate | ask you via the console answer-file channel (BUSINESS_QUESTION / PR_DESCRIPTION / classifier failure) |
   | skip | do nothing (OUTDATED / INVALID) |
   | defer | the day's human-interrupt budget is spent: hold the item, never drop it |

The disposition is the **kernel's** call, not a pile of hand-tuned `if` branches in
the adapter — `buddhi-review` only carries out the I/O; every decision stays in the
kernel.

## When it asks you

The kernel asks you only when a review comment can't be settled by the code plus your
project's own docs, conventions, and PR description — when resolving it would mean
making a call that is really yours. In practice that is a question about product
direction, scope, or a business rule the docs don't answer; a genuinely ambiguous
technical fork with more than one defensible answer and nothing in the repo to choose
between; or a taste call about user-facing wording or design that the docs leave open.
Anything the code and docs already settle, the loop handles on its own without
interrupting you.

When it does need you, the question is written to an editable answer file: the loop
prints a `file://` link, you type a number (or free text) on the `>` line and save,
and the loop picks it up. Everything happens locally.

The notifier is a small, swappable interface, so an alternative delivery channel
can be wired in later without touching the review loop.

## Install

```bash
pip install buddhi-review
```

This pulls the kernel ([`buddhikernel`](https://github.com/buddhikernel/buddhi)) and
`PyYAML`, and installs the `buddhi-review` command. The two slash-command skills it
backs — **`/review-pr`** (review an open PR) and **`/create-pr`** (open a PR, then
review it) — ship inside the package but are **not** added to Claude Code
automatically; install them as Claude Code **skills** (each becomes
`~/.claude/skills/<name>/SKILL.md`):

```bash
SKILLS=$(python3 -c "import buddhi_review, os; print(os.path.join(os.path.dirname(buddhi_review.__file__), 'skills'))" 2>/dev/null)
if [ -d "$SKILLS" ]; then
  mkdir -p ~/.claude/skills/
  rm -rf ~/.claude/skills/review-pr ~/.claude/skills/create-pr
  cp -R "$SKILLS"/review-pr "$SKILLS"/create-pr ~/.claude/skills/
  echo "✓ Skills installed to ~/.claude/skills/ — restart Claude Code to load them"
else
  echo "✗ Error: Could not locate buddhi_review skills. Ensure buddhi-review is installed in the active Python environment."
fi
```

**Restart Claude Code** so it loads the new skills, then run **`/review-pr setup`**
once to onboard (see [Getting started](https://github.com/buddhikernel/buddhi-review/blob/main/GETTING_STARTED.md)).
If a slash-command of the same name already exists, the skill takes precedence.

Each skill's `SKILL.md` frontmatter includes a **git-guardrail hook** that stops the
agent from hand-running history-rewriting git (rebase / merge / reset --hard /
cherry-pick / force-push) while a review is in flight; it activates only when the
skill runs and leaves your everyday git untouched.

To work from a clone instead (for development or to run the tests):

```bash
pip install -e ".[test]"
python3 -m pytest -q
```

## Quickstart

```bash
# 1. Health check — runs the kernel-driven pipeline on built-in fixtures.
#    No network, no `claude`; proves the kernel decides every disposition.
python3 -m buddhi_review self-check
```

```text
buddhi_review 0.1.0 — kernel-driven self-check


[Clearance — a decision the loop needs from you] How should item 'c4' be handled?
  1. Apply the suggested change
  2. Skip — the suggestion is not valid here
  3. Defer — this needs your judgment
  answer here → file:///…/review-answer-local-c4.md

[Clearance — a decision the loop needs from you] How should item 'c5' be handled?
  1. Apply the suggested change
  2. Skip — the suggestion is not valid here
  3. Defer — this needs your judgment
  answer here → file:///…/review-answer-local-c5.md

[Clearance — a decision the loop needs from you] How should item 'c6' be handled?
  1. Apply the suggested change
  2. Skip — the suggestion is not valid here
  3. Defer — this needs your judgment
  answer here → file:///…/review-answer-local-c6.md
  [ok ] SUBSTANTIVE          kernel=MODEL_HANDLED disposition=fix            (want fix)
  [ok ] COSMETIC             kernel=MODEL_HANDLED disposition=fix            (want fix)
  [ok ] OUTDATED             kernel=DISCARDED     disposition=skip           (want skip)
  [ok ] INVALID              kernel=DISCARDED     disposition=skip           (want skip)
  [ok ] BUSINESS_QUESTION    kernel=ESCALATED     disposition=escalate       (want escalate)
  [ok ] PR_DESCRIPTION       kernel=ESCALATED     disposition=escalate       (want escalate)
  [ok ] CLASSIFICATION_FAILED kernel=ESCALATED     disposition=escalate       (want escalate)

SELF-CHECK OK — the kernel decided every disposition.
```

The three `[Clearance …]` panels are expected: the self-check feeds the kernel its
escalation fixtures (`BUSINESS_QUESTION`, `PR_DESCRIPTION`, and a forced classifier
failure), so it writes each one's answer file (and immediately cleans it up) before
printing the results table. A clean run still ends with `SELF-CHECK OK`.

```bash
# 2. One-time onboarding (plan, repo, reviewer fleet).
/review-pr setup

# 3. Review an open PR.
/review-pr <pr>

# 4. Open a PR from your local work and review it in one step.
/create-pr
```

You can also drive the CLI directly:

```bash
python3 -m buddhi_review review-pr 123 --repo OWNER/REPO --cwd /path/to/checkout
```

or detach it as a background loop and follow its log:

```bash
LAUNCHER=$(python3 -c "import os, buddhi_review; print(os.path.join(os.path.dirname(buddhi_review.__file__), 'launch-review.sh'))" 2>/dev/null)
if [ -f "$LAUNCHER" ]; then
  bash "$LAUNCHER" 123 --repo OWNER/REPO --cwd /path/to/checkout
fi
```

## What a review costs you

Buddhi is free and MIT-licensed, but **the reviews it runs spend your own provider
quotas.** Buddhi never bills you and never proxies a review through an account of its
own. Each reviewer draws on a plan you already hold, and the loop's own classify and
fix calls run on your machine against your Claude subscription:

| Surface | Whose meter it spends |
|---|---|
| **Copilot review** | Your **GitHub AI credits** (a paid GitHub Copilot plan). |
| **`claude[bot]` review** | Your **GitHub Actions minutes** (the bundled `claude-code-review.yml` workflow runs on each summon) plus your Claude subscription (`CLAUDE_CODE_OAUTH_TOKEN`) or pay-as-you-go API credit (`ANTHROPIC_API_KEY`) — whichever the repo secret holds. |
| **Codex review** | Your **ChatGPT plan** (the OpenAI Codex GitHub app). |
| **Gemini review** | Your **Gemini Code Assist** entitlement. |
| **The loop's own classify / fix calls** | Your **Claude subscription**: the loop drives the local `claude` CLI to classify each comment and apply fixes. |

Check or cap your GitHub-side spend at
**[github.com/settings/billing/summary](https://github.com/settings/billing/summary)**.
Enable only the reviewers whose plans you hold. `/review-pr setup` subtracts the rest,
so no trigger fires into a void and nothing is spent on a reviewer you do not run. See
[Getting started](https://github.com/buddhikernel/buddhi-review/blob/main/GETTING_STARTED.md#what-a-review-costs-you)
for the full breakdown.

## Status

buddhi-review is in **alpha**: the CLI flags, output format, and Python API may
change between releases, with no semantic-versioning guarantees before v1.0. It has
been exercised end to end but not yet hardened across a wide range of repositories,
so expect rough edges. Issues and PRs are welcome.

Run the test suite with `pip install -e ".[test]" && python3 -m pytest -q`.

## Architecture

`buddhi-review` is an adapter of the Buddhi kernel onto the GitHub PR-review substrate.

- **The four-verb adapter** (`buddhi_review/adapter.py`) implements the kernel's
  `Adapter` contract: `ingest` (yield the PR's raw comments), `run_embedded`
  (Stage-0 condition one item, then run the seven decisions), `escalate_async`
  (deliver a pre-reasoned ask), and `detect_resolved` (observe a *signaled*
  out-of-band resolution).
- **The five seams** (`buddhi_review/seams.py` + `policy.py`) are the concrete
  implementations the kernel calls back through:
  - **Store**: interrupt counters plus the two-tier exclusion lattice (quota and
    PR-too-large are permanent; an errored bot is transient and comes back on its
    next substantive comment).
  - **Router**: a stakes-based effort recommendation.
  - **Escalation**: translates the kernel's pre-reasoned ask into a
    channel-agnostic ask and delivers it over the console channel.
  - **OOB source**: declares the substrate can observe a *signaled* resolution.
  - **PolicyPack**: the single policy contract, bundling the discard predicate, the
    effort taxonomy, the judgment threshold, the validity rule, the ask phrasings,
    and the bounded interrupt budget.

Nothing domain-specific lives in the kernel; it all enters through the policy pack
and the seams. See the [Buddhi kernel](https://github.com/buddhikernel/buddhi) for
the kernel's own design.

## License

MIT. See [LICENSE](https://github.com/buddhikernel/buddhi-review/blob/main/LICENSE).

This package depends on the [Buddhi kernel](https://github.com/buddhikernel/buddhi)
(`buddhikernel`), which is licensed under Apache-2.0.
