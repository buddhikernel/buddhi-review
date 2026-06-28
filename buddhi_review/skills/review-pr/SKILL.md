---
name: review-pr
description: >
  Automated PR review loop. Triggers reviewer bot comments (Copilot, Gemini,
  Codex, Claude), classifies each review comment, and escalates business
  questions to the console answer-file. Run `/review-pr setup` on first use.
when_to_use: >
  When the user asks to review an open PR, run the bot review loop, or merge a PR
  after automated review. Also handles `/review-pr setup`.
argument-hint: "[pr-number] [owner/repo] [--rr|--rr-active]"
arguments:
  - $pr
  - $repo
allowed-tools:
  - Bash
  - Read
  - AskUserQuestion
hooks:
  PreToolUse:
    - matcher: Bash
      hooks:
        - type: command
          command: python3 -m buddhi_review.git_guardrail_hook
---

# /review-pr — automated PR review loop

**Buddhi lands your PRs.** A PR is a flight: it takes off, flies the review
rounds, and comes in clean, ready to land (merge) on the base branch.

Triggers reviewer bot fan-out and classifies review comments on an already-open
PR. It runs the review loop only, not prompt clarification, coding, or PR creation.
Decisions the loop needs from you are surfaced as **clearance requests**
(business questions) on a console answer-file.

## Critical behaviour rules

- **Only sanctioned interactive gates, otherwise silent.** The ONLY questions you
  may ask are the **first-run setup** prompt (Step 0), the **per-repo reviewer
  confirmation** prompt (Step 1.1, only when this repo's reviewers are
  unconfirmed), and the **PR-selection** prompt (Step 2). PR selection is skipped
  when a PR number was passed; if none was passed, resolve the single open PR (or
  ask the user which number, once). Run everything else back-to-back.
- **Never merge yourself.** Never run `gh pr merge`. Merge via GitHub once the
  review loop exits clean.
- **Never skip the review loop.** It MUST run; it has a 7-minute minimum wait
  built in. Do not short-circuit it, summarise it, or substitute your own review
  logic for it.
- If any step fails, log the error and stop. Do not ask the user what to do.

## Arguments

- PR number (optional): `/review-pr 42` or `/review-pr 42 owner/repo`.
- `owner/repo` (optional): only needed when the cwd is not inside the target repo.
- Re-request flags (mutually exclusive):
  - `--rr` — round 1 re-requests reviews from EVERY re-reviewable bot (clears the
    voluntarily-done / done-polishing exclusions). Use when you want every bot to
    look again.
  - `--rr-active` — round 1 re-requests only bots still actively engaged; exits
    cleanly if none remain. The usual choice after resolving a business question.

### Clearance requests (business questions) are answered from your terminal

When the loop needs a decision from you it requests **clearance** (a go/no-go
call). On a `BUSINESS_QUESTION` the loop writes a formatted panel to its log AND
an editable answer file, then prints a clickable `file://` link. Open the file, type
a number (or your own text) on the `>` line, and save. The loop applies your decision
as guidance and continues its rounds.

## Execution steps

### 0. First-run setup gate

Check whether the user has ever configured the skill — a machine with no
`~/.config/review-loop/config.yaml` runs on defaults and emits config-unset
warnings instead of asking the user to onboard:

```bash
test -s ~/.config/review-loop/config.yaml && echo configured || echo unconfigured
```

- **`configured`** — proceed silently to Step 1.
- **`unconfigured`** — ask with **AskUserQuestion** (sanctioned; ask ONCE):
  - Question: *"No review-loop config found. Set it up before launching?"*
  - Options:
    1. **Run setup now** *(recommended)* — open the interactive wizard in a fresh
       terminal window (it is a raw-mode TTY program this agent session cannot
       drive), then **EXIT** so the user can complete it:

       ```bash
       SETUP=$(python3 -c "import buddhi_review,os;print(os.path.join(os.path.dirname(buddhi_review.__file__),'launch-setup.sh'))")
       bash "$SETUP"
       ```

       On a headless host the launcher prints the one-liner to run by hand instead.
    2. **Proceed once with defaults** — continue to Step 1. The loop runs with
       defaults (plan `max-5x`, all four reviewers); fleet warnings may appear in
       the log this run.

This gate is interactive-only and best-effort; if you cannot prompt, proceed
silently with defaults.

### 1. Resolve the repo

Resolve `OWNER/REPO` and `CWD`:

1. **Infer from the cwd's git remote first.** If the cwd is inside a git repo,
   derive `OWNER/REPO` from its `origin` remote and set `CWD` to its toplevel:

   ```bash
   CWD=$(git rev-parse --show-toplevel)
   OWNER_REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
   ```

   If that resolves cleanly, proceed.
2. **Else accept an explicit `owner/repo` argument** and set `CWD` to the cwd (or
   a path argument if given).

### 1.1 Per-repo reviewer confirmation gate

Reviewer availability is **per-repo** — Copilot/Gemini/Codex are vendor GitHub
Apps installed per repo, and `claude[bot]` needs `claude-code-review.yml` committed
in each repo — so a fleet confirmed for one repo must NOT be assumed for another.
Using the `OWNER_REPO` resolved in Step 1, ask the status reader whether THIS
repo's reviewers have been confirmed:

```bash
python3 -m buddhi_review status --repo "$OWNER_REPO" 2>/dev/null
```

If `OWNER/REPO` could not be resolved, or the command is absent / prints nothing /
unparseable output, **skip this gate** and proceed to Step 2 — it is best-effort
and must NEVER block the loop. Otherwise parse the single JSON object
(`{"repo_confirmed": …, "has_global_default": …}`) and act on `repo_confirmed`:

- **`true`** — proceed silently to Step 2.
- **`false`** — ask with **AskUserQuestion** (a sanctioned gate; ask ONCE). **Do
  NOT configure reviewers in this session — every piece of deterministic setup
  (reviewer selection, auto-on-open, auto-merge, label-gated CI, GitHub-side
  provisioning) runs in the terminal wizard, never here:**
  - Question: *"Reviewers for `<OWNER/REPO>` haven't been confirmed (they're
    installed per-repo). Configure this repo now?"*
  - Options:
    1. **Run setup now** *(recommended)* — open the per-repo setup wizard in a
       fresh terminal window (a raw-mode TTY this session cannot drive), then
       **EXIT** so the user can finish it:

       ```bash
       SETUP=$(python3 -c "import buddhi_review,os;print(os.path.join(os.path.dirname(buddhi_review.__file__),'launch-setup.sh'))")
       bash "$SETUP" --repo "$OWNER_REPO"
       ```

       On a headless host the launcher prints the one-liner to run by hand
       instead. After it returns, reply exactly: ``Setup opened in a new window —
       finish it there, then re-run /review-pr.`` and **EXIT**.
    2. **Use global defaults** — continue to Step 2 without writing a per-repo
       entry; the loop runs with your global default fleet. (When
       `has_global_default` is `false` there is no fallback fleet and the review
       loop refuses to launch by design — pick option 1.)

This gate is interactive-only and **never configures reviewers itself** — it only
offers to launch the terminal wizard (the single deterministic setup brain) or
falls back to global defaults. If you cannot prompt, proceed to Step 2 with
defaults.

### 2. Select which PR to review

- If a PR number was given explicitly, use it directly: set `PR_NUMBER`, set
  `TARGET_CWD = <CWD>`, and skip the rest of this step.
- Otherwise list the open PRs and never silently pick the first one:

  ```bash
  gh pr list --repo "$OWNER_REPO" --state open --json number,title,headRefName
  ```

  - **No open PR** — print "No open PR found in <repo>. Nothing to review." and
    **EXIT**.
  - **Exactly one** — select it (no question): `PR_NUMBER` = that number,
    `TARGET_CWD = <CWD>`.
  - **More than one, but the cwd pins one** — when the current checkout's branch
    (`git -C <CWD> branch --show-current`) equals exactly one listed PR's
    `headRefName`, this session is already working in that PR's checkout — select
    it (no question): `PR_NUMBER` = that number, `TARGET_CWD = <CWD>`. Print ONE
    line: `Auto-selected this session's checkout: PR #<n>`. (A cwd on the base
    branch matches no PR head, so it never short-circuits the ask.)
  - **More than one otherwise** — ask with **AskUserQuestion** which PR number to
    review. Match the answer against the listed numbers; re-ask only if nothing
    matches.

The skill reviews whichever checkout the cwd points at; the loop materialises
the PR's worktree if it is not already checked out.

### 3. Launch the review loop (mandatory)

Run the launcher command — it returns immediately, so this is safe to run in the
foreground:

```bash
python3 -m buddhi_review review-pr <PR_NUMBER> --repo "$OWNER_REPO" --cwd "$TARGET_CWD" [--rr or --rr-active if the user passed it]
```

This is the front door: it selects the review engine, **detaches the loop** (so the
long-running work survives the Bash tool's timeout), prints where to watch the run,
and returns immediately. You do not need to resolve any script path yourself.

After the command returns, **your job is done.** Do NOT:

- re-run the loop or run any review module in the foreground,
- tail the log file,
- poll for progress,
- print any further summary.

## `/review-pr setup`

`/review-pr setup` runs the onboarding wizard. It is an interactive raw-mode TTY
program (arrow-key selectors, hidden secret prompts) this agent session cannot
drive, so open it in a fresh terminal window via the bundled launcher:

```bash
SETUP=$(python3 -c "import buddhi_review,os;print(os.path.join(os.path.dirname(buddhi_review.__file__),'launch-setup.sh'))")
bash "$SETUP"
```

`launch-setup.sh` ships inside the `buddhi_review` package; the one-liner resolves
its installed path. It opens `python3 -m buddhi_review setup` in a new window (and
on a headless host prints the one-liner to run by hand). The wizard walks you
through the tooling doctor, your Claude plan, the repo binding, and your reviewer
fleet, and confirms the console answer-file as the notification channel. Each
reviewer you enable still needs its vendor GitHub app + plan configured in advance.
See [`references/reviewer-setup.md`](references/reviewer-setup.md).

## References

- [`references/configuration.md`](references/configuration.md) — config keys and
  the plan → model mapping.
- [`references/reviewer-setup.md`](references/reviewer-setup.md) — what each
  reviewer requires (vendor app, plan, trigger, `auto_on_open`).
- [`references/claude-code-review.yml`](references/claude-code-review.yml) — the
  GitHub Actions workflow that makes `claude[bot]` review PRs (install it on the
  repo's default branch + set `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`).

## Scope

Single repo only. To review multiple repos, call `/review-pr` separately for each.
