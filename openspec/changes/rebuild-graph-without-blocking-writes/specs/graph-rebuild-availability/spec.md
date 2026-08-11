## ADDED Requirements

### Requirement: Canonical mutations publish a durable graph checkpoint

Every graph-relevant guarded canonical batch SHALL publish the closed version 1 graph-sync checkpoint at `<Knowledge Base>/.graph-sync.json` in the same staged replacement set as its canonical files. The checkpoint SHALL carry a monotonic generation greater than both any valid current checkpoint and live-graph acknowledgement, independent mutation identity, bounded sorted post-write path hashes/deletions or full scope, created paths, and a domain-separated canonical digest. It SHALL be excluded from semantic indexing and its own input set. A malformed/missing checkpoint after acknowledgement SHALL make the graph unavailable and SHALL NOT permit generation reuse.

#### Scenario: Canonical batch and checkpoint commit together
- **WHEN** a graph-relevant mutation commits normally
- **THEN** its canonical files and exact next-generation checkpoint are both published before writer authority releases
- **AND** a caught failure leaves neither the canonical change nor a newer checkpoint

#### Scenario: Checkpoint digest is deterministic and non-recursive
- **WHEN** the same version, generation, mutation identity, scope, paths, and created paths are canonicalized twice
- **THEN** SHA-256 over `exomem-graph-sync-checkpoint:v1\0` plus those canonical JSON bytes is identical
- **AND** the digest input excludes the digest itself and all graph output bytes

#### Scenario: Shared checkpoint digest vector
- **WHEN** the canonical checkpoint input is `{"created_paths":["Knowledge Base/Notes/example.md"],"generation":1,"mutation_id":"0123456789abcdef01234567","paths":[["Knowledge Base/Notes/example.md","dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"]],"scope":"paths","version":1}`
- **THEN** its domain-separated digest is `941d8a67ae715b6795daade34607f445d4c5b5726b9dc5e4ac095c9946c6d877`

### Requirement: Full graph work and joins occur outside writer authority

When a canonical mutation encounters an unusable graph, dispatch SHALL start or join one per-vault full rebuild under a content-free join token, return control to the leaf, and release every leaf/command mutation guard before waiting. The request MAY wait off-boundary to preserve its terminal contract.

#### Scenario: Second canonical batch enters during a held rebuild
- **WHEN** one request has committed generation N and is waiting on a deliberately held rebuild
- **AND** a second valid mutation begins
- **THEN** the second mutation acquires writer authority and commits generation N+1 without waiting for graph build work
- **AND** both requests may join the same per-vault flight

#### Scenario: Wide command does not hide an outer hold
- **WHEN** a graph-relevant wide command commits and requires a rebuild
- **THEN** its outer operation guard releases before the graph join just as a narrow command's leaf guard does

### Requirement: Concurrent rebuild requests are checkpoint-aware single-flight

The system SHALL run at most one full graph builder per vault. A flight SHALL continue until it publishes a stable graph covering the latest checkpoint observed before publication or reports a bounded failure. Waiters SHALL resolve only when the published generation covers their required checkpoint lineage.

#### Scenario: New checkpoint arrives during a build pass
- **WHEN** the builder started at generation N and another mutation publishes N+1 before swap
- **THEN** the N pass is not published as current
- **AND** the same flight retries and resolves both waiters only after a stable graph consumes N+1 or the attempt budget fails

#### Scenario: Several writers require one rebuild
- **WHEN** several writers commit while the graph is unusable
- **THEN** exactly one builder runs and bounded waiters resolve from its checkpoint-covered outcome

### Requirement: A partially rebuilt graph is never observable

The builder SHALL populate a reserved temporary database beside the live sidecar. Readers SHALL observe the previous usable graph or the fully populated replacement, never the temporary or partially filled database. The final swap SHALL occur under a boundary hold that performs no vault-size-dependent build work.

#### Scenario: Read during rebuild
- **WHEN** a graph read occurs during full rebuild
- **THEN** it sees the prior usable sidecar or graph unavailable
- **AND** it never sees empty or partial rebuilt tables

#### Scenario: Boundary hold is bounded by verification and swap
- **WHEN** a full rebuild completes
- **THEN** writer authority is acquired only to recheck checkpoint/freshness and replace the sidecar
- **AND** hold duration does not scale with vault size

### Requirement: Published graphs consume an exact stable checkpoint

Each build pass SHALL snapshot full vault freshness and the current valid graph checkpoint. Under the final boundary hold it SHALL re-read both and publish only if unchanged. The live graph metadata SHALL record the exact consumed generation/digest, and availability SHALL require parity with the current checkpoint.

#### Scenario: Vault or checkpoint changes during final pass
- **WHEN** vault freshness or checkpoint identity differs at publication
- **THEN** the pass is discarded/retried and no stale graph is published

#### Scenario: Stable pass publishes acknowledgement
- **WHEN** both inputs remain stable through the final recheck
- **THEN** the replacement graph atomically records the consumed checkpoint generation/digest and becomes available

### Requirement: Crash recovery preserves canonical truth

Startup/readiness and reconcile SHALL compare the current checkpoint with graph acknowledgement. An unacknowledged or malformed current checkpoint SHALL make the graph unavailable and require a full recovery. Abandoned reserved temp databases SHALL be swept only after proving no matching live flight owns them.

#### Scenario: Process dies after canonical commit
- **WHEN** the process terminates after checkpoint publication but before graph acknowledgement
- **THEN** restart retains canonical files/checkpoint, refuses to call the graph current, and rebuilds from the full current vault

#### Scenario: Process dies during temporary build
- **WHEN** restart finds a reserved temp database with no live owner
- **THEN** reconcile removes it without changing the live sidecar or checkpoint

### Requirement: Write terminals distinguish canonical commit from graph failure

A request whose canonical batch committed SHALL remain committed while it waits off-boundary. Success SHALL prove graph availability for the required checkpoint. Exhausted stabilization SHALL return a committed terminal with a stable graph failure code, required checkpoint digest, and recovery guidance; it SHALL NOT report the canonical mutation rejected. Exact idempotent retry SHALL replay the same committed outcome without applying the canonical write again.

#### Scenario: Write succeeds with current graph
- **WHEN** the joined flight publishes a graph covering the request checkpoint
- **THEN** the request returns committed with graph sync completed and the graph reports available

#### Scenario: Stabilization exhausts after canonical commit
- **WHEN** the builder cannot publish a stable checkpoint within its bounded attempts
- **THEN** the request returns committed with explicit graph-sync failure and reconcile guidance
- **AND** exact retry does not duplicate the canonical mutation
