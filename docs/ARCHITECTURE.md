---
title: Architecture
---

# Architecture

`buddhi-review` is an adapter of the [Buddhi kernel](https://github.com/buddhikernel/buddhi)
onto the GitHub PR-review substrate.

- **The four-verb adapter** (`buddhi_review/adapter.py`) implements the kernel's
  `Adapter` contract: `ingest` (yield the PR's raw comments), `run_embedded`
  (Stage-0 condition one item, then run the seven decisions), `escalate_async`
  (deliver a pre-reasoned ask), and `detect_resolved` (observe a *signaled*
  out-of-band resolution).
- **The five seams** (`buddhi_review/seams.py` + `policy.py`) are the concrete
  implementations the kernel calls back through:
  - **Store**: interrupt counters plus the two-tier exclusion lattice (quota and
    PR-too-large are permanent; an errored bot is transient and comes back on its
    next substantive comment).
  - **Router**: a stakes-based effort recommendation.
  - **Escalation**: translates the kernel's pre-reasoned ask into a
    channel-agnostic ask and delivers it over the console channel.
  - **OOB source**: declares the substrate can observe a *signaled* resolution.
  - **PolicyPack**: the single policy contract, bundling the discard predicate, the
    effort taxonomy, the judgment threshold, the validity rule, the ask phrasings,
    and the bounded interrupt budget.

Nothing domain-specific lives in the kernel; it all enters through the policy pack
and the seams. See the [Buddhi kernel](https://github.com/buddhikernel/buddhi) for
the kernel's own design.

## Protections in a pull request

A pull request can list requirements under `Protections this PR must keep`. The loop includes the complete list when it classifies, applies, and verifies review fixes. If it detects a conflict, it holds the change and asks whether to keep the protection, allow that change on that thread, or handle it yourself. When an automatic description rewrite would change the protections section, the choices are to keep the protection or handle it yourself. You answer through the answer file named on the console.

The list may contain up to 40 protections, with a combined length of up to 8,000 characters in their parsed texts. The loop holds review fixes when either limit is exceeded, or when the description cannot be read and there is no earlier successful reading in the run.

With protections present, the verifier receives the measured additions, deletions, and total for the whole proposed pull request when measurement succeeds. Otherwise, it receives an explicit UNAVAILABLE result, never a zero substituted for a failed measurement.

Protections force verification of nonempty review-fix diffs even when optional fix verification is disabled. If verification returns no verdict, an initial fix is retained and marked UNVERIFIED. A guided retry after a verifier rejection must be verified; otherwise, the loop attempts to undo it and retains the rejected outcome. A model assesses protection conflicts, so detection of every violation is not guaranteed.

Automatic description rewrites are refused if they change any existing protections section, including its heading, comments, or whitespace.
