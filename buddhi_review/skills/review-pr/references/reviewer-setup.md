# Reviewer setup — what each reviewer requires

The loop drives up to four review bots. **None is automatic for a new user.** Two
separate facts must hold for each reviewer, and neither is inferable from the
other:

1. **Prerequisite config (per bot).** A reviewer can only be *summoned at all* if
   its vendor was set up in advance on the repo: the GitHub app installed, the
   right plan active, and its @mention or similar trigger for requesting review
   working. Without this, the loop's request goes into a void. This is true for
   **every** reviewer, whether or not it auto-comments on PR open.
2. **Auto-on-open (per bot).** Whether a *configured* reviewer actually posts a
   review the moment a PR opens is a **separate** fact the loop cannot deduce from
   "is it installed." You tell the loop this per bot via `auto_on_open` (Step 5 of
   `/review-pr setup`). The loop then summons only the bots that need it in round 1
   — see ["Round-1 summoning"](#round-1-summoning--auto_on_open) below.

Enable only the reviewers you have — the rest are subtracted everywhere (no
trigger comments fired into a void, no dead waits). `/review-pr setup` (Step 5)
validates each one's reachability and persists the working set as
`active_reviewers` (+ the per-bot `auto_on_open` map) in
`~/.config/review-loop/config.yaml`.

| Reviewer | Trigger | Requires | How the wizard validates |
|---|---|---|---|
| **Copilot** | `requested_reviewers` API with the `copilot-pull-request-reviewer[bot]` slug (no comment trigger) | `gh` CLI **≥ 2.87** + a **Copilot Pro/Pro+/Enterprise** plan, GitHub Copilot **code review** enabled for the repo/org | `gh` version check in Step 1 + `gh auth status` in Step 1 |
| **Gemini** | `/gemini review` (PR comment) | The **Gemini Code Assist** GitHub app installed on the repo/org | trusted from your selection (trigger is comment-driven; no reliable API probe for non-app-authenticated callers) |
| **Codex** | `@codex review` (PR comment) | The **OpenAI Codex** GitHub app (code review) + a **ChatGPT** plan (Plus/Pro — model availability differs by tier) | trusted from your selection (trigger is comment-driven via GitHub App; the local `codex` CLI is a separate product) |
| **Claude** | `@claude review` (PR comment) | `.github/workflows/claude-code-review.yml` on the repo's **default branch** + a `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` repo secret | check for the workflow on the default branch; offer to write the bundled template + walk through `gh secret set` |

## Per-reviewer setup how-to

### Copilot
- **App / plan:** GitHub Copilot is a GitHub-native feature, not a separate
  marketplace app. You need a paid **Copilot** plan (Pro, Pro+, Business, or
  Enterprise) on the account or org that owns the repo, and **Copilot code review**
  must be enabled — Settings → Copilot (for an org) or your personal Copilot
  settings. On Business/Enterprise an admin may need to enable code review in the
  policy settings.
- **Trigger:** the loop requests Copilot each round via the GitHub
  `requested_reviewers` API using the `copilot-pull-request-reviewer[bot]` slug
  (no comment; no remove-then-re-add cycle). Needs `gh` **≥ 2.87**. Without a
  paid plan or with code review disabled, GitHub silently ignores the request.
- **Review on PR opened:** GitHub Copilot can be set to **automatically review new
  PRs** via a repository/org ruleset ("Request Copilot review" / automatic code
  review rule). If that rule is on, set `auto_on_open: true`; if you only get a
  Copilot review when one is requested, set `auto_on_open: false` so the loop
  requests it in round 1.

### Gemini
- **App / plan:** install the **Gemini Code Assist** GitHub app on the repository
  (or the org) from the GitHub Marketplace. The individual tier is free; larger
  orgs may use a Code Assist Standard/Enterprise subscription.
- **Trigger:** the loop re-triggers it each round with a `/gemini review` PR
  comment. If the app isn't installed on the repo, the comment does
  nothing.
- **Review on PR opened:** by default the Gemini Code Assist app **reviews a PR
  automatically when it is opened** (and posts a summary). If you've left that
  default on, set `auto_on_open: true`. If your `.gemini/config.yaml` disables
  automatic review (`code_review.disable: true` or pull-request automation off),
  set `auto_on_open: false` so the loop summons it in round 1.

### Codex
- **App / plan:** install the **OpenAI Codex** GitHub app (code review) and have a
  ChatGPT plan (Plus or Pro). Available model versions depend on your plan tier.
  The wizard trusts your selection — the trigger is the GitHub App, not the local
  `codex` CLI (a separate product).
- **Trigger:** the loop posts `@codex review` as a PR comment each round. Codex
  auto-triggers on the **first** commit of a PR but does **not** reliably re-review
  later commits, so the loop nudges it explicitly every round.
- **Review on PR opened:** Codex can be configured to **review PRs automatically**
  (the app's "Code review" / automatic review setting). If automatic review is on
  for this repo, set `auto_on_open: true`; otherwise set `auto_on_open: false`.

### Claude
Claude review is **workflow-driven, not app-driven**, and it is **mention-driven
only** — there is no automatic review on PR open:

1. The repo's **default branch** must contain
   `.github/workflows/claude-code-review.yml` (the template is bundled in this
   `references/` folder). Because `issue_comment` workflows only run from the
   default branch, a PR that merely *adds* the workflow can't invoke Claude until
   that PR lands.
2. The repo needs a Claude credential as a secret — either a `claude setup-token`
   subscription token in `CLAUDE_CODE_OAUTH_TOKEN`, or a pay-as-you-go key in
   `ANTHROPIC_API_KEY`. Either works — use whichever you have. Add it with
   `gh secret set CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`). Note: the
   setup wizard only configures `CLAUDE_CODE_OAUTH_TOKEN`; if you are using
   `ANTHROPIC_API_KEY`, set it with `gh secret set ANTHROPIC_API_KEY` and skip
   the wizard's token step.

The shipped workflow has **no** `pull_request`/`synchronize` trigger (so the loop
owns the cadence), which means Claude never posts on PR open. Leave
`auto_on_open: false` for Claude (the default) so the loop summons it with
`@claude review` in round 1. The workflow's prompt emits a literal `No issues
found.` sentinel on a clean review — this is load-bearing (it matches the loop's
clean-review detector). Ship the template verbatim; do not change the sentinel
line (see [`configuration.md`](configuration.md)).

## Round-1 summoning & `auto_on_open`

Every enabled reviewer is **polled** for comments each round. What differs is
round-1 **summoning** (posting the trigger):

- A reviewer with **`auto_on_open: true`** posts its review when the PR opens, so
  the loop does **not** trigger it in round 1 (avoiding a duplicate review).
- A reviewer with **`auto_on_open: false`** does not post on open, so the loop
  **summons it in round 1** so its review still arrives.

In **every later round** the loop re-triggers all enabled reviewers, regardless of
`auto_on_open`.

**Org policy caveat.** Some orgs forbid changing the default "review on PR opened"
for a repo (e.g. a ruleset is locked, or automatic review is disabled org-wide and
you cannot enable it). In that case set `auto_on_open` to match reality:
- If the repo **cannot** auto-review on open, set `auto_on_open: false` so the
  loop summons that bot in round 1 instead of waiting for an auto-review that will
  never come.
- If the repo **always** auto-reviews on open and you cannot turn it off, set
  `auto_on_open: true` so the loop doesn't post a duplicate trigger.

## When a reviewer posts nothing

An **enabled** reviewer that produces no comments stays in the round table marked
`No review posted 🔇` — it is still expected, and the loop keeps polling for it
each round. `Active ✅` only appears once the reviewer has engaged in the current
round (reviewers disabled in config keep a `Not requested 🙅` row for
completeness — never summoned or polled; reviewers
excluded mid-run show `Approved 👍`/`Reviewed — no findings ✓`/`Reviewed — no change ✓`/`Quota exhausted ⚠️`/`PR too large 📦`/`Could not review ❌`/`Polish-only 🧹`; `Could not review ❌` is
per-round — it marks the round whose review attempt errored, and a later round where that
reviewer is no longer re-requested shows `Not requested 🙅`). If a reviewer
*never* posts across the whole run, that is the
signal your prerequisite setup is incomplete — check, for that bot:

- the vendor GitHub app is actually installed on **this** repo (not just your
  account), and the plan covers code review;
- the @-mention trigger is the right one (the table above);
- for an `auto_on_open: true` bot, that "review on PR opened" is genuinely enabled
  here — if it isn't, flip the bot to `auto_on_open: false` and re-run setup.
