---
name: open-pr
description: >
  Create a PR from your local changes, then run the automated review loop on it.
  Branches, commits, pushes, opens the PR with `gh pr create`, and launches the
  reviewer-fan-out + classify + fix loop. The loop notifies you when the review is
  clean; merging stays yours. Run `/review-pr setup` first.
when_to_use: >
  When the user asks to create a PR from local work, open and review a PR in one
  step, or ship the current branch through automated review.
argument-hint: "[owner/repo] [--max-rounds N]"
arguments:
  - $repo
allowed-tools:
  - Bash
  - Read
  # Edit/Write: Step 2 option 1 ("I resolve the conflicts") has the agent edit
  # conflicted files mid-rebase before `git add` + `rebase --continue`.
  - Edit
  - Write
  - AskUserQuestion
hooks:
  PreToolUse:
    - matcher: Bash
      hooks:
        - type: command
          command: python3 -m buddhi_review.git_guardrail_hook
---

# /open-pr — create a PR, then review it

**Buddhi lands your PRs.** This skill gets a PR airborne — branch, commit, push,
open — then flies the automated review rounds; once review is clean it is ready
to land (merge) on the base branch.

## Critical behaviour rules

- **Only sanctioned interactive gates, otherwise silent.** The ONLY questions you
  may ask are the **first-run onboarding** prompt (Step 0, only when this machine has
  no config), the **per-repo reviewer confirmation** prompt (Step 1.1, only when this
  repo's reviewers are unconfirmed), and the **pre-launch rebase** prompt (Step 2,
  only when the engine reports a non-clean rebase status). Everything else runs
  silently.
- **NEVER pause for confirmation** between any OTHER steps. Run them back-to-back.
  Never ask the user what to do on error — log it and stop.
- **Reviewers trigger based on the fleet confirmed for THIS repo** (per-repo, because
  the vendor GitHub Apps + the claude workflow are installed per repo; set via the
  Step 1.1 terminal setup wizard or `/review-pr setup`, stored under `repos:` in
  `~/.config/review-loop/config.yaml`), falling back to your global default, then the
  built-in Copilot/Gemini/Codex/Claude set. If a reviewer is not responding, confirm
  reviewers for this repo (Step 1.1) or run `/review-pr setup`. Do not assume any
  specific reviewer is auto-triggered.
- **NEVER merge manually.** Never run `gh pr merge` yourself. Whether the loop squash-merges on
  a clean exit is the engine's call — this skill passes no merge flag, so the loop runs on its
  own default, which is NOT to merge: it notifies you on a clean exit and you merge via GitHub.
  On a non-clean exit it notifies you as well. Do not assume a merge happened.
- **NEVER skip the review loop.** It MUST run. It has a 7-minute minimum wait built
  in. Do not short-circuit it.
- **The actuator does the git mechanics.** `python3 -m buddhi_review open-pr` detects
  the git state, commits/branches/pushes as needed, opens the PR, and launches the
  review loop. You author the title/body and pick the branch; you do NOT run the
  branch/commit/push git commands yourself.
- If any step fails, log the error and stop. Do not ask the user what to do.

## Arguments

Repo name as first argument: `/open-pr owner/repo`. It is optional — when omitted the
repo is inferred from the current directory's git remote; pass it only when the cwd is
not inside the target repo.

Optional: `--max-rounds <N>` overrides the review loop's default cap of 10 rounds.
Example: `/open-pr owner/repo --max-rounds 20`. When omitted, the loop uses its own
default (the `BUDDHI_MAX_ROUNDS` environment variable, else a budget sized from the PR
diff, else 10). Forward it verbatim to the actuator in Step 3 — do NOT substitute,
validate, or prompt the user about it.

## What this does

Creates a branch, commits all changes, pushes, opens a PR, then runs the automated
reviewer-fan-out + classify + fix loop. Copilot, Gemini, and Codex auto-trigger on PR
creation (GitHub-level config) — do NOT request them as reviewers. Handles new repos,
divergent histories, and the already-on-a-feature-branch case. Clearance requests
(business questions — decisions the loop needs from you) are answered from your
terminal.

## Execution steps

### 0. First-run onboarding gate

Before anything else, check whether the user has ever completed setup — a machine with
no config runs the loop with defaults and emits config-unset warnings instead of asking
the user to onboard:

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
       SETUP=$(python3 -c "import buddhi_review,os;print(os.path.join(os.path.dirname(buddhi_review.__file__),'launch-setup.sh'))")
       bash "$SETUP"
       ```

       On success, reply exactly: ``Setup opened in a new window — finish it there, then
       re-run /open-pr.`` and **EXIT**. Only on a headless host with no window server
       does the launcher instead print a ready-to-run command itself — relay that exact
       line and **EXIT**.
    2. **Proceed once with defaults** — continue to Step 1. The loop runs with defaults;
       fleet warnings may appear in the log this run.

This gate is interactive-only and best-effort; if you cannot prompt, proceed silently
with defaults. It must NEVER block the loop.

### 1. Resolve repo

Resolve `OWNER/REPO` and `CWD` in this order:

1. **If the user passed an explicit `owner/repo` (or repo-name) argument, honor it
   first**: set `OWNER_REPO` to that literal argument value and `CWD` to the cwd or a
   given path. Running `/open-pr owner/target` from an unrelated checkout must target
   `owner/target`, never silently fall back to whatever repo the cwd happens to sit in —
   do NOT run the `gh repo view` fallback below when an explicit argument was given.
2. **Else, infer from the current directory's git remote first.** If the cwd is inside a
   git repo, derive `OWNER/REPO` from its `origin` remote and set `CWD` to its toplevel
   (the same resolution the actuator does):

   ```bash
   CWD=$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")
   OWNER_REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)
   ```

**Auto-target the worktree this session worked in.** When the work was done in a NEW
worktree off `main` (the standing rule), the session's `$PWD` can still point at the
spawn checkout while the real work sits in a `git -C <worktree>` elsewhere. Consult the
resolver — it returns the session's recorded worktree only when that worktree is a live
checkout of the target repo and differs from `$PWD`, else it echoes `$CWD` unchanged:

```bash
# $CLAUDE_CODE_SESSION_ID is a real Claude Code env var (a plain UUID, no prefix),
# exported into every Bash tool call. The git-guardrail PreToolUse hook receives the
# same value as the `session_id` field in its stdin JSON payload, so the key it
# registers and the key looked up here are byte-identical.
RESOLVED=$(python3 -m buddhi_review.worktree_target resolve \
  --session-id "$CLAUDE_CODE_SESSION_ID" --repo "$OWNER_REPO" --cwd "$CWD" 2>/dev/null)
if [ -n "$RESOLVED" ] && [ "$RESOLVED" != "$CWD" ]; then
  TARGET_CWD="$RESOLVED"
  echo "Auto-selected this session's worktree: $TARGET_CWD"
else
  TARGET_CWD="$CWD"
fi
```

This is silent (no ask) — it only prefers the session's own worktree over a stale
`$PWD`. Use `TARGET_CWD` as the working directory for **every** subsequent step.

> **Carry the resolved values forward yourself.** Each Bash call runs in its OWN shell, so
> `TARGET_CWD` / `OWNER_REPO` / `BASE_BRANCH` do NOT survive from one step's code block to
> the next. Read each value out of the command's output and substitute it literally into the
> later commands (or re-derive it in the same block that uses it). Never run a later step
> with an empty variable.

**Author the PR title + body** from the work on the branch, and pick a branch prefix
(`feat` / `fix` / `refactor`) — used only when the work sits on the base branch and a
new branch must be created.

**PR title — plan-ID prefix.** If the **branch name encodes a plan id** (e.g.
`feat/pro-9-…` → `PRO-9`), the title MUST carry that id right after the
conventional-commit prefix: `<type>(<scope>): <PLAN-ID> — <summary>`. A branch with no
plan id uses a normal conventional-commit title with no id.

**PR title — conventional-commit type.** The type starting the title must be one of:
`feat`, `fix`, `perf`, `docs`, `chore`, `ci`, `test`, `refactor`, `style`, `build` —
pick the one that matches what the change actually is. Use `feat!:` (or a
`BREAKING CHANGE:` footer) only for a change that deliberately breaks the public API.

### 1.1 Per-repo reviewer confirmation gate

Reviewer availability is **per-repo** — Copilot/Gemini/Codex are vendor GitHub Apps
installed per repo, and `claude[bot]` needs `claude-code-review.yml` committed in each
repo — so a fleet confirmed for one repo must NOT be assumed for another. Ask the status
reader whether THIS repo's reviewers have been confirmed:

```bash
python3 -m buddhi_review status --repo "$OWNER_REPO" 2>/dev/null
```

If `OWNER/REPO` cannot be resolved yet (a brand-new repo with no remote), or the command
is absent / prints nothing / unparseable output, **skip this gate** and proceed to Step 2
— it is best-effort and must NEVER block the flow. Otherwise parse the single JSON object
(`{"repo_confirmed": …, "has_global_default": …}`) and act on `repo_confirmed`:

- **`true`** — proceed silently to Step 2.
- **`false`** — ask with **AskUserQuestion** (a sanctioned gate; ask ONCE). **Do NOT
  configure reviewers in this session — every piece of deterministic setup (reviewer
  selection, auto-on-open, auto-merge, label-gated CI, GitHub-side provisioning) runs in
  the terminal wizard, never here:**
  - Question: *"Reviewers for `<OWNER/REPO>` haven't been confirmed (they're installed
    per-repo). Configure this repo now?"*
  - Options:
    1. **Run setup now** *(recommended)* — open the per-repo setup wizard in a fresh
       terminal window (a raw-mode TTY this session cannot drive), then **EXIT** so the
       user can finish it:

       ```bash
       SETUP=$(python3 -c "import buddhi_review,os;print(os.path.join(os.path.dirname(buddhi_review.__file__),'launch-setup.sh'))")
       bash "$SETUP" --repo "$OWNER_REPO"
       ```

       On a headless host the launcher prints the one-liner to run by hand instead. After
       it returns, reply exactly: ``Setup opened in a new window — finish it there, then
       re-run /open-pr.`` and **EXIT**.
    2. **Use global defaults** *(offer only when `has_global_default` is `true`)* —
       continue to Step 2 without writing a per-repo entry; the loop runs with your global
       default fleet. When `has_global_default` is `false`, omit this option entirely —
       there is no fallback fleet and the loop will refuse to launch; option 1 is the only
       path.

This gate is interactive-only and **never configures reviewers itself** — it only offers
to launch the terminal wizard (the single deterministic setup brain) or falls back to
global defaults. If you cannot prompt, proceed to Step 2 with defaults.

### 2. Pre-launch rebase gate

A feature branch that is behind its base must never be launched un-rebased. Resolve the
base and the current branch first:

```bash
BASE_BRANCH=$(git -C "$TARGET_CWD" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
[ -z "$BASE_BRANCH" ] && BASE_BRANCH=$(git -C "$TARGET_CWD" branch --list main master 2>/dev/null | head -1 | tr -d ' *')
BASE_BRANCH=${BASE_BRANCH:-main}
CURRENT_BRANCH=$(git -C "$TARGET_CWD" branch --show-current)
```

**This gate runs only on a feature branch** (`CURRENT_BRANCH` is non-empty and differs
from `BASE_BRANCH`). When the work is still sitting on the base branch, **skip this gate
entirely** and go to Step 3: the actuator fetches `origin` and cuts the new branch from
the freshly-fetched base itself, so there is nothing to rebase and nothing to ask.

On a feature branch, ask the engine for the rebase status — this verb is strictly
read-only on every tier and never mutates your tree:

```bash
python3 -m buddhi_review rebase-check --cwd "$TARGET_CWD" --base "$BASE_BRANCH" --repo "$OWNER_REPO"
```

Parse the JSON object on stdout and act on `status`:

- **`up-to-date`** — the branch sits on the latest base, but this check compares against
  BASE only: a branch whose rebase succeeded locally while its push failed in an EARLIER
  run also reads `up-to-date` here, with the remote still holding the pre-rebase head.
  Run the **stranded-branch check** before proceeding:

  ```bash
  BR=$(git -C "$TARGET_CWD" rev-parse --abbrev-ref HEAD)
  git -C "$TARGET_CWD" fetch origin "$BR" 2>/dev/null || true
  if git -C "$TARGET_CWD" rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
    AHEAD=$(git -C "$TARGET_CWD" rev-list --count '@{u}..HEAD') || AHEAD=x
    BEHIND=$(git -C "$TARGET_CWD" rev-list --count 'HEAD..@{u}') || BEHIND=x
    case "$AHEAD,$BEHIND" in
      *x*|,*|*,) echo "CHECK-FAILED: could not count local-vs-remote commits" ;;
      *) if [ "$AHEAD" -gt 0 ] && [ "$BEHIND" -gt 0 ]; then
           echo "STRANDED: local and remote have diverged (a rebased branch was never force-pushed)"
         fi ;;
    esac
  fi
  ```

  No output (or no upstream yet) → proceed to Step 3. `CHECK-FAILED` → STOP and report it
  (a transient git failure must never read as healthy). `STRANDED` → do NOT proceed and do
  NOT push yourself: log the state and STOP, printing the exact recovery command for the
  operator to run (the operator runs hard pushes):
  `git -C "$TARGET_CWD" push --force-with-lease origin HEAD` — then re-run /open-pr after
  it lands.
- **`dirty`** with `behind == 0` — uncommitted work, nothing to rebase onto. Proceed to
  Step 3; the actuator commits it.
- **`clean`**, **`conflicts`**, or **`dirty`** with `behind > 0` — hand the action to the
  engine. This verb is tier-aware: an engine that carries the rebase capability performs
  the rebase (updating the remote branch with a lease-protected push); an engine without it
  prints the manual steps and declines to touch your tree.

  ```bash
  python3 -m buddhi_review rebase --cwd "$TARGET_CWD" --base "$BASE_BRANCH" --repo "$OWNER_REPO"
  ```

  Read the JSON result:
  - `status` is `rebased` (with `pushed == true` when the branch is already on the remote),
    `up-to-date`, or `current` → the engine handled it. Proceed to Step 3.
  - anything else — the engine did NOT rebase (it printed the manual steps instead, or the
    rebase could not complete) → ask with **AskUserQuestion** (a sanctioned gate). Offer
    "rebase manually yourself" ONLY if resolution is genuinely beyond you:
    - Question: *"`<branch>` is `<behind>` commit(s) behind `<base>` and needs a rebase
      before review. How should I handle it?"* — when the status was `conflicts`, append the
      predicted files: *" — conflicts in `<conflict_files>`"*. Omit that clause for `clean` /
      `dirty`, where `conflict_files` is empty.
    - Options:
      1. **Rebase — I resolve the conflicts** *(recommended)*: commit any pending work, run
         `BUDDHI_ALLOW_MANUAL_GIT=1 git -C "$TARGET_CWD" rebase <base_resolved>` (the
         override prefix must be the very START of the command — the git guardrail hook
         blocks a bare agent-run `rebase`, and honors the prefix only there — and
         `<base_resolved>` is the `base_resolved` field from the `rebase-check`/`rebase`
         JSON, e.g. `upstream/main` in a fork checkout, NOT a hard-coded
         `origin/$BASE_BRANCH`, which can point at the fork's own stale copy of the base),
         edit each conflicted file to a correct merged state, `git -C "$TARGET_CWD" add` them, then
         `BUDDHI_ALLOW_MANUAL_GIT=1 git -C "$TARGET_CWD" rebase --continue` until done.
         Upload: a branch NOT yet on the remote takes a plain
         `git -C "$TARGET_CWD" push -u origin HEAD`; a branch ALREADY on the remote needs a
         force-push, which the agent never runs — print
         `git -C "$TARGET_CWD" push --force-with-lease origin HEAD` for the operator, wait
         for their confirmation, then proceed.
      2. **Skip rebase — launch as-is**: the loop starts on the branch as it stands. The
         engine resolves base drift mid-review as far as its capabilities allow; where it
         cannot, the conflicts surface on the PR. Proceed to Step 3 without rebasing.
      3. (Other / free-text) a different approach, or "I'll rebase manually" — accept manual
         only when truly impossible for you; if so print the exact commands and **EXIT**.
- **`error`** — the check itself failed, so the rebase state is unknown (never read that as
  healthy). Log the `detail` and STOP.

If you cannot prompt, proceed to Step 3 — the actuator emits a non-blocking behind-notice
and leaves the rebase to the operator.

### 3. Create the PR + launch the review loop

Hand off to the actuator — it detects the git state (feature branch / clean / uncommitted /
on-base), commits/branches/pushes as needed, ensures remote infra, opens the PR (with an
idempotent fallback to the existing PR when the branch already has one), and launches the
review loop detached.

Run this EXACT command — substitute every angle-bracket placeholder with its literal resolved
value (`<OWNER_REPO>` and `<TARGET_CWD>` come from Step 1; shell variables do NOT survive
between Bash calls, and an empty `--repo` / `--cwd` would silently fall back to gh-inference
and the tool's own cwd, discarding the worktree you auto-targeted). Append `--max-rounds <N>`
ONLY if the user passed it as a slash-command argument — omit the flag entirely otherwise so
the loop uses its own default:

```bash
python3 -m buddhi_review open-pr \
  --title "<title>" --body "<body>" \
  --repo <OWNER_REPO> --cwd "<TARGET_CWD>" \
  [--branch-prefix <feat|fix|refactor>] [--branch "<explicit-branch-name>"] \
  [--max-rounds <N>]
```

`--branch-prefix` takes exactly ONE word — pick `feat`, `fix`, or `refactor`; never pass the
`a|b|c` alternation literally.

The actuator prints the **PR URL on the last line of stdout** (`^https?://`-grepable). Most
status and decoration go to stderr, but a non-blocking behind-notice (`⚙ [auto] rebase gate …`)
can also land on stdout — so take the URL as the LAST `^https?://` stdout line, not the only
stdout line. It launches the review loop detached and returns immediately.

**Nothing to do.** If the actuator reports `No changes to commit in <repo>. Nothing to
do.` (on stderr), there was no work to ship — relay that line and **EXIT**. No PR is created.

**After the command returns, print EXACTLY this brief block — it is the ONE allowed output
(it replaces any longer summary), and it MUST go in your CHAT REPLY, not be left inside the
tool output (Claude Code collapses tool output under Ctrl+O but never your message, so a
link left in the tool output is effectively hidden from a first-time user):**

> ✅ PR #<n> · review loop running
>    PR: <pr_url>

**Then relay every `NOTICE:` line the command printed** — verbatim, one per line, in the
order printed, with the `NOTICE: ` prefix stripped — in that same chat reply. Check BOTH
stdout and stderr for them (on this command the launcher's output is folded into stderr).
Those lines are how the engine tells the user where to watch the run, and anything else it
needs them to know; the skill never authors them, never rewords them, and never invents one.
If the engine printed no `NOTICE:` line, add nothing. Keep the whole reply to these few
lines — clickable links, no prose.

**Do NOT request Copilot, Gemini, or Codex as reviewers.** All three auto-trigger on PR
creation (GitHub-level config); manually adding them is redundant.

Once you have printed that block, **your job is done.** Do NOT:

- run the review module in the foreground (the Bash tool's timeout would kill it),
- tail the log file,
- poll for progress,
- add anything beyond that brief block + the relayed `NOTICE:` lines (no extra summary, no
  next steps).

## Scope

Single repo only. This skill opens the PR from the checkout resolved in Step 1 — switch to
the worktree you want to ship from before running it.
