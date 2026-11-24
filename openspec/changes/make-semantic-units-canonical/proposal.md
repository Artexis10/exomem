## Why

Stage 2 of `make-vault-store-canonical` stores each semantic unit as a derived row and its typed relationships as stored rows. A stored relationship keys on a store-assigned unit id, and a table maps each anchored unit to its id. A unit without an anchor still has no stable identity: its reference is a fingerprint of its text and changes with every edit. A soft reference to it, such as a working-set entry or a memory reference, then stops resolving, and an agent's unit id for it goes stale. No page can show a unit that lives in another page.

This change gives every unit a record with a stable unit id, a lifecycle and a lineage. It keeps the page bytes as the one byte store, Markdown as their byte-exact projection, and the canonical write transaction as stage 1 bounds it.

A measurement on a copy of a real vault of 17,329 pages supports it. The existing parser split every page into units and raw segments and rebuilt every page byte for byte. 81.5% of pages hold no unit and stay whole. With a light identity rule and an anchor on every referenced unit, 100 generated hand edits replace about 17 units, and open about 0.06 review items at today's share of units with relationships.

## What Changes

- **Unit records.** Each unit of an indexed page gets a unit id, a placement (page, ordinal, byte span), a state and a lineage. An anchored unit keeps the unit id that stage 2 assigned. A history maps every reference that a unit had to its unit id. The unit reference that readers use stays derived from the bytes, and no derived row changes apart from a new `unit_id` column.
- **Identity in the derivation.** The canonical commit writes no unit record. The store thread matches identity when it derives the page, from the parse that the derivation already makes, by a light rule: exact bytes, then anchor, then the same slot with whitespace or one short run changed. No similarity score decides identity. A writer that knows which units it changed sends a declaration on the derivation intent.
- **Unit operations.** A writer can update, insert, retire, move or embed a unit by its unit id, as a page write with a declaration. `observe_memory` and the stage-2 claim operations use them. A semantic write that would drop a referenced unit refuses with `REFERENCED_UNIT_CHANGED` unless it names the unit in `retire_units`.
- **Hand edits.** An outside edit imports through the same commit after the edit-back settle window. A retired referenced unit, or a kept one that lost its anchor, keeps its references and opens a review item with three choices: restore the anchor, attach or retire.
- **Anchors.** The store writes an anchor into a unit only when it becomes either end of its first stored relationship or gains its first embed.
- **Embeds.** A unit has one home page. Other pages show it with an Obsidian block embed. A page read can expand embeds within a bound.
- **Trash.** A trashed page's units keep their ids and relationships, and a recovery restores them.
- **Split pass.** A registry or parser change splits the affected pages again in bounded background batches. A page that a write reaches first takes the pass's step before the write's match, so the writer is never blamed for registry drift.
- **Parity.** Verification and a batched audit check four invariants: placements, split, references and derived rows. `exomem store-mode resplit` repairs a page that fails one.
- **Migration and rollback.** The upgrade pass records `vault-store-v2` before its first batch, so an older build refuses the store. Rollback to stage 2 or to file mode exports the unit records, the reference history, the lineage and the open review items, and a later migration matches the export by the light rule.
- **Not in this change.** Generated pages, which render a stored selection of units, are a follow-up change that waits for the outcome evaluation.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `canonical-vault-store`: unit records, identity in the derivation, declarations, unit operations, review of referenced units, anchors, embeds, trash, the split pass, parity and the migration and rollback of unit records.

## Impact

- **Code.** The derivation (`vault_store/derive.py`, `derivation.py`), `vault_store/write.py` and `batch.py` (declarations), the semantic writer's preflight and the page-edit tools (`retire_units`), `observe_memory.py`, `vault_store/importer.py` and `held.py`, `migrate.py`, `rollback.py`, `verify.py` and `rederive.py`, `semantic_unit_read.py`, `get_page.py`, `memory_refs.py`, the file watcher, and the schema list in `collection_store/schema.py`. About 45 other write sites in 35 modules hand whole-page bytes to the commit and need no change.
- **Related changes.** It builds on stage 2 of `make-vault-store-canonical` (D10, tasks 5.1, 5.2, 5.5 and 5.6) and archives after that change. This change also replaces D10's paragraph on the next stage with a pointer here.
- **Users.** Agents see unit ids that stay the same across edits and moves. Obsidian users see embeds as Markdown that Obsidian renders. A hand edit that drops a referenced unit opens a review item.
- **Operations.** The upgrade pass runs with the service up, in batches. A build without this change refuses to serve an upgraded store from the pass's first batch. Rollback to stage 2 is an explicit command.
