# Proposal

## Why

Recall can present a claim as current after a unit on a different page explicitly replaces it. The replacement can be outside the pages that supply current context.

## What Changes

- Retire the exact predecessor from current-context lines for personal recall with unrestricted source access.
- Keep the predecessor, its neighbours and its sources readable.
- Review possible contradictions separately from proven replacements.

## Capabilities

### Modified Capabilities

- `context-activation` honours cross-page unit replacements in current context.

## Impact

The change uses the existing graph, unit references and release rules. It adds no model, mutation route or migration.
