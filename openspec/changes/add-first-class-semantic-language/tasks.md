## 1. Semantic Unit Contract Tests

- [x] 1.1 Add parser tests for Unicode category grammar/NFKC+casefold, canonical collisions, trailing-only tags, balanced/escaped context, terminal anchors, source spans, bullets outside `## Observations`, fences, `[ ]|[x]|[X]|[-]`, `[take: ]`, punctuation, and malformed candidates.
- [ ] 1.2 Add normalization/compatibility tests proving compact observations and rich semantic blocks share one result shape while category and governed kind remain distinct, preserve existing rich graph keys, and do not double-parse/store/rank legacy `semantic_blocks` projections.
- [ ] 1.3 Add identity tests for rich IDs, cross-form duplicate anchors, compact anchors, anonymous authored-signature fingerprints, registry-alias stability, stable-ID versus legacy path moves, semantic edits, source-ordered duplicates, and stale references.
- [ ] 1.4 Add registry tests for open unknown categories, raw/canonical identity, aliases, deprecation/replacement, scopes, custom rich kinds, conflicts, and deterministic proposal output.

## 2. Parser And Semantic Language Registry

- [x] 2.1 Implement the focused semantic-unit data model and compact-observation parser without a model or side effect.
- [x] 2.2 Compose compact observations with the existing semantic-block parser and emit deterministic structured diagnostics with path/span remediation.
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

## 5. Structured Retrieval Filters

- [ ] 5.1 Add failing typed-AST tests for reserved `page.*` fields, runtime mapping-only RFC-6901 frontmatter traversal, and the closed `unit.*` field set, including pointer escaping, numeric mapping keys versus array nonmatch, missing-versus-null, heterogeneous scalar/terminal-array/null/mapping values, dates, numbers, booleans, and canonical category resolution.
- [ ] 5.2 Add operator tests for scalar/null-only exact-type `$eq|$ne`, terminal-array `$in|$all|$contains`, string `$contains`, `$exists|$gt|$gte|$lt|$lte|$between`, same-field conjunction, duplicate/order invariance, `$and|$or|$not`, missing/null/incompatible-type semantics, non-empty operands, inclusive bounds, UTC date-time normalization, invalid arity/type, unknown fields/operators, no coercion, depth four, 32 clauses, 16-KiB raw/structural/resolved plans, pointer/string/numeric lengths, per-list 64, and combined generic-plus-shortcut value 256 limits.
- [ ] 5.3 Implement the pure parser/validator/normalizer for the bounded filter expression with stable JSON-path errors and no SQL/regex/executable escape hatch.
- [ ] 5.4 Compile existing type/project/tag/speaker/file-type/date/category/kind shortcuts into the same normalized plan while preserving OR-within-list and AND-across-axis behavior and enforcing per-list/value/combined-plan bounds before deduplication or alias resolution.
- [ ] 5.5 Implement page and unit predicate evaluation before ranking, same-unit grouping for page results, unit-predicate `auto` resolution, and filter-only filtered-most-recent behavior.
- [ ] 5.6 Add conformance fixtures proving identical eligibility across keyword, BM25, vector, hybrid, graph-enriched, SQLite, and every optional backend for heterogeneous scalar/array/null/missing/mapping values, exact-type equality/inequality, array operators, numeric mapping keys, and continued-array-traversal nonmatch; fail rather than silently broaden when parity is impossible.
- [ ] 5.7 Add adversarial surface/backend tests for nested metadata, Unicode values, explicit null, absent keys, strings that resemble dates/numbers, each independent generic and shortcut resource limit (including one huge leaf/list and duplicates counted raw), injection payloads, pre-candidate rejection, bounded explanation echo, and category alias conflicts.

## 6. First-Class Recall, Explanation, Read, And Context

- [ ] 6.1 Add failing recall tests for `auto|page|unit|mixed`, byte-compatible default page recall, OR-within-list/AND-across text-category-kind-structured-filter axes, empty-query filter lookup, parent caps, and truncation.
- [ ] 6.2 Define page/unit hit and exact-read response models with parent citation, anchor/span/hash, lifecycle, ranking, degradation, and optional explanation fields.
- [ ] 6.3 Implement unit lexical/vector candidate lanes and metadata filters before ranking; keep default page ranking unchanged.
- [ ] 6.4 Implement page `matched_units`, independently ranked unit results, and bounded mixed fusion/grouping.
- [ ] 6.5 Preserve raw BM25 backend scores through candidate collection; preserve vector/CLIP cosine, keyword/graph/temporal ranks, fusion inputs/contributions, boost factors, reranker values, and final sortable values without overloading one score.
- [ ] 6.6 Add failing `explain=false|true` tests for byte-compatible defaults, compact no-content-leak behavior, top-level retrieval plan, effective filters/result level, lane availability/degradation/nonparticipation, filter-only/single-lane no-fusion behavior, actual sort tuples/tie-breaks, and bounded response size.
- [ ] 6.7 Implement versioned `retrieval_profile` with intent, requested/effective modes, normalized filters, lanes/reasons/weights, backend/model/metric direction/range/rounding, fusion constants, rerank decision, compute context needed for interpretation, and final-order/tie-break policy.
- [ ] 6.8 Implement per-hit explanations with only participating lanes, metric-labelled ranks/values, exact RRF math only when fusion runs, graph provenance, actual single-lane/filter-only sort tuples, ordered multipliers/reranking/tie-break chain, and final rank; keep unavailable/nonparticipating lanes top-level rather than fabricating hit entries or zero.
- [ ] 6.9 Add isolated-lane fidelity tests proving public hybrid explanations reproduce candidate membership, ranks, fusion contributions, degradation, and final ordering within documented rounding.
- [ ] 6.10 Extend `read_memory` to resolve exact anchored/fingerprint-bound unit references and return bounded parent context or explicit stale/ambiguous status.
- [ ] 6.11 Extend graph context to seed/filter compact and rich unit nodes by category/kind without inferring semantics.
- [ ] 6.12 Extend context packs with bounded cited semantic units, unit-seeded authored relations, provenance/lifecycle context, explicit truncation, and a nonduplicating legacy `semantic_blocks` projection.
- [ ] 6.13 Add embeddings-disabled and degraded-path tests proving lexical/category/filter recall, explanations, and context remain useful without loading a model.

## 7. Structured Unit Mutation And Lifecycle

- [ ] 7.1 Add failing `observe_memory` tests for add/update/remove/validate, compact/rich authoring, compact-relation rejection/remediation, expected-hash/unit-fingerprint guards, duplicate selection, and no-write validation.
- [ ] 7.2 Implement canonical compact/rich Markdown rendering and minimal span-aware edits that preserve unrelated formatting.
- [ ] 7.3 Implement `observe_memory` over writable compiled pages and shared atomic writer/contract/index hooks.
- [ ] 7.4 Enforce Sources/Evidence, outside-KB, read-only/excluded, superseded, and append-only boundaries on unit mutation.
- [ ] 7.5 Add create/draft-review/edit/move/trash/recover/delete tests proving atomic reviewed-none creation, changed-draft rejection, old category/text/path/index hit removal, stable-ID anchor survival, and legacy path-reference invalidation.
- [ ] 7.6 Add out-of-band watcher/reconcile tests proving user Markdown survives invalid edits, valid units remain indexed, and repaired state clears findings idempotently.
- [ ] 7.7 Add sidecar-failure tests proving committed Markdown is preserved and deterministic reconcile guidance/drift is returned.

## 8. Product Surfaces And Agent Language

- [ ] 8.1 Register `observe_memory`, creation draft-review fields, `filters`, `explain`, result levels, category/kind shortcuts, and all new recall/read/schema fields once in the command registry with MCP annotations and shared error codes.
- [ ] 8.2 Regenerate/verify MCP, REST, CLI, OpenAPI, capability docs, parameter parity, structured-filter schemas, explanation schemas, and response fidelity snapshots.
- [ ] 8.3 Extend compact/full bootstrap contracts and the hand-authored generic skill scaffold with category-versus-kind, observation authoring, page/unit filters, filter-only retrieval, score interpretation, canonical relation disposition, and review guidance.
- [ ] 8.4 Update semantic-block, schema, graph, search, adoption, and AI-assistant documentation with compact/rich examples, the filter expression, explained retrieval examples, score caveats, and migration behavior.
- [ ] 8.5 Run scaffold leak checks and verify no private or competitor-specific token enters `src/exomem/`; keep direct comparison names in maintainer benchmark/docs only.

## 9. Adoption, Audit, And Existing Corpus Activation

- [ ] 9.1 Add read-only existing-vault census tests for compact observation coverage, category frequencies, false positives, malformed candidates, schema debt, and relation dispositions.
- [ ] 9.2 Extend adoption scan-only output with semantic-language census and safe next actions; never rewrite originals or require categories.
- [ ] 9.3 Implement the portable governed activation manifest that snapshots existing-page ID/hash baselines without page rewrites and survives rebuild/transfer.
- [ ] 9.4 Extend audit/attention/review with malformed-unit, category-governance, strict-schema-drift, and stale relation-disposition findings using stable fingerprints.
- [ ] 9.5 Add empty-vault, first-note, portable existing-vault activation-manifest, legacy-no-ID/move fallback, direct-editor/new-file, sync-conflict, rebuild/transfer, and registry-upgrade acceptance fixtures.
- [ ] 9.6 Verify default installs and upgrades do not fabricate observations, categories, relations, IDs, or review decisions.

## 10. Layered Basic Memory Local-Core Benchmark

- [ ] 10.1 Replace the narrow semantic manifest with a versioned local-core capability inventory reconciled against Exomem's command registry and each pinned contender's runtime MCP/public-CLI inventory, failing on unclassified operations and requiring every supported in-scope capability to have an executed public-path probe backed by a deterministic fixture; only verified unsupported or justified exclusion may replace execution.
- [ ] 10.2 Extend neutral fixtures/native renderers with notes/entities, observations/categories, duplicate content, tags/context, nested metadata, schemas, typed/directional multi-hop relations, provenance, lifecycle, distractors, mutations, datasets, and deterministic media-extension artifacts.
- [ ] 10.3 Add a benchmark-managed pinned Basic Memory environment setup/verification path that records revision/dependency/config hashes plus resolved embedding/reranker model revisions/artifact hashes, backend/device/dtype/quantization/runtime versions and supported seeds, and never uses its global home, database, cache, project, or a live vault.
- [ ] 10.4 Extend both adapters to perform full indexing and use one persistent public MCP session for agent-facing cases; classify and label any genuinely CLI-only maintenance probe without treating it as MCP parity, and capture raw envelopes, resolved model/artifact/backend/device metadata, cold/warm latency, response bytes, index duration, and before/after state evidence.
- [ ] 10.5 Add shared authoring/read/update, title/permalink/exact, rare-token/phrase/stemming/full-text, no-overlap semantic, adversarial hybrid, structured-filter, filter-only, graph depth/direction, and bounded-context cases with predeclared identities/order constraints.
- [ ] 10.6 Add isolated BM25/vector/keyword/graph/CLIP/temporal probes plus public hybrid probes and verify backend/model identities, metric direction/range, raw measurements, fusion math, boosts, reranking, degradation, deterministic tie-breaks, and final ordering from recorded evidence.
- [ ] 10.7 Add schema infer/diff/validate/save, invalid public write, direct filesystem edit, watcher/reconcile/full-reindex, edit/move/delete/recovery, history/supersession, stale-row, mixed-generation, and content-preservation cases.
- [ ] 10.8 Add separate Exomem extension probes for durable refs, Sources/Evidence provenance, governed note/block relations, semantic units, review/audit/adoption/reconcile, context packs, dataset query, and capability-gated PDF/image/audio/video ingestion/search/read.
- [ ] 10.9 Implement controlled cold/warm performance sampling with host/compute/resolved-model-artifact/backend/device/dtype/runtime/cache fingerprints, supported seeds, predeclared numeric/order tolerances, warmups, repeated counterbalanced order, timeouts, median/p95 latency, index duration, response bytes/context size, and immutable paired non-inferiority bands.
- [ ] 10.10 Implement independent `shared_core`, `lifecycle_integrity`, `explanation_truth`, `performance_envelope`, and `exomem_extensions` reports with immutable thresholds and no weighted aggregate.
- [ ] 10.11 Implement the revision/corpus-bound local-core-advantage gate: valid preflight and completed required probes for both contenders; every required Exomem shared case/outcome green; shared unsupported/mutual failure counts as not passed; case-level no-regression wherever Basic Memory passes; paired performance thresholds and every required advertised full-profile extension green; and at least one proved public extension absent from Basic Memory. Harness/setup/adapter/environment failure invalidates the claim, coarse ratios cannot hide case failures, lean missing-extras runs cannot claim the full gate, and no result generalizes to hosting or overall superiority.
- [ ] 10.12 Keep a fast Exomem-only fixture gate in the normal suite and document the explicit desk-side direct command, unavailable-sibling behavior, environment setup, quiesced-machine protocol, and raw artifact locations.
- [ ] 10.13 Run the direct benchmark against the pinned sibling revision and fix or explicitly record every shared-core gap, unsupported capability, environment difference, and threshold failure before making only the permitted recorded claim.

## 11. Verification And Delivery

- [ ] 11.1 Run focused parser/registry/contract/index/filter/recall/explanation/context/mutation/surface/adoption/benchmark tests with embeddings disabled.
- [ ] 11.2 Run schema validation, OpenSpec validation, capability generation checks, OpenAPI/MCP fidelity, scaffold leak tests, and Ruff on every changed Python file.
- [ ] 11.3 Run the complete lean suite with embeddings disabled and record totals plus any unrelated baseline failures.
- [ ] 11.4 Run targeted desk-side semantic/hybrid, media-extension, and direct-contender benchmarks on a quiesced machine and record revisions/config/results/raw envelopes.
- [ ] 11.5 Have an independent reviewer verify implementation against every OpenSpec scenario and inspect filter safety/parity, score truth, migration, mutation safety, backward compatibility, generation freshness, benchmark completeness, and claim scope.
- [ ] 11.6 Update the product gap matrix and graph/semantic comparison docs only from recorded benchmark evidence; do not claim unscoped overall product superiority.
- [ ] 11.7 Capture the durable final decision/results in Exomem with typed relations to the earlier comparison and semantic-block registry notes.
