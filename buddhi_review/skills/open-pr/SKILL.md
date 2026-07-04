---
name: open-pr
description: >
  Create a PR from your local changes, then run the automated review loop on it.
  Branches, commits, pushes, opens the PR with `gh pr create`, and launches the
  reviewer-fan-out + classify + fix loop. Once the loop exits clean, merge the PR
  manually via GitHub. Run `/review-pr setup` first.
when_to_use: >
  When the user asks to create a PR from local work, open and review a PR in one
  step, or ship the current branch through automated review.
argument-hint: "[owner/repo]"
arguments:
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

# /open-pr — create a PR, then review it

**Buddhi lands your PRs.** This skill gets a PR airborne — branch, commit, push,
open — then flies the automated review rounds; once review is clean it is ready
to land (merge) on the base branch.

Creates a branch, commits all changes, pushes, opens a PR, then runs the automated
reviewer-fan-out + classify + fix loop on it. Handles new repos, divergent
histories, and the already-on-a-feature-branch case. Clearance requests
(business questions — decisions the loop needs from you) are answered from your
terminal.

## Critical behaviour rules

- **Only sanctioned interactive gates, otherwise silent.** The ONLY questions you
  may ask are the **first-run setup** prompt (Step 0), the **per-repo reviewer
  confirmation** prompt (Step 1.1, only when this repo's reviewers are
  unconfirmed), and the **rebase gate** (Step 2, only when the branch is behind
  base). Run everything else back-to-back.
- **The actuator does the git mechanics.** `python3 -m buddhi_review open-pr`
  detects the git state, commits/branches/pushes as needed, opens the PR, and
  launches the review loop. You author the title/body and pick the branch; you do
  NOT run the branch/commit/push git commands yourself.
- **Never merge automatically.** Never run `gh pr merge` yourself. Merge manually
  via GitHub once the review loop exits clean.
- **Never skip the review loop.** It MUST run; it has a 7-minute minimum wait
  built in. Do not short-circuit it.
- If any step fails, log the error and stop. Do not ask the user what to do.

## Arguments

- `owner/repo` (optional): only needed when the cwd is not inside the target repo.

## Execution steps

### 0. First-run setup gate

```bash
test -s ~/.config/review-loop/config.yaml && echo configured || echo unconfigured
```

- **`configured`** — proceed silently to Step 1.
- **`unconfigured`** — ask with **AskUserQuestion** (sanctioned; ask ONCE):
  - Question: *"No review-loop config found. Set it up before launching?"*
  - Options:
    1. **Run setup now** *(recommended)* — open the interactive wizard in a fresh
       terminal window (a raw-mode TTY this session cannot drive), then **EXIT**:

       ```bash
       SETUP=$(python3 -c "import buddhi_review,os;print(os.path.join(os.path.dirname(buddhi_review.__file__),'launch-setup.sh'))")
       bash "$SETUP"
       ```
    2. **Proceed once with defaults** — continue to Step 1.

Interactive-only and best-effort; if you cannot prompt, proceed with defaults.

### 1. Resolve the repo and author the PR

1. **Infer the repo** (informational — the actuator re-resolves it the same way).
   If the cwd is inside a git repo, set `CWD` to its toplevel; otherwise accept an
   explicit `owner/repo` argument and pass it as `--repo`.

   ```bash
   CWD=$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")
   ```

2. **Author the PR title + body** from the work on the branch, and pick a branch
   prefix (`feat` / `fix` / `refactor`) — used only when the work is sitting on the
   base branch and a new branch must be created.

### 1.1 Per-repo reviewer confirmation gate

Reviewer availability is **per-repo** — Copilot/Gemini/Codex are vendor GitHub
Apps installed per repo, and `claude[bot]` needs `claude-code-review.yml` committed
in each repo — so a fleet confirmed for one repo must NOT be assumed for another.
Resolve `OWNER/REPO` (the explicit `owner/repo` argument when given, else the cwd's
remote) and ask the status reader whether THIS repo's reviewers have been
confirmed:

```bash
OWNER_REPO=${OWNER_REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)}
python3 -m buddhi_review status --repo "$OWNER_REPO" 2>/dev/null
```

If `OWNER/REPO` cannot be resolved yet (a brand-new repo with no remote), or the
command is absent / prints nothing / unparseable output, **skip this gate** and
proceed to Step 2 — it is best-effort and must NEVER block the flow. Otherwise
parse the single JSON object (`{"repo_confirmed": …, "has_global_default": …}`)
and act on `repo_confirmed`:

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
       finish it there, then re-run /open-pr.`` and **EXIT**.
    2. **Use global defaults** *(offer only when `has_global_default` is `true`)* —
       continue to Step 2 without writing a per-repo entry; the loop runs with
       your global default fleet. When `has_global_default` is `false`, omit this
       option entirely — there is no fallback fleet and the loop will refuse to
       launch; option 1 is the only path.

This gate is interactive-only and **never configures reviewers itself** — it only
offers to launch the terminal wizard (the single deterministic setup brain) or
falls back to global defaults. If you cannot prompt, proceed to Step 2 with
defaults.

### 2. Pre-launch rebase gate (interactive)

If the branch is behind its base, this skill offers a manual rebase gate — you
rebase by hand, it never rebases for you. Check whether the branch is behind base:

```bash
BASE_BRANCH=$(git -C "$CWD" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
[ -z "$BASE_BRANCH" ] && BASE_BRANCH=$(git -C "$CWD" branch --list main master 2>/dev/null | head -1 | tr -d ' *')
BASE_BRANCH=${BASE_BRANCH:-main}
git -C "$CWD" fetch origin "$BASE_BRANCH" 2>/dev/null || true
BEHIND=$(git -C "$CWD" rev-list --count HEAD..origin/$BASE_BRANCH 2>/dev/null || echo 0)
```

- **`BEHIND == 0`** — proceed to Step 3.
- **`BEHIND > 0`** — ask with **AskUserQuestion** (sanctioned gate):
  1. **Rebase manually now** *(recommended)*: commit any pending work, run
     `BUDDHI_ALLOW_MANUAL_GIT=1 git -C "$CWD" rebase origin/$BASE_BRANCH`, resolve
     any conflicts, then continue to Step 3. (The `BUDDHI_ALLOW_MANUAL_GIT=1`
     prefix is the one sanctioned, deliberate rebase; the guardrail hook blocks
     any other hand-run history rewrite during the flow.)
  2. **Proceed as-is**: continue to Step 3 without rebasing. The actuator notes the
     behind-ness; GitHub will show conflicts if the branch genuinely diverges.

If you cannot prompt, proceed to Step 3 (the actuator emits a non-blocking
behind-notice and leaves the rebase to you).

### 3. Create the PR + launch the review loop

Hand off to the actuator — it detects the git state (feature branch / clean /
uncommitted / on-base), commits/branches/pushes as needed, ensures remote infra,
opens the PR, and launches the review loop detached:

```bash
python3 -m buddhi_review open-pr \
  --title "<title>" --body "<body>" \
  [--repo "<owner/repo>"] [--cwd "$CWD"] \
  [--branch-prefix feat|fix|refactor] [--branch "<explicit-branch-name>"]
```

The actuator prints the **PR URL on the last line of stdout** (`^https?://`
-grepable); all status and decoration go to stderr. It launches the review loop
detached and returns immediately.

After the command returns, **your job is done.** Do NOT run the review module in
the foreground, tail the log, poll for progress, or print a summary.

## Scope

Single repo only. This skill opens the PR from the current checkout — switch
to the worktree you want to ship from before running it.
