# buddhi-review

A **free, MIT-licensed PR review-and-fix loop for Claude Code** (alpha), built on the
public [Buddhi kernel](https://github.com/buddhikernel/buddhi). It fans a PR review
out to a panel of AI reviewers from different vendors, such as Copilot, Codex,
Gemini, and Claude, then classifies every comment, applies the fixes, re-reviews
until the PR is clean, and merges it when you opt in. Your pull request takes off, flies the review
rounds, and lands.

**Across 88 real review runs on Claude-written code, Claude caught 3.8% of the valid
bugs. Reviewers from other vendors caught the rest, and half of all bugs surfaced
only after the first round of fixes.**

New here? `pip install buddhi-review`, then
**[Getting started](https://github.com/buddhikernel/buddhi-review/blob/main/GETTING_STARTED.md)**
walks you to your first reviewed PR.

## What real review runs show

The data in this section comes from real Buddhi review loops on a large private
repository, reviewing changes written with Claude Code. The loop records every
verified, fixed bug in a ledger, along with its severity, the reviewer that caught
it, and the round it was caught in. The 88 merged runs that meet the selection rules
below contain 681 verified fixed bugs in total. Two patterns stand out: a model is
soft on its own work, and many bugs surface only after earlier fixes land. The
research in [Why a panel, and why rounds](#why-a-panel-and-why-rounds) predicts
both effects.

<details>
<summary>How runs qualify (the selection rules)</summary>

A run is counted when all three rules hold:

- the change is larger than 50 lines, so trivial micro PRs are excluded;
- all four reviewers (Claude, Codex, Gemini, Copilot) actually reviewed the PR;
- no reviewer was budget-throttled, errored, or refused the PR as too large at any
  point in the run.

88 of 123 merged runs qualify. The 35 excluded runs are 2 micro changes, 7 runs
missing a reviewer, and 26 runs where a reviewer was throttled, errored, or refused
the PR. Each rule was checked against the PR's own review record: diff size
from the PR itself, participation from posted reviews and comments, and throttle,
error, or refusal notices detected with the loop's own signal patterns.

</details>

### "Just have Claude review it again" is not adversarial review

Across the 88 qualifying runs, Claude caught 26 of the 681 valid bugs (3.8%).
Reviewers from other vendors caught the remaining 96.2%. Claude caught 18 of the 189
high or critical bugs (9.5%), and of the high or critical bugs found in round 2 or
later, 93.5% came from a reviewer other than Claude.

This is the self-preference effect described in
[[LLM Evaluators Recognize and Favor Their Own Generations](https://arxiv.org/abs/2404.13076)],
visible in real review runs: a model reviewing its own work misses most of what a
diverse panel catches.

<img src="docs/assets/who-catches-the-bugs.svg" alt="Claude's share of the valid bugs, per review run, with the all-runs aggregate line" width="100%">

*Bars: the 20 qualifying runs with 10 or more valid bugs; runs with fewer are
omitted because percentages over tiny counts are noisy. Line: all 88 qualifying
runs. The selection rules are listed under "How runs qualify" above.*

### One round is not a complete review

Across the same 88 runs, **50.1% of the valid bugs, and 49.2% of the 189 high or
critical ones, were caught only in round 2 or later**, after the round-1 fixes had
been applied and the changed code was reviewed again. A reviewer that posts one round
of comments and stops would have shipped half the real bugs, including half the
high or critical ones. The effect grows with the number of bugs in a run: on the 20
runs with ten or more valid bugs, the round 2+ share rises to 65.4%.

Built-in review features that post one round of comments and stop cannot catch issues
that become visible only after earlier findings have been fixed.

<img src="docs/assets/when-bugs-surface.svg" alt="Share of bugs caught in round 2 or later, per review run, with the all-runs aggregate line" width="100%">

*Bars and line: the same sets as the chart above. The line (50.1% across all 88
runs) is the typical case; the bars show the larger runs, where the share is
higher.*

<details>
<summary><b>The data behind the charts</b></summary>

A closer look at every qualifying run with 20 or more valid bugs (there are seven),
reviewer by reviewer:

<img src="docs/assets/reviewer-drilldown.svg" alt="Valid bugs caught by each reviewer, per run, for the seven qualifying runs with 20 or more bugs" width="100%">

| Run | Valid bugs | Caught by Claude | Claude % | Caught in round 2+ | Round 2+ % | High/critical | High/crit in round 2+ |
|---|---|---|---|---|---|---|---|
| A | 21 | 0&Dagger; | 0.0% | 17 | 81.0% | 7 | 5 (71.4%) |
| B | 47 | 0&Dagger; | 0.0% | 41 | 87.2% | 14 | 10 (71.4%) |
| C | 22 | 0&dagger; | 0.0% | 15 | 68.2% | 12 | 7 (58.3%) |
| D | 42 | 2 | 4.8% | 34 | 81.0% | 19 | 14 (73.7%) |
| E | 29 | 1 | 3.4% | 22 | 75.9% | 8 | 8 (100%) |
| F | 24 | 0&dagger; | 0.0% | 19 | 79.2% | 6 | 5 (83.3%) |
| G | 20 | 0&dagger; | 0.0% | 4 | 20.0% | 2 | 0 (0.0%) |
| **All 88 qualifying runs** | **681** | **26** | **3.8%** | **341** | **50.1%** | **189** | **93 (49.2%)** |

&dagger; On Runs C, F, and G, Claude reviewed and posted an explicit all-clear ("No
issues found."), and the panel then caught 22, 24, and 20 valid bugs respectively.

&Dagger; On Runs A and B, Claude did not post an all-clear. It reviewed and left
comments, but none of them produced a valid bug.

Notes and caveats:

- The runs come from merged PRs on one private repository (anonymized). The
  numbers come from the loop's own per-bug ledger, which records each verified, fixed
  bug with its severity, the reviewer that caught it, and the round it was caught in.
- Severity is assigned by the loop's classifier, which itself runs on Claude, so
  the severity ratings do not disfavor Claude.
- Each bug is credited to one catching reviewer. Claude's raw comment counts on
  the underlying PRs match the ledger's counts, so every Claude catch is credited.
- The qualifying rules were checked run by run against each PR's review record:
  diff size from the PR itself, reviewer participation from posted reviews and
  comments, and throttle, error, or refusal notices detected with the loop's own
  signal patterns.
- The underlying PRs are private, so these numbers are not independently verifiable
  yet. The same charts will be regenerated from this repo's own public review loops,
  with PR links included, and the public numbers will replace these, whether they
  come back better or worse.

</details>

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

<details>
<summary><b>Install the /review-pr and /create-pr skills</b></summary>

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

</details>

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
#    No network and no `claude` CLI needed; proves the kernel makes every
#    decision (fix, ask, skip, or defer) on its own.
python3 -m buddhi_review self-check
```

```text
  [ok ] SUBSTANTIVE          kernel=MODEL_HANDLED disposition=fix            (want fix)
  [ok ] COSMETIC             kernel=MODEL_HANDLED disposition=fix            (want fix)
  …
SELF-CHECK OK — the kernel decided every disposition.
```

<details>
<summary><b>Full self-check output</b></summary>

```text
buddhi_review <version> — kernel-driven self-check


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

</details>

The `[Clearance …]` panels in the full output are expected: the self-check includes
cases that must be escalated to a human (`BUSINESS_QUESTION`, `PR_DESCRIPTION`, and a
forced classifier failure), so it briefly creates, then removes, the answer files a
real run would use to ask you (see [When it asks you](#when-it-asks-you)). A clean
run still ends with `SELF-CHECK OK`.

```bash
# 2. One-time onboarding (plan, repo, reviewer fleet).
/review-pr setup

# 3. Review an open PR.
/review-pr <pr>

# 4. Open a PR from your local work and review it in one step.
/create-pr
```

To drive the CLI directly or detach the loop as a background process, see
[Getting started](https://github.com/buddhikernel/buddhi-review/blob/main/GETTING_STARTED.md#4-review-a-pr).

## What a review costs you

Buddhi is free and MIT-licensed, but **the reviews it runs spend your own provider
quotas.** Buddhi never bills you and never proxies a review through an account of its
own. Each reviewer draws on a plan you already hold:

| Surface | Whose meter it spends |
|---|---|
| **Copilot review** | Your **GitHub AI credits** (a paid GitHub Copilot plan). |
| **`claude[bot]` review** | Your **GitHub Actions minutes** on a private repo (the bundled `claude-code-review.yml` workflow runs on each summon; public repos on standard runners are free) plus your Claude subscription (`CLAUDE_CODE_OAUTH_TOKEN`) or pay-as-you-go API credit (`ANTHROPIC_API_KEY`) — whichever the repo secret holds. |
| **Codex review** | Your **ChatGPT plan** (the OpenAI Codex GitHub app). |
| **Gemini review** | Your **Gemini Code Assist** entitlement. |
| **The loop's own classify / fix calls** | Your **Claude subscription**: the loop drives the local `claude` CLI to classify each comment and apply fixes. |

Minimum viable setup: a Claude subscription (which powers the loop's own
classify and fix calls) plus at least one reviewer plan you already hold;
`/review-pr setup` disables the rest, so nothing is spent on a reviewer you do not
run.

Check or cap your GitHub-side spend at
**[github.com/settings/billing/summary](https://github.com/settings/billing/summary)**.
See [Getting started](https://github.com/buddhikernel/buddhi-review/blob/main/GETTING_STARTED.md#what-a-review-costs-you)
for the full breakdown.

## Why a panel, and why rounds

The numbers in [What real review runs show](#what-real-review-runs-show) are the
result of a deliberate design: Buddhi does not hand your PR to one reviewer; it fans
the review out to a panel of independent models from *different* labs and keeps
flying rounds until a round comes back clean. Three reasons that beats one strong
reviewer running once.

**Different labs, different blind spots.** You have probably watched one reviewer flag
a real bug another signed off on. Across many PRs that stops being luck and becomes the
whole point: models trained by different labs, on different data, fail *differently*,
so where one is blind, another tends to see. This is the ensemble-diversity result (a
panel's misses shrink the less its members' errors overlap), and it has been measured
on today's models: same-vendor models make *more correlated* errors than cross-vendor
ones [[Correlated Errors in Large Language Models](https://arxiv.org/abs/2506.07962)].
A model is also soft on its own work: it
rates its own output more favorably than another model's
[[LLM Evaluators Recognize and Favor Their Own Generations](https://arxiv.org/abs/2404.13076)],
so a reviewer from a *different* family is not just a second pair of eyes; it is a
less self-flattering one.

**A fix is a new change, so it needs a fresh look.** When a fixer resolves a round-1
comment, it edits the code, and an edit can be wrong or introduce a new bug that exists
*only because* of the fix. Re-reading the *fixed* code catches regressions the first
pass could not have seen, and the review that counts most is by a *different* model
than the one that wrote the fix, for the self-preference reason above. This is why
the loop re-reviews after every fix rather than trusting the fixer: a model
re-reading its own fix is grading its own homework.

**It converges on a clean round, not on a fixed count, and not on "zero findings."**
More rounds are not automatically better: repeated review tends to plateau within a few
rounds, and pushing past that entrenches noise instead of removing it
[[Improving Factuality and Reasoning in Language Models through Multiagent Debate](https://arxiv.org/abs/2305.14325)]. So Buddhi does not loop a set number of times.
Each round it re-summons the reviewers on the *fixed* code, acts on the findings they
raise (fixing the substantive and the cosmetic ones alike), and goes again. Convergence
is the round that comes back with **no new findings to act on**: not the reviewers
falling silent, and not a finding count that was zero from the start, but the loop
having resolved
everything actionable, cosmetic nits included. Two guardrails keep it honest: there is
always at least one confirmation round after the last fix, so a fix never lands
unreviewed, and the round budget scales with the size of the change (a one-line tweak
gets a couple of rounds, a thousand-line diff earns more). Convergence is not the same as
escalation: when a comment genuinely needs your judgment, Buddhi asks you instead, a
separate mechanism described in [*When it asks you*](#when-it-asks-you) below. (Buddhi
claims nothing more: no model here is superhuman, and agreement between models is not
proof of correctness. A diverse panel reviewing to a clean round simply catches more
than one reviewer running once.)

```mermaid
flowchart LR
  PR[Pull request] --> R{Panel review<br/>of the current code}
  R --> C1[Reviewer<br/>lab A]
  R --> C2[Reviewer<br/>lab B]
  R --> C3[Reviewer<br/>lab C]
  C1 & C2 & C3 --> F[Collect + dedupe findings]
  F --> Q{New findings<br/>to act on?}
  Q -- "yes" --> FIX[Fix, then re-review the FIXED code]
  FIX --> R
  Q -- "no: clean round" --> DONE[Converged, clear to land]
```

<details>
<summary><b>The research behind this</b></summary>

- **Ensemble diversity / error decorrelation.** A group's error shrinks in proportion
  to how *uncorrelated* its members' mistakes are. Classical roots: Krogh & Vedelsby,
  *Neural Network Ensembles* (NeurIPS 1995); the Condorcet Jury Theorem; Surowiecki,
  *The Wisdom of Crowds* (2004).
- **Same-vendor models are more error-correlated than cross-vendor ones.** Kim et
  al., [*Correlated Errors in Large Language Models*](https://arxiv.org/abs/2506.07962).
  The same paper carries an honest caveat: that decorrelation *shrinks* for the largest,
  most accurate models. Cross-vendor diversity helps, but it is not a free lunch at the
  frontier, which is part of why Buddhi does not just stack rounds forever.
- **Self-preference bias.** Panickssery et al.,
  [*LLM Evaluators Recognize and Favor Their Own Generations*](https://arxiv.org/abs/2404.13076):
  an LLM rates text it wrote more favorably than another model's.
- **Rounds plateau.** Multi-agent debate gains saturate after a few rounds (Du et
  al., [*Improving Factuality and Reasoning through Multiagent Debate*](https://arxiv.org/abs/2305.14325));
  pushing further tends to entrench errors rather than remove them.
- **Why Buddhi *unions and dedupes* findings rather than making models vote.** Voting
  correlated judges caps out fast (even ~9 diverse LLM judges behave like ~2 independent
  votes: Kohli, [*Nine Judges, Two Effective Votes*](https://arxiv.org/abs/2605.29800)),
  whereas a diverse cross-vendor panel beats a single strong judge in LLM *evaluation*
  (Cohere, [*Replacing Judges with Juries* / PoLL](https://arxiv.org/abs/2404.18796)).
  Finding bugs is a *coverage* problem, where every reviewer's real catch is kept
  rather than put to a majority vote, so Buddhi takes the union and skips the voting
  ceiling.

</details>

## How it works

Buddhi splits a PR review into two halves: the decision, and the I/O around it. The
[Buddhi kernel](https://github.com/buddhikernel/buddhi) makes the decision — for each
review comment it decides whether to fix it, ask you, skip it, or defer it.
`buddhi-review` is the **adapter** that does everything around that decision on the
GitHub side: it reads the comments off your PR, hands each one to the kernel, and
carries out whatever the kernel returns. Concretely, for each comment the loop:

1. **Classifies** it into one of six labels: `SUBSTANTIVE`, `COSMETIC`,
   `BUSINESS_QUESTION`, `PR_DESCRIPTION`, `OUTDATED`, `INVALID`. If the classifier
   can't produce a usable label, the comment becomes a synthetic
   `CLASSIFICATION_FAILED` and is escalated to you when the interrupt budget (a
   daily cap on how many times the loop may interrupt you) allows, otherwise
   deferred — so a comment is never silently lost just because it could not be
   classified.
2. **Maps** the label onto a kernel work item and runs it through the kernel's
   decision pipeline, a fixed set of seven checks (see the
   [Buddhi kernel](https://github.com/buddhikernel/buddhi)).
3. **Acts** on the kernel's disposition:

   | Kernel disposition | What happens |
   |---|---|
   | fix | dispatch a fixer (SUBSTANTIVE / COSMETIC) |
   | escalate | ask you via the console answer-file channel (BUSINESS_QUESTION / PR_DESCRIPTION / classifier failure) |
   | skip | do nothing (OUTDATED / INVALID) |
   | defer | the day's human-interrupt budget is spent: hold the item, never drop it |
   | already-resolved | the comment was already resolved before the loop reached it — no action taken |

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

Two additional cases always route to you regardless of the docs: a `PR_DESCRIPTION`
comment (a reviewer asked you to update the PR body itself) and a
`CLASSIFICATION_FAILED` comment (the classifier could not produce a usable label).
Both escalate when your interrupt budget allows, and defer rather than drop if it
doesn't.

When it does need you, the question is written to an editable answer file: the loop
prints a `file://` link, you type a number (or free text) on the `>` line and save,
and the loop picks it up. Everything happens locally.

This answer-file prompt sits behind a small notifier interface, so other delivery
channels can be added later without touching the review loop.

## Status

buddhi-review is in **alpha**: the CLI flags, output format, and Python API may
change between releases, with no semantic-versioning guarantees before v1.0. It has
been exercised end to end but not yet hardened across a wide range of repositories,
so expect rough edges. Issues and PRs are welcome.

Run the test suite with `pip install -e ".[test]" && python3 -m pytest -q`.

## Architecture

`buddhi-review` is a thin adapter over the
[Buddhi kernel](https://github.com/buddhikernel/buddhi): the adapter does only the
GitHub I/O, and every decision stays in the kernel. The full design, including the
four adapter operations and five extension seams, is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## License

MIT. See [LICENSE](https://github.com/buddhikernel/buddhi-review/blob/main/LICENSE).

This package depends on the [Buddhi kernel](https://github.com/buddhikernel/buddhi)
(`buddhikernel`), which is licensed under Apache-2.0.
