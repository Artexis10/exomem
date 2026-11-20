## Why

`erase-captured-source` erases a captured Source incarnation in file-mode vaults only, and a store-mode vault refuses erasure.
Store-mode cells need the same guarantee before a service caller can hold Sources there.
Several file-mode gaps also remain: legacy coverage, residue producers and dependency clearing.

## What Changes

- Add the store purge for page rows, held content, projection intents and files, and recovery copies, with redacted audit rows.
- Retire or replace managed store snapshots and published replicas older than the purge revision.
- Lift the store-mode refusal once the store purge acknowledges its components.
- Reconcile legacy coverage in bounded steps, and invalidate it for unexplained external changes through the existing watcher.
- Wire capture producers that reserve pre-reference residue before their bytes can survive.
- Clear a dependency reservation that proves no effect, and reconcile dependency evidence after replica handoff.
- Qualify the p95 latency of a progress call on the generated 20,000-Source reference.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `captured-source-erasure`: extend complete retirement to store-mode vaults, legacy coverage and dependency reconciliation.

## Impact

The change touches the vault store purge and audit migration, managed replica publication, capture residue producers and dependency custody.
The canonical store owner reviews the purge contract and the audit migration.
The host erasure policy stays off by default; enabling a store cell stays an operator action.
