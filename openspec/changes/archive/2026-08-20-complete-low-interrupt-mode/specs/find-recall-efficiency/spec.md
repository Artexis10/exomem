## MODIFIED Requirements

### Requirement: Startup Cache Warm-Up

The system SHALL warm the BM25 index (KB and vault scope), the wikilink resolver,
and the parsed-page cache during server startup when startup warm-up is enabled
and the active resource policy allows CPU cache preloading, so that a subsequent
`find` call does not pay first-call index/resolver/page-parse construction cost.
Warm-up SHALL be skipped when disabled by `EXOMEM_DISABLE_WARMUP` or when the
active resource policy is `quiet`, where low RAM residency is preferred over
first-query latency. Warm-up SHALL soft-fail per stage without preventing server
startup and MUST NOT change `find`'s returned results.

#### Scenario: Warm-up primes caches before the first query

- **WHEN** the server starts with warm-up enabled
- **AND** the active resource policy allows CPU cache preloading
- **THEN** the BM25 index for KB scope, the BM25 index for vault scope, the
  wikilink resolver, and the parsed-page cache are populated before the first
  `find` call is served
- **AND** the first `find` call's results are identical to what it would return
  without warm-up

#### Scenario: Warm-up can be disabled

- **WHEN** the server starts with `EXOMEM_DISABLE_WARMUP` set
- **THEN** no warm-up work is performed at startup
- **AND** `find` still returns correct results, built lazily on first use as it
  does today

#### Scenario: Quiet mode skips CPU cache warm-up

- **WHEN** the server starts in `quiet` mode
- **THEN** startup warm-up does not populate BM25 corpora, the wikilink resolver,
  parsed-page cache entries, embedding matrices, or CLIP matrices solely for
  warm-up
- **AND** `find` still returns correct results by building the required data
  lazily on first use

#### Scenario: A warm-up stage failure does not block startup

- **WHEN** one warm-up stage (for example, building the BM25 vault-scope index)
  fails
- **THEN** the server still starts successfully
- **AND** the failure is logged without raising, and other allowed warm-up stages
  still run

## ADDED Requirements

### Requirement: Quiet Mode Uses Evictable Find Caches

The system SHALL make find's large CPU-side caches evictable in quiet mode. This
includes parsed pages, hot find-result entries, resolver state, BM25 corpora and
token caches, embedding matrices, and CLIP matrices. Eviction MUST NOT delete
sidecar rows, mutate vault files, or disable future `find` calls.

#### Scenario: Entering quiet evicts find caches

- **WHEN** the process has populated find-related RAM caches
- **AND** the effective mode changes to `quiet`
- **THEN** the process evicts the large find-related RAM caches that can be
  rebuilt lazily
- **AND** no vault file or sidecar row is deleted as part of cache eviction

#### Scenario: Idle quiet cache is evicted

- **WHEN** the effective mode is `quiet`
- **AND** a find-related RAM cache has been idle longer than the configured idle
  threshold
- **THEN** the idle resource reaper evicts that cache

#### Scenario: Find after eviction is correct

- **WHEN** a `find` request runs after quiet-mode cache eviction
- **THEN** `find` rebuilds or reloads the required cache data from the vault or
  sidecar
- **AND** the ranked result paths match a warm-cache request over the same vault
  and sidecar state

### Requirement: Resource Status Reports Find Cache Residency

The system SHALL expose best-effort residency diagnostics for find-related caches
without loading those caches. Diagnostics SHALL include whether each large cache
class is loaded and SHOULD include counts or byte estimates when those values are
available from existing in-memory objects.

#### Scenario: Status reports matrix cache residency

- **WHEN** an embedding or CLIP matrix cache is resident
- **THEN** resource status reports that the matrix cache is loaded and includes
  its row count or byte estimate
- **AND** the status call does not read the sidecar to compute that value

#### Scenario: Status reports absent cache without loading it

- **WHEN** a BM25 corpus or parsed-page cache is not resident
- **THEN** resource status reports it as absent or zero-sized
- **AND** the status call does not build the cache to answer the query
