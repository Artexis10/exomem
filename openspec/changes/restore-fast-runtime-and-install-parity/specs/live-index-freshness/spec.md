## ADDED Requirements

### Requirement: Warm Semantic Corpus State Is Event-Maintained

The persistent server SHALL maintain a bounded rebuildable semantic corpus context containing current parent generations, stable identities, typed relation facts, resolver entries, and governed eligibility. Canonical writers and watcher events SHALL patch the warm context by exact changed path. Startup SHALL begin warming the context in the background so model loading and normal request handling do not wait for it synchronously.

#### Scenario: Canonical write patches one parent

- **WHEN** a governed write changes one Markdown parent without changing its path or stable identity
- **THEN** the snapshot replaces that parent generation and its affected relations incrementally
- **AND** unchanged parents are neither reopened nor reparsed

#### Scenario: Restart starts a cold warm-up

- **WHEN** a server process starts with no warm semantic context
- **THEN** semantic warm-up begins outside the foreground startup path
- **AND** later foreground requests reuse the warmed context

#### Scenario: Mutation arrives during restart warm-up

- **WHEN** a mutation arrives before the semantic corpus is ready
- **THEN** it returns the retryable `MUTATION_WARMING` outcome with `committed: false`
- **AND** it does not acquire or hold the vault mutation boundary while waiting for the cold build
- **AND** the unchanged mutation may be retried after the supplied `retry_after_ms`

### Requirement: External Changes Patch The Warm Context By Path

The watcher/freshness subsystem SHALL publish exact Markdown create, modify, delete, and move paths to the semantic corpus cache. A matching freshness event token SHALL prove that the cached context includes the observed batch without a full corpus stat walk.

#### Scenario: Synced backdated replacement is applied

- **WHEN** sync replaces one Markdown file and the watcher reports the path
- **THEN** that parent is reopened and reparsed into the current context
- **AND** unchanged parents are not reopened or reparsed

#### Scenario: Event continuity cannot prove currency

- **WHEN** no matching freshness event token can prove the warm context current
- **THEN** the exact existing census detects changes
- **AND** Markdown-only changes are applied as bounded path deltas
- **AND** corpus-wide registry/config changes retain the full-build correctness fallback

#### Scenario: Unsafe delta breaks event continuity

- **WHEN** a watcher delta encounters a symlink, reparse point, nonregular Markdown entry, unreadable page, or invalid semantic state after freshness advances
- **THEN** the warm corpus captions are evicted before the failure is surfaced
- **AND** a later valid event cannot stamp the older context as current
- **AND** the next rebuild applies the full identity and filesystem safety oracle

### Requirement: Incremental State Matches The Full-Rebuild Oracle

For stable-topology edits and exact-path create/edit/delete events, the incrementally maintained context SHALL produce the same page states, stable-identity census, resolved relation facts, inbound/outbound maps, and eligibility sets as a fresh full rebuild over the same Markdown bytes. Resolver-topology or registry changes MAY use the full-build oracle.

#### Scenario: Incremental and rebuilt contexts are equivalent

- **WHEN** a deterministic transition sequence is applied once through incremental updates and once through the full-rebuild oracle
- **THEN** their serialized semantic corpus contexts are identical
