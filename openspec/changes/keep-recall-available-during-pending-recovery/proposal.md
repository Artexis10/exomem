# Proposal

## Why

One pending write whose bytes cannot be hydrated can disable every ordinary recall request, even when the persistent indexes remain usable.
Pending index work must reduce coverage honestly while the service repairs it, without making unrelated knowledge unavailable.

## What Changes

- Replace whole-request refusal for healthy, incomplete pending work with path-scoped suppression and an explicit coverage warning.
- Keep proven pending pages available and suppress persistent evidence for every pending identity.
- Retain one admitted catalogue during selection so ordinary publication cannot cancel a coherent read.
- Treat the hydration limit as a body limit; use the existing custody rows for a complete path footprint during larger bursts.
- Recover ready and stranded custody through the existing bounded service worker, including paths superseded outside governed writes.
- Preserve current policy, access, tombstone, catalogue identity, and exact publication checks.
- Omit uncertain optional citations and pointer origins while preserving admitted material and mandatory custody checks.
- Keep malformed or unreadable authoritative state distinct from ordinary index lag.
- Reuse the programme's shared live recall canary and release gate. This change adds no scheduler or synthetic production writes.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `recall-read-path`: Keep unaffected recall available during pending hydration failure and recover custody without manual maintenance.

## Impact

The change affects pending snapshots, catalogue selection, recall and reference overlays, derived worker recovery, and their integration checks.
It changes the ordinary recall contract that currently requires a complete pending overlay before any catalogue read.
It adds no model, external service, canonical data rewrite, or runtime flag.
Existing optional heavy capabilities keep their current defaults and failure behavior.
The shared canary and managed release remain programme dependencies; installation must verify real recall under natural concurrent traffic.
