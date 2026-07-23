# Survive session-store outages

## Why

On 2026-07-21 the worker auth-state Durable Object (`state:personal-main`) hung
for roughly two hours during a Cloudflare partial outage. Origin session
validation hard-depends on that remote store and has only a 300-second
in-memory cache, so every claude.ai and ChatGPT connector session failed on
both replicas while both origins were otherwise healthy.

One remote KV instance must not be able to take down the entire connector
surface. A bounded, encrypted local validation record lets already-proven
sessions survive a store outage without permitting stale issuance or caching
negative validation results.

## What Changes

- Add a per-replica sqlite cache of successful session validations outside the
  synced vault, keyed by `sha256(token)` with encrypted claims and validation
  timestamps.
- On remote session-store unavailability, serve a previously validated session
  within `EXOMEM_SESSION_STALE_GRACE_SECONDS` (default 86400); continue to fail
  closed when no eligible entry exists.
- Clear the local entry on an authoritative invalid, revoked, or expired result,
  and never cache or serve negative validation results.
- Keep token issuance, exchange, refresh, dynamic client registration, and
  revocation dependent on the live store.
- Add a kill switch: `EXOMEM_SESSION_STALE_GRACE_SECONDS=0` restores the prior
  fail-closed behavior.
- Add content-free stale-serving logs and an additive `session_store` object to
  `/health/ready` with store state and a running stale-served count.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `hosted-gateway-contract`: Adds bounded outage tolerance for validation of
  sessions that were previously validated successfully on the same replica.

## Impact

The session validation/load path gains an encrypted local sqlite cache and
store-health telemetry. Server authentication wiring supplies the existing
derived storage-encryption key and a cache path in the local state-directory
family. The readiness response gains one additive object. Issuance, exchange,
refresh, DCR, and revocation behavior do not change.
