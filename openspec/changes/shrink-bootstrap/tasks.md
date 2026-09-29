## 1. Phase 1: measure and propose

- [x] 1.1 Measure the compact bootstrap by level, surface, section and field; record it in `measurements.md` with `scripts/bootstrap-byte-breakdown.py` as the reproducible source.
- [x] 1.2 Classify each block as every-session or on-demand, propose the core and its allocation, and specify the retrieval path, compatibility plan and regression proof in `design.md`.
- [ ] 1.3 Ruling on the open questions in `design.md` (target, `v4`, the `withheld` sentence, `session` profile). Phase 2 does not start before this.

## 2. Phase 2: implement (blocked on 1.3)

- [ ] 2.1 Red first: add `tests/test_bootstrap_frozen_profiles.py` pinning the pre-diet payload digest of `hosted-alpha-agent-v1` to `-v4` at every level, with the volatile fields normalised, and show it green on the untouched base.
- [ ] 2.2 Red first: add `tests/test_bootstrap_core_rules.py` (rule manifest, per-level presence, core ceiling) and show it failing against the current payload.
- [ ] 2.3 Introduce the section registry and the `section` argument for non-legacy profiles; `section="index"` lists sections with bytes; an unknown section fails with a validation error naming the accepted sections.
- [ ] 2.4 Move blocks to sections byte-identically and write the core digests; add the losslessness test (core plus all sections reconstructs the pre-diet compact payload for every level and non-legacy surface).
- [ ] 2.5 Migrate existing bootstrap tests to the assembling helper; run the bootstrap, memory-loop, continuity and hosted plugin suites.
- [ ] 2.6 Regenerate derived artifacts per `CONTRIBUTING.md` (tool schemas, plugin tree, hosted render, v5 candidate, `docs/capabilities.md`, `tests/harness_modules.txt` for new modules that import `benchmarks/`).
- [ ] 2.7 Replace `COMPACT_BYTE_CEILING` with the ruled core ceiling and a documented margin in both budget test modules; add per-section ceilings.
- [ ] 2.8 Gates: bootstrap tests, `test_scaffold_no_leak`, the privacy gate, `openspec validate --all --strict` (CI-pinned), the hosted-plugin check, ruff F, `generate-capabilities.py --check`.
