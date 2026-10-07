## Programme order

This change is the programme's one home. S1 is this delivery. S2 to S10 are sequenced here and land as their own pull requests; S3, S4 and S7 also get their own OpenSpec changes (`add-lifecycle-status-registry`, `add-note-type-registry`, `add-language-packs`), and Planning gets a small change when S10 starts. After S1, S2, S3, S5 and S10 can run in parallel; everything else is sequential.

## S1. Substrate, revert, generic contract, bootstrap section (this delivery)

- [ ] 1.1 Red first: through the MCP surface, promote an entity type, see it in `bootstrap(section="vocabulary")` with a count, use it on a page, restore the previous version, and see the page reported as unregistered debt with its bytes untouched.
- [ ] 1.2 Red first: a stale `expected_hash` refusal; a restricted principal's save becoming a pending vocabulary item on a governed vault; a hand-edited overlay read with findings and snapshotted on the next save.
- [ ] 1.3 Add the parity check for entity types, relations and source kinds on an unchanged vault and on legacy overlays; delete it together with the Python constants it compares.
- [ ] 1.4 Add `src/exomem/vocabulary/` with the registry spec, the generic loader, the stat-then-digest cache, the effective digest and the core packs; move `core-relations.yaml` into the pack directory.
- [ ] 1.5 Make `entity_types`, `relation_registry`, `source_taxonomy` and `semantic_language_registry` read through the loader, keeping their public APIs.
- [ ] 1.6 Commit every registry save and restore through `registry_history.commit`, with the principal hash in the snapshot header; seal the snapshot and log entry as derived auxiliaries for the v2 writer gate.
- [ ] 1.7 Add `inspect`, `propose`, `save`, `history` and `restore` to `schema_memory` for the five subjects; keep the old operation names; change the tool description once; classify `propose` in the egress selector table.
- [ ] 1.8 Apply the governance rule at `owner_only_aggregate`: owner saves apply; restricted saves become pending vocabulary items; counts and reasons are owner-only.
- [ ] 1.9 Serve usage counts from the graph snapshot and the lexical catalogue, `unavailable` when cold; replace the bootstrap `entities` section with `vocabulary` and the core entity-type list with a pointer.
- [ ] 1.10 Invalidate the registry cache from the `_Schema/` watcher event.
- [ ] 1.11 Add the repository rule to `CLAUDE.md`.
- [ ] 1.12 Regenerate the skill contract, plugins, tool schemas, hosted candidate, cloud plugin and capability docs; run the registry, schema_memory, bootstrap, egress, contract and carrier gates, ruff, mypy, OpenSpec strict and the privacy gate.

## S2. Skills

- [ ] 2.1 Add `references/vocabulary.md` and link it from SKILL.md and the capture, ingest, curate and review skills. Teach the loop: read the live set, reuse or alias, promote under a parent only when nothing fits, know it takes effect at once, report the promotion with its revert route.
- [ ] 2.2 Remove hard-coded vocabulary from the skills: the `COMPILED_TYPES` sentences in `semantic-authoring.md` and nine workflow skills, and the inactive-status list. Proof: skill-contract validation, the no-leak scan and the plugin sync.

## S3. Lifecycle statuses (`add-lifecycle-status-registry`)

- [ ] 3.1 Add the status registry with `class` (live, pending, superseded, retired, abandoned) and turn the inactive, retired, history and find-penalty sets into predicates on it; record the `planned` entity-page change.
- [ ] 3.2 Keep an unregistered status live and report it as debt. Proof: register `abandoned` with its class and see `activate_context` stop serving that page as current; run the full suite.

## S4a. Note types, read side (`add-note-type-registry`)

- [ ] 4.1 Add the note-type registry with `role`, move the ranking multipliers into `ranking_config`, and move the semantic contract, audit, claims and the skills' role text onto role predicates. Proof: the suites pass unchanged; run the full suite.

## S4b. Note types, write side and promotion

- [ ] 4.2 Move `folder`, `partition`, `stem`, `required_fields`, `statuses`, typed fields, `sections`, `claim_sections`, `sources` and `time_bounded` into the registry for `note.py`, `create_file`, `indexes`, `adoption_proposals`, `hosted_plugins` and `commands`.
- [ ] 4.3 Proof: promote `meeting-note` under `insight`; `note()` lands it in `Notes/Meetings` with compiled behaviour; `restore` removes it. Run the full suite at the completion boundary.

## S5. Entity, relation and source attributes (after the retire-Other slice)

- [ ] 5.1 Add `claim_sections` and `optional_frontmatter` behaviour for entity types and make `cue_nouns` the only cue-noun source; key relation behaviour on `family`; add `path_label`, `requires_url`, `reserved_path` and `working_context` to source kinds; make semantic-category aliases the source for working-set categories and move the category core into a pack.

## S6. Vault-specific conventions (after S3)

- [ ] 6.1 Replace the four copies of slug suffixes and hub or snapshot tags with `page_classes` in activation conventions, and the research-folder demotion with a project-key rule.

## S7. Language packs (`add-language-packs`, after the licensing slice)

- [ ] 7.1 Move the nine language-word copies verbatim into the `en` pack, add `_Schema/language.yaml`, then collapse divergent copies only where the benchmarks show no per-case loss. Proof: English benchmarks unchanged and a German fixture resolves "weiter".

## S8. Model-judgement replacements

- [ ] 8.1 One PR each for the word-based find intent, artifact-role cues, contact intent and deadline cues. Proof: a per-case ablation with a confidence interval.

## S9. Nudges (after S3 and S4b)

- [ ] 9.1 Use the `activate-agent-led-vocabulary-evolution` work-item queue as the nudge channel: "you used X, the vault calls it Y" in the advisory slot; Dreamer upkeep families for recurring unregistered values, near-duplicate entries and per-registry debt.

## S10. Planning

- [ ] 10.1 Move Planning values into `_collection_types/planning.yaml` as enum values with `rank`, `parents`, `class` and `order` attributes, under a small Planning change.

## Closure

- [ ] 11.1 After S10 merges and every non-optional task above is evidenced as shipped, synchronize the deltas and archive this change with `openspec archive`; run `openspec validate --all --strict` before and after.
