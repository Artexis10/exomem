## Why

The hosted handoff validates destinations but still requires operators to rediscover the source of the production control-plane key. Bind that source to its existing BWS entry so the routine path cannot substitute a same-named secret or a provider display placeholder.

## What Changes

- Add an explicit BWS source kind to the existing secret handoff matrix and CLI.
- Record the production control-plane key's exact source binding and use the shared `bwsx-secret` validator for retrieval.
- Document the BWS-first path while retaining deliberate stdin/prompt compatibility.

## Capabilities

### New Capabilities

- `hosted-secret-custody`: identity-bound source retrieval for governed hosted secret delivery.

### Modified Capabilities

None. Destination, version, receipt and rotation rules are unchanged.

## Impact

Infrastructure handoff, matrix, binding data, tests and runbook only. The shared BWS tool is an operator prerequisite, not a server dependency. No live secret, database credential, encryption key, gateway destination or deployment changes in this delivery.
