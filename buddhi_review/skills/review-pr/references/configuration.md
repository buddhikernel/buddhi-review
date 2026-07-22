# Configuration & tuning

All configuration below lives in `~/.config/review-loop/config.yaml`, written by
`/review-pr setup`. Until that file exists the loop runs on defaults and prints a
short config-unset note to its log rather than asking you to onboard. The skills
run a first-run gate before launching: if the config is absent they ask once
whether to run setup now or proceed with defaults.

The config surface is small:

| key | type | default |
|---|---|---|
| `plan` | string (`max-20x`, `max-5x`, or `pro`) | `max-5x` |
| `active_reviewers` | list of `copilot`/`gemini`/`codex`/`claude` | all four |
| `auto_on_open` | map `{bot: bool}` | per-bot: `claude:false`, the three GitHub-App bots `true` |
| `repos` | map `{owner/repo: {…}}` of per-repo `active_reviewers` / `auto_on_open` overrides | unset (the top-level keys apply to every repo) |
| `notifications` | string channel (always `console`) | `console` |
| `repo` / `cwd` | string | unset (inferred at runtime from the cwd's git remote) |

## Plan profiles (model selection)

Model resolution is driven by the plan profile: the `Policy` object in
`buddhi_review/policy.py` supplies the role→model mapping at construction time,
so a plan-profile change requires only a one-line edit there — no engine code
changes. `Policy` ships with alias defaults (`"sonnet"` / `"haiku"` / `"opus"`).

Each plan maps **roles** to a concrete model + context window:

| Role | Default tier (max-5x) | Why |
|---|---|---|
| `classifier` | Sonnet / standard | Highest-frequency call; a bounded labelling task. Sonnet is reliable and far cheaper/faster than Opus here. |
| `fix-substantive` | Sonnet / standard | Real-flaw fixes; never below Sonnet. |
| `fix-cosmetic` | Haiku / standard | Trivial mechanical edits. |
| `clean-review-detector` | Haiku / standard | Binary CLEAN/NOT_CLEAN. |
| `quota-detector` | Haiku / standard | Binary QUOTA/NOT_QUOTA. |

`context: standard` is the 200K window. The `[1m]` 1M-context selector is reserved
as an **overflow escalation** — the loop escalates to it at runtime only when a
prompt is actually large enough (> ~160K tokens), which the input byte caps make
rare. No role defaults to `[1m]`.

Three plans ship: `max-20x` and `max-5x` both have Opus and resolve every role
identically; `pro` has no Opus, so it maps the higher-tier roles **down** to its
best available model **in the table** (not in code), and the loop degrades
gracefully with no failed round-trips.

> The model is resolved per role from your plan, and the same model is retried on
> a transient error.

## Reviewer fleet

`active_reviewers` in `~/.config/review-loop/config.yaml` sets the starting
universe of reviewers. Defaults to `[copilot, gemini, codex, claude]` when no
config exists. See [`reviewer-setup.md`](reviewer-setup.md).

### `auto_on_open` — does a reviewer post on PR open?

Each enabled reviewer carries one extra fact the loop **cannot deduce** from "is
the app installed": does this bot post a review **automatically when a PR is
opened**? It lives in an `auto_on_open:` map keyed by bot:

```yaml
active_reviewers: [copilot, gemini, codex, claude]
auto_on_open:
  copilot: true     # reviews on PR open → loop does NOT re-summon it in round 1
  gemini: true
  codex: true
  claude: false     # mention-driven → loop summons it (@claude review) in round 1
```

What the loop does with it:

- **`auto_on_open: true`** — the bot reviews on its own when the PR opens, so the
  loop does **not** post its trigger in round 1 (that would produce a duplicate
  review). It is still re-triggered in every later round.
- **`auto_on_open: false`** — the bot does **not** review on open, so the loop
  **summons it in round 1** (posts its @-mention trigger) and every round after,
  ensuring its review still arrives.

Either way the loop **polls for every enabled reviewer's comments** — `auto_on_open`
only governs round-1 *summoning*, never whether the bot is expected.

**Backward compatible.** A config with only `active_reviewers` (no `auto_on_open`
key) uses the per-bot defaults — `claude: false`, the three GitHub-App reviewers
`true`.

**Why it matters.** If your Copilot/Codex/etc. does NOT review on PR open in your
org (e.g. org policy disables "review on PR opened", or you summon it manually),
set `auto_on_open: false` so the loop summons it in round 1 instead of waiting for
an auto-review that never comes. `/review-pr setup` (Step 5) asks this per
reviewer. See [`reviewer-setup.md`](reviewer-setup.md).

### When a reviewer posts nothing

An **enabled** reviewer that produces no comments stays in the round table marked
`No review posted 🔇` — it is still expected, and the loop keeps polling for it.
`Active ✅` only appears once the reviewer has engaged in the current round
(reviewers disabled in config keep a `Not requested 🙅` row for completeness —
never summoned or polled; reviewers
excluded mid-run show `Approved 👍`/`Reviewed — no findings ✓`/`Reviewed — no change ✓`/`Quota exhausted ⚠️`/`PR too large 📦`/`Could not review ❌`/`Polish-only 🧹`; `Could not review ❌` is
per-round — it marks the round whose review attempt errored, and a later round where that
reviewer is no longer re-requested shows `Not requested 🙅`). A reviewer that *never* posts
across the run points at an
incomplete prerequisite setup: the vendor GitHub app / plan may not be installed,
or for an `auto_on_open: true` bot "review on PR opened" may be off for this repo.
See [`reviewer-setup.md`](reviewer-setup.md) for the per-bot checklist.

## Autonomous actions — visibility & control

The loop performs a few actions on your behalf **without asking** — squash-merge
on a clean exit when you opt into `--auto-merge`, and a test-gate skip when you turn
the gate off. Each prints a
distinct, greppable line: `⚙ [auto] <action> — <why>` (intent), then
`✓`/`⊘`/`⚠`/`✗ [auto] …` for the outcome (done / skipped-because-disabled /
fell-back / hard-stopped). `grep -F '[auto]'` on the log shows the full
autonomous-action trail; each intent line names the flag that disables it.

| Flag | Effect |
|---|---|
| `--auto-merge` / `--no-auto-merge` | Squash-merge on a clean exit, or don't. The merge is **opt-in**: `--no-auto-merge` is the default, so the loop stops on a clean pass and notifies you to merge manually unless you pass `--auto-merge`. |
| `--test-failure-mode escalate&#124;off` | `escalate` (default): when a fix breaks a local test, the loop never edits a test or reverts the round — it escalates to the console answer-file with three options: *Push as-is* (bypass the gate this round), *Stop the run* (hand over for manual review, the default), or *I've fixed it — re-run the gate & continue*. `off`: skip the local test gate entirely (push unverified). |

## Test-runner detection & tiers

Before it pushes a round's fix the loop runs your test command and reads the
result. How precisely it can read that result depends on whether it recognises the
runner behind the command — detection is read-only (it inspects your command and
your repo's marker files; it installs nothing and hits no network).

**What the loop recognises**

| Where it looks | What it accepts |
|---|---|
| The command itself | `pytest` / `py.test`, `python -m pytest\|unittest\|nose2\|tox\|nox`, `manage.py test`, `tox`, `nox`, `jest`, `vitest`, `mocha`, `jasmine`, `karma`, `ng test`, `node --test`, `ava`, `bun test`, `deno test`, `go test`, `cargo test`, `cargo nextest run`, `mvn` / `mvnw`, `gradle` / `gradlew`, `mix test`, `dotnet test`, `rspec`, `phpunit`, `pest`, `ctest`, `swift test`, `dart test`, `flutter test` |
| Through a launcher | `npx` / `bunx` / `pnpx`, and `bundle exec` / `poetry run` / `uv run` / `pipenv run` / `rye run` / `pdm run` |
| Through a package script | `npm test` (and the `t` / `tst` aliases), `npm run <name>` / `run-script`, and the same shapes for `yarn`, `pnpm`, and `bun` — resolved through your `package.json` `scripts` to the real runner, following one script to another when a script just calls a second script |
| Through a shell string | `bash -lc "<cmd>"`, including a leading `cd`, an env prefix (`CI=1 …`), or an earlier `&&` step, as long as the string names exactly one recognised runner |
| Repo marker files | `pytest.ini`, `conftest.py`, `pyproject.toml`, `setup.cfg`, `noxfile.py`, `tox.ini`, `package.json`, `bunfig.toml`, `bun.lock*`, `deno.json(c)`, `deno.lock`, `go.mod`, `Cargo.toml`, `mix.exs`, `pom.xml`, `build.gradle(.kts)`, `.rspec`, `Gemfile`, `phpunit.xml`, `tests/Pest.php`, `*.csproj`, `CMakeLists.txt`, `Package.swift`, `pubspec.yaml`, `manage.py` — used when the command alone does not decide |

**The three tiers**

| Tier | What it is | What the gate does |
|---|---|---|
| **A** | A recognised runner resolved from your command — including a `npm test` that unwraps to `jest`, or a `bash -lc` string naming one runner | Reads the result with that runner's own exit-code and marker rules. A zero-test run is reported as *no tests* instead of a pass; `pytest`'s exit 5 stays a skip rather than a red; a red gate's headline names whether it was a test failure, a compile error, a missing dependency, or a timeout |
| **B** | An opaque wrapper — `make test`, `./run-tests.sh`, `nx`, `turbo`, `bazel`, `dbt`, `docker compose`, `tox`, `nox`, a script chaining two tools (`tsc && jest`), or a workspace-wide run (`pnpm -r test`, `npm test -w pkg`, `bun run --filter='*' test`) | Runs the command and gates on it exactly the same way, but reads the output through a marker net pooled across every runner. A non-zero exit is always red; a zero exit whose output carries any runner's zero-test marker is reported as *no tests* |
| **C** | A recognised runner that reports one whole-suite result — `bun test`, `deno test`, `jasmine`, `karma`, `ava`, `node --test`, `cargo test`, minitest | Same per-runner reading as tier A |

A repo driving `pytest` through `tox` or `nox` is reported as the wrapper, which is
what actually ran — so it is gated at tier B, on the wrapper's own exit code, rather
than on `pytest`'s. That is why a `tox` run that ends in a collection error or a
missing dependency is reported as a plain test failure: only the runner the wrapper
hides could tell those apart.

**Bun and Deno.** Both are recognised from the command (`bun test`, `deno test`) and
from their lockfiles/config, and both have runner-specific rules, because each can
exit in a way a generic reading would get wrong: a filtered `deno test` that matches
nothing exits **1** printing `No test modules found`, which is reported as *no tests*
rather than a red, and a `deno` or `bun` run that executed zero tests is reported as
*no tests* even when it exits 0. A `bun run <script>` resolves through `package.json`
the same way `npm run` does, and `bun run --filter`/`-F` (which runs the script in
every matching workspace package) is treated as tier B, so a multi-package run is
never read as one package's result.

## Reviewer quota

The loop does not throttle on a budget: if a provider reports its quota exhausted,
the loop stops gracefully and leaves no corrupt state.

## Effort levels & CLI portability

The loop drives `claude` with a reasoning effort level (`low` … `max`), sized per
role inside the skill (it does **not** inherit your global `~/.claude/settings.json`,
so cost and behaviour are identical across machines). Older CLI builds may not
accept the higher levels (`xhigh` / `max`); the loop probes `claude --help` once
for the levels your CLI advertises and **degrades any unsupported level to `high`**
before the call (logged once). The probe fails open — a modern CLI keeps all five
and behaves exactly as before. Automatic, nothing to set.

## Vendor trigger strings

The reviewer slugs / re-trigger comments (`copilot-pull-request-reviewer[bot]`, `/gemini review`,
`@codex review`, `@claude review`) are config seams: if a vendor renames
its slug, export `BUDDHI_TRIGGER_COPILOT` / `_GEMINI` / `_CODEX` / `_CLAUDE`
(see [`env-vars.md`](env-vars.md)) instead of editing source. Blank/unset uses the
shipped default.

## Load-bearing: the `No issues found.` sentinel

The clean-review detector's first pattern is **coupled to the Claude reviewer
workflow's prompt**: the bundled `claude-code-review.yml` emits the literal
`No issues found.` on a clean review, and that string is how the loop's generic
clean-review detector flips Claude to voluntarily-done. If you change one, change
both in lockstep — otherwise the loop will wait on Claude forever after a clean
review. Ship the workflow template verbatim (see
[`reviewer-setup.md`](reviewer-setup.md)).
