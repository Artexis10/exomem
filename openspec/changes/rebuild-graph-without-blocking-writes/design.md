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

The public v1 checkpoint shape and digest stay unchanged. An internal content-free `<Knowledge Base>/.graph-sync-floor.json` stores the highest generation ever issued as the closed object `{version, generation, floor_sha256}` under the distinct `exomem-graph-sync-generation-floor:v1\0` digest domain. `generation` is one greater than the maximum valid floor, current checkpoint, and live graph acknowledgement, defaulting to 1 only for exact legacy state where all three are absent. This auxiliary floor is necessary because a deleted or corrupted unacknowledged checkpoint otherwise erases the only evidence of its issued generation. `mutation_id` is a normalized existing correlation when it yields exactly 24 lowercase hex characters, otherwise an independently generated 24-hex identifier. `scope` is `paths` while the sorted unique safe relative path/hash entries fit the fixed bound and `full` otherwise. Each path entry is `[relative_path, post_write_sha256_or_null]`; null represents deletion. `created_paths` is the sorted subset created by the batch. The checkpoint digest remains SHA-256 over `exomem-graph-sync-checkpoint:v1\0` plus its existing canonical JSON.

For graph-relevant ordinary writes, the staged guarded order is floor, caller canonical replacements, then checkpoint. Caught publication failure rolls the whole set back. The floor and checkpoint are excluded from semantic candidates, returned canonical paths, watcher fanout, and their own input. Graph-irrelevant writes do not advance either artifact. A malformed/missing checkpoint with a valid floor recovers by publishing a higher full-scope checkpoint; a missing/malformed floor after non-legacy state fails closed rather than inventing history. A direct human edit has no checkpoint, so full-vault freshness remains an independent backstop.

Graph-relevant file and recursive-directory deletion use the same epoch through the existing lifecycle/trash transition: durable tombstone, floor publication, canonical rename, checkpoint publication, then the existing deletion commit point. Caught checkpoint failure reverses and fsyncs the rename and restores the prior floor. Abrupt failure leaves the tombstone and/or ahead floor as proof that reconcile must issue a higher full-scope recovery checkpoint.

### Schedule under the boundary, join only after release

Post-commit fanout may synchronously refresh a usable graph when the exact new path-scoped checkpoint proves the live graph acknowledges its immediate predecessor. The incremental SQLite transaction publishes row changes, recall availability markers, and the new checkpoint acknowledgement together. If that narrow proof fails, dispatch only records the exact full-work requirement while under writer authority; it does not build or wait.

After canonical commit is observed and while the operation guard remains held, writer coordination durably changes the idempotency row from owned `pending` to nonterminal `graph_pending`, storing the canonical committed terminal plus complete validated content-free checkpoint. The guard then releases before any full build/join. Concurrent exact retries wait; after proven owner death, a new owner may CAS-claim `graph_pending` and resume only graph work. It never runs the canonical leaf. Completion or explicit committed graph failure transitions the row to `completed`; no internal pending payload is publicly replayable.

The vault batch and idempotency SQLite store cannot share one atomic transaction. Process death after canonical commit but before `graph_pending` persistence therefore retains the existing committed-uncertain boundary: a dead ordinary `pending` row for that cut never expires into leaf re-execution, exact retry returns outcome-unknown/readback guidance, and the durable checkpoint still drives graph recovery. This is less convenient than graph-only resume but preserves the non-duplication invariant without fabricating a terminal.

This ordering applies to narrow and wide commands: no outer command guard may enclose the graph join. The join token is content-free and process-local; the durable checkpoint, not the token, is crash authority. A later writer can enter, commit generation N+1, and join the same flight while the builder that began at N restarts its stabilization pass to consume N+1.

### Build a temporary sidecar and publish one stable checkpoint

One single-flight owner across threads and processes holds a persistent OS-locked regular file under the configured shared runtime-state root, keyed by the same canonical vault identity as writer coordination, before creating a unique reserved temporary database beside the live sidecar. The lock path is descriptor-bound, no-follow/no-symlink validated, permission-restricted, never placed in human/sync-writable vault content, and never unlinked. Kernel lock lifetime is the ownership proof; PID metadata is not authority. Reconcile acquires the same lock before sweeping well-formed abandoned temps.

Each pass snapshots the coherent floor/checkpoint epoch and full vault freshness, fills and closes/checkpoints a self-contained temp database, and validates it. Under the mutation boundary it re-reads epoch and freshness, writes the exact acknowledgement to temp and closes it, invokes a stable publication test seam on the original index, then re-reads epoch and freshness immediately before replacement. If either check changes it retries. A platform sharing refusal leaves the previous live sidecar intact and returns explicit committed graph failure.

Readers continue using the previous usable sidecar during the build. If no usable sidecar exists, reads report graph unavailable until publish. They never open the temporary database. Replacement behavior is explicitly tested on the Windows service path as well as POSIX.

### Resolve waiters from checkpoint coverage

A successful flight resolves every waiter whose required checkpoint identity is covered by observed monotonic lineage. If a newer checkpoint appears, the flight continues before resolving it. Same-generation/different-digest requirements fail explicitly; a stopped uncovered flight wakes all waiters with lineage failure. Exactly one builder runs per vault, and waiter registration is bounded.

The triggering command preserves the existing semantic contract: it returns with graph availability proven for its checkpoint. If stabilization attempts are exhausted, canonical state remains committed and the terminal reports `state: committed`, `graph_sync: failed`, a stable failure code, the required checkpoint digest, and reconcile guidance. It never reports the mutation rejected or invites a blind write retry. Exact request retry replays that committed terminal; a later explicit recovery may satisfy the durable checkpoint.

### Recover from restart and sweep abandoned work

Startup/readiness and reconcile compare the generation floor, checkpoint, and graph acknowledgement. A valid floor plus missing/malformed/contradictory checkpoint permits recovery only at a higher generation. Missing/corrupt floor state fails closed except for two exact cases: all three artifacts absent is legacy, while a valid checkpoint plus absent floor plus absent or exactly matching acknowledgement is the pre-floor migration state and seeds the floor at that checkpoint generation under mutation authority. Conflicting acknowledgement or acknowledgement without checkpoint/floor is ambiguous and refuses repair. Reconcile reports refreshed/current only after it re-reads both epoch coherence and graph availability.

Temporary databases use a reserved per-vault prefix and contain their target checkpoint digest plus a unique nonce. Reconcile removes them only while holding the same cross-process rebuild lock; it never removes the lock file, live sidecar, or a live builder's work.

## Risks / Trade-offs

- [Checkpoint update adds one small write per graph-relevant batch] → Keep it content-free, bounded, excluded from recall, and whole-file atomic.
- [The monotonic floor is another operational artifact] → Keep it closed, content-free, domain-separated, batch-guarded, and excluded from all semantic/index output; it exists solely to prevent generation reuse after lost unacknowledged state.
- [Sustained writers prevent a stable pass] → Retain bounded stabilization; fail explicitly as committed-with-derived-failure rather than holding writers or publishing stale graph state.
- [Many callers wait on one rebuild] → Bound waiter registration and expose current flight/checkpoint status; callers do not hold mutation authority.
- [Crash after canonical replacement but before all batch artifacts] → Existing abrupt-batch inspection plus full-vault freshness detects the partial state; restart never trusts checkpoint metadata alone.
- [Windows cannot replace an open SQLite file like POSIX] → Close publication handles, test the Windows path, and refuse publication without disturbing the previous sidecar when replacement is unsafe.
- [A post-commit graph failure is mistaken for failed mutation] → Terminal state remains committed and names derived recovery separately; idempotent replay returns the same canonical receipt.

## Migration Plan

No user-data migration. On first graph-relevant mutation after upgrade, floor/checkpoint generation 1 is written and the graph records its acknowledgement. A valid pre-floor checkpoint initializes the floor at its generation. Exact legacy state—no floor, checkpoint, or acknowledgement—remains accepted until first publication. Any other missing/corrupt floor state fails closed.

Rollback before any epoch exists may use the prior release. After publication, supported rollback uses a compatibility build that retains floor/checkpoint parsing, graph-pending idempotency semantics, and recovery while disabling newer scheduling if needed. Both hidden artifacts remain safe to preserve.

## Open Questions

None. The auxiliary generation floor resolves non-reuse after lost unacknowledged checkpoint state; the accepted pre-floor migration states are closed; the unavoidable cross-store crash cut returns committed-uncertain without re-execution; and private graph construction is explicitly reconciled with hosted mutation safety through an unreachable-work exception and fixed lock order.
