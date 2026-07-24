# disclosure-evidence

## ADDED Requirements

### Requirement: Proportional plaintext-free receipts

Governed egress and policy lifecycle events SHALL be recorded as receipts, and
ordinary ungoverned recall SHALL produce no receipt. A receipt SHALL be emitted
for a governed disclosure decision (any release below full), a policy change, a
grant or revocation, a deletion of governed material, and a budget warning. A
receipt SHALL carry item refs, source and released content hashes, byte sizes,
the representation level, redaction counts, the principal, the audience, the
declared purpose, the policy fingerprint, and the confirmation type — and SHALL
NOT contain released content. Scope identity SHALL be recorded as ids and hashed
labels, never as human label text, unless an explicit per-scope full-text opt-in
is set.

#### Scenario: Ungoverned recall writes nothing

- **WHEN** a query runs on a vault with no governance policy
- **THEN** no receipt is written

#### Scenario: Governed release is receipted without plaintext

- **WHEN** an item is released below full to an audience
- **THEN** a receipt records the refs, hashes, sizes, level, audience, and policy
  fingerprint, and contains neither the released text nor the scope's label text

### Requirement: Per-machine hash-chained log with truncation detection

Receipts SHALL be appended to a per-machine hash-chained log under
`_Governance/events/<instance-id>/`, each record linking to the previous by hash,
so a synced vault never forks a single chain. The chain head and a monotonic
sequence SHALL be anchored in the per-machine sidecar so that truncation or
rollback of the log is detected on load. Month boundaries SHALL link across files.

#### Scenario: Tamper is detected

- **WHEN** a record in the log is edited or the tail is truncated and the chain is
  verified
- **THEN** verification reports the break

#### Scenario: Sync does not fork the chain

- **WHEN** two machines each append receipts to a synced vault
- **THEN** each writes its own per-instance chain and neither corrupts the other's
  sequence

### Requirement: Deletion evidence outlives the source

Deleting governed material SHALL emit a deletion receipt carrying the item's refs
and content hashes so evidence of the deletion survives, without retaining the
deleted plaintext. Recovery from trash SHALL emit an inverse receipt. Deletion
receipts SHALL NOT be removed by content deletion.

#### Scenario: Deletion leaves evidence, not plaintext

- **WHEN** a governed page is deleted
- **THEN** a deletion receipt records its refs and hashes, and no deleted content
  is retained in the log

### Requirement: Chain verification via audit

Chain verification SHALL be exposed as an audit category reachable through
`maintain_memory(mode="audit")`, reporting edited records, truncated tails, and
broken cross-month links, and repairing a lagging head cache forward after a
crash.

#### Scenario: Audit verifies the chain

- **WHEN** the governance-receipts audit category runs on an intact log
- **THEN** it reports the chain valid; on a tampered log it reports the break
