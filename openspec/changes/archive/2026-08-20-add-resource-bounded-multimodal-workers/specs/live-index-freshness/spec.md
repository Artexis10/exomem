## ADDED Requirements

### Requirement: Bounded live semantic encoding
Live semantic indexing SHALL split work by a configurable maximum chunk count. A single encode
call MUST NOT receive more than that bound, including when one document alone exceeds it, and
all chunks SHALL be committed under the existing file identity contract.

#### Scenario: Large import batch
- **WHEN** changed files produce more chunks than the live encode bound
- **THEN** embedding runs as multiple bounded encode calls
- **AND** every eligible file is indexed without one unbounded flattened allocation

#### Scenario: One oversized document
- **WHEN** one document produces more chunks than the live encode bound
- **THEN** its chunks are encoded in bounded slices
- **AND** the final file rows contain all slices in original order

### Requirement: Deferred semantic work survives restart
Deferred semantic paths SHALL be stored in a rebuildable per-vault SQLite sidecar, deduplicated,
visible through resource status, and removed only after successful dispatch or explicit healing.

#### Scenario: Restart with deferred paths
- **WHEN** the server restarts after an import was deferred
- **THEN** resource status still reports the deferred paths
- **AND** an explicit index/reconcile can clear them after processing
