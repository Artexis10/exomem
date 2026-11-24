This change starts after stage 2 of `make-vault-store-canonical` ships and the owner's vault runs it. A group waits only on what its heading names. Each group ends with its highest-level check.

## 0. Prerequisites in stage 2

- [ ] 0.1 Confirm the shape that stage 2 shipped: the derived unit row set (5.1), unit-level link edges (5.2), the stored relationship table and its one store function (5.5), and the review item for an orphaned relationship (5.6). Record in design U11 whether stored relationships are keyed by unit reference or by a store-assigned unit id, and size the re-key of 1.4 from it.

## 1. Unit records and the upgrade pass

- [ ] 1.1 Add the unit record, placement, reference history and lineage tables, with indexes by unit id, by page and ordinal, and by reference. Add them to `VAULT_STORE_TABLES` and `store_mode_objects`, so the collection replica leaves them out and rollback can drop them.
- [ ] 1.2 Record each page's split version (parser version and registry fingerprint) and the store's parser version in `store_meta`.
- [ ] 1.3 Add the upgrade pass. It assigns records to every page that holds units, in path order, one bounded batch under the vault mutation boundary at a time, with its cursor in the batch's transaction. A commit to a page beyond the cursor assigns that page's records first.
- [ ] 1.4 Move each stage-2 relationship keyed by unit reference to its unit id in the batch that assigns the unit.
- [ ] 1.5 Record `vault-store-v2` when the pass ends. A build without this change refuses to serve the store and names the rollback.
- [ ] 1.6 Add `exomem store-mode upgrade-units --plan` and `--apply`. The plan prints the pages and units that the pass would assign. The CLI fence declares both.
- [ ] 1.7 Highest-level check: on a generated 20,000-page stage-2 store, the plan and the apply agree, a kill at a random batch resumes at the cursor, every derived row is unchanged apart from `unit_id`, and the four checks of 9.1 pass.

## 2. Split and match in the commit

- [ ] 2.1 Add the split: a pure function from raw page bytes, parser version and registry to placements. It cuts byte spans from the raw bytes, so CRLF and a byte-order mark survive. It reuses `parse_semantic_units`.
- [ ] 2.2 Run the split in the writer before it takes the vault mutation boundary, and pass it with the bytes in `PagePut` and in `PageMove` with a new body. Split again under the boundary when the split version no longer matches.
- [ ] 2.3 Match the split against the page's live records at the base revision inside the commit's transaction, by the light rule of design U3. Write only the records of units that changed, and move later placements with one statement.
- [ ] 2.4 Accept a declaration in `PagePut` and check it against the split. Refuse a declaration that does not fit with `UNIT_DECLARATION_INVALID`.
- [ ] 2.5 Record lineage: `replaces` when exactly one new unit took a retired unit's slot, `moved_from` for a moved unit, `folded_into` for a folded copy.
- [ ] 2.6 For a retired referenced unit in an outside-editor commit, keep every reference and open one review item through stage 2's review item.
- [ ] 2.7 For a retired referenced unit in a governed commit without a declaration, refuse with `UNIT_SUCCESSOR_REQUIRED`, naming the unit and the two choices.
- [ ] 2.8 Highest-level check: replay the spike's generated hand edits on a real-vault copy through the real importer. The replaced units per 100 edits match the spike's figures for the same anchor state within the run's interval, no unit is kept wrongly by the spike's check, and every retired referenced unit has a review item.

## 3. Unit operations

- [ ] 3.1 Add update, insert, retire and move to `StoreChange`. Each binds the page revision and names the unit id with its version. Update splices bytes at the placement.
- [ ] 3.2 Route `observe_memory` add, update and remove, and the stage-2 claim operations, through the unit operations.
- [ ] 3.3 Take the head of the agent-facing unit id (`page_ids.part_id`) from the unit id. Resolve an id issued before the upgrade through the reference history.
- [ ] 3.4 Let a move rewrite the embeds of the moved unit in other pages in the same transaction, up to its bound, and refuse beyond it with a remediation.
- [ ] 3.5 Update the skill scaffold so agents change a unit by its id.
- [ ] 3.6 Highest-level check: an agent updates an unanchored unit's text through `observe_memory` three times, then moves it to another page. Its unit id and its stored relationships stay the same, and the page bytes equal what the old path would have written.

## 4. Readers

- [ ] 4.1 Resolve a stored unit reference through the reference history in `semantic_unit_read`, memory references and conversation references.
- [ ] 4.2 Add a read of a unit's lineage.
- [ ] 4.3 Let a page read expand embeds in one batched query, up to 64 embeds and 256 KiB, and say when it stopped at the bound.
- [ ] 4.4 Name in the held list the referenced units that a keep-mine would retire.
- [ ] 4.5 Highest-level check: a memory reference saved before an edit of an unanchored unit resolves to the edited unit; a page with 100 embeds returns 64 with the bound notice in one query.

## 5. Split pass

- [ ] 5.1 Queue a split pass when a commit changes a registry source or the parser version differs from the store's.
- [ ] 5.2 Run the pass one bounded batch under the boundary at a time, with the light rule and review items. Report `splitting` with counts in status.
- [ ] 5.3 Refuse a unit operation on a page that the pass has not reached with `UNITS_BEHIND` and a retry hint.
- [ ] 5.4 Highest-level check: a registry change that adds a category turns matching bullets into units and keeps every other unit id; a change that removes a category retires its units and opens review items only for referenced ones.

## 6. Import

- [ ] 6.1 Import a page that holds units only after its bytes stay unchanged for 2 seconds.
- [ ] 6.2 Name in a review item the unit in the same slot that holds the retired unit's anchor text, and a unit with the same anchor on another page.
- [ ] 6.3 Highest-level check: a scripted editor that saves every 300 ms while it types a sentence into a referenced unit opens no review item, and one that types after the anchor opens one that names the edited unit.

## 7. Embeds

- [ ] 7.1 Write an anchor into a unit when it gains its first embed, in the write that adds the embed.
- [ ] 7.2 Count embeds and unit-level links as references for 2.6 and 2.7.
- [ ] 7.3 Highest-level check: page B embeds a unit of page A; a hand edit of the unit in A shows in B without a write to B; deleting the unit in A opens a review item that names B.

## 8. Generated pages

- [ ] 8.1 Add the definition table: a selector over registry values and page scopes, an order and a layout from closed sets that its schema declares.
- [ ] 8.2 Render a generated page as headings and embeds, up to 500 members, with a bound notice.
- [ ] 8.3 Add the render job on the store thread: render when the member set changes, in bounded batches, with pending renders in status.
- [ ] 8.4 Hold a hand edit of a generated page. Keep-store renders again; keep-mine detaches the definition. Refuse a governed page write with `PAGE_IS_GENERATED`.
- [ ] 8.5 Before this group ships, compare agent outcomes with and without generated pages on tasks that span several pages, by the outcome evaluation's method, case by case with 95% intervals.
- [ ] 8.6 Highest-level check: a generated page of one category gains a member when an agent adds a unit of that category, renders nothing when a member's text changes, and holds a hand edit.

## 9. Parity, rollback and Cloud

- [ ] 9.1 Check the four invariants of design U10 in migration and upgrade verification, in the rollback drills and in a periodic store-thread audit. Report a failure in status and refuse unit operations on that page.
- [ ] 9.2 Assign records in the file-mode migration's load step.
- [ ] 9.3 Add rollback to stage 2: export the unit records, the reference history, the lineage, the generated-page definitions and the open review items beside the vault, drop this change's tables and record `vault-store-v1`.
- [ ] 9.4 Add the same exports to rollback to file mode, and refuse while review items remain open unless the operator exports them.
- [ ] 9.5 Read an identity export in a later migration when its span hashes match the bytes.
- [ ] 9.6 Carry the identity export in a Cloud owner export and read it in an import.
- [ ] 9.7 Highest-level check: on a real-vault copy, store to file mode to store keeps every unit id, and store to stage 2 to this change keeps every byte and every relationship.

## 10. Acceptance

- [ ] 10.1 On a disposable copy of a real vault: run the upgrade pass, the four checks, the replay of 2.8, hard kills inside the match and inside the upgrade and split passes, and both rollbacks.
- [ ] 10.2 Measure the commit's hold of the vault mutation boundary before and after this change on the same instrument, for a page write and for a link rewrite of a hub page.
- [ ] 10.3 Measure page reads and searches before and after on the same instrument; neither may be slower.
- [ ] 10.4 Archive this change only after `make-vault-store-canonical` has archived, so its MODIFIED requirement has a base.
