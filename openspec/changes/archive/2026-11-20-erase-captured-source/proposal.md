## Why

Service callers must withdraw one captured Source without leaving its managed copies searchable or recoverable through ordinary runtime state.
Ordinary deletion preserves raw evidence and trash; index removal alone cannot establish complete logical erasure.

## What Changes

- Add one explicit, owner-enabled erasure operation for independent identity-bearing text captures.
- Keep host policy disabled by default and ordinary append-only capture, editing and deletion behavior unchanged.
- Bind every erase operation to one immutable Source reference and expected hash, so delayed retries cannot delete a new incarnation.
- Retain resumable cleanup progress and report complete, pending, failed or unknown outcomes without inventing absence.
- Refuse known dependent content and correction chains before retirement; report missing ownership or dependency evidence as unknown.
- Reuse existing persistence owners and publication machinery, adding only the custody evidence needed for producer-owned cleanup.
- Publish Source custody with canonical producer batches so dependency evidence follows normal replica handoff.
- Qualify configured erasure policy through an authenticated read, separately from static operation discovery.
- Permit fresh capture of the same logical identity after completed retirement, without permanent identity tombstones.

No model makes an erasure or dependency decision.
The explicit operation grants no creator activation or production write authority.

## Capabilities

### New Capabilities

- `captured-source-erasure`: scoped, incarnation-fenced logical erasure with producer-owned cleanup and explicit completeness.

### Modified Capabilities

- `durable-source-capture`: qualify immutable preservation with the explicit owner-enabled erasure exception and prevent erased capture receipts from replaying as live Sources.

## Impact

The change touches canonical capture custody, private retention producers, derived-data cleanup and the shared governed command registry.
P4 remains the identity inventory; the custody evidence is not a second Source catalogue or watcher.
This change covers file-mode vaults. A store-mode vault refuses erasure until `erase-captured-source-in-store` ships its purge and provenance integration.
cellctl and the platform chart gain a per-cell policy selection, so the operator enables erasure one cell at a time.
Q retains each issued retirement intent until that exact Source incarnation finishes cleanup, then may capture a fresh incarnation.

The guarantee excludes earlier backups, downloaded exports, caller-held copies, external synchronization history and forensic storage remnants.
Those copies can reintroduce material when restored; the operation does not claim permanent global nonexistence.
