## Why

A Records query can exhaust its time limit behind a busy store thread. The committed rows remain available in SQLite while the thread works.

## What Changes

- Serve ordinary queries for store-owned Records from the last commit for owners with unrestricted access and an empty release policy.
- Keep row selection, source proof, and result versions in that same snapshot.

## Capabilities

### Modified Capabilities

- `records` can answer a qualifying query while the store thread runs another operation.

## Impact

The change reuses the bounded Records query session and the store's existing read admission. Restricted reads, typed queries and derived collections use their existing routes.
