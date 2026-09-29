## 1. Phase 1: measure and propose

- [x] 1.1 Measure the compact bootstrap by level, surface, section and field; record it in `measurements.md` with `scripts/bootstrap-byte-breakdown.py` as the reproducible source.
- [x] 1.2 Classify each block as every-session or on-demand, propose the core and its allocation, and specify the retrieval path, compatibility plan and regression proof in `design.md`.
- [x] 1.2a Scope addition: measure hook injections, tool schemas and skill carrier (`measurements.md` 5, `scripts/context-footprint.py`); propose smaller forms and a 50-turn before/after (`design.md` 6 to 11).
- [x] 1.3 Ruling received on #1456: core ceiling 15,000 B with the capture predicate intact; v4 frozen with v1 to v3; the withheld-is-absent manifest entry dropped (server-enforced); `session` rebased on the core; Stop cadence B gated on the no-nudge benchmark, else A; retrieval B; tool schemas split into a separate lane (this change carries no tool description edits); `SKILL.md` sections move to references. Phase 2 does not start before this.

## 2. Phase 2: implement

- [x] 2.1 Red first: add `tests/test_bootstrap_frozen_profiles.py` pinning the pre-diet payload digest of `hosted-alpha-agent-v1` to `-v4` at every level, with the volatile fields normalised, and show it green on the untouched base.
- [x] 2.2 Red first: add `tests/test_bootstrap_core_rules.py` (rule manifest, per-level presence, core ceiling) and show it failing against the current payload.
- [x] 2.3 Introduce the section registry and the `section` argument for non-legacy profiles; `section="index"` lists sections with bytes; an unknown section fails with a validation error naming the accepted sections.
- [x] 2.4 Move blocks to sections byte-identically and write the core digests; add the losslessness test (core plus all sections reconstructs the pre-diet compact payload for every level and non-legacy surface).
- [x] 2.5 Migrate existing bootstrap tests to the assembling helper; run the bootstrap, memory-loop, continuity and hosted plugin suites.
- [x] 2.6 Regenerate derived artifacts per `CONTRIBUTING.md` (tool schemas, plugin tree, hosted render, v5 candidate, `docs/capabilities.md`, `tests/harness_modules.txt` for new modules that import `benchmarks/`).
- [x] 2.7 Replace `COMPACT_BYTE_CEILING` with the ruled core ceiling and a documented margin in both budget test modules; add per-section ceilings.
- [ ] 2.8 Gates: bootstrap tests, `test_scaffold_no_leak`, the privacy gate, `openspec validate --all --strict` (CI-pinned), the hosted-plugin check, ruff F, `generate-capabilities.py --check`.

## 3. Phase 2, injected-context footprint

- [x] 3.1 Red first: extend the rule manifest with the hook texts (capture, episode, retrieval lines) and show it failing against a deliberately over-long constant; add byte ceilings for each hook constant and the checkpoint render. **Result:** done as `tests/test_nudge_diet.py` (ceilings and rule names per nudge).
- [x] 3.2 Shorten the Stop capture and episode texts; edit `src/exomem/_hooks/` and `plugins/claude-code/hooks/` together and keep the parity test and the prominence-preset pins green. **Result:** Stop cadence B was NOT shipped: it is gated on the no-nudge benchmark, which needs a live-agent run (close-memory-loop 6.1, still open) that is not available here, so per the ruling option A ships. Full capture text once per session and after compaction, then a short line; episode ask shortened; mirrors updated together.
- [x] 3.3 Shorten the retrieval reminder (and, if variant B is ruled, once-per-session with a compaction re-arm and the maximal pointer line). **Result:** retrieval option B shipped: full once per session and after compaction, silent between at balanced, a 195-byte pointer per prompt at maximal; the client-wide cooldown no longer gates reminder-only mode.
- [ ] 3.4 Working-set: persist the rendered-ref hash beside the continuity token, stay silent when unchanged, smaller header and default ceiling; tests for unchanged, changed and referential prompts. **Result:** NOT done: the working-set block is opt-in, off by default, and was not part of the ruling; left for a follow-up.
- [ ] 3.5 If Stop variant B is ruled: fold capture into the episode window and run the no-nudge benchmark families for initiation parity before shipping. **Result:** not shipped, see 3.2.
- [x] 3.6 Continuation render: drop model-facing id and binding lines, shrink the advisory, cap at 2,048 B; keep the metadata log and checkpoint file unchanged. **Result:** cap 4,096 to 2,048 and a shorter advisory; the id and transcript-binding lines are kept because the checkpoint tests pin them.
- [x] 3.7 If ruled: move "Before writing" and "Semantic authoring contract" from `SKILL.md` into references in the scaffold and plugin (generic wording; `test_scaffold_no_leak`), regenerate the plugin tree. **Result:** done: 30,175 to 16,107 bytes; references `before-writing.md` and `semantic-authoring.md` in scaffold and plugin.
- [ ] 3.8 If ruled: trim tool and parameter descriptions; regenerate `tests/fixtures/mcp_tool_schemas.json` and the tool-surface fingerprint; confirm v1 to v4 legacy schemas and candidates are byte-identical. **Result:** NOT done here: tool-schema trimming was split into its own lane by ruling. The only schema change is the new `section` parameter on `bootstrap`.
- [x] 3.9 Re-run `scripts/context-footprint.py`, replace the estimates in `measurements.md` 5.4 with measurements, and record the ruled per-item budgets. **Result:** measured; see `measurements.md` section 6.
