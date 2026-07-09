## ADDED Requirements

### Requirement: Optional Find Timing Diagnostics

The system SHALL expose opt-in timing diagnostics for `find` calls. When requested, the response
SHALL include total elapsed time, cache status, and per-stage timing entries for the retrieval work
that may affect latency, including freshness/cache lookup, keyword, BM25, vector, CLIP, graph,
temporal, fusion, filtering/hit construction, rerank, out-of-KB widening, date filtering, pack
assembly, and serialization. A skipped or unavailable optional lane MUST be represented as skipped
or unavailable rather than causing the call to fail. Timing diagnostics MUST NOT include note bodies,
excerpts, vectors, or other bulk content.

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

### Requirement: Compact and Full Find Result Surfaces

The system SHALL support a `find` result detail mode with `full` and `compact` values. `full` SHALL
be the default and SHALL preserve the current hit dictionary shape. `compact` SHALL return the same
ranked hits in a token-cheap shape that includes routing fields such as path, title, type, scope,
updated date, lifecycle status, media pointers, out-of-KB marker, and clip timestamp when present,
and MUST omit excerpt and detailed ranking signals unless a future explicit option asks for them.

#### Scenario: Compact mode omits token-heavy fields

- **WHEN** `find` is called with compact detail mode
- **THEN** each returned hit includes its path and title
- **AND** each returned hit omits `excerpt`
- **AND** each returned hit omits detailed `signals`

#### Scenario: Full mode remains the default

- **WHEN** `find` is called without a detail mode
- **THEN** each returned hit has the existing full shape, including `excerpt` and any existing
  optional fields that would have been present before this change
- **AND** the ranking and default return type are unchanged

#### Scenario: Compact mode preserves ranking and routing metadata

- **WHEN** the same `find` request is made once with full detail and once with compact detail
- **THEN** the ordered paths are identical
- **AND** compact hits still include the metadata needed to choose a follow-up `get` call

### Requirement: Hot Find Cache With Freshness Invalidation

The system SHALL maintain a small bounded in-process cache for repeated identical `find` requests.
The cache key MUST include every request parameter that can affect ranking or filtering. The cache
MUST be invalidated or bypassed when markdown freshness for the relevant scope changes, when an
embedding or CLIP sidecar that can affect the request changes, or when the active ranking config
identity changes. Cache hits MUST return copies or immutable results so caller mutation cannot alter
future cached responses.

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

### Requirement: Retrieval Architecture Changes Are Deferred Until Measured

The system SHALL NOT add LSH, a new ANN/vector database, or a broader retrieval architecture rewrite
as part of this change. Retrieval architecture changes SHALL be considered only after the new timing
diagnostics identify the lane or stage responsible for unacceptable latency.

#### Scenario: Change adds measurement before retrieval rewrite

- **WHEN** this change is implemented
- **THEN** the existing BM25, vector, keyword, CLIP, graph, temporal, fusion, rerank, and auto-widen
  paths remain the retrieval architecture
- **AND** the new behavior is limited to timing visibility, result serialization, cache reuse, and
  freshness-safe invalidation
