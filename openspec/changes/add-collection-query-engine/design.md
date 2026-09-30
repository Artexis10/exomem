## Context

See `proposal.md` for motivation. This is a proposed follow-up to `move-structured-collections-to-sqlite`, not a revision of its storage migration. Its design §2 supplies the STRICT store and versioned declarations; §7 supplies governance; §12 and §16 A7 supply latency budgets; §14 supplies the generic type mechanism. P1a.11 requires a passing Python parity test for every SQL push-down operator.

Today `query_data.evaluate_rows` materializes a collection, filters in Python, sorts one key and applies one aggregate. Its operator set also includes `missing`. Equality and ordering use tolerant numeric coercion; `in` uses string identity; `startswith` ignores case; missing and JSON null both read as `None`. The store's only user-field index is the natural key. Simply translating these operators into SQLite affinity, LIKE or NULL comparisons would change behavior.

Settled: no raw SQL from users or agents; one SQLite store per vault; governance is part of SQL row admission; withheld rows behave as absent; governed writes and their audit remain authoritative. Source amendments A1–A10 remain binding. A11 explicitly defers pinned version links and the compiler-lane rewrite: neither is delivered here.

The defaults below are concrete recommendations for the orchestrator to rule on. They are proposal contracts, not claims of measured performance or owner ratification. Every unresolved choice has a recommendation under **Open questions**.

## Goals / Non-Goals

**Goals:** Make the same structured query work for built-ins and declared types; keep hot reads and memory bounded; make existing semantics testable before optimizing them; support useful analysis and declared relations without SQL knowledge.

**Non-Goals:** Arbitrary SQL/expressions, arbitrary join keys, cross-vault joins, historical-version joins, recursive joins, write queries, another database, a new tool, automatic index tuning, dataset-engine replacement, or new context-lane selection/ranking. The agent authors the query; the server validates and computes deterministically, with no model.

## Decisions

### 1. Versioned structured requests and one compiler

Add one optional `query` object to collection-mode requests. Its version is `1`; unknown keys, operators and field paths fail closed with field-addressed findings. Legacy arguments remain accepted when `query` is absent. Mixing the object with legacy query-shaping arguments is `QUERY_ARGUMENT_CONFLICT`, rather than silently choosing one. Collection/path and saved-view selectors remain outside the object.

The grammar comprises `where` (`all`, `any`, `not`, or a field/operator/value leaf), `order_by`, `select`, `page`, `group_by`, named `aggregates`, `having`, `joins`, `text` and `as_of`. It admits data, enumerated operations and typed references only: no SQL fragments, expression strings, user functions, collation names or JSON-path syntax. Dotted fields resolve against declared field paths; aggregate aliases and join aliases are validated identifiers, never SQL identifiers.

An invented row query:

```json
{
  "version": 1,
  "where": {"all": [
    {"field": "status", "op": "eq", "value": "complete"},
    {"field": "duration_minutes", "op": "between", "value": {"lower": 10, "upper": 60}}
  ]},
  "order_by": [{"field": "started_at", "direction": "desc", "nulls": "last"}],
  "select": ["item_key", "started_at", "duration_minutes"],
  "page": {"limit": 50}
}
```

The compiler validates the AST, binds types and declared relations, adds governed row predicates, admits cost, then prepares parameterized SQL. SQL templates and compiler-generated identifiers alone form executable text. Every literal, FTS term, cursor value and date boundary is a bound parameter. Field labels resolve through registry mappings; user strings are never spliced into SQL, including DDL. Generated objects use opaque internal names. Migration expressions come from a closed template set with bound/encoded registry metadata, never authored expression text.

The alternatives are raw SQL (violates the settled boundary) and a growing list of top-level arguments (duplicates validation across tools). One object keeps the static tool schema small; `describe` teaches the grammar and capabilities on demand. The parent change's ≤400-byte schema budget applies to its own additions, not an implied allowance for this new contract: measure and review this change's separate delta before publication.

### 2. Declared indexes with an explicit physical budget

Extend type fields with `filterable: true` and/or `sortable: true`; extend type/collection declarations with named `indexes` for composite field orders. A field with both flags needs one default index, not two. A composite index can satisfy a flag when the planner proves its prefix/order usable. Unfulfilled flags produce automatic *declared* single-field index entries at validation time, visible in `diff`; query execution never creates an index. Collections may add indexes for their declared extensions and disable inherited optional indexes through a governed revise, but cannot remove indexes needed by declared relations or enabled saved views without revising those dependencies together.

Recommended limits per collection: **8 secondary B-tree indexes**, **4 keys per composite index**, **16 distinct indexed scalar field paths**, and **one FTS5 index over at most 8 declared string fields**. Required identity/natural-key indexes are exempt; relation lookup indexes count within the 8; the FTS index has its separate cap. Reject over-budget declarations before writes. Arrays/objects may be projected, but require a declared scalar subfield to get a B-tree index; no multi-value index is implied.

The shared `items` table must not gain one generated column for every field of every collection: that runs into SQLite's column limit and amplifies unrelated writes. Instead use a collection-local derived query table inside the same database, keyed by `row_id` and carrying a derived `index_values_json` object plus generated **VIRTUAL** columns for the declared indexed paths. The writer extracts declared fields into immutable internal ordinal keys; generated-column expressions address only those compiler-owned keys, never user-authored JSON paths. Each indexed path gets presence/type information and the typed value needed by its operator contract. Compatibility projections may need both the Python-equivalent numeric and text keys. Only generated columns actually used by an index are indexed. A write to another collection touches none of these indexes.

These tables are read projections; `items` and its version/audit tables remain the sole canonical data. Generic writes, bulk upsert, type migration and edit-back update the affected query table and FTS rows in the **same transaction**, with no second fsync. Parity checks verify the extracted fields and typed projections after writes, rollback and restart. Do not route a read to a stale projection.

Store migrations own the table templates, index creation/drop, manifest-to-physical mapping and schema version. A governed declaration `diff` lists affected collections, objects to create/drop, projected disk bytes and whether a backfill is required. Save/revise invokes the installed migration machinery under the writer lease. Build replacement structures in bounded batches while queries continue on the old ready contract; at a fenced final catch-up publish the declaration and physical mapping atomically. A failed build preserves the old ready contract. Declaration changes with no old equivalent remain unavailable until ready; no query-triggered DDL or unbounded fallback. The mapping states `building`, `ready` and `failed` are operator diagnostics, disclosed only after authorizing the collection.

Write-amplification recommendation: at the default profile (4 secondary indexes), indexed/FTS-disabled guarded append and field-changing update p95 are **≤1.5×** their no-secondary-index baseline; at the maximum profile (8 plus FTS), **≤2×**. Report WAL bytes per mutation with the same ratios. The parent §12 targets (<20 ms append at 10k; <1 s for 500-row bulk upsert) must still pass, rather than being replaced by ratios. Expensive declarations are not auto-created from observed queries. Expression indexes directly on shared JSON would be simpler, but do not meet the generated-column requirement or isolate collection write costs.

### 3. Operator compatibility, nulls and dates

Legacy forms compile through a compatibility adapter with exactly today's behavior for `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `contains`, `icontains`, `startswith`, `in`, `nin`, `exists`, `missing`; `count`, `min`, `max`, `sum`, `avg`, `latest`, `distinct`, `group` and `profile`; inclusive legacy date bounds; one-key stable sorting; projections, offsets, totals and truncation. Legacy sort ties use the same stable store input order as the Python oracle. Preserve tolerant number parsing (including decimal commas and numeric prefixes), Python string representations, Unicode `.lower()`, substring behavior and the distinction between membership string identity and numeric equality. SQL LIKE and SQLite NOCASE are not substitutes. Existing distinct/group limits and aggregate envelopes remain unchanged.

Until parity is proved, an operator is not enabled for push-down. A deterministic compatibility function/collation with bounded work may bridge a mismatch; its registration and semantics are versioned. During preview, the old evaluator may handle only legacy requests within its existing bounds; an unsupported **new** query returns `QUERY_UNSUPPORTED`, never silently loads the collection. Retirement of the Python evaluator requires a later explicit gate, not this design.

Version 1 uses declared typed values: no coercion of a free-text field into a number. Present-null and absent are distinct, while `exists`/`missing` retain their compatibility meanings. `is_null` matches present JSON null, `is_missing` matches an absent key and `is_not_null` matches present non-null values (including an empty string). Other typed comparisons exclude null/missing; `ne`/`nin` do not accidentally include them through SQL three-valued logic. `between` takes typed lower/upper bounds and optional inclusivity flags (both true by default); omitted bounds are invalid, while one-sided ranges use existing ordering operators. Grouping keeps null and missing as separate tagged keys; a left-join miss uses an explicit absent-target marker.

Date fields distinguish a calendar date from an instant. Instants normalize to UTC at write time; unzoned datetime values fail validation. Date math is a typed value such as `{"relative_to":"as_of","amount":-30,"unit":"day"}`. Units are day/week/month; month arithmetic clamps to the last valid day; invalid and reversed ranges fail. Freeze one `as_of` per first page, return it, bind it into continuations, and never re-evaluate relative dates on later pages. Calendar operations and buckets use UTC in v1: days start at midnight, ISO weeks on Monday, months on day 1. Owner choice on named time zones is deferred below.

### 4. Ordering, keyset pages and projection

`order_by` accepts up to 4 explicit keys with asc/desc and first/last null placement; the default is `item_key asc`. Append the base `item_key` as the final unique tie-breaker for row queries. Typed strings use deterministic binary ordering; compatibility queries keep the old comparison keys. Null and missing have separate stable ranks. Validate projection paths before compiling, fetch only selected values plus hidden execution keys, and remove unselected execution fields before egress.

Row pages default to 50 and cap at 1,000. Version 1 accepts only opaque keyset `after` cursors, not offsets; legacy offsets remain on the compatibility path. Cursors authenticate query version/hash, collection/declared relation identities, schema versions, audience and policy fingerprint, frozen `as_of`, sort tuple and an **authorized visible-state basis**. The internal full-store commit counter is not exposed or used alone to invalidate a restricted caller's cursor. Reuse §7/A7's release cache, with bounded eviction, to derive the visible-state digest from authorized row identities/versions without decoding values. A hidden-only write must leave continuation semantics unchanged. Visible row changes, a policy or schema change return `QUERY_CURSOR_STALE`; malformed, tampered or differently bound cursors return `QUERY_CURSOR_INVALID`.

Each page runs in one read snapshot, closes its cursor/transaction on completion and holds no transaction across calls. Keyset continuation is consistent while its visible basis remains unchanged; it is not a promise of a long-lived SQLite snapshot. Group pages order by the full tagged group-key tuple (with requested aggregate sort keys ahead of it); that tuple is their tie-breaker. Read one extra **authorized** result to establish `has_more`, including byte-cap truncation; resume after the last emitted result, never after a fetched-but-unemitted row. A single result larger than the byte cap returns `QUERY_RESULT_TOO_LARGE` with no false completion claim.

### 5. Grouped analysis and HAVING

Support up to 4 group keys and 8 named aggregates in one query: `count` (rows, or non-null values when a field is supplied), `sum`, `avg`, `min`, `max`, exact `percentile` and exact `distinct_count`. Version 1 numeric aggregates accept declared integer/number fields; min/max also accept typed strings/dates with their declared order. Count of an empty input is 0; other empty aggregates are null; distinct-count excludes missing/null. No grouping and empty input yields one aggregate row; grouped empty input yields no groups.

`percentile` takes `p` in [0,1] and uses continuous linear interpolation at rank `(n-1)*p` over sorted non-null numbers. No dependency on an optional SQLite percentile extension: use bounded disk-backed ordering of the released values, with SQL/window primitives or a streaming deterministic reducer. Reject work above the admitted row/time limits; never collect the entire numeric column in Python. Do not fold approximate sketches into an exact contract. Percentile has its own benchmark because its sort cost exceeds ordinary grouped reductions.

Time bucket group keys are structured `{field, bucket: day|week|month}`. `having` uses the same boolean tree but can address only aggregate aliases and group keys, and executes after grouping over admitted rows. Cap total pre-HAVING group cardinality at **1,000** (checked using 1,001 authorized groups, not a `LIMIT` pretending the reduction was complete). Group pages default/cap at 50/200 and also respect the byte cap. Typed `distinct_count` differs intentionally from legacy `distinct`, which retains its bounded value list and Python identity. Legacy `latest` and `profile` remain available through compatibility, not redefined as aggregate aliases.

An invented saved analysis view:

```json
{
  "version": 1,
  "group_by": [{"field": "started_at", "bucket": "month"}, {"field": "status"}],
  "aggregates": {
    "runs": {"op": "count"},
    "minutes": {"op": "sum", "field": "duration_minutes"},
    "typical_minutes": {"op": "percentile", "field": "duration_minutes", "p": 0.5},
    "unique_recipes": {"op": "distinct_count", "field": "recipe_key"}
  },
  "having": {"field": "runs", "op": "gte", "value": 3},
  "page": {"limit": 50}
}
```

### 6. Declared links, row admission and joins

Extend a type with named relations, for example `recipe: {field: recipe_key, target_type: recipes, cardinality: many-to-one}` on an invented execution type, or `account` on a subscription type. The source field is an opaque item reference/key; a collection instance binds the relation to exactly one authorized target collection of the declared type. References stay in that vault. V1 joins resolve the current target item by its existing unique `item_key`; pinned historical links remain deferred by A11. Required lookup indexes are declared and budgeted. No arbitrary field-to-field key or collection inference is accepted.

`joins` requests only `{relation, alias, kind: inner|left}` along declared links, with at most **2 hops** and **2 join edges**. V1 supports many-to-one and one-to-one, avoiding unbounded row fan-out; reverse one-to-many traversal is an owner question. `select`, `where`, grouping and aggregate fields can address a validated relation alias. Ambiguous aliases, cycles, undeclared edges, invalid type binding and cross-vault requests fail before reading row values.

Resolve policy once, bind each collection's governance subject (path, ref, type, tags, project, default-deny), and compile admission into every SQL relation. Uniform release compiles to a collection predicate or empty relation. For mixed release, retain the parent §7 identity-only authorization pass and its cache; stream identity batches into a bounded disk-backed authorized-ID relation and compile an EXISTS/membership predicate into each alias's WHERE/derived table. This compiles the **decisions** of the existing pure governance evaluator, rather than attempting an unproved SQL translation of every policy scope.

Join against a prefiltered authorized right relation, or put its admission in the join's ON clause. A right-side predicate placed after a left join would incorrectly erase visible left rows. A missing or withheld target item therefore yields the same absent right projection for a left join; neither satisfies an inner join. Explicit right-field conditions in the user's WHERE may still exclude absent targets. A missing/withheld target **collection** returns the same generic `COLLECTION_NOT_FOUND` before relation/type metadata is disclosed. No hidden target field, existence flag, ref, uniqueness conflict or diagnostic is returned.

Admission precedes filtering, totals, sorting, group cardinality, HAVING, full text, pagination and joining. Apply the existing egress envelope and governance receipts after execution as defense in depth; no post-query filter substitutes for SQL admission. Leave §7's complete-authorized-state rule for mutations unchanged. SQL reads create no collection transaction or mutation audit effect.

### 7. Collection-local full text without ranking leaks

Declare `full_text: {fields: [title, description], enabled: true}` on a type or collection. FTS5 is separately default-off and unavailable builds expose `QUERY_FTS_UNAVAILABLE` for text queries; ordinary indexed queries remain available. One content-bearing FTS table per enabled collection uses only its declared text fields, `unicode61` with pinned settings, and the canonical `row_id`. Maintained copies remain derived and update atomically with current items. Rebuild through migration, never on read, and never index held candidates or historical versions.

Accept structured literal terms/phrases with `all`, `any` and `not`; escape tokenizer syntax through a closed encoder and bind the resulting MATCH value. Do not accept a raw MATCH expression, wildcard operator or tokenizer command. Combine MATCH row IDs with the same authorized relation and ordinary filters before reductions. V1 ordering is the declared field order/keyset, not global FTS BM25: whole-index document frequency can reveal withheld rows through scores or changed ordering. Snippets and raw FTS diagnostics are omitted; an unselected text field is never echoed. Exact search semantics have a separate reference tokenizer corpus; legacy Python `contains` is not redefined as full text.

### 8. Tools, saved views and existing compiler consumers

All surfaces call the same generic collection query leaf: `record_memory(action="query")` for Records/declared types, `plan_memory(action="query")` for Planning, and collection-mode `query_data` resolving an explicit collection selector. Dataset-mode `query_data` remains unchanged and rejects collection-only query objects. Facade type checks and Planning lifecycle/hierarchy constraints still hold; hierarchy expansion outside the new query continues under the old bounded contract.

Local and non-frozen hosted surfaces gain the optional object plus describe text, with a contract fingerprint/schema refresh; frozen hosted candidates remain byte-identical. CLI and REST share the object and leaf, rather than parsing an independent grammar. Successful v1 responses carry rows or groups, returned count, `has_more`, optional `next_cursor`, truncation reason, frozen `as_of`, authorized source refs and query/schema version. Exact visible total is opt-in and charged against the same cost budget; no approximate count is labeled exact. Counts and sources never name a hidden collection. Legacy response envelopes stay unchanged.

Bounds are **64 KiB for the whole serialized v1 result**, 1,000 row results, 200 group results and 32 selected fields. Bound input separately: 16 KiB query object, 64 predicate leaves, boolean depth 8, 100 membership values per leaf, 32 text terms/phrases and 4 KiB total text. Reject an oversized query before preparation. Execution keys are not automatically added to the projection. Partial pages state `truncated`/reason and a usable cursor; cost/time failure returns no apparently complete aggregate or count. Optional totals that cannot finish return typed unavailable, never zero. Consumer-specific budgets can only tighten these limits.

Type `views` may hold named version-1 query objects and typed parameter declarations/defaults. Validate against that type's fields, relations and index budget at save time; bind user parameters as data; forbid field/operator parameters. Instance bindings validate target collections at execution under the caller's audience. A saved view shares cost, governance, cursor and byte limits with an ad hoc query; declaration does not grant release. Legacy saved views remain compatible. Existing context compiler consumers may execute an authored saved view through the shared leaf, preserving their current item and character budgets and served-state filters; coalesce identical `(collection, view, parameters, audience)` executions within a call. They present query-unavailable/partial states and do not invent zero values. This is a query backend integration only: no generic collection lane, new turn cues, new kind selection, automatic ranking or pinned-version resolution.

### 9. Admission, memory and testable physical plans

Proposed default cost envelope: at most **100,000 estimated admitted row visits** across base and target relations, 2 join hops/edges, 1,000 groups and 8 aggregates. Estimate before execution from declaration-bound indexes, cardinalities of the **released** relations and query shape; SQLite EXPLAIN QUERY PLAN confirms access paths, but is not a row estimator. Physical table counts/statistics of withheld rows must not affect public estimated counts or semantic group limits. For a mixed-release collection lacking safe cached selectivity, use the conservative authorized cardinality; an unknown bound refuses `QUERY_COST_LIMIT`. Maintain statistics through migration/maintenance, not as a query side effect. The planner cannot promise a hard bound from estimates; the runtime deadline is the backstop.

Set a **200 ms execution deadline**, cancellation and a SQLite progress handler checked at least every 1,000 VM instructions, covering SQL and deterministic compatibility functions; also check it between identity batches and result serialization. Close cursors, clear connection-local authorization tables and restore handlers on success, error, disconnect and timeout. Return `QUERY_TIMEOUT` with no partial reduction. Bounded compiler consumers use a tighter call-wide deadline. Exhausting cost/group limits returns `QUERY_COST_LIMIT`/`QUERY_GROUP_LIMIT`, without raw estimates, SQL, paths or FTS statistics.

Read connection policy: `cache_size=-8192` (**8 MiB**), `mmap_size=0`, disk-backed `temp_store=FILE`, at most **2 concurrent read connections per vault**, and `fetchmany` batches of **128 rows**. Separate the writer connection; budget the existing release cache to **8 MiB per vault** with eviction and streaming recomputation. Query transient memory target is **≤16 MiB incremental peak RSS per active reader** including its SQLite page cache, excluding the already resident service and bounded shared release cache. No `fetchall`, row list, Python numeric array or unbounded in-memory authorized-ID set. Temp storage has a **64 MiB per-query budget** and is cleaned on interruption; exact percentile sorting and grouped intermediates spill under that cap. Connection concurrency, queueing and temp exhaustion have typed busy/cost refusals, not fallback materialization.

EXPLAIN tests on invented fixtures assert named declared-index usage for representative equality/range + sort + page queries and relation lookups, no full base-table scan and no temporary ORDER BY B-tree where the matching composite index satisfies order. A bounded FTS virtual-table search is expected for text queries. Aggregations/percentiles are allowed admitted scans/sorts, so do not assert every query is an index seek. Plan text is test/operator evidence only, never a user response. Also assert actual visited-row/memory counters in benchmarks so merely seeing an index name does not prove useful indexing.

### 10. Performance acceptance and invented-data protocol

Extend the parent §12 harness; these are gates to measure, not existing achievements. Timings cover the generic query leaf from policy/manifest resolution through bounded response construction, excluding network transit. Warm means database pages, declarations and the unchanged release basis are ready. Uniform release is the headline case; cold runs and mixed release are measured separately. Keep the parent's N=10k gates as regressions.

| Workload, N=100,000 current rows | Recommended warm p95 |
| --- | ---: |
| Indexed equality/range + matching sort + page of 50, uniform release | ≤10 ms |
| Same, mixed release withholding 10%, unchanged release basis | ≤25 ms |
| Group-by ≤100 groups with count/sum/avg/min/max, uniform release | ≤50 ms |
| Same, mixed release with ready authorization basis | ≤75 ms |
| Two declared many-to-one hops, indexed filter/sort, 50 results | ≤25 ms |
| FTS + indexed filter + 50 results, uniform release | ≤25 ms |
| Exact percentile across 100k numeric values, ≤100 groups | ≤150 ms |
| Memory per active query reader / page cache | ≤16 MiB / 8 MiB |

Generate deterministic invented datasets at N=1k, 10k and 100k, with recorded seeds: execution rows, recipes, subscriptions and accounts; duplicates in sort values; missing/null/empty strings; integer, decimal-comma legacy values and Unicode; midnight/month/week boundaries; sparse and skewed status distributions; target links missing or withheld; text with quotes/operator punctuation. Test 1%, 10% and 90% selectivity and both uniform and ref/tag-scoped release (10% withheld). Test 0, default 4 and maximum 8+FTS index profiles.

Run on a quiescent release host using the pinned wheel/interpreter and record OS/filesystem, CPU/RAM, SQLite version/options, WAL/synchronous pragmas, input shape, seed, index declarations, plans, connection/cache settings and scope. After 20 warmups, measure at least 200 calls per cell and report p50/p95/p99, deadline failures, scanned/admitted/returned rows, incremental peak RSS, database/index/WAL bytes, append/update ratios and the parent bulk-upsert budget. Report fresh-connection/cold-policy behavior, warm and invalidated mixed-release cache, and concurrent reader/writer behavior separately; do not average them into the ≤10 ms headline. Run Linux and Windows/NTFS as required by A7. Freeze semantic correctness before comparing timings; a fast wrong result is a failed gate.

## Risks / Trade-offs

- Index and FTS maintenance increases write/WAL costs → explicit caps, measured ratios and the unchanged parent write gates; no automatic indexes.
- Typed v1 semantics differ from tolerant legacy semantics → separate explicit version, legacy adapter and parity corpus; no implicit conversion of old views.
- Mixed governance needs an initial identity pass → bounded shared cache, disk-backed authorization sets and separately reported cold latency; no value decoding ahead of admission.
- Exact percentile needs sorting → streaming/disk-backed reduction, its own gate and explicit timeout/cost refusal.
- FTS distributions and full-store generations expose hidden state → no global score/snippet output, visible-basis continuations, rows-absent twin tests across every reduction.
- Migration/backfill may take a long time → bounded background build, writer fencing, atomic mapping publication and old ready queries until cutover; no read-path rebuild.
- SQLite build differences → capability checks for JSON/generated columns/FTS5 and a pinned deterministic compatibility layer; unsupported FTS soft-fails only text queries.

## Migration Plan

1. **Prerequisite gate:** parent P1a is merged, including the built-in registry, generic store reads, row governance and P1a.11 parity generator. P1a does not imply that P4's authoring API exists.
2. **Q1–Q3 dark:** add versioned query validation, bounded compiler/executor, migration-managed indexes, grouped analysis and declared joins against registry fixtures. Run in parallel with parent P2–P4. Generic types are data even before their public declaration API is ready.
3. **Q4 text:** opt-in FTS capability checks, schema migrations and governed combined search. It remains off on undeclared collections and unsupported builds.
4. **Q5 surfaces:** publish only after parent P4 validates declarations and P5 serves declared types. Update non-frozen tool contracts, saved views and existing compiler consumers; A11 deferrals remain intact.
5. **Q6 release:** finish parity, rows-absent twins, installed-wheel/realistic MCP proofs, integrated full checks and the benchmark matrix. Roll out `collections-query-v1` preview per vault, verify fresh and pre-existing stores and source parent gates, then declare readiness through the normal repository release process. Owner rulings precede implementation of affected choices.

Rollback disables the preview and stops new-object queries with typed unavailable responses; legacy queries and canonical writes continue. Derived structures remain transactionally maintained until an explicit migration retires them. Never drop canonical items/history/audit or switch storage engines. Backups/portability include the schema and projection mappings in the one-store snapshot. Readers predating the new schema must refuse it under the store's schema-version check, so binary downgrade requires a compatible reader or a validated pre-upgrade snapshot, never opening an unsupported database. A failed optional FTS build preserves ordinary queries and the previous ready declaration.

## Open questions

These are proposed owner rulings. The orchestrator can accept the recommendations directly; an amendment must update this design, its spec and the affected tasks together before implementation.

1. **Index and write budgets:** accept 8 B-tree indexes, 16 indexed scalar paths, 4 keys/index and one 8-field FTS index; default/max write and WAL ratios 1.5×/2×? **Recommendation:** yes, measured with parent absolute write gates unchanged; revisit only with benchmark evidence.
2. **Typed v1 versus legacy coercion:** should new objects inherit tolerant numeric/string equality? **Recommendation:** typed v1, explicit compatibility for old requests/views; teach the difference in `describe`.
3. **Calendar time zones:** should v1 support named IANA zones? **Recommendation:** UTC-only day/week/month and frozen `as_of`; named zones require a later explicit DST contract.
4. **Join cardinality:** should reverse one-to-many links ship immediately? **Recommendation:** one-to-one/many-to-one only with 2 hops/edges; add fan-out only with explicit multiplicity, counting and cost semantics in a follow-up.
5. **Percentile implementation:** exact or approximate, and an optional SQLite extension? **Recommendation:** exact continuous interpolation using shipped deterministic SQL/streaming reduction, no extension dependency; separate ≤150 ms gate and refusal above budget.
6. **Execution and RAM budgets:** accept 100k estimated authorized row visits, 1k groups, 200 ms deadline, 8 MiB page cache, 2 readers, 8 MiB shared release cache and 64 MiB query temp space? **Recommendation:** yes as conservative defaults; tighten for hosted/compiler callers, do not silently widen.
7. **FTS ordering:** include relevance scores in v1? **Recommendation:** field/keyset ordering only; release-local relevance requires a proved rows-absent scoring design in a follow-up.
8. **Tool schema growth:** how much additional static schema is acceptable for the query object? **Recommendation:** one shallow optional object per existing query surface, detailed grammar via `describe`, an explicit measured byte delta and non-frozen contract refresh; preserve the parent's separate schema budget and frozen candidates.
