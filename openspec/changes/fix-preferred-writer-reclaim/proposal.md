# Fix preferred-writer reclaim

## Why

A replica configured with `EXOMEM_WRITER_LEASE_PREFERRED=1` attempts to become
writer exactly once, at server startup. If that attempt fails because another
replica holds a live lease, the `OpError` is swallowed and the replica stays a
follower **permanently** — for the entire lifetime of the process.

`start_server_lifecycle()` justifies swallowing the failure with "Mutations will
retry authoritatively." That assumption is false under the HA edge. The edge
worker routes mutation-capable requests to the **current lease holder**
(`deploy/cloudflare-ha/src/worker.js`), so a follower never receives a mutation.
The result is a circular deadlock:

- the preferred replica reclaims the lease only when a mutation arrives;
- a mutation arrives only once it holds the lease.

Observed on 2026-07-20. The desktop replica restarted at 21:01 the previous
evening, lost a startup race to the laptop, and remained a follower for over 15
hours while reporting `role: "follower"`, `coordinator_healthy: true`,
`takeover_eligible: true`, `reasons: []`. Nothing was unhealthy; it had simply
stopped trying. Its own access log recorded 118 external requests before the
restart and zero after.

The practical damage is that the preferred replica is also the machine the
operator deploys to. A release installed and restarted there changes nothing
observable, because the edge is still routing every request to the other
replica. A 0.25.4 deploy on 2026-07-20 was diagnosed as ineffective for exactly
this reason.

## What Changes

- The lease renewer periodically retries acquisition when this replica is
  configured as the preferred writer and currently holds no fencing token,
  instead of relying on a single startup attempt.
- Retry failures stay non-fatal and unlogged-at-error, matching the existing
  "startup remains readable" posture. A follower that cannot acquire is a normal
  steady state, not a fault.
- No change to preemption semantics. See design.md: the coordinator already
  refuses to displace a live holder, so this cannot steal a lease from a running
  replica — it only claims one that has genuinely expired.

## Capabilities

### Modified Capabilities

- `hosted-mutation-safety`: a preferred replica must keep attempting to reclaim
  writer authority for as long as it is a follower, so that edge routing can
  return to it without operator intervention.

## Impact

- `writer_lease.py` renew loop only. No coordinator protocol change, no schema
  change, no edge worker change.
- Deterministic tests for reclaim-after-expiry, no-preemption-of-a-live-holder,
  and non-preferred replicas never self-promoting.
