# Proposal

## Why

The public MCP observer currently measures three read tools against one programme bar. Collection reads and canonical recall names need the same instrument.

The durable decision is to extend that observer with selectable configured bars and a terminal boundary that excludes observer assertions. This prevents separate runners from measuring different work.

## What Changes

- Admit canonical recall tools and collection `inspect` or `query` cases through the existing read-only observer.
- Validate collection operation shapes and every selected bar before connecting.
- Report each case's bar, measured terminal count, and descriptive p50 and p95, including failed attempts.
- Stop caller timing when FastMCP returns its parsed terminal, before checking source truth or schemas.
- Keep the wider sandbox programme open: frozen source truth, nine operation families, matched pairs, layer profiles, and a configurable CI gate.

The initial p95 target of 150 ms is provisional. Matched evidence must establish an applicable budget before any acceptance or improvement claim.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `request-latency-contract`: Extend the shared MCP observer's read-only protocol, configured bar selection, terminal boundary, and descriptive evidence.

This capability currently lives in the active `bound-request-latency-across-surfaces` change. Its requirement must enter the canonical specs before this change archives.

## Impact

The change extends `recall_canary` and its existing FastMCP tests. It reuses generated operation schemas, the latency contract, and the existing percentile implementation.

No dependency, server model, scheduler, governance rule, production write, or automatic repair is added. Sandbox measurement remains an explicit operation. Missing evidence remains incomplete.
