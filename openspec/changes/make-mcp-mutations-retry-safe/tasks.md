## 1. Retry Semantics Tests

- [x] 1.1 Add unit tests for bounded implicit replay, expiry, principal isolation, and failure retry
- [x] 1.2 Add regression coverage for explicit idempotency when writer leasing is disabled
- [x] 1.3 Add MCP adapter tests proving bearer-derived scope without credential leakage

## 2. Core Implementation

- [x] 2.1 Extend the durable idempotency store with expiring implicit entries and opportunistic cleanup
- [x] 2.2 Apply idempotency to standalone mutations while preserving lease gating for coordinated deployments
- [x] 2.3 Derive and pass an authenticated MCP retry scope from the FastMCP request context
- [x] 2.4 Log implicit replay decisions without changing tool result shapes

## 3. Verification and Documentation

- [x] 3.1 Run focused retry, command-surface, REST, and writer-lease tests
- [x] 3.2 Run Ruff and the full non-embedding test suite
- [x] 3.3 Document bounded MCP retry behavior and its cross-replica limitation
