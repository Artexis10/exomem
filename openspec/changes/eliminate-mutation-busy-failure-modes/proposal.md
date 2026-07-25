## Why

`MUTATION_BUSY` and "cannot write" (`WRITER_LEASE_REQUIRED` /
`MUTATION_ACKNOWLEDGEMENT_PENDING`) recur without a usable explanation, and
four independently verified root causes explain why:

- **GAP A** — the writer lease is held for the process lifetime.
  `_renew_loop` (`writer_lease.py`) renews forever; release only happens at
  `atexit`; the coordinator never preempts a live holder. A live laptop can
  block a desktop replica indefinitely, with no CLI to release the lease.
- **GAP B** — `pending` idempotency rows never expire (`writer_lease.py`
  prunes only `completed`/`committed_failure`). A crash mid-write poisons that
  payload with `MUTATION_ACKNOWLEDGEMENT_PENDING` forever; only manual SQLite
  surgery clears it.
- **GAP C** — an orphaned `.holder.json` left behind after a failed release
  (`mutation_lock.py`) makes `snapshot()` fabricate an unknown external holder
  at age 0 with `overdue: false`, hiding genuinely stuck state from the
  operator and from `coordination_status`.
- **GAP D** — hosted reads take the mutation boundary
  (`writer_lease.py`) unless individually allow-listed, so a plain read
  returns `MUTATION_BUSY` while an unrelated long write is in progress.

Compounding this, hold time is effectively unbounded — corpus validation runs
12-45s plus a nested 30s `vault_creation_lock` — against a 5s acquire budget,
producing busy storms; the `retry_after_ms` hint is a static 750ms and gets
clobbered even when the caller already computed a better one.

## What Changes

- **R1** — `mutation_lock.py::hold()` emits `acquired`/`released` telemetry
  with `wait_ms`/`hold_ms`, and `retry_after_ms` becomes
  `min(15000, max(750, age_seconds*500))` (≥5000 when overdue) instead of a
  static 750, with the computed hint surviving instead of being clobbered.
- **R2 (GAP C)** — when the orphan-snapshot metadata mutex cannot be taken
  quickly, a lock-free read of the sidecar (published via atomic replace, so
  it is tear-free) reports real age and `overdue`, with `verified: false`;
  only the total absence of a sidecar still fabricates an unknown holder.
- **R3 (GAP D)** — hosted reads stop taking the mutation boundary at all,
  the same as local reads always have; consistency is already provided by
  atomic whole-file staging. This supersedes the archived design decision that
  reserved the bypass to `mode="audit"`/`validate_only` reads
  (`openspec/changes/archive/2026-07-20-make-mcp-acknowledgement-replay-safe/design.md:64`).
- **R4 (GAP B)** — idempotency rows gain an `owner` (`pid:process-nonce`)
  whose liveness is an exclusive OS lock file per process, immune to PID
  reuse and fail-closed on a probe error. A `pending` row whose owner is dead
  becomes `abandoned`; the client gets `MUTATION_OUTCOME_UNKNOWN` (HTTP 409)
  instead of hanging forever, and an identical retry executes fresh after 60s.
- **R5 (GAP A, highest risk)** — the writer lease releases itself after
  `EXOMEM_WRITER_LEASE_IDLE_SECONDS` (default 60) of holding no active
  mutation, so a non-preferred holder (the laptop) hands authority back
  without an operator intervening. A preferred replica is exempt, so it never
  churns itself through an acquire→idle-release→reclaim loop. A single choke
  point (`writer_authority_guard()`) tracks the in-flight mutation count so
  idle release only fires when it is provably safe.
- **R6** — `exomem lease status|release [--yes] [--json]`, an ops-only CLI,
  including cross-device release via a small generalization of the existing
  coordinator release call.

## Capabilities

### Modified Capabilities

- `hosted-mutation-safety`: reads no longer take the mutation boundary,
  orphaned holder state reports real age instead of a fabricated healthy
  snapshot, abandoned idempotency receipts recover instead of wedging
  forever, and retry guidance scales with observed contention.

### New Capabilities

- `lease-operations`: idle-triggered writer-lease release and an operator CLI
  to inspect and release lease state, so a stuck lease has both an automatic
  and a manual way out.

## Impact

- `writer_lease.py`, `mutation_lock.py`, `lease_coordinator.py`,
  `epistemic_graph.py` (retry hint), `vault.py` (fenced-write revalidation
  unaffected, straggler commits still rejected), `__main__.py` (lease CLI).
- No escape-hatch environment variable preserves the read-bypass — that would
  keep the failure mode alive and double the test matrix for no benefit.
- Deferred, explicit follow-up (`shorten-mutation-critical-section`): moving
  the 12-45s corpus validation outside the boundary. This change's telemetry
  (R1) produces the hold/wait distributions needed to size that work; it does
  not attempt it.
- No new runtime dependency. No change to the MCP tool schema. Existing
  writer-lease fencing, reclaim, and idempotency fail-closed tests
  (`test_writer_lease.py`, `test_mutation_lock.py`) must stay green.
