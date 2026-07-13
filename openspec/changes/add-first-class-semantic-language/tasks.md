## 1. Semantic Unit Contract Tests

- [ ] 1.1 Add parser tests for Unicode category grammar/NFKC+casefold, canonical collisions, trailing-only tags, balanced/escaped context, terminal anchors, source spans, bullets outside `## Observations`, fences, `[ ]|[x]|[X]|[-]`, `[take: ]`, punctuation, and malformed candidates.
- [ ] 1.2 Add normalization/compatibility tests proving compact observations and rich semantic blocks share one result shape while category and governed kind remain distinct, preserve existing rich graph keys, and do not double-parse/store/rank legacy `semantic_blocks` projections.
- [ ] 1.3 Add identity tests for rich IDs, cross-form duplicate anchors, compact anchors, anonymous authored-signature fingerprints, registry-alias stability, stable-ID versus legacy path moves, semantic edits, source-ordered duplicates, and stale references.
- [ ] 1.4 Add registry tests for open unknown categories, raw/canonical identity, aliases, deprecation/replacement, scopes, custom rich kinds, conflicts, and deterministic proposal output.

## 2. Parser And Semantic Language Registry

- [ ] 2.1 Implement the focused semantic-unit data model and compact-observation parser without a model or side effect.
- [ ] 2.2 Compose compact observations with the existing semantic-block parser and emit deterministic structured diagnostics with path/span remediation.
- [ ] 2.3 Implement anchored and fingerprint-bound unit references over durable parent identity, with explicit move-unstable legacy path fallback and duplicate-occurrence invalidation behavior.
- [ ] 2.4 Add the generic scaffold semantic-language registry for category/kind extensions and implement load/validate/resolve behavior.
- [ ] 2.5 Extend `schema_memory(subject="categories")` with scoped frequency/example/alias proposals and reviewed hash-guarded persistence.
- [ ] 2.6 Replace independent semantic-block/relation reparsing in downstream read-only consumers with the shared parsed semantic document.

## 3. Memory Contracts And Relation Disposition

- [ ] 3.1 Add failing tests for per-rule multi-project contract resolution, specificity, compatible conjunction, warn/strict/off modes, category/kind requirements, unknown-category policy, equal-specificity conflicts, and strict pre-write rejection.
- [ ] 3.2 Add failing lifecycle tests for every exact qualifying-relation predicate branch, normal origins, the frontmatter+supersession-only exception, family exclusions, target resolution, fingerprint-bound reviewed-none, empty-corpus bootstrap expiry, invalid placeholder, generic-link exclusion, and provenance separation.
- [ ] 3.3 Extend saved memory contracts and infer/validate/diff output to semantic-unit kinds/categories with deterministic scope resolution.
- [ ] 3.4 Implement one pure semantic contract result over fields, units, categories, typed relations, schema findings, page lifecycle, and review disposition, with precommit-blocking and posthoc-nonblocking caller modes.
- [ ] 3.5 Add portable fingerprint-bound reviewed-none state plus validate-only `draft_id`/`draft_hash`/candidate output and atomic unchanged-draft commit/reuse guards.
- [ ] 3.6 Route remember/note, replace, edit, entity creation, Tier-2 compiled writes, and adoption compile through precommit evaluation; encode the move/delete/trash/other-KB/Sources/Evidence/watcher/reconcile applicability matrix.
- [ ] 3.7 Remove malformed `- (none yet)` generation and make empty Relations sections conditional on a valid non-edge disposition.
- [ ] 3.8 Grandfather existing pages into activation/review using stable finding keys and the mechanical after-errors-subset-of-before-errors rule while preventing invalidated accepted dispositions.

## 4. Derived Unit Indexes And Freshness

- [ ] 4.1 Add lexical-sidecar tests for parent-owned generation/source-hash rows, exact category/kind filters, same-content/different-category identity, and category-only queries.
- [ ] 4.2 Add optional embedding-sidecar tests for unit keys, parent generation/linkage, replacement/deletion, disabled imports, warming, and post-start failure fallback.
- [ ] 4.3 Add graph tests for generation-stamped compact/rich unit nodes, preserved rich node keys/relations, `derived_from` edges, and absence of inferred compact typed edges or duplicate rich nodes.
- [ ] 4.4 Implement semantic-unit record discriminators, shared deterministic parent generation/source hash/parser version, and parent-transactional replacement in the lexical sidecar.
- [ ] 4.5 Implement optional unit embedding upsert/delete/rebuild through the existing measurement-only embedding seam.
- [ ] 4.6 Extend epistemic graph indexing and schema migration for compact units and normalized shared parser output.
- [ ] 4.7 Update writer and watcher events for one-pass parse plus per-sidecar lexical/vector/graph refresh without duplicate self-write work; add query-time current-file generation validation that rejects stale/mixed joins.
- [ ] 4.8 Extend reconcile to detect/repair missing, stale, mixed-generation, orphaned, moved, trashed, and recovered unit rows while separately reporting contract drift.

## 5. First-Class Recall, Read, And Context

- [ ] 5.1 Add failing recall tests for `auto|page|unit|mixed`, byte-compatible default page recall, OR-within-list/AND-across-text-category-kind filtering, empty-query category lookup, parent caps, and truncation.
- [ ] 5.2 Define the semantic-unit hit and exact-read response models with parent citation, anchor/span/hash, lifecycle, ranking, and degradation fields.
- [ ] 5.3 Implement unit lexical/vector candidate lanes and metadata filters before ranking; keep default page ranking unchanged.
- [ ] 5.4 Implement page `matched_units`, independently ranked unit results, and bounded mixed fusion/grouping.
- [ ] 5.5 Extend `read_memory` to resolve exact anchored/fingerprint-bound unit references and return bounded parent context or explicit stale/ambiguous status.
- [ ] 5.6 Extend graph context to seed/filter compact and rich unit nodes by category/kind without inferring semantics.
- [ ] 5.7 Extend context packs with bounded cited semantic units, unit-seeded authored relations, provenance/lifecycle context, explicit truncation, and a nonduplicating legacy `semantic_blocks` projection.
- [ ] 5.8 Add embeddings-disabled and degraded-path tests proving lexical/category recall and context remain useful without loading a model.

## 6. Structured Unit Mutation And Lifecycle

- [ ] 6.1 Add failing `observe_memory` tests for add/update/remove/validate, compact/rich authoring, compact-relation rejection/remediation, expected-hash/unit-fingerprint guards, duplicate selection, and no-write validation.
- [ ] 6.2 Implement canonical compact/rich Markdown rendering and minimal span-aware edits that preserve unrelated formatting.
- [ ] 6.3 Implement `observe_memory` over writable compiled pages and shared atomic writer/contract/index hooks.
- [ ] 6.4 Enforce Sources/Evidence, outside-KB, read-only/excluded, superseded, and append-only boundaries on unit mutation.
- [ ] 6.5 Add create/draft-review/edit/move/trash/recover/delete tests proving atomic reviewed-none creation, changed-draft rejection, old category/text/path/index hit removal, stable-ID anchor survival, and legacy path-reference invalidation.
- [ ] 6.6 Add out-of-band watcher/reconcile tests proving user Markdown survives invalid edits, valid units remain indexed, and repaired state clears findings idempotently.
- [ ] 6.7 Add sidecar-failure tests proving committed Markdown is preserved and deterministic reconcile guidance/drift is returned.

## 7. Product Surfaces And Agent Language

- [ ] 7.1 Register `observe_memory`, creation draft-review fields, and all new recall/read/schema parameters once in the command registry with MCP write/read annotations and shared error codes.
- [ ] 7.2 Regenerate/verify MCP, REST, CLI, OpenAPI, capability docs, parameter parity, and response-schema fidelity snapshots.
- [ ] 7.3 Extend compact/full bootstrap contracts and the hand-authored generic skill scaffold with category-versus-kind, observation authoring, exact recall, canonical relation disposition, and review guidance.
- [ ] 7.4 Update semantic-block, schema, graph, search, adoption, and AI-assistant documentation with compact/rich examples and migration behavior.
- [ ] 7.5 Run scaffold leak checks and verify no private or competitor-specific token enters `src/exomem/`; keep direct comparison names in maintainer benchmark/docs only.

## 8. Adoption, Audit, And Existing Corpus Activation

- [ ] 8.1 Add read-only existing-vault census tests for compact observation coverage, category frequencies, false positives, malformed candidates, schema debt, and relation dispositions.
- [ ] 8.2 Extend adoption scan-only output with semantic-language census and safe next actions; never rewrite originals or require categories.
- [ ] 8.3 Implement the portable governed activation manifest that snapshots existing-page ID/hash baselines without page rewrites and survives rebuild/transfer.
- [ ] 8.4 Extend audit/attention/review with malformed-unit, category-governance, strict-schema-drift, and stale relation-disposition findings using stable fingerprints.
- [ ] 8.5 Add empty-vault, first-note, portable existing-vault activation-manifest, legacy-no-ID/move fallback, direct-editor/new-file, sync-conflict, rebuild/transfer, and registry-upgrade acceptance fixtures.
- [ ] 8.6 Verify default installs and upgrades do not fabricate observations, categories, relations, IDs, or review decisions.

## 9. Direct Basic Memory Scoped Outcome Benchmark

- [ ] 9.1 Extend the neutral benchmark manifest/renderers with compact observations, duplicate content/categories, schemas, mutations, provenance, lifecycle, expected unit results, pinned revisions, and immutable pre-run outcome/threshold definitions.
- [ ] 9.2 Extend the isolated Basic Memory adapter with full index, persistent MCP, category-only/text/hybrid recall, schema validate/write/sync/reindex probes, edit, move, delete, and raw-envelope capture.
- [ ] 9.3 Extend the Exomem adapter and normalizer with equivalent public-path cases and governed identity/provenance/lifecycle/context evidence.
- [ ] 9.4 Implement contender-neutral outcome dimensions, common no-regression thresholds, and governed differentiation evidence without a compensating weighted score.
- [ ] 9.5 Add renderer parity, mutation-safety, revision/config/corpus-hash, stale-row, latency/response-byte, unsupported, and execution-failure reporting.
- [ ] 9.6 Keep a fast Exomem-only fixture gate in the normal suite and document the optional desk-side direct command against a sibling checkout.
- [ ] 9.7 Run the direct benchmark against the pinned sibling revision and fix or explicitly document every failed/unsupported criterion before claiming only a scoped, revision-bound semantic-governance advantage.

## 10. Verification And Delivery

- [ ] 10.1 Run focused parser/registry/contract/index/recall/context/mutation/surface/adoption/benchmark tests with embeddings disabled.
- [ ] 10.2 Run schema validation, OpenSpec validation, capability generation checks, OpenAPI/MCP fidelity, scaffold leak tests, and Ruff on every changed Python file.
- [ ] 10.3 Run the complete lean suite with embeddings disabled and record totals plus any unrelated baseline failures.
- [ ] 10.4 Run targeted desk-side semantic/hybrid and direct-contender benchmarks on a quiesced machine and record revisions/config/results.
- [ ] 10.5 Have an independent reviewer verify implementation against every OpenSpec scenario and inspect migration, mutation safety, backward compatibility, generation freshness, and claim scope.
- [ ] 10.6 Update the product gap matrix and graph/semantic comparison docs only from recorded benchmark evidence; do not claim unscoped overall product superiority.
- [ ] 10.7 Capture the durable final decision/results in Exomem with typed relations to the earlier comparison and semantic-block registry notes.
