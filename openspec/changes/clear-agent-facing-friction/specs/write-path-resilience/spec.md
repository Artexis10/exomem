## ADDED Requirements

### Requirement: Corpus Context Reuse Is Sync-Safe And Bounded

The governed write path SHALL reuse a previously built corpus context only when an exact census of every filesystem input read by that context still matches. The census MUST be identical before and after a cacheable build, cached contexts MUST be immutable and bounded, and candidate-bearing or disk-divergent registry builds MUST bypass the cache.

#### Scenario: Synced content carries an older timestamp
- **WHEN** Syncthing materializes changed same-size content at an existing path with an older `mtime_ns`
- **THEN** the per-path census differs from the cached census and the context is rebuilt
- **AND** the system does not rely on a vault-wide maximum timestamp

#### Scenario: Corpus changes while context is building
- **WHEN** any censused input changes between the pre-build and post-build census
- **THEN** the raced context is not stored for reuse
- **AND** a later call must establish a stable census before receiving a cached result

### Requirement: Mutation Delivery Has A 60-Second Single-Origin Budget

The reference Cloudflare HA edge SHALL give mutation-capable MCP tool calls a default 60-second origin budget. Timeout, cancellation, or an origin 5xx MUST NOT replay a mutation-capable request to another replica. Raising the budget MUST NOT substitute for the corpus-context performance fix or change the meaning of acknowledgement loss.

#### Scenario: Origin outlives the edge budget
- **WHEN** the selected origin does not acknowledge a mutation within 60 seconds
- **THEN** the edge returns an acknowledgement-delivery failure and sends no copy to the passive origin
- **AND** the origin may still finish and persist the terminal result

#### Scenario: Live deployment pins the old setting
- **WHEN** a deployed worker variable overrides the code default with the previous 15-second value
- **THEN** the code default is not effective and the rollout remains incomplete
- **AND** the operator must update the live value to 60000 milliseconds rather than assuming a code deploy changed it

### Requirement: Implicit Acknowledgement Recovery Retains Results For 600 Seconds

An identical mutation from the same stable authenticated principal, operation, vault identity, and canonical payload SHALL resolve the original per-replica completed receipt for 600 seconds without rerunning the leaf. Explicit idempotency keys retain their separate 24-hour contract. Pending and committed-uncertain receipts remain fail-closed.

#### Scenario: Human retries after investigating an abandoned request
- **WHEN** the same principal repeats a byte-identical mutation within 600 seconds of its completed receipt
- **THEN** the server returns the original terminal result without executing the leaf again
- **AND** the original path and request identity are preserved

#### Scenario: Implicit window or replica boundary is crossed
- **WHEN** the retry occurs after 600 seconds or reaches a replica without the original local receipt
- **THEN** the request is outside the implicit exactly-once guarantee
- **AND** the system does not infer idempotency from a similar title or silently claim cross-replica replay safety

### Requirement: Remember Preview Does Not Take Mutation Authority

`remember(validate_only=true)` SHALL remain a read-only planning operation. It MUST NOT acquire the mutation boundary or writer lease, and the eventual draft commit MUST revalidate under mutation authority.

#### Scenario: Preview overlaps a live writer
- **WHEN** a validate-only remember call overlaps another mutation
- **THEN** the preview runs without returning `MUTATION_BUSY` solely because that writer holds the boundary
- **AND** committing the returned draft still performs the normal under-lock semantic and writer checks
