---
type: pattern
status: active
created: 2026-06-10
updated: 2026-06-10
sources: []
pattern_type: architectural
supersedes: "[[Knowledge Base/Notes/Patterns/retry-with-fixed-interval]]"
tags: [retry, backoff, jitter, resilience]
---

# Retry with exponential backoff and full jitter

## Problem

Clients that retry a failed request on a fixed schedule synchronize into retry storms: every caller waits the same interval and hammers the recovering service in lockstep, so it never gets breathing room.

## Solution

Retry with exponential backoff AND full jitter: sleep a random duration in `[0, base * 2**attempt]` before each retry, capped at a ceiling. The randomness spreads retries across the window, so load on the downstream smooths out instead of spiking.

## When to use

Any client calling a remote service that can fail transiently — HTTP calls, queue consumers, database connections.

## When NOT to use

Non-idempotent operations without a dedup key, where a blind retry could double-apply.

## Connections

- [[Knowledge Base/Notes/Patterns/circuit-breaker-for-downstream-failures]]
