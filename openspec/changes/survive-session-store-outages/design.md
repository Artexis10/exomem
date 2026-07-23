# Design — survive session-store outages

## Context

Hosted connector access tokens are resolved through `SessionAuthority.remote`
against the worker auth-state store. The existing 300-second in-memory cache
cannot bridge a material store outage, and because both replicas share that
remote dependency a single Cloudflare failure can disable every otherwise
healthy connector path.

This change applies only to validation/loading of an already-issued session.
All token issuance, exchange, refresh, dynamic client registration, and
revocation paths continue to require the live store.

## Decision 1 — Durable local validation cache

Each replica has a sqlite cache outside the synced vault, in the same local
state-directory family and with the same default (`~/.cache/exomem`) used by the
idempotency store. The cache key is `sha256(token)`; raw bearer tokens never
touch disk.

Each value contains the validated principal/claims payload and `validated_at`.
The value is encrypted at rest with the derived storage-encryption key already
used for the local OAuth `FileTreeStore`. Server wiring derives that key once
from the signing root and passes it to the cache. The cache module owns sqlite
schema initialization, encrypted serialization, upsert, lookup, and deletion.

Successful remote validation upserts the entry and otherwise behaves exactly as
before. An authoritative negative response — invalid, revoked, or expired —
deletes the entry and is refused. Negative results are never cached.

## Decision 2 — Serve stale only on unavailability

The session load path distinguishes remote unavailability (timeout, connection
error, or HTTP 5xx) from an authoritative negative response. On unavailability,
it loads the digest-keyed local entry and serves its claims only when:

```
now - validated_at <= EXOMEM_SESSION_STALE_GRACE_SECONDS
```

The grace defaults to 86400 seconds. A missing, unreadable, or expired cache
entry fails closed with `temporarily_unavailable`, matching the existing outage
behavior. Setting `EXOMEM_SESSION_STALE_GRACE_SECONDS=0` bypasses stale lookup
and restores exact pre-change fail-closed behavior.

Every stale serve emits a content-free `event=session_stale_served` log with a
monotonic process-local counter. Claims, principals, token digests, and tokens
are not logged.

## Decision 3 — Readiness state

The process tracks whether the last remote session-store contact failed while
stale serving is enabled, plus the running stale-served count. `/health/ready`
adds:

```json
{
  "session_store": {
    "state": "ok",
    "stale_served_count": 0
  }
}
```

`state` is `degraded` while the last remote contact failed and stale serving is
active; a later successful or authoritatively negative remote contact restores
`ok`. With the grace set to zero, stale serving is inactive and readiness
retains `ok` while validation continues to fail closed as before.

## Accepted tradeoff

A revocation performed while the store is down propagates to an affected
replica only when the store recovers, bounded by the configured grace window.
For a single-operator personal deployment this is strictly better than a total
connector outage. An authoritative revocation observed from the store deletes
the cached entry immediately, so it can never be served stale afterward.

## Fail-safe mechanisms around cache invalidation

Two deliberate safety devices back the revocation-always-wins invariant:

- **In-memory block list.** `delete()` records the token digest in an
  in-memory set before touching sqlite, so a revocation survives even when the
  row DELETE fails. When the DELETE succeeds and removed no row, the digest is
  discarded again — every failed validation (including forged bearers from
  unauthenticated callers) passes through `delete()`, and only digests that
  actually guarded a cached row may stay resident. The set is also cleared on
  generation rotation and process restart.
- **Stale-serving disable latch.** If a bulk invalidation (`delete_session`,
  `delete_family`, `clear`) hits a sqlite error mid-operation, stale serving is
  latched off for the remainder of the process lifetime: the cache can no
  longer prove a revoked entry was removed, so availability yields to
  correctness until a restart re-establishes a clean baseline. The latch is
  logged content-free when it engages.

## Rollback

Set `EXOMEM_SESSION_STALE_GRACE_SECONDS=0` to restore fail-closed validation
without redeploying. The additive readiness field and unused local sqlite file
are inert under the kill switch.

## Test strategy

- Cache tests cover digest-only keys, encrypted values, round-trip claims,
  deletion, expiry inputs, and corrupt/unreadable entries failing closed.
- Session-authority tests cover remote success/upsert, outage with an eligible
  entry, outage without an entry, authoritative revocation clearing the entry,
  expiry, the zero-grace kill switch, logging/counters, and readiness state
  recovery.
- Existing server transport tests prove unchanged hosted transport behavior and
  the additive readiness payload.
