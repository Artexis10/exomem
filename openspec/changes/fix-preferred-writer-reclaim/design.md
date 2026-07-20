# Design — preferred-writer reclaim

## The deadlock, precisely

Three components each behave reasonably alone and deadlock together.

1. `start_server_lifecycle()` (`writer_lease.py`) calls `ensure_writer()` once
   when `preferred_writer` is set, and swallows `OpError`.
2. `_renew_loop()` reads `self._fencing_token`; when it is `None` it `continue`s.
   The loop **renews an existing lease and never acquires one**.
3. The HA edge worker routes mutation-capable requests to the lease holder.

A preferred replica that loses the startup race therefore has no remaining path
to writer authority. There is no retry, and the event that the code expects to
trigger a retry — an incoming mutation — is precisely what the edge withholds
from followers.

## Why a plain retry is safe

The reflex worry is that retrying takeover lets a stale replica seize authority
and write from a vault copy that is behind the current writer's. Under
Syncthing-style replication that would be a real hazard.

It does not apply here, because the coordinator does not preempt:

```python
# lease_coordinator.py
active = holder is not None and old_expiry is not None and old_expiry > now
if active and holder != replica_id:
    # not granted
```

An acquisition is granted only when the lease is **free or expired**. A retrying
preferred replica cannot displace a live holder no matter how often it asks; it
can only claim authority once the other replica has actually stopped renewing
(one TTL, default 12s).

That leaves exactly one residual: when a holder stops and another replica takes
over, the new writer's copy may lag the old writer's final writes. This is
**pre-existing and symmetric** — it is how the laptop takes over from the desktop
today, by the same code path — and the deployment documentation already places
replication convergence with the operator. This change restores the missing
direction of an existing behaviour; it does not introduce a new class of risk.

Consequently no freshness/lag protocol is needed, and none is added. Inventing
one here would be speculative: there is currently no cross-replica currency
marker to compare against, and adding one would mean a coordinator schema change
for a hazard the coordinator already prevents.

## Rejected alternatives

**Retry inside `start_server_lifecycle()` with a bounded loop.** Only moves the
race a few seconds later. If the other replica is up and renewing — the actual
observed situation, sustained for 15 hours — any bounded startup loop still ends
in permanent follower state.

**Have the edge probe followers and redirect.** The edge deliberately pins
mutation-capable traffic to a single origin and never replays it, because the
origin may commit after the edge stops waiting. Routing mutations to a
non-holder to provoke acquisition would attack that guarantee directly.

**Operator-triggered takeover only.** Makes every restart of the preferred
replica a manual operation, and offers no signal that intervention is needed —
the replica reports healthy with `takeover_eligible: true` throughout, which is
what made this cost a full session to find.

## Cadence

Reclaim is attempted on the existing renew cadence, `max(1.0, ttl/3)` — 4s at the
default 12s TTL. This bounds reclaim latency at roughly one TTL after the other
holder stops, matches the responsiveness the renewer already assumes, and adds
one coordinator call per interval only while this replica is a follower.

No additional backoff is introduced. The call is a single indexed SQLite
statement against a coordinator the replica already contacts on this same
interval when it *is* the writer, so the steady-state cost is unchanged rather
than added.
