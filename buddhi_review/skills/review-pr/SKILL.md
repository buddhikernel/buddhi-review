---
name: review-pr
description: >
  Automated PR review loop. Triggers reviewer bot comments (Copilot, Gemini,
  Codex, Claude), classifies each review comment, and escalates business
  questions as clearance requests. Run `/review-pr setup` on first use.
when_to_use: >
  When the user asks to review an open PR, run the bot review loop, or merge a PR
  after automated review. Also handles `/review-pr setup`.
argument-hint: "[pr-number] [owner/repo] [--rr|--rr-active|--rr-none]"
arguments:
  - $pr
  - $repo
allowed-tools:
  - Bash
  - Read
  # Edit/Write: Step 2.5 option 1 ("I resolve the conflicts") has the agent edit
  # conflicted files mid-rebase before `git add` + `rebase --continue`.
  - Edit
  - Write
  - AskUserQuestion
hooks:
  PreToolUse:
    - matcher: Bash
      hooks:
        - type: command
          # Shell-agnostic dispatch (works under sh AND cmd.exe — no POSIX builtins):
          # plugin install ($CLAUDE_PLUGIN_ROOT set) runs the plugin entry, which makes
          # the SessionStart-installed package importable and degrades fail-open (one
          # stderr line, exit 0) if it is still absent; pip install (skill copied to
          # ~/.claude/skills, no $CLAUDE_PLUGIN_ROOT) runs the module directly, unchanged.
          # Both invoke buddhi_review.git_guardrail_hook.
          command: python3 -c "import os,sys,runpy; root=os.environ.get('CLAUDE_PLUGIN_ROOT'); entry=os.path.join(root,'scripts','guardrail_hook.py') if root else ''; runpy.run_path(entry,run_name='__main__') if (entry and os.path.isfile(entry)) else runpy.run_module('buddhi_review.git_guardrail_hook',run_name='__main__')"
---

# /review-pr — automated PR review loop

**Buddhi lands your PRs.** A PR is a flight: it takes off, flies the review
rounds, and comes in clean, ready to land (merge) on the base branch.

## Critical behaviour rules

- **Only sanctioned interactive gates, otherwise silent.** The ONLY questions you
  may ask are the **first-run onboarding** prompt (Step 0, only when this machine has
  no config), the **per-repo reviewer confirmation** prompt (Step 1.1, only when this
  repo's reviewers are unconfirmed), the **PR-selection** prompt (Step 2), and the
  **pre-launch rebase** prompt (Step 2.5, only when the engine reports a non-clean
  rebase status). PR-selection is skipped when a PR number was passed explicitly.
  Everything else runs silently.
- **NEVER pause for confirmation** between any OTHER steps. Run them back-to-back.
  Never ask the user what to do on error — log it and stop.
- **Reviewers trigger based on the fleet confirmed for THIS repo** (per-repo, because
  the vendor GitHub Apps + the claude workflow are installed per repo; set during the
  Step 1.1 confirm or `/review-pr setup`, stored under `repos:` in
  `~/.config/review-loop/config.yaml`), falling back to your global default, then the
  built-in Copilot/Gemini/Codex/Claude set. If a reviewer is not responding, confirm
  reviewers for this repo (Step 1.1) or run `/review-pr setup`. Do not assume any
  specific reviewer is auto-triggered.
- **NEVER merge manually.** Never run `gh pr merge` yourself. Whether the loop squash-merges on
  a clean exit is the engine's call — this skill passes no merge flag, so the loop runs on its
  own default, which is NOT to merge: it notifies you on a clean exit and you merge via GitHub.
  On a non-clean exit it notifies you as well. Do not assume a merge happened.
- **NEVER skip the review loop.** It MUST run. It has a 7-minute minimum wait built
  in. Do not short-circuit it, summarise it, or substitute your own review logic for
  it.
- If any step fails, log the error and stop. Do not ask the user what to do.

## Arguments

Repo name as first argument: `/review-pr owner/repo`. It is optional — when omitted the
repo is inferred from the current directory's git remote.

Optional PR number: `/review-pr owner/repo 42` (or just `/review-pr 42` inside the
repo). When omitted, Step 2 selects the PR.

Optional re-request flags (mutually exclusive):

- `--rr` — round 1 re-requests fresh reviews from EVERY re-reviewable bot, clearing the
  voluntarily-done / done-polishing exclusions. Use when you want every bot to look
  again.
- `--rr-active` — round 1 re-requests only bots still actively engaged (skips bots that
  already +1'd, hit quota, or never responded); exits cleanly if none remain. The usual
  choice after resolving a clearance request — it preserves the thumbs-up bots'
  approvals.
- `--rr-none` — summons no reviewers: resolves the review comments already on the PR and
  merges on a clean exit. The one explicit way to lift the never-merge-unreviewed block.

### Clearance requests (business questions) are answered from your terminal

When the loop needs a decision from you it requests **clearance** (a go/no-go call). On a
`BUSINESS_QUESTION` the loop writes a formatted panel to its log AND an editable answer
file, then prints a clickable `file://` link. Open the file, type a number (or your own
text) on the `>` line, and save. The loop applies your decision as guidance and continues
its rounds.

## What this does

Runs the automated bot review-and-fix loop on an already-open PR in a **single repo**.
No prompt clarification. No coding. No PR creation. Just the review loop.

## Execution steps

### 0. First-run onboarding gate

Before anything else, check whether the user has ever completed setup — a machine with no
config runs the loop with defaults and emits config-unset warnings instead of asking the
user to onboard:

```bash
test -s ~/.config/review-loop/config.yaml && echo configured || echo unconfigured
```

- **`configured`** — proceed silently to Step 1.
- **`unconfigured`** — ask with **AskUserQuestion** (a sanctioned gate; ask ONCE):
  - Question: *"No buddhi config found (`~/.config/review-loop/config.yaml`). Set it up before launching?"*
  - Options:
    1. **Run setup now** *(recommended)* — open the interactive wizard in a **fresh
       terminal window** (your agent session stays alive — the wizard is a raw-mode TTY
       you cannot drive), then **EXIT**:

       ```bash
       SETUP=$(PYTHONPATH="${CLAUDE_PLUGIN_DATA}/site:$PYTHONPATH" python3 -c "import buddhi_review,os;print(os.path.join(os.path.dirname(buddhi_review.__file__),'launch-setup.sh'))")
       PYTHONPATH="${CLAUDE_PLUGIN_DATA}/site:$PYTHONPATH" bash "$SETUP"
       ```

       On success, reply exactly: ``Setup opened in a new window — finish it there, then
       re-run /review-pr.`` and **EXIT**. Only on a headless host with no window server
       does the launcher instead print a ready-to-run command itself — relay that exact
       line and **EXIT**.
    2. **Proceed once with defaults** — continue to Step 1. The loop runs with defaults;
       fleet warnings may appear in the log this run.

This gate is interactive-only and best-effort; if you cannot prompt, proceed silently with
defaults. It must NEVER block the loop.

### 1. Resolve repo

Resolve `OWNER/REPO` and `CWD` in this order:

1. **If the user passed an explicit `owner/repo` (or repo-name) argument, honor it
   first**: set `OWNER_REPO` to that literal argument value and `CWD` to the cwd or a
   given path. Running `/review-pr owner/target` from an unrelated checkout must target
   `owner/target`, never silently fall back to whatever repo the cwd happens to sit in —
   do NOT run the `gh repo view` fallback below when an explicit argument was given.
2. **Else, infer from the current directory's git remote first.** If the cwd is inside a
   git repo, derive `OWNER/REPO` from its `origin` remote and set `CWD` to its toplevel:

   ```bash
   CWD=$(git rev-parse --show-toplevel)
   OWNER_REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
   ```

Step 2 picks the PR to review and the checkout to review it in (`TARGET_CWD`).

> **Carry the resolved values forward yourself.** Each Bash call runs in its OWN shell, so
> `CWD` / `OWNER_REPO` / `PR_NUMBER` / `BASE_BRANCH` do NOT survive from one step's code block
> to the next. Read each value out of the command's output and substitute it literally into the
> later commands (or re-derive it in the same block that uses it). Never run a later step with
> an empty variable.

### 1.1 Per-repo reviewer confirmation gate

Reviewer availability is **per-repo** — Copilot/Gemini/Codex are vendor GitHub Apps
installed per repo, and `claude[bot]` needs `claude-code-review.yml` committed in each repo
— so a fleet confirmed for one repo must NOT be assumed for another. Using the `OWNER_REPO`
resolved in Step 1, ask the status reader whether THIS repo's reviewers have been confirmed:

```bash
PYTHONPATH="${CLAUDE_PLUGIN_DATA}/site:$PYTHONPATH" python3 -m buddhi_review status --repo "$OWNER_REPO" 2>/dev/null
```

If `OWNER/REPO` could not be resolved, or the command is absent / prints nothing /
unparseable output, **skip this gate** and proceed to Step 2 — it is best-effort and must
NEVER block the loop. Otherwise parse the single JSON object (`{"repo_confirmed": …,
"has_global_default": …}`) and act on `repo_confirmed`:

- **`true`** — proceed silently to Step 2.
- **`false`** — ask with **AskUserQuestion** (a sanctioned gate; ask ONCE). **Do NOT
  configure reviewers in this session — every piece of deterministic setup (reviewer
  selection, auto-on-open, auto-merge, label-gated CI, GitHub-side provisioning) runs in the
  terminal wizard, never here:**
  - Question: *"Reviewers for `<OWNER/REPO>` haven't been confirmed (they're installed
    per-repo). Configure this repo now?"*
  - Options:
    1. **Run setup now** *(recommended)* — open the per-repo setup wizard in a fresh
       terminal window (a raw-mode TTY this session cannot drive), then **EXIT** so the user
       can finish it:

       ```bash
       SETUP=$(PYTHONPATH="${CLAUDE_PLUGIN_DATA}/site:$PYTHONPATH" python3 -c "import buddhi_review,os;print(os.path.join(os.path.dirname(buddhi_review.__file__),'launch-setup.sh'))")
       PYTHONPATH="${CLAUDE_PLUGIN_DATA}/site:$PYTHONPATH" bash "$SETUP" --repo "$OWNER_REPO"
       ```

       On a headless host the launcher prints the one-liner to run by hand instead. After it
       returns, reply exactly: ``Setup opened in a new window — finish it there, then re-run
       /review-pr.`` and **EXIT**.
    2. **Use global defaults** *(offer only when `has_global_default` is `true`)* — continue
       to Step 2 without writing a per-repo entry; the loop runs with your global default
       fleet. When `has_global_default` is `false`, omit this option entirely — there is no
       fallback fleet and the loop will refuse to launch; option 1 is the only path.

This gate is interactive-only and **never configures reviewers itself** — it only offers to
launch the terminal wizard (the single deterministic setup brain) or falls back to global
defaults. If you cannot prompt, proceed to Step 2 with defaults.

### 2. Select which PR to review

If a PR number was given explicitly, use it directly — set `PR_NUMBER`. Prefer `<CWD>` when it
is ALREADY checked out on that PR's own branch — consulting the registry in that case could
override a correct checkout with a stale worktree the git-guardrail hook recorded earlier in
the session. Only when `<CWD>` is NOT on the PR's branch, resolve `TARGET_CWD` through the
session→worktree registry rather than assuming `<CWD>`: when the work was done in a NEW
worktree off `main` (the standing rule), `$PWD` can still point at the spawn checkout while the
PR is actually checked out elsewhere, and the registry (populated by the git-guardrail hook on
`git worktree add` / `git -C <worktree>`) knows where.

```bash
PR_HEAD_BRANCH=$(gh pr view "$PR_NUMBER" --repo "$OWNER_REPO" --json headRefName -q .headRefName)
if [ -n "$PR_HEAD_BRANCH" ] && [ "$(git -C "$CWD" branch --show-current)" = "$PR_HEAD_BRANCH" ]; then
  TARGET_CWD="$CWD"
else
  TARGET_CWD=$(PYTHONPATH="${CLAUDE_PLUGIN_DATA}/site:$PYTHONPATH" python3 -m buddhi_review.worktree_target resolve \
    --session-id "$CLAUDE_CODE_SESSION_ID" --repo "$OWNER_REPO" --cwd "$CWD")
  : "${TARGET_CWD:=$CWD}"
fi
```

The resolver itself never raises and always prints a usable path — `<CWD>` unchanged when
nothing better is recorded, else the session's own worktree; the `:` fallback above additionally
covers the invocation failing outright (e.g. a missing `python3`) and printing nothing at all.
Then skip to the **checked-out check** at the end of this step (it runs on EVERY path, including
this one).

Otherwise enumerate the open PRs (each annotated with the worktree it is checked out in) —
never silently pick the first one:

```bash
PYTHONPATH="${CLAUDE_PLUGIN_DATA}/site:$PYTHONPATH" python3 -m buddhi_review.worktree_target list \
  --cwd "$CWD" --repo "$OWNER_REPO" --command review-pr \
  --caller-cwd "$PWD" --session-id "$CLAUDE_CODE_SESSION_ID"
```

Pass `--caller-cwd "$PWD"` AND `--session-id "$CLAUDE_CODE_SESSION_ID"` **verbatim** (the
shell expands both). `--caller-cwd` lets the engine recognise that this session is sitting
in one candidate PR's checkout and auto-select that PR. `--session-id` covers the case it
cannot: when you did your work in a NEW worktree off `main`, `$PWD` stays at the spawn
checkout and does NOT name that worktree — but the git-guardrail hook recorded
`session_id → that worktree` automatically (on `git worktree add` / `git -C <worktree>`),
so the engine resolves and auto-selects the matching PR WITHOUT asking.
(`$CLAUDE_CODE_SESSION_ID` is a real Claude Code env var — a plain UUID, no prefix —
exported into every Bash tool call; the hook receives the same value as the `session_id`
field in its stdin JSON payload, so the key it registers and the key looked up here are
byte-identical.)

Parse the single JSON object on stdout and act on `present.mode`:

- **`none`** — print "No open PR found in <repo>. Nothing to review." and **EXIT**.
- **`single`** — exactly one open PR. Auto-select it (no question): `PR_NUMBER` = that
  candidate's `open_pr.number`, `TARGET_CWD` = its `path` — or `<CWD>` when that `path`
  is `null` (the sole open PR is `kind == "pr-only"`, not checked out anywhere; see the
  `pr-only` note below).
- **`caller`** — several PRs are open, but exactly one candidate is this session's own
  checkout — either where `$PWD` sits (`caller_match`) or, when `$PWD` is elsewhere, the
  worktree this session worked in, resolved from the session→worktree registry
  (`session_match`). Auto-select it (no question): `PR_NUMBER` = that candidate's
  `open_pr.number`, `TARGET_CWD` = its `path`. (This candidate's `path` is never `null`
  here — both `caller_match` and `session_match` are resolved by comparing worktree
  paths, so only a candidate that already has a `path` can ever be matched.) Print ONE
  line — `Auto-selected this session's worktree: PR #<n> (<path>)` — and continue.
- **`two`** / **`many`** — ask with **AskUserQuestion** (a sanctioned gate) which PR to
  review: render each `present.options[]` (its `label` as the option, its `detail` as the
  description). Free-text **"Other"** is offered ONLY in `many` mode
  (`present.free_input == true`); match the typed text against the full `candidates` array
  by **PR number** (with or without a leading `#`) first, then branch substring — re-ask
  only if nothing matches. `value == "all"` → review **each** candidate PR sequentially,
  re-binding `PR_NUMBER` / `TARGET_CWD` (and re-deriving `BASE_BRANCH` in Step 2.5) per
  iteration — never carry one PR's values into the next. Otherwise set `PR_NUMBER` and
  `TARGET_CWD` from the chosen candidate exactly as in the `single` case.

A candidate whose `path` is `null` (`kind == "pr-only"`) is an open PR that is **not
checked out in any worktree**. The loop applies its fixes in `TARGET_CWD` and does not
check the PR out for you, so such a PR cannot be launched as it stands: set `TARGET_CWD` to
`<CWD>` and let the checked-out check below report the mismatch and stop with the recovery
instruction.

If the command fails (a non-zero exit with `{"status": "error", …}`), log its `detail` and
STOP — a PR list that could not be read must never be treated as "nothing to review".

**Checked-out check (runs on EVERY path, including an explicitly-passed PR number).** The loop
applies its fixes IN `TARGET_CWD` and commits + pushes whatever branch is checked out there —
it does NOT check the PR out for you. So confirm the PR's own branch is the one checked out:

```bash
gh pr view "$PR_NUMBER" --repo "$OWNER_REPO" --json headRefName -q .headRefName
git -C "$TARGET_CWD" branch --show-current
```

- The two match → `PR_CHECKED_OUT=true`. Continue to Step 2.5.
- They differ (or the branch cannot be read) → `PR_CHECKED_OUT=false`. **Do NOT launch.** A loop
  pointed at a checkout that is not on the PR's branch would commit its review fixes to whatever
  branch IS checked out there — often the base branch. STOP and tell the user:
  `PR #<n> is not checked out here (<TARGET_CWD> is on <branch>). Check its branch out in a
  dedicated worktree, then re-run /review-pr.`

### 2.5 Pre-launch rebase gate

Step 2 has established `PR_CHECKED_OUT == true` (it stops otherwise), so the PR's branch is the
one checked out in `TARGET_CWD`. Confirm it is based on the latest base before the loop starts.
Resolve the base from the PR itself, then ask the engine — this verb is strictly read-only on
every tier and never mutates your tree:

```bash
BASE_BRANCH=$(gh pr view "$PR_NUMBER" --repo "$OWNER_REPO" --json baseRefName -q .baseRefName)
PYTHONPATH="${CLAUDE_PLUGIN_DATA}/site:$PYTHONPATH" python3 -m buddhi_review rebase-check --cwd "$TARGET_CWD" --base "$BASE_BRANCH" --repo "$OWNER_REPO"
```

Parse the JSON object on stdout and act on `status`:

- **`up-to-date`** — launch (Step 3).
- **`clean`**, **`conflicts`**, or **`dirty`** with `behind > 0` — hand the action to the
  engine. This verb is tier-aware: an engine that carries the rebase capability performs the
  rebase (updating the PR branch with a lease-protected push, since it is already pushed); an
  engine without it prints the manual steps and declines to touch your tree.

  ```bash
  PYTHONPATH="${CLAUDE_PLUGIN_DATA}/site:$PYTHONPATH" python3 -m buddhi_review rebase --cwd "$TARGET_CWD" --base "$BASE_BRANCH" --repo "$OWNER_REPO"
  ```

  Read the JSON result: `status` is `rebased` (with `pushed == true` when the branch is already
  on the remote), `up-to-date`, or `current` → the engine handled it; launch (Step 3). Anything
  else — the engine did NOT rebase (it printed the manual steps instead, or the rebase could not complete)
  → ask with **AskUserQuestion**, the same three options as `/open-pr` Step 2:

  1. **Rebase — I resolve the conflicts** *(recommended)*: run
     `BUDDHI_ALLOW_MANUAL_GIT=1 git -C "$TARGET_CWD" rebase <base_resolved>` (the override
     prefix must be the very START of the command — the git guardrail hook blocks a bare
     agent-run `rebase`, and honors the prefix only there — and `<base_resolved>` is the
     `base_resolved` field from the `rebase-check`/`rebase` JSON, e.g. `upstream/main` in a
     fork checkout, NOT a hard-coded `origin/$BASE_BRANCH`, which can point at the fork's own
     stale copy of the base), resolve each conflicted file,
     `git -C "$TARGET_CWD" add` them, then
     `BUDDHI_ALLOW_MANUAL_GIT=1 git -C "$TARGET_CWD" rebase --continue` until done. The PR
     branch is already on the remote, so its upload needs a force-push, which the agent never
     runs — print `git -C "$TARGET_CWD" push --force-with-lease origin HEAD` for the operator,
     wait for their confirmation, then launch.
  2. **Skip rebase — launch as-is**: the loop starts on the branch as it stands. The engine
     resolves base drift mid-review as far as its capabilities allow; where it cannot, the
     conflicts surface on the PR.
  3. (Other / free-text) a different approach, or "I'll rebase manually" — accept manual only
     when truly impossible for you; if so print the exact commands and **EXIT**.
- **`dirty`** with `behind == 0` — nothing to rebase onto, but the worktree has uncommitted or
  untracked changes. Do NOT launch: once the loop applies its first fix, `commit_and_push` stages
  with `git add -A`, which would sweep this pre-existing WIP into the PR. Print the `detail` field
  (names the dirty state) and ask the user to commit, stash, or clean the tree in `$TARGET_CWD`,
  then STOP — re-run `/review-pr` once the tree is clean.
- **`error`** — the check itself failed, so the rebase state is unknown (never read that as
  healthy). Log the `detail` and STOP.

### 3. Launch the review loop (mandatory)

Run this EXACT command — substitute the angle-bracket placeholders, AND replace `$OWNER_REPO` /
`$TARGET_CWD` with the literal values you resolved in Steps 1–2 (shell variables do not survive
between Bash calls; an empty `--repo` / `--cwd` would silently fall back to the tool's own cwd):

```bash
PYTHONPATH="${CLAUDE_PLUGIN_DATA}/site:$PYTHONPATH" python3 -m buddhi_review review-pr <PR_NUMBER> --repo <OWNER_REPO> --cwd "<TARGET_CWD>" [--rr or --rr-active or --rr-none if the user passed it]
```

This is the front door: it selects the review engine, **detaches the process and returns
immediately** (so the long-running work survives the Bash tool's timeout), and prints where to
watch the run. You do not need to resolve any script path yourself.

**Before you stop, print EXACTLY this brief block — it is the ONE allowed output (it replaces
any longer summary), and it MUST go in your CHAT REPLY, not be left inside the tool output
(Claude Code collapses tool output under Ctrl+O but never your message, so a link left in the
tool output is effectively hidden from a first-time user).** The PR link is
`https://github.com/<OWNER_REPO>/pull/<PR_NUMBER>` — both values are already resolved:

> ✅ PR #<n> · review loop running
>    PR: <pr_url>

**Then relay every `NOTICE:` line the command printed** — verbatim, one per line, in the order
printed, with the `NOTICE: ` prefix stripped — in that same chat reply. Check BOTH stdout and
stderr for them. Those lines are how the engine tells the user where to watch the run, and
anything else it needs them to know; the skill never authors them, never rewords them, and never
invents one. If the engine printed no `NOTICE:` line, add nothing. Keep the whole reply to these
few lines — clickable links, no prose.

Then **your job is done.** Do NOT:

- run the review module in the foreground (the Bash tool's timeout would kill it),
- tail the log file,
- poll for progress,
- add anything beyond that brief block + the relayed `NOTICE:` lines (no extra summary, no next
  steps).

## `/review-pr setup`

`/review-pr setup` runs the onboarding wizard. It is an interactive raw-mode TTY program
(arrow-key selectors, hidden secret prompts) this agent session cannot drive, so open it in a
fresh terminal window via the bundled launcher:

```bash
SETUP=$(PYTHONPATH="${CLAUDE_PLUGIN_DATA}/site:$PYTHONPATH" python3 -c "import buddhi_review,os;print(os.path.join(os.path.dirname(buddhi_review.__file__),'launch-setup.sh'))")
PYTHONPATH="${CLAUDE_PLUGIN_DATA}/site:$PYTHONPATH" bash "$SETUP"
```

`launch-setup.sh` ships inside the `buddhi_review` package; the one-liner resolves its installed
path. It opens `python3 -m buddhi_review setup` in a new window (and on a headless host prints
the one-liner to run by hand). The wizard walks you through the tooling doctor, your Claude plan,
the repo binding, and your reviewer fleet, and confirms the console answer-file as the
notification channel. Each reviewer you enable still needs its vendor GitHub app + plan
configured in advance. See [`references/reviewer-setup.md`](references/reviewer-setup.md).

## References

- [`references/configuration.md`](references/configuration.md) — config keys and the plan → model
  mapping.
- [`references/reviewer-setup.md`](references/reviewer-setup.md) — what each reviewer requires
  (vendor app, plan, trigger, `auto_on_open`).
- [`references/claude-code-review.yml`](references/claude-code-review.yml) — the GitHub Actions
  workflow that makes `claude[bot]` review PRs (install it on the repo's default branch + set
  `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`).

## Scope

Single repo only. To review multiple repos, call `/review-pr` separately for each.
