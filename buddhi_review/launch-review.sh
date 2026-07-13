#!/usr/bin/env bash
# launch-review.sh — detach the buddhi-review loop on a PR.
#
# Usage:
#   bash launch-review.sh <PR> --repo OWNER/REPO --cwd PATH [extra args...]
#
# Runs `python3 -m buddhi_review run-loop "<PR>" [args...]` under nohup so it keeps
# running after this shell exits, writes combined output to a temp log, and prints
# the log path so you can follow it. (`run-loop` is the in-process free engine; the
# user-facing `review-pr` front door picks the backend, then this launcher detaches
# the free engine.) Business questions are answered from the terminal via the answer
# file the loop prints. No execute bit needed; invoke as `bash launch-review.sh ...`.
#
# Output streams: the log path is the one machine-readable datum and goes to STDOUT;
# every other line (the launch notice, the follow hint, the macOS extras below) is
# decoration and goes to STDERR.
#
# macOS extras (only when `uname` is Darwin):
#   * A click-to-tail helper `<log>.command` is written next to the log. It runs
#     `tail -n +1 -f` — a FULL replay from line 1, then follow — so a window opened
#     late still shows every earlier round, not just the default last 10 lines.
#     A clickable `file://` "Watch" link to it is printed.
#   * Opt-in auto-open of that window (default OFF). When enabled, the launcher
#     `open -g`s the helper (background open, no focus steal) right after writing it,
#     with a `pgrep` dedupe so a repeat launch never stacks a second window. Resolve
#     order:
#       1. BUDDHI_TAIL_NO_AUTO_OPEN set (any value) → hard OFF. A batch fan-out
#          exports it so N loops never pop N windows, regardless of the user's opt-in.
#       2. BUDDHI_TAIL_AUTO_OPEN truthy (1/true/yes/on) → ON.
#       3. otherwise → OFF.
#     The printed `file://` Watch link is always the universal fallback.
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: bash launch-review.sh <PR> --repo OWNER/REPO --cwd PATH [extra args...]" >&2
  exit 2
fi

PR="$1"
shift

[[ "$PR" =~ ^[1-9][0-9]*$ ]] || { echo "error: PR must be a positive integer, got: $PR" >&2; exit 2; }

# Derive the short repo name for the log + click-to-tail filenames so two repos
# that share a PR number never collide on the same per-PR log. This matches the
# reference tmp-path convention: <repo> is the part after the last "/" in the
# OWNER/REPO passed via --repo (handles both `--repo X` and `--repo=X`), or
# "local" when --repo is absent. The arg is only SCANNED here — it stays in "$@"
# and is forwarded verbatim to run-loop below, so this never alters the loop's args.
REPO_SHORT="local"
_prev=""
for _a in "$@"; do
  case "$_a" in
    --repo=*)
      REPO_SHORT="${_a#--repo=}"
      REPO_SHORT="${REPO_SHORT//\\//}"
      while [[ "$REPO_SHORT" == */ ]]; do REPO_SHORT="${REPO_SHORT%/}"; done
      REPO_SHORT="${REPO_SHORT##*/}"
      ;;
  esac
  if [ "$_prev" = "--repo" ]; then
    case "$_a" in
      -*)
        ;;
      *)
        REPO_SHORT="${_a}"
        REPO_SHORT="${REPO_SHORT//\\//}"
        while [[ "$REPO_SHORT" == */ ]]; do REPO_SHORT="${REPO_SHORT%/}"; done
        REPO_SHORT="${REPO_SHORT##*/}"
        ;;
    esac
  fi
  _prev="$_a"
done
# Replace invalid chars with '_' (not delete) to match tmp_paths.py repo_short()
REPO_SHORT="$(printf '%s' "$REPO_SHORT" | sed 's/[^[:alnum:]_.-]/_/g')"
[ -n "$REPO_SHORT" ] || REPO_SHORT="local"

# A per-PR log in the buddhi-review temp dir. BUDDHI_REVIEW_TMP is the plugin's
# documented temp-dir knob (notifier.py keys the answer/escalation files off it);
# honour it here so the log + click-to-tail .command land in the same place,
# falling back to $TMPDIR then /tmp when it is unset.
TMP_BASE="${BUDDHI_REVIEW_TMP:-${TMPDIR:-/tmp}}"
TMP_BASE="${TMP_BASE%/}"
# When the resolved path is bare /tmp (world-writable, shared), redirect to a
# user-private subdirectory to prevent symlink hijacking (CWE-377 / CWE-59):
# a local attacker could pre-create the predictable per-PR filename as a symlink
# and cause the nohup redirect to overwrite an arbitrary file the victim owns.
# BUDDHI_REVIEW_TMP and TMPDIR are assumed to be user-controlled when set.
if [ "${TMP_BASE%/}" = "/tmp" ]; then
  TMP_BASE="/tmp/buddhi-review-${USER:-shared}"
fi
# Guard against a pre-created directory owned by another user (hijack prevention).
if [ -d "${TMP_BASE%/}" ] && [ ! -O "${TMP_BASE%/}" ]; then
  echo "error: temp dir ${TMP_BASE%/} exists but is not owned by you" >&2
  exit 1
fi
if ! mkdir -p -m 700 "${TMP_BASE%/}" 2>/dev/null; then
  TMP_BASE="${TMPDIR:-/tmp}"
  if [ "${TMP_BASE%/}" = "/tmp" ]; then
    TMP_BASE="/tmp/buddhi-review-${USER:-shared}"
  fi
  if [ -d "${TMP_BASE%/}" ] && [ ! -O "${TMP_BASE%/}" ]; then
    echo "error: temp dir ${TMP_BASE%/} exists but is not owned by you" >&2
    exit 1
  fi
  mkdir -p -m 700 "${TMP_BASE%/}" || { echo "error: cannot create temp dir (tried BUDDHI_REVIEW_TMP, TMPDIR, /tmp)" >&2; exit 1; }
fi
if [[ "$TMP_BASE" != /* ]]; then
  TMP_BASE="$PWD/$TMP_BASE"
fi
export BUDDHI_REVIEW_TMP="$TMP_BASE"
LOG="${TMP_BASE%/}/buddhi-${REPO_SHORT}-PR${PR}.log"

# BUDDHI_LAUNCH_PYTHON is a test seam (default `python3`): the harness points the
# DETACHED loop interpreter at a no-op (`true`) so it can exercise the rest of the
# launcher — the tailcmd write + the auto-open block — without spawning a real loop.
LAUNCH_PY="${BUDDHI_LAUNCH_PYTHON:-python3}"
command -v "$LAUNCH_PY" >/dev/null 2>&1 || { echo "error: python interpreter '$LAUNCH_PY' not found" >&2; exit 1; }
nohup "$LAUNCH_PY" -u -m buddhi_review run-loop "$PR" "$@" < /dev/null >"$LOG" 2>&1 &
PID=$!

# ── Startup-gate refusal surfaced IN THIS SESSION ────────────────────────────
# The two launch preflight gates (primary-checkout + repo-confirmation) run
# INSIDE the detached run-loop, so a refusal would otherwise be silent — the loop
# exits ~immediately and only $LOG carries the reason. Poll the just-spawned PID
# briefly: if it is already gone AND $LOG carries the "refused to launch" marker
# (round_driver.REFUSED_TO_LAUNCH_MARKER — keep this literal in lockstep), surface
# a red refusal panel here and exit 2. A loop that survives the short cap (the
# normal case) falls through to the usual log:/Watch output and returns promptly.
# The poll exits the instant the loop dies, so a fast clean exit (e.g. a test
# recorder, no marker) adds ~no latency and is deliberately left to proceed.
# Skippable (set BUDDHI_SKIP_LIVENESS_CHECK to any non-empty value to skip;
# a batch fan-out sets it to avoid N×wait); the cap is tunable (BUDDHI_LIVENESS_WAIT, default 3s).
if [ -z "${BUDDHI_SKIP_LIVENESS_CHECK:-}" ]; then
  _wait_val="${BUDDHI_LIVENESS_WAIT:-3}"
  if ! [[ "$_wait_val" =~ ^[0-9]+$ ]]; then
    _wait_val=3
  fi
  _max_ds=$(( _wait_val * 10 )); _ds=0
  while [ "$_ds" -lt "$_max_ds" ] && kill -0 "$PID" 2>/dev/null; do
    # sleep 0.1 is fine: this script is #!/usr/bin/env bash on macOS, and
    # macOS bash's sleep accepts fractional seconds. POSIX-minimal portability
    # is not a goal here (the script already uses bash-only [[ … =~ … ]]).
    sleep 0.1; _ds=$(( _ds + 1 ))
  done
  if ! kill -0 "$PID" 2>/dev/null \
     && [ -s "$LOG" ] && grep -qF "refused to launch" "$LOG" 2>/dev/null; then
    # A loud refusal panel, on STDERR (stdout stays empty so no caller mistakes
    # this for a successful launch). Colour only on a TTY, honouring NO_COLOR /
    # BUDDHI_LOOP_NO_COLOR (the same env the rest of the pipeline respects).
    if [ -t 2 ] && [ -z "${NO_COLOR:-}" ] && [ -z "${BUDDHI_LOOP_NO_COLOR:-}" ]; then
      _R=$'\033[31m'; _B=$'\033[1m'; _X=$'\033[0m'
    else
      _R=''; _B=''; _X=''
    fi
    _BAR="══════════════════════════════════════════════════════════════════════════"
    {
      printf '\n%s%s%s\n' "$_R" "$_BAR" "$_X"
      printf '%s%s✗ Review loop refused to launch — see why below%s\n' "$_R" "$_B" "$_X"
      printf '%s  PR #%s — a startup gate blocked it (the loop did NOT launch)%s\n' "$_R" "$PR" "$_X"
      printf '%s%s%s\n' "$_R" "$_BAR" "$_X"
      printf '\n── why (tail of %s) ──────────────────────────────────\n' "$LOG"
      tail -n 20 "$LOG" || true
    } >&2
    exit 2
  fi
fi

echo "log: $LOG"
echo "Cleared for takeoff — buddhi-review launched (PID $PID) on PR #${PR}" >&2
echo "Telemetry (live log) — follow it with:  tail -n +1 -f \"$LOG\"" >&2

# The "where to watch" pointer as a NOTICE: line on STDOUT. The tier-neutral
# SKILL.md relays every NOTICE: line verbatim, so this telemetry pointer reaches
# the user's chat reply without the skill hard-coding any engine-specific content
# itself — an engine with a richer place to watch simply prints its own NOTICE:
# line instead, and the same skill text carries it. The `log:` / stderr `Telemetry`
# lines above are kept unchanged for backward compatibility with any existing
# parser. On open-pr this launcher's stdout is folded into the actuator's stderr,
# so the PR URL stays the last line of the actuator's stdout — the NOTICE line
# never touches that contract.
printf 'NOTICE: Watch the live log:  tail -n +1 -f %q\n' "$LOG"

# ── macOS: click-to-tail .command + opt-in auto-open ─────────────────────────
if [ "$(uname)" = "Darwin" ]; then
  TAILCMD="${TMP_BASE%/}/review-tail-${REPO_SHORT}-PR${PR}.command"
  # Write the click-to-tail helper. `tail -n +1 -f`: replay from line 1, then
  # follow — so a window opened late shows every earlier round. %q quotes the log
  # path so spaces/specials survive. Rewriting is harmless (a live tail follows
  # the LOG, not this file) and the content is identical run-to-run. The whole
  # write runs in a subshell whose stderr is silenced, so an unwritable temp dir
  # degrades cleanly (no leaked redirect error) — the file:// link is just skipped.
  rm -f "$TAILCMD" 2>/dev/null || true
  # Use noclobber (set -C) so the create is O_EXCL — if an attacker races to
  # plant a symlink after the rm -f, the write fails silently rather than
  # following the symlink into an unintended target.
  if (set -C; {
       printf '#!/bin/bash\n'
       printf '# Auto-generated by launch-review.sh — tails the buddhi-review log.\n'
       printf 'exec tail -n +1 -f %q\n' "$LOG"
     } > "$TAILCMD") 2>/dev/null; then
    chmod +x "$TAILCMD" 2>/dev/null || true
    # The file:// Watch link is the universal fallback, so it must always carry
    # the .command path — including when $LAUNCH_PY is NOT real Python. That var
    # is the detached-loop interpreter seam; a caller (or the test harness) may
    # point BUDDHI_LAUNCH_PYTHON at a no-op like `true`, which ignores `-c`, prints
    # nothing and exits 0 — so a `|| ...` fallback that only fires on a NON-zero
    # exit would leave TAILCMD_URL empty and the link broken. Encode via Python
    # when it actually produces output, else fall back to a pure-sed space encode.
    TAILCMD_URL=$("$LAUNCH_PY" -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1]))" "$TAILCMD" 2>/dev/null || true)
    [ -n "$TAILCMD_URL" ] || TAILCMD_URL=$(printf '%s' "$TAILCMD" | sed 's/ /%20/g')
    echo "Telemetry (live log) — Watch in a new terminal: file://$TAILCMD_URL" >&2
    # S3 contract: the clickable file:// pointer as a NOTICE: line on STDOUT so the
    # skill relays it to chat (macOS only — the .command exists). A Mac user gets a
    # one-click Watch link; the universal `tail` NOTICE above still covers every OS.
    printf 'NOTICE: Watch the live log (clickable):  file://%s\n' "$TAILCMD_URL"

    # Opt-in auto-open (default OFF). The file:// link above is always the
    # universal fallback; this only ADDS a background open when opted in.
    AUTO_OPEN=0
    if [ -n "${BUDDHI_TAIL_NO_AUTO_OPEN:-}" ]; then
      AUTO_OPEN=0  # hard-OFF suppression (e.g. batch fan-out)
    else
      case "$(printf '%s' "${BUDDHI_TAIL_AUTO_OPEN:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) AUTO_OPEN=1 ;;
        *)             AUTO_OPEN=0 ;;
      esac
    fi

    if [ "$AUTO_OPEN" = "1" ]; then
      # Test seams: BUDDHI_OPEN_BIN (default `open`) and BUDDHI_PGREP_BIN (default
      # `pgrep`) let a test stub the external commands without real windows.
      OPEN_BIN="${BUDDHI_OPEN_BIN:-open}"
      PGREP_BIN="${BUDDHI_PGREP_BIN:-pgrep}"
      # Dedupe: the .command execs `tail … <log>`, so the log path is in the
      # tail's argv. A live pgrep match means a live window for THIS log — skip
      # the open so a repeat launch doesn't stack a second window. Escape the
      # regex metacharacters in the log path so the match stays literal.
      LOG_ESC=$(printf '%s\n' "$LOG" | sed 's/[][\\.*^${}?+|()]/\\&/g')
      if "$PGREP_BIN" -f "tail.* $LOG_ESC$" >/dev/null 2>&1; then
        echo "tail window already open for this log — not opening another." >&2
      elif "$OPEN_BIN" -g "$TAILCMD" >/dev/null 2>&1; then
        # open -g: background open, no focus steal. Degrade silently on failure.
        echo "⚙ [auto] opened the telemetry (live-log) window in the background (disable: BUDDHI_TAIL_AUTO_OPEN=0)" >&2
      fi
    fi
  fi
fi
