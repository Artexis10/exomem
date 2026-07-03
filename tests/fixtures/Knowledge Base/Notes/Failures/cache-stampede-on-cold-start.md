---
type: failure
status: active
created: 2026-05-30
updated: 2026-05-30
sources: []
severity: serious
tags: [caching, thundering-herd, latency]
---

# Cache stampede when a hot key expires under load

## What happened

A single hot cache key expired during peak traffic. Every concurrent request missed at once and all of them hit the database to recompute the same value — a thundering herd that spiked database load and tail latency until the key was repopulated.

## Mechanism

With no coordination, expiry converts N cache readers into N simultaneous recomputers. The cost is proportional to concurrency at the exact moment of expiry, not to the real rate of change of the data.

## Detection

Database CPU and p99 latency spiked on a sawtooth aligned with the cache TTL boundary.

## Mitigation

Add request coalescing (single-flight) so only one caller recomputes while the rest wait on its result, plus early/probabilistic recomputation before the TTL hard-expires.

## Connections

- [[Knowledge Base/Notes/Patterns/circuit-breaker-for-downstream-failures]]
