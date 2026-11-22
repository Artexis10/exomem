## MODIFIED Requirements

### Requirement: Per-machine hash-chained log with truncation detection

Receipts SHALL be appended to a per-machine hash-chained log under
`_Governance/events/<instance-id>/`, each record linking to the previous by hash,
with month boundaries linked across files. A process-safe receipt lock SHALL
serialize appenders of one instance. The JSONL chain SHALL be the evidence
authority and the sidecar only an anchor that separates the fsync'd durable head
from the observed head. No append, reconcile or recovery SHALL fork the chain or
move the durable head backward.

#### Scenario: Tamper is detected

- **WHEN** a record in the log is edited or the tail is truncated and the chain is
  verified
- **THEN** verification reports the break

#### Scenario: Sync does not fork the chain

- **WHEN** two machines each append receipts to a synced vault
- **THEN** each writes its own per-instance chain and neither corrupts the other's
  sequence

#### Scenario: Stale anchor cannot fork the chain

- **WHEN** a prior crash left the JSONL tail ahead of the sidecar anchor
- **THEN** the next append refuses pending verification/reconcile rather than
  linking a new record from the stale anchor

#### Scenario: Crash after JSONL fsync but before anchor update

- **WHEN** a critical record is durable in JSONL but the sidecar still names the
  preceding durable head
- **THEN** recovery verifies and promotes that suffix before allocating another
  sequence, without duplicating or forking the record

#### Scenario: Buffered suffix loss cannot roll back durable evidence

- **WHEN** power loss removes some non-fsynced read receipts after the observed
  head advanced
- **THEN** recovery may reset the observed cache only to a valid actual tail at
  or after the durable head, and never rolls the durable head backward

## ADDED Requirements

### Requirement: Receipt evidence rotates into bounded parts

Within a month, the writer SHALL rotate receipt files before an append would
exceed the active byte limit. Each new part SHALL name its first sequence and
follow the legacy monthly file in chain order. Rotation SHALL leave existing
receipt bytes and prior locators intact, and the same durability and recovery
rules SHALL apply across parts. Every reader and validator of receipt file names
SHALL accept both forms.

#### Scenario: Rotation preserves a continuous chain

- **WHEN** an append would exceed the active receipt file limit
- **THEN** the writer continues in a numbered part without rewriting earlier evidence
- **AND** verification and restore prove continuity through the legacy file and every part

#### Scenario: A rollback endpoint may name a numbered part

- **WHEN** a governance rollback marker records a terminal receipt that lives in a
  numbered part
- **THEN** the state migration manifest accepts that endpoint and still refuses a
  malformed part name

### Requirement: Governed disclosure verifies current receipt evidence

Verification MAY reuse its proof for a closed part while that part's guarded
physical version and incoming chain endpoint still match. It SHALL verify the
active part under the append lock with a byte bound, and it SHALL retry a raced
read once under that lock before failing closed. It SHALL verify changed
evidence before using it for disclosure.

#### Scenario: Changed archived evidence blocks a governed disclosure

- **WHEN** a closed receipt part changes after an earlier verification
- **THEN** the service withholds governed report facts until it proves the current evidence
- **AND** a changed older receipt cannot pass by preserving its file length or the current chain tail

#### Scenario: A read that races an append still proves the evidence

- **WHEN** a lifecycle read overlaps an ordinary or critical receipt append
- **THEN** the read returns the verified intents, including any newly appended
  critical intent
- **AND** a same-size edit to the active part still refuses after the retry
