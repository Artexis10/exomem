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
- **Declaration**: a writer's statement of which unit id each unit that it wrote keeps, adds or retires.
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

Each unit record holds its unit id, its placement, the hash of its bytes, the revision that created it, its state and its lineage. The store schema declares the states (`live`, `trashed`, `retired`) and the lineage kinds (`replaces`, `moved_from`, `retired_by`) as closed sets that code implements. A history table maps every unit reference that a unit had to its unit id, with the revisions between which it held. Stored relationships and embeds name unit ids.

The unit reference stays a derived value. Search, graph, context packs and the other readers that key on it keep working, because their rows still derive from the bytes. A reader that resolves a reference stored earlier, such as a soft reference or a relationship end, resolves it through the history. The agent-facing unit id (`page_ids.part_id`) takes its head from the unit id, so it stays the same across edits.

The history keeps its rows while their unit record exists. A retired record and its history rows are dropped 180 days after it retires, when nothing names its unit id. The history grows with edits of unanchored units only, because an anchored unit keeps its reference.

### U3. The store thread matches identity when it derives the page

The canonical commit stays as stage 1 defines it: page bytes, audit rows, projection intents and derivation intents in one transaction. It writes no unit record. A writer's declaration rides on the page's derivation intent, in the row that the commit already writes. The commit's statements therefore do not grow with a page's units, and the stage-1 scenario of a 1-unit and a 400-unit page still runs the same number of statements.

The store thread writes the records when it derives the page. The derivation already parses each page for its unit rows (`semantic_index.current_parent_index_state`), so the split adds no parse. In the same derivation transaction, the store thread reads the page's live records and their unit rows, matches the new split against them, and writes the records with one set-based statement per kind for the batch. The derivation takes whole revisions, up to 32 pages per transaction, without the vault mutation boundary. No split or match therefore runs under the boundary, and no writer that hands bytes to the commit changes. A rename of a hub with 10,000 linking pages costs no more boundary time than in stage 1. Its derivation already parses each linking page in batches.

The light rule matches each new unit to an unmatched live record of the same page, in this order:

1. **Exact bytes.** A new unit whose bytes equal a record's bytes takes its unit id, wherever it moved on the page.
2. **Anchor.** A new unit whose anchor equals the anchor of exactly one record takes its unit id.
3. **Slot.** A new unit that sits between the same matched neighbours as a record takes its unit id when the two differ only in whitespace, or by one inserted, deleted or substituted run of at most 12 characters that holds no whitespace. The character bound covers a one-word change in text that separates words with spaces. It also bounds the rule in text without spaces, such as Chinese or Japanese, where a "word" bound alone would keep any rewrite.

Then, across the pages of one revision, a new unit that nothing matched takes the unit id of an unmatched record from another page of that revision when its bytes are equal, or its anchor names exactly one such record. Its lineage says `moved_from`. A curation fold or a move of a unit between pages therefore needs no declaration.

A new unit that none of these match gets a new unit id. An unmatched record retires. When exactly one new unit took its slot, the new unit's lineage says `replaces`.

A declaration replaces the light rule for the units that it names. It may carry a unit id that the writer minted for a unit it adds, so the writer's reply can name the id before the derivation runs. A derivation refuses a declaration that does not fit the split. It keeps the intent as failed with `UNIT_DECLARATION_INVALID`, and status reports it. A later commit that coalesces with a pending intent drops that intent's declaration, and the match applies the light rule to the later bytes. That happens only when a derivation failed, because the store thread derives each commit's intents before its next request.

Identity cannot be derived from the bytes alone, because a cold derivation has no history to match against. Its inputs are all durable before the derivation runs: the stored bytes, the intent with its declaration, the live records and, when a page is behind its split (U9), the prior bytes. A kill at any point leaves the intent, and the next start derives it with the same result. Rollback exports the records, because nothing can rebuild them (U11).

A unit operation or a page-level read of unit ids on a page whose intent is pending or failed refuses with `UNITS_BEHIND` and a retry hint. A read request waits for derived records as stage 1's reads wait for derived rows.

### U4. A hand edit imports through the same commit

The importer keeps its job: a file whose hash differs from the manifest imports as an outside-editor write, or is held. The derivation matches the imported bytes against the records of the bytes that the person saw, because an import commits only while the store has not moved past the manifest. When the match retires a referenced unit, the store keeps everything that references it and opens one review item. The review item names the unit, what references it, and the new unit that took its slot, if one did. When another page holds a unit with the same anchor, the review item names that unit too.

A referenced unit that the slot rule keeps but whose anchor the edit removed also opens a review item, for example when a person types after the anchor. Its embeds and unit-level links would otherwise point at nothing, and the stage-2 relations file could not name the unit.

A review item resolves through the repair tool, as a held item does, with one of three choices:

- **Restore the anchor**: write the unit's old anchor back at the end of the kept unit.
- **Attach**: give a named unit on the same page the old anchor, and move the stored relationships to its unit id. Embeds and links then resolve again with one page write. A named unit on another page is a move (U5).
- **Retire**: retire the stored relationships with the caller's reason. Embeds and links stay as text that names nothing, as file mode leaves a link to a deleted page.

The watcher imports a page that holds units only after its bytes stay the same for the collection views' edit-back settle window (`edit_back.SETTLE_SECONDS`, 2 seconds). An editor saves while a person types, and the window keeps a half-typed unit from replacing the old one.

A keep-mine resolution of a held item takes the same path. The held list names the referenced units that a keep-mine would retire.

### U5. Agents name units

A unit operation is a page write that a writer builds from the unit's record and sends with a declaration. Each binds the page revision and the page's records revision, and names the unit id with its version:

- **Update** splices the unit's new bytes at its placement and declares that the unit keeps its id.
- **Insert** splices a new unit after a named unit or at the end of a section, and declares a minted id.
- **Retire** removes the unit's bytes and retires its stored relationships with a reason.
- **Move** removes the unit from one page and inserts it in another in one commit, and declares that it keeps its id. It rewrites the unit's embeds in other pages in the same commit, up to 256 embedding pages. Beyond that it refuses with `UNIT_MOVE_TOO_WIDE`, which names the count and the curation plan that moves in steps.
- **Embed** writes an embed of a named unit into a page. When the unit has no anchor, the same commit writes its anchor in the home page.

`observe_memory` add, update and remove use these operations, and so do the stage-2 claim operations.

A semantic write checks referenced units in its preflight, which already parses the page before and after. It runs the light rule over those parses, for all the pages of its change, and reads the inbound unit links through the stage-1 rule that link reads do not trust rows that lag their page. A write that would retire a referenced unit, or remove the anchor of one it keeps, refuses with `REFERENCED_UNIT_CHANGED`. The refusal names the unit and the two ways forward: change the unit with `observe_memory` update, which keeps its id, or name it in the page-edit tools' `retire_units` argument. This control prevents a detachment that a review item would surface only later, after the agent's context is gone. When it fires wrongly, the agent pays one retry. A writer outside the semantic writer, such as a media sidecar or a link rewrite, does not check. A link rewrite keeps each anchor, so it cannot retire a referenced unit. If any such write does, its derivation opens a review item.

*Design source.* The primary-source roster row "The model keeps misusing a tool/MCP surface" points to "Writing effective tools for agents": return stable identifiers, and offer one obvious operation. A unit id that survives edits, and one unit operation per intent, follow it.

### U6. An anchor is written only when the store must name a unit

The store writes an anchor into a unit's bytes when the unit has none and becomes either end of its first stored relationship (stage 2), or gains its first embed. The write that adds the reference writes the anchor in the same commit. A unit that nothing references keeps its bytes, and a soft reference never writes an anchor. Every referenced unit therefore carries an anchor, so the measured rate with an anchor on every unit, 17 replaced units per 100 hand edits, is the rate that matters for review items.

### U7. A page embeds a unit; the unit keeps one home

A unit lives in one page, its home. Another page shows it with an embed. Obsidian renders the embed, so the person sees the unit's current text in every page without a second copy of the bytes. A hand edit of the unit can therefore happen only at its home, where it takes the light rule. A hand edit of an embed line is an edit of a segment of the embedding page: it removes or changes that page's embed and nothing else. An embed is a unit-level link, which stage 2 resolves (its task 5.2), so the graph sees it.

A page read can expand embeds. It returns each embedded unit's bytes from its home, in one batched read, up to 64 embeds and 256 KiB per page, and says when it stopped at the bound. A review item lists the pages that embed its unit 50 at a time, with a count of all of them.

*Alternative rejected: inline copies.* The text of a shared unit would sit in every page that shows it. A hand edit in one copy must then either change every copy, which writes and projects every page and turns a concurrent edit of any of them into a held correction, or fork the unit. A rebuild in file mode would see several units with one anchor. Both break the byte and rollback guarantees that an embed keeps for free.

### U8. Trash, recovery and purge

A trash moves a page into the trash folder. The derivation of that move sets the page's records to `trashed` and keeps their unit ids and stored relationships. A trashed unit is not visible, so a relationship with a trashed end is not served, as two-ended visibility requires. A recovery moves the page back, and its records become `live` with the same unit ids. A purge or a governed deletion retires the records and their stored relationships with lineage `retired_by` and the operation's name. Its result lists the stored relationships that it retired. It opens no review item, because the caller chose it.

### U9. A registry or parser change splits pages again

The split depends on the registry: a bullet whose category the registry does not know is a segment, not a unit. The store records each page's split version. A commit that changes a registry source, and a release whose parser version differs from the store's, queue a split pass. The pass runs on the store thread under the derived-row authority, one bounded batch at a time with its cursor, as the registry job does. It splits each page of the batch again at the new version and applies the light rule. A retired referenced unit opens a review item that names the registry or parser change as its cause. Status reports `splitting` with counts until the pass ends.

A write can reach a page before the pass does. That page's records still reflect the old split version, and a match of the new bytes would retire the units of a removed category as if the writer had removed them. So, while a pass is pending, the commit keeps the prior bytes of each page in its change whose split version is behind, in one set-based statement for the whole change. The derivation of that page first applies the pass's step to the prior bytes, with the review items of the registry change. Then it matches the new bytes. The semantic writer's preflight parses both versions at the current registry, so it never blames the writer either. The upgrade pass follows the same order (U11).

### U10. Parity invariants

The store keeps four invariants:

1. **Placements.** The live placements of a page lie inside its body, in ordinal order, without overlap, and the bytes at each span hash to its record's hash.
2. **Split.** A parse of the body at the page's split version gives exactly the live placements.
3. **References.** Every derived unit row's reference maps to exactly one live unit record.
4. **Derived rows.** The derived rows equal a cold derivation of the bodies, as in stage 1. This change adds a `unit_id` column to derived unit rows and changes no other column.

Verification checks them after a migration and after the upgrade pass, and the rollback drills check them. A store-thread audit checks them in bounded batches with a cursor. A failed check shows in status with the invariant's name and the number of pages. Unit operations on that page refuse, because a splice at a wrong span would corrupt the page. The operator remedy is `exomem store-mode resplit --page PATH`. It splits the page again by the light rule against its records and opens review items for referenced units that retire.

### U11. Migration, compatibility and rollback

The upgrade pass moves a stage-2 store to this change. It first records the compatibility id `vault-store-v2`, so a build without this change refuses to serve the store from the first batch onward and names the rollback. It then creates the tables and assigns unit records to every page that holds units, in path order, one bounded batch at a time with its cursor, under the derived-row authority. Each anchored unit adopts the unit id that stage 2's anchor-to-id table holds, so every stored relationship stays attached. Each other unit gets a new unit id. A derivation of a page that the pass has not reached assigns that page's records first, then matches its new bytes.

The store keeps stage 2's anchor-to-id table in step: each derivation writes the rows of the anchored records it changed. A rollback to stage 2 therefore finds the table current, even after unit moves.

A file-mode migration assigns records in its load step and checks U10 in its verification.

Rollback to stage 2 exports the unit records with their bytes, the reference history, the lineage and the open review items as files beside the vault. It then drops this change's tables, and it removes `vault-store-v2` after that commit. The bodies are untouched, so stage 2 serves on. Rollback to file mode adds the same exports to D8's. Both refuse while review items are open, unless `--export-review` exports them, as `--export-held` does for held items. File mode sees embeds as links.

A later migration that finds an identity export matches it to the file's split by the light rule, with the exported records as the previous records. A line that file mode added at the top of a page therefore moves the placements and keeps every unit id.

### U12. Cloud cells

A Cloud cell holds the same bodies, so per-write projection can stay off and export on request reads the bodies. A cell has no editor, so review items do not arise from hand edits there. Agent writes take the same commit and derivation. An owner export carries the identity export of U11, and an import into another cell reads it.

### U13. Scale

The load target stays the store's: 20,000 pages, about 100,000 units and 120,000 edges, with a hub of 10,000 links.

- The canonical commit runs the same statements as in stage 1, plus one set-based statement while a split pass is pending. The vault mutation boundary holds no split and no match.
- The derivation writes records with one set-based statement per kind for each batch of up to 32 pages.
- A unit read by id, a placement lookup and a reference lookup are each one indexed query. Records are indexed by unit id, by page and ordinal, and history by reference.
- An embed expansion reads all its units in one batched query, within its bound.
- A unit move rewrites at most 256 embedding pages. A review item pages its embedding pages 50 at a time.
- The split pass, the upgrade pass and the audit run in bounded batches with a cursor.

## The closest option: stage 2's anchor table

Stage 2 already gives every referenced unit a stable id through its anchor-to-id table, and U6 anchors every referenced unit. For referenced units, placements, the slot rule, the history and the split pass therefore add nothing that the anchor table lacks.

Full records pay off only for units that carry no anchor:

- **Soft references keep their unit.** A working-set entry, a memory reference, a correction or a conversation reference names a unit by its reference. Today the reference of an unanchored unit changes with every edit, so the soft reference stops resolving. The working set saves such references on recall (`working_set.py`), a read path. The anchor option would have to write an anchor into a page on that read path, which means a page write, a projection and a possible held correction against a file that the person has open. Records keep the reference through the history and write no byte.
- **Agent ids stay put.** An agent's unit id for an unanchored unit changes with each edit today, so the agent must read again before its next change.
- **Lineage.** A unit records what it replaced, where it moved from, and what retired it.

The spike's result files do not count soft references that name unanchored units. Task 0.2 counts them on a real-vault copy before group 1 starts. If few soft references name unanchored units, the owner can choose the anchor option for them instead. That option costs about 2 to 3.5 weeks at the same pace: an anchor written when a soft reference is saved, the unit operations and embeds of U5 to U7 keyed by anchor, and their acceptance. It needs no placements, slot rule, history or split pass.

## What stage 2 builds that this change reuses

- The derived unit row set (stage 2 task 5.1). It gains a `unit_id` column and stays derived.
- Unit-level link edges (5.2). Embeds are unit-level links.
- The stored relationship table keyed by store-assigned unit id, the anchor-to-id table, history, two-ended visibility and the one store function for claim operations (5.5).
- The anchor written for either end of a first stored relationship (5.5). U6 extends the same write to embeds.
- The relations block, the relations file, the import of relationship edits and the review item for an orphaned relationship (5.6). The derivation's light rule replaces the orphan detection; the review item is reused.
- From stage 1: the one commit function, the derivation intents and their store-thread drain, the projector, the importer and held flow, the registry job's batch and cursor pattern, the owner-schema version, verification, rollback exports and the availability rules.

## Estimate

Sizes use the store change's pace, the Records and Planning move's, which landed mostly one PR at a time. Stage 2 keys stored relationships by unit id, so no re-key is planned.

| Package | Size | Weeks | Why |
| --- | --- | ---: | --- |
| Unit records, placements, history, lineage; upgrade pass with the compatibility id first; the stage-2 table in step; verification of U10; rollback exports | M | 0.5–1 | Reuses the owner-schema pass, the object list and the export path. |
| Match in the derivation: light rule with its character bound, cross-page match in a revision, declarations from intents, prior bytes of behind pages, lineage, review items, anchor-loss review | L | 1–1.5 | The spike fixes the rule and the round trip. Real bytes add CRLF, rich blocks, registry drift and kill points. |
| Writer side: declarations on `PagePut` and intents; `observe_memory` and the claim operations; move and embed operations; the preflight check with `retire_units`; agent-facing ids | M | 1–1.5 | `observe_memory` already splices bytes by unit; the semantic preflight already parses before and after. |
| Split pass for registry and parser changes; the resplit command | M | 0.5–1 | Reuses the registry job. |
| Trash, recovery and purge of records | S | 0.25–0.5 | Lifecycle moves already reach the derivation. |
| Readers: reference history, lineage read, embed expansion, held list, paged review list | S | 0.5 | One indexed lookup each. |
| Import: the settle window and the repair-tool choices | S | 0.25–0.5 | Watcher and review hooks only. |
| Embeds: anchor on first embed, attach by anchor, the move bound | S–M | 0.5–1 | Needs stage 2's unit-level links. |
| Acceptance on a real-vault copy | M | 1–1.5 | Upgrade, parity, replay of the spike's generated edits through the real importer, kills, a downgrade during the upgrade pass, both rollbacks, boundary hold before and after. |
| Contracts, specs, skill scaffold guidance, Cloud identity export | S | 0.5 | |

**Result.** 6 to 9.5 weeks at that pace. Plan for the upper end. Generated pages are a follow-up change of 1 to 2 weeks after the outcome evaluation.

**Parallel schedule.** With three workstreams, as the store change planned:

1. Week 1: the tables, the upgrade pass and the specs, which fix the contract that the others share.
2. Weeks 2 and 3: the match in the derivation with trash and import; the writer side with the readers; the split pass with embeds.
3. Weeks 4 and 5: acceptance on a real-vault copy.

Stage 1 reached its gate-lift release 13 days after its first code. The five rows it covered measure 6 to 7.5 weeks at this pace, so building may run faster. Its acceptance on a real-vault copy was still open 16 days after its first code, so acceptance keeps its two weeks.

**What drives the spread.** The real-byte edge cases of the match, which the spike's round trip does not cover. The acceptance tail on a real-vault copy. The writer-side changes of U5, which touch the semantic preflight and the page-edit tools. The count of task 0.2, which can shrink the whole change to the closest option.

## Risks / Trade-offs

- **Two identities for one unit.** The unit id and the unit reference both exist. Readers that only read derived rows keep the reference; readers that hold a reference over time resolve it through the history. The agent sees one id. A reader that compares references across revisions without the history sees a new reference after an edit of an unanchored unit, as it does today.
- **Records lag their bytes.** Records follow the derivation, like every derived row in stage 1. A unit operation on a page that lags refuses with `UNITS_BEHIND`, and a read waits or says what lags. The store thread derives each commit's intents before its next request, so the lag is one derivation.
- **Typing in an editor.** Text typed after an anchor ends the anchor (4 of 100 generated edits). For a referenced unit, a review item offers to restore it. The settle window keeps half-typed states out.
- **Registry drift.** A registry change can turn segments into units or units into segments. The split pass settles it in the background and blames the registry change, not a writer.
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
