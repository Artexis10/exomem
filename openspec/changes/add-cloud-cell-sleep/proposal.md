## Why

Every Exomem Cloud cell keeps its pod running all day. An idle cell still holds about 728 MiB of node memory, the measured idle floor of the serving process. Most tenants use their memory for a few minutes at a time, so most of that memory serves nobody. Node memory, not disk, therefore limits how many tenants one node can host.

All of a cell's state is on its volume. A cell can stop its pod while idle and start it again on the next request, without losing anything. The owner approved this as the direction for tenant density on 2026-10-10.

A measurement on 2026-10-10 shows what a wake costs today:
- **Setup:** the 0.114.0 Cloud image, 2 CPUs, 3 GiB, and a copy of the owner's vault.
- **Ready:** a woken cell reports ready after 12 to 16 s.
- **First complete answer:** it arrives only after 90 s with a warm page cache, and after 123 s with a cold one.

A connector waits about 60 s for a tool call. So sleep needs more than a stop and a start. It needs a faster wake, and a gateway that hides the wake from the client.

## What Changes

- **Sleep is its own state.** A cell gains an activity state, `awake` or `asleep`, beside its desired state.
  - The desired state keeps meaning entitlement and operator intent.
  - An asleep cell runs no pod, keeps its volume, and is still entitled.
- **cellctl puts idle cells to sleep.** A cell sleeps when no tenant request has reached it for its idle period and it reports no pending durable work.
  - The idle period is deployment configuration, with a per-cell override, and a cell can be set never to sleep.
- **The gateway records activity and wakes cells.**
  - The gateway records each cell's last request time and asks for a wake when a request arrives for an asleep cell. cellctl stays the only Kubernetes writer.
  - The gateway holds a woken request up to a hold budget, then answers with a typed, retryable `CELL_WAKING` error.
  - It answers `initialize` and `tools/list` for an asleep cell without waking it, from the last answer that cell gave on the same image, and starts a wake in the background.
- **A wake is fast.** A woken cell answers reads from its persisted indexes and finishes its graph handoff and drift checks in the background. The semantic corpus state is read from disk, not rebuilt on every start. The init container prefetches the state and model files.
- **Holds work on sleeping cells.** Backups run against an asleep cell's volume without a stop. Upgrades apply while a cell sleeps, so a wake never pays for a migration.
- **Capacity counts awake cells.**
  - Admission bounds the total number of cells by storage and attach limits.
  - The awake set is bounded by node memory. When the awake set is full, the least recently used quiescent cell sleeps first.
- **Tenants and operators see the state.** The tenant home shows that the memory is asleep and wakes on the next request. Operators see activity, the last request time and wake latency.

## Capabilities

### New Capabilities

- `cloud-cell-sleep`: idle detection, the activity state, wake on request, the gateway hold and retry, discovery without a wake, holds on sleeping cells, the awake set and the fast-wake startup.

### Modified Capabilities

- `instant-start`: a woken Cloud cell answers reads before its graph handoff and drift checks finish, and discloses what is still warming.

## Impact

- **Exomem:**
  - cellctl: the activity column, the idle and awake-set decisions, holds on asleep cells and backup eligibility;
  - the cell runtime: quiescence in `/health/ready`, read admission before the graph handoff, the persisted semantic corpus and init prefetch;
  - the platform chart values, and the control database schema and grants.
- **Substrate:**
  - the gateway: activity records, wake requests, the hold, `CELL_WAKING`, and descriptor-served discovery;
  - admission: total cells against storage, and the awake set against memory;
  - the tenant home status.
- **Order:** sleep ships after the friend launch. The fast-wake startup work helps every restart and roll, so it can ship first.
