# Verdict-parity fixtures — the frozen example set

Each `*.json` here is one **frozen past PR**, captured as the input + the
**expected verdict** the rebuilt classify/decide path must reach. They are the
ground truth the verdict-parity suite (`tests/test_verdict_parity.py`) grades the
free skill against — the public stand-in for running the private monolith
side-by-side (which the OSS repo cannot ship). The fixtures travel with this
repo; the monolith does not.

## The parity bar (build-spec §5)

For the same PR + flags, the free skill must reach the **same verdicts**:

1. **Same per-comment classification label** (the 7-label taxonomy).
2. **Same terminal disposition** — `merge` / `escalate-to-human` / `stop` — and
   per-comment `fixed` / `skipped-invalid` / `escalated`.
3. **Same set of autonomous actions** — the `⚙ [auto]` trail compared as a
   **set of `(action, status)` pairs**, never byte-identical text.

Byte-identical logs, fix-commit prose, and model wording are explicitly **not**
graded.

## Fixture schema

```jsonc
{
  "name": "...",                  // unique slug
  "description": "...",           // what this PR exercises, in one line
  "pr": "101",                    // sanitized PR number
  "repo": "example-org/widget",   // sanitized owner/repo — NEVER a real private name
  "auto_merge": true,             // did the run opt into squash-merge on clean exit?
  "config": {                     // the free config surface (active_reviewers + auto_on_open)
    "active_reviewers": ["claude"],
    "auto_on_open": {"claude": false}
  },
  "answer_mode": "none",          // none | unanswered | operator-stop (drives the escalation answer)
  "rounds": [                     // one list of comments per review round
    [ { /* comment */ } ],        // round 1
    [ { /* comment */ } ]         // round 2 (optional — usually the clean sentinel)
  ],
  "expected": {
    "labels": { "c1": "SUBSTANTIVE" },        // per ACTIONABLE comment id -> expected label
    "terminal_disposition": "merge",          // merge | escalate-to-human | stop
    "per_comment": { "c1": "fixed" },         // fixed | skipped-invalid | escalated
    "auto_actions": [["squash-merge","do"], ["squash-merge","done"]]  // the (action,status) SET
  }
}
```

### Comment fields

```jsonc
{
  "id": "c1",
  "login": "claude[bot]",                 // reviewer login (bot_for_login maps it)
  "body": "this upload() call can throw; there is no retry",
  "path": "uploader.py",                  // optional — feeds the classifier prompt
  "diff_hunk": "@@ -10,3 +10,5 @@\n+    upload(file)",  // optional
  "created_at": "2026-01-05T10:00:00Z",   // optional — drives the errored-comeback rule
  "from_issue_channel": false,            // true = PR-conversation timeline (scanned for signals only)

  // An ACTIONABLE comment carries a recorded classifier response — the raw model
  // text the classify step would emit. The suite replays it through the REAL
  // tolerant parser, so the recorded text varies in shape (fenced JSON, plain
  // JSON, JSON-with-preamble, legacy pipe form, label-less garbage) on purpose.
  "classifier_response": "```json\n{\"label\": \"SUBSTANTIVE\", ...}\n```",

  // Optional — the faked fixer outcome for a comment the kernel routes to "fix".
  // Default "applied". Use "skipped" for a fixer self-skip, "transient-failed"
  // to drive the failed-fix escalation.
  "fix_outcome": "applied"
}
```

A comment **with** `classifier_response` is actionable (it is classified). A
comment **without** one is a single-shot signal (clean sentinel / quota /
errored / PR-too-large): its `body` drives the deterministic signal detector and
it never reaches the classifier.

## Sanitization contract (publish-clean)

Nothing in any fixture may contain an author path (`/Users/...`), the owner
handle, a private registry or repo name, or a token. The parity suite enforces
this with a grep guard over every fixture file. Use neutral placeholders:
`example-org/widget`, `app/uploader.py`, generic PR numbers.
