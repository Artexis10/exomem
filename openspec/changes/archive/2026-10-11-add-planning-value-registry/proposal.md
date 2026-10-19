# Proposal

## Why

Planning kinds, statuses, priorities, commitments, horizons and health values are Python constants in `planning.py`, with copies in the audit, the saved-view check and the bootstrap.
A vault cannot add a status such as `waiting` without a code change, and the archive and hierarchy rules compare literal keys.

## What Changes

- Add the `planning-values` vocabulary registry: a shipped pack `packs/core/planning-values.yaml`, the vault overlay `_Schema/planning-values.yaml`, and the `schema_memory` subject `planning-values` with inspect, propose, save, history and restore.
- Key each entry `<field>.<value>`; a Planning item keeps storing the bare value. Shipped values are fixed; a vault adds values and deprecates its own.
- Declare two attributes: `class` on a status (the planning class: `open`, `done` or `dropped`) and `parents` on a kind. Both are fixed once saved.
- Validate kind, status, priority, commitment, horizon and health against the registry. A stored value stays readable after its definition is deprecated or removed; only a new or changed value must be active and registered.
- Read the archive rule and the audit's open-item rule from the planning class, and the parent-kind rule from `parents`.
- Read the bootstrap lists, the default horizon views and the saved-view horizon check from the registry.

## Capabilities

### Modified Capabilities

- `planning`: Planning values come from a governed registry; the archive rule uses the planning class, and the parent rule uses a kind's `parents`.

## Impact

This is vocabulary task S10 of `add-vocabulary-registries`.
`_collection_types/planning.yaml` keeps its bytes: a built-in type cannot change before the declared-type revision path (P4 of `move-structured-collections-to-sqlite`) exists.
Frozen hosted profiles keep their published bootstrap lists. Stored items are not rewritten.
The `class` here is separate from the page lifecycle `class` of `add-lifecycle-status-registry`.
