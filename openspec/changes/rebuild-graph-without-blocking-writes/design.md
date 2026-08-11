## Context

`EpistemicGraphIndex.refresh_paths` currently acquires the vault mutation boundary and escalates to `_rebuild_all_locked()` whenever the graph sidecar is unusable. The rebuild pass deletes and repopulates the live SQLite tables, so the wide boundary is also what prevents readers seeing an empty or partial graph. That produces the incident: a slow reconcile or first write can hold the shared boundary long after the client deadline.

The current terminal contract remains correct: a write that encounters an unusable graph returns only after a whole-vault rebuild either publishes an available graph or reports an explicit derived failure. What is wrong is waiting while canonical writer authority remains held.

## Goals / Non-Goals

**Goals:**

- Publish canonical files and durable graph handoff state together.
- Release canonical writer authority before any full graph build or join.
- Let a second writer publish while earlier callers wait on one per-vault rebuild.
- Never expose a partial graph or claim a checkpoint was consumed when it was not.
- Recover after process death without force-unlocking or losing a committed canonical write.
- Preserve an idempotent terminal: retry never duplicates canonical state.

**Non-Goals:**

- Change the user-authored vault schema or graph query API.
- Make incremental available-graph refresh asynchronous.
- Return success while the graph is silently unavailable.
- Force-unlock a live mutation owner.
- Reduce how often schema/registry changes invalidate the sidecar.

## Decisions

### Publish a durable checkpoint in the canonical batch

Every graph-relevant guarded batch includes a replacement for the vault-local hidden graph-sync checkpoint sidecar at `<Knowledge Base>/.graph-sync.json` (resolved through the configured Knowledge Base directory name). Version 1 is a closed content-free object:

`{version, generation, mutation_id, scope, paths, created_paths, checkpoint_sha256}`.

`generation` is one greater than the highest valid generation in the current checkpoint or live graph acknowledgement, defaulting to 1 only when neither exists. A malformed/missing checkpoint after acknowledgement makes the graph unavailable but does not permit generation reuse. `mutation_id` is the existing invocation/transition correlation when available, otherwise an independently generated 24-hex identifier. `scope` is `paths` while the sorted unique safe relative path/hash entries fit the fixed bound and `full` otherwise. Each path entry is `[relative_path, post_write_sha256_or_null]`; null represents deletion. `created_paths` is the sorted subset created by the batch. The digest is SHA-256 over the ASCII domain prefix `exomem-graph-sync-checkpoint:v1\0` followed by UTF-8 canonical JSON of every field except `checkpoint_sha256` (sorted object keys, no whitespace, `ensure_ascii=false`).

The checkpoint replacement is staged and guarded with the canonical files. Caught publication failure rolls both back; an abrupt partial batch remains detectable through the existing guarded-batch and vault-freshness contracts. The checkpoint is excluded from semantic candidates and from its own path list. A direct human edit has no checkpoint, so full-vault freshness remains an independent backstop.

### Schedule under the boundary, join only after release

Post-commit fanout may synchronously refresh an already-available graph. When the sidecar is unusable or a full rebuild is already in flight, graph dispatch records a per-invocation join token for the required checkpoint and starts or joins one per-vault rebuild without waiting. The leaf then exits its mutation guard. Writer coordination joins the token only after the operation guard has released and before finalizing the request/idempotency terminal.

This ordering applies to narrow and wide commands: no outer command guard may enclose the graph join. The join token is content-free and process-local; the durable checkpoint, not the token, is crash authority. A later writer can enter, commit generation N+1, and join the same flight while the builder that began at N restarts its stabilization pass to consume N+1.

### Build a temporary sidecar and publish one stable checkpoint

One single-flight owner builds into a reserved temporary database beside the live sidecar. Each pass snapshots the valid durable checkpoint and full vault freshness, fills the temp database, and then acquires the vault mutation boundary only to re-read both. If either changed, it releases the boundary and retries. If stable, it writes the consumed checkpoint generation/digest into temp metadata and atomically replaces the live sidecar under the same short hold.

Readers continue using the previous usable sidecar during the build. If no usable sidecar exists, reads report graph unavailable until publish. They never open the temporary database. Replacement behavior is explicitly tested on the Windows service path as well as POSIX.

### Resolve waiters from checkpoint coverage

A successful flight resolves every waiter whose required generation is less than or equal to the published generation and whose checkpoint lineage was observed during stabilization. If a newer checkpoint appeared, the flight continues before resolving it. Exactly one builder runs per vault, but any number of bounded waiters may join.

The triggering command preserves the existing semantic contract: it returns with graph availability proven for its checkpoint. If stabilization attempts are exhausted, canonical state remains committed and the terminal reports `state: committed`, `graph_sync: failed`, a stable failure code, the required checkpoint digest, and reconcile guidance. It never reports the mutation rejected or invites a blind write retry. Exact request retry replays that committed terminal; a later explicit recovery may satisfy the durable checkpoint.

### Recover from restart and sweep abandoned work

Startup/readiness and reconcile compare the current valid checkpoint with graph metadata. Missing/malformed checkpoint state, a graph acknowledgement behind the current generation/digest, or an unusable graph makes graph availability false and schedules/requires full recovery. The checkpoint is not cleared on acknowledgement; its current generation remains the crash-safe comparison point.

Temporary databases use a reserved per-vault prefix and contain their target checkpoint digest. A live single-flight owns only its exact temp path. Reconcile removes abandoned temps after proving there is no matching live owner; it never removes the live sidecar or another vault's work.

## Risks / Trade-offs

- [Checkpoint update adds one small write per graph-relevant batch] → Keep it content-free, bounded, excluded from recall, and whole-file atomic.
- [Sustained writers prevent a stable pass] → Retain bounded stabilization; fail explicitly as committed-with-derived-failure rather than holding writers or publishing stale graph state.
- [Many callers wait on one rebuild] → Bound waiter registration and expose current flight/checkpoint status; callers do not hold mutation authority.
- [Crash after canonical replacement but before all batch artifacts] → Existing abrupt-batch inspection plus full-vault freshness detects the partial state; restart never trusts checkpoint metadata alone.
- [Windows cannot replace an open SQLite file like POSIX] → Close publication handles, test the Windows path, and refuse publication without disturbing the previous sidecar when replacement is unsafe.
- [A post-commit graph failure is mistaken for failed mutation] → Terminal state remains committed and names derived recovery separately; idempotent replay returns the same canonical receipt.

## Migration Plan

No user-data migration. On first graph-relevant mutation after upgrade, generation 1 is written and the graph records its acknowledgement. Startup accepts a legacy graph with no checkpoint only until the first checkpoint exists; once present, acknowledgement parity is required for availability.

Rollback before any checkpoint exists may use the prior release. After checkpoint publication, supported rollback uses a compatibility build that retains checkpoint parsing/recovery while restoring prior higher-level behavior. The hidden checkpoint remains safe to preserve.

## Open Questions

None. Waiting callers join off-boundary, checkpoint coverage defines completion, and stabilization exhaustion returns a committed canonical terminal with explicit derived failure.
