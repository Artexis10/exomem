## Context

See proposal.md for the production measurement. A read-only code map and an evidence-first reproduction (2026-09-13, disposable 446-file vault, real `FileWatcher`, scripts retained under the session scratch root as `lane-debug/scripts/{harness,exp4,exp5,exp6}.py`) established the mechanism:

- The public listener and accepted connections live in the supervisor; only the worker child is replaced, and the MCP transport is stateless, so there is no session to lose. The upgrade sequence is pause ingress, drain (30 s), stop the old worker, run the offline state migrator, spawn the new worker, poll `/health` and `/health/ready`, resume. The whole transition budget is 40 s and nobody serves between stop and ready.
- `/health/ready` reports `ready` from the tool surface, lease state and lexical admission only. Model preload and graph state never gate it, and the heavy background threads (graph drain, media, retrieval compute) start in-process at about the same moment traffic resumes.
- `freshness._external_pending` and `file_watcher._SELF_UPSERTS` are process-local. The new worker starts with an empty self-upsert table, so every inotify event it cannot attribute to itself (the old worker's last writes, the migrator, ordinary vault traffic) re-arms `external_pending`. `EpistemicGraphIndex._open_read_snapshot` consults that flag first and declines the predecessor probe, `_graph_sync_predecessor_state` reports `graph_sync_predecessor_unreadable`, and `upsert_after_write` collapses that into the same branch as a real lineage gap: a whole-vault rebuild. On a live vault the flag is continuously re-armed, so every write rebuilds. Measured: clean writes 0.17 s incremental with zero rebuilds; with the flag armed 3.5 to 4.0 s per write, every one a rebuild.
- `_join_registered_standalone` waits on `graph_sync.wait_for_registered` with no budget, so a caller that cannot carry a pending outcome blocks for the whole rebuild. MCP callers can carry pending and return fast, which matches the production acknowledgements; the one 314 s non-response was not reproduced and is not explained by mutation-boundary contention (a governed write during a rebuild completed in 0.32 s).
- Keyword recall during one rebuild degrades by about a third (p50 178 to 234 ms); what makes it permanent is back-to-back rebuilds, not one.
- The cold snapshot proof costs 0.66 s for 446 files, about 3 s at 2,000 notes, and adopts the previous snapshot; it never ran on the write path because the external-pending fence fails earlier.

## Goals / Non-Goals

Goals: a managed upgrade leaves the service usable within seconds; a write after a worker replacement takes the incremental graph path; no write ever waits unbounded on graph work; rebuild demand coalesces; the cold start of a replacement is paid while the old worker still serves.

Non-goals: session persistence (none is needed), changing the MCP transport, the OAuth authority, the public endpoint, write scope or compute mode, or the per-request recall stage budgets owned by `bound-connector-request-latency`.

## Decisions

### Phase 1: the rebuild loop is a dispatch defect and is fixed as one

**D1 External-pending fences reads, never governed writes.** `external_pending` exists so a read that requires a current projection can refuse rather than serve stale relations. A governed write does not require a current projection to compute its own predecessor: `_open_read_snapshot` applies the fence only to `require_current_projection=True` callers. For writes the predecessor probe always runs.

**D2 An unreadable predecessor is queued repair, not a lineage gap.** `upsert_after_write` routes `graph_sync_predecessor_unreadable` to the incremental path with the affected paths recorded as graph repair demand, returning `pending`. Only a proven lineage mismatch or an unusable snapshot (schema, registry, corrupt checkpoint) schedules a whole-vault rebuild. The two states stay distinct in the returned outcome so the alarm and the doctor can tell them apart.

**D3 External marks are path-scoped.** `_record_external_locked` records the affected paths; `external_pending` becomes "there are unrepaired external paths" and those paths drain through the existing bounded incremental repair. A write on one path is never fenced by an unrelated external edit. With path-scoped marks the process-local self-upsert table no longer needs seeding across a handoff: the worst case after replacement is a bounded list of paths to repair incrementally, and that is measured in the regression test rather than assumed.

**D4 Every join on graph work carries a budget.** `_join_registered_standalone` derives its wait from the request deadline when one exists and from a bounded default otherwise; past the budget the caller receives `pending` with the checkpoint it can poll, exactly as MCP callers do today. No caller can block for the duration of a rebuild.

**D5 Rebuild demand coalesces.** Demand arriving while a rebuild is in flight sets one follow-up mark; at most one further whole-vault pass runs after the current one, never one per write. The single-flight owner stays as it is.

**D6 Regression evidence is the reproduction.** Measured residue after D1 to D5: a fresh interpreter cannot derive a delta from a checkpoint another instance published, so its first write still pays one whole-vault pass (`recall_delta_incomplete`); every later write is incremental. D7 removes that pass by proving the snapshot in the standby before promotion. The `exp5.py external` shape becomes a test: a live watcher, one unattributed edit before each of ten writes, and the assertions are zero whole-vault rebuilds, incremental acknowledgement latency within a stated bound, and the repair queue draining to zero. A second test replays a real worker handoff through the supervisor harness and makes the same assertions for the first ten writes after promotion.

### Phase 2: the replacement warms as a standby, then is promoted

**D7 Standby before cutover.** The supervisor spawns the candidate beside the serving worker in standby: it binds its private socket, warms the lexical catalog, loads models when preload is allowed, opens the graph snapshot read-only and runs the cold proof, and reports a distinct `cutover` readiness. It takes no writer lease, publishes nothing, schedules no drain and owns no state. The old worker serves throughout. The standby warm budget is separate from the 40 s cutover budget and defaults to minutes; a standby that does not reach cutover-ready inside it is discarded with a recorded reason and the old worker keeps serving.

**D8 Promotion is short and ordered.** Cutover pauses ingress, drains, stops the old worker and proves its descendants exited, runs the offline migrator only when the target release declares a state migration (otherwise the step is recorded as skipped), promotes the standby (it acquires ownership, re-validates the snapshot it proved, applies the repair that adoption owes, and starts its schedulers), and resumes.

Re-validation is proportional to what could have changed. The migrator is the only writer between the two workers, and whether it runs is declared by the staged target, so: when it ran, the standby re-runs the whole source proof before promotion; when it did not, promotion re-compares the recall and graph-sync checkpoint pair, which is what a write by anyone else would move. A full source proof in the second case would add seconds to the one window this change exists to shorten, for a writer the sequence has already excluded. If the proof or the comparison fails, the standby rebuilds after promotion using the phase-1 coalesced path, and the handoff record says so.

Adoption's residue is applied at promotion, not during the warm. Proving a snapshot is a read, but enqueueing the repair it owes is scheduling a drain and withdrawing the availability marker is publishing graph state -- both belong to whoever owns the vault, and during the warm that is still the old worker. The standby therefore adopts with the residue returned rather than applied (`apply_residue=False`), carries it, and applies it once promotion is accepted. Making the checkpoint the delta origin is process-local, so deferring the two writes costs the adoption nothing.

**D9 Readiness names what it means.** `/health/ready` keeps `ready` for serving and adds a `cutover` component set: `lexical`, `embeddings` (when preload is allowed), `graph_snapshot`. The supervisor polls `cutover` for a standby and `ready` for a promoted worker. Operators see which component a candidate is waiting on.

**D10 Rebuild leaves the request CPU.** A whole-vault rebuild runs in a bounded child process at reduced priority that writes the sidecar and returns; the worker publishes it under the existing single-flight owner. Where a child cannot be spawned the in-process path remains, and the readiness surface reports which one ran. Measured need: one rebuild costs recall about a third; phase 1 removes the back-to-back case, so this decision is kept but scheduled last and re-measured after phase 1 ships.

**D11 Streams retry through promotion.** The ingress reattachment retries the upstream `GET` with backoff inside the cutover budget instead of closing the stream on the first non-success, so a long-lived client's stream survives a promotion it never noticed.

**D12 Queued vocabulary recovery drains after activation.** Once the graph projection is current, the activation sequence drains the bounded recovery queue in the background rather than waiting for an explicit review call.

**D13 Preload is an operator setting for the personal service.** `EXOMEM_PRELOAD_MODELS=1` goes into the personal service environment; it is a deployment step, not a code default, because the shared machine configuration is also read by the second instance.

## Risks / Trade-offs

- Narrowing the external-pending fence could let a write compute a predecessor against a stale projection → the predecessor is derived from the checkpoint lineage, not from the projection, and the regression test asserts drift stays zero across external edits.
- Path-scoped marks change what "current" means for reads → reads still refuse while any external path is unrepaired; only the write path changes.
- A standby doubles memory briefly (two workers, two model copies) → bounded by the warm budget; the standby is discarded, not the serving worker, when the host cannot afford it.
- Skipping the migrator is a correctness risk → it is skipped only when the target release declares no migration; the declaration is part of the staged target and is verified by the existing target check.
- **Adoption is what a replacement worker's first write depends on, and a
  mid-traffic handoff does not guarantee it.** A write that defers withdraws the
  availability marker, and the background repair may not have republished it
  before the old worker stops, so the promoted process inherits a fenced sidecar
  and one whose recorded bytes are behind the vault for the deferred paths.
  While adoption ran the public-reader proof it refused exactly those vaults --
  in 23 ms -- and the first write then paid a whole-vault pass which, under
  continuous writes, cannot stabilize (Class C, three attempts, 10-12 s) and
  which a standalone caller has to join. Measured over four runs: adoption
  succeeded once and gave six writes at 0.50-0.82 s with zero joins; the run
  whose predecessor left the sidecar fenced failed adoption and paid 7.2 s on
  its first write and 11.0 s later. The discriminator is adoption, not lineage
  age. Adoption therefore proves through a maintenance read and tolerates a
  bounded residue (one drain pass, `graph_drain.DRAIN_LIMIT`): it adopts the
  checkpoint, queues exactly the unrepaired paths, and leaves the marker
  withdrawn so reads still refuse until that repair lands. An unbounded residue
  or a structurally unusable sidecar still pays the whole-vault pass.
- **What that does not close.** With a live watcher repairing unattributed edits
  while governed writes continue, the two writers advance the graph lineage
  independently, and a governed write whose predecessor has genuinely moved
  registers a whole-vault rebuild through the proven gate D2 keeps
  (`reason=graph_sync_predecessor_mismatch`, or `..._present_at_genesis` on a
  freshly built vault). Under that same write traffic the rebuild cannot
  stabilize -- `attempts=3 elapsed_ms=11934.1 class=C cause=the recall
  projection identity moved across the pass` -- and a standalone library caller,
  which has no envelope to carry `pending`, joins it (bounded at 15 s), reports
  the re-raised `GraphProjectionMoved` as `failed`, and then joins the successor
  that publishes. The write itself always succeeds: its canonical bytes are
  durable before any of this. Observed in one write of six across runs, moving
  between writes as the lineage race does, and independent of whether adoption
  succeeded (seen with residue 0 and with residue 3). Every mechanism in that
  chain predates this change and two are deliberate (the proven gate, the
  stabilization contract); closing it means either letting a standalone caller
  carry `pending` -- which ten governance and deletion-lineage tests currently
  forbid -- or making a rebuild that lost a race to concurrent writes report
  something other than `failed`. Both are their own decisions.
- The 314 s non-response remains unexplained → D4 removes the only unbounded join found; the acknowledgement path is instrumented (ledger spans already exist for recall) so a recurrence names its stage.

## Migration Plan

Phase 1 is additive on the write path and deploys through the ordinary managed upgrade; the very upgrade that deploys it still pays today's cost once. Phase 2 changes the supervisor sequence; the first upgrade under it runs the old sequence (the running supervisor is the old code) and later ones run the new one. Rollback of either phase is a normal downgrade; no state format changes.

## Open questions

- Whether the 30 s stop-to-ready gap can also shrink by keeping the old worker serving reads during promotion. Not in this change; it needs the writer-lease design.
