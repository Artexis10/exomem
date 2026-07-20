## Why

An MCP retry can currently time out on the vault mutation boundary and report `MUTATION_BUSY` while the original identical request continues and commits. On 2026-07-17 that ambiguity caused an agent to retry three changed `remember` payloads, creating three distinct notes while every visible attempt appeared to fail.

## What Changes

- Claim and resolve mutation idempotency before mutation-boundary contention, then persist the terminal committed result before response delivery.
- Give identical in-flight retries a bounded path to the original terminal result; if it is still pending, report acknowledgement uncertainty rather than a false pre-commit busy result.
- Bind explicit and implicit mutation identity to tenant/vault, authenticated principal, operation, and canonical payload; reject key reuse with a different payload.
- Expose stable request correlation in mutation logs and preserve deterministic fault-injection seams around terminal-result persistence and response delivery.
- Classify read-only maintenance audit invocations as reads so diagnostics cannot retain the mutation boundary.
- Preserve the HA edge rule that an ambiguous MCP `tools/call` is never replayed to another origin.

## Capabilities

### New Capabilities

- `acknowledgement-safe-mutations`: Terminal-state, replay, cancellation, and acknowledgement-loss guarantees for mutations across MCP, REST, and CLI.

### Modified Capabilities

- `hosted-mutation-safety`: Idempotency must be consulted before lock contention, principal isolation must apply to caller-supplied keys, and read-only maintenance audit must not acquire the mutation boundary.

## Impact

- `writer_lease.py`, mutation error/result handling, MCP command binding, hosted/personal request context, and structured logging.
- Deterministic unit and black-box tests for in-flight replay, response interruption, cancellation, contention, key mismatch, principal isolation, audit classification, and Cloudflare single-origin fail-closed behavior.
- Local idempotency runtime state only; no vault migration and no new network dependency.
