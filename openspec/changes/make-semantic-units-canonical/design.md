## Context

`make-vault-store-canonical` makes one SQLite store canonical for a vault's page bytes. Its stage 2 (D10) adds one derived row per semantic unit and stores typed relationships as rows. A stored relationship keys on a store-assigned unit id, and a table maps each anchored unit to its id. In stage 2 the page bytes stay the source of a unit's text. A unit's reference (`unit_ref`) is its anchor, or a reference bound to a fingerprint of its text. When a unit becomes either end of its first stored relationship, it gets an anchor.

This change gives every unit a record with a stable unit id, a lifecycle and a lineage, whether or not it carries an anchor. A page can embed units of other pages. Agents change a unit by naming it. A hand edit in an editor stays allowed and takes a light rule with no similarity matching.

### Words used here

- **Unit**: a semantic unit as the parser finds it: a one-line compact unit or a rich section block.
- **Unit record**: the store row of one unit: its unit id, placement, state and lineage.
- **Unit id**: the opaque id that the store assigns to a unit. It does not depend on the text or the page. For an anchored unit it is the id that stage 2 assigned.
- **Unit reference**: today's `unit_ref`, derived from the page bytes. Readers keep using it.
- **Placement**: where a unit sits: its page, ordinal and byte span in the page bytes.
- **Segment**: the page bytes between placements: frontmatter, headings, prose and other text.
- **Split**: the placements that a parse of a page gives. Its **split version** names the parser version and the registry fingerprint that it used.
- **Light rule**: exact bytes, then anchor, then slot (U3).
- **Referenced unit**: a unit that a stored relationship (at either end), an embed or a unit-level link in another page names. Every referenced unit carries an anchor (U6).
- **Soft reference**: a unit reference that recall state saves, such as a working-set entry, a memory reference, a correction or a conversation reference. It does not make a unit referenced.
- **Declaration**: a writer's statement of which unit id each unit that it wrote keeps, adds, retires, moves or folds.
- **Identity step**: one page version that the identity derivation must walk: its bytes, its revision and its declaration.
- **Embed**: an Obsidian block embed, `![[Page#^anchor]]`.

### What a measurement on a real vault showed

A spike on 2026-11-24 used a copy of a real vault of 17,329 pages.

- The existing parser (`parse_semantic_units`) split every page into units and segments. Every page rebuilt byte for byte. No two units overlapped. The only loss risk was line endings: the page loader turns CRLF into LF before it parses, and 80 pages use CRLF. Segment bytes must therefore stay raw.
- 3,211 pages (18.5%) hold units; 14,118 hold none. Units are 7.6% of all page bytes and 48.1% of the bytes of the pages that hold them.
- 83.5% of units carry an anchor. 0.38% of units carry a relationship.
- A parse of a page that holds units took 0.71 ms at p50, 3.15 ms at p95 and 7.63 ms at p99.
- In 5,978 generated hand edits on real pages, the light rule cut replaced units from 80 to 35 per 100 edits. With an anchor on every unit it cut them to 17. What remains is deletes (8), merges (5) and text typed after an anchor (4). The rule kept no unit wrongly.
- At today's share of relationships, 100 hand edits open about 0.06 review items. If one unit in ten carried a relationship, they would open about 1.7.
- In 3,773 real agent edits, agents replaced 2.74% of the units on the pages they changed. The light rule kept 60% of those.

## Goals / Non-Goals

**Goals**
- A unit keeps one unit id through agent edits, light hand edits, page moves, trash and recovery, and moves between pages.
- An agent changes a unit by naming its unit id, and keeps the id by construction.
- A hand edit takes the light rule. A retired referenced unit opens a review item. No similarity score decides identity.
- A page can embed units of other pages.
- Every derived row stays equal to a cold derivation of the page bytes. Markdown stays a byte-exact projection.
- The canonical write transaction and its hold of the vault mutation boundary stay as stage 1 bounds them.
- Rollback to stage 2 and to file mode keeps every byte and exports what only the store holds.

**Non-Goals**
- No change to the unit reference rule, the unit syntax, the relation vocabulary or any derived row apart from a `unit_id` column.
- No inline copies of one unit's text in several pages (U7).
- No assembly of page bytes at read time (U1).
- No similarity or model-based matching.
- No anchor written into a unit that nothing references (U6).
- No unit-level write binding: a write still binds its page (U5).
- No generated pages. A page rendered from a stored selection of units is a follow-up change that waits for the outcome evaluation (owner decision).

## Decisions

### U1. The page body stays the one byte store; unit records index into it

A page that holds units keeps its exact bytes in `store_pages.body`, as stage 1 stores them. Its units are unit records with one placement each. The segments are the body's bytes between placements, kept raw, line endings included. A page with no unit has no records and keeps its bytes whole. That covers 81.5% of pages in the measured vault.

Records exist only for the pages whose derivation writes unit rows today, by the derivation's corpus rule. Compiled-history images, logs and other pages that the rule leaves out of indexing get no records, so a retained before-image never mints a copy of a unit.

The store assembles a page at write time, not at read time. A unit operation splices the unit's new bytes into the body at its placement. Every byte reader (`read.StoreSnapshot.page`, `page_sources`, `bodies`, the projector, the derivation, rollback, media bindings and page admission) therefore reads the body unchanged.

*Alternative rejected: segment rows.* A table of ordered segments and units, with or without the body, matches the model word for word. Without the body, every byte reader changes and every page read joins and concatenates rows. With the body, the store keeps two copies of the same bytes that must agree. Neither buys anything while each unit lives in one page (U7).

*Alternative rejected: assemble on read.* It changes about fifteen read sites and adds a join to every page read, whose store-mode latency is already a switch criterion.

### U2. A unit record is the source of a unit's identity and lifecycle

Each unit record holds its unit id, its placement, the unit's raw bytes, the revision that created it, its state and its lineage. The store schema declares the states (`live`, `trashed`, `retired`) and the lineage kinds (`replaces`, `moved_from`, `folded_into`, `retired_by`) as closed sets that code implements. A history table maps every unit reference that a unit had to its unit id, with the revisions between which it held. Stored relationships name unit ids. The store resolves an embed, which names an anchor in bytes, to the unit id of the unit that carries that anchor.

A record keeps the unit's raw bytes because the match needs the old unit after the commit has replaced the body. The derived unit rows hold decoded, normalised text, and other jobs rewrite them, so they are not an input to identity.

A new unit id is a hash of the store's id, the page path, the revision and the unit's ordinal in the first page version that holds the unit. The same inputs always give the same id, so a derivation that a kill rolled back mints the same ids when it runs again, and a writer can compute the id of a unit it adds from its commit's revision.

The unit reference stays a derived value. Search, graph, context packs and the other readers that key on it keep working, because their rows still derive from the bytes. A reader that resolves a reference stored earlier, such as a soft reference or a relationship end, resolves it through the history. The agent-facing unit id (`page_ids.part_id`) takes its head from the unit id, so it stays the same across edits.

The history keeps its rows while their unit record exists. A retired record and its history rows are dropped 180 days after it retires, when nothing names its unit id. The history grows with edits of unanchored units only, because an anchored unit keeps its reference.

### U3. The store thread derives identity from every page version, in order

The canonical commit stays as stage 1 defines it: page bytes, audit rows, projection intents and derivation intents in one transaction. It writes no unit record. A writer's declaration rides on the page's derivation intent, in the row that the commit already writes. The commit's statements therefore do not grow with a page's units, and the stage-1 scenario of a 1-unit and a 400-unit page still runs the same number of statements.

**Identity steps.** Stage 1 keeps one derivation intent per path, and a later commit overwrites it. A request on the store thread derives its intents only after it ends, and a failed or `no_room` derivation retries for up to 300 s while later writes keep merging into it. If identity read only the latest bytes, the result would depend on that timing. So the commit keeps every page version that identity needs, in one table of identity steps. For each page of its change that already has a pending intent, the commit copies the page's current bytes and that intent's declaration into a step, before it overwrites them. While the registry job is pending, it does the same for each page whose records are behind (U9). Both copies are one set-based statement for the whole change, so the statement count stays constant. The identity derivation walks a page's steps in revision order, then its current bytes, and deletes the steps it has applied. Its result is then the same as if every commit had been derived on its own, whatever the queue merged.

A page keeps at most 16 steps without a declaration. When a commit would add a 17th, the oldest step without a declaration goes, and its edit merges into the next one; that only makes the light rule coarser. Steps with a declaration are never dropped. They last only until the next identity derivation, which runs before each request and no longer waits for a failed row derivation.

**Its own transaction.** The store thread runs the identity derivation of a batch in its own transaction, before the transaction that derives the batch's rows. For the current bytes it reuses the batch's parse (`semantic_index.current_parent_index_state` in `derive.py`), so it adds no parse there; only a kept step's older bytes need a parse of their own. Because no rule matches across pages, a batch that the drain re-runs one path at a time gives the same identity. It reads the records and steps of the whole batch with one query each, and writes records with one set-based statement per kind. A failed or `no_room` row derivation therefore never holds identity back. Identity never raises for its own reasons:

- A declaration that does not fit its version's split falls back to the light rule for that version. The store records the mismatch as `UNIT_DECLARATION_INVALID` in status and in the audit trail.
- A version whose parse fails (`derive._parse` returns no state) changes no record. Its step is skipped. When the current bytes do not parse, the page's identity is `unknown` until a later version parses. No record retires because of a failed parse.

The derivation takes whole revisions, up to 32 pages per transaction, without the vault mutation boundary. No split or match runs under the boundary, and no writer that hands bytes to the commit changes. A rename of a hub with 10,000 linking pages costs no more boundary time than in stage 1.

**Byte spans.** The parser reads decoded text with CRLF turned into LF. The split module maps each parsed span back to raw byte offsets through the raw bytes, so a placement is exact for CRLF, a byte-order mark and multi-byte characters.

**The light rule** matches each unit of a version to an unmatched live record of the same page, in this order:

1. **Exact bytes.** A unit whose bytes equal a record's bytes takes its unit id, wherever it moved on the page.
2. **Anchor.** A unit whose anchor equals the anchor of exactly one record takes its unit id.
3. **Slot.** A unit that sits between the same matched neighbours as a record takes its unit id when the two differ only in whitespace, or by one inserted, deleted or substituted run of at most 12 characters that holds no whitespace. The character bound covers a one-word change in text that separates words with spaces. It also bounds the rule in text without spaces, such as Chinese or Japanese, where a "word" bound alone would keep any rewrite.

Rules 1 and 3 compare links by the page that they resolve to, not by their text. Each wikilink that the existing move-rewrite pattern finds (`semantic_writes.MOVE_WIKILINK_PATTERN`) stands for the page that the resolver gives it, with its prior-path step for the old version. A rename or a move therefore changes no unit's identity, whether a governed link rewrite or an editor's link update made it. This reuses the existing pattern and resolver and adds no new regex.

A unit that none of these match gets a new unit id. An unmatched record retires. When exactly one new unit took its slot, the new unit's lineage says `replaces`. The rule never matches across pages: a unit moves or folds between pages only by a declaration on each page's intent (U5).

**The lag of identity.** The store thread derives each commit's intents before its next request, so identity usually lags its bytes by one derivation. Three cases make it longer: a request that commits a page more than once, a drain that a lost lease or a handoff stopped, and a store thread that is not running. Row failures no longer lengthen it. Status reports the pages whose identity is pending with the oldest age, the pages whose identity is `unknown`, and the count of declaration mismatches.

**Reads and unit operations during lag.** A page read never refuses for this lag, as stage 1's read contract requires. It serves the page and its units, and marks each unit of a page whose identity is pending as `pending`, with no unit id. A unit operation computes the pending identity in memory from the same durable inputs, the records, steps and current bytes, and the deterministic rule gives the result that the derivation will write. A unit operation on a page whose identity is `unknown` refuses with `UNITS_UNKNOWN`, which names the remedy, `exomem store-mode resplit --page`.

Identity cannot be derived from the bytes alone, because a cold derivation has no history to match against. Its inputs are all durable before the derivation runs, so a kill at any point leaves the same inputs and the same result. Rollback exports the records, because nothing can rebuild them (U11).

### U4. A hand edit imports through the same commit

The importer keeps its job: a file whose hash differs from the manifest imports as an outside-editor write, or is held. The identity derivation matches the imported bytes against the records of the bytes that the person saw, because an import commits only while the store has not moved past the manifest. When an earlier commit's intent was still pending, its step keeps those bytes in order (U3). When the match retires a referenced unit, the store keeps everything that references it and opens one review item. The review item names the unit, what references it, and the new unit that took its slot, if one did. When another page holds a unit with the same anchor, the review item names that unit too.

A referenced unit that the slot rule keeps but whose anchor the edit removed also opens a review item, for example when a person types after the anchor. Its embeds and unit-level links would otherwise point at nothing, and the stage-2 relations file could not name the unit.

A review item resolves through the repair tool, as a held item does, with one of three choices:

- **Restore the anchor**: write the unit's old anchor back at the end of the kept unit.
- **Attach**: give a named unit on the same page the old anchor, and move the stored relationships to its unit id. Embeds and links then resolve again with one page write. A named unit on another page is a move (U5).
- **Retire**: retire the stored relationships with the caller's reason. Embeds and links stay as text that names nothing, as file mode leaves a link to a deleted page.

The watcher imports a page that holds units only after its bytes stay the same for the collection views' edit-back settle window (`edit_back.SETTLE_SECONDS`, 2 seconds). An editor saves while a person types, and the window keeps a half-typed unit from replacing the old one.

A keep-mine resolution of a held item takes the same path. The held list names the referenced units that a keep-mine would retire.

### U5. Agents name units

A unit operation is a page write that a writer builds from the unit's record and sends with a declaration. Each binds the page revision and the page's records revision, and names the unit id with its version. When the page's identity is pending, the writer first computes it in memory from the durable inputs (U3), so the operation never waits for the derivation:

- **Update** splices the unit's new bytes at its placement and declares that the unit keeps its id.
- **Insert** splices a new unit after a named unit or at the end of a section, and declares it new. Its reply names the unit id that the deterministic rule gives for the commit's revision and the unit's ordinal (U2).
- **Retire** removes the unit's bytes and retires its stored relationships with a reason.
- **Move** removes the unit from one page and inserts it in another in one commit. Each page's intent carries a complete declaration: the source says the unit left for the destination, and the destination says the unit arrived with its unit id. Each page's derivation applies its own half, in any order, so a batch that the drain splits path by path gives the same result. The move rewrites the unit's embeds in other pages in the same commit, up to 256 embedding pages. Beyond that it refuses with `UNIT_MOVE_TOO_WIDE`, which names the count and the curation plan that moves in steps.
- **Embed** writes an embed of a named unit into a page. When the unit has no anchor, the same commit writes its anchor in the home page.

`observe_memory` add, update and remove use these operations, and so do the stage-2 claim operations.

A writer that copies units between pages declares it. The duplicate fold is the one such writer today: it trashes a copy whose bytes equal a canonical page's, so it declares on the copy's intent that each unit folds into the unit at the same ordinal of the canonical page. The identity derivation then moves the copy's stored relationships to those units and records `folded_into`. Without a declaration, no rule matches units across pages. An agent that copies text to another page and trashes the source leaves the source's units trashed. The trash's result then names the referenced units that it hides (U8), so the agent can move them.

A semantic write checks referenced units in its preflight, which already parses the page before and after. It runs the light rule over those parses, for all the pages of its change, and reads the inbound unit links through the stage-1 rule that link reads do not trust rows that lag their page. A write that would retire a referenced unit, or remove the anchor of one it keeps, refuses with `REFERENCED_UNIT_CHANGED`. The refusal names the unit and the two ways forward: change the unit with `observe_memory` update, which keeps its id, or name it in the page-edit tools' `retire_units` argument. This control prevents a detachment that a review item would surface only later, after the agent's context is gone. When it fires wrongly, the agent pays one retry. A writer outside the semantic writer, such as a media sidecar or a link rewrite, does not check. A link rewrite keeps each anchor, and the light rule compares links by the page they resolve to (U3), so it retires no unit. If any such write does, its derivation opens a review item.

*Design source.* The primary-source roster row "The model keeps misusing a tool/MCP surface" points to "Writing effective tools for agents": return stable identifiers, and offer one obvious operation. A unit id that survives edits, and one unit operation per intent, follow it.

### U6. An anchor is written only when the store must name a unit

The store writes an anchor into a unit's bytes when the unit has none and becomes either end of its first stored relationship (stage 2), or gains its first embed. The write that adds the reference writes the anchor in the same commit. A unit that nothing references keeps its bytes, and a soft reference never writes an anchor. Every referenced unit therefore carries an anchor, so the measured rate with an anchor on every unit, 17 replaced units per 100 hand edits, is the rate that matters for review items.

### U7. A page embeds a unit; the unit keeps one home

A unit lives in one page, its home. Another page shows it with an embed. Obsidian renders the embed, so the person sees the unit's current text in every page without a second copy of the bytes. A hand edit of the unit can therefore happen only at its home, where it takes the light rule. A hand edit of an embed line is an edit of a segment of the embedding page: it removes or changes that page's embed and nothing else. An embed is a unit-level link, which stage 2 resolves (its task 5.2), so the graph sees it.

A page read can expand embeds. It returns each embedded unit's bytes from its home, in one batched read, up to 64 embeds and 256 KiB per page, and says when it stopped at the bound. A review item lists the pages that embed its unit 50 at a time, with a count of all of them.

*Alternative rejected: inline copies.* The text of a shared unit would sit in every page that shows it. A hand edit in one copy must then either change every copy, which writes and projects every page and turns a concurrent edit of any of them into a held correction, or fork the unit. A rebuild in file mode would see several units with one anchor. Both break the byte and rollback guarantees that an embed keeps for free.

### U8. Trash, recovery and removal

Stage 1 calls a governed delete a trash: it moves a page into the trash folder. The identity derivation of that move sets the page's records to `trashed` and keeps their unit ids and stored relationships. A trashed unit is not visible, so a relationship with a trashed end is not served, as two-ended visibility requires. The trash's result names the referenced units that it hides, with their relationship counts, so hiding them is never silent. A recovery moves the page back, and its records become `live` with the same unit ids.

A removal takes a page out of the store, as a keep-mine of a held deletion does. It retires the page's records with lineage `retired_by`. A retired referenced unit opens a review item, as any retirement from an outside edit does (U4).

### U9. The registry job settles units after a registry or parser change

The split depends on the registry: a bullet whose category the registry does not know is a segment, not a unit. The store records each page's split version: the parser version and the registry fingerprint that its records used. Stage 1's registry job (`rederive`) already derives every page again after a change to the schema overlay, in bounded batches with its cursor, under the derived-row authority. This change gives that job a unit step, and a release whose parser version differs from the store's starts the job the way an owner-schema bump does. There is no second pass. For each page, the unit step splits the page again at the new version and applies the light rule. A retired referenced unit opens a review item that names the registry or parser change as its cause. Status reports the job's `rebuilding` state as stage 1 does, with the count of pages whose units it has not settled.

The rows and the records of a page always come from one registry view, because the identity derivation reuses the parse of the rows derivation in the same batch.

A write can reach a page before the job does. That page's records still reflect the old split version, and a match of the new bytes would retire the units of a removed category as if the writer had removed them. So, while the job is pending, the commit keeps an identity step with the current bytes of each page in its change whose split version is behind (U3). The identity derivation applies the job's step to that version first, with the review items of the registry change, and then matches the new bytes. The semantic writer's preflight parses both versions at the current registry, so it never blames the writer either. The upgrade pass follows the same order (U11).

One case stays imprecise, and this design accepts it. When a page already had a pending intent at the moment the registry changed, its oldest kept version holds that commit's edits too, so the registry change's review items also cover them. The store thread drains before each request, so such a page is rare.

### U10. Parity invariants

The store keeps four invariants:

1. **Placements.** The live placements of a page lie inside its body, in ordinal order, without overlap, and the bytes at each span equal its record's bytes.
2. **Split.** A parse of the body at the page's split version gives exactly the live placements.
3. **References.** Every derived unit row's reference maps to exactly one live unit record.
4. **Derived rows.** The derived rows equal a cold derivation of the bodies, as in stage 1. This change adds a `unit_id` column to derived unit rows and changes no other column. Every comparison of derived rows with file mode sets `unit_id` aside, as it sets aside the columns of write time and write order.

Verification checks them after a migration and after the upgrade pass, and the rollback drills check them. A store-thread audit checks them in bounded batches with a cursor. It skips a page whose identity is pending, and checks the split and references only of pages whose split version is the current one, so the registry job's window reports no false defect. A failed check shows in status with the invariant's name and the number of pages. Unit operations on that page refuse, because a splice at a wrong span would corrupt the page. The operator remedy is `exomem store-mode resplit --page PATH`. It splits the page again by the light rule against its records and opens review items for referenced units that retire.

### U11. Migration, compatibility and rollback

The upgrade pass moves a stage-2 store to this change. It first records the compatibility id `vault-store-v2`, so a build without this change refuses to serve the store from the first batch onward and names the rollback. It then creates the tables and assigns unit records to every page that holds units, in path order, one bounded batch at a time with its cursor, under the derived-row authority. Each anchored unit adopts the unit id that stage 2's anchor-to-id table holds, so every stored relationship stays attached. Each other unit gets the deterministic unit id of U2 for the page's revision, so a pass that a kill interrupted assigns the same ids again. A derivation of a page that the pass has not reached assigns that page's records first, then matches its new bytes.

The store keeps stage 2's anchor-to-id table in step: each derivation writes the rows of the anchored records it changed. A rollback to stage 2 therefore finds the table current, even after unit moves.

A file-mode migration assigns records in its load step and checks U10 in its verification.

Rollback to stage 2 exports the unit records with their bytes, the reference history, the lineage and the open review items as files beside the vault. It then drops this change's tables, and it removes `vault-store-v2` after that commit. The bodies are untouched, so stage 2 serves on. Rollback to file mode adds the same exports to D8's. Both refuse while review items are open, unless `--export-review` exports them, as `--export-held` does for held items. File mode sees embeds as links.

A later migration that finds an identity export matches it to the file's split by the light rule, with the exported records as the previous records. A line that file mode added at the top of a page therefore moves the placements and keeps every unit id.

### U12. Cloud cells

A Cloud cell holds the same bodies, so per-write projection can stay off and export on request reads the bodies. A cell has no editor, so review items do not arise from hand edits there. Agent writes take the same commit and derivation. An owner export carries the identity export of U11, and an import into another cell reads it.

### U13. Scale

The load target stays the store's: 20,000 pages, about 100,000 units and 120,000 edges, with a hub of 10,000 links.

- The canonical commit runs the same statements as in stage 1, plus one set-based statement that keeps identity steps. The vault mutation boundary holds no split and no match.
- An identity step copies a page's bytes only when that page already has a pending intent, or while the registry job is pending and the page is behind. A hub rename during the job can copy up to 10,000 bodies in one statement, which doubles that commit's byte writes once. Each step goes when its page's identity derivation applies it.
- A page keeps at most 16 steps without a declaration.
- The identity derivation reads a batch's records and steps with one query each and writes with one set-based statement per kind, for batches of up to 32 pages.
- A unit read by id, a placement lookup and a reference lookup are each one indexed query. Records are indexed by unit id, by page and ordinal, and history by reference.
- An embed expansion reads all its units in one batched query, within its bound.
- A unit move rewrites at most 256 embedding pages. A review item pages its embedding pages 50 at a time.
- The upgrade pass, the registry job's unit step and the audit run in bounded batches with a cursor.

## The closest option: stage 2's anchor table

Stage 2 already gives every referenced unit a stable id through its anchor-to-id table, and U6 anchors every referenced unit. For referenced units, placements, the slot rule, the history, the identity steps and the registry job's unit step therefore add nothing that the anchor table lacks.

Full records pay off only for units that carry no anchor:

- **Soft references keep their unit.** A working-set entry, a memory reference, a correction or a conversation reference names a unit by its reference. Today the reference of an unanchored unit changes with every edit, and with every rename of a page that the unit links to, so the soft reference stops resolving. The working set saves such references on recall (`working_set.py`), a read path. The anchor option would have to write an anchor into a page on that read path, which means a page write, a projection and a possible held correction against a file that the person has open. Records keep the reference through the history and write no byte.
- **Agent ids stay put.** An agent's unit id for an unanchored unit changes with each edit today, so the agent must read again before its next change.
- **Lineage.** A unit records what it replaced, where it moved from, and what retired it.

The spike's result files do not count soft references that name unanchored units. Task 0.2 counts them on a real-vault copy before group 1 starts. If few soft references name unanchored units, the owner can choose the anchor option for them instead. That option costs about 2 to 3.5 weeks at the same pace: an anchor written when a soft reference is saved, the unit operations and embeds of U5 to U7 keyed by anchor, and their acceptance. It needs no placements, slot rule, history, identity steps or unit step in the registry job.

## What stage 2 builds that this change reuses

- The derived unit row set (stage 2 task 5.1). It gains a `unit_id` column and stays derived.
- Unit-level link edges (5.2). Embeds are unit-level links.
- The stored relationship table keyed by store-assigned unit id, the anchor-to-id table, history, two-ended visibility and the one store function for claim operations (5.5).
- The anchor written for either end of a first stored relationship (5.5). U6 extends the same write to embeds.
- The relations block, the relations file, the import of relationship edits and the review item for an orphaned relationship (5.6). The identity derivation's light rule replaces the orphan detection; the review item is reused.
- From stage 1: the one commit function, the derivation intents and their store-thread drain, the projector, the importer and held flow, the registry job with its batches and cursor, the owner-schema version, verification, rollback exports and the availability rules.
- From the existing code: the parse of the rows derivation, the move-rewrite wikilink pattern and the resolver with its prior-path step.

## Estimate

Sizes use the store change's pace, the Records and Planning move's, which landed mostly one PR at a time. Stage 2 keys stored relationships by unit id, so no re-key is planned.

| Package | Size | Weeks | Why |
| --- | --- | ---: | --- |
| Unit records, placements, history, lineage; upgrade pass with the compatibility id first; the stage-2 table in step; verification of U10; rollback exports | M | 0.5–1 | Reuses the owner-schema pass, the object list and the export path. |
| Identity derivation: identity steps and their bound, its own transaction before the rows, the light rule with its character bound and link comparison, deterministic ids, declarations with fallback, parse failure as `unknown`, raw byte spans, lineage, review items, anchor-loss review | L | 1.5–2 | The spike fixes the rule and the round trip. Real bytes add CRLF, rich blocks, registry drift, queue merging and kill points. |
| Writer side: declarations on `PagePut` and intents; `observe_memory` and the claim operations; move, embed and fold declarations; in-memory identity for unit operations; the preflight check with `retire_units`; agent-facing ids | M | 1–1.5 | `observe_memory` already splices bytes by unit; the semantic preflight already parses before and after. |
| Unit step of the registry job for registry and parser changes; the resplit command | M | 0.5–1 | Reuses the registry job. |
| Trash, recovery and removal of records; the trash's list of hidden units | S | 0.25–0.5 | Lifecycle moves already reach the derivation. |
| Readers: reference history, lineage read, `pending` units in a page read, embed expansion, held list, paged review list | S | 0.5 | One indexed lookup each. |
| Import: the settle window and the repair-tool choices | S | 0.25–0.5 | Watcher and review hooks only. |
| Embeds: anchor on first embed, attach by anchor, the move bound | S–M | 0.5–1 | Needs stage 2's unit-level links. |
| Acceptance on a real-vault copy | M | 1–1.5 | Upgrade, parity, replay of the spike's generated edits through the real importer, merged and failed derivations, kills, a downgrade during the upgrade pass, both rollbacks, boundary hold before and after. |
| Contracts, specs, skill scaffold guidance, Cloud identity export | S | 0.5 | |

**Result.** 6.5 to 10 weeks at that pace. Plan for the upper end. Generated pages are a follow-up change of 1 to 2 weeks after the outcome evaluation.

**Parallel schedule.** With three workstreams, as the store change planned:

1. Week 1: the tables, the upgrade pass and the specs, which fix the contract that the others share.
2. Weeks 2 and 3: the identity derivation with trash and import; the writer side with the readers; the registry job's unit step with embeds.
3. Weeks 4 and 5: acceptance on a real-vault copy.

Stage 1 reached its gate-lift release 13 days after its first code. The five rows it covered measure 6 to 7.5 weeks at this pace, so building may run faster. Its acceptance on a real-vault copy was still open 16 days after its first code, so acceptance keeps its two weeks.

**What drives the spread.** The real-byte edge cases of the match and the order of identity steps under merged and failed derivations, which the spike's round trip does not cover. The acceptance tail on a real-vault copy. The writer-side changes of U5, which touch the semantic preflight and the page-edit tools. The count of task 0.2, which can shrink the whole change to the closest option.

## Risks / Trade-offs

- **Two identities for one unit.** The unit id and the unit reference both exist. Readers that only read derived rows keep the reference; readers that hold a reference over time resolve it through the history. The agent sees one id. A reader that compares references across revisions without the history sees a new reference after an edit of an unanchored unit, as it does today.
- **Records lag their bytes.** Records follow the identity derivation, which usually runs before the next request. A page read during the lag marks the page's units `pending`, and a unit operation computes the pending identity in memory. Identity steps keep the result independent of how the queue merges intents.
- **Typing in an editor.** Text typed after an anchor ends the anchor (4 of 100 generated edits). For a referenced unit, a review item offers to restore it. The settle window keeps half-typed states out.
- **Registry drift.** A registry change can turn segments into units or units into segments. The registry job's unit step settles it in the background and blames the registry change, not a writer.
- **Copies between pages.** No rule matches units across pages. A writer that moves or folds units declares it. An agent that copies text and trashes the source sees the hidden referenced units in the trash's result.
- **Downgrade.** A build without this change must not write a store that has it. The compatibility id refuses it from the upgrade pass's first batch. Rollback to stage 2 is an explicit command.
- **Embeds at hub scale.** A move rewrites at most 256 embedding pages, and a retirement leaves embeds as text. Attaching to a unit on the same page needs one page write.
- **Split and merge by hand.** The light rule keeps the id on the piece that keeps the anchor. The other piece is new (owner decision). This keeps 10 of 100 generated edits out of review.

## Migration Plan

1. The owner's vault switches to store mode, then stage 2 ships and the vault runs it.
2. This change ships behind its release capability. File mode and stage 2 stay the defaults.
3. `exomem store-mode upgrade-units --plan` reports the pages and units that the pass would assign. `--apply` records `vault-store-v2`, then runs the upgrade pass with the service up, in batches.
4. Verification checks U10 on the whole store before unit operations serve.
5. Rollback to stage 2 or to file mode follows U11.

## Owner decisions (2026-11-24)

- A shared unit appears in other pages as an embed of its one home (U7), not as copied text.
- Generated pages wait for the outcome evaluation and go to a follow-up change.
- A hand split or merge keeps the anchored piece's unit id; the other piece is a new unit.
- The order is: the owner's switch to store mode, then stage 2 of `make-vault-store-canonical`, then this change.

## Open Questions

- Should soft references to unanchored units keep their unit through full records, or through anchors written when they are saved (the closest option)? Task 0.2 measures the count that decides it.

## Design sources

The roster row "The model keeps misusing a tool/MCP surface" ("Writing effective tools for agents") shapes U5. No row of the primary-source roster's pick-by-problem table covers a storage data model, so none applies to U1 to U4 and U8 to U11. The motivation for embeds is the subject-page result in [The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory), which the store change's D6 cites. That study did not isolate links or embeds.
