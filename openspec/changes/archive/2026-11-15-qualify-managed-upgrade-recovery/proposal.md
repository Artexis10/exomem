# Proposal

## Why

The rehearsal checklist rejects a full repair that safely declines a busy database and later recovers. The lexical freshness contract requires that retry. Installation acceptance needs one durable definition that distinguishes recovered contention from unresolved work.

## What Changes

- Qualify the target's recovered catalogue state within the existing fixed observation window.
- Keep the handoff, readiness and caller read/write checks.
- Preserve declined attempts, errors, unknown attribution and earlier verdicts.
- Align the runbook with the managed-upgrade specification.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `managed-service-upgrades`: define rehearsal acceptance through observed publication, authoritative admission and caller availability.

## Impact

This change affects the runbook and its acceptance contract. It adds no runtime code, API, dependency or retry. Existing WAL safety and runtime budgets continue to apply.
