## Context

The lease decision is currently binary per command: `writer_lease.invoke` runs a command directly if `command.read_only` is True, else it requires the lease. `command.read_only` is derived from `cli_writes`, which is set True for `connect_memory`/`adopt_vault` because SOME of their operations write. So the read-only operations inherit the write gating and fail closed when the coordinator is down — the opposite of the "reads stay available during a coordinator outage" promise.

## Goals / Non-Goals

- Goal: a read-only operation of a write-capable tool bypasses the lease and works during a coordinator outage.
- Goal: write operations stay lease-gated exactly as today.
- Non-goal: changing which operations are reads vs writes (that mapping already exists in the operation dispatch).
- Non-goal: expanding the public MCP tool surface unless unavoidable; if it changes, the schema-fidelity baseline is regenerated and reviewed deliberately.

## Approach (to be finalized by the implementer)

Two viable shapes:

- **A — per-operation read-only predicate (preferred, no schema change):** give the lease gate visibility into the resolved operation so it can ask "does THIS invocation write?" instead of "is this command write-capable?". The operation-to-read/write mapping already exists in `connect_memory`/`adopt_vault` dispatch (proposal/scan branches are reads; `create-entity`/`accept-relation`/adopt-write are writes). Route that per-operation read-only signal into `writer_lease.invoke`'s bypass check.
- **B — split surfaces:** move the read-only operations onto a read-only command surface. Cleaner conceptually but changes the tool surface and the schema baseline — only if A proves infeasible.

Prefer A. Verify no write operation slips into the read bypass (fail safe: default to lease-required if the operation's read-only-ness is unknown).

## Risks

- Getting the per-operation read/write classification wrong would either block reads (no worse than today) or, dangerously, let a write bypass the lease — so the classification must fail SAFE (unknown → treat as write, require lease).
