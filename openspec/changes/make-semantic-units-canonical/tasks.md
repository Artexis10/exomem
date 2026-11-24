This change starts after the owner's vault switches to store mode and stage 2 of `make-vault-store-canonical` ships. A group waits only on what its heading names. Each group from 1 on ends with its highest-level check.

## 0. Before the build

- [ ] 0.1 Read the shape that stage 2 shipped: the anchor write for a relationship end and its write-admission rule, the anchor-to-id table and store-assigned unit id (5.5), unit-level link edges (5.2), and the review item for a removed unit (5.6). Record any difference from design A2, A4 and A7 in the design.
- [x] 0.2 On a read-only copy of a real vault, count the soft references that name a unit without an anchor, and how many a read path saved. Done 2026-11-24 with the product's own parsers. Working set, 30 days: 5,997 rows, 4 references to one unanchored unit, all position references. Page text: 5,369 memory references, none unanchored. Corrections: 7,883, none unanchored. Episodes: 3 conversation references, none unanchored. Compiled history: 372 unit references, 98 stale, saved by the writer. The owner chose anchors over full records on these counts.

## 1. Anchors for soft references

- [ ] 1.1 Add the anchor-request and alias tables as owner objects under `vault_store_owner_schema`, named in `store_mode_objects`, each indexed by reference. Add no column or index to a collection-schema table.
- [ ] 1.2 Let every save of a soft reference in store mode (the served-context record, memory references, the correction and curation writers) hand each fingerprint-bound reference to the store thread in memory, with the saving principal. The save never waits or fails for it.
- [ ] 1.3 On the store thread, record each request once per unit. Between requests, take up to 32 per step: drop a request whose unit no longer carries that reference or whose principal may not write the page; wait while the file changed within `edit_back.SETTLE_SECONDS` or the page is not projected; otherwise commit one governed write that adds only the anchor, binding the page revision and `Expect.projected`, and record the alias and delete the request in the same transaction. Expire a request after 30 days.
- [ ] 1.4 Follow an alias in reference resolution, with one indexed lookup, when the exact match is stale or missing.
- [ ] 1.5 Report in status the count of pending anchor requests with the oldest age, and the count dropped or expired. Only the owner's status names their pages.
- [ ] 1.6 Highest-level check: on a real-vault copy, a recall that saves a working-set entry for an unanchored unit returns without writing; the anchor lands once the file is quiet; a later edit of the unit's text keeps the saved reference resolving. A file changed within the settle window waits, a unit changed first gets no anchor, and a reader that may not write the page gets none. A scripted editor that saves while the anchor job runs opens no held item in 100 runs, or, when the projection race fires, a held item whose keep-mine drops only the anchor.

## 2. Unit operations by reference

- [ ] 2.1 Add the unit operations of design A4 as page writes that bind the page revision: update, insert, retire, move and embed. An operation on an unanchored unit writes its anchor in the same commit, records an alias and replies with the anchored reference.
- [ ] 2.2 Route `observe_memory` add, update and remove, and the stage-2 claim operations, through the unit operations.
- [ ] 2.3 Let a move keep or rewrite the anchor, move stage 2's anchor-to-id row, record an alias, update the aliases that point at the old reference, and rewrite the unit's embeds in up to 256 other pages in its commit. Refuse beyond that with `UNIT_MOVE_TOO_WIDE`.
- [ ] 2.4 Refuse an operation that needs an anchor in a visible page that the caller may not write with `UNIT_PAGE_NOT_WRITABLE`, changing nothing.
- [ ] 2.5 Let the duplicate fold move the copy's stored relationships to the canonical page's unit with the same anchor and record an alias for each copy reference.
- [ ] 2.6 Update the skill scaffold so agents change a unit by its reference and know `retire_units`.
- [ ] 2.7 Highest-level check: an agent updates an unanchored unit's text through `observe_memory` three times, then moves it to another page. Its reference after the first update, its unit id and its stored relationships stay the same, and a reference saved before the first update resolves to it on its new page. A stale revision refuses and names the current one.

## 3. Embeds

- [ ] 3.1 Let an embed of an unanchored unit write the home page's anchor in the same commit.
- [ ] 3.2 Let a page read expand embeds in one batched query, up to 64 embeds and 256 KiB, and say when it stopped at the bound.
- [ ] 3.3 Highest-level check: page B embeds a unit of page A; a hand edit of the unit in A shows in B without a write to B; a page with 100 embeds returns 64 with the bound notice in one query.

## 4. Referenced units

- [ ] 4.1 In the semantic writer's preflight, refuse with `REFERENCED_UNIT_CHANGED` a write that removes a referenced unit or its anchor, unless the write names the unit in `retire_units`. Read inbound unit links by stage 1's lag rule. Add `retire_units` to the page-edit tools.
- [ ] 4.2 Extend stage 2's review item for a removed unit to embeds, unit-level links and a kept unit that lost its anchor. List embedding pages 50 at a time with their total. Add its three repair-tool choices: restore the anchor, attach, retire.
- [ ] 4.3 Import a page that holds referenced units only after its bytes stay unchanged for `edit_back.SETTLE_SECONDS`.
- [ ] 4.4 Highest-level check: a page edit that would drop an embedded unit refuses and names both ways forward; a write that drops a unit that only a soft reference names commits; deleting an embedded unit by hand opens a review item that names the embedding page; typing after its anchor opens one that offers to restore it; attaching it to another unit of the same page makes the embed resolve again; a scripted editor that saves every 300 ms while it types opens no review item.

## 5. Compatibility, rollback and acceptance

- [ ] 5.1 Export the aliases with D8's rollback exports, and load them in a later migration.
- [ ] 5.2 On a disposable copy of a real vault: run this change's build on a stage-2 store, then a stage-2 build, then this change's build again; the collection schema equals stage 2's throughout, and aliases and pending requests resume. Run a rollback to file mode and a migration back, and check that a moved unit's old reference resolves again.
- [ ] 5.3 Measure the hold of the vault mutation boundary for a page write and a unit operation before and after this change on the same instrument. A page write's hold may not grow beyond its run-to-run spread.
- [ ] 5.4 Archive this change only after `make-vault-store-canonical` has archived, so its MODIFIED requirement has a base.
