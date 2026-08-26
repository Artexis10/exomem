# Proposal: bound-contended-write-index-refresh

## Why

A canonical batch write whose lexical index upsert cannot complete records a
**durable full-index refresh** demand (`vault.py`, WARNING "index upsert
incomplete after batch_atomic_write; durable full-index refresh recorded").
That fail-safe predates the bounded-delta machinery: it answers an O(1) cause —
one contended batch, typically refused by the publication barrier's 50 ms
foreground timeout while a rebuild holds it — with an O(vault) response.

Measured on the personal cell, 2026-08-26, running 0.63.0 under live MCP
traffic (54 client requests): a real 356-file semantic import burst started one
legitimate full build; during that build, exactly **three** contended batch
writes each recorded a full-index refresh demand, and each demand seeded
another whole-vault build with its own retrieval-warming window
(`RETRIEVAL_INDEX_WARMING`; one live client request observed a 25-minute
warming stall). The 0.63.0 machinery made every episode converge — each build
published through the writer burst and readiness returned — so this is
bounded churn with a cause, not the starvation loop. But the amplification
stands: heavy client write activity during any build costs whole-vault rebuild
cycles and demotion windows that a targeted repair would avoid.

This is the remaining instance of the pattern the 2026-08 latency incident
established: unbounded O(vault) work from an O(1) cause.

## What Changes

- An incomplete index upsert after `batch_atomic_write` records a **targeted,
  path-scoped** durable refresh demand (the exact paths of the batch), drained
  through the existing bounded machinery (`retry_deferred_upsert`, the
  single-flight repair owner's targeted mode) instead of a full-index refresh.
- A full-index refresh remains the fail-safe only when the incomplete set
  cannot be named exactly (fail closed, and count it in telemetry).
- Stable, content-free telemetry distinguishes targeted-refresh demands from
  full-refresh escalations so this amplification is observable in the health
  decision trail.

## Capabilities

### Modified Capabilities

- `live-index-freshness` — contended canonical writes SHALL degrade to
  bounded, path-scoped refresh demands; whole-vault work requires a cause that
  names why path-scoped repair is impossible.

## Impact

- `src/exomem/vault.py` — the incomplete-upsert fail-safe.
- `src/exomem/lexstore.py` — draining the path-scoped demand through the
  bounded repair path.
- Regression tests reproducing the contended-batch escalation red-first.
