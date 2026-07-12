## Why

An MCP client can lose or misclassify a successful mutation response and retry the same tool call. Exomem currently commits each retry independently when MCP supplies no explicit idempotency key, producing duplicate notes and misleading `NOT_FOUND` errors after successful deletes.

## What Changes

- Make retried MCP mutations replay the original result instead of executing the filesystem mutation again.
- Scope implicit retry detection narrowly by caller session, command, and canonical arguments, with a bounded lifetime so intentional later repetitions still execute.
- Preserve explicit `Idempotency-Key` behavior for REST and CLI callers as the stronger caller-controlled contract.
- Make replay decisions observable in server logs and tests without changing tool result shapes or storing vault content in coordination services.

## Capabilities

### New Capabilities

- `retry-safe-mutations`: Bounded, session-scoped deduplication and result replay for ambiguous MCP mutation retries.

### Modified Capabilities

<!-- None. -->

## Impact

- MCP command registration and invocation context
- Writer-lease idempotency storage and replay behavior
- Mutation regression tests across save and delete operations
- Deployment/runtime state only; no vault schema or external dependency changes
