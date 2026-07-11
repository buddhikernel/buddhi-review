# Environment variables

Environment variables the skill recognises. **None carries a baked secret**, and
the package is pip-installed, so there is no clone-path variable to set. Defaults
are sane; the skill works with no env vars at all.

| Var | Controls | Default | If unset/invalid |
|---|---|---|---|
| `BUDDHI_CONFIG` | Path to the config file the skill reads/writes (test/CI seam; normally `~/.config/review-loop/config.yaml`) | `~/.config/review-loop/config.yaml` | uses the default path |
| `BUDDHI_LOOP_PLAN` | Active plan tier override (test/CI seam; normally set via `~/.config/review-loop/config.yaml`) | config → `max-5x` | uses config/default |
| `CLAUDE_BIN` | Claude CLI path override | PATH + common dirs | hard exit if unresolved |
| `BUDDHI_MAX_ROUNDS` | Max fix/review rounds | auto-size from the PR diff → 10 | positive int wins; else auto-size, else a stderr-warned fallback of 10 |
| `BUDDHI_BOT_QUIESCENCE_SECS` | Silence window (seconds) after a bot's last comment before it is done for the round | 60 | positive int wins; `0`/negative/garbage/blank → default |
| `BUDDHI_MAX_WAIT_TOTAL` | Hard ceiling (seconds) on how long one round waits for reviewer bots | 1800 (30 min) | clamped ≥ 1; garbage/blank → default |
| `BUDDHI_TEST_GATE_TIMEOUT_SECS` | Hard timeout (seconds) on the pre-push local test-gate subprocess | 600 (10 min) | positive int wins; `0`/negative/garbage/blank → default |
| `BUDDHI_CLASSIFY_TIMEOUT` | Per-comment classify subprocess timeout (seconds) | 120 | clamped ≥ 1; garbage/blank → default |
| `BUDDHI_CLASSIFY_RETRIES` | Retries on a failed comment classification (`0` disables the retry) | 1 | clamped ≥ 0; garbage/blank → default |
| `BUDDHI_FIX_RETRIES` | Retries on a transient per-comment fix failure (timeout / non-zero rc; `0` disables; SKIP/success are never retried) | 1 | clamped ≥ 0; garbage/blank → default |
| `BUDDHI_VERIFY_REJECT_RETRIES` | Bounded guided retries after a fix-verify REJECT — re-dispatches with the rejection reason (`0` disables; the retry's verify is forced; a retry that SKIPs or can't be verified never resolves) | 1 | clamped ≥ 0; garbage/blank → default |
| `BUDDHI_TRIGGER_COPILOT` | Override the Copilot reviewer slug — lets a vendor slug rename be config, not a source edit | `copilot-pull-request-reviewer[bot]` | blank/unset → default |
| `BUDDHI_TRIGGER_GEMINI` | Override the Gemini re-trigger PR comment | `/gemini review` | blank/unset → default |
| `BUDDHI_TRIGGER_CODEX` | Override the Codex re-trigger PR comment | `@codex review` | blank/unset → default |
| `BUDDHI_TRIGGER_CLAUDE` | Override the Claude re-trigger PR comment | `@claude review` | blank/unset → default |
| `BUDDHI_TEST_FAILURE_MODE` | How a red local-test gate is handled — seeds the `--test-failure-mode` default. `escalate` (default; never edits a test or reverts the round — escalates to the console) or `off` (skip the gate). | `escalate` | invalid → `escalate` |
| `BUDDHI_TEST_COMMAND` | Shell command the local test gate runs (overrides auto-detect). Auto-detect runs `python3 -m pytest tests/ -q` when a `tests/` directory exists; set this to use a different test runner or path. Unset with no `tests/` directory = gate is skipped entirely. | auto-detect | blank/unset → auto-detect |
| `BUDDHI_TEST_FAILURE_RERUNS` | How many times the test gate re-runs a failing suite before declaring it red (`0` disables reruns). | `3` | invalid/garbage → `3` |
| `GH_TOKEN` / `GITHUB_TOKEN` | CI/non-interactive `gh` auth | unset | treated as authed when either is set |
| `NO_COLOR` / `BUDDHI_LOOP_NO_COLOR` | Disable ANSI colour | colour on | presence-toggle |
| `BUDDHI_LOOP_NO_LINKS` | Disable OSC-8 / `file://` hyperlinks (keeps colour) | links on | presence-toggle |
| `BUDDHI_NO_UPSELL` | Silence the locked "paid tier" upgrade teasers (the `/review-pr setup` nudges and the end-of-run upgrade line) | teasers shown | blank/unset → teasers render |
| `BUDDHI_ALLOW_UNCONFIRMED_REPO` | Run on the built-in defaults instead of stopping when a repo has no confirmed reviewer fleet and no global default fleet (bypasses the confirmation gate) | gate enforced (exits with a setup banner) | blank/unset → gate enforced |
| `BUDDHI_ALLOW_PRIMARY_CHECKOUT` | Let the loop run in the repo's PRIMARY checkout while it sits on the PR branch (bypasses the worktree-isolation refusal) | refused (the loop exits) | blank/unset → refused |

Model identity is **not** an env var: it is resolved per role from your active
plan profile (see [`configuration.md`](configuration.md)).

The reviewer fleet is **not** an env var either: the enabled set
(`active_reviewers`) and the per-bot "reviews on PR open" facts (`auto_on_open`)
live only in `~/.config/review-loop/config.yaml`, written by `/review-pr setup`.
See [`configuration.md`](configuration.md) and [`reviewer-setup.md`](reviewer-setup.md).

## Effort levels & CLI portability

The loop drives the `claude` CLI with an effort level (`low`, `medium`, `high`,
`xhigh`, `max`). Older CLI builds may not accept the higher levels and would reject
them at parse time. To stay portable the loop probes `claude --help` **once** at
first use for the levels your installed CLI actually advertises, and **degrades any
level it does not support down to `high`** before the call (logging the degrade
once). An unknown / malformed level also lands on `high`. The probe **fails open**:
if it cannot read the supported set, it assumes the full five, so a modern CLI
behaves exactly as before. Nothing to configure.
