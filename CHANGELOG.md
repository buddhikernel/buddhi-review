# Changelog

## [0.3.0](https://github.com/buddhikernel/buddhi-review/compare/v0.2.1...v0.3.0) (2026-07-11)


### Features

* guided verify-reject retry + per-test circuit-breaker ([#71](https://github.com/buddhikernel/buddhi-review/issues/71)) ([009f5df](https://github.com/buddhikernel/buddhi-review/commit/009f5dfe02bc26705717d1b742813a306671f43b))
* muted update-available banner at the skill-call launch surface (F4) ([#70](https://github.com/buddhikernel/buddhi-review/issues/70)) ([31f36be](https://github.com/buddhikernel/buddhi-review/commit/31f36be6adf95b02cf8cf78faa4d092cad1db7a6))
* **test-gate:** F1 — OSS command-seam reconcile + shell-operator executor fix ([#67](https://github.com/buddhikernel/buddhi-review/issues/67)) ([10e31a0](https://github.com/buddhikernel/buddhi-review/commit/10e31a0049e61369b44e11725b9216988fc2cc6c))


### Bug Fixes

* converge the behind-check to the reference tree's merged state ([#66](https://github.com/buddhikernel/buddhi-review/issues/66)) ([473b707](https://github.com/buddhikernel/buddhi-review/commit/473b70715520396f947d7e4612804d0805b012a4))


### Documentation

* add README badges (PyPI, Python versions, MIT license) ([#64](https://github.com/buddhikernel/buddhi-review/issues/64)) ([73ebba2](https://github.com/buddhikernel/buddhi-review/commit/73ebba279accbd31df79006ff72ca74e7a660cb4))
* **readme:** fix clipped words in mobile chart wrapping ([#53](https://github.com/buddhikernel/buddhi-review/issues/53)) ([7ddcea7](https://github.com/buddhikernel/buddhi-review/commit/7ddcea794f4f64e7acdd237235c9c6a863a681c5))

## [0.2.1](https://github.com/buddhikernel/buddhi-review/compare/v0.2.0...v0.2.1) (2026-07-09)


### Bug Fixes

* rebase behind+red PRs — git behind-check independent of mergeStateStatus ([#62](https://github.com/buddhikernel/buddhi-review/issues/62)) ([3db5bee](https://github.com/buddhikernel/buddhi-review/commit/3db5beeb2c8c3a3572c126dd26f729af0da97d38))

## [0.2.0](https://github.com/buddhikernel/buddhi-review/compare/v0.1.3...v0.2.0) (2026-07-08)


### Features

* **labels:** adopt the canonical round-summary label set ([#34](https://github.com/buddhikernel/buddhi-review/issues/34)) ([8a32784](https://github.com/buddhikernel/buddhi-review/commit/8a32784f22ff875c84e595c1876c63fc2379ca99))
* **loop:** rebase a manual-landing PR onto latest base on hand-back (F12) ([#13](https://github.com/buddhikernel/buddhi-review/issues/13)) ([7b8929e](https://github.com/buddhikernel/buddhi-review/commit/7b8929ed57c353b90a3e57752986d995ff4b8c7a))
* **merge:** pre-merge mergeability gates + reviewer-visibility surfaces ([#46](https://github.com/buddhikernel/buddhi-review/issues/46)) ([e97089a](https://github.com/buddhikernel/buddhi-review/commit/e97089adba1e74aa29353ea403ca2e52a169bf97))
* **open-pr:** auto-target the worktree the session worked in ([#50](https://github.com/buddhikernel/buddhi-review/issues/50)) ([9337d3a](https://github.com/buddhikernel/buddhi-review/commit/9337d3a22e9b3b38e6b513bed1ba9d67b00d20b7))
* rename the create-pr skill to open-pr ([#40](https://github.com/buddhikernel/buddhi-review/issues/40)) ([b67afc5](https://github.com/buddhikernel/buddhi-review/commit/b67afc50705c8ce4e593e4ec3a73acb1f4f40d82))
* **reviewers:** --rr-none — summon nobody, resolve existing comments, merge ([#44](https://github.com/buddhikernel/buddhi-review/issues/44)) ([274c1d5](https://github.com/buddhikernel/buddhi-review/commit/274c1d5e256edb724c1acf50380809eb9e767084))
* **reviewers:** detect Claude usage-limit silence — timed rate-limit comeback ([#41](https://github.com/buddhikernel/buddhi-review/issues/41)) ([c822b6f](https://github.com/buddhikernel/buddhi-review/commit/c822b6f3b648a18a6f48391280f94ede0de97ef5))
* **review:** fail CI red on a silent reviewer 401 + loud re-mint console signal (F9) ([#11](https://github.com/buddhikernel/buddhi-review/issues/11)) ([f36675d](https://github.com/buddhikernel/buddhi-review/commit/f36675dfe4c2b7e9eaaa09650183620e3adf980b))
* **round-driver:** preflight PR-state snapshot + reaction signals + round baselines ([#47](https://github.com/buddhikernel/buddhi-review/issues/47)) ([ecc15ff](https://github.com/buddhikernel/buddhi-review/commit/ecc15ffc5bb76ed3643e6813d290569cf8d5433a))
* **round-driver:** resolve the run's own review threads before a clean-exit merge ([#48](https://github.com/buddhikernel/buddhi-review/issues/48)) ([744a389](https://github.com/buddhikernel/buddhi-review/commit/744a389b704b50a3797f5e387497e227949b71f5))
* **setup:** auto-store global config, harden token paste, reject inconclusive tokens ([#52](https://github.com/buddhikernel/buddhi-review/issues/52)) ([71e0ef0](https://github.com/buddhikernel/buddhi-review/commit/71e0ef02a2d4940c8883ad790b6f28b2350596d7))
* **setup:** fail-closed reviewer install gates + canonical wizard wording (F1) ([#18](https://github.com/buddhikernel/buddhi-review/issues/18)) ([09c7ed7](https://github.com/buddhikernel/buddhi-review/commit/09c7ed7f894a68fa172fccbb24eddbffc57b259b))
* **setup:** FREE wizard provisioning engine — org detect + server-side installer + safe secret scoping (F2) ([#4](https://github.com/buddhikernel/buddhi-review/issues/4)) ([16c3d58](https://github.com/buddhikernel/buddhi-review/commit/16c3d586afbc335ad6b7407d29d813b3af146e02))
* **setup:** guide the Claude GitHub App install in the wizard ([#5](https://github.com/buddhikernel/buddhi-review/issues/5)) ([eec502b](https://github.com/buddhikernel/buddhi-review/commit/eec502b5cee8bd21e19c6235210376360ee9854b))
* **setup:** per-repo write path + label_gated_ci config + status CLI (F1) ([#3](https://github.com/buddhikernel/buddhi-review/issues/3)) ([c0dee89](https://github.com/buddhikernel/buddhi-review/commit/c0dee89c52dc07d8af79bda5f602640f4fa5a9e8))
* **setup:** SKILL.md per-repo reviewer-confirmation gate (F6) ([#9](https://github.com/buddhikernel/buddhi-review/issues/9)) ([1afdc9c](https://github.com/buddhikernel/buddhi-review/commit/1afdc9cf6d0ca258b0cd117477ba51fa5d639611))
* **setup:** token-mint consent gate, validator diagnostics, and setup-copy polish ([#33](https://github.com/buddhikernel/buddhi-review/issues/33)) ([4ec3963](https://github.com/buddhikernel/buddhi-review/commit/4ec3963781f115aae28180aad730e18cd8f1b0e8))
* **setup:** validate a fresh Claude token + re-mint a broken stored one (F10) ([#14](https://github.com/buddhikernel/buddhi-review/issues/14)) ([4a88c81](https://github.com/buddhikernel/buddhi-review/commit/4a88c81180c8ecfcce2367b5234e853960238ca8))
* **setup:** verify a pasted GH_TOKEN before storing it in the shell rc (F11) ([#15](https://github.com/buddhikernel/buddhi-review/issues/15)) ([79bce1a](https://github.com/buddhikernel/buddhi-review/commit/79bce1ad7b017629b2e21870ca701d00e9698ee1))
* **setup:** wire provisioning into per-repo confirm + bundle clean ready-for-ci template (F4) ([#10](https://github.com/buddhikernel/buddhi-review/issues/10)) ([ffa4326](https://github.com/buddhikernel/buddhi-review/commit/ffa432673b109688783117f7bda2f3d3df7aa54c))
* **setup:** wizard per-repo confirm flow + label-gated-CI question (F3) ([#8](https://github.com/buddhikernel/buddhi-review/issues/8)) ([622bb63](https://github.com/buddhikernel/buddhi-review/commit/622bb63597d69c09f4bd37f3e81d09c44bef63ac))
* single-source package version + add skill provenance/transform seam ([#1](https://github.com/buddhikernel/buddhi-review/issues/1)) ([ca6bf95](https://github.com/buddhikernel/buddhi-review/commit/ca6bf95ad96bda64b3df322ce35f82952b38dcc7))
* **upsell:** offer /review-pr setup alongside the upgrade nudge ([#55](https://github.com/buddhikernel/buddhi-review/issues/55)) ([5bae01b](https://github.com/buddhikernel/buddhi-review/commit/5bae01bd0a8197a5ac35391ab8a0360da6588c25))


### Bug Fixes

* **ci:** obey label-gated CI — delete redundant per-push ci.yml (F7) ([#16](https://github.com/buddhikernel/buddhi-review/issues/16)) ([c8bd5f6](https://github.com/buddhikernel/buddhi-review/commit/c8bd5f61fca5fd6dd9e9cec7306ee109b6749587))
* **ci:** restore the real CI command the [#21](https://github.com/buddhikernel/buddhi-review/issues/21) re-bake dropped (R1) ([#25](https://github.com/buddhikernel/buddhi-review/issues/25)) ([aefd4ec](https://github.com/buddhikernel/buddhi-review/commit/aefd4ecd20cc280a7d227fca30f2e39ae7f693d0))
* **ci:** wire buddhi-review's ready-for-ci gate to the real CI (dogfood) ([#6](https://github.com/buddhikernel/buddhi-review/issues/6)) ([f4207cd](https://github.com/buddhikernel/buddhi-review/commit/f4207cd810ab63e1df09da7a3c6596dcdb30bc54))
* **config:** prune stale auto_on_open when a setup re-run drops a reviewer ([#19](https://github.com/buddhikernel/buddhi-review/issues/19)) ([57c49b6](https://github.com/buddhikernel/buddhi-review/commit/57c49b609f5b69a70ed99ac213e8bf8923aa5edb))
* **detectors:** per-cause placeholder gates + errored retraction fixes ([#39](https://github.com/buddhikernel/buddhi-review/issues/39)) ([85c0992](https://github.com/buddhikernel/buddhi-review/commit/85c099275cdcb11c00588442a8cd773ea802f32b))
* **fix-apply:** distinguish BLOCKED / refusal / reject from a genuine skip ([#45](https://github.com/buddhikernel/buddhi-review/issues/45)) ([4048795](https://github.com/buddhikernel/buddhi-review/commit/4048795880385cf6ef55c8f906643d6da82622c3))
* **fix-apply:** harden the dangerous-change tripwire (wide constructs + diff cap) ([#36](https://github.com/buddhikernel/buddhi-review/issues/36)) ([5e4393b](https://github.com/buddhikernel/buddhi-review/commit/5e4393b3686fd4ba153426928eed9c0677b76708))
* **labels:** uniform status-glyph convention across the round summary ([#42](https://github.com/buddhikernel/buddhi-review/issues/42)) ([fb34cfc](https://github.com/buddhikernel/buddhi-review/commit/fb34cfc354086e0930a5c0d6ce10b35cfbd690df))
* **open-pr:** prefix the PR title with the branch's plan id when present ([#57](https://github.com/buddhikernel/buddhi-review/issues/57)) ([bbfa4bc](https://github.com/buddhikernel/buddhi-review/commit/bbfa4bcacd935aa59dd41e7afb8983bc7372b6dd))
* **round-driver:** badge idle reviewers by what the loop can detect ([#49](https://github.com/buddhikernel/buddhi-review/issues/49)) ([92dcff1](https://github.com/buddhikernel/buddhi-review/commit/92dcff12be8caeb215b4eb76d363483c23ac238e))
* **round-summary:** render a row for every built-in reviewer (Not requested ·) ([#38](https://github.com/buddhikernel/buddhi-review/issues/38)) ([f1bedd9](https://github.com/buddhikernel/buddhi-review/commit/f1bedd958cd8d435dcefe322feb478996f87e4d2))
* **round-summary:** round-scope the 'Could not review' label ([#43](https://github.com/buddhikernel/buddhi-review/issues/43)) ([9dc160a](https://github.com/buddhikernel/buddhi-review/commit/9dc160a97100fa7dbb17a7bde124f8752ab90b4f))
* **setup:** arrow-selector Yes/No + blank-line rhythm in the wizard; docs currency ([#22](https://github.com/buddhikernel/buddhi-review/issues/22)) ([8ecacf3](https://github.com/buddhikernel/buddhi-review/commit/8ecacf3eaa7f073040bcd02d419f75071e9f296a))
* **setup:** harden the TTY token reader (converge to code-review's merged version) ([#56](https://github.com/buddhikernel/buddhi-review/issues/56)) ([00c476d](https://github.com/buddhikernel/buddhi-review/commit/00c476dc8b8d149c475809213af409195938efb6))
* **setup:** preserve the installed CI command on gate updates + "reviewed — no findings" round card (R2) ([#31](https://github.com/buddhikernel/buddhi-review/issues/31)) ([b172979](https://github.com/buddhikernel/buddhi-review/commit/b1729796a59407b1ce84cff8d7d52cee51415ca2))
* **setup:** read multi-line pasted tokens via a no-flush TTY reader ([#54](https://github.com/buddhikernel/buddhi-review/issues/54)) ([56794c4](https://github.com/buddhikernel/buddhi-review/commit/56794c4b9d0e227dc14b5901159e2c65b5dfe371))
* **setup:** reconstruct a wrapped-paste token + idempotent update PR ([#24](https://github.com/buddhikernel/buddhi-review/issues/24)) ([da5f5f8](https://github.com/buddhikernel/buddhi-review/commit/da5f5f8b674f47a86c478f92eae7043346e5ace4))
* **setup:** managed-file update probe skips no-op runs; offer outdated-file update ([#17](https://github.com/buddhikernel/buddhi-review/issues/17)) ([64b6231](https://github.com/buddhikernel/buddhi-review/commit/64b623152fa25b10680b765b22bc45d3b34bc014))
* **types:** relax injectable run: annotations to keyword-aware Callable[...] ([#35](https://github.com/buddhikernel/buddhi-review/issues/35)) ([8717638](https://github.com/buddhikernel/buddhi-review/commit/8717638c332b47ef3de5df182cf594d610117871))
* **wizard:** fail closed on a non-ubuntu CI gate runner ([#32](https://github.com/buddhikernel/buddhi-review/issues/32)) ([cb96172](https://github.com/buddhikernel/buddhi-review/commit/cb961725a38e366c4e0a7737a0d3ae428cce8791))


### Documentation

* README opening/charts/cost refresh + GETTING_STARTED onboarding restructure ([#30](https://github.com/buddhikernel/buddhi-review/issues/30)) ([d165bd7](https://github.com/buddhikernel/buddhi-review/commit/d165bd7f42e407576d84accf9a77ef57b5e6db38))
* **readme:** add "Why a panel, and why rounds" value-prop section ([#23](https://github.com/buddhikernel/buddhi-review/issues/23)) ([49726ee](https://github.com/buddhikernel/buddhi-review/commit/49726ee5c7ab1f86f8e9af02cfade2107eab7a09))
* **readme:** dark-mode media + WebP demo, shorter caption ([#51](https://github.com/buddhikernel/buddhi-review/issues/51)) ([cff6331](https://github.com/buddhikernel/buddhi-review/commit/cff63310a30aa6714653111bb075cd2ca75ff74e))
* **readme:** sharpen What-it-is, escalation, Status, and Architecture ([#12](https://github.com/buddhikernel/buddhi-review/issues/12)) ([6781a28](https://github.com/buddhikernel/buddhi-review/commit/6781a28a7a74a5079151b13a303f7782a355b410))
