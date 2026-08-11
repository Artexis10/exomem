## ADDED Requirements

### Requirement: Canonical mutations publish a durable graph checkpoint

Every graph-relevant guarded canonical batch SHALL publish the closed version 1 graph-sync checkpoint at `<Knowledge Base>/.graph-sync.json` in the same staged replacement set as its canonical files. The checkpoint SHALL carry a monotonic generation greater than every generation previously issued for the vault, an independent lowercase 24-hex mutation identity, bounded sorted post-write path hashes/deletions or full scope, created paths, and a domain-separated canonical digest. It SHALL be excluded from semantic indexing and its own input set.

The same guarded set SHALL maintain an internal content-free monotonic generation floor at `<Knowledge Base>/.graph-sync-floor.json`. Its closed version 1 object SHALL contain exactly `version`, `generation`, and `floor_sha256`, with a distinct domain-separated canonical digest. New generation selection SHALL be one greater than the maximum valid floor, current checkpoint, and live-graph acknowledgement. The floor SHALL publish before caller canonical replacements and the checkpoint SHALL publish last; caught failure SHALL restore every member of the set. With a valid floor, missing/malformed or contradictory checkpoint state SHALL make the graph unavailable, SHALL force full-scope recovery at a generation greater than that floor, and SHALL NOT permit generation reuse. Without a valid floor, only exact legacy or the closed pre-floor migration states defined below are admissible; every other combination fails closed. Legacy state is exactly the absence of floor, checkpoint, and acknowledgement.

#### Scenario: Canonical batch and checkpoint commit together
- **WHEN** a graph-relevant mutation commits normally
- **THEN** its canonical files and exact next-generation checkpoint are both published before writer authority releases
- **AND** a caught failure leaves neither the canonical change nor a newer checkpoint

#### Scenario: Lost unacknowledged checkpoint cannot reuse its generation
- **WHEN** restart finds a valid generation floor N but the current checkpoint is missing, malformed, or contradictory
- **THEN** the graph remains unavailable and recovery publishes a full-scope checkpoint with generation greater than N
- **AND** no later mutation or recovery reuses generation N

#### Scenario: Graph-irrelevant write does not advance the epoch
- **WHEN** a canonical mutation affects only paths excluded by the binding recall policy
- **THEN** neither the generation floor nor graph checkpoint changes
- **AND** no full graph rebuild is scheduled for that mutation

#### Scenario: Checkpoint digest is deterministic and non-recursive
- **WHEN** the same version, generation, mutation identity, scope, paths, and created paths are canonicalized twice
- **THEN** SHA-256 over `exomem-graph-sync-checkpoint:v1\0` plus those canonical JSON bytes is identical
- **AND** the digest input excludes the digest itself and all graph output bytes

#### Scenario: Shared checkpoint digest vector
- **WHEN** the canonical checkpoint input is `{"created_paths":["Knowledge Base/Notes/example.md"],"generation":1,"mutation_id":"0123456789abcdef01234567","paths":[["Knowledge Base/Notes/example.md","dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"]],"scope":"paths","version":1}`
- **THEN** its domain-separated digest is `941d8a67ae715b6795daade34607f445d4c5b5726b9dc5e4ac095c9946c6d877`

### Requirement: Full graph work and joins occur outside writer authority

When a canonical mutation encounters an unusable graph, dispatch SHALL register exact checkpoint work and return control to the leaf. After canonical commit is observed and while the operation guard still owns mutation authority, the idempotency store SHALL durably transition the result from owned `pending` to nonterminal `graph_pending`, binding the canonical committed terminal and complete validated content-free checkpoint. It SHALL release every leaf/command mutation guard before starting or joining full graph work. A concurrent exact retry SHALL wait rather than receive or replay that internal payload. If the owner dies after `graph_pending`, a new proven owner MAY resume only graph work; it SHALL NOT execute the canonical leaf again.

No storage engine can atomically commit the vault batch and the separate idempotency database. If the process terminates after the canonical commit point but before `graph_pending` persistence, the dead ordinary `pending` row SHALL become or project the existing committed-uncertain outcome and SHALL never expire into canonical re-execution. The durable checkpoint/batch evidence SHALL drive graph recovery independently, while the caller must re-read canonical state rather than blindly retry the mutation. The request MAY wait off-boundary to preserve its terminal contract.

#### Scenario: Second canonical batch enters during a held rebuild
- **WHEN** one request has committed generation N and is waiting on a deliberately held rebuild
- **AND** a second valid mutation begins
- **THEN** the second mutation acquires writer authority and commits generation N+1 without waiting for graph build work
- **AND** both requests may join the same per-vault flight

#### Scenario: Wide command does not hide an outer hold
- **WHEN** a graph-relevant wide command commits and requires a rebuild
- **THEN** its outer operation guard releases before the graph join just as a narrow command's leaf guard does

#### Scenario: Crash during graph wait cannot duplicate the write
- **WHEN** a process dies after persisting `graph_pending` but before graph completion
- **THEN** an exact retry resumes or joins the bound checkpoint work without invoking the canonical mutation again
- **AND** no internal pending payload is returned as a terminal success

#### Scenario: Crash between canonical commit and graph-pending persistence is uncertain
- **WHEN** a process dies after the canonical/checkpoint commit point but before the idempotency row reaches `graph_pending`
- **THEN** exact retry returns committed-uncertain guidance and does not invoke the canonical leaf
- **AND** checkpoint recovery may repair the graph without fabricating a mutation terminal

### Requirement: Concurrent rebuild requests are checkpoint-aware single-flight

The system SHALL run at most one full graph builder per vault across threads and processes. Builder authority SHALL use a persistent OS-locked regular file under the configured shared runtime-state root, keyed by the same canonical vault identity as writer coordination. The lock directory and file SHALL be opened descriptor-first with no-follow/no-symlink validation and restrictive permissions; the file SHALL never live in human/sync-writable vault content and SHALL never be unlinked. Kernel lock ownership, not PID metadata, is liveness authority. A builder SHALL acquire it before creating its unique temporary database, and reconcile SHALL acquire the same lock before sweeping proven abandoned temporary databases. A flight SHALL continue until it publishes a stable graph covering the latest checkpoint observed before publication or reports a bounded failure. Waiters SHALL resolve only when the published generation covers their required checkpoint lineage; a same-generation/different-digest requirement or a stopped uncovered flight SHALL terminate with explicit lineage failure rather than wait forever.

#### Scenario: New checkpoint arrives during a build pass
- **WHEN** the builder started at generation N and another mutation publishes N+1 before swap
- **THEN** the N pass is not published as current
- **AND** the same flight retries and resolves both waiters only after a stable graph consumes N+1 or the attempt budget fails

#### Scenario: Several writers require one rebuild
- **WHEN** several writers commit while the graph is unusable
- **THEN** exactly one builder runs and bounded waiters resolve from its checkpoint-covered outcome

#### Scenario: Another process cannot sweep a live builder
- **WHEN** one process holds the vault-local rebuild lock and owns a temporary sidecar
- **THEN** another writer or reconcile process cannot start a second builder or sweep that temporary path
- **AND** process death releases authority through the kernel lock

#### Scenario: Replacing a vault path cannot replace lock authority
- **WHEN** a user or sync client unlinks or replaces a similarly named path inside the vault
- **THEN** all processes still contend on the descriptor-validated runtime-state lock keyed by canonical vault identity
- **AND** POSIX and Windows multi-process tests prove a second builder cannot enter

### Requirement: A partially rebuilt graph is never observable

The builder SHALL populate a unique reserved temporary database beside the live sidecar. It SHALL close/checkpoint every temporary SQLite handle and validate a self-contained database before publication. Readers SHALL observe the previous usable graph or the fully populated replacement, never the temporary or partially filled database. The final swap SHALL occur under a boundary hold that performs no vault-size-dependent build work. A platform sharing refusal SHALL leave the prior live sidecar intact and the checkpoint unacknowledged, returning explicit committed graph failure rather than copying or deleting the live database.

#### Scenario: Read during rebuild
- **WHEN** a graph read occurs during full rebuild
- **THEN** it sees the prior usable sidecar or graph unavailable
- **AND** it never sees empty or partial rebuilt tables

#### Scenario: Boundary hold is bounded by verification and swap
- **WHEN** a full rebuild completes
- **THEN** writer authority is acquired only to recheck checkpoint/freshness and replace the sidecar
- **AND** hold duration does not scale with vault size

### Requirement: Published graphs consume an exact stable checkpoint

Each full build pass SHALL snapshot full vault freshness and the current coherent generation floor/checkpoint epoch. Under the final boundary hold it SHALL re-read both before acknowledgement, write the exact acknowledgement into the closed temporary database, then re-read both again immediately before replacement. It SHALL publish only if both checks match. The live graph metadata SHALL record the exact consumed generation/digest, and public availability SHALL require parity with the current checkpoint.

When the prior graph is otherwise usable and the exact new checkpoint has bounded path scope, incremental refresh MAY admit the exact predecessor acknowledgement instead of forcing a full rebuild. It SHALL validate that the durable checkpoint still equals the passed checkpoint, its paths/deletions match the committed graph-relevant mutation, the graph acknowledges the immediately preceding generation (or the explicit generation-1 legacy predecessor), and every existing recall/freshness/topology proof succeeds. Row changes, ordinary availability markers, and the new graph checkpoint acknowledgement SHALL commit in one SQLite transaction. Failed proof SHALL roll back and require off-boundary full work; it SHALL NOT rebuild while writer authority is held.

#### Scenario: Vault or checkpoint changes during final pass
- **WHEN** vault freshness or checkpoint identity differs at publication
- **THEN** the pass is discarded/retried and no stale graph is published

#### Scenario: Stable pass publishes acknowledgement
- **WHEN** both inputs remain stable through the final recheck
- **THEN** the replacement graph atomically records the consumed checkpoint generation/digest and becomes available

#### Scenario: Ordinary path-scoped write stays incremental
- **WHEN** a usable graph acknowledges generation N and a graph-relevant write publishes a valid path-scoped checkpoint N+1
- **THEN** incremental refresh atomically updates graph rows and acknowledgement to N+1 without starting a full-vault builder

### Requirement: Crash recovery preserves canonical truth

Startup/readiness and reconcile SHALL compare the generation floor, current checkpoint, and graph acknowledgement. An unacknowledged, missing, malformed, or contradictory checkpoint with a valid floor SHALL make the graph unavailable and require full recovery at a generation above that floor. A missing or malformed floor in any non-legacy or ambiguous state SHALL fail closed and SHALL NOT be automatically repaired. Exact legacy state has no floor, checkpoint, or acknowledgement. The only accepted pre-floor migration states have a valid checkpoint and either no acknowledgement or an acknowledgement exactly matching that checkpoint; under mutation authority they initialize the floor to the checkpoint generation before ordinary availability/recovery logic proceeds. A checkpoint plus conflicting acknowledgement, or acknowledgement without checkpoint/floor, SHALL fail closed. Recovery SHALL report refreshed/current only after the repaired epoch and graph availability are independently re-read and proven. Abandoned reserved temp databases SHALL be swept only while holding the runtime-state rebuild lock.

#### Scenario: Process dies after canonical commit
- **WHEN** the process terminates after checkpoint publication but before graph acknowledgement
- **THEN** restart retains canonical files/checkpoint, refuses to call the graph current, and rebuilds from the full current vault

#### Scenario: Process dies during temporary build
- **WHEN** restart finds a reserved temp database with no live owner
- **THEN** reconcile removes it without changing the live sidecar or checkpoint

#### Scenario: Malformed checkpoint recovery is real
- **WHEN** reconcile finds a valid floor but a malformed or missing current checkpoint
- **THEN** it publishes a higher full-scope checkpoint, builds and acknowledges it, and reports refreshed only after availability proves current
- **AND** user-authored canonical bytes remain unchanged

#### Scenario: Valid pre-floor checkpoint seeds the floor
- **WHEN** upgrade finds a valid checkpoint, no floor, and either no graph acknowledgement or an exact matching acknowledgement
- **THEN** it atomically seeds the floor at that checkpoint generation before proceeding with current-state proof or recovery

#### Scenario: Ambiguous missing floor refuses repair
- **WHEN** the floor is missing or malformed and checkpoint/acknowledgement state is absent, malformed, or contradictory outside exact legacy or accepted pre-floor migration
- **THEN** readiness and reconcile fail closed without inventing a generation or rewriting epoch state

### Requirement: Deletions publish a recoverable graph handoff

A graph-relevant file or recursive-directory deletion SHALL publish its null-path or bounded/full checkpoint through the same guarded trash-move transition, not through best-effort post-delete fanout. The transition SHALL durably establish its lifecycle tombstone and next generation floor before the canonical rename, install the exact checkpoint before the deletion commit point, and pass that checkpoint to derived fanout. A caught checkpoint failure SHALL reverse and fsync the rename and restore the prior epoch. An abrupt interruption after rename SHALL leave enough tombstone/floor evidence for reconcile to force higher full-scope recovery without generation reuse.

#### Scenario: Caught deletion checkpoint failure rolls back
- **WHEN** checkpoint installation fails after a file or directory was moved toward trash but before the deletion commit point
- **THEN** the source placement and prior epoch are restored and the mutation is not reported committed

#### Scenario: Crash after rename remains recoverable
- **WHEN** a process dies after the canonical trash rename but before checkpoint publication completes
- **THEN** restart observes lifecycle/floor evidence, refuses the old graph as current, and recovers at a higher generation without restoring or duplicating the deletion

### Requirement: Write terminals distinguish canonical commit from graph failure

A request whose canonical batch committed SHALL remain canonically committed while its idempotency state stays nonterminal `graph_pending`. No public response or terminal projector SHALL expose internal pending graph state as success. Success SHALL durably transition to `completed` only after proving graph availability for the required checkpoint. Exhausted stabilization, waiter admission failure, lineage failure, or safe platform publication refusal SHALL transition to `completed` with a stable committed graph failure code, required checkpoint digest, and recovery guidance; compact/full public projection SHALL retain those bounded fields, while legacy projection remains unchanged. Exact idempotent retry SHALL replay the same completed outcome without applying the canonical write again.

#### Scenario: Write succeeds with current graph
- **WHEN** the joined flight publishes a graph covering the request checkpoint
- **THEN** the request returns committed with graph sync completed and the graph reports available

#### Scenario: Stabilization exhausts after canonical commit
- **WHEN** the builder cannot publish a stable checkpoint within its bounded attempts
- **THEN** the request returns committed with explicit graph-sync failure and reconcile guidance
- **AND** exact retry does not duplicate the canonical mutation
