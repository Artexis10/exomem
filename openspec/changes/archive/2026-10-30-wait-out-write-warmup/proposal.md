## Why

A governed write that arrives while the graph handoff or the semantic corpus is still warming gets `MUTATION_WARMING` at once. The client can only retry the same write. Warm-up ends by itself within seconds: on one personal vault, five restarts in late October finished both components in 5 to 20 seconds. The mutation request budget is 60 seconds. The refusal therefore hands the client a wait that the server can do itself.

## What Changes

- A keyed governed write that meets a warming write-gate component waits for it inside its request. The wait uses the bound and the parked-worker cap that a busy boundary already uses. When the component becomes ready, the write runs once.
- The write gets the retryable `MUTATION_WARMING` only when warm-up outlasts that bound, or when no worker slot is free to wait in. The refusal reports `waited_ms`.
- An unkeyed write keeps the immediate refusal. If its client gave up during a wait and resent it, the write would run twice.
- Each wait is logged as the mutation event `warmup_waited`, with `waited_ms` and the outcome.

## Impact

- Code: `src/exomem/writer_lease.py` (the warm-up gate in `LeaseManager.invoke`).
- Spec: `hosted-mutation-safety` gains one requirement.
- No change to reads, to the readiness payload or to the gate's components.
