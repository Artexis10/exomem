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

A type or collection SHALL declare scalar fields as filterable/sortable and optional composite indexes. For json-v1 collections the store SHALL maintain generated projection columns and matching indexes; for canonical typed-v1 collections it SHALL index direct typed scalar/derived exact comparison columns without requiring duplicate generated JSON columns. Both SHALL use schema migrations, never query-triggered DDL. Each collection SHALL allow at most 8 secondary B-tree indexes, 4 keys per index and 16 distinct indexed scalar paths; identity/natural-key indexes are exempt and relation indexes count toward the limit. A governed declaration preview SHALL report additions, removals, backfill and dependent-view impact.

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

Version 1 SHALL support all existing operators, boolean composition, inclusive/exclusive `between`, explicit `is_null`, `is_missing` and `is_not_null`, and day/week/month date math frozen to one `as_of`. Typed comparisons SHALL exclude missing/null values; `is_null` SHALL match only present null, and `is_not_null` SHALL include present empty strings. Instants SHALL require the source UTC offset and store UTC plus that per-record UTC offset; date-only summaries SHALL retain the supplied local date without inventing an instant. Calendar day/ISO-week/month buckets SHALL use the source-recorded local date for each record. Missing both offset and local date SHALL be flagged, never guessed from the host zone or other records; import preview SHALL verify the real export's time fields before start. Frozen UTC relative-window month arithmetic SHALL clamp to a valid last day. IANA/DST machinery SHALL be deferred. Invalid types, dates and reversed ranges SHALL fail validation.

#### Scenario: Null is distinct from absent and empty
- **WHEN** rows contain an absent field, present null and a present empty string
- **THEN** `is_missing`, `is_null` and `is_not_null` select their defined distinct cohorts without changing legacy `exists`/`missing`

#### Scenario: Relative range is frozen across pages
- **WHEN** a caller continues a query with an as-of-relative date range after the clock crosses midnight or a month boundary
- **THEN** the same frozen instant/window and declared source-local bucket basis apply to every page

#### Scenario: Source local day is never guessed
- **WHEN** records straddle UTC midnight with different source offsets, daily summaries supply only a local date, or a record has neither offset nor date
- **THEN** preview identifies the real time fields, import/queries/rollups agree on each supplied local day and date-only summary, and the missing-basis record is flagged under explicit stop/skip policy without inventing a timezone

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

Version 1 SHALL support up to 4 group keys, source-local day/ISO-week/month buckets, 8 named aggregates (`count`, `sum`, `avg`, `min`, `max`, `percentile`, `distinct_count`) and HAVING over group keys/aggregate aliases. Percentile SHALL be exact continuous interpolation at `(n-1)*p`, with p in [0,1]. Null/missing SHALL be separate group keys and excluded from field reductions. Empty input SHALL produce zero count/distinct-count, null other aggregates, and no grouped rows. Pre-HAVING groups SHALL cap at 1,000; exceeding it SHALL fail rather than silently trim a reduction. Group pages SHALL default/cap at 50/200.

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

Before ordinary interactive execution the planner SHALL check at most 100,000 estimated authorized row visits and the join/group limits, refusing unbounded work. Interactive execution SHALL have a 200 ms deadline and SQLite progress-handler cancellation checked at least every 1,000 VM instructions, with deadline checks around non-SQL work. Readers SHALL use an 8 MiB page cache, at most 2 connections per vault, bounded 128-row fetches and disk-backed intermediate state capped at 64 MiB/query. Incremental query-reader peak RSS SHALL be at most 16 MiB and shared release cache at most 8 MiB/vault. No full-collection Python materialization SHALL occur. Explicit `query.execution_profile="analytics"` on the same MCP collection-query route MAY select a synchronous ceiling of 10 million released visits, 2 seconds for the whole call including admission/serialization and 256 MiB disk temp, preserving 16 MiB reader RSS and existing group/output caps. Profile omission SHALL mean interactive. Compose/preview/explain/dry-run SHALL validate/report selected profile bounds. Analytics SHALL return an exact completed reduction or typed cost/timeout refusal with no partial reduction or resumable half-computed aggregate; it SHALL NOT widen ordinary interactive or compiler budgets.

#### Scenario: Expensive or interrupted work returns no partial reduction
- **WHEN** the estimated cost exceeds the limit, actual execution times out, temp space exhausts or the caller cancels
- **THEN** execution releases cursors, authorization temporaries and handlers and returns a typed failure without complete-looking counts or aggregates

#### Scenario: Next caller inherits no authorization state
- **WHEN** a reader is reused after timeout or disconnect under a different audience
- **THEN** it starts with fresh admission and cannot read the previous caller's temporary release set

#### Scenario: Agent explicitly selects bounded large analysis
- **WHEN** an agent previews and executes a million-row exact reduction that declared rollups cannot answer using query.execution_profile="analytics"
- **THEN** the existing MCP route applies the published 10-million-visit/2-second whole-call ceiling and returns an exact completed result or cost/timeout refusal, while omission retains 100,000 visits/200 ms and activation remains within its 30 ms stage

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

### Requirement: Agent query lifecycle supports self-correction

The system SHALL expose compose, explain, safe cost preview, dry-run and execution through one versioned query envelope on existing non-frozen MCP tool names, sharing the same IR and query leaf with CLI/REST. Explain SHALL identify authorized logical indexes/access stages, bounds and release strategy without raw SQL or hidden statistics. Compose SHALL normalize/validate without value reads; dry-run SHALL bind/admit/prepare without executing results or persistent changes. Saved views SHALL accept typed value parameters only and SHALL be revalidated after incremental query refinement. Errors SHALL carry a stable code, request field, expected/allowed released values, repair guidance and retryability suitable for an agent to correct its request.

#### Scenario: Agent refines an expensive query
- **WHEN** an agent previews a wide time-range reduction beyond admitted cost
- **THEN** it receives a cost refusal or unknown bound with safe guidance to narrow the window or use an exact declared rollup, can compose/dry-run the revised request and execute it through the same MCP tool

#### Scenario: Explain exposes useful index information without SQL
- **WHEN** an admitted equality/range and ordered page uses a declared index
- **THEN** explain names that released logical index and access stage while omitting physical SQL and withheld cardinalities

#### Scenario: Dry-run has no persistent effect
- **WHEN** a saved or ad hoc query is dry-run
- **THEN** binding/governance/cost and preparation findings are returned without result iteration, schema changes, collection writes or audit effects

### Requirement: Large collections use compact typed storage and streaming ingest

Large declared collections SHALL support a migration-managed canonical typed-column encoding in the single per-vault store, retaining stable identities, immutable version/source history, logical hashing/audit, governance and the explicit items/summary query/render/edit-back contracts in the MODIFIED structured-collections requirements. Declared dense scalars SHALL not require a full JSON row plus duplicate JSON projection; sparse residual objects MAY retain bounded JSON. Existing JSON encoding SHALL remain readable and migration SHALL prove logical parity before publishing. Optional bulky-text compression SHALL be lossless, versioned and bounded at decode; indexed scalar fields SHALL remain directly comparable.

The importer SHALL stream authorized preserved-source CSV, NDJSON and JSON arrays through declared mappings, with decoded rows ≤1 MiB/depth 32 and batches ≤500 rows/4 MiB. Each batch SHALL enforce normal mutation authority and atomically commit values/history/audit/checkpoint under one idempotent identity. Preview/start/status/cancel SHALL be agent-reachable through existing MCP names with bounded responses. Invalid/cancelled/interrupted imports SHALL report actual partial state and resume positions, never a fabricated complete import. Additional import peak RSS SHALL be ≤64 MiB independent of collection size. Time-series indexes SHALL support timestamp/type orders within the declared index budget.

#### Scenario: Crash after a committed import batch
- **WHEN** a streaming job loses its response after a batch commits and retries the same source/job/batch identity
- **THEN** rows, version/audit and checkpoint are returned exactly once without duplicate effects, and later work resumes from the next source position

#### Scenario: Invalid row leaves honest partial progress
- **WHEN** a later source row fails declared type validation under stop-on-error policy
- **THEN** previous committed batches remain durable, the current batch is absent and status names failed/partial state, imported count and repairable position without claiming completion

#### Scenario: Typed storage migration fails parity
- **WHEN** reconstructed values/history/hashes differ from the previous encoding
- **THEN** cutover refuses and the previous canonical mapping remains active with no rewritten append-only history

#### Scenario: Million-row capacity gate includes audit
- **WHEN** the deterministic 8-scalar-field/2-index fixture imports at least one million rows
- **THEN** active typed payload/index bytes are ≤96 MiB per million, initial live database including identity/version/source/audit is ≤512 MiB per million, sustained committed throughput is ≥10,000 rows/s, 500-row batch latency retains the parent <1 s gate and all component/RAM/WAL measurements are reported

#### Scenario: Raw export is smaller than audited storage
- **WHEN** the private authorized wearable-export journey compares its roughly 244 MB raw source to the imported store
- **THEN** the report separately names raw bytes, active typed/index bytes, full audited database, retained evidence, replica and WAL peak, and makes no unsupported claim of whole-import compression

### Requirement: Incremental rollups preserve governed exactness

Declared daily/ISO-week rollups SHALL use each record's source-local day basis and SHALL support exact count/sum/avg/min/max/latest and update affected old/new buckets, readiness and generation in the same canonical transaction as every relevant write. Dirty/unproven rollups SHALL be unavailable, never stale-complete. Rollup implementation SHALL begin with uniform release and bounded exact base fallback; maintained mixed partitions SHALL require measured demand and SHALL cap at 8 rollups/collection, 4 group dimensions, 16 total maintained release partitions and 16 affected old/new bucket-partition updates/row mutation, with precommit refusal above bounds. Rollup reuse SHALL require uniform release or exact fully admitted release partitions; a policy splitting a partition SHALL use a bounded governed base reduction or an explicit unavailable/cost result. Percentile and distinct-count SHALL NOT be fabricated by combining insufficient rollup statistics.

#### Scenario: A hidden member would change the maximum
- **WHEN** a mixed-release query runs against a bucket containing a withheld maximum value
- **THEN** result, count, preview and completeness equal the twin with that row absent, using an exact admitted partition or governed base query

#### Scenario: Update changes time bucket and minimum
- **WHEN** a governed update moves an item between dates and replaces the previous bucket minimum
- **THEN** both buckets, recomputed minimum, row state and generation commit together or all remain at the old state

#### Scenario: Policy change splits a rollup partition
- **WHEN** release policy changes after a rollup/result cache was populated
- **THEN** the old rollup eligibility and cached query are invalidated before serving results, with no stale aggregate or false zero

### Requirement: Unified query IR preserves governance across backends

The system SHALL use a backend-neutral typed IR for documents, collection rows and graph references with stable semantic/ontology identities. Source scans, declared joins, reductions and sibling traversals SHALL carry one admission plan; every backend SHALL compile its release predicates before projection/reduction. SQLite SHALL be the only delivered backend in this change. Unknown or unavailable domain/operation capabilities SHALL refuse explicitly. Future backends SHALL pass the same semantic/rows-absent corpus and cancellation/snapshot contract without requiring changed agent queries or weakened release rules. Markdown documents SHALL remain file-canonical, with current read-only metadata/unit projections. Scale tiers SHALL distinguish measured embedded gates from future hosted/billions targets and use explicit migration/equivalence proof for promotion.

#### Scenario: Same request targets a backend lacking admitted traversal
- **WHEN** the fake adapter used by contract tests does not support a required graph/release operation
- **THEN** the unchanged structured query receives typed unsupported/unavailable rather than an unrestricted fallback or rewritten semantics

#### Scenario: Unified document and row query
- **WHEN** an agent queries declared links between a collection row and a knowledge-page dimension after the sibling capability is ready
- **THEN** both sources use the shared IR/admission plan and returned definitions/provenance describe only released rows/pages

### Requirement: Activation serves query-derived units within exact freshness budgets

On every matching turn, `activate_context` SHALL select query-derived working-set units through declared surfacing cues/match_fields/anchors/roles and kind restrictions, using authored views or closed deterministic query recipes through the shared leaf. Units SHALL state metric, window, source collection, query/fingerprint, released row count or explicit unknown, provenance and complete/partial/unknown/failed state. Unknown, partial or failed reads SHALL NOT be shown as zero or a complete trend. The combined collection/graph stage SHALL have a 30 ms hard deadline and ≤30 ms p95 within <1 s p95 activation; at most two collection queries and one sibling graph request SHALL execute. It SHALL abstain on deadline/unproven freshness, not fall back to unbounded work.

Each query unit SHALL fit 2,048 UTF-8 bytes, collection units 4,096 bytes total, one graph unit 2,048 bytes and the combined stage 6,144 bytes or the active packet's smaller remaining allowance, preserving existing mandatory continuity/tool/bootstrap budgets. Cache keys SHALL contain query fingerprint, participating collection generations and audience, with policy/window/schema/view/vocabulary/projection basis in the fingerprint. Writes and policy/ontology/projection changes SHALL exactly invalidate dependent entries; stale aggregate reuse SHALL be forbidden.

#### Scenario: Resting metric turn produces a sourced unit
- **WHEN** a declared wearable type matches a last-30-days resting-heart-rate turn
- **THEN** the compiler serves a bounded released latest/trend recipe unit identifying metric, frozen window/source-local day basis, collection, count/completeness and normalized query without exposing raw SQL

#### Scenario: Query timeout is not an empty window
- **WHEN** a matched query cannot finish admission or execution inside the shared 30 ms stage deadline
- **THEN** that unit abstains with explicit failed/unknown state and no zero or complete trend, while independent ready units may remain

#### Scenario: Cached renewal count changes after a write
- **WHEN** a write changes a subscription's renewal month or a participating collection generation
- **THEN** the cached renewing-this-month view is invalidated transactionally and the next unit uses the new exact released count/window/source

#### Scenario: Activation gains preserve current gates
- **WHEN** the extended benchmark runs at least 30 query-derived and 30 sibling path-derived turns and their negative/cold twins
- **THEN** correct unit inclusion improves by at least 15 percentage points per new family, irrelevant units are ≤1%, existing continuity/corpus gates and sub-second p95 pass, and withheld disclosure/false-zero cases remain zero

### Requirement: Every capability is discoverable and agent-reachable

Structured queries, lifecycle/explain, aggregates/rollups, big-data import, saved views, sibling graph/path/ontology operations and query/path compiler units SHALL have an existing-name MCP route and generic skill instructions, discoverable through bootstrap core route stubs and the concrete existing-tool read routes in Existing tool routes preserve text queries and bounded discovery. Publication SHALL preserve #1458 tool-schema diet, #1464 compact-core ceiling (currently 63,300 serialized bytes), route visibility, response limits and frozen adapter schemas. A capability without a successful real-MCP agent-task journey SHALL NOT count as delivered.

#### Scenario: Agent discovers and runs every delivered capability
- **WHEN** held-out invented row/analysis, graph/mixed and import/view/refinement tasks run through the installed MCP surface and current generic skill
- **THEN** each capability has a successful end-to-end case, per-family correct final answers are ≥95%, first executable queries ≥90%, repair within two revisions ≥90%, median calls ≤4/p95 ≤8 excluding separately reported long import polling, and governance/false-complete/false-zero failures are zero

#### Scenario: Internal tests pass but the tool route is absent
- **WHEN** a feature works through a Python leaf but its current MCP adapter cannot discover or invoke it
- **THEN** the capability release gate fails until its tool/skill/adapter journey succeeds

#### Scenario: Grammar exceeds bootstrap core budget
- **WHEN** lifecycle or graph guidance would increase compact core above its existing ceiling
- **THEN** details move to bounded schema_memory inspect chapters, the core route remains discoverable and no ceiling is increased or frozen schema changed

### Requirement: First owner slice is a governed new summary collection in the real vault

The first slice SHALL run in the owner's real vault, preserving the raw export as immutable Source/Evidence before preview/import into a NEW collection of the extensible built-in Records type with collection-local fields/natural key and summary view mode. It SHALL NOT migrate existing Records or require a preview vault. The store SHALL remain outside the vault folder under the existing placement contract. Merged parent P1a writer/registry/lease/parity and row-level release/egress for store rows SHALL ship before any agent or connector read. Streaming typed import, structured filter/order/keyset/count/sum/avg and exact daily rollup SHALL be reachable through existing record_memory describe/create/import/query routes and SHALL precede joins, FTS, ontology, graph and generic activation. The S1 gate SHALL prove the real installed-MCP preserved-source → create → preview/import → status/resume → governed query/daily sourced answer journey against independent expected rows/counts/values/local days, plus authority/crash/retry, placement/portability, schema-byte, storage/RSS/throughput and integrated correctness checks. It SHALL NOT require P4/P5 arbitrary type authoring or waive parent P1b/P2/P3 for existing-collection migration. Full capability/graph gates SHALL remain separate.

#### Scenario: Owner queries the real export before unrelated capabilities
- **WHEN** S1 prerequisites and the real-MCP gate pass for a newly created summary Records collection
- **THEN** the owner can obtain governed daily counts/sums/averages from the real preserved export without waiting for joins, FTS, ontology, graph or generic activation, and all existing Records remain unmigrated

#### Scenario: Pre-GA source replay is explicit
- **WHEN** an incompatible pre-GA schema change affects the new summary slice
- **THEN** the operator may require a new authorized import from the preserved immutable Source/Evidence under a compatible schema, with old progress/lineage reported honestly and no implicit migration of existing Records

#### Scenario: Location samples stay withheld until released
- **WHEN** the imported export carries GPS route or place samples and an external connector or hosted client queries the collection
- **THEN** location fields are withheld from results, counts, previews, summary pages and rollups until the owner releases them, and the rest of the row's released fields are still returned

### Requirement: Typed numeric values and history are lossless

Canonical typed-v1 SHALL preserve the full accepted integer/finite-number domain, int versus float subtype and signed zero. A discriminator SHALL select signed-64-bit INTEGER, exact canonical decimal TEXT for larger integers, or authoritative IEEE-754 binary64 BLOB for floats; REAL and sortable comparison keys MAY be derived, never canonical hash input. Comparisons/reductions SHALL not narrow oversized integers through REAL. Decode SHALL reconstruct original scalar subtype and exact canonical JSON input through existing normalization/payload hashing without a duplicate full JSON row. The forward schema SHALL introduce version_identity keyed by (row_id,row_version) with encoding/hash/txn/schema identity, immutable JSON and typed history payloads, sources FK to that spine and BEFORE UPDATE/BEFORE DELETE abort triggers on all new history tables. Migration SHALL preserve old immutable bytes, hashes, audit/source identities and prove full parity before atomic mapping publication; older readers SHALL refuse unsupported schema/encoding before any access. Direct typed indexes and generated json-v1 projections SHALL expose one logical query interface.

#### Scenario: Numeric counterexamples round-trip exactly
- **WHEN** fields contain 1, 1.0, -0.0, 9007199254740993 or 2**63
- **THEN** typed current/version decoding preserves value, int/float subtype, signed zero, hash input and exact comparisons/reductions without REAL rounding or SQLite integer overflow

#### Scenario: New history rejects mutation and orphan sources
- **WHEN** a caller updates/deletes a typed_versions or version_identity row, or inserts a source without its (row_id,row_version) identity
- **THEN** the triggers/FK abort and immutable version/source/audit history is unchanged

### Requirement: Import jobs retain continuing source and target authority

An import job SHALL bind its initiating principal, live authorization session/grant identity/expiry, purpose/policy basis, immutable source receipt/hash/lineage, exact target collection/store lineage and declaration/mapping version. It SHALL re-resolve applicable principal authority and source release before each batch and recheck at commit under the writer lease, including the parent's complete-authorized-state target mutation rule. Ambient service authority or an initial cached grant SHALL never substitute. Source revocation, expiry, target-authority loss or invalid lineage SHALL roll back the current batch and pause with authority_lost and honest partial acknowledged progress. Explicit resume SHALL reauthorize the same initiating principal and unchanged source/target basis; a different principal SHALL NOT inherit it implicitly. Service-host takeover SHALL re-prove lineage and live authority before replay/commit. Status and cancel SHALL separately authorize source, target and job ownership. Protected credential state SHALL never appear in pages/receipts. Refused status SHALL not disclose hidden job identifiers or counts.

#### Scenario: Revocation pauses between batches
- **WHEN** source release or full-state target authority is revoked after one batch, including a target becoming mixed-release
- **THEN** the next batch or commit recheck refuses without additional rows/audit/checkpoint advancement, reports partial progress only to an authorized caller and resumes only after explicit renewed authority

#### Scenario: Expiry or takeover cannot widen job authority
- **WHEN** the initiating grant expires or another host/principal attempts takeover after a committed batch
- **THEN** no ambient owner authority completes the import, host takeover proves the original binding, a different principal's continuation refuses and acknowledged batches remain durable once

### Requirement: Existing tool routes preserve text queries and bounded discovery

connect_memory SHALL retain its existing bounded string query argument. Only operation=query SHALL accept the separate shallow query_request object (≤16 KiB) and SHALL refuse text query/unrelated operation arguments; existing operations SHALL reject query_request. On-demand guidance SHALL use schema_memory(subject=query-engine, operation=inspect, name=collections|import|graph) and schema_memory(subject=ontology, operation=inspect), bounded at 64 KiB with capability/version and no writes; record_memory(action=describe) SHALL describe collection fields/views/indexes. Bootstrap SHALL keep its existing profiles with a route stub and no section accessor. Mixed saved views SHALL use schema_memory(subject=query-views, operation=inventory-query-views, limit, continuation) and operation=inspect-query-view with name, beside diff-query-views/save-query-views. Inventory SHALL default to 20/cap at 100; definitions SHALL cap at 100/vault and 16 KiB each, with typed value parameters and release-qualified dependency validation. Missing/withheld definitions/dependencies SHALL have identical public behavior. The exact schema/description/route additions SHALL have a separate measured before/after byte/fingerprint delta against generated installed adapters at S1 and integrated Q5/G5 gates, fitting existing schema/core/route ceilings without reusing the parent's 400-byte allowance or changing frozen adapters.

#### Scenario: Legacy text and structured graph calls stay distinct
- **WHEN** an agent uses legacy graph-context with string query, then operation=query with query_request, and attempts either envelope on the wrong operation
- **THEN** the first two use their respective validated routes and the crossed arguments refuse without coercion or silent routing

#### Scenario: Agent inspects grammar and mixed views through real tools
- **WHEN** an agent calls schema_memory inspect for query-engine/ontology and inventory-query-views then inspect-query-view
- **THEN** bounded current guidance/authorized definitions are returned, hidden/absent twins match and installed schema/core measurements cover exactly those invocable routes
