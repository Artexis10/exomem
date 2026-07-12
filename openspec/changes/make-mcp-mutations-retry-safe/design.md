## Context

FastMCP tool calls currently reach the shared command leaf through `bind_vault`, but that adapter supplies no idempotency key. If an authenticated MCP client loses a successful acknowledgement and repeats identical arguments, the writer lease correctly authorizes both calls and additive commands create duplicates. The existing SQLite idempotency store is durable but only used when REST or CLI callers explicitly supply a key, and only while lease coordination is enabled.

## Goals / Non-Goals

**Goals:**

- Replay successful, identical MCP mutation results when the same authenticated client retries promptly.
- Keep failed calls retryable and intentional later repetitions executable.
- Preserve the existing response schema and explicit idempotency contract.
- Apply explicit and implicit idempotency in standalone as well as coordinated deployments.

**Non-Goals:**

- Promise distributed exactly-once filesystem transactions across a replica crash at every possible instruction boundary.
- Deduplicate read operations or calls from different authenticated principals.
- Store vault content or mutation results in the lease coordinator.

## Decisions

### Derive an implicit key from authenticated principal, command, and arguments

The MCP adapter will hash the bearer credential (never persist the credential itself) to create a caller scope. The lease manager will combine that scope with its existing canonical command/argument digest. This catches a retry even if FastMCP allocates a new JSON-RPC request ID or MCP session after a lost acknowledgement. If no bearer credential is available, the adapter falls back to FastMCP's session ID; outside MCP it supplies no implicit scope.

Using only request ID was rejected because a retry may receive a new ID. Global argument-only deduplication was rejected because one user's call could suppress another's.

### Use a bounded implicit retry window

Implicit completed entries expire after 60 seconds. Exact repeats within that window replay the stored result; after it they execute normally. Explicit idempotency keys retain their existing durable semantics and mismatch protection. Failed operations remove their pending entry immediately and are never cached.

An unbounded implicit cache was rejected because identical user-intended actions must remain possible. A very short transport-only window was rejected because agent retries can include model/tool round trips.

### Reuse the local durable SQLite store

The existing per-replica idempotency store will support expiring implicit entries and work even when writer leasing is disabled. Results remain in trusted local runtime state, outside the synced vault. Replay emits an informational log without altering the returned leaf result.

Remote shared result storage was rejected for this change because it would export serialized mutation results, expand the coordinator contract, and still could not make a local filesystem mutation and remote acknowledgement atomic. The writer lease continues to provide cross-replica exclusion; this change addresses the observed same-writer acknowledgement-loss retry.

## Risks / Trade-offs

- [A user intentionally repeats exactly the same mutation within 60 seconds] → The second call replays; callers needing deliberate rapid repetition can vary the operation input or use a later call after the bounded window.
- [A retry lands on another replica after the first replica disappears] → Per-replica replay state cannot prove the first local commit; the lease prevents concurrency but distributed exactly-once semantics remain a documented non-goal.
- [Bearer credentials appear in runtime state] → Only a one-way SHA-256 scope digest is persisted as part of another hashed key; raw credentials are never logged or stored.
- [SQLite accumulates expired implicit rows] → Opportunistically prune expired implicit entries during mutation calls.

## Migration Plan

Ship as an in-place runtime behavior improvement with no vault migration. Restart Exomem to load it. Rollback restores the previous invocation behavior; the local SQLite table remains schema-compatible.

## Open Questions

None for this scoped fix.
