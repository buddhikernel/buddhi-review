#!/usr/bin/env bash
# launch-setup.sh — open the buddhi-review setup wizard in a fresh terminal window.
#
# Usage:
#   bash launch-setup.sh [--repo owner/repo]
#
# Why this exists: the setup wizard is an interactive raw-mode TTY program
# (arrow-key selectors, getpass secret prompts). An AI coding agent running the
# review flow through a non-interactive Bash tool cannot drive those prompts, so
# the `/review-pr setup` skill step shells out to THIS, which opens the wizard in
# a NEW terminal window — the agent session stays alive. All the cross-platform
# spawn logic (and its unit tests) live in setup_launcher.py; this is a thin,
# convention-matching wrapper (mirrors launch-review.sh) so the skill has a stable
# bash entry point. No execute bit needed; invoke as `bash launch-setup.sh ...`.
#
# On a headless / SSH host with no window server, setup_launcher.py prints the
# one-liner to run by hand and exits 0 (it does not fail the calling flow).
#
# BUDDHI_SETUP_PYTHON is a test seam (default `python3`): the harness points it at
# a stub so the wrapper can be exercised without a real interpreter.
set -euo pipefail

PY="${BUDDHI_SETUP_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "error: python interpreter '$PY' not found" >&2; exit 1; }
exec "$PY" -m buddhi_review.setup_launcher "$@"
