---
title: Getting started
---

# Getting started

This guide takes you from the required tools and accounts to your first PR reviewed by
Buddhi. You will install the package, connect at least one reviewer, run the setup
wizard, and start a review.

> Buddhi sends the PR through repeated review-and-fix rounds. When it needs your
> judgment, it links you to a local answer file. It never merges unless you opt in.

## 1. Before you start

You need these four things in place:

- **Claude Code** with the `claude` CLI installed and signed in — the loop drives
  `claude -p` to classify comments and apply fixes.
- **The GitHub CLI (`gh`)** installed and authenticated, so that `gh auth status`
  confirms that you are signed in. Triggering Copilot reviews requires `gh` 2.87 or
  later.
- **Python 3.9+**.
- **A GitHub repository in which you can open pull requests** and configure at least
  one supported reviewer (see [Choose and set up your reviewers](#3-choose-and-set-up-your-reviewers)).

Each reviewer you enable also has its own plan, app, workflow, or secret requirements,
covered in step 3.

## 2. Install

```bash
pip install buddhi-review
```

This installs the `buddhi-review` command, the
[Buddhi kernel](https://github.com/buddhikernel/buddhi), and the bundled `/review-pr`
and `/open-pr` skills. Install the two bundled skills in Claude Code with one command:

```bash
buddhi-review install-skills
```

This copies each skill into `~/.claude/skills/<name>/` (or `$CLAUDE_CONFIG_DIR/skills`
if that is set). It is idempotent and safe to re-run at any time: it skips files that
are already current, updates ones it installed earlier, and refuses to overwrite a skill
file you have edited yourself (pass `--force` to overwrite, which backs up the existing
file first). Run `buddhi-review install-skills --uninstall` to remove them again.

Restart Claude Code to load the new skills. Each skill is installed under
`~/.claude/skills/<name>/SKILL.md`. If a slash command with the same name already
exists, the skill takes precedence.

Verify the installation with the offline health check, which runs the kernel-driven
pipeline on built-in fixtures without network access or a `claude` CLI invocation:

```bash
python3 -m buddhi_review self-check
```

A successful check ends with `SELF-CHECK OK — the kernel decided every disposition.`

## 3. Choose and set up your reviewers

Buddhi supports up to four reviewers, but it does not install their vendor apps or
workflows. Configure each reviewer on the repository before enabling it. Disabled
reviewers are excluded from triggering, waiting, and run summaries.

### What a review costs you

Buddhi is free and MIT-licensed, but each review consumes quota from the provider
accounts you connect. Buddhi does not bill you or proxy reviews through its own
accounts. Each enabled reviewer uses the corresponding account or plan, and the loop's
own classify and fix calls run on your machine against your Claude subscription:

| Surface | Account or quota used |
|---|---|
| **Copilot review** | Your **GitHub AI credits** (a paid GitHub Copilot plan). |
| **`claude[bot]` review** | Your **GitHub Actions minutes** on a private repo (the bundled `claude-code-review.yml` workflow runs on each summon; public repos on standard runners are free) plus your Claude subscription (`CLAUDE_CODE_OAUTH_TOKEN`) or pay-as-you-go API credit (`ANTHROPIC_API_KEY`) — whichever the repo secret holds. |
| **Codex review** | Your **ChatGPT plan** (the OpenAI Codex GitHub app). |
| **Gemini review** | Your **Gemini Code Assist** entitlement. |
| **The loop's own classify / fix calls** | Your **Claude subscription**: the loop drives the local `claude` CLI to classify each comment and apply fixes. |

Two forms of usage may appear in your GitHub billing: Copilot AI credits and GitHub
Actions minutes (the `claude[bot]` workflow). Watch or cap them at
**[github.com/settings/billing/summary](https://github.com/settings/billing/summary)**.
The remaining usage is covered by the corresponding vendor accounts or plans (ChatGPT
for Codex, Gemini Code Assist, and your Claude subscription for the loop itself).

Enable only reviewers for which you have the required account or plan. `/review-pr setup`
disables the others, so they are not triggered and consume no quota.

### Reviewer requirements

Buddhi triggers each reviewer differently, and each needs its vendor app or workflow
installed on the repository first:

| Reviewer | How the loop triggers it | What you must set up first |
|---|---|---|
| **Copilot** | requests review via the `requested_reviewers` API (`copilot-pull-request-reviewer[bot]`) | a paid GitHub **Copilot** plan with **code review** enabled, and `gh` ≥ 2.87 |
| **Gemini** | `/gemini review` comment | the **Gemini Code Assist** GitHub app installed on the repository or organization |
| **Codex** | `@codex review` comment | the **OpenAI Codex** GitHub app and a **ChatGPT** plan |
| **Claude** | `@claude review` comment | the bundled **`claude-code-review.yml`** workflow and a `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` repo secret |

The full per-reviewer how-to (apps, plans, triggers, and the `auto_on_open` setting for
each) is in
[`references/reviewer-setup.md`](https://github.com/buddhikernel/buddhi-review/blob/main/buddhi_review/skills/review-pr/references/reviewer-setup.md).
Config keys and the plan → model mapping are in
[`references/configuration.md`](https://github.com/buddhikernel/buddhi-review/blob/main/buddhi_review/skills/review-pr/references/configuration.md).

### Claude workflow setup

<details markdown="1">
<summary><b>Only if you enable the Claude reviewer</b></summary>

Claude review is workflow-driven, not app-driven. Install the bundled GitHub Actions
workflow — [`claude-code-review.yml`](https://github.com/buddhikernel/buddhi-review/blob/main/buddhi_review/skills/review-pr/references/claude-code-review.yml)
— on your repository's **default branch**, then add a Claude credential as a repo
secret: either a subscription token generated with `claude setup-token` in
`CLAUDE_CODE_OAUTH_TOKEN`, or a pay-as-you-go key in `ANTHROPIC_API_KEY` (use whichever
you have):

```bash
gh secret set CLAUDE_CODE_OAUTH_TOKEN   # or: gh secret set ANTHROPIC_API_KEY
```

Copy the workflow without modification. The workflow must preserve the exact
`No issues found.` output on a clean review, because the loop uses that string to
detect an all-clear.

**Re-running `/review-pr setup` checks the Claude integration.** If Claude stops posting
reviews, its stored token may have expired or been entered incorrectly — setup checks
the repo's last run and offers to generate and store a replacement token. It also offers
to update the bundled workflow when a newer version is available; workflow updates are
proposed through a PR, so the previous version remains available in Git history.

</details>

## 4. Run the setup wizard

```bash
/review-pr setup
```

The wizard is an interactive terminal program, so it opens in a **new terminal
window**; complete it there. It walks you through:

- a **tooling doctor** that checks `gh` and `claude` are present and authenticated;
- your **Claude plan**, which determines the model used for each role;
- the **repository** to configure; and
- your **reviewer fleet**: which reviewers — Copilot, Gemini, Codex, and Claude — you
  have configured, and whether each one reviews new PRs automatically (`auto_on_open`).

It then confirms the **local answer-file channel** for questions that require your
input.

The wizard stores your global settings in `~/.config/review-loop/config.yaml`. When you
run the loop from inside a repository without `--repo`, it uses the current repository
automatically. When you pass `--repo OWNER/REPO`, that repository must be confirmed
unless a global default fleet is configured or `BUDDHI_ALLOW_UNCONFIRMED_REPO=1` is set.

## 5. Review a PR

Two entry points:

```bash
# Review an existing PR:
/review-pr <pr>

# …or open a PR from your current branch and review it in one step:
/open-pr
```

Both commands launch the review loop as a detached process and immediately return
control to you. The loop continues even after the Claude Code session ends.

### What you will see during a run

During a run, you will see:

- **the live-log location** — the launcher prints the log path and a
  `tail -n +1 -f <log>` command. On macOS, it also prints a clickable `file://` "Watch"
  link;
- **a per-round summary table** — one row per enabled reviewer, showing what each posted
  and its current status; if a reviewer stops participating, the summary displays the
  reason;
- **clearance requests** when the loop needs a decision from you — it writes the
  question to an editable answer file and prints a `file://` link; enter a number (or
  your own text) on the answer line, save, and the loop continues.

<details markdown="1">
<summary><b>Example run output</b></summary>

The launcher prints a log path and a live-log hint:

```text
log: /tmp/buddhi-review-<user>/buddhi-<repo>-PR123.log
Cleared for takeoff — buddhi-review launched (PID 4242) on PR #123
Telemetry (live log) — follow it with:  tail -n +1 -f "…/buddhi-<repo>-PR123.log"
```

On macOS, the launcher also prints a clickable `file://` "Watch" link that replays the
log from line 1 in a new terminal. The log basename carries the repo name (`<repo>` =
the part after `/` in `owner/repo`), so reviewing the same PR number in two different
repos never writes to the same file.

Each round prints a summary table — one row per enabled reviewer plus any other that
posted, shown for completeness — with what each reviewer posted and its status:
`Active ✅`, `Approved 👍`, `Reviewed — no findings ✓`, `Reviewed — no change ✓`,
`Polish-only 🧹`, `Quota exhausted ⚠️`, `PR too large 📦`, `Could not review ❌`,
`No review posted 🔇`, `Not configured (repo) 🔧`, `Not requested 🙅`, or the internal
fallback `excluded`:

```text
Round 1 of 10 summary
┌───────────┬────────┬─────…
│ Bot       │ Posted │  …
```

Clearance requests look like this:

```text
[Clearance — a decision the loop needs from you] How should item 'c4' be handled?
  1. Apply the suggested change
  2. Skip — the suggestion is not valid here
  3. Defer — this needs your judgment
  answer here → file:///…/review-answer-<repo>-PR123-c4.md
```

`⚙ [auto]` markers appear whenever the loop takes an action on its own (for example a
squash-merge on a clean exit, when you opt into auto-merge). Every autonomous action is
logged, so the complete action trail remains searchable in the log.

</details>

The loop runs review-and-fix rounds. It starts another review round only when the
current round applies at least one substantive fix that changes files. It stops when:

- the round applies no substantive code change — for example, the review is clean, the
  findings are only cosmetic or concern the PR description, or a substantive finding is
  skipped or produces no change;
- the round budget is exhausted — the budget scales with the size of the diff, with a
  minimum of two rounds; if the diff size cannot be determined, it falls back to ten; or
- it encounters a condition that requires human intervention, such as an unanswered
  escalation, a failed push, or a worktree it cannot restore.

Buddhi merges only when you enable auto-merge with `--auto-merge` or `auto_merge` in the
repository configuration. Otherwise, it hands the PR back to you for merging.

## 6. Advanced: drive the CLI directly

Most first-time users stay with `/review-pr` and `/create-pr`. To skip the slash
commands and drive the loop yourself:

```bash
python3 -m buddhi_review review-pr 123 --repo OWNER/REPO --cwd /path/to/checkout
```

<details markdown="1">
<summary><b>Run it as a detached background loop</b></summary>

```bash
LAUNCHER=$(python3 -c "import os, buddhi_review; print(os.path.join(os.path.dirname(buddhi_review.__file__), 'launch-review.sh'))" 2>/dev/null)
if [ -f "$LAUNCHER" ]; then
  bash "$LAUNCHER" 123 --repo OWNER/REPO --cwd /path/to/checkout
fi
```

</details>

## 7. Where to go next

- [`references/reviewer-setup.md`](https://github.com/buddhikernel/buddhi-review/blob/main/buddhi_review/skills/review-pr/references/reviewer-setup.md)
  — what each reviewer requires (vendor app, plan, trigger, `auto_on_open`).
- [`references/configuration.md`](https://github.com/buddhikernel/buddhi-review/blob/main/buddhi_review/skills/review-pr/references/configuration.md)
  — config keys and the plan → model mapping.
- [`references/env-vars.md`](https://github.com/buddhikernel/buddhi-review/blob/main/buddhi_review/skills/review-pr/references/env-vars.md)
  — every environment variable the skill recognises.
- [`README.md`](https://github.com/buddhikernel/buddhi-review/blob/main/README.md) — what Buddhi is and how the kernel decides.
- [Buddhi kernel](https://github.com/buddhikernel/buddhi) — the kernel's own design.
