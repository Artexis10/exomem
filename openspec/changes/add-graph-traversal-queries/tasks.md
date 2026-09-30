## 0. Rulings, interfaces and baseline

All implementation tasks remain unchecked; this lane drafts artifacts only. G1–G3 can run dark after collection Q1/parent P1a interfaces; G4 requires a proven canonical store; public G5 waits for collection Q5 and parent P4/P5. G6 is the final graph release gate, coordinated with collection Q6 rather than a PR split solely for lanes. Numerical targets and open-question recommendations are provisional release gates, not measured achievements.

- [ ] 0.1 Record rulings on the eight graph design open questions and collection cross-references; verify coherent proposal/design/spec/tasks with strict OpenSpec validation.
- [ ] 0.2 Verify shared admitted IR/adapter/query leaf, collection identity/generation, schema/replay/lease and authored-currency interfaces from merged source; record exact dependency evidence, never assume checked boxes mean shipped.
- [ ] 0.3 Freeze invented graph seed/shape, independent simple-path/ontology/released-view oracle, current graph/activation failure baseline and pinned Neo4j version/edition/driver/index/transport settings; verify comparable workloads before latency claims.

## 1. G1 — Stable dynamic ontology and admitted graph IR

- [ ] 1.1 Red: add transitive subtype/exact-only/category/entity-kind/definition tests, undefined open category, alias/rename/merge/incompatible view tests, cycles, current-version queries and withheld hierarchy/definition twins; verify string-only/direct-parent behavior fails recursive/stable-ID contracts.
- [ ] 1.2 Implement vocabulary identity/version/alias/DAG closure projection from governed sources and stable query/view binding; verify 1.1 passes and old authored labels/history remain intact without a second writer.
- [ ] 1.3 Red: add closed Traverse/Path/Pattern IR validation, cross-vault/raw SQL/Cypher/arbitrary keys, typed directions, admitted node/edge/author/evidence obligations and unavailable fake-backend tests; verify no unsupported operation can bypass admission.
- [ ] 1.4 Implement graph IR normalization/admission and source bindings with safe unknown estimates; verify 1.3 passes using the shared executor and no new policy language/model.
- [ ] 1.5 **G1 gate:** run scoped vocabulary/IR/reference/hidden-absent suites; prove transitive supports subtype coverage, stable saved-view renames and immediate acknowledged vocabulary query/explicit warming through current version semantics.

## 2. G2 — Recursive typed traversal and declared row/page links

- [ ] 2.1 Red: add independent directed/symmetric 0..5-hop reference tests, withheld seed/intermediary/edge/author/evidence, source scope, parallel edges, cycles, unknown anchors and declared mixed links; verify post-filtered frontier/count/preview behavior fails twins.
- [ ] 2.2 Compile recursive CTEs over admitted node/edge relations, stable ontology filters and current declared virtual links; maintain collection-row/link edges in the canonical transaction; verify 2.1 passes without arbitrary join keys or full-graph Python materialization.
- [ ] 2.3 Red: add released degree/visit/state/path/result/byte caps and conservative distinct-versus-repeated preview estimates on reconverging layered diamonds, cursor visible basis/tamper/cache, cold unknown estimates, timeout/cancel/connection reuse and indexed adjacency plan tests; verify exhaustive results cannot look complete after truncation.
- [ ] 2.4 Implement 5-hop/64-default/256-hard fan-out, 10k nodes/50k edges/10k states/1k paths, 200 ms/16 MiB RSS/64 MiB temp envelope, safe preview and partial neighbourhood/refusal distinctions; verify 2.3 passes including every-hop admission cleanup.
- [ ] 2.5 **G2 gate:** run scoped traversal/link/resource/twin suites, assert both typed adjacency indexes and no avoidable base scan, measure the 100k/500k 3-hop ≤50 ms p95 and record cold mixed admission/refusal separately.

## 3. G3 — Shortest/all paths, patterns, reductions and evidence

- [ ] 3.1 Red: add simple shortest-path/equal tie/identity/unreachable/directed reference tests, exhaustive all-path k≤5 enumeration with cycles/parallel edges, incomplete shortest proof and path-cap cases; verify no sampled result can claim exact shortest/all-path completeness.
- [ ] 3.2 Implement breadth-depth admitted shortest/simple-path enumeration with stable refs and keyset path paging; verify 3.1 and hidden/absent count/cursor twins pass.
- [ ] 3.3 Red: add supports/contradicts pattern, 8-variable/8-edge validation, reused identity, optional match, mixed-domain binding and count/distinct/length/group oracle cases; verify reduction happens only over complete admitted enumeration.
- [ ] 3.4 Compile bounded anchored patterns and exact path aggregates through the shared leaf; verify 3.3 passes without changing ordinary relational join multiplicity.
- [ ] 3.5 Red: add ordered explanation/source/hash/definition/evidence/currency tests and withheld evidence obligations; verify path interpretation cannot invent proof, confidence or hidden relation details.
- [ ] 3.6 Implement quotable path explanation and agent-repairable errors/explain; verify 3.5 passes with authored/raw versus current vocabulary clearly identified.
- [ ] 3.7 **G3 gate:** run scoped path/pattern/aggregate/explanation suites plus independent reference and all visibility twins; require exact results or explicit refusal for exhaustive/shortest work.

## 4. G4 — Consolidated store migration and replay safety

- [ ] 4.1 Red: add graph-only rebuild/current-source/registry/parser/dependency/checkpoint parity, failed staging/cutover, concurrent collection write, combined-store file replacement prohibition and canonical head/audit invariance tests; verify old sidecar replacement cannot be reused against the canonical store.
- [ ] 4.2 Implement projection namespace, bounded staging/copy, adjacency/vocabulary mappings and fenced graph-generation publication in the per-vault store; verify 4.1 passes without canonical table resets or whole-store replacement.
- [ ] 4.3 Red: add file-write-before-projection crash, queued replay/poison/quarantine/current epoch, row-link same-transaction rollback, reader lease, takeover, snapshot portability and legacy rollback proof tests; verify stale graph cannot serve complete results.
- [ ] 4.4 Integrate existing source receipt/replay/availability and service single-writer ownership with the combined placement; verify 4.3 passes, collection updates remain available and failed graph work abstains honestly.
- [ ] 4.5 **G4 gate:** run scoped migration/replay/lease/backup/resource suites and existing convergence contracts; prove ≤64 MiB migration additional RSS, unchanged canonical data/head on graph-only rebuild and no separate live edge store after acknowledged cutover.

## 5. G5 — Agent reachability and graph-aware context units

- [ ] 5.1 Red: add installed-MCP compose/explain/preview/dry-run/execute/refinement/saved mixed-view tests, on-demand bootstrap/skill discovery, vocabulary write→acknowledged query, ontology category define/rename/merge diff/save source guards and family delegation, and schema/core/frozen-adapter bytes; verify every graph/path/ontology feature fails delivery if its tool route is absent.
- [ ] 5.2 Extend existing connect_memory operation, schema_memory ontology diff/save through governed sources/family writers, and shared lifecycle/view surfaces, add generic graph/ontology skill sections and repair guidance; verify 5.1 passes with no new tool names or exposed SQL/Cypher.
- [ ] 5.3 Red: add anchor→supersedes/supports/contradicts/mixed-link working-set turns, authored-currency/#1453 branches/cycles/mtime/hidden successor twins, source/path provenance, exact cache invalidation, cold/stale abstention and 30 ms stage/3-hop/8-fanout/64-node/128-edge/2-path/byte caps; verify missing graph expansion and false currency fail before integration.
- [ ] 5.4 Integrate bounded graph expansion in collection Q8's shared stage/cache/packet contract; verify 5.3 passes, graph abstention preserves independent fresh row units and no stale projection or hidden successor changes currentness.
- [ ] 5.5 **G5 gate:** run scoped MCP/skill/bootstrap/activation/cache/currency suites, installed wheel journeys and ≥30 path-derived turns; require ≥15 percentage-point correct unit gain, ≤1% irrelevant units, zero disclosure/false-zero, unchanged continuity/corpus and <1 s activation p95/≤30 ms stage p95.

## 6. G6 — Comparative, scale and integrated release gate

- [ ] 6.1 Red: freeze versioned Neo4j capability/matched-query and real-MCP agent benchmark acceptance, including algorithm omissions and unmeasured baseline refusal; verify missing agent routes or required latency/correctness failures block “better” claims.
- [ ] 6.2 Run the pinned invented 100k/500k ungoverned and application-governed Neo4j/Exomem matrix, 20 warmups/≥200 samples/3 runs, and 1m/5m/10m scale probes; verify every matched personal multi-hop/path/pattern p95 beats or matches baseline, headline ≤50 ms, ≥99% completion and explicit future-tier limits.
- [ ] 6.3 Run ≥60 held-out graph/mixed/ontology real-MCP agent tasks and shared import/view/error recovery cases; verify ≥95% correct, ≥90% first query/repair, zero governance failures and ≥15-point provenance/currency/context gain over bare Neo4j with equal-or-better application-equivalent quality.
- [ ] 6.4 **G6 gate:** independently review the integrated diff for every-hop release, evidence/currency, ontology merges, canonical-store rebuild safety and honest parity claims; run full repository checks, OpenSpec/privacy/non-frozen contract/wheel gates, all reference/twin/MCP/activation/benchmark gates and parent collection write/portability regressions.
- [ ] 6.5 Release capability-versioned graph preview through the repository process only after gates; verify fresh/migrated/portable stores, actual agent explain/path journeys, rollback/currentness and collection writes, recording measured evidence without private source data.
- [ ] 6.6 Once merge/release evidence proves non-optional tasks complete, sync deltas and archive with openspec archive in the same delivery; validate --all --strict before/after and preserve newer canonical requirements. Never archive this design-only draft or mark future tasks done.
