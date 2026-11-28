## Why

Stage 2 of `make-vault-store-canonical` stores each semantic unit as a derived row and its typed relationships as stored rows. A unit that becomes either end of a stored relationship gets an anchor, so its reference survives edits. Every other unit without an anchor has a reference bound to a fingerprint of its text, which changes with every edit. A soft reference to such a unit, such as a working-set entry, then stops resolving. Agents also have no operation that changes, moves or embeds one unit by name, and no page can show a unit that lives in another page.

A count on a copy of a real vault on 2026-11-24 found that soft references almost never name an unanchored unit: 4 working-set references to one unit in 30 days, and none in page text, corrections or episodes. Agent writes already anchor the units they write. The owner therefore chose to extend anchors to every unit that something names, rather than give every unit a stored record.

## What Changes

- **Anchors for soft references.** When a soft reference names an unanchored unit, the store writes that unit's anchor later, in a governed write on the store thread. The saving read never waits or fails. The write needs the saver's write admission, binds the page revision and a projected file, and waits until the file has been quiet for the edit-back settle window.
- **Aliases.** When the store replaces a unit's reference, by anchoring or moving the unit, it records an alias from the old reference to the unit's stage-2 unit id. Reference resolution follows it with one indexed lookup, before the exact match for a fingerprint-bound reference.
- **Unit operations.** A writer can update, insert, retire, move or embed a unit by its reference. An operation on an unanchored unit anchors it in the same commit. A move keeps the unit's stored relationships and rewrites its inbound embeds and unit-level links up to a bound that a measurement sets, so its hold stays under the mutation boundary's 5 s acquire timeout. `observe_memory` and the stage-2 claim operations use these operations.
- **Embeds.** A unit has one home page. Other pages show it with an Obsidian block embed. A rich unit with an `id` is named by its block ids wherever they sit in its body, as Obsidian resolves them. A name that two units share answers ambiguous, as duplicate anchors do. Its first embed reuses a unique one or writes a new one, unique on the page, at the end of its first paragraph, and a promotion keeps a compact unit's block id, so both forms render. A page read can expand embeds one level deep within a bound.
- **Referenced units.** A semantic write that would drop a unit that a relationship, an embed or a unit-level link names, on its own page or another, refuses with `REFERENCED_UNIT_CHANGED` unless it names the unit in `retire_units`. A hand edit that removes such a unit or its anchor opens stage 2's review item, with three repair choices.
- **Visibility.** Every cross-page answer applies stage 2's two-ended visibility. A unit move rewrites referring pages only within the mover's scope, and pages it leaves resolve through the alias and go to an owner review item. Rollback to file mode rewrites them before it projects.
- **Owner tables only.** The anchor requests and aliases live in two owner tables, which the registry pass creates. No collection-schema table changes, and an earlier build serves the store unchanged. Rollback to file mode exports the aliases.
- **Not in this change.** Full unit records, which the owner rejected. Generated pages, which wait for an outcome test in a later change.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `canonical-vault-store`: anchors for soft references and named units, aliases, unit operations by reference, embeds of compact and rich units, the referenced-unit preflight and review, cross-page visibility, and owner tables that an earlier build ignores. The unit requirements retain Stage 2's transaction and derivation rules. This change expands the cases in which the store writes an anchor.

## Impact

- **Code.** The store thread's anchor job and the reference and link resolvers (`vault_store`, `memory_refs.py`), the soft-reference savers (`served_context.py` and the correction and curation writers), `observe_memory.py` and the stage-2 claim operations, the rich-unit parser (`semantic_units.py`), the semantic writer's preflight and the page-edit tools (`retire_units`), the duplicate fold, the importer, page reads with embeds, and the rollback exports.
- **Related changes.** It builds on stage 2 of `make-vault-store-canonical` (D10, tasks 5.2, 5.5 and 5.6) and on `add-unit-level-supersession`, and archives after `make-vault-store-canonical`. This change also replaces D10's paragraph on the next stage with a pointer here.
- **Users.** A saved reference to a unit keeps resolving after an edit. Agents change a unit by its reference. Obsidian users see embeds as Markdown that Obsidian renders. A hand edit that drops a referenced unit opens a review item.
- **Operations.** About one anchor write a month on the measured vault. The build's registry pass creates the owner tables and derives every page once, as any owner-schema bump does. No compatibility id is needed, and a return to an earlier build needs no command.
