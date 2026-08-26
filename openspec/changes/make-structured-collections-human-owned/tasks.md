## 1. Representation Contract and Pure Logic

- [x] 1.1 Add failing pure-logic tests for `item_filename` and `item_presentation` manifest validation, including forbidden mutable fields, natural-key eligibility, unknown versions, unsafe paths, invalid fields, dual Records recipes, and bounded output.
- [x] 1.2 Extract or reuse the ordinary-note filename sanitizer and implement deterministic structured-item filename rendering with Unicode/path-byte handling and collision suffixes.
- [x] 1.3 Implement shared representation recipe models and eager validation in `structured_collections.py` and the profile adapters without changing existing manifests.
- [x] 1.4 Add failing renderer tests for headings, labelled canonical values, long text, managed markers, authored-byte preservation, opaque external pointers, and non-disclosing typed-link resolution.
- [x] 1.5 Implement the deterministic shared managed-block renderer and profile-neutral authorized wikilink projection.

## 2. Mutation and Inspection Integration

- [x] 2.1 Add failing Planning and Records mutation tests proving new human paths, same-batch presentation refresh, rollback on render failure, and no automatic move after a filename-field update.
- [x] 2.2 Wire filename materialization and shared presentation refresh into Planning add/update/triage and Records append/update through the existing atomic writers and receipts.
- [x] 2.3 Add failing inspection tests for filename drift, collisions, missing/stale/unrenderable presentation, authored managed-block changes, and orphan markers when no recipe is active.
- [x] 2.4 Extend collection inspection to scan recognized markers independently of current recipes and return bounded shared representation diagnostics without mutation.
- [x] 2.5 Add failing manifest lifecycle tests for recipe removal and legacy-to-shared conversion, including byte-preserved authored Markdown and all-or-nothing cleanup.
- [x] 2.6 Implement transactional managed-block cleanup or replacement during guarded manifest revision and refuse any revision that would orphan an owned block.

## 3. Planning Manifest Lifecycle and Views

- [x] 3.1 Add failing command and governance tests for Planning create-mode/revision-mode validation, exact lifecycle guards, stale revision, exact rebaseline acknowledgements, reader floor, and audit publication.
- [x] 3.2 Refactor the existing Records manifest lifecycle leaf into shared collection mechanics and implement Planning `validate`, `revise`, and `rebaseline` with Planning-specific audit/error projection.
- [x] 3.3 Add failing Planning manifest tests for saved-view literals outside the canonical horizon vocabulary and for valid canonical horizon views.
- [x] 3.4 Implement profile-aware saved-view predicate validation without narrowing generic structured string fields.
- [x] 3.5 Update the generic Planning scaffold to declare safe title-based naming, readable presentation, and canonical horizon views; extend leak-guard and scaffold acceptance tests.

## 4. Previewed Structured-File Migration

- [x] 4.1 Add failing pure planner tests for deterministic preview identity, exact source snapshot, UUID-to-human moves, presentation rewrites/removals, path collisions, bounded output, and inverse receipt data.
- [x] 4.2 Add failing inbound-link tests for mutable governed rewrites and blockers from append-only, withheld, ambiguous, or otherwise unowned material.
- [x] 4.3 Implement the read-only one-collection structured-files planner using current manifest/profile validation, canonical item identity, governance-aware link resolution, and no side effects.
- [x] 4.4 Add failing apply tests for exact-plan guards, stale manifest/item/link refusal, concurrent writer serialization, atomic moves and rewrites, idempotent terminal replay, and complete rollback on failure.
- [x] 4.5 Implement guarded structured-files apply and audited inverse metadata through the existing same-vault writer and publication boundary.

## 5. Product Surfaces and Compatibility

- [x] 5.1 Update the canonical command registry and `plan_memory` signature/action matrix for `validate`, `revise`, and `rebaseline`; regenerate MCP, CLI, REST, OpenAPI, capability, and golden schema artifacts intentionally.
- [x] 5.2 Register `maintain_memory(mode="structured-files")` preview/apply classification and parameters once, with byte-consistent generated facades and refusal envelopes.
- [x] 5.3 Add compatibility tests proving unchanged behaviour for manifests without recipes and for existing `record_presentation` rendering plus child-query projection.
- [x] 5.4 Update scaffold skill guidance and command descriptions to teach identity-versus-filename, explicit rename preview/apply, managed-body ownership, and the Planning/Records boundary in generic language.

## 6. Acceptance and Delivery

- [x] 6.1 Add an end-to-end copied-vault fixture with UUID Planning and Records items, typed relationships, an orphan legacy block, view-vocabulary drift, collisions, and a blocked immutable inbound link.
- [x] 6.2 Prove preview is read-only, apply produces meaningful filenames/bodies and valid Obsidian wikilinks, inspection becomes healthy, and a second preview is empty on the migratable fixture.
- [x] 6.3 Run focused representation, Planning, Records, command-surface, governance, scaffold leak, and writer tests, then `ruff check`, the full embeddings-disabled pytest suite, and `openspec validate --all --strict`.
  - Verified on 2026-08-26 with the final tree: focused acceptance suites, four deterministic embeddings-disabled pytest shards, the isolated 20-test bounded-graph timing file, required Ruff checks, generated-artifact checks, public-artifact validation, and 168/168 strict OpenSpec validations passed.
- [ ] 6.4 Perform a manual Obsidian smoke test on a disposable migrated fixture to confirm readable file-tree labels, readable documents, working wikilinks, and graph edges; record the exact fixture and observations without changing a live vault.
  - The reproducible disposable fixture is built by `tests/test_structured_file_migration.py::_seed_acceptance_vault`; automated migration produced `Improve onboarding.md` and `Improve onboarding — 991acdd4.md`, readable managed bodies, and valid rewritten wikilinks. GUI acceptance remains pending because Windows application interop was unavailable from this WSL session.
