---
title: Security policy
---

# Security policy

`buddhi-review` is alpha software. Security fixes land on the latest released
version; there is no separate maintenance branch.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** Report it privately so
a fix can ship before details are public:

1. Go to the repository's **Security** tab and choose **Report a vulnerability**
   (GitHub private vulnerability reporting). This opens a private advisory visible
   only to you and the maintainers.
2. If private reporting is not enabled on the repo, open a minimal public issue that
   only asks a maintainer to turn it on. Do **not** include reproduction steps,
   payloads, or affected-path details in that public issue.

Please include the version, your OS and Python version, a clear reproduction, and the
impact you observed. We aim to acknowledge a report within a few days.

## What the package does on your machine

Understanding the trust boundary helps you scope a report:

- **It runs local subprocesses.** The loop shells out to `git`, the GitHub CLI
  (`gh`), and the Claude CLI (`claude`) in the repository you point it at. It runs
  fixers inside a dedicated worktree, and on macOS wraps each fixer with
  `sandbox-exec` so a fixer cannot write to the primary checkout (fail-open if
  `sandbox-exec` is absent).
- **It installs a PreToolUse hook.** Each bundled skill registers a git-guardrail
  hook that blocks history-rewriting git (rebase / merge / reset --hard /
  cherry-pick / force-push) while a review is in flight. It activates only when the
  skill runs and leaves your everyday git untouched.
- **It reads and writes local files.** Config lives at
  `~/.config/review-loop/config.yaml`; escalation answer files are written under your
  temp directory and created with `O_EXCL` (plus `O_NOFOLLOW` where the platform has
  it) so the loop owns the path it writes.
- **It makes no outbound network connections of its own.** The reviewers it triggers
  run on GitHub Actions or each vendor's service; the loop itself only drives the
  local CLIs above.
- **It performs no license, lease, or activation check** and ships no compiled
  extension. The published wheel is pure Python you can read end to end.

Credentials (a Claude token, a `GH_TOKEN`) are never baked into the package: they
stay in your shell environment or in GitHub repo secrets.
