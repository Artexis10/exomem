## 1. Amendment and governance

- [ ] 1.1 Author the §7 sequence-3 entry (dated, reasoned): f27 `lifecycle_routing_replay`, kind operational, no public coverage, the two assertions, the tiers, the harness-fault rule, the store-bearing gate, the paired-readout rule, the expected-partial declaration, no catastrophic additions, no budget constants. Add the §1 row, the §2 assertion names, and the §4 representation-neutral predicates.
- [ ] 1.2 Mint `benchmarks/epistemic/contracts/amendment-2026-08-lifecycle-replay.v1.json` (sequence 3, pending: `parent_contract_sha256` = the sequence-2 `contract_sha256`, `contract_sha256` = the amended document) and prove `validate_working_preregistration` folds the chain.
- [ ] 1.3 Mirror in the registry: `PREREGISTERED_FAMILIES` gains `("f27", "lifecycle_routing_replay")`, the assertion-name tuple gains the two names, `AMENDMENT_INTRODUCED_FAMILIES` gains `f27 → 3`; `tests/test_epistemic_registry.py` drift tests green. Red fixture `red-sequence3-withheld-family.yaml` refuses at load with the typed error naming sequence 3 and f27.

## 2. Corpus and gate

- [ ] 2.1 `benchmarks/epistemic/corpora/lifecycle_replay.py`: seeded vault (slice-1 manifests: Planning keyed `[title]`, Records keyed `[occurred_on, title, event_type]` joined on `title`, the outcome/initiative chain, no items), the transcript with per-turn tier annotations (six deliverables; at least one tentative claim, one elapsed-time remark, one deferral annotated `none`; no two expected events sharing `(title, event_type)`), the folded expected end-state, and `corpus_digest()`. Red-first: the fold test and the no-leak test go red before the module exists.
- [ ] 2.2 `STORE_BEARING_RE` and `assert_no_store_bearing_utterance`, run at corpus construction and at scenario load. Red fixture `red-sequence3-store-bearing-utterance.yaml` ("save this one") refuses naming the turn and the token; the ordinary-language turns are admitted. Cite the retrieve-nudge regex as the sibling and state why it is not reused.
- [ ] 2.3 `benchmarks/epistemic/fixtures/sequence3/f27-lifecycle-routing-replay.yaml`: two phases (`hookless`, `hooked`), each `configure` → one `agent_turn` per corpus turn → `snapshot`, both assertions expected per phase, fairness block in the f26 shape.

## 3. Assertions

- [ ] 3.1 `lifecycle_consequence_landed_unprompted` in `assertions.py`: the collections gate (`blocked` on empty / missing profile), per-tier counting with the pinned normalisation, pass only when every tier is complete, fractions and missing keys in the detail. Red-first, then mechanism removal: drop the gate → blocked case passes; drop the normalisation → case-variant title misses; drop the tier loop → partial landing passes.
- [ ] 3.2 `no_structured_write_beyond_expectation`: extras over plan items, records, collections and pages against the allowlist; pass only on an empty extras set. Red-first, then mechanism removal: drop the page check → a stray note passes; drop the status check → a wrong status passes; drop the collection check → a created collection passes.
- [ ] 3.3 `tests/test_epistemic_no_nudge_families.py` (or a sibling module): both assertions evaluated through `evaluate_scenario` on a hand-built snapshot for the green, the partial and the spurious cases; the `blocked` branch for the unprojected section.

## 4. Journey driver

- [ ] 4.1 `benchmarks/epistemic/journeys/f27_replay.py`: envelope discovery (`claude --version`, refuse when absent), the environment floor (strip `CLAUDECODE` / `CLAUDE_CODE_*` / `CLAUDE_PID`), `build_turn_argv` (`--setting-sources project`, benchmark project cwd, `--strict-mcp-config --mcp-config` via track C's `write_mcp_config`, `--output-format stream-json --verbose --include-hook-events`, `--allowedTools` for the exomem server, `--max-turns`, `--model`, `--session-id` then `--resume`), arm configuration (hookless: no plugin, `--tools ""`, `--append-system-prompt-file` = the `maximal` block from `docs/prominence.md` cited by line, prominence `maximal` written through `python -m exomem prominence` under the arm env; hooked: `--plugin-dir plugins/claude-code`, `--tools Skill`, `EXOMEM_HOOK_HOME` under the workdir, prominence `balanced`), injectable runner, per-turn transcript capture via `parse_stream_json_transcript` plus hook-event counting, vault projection through `VaultProjector`, persistence through the evidence module, `report.json` and `manifest.json` in the D7 shape.
- [ ] 4.2 Harness-fault semantics: non-zero exit, error subtype, `is_error`, "Not logged in", malformed line → arm blocked with reason, no snapshot, both assertions `blocked`. Red-first with an injected failing runner.
- [ ] 4.3 `python -m epistemic.journeys.f27_replay --arm {hookless,hooked,both} --out <dir> [--model M] [--dry-run]`; dry-run prints argv and the environment delta per turn and runs nothing. Test the dry-run output against the installed CLI's declared options (`required_options` pattern from f26).
- [ ] 4.4 Tests with recorded transcripts: a fabricated-but-well-formed stream for each arm drives the full path offline (parse → project → evaluate → report), so the harness is proven before any live run.

## 5. Verification

- [ ] 5.1 `tests/test_epistemic_*` green; `tests/test_epistemic_amendment_governance.py`, `tests/test_epistemic_contract_receipts.py` and `tests/test_epistemic_registry.py` green on the sequence-3 chain; `tests/test_scaffold_no_leak.py` green over the corpus.
- [ ] 5.2 `uvx ruff check . --select F` clean; `openspec validate --all --strict` green; `scripts/check_openspec_archive_discipline.py` ok.
- [ ] 5.3 Dry-run output pasted: both arms, every turn's argv, the stripped variables.
- [ ] 5.4 Development run (orchestrator-run on the subscription login, recorded here as the finding, not a claim): per arm, per tier coverage beside extras, nudge count, write tool-use count, model and CLI pins.
- [ ] 5.5 `benchmarks/README.md` names f27 and the sequence-3 receipt; the f26 docstring cross-references f27 as the agent-driven sibling.
