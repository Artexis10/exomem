# Proposal

## Why

An import can commit a chunk before its mutation terminal exists. The writer then blocks an exact retry, stranding an otherwise recoverable run.
The public status response includes every inventory, plan and outcome row, so an agent cannot inspect a large interrupted run.

## What Changes

- Reconcile interrupted import applies under their original mutation identity, with a frozen invocation scope and exact per-item custody checks.
- Preserve completed Sources, artifacts, draft notes and originals through interrupted publication and settlement.
- **BREAKING**: Return bounded public run status by default, with revision-bound pages for inventory, selection, plan and outcomes.
- Preserve the full internal browser status view and keep unrelated unknown mutations fail-closed.
- Ship updated generated tool schemas and recovery guidance with synthetic boundary proof.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `adoption-studio`: Recover interrupted apply calls and inspect large runs through bounded public views.
- `retry-safe-mutations`: Admit command-scoped import reconciliation without treating partial commits as complete mutation receipts.

## Impact

The change affects the adoption engine, writer idempotency store, shared command registry, generated plugin contracts and import guidance.
It reuses existing custody, rename and canonical batch machinery. It adds no model, provider, background service or optional heavy capability.
Synthetic isolated tests cover crash cuts, mutation identity, scope, large status and original-byte custody. Customer state is outside implementation and verification.
