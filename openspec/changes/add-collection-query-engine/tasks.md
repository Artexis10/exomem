## 0. Owner rulings and prerequisite gate

The recommendations in `design.md` are proposed defaults. This change is design-only until the orchestrator records the owner's rulings here; an amendment updates proposal/design/spec/tasks together. Q1–Q4 run alongside parent P2–P4 after P1a. Q5 waits for parent P4/P5; this change does not take over their implementation or A11 deferrals. Implementation tasks below are future work and intentionally unchecked.

- [ ] 0.1 Record rulings on the eight design open questions (index/write limits, typed semantics, UTC dates, join cardinality, exact percentile, cost/RAM, FTS order and schema bytes); verify every amendment is reflected in the spec and affected tasks with `openspec validate add-collection-query-engine --strict`.
- [ ] 0.2 Verify parent P1a store, built-in registry, row governance and P1a.11 corpus are merged; record merge/test evidence and the exact registry/query interfaces available, without assuming parent P4 authoring is shipped.
- [ ] 0.3 Capture baseline parent §12/A7 query/write latency, WAL bytes and scoped query/facade test failures on invented fixtures; verify a persisted benchmark report names versions, host, seeds and scope so later ratios and failure diffs have a baseline.

## 1. Q1 — Query contract, admission and legacy parity (dark)

- [ ] 1.1 Red: add pure validation/reference tests for v1 AST, typed fields, unknown keys, SQL/injection-shaped values, input byte/depth/leaf caps and legacy/object conflicts; verify each fails because v1 validation is absent before implementation.
- [ ] 1.2 Implement the versioned AST validator and declaration binding without surface wiring; verify the scoped pure-logic tests from 1.1 pass and no SQL fragment/string expression is executable.
- [ ] 1.3 Red: extend P1a.11's invented corpus for every existing operator (including `missing`), tolerant numeric parsing, Unicode lowercase/prefix, membership identity, nested fields, ties, date bounds, all aggregates including group/profile/latest, envelopes and rendering; verify the SQL implementation fails parity before an operator is admitted.
- [ ] 1.4 Implement the legacy adapter and versioned deterministic compatibility primitives; verify equal Python/SQL rows, order, totals, aggregate shapes and bounded rendering for both built-ins and declared-type fixtures, and verify the per-operator push-down allowlist requires a passing test.
- [ ] 1.5 Red: add rows-absent twins, SQL-side admission, identity-before-value reads, default-deny types and policy invalidation tests; verify filtered rows cannot affect any output before enabling v1 queries.
- [ ] 1.6 Compile uniform predicates and streamed mixed-release identity membership from the once-resolved evaluator into the query; verify 1.5 passes and no post-query authorization filter is needed to correct a reduction.
- [ ] 1.7 Red: add deadline/progress-handler, cancellation, cost estimate, 8 MiB page cache, reader concurrency, temp cap and reader-reuse cleanup tests; verify denied work cannot return a complete-looking result or preserve another audience's authorization state.
- [ ] 1.8 Implement bounded preparation/execution, ≤128-row streaming fetches, 200 ms deadline, progress callbacks, 64 MiB disk-temp cap and bounded release cache; verify 1.7 passes and the memory probe finds no full-collection Python row or numeric list.
- [ ] 1.9 **Q1 gate:** run the scoped AST/compiler/compatibility/governance/executor suites, parent P1a.11 parity and failure-name diff against 0.3; verify preview remains default-off, dataset behavior is unchanged and existing store-mode query contracts still pass.

## 2. Q2 — Declared indexes, typed row queries and pagination

- [ ] 2.1 Red: add declaration tests for filterable/sortable paths, composite orders, duplicate flag coalescing, 8-index/16-path/4-key caps, relation dependencies and generated-object injection; verify over-budget or dependency-breaking proposals cannot change schema.
- [ ] 2.2 Implement declaration normalization, impact preview and migration-owned collection-local generated-column tables/indexes using internal ordinal keys; verify 2.1 passes and user field/path strings never enter executable DDL.
- [ ] 2.3 Red: add backfill, atomic mapping publication, concurrent writes, rollback/restart, index drop/revise and unrelated-collection write tests; verify stale/missing projections cannot silently serve new queries.
- [ ] 2.4 Implement bounded migration build/catch-up/cutover and transaction-coupled projection maintenance through all generic writer paths; verify 2.3 passes and old ready queries survive a failed build without canonical or audit changes.
- [ ] 2.5 Red: add typed comparisons, between inclusivity, missing/null/empty, UTC date math, month clamp, invalid instants and frozen-time tests; verify the independent reference semantics before compiler optimization.
- [ ] 2.6 Implement typed predicates, presence/type keys and bound frozen date values; verify 2.5 and legacy parity pass without silently applying tolerant legacy coercion to v1.
- [ ] 2.7 Red: add multi-key sort, explicit null ranks, projection, unique tie-breakers, cursor tamper/audience/query/schema binding, visible changes, hidden-only changes and byte-cap resumption tests; verify no skip, duplicate or hidden-state continuation leak.
- [ ] 2.8 Implement keyset cursors, bounded visible-state basis and projection/response encoding; verify 2.7 passes, oversized single results fail without a looping cursor and every SQLite read transaction closes at page end.
- [ ] 2.9 **Q2 gate:** run scoped declaration/migration/typed query/cursor suites; assert EXPLAIN uses declared filter/sort indexes without base scans or avoidable ORDER BY temp trees; measure invented 100k-row page-50 p95 ≤10 ms uniform/≤25 ms warm mixed and check memory, parent N=10k query gates and 4-index write/WAL ratios.

## 3. Q3 — Grouped analysis and declared joins

- [ ] 3.1 Red: add reference tests for four group keys, multiple named aggregates, empty reductions, distinct-count, null/missing groups, UTC day/ISO-week/month buckets, HAVING and group pagination; verify 1,001 pre-HAVING groups refuse instead of returning a partial reduction.
- [ ] 3.2 Compile grouping/HAVING and bounded multi-aggregate results over admitted rows; verify 3.1 passes and grouped values/totals equal the independent reference on uniform and rows-absent twins.
- [ ] 3.3 Red: add exact percentile interpolation tests (including empty, singleton, duplicates, p endpoints and invalid p), disk-spill and timeout tests; verify 100k numeric values are never collected into a Python array.
- [ ] 3.4 Implement extension-independent exact percentile via bounded disk-backed order and deterministic streaming reduction; verify 3.3 passes, errors return no partial percentile and legacy aggregates retain parity.
- [ ] 3.5 Red: add relation declarations/bindings, current-item lookup, target-type mismatch, cross-vault/arbitrary keys, cycles, hop/edge caps and reverse fan-out refusal tests; verify validation is closed before target values are read.
- [ ] 3.6 Implement declared one-to-one/many-to-one relation compilation, named lookup indexes within budget and inner/left joins; verify 3.5 passes using declared registry fixtures without needing P4's public authoring API.
- [ ] 3.7 Red: add withheld/missing target row twins, withheld/missing target collection refusal twins, right-side user predicates, joined grouping/counts/cursors and duplicate source-link tests; verify left-join admission is applied before target projection and does not erase visible left rows.
- [ ] 3.8 Bind governance to every SQL alias and retain existing egress receipts; verify 3.7 passes, joins preserve their declared multiplicity and no hidden existence/count/diagnostic enters a result.
- [ ] 3.9 **Q3 gate:** run scoped analysis/percentile/link suites and the expanded parity/twin corpus; assert indexed relation lookup plans and benchmark 100k-row grouping ≤50 ms uniform/≤75 ms warm mixed, percentile ≤150 ms, two-hop pages ≤25 ms and the resource budgets.

## 4. Q4 — Optional collection full text

- [ ] 4.1 Red: add FTS declaration/build-capability tests, 1-index/8-field cap, default-off/unready states and literal quote/operator/punctuation cases; verify unsupported text returns unavailable without scanning or breaking non-text queries.
- [ ] 4.2 Implement migration-owned collection-local FTS5 tables, pinned unicode61 settings and a structured literal MATCH encoder; verify 4.1 passes and text/schema inputs are never interpolated into SQL.
- [ ] 4.3 Red: add transactional update/edit-back/bulk/migration/rollback tests and governed text+scalar filter/count/group/cursor twins; verify held/history text is excluded and hidden-only text changes cannot affect field ordering.
- [ ] 4.4 Wire FTS maintenance into existing canonical transactions and combine MATCH with admitted relations; verify 4.3 passes and results contain no global FTS score, snippet, statistics or unselected text.
- [ ] 4.5 **Q4 gate:** run scoped FTS tests and tokenizer reference corpus; assert bounded FTS plans, benchmark text+filter/page-50 ≤25 ms at 100k and prove the 8-index+FTS write/WAL ratios plus unchanged parent absolute write/bulk gates.

## 5. Q5 — Shared tool contract, saved views and compiler consumers

- [ ] 5.1 Verify parent P4 type validation and P5 declared-type facade are merged; record evidence before publishing new declaration fields or public declared-type queries, preserving parent A11 deferrals.
- [ ] 5.2 Red: add facade/collection-mode `query_data`/CLI/REST contract tests, legacy-object conflict tests, wrong-profile refusal and dataset-mode compatibility; verify the same query reaches one leaf and frozen hosted candidates remain byte-identical.
- [ ] 5.3 Wire one optional query object and on-demand describe grammar to existing surfaces; verify 5.2 passes, record schema-byte deltas and refresh only local/non-frozen generated surfaces and contract fingerprints.
- [ ] 5.4 Red: add type saved-view validation/typed-value parameters, inherited/instance binding, schema change, missing/withheld view targets and ad hoc equivalence tests; verify a view never grants release or executes field/operator parameters.
- [ ] 5.5 Implement declaration-backed query views through the generic leaf; verify 5.4 passes and legacy saved views retain parity.
- [ ] 5.6 Red: add existing compiler-consumer tests for view execution, served-state filters, same-call deduplication, item/character/deadline bounds and partial/unavailable values; verify no new generic lane, cue interpretation, kind ranking or pinned-version resolution appears.
- [ ] 5.7 Integrate only the existing compiler consumers with the shared query leaf; verify 5.6 passes and consumer output names authorized source/window/completeness without substituting zero for failure.
- [ ] 5.8 **Q5 gate:** run affected surface/registry/view/compiler suites, installed-wheel proofs and disposable MCP journeys for Records, Planning and two invented declared types; verify cross-surface semantics, response/input bounds and generated non-frozen contracts/privacy gates.

## 6. Q6 — Integrated release and closure

- [ ] 6.1 Build the deterministic N=1k/10k/100k benchmark matrix from design §10; verify seeds, selectivity/skew, three index profiles, cold/warm policy states, Linux/Windows settings, plans, latency distributions, peak RSS and WAL/write ratios are recorded in a reusable report using invented data only.
- [ ] 6.2 Run the completion-boundary full repository checks, all query parity/twin and installed-wheel journeys, parent store performance gates and new benchmark gates; verify every required result passes and independently review the integrated change for SQL/input safety, row admission, left joins, FTS leakage and hidden-state cursor/cost behavior.
- [ ] 6.3 Roll out default-off `collections-query-v1` preview through the repository release process after all gates, verify fresh/migrated/portable stores and rollback-to-legacy behavior, then record the release capability and observed query metrics; verify no canonical data is dropped or unsupported older reader admitted.
- [ ] 6.4 Update generic user/describe guidance and measured budget documentation; verify public-artifact privacy and non-frozen schema checks pass and every count/state/example is sourced from data or explicitly invented.
- [ ] 6.5 After merge/release evidence proves non-optional work shipped, sync the delta into canonical OpenSpec and archive using `openspec archive`; verify `openspec validate --all --strict` passes before/after archive and preserve later requirements. This design drafting lane does not check implementation boxes or archive the new change.
