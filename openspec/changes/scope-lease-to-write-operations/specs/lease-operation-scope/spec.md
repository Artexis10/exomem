## ADDED Requirements

### Requirement: Lease Gates Only Writing Operations

The writer lease SHALL gate a command invocation only when the specific operation being invoked writes to the vault. A read-only operation of a write-capable tool (for example `connect_memory` in a suggest/graph-context mode, or `adopt_vault` in scan-only mode) MUST NOT require the lease and MUST remain available when the lease coordinator is unreachable. Classification MUST fail safe: an operation whose read/write nature is unknown is treated as a write and requires the lease.

#### Scenario: A read-only operation works during a coordinator outage

- **WHEN** `EXOMEM_WRITER_LEASE_URL` points at an unreachable coordinator and a `connect_memory` suggest-links (or `adopt_vault` scan-only) call is made
- **THEN** the call succeeds and performs no vault write
- **AND** it does not raise `WRITER_COORDINATOR_UNAVAILABLE`

#### Scenario: A write operation still requires the lease

- **WHEN** the coordinator is unreachable and a `connect_memory` `create-entity`/`accept-relation` (or an `adopt_vault` write mode) call is made
- **THEN** the call is refused with `WRITER_COORDINATOR_UNAVAILABLE` and writes nothing

#### Scenario: Unknown operations fail safe

- **WHEN** an operation's read/write classification cannot be determined
- **THEN** it is treated as a write and requires the lease
