# Proposal

## Why

The call ledger records a completed activation as `ok` even when the final packet abstains after request-budget exhaustion.
Operators cannot distinguish that result from normal execution with a non-abstained packet.

## What Changes

- Record strict activation abstention as `outcome: abstained`, with no error code.
- Add an optional, content-free `activation_result` observation to schema version 1.
- Preserve `ok` for non-abstained packets, including packets whose optional work meets the request budget.
- Update strict ledger readers and timing joins together with the producer.
- Treat historical activation semantics as unknown; retain historical rows and hashes without modification.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `call-ledger`: distinguish observed activation abstention from normal completion and preserve honest historical interpretation.

## Impact

The change affects the MCP wrapper, request context, middleware, ledger builder and offline ledger readers.
A small shared module owns the fixed activation reason protocol and bounded observation projection.
The activation response, 50-second deadline, retrieval policy and production controls retain their current behavior.
The existing latency watch continues to report timing independently.
The observation soft-fails without changing a tool response. It adds no model, polling or mandatory benchmark call.
