---
type: entity
entity_type: concept
status: active
created: 2026-04-18
updated: 2026-04-18
domain: distributed-systems
tags: [idempotency, api, http]
---

# Idempotency Key

## Summary

An idempotency key is a client-generated unique token sent with a mutating request so the server can deduplicate retries. The client puts the token in the `Idempotency-Key` header; if the same key arrives twice, the server returns the stored result of the first attempt instead of applying the operation again.

## Why in the KB

Referenced whenever a payment or order API needs exactly-once semantics on top of at-least-once retries. The exact header name `Idempotency-Key` is the interoperable convention.

## Connections

- [[Knowledge Base/Notes/Patterns/retry-with-full-jitter-backoff]]
