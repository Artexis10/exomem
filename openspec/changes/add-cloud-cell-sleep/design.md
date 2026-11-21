## Context

**Cells today.**
- A Cloud cell is one tenant's StatefulSet: one serving process with the cell runtime profile, and a volume (a Hetzner CSI volume, or node-local storage).
- cellctl reconciles the cell from the control database.
- `desired_state` is one of `running`, `read_only`, `stopped` and `deleted`. `stopped` already means entitlement or operator intent; Substrate writes it on billing changes.
- cellctl derives replicas only from `desired_state` (`decide.py` `_decide_stopped`, `_routine_converge`). A `NOTIFY` on `exomem_cloud_cells` wakes the loop at once, and the polling floor is 30 s.

**The gateway** (Substrate `src/exomem-gateway/cloud-handler.ts`).
- It resolves a bearer token to a cell and proxies to `cell.exo-cell-<id>.svc:8765/mcp`.
- It gates only on `desired_state`, and answers 503 `CELL_NOT_READY` on any fetch failure.
- It has no timeout and no hold. Its database role reads only `cell_id`, `tenant_id` and `desired_state`, and it has no Kubernetes access.
- It forwards `initialize` and `tools/list` to the cell.
- Cell HTTP is stateless (`stateless_http=True`), so a restart breaks no MCP session.

**Backups and admission.**
- Backups are cellctl Jobs against the volume. Eligibility requires an observed state of `running` or `read_only`.
- Substrate admission counts every non-deleted cell against the `cell_slots` that cellctl publishes. Stopping a pod frees memory but no slot.

**Cold start, measured 2026-10-10.**
- Setup: the 0.114.0 Cloud image, 2 CPUs, 3 GiB, an owner-vault copy with prebuilt state, 5 runs per arm.

| Phase (median s) | Cold page cache | Warm page cache |
|---|---:|---:|
| Process start to `/health/ready` | 15.9 | 12.1 |
| Start to first complete answer | 122.6 | 89.9 |
| `initialize`, `tools/list` | < 0.04 | < 0.04 |

- Where the time goes:
  - the graph handoff, 31 to 44 s;
  - the semantic corpus build, 16 to 20 s on every start;
  - recall calls of 24 to 29 s each on 0.114, which 0.115 and 0.116 address;
  - about 33 s more with a cold page cache.
- `warmup.warm_all` admits retrieval first, then runs the graph handoff, then the semantic corpus, then the lexical caches and the recall encoder preload. The vector lane therefore waits for every write prerequisite before it stops reporting `warming`.
- Anonymous memory after the first answer is 570 to 720 MiB.

**The client.** A connector tool call waits about 60 s (`docs/remote-quickstart.md`).

## Goals / Non-Goals

**Goals:**
- An idle cell holds no node memory. Its volume, entitlement and backups are unchanged.
- The next request wakes it, and the tenant sees a delay at most, never an action to take.
- A connector's first tool call after a wake usually succeeds within its timeout. When it cannot, it gets a typed retryable error, never a generic failure.
- Capacity follows the cells that are awake, so a node hosts tenants up to its storage, not up to every cell's idle memory.

**Non-Goals:**
- Cross-node placement or moving cells between nodes on wake.
- Sharing an embedding process between cells. Each cell keeps its own model; the owner ruled this out on 2026-10-10.
- Changing billing or entitlement. An asleep cell is fully entitled.

## Decisions

### D1. Activity is its own column

`cells` gains three columns:
- `activity`: `awake` or `asleep`;
- `last_request_at`;
- `wake_requested_at`.

cellctl renders replicas = 1 only when `desired_state` serves and `activity` is `awake`. Holds keep their own replica rules.

A `sleeping` value in `desired_state` would make Substrate (billing) and cellctl (idleness) race on one column, and `stopped` already carries billing meaning.

The tenant and operator labels for these states live in the lifecycle status registry, not in code.

### D2. The gateway records requests and asks for wakes; cellctl acts

The gateway gets a column grant: `UPDATE (last_request_at, wake_requested_at)` on `cells`, and nothing else.
- It coalesces `last_request_at` in memory and writes it at most once a minute per cell.
- On a request for an asleep cell, it writes `wake_requested_at`.
- The `NOTIFY` trigger extends to that column, so cellctl reacts within its loop latency.

cellctl stays the only Kubernetes writer and the only writer of `activity`.

A compromised gateway could wake cells but not read or change tenant data or entitlement. That costs memory, not privacy.

### D3. Hold, then retry

For a request to an asleep or waking cell, the gateway writes the wake request and holds the request. It polls the cell Service's readiness until either the cell answers, or the hold budget runs out (configuration; 40 s initially, under the connector's ~60 s).
- If the cell answers in time, the request is forwarded.
- If the budget runs out, the gateway answers with a JSON-RPC error with code `CELL_WAKING`, `retryable: true` and a `Retry-After`.

Held requests count against the gateway's existing concurrency caps of 16 in total and 4 per tenant.

### D4. Discovery answers without a wake

The gateway caches each cell's last `initialize` and `tools/list` responses, keyed by the cell's image digest.
- For an asleep cell, it answers those methods from the cache and starts a wake in the background.
- A client that lists tools at the start of a conversation therefore warms the cell before its first `tools/call`.
- With no cache entry for that digest, the request is held as in D3.

The cache holds tool descriptors only, never tenant content.

### D5. A cell sleeps only when idle and quiescent

cellctl sets `activity = asleep` when two conditions hold:
- `now − last_request_at` is at least the cell's idle period;
- the cell's `/health/ready` reports `quiescent: true`.

Quiescent means none of these is in progress:
- an initial or re-embed build;
- an import or upload session;
- a media job;
- derived, semantic or graph drain debt;
- an unacknowledged write.

The cell computes this; upkeep (the dreamer) does not count.

The idle period is chart configuration with a per-cell override. A cell can be set never to sleep, for example the owner's own cell. Health probes are not requests.

### D6. Holds run on sleeping cells

- **Backups:** eligibility accepts asleep cells. An asleep cell needs no stop for a consistent backup, so it is the cheapest one.
- **Upgrades:** an image change for an asleep cell runs its upgrade hold while the cell sleeps. The hold takes the pre-upgrade backup, starts the new image so cell-init migrates the state, waits for readiness, then returns the cell to asleep. A wake therefore never pays for a migration.
- **A wake during a hold:** it waits for the hold, within the D3 budget.

### D7. Capacity has two bounds

cellctl publishes two capacities:
- **total cells:** bounded by storage, by the volume count for CSI volumes, and by the local pool;
- **awake cells:** bounded by node memory, using the larger of the cell's memory request and its measured warm peak.

Substrate admission counts non-deleted cells against total cells.

When a wake needs room and the awake set is full, cellctl puts the least recently used quiescent awake cell to sleep first. If none is quiescent, the wake waits in a first-in, first-out queue, and the gateway answers `CELL_WAKING` once its hold budget runs out.

A detached CSI volume does not count toward the node's attach limit. Verify that a pod stop detaches the volume before admission relies on it.

### D8. A woken cell answers reads first

`instant-start` already admits retrieval before the graph handoff. The vector lane joins it:
- The recall encoder preloads right after retrieval admission, in parallel with the graph handoff.
- The vector lane stops reporting `warming` once the encoder is resident and the active sidecar is open.
- Writes keep waiting for the graph handoff and the semantic corpus, as `warm_all` documents.

The semantic corpus state is read from disk (the disk-authoritative semantic state work), not rebuilt on every start. The init container reads the state and model files once, so the page cache is warm before the server starts.

The target is a first complete answer within 20 s at p95 after the pod starts on a warm node. Measure it on the owner-vault copy before sleep ships.

### D9. Honest status

- **Tenant home:** "Asleep: wakes on your next request" while the cell is asleep, and "Waking" during a wake.
- **Operators:** they see `activity`, `last_request_at`, wake count and wake latency. Each value is computed when it is shown.

## Risks / Trade-offs

- **A wake longer than the hold budget fails the first tool call.**
  - Mitigations: D4 pre-wake, D8 startup, and the typed retryable error.
  - Residual: a client that never lists tools before calling, on a cold node.
- **A wake storm.**
  - Many cells waking at once can exceed node memory. D7's LRU and queue bound the awake set; a cell that is not quiescent is never evicted.
- **CSI attach time.**
  - A detached Hetzner volume must attach again on wake. That time is unmeasured, and node-local storage avoids it.
- **Upkeep runs less often.**
  - Upkeep pauses while a cell sleeps and resumes on wake. Durable work keeps a cell awake (D5), so nothing owed is lost.

## Migration Plan

1. **Ship D8.** It shortens every restart and roll, with or without sleep.
2. **Add the D1 columns, the D2 grant and the D5 quiescence report,** with every idle period unset, so nothing sleeps.
3. **Ship the gateway's D3 and D4,** and the D7 capacity split.
4. **Canary sleep on the QA cell:**
   - set a short idle period;
   - check that it sleeps, wakes on a `tools/call` within the budget, answers `tools/list` from cache, and takes a backup while asleep;
   - record the timings.
5. **Enable sleep for friend cells.** The owner's cell keeps never-sleep until the owner chooses otherwise.

**Rollback:** unset the idle periods. Every asleep cell wakes on its next request and then stays awake.

## Open Questions

- **The default idle period.** 30 minutes is the starting point. Tune it once wake timings and per-tenant use are measured.
- **The hold budget.** Set it once the D8 measurement shows the real wake time.
