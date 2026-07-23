## 1. Red Retrieval Tests

- [x] 1.1 Add failing algebra and 2,000/8,000-page tests covering AND narrowing, OR/NOT unsupported plans, complete-empty sets, and candidate-bounded opens.
- [x] 1.2 Add failing empty-query and scene-frame/access-policy tests proving direct eligible-path iteration and parity with the correctness oracle.
- [x] 1.3 Add failing catalog tests for compound parser/core/extension identity, pre-core alias migration, FTS-unavailable exact metadata, explicit warming, transient recovery, and narrow fatal classification.
- [x] 1.4 Add failing freshness tests for atomic checkpoints, target-state event coalescing, concurrent events/rebuilds, restart/overflow incompleteness, and rollback.

## 2. Lexical Reliability

- [x] 2.1 Split normal-table semantic catalog schema/readiness from optional FTS schema so exact metadata works without FTS5.
- [x] 2.2 Introduce explicit internal outcomes and typed non-cacheable `RETRIEVAL_INDEX_WARMING` command-envelope projection.
- [x] 2.3 Remove generic process-lifetime retirement and implement exact transient/fatal/rebuildable SQLite classification.
- [x] 2.4 Move journal-mode negotiation out of ordinary reads and implement background start-snapshot, delta-replay, exact-checkpoint atomic replacement.

## 3. Indexed Filter Eligibility

- [x] 3.1 Add conservative structured-filter algebra with distinct complete-empty and unsupported states.
- [x] 3.2 Add a metadata-only lexical query for distinct matching parent paths with scope enforcement.
- [x] 3.3 Route safe plans through candidate hydration and canonical evaluation; retain full-scan evaluation for unsupported plans.
- [x] 3.4 Make empty-query keyword recall iterate finite eligible paths and preserve scene-frame/access-policy behavior.

## 4. Bounded Freshness Repair

- [x] 4.1 Add process-instance/generation checkpoints and non-destructive atomic deltas with mutually disjoint target-state changed/deleted sets.
- [x] 4.2 Apply at most 32 complete delta paths and target checkpoint in one sidecar transaction; return warming and schedule background repair otherwise.
- [x] 4.3 Expose bounded completeness/degradation/timing status without queries, exact category values/counts, paths, note content, or excerpts.

## 5. Performance Verification

- [x] 5.1 Extend the latency harness with the specified 30-sample cold/hot page and unit lanes and anonymized bucketed output.
- [x] 5.2 Run focused filter, lexical fallback, freshness, bounded-retrieval, and latency-curve tests.
- [x] 5.3 Measure the real-vault cold and hot category lanes on a quiescent service against the specified gates.
- [ ] 5.4 Run Ruff and the full lean test suite, then record verification evidence.

## Verification evidence (2026-07-23)

- Focused retrieval, freshness, catalog, teaching, surface, and privacy suite: 259 passed.
- Independent review regressions cover default-scope auto-widen scans, post-filter-before-limit false empties in keyword/vector unit recall, empty vector filter-only recall, and FTS-less catalog write/delete maintenance.
- Typed diagnostics and four-lane harness suite: 45 passed.
- Ruff: all 37 changed/new Python files passed the repository lint selection.
- 2,500-page synthetic gate (30 samples/lane): page cold/hot total p95 10.89/3.139 ms; unit cold/hot 8.304/2.08 ms; all gates passed.
- Aggregate real-vault Markdown mirror gate (30 samples/lane; no identities or content retained): page cold/hot total p95 15.227/3.44 ms; unit cold/hot 10.493/1.905 ms; all gates passed. The temporary mirror was moved to the Windows Recycle Bin after measurement.
- Full Linux lean suite remains delegated to the required GitHub Python 3.11/3.13 matrix because the managed Windows sandbox denies WSL, named pipes, and POSIX-only collection paths.
