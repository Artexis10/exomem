# Tasks — survive session-store outages

## Session validation cache

- [x] C1. Add the per-replica sqlite validation cache outside the synced vault,
      keyed by `sha256(token)` with encrypted successful claims and
      `validated_at`; include safe upsert, lookup, and deletion operations.
- [x] C2. Wire the cache path and the existing derived local OAuth storage key
      from server authentication setup without changing other auth flows.

## Validation behavior and telemetry

- [x] V1. Update the remote session validation/load path to upsert on success,
      delete on authoritative negative, and serve an eligible cached success
      only for remote timeout, connection, or 5xx unavailability.
- [x] V2. Implement `EXOMEM_SESSION_STALE_GRACE_SECONDS` with default 86400 and
      zero as exact fail-closed parity; keep issuance, exchange, refresh, DCR,
      and revocation live-store-only.
- [x] V3. Emit content-free `event=session_stale_served` logging with a running
      counter and expose additive `session_store` state/count readiness data.

## Tests and verification

- [x] T1. Add focused cache tests proving digest-only keys, encrypted values,
      round-trip/deletion behavior, and raw-token absence.
- [x] T2. Add focused validation tests for remote success, eligible and
      ineligible outage fallback, authoritative revocation, grace expiry, zero
      grace, counters, degraded readiness, and recovery.
- [x] T3. Run the task acceptance pytest, ruff, and OpenSpec validation commands.
