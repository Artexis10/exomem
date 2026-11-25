## MODIFIED Requirements

### Requirement: Bounded multi-horizon Planning query
Planning query SHALL reuse the structured filter/aggregate grammar and 64 KiB response bound and SHALL default to 100 rows and cap at 1,000. It SHALL support exact horizon and date-window filters, deterministic sort, bounded pagination, `active`/`archived`/`all` lifecycle selection, saved views, projection, bounded grouping, and optional hierarchy expansion. Newly scaffolded manifests SHALL declare `inbox`, `week`, `month`, `quarter`, `year`, and `multi-year` views over active lifecycle. A compatible hand-authored manifest SHALL expose only views it declares; missing views refuse and are never synthesized, while explicit horizon filters remain available.

`hierarchy_mode` SHALL be `none`, `ancestors`, or `descendants`; depth SHALL default to 3 and cap at 8; hierarchy nodes SHALL default to 100 and cap at 500. Expansion SHALL start from the normal page, leave page totals unchanged, and return either null for `none` or exactly `{mode, roots, nodes, edges, max_depth, max_nodes, truncated}`. `roots` is the ordered page plan IDs, `nodes` is the deterministic authorized expanded row list, and each edge is exactly `{parent, child}`. Aggregates and CSV SHALL refuse hierarchy expansion.

Every query response SHALL contain exactly `collection_id`, `snapshot`, `rows`, `returned`, `total_matched`, `truncated`, `continuation`, `derived`, `generated_at`, `rendered`, `output_format`, `aggregate`, `query`, `source_versions`, `view`, `hierarchy`, `agent_history`, and `unresolved_links`. Rows SHALL contain `collection_id`, `plan_id`, `item_version`, `body`, and projected schema fields; `body` is the complete authorized Markdown body and may be omitted only by explicit column projection. `query` SHALL contain the normalized effective filter/projection/sort/date/lifecycle/hierarchy definition; `source_versions` entries SHALL be exact `{path, hash}` mappings; `view` SHALL be null or exact `{name, definition, identity}`; `agent_history` SHALL be null or exact `{status, complete, truncated, events}` capped at 50 content-free events; `generated_at` SHALL be an ISO-8601 UTC datetime; `derived` SHALL be true; absent optional payloads SHALL be null, not omitted.

#### Scenario: Week and multi-year use one schema
- **WHEN** a collection contains work across week, quarter, year, and multi-year horizons
- **THEN** named views select the authored horizons without copying items into horizon-specific stores

#### Scenario: Hierarchy expansion is bounded
- **WHEN** a query requests hierarchy expansion over more nodes or depth than allowed
- **THEN** it applies deterministic caps and reports truncation instead of returning an unbounded graph

#### Scenario: Aggregates and CSV refuse hierarchy expansion
- **WHEN** an aggregate or CSV query requests hierarchy expansion
- **THEN** the query refuses

#### Scenario: A query names the link fields it left out
- **WHEN** a query returns a link field whose bare titles and memory references it could not check
- **THEN** `unresolved_links` lists that field
- **AND** `unresolved_links` is null when the query could check every link field that it returns

#### Scenario: Source change invalidates continuation
- **WHEN** a direct edit changes an authorized item between paginated Planning requests
- **THEN** a continuation bound to the prior snapshot refuses rather than skipping or duplicating work

#### Scenario: No computed progress rollup appears
- **WHEN** a caller queries an outcome with descendants and Records evidence descriptors
- **THEN** the result may show authored fields and bounded relationships but does not invent completion percentages, capacity, or success judgments
