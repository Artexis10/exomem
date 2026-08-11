## MODIFIED Requirements

### Requirement: One Process-Safe Mutation Boundary Per Vault

The system SHALL serialize every operation that can modify a vault's canonical Markdown, media, live governed indexes, logs, or mutation-owned published runtime state through one process-safe boundary keyed by the vault's canonical identity. MCP, REST, CLI, transfer routes, and background writers MUST NOT maintain independent locks for those observable mutations or bypass that boundary.

An epistemic-graph full rebuild MAY construct an unreachable private temporary database and MAY sweep proven abandoned graph temporary databases outside the canonical mutation boundary under the dedicated cross-process rebuild lock defined by `graph-rebuild-availability`. This narrow exception SHALL NOT modify canonical files, floor/checkpoint epoch state, the live graph sidecar, logs, or any path a reader can open. The rebuild lock SHALL be acquired only outside the canonical boundary and before a later bounded canonical-boundary acquisition; code SHALL never wait for the rebuild lock while holding canonical writer authority. Final checkpoint/floor rechecks, live-sidecar acknowledgement and replacement, and all reader-visible publication SHALL remain inside the canonical vault boundary.

#### Scenario: Concurrent commands from different product surfaces

- **WHEN** MCP and REST submit write-capable commands against the same vault at the same time
- **THEN** at most one command executes its observable mutation section at a time
- **AND** both commands reach the same existing command leaves after acquiring the shared boundary

#### Scenario: Separate processes target the same vault

- **WHEN** two Exomem processes resolve different path spellings to the same canonical vault and attempt observable mutations concurrently
- **THEN** they contend on the same process-safe vault boundary
- **AND** they cannot both enter their observable mutation sections

#### Scenario: Private graph construction does not hold canonical authority

- **WHEN** a full graph rebuild populates a unique temporary database that no reader can address
- **THEN** the builder holds only the cross-process rebuild lock for vault-size-dependent construction
- **AND** unrelated canonical writers may use the canonical mutation boundary concurrently

#### Scenario: Live graph publication retains canonical serialization

- **WHEN** a private graph build is ready to acknowledge and replace the live sidecar
- **THEN** it acquires the canonical mutation boundary while already holding the rebuild lock, performs only bounded rechecks and publication, and releases the canonical boundary before releasing the rebuild lock
- **AND** no code acquires or waits for the rebuild lock while holding the canonical boundary

### Requirement: Mutation Boundary Composes With Transactional Writes

The shared boundary SHALL enclose every complete reader-visible mutation, including canonical file changes, floor/checkpoint epoch publication, live index/log updates, and mutation-owned published notifications. Existing transactional write and rollback semantics MUST remain in force inside the boundary, and nested write helpers invoked by one command MUST NOT deadlock by attempting to become a competing mutation. Unreachable private graph construction and proof-scoped abandoned-temp cleanup are governed by the explicit rebuild-lock exception above and are not reader-visible mutation sections.

#### Scenario: Multi-file mutation succeeds

- **WHEN** a governed write updates a note, graph epoch, indexes, and the activity log
- **THEN** the command retains the vault boundary until the complete transactional canonical batch and its mutation-owned publication dispatch finish
- **AND** any full graph build/join begins only after that boundary releases

#### Scenario: Transactional batch rolls back

- **WHEN** a caught failure causes an existing transactional write to restore its pre-write state
- **THEN** the rollback completes while the same mutation boundary is still held
- **AND** the next mutation cannot observe the partially committed state

#### Scenario: Private build publishes through the boundary

- **WHEN** a graph builder has completed an unreachable temporary sidecar
- **THEN** it cannot acknowledge or replace the live sidecar until the canonical boundary admits the bounded publication phase
- **AND** a caught publication failure leaves the prior live graph and canonical epoch observable
