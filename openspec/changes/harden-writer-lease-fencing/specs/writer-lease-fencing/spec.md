## ADDED Requirements

### Requirement: Fenced Vault Mutations

A vault mutation issued under a writer lease SHALL complete only while the issuing replica still holds the fencing token it was authorized under. If the lease was lost or superseded (its renewal was rejected, or the coordinator reports a newer holder) before the write commits, the system MUST abort the write with a fenced error and leave the vault unchanged. This applies only when the lease is enabled; lease-disabled operation and read-only commands are unaffected.

#### Scenario: A superseded replica cannot land a stale write

- **WHEN** replica A begins a mutating command under a valid lease, its lease then expires, and replica B acquires the next fencing token before A's write commits
- **THEN** A's write is refused with a `WRITER_FENCED` error
- **AND** no staged bytes are replaced into the vault (the target files are unchanged)

#### Scenario: A rejected renewal fences an in-flight write

- **WHEN** a replica begins a mutating command under a valid fencing token
- **AND** its lease renewal is rejected before the staged write commits
- **THEN** the commit boundary refuses the write with a `WRITER_FENCED` error
- **AND** cleans up the staged temporary files without changing the target files

#### Scenario: The coordinator reports a newer holder at commit

- **WHEN** a replica begins a mutating command under fencing token N
- **AND** the coordinator reports another holder with a fencing token greater than N before commit
- **THEN** the commit boundary refuses the stale write with a `WRITER_FENCED` error
- **AND** no staged bytes are replaced into the vault

#### Scenario: The holder writes normally while its token is current

- **WHEN** a replica holds a current, un-superseded fencing token throughout a mutating command
- **THEN** the write commits normally with no additional refusal

#### Scenario: Lease-disabled mutation is unchanged

- **WHEN** no writer lease is configured and a mutating command writes to the vault
- **THEN** the command commits exactly as before with no fencing check

#### Scenario: Read-only commands bypass fencing

- **WHEN** a command is read-only
- **THEN** it runs exactly as before without acquiring, threading, or validating a fencing token
