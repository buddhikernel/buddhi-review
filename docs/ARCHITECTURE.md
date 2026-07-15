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
