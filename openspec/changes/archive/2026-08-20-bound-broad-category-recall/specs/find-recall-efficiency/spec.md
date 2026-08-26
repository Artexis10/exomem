## ADDED Requirements

### Requirement: Limited Exact Unit Recall Bounds Parent Work

For an empty-query semantic-unit request with a finite limit and a complete exact category/kind plan that requires no canonical structured post-filter, the system SHALL retrieve candidates in stable catalog order through a leading prefix bounded initially by a function of the requested limit. It SHALL apply canonical access policy and selected-parent validation, geometrically expand and recompute the complete prefix when a leading candidate is rejected, and stop after the requested number of eligible hits or catalog exhaustion. Expansion MUST NOT use a moving offset across separate catalog transactions. When leading candidates are eligible, the number of parents opened MUST be independent of total matching-category cardinality. Catalog readiness, exact DNF correlation, scope, and final ordering MUST remain unchanged.

#### Scenario: Broad category opens only a bounded leading window

- **WHEN** at least 128 current accessible semantic units share one exact category, the normalized query is empty, and `limit=3`
- **THEN** the three newest units are returned in the established deterministic order
- **AND** no more than eight candidate parents are validated or hydrated
- **AND** no Markdown scope walk occurs

#### Scenario: Inaccessible leading candidates do not false-empty

- **WHEN** the newest exact category candidates are excluded by canonical access policy and a later candidate is accessible
- **AND** an ordinary catalog writer deletes or reorders a leading row between prefix reads
- **THEN** ordered retrieval recomputes a complete expanded prefix until the accessible candidate is returned or the catalog is exhausted
- **AND** excluded content is never returned

#### Scenario: Page post-filter remains correctness-first

- **WHEN** an exact category seed is combined with a page predicate and more than one bounded window of newer candidates fails that predicate
- **THEN** the system evaluates candidates beyond those windows before applying the result limit
- **AND** it returns the first canonically eligible result rather than an empty list

#### Scenario: Incomplete catalog never becomes an authoritative empty result

- **WHEN** a limited exact unit request encounters a stale, transient, unsupported, or fatal catalog outcome
- **THEN** it preserves the typed incomplete exact-recall behavior
- **AND** it does not return or cache an authoritative empty hit list

### Requirement: Broad Category Latency Is Reproducibly Gated

The aggregate category-recall latency harness SHALL support an explicit broad-cardinality preflight in addition to its selective exact-cardinality default. The broad preflight SHALL require at least a configured number of candidate parents before running timed lanes, SHALL run the same fixed empty-query exact-category request shape, and SHALL apply the existing cold and hot percentile gates. Reports MUST continue to exclude the category, query text, vault path, exact candidate count, note paths, and excerpts.

#### Scenario: Broad profile rejects a selective category

- **WHEN** the broad-cardinality profile requires at least 100 candidates and preflight finds fewer than 100
- **THEN** timed lanes do not run
- **AND** the aggregate report marks cardinality preflight as failed without exposing the exact category or paths

#### Scenario: Broad profile runs the existing latency gates

- **WHEN** broad preflight meets its configured minimum and the catalog is ready
- **THEN** the harness samples the existing page/unit cold and hot lanes
- **AND** the existing filter-eligibility and total-latency percentile thresholds determine pass or failure
- **AND** only bucketed candidate cardinality is reported
