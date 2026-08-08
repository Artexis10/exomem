## Why

A full epistemic graph rebuild runs inside the vault mutation boundary, so it
blocks every other vault mutation for its entire duration. Measured on a
maintainer workstation against synthetic vaults, on a single `upsert_after_write`
with no usable sidecar:

| vault | first write | steady-state write | ratio |
|---|---|---|---|
| 500 pages | 7,728 ms | 315 ms | 24.6x |
| 2,000 pages | 32,382 ms | 1,186 ms | 27.3x |

CI's write-latency benchmark records the same shape at larger scale:
`hold_ms=39092` over 2,000 pages and `hold_ms=172205` over 8,000.

This is not an edge case. `_open_read_snapshot` treats the sidecar as unusable
whenever `schema_version`, `core_registry_version`, or `extension_registry_hash`
disagrees with the running build, so a full rebuild is triggered by:

- any release that bumps `SCHEMA_VERSION` — at 7 today, bumped seven times;
- any change to the relation registry, including a user adding or editing one
  extension relation;
- a sidecar that is missing, deleted, or corrupt.

The first write after any of those stalls for tens of seconds on a
maintainer-sized vault, and every concurrent writer is blocked behind it. A
client that gives up meanwhile sees a timeout on a write that then lands.

An earlier attempt (#346) removed the escalation so writes deferred to
`reconcile`. That was wrong and CI rejected it:
`test_refresh_missing_sidecar_routes_to_full_rebuild` requires a write against a
missing sidecar to index the whole vault and leave the graph available. The
rebuild has to happen; it must stop holding the boundary while it does.

## What Changes

- Build a full graph rebuild into a temporary database outside the vault
  mutation boundary, then acquire the boundary only to swap it into place.
- Make concurrent rebuild requests single-flight, so N blocked writers do not
  each start their own full-vault rebuild.
- Keep the existing stabilization contract: a rebuild that cannot observe a
  stable vault still fails and marks the graph unavailable.
- Preserve the write-path contract that a write against an unusable sidecar
  leaves the graph built and available.
- Sweep abandoned temporary rebuild databases during reconcile.

## Capabilities

### New Capabilities

- `graph-rebuild-availability`: A full epistemic graph rebuild does not block
  unrelated vault mutations, and never exposes a partially rebuilt graph.

### Modified Capabilities

None.

## Impact

Affected areas are `src/exomem/epistemic_graph.py`, `src/exomem/reconcile.py`
(temp sweep), and the epistemic graph freshness, boundary, and semantic unit
graph tests.

No MCP tool schema, vault format, OAuth, or stdio behavior changes. The sidecar
file format is unchanged; only how and where it is produced changes.
