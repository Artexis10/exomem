## Purpose

Let agents and the context compiler query and explain typed epistemic paths across knowledge and collection rows through one governed, bounded, ontology-aware query model, preserving file authority and embedded per-vault operation.

## ADDED Requirements

### Requirement: Traversal applies release decisions at every hop

The shared structured IR SHALL support anchored variable-length typed traversal with 0≤min_hops≤max_hops≤5, direction filters, transitive relation-type filters and declared row/page links. SQLite SHALL compile recursive CTEs over admitted node/edge relations with bound values and closed identifier templates. Seeds, every traversed endpoint, edge author and required evidence obligations SHALL be authorized before frontier expansion. A withheld node/edge/author SHALL break a path as if absent, not appear in a post-filtered result. Unknown/withheld anchors SHALL have the same not-found response. Cross-vault, arbitrary join keys and raw SQL/Cypher SHALL be refused.

Default work SHALL be bounded by 64 released edges per expanded node (absolute explicit maximum 256), 10,000 admitted nodes, 50,000 edge visits, 10,000 path-state expansions, 1,000 total paths, 200 returned nodes/400 edges, 64 KiB output, 200 ms deadline, ≤16 MiB additional reader RSS and 64 MiB temp. Compiler bounds SHALL be smaller. Preview/caps/counts SHALL depend only on admitted state. Preview SHALL bound distinct nodes/edges separately from repeated edge visits/path-state expansions and SHALL NOT cap repeated work at distinct graph cardinality; physical timeout SHALL report availability without falsely claiming complete traversal.

#### Scenario: Withheld middle hop cannot connect visible endpoints
- **WHEN** visible A and C connect only through withheld B or a withheld/author-withheld edge
- **THEN** traversal, paths, counts, safe explain/preview and continuation equal the twin with that node/edge absent, and no shortcut or withheld reference is returned

#### Scenario: Row-to-page traversal uses only declared links
- **WHEN** an admitted row links through a declared current-page relation and then a typed epistemic edge
- **THEN** the mixed path joins under one admitted snapshot with released endpoints/author/evidence and no arbitrary field-key inference

#### Scenario: Degree cap is exceeded
- **WHEN** an admitted expansion exceeds its released fan-out/work budget
- **THEN** exhaustive path/aggregate queries refuse with a repairable graph-cost error, while explicitly partial neighbourhood output carries its reason and valid continuation

#### Scenario: Reconverging paths repeat edge work
- **WHEN** a five-hop reconverging layered graph has 10 admitted nodes, 16 distinct edges, 16 simple paths and 46 enumeration edge visits
- **THEN** preview/admission account for at least 46 repeated visits with a separate distinct-entity bound, and path-state/runtime limits cannot be bypassed using the smaller edge cardinality

### Requirement: Path and pattern queries retain epistemic explanations

The system SHALL support bounded unweighted shortest simple path, all simple paths up to k≤5, anchored typed pattern matching (including A supports B while C contradicts B), optional matches, reused variables and exact path count/distinct endpoint/min/max length/group reductions. Shortest SHALL be declared only after shorter admitted depths are exhausted, with deterministic stable-ref tie order. All-path completeness and aggregate exactness SHALL require exhausted admitted enumeration; path/work caps SHALL refuse rather than return a complete-looking sample. Parallel edges SHALL retain distinct authored identity, and no path SHALL repeat a node.

Every returned path SHALL carry ordered nodes, typed edge refs/orientation, authored raw/current canonical relation and definition version, released source/evidence refs and authored currency/projection basis. Interpretation SHALL quote those relationships without invented evidence or epistemic authority. Optional matches SHALL preserve an admitted left endpoint when the right endpoint is absent/withheld; user right-side predicates retain their explicit semantics.

#### Scenario: Equal shortest paths have a stable explanation
- **WHEN** two released paths have the same minimum length
- **THEN** the stable node/edge-ref ordering selects the same path, each step is quotable with type/source/evidence and no longer or unproven path is called shortest

#### Scenario: All paths reach the cap
- **WHEN** more than 1,000 admitted simple paths or more than 10,000 path states are required
- **THEN** the exhaustive request and its exact count refuse rather than label the returned prefix complete

#### Scenario: Supports and contradicts pattern
- **WHEN** the agent anchors A and asks for A -[supports]-> B <-[contradicts]- C
- **THEN** all qualifying admitted variable bindings within bounds match the independent reference, with path provenance and optional-match behavior equal to hidden/absent twins

### Requirement: Dynamic ontology resolves transitive types and stable identities

Relation types, open-vocabulary categories, entity kinds and declared collection types SHALL be first-class query dimensions with stable internal identities and current released definitions or explicit undefined status. Versioned vocabulary/alias/closure projections SHALL live beside graph edges in the same per-vault store and remain derived from governed Markdown/registry authoring sources. Queries by parent relation SHALL include all audience-admitted recursive subtypes by default; exact-only SHALL be explicit. Type-hierarchy cycles SHALL be rejected. Is-a/part-of instance closure SHALL traverse admitted hops and preserve path provenance, never use global closure to bypass release.

Renames SHALL preserve identity/aliases; compatible merges SHALL preserve historical IDs/edge identities and revalidate current saved views/compiler bindings without coalescing authored parallel edges. Retirement and incompatible replacement SHALL follow Retired and replaced relation types preserve authored identity. Semantic incompatibility SHALL make a view explicitly unavailable with correction guidance. Vocabulary changes SHALL invalidate affected closure/query/cache bases before serving new results. Agents SHALL have explicit existing-name MCP routes for category definition/rename/merge via schema_memory subject ontology diff-ontology/save-ontology with immutable source-hash guard/dependency preview and existing schema mutation confirmation; relation/entity-kind operations SHALL delegate to existing family writers. Ordinary open category authoring SHALL remain available through observe_memory without mandatory definition registration. Agents SHALL author/reuse definitions through existing vocabulary governance and then query the acknowledged version immediately, or receive a typed warming result rather than falsely empty data.

#### Scenario: Custom subtype recursively rolls up
- **WHEN** corroborates extends supports and independently_corroborates extends corroborates
- **THEN** a supports filter includes both subtypes with their current released definitions, while exact-only supports excludes them

#### Scenario: Saved view survives a renamed category
- **WHEN** a category label changes under the same stable term identity
- **THEN** the view and compiler query remain valid through aliases/current identity, return the new definition/version and invalidate prior cached bindings

#### Scenario: Agent defines and renames a category with source guards
- **WHEN** an agent previews an ontology category definition/rename and performs the authorized source-hash-guarded save through schema_memory
- **THEN** the acknowledged version returns its authored definition/stable identity, existing saved views follow the alias and an intervening source change refuses rather than overwriting another definition

#### Scenario: Withheld hierarchy intermediate breaks closure
- **WHEN** two released entities are connected by is-a/part-of edges only through a withheld intermediary
- **THEN** transitive results/explanations equal the twin without that intermediary

#### Scenario: New governed type is used in the same conversation
- **WHEN** an agent completes the existing reviewed/authorized definition write
- **THEN** the current acknowledged vocabulary version accepts the type in query/preview, or an unacknowledged projection returns explicit warming without silently empty results

### Requirement: Graph projection shares the vault transaction store

The system SHALL migrate derived graph nodes/edges, document metadata, vocabulary and replay/projection metadata into the single per-vault SQLite store while preserving canonical collection data/history/audit and Markdown/registry authority. Collection row/link/edge updates SHALL commit transactionally together. File/registry writes SHALL retain durable replay/checkpoints and current-source/semantic/registry proof; no false atomicity across files and SQLite SHALL be claimed. Graph rebuild/cutover SHALL publish only derived table mappings and coherent generations, never replace/reinitialize the combined store file or drop canonical tables. A graph-only rebuild SHALL leave canonical collection commit head/audit unchanged. Backup/portable restore SHALL prove current derived bases before graph results are served.

#### Scenario: Graph rebuild overlaps canonical writes
- **WHEN** a replacement graph projection is built while a collection write commits
- **THEN** cutover preserves that row/history/audit/head and publishes only a fenced proven graph mapping, with no whole-store file replacement

#### Scenario: Crash after Markdown write before projection acknowledgement
- **WHEN** source bytes commit but replay/projection publication is interrupted
- **THEN** graph/mixed reads return warming/unavailable until a coherent current checkpoint is proven, while fresh independent collection reads remain available

#### Scenario: Failed migration retains previous placement
- **WHEN** copied graph/vocabulary/replay parity or source proof fails before cutover
- **THEN** the previous proven placement remains active, work remains queued and canonical collection state is untouched

### Requirement: Agent and compiler use the shared graph lifecycle

Graph/path/pattern/ontology queries SHALL be discoverable through schema_memory(subject=query-engine, operation=inspect, name=graph) and schema_memory(subject=ontology, operation=inspect), bootstrap route stubs and generic skill guidance, using connect_memory(operation=query, query_request=…) and the shared compose/explain/preview/dry_run/execute envelope. Existing connect_memory.query SHALL remain a bounded string on existing operations; operation=query SHALL refuse text query and existing operations SHALL refuse query_request. Mixed views SHALL expose schema_memory subject=query-views inventory-query-views/inspect-query-view alongside diff/save, under the collection sibling's definition/paging/authorization limits. Exact installed-adapter schema/core/route byte/fingerprint deltas SHALL be measured as the integrated union before publication, without reusing the parent 400-byte allowance. Saved mixed views SHALL be governed, typed and versioned; errors SHALL be repairable without leaked SQL/hidden refs. Existing vocabulary authoring routes and confirmations SHALL remain binding. Real-MCP agent journeys SHALL cover each capability; absent agent access SHALL fail delivery. Tool-schema/core ceilings and frozen adapters SHALL be preserved.

Graph-aware `activate_context` SHALL expand resolved anchors along authored supersedes/supports/contradicts and declared links, preserving authored-currency rules and path provenance. It SHALL use at most 3 hops, 8 released neighbours/node, 64 nodes/128 edge visits, 2 paths and one graph request within the shared 30 ms stage deadline (10 ms graph allocation), 2,048-byte graph unit and 6,144-byte combined stage cap. Stale/unproven/costly traversal SHALL abstain. Private cache freshness SHALL depend on audience/policy, collection generations, proven graph/vocabulary basis and exact query fingerprint. Public provenance/cursors SHALL expose only released-dependency semantics under Graph continuation separates public basis from private freshness. Hidden successors SHALL not suppress visible predecessors or disclose currency beyond release.

#### Scenario: Currency chain explains why a unit is current
- **WHEN** a resolved anchor has a released authored supersedes chain within compiler limits
- **THEN** the selected current unit carries the admitted path and authored currency/source basis, without treating file mtime as supersession

#### Scenario: Hidden successor is not a currency signal
- **WHEN** a visible predecessor's only successor is withheld
- **THEN** the compiler behaves as with that successor absent and does not name it or suppress the predecessor via its hidden path

#### Scenario: Graph stage misses its deadline
- **WHEN** freshness/admission/traversal cannot complete within the shared activation stage limit
- **THEN** path-derived output abstains with an explicit availability reason and no false complete/zero state, and an independent ready collection unit can still be served

### Requirement: Graph benchmark proves the personal-scale baseline

Release SHALL require a deterministic invented 100,000-node/500,000-edge corpus, independent reference/rows-absent correctness, 3-hop typed traversal warm p95≤50 ms and matched multi-hop/path/pattern p95 no greater than a pinned Neo4j baseline with equivalent corpus, semantics, release/work bounds, indexes and output. Governance/cold/setup/transport SHALL be reported separately and equivalent Neo4j application-governance comparison SHALL be included. At least 99% admitted moderate-degree requests SHALL finish. The versioned capability matrix SHALL honestly identify matched/beaten/deferred capabilities, including PageRank/community/centrality and server/distributed limits; unrestricted Cypher/GDS parity SHALL NOT be claimed.

Real-MCP graph/mixed/ontology tasks SHALL meet ≥95% correct answers, ≥90% first executable queries, ≥90% fixable-error recovery within 2 revisions and zero governance failures. Provenance/currency/context correctness SHALL improve ≥15 percentage points over bare Neo4j and remain at least equal to an application-equivalent baseline. Activation continuity/corpus gates, ≤1% irrelevant-unit rate and sub-second p95 SHALL remain passing. Numerical targets SHALL be measured gates, not asserted achievements.

#### Scenario: Dedicated engine is faster on a required task
- **WHEN** a matched required personal multi-hop/path/pattern cell misses the absolute or comparative p95 gate
- **THEN** graph capability remains preview and no “better”/latency parity claim is published until the failing gate is met

#### Scenario: Baseline or real agent path is missing
- **WHEN** internal graph tests pass but Neo4j baseline or a required MCP journey has not run
- **THEN** comparative/delivery evidence is pending, not synthesized or treated as complete

### Requirement: Scale tiers preserve engine-independent governance

Graph query IR SHALL use backend-neutral stable references and declared semantics, with no agent-facing SQLite/ROWID/CTE dependency. Embedded support SHALL target 10^5–10^7 personal edges, with separately measured degree/work-bounded p95 targets at 1m/5m/10m edges of 75/100/150 ms. Billion-edge/server backend implementations SHALL remain deferred and SHALL require explicit partition/snapshot/cancellation contracts, release-equivalence tests and migration proof before enabling. Promotion SHALL follow the shared measured latency/memory/disk/single-writer triggers, never silently route to a weaker backend. Unsupported governance/currency operations SHALL refuse.

#### Scenario: Backend adapter cannot preserve a release predicate
- **WHEN** the fake future adapter cannot compile a per-hop edge/author obligation
- **THEN** the unchanged query refuses unsupported rather than leaking an unrestricted intermediate

#### Scenario: Embedded tier is exceeded
- **WHEN** measured degree/work load or store/replica/throughput gates exceed the embedded tier over three benchmark runs
- **THEN** the system reports the bounded limit and proposes an explicit migration, without pretending SQLite supports unrestricted billion-edge traversal

### Requirement: Ontology closure uses the admitted term graph

Subtype and replacement closure SHALL be audience-qualified over the admitted term graph, admitting each term, alias/definition version, hierarchy/replacement assertion and its author/source/evidence obligations before resolving names or computing closure. Visible endpoints SHALL NOT authorize a withheld defining assertion. Hidden intermediate terms/assertions SHALL break closure as absent. A global closure SHALL be reused only after complete release of all its dependencies is proved; otherwise bounded admitted closure or typed unavailable/cost SHALL apply. Released assertion paths SHALL provide provenance. Filtering, counts, definitions, explain/preview, saved views, caches and cursors SHALL all use this closure, separately from admitted instance is-a/part-of traversal.

#### Scenario: Withheld term intermediate breaks subtype closure
- **WHEN** visible supports reaches visible public_leaf only through withheld secret_subtype or a withheld parent assertion, and a visible authored edge uses public_leaf
- **THEN** the supports query's filtering/counts/definitions/preview/saved-view/continuation agree with the twin omitting that term/assertion, without exposing a hierarchy shortcut; exact public_leaf matching remains governed by its released identity

### Requirement: Graph continuation separates public basis from private freshness

Every call SHALL privately prove the current source/parser/registry/checkpoint graph generation. A distinct authenticated public continuation basis SHALL bind normalized query/schema/semantics, released anchors, audience/purpose/live policy, frozen time/order and query-relevant released nodes/edges/authors/evidence/ontology dependency identities/versions, including admitted candidates that may affect enumeration. Full projection/vocabulary/store generations SHALL NOT cause public staleness or appear in public provenance. Hidden-only changes and semantically identical rebuilds SHALL preserve cursors when this admitted basis is unchanged after private reproof. Visible dependency or live policy/schema changes SHALL return QUERY_CURSOR_STALE; tampered/malformed/differently bound cursors SHALL return QUERY_CURSOR_INVALID. Unproven freshness SHALL return warming/unavailable, never generation-derived stale or falsely complete. No transaction SHALL span page calls; byte continuation SHALL resume after the last emitted result.

#### Scenario: Hidden edge write leaves a cursor valid
- **WHEN** a caller pages a released graph result and an unrelated withheld edge changes before a coherent generation is published
- **THEN** private freshness is re-proven and the same next paths/count/estimates/availability/continuation are returned as the twin without that hidden change

#### Scenario: Hidden vocabulary write leaves a cursor valid
- **WHEN** a withheld term, definition or hierarchy assertion changes between pages without changing the admitted term graph
- **THEN** the released cursor stays valid through private closure/cache recomputation, with the same result and availability as its absent twin

#### Scenario: Released basis change distinguishes stale from invalid
- **WHEN** a query-relevant released edge/type dependency changes, or a cursor is tampered/rebound to another audience/query
- **THEN** the released change returns stale and the tampered/rebound token invalid, with no hidden diagnostics; a semantically identical projection rebuild alone returns neither

### Requirement: Retired and replaced relation types preserve authored identity

Retired relation types SHALL retain immutable identity, labels/versions, released hierarchy and a retirement tombstone. Existing edges SHALL continue answering exact and parent queries over admitted hierarchy and SHALL be labeled retired in output/definitions/views; new authoring under a retired type SHALL refuse. Compatible merges SHALL rebind current admitted interpretation without coalescing distinct authored edges or changing raw label/version/history. Incompatible replacements SHALL NOT automatically reinterpret existing edges; their original identity/meaning SHALL remain queryable and incompatible view rewrites SHALL require explicit governed authoring. Alias/replacement cycles and reuse of any historical canonical label for another ID SHALL be refused before source save/projection publication. Hidden mappings SHALL not reveal a replacement target or grant authority.

#### Scenario: Retired relation keeps exact and parent matching
- **WHEN** a visible edge's type is retired without a successor
- **THEN** exact and admitted-parent queries and compatible saved views still include its same edge identity labeled retired, while new authoring under it refuses

#### Scenario: Compatible merge retains parallel edges
- **WHEN** two compatible types with distinct authored edges sharing endpoints merge to one admitted canonical interpretation
- **THEN** both edge refs/history/path multiplicities remain distinct and exact counts are unchanged by canonical interpretation rebinding

#### Scenario: Incompatible replacement preserves old interpretation
- **WHEN** a type receives a semantically incompatible successor
- **THEN** existing edges keep their original released interpretation and only an explicitly governed authored change may alter it; incompatible view bindings are unavailable with released repair guidance

#### Scenario: Historical label cannot be reused
- **WHEN** a proposal creates an alias/replacement cycle or assigns a historical canonical label to a different ID
- **THEN** validation refuses without changing term/edge identity or version history
