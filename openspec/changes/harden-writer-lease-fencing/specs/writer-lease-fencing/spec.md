## ADDED Requirements

### Requirement: Fenced Vault Mutations

A vault mutation issued under a writer lease SHALL complete only while the issuing replica still holds the fencing token it was authorized under. If the lease was lost or superseded (its renewal was rejected, or the coordinator reports a newer holder) before the write commits, the system MUST abort the write with a fenced error and leave the vault unchanged. This applies only when the lease is enabled; lease-disabled operation and read-only commands are unaffected.

#### Scenario: A superseded replica cannot land a stale write

- **WHEN** replica A begins a mutating command under a valid lease, its lease then expires, and replica B acquires the next fencing token before A's write commits
- **THEN** A's write is refused with a `WRITER_FENCED` error
- **AND** no staged bytes are replaced into the vault (the target files are unchanged)

#### Scenario: The holder writes normally while its token is current

- **WHEN** a replica holds a current, un-superseded fencing token throughout a mutating command
- **THEN** the write commits normally with no additional refusal

#### Scenario: Lease disabled and read-only commands are unaffected

- **WHEN** no writer lease is configured, or the command is read-only
- **THEN** the command runs exactly as before with no fencing check
