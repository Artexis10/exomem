## Context

The collection store keeps one `items` row per Records row. `item_versions`, `audit_effects`, `projection_state`, `version_identity`, `rollup_members` and every per-collection typed, native and query-projection table reference `items(row_id)`. History replay (`history.effects`) needs the `items` row for every past effect. Writes form a hashed chain of transactions.

About 60 reads across about 24 source files read `items` as the collection's current state. A row's `item_key` is a UUIDv5 of the collection and its serialized natural key, so a row re-admitted with the same natural key has the same `item_key`.

Design basis: Anthropic, "Writing effective tools for agents": a destructive tool is marked as such, takes explicit guards, and refuses with an actionable code.

## Goals / Non-Goals

**Goals**

- The owner can take one committed row out of a store collection's current state, with a reason, under the same authority and guards as an update.
- No version, audit effect or history of the row is lost.
- No existing current-state read has to change.

**Non-goals**

- No hard delete.
- No retraction on file-canonical collections, or on collections that render one view per row.
- No new tool on the simple add and edit surface.
- No bulk retraction.

## Decisions

### 1. `items` becomes a view of the current rows

Schema 18 renames the table `items` to `item_rows` and adds `retracted_txn INTEGER`. A `NULL` value means the row is current. A view named `items` selects the current rows with the exact column list of schema 17. A partial index `item_rows_current ON item_rows(collection_id, row_id) WHERE retracted_txn IS NULL` keeps current-row counts index-only.

- SQLite rewrites child foreign keys, indexes and trigger bodies on `ALTER TABLE ... RENAME`. The minimum supported SQLite (3.38) does this.
- The view flattens onto the unique and partial indexes.
- A write to the view fails loudly, so every raw writer that names `items` must move to `item_rows`. Blob I/O cannot open a view, so the blob readers move too.
- Reads that must see retracted rows (history, identity, typed version reads) read `item_rows`.

**Rejected alternatives:**

- A physical delete. It needs a new identity table and a rebuild of three append-only history tables, and it loses the row's governance metadata. `row_id` has no `AUTOINCREMENT`, so deleting the newest row lets the next insert reuse its `row_id` and inherit its history.
- A retracted marker filtered in every reader. Each missed reader silently brings the row back, and every future reader must remember the filter.
- A marker in existing columns without a schema change. An older release would show the retracted row as current, or fail its history check.

### 2. Retraction is a guarded write with its own audit effect

`record_memory(action="retract")` resolves the collection under the caller's write authority, binds the row by id or key, checks `expected_item_version` and `expected_container_hash`, and commits one transaction that:

- sets `retracted_txn` and clears `natural_key`;
- removes the row from the typed current table, the query-projection tables, its rollup memberships and its outgoing references;
- writes an audit effect `retract` with `version_before` and `hash_before` of the last version, and no after values;
- ends open query continuations and discards the collection's cached release state.

The effect material enters the transaction's event hash like any other effect. `history.effects` rebuilds it from the audit row and the version identity.

An absent row and a retracted row are refused identically, `RECORD_NOT_FOUND`. A caller who cannot read the whole collection gets the collection refusal, as for update. A request id replays.

### 3. Re-admission revives the same row

A later write with the same natural key finds the retracted row by `item_key`, clears `retracted_txn`, and writes the next version with an `insert` effect whose `version_before` is set. This keeps one row per `item_key`, so record references, history and the row-id binder stay one-to-one. Re-admission counts against the collection's row capacity. Row history shows identical re-admitted content; it skips only `update` effects with equal hashes.

### 4. Refused collection kinds

- File-canonical collections refuse `RETRACT_UNAVAILABLE` after the collection resolves under the caller's authority.
- A collection that renders one view per row refuses `RETRACT_UNAVAILABLE` in this change. Retiring and re-occupying its view paths needs its own design. Production creates use the summary view.
- Derived collections keep their existing refusal.

### 5. Migration 18

One immediate transaction:

1. Rename `items` to `item_rows`, and add `retracted_txn`.
2. Create the view and the partial index.
3. Rebuild `audit_effects` with the widened check and its foreign key to `item_rows`. Drop its append-only triggers first and recreate its index, as earlier revisions did.
4. Let schema setup recreate the triggers, then run `foreign_key_check`.

A failure leaves schema 17.

## Risks / Trade-offs

- **Every earlier release refuses a schema-18 store.** Back up the store with the current release before the upgrade. To go back, restore that backup, which loses writes made after it, or roll forward.
- **A reader that must see retracted rows but reads the view** silently omits them. The history, identity and typed version reads are named in the tasks, and the history tests cover them.
- **The import row counter assumed rows are never removed.** It becomes a count through the partial index.

## Migration Plan

1. Release this change on its own, after the release that carries `harden-records-import-and-row-access`.
2. Rehearse migration 18 on a byte copy of a production store. Compare integrity, foreign keys, history verification and per-collection current counts before and after.
3. Back up the store, then install.
4. Retract the one stored row with a `null` key part, and verify the collection's current count and that no current row has a `null` key part, from stored state.

**Rollback:** restore the pre-upgrade backup, or roll forward.
