## ADDED Requirements

### Requirement: Lease Gates Only Writing Operations

The writer lease SHALL gate a command invocation only when the specific operation being invoked writes to the vault. A read-only operation of a write-capable tool (for example `connect_memory` in a suggest/graph-context mode, or `adopt_vault` in scan-only mode) MUST NOT require the lease and MUST remain available when the lease coordinator is unreachable. Classification MUST fail safe: an operation whose read/write nature is unknown is treated as a write and requires the lease.

#### Scenario: Every read-only connect operation works during a coordinator outage

- **WHEN** `EXOMEM_WRITER_LEASE_URL` points at an unreachable coordinator
- **AND** `connect_memory` is invoked with omitted/default `suggest-links`, explicit `suggest-links`, `suggest-relations`, `context`, `graph-context`, or `inbound-links`
- **THEN** the call succeeds and performs no vault write
- **AND** it does not raise `WRITER_COORDINATOR_UNAVAILABLE`

#### Scenario: Every read-only adopt operation works during a coordinator outage

- **WHEN** `EXOMEM_WRITER_LEASE_URL` points at an unreachable coordinator
- **AND** `adopt_vault` is invoked with its mode omitted/defaulted or explicitly set to `scan-only`
- **THEN** the call succeeds and performs no vault write
- **AND** it does not raise `WRITER_COORDINATOR_UNAVAILABLE`

#### Scenario: Connect write operations still require the lease

- **WHEN** the coordinator is unreachable and `connect_memory` is invoked with `create-entity` or `accept-relation`
- **THEN** the call is refused with `WRITER_COORDINATOR_UNAVAILABLE` and writes nothing

#### Scenario: Adopt write operations still require the lease

- **WHEN** the coordinator is unreachable and `adopt_vault` is invoked with `save-manifest`, `copy-as-sources`, or `compile-selected`
- **THEN** the call is refused with `WRITER_COORDINATOR_UNAVAILABLE` and writes nothing

#### Scenario: Unknown or malformed operation values fail safe

- **WHEN** a write-capable command's operation or mode is empty, explicitly null, unknown, or introduced by a future version without a read-only classification
- **THEN** it is treated as a write and requires the lease

#### Scenario: Other write-capable commands remain gated

- **WHEN** a write-capable command without a per-operation read-only classification is invoked
- **THEN** it requires the lease regardless of its arguments
