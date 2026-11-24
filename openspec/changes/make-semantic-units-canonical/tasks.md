This change starts after the owner's vault switches to store mode and stage 2 of `make-vault-store-canonical` ships. A group waits only on what its heading names. Each group from 1 on ends with its highest-level check.

## 0. Before the build

- [ ] 0.1 Read the shape that stage 2 shipped: the derived unit row set (5.1), unit-level link edges (5.2), the stored relationship table keyed by store-assigned unit id with its anchor-to-id table (5.5), and the review item for an orphaned relationship (5.6). Record any difference from design U11 in the design.
- [ ] 0.2 On a disposable copy of a real vault, count the soft references (working-set entries, memory references, corrections, conversation references) that name a unit without an anchor, and how many of them a read path saved. Record the counts in the design's section on the closest option, and ask the owner to choose between full records and the closest option before group 1 starts.

## 1. Unit records and the upgrade pass

- [ ] 1.1 Add the unit record, placement, reference history and lineage tables, with indexes by unit id, by page and ordinal, and by reference. Declare the states and lineage kinds in the store schema. Add the tables to `VAULT_STORE_TABLES` and `store_mode_objects`, so the collection replica leaves them out and rollback can drop them.
- [ ] 1.2 Record each page's split version, the store's parser version and a records revision per page.
- [ ] 1.3 Add the upgrade pass. It records `vault-store-v2` first, then assigns records to every page that its derivation indexes, in path order, one bounded batch at a time under the derived-row authority, with its cursor in the batch's transaction. Each anchored unit adopts stage 2's unit id. A derivation of a page beyond the cursor assigns that page's records first.
- [ ] 1.4 Keep stage 2's anchor-to-id table in step from each derivation that changes an anchored record.
- [ ] 1.5 Add `exomem store-mode upgrade-units --plan` and `--apply`. The plan prints the pages and units that the pass would assign. The CLI fence declares both.
- [ ] 1.6 Drop history rows of a retired record 180 days after it retires, when nothing names its unit id.
- [ ] 1.7 Highest-level check: on a generated 20,000-page stage-2 store, the plan and the apply agree; a kill at a random batch resumes at the cursor; a stage-2 build started halfway through refuses to serve; every stored relationship stays attached; every derived row is unchanged apart from `unit_id`; and the four checks of 8.1 pass.

## 2. Match in the derivation

- [ ] 2.1 In the derivation, after its existing parse of a page, read the page's live records and unit rows, and match the split by the light rule of design U3, with its 12-character slot bound. Write records with one set-based statement per kind for each batch.
- [ ] 2.2 Match across the pages of one revision by equal bytes or a unique anchor, with lineage `moved_from`.
- [ ] 2.3 Carry a declaration in `PagePut` and on the derivation intent's row. Apply a declaration that fits the split. Keep an intent whose declaration does not fit as failed with `UNIT_DECLARATION_INVALID`. Drop a pending intent's declaration when a later commit coalesces with it.
- [ ] 2.4 Record lineage: `replaces` when exactly one new unit took a retired unit's slot.
- [ ] 2.5 For a retired referenced unit, and for a kept referenced unit that lost its anchor, keep every reference and open one review item through stage 2's review item.
- [ ] 2.6 Refuse a unit operation or a read of unit ids on a page whose intent is pending or failed with `UNITS_BEHIND` and a retry hint.
- [ ] 2.7 Highest-level check: replay the spike's generated hand edits on a real-vault copy through the real importer. The replaced units per 100 edits match the spike's figures for the same anchor state within the run's interval. No unit is kept wrongly by the spike's check. Every retired referenced unit, and every referenced unit that lost its anchor, has a review item. The canonical transaction runs the same number of statements as before this change.

## 3. Writer side

- [ ] 3.1 Add the unit operations of design U5 as page writes with declarations: update, insert, retire, move and embed. Each binds the page revision and the page's records revision.
- [ ] 3.2 Route `observe_memory` add, update and remove, and the stage-2 claim operations, through the unit operations. Let an add mint its unit id, so the reply names it.
- [ ] 3.3 Let a move rewrite the unit's embeds in up to 256 other pages in its commit, and refuse beyond that with `UNIT_MOVE_TOO_WIDE`.
- [ ] 3.4 Let an embed of an unanchored unit write the home page's anchor in the same commit.
- [ ] 3.5 In the semantic writer's preflight, run the light rule over its before and after parses for the pages of the change, read inbound unit links by the stage-1 lag rule, and refuse with `REFERENCED_UNIT_CHANGED` unless the write names the unit in `retire_units`. Add `retire_units` to the page-edit tools.
- [ ] 3.6 Take the head of the agent-facing unit id (`page_ids.part_id`) from the unit id. Resolve an id issued before the upgrade through the reference history.
- [ ] 3.7 Update the skill scaffold so agents change a unit by its id and know `retire_units`.
- [ ] 3.8 Highest-level check: an agent updates an unanchored unit's text through `observe_memory` three times, then moves it to another page. Its unit id and its stored relationships stay the same, and the page bytes equal what the old path would have written. A page edit that would drop a referenced unit refuses and names both ways forward.

## 4. Readers

- [ ] 4.1 Resolve a stored unit reference through the reference history in `semantic_unit_read`, memory references and conversation references.
- [ ] 4.2 Add a read of a unit's lineage.
- [ ] 4.3 Let a page read expand embeds in one batched query, up to 64 embeds and 256 KiB, and say when it stopped at the bound.
- [ ] 4.4 Name in the held list the referenced units that a keep-mine would retire. List a review item's embedding pages 50 at a time with their total count.
- [ ] 4.5 Highest-level check: a memory reference saved before an edit of an unanchored unit resolves to the edited unit; a page with 100 embeds returns 64 with the bound notice in one query.

## 5. Split pass

- [ ] 5.1 Queue a split pass when a commit changes a registry source or the parser version differs from the store's.
- [ ] 5.2 Run the pass on the store thread under the derived-row authority, one bounded batch at a time with its cursor, with the light rule and review items that name the change as their cause. Report `splitting` with counts in status.
- [ ] 5.3 While a pass is pending, keep the prior bytes of each behind page of a commit in one set-based statement, and apply the pass's step to them in that page's derivation before the match.
- [ ] 5.4 Add `exomem store-mode resplit --page PATH`, which splits one page again against its records.
- [ ] 5.5 Highest-level check: a registry change that removes a category, followed by an agent edit of an unrelated unit on a page that the pass has not reached, does not refuse the edit; the review items name the registry change; every other unit keeps its unit id.

## 6. Import, trash and purge

- [ ] 6.1 Import a page that holds units only after its bytes stay unchanged for `edit_back.SETTLE_SECONDS`.
- [ ] 6.2 Name in a review item the unit in the same slot that holds the retired unit's anchor text, and a unit with the same anchor on another page. Add its three repair-tool choices: restore the anchor, attach, retire.
- [ ] 6.3 Set records to `trashed` on a move into the trash and to `live` on recovery. Retire them on a purge or governed deletion, and list the retired relationships in its result.
- [ ] 6.4 Highest-level check: a scripted editor that saves every 300 ms while it types a sentence into a referenced unit opens no review item, and one that types after the anchor opens one that offers to restore it. A trashed and recovered page keeps its unit ids and its relationships.

## 7. Embeds

- [ ] 7.1 Count embeds and unit-level links as references for 2.5 and 3.5.
- [ ] 7.2 Let an attach to a unit on the same page move the old anchor to it in one page write.
- [ ] 7.3 Highest-level check: page B embeds a unit of page A; a hand edit of the unit in A shows in B without a write to B; deleting the unit in A opens a review item that names B; attaching it to another unit of A makes B's embed resolve again.

## 8. Parity, rollback and Cloud

- [ ] 8.1 Check the four invariants of design U10 in migration and upgrade verification and in the rollback drills, and in a store-thread audit that runs in bounded batches with a cursor. Report a failure in status and refuse unit operations on that page until `store-mode resplit` fixes it.
- [ ] 8.2 Assign records in the file-mode migration's load step.
- [ ] 8.3 Add rollback to stage 2: export the unit records with their bytes, the reference history, the lineage and the open review items beside the vault, drop this change's tables, and remove `vault-store-v2` after that commit.
- [ ] 8.4 Add the same exports to rollback to file mode. Refuse while review items remain open unless `--export-review` exports them.
- [ ] 8.5 In a later migration, match an identity export to the bytes by the light rule.
- [ ] 8.6 Carry the identity export in a Cloud owner export and read it in an import.
- [ ] 8.7 Highest-level check: on a real-vault copy, store to file mode, an added line at the top of a page, then store again keeps every unit id; store to stage 2 to this change keeps every byte and every relationship.

## 9. Acceptance

- [ ] 9.1 On a disposable copy of a real vault: run the upgrade pass, the four checks, the replay of 2.7, hard kills during a derivation and inside the upgrade and split passes, a stage-2 build started during the upgrade pass, and both rollbacks.
- [ ] 9.2 Measure the hold of the vault mutation boundary before and after this change on the same instrument, for a page write and for a rename of a hub page with 10,000 linking pages. The hold may not grow.
- [ ] 9.3 Measure the derivation time of the same writes before and after, and page reads and searches before and after; reads and searches may not be slower.
- [ ] 9.4 Archive this change only after `make-vault-store-canonical` has archived, so its MODIFIED requirement has a base.
