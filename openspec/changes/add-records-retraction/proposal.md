## Why

An agent or an import can admit a wrong row into a store-backed Records collection. A validation defect, now fixed, stored a row whose natural key held a `null` part. The owner has no governed way to take such a row out of the collection's current state. No store operation removes a committed row. A direct delete would lose the row's history and would violate the foreign keys that tie every version and audit effect to the row.

## What Changes

- Add `record_memory(action="retract")` for store-backed Records collections. It takes the collection, a row id or item key, the current item and container guards, and a `why`.
- Retraction takes the row out of current state. Queries, counts, inspect and row-id binding then treat it as absent. Every version and audit effect of the row stays.
- Retraction writes a `retract` audit effect into the history chain. Row history shows the retraction and its reason.
- A later write with the same natural key brings the row back into current state as its next version.
- Store schema 19 adds the representation. The table of rows keeps every row; `items` becomes a view of the current rows, so the existing current-state reads do not change. The migration also widens the audit effect check to admit `retract`.
- Retraction is refused on file-canonical collections and on collections that render one view per row.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `records`: adds governed retraction of a committed row.

## Impact

- Store schema 19. Every earlier release refuses a schema-19 store. To go back after the upgrade, restore the backup taken before it, or roll forward.
- `collection_store` writer, schema, history, typed storage, rollups, reference index and importer; `record_memory`, `operation_entries` and the generated capability docs.
- Builds on the row-id binder and on the set-based batch reads of the change `harden-records-import-and-row-access`.
