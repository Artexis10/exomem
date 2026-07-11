---
type: pattern
status: active
created: 2026-05-15
updated: 2026-05-15
sources: []
pattern_type: architectural
tags: [resilience, circuit-breaker, downstream]
---

# Circuit breaker for a failing downstream service

## Problem

When a downstream dependency starts failing or timing out, callers that keep hammering it waste threads on doomed calls and can drag the whole caller down with it.

## Solution

Wrap calls in a circuit breaker. After a threshold of failures the breaker trips OPEN and short-circuits calls immediately (failing fast or serving a fallback) instead of waiting on timeouts. After a cool-down it goes HALF-OPEN and lets a trial request through; success closes it, failure re-opens it.

## When to use

Any synchronous call to a remote dependency that can degrade, especially on a hot path where piled-up timeouts would exhaust a thread pool.

## When NOT to use

Cheap in-process calls, or when every request truly must reach the dependency.

## Relations

- mitigates [[Knowledge Base/Notes/Failures/cache-stampede-on-cold-start]]

## Connections

- [[Knowledge Base/Notes/Patterns/retry-with-full-jitter-backoff]]
