## Programme order

This change is the programme's one home. S1 shipped in foundation merge `e844290a16b298fa30edfefea5e38d9951d54973` (PR #1623, CI `37762518836`).
S2 guidance and S3 lifecycle consumers now share one meaning and form one cohesive delivery under `add-lifecycle-status-registry`.
Their source checkpoint does not claim generated-carrier, runtime or delivery completion.
Later slices remain sequenced here; S4 and S7 use their own OpenSpec changes, and Planning gets a small change when S10 starts.
After S1, the coupled S2/S3 batch, S5 and S10 can run in parallel; other dependencies remain unchanged.

Base `118f377ab04c595db26f13024a682f05aca31fc0` has the foundation merge's exact tree.
CI run `37762518836` reported success on that exact head.
The S1 boxes cite their proof below. Task 2.1 is delivered in PR #1630. Task 2.2 stays open. The S3 boxes stay open until PR #1630 merges.

## S1. Substrate, revert, generic contract, bootstrap section (shipped foundation)

Delivered in [PR #1623](https://github.com/Artexis10/exomem/pull/1623), merge `e844290a16b298fa30edfefea5e38d9951d54973`.
[CI run 37762518836](https://github.com/Artexis10/exomem/actions/runs/37762518836) passed on PR head `118f377ab04c595db26f13024a682f05aca31fc0`.

- [x] 1.1 Red first: through the MCP surface, promote an entity type, see it in `bootstrap(section="vocabulary")` with a count, use it on a page, restore the previous version, and see the page reported as unregistered debt with its bytes untouched.
- [x] 1.2 Red first: a stale `expected_hash` refusal; a resolved nonowner's v1 save becoming a pending item; a limited owner's permitted save without approval; a hand-edited overlay snapshotted on the next save.
- [x] 1.3 Add the parity check for entity types, relations and source kinds on an unchanged vault and on legacy overlays; delete it together with the Python constants it compares.
- [x] 1.4 Add `src/exomem/vocabulary/` with the registry spec, the generic loader, the stat-then-digest cache, the effective digest and the core packs; move `core-relations.yaml` into the pack directory.
- [x] 1.5 Make `entity_types`, `relation_registry`, `source_taxonomy` and `semantic_language_registry` read through the loader, keeping their public APIs.
- [x] 1.6 Commit every registry save and restore through `registry_history.commit`, with the principal hash in the snapshot header; seal the snapshot and log entry as derived auxiliaries for the v2 writer gate.
- [x] 1.7 Add `inspect`, `propose`, `save`, `history` and `restore` to `schema_memory` for the five subjects; keep the old operation names; change the tool description once; classify `propose` in the egress selector table.
- [x] 1.8 Separate existing owner/write authority from aggregate disclosure: v1 owner saves apply, resolved nonowner saves use the existing queue, and activated v2 retains its writer gate. Prove hosted RAW exemption and absent context grant no owner write authority; counts, reasons and private-dependent operations follow content admission.
- [x] 1.9 Serve usage counts from the graph snapshot and the lexical catalogue, `unavailable` when cold; replace the bootstrap `entities` section with `vocabulary` and the core entity-type list with a pointer.
- [x] 1.10 Invalidate the registry cache from the `_Schema/` watcher event.
- [x] 1.11 Add the repository rule to `CLAUDE.md`.
- [x] 1.12 Regenerate the skill contract, plugins, tool schemas, hosted candidate, cloud plugin and capability docs; run the registry, schema_memory, bootstrap, egress, contract and carrier gates, ruff, mypy, OpenSpec strict and the privacy gate.

## S2. Skills

Task 2.1 is delivered in [PR #1630](https://github.com/Artexis10/exomem/pull/1630).
The scaffold holds `references/vocabulary.md`; `SKILL.md` and the capture, ingest, curate and review skills link it.

- [x] 2.1 Add `references/vocabulary.md` and link it from SKILL.md and the capture, ingest, curate and review skills. Teach the loop: read the live set, reuse or alias, promote under a parent only when nothing fits, know it takes effect at once, report the promotion with its revert route.
- [ ] 2.2 Remove hard-coded vocabulary from the skills: the `COMPILED_TYPES` sentences in `semantic-authoring.md` and nine workflow skills, and the inactive-status list. Proof: skill-contract validation, the no-leak scan and the plugin sync. S4a (`add-note-type-registry`, task 1.10) delivers the `COMPILED_TYPES` sentence removal.

## S3. Lifecycle statuses (`add-lifecycle-status-registry`)

- [ ] 3.1 Add the status registry with `class` (live, pending, superseded, retired, abandoned) and turn the inactive, retired, history and find-penalty sets into predicates on it; record the planned-evidence recurrence change while preserving entity identity rules.
- [ ] 3.2 Keep an unregistered status live and report it as debt. Proof: register `abandoned` with its class and see `activate_context` stop serving that page as current; run the full suite.

## S4a. Note types, read side (`add-note-type-registry`)

Tasks 1.1 to 1.13 of [`add-note-type-registry`](../add-note-type-registry/tasks.md) carry S4a. `folder`, `time_bounded` and `sources` join S4a there.

- [ ] 4.1 Add the note-type registry with `role`, move the ranking multipliers into `ranking_config`, and move the semantic contract, audit, claims and the skills' role text onto role predicates. Proof: the suites pass unchanged; run the full suite.

## S4b. Note types, write side and promotion (`add-note-type-registry`)

Tasks 2.1 to 2.15 of [`add-note-type-registry`](../add-note-type-registry/tasks.md) carry S4b, including the type-key debt that S4a defers.

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
