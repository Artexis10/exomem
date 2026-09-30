## Why

Moving structured collections to SQLite removes file-level scans from storage, but today's query path still evaluates rows in Python and offers one aggregate at a time. Users need fast filtering, analysis and linked queries over Records, Planning and their own declared types, expressed through structured requests and saved views without learning SQL.

## What Changes

- Add declared filter/sort indexes, generated columns and collection-local FTS5 indexes, maintained by governed schema migrations and ordinary store transactions.
- Add a versioned structured query: existing operators with parity, ranges, explicit null tests, date math, multiple sort keys, keyset pages, projections, grouped multi-aggregate analysis, time buckets and HAVING.
- Allow inner and left joins only along type-declared links, within one vault, applying row governance before every reduction on every participating collection.
- Extend `record_memory`, `plan_memory` and collection-mode `query_data` with one optional query object; expose saved queries through existing type declarations and existing compiler consumers. Preserve legacy requests, frozen hosted surfaces and the deferred compiler-lane rewrite.
- Bound query cost, response bytes, page cache and query memory; require invented-data benchmarks, Python/SQL parity and EXPLAIN-based index assertions before each phase ships.
- Keep the engine dark behind a default-off preview until its gates pass. FTS is separately default-off per collection; unavailable FTS returns a typed unavailable result while ordinary queries keep working, never silently substitutes a full scan.

## Capabilities

### New Capabilities

- `collection-query-engine`: declared index lifecycle, structured query semantics, linked queries, governed full text, shared tool/view surfaces and bounded execution with performance gates.

### Modified Capabilities

None. The new opt-in query contract supplements existing collection requests; their existing requirements and response shapes remain binding.

## Impact

- Depends on `move-structured-collections-to-sqlite` P1a: store, built-in type registry, governance and parity corpus. Query work proceeds alongside its P2–P4; declared-type publication waits for P4 validation and P5 facade support.
- Implementation will affect `collection_store/schema.py`, the generic collection query leaf, `query_data.py`, Records/Planning facades, type/manifest validation, saved-view consumers and tool schema generation. This change drafts artifacts only.
- SQLite remains one store per vault. No server database, arbitrary joins, raw SQL surface or server-side model is introduced. CSV/TSV/JSON dataset querying stays on its existing contract.
- Parameterized SQL, authorization before values are decoded, unchanged write authorization and audit, and portable versioned declarations remain mandatory. Derived query structures travel with the store snapshot and never become another canonical source.
- New local and non-frozen hosted schemas require the repository's normal generated-surface and contract gates. Proposed budgets and remaining owner choices are recorded in `design.md`.
