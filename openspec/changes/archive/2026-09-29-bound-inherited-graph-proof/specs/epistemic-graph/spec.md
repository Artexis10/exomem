## ADDED Requirements

### Requirement: An inherited graph sidecar is proved once per identity

A public graph reader that cannot use the exact live checkpoint SHALL NOT repeat
the source-bytes proof while nothing it depends on has changed. The system SHALL
remember the proof's verdict per sidecar, keyed by the sidecar's stored
metadata, its file identity, and the current recall projection identity. A
change to any of them SHALL require a new proof. A proof that raised SHALL NOT
be remembered. A remembered verdict SHALL NOT bypass the checks that precede
the proof: schema, registry, policy, read barrier, graph_sync acknowledgement,
projection identity, and external-pending.

Status and readiness probes SHALL NOT run the proof. They SHALL report the
remembered verdict, or `unproven` when no verdict covers the current identity.
Graph recall SHALL NOT wait on the proof. When the sidecar is unproven, recall
SHALL start at most one background proof per sidecar and proceed without the
graph lane, reporting that lane as degraded. The graph drain SHALL act on a
declined verdict by scheduling one whole-vault rebuild through the existing
marker path, and SHALL NOT re-prove the sidecar on each pass.

#### Scenario: Repeated availability over an inherited sidecar proves once

- **WHEN** a process opens a graph sidecar published by an earlier process lineage and checks availability repeatedly with no change to the sidecar or the corpus
- **THEN** the source-bytes proof runs once and later checks return its verdict

#### Scenario: A changed identity is proved again

- **WHEN** the sidecar or the recall projection identity changes after a verdict was remembered
- **THEN** the next public read does not use the remembered verdict

#### Scenario: The drain schedules one rebuild for a declined proof

- **WHEN** the proof over an inherited sidecar declines and the drain runs repeated passes while the rebuild is held
- **THEN** exactly one whole-vault rebuild is queued and the sidecar is proved at most once

#### Scenario: Readiness never proves

- **WHEN** readiness or coordination status is read for an inherited sidecar no proof has covered
- **THEN** the proof does not run and the graph state is reported as `unproven`

#### Scenario: Graph recall is bounded

- **WHEN** graph recall runs while the inherited sidecar is unproven or its proof is in flight
- **THEN** recall returns without the graph lane, reports `graph` as degraded, and the proof runs off the request thread
