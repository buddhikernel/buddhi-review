# Getting started

This walks you from a fresh machine to your first PR reviewed by Buddhi. It takes a
few minutes, most of which is one-time reviewer setup you do once per repo.

> **Buddhi lands your PRs.** A pull request is a flight: it takes off, flies the
> review rounds, and comes in clean, ready to land (merge). Buddhi reads each
> reviewer's comments, classifies them, and lets the kernel decide what to do; you
> answer anything it can't decide right in your terminal.

## Before you start

You need four things in place:

- **Claude Code** with the `claude` CLI installed and signed in (the loop drives
  `claude -p` to classify comments and apply fixes).
- **The GitHub CLI (`gh`)** installed and authenticated (`gh auth status` is clean).
  Copilot summoning needs `gh` **≥ 2.87**.
- **Python 3.9+**.
- **A GitHub repository you can open PRs on**, with at least one reviewer bot you can
  enable (see [Set up your reviewers](#3-set-up-your-reviewers) below).

## 1. Install

```bash
pip install buddhi-review
```

This pulls the kernel ([`buddhikernel`](https://github.com/buddhikernel/buddhi)) and
`PyYAML`, and installs the `buddhi-review` command. Then add the two slash-command
skills to Claude Code as **skills** (one time; each becomes `~/.claude/skills/<name>/SKILL.md`):

```bash
SKILLS=$(python3 -c "import buddhi_review, os; print(os.path.join(os.path.dirname(buddhi_review.__file__), 'skills'))" 2>/dev/null)
if [ -d "$SKILLS" ]; then
  mkdir -p ~/.claude/skills/
  rm -rf ~/.claude/skills/review-pr ~/.claude/skills/open-pr ~/.claude/skills/create-pr
  cp -R "$SKILLS"/review-pr "$SKILLS"/open-pr ~/.claude/skills/
  echo "✓ Skills installed to ~/.claude/skills/ — restart Claude Code to load them"
else
  echo "✗ Error: Could not locate buddhi_review skills. Ensure buddhi-review is installed in the active Python environment."
fi
```

Restart Claude Code afterward so it picks up the new skills directory. (If a
slash-command of the same name already exists, the skill takes precedence.)

Sanity-check the install with the offline health check, which runs the kernel-driven
pipeline on built-in fixtures, with no network and no `claude` call:

```bash
python3 -m buddhi_review self-check
```

A clean install ends with `SELF-CHECK OK — the kernel decided every disposition.`

## 2. Run the setup wizard

```bash
/review-pr setup
```

The wizard is an interactive terminal program, so it opens in a **fresh terminal
window**; complete it there. It walks you through:

- a **tooling doctor** that checks `gh` and `claude` are present and authenticated,
- your **Claude plan** (drives which model each role uses),
- the **repository** to bind, and
- your **reviewer fleet**: which of Copilot / Gemini / Codex / Claude you have, and
  for each, whether it already reviews a PR automatically when one opens
  (`auto_on_open`).

It then confirms the **console answer-file** as your notification channel and writes
everything to `~/.config/review-loop/config.yaml`. If you run without `--repo` (the
normal case when you're inside the target repo), the loop proceeds without a config
file. If you pass `--repo OWNER/REPO` explicitly, the repo must be confirmed first.
An unconfirmed repo exits with a setup banner unless a global default fleet is already
configured or `BUDDHI_ALLOW_UNCONFIRMED_REPO=1` is set.

## 3. Set up your reviewers

Buddhi can drive up to four review bots, but **none is automatic for a new user**.
Each one needs its vendor app or workflow installed on the repo first, or the loop's
trigger comment fires into a void. Enable only the reviewers you actually have; the
rest are subtracted everywhere.

| Reviewer | How the loop triggers it | What you must set up first |
|---|---|---|
| **Copilot** | requests review via the `requested_reviewers` API (`copilot-pull-request-reviewer[bot]`) | a paid GitHub **Copilot** plan with **code review** enabled, and `gh` ≥ 2.87 |
| **Gemini** | `/gemini review` comment | the **Gemini Code Assist** GitHub app installed on the repo/org |
| **Codex** | `@codex review` comment | the **OpenAI Codex** GitHub app + a **ChatGPT** plan |
| **Claude** | `@claude review` comment | the bundled **`claude-code-review.yml`** workflow + a `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` repo secret |

### The `claude[bot]` reviewer needs a workflow

Claude review is workflow-driven, not app-driven. Install the bundled GitHub Actions
workflow — [`claude-code-review.yml`](https://github.com/buddhikernel/buddhi-review/blob/main/buddhi_review/skills/review-pr/references/claude-code-review.yml)
— on your repository's **default branch**, then add a Claude credential as a repo
secret: either a `claude setup-token` subscription token in `CLAUDE_CODE_OAUTH_TOKEN`
or a pay-as-you-go key in `ANTHROPIC_API_KEY` (use whichever you have):

```bash
gh secret set CLAUDE_CODE_OAUTH_TOKEN   # or: gh secret set ANTHROPIC_API_KEY
```

Ship the workflow verbatim: its prompt emits a literal `No issues found.` line on a
clean review, and the loop's clean-review detector is coupled to that exact string.

**Re-running `/review-pr setup` keeps this current.** If Claude's reviews go silent,
its stored token may have expired or been mis-pasted — setup checks the repo's last
run and offers to re-mint the secret. It also offers to update the bundled workflow
when a newer version has shipped (your old copy stays in the update PR's history).

The full per-reviewer how-to (apps, plans, triggers, and the `auto_on_open` setting
for each) is in
[`references/reviewer-setup.md`](https://github.com/buddhikernel/buddhi-review/blob/main/buddhi_review/skills/review-pr/references/reviewer-setup.md).
Config keys and the plan → model mapping are in
[`references/configuration.md`](https://github.com/buddhikernel/buddhi-review/blob/main/buddhi_review/skills/review-pr/references/configuration.md).

## 4. Review a PR

Two entry points:

```bash
# Review a PR that already exists:
/review-pr <pr>

# …or open a PR from your current branch and review it in one step:
/open-pr
```

Both launch the review loop **detached** so it survives the session, and hand control
back to you. Here is what you see while it flies:

- **A launch line and a live-log link.** The launcher prints the log path and a
  *Telemetry (live log)* hint. Follow the run with `tail -n +1 -f <log>` (on macOS a
  clickable `file://` "Watch" link opens a window that replays the run from line 1):

  ```text
  log: /tmp/buddhi-review-<user>/buddhi-<repo>-PR123.log
  Cleared for takeoff — buddhi-review launched (PID 4242) on PR #123
  Telemetry (live log) — follow it with:  tail -n +1 -f "…/buddhi-<repo>-PR123.log"
  ```

  The log basename carries the repo name (`<repo>` = the part after `/` in `owner/repo`),
  so reviewing the same PR number in two different repos never writes to the same file.

- **A per-round summary table**: one row per reviewer — every built-in reviewer
  plus any other that posted, shown for completeness — with what each posted
  and its status (`Active`, `Approved`, `Reviewed — no findings`,
  `Reviewed — no change`, `Polish-only`, `Quota exhausted`,
  `PR too large to review`, `Could not review`, `No review posted`,
  `Not configured (repo)`, `Not requested ·` for a reviewer outside the
  enabled fleet, or the internal fallback `excluded`), so a
  reviewer that drops out of the expected set never disappears without a
  reason:

  ```text
  Round 1 of 10 summary
  ┌───────────┬────────┬─────…
  │ Bot       │ Posted │  …
  ```

- **Clearance requests** when the loop needs a decision from you. It writes the
  question to an editable answer file and prints a `file://` link; open it, type a
  number (or your own text) on the answer line, save, and the loop continues:

  ```text
  [Clearance — a decision the loop needs from you] How should item 'c4' be handled?
    1. Apply the suggested change
    2. Skip — the suggestion is not valid here
    3. Defer — this needs your judgment
    answer here → file:///…/review-answer-<repo>-PR123-c4.md
  ```

- **`⚙ [auto]` markers** whenever the loop takes an action on its own (for example a
  squash-merge on a clean exit, when you opt into auto-merge). Every autonomous action
  is logged so the trail is greppable.

The loop runs review→fix rounds and ends the moment a round produces no substantive
progress — a round that lands a real fix earns another review round, but a cosmetic-only
(or nothing-to-fix) round finishes clean. If the round budget runs out with the final
round clean, that exit is treated like any other clean finish. It **never merges unless
you opt in** (`--auto-merge`); by default it stops on a clean pass and leaves the merge
to you.

### Drive the CLI directly

You can skip the slash commands and drive the loop yourself:

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
| **`claude[bot]` review** | Your **GitHub Actions minutes** on a private repo (the bundled `claude-code-review.yml` workflow runs on each summon; public repos on standard runners are free) plus your Claude subscription (`CLAUDE_CODE_OAUTH_TOKEN`) or pay-as-you-go API credit (`ANTHROPIC_API_KEY`) — whichever the repo secret holds. |
| **Codex review** | Your **ChatGPT plan** (the OpenAI Codex GitHub app). |
| **Gemini review** | Your **Gemini Code Assist** entitlement. |
| **The loop's own classify / fix calls** | Your **Claude subscription**: the loop drives the local `claude` CLI to classify each comment and apply fixes. |

Two of these meters are on your GitHub bill: **GitHub AI credits** (Copilot) and
**GitHub Actions minutes** (the `claude[bot]` workflow). Watch or cap them at
**[github.com/settings/billing/summary](https://github.com/settings/billing/summary)**.
The rest are the vendor plans you already pay for (ChatGPT for Codex, Gemini Code
Assist, your Claude subscription for the loop itself).

Enable only the reviewers whose plans you hold. `/review-pr setup` subtracts the
rest, so no trigger fires into a void and nothing is spent on a reviewer you do not
run.

## Where to go next

- [`references/reviewer-setup.md`](https://github.com/buddhikernel/buddhi-review/blob/main/buddhi_review/skills/review-pr/references/reviewer-setup.md)
  — what each reviewer requires (vendor app, plan, trigger, `auto_on_open`).
- [`references/configuration.md`](https://github.com/buddhikernel/buddhi-review/blob/main/buddhi_review/skills/review-pr/references/configuration.md)
  — config keys and the plan → model mapping.
- [`references/env-vars.md`](https://github.com/buddhikernel/buddhi-review/blob/main/buddhi_review/skills/review-pr/references/env-vars.md)
  — every environment variable the skill recognises.
- [`README.md`](https://github.com/buddhikernel/buddhi-review/blob/main/README.md) — what Buddhi is and how the kernel decides.
- [Buddhi kernel](https://github.com/buddhikernel/buddhi) — the kernel's own design.
