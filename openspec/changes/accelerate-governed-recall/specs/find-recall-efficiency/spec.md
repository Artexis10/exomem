## MODIFIED Requirements

### Requirement: Hot Find Cache With Freshness Invalidation

The system SHALL maintain a small bounded in-process cache for repeated identical `find` requests.
The cache key MUST include every request parameter that can affect ranking or filtering. The cache
MUST be invalidated or bypassed when markdown freshness for the relevant scope changes, when an
embedding or CLIP sidecar that can affect the request changes, or when the active ranking config
identity changes. Cache hits MUST return copies or immutable results so caller mutation cannot alter
future cached responses.

Beyond the hit-list cache, the per-request substrate caches (the lexical corpus,
the eligibility catalogue, the frontmatter cache and the embedding matrix) SHALL
take exact path custody from the governed write receipts: a governed write
invalidates only the entries for the paths its receipt names, and a change in a
whole-scope freshness key MUST NOT by itself discard a substrate cache whose
paths are all covered by exact receipts. A change that arrives without a
receipt, such as an external edit detected by reconciliation, MAY invalidate the
affected scope.

#### Scenario: Repeated identical request can use cache

- **WHEN** the same `find` request is executed twice without vault, sidecar, or ranking-config
  freshness changes
- **THEN** the second call may be served from the hot cache
- **AND** timing diagnostics, when requested, report a cache hit
- **AND** the returned hits match the uncached result

#### Scenario: Markdown edit invalidates cached recall

- **WHEN** a markdown file that is in scope for a cached `find` request is created, edited, moved,
  or deleted
- **THEN** the next matching `find` request does not reuse the stale cached hit list
- **AND** the next result reflects the changed vault contents

#### Scenario: Sidecar freshness invalidates semantic recall cache

- **WHEN** an embedding or CLIP sidecar that can contribute to a cached hybrid, vector, or visual
  `find` request changes
- **THEN** the next matching `find` request does not reuse stale cached semantic results

#### Scenario: Different request knobs do not collide

- **WHEN** two `find` calls differ by query, filters, limit, scope, mode, graph/rerank options,
  date filters, activity preferences, or ranking configuration
- **THEN** they do not share the same cached hit list

#### Scenario: A governed write keeps the substrate caches warm

- **WHEN** one governed page is written while the lexical corpus and eligibility catalogue are loaded
- **THEN** the next recall pays an exact update for that page's rows only
- **AND** the substrate caches are not rebuilt from the scope

#### Scenario: A receipt-less external edit still invalidates

- **WHEN** a page under the scope changes without a governed write receipt
- **THEN** reconciliation invalidates the affected scope and the next recall reflects the edit

### Requirement: Optional Find Timing Diagnostics

The system SHALL expose opt-in timing diagnostics for `find` calls. When requested, the response
SHALL include total elapsed time, cache status, and per-stage timing entries for the retrieval work
that may affect latency, including freshness/cache lookup, keyword, BM25, vector, CLIP, graph,
temporal, fusion, filtering/hit construction, rerank, out-of-KB widening, date filtering, pack
assembly, and serialization. A skipped or unavailable optional lane MUST be represented as skipped
or unavailable rather than causing the call to fail. Timing diagnostics MUST NOT include note bodies,
excerpts, vectors, or other bulk content.

Every reported stage SHALL be a registered interval so the unattributed remainder
is computed from real intervals, and each stage SHALL carry its source (`index`,
`cache`, `declined` or `computed`) so a corpus walk is visible in the diagnostics.

#### Scenario: Timing diagnostics are returned when requested

- **WHEN** `find` is called with timing diagnostics enabled
- **THEN** the result includes `timings.total_ms`
- **AND** the result includes per-stage timing entries for the stages that ran or were skipped
- **AND** the hit ranking is the same as the same request without timing diagnostics

#### Scenario: Timing diagnostics are omitted by default

- **WHEN** `find` is called without timing diagnostics enabled
- **THEN** the response shape is unchanged from the existing default `find` response
- **AND** no timing object is included in the returned hits

#### Scenario: Optional lane failure remains soft-fail

- **WHEN** an optional vector, CLIP, or rerank lane is unavailable during a timed `find` call
- **THEN** `find` still returns the fallback results it would return today
- **AND** the timing diagnostics identify that lane as skipped, unavailable, or failed without
  exposing bulk content

#### Scenario: Every stage is an interval with a source

- **WHEN** a timed `find` runs through the public leaf
- **THEN** every stage entry carries a duration produced by a registered interval and a source value
- **AND** `sum(stage.ms) + unattributed_ms` does not exceed `total_ms`

## ADDED Requirements

### Requirement: Scope Widening Is Opt-In And Index-Backed

A recall with `scope="kb"` SHALL serve the knowledge-base scope only by
default. A caller MAY request out-of-KB widening explicitly; when requested, the
widening SHALL run one catalogue-backed lexical query restricted to the eligible
out-of-KB paths resolved from the same index as the KB eligibility, SHALL
reserve at most `limit - 1` slots, and SHALL NOT walk the vault or rebuild a
lexical corpus on the reader thread. A widening the catalogue cannot serve
SHALL be reported as declined in the diagnostics and omitted, not substituted
by a scan.

#### Scenario: Default KB recall does not widen

- **WHEN** a recall runs with `scope="kb"` and no widening request
- **THEN** no out-of-KB widening stage runs and the diagnostics report it as skipped

#### Scenario: Requested widening is catalogue-backed

- **WHEN** a recall requests widening and the maintained catalogue is live
- **THEN** out-of-KB hits come from one catalogue query over the index-resolved eligible set
- **AND** the stage reports `index` as its source

#### Scenario: Widening declines when the catalogue is not live

- **WHEN** a recall requests widening while the catalogue generation is stale
- **THEN** the widening stage is reported as declined and the KB results are returned unchanged
