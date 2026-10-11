## Why

Collection store custody refuses a live store inside a file-sync root. The evidence it looks for is a sync client's metadata files, cloud document path components, platform variables, Windows sync folder names and vault sync plugins. That evidence changes whenever a sync provider changes, but it was fixed in code. An owner whose vault lives in another provider's folder had no way to declare it.

## What Changes

- A `sync-providers` vocabulary registry holds the evidence: a shipped pack under `src/exomem/vocabulary/packs/core/` plus the vault overlay `_Schema/sync-providers.yaml`.
- Each entry names one evidence kind and its value. Custody implements the closed set of kinds and reads the values from the registry.
- An overlay can only add entries. Shipped entries cannot be overridden or retired, so an overlay never weakens custody. A malformed overlay leaves the pack in force and reports a finding.
- A shared `FixedPackAdapter` in `vocabulary/registry.py` supplies this add-only grammar.

## Capabilities

### New Capabilities

- `sync-provider-registry`: sync-provider evidence for store custody is registry data that a vault overlay can only extend.

### Modified Capabilities

None. The custody contract in `move-structured-collections-to-sqlite` already speaks of configured supported sync roots and does not name providers.

## Impact

`collection_store/custody.py`, `collection_store/owner.py`, the new `sync_providers.py` and pack, `vocabulary/registry.py` and `vocabulary/__init__.py`. Custody verdicts do not change for any shipped provider.
