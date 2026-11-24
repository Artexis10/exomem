## Why

Stage 2 of `make-vault-store-canonical` stores each semantic unit as a derived row and its typed relationships as stored rows. The page bytes stay the source of a unit's identity: a unit's reference is its anchor, or a fingerprint of its text that changes with every edit. A unit that moves to another page gets a new reference. No page can show a unit that lives in another page, and no page can be generated from the units that match a question.

This change makes the store the source of each unit's identity and lifecycle. It keeps the page bytes as the one byte store and Markdown as their byte-exact projection.

A measurement on a copy of a real vault of 17,329 pages supports it. The existing parser split every page into units and raw segments and rebuilt every page byte for byte. 81.5% of pages hold no unit and stay whole. With a light identity rule and an anchor on every referenced unit, 100 generated hand edits replace about 17 units, and open about 0.06 review items at today's share of units with relationships.

## What Changes

- **Unit records.** Each unit gets a store-assigned unit id, a placement (page, ordinal, byte span), a lifecycle and a lineage. A history maps every reference that a unit ever had to its unit id. The unit reference that readers use stays derived from the bytes, and no derived row changes apart from a new `unit_id` column.
- **Identity at commit.** A writer splits a page's new bytes before it takes the vault mutation boundary. The commit matches the split against the page's unit records by a light rule: exact bytes, then anchor, then the same slot with whitespace or one word changed. No similarity score decides identity. A writer that knows which units it changed declares them.
- **Unit operations.** A store change can update, insert, retire or move a unit by its unit id. `observe_memory` and the stage-2 claim operations use them. A governed write that would retire a referenced unit without saying so refuses with `UNIT_SUCCESSOR_REQUIRED`.
- **Hand edits.** An outside edit imports through the same commit, after its bytes stay unchanged for 2 seconds. A retired referenced unit keeps its references and opens a review item.
- **Anchors.** The store writes an anchor into a unit only when the unit gains its first reference: a stored relationship, an embed or a pin by a generated page.
- **Embeds.** A unit has one home page. Other pages show it with an Obsidian block embed. A page read can expand embeds within a bound.
- **Generated pages.** A page can be rendered from a stored definition that selects units by registry values. A job renders it again when its member set changes. A hand edit of a generated page is held.
- **Split pass.** A registry or parser change splits the affected pages again in bounded background batches and keeps unit ids by the same rule.
- **Parity.** Verification checks four invariants: placements, split, references and derived rows.
- **Migration and rollback.** An upgrade pass assigns unit records to a stage-2 store. Activation records `vault-store-v2`. Rollback to stage 2 or to file mode exports the unit records, the reference history, the lineage, the generated-page definitions and the open review items.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `canonical-vault-store`: unit records, identity at commit, unit operations, review of retired referenced units, anchors, embeds, generated pages, the split pass, parity and the migration and rollback of unit records.

## Impact

- **Code.** `vault_store/write.py` (the commit), a new split module, `vault_store/importer.py` and `held.py`, `vault_store/migrate.py`, `rollback.py`, `verify.py` and `rederive.py`, `observe_memory.py`, `semantic_unit_read.py`, `get_page.py`, `memory_refs.py`, the file watcher, and the schema list in `collection_store/schema.py`. About 45 other write sites in 35 modules hand whole-page bytes to the commit and need no change.
- **Related changes.** It builds on stage 2 of `make-vault-store-canonical` (D10, tasks 5.1, 5.2, 5.5 and 5.6) and archives after that change. It reuses stage 2's relationship table, unit-level links and review item. It works best when stage 2 keys stored relationships by a store-assigned unit id.
- **Users.** Agents see unit ids that stay the same across edits and moves. Obsidian users see embeds and generated pages as Markdown that Obsidian renders. A hand edit that retires a referenced unit opens a review item.
- **Operations.** The upgrade pass runs with the service up, in batches. A build without this change refuses to serve an upgraded store. Rollback to stage 2 is an explicit command.
