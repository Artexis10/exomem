## Purpose

Let users query and analyze their declared structured collections through governed, bounded requests and saved views, with indexed SQLite execution and no requirement to know or submit SQL.

## ADDED Requirements

### Requirement: Structured queries compile without user SQL

The system SHALL accept a closed version-1 structured query over Records, Planning and declared collection types, validating fields, aliases, types and operations before execution. It SHALL compile only approved operations to parameterized SQL. User strings SHALL never be interpolated into SQL or schema DDL. SQL, expression strings, arbitrary join keys, cross-vault references and user-defined executable functions SHALL be refused. Queries SHALL execute no mutations or collection audit effects.

#### Scenario: Injection-shaped text is literal data
- **WHEN** a filter value or text phrase contains quotes, SQL syntax or tokenizer operators
- **THEN** it is bound as literal data and cannot change the prepared query or schema

#### Scenario: Undeclared executable input is refused
- **WHEN** a request supplies SQL, an unknown operator, an undeclared field or a cross-vault relation
- **THEN** validation refuses before item values are read and identifies only authorized request field paths

### Requirement: Declared indexes have migration-owned lifecycle and budgets

A type or collection SHALL declare scalar fields as filterable/sortable and optional composite indexes. The store SHALL maintain generated columns and matching indexes through schema migrations, never query-triggered DDL. Each collection SHALL allow at most 8 secondary B-tree indexes, 4 keys per index and 16 distinct indexed scalar paths; identity/natural-key indexes are exempt and relation indexes count toward the limit. A governed declaration preview SHALL report additions, removals, backfill and dependent-view impact.

#### Scenario: A declared index becomes ready atomically
- **WHEN** an authorized declaration revision adds an indexed field to a populated collection
- **THEN** migration builds it in bounded batches, publishes the declaration and ready mapping together, and preserves the old ready contract if the build fails

#### Scenario: Index budget is exceeded
- **WHEN** a proposed declaration needs a ninth secondary B-tree index
- **THEN** validation refuses before the declaration or any physical schema is changed

#### Scenario: Removing a required lookup is refused
- **WHEN** a revision drops an index needed by a declared relation or enabled saved view without revising that dependency
- **THEN** validation reports that dependency and preserves the current declaration

### Requirement: Index maintenance preserves canonical writes and write budgets

Derived query and FTS structures SHALL remain within the vault's single SQLite store and update atomically with ordinary governed writes, bulk upsert, type migration and edit-back. They SHALL never become canonical data or add a second acknowledgement-path fsync. With 4 secondary indexes, append/update p95 and WAL bytes per mutation SHALL be at most 1.5 times the no-secondary-index baseline; with 8 plus FTS, at most twice baseline. Parent store absolute write gates SHALL also remain passing.

#### Scenario: A rolled-back update changes no search result
- **WHEN** an update modifies an indexed/text field but precommit authorization or transaction completion fails
- **THEN** canonical rows, query projections, FTS matches and mutation audit remain at the previously committed state

#### Scenario: Maximum declared indexes retain the parent write gate
- **WHEN** the benchmark runs the maximum index/FTS profile
- **THEN** it meets the relative latency/WAL budgets and the parent append and bulk-upsert limits, reporting both

### Requirement: Existing Python query semantics gate every push-down

Legacy requests and saved views SHALL preserve Python-evaluator behavior for every existing filter, sort, date bound, projection, total, aggregate and truncation shape. This includes `missing`, tolerant numeric coercion, string-identity membership, Unicode lowercase substring/prefix matching and stable tie ordering. Every pushed-down existing operator SHALL first pass P1a.11 parity on invented data, for both built-ins and declared-type fixtures. New operators SHALL have an independent reference oracle before push-down is enabled.

#### Scenario: Numeric and Unicode edge cases retain parity
- **WHEN** the corpus queries numeric prefixes, decimal commas, mixed numeric/text values, missing/null/empty values, membership and non-ASCII prefixes
- **THEN** SQL and Python return equal authorized rows, order, totals, aggregate values and bounded rendered results

#### Scenario: An unproved operator cannot push down
- **WHEN** an operator lacks a passing parity/reference test
- **THEN** its SQL path remains disabled and a new query needing it receives a typed unsupported result

### Requirement: Versioned filters distinguish typed values, nulls and dates

Version 1 SHALL support all existing operators, boolean composition, inclusive/exclusive `between`, explicit `is_null`, `is_missing` and `is_not_null`, and day/week/month date math frozen to one `as_of`. Typed comparisons SHALL exclude missing/null values; `is_null` SHALL match only present null, and `is_not_null` SHALL include present empty strings. Instants SHALL require a zone and normalize to UTC; UTC calendar month arithmetic SHALL clamp to a valid last day. Invalid types, dates and reversed ranges SHALL fail validation.

#### Scenario: Null is distinct from absent and empty
- **WHEN** rows contain an absent field, present null and a present empty string
- **THEN** `is_missing`, `is_null` and `is_not_null` select their defined distinct cohorts without changing legacy `exists`/`missing`

#### Scenario: Relative range is frozen across pages
- **WHEN** a caller continues a query with an as-of-relative date range after the clock crosses midnight or a month boundary
- **THEN** the same frozen instant and UTC calendar rules apply to every page

### Requirement: Multi-key order and projection use bounded keyset pages

Version 1 SHALL support up to 4 explicit sort keys with direction/null placement, stable unique tie-breakers and up to 32 selected fields. Row pages SHALL default to 50 and cap at 1,000. Continuation SHALL use authenticated opaque keyset cursors bound to query, schema, audience, policy, frozen time and authorized visible state. Hidden-only changes SHALL not alter continuation. Visible/policy/schema changes SHALL return stale; invalid or differently bound cursors SHALL return invalid. No read transaction SHALL span page calls.

#### Scenario: Ties and nulls page without loss
- **WHEN** rows share all explicit sort values or carry null/missing sort fields
- **THEN** unchanged visible state yields each authorized row once in stable order, and unselected execution keys are absent from the response

#### Scenario: Hidden writes leave continuation unchanged
- **WHEN** two equivalent visible collections differ only by a withheld-row insertion or update between page calls
- **THEN** both yield the same visible next page, availability and continuation behavior

#### Scenario: Changed audience cannot reuse a cursor
- **WHEN** a caller reuses a cursor under another audience or query
- **THEN** the cursor is refused before values are disclosed

### Requirement: Grouping supports multiple exact aggregates and HAVING

Version 1 SHALL support up to 4 group keys, UTC day/ISO-week/month buckets, 8 named aggregates (`count`, `sum`, `avg`, `min`, `max`, `percentile`, `distinct_count`) and HAVING over group keys/aggregate aliases. Percentile SHALL be exact continuous interpolation at `(n-1)*p`, with p in [0,1]. Null/missing SHALL be separate group keys and excluded from field reductions. Empty input SHALL produce zero count/distinct-count, null other aggregates, and no grouped rows. Pre-HAVING groups SHALL cap at 1,000; exceeding it SHALL fail rather than silently trim a reduction. Group pages SHALL default/cap at 50/200.

#### Scenario: One grouped query computes several reductions
- **WHEN** a query groups invented executions by month/status and asks for count, sum, average, median and distinct recipe count with HAVING
- **THEN** all values derive from the same authorized snapshot and HAVING removes groups only after the complete admitted reductions

#### Scenario: Percentile matches its exact reference
- **WHEN** the ordered values are 10 and 20 and p is 0.25
- **THEN** percentile is 12.5, independent of optional SQLite percentile-extension availability

#### Scenario: Too many groups are not reported as a complete page
- **WHEN** admitted input yields 1,001 groups before HAVING
- **THEN** the result is a typed group-limit failure with no apparently complete aggregates

### Requirement: Joins follow only declared current-item relations

Type declarations SHALL define named one-to-one/many-to-one links to a target type, and collection instances SHALL bind each used link to one target collection in the same vault. Requests SHALL follow only those links with inner/left semantics, at most 2 hops and 2 edges. Joins SHALL resolve current item identities, never infer arbitrary keys, traverse reverse fan-out, recurse or resolve historical pins. Both sides SHALL pass the same row governance before joining.

#### Scenario: Withheld right item is indistinguishable from absent
- **WHEN** a visible execution links to a withheld recipe in one fixture and a missing recipe in its twin
- **THEN** a left join retains the same visible execution with an absent target, and an inner join excludes it in both fixtures

#### Scenario: Undeclared or excessive traversal is refused
- **WHEN** a request supplies a new join key, reverse one-to-many edge, cycle or third hop
- **THEN** it fails before any target values are decoded

### Requirement: Governance is SQL row admission before every reduction

Each participating relation SHALL have governance compiled into its SQL admission predicate from the once-resolved existing evaluator, using an authorized identity relation when release is mixed. Withheld rows SHALL be absent from rows, text matches, counts, totals, aggregates, group limits, HAVING, joins, sorting and pagination. Missing/withheld collections SHALL share the same refusal. Exact totals, cursor bases and public cost diagnostics SHALL derive only from released state. Existing mutation authorization, disclosure receipts and egress envelopes SHALL remain binding.

#### Scenario: Rows-absent twins match on every query surface
- **WHEN** one invented store contains withheld base/target rows and its twin physically omits those rows
- **THEN** authorized row/group output, totals, HAVING decisions, join outcomes, pagination and semantic limit/refusal behavior match across the shared tool and saved-view paths

#### Scenario: Default-deny type remains private through analysis
- **WHEN** an audience lacks release for a declared type or relation target collection
- **THEN** direct, saved-view, aggregate and text queries disclose no item, count, type metadata or existence distinction

### Requirement: Declared full text is optional and governed

Each collection SHALL allow one opt-in FTS5 index over at most 8 declared string fields, maintained through migration and canonical transactions. Text requests SHALL use structured literal terms/phrases combined with ordinary filters and governance, never raw MATCH syntax. Version 1 SHALL use declared field/keyset ordering and disclose no global FTS scores, snippets or statistics. Text capability SHALL be default-off until declared; unsupported/unready text queries SHALL return typed unavailable while ordinary queries remain available.

#### Scenario: Text combines with filters and withheld-as-absent
- **WHEN** a text term matches visible and withheld rows and a scalar filter further restricts results
- **THEN** only admitted matching rows affect results, counts and order, identically to the rows-absent twin

#### Scenario: FTS is unavailable without a scan fallback
- **WHEN** SQLite lacks FTS5 or the declared index is not ready
- **THEN** the text query reports unavailable without materializing the collection, and non-text indexed queries still run

### Requirement: Tools and saved views share one bounded query contract

Collection-mode `query_data`, Records/declared-type `record_memory` and Planning `plan_memory` SHALL expose the same optional query object through one generic leaf, with CLI/REST parity and existing facade checks. Legacy requests and frozen hosted candidates SHALL retain their contracts; mixing old shaping arguments with the object SHALL fail. Type saved views SHALL validate this grammar and bind only typed value parameters. Existing compiler consumers SHALL preserve authored view selection, served-state restrictions and tighter budgets; no new compiler lane or kind/ranking inference SHALL be added.

#### Scenario: The same saved view works across allowed surfaces
- **WHEN** an authorized caller executes a declared query view through its facade, collection query path and existing compiler consumer
- **THEN** each uses the same query semantics and snapshot, with only its documented projection and tighter output budget

#### Scenario: Dataset queries keep their existing contract
- **WHEN** a CSV/TSV/JSON dataset request supplies a collection-only query object
- **THEN** it is refused rather than routed to the collection engine or silently changing the dataset grammar

#### Scenario: View declaration grants no release
- **WHEN** a saved view names a declared relation whose target is not released to the executing audience
- **THEN** execution follows the same missing/withheld target-collection refusal as an ad hoc query

### Requirement: Response bounds preserve completeness and continuation

The whole serialized v1 result SHALL cap at 64 KiB. It SHALL label source, query/schema version, frozen time, returned count and completeness; exact visible totals SHALL be opt-in and budgeted. Byte-limited pages SHALL report partial/truncated state and continue after the last emitted result. A single oversized result SHALL fail without a looping cursor. Failed or unavailable counts/aggregates SHALL never be represented as zero or complete. Input SHALL cap at 16 KiB, 64 predicate leaves, depth 8, 100 membership values per leaf, 32 text terms and 4 KiB text.

#### Scenario: Byte bound resumes without skipping a fetched row
- **WHEN** response bytes exhaust before the row-count limit
- **THEN** the result marks byte truncation and its cursor resumes with the first authorized row not emitted

#### Scenario: Failed optional total is visibly unavailable
- **WHEN** a bounded page succeeds but its requested exact total cannot complete under the budget
- **THEN** the page labels the total unavailable and returns no invented zero or unqualified complete statistic

### Requirement: Query admission and cancellation bound resource use

Before execution the planner SHALL check at most 100,000 estimated authorized row visits and the join/group limits, refusing unbounded work. Execution SHALL have a 200 ms deadline and SQLite progress-handler cancellation checked at least every 1,000 VM instructions, with deadline checks around non-SQL work. Readers SHALL use an 8 MiB page cache, at most 2 connections per vault, bounded 128-row fetches and disk-backed intermediate state capped at 64 MiB/query. Incremental query-reader peak RSS SHALL be at most 16 MiB and shared release cache at most 8 MiB/vault. No full-collection Python materialization SHALL occur.

#### Scenario: Expensive or interrupted work returns no partial reduction
- **WHEN** the estimated cost exceeds the limit, actual execution times out, temp space exhausts or the caller cancels
- **THEN** execution releases cursors, authorization temporaries and handlers and returns a typed failure without complete-looking counts or aggregates

#### Scenario: Next caller inherits no authorization state
- **WHEN** a reader is reused after timeout or disconnect under a different audience
- **THEN** it starts with fresh admission and cannot read the previous caller's temporary release set

### Requirement: Physical plans and release benchmarks prove the targets

Invented-data release tests SHALL assert declared-index access through EXPLAIN for representative filter/sort/keyset and relation lookups, avoiding base scans/temporary sorts where the declared composite index should satisfy the query. At 100k rows, warm uniform-release indexed filter/sort/page-50 p95 SHALL be ≤10 ms and count/sum/avg/min/max grouping into at most 100 groups ≤50 ms. Joins/FTS SHALL meet ≤25 ms and exact percentile ≤150 ms. Mixed-release filter/group p95 SHALL meet ≤25/75 ms with unchanged release basis. The matrix SHALL report cold/mixed cases, memory, WAL/write ratios and parent regression gates separately.

#### Scenario: The declared composite index actually serves the query
- **WHEN** an invented 100k-row fixture executes a matching equality/range plus ordered page query
- **THEN** EXPLAIN proves use of its declared index without a base scan or unnecessary ORDER BY temporary B-tree, and recorded p95 meets the gate

#### Scenario: Cold governance is not disguised as warm performance
- **WHEN** the release benchmark varies policy cache state, withheld rows and index profile
- **THEN** it records each scope and seed, separate timings/memory/write costs and no success claim for an unmeasured case

### Requirement: Preview release follows prerequisites and preserves portability

The engine SHALL remain default-off until parent P1a prerequisites and phase gates pass, with internal work parallel to P2–P4 and declared-type public surfaces gated on P4/P5. Release SHALL require integrated correctness, installed-wheel proofs, performance/privacy checks and non-frozen contract refresh. Canonical data and query schema SHALL travel in the existing single-store snapshot. Disabling preview SHALL preserve legacy queries and canonical writes; older readers SHALL refuse unsupported schema versions rather than corrupt the store.

#### Scenario: Failed optional upgrade preserves an existing vault
- **WHEN** an index/FTS build or preview enablement fails on an existing store
- **THEN** the previous ready query contract and canonical data remain intact, with typed unavailability for the new capability

#### Scenario: Portability keeps indexed queries consistent
- **WHEN** a validated one-store snapshot is restored under a compatible query-engine reader
- **THEN** declared schemas, projection mappings, governed query results and audit history agree with the exported state
