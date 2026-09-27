## ADDED Requirements

### Requirement: A waiting standby re-proves its graph snapshot

A standby SHALL NOT treat one graph snapshot proof as final while it waits for
promotion. Until it is promoted or discarded, it SHALL re-run the read-only proof when
the durable graph checkpoint generation or the published snapshot's checkpoint has
changed since its previous attempt, no sooner than the re-proof interval (30 seconds by
default) after that attempt ended, and SHALL look for that change without opening the
sidecar while neither file has moved. A proof SHALL be held only for the snapshot it
read: when the published snapshot changed between the proof's start and its end, the
attempt proves nothing. A newer proof that succeeds SHALL replace the held one; a newer
proof that declines SHALL NOT. A re-proof SHALL write nothing the serving worker owns,
SHALL reseed only this process's recall registry before proving, and SHALL NOT replace
the adoption once promotion or discard has begun; a promotion that raises SHALL leave
the standby proving.

#### Scenario: The serving worker republishes after a declined proof

- **WHEN** a standby's first proof declined and the serving worker then publishes a
  snapshot the standby can prove
- **THEN** the standby re-proves within the interval after it observes the change, the
  `graph_snapshot` component becomes ready, and the cutover is not held to its warm
  budget

#### Scenario: The serving worker publishes during the proof

- **WHEN** the serving worker publishes a new snapshot while the standby's proof runs
- **THEN** the standby does not name the new snapshot as proved, the component stays
  waiting, and the next re-proof proves the new snapshot itself

#### Scenario: Nothing changed since the last attempt

- **WHEN** neither the durable checkpoint generation nor the published snapshot changed
  since the standby's previous proof
- **THEN** the standby does not re-run the proof

### Requirement: Promotion retires whole-vault debt its source proof covers

Promotion SHALL sample the whole-vault full-rebuild marker and its raise count before
the source proof it relies on: the promotion re-proof when one runs, otherwise the
standby's own proof when promotion re-verified that proof's checkpoint as current. When
that proof adopted the snapshot, its residue was durably queued, and the durable graph
checkpoint generation still equals the one sampled before that proof, promotion SHALL
retire the sampled marker by compare-and-swap on its value and raise count, so debt
raised after the sample survives. The generation check exists because a full-scope
batch raises its marker before its bytes land, so a proof that finished in between saw
the marker but not the change it stands for. The handoff record SHALL say whether the marker was
retired or retained. A snapshot that advanced under the standby, an unproven snapshot
and a residue that could not be queued SHALL retain the marker.

#### Scenario: A full marker raised before the proof is retired at promotion

- **WHEN** the serving worker raised a whole-vault marker, the standby's proof adopted
  the snapshot with a bounded residue, and promotion re-verified that checkpoint
- **THEN** promotion queues the residue, retires the marker, and the promoted worker's
  drain converges the residue without a whole-vault rebuild

#### Scenario: A batch that landed after the proof keeps its marker

- **WHEN** a full-scope batch raised the marker, the standby's proof finished before the
  batch's bytes landed, and nothing re-proved before promotion
- **THEN** promotion retains the marker because the durable generation moved

#### Scenario: Debt raised after the proof survives promotion

- **WHEN** the marker is raised again after the proof sampled it
- **THEN** promotion retains it and the handoff record says so
