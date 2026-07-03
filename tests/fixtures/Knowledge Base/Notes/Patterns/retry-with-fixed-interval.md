---
type: pattern
status: superseded
created: 2026-01-12
updated: 2026-06-10
sources: []
pattern_type: architectural
superseded_by: "[[Knowledge Base/Notes/Patterns/retry-with-full-jitter-backoff]]"
tags: [retry, backoff, resilience]
---

# Retry with a fixed interval

## Problem

A client needs to retry a request that failed transiently.

## Solution

Wait a constant interval (e.g. one second) between attempts, up to a maximum retry count.

## When to use

Superseded — a fixed interval synchronizes callers into retry storms under load. Use exponential backoff with full jitter instead.

## Connections

- [[Knowledge Base/Notes/Patterns/retry-with-full-jitter-backoff]]
