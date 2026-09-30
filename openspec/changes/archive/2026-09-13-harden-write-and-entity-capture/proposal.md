## Why

Exomem 0.24.2 can leave callers facing minutes of opaque `MUTATION_BUSY` contention after an edit/preflight sequence, while acknowledgement-loss retries are not replay-safe in the released tree.

This undermines governed-write reliability.

The original change also introduced the first registry-driven entity-capture contract. That scope subsequently shipped through the vault-extensible `entity-type-registry` and was superseded by `complete-recurring-entity-lifecycle`; it is removed from this active delta so this historical change cannot reintroduce a fixed release-owned ontology.

## What Changes

- Keep `edit_memory` as the canonical surgical-edit tool, but make validate-only invocations genuinely read-only and non-blocking on the vault mutation boundary.
- Integrate replay-safe mutation receipts so identical MCP retries wait/replay outside the exclusive mutation boundary instead of surfacing ambiguous `MUTATION_BUSY` results after acknowledgement loss.
- Add bounded owner/request/age telemetry for the vault mutation boundary and expose safe readiness diagnostics for long holders without leaking vault content.
- Bound background reconciliation work so optional media maintenance cannot monopolize the global vault mutation boundary for an unbounded batch.
- Add real edit/preflight/cancellation/retry regressions and bounded background-writer tests.

## Capabilities

### Modified Capabilities
- `hosted-mutation-safety`: Strengthens cancellation/retry behavior, long-holder observability, and bounded background-writer participation in the shared mutation boundary.
- `command-surface`: Classifies `edit_memory(validate_only=true)` as read-only across MCP, CLI, REST, and bootstrap guidance.

## Impact

Affected areas include writer-lease/idempotency coordination, mutation-lock telemetry, file-watcher/media reconciliation, command read/write classification, readiness output, focused transport/write tests, and connector contract verification. The `edit_memory` tool name remains compatible.
