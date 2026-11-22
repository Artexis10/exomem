## MODIFIED Requirements

### Requirement: Per-machine hash-chained log with truncation detection

Receipts SHALL be appended to a per-machine hash-chained log under
`_Governance/events/<instance-id>/`, each record linking to the previous by hash,
so a synced vault never forks a single chain. The sidecar SHALL distinguish the
last fsync'd durable head/sequence from the latest flushed observed
head/sequence. Month boundaries SHALL link across files.
Concurrent appenders sharing one instance id SHALL be serialized by a
process-safe receipt lock. The JSONL chain SHALL remain the evidence authority;
the sidecar SHALL be only an anchor and recovery index.
No append SHALL allocate a sequence until a bounded final-record read confirms
the actual tail matches an adopted observed head. Critical events SHALL fsync
JSONL before advancing the durable head. Reconcile SHALL verify and promote a
file-ahead suffix before later append; it MAY discard a power-loss-missing
observed suffix only when the actual file remains a valid extension of the
durable head. A tail behind or divergent from the durable head SHALL remain
blocked as truncation/tamper.

Within a month, the writer SHALL rotate receipt files before an append would
exceed the active byte limit. Each new part SHALL name its first sequence.
Numbered parts SHALL follow the legacy monthly file in chain order. Rotation
SHALL leave existing receipt bytes and prior locators intact. The same
durability and recovery rules SHALL apply across parts.

Verification MAY reuse evidence for a closed part while its guarded physical
version and incoming chain endpoint still match. It SHALL verify changed
evidence before using it for disclosure. A portable evidence archive SHALL
identify its receipt layout so a reader that does not support it cannot attest
a truncated chain.

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

#### Scenario: Rotation preserves a continuous chain

- **WHEN** an append would exceed the active receipt file limit
- **THEN** the writer continues in a numbered part without rewriting earlier evidence
- **AND** verification and restore prove continuity through the legacy file and every part

#### Scenario: Changed archived evidence blocks a governed disclosure

- **WHEN** a closed receipt part changes after an earlier verification
- **THEN** the service withholds governed report facts until it proves the current evidence
- **AND** a changed older receipt cannot pass by preserving its file length or the current chain tail
