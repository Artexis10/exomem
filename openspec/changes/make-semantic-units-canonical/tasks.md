This change starts after the owner's vault switches to store mode and stage 2 of `make-vault-store-canonical` ships. A group waits only on what its heading names. Each group from 1 on ends with its highest-level check.

## 0. Before the build

- [ ] 0.1 Read the shape that stage 2 shipped: the derived unit row set (5.1), unit-level link edges (5.2), the stored relationship table keyed by store-assigned unit id with its anchor-to-id table (5.5), and the review item for an orphaned relationship (5.6). Record any difference from design U11 in the design.
- [ ] 0.2 On a disposable copy of a real vault, count the soft references (working-set entries, memory references, corrections, conversation references) that name a unit without an anchor, and how many of them a read path saved. Record the counts in the design's section on the closest option, and ask the owner to choose between full records and the closest option before group 1 starts.

## 1. Unit records and the upgrade pass

- [ ] 1.1 Add the unit record, placement, reference history and lineage tables as owner objects under `vault_store_owner_schema`, with indexes by unit id, by page and ordinal, and by reference. Name them in `store_mode_objects`, so the collection replica leaves them out and rollback can drop them. Declare the states and lineage kinds in the store schema. Add no column or index to a collection-schema table.
- [ ] 1.2 Record each page's split version, the store's parser version and a records revision per page.
- [ ] 1.3 Add the upgrade pass. It records `vault-store-v2` first. Then one transaction creates this change's owner tables and settles every stored page's key (2.1), with the bulk rule for duplicates (2.3). The pass then assigns records to every page that its derivation indexes, in path order, one bounded batch at a time under the derived-row authority, with its cursor in the batch's transaction. Each anchored unit adopts stage 2's unit id. The pass skips a page with records or pending versions, whose first pending version assigns its first records by the same rule.
- [ ] 1.4 Keep stage 2's anchor-to-id table in step from each derivation that changes an anchored record.
- [ ] 1.5 Add `exomem store-mode upgrade-units --plan` and `--apply`. The plan prints the pages and units that the pass would assign. The CLI fence declares both.
- [ ] 1.6 Drop history rows of a retired record 180 days after it retires, when nothing names its unit id.
- [ ] 1.7 Highest-level check: on a generated 20,000-page stage-2 store, the plan and the apply agree; a kill at a random batch resumes at the cursor; a stage-2 build started halfway through refuses to serve; every stored relationship stays attached; every derived row is unchanged; the collection schema equals stage 2's; the key step runs as one transaction whose measured hold stays below the vault mutation boundary's 5 s acquire timeout; and the four checks of 8.1 pass.

## 2. Unit derivation

- [ ] 2.1 Add the key table as an owner table (page key, path, stem from the resolver's own path key `vault._wikilink_path_key`, carried `exomem_id` from `memory_refs.normalize_id` passed as a parameter, revision opened, revision closed), with at most one open entry per path and per key, indexed by path, by key, by stem and by carried id. In the commit, apply the five key rules of design U3 with one set-based statement each, closes and moves first. Mint a new key as a UUID that hashes the store's id, the path and the revision. Drop each entry that closed before the oldest pending version. Read no stage-1 table other than `store_pages`.
- [ ] 2.2 Let `backfill-ids`, which stays an operator-run reviewed job, stamp a page's minted key as its `exomem_id` instead of a new value. Never stamp an id from the store on its own.
- [ ] 2.3 In the commit, when a removal closes a key that exactly one open entry records as its page's `exomem_id`, give that page the key, and log its version under that key, naming the replaced key and standing for the removal. Let rule 4 never take a key that a removal of the same commit closed. In the unit derivation, take the replaced key's pending versions into the same batch, apply both keys' older versions first, then match that page's units against the removed key's records by the light rule: the removed key's unit ids win, each replaced record retires with lineage `replaces`, and its stored relationships move to the kept id. Otherwise retire the key's records. On a bulk assignment, give a duplicated id to the path that the identity export names, else to the first path in path order.
- [ ] 2.4 Add the version log as an owner table, keyed by page key and revision and indexed by revision and by path. In the commit, log one pending version per page of the change (key, revision, path, declaration and the time it was logged; a removal marked as one). First copy each changed path's current bytes into its pending version that holds none. Use one set-based statement for each, and reserve room for the bytes as `_reserve_payload_pages` does for puts.
- [ ] 2.5 Refuse a unit operation on a page that keeps 16 or more pending versions with `UNITS_BUSY` and a retry hint. Let every other commit log its versions without refusing, merging or dropping one. Apply at most 16 versions of a page per unit-derivation transaction.
- [ ] 2.6 Select pages for the unit derivation from the version log only, up to 32 pages per transaction by their oldest pending revision. Never read the derivation queue or the audit trail, and never scan stored pages. Run it in its own transaction before the rows transaction, sharing the parse when the rows derivation derives the same current bytes. Read records and versions with one query each, apply each page's versions in revision order, write records with one set-based statement per kind, and delete the applied versions. Advance the units revision in `store_meta`, not the store revision.
- [ ] 2.7 Map each parsed span to raw byte offsets in the split module, so CRLF, a byte-order mark and multi-byte characters keep exact placements. Keep each unit's normalised form and a hash of its raw bytes in its record, not the bytes.
- [ ] 2.8 Match by the light rule of design U3, with its 12-character slot bound and equal units in ordinal order. Compare wikilinks found by `semantic_writes.MOVE_WIKILINK_PATTERN` by their target's page key, keeping fragment and alias, and store each record's unit in that form. Resolve a version's targets with one query over a batch's distinct targets: the key-table entries open at the version's revision whose path or stem the target names, picked by the existing resolver's rule. Compare a target that no entry names by its text. Match no unit across pages.
- [ ] 2.9 Mint each new unit id as the hash of the store's id, the page key, the revision and the unit's ordinal in the first version that holds it.
- [ ] 2.10 Carry a declaration in `PagePut` and on the page's pending version. Apply a declaration that fits its version's split. Fall back to the light rule for one that does not. Record `UNIT_DECLARATION_INVALID` in an owner mismatch table, one row per page for its latest mismatch, cleared by a later fitting declaration, and report it in status. Write nothing to the audit trail.
- [ ] 2.11 When a version does not parse, change no record and skip it. When the current bytes do not parse, mark the page's units `unknown`.
- [ ] 2.12 Set records to `trashed` while their page's path lies under the trash folder (`trash_metadata.is_trash_path`), and apply a removal by 2.3. Record lineage `replaces` when exactly one new unit took a retired unit's slot.
- [ ] 2.13 For a retired referenced unit, and for a kept referenced unit that lost its anchor, keep every reference and open one review item through stage 2's review item.
- [ ] 2.14 Report in status the pages whose units are pending with the oldest age, the pages whose units are `unknown`, and the count of declaration mismatches.
- [ ] 2.15 Highest-level check: replay the spike's generated hand edits on a real-vault copy through the real importer. The replaced units per 100 edits match the spike's figures for the same anchor state within the run's interval. No unit is kept wrongly by the spike's check. Every retired referenced unit, and every referenced unit that lost its anchor, has a review item. The canonical transaction runs the same number of statements as before this change. Then cover the timing cases, each against a derivation after every commit: two declared commits in one request, a `no_room` row failure while identity stays current, the same commits derived in batches of 32 and one page at a time, an invalid declaration, a kill before the unit transaction commits and a kill between the two transactions. Then a rename, a trash and a recovery of a page with a referenced unit, each with the moved page derived before and after its citing pages: every unit id and relationship stays, and a unit that cites the renamed page keeps its id. Then one request that moves A to X, X to Y and B to X keeps both pages' keys; a page without an `exomem_id` moved twice keeps its key; a copied page file gets its own key; and a held rename keeps its unit ids once keep-mine removes the old page. Every key and link-target lookup uses an index, no rule reads the derivation queue or the audit trail, no selection scans the stored pages, and the unit derivation of a hub rename runs in batches of at most 32 pages.

## 3. Writer side

- [ ] 3.1 Add the unit operations of design U5 as page writes with declarations: update, insert, retire, move and embed. Each binds the page revision and the page's records revision. When the page's units are pending, compute them in memory from the records, pending versions and key table, at most 16 parses of that page, and keep the result for the rest of the request.
- [ ] 3.2 Route `observe_memory` add, update and remove, and the stage-2 claim operations, through the unit operations. Let an add's reply name the deterministic unit id of its commit.
- [ ] 3.3 Let a move declare its whole effect on each page's version, and rewrite the unit's embeds in up to 256 other pages in its commit. Refuse beyond that with `UNIT_MOVE_TOO_WIDE`.
- [ ] 3.4 Let an embed of an unanchored unit write the home page's anchor in the same commit.
- [ ] 3.5 Let the duplicate fold declare, on the copy's version, that each unit folds into the unit at the same ordinal of the canonical page, so the unit derivation moves its stored relationships and records `folded_into`.
- [ ] 3.6 In the semantic writer's preflight, run the light rule over its before and after parses for the pages of the change, read inbound unit links by the stage-1 lag rule, and refuse with `REFERENCED_UNIT_CHANGED` unless the write names the unit in `retire_units`. Add `retire_units` to the page-edit tools.
- [ ] 3.7 Refuse a unit operation on a page whose units are `unknown` with `UNITS_UNKNOWN`, naming `exomem store-mode resplit --page`.
- [ ] 3.8 Take the head of the agent-facing unit id (`page_ids.part_id`) from the unit id. Resolve an id issued before the upgrade through the reference history.
- [ ] 3.9 Update the skill scaffold so agents change a unit by its id and know `retire_units`.
- [ ] 3.10 Highest-level check: an agent updates an unanchored unit's text through `observe_memory` three times, then moves it to another page. Its unit id and its stored relationships stay the same, and the page bytes equal what the old path would have written. A rename of a page that the unit cites keeps its unit id. A page edit that would drop a referenced unit refuses and names both ways forward.

## 4. Readers

- [ ] 4.1 Resolve a stored unit reference through the reference history in `semantic_unit_read`, memory references and conversation references.
- [ ] 4.2 Add a read of a unit's lineage.
- [ ] 4.3 Let a page read expand embeds in one batched query, up to 64 embeds and 256 KiB, and say when it stopped at the bound.
- [ ] 4.4 Name in the held list the referenced units that a keep-mine would retire. List a review item's embedding pages 50 at a time with their total count.
- [ ] 4.5 Let a page read with units mark each unit `pending`, with no unit id, while the page's units are pending, and `unknown` when they are unknown. A page read never refuses for this lag. Map a page's units to their records with one query, by reference, or by page key and ordinal for a page without an `exomem_id`.
- [ ] 4.6 Highest-level check: a memory reference saved before an edit of an unanchored unit resolves to the edited unit; a page with 100 embeds returns 64 with the bound notice in one query; a page read during a pending unit derivation returns every unit marked `pending`.

## 5. Unit step of the registry job

- [ ] 5.1 Give the registry job (`rederive`) a unit step that splits each page of its batch again by the light rule, with review items that name the change as their cause. Start the job when the parser version differs from the store's, as an owner-schema bump does.
- [ ] 5.2 When a record retires while its page's split version is behind, name the registry or parser change as its cause, and the write too unless the record's normalised form still stands in the new body, normalised the same way.
- [ ] 5.3 Add `exomem store-mode resplit --page PATH`, which splits one page again against its records.
- [ ] 5.4 Highest-level check: a registry change that removes a category, followed by an agent edit of an unrelated unit on a page that the job has not reached, does not refuse the edit; the review items name the registry change; every other unit keeps its unit id; the audit reports no defect while the job runs.

## 6. Import, trash and removal

- [ ] 6.1 Import a page that holds units only after its bytes stay unchanged for `edit_back.SETTLE_SECONDS`.
- [ ] 6.2 Name in a review item the unit in the same slot that holds the retired unit's anchor text, and a unit with the same anchor on another page. Add its three repair-tool choices: restore the anchor, attach, retire.
- [ ] 6.3 Name in a trash's result the referenced units that it hides, with their relationship counts. Apply a removal by 2.3, with a review item for each retired referenced unit.
- [ ] 6.4 Highest-level check: a scripted editor that saves every 300 ms while it types a sentence into a referenced unit opens no review item, and one that types after the anchor opens one that offers to restore it. A trashed and recovered page keeps its unit ids and its relationships.

## 7. Embeds

- [ ] 7.1 Count embeds and unit-level links as references for 2.13 and 3.6.
- [ ] 7.2 Let an attach to a unit on the same page move the old anchor to it in one page write.
- [ ] 7.3 Highest-level check: page B embeds a unit of page A; a hand edit of the unit in A shows in B without a write to B; deleting the unit in A opens a review item that names B; attaching it to another unit of A makes B's embed resolve again.

## 8. Parity, rollback and Cloud

- [ ] 8.1 Check the four invariants of design U10 in migration and upgrade verification and in the rollback drills, and in a store-thread audit that runs in bounded batches with a cursor and skips the pages that U10 names. Report a failure in status and refuse unit operations on that page until `store-mode resplit` fixes it.
- [ ] 8.2 Settle every page's key in the file-mode migration's load step with the statement of 1.3, keys from each page's `exomem_id`, and assign records there.
- [ ] 8.3 Add rollback to stage 2: export the unit records with their normalised forms, hashes and page keys, the key table's open entries, the reference history, the lineage and the open review items beside the vault; drop this change's owner objects and restore the `store_meta` keys that it wrote; and remove `vault-store-v2` after that commit. Change no collection-schema table.
- [ ] 8.4 Add the same exports to rollback to file mode. Refuse while review items remain open unless `--export-review` exports them.
- [ ] 8.5 In a later migration, and in a later upgrade after a rollback to stage 2, read an identity export: match each page by its page key, the `exomem_id` in its bytes, or by its path when its key was minted; then match its units by the light rule.
- [ ] 8.6 Carry the identity export in a Cloud owner export and read it in an import.
- [ ] 8.7 Highest-level check: on a real-vault copy, store to file mode, an added line at the top of a page, then store again keeps every unit id; store to stage 2 to this change keeps every byte and every relationship, and a stage-2 build opens the store after the rollback to stage 2.

## 9. Acceptance

- [ ] 9.1 On a disposable copy of a real vault: run the upgrade pass, the four checks, the replay of 2.15, hard kills during a unit derivation, the upgrade pass and the registry job's unit step, a stage-2 build started during the upgrade pass, and both rollbacks.
- [ ] 9.2 Measure the hold of the vault mutation boundary before and after this change on the same instrument, for a page write and for a rename of a hub page with 10,000 linking pages, and record what the key rules and the version log add to each. The page write's hold may not grow beyond its run-to-run spread, and the hub rename's by more than 10%. If the hub rename misses, apply the remedy of design U13: rule 3 stops updating the carried id.
- [ ] 9.3 Measure the derivation time of the same writes before and after, and page reads and searches before and after; reads and searches may not be slower.
- [ ] 9.4 Archive this change only after `make-vault-store-canonical` has archived, so its MODIFIED requirement has a base.
