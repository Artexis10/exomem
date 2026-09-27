## ADDED Requirements

### Requirement: A waiting standby re-proves its graph snapshot

A standby whose graph snapshot proof declined SHALL NOT treat that decline as final
while it is still warming. While the `graph_snapshot` cutover component is waiting and
the standby is neither promoted nor discarded, it SHALL re-run the read-only proof when
the durable graph checkpoint generation or the published snapshot's checkpoint has
changed since its previous attempt, at most once per re-proof interval (30 seconds by
default). A re-proof SHALL write nothing the serving worker owns, SHALL reseed only this
process's recall registry before proving, and SHALL NOT overwrite the adoption once
promotion or discard has begun.

#### Scenario: The serving worker republishes after a declined proof

- **WHEN** a standby's first proof declined and the serving worker then publishes a
  snapshot the standby can prove
- **THEN** the standby re-proves within the interval after it observes the change, the
  `graph_snapshot` component becomes ready, and the cutover is not held to its warm
  budget

#### Scenario: Nothing changed since the last attempt

- **WHEN** neither the durable checkpoint generation nor the published snapshot changed
  since the standby's previous proof
- **THEN** the standby does not re-run the proof

### Requirement: Promotion retires whole-vault debt its source proof covers

Promotion SHALL sample the whole-vault full-rebuild marker and its raise count before
the source proof it relies on: the promotion re-proof when one runs, otherwise the
standby's own proof when promotion re-verified that proof's checkpoint as current. When
that proof adopted the snapshot and its residue was durably queued, promotion SHALL
retire the sampled marker by compare-and-swap on its value and raise count, so debt
raised after the sample survives. The handoff record SHALL say whether the marker was
retired or retained. A snapshot that advanced under the standby, an unproven snapshot
and a residue that could not be queued SHALL retain the marker.

#### Scenario: A full marker raised before the proof is retired at promotion

- **WHEN** the serving worker raised a whole-vault marker, the standby's proof adopted
  the snapshot with a bounded residue, and promotion re-verified that checkpoint
- **THEN** promotion queues the residue, retires the marker, and the promoted worker's
  drain converges the residue without a whole-vault rebuild

#### Scenario: Debt raised after the proof survives promotion

- **WHEN** the marker is raised again after the proof sampled it
- **THEN** promotion retains it and the handoff record says so
