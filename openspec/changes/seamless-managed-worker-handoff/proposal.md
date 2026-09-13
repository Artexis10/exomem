## Why

A managed upgrade keeps the public listener and its connections alive, but the replacement worker starts cold after the old worker has already stopped, and it reports ready before it is usable. Measured on the personal service on 2026-09-13 (0.81.0 to 0.82.0, about 2,000 notes): health answered in tens of milliseconds while the new worker spent more than thirty minutes near 145 percent CPU producing one full 50 to 90 MB graph rebuild after every write, writes returned `GRAPH_SYNC_REPAIR_QUEUED`, one write committed within a minute but its response never returned in 314 seconds, recall exceeded the client timeout, thirty vocabulary recovery rows accumulated, and the embedding model loaded inside the first request because preload is off. Every deploy currently costs the operator this window.

## What Changes

- Warm the replacement as a standby before cutover: the candidate worker starts beside the serving worker without state ownership, loads models when preload is allowed, warms the lexical catalog and proves or adopts the current graph snapshot while the old worker keeps serving. Cutover then pauses ingress, drains, stops the old worker, runs offline migration only when the release declares one, promotes the standby and resumes. The unavailable window becomes drain plus promotion instead of a cold start.
- Separate the standby warm budget from the cutover budget, and report standby and cutover readiness through the runtime readiness surface so the supervisor and operators can see what a candidate is waiting on.
- Make an adopted or promoted graph snapshot live for the new process, so committed writes take the incremental graph path instead of scheduling a full rebuild merely because the process is new.
- Coalesce graph rebuild demand: requests that arrive while a rebuild is in flight collapse into at most one follow-up rebuild, never one rebuild per write.
- Keep the request-serving loop responsive during a rebuild by moving the full rebuild off the worker's CPU (a bounded child process at reduced priority that produces the sidecar and hands publication back), with the in-process path retained only where a child cannot be spawned.
- Return a write's acknowledgement once the canonical commit and its receipt are durable, reporting graph work as pending within the request deadline instead of waiting on it.
- Retry the detached stream reattachment within a bounded budget while the standby is being promoted, instead of closing the stream on the first non-success.
- Drain queued vocabulary recovery jobs from the background activation sequence once the graph projection is current, without waiting for an explicit review call.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `managed-service-upgrades`: the replacement warms as a standby before the old worker stops; standby holds no state ownership; cutover and warm budgets are distinct; stream reattachment retries during promotion.
- `instant-start`: readiness distinguishes serving-ready from cutover-ready; graph rebuilds have one owner, coalesce, and run off the request CPU; an adopted snapshot is live; queued vocabulary recovery drains after activation.

## Impact

`service_manager.py` (supervisor upgrade sequence, worker runtime standby and promote), `service_ingress.py` (reattachment retry), `readiness.py` and `runtime_readiness.py` (standby and cutover components), `warmup.py` (standby warm ordering), `epistemic_graph.py` and `graph_sync.py` (snapshot adoption liveness, rebuild coalescing, child-process rebuild), `freshness.py` (instance liveness after adoption), `vocabulary_delivery.py` and `server_runtime.py` (recovery drain in activation), `mutation_terminal.py` or the writer lease where the acknowledgement waits on graph work, the operator release scripts, `docs/`. No change to the transport, the OAuth authority, the public endpoint, write scope or compute mode. The personal service additionally enables model preload in its service environment as an operator step.
