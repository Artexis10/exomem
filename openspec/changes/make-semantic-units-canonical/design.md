## Context

`make-vault-store-canonical` makes one SQLite store canonical for a vault's page bytes. Its stage 2 (D10) adds one derived row per semantic unit and stores typed relationships as rows. In stage 2 the page bytes stay the source of a unit's text and identity. A unit's identity is its reference (`unit_ref`): the authored anchor when the unit carries one, otherwise a reference bound to a fingerprint of its text. A unit that gains its first stored relationship gets an anchor, so its reference survives an edit.

This change makes the store the source of each unit's identity and lifecycle. A page can then embed units from other pages, and a page can be generated from a stored definition. Agents change a unit by naming it. A hand edit in an editor stays allowed and takes a light rule with no similarity matching.

### Words used here

- **Unit**: a semantic unit as the parser finds it: a one-line compact unit or a rich section block.
- **Unit record**: the canonical store row of one unit: its unit id, home page, lifecycle and lineage.
- **Unit id**: the opaque id that the store assigns to a unit record. It does not depend on the text or the page.
- **Unit reference**: today's `unit_ref`, derived from the page bytes. Readers keep using it.
- **Placement**: where a unit sits: its page, ordinal and byte span in the page bytes.
- **Segment**: the page bytes between placements: frontmatter, headings, prose and other text.
- **Split**: the placements that a parse of a page gives. Its **split version** names the parser version and the registry fingerprint it used.
- **Light rule**: exact bytes, then anchor, then slot (U3).
- **Referenced unit**: a unit that a stored relationship, a link or embed in another page, or a generated page names.
- **Declaration**: a writer's statement of which unit id each unit it changed keeps.
- **Embed**: an Obsidian block embed, `![[Page#^anchor]]`.
- **Generated page**: a page whose bytes the store renders from a stored definition.

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
- A unit keeps one unit id through agent edits, light hand edits, page moves and moves between pages.
- An agent changes a unit by naming its unit id, and keeps the id by construction.
- A hand edit takes the light rule. A replaced referenced unit opens a review item. No similarity score decides identity.
- A page can embed units of other pages, and a page can be generated from a stored definition.
- Every derived row stays equal to a cold derivation of the page bytes. Markdown stays a byte-exact projection.
- Rollback to stage 2 and to file mode keeps every byte and exports what only the store holds.

**Non-Goals**
- No change to the unit reference rule, the unit syntax, the relation vocabulary or any derived row.
- No inline copies of one unit's text in several pages (U7).
- No assembly of page bytes at read time (U1).
- No similarity or model-based matching.
- No anchor written into a unit that nothing references (U6).
- No unit-level write binding: a write still binds its page (U5).

## Decisions

### U1. The page body stays the one byte store; unit records index into it

A page that holds units keeps its exact bytes in `store_pages.body`, as stage 1 stores them. Its units are unit records with one placement each. The segments are the body's bytes between placements, kept raw, line endings included. A page with no unit has no records and keeps its bytes whole. That covers 81.5% of pages in the measured vault.

The store assembles a page at write time, not at read time. A unit operation splices the unit's new bytes into the body at its placement and moves the later placements by the length difference in one statement. Every byte reader (`read.StoreSnapshot.page`, `page_sources`, `bodies`, the projector, the derivation, rollback, media bindings and page admission) therefore reads the body unchanged.

*Alternative rejected: segment rows.* A table of ordered segments and units, with or without the body, matches the model word for word. Without the body, every byte reader changes and every page read joins and concatenates rows. With the body, the store keeps two copies of the same bytes that must agree. Neither buys anything while each unit lives in one page (U7).

*Alternative rejected: assemble on read.* It changes about fifteen read sites and adds a join to every page read, whose store-mode latency is already a switch criterion.

### U2. A unit record is the source of a unit's identity and lifecycle

Each unit record holds its unit id, its placement, the hash of its bytes, the revision that created it, its state (`live`, `trashed`, `retired`) and its lineage: `replaces`, `moved_from`, `folded_into` or `retired_by`. A history table maps every unit reference that a unit ever had to its unit id, with the revisions between which it held. Stored relationships, embeds and generated pages name unit ids.

The unit reference stays a derived value. Search, graph, context packs and the other readers that key on it keep working, because their rows still derive from the bytes. A reader that resolves a reference stored earlier, such as a memory reference, a conversation reference or a relationship end, resolves it through the history. The agent-facing unit id (`page_ids.part_id`) takes its head from the unit id, so it stays the same across edits.

### U3. Split before the boundary, match inside the transaction

The light rule matches each new unit of a page to the page's live unit records, in this order:

1. **Exact bytes.** A new unit whose bytes equal an unmatched record's bytes takes its unit id, wherever it moved on the page.
2. **Anchor.** A new unit whose anchor equals the anchor of exactly one unmatched record takes its unit id.
3. **Slot.** A new unit that sits between the same matched neighbours as an unmatched record, and differs from it only in whitespace or in at most one word, takes its unit id. A word is a run of characters without whitespace.

A new unit that none of these match gets a new unit id. An unmatched record retires. When exactly one new unit took its slot, the new unit's lineage says `replaces`.

The writer splits the new bytes before it takes the vault mutation boundary. The split is a pure function of the bytes, the parser version and the registry. It cuts byte spans from the raw bytes, so CRLF and a byte-order mark survive. The commit then matches the split against the records at the base revision inside its transaction. The match touches only the units that changed, so the transaction's statements grow with the changed units, not with the page's units. If the registry or the parser changed between split and commit, the commit splits again under the boundary. That is rare and costs one parse per page of the change.

A writer that knows which units it changed sends a declaration with its bytes. `observe_memory` declares the unit it updated. A link rewrite declares that every unit keeps its place. The commit checks a declaration against the split and refuses a declaration that does not fit with `UNIT_DECLARATION_INVALID`. A valid declaration replaces the light rule for the units it names.

Identity is not derived. A cold derivation has no history to match against, so the records commit with the bytes and are never rebuilt from them, apart from the upgrade pass (U11). Unit records are not the derived identity rows that the store change keeps out of the write transaction: those rows, unit references among them, still follow the derivation intents. The transaction's statements still do not grow with a page's unchanged units, as that requirement asks.

### U4. A hand edit imports through the same commit

The importer keeps its job: a file whose hash differs from the manifest imports as an outside-editor write, or is held. The commit applies the light rule to the imported bytes against the records at the hold's or the manifest's base revision, so a person's edit is matched against the bytes they saw. When the rule retires a referenced unit, the commit keeps everything that references it and opens one review item. The review item names the unit, what references it, and the new unit that took its slot, if one did. When another page holds a unit with the same anchor, the review item names that unit too. Its two choices are: attach the references to a named unit, or retire them.

The watcher imports a page that holds units only once its bytes have stayed the same for 2 seconds, as the collection views' edit-back does. An editor saves while a person types, and a settle window keeps a half-typed unit from replacing the old one.

A keep-mine resolution of a held item takes the same path. The held list names the referenced units that a keep-mine would retire.

### U5. Agents name units

A store change gains unit operations: update a unit, insert a unit after a unit or at the end of a section, retire a unit, and move a unit to another page. Each binds the page revision, as a page write does today, and names the unit id with its version. `observe_memory` and the stage-2 claim operations use them. A move keeps the unit id. It rewrites the embeds of that unit in other pages in the same transaction, up to a bound, and refuses beyond it with a remediation.

A governed write that would retire a referenced unit without saying so refuses with `UNIT_SUCCESSOR_REQUIRED`. The refusal names the unit and the two choices: declare the successor, or retire the references. This control prevents a silent detachment that a review item would surface only later, after the agent's context is gone. When it fires wrongly, the agent pays one retry. A hand edit cannot answer a refusal, so it opens a review item instead (U4).

*Design source.* The primary-source roster row "The model keeps misusing a tool/MCP surface" points to "Writing effective tools for agents": return stable identifiers, and offer one obvious operation. A unit id that survives edits, and one unit operation per intent, follow it.

### U6. An anchor is written only when the store must name a unit

The store writes an anchor into a unit's bytes when the unit has none and gains its first reference: a stored relationship (stage 2), an embed, or a pin by a generated page. The write that adds the reference writes the anchor in the same transaction. A unit that nothing references keeps its bytes. A replaced unit that nothing references costs nothing, so anchoring every unit would rewrite about 17% of units in the measured vault for no gain. Every referenced unit carries an anchor, so the measured rate with an anchor on every unit, 17 replaced units per 100 hand edits, is the rate that matters for review items.

### U7. A page embeds a unit; the unit keeps one home

A unit lives in one page, its home. Another page shows it with an embed. Obsidian renders the embed, so the person sees the unit's current text in every page without a second copy of the bytes. A hand edit of the unit can therefore happen only at its home, where it takes the light rule. A hand edit of an embed line is an edit of a segment of the embedding page: it removes or changes that page's embed and nothing else. Retiring a unit that other pages embed opens a review item that names those pages (U4, U5). An embed is a unit-level link, which stage 2 resolves (its task 5.2), so the graph sees it.

A page read can expand embeds. It returns each embedded unit's bytes from its home, in one batched read, up to 64 embeds and 256 KiB per page, and says when it stopped at the bound.

*Alternative rejected: inline copies.* The text of a shared unit would sit in every page that shows it. A hand edit in one copy must then either change every copy, which writes and projects every page and turns a concurrent edit of any of them into a held correction, or fork the unit. A rebuild in file mode would see several units with one anchor. Both break the byte and rollback guarantees that an embed keeps for free.

### U8. A generated page renders a stored definition

A generated page's definition is a stored row: a selector over unit records and their derived rows, an order and a heading layout. A selector names only registry values (categories, tags, relationship types) and page scopes. The order and the layout come from closed sets that the definition schema declares. The store renders the page as headings and embeds, within a bound of 500 members, and says in the page when it stopped at the bound.

A store-thread job re-renders a generated page when its member set changes. A change of a member's text needs no render, because the embed shows it. The job runs in bounded batches, as the registry job does, and status reports pending renders.

A hand edit of a generated page is held. Keep-store renders the page again. Keep-mine detaches the definition, and the page becomes an ordinary page with the person's bytes. A governed page write to a generated page refuses with `PAGE_IS_GENERATED`, which names the definition operations.

### U9. A registry or parser change splits pages again

The split depends on the registry: a bullet whose category the registry does not know is a segment, not a unit. The store records each page's split version. A commit that changes a registry source, and a release whose parser version differs from the store's, queue a split pass. The pass takes the vault mutation boundary for one bounded batch at a time, as the projector does. It splits each page of the batch again, applies the light rule, and retires or creates records. A retired referenced unit opens a review item. Status reports `splitting` with counts until the pass ends. A unit operation on a page that the pass has not reached refuses with `UNITS_BEHIND` and a retry hint.

### U10. Parity invariants

The store keeps four invariants and checks each one in verification, in the rollback drills and in a periodic store-thread audit:

1. **Placements.** The live placements of a page lie inside its body, in ordinal order, without overlap, and the bytes at each span hash to its record's hash.
2. **Split.** A parse of the body at the page's split version gives exactly the live placements.
3. **References.** Every derived unit row's reference maps to exactly one live unit record.
4. **Derived rows.** The derived rows equal a cold derivation of the bodies, as in stage 1. This change adds a `unit_id` column to derived unit rows and changes no other column.

A failed check is a defect, not a repair trigger. The audit reports it in status with the page path count and the invariant's name, and unit operations on that page refuse until the defect is fixed.

### U11. Migration, compatibility and rollback

An upgrade pass moves a stage-2 store to this change. It creates the tables, then assigns unit records to every page that holds units, in path order, one bounded batch at a time under the boundary, with a cursor that commits with each batch. A commit to a page that the pass has not reached assigns that page's records first. A stage-2 relationship keyed by unit reference moves to the unit id in the same batch. Activation records the compatibility id `vault-store-v2`, so a build without this change refuses to serve the store and names the rollback.

A file-mode migration assigns records in its load step and checks U10 in its verification.

Rollback to stage 2 exports the unit records, the reference history, the lineage, the generated-page definitions and the open review items as files beside the vault, then drops this change's tables and records `vault-store-v1`. The bodies are untouched, so stage 2 serves on. Rollback to file mode adds the same exports to D8's. It refuses while review items remain open, unless the operator exports them. File mode sees embeds as links and generated pages as ordinary pages. A later migration reads an identity export when one is present and its span hashes match the bytes, so unit ids survive a round trip.

### U12. Cloud cells

A Cloud cell holds the same bodies, so per-write projection can stay off and export on request reads the bodies. A cell has no editor, so the importer and its review items do not arise from hand edits. Agent writes take the same commit. An owner export carries the identity export of U11, and an import into another cell reads it.

### U13. Scale

The load target stays the store's: 20,000 pages, about 100,000 units and 120,000 edges, with a hub of 10,000 links.

- A commit's identity work grows with the units it changes. The split runs before the boundary.
- A unit read by id, a placement lookup and a reference lookup are each one indexed query. Records are indexed by unit id, by page and ordinal, and history by reference.
- An embed expansion reads all its units in one batched query, within its bound.
- A generated page renders within its member bound, and its job runs in bounded batches.
- The split pass and the upgrade pass hold the boundary per batch, as the projector does (69 ms per hold in its served test).
- A unit move that would rewrite more embeds than its bound refuses with a remediation, so one write cannot hold the boundary for a hub.

## What stage 2 builds that this change reuses

- The derived unit row set (stage 2 task 5.1). It gains a `unit_id` column and stays derived.
- Unit-level link edges (5.2). Embeds are unit-level links.
- The stored relationship table, its history, two-ended visibility and the one store function for claim operations (5.5).
- The anchor written for a first stored relationship (5.5). U6 extends the same write to embeds and generated pages.
- The relations block, the relations file, the import of relationship edits and the review item for an orphaned relationship (5.6). The commit's light rule replaces the orphan detection; the review item is reused.
- From stage 1: the one commit function, the projector, the importer and held flow, the registry job's batch and cursor pattern, the owner-schema version, verification, rollback exports and the availability rules.

Stage 2 lowers this change's cost if it keys stored relationships by a store-assigned unit id from the start, with a small id table for anchored units. U11 then has nothing to re-key, and the anchor match of U3 already exists.

## Estimate

Sizes use the store change's pace, the Records and Planning move's, which landed mostly one PR at a time. They assume stage 2 ships as D10 plans it.

| Package | Size | Weeks | Why |
| --- | --- | ---: | --- |
| Unit records, placements, history, lineage; upgrade pass; compatibility id; verification of U10; rollback exports | M | 0.5–1 | Reuses the owner-schema pass, the object list and the export path. New: tables, a pass under the boundary. |
| Split before the boundary and match in the commit; declarations; lineage; review-item hook | L | 1–1.5 | The spike fixes the rule and the round trip. Real bytes add CRLF, rich blocks, registry drift and kill points. |
| Unit operations and their callers; agent-facing ids; refusal of an undeclared retirement | M | 0.5–1 | `observe_memory` already addresses units by id and splices bytes; the change moves the splice into the store. |
| Split pass for registry and parser changes | M | 0.5 | Reuses the registry job; adds the boundary per batch. |
| Readers: reference history, unit lineage read, embed expansion, held list | S | 0.5 | One indexed lookup each. |
| Import: settle window, review items from the commit | S | 0.5 | Watcher and commit hooks only. |
| Embeds: anchor on first embed, retirement guard, move rewrite with its bound | S–M | 0.5–1 | Needs stage 2's unit-level links. |
| Generated pages: definitions, render, job, hold, export | M–L | 1–2 | New surface; the job and hold follow existing patterns. |
| Acceptance on a real-vault copy | M | 1–1.5 | Upgrade, parity, replay of the spike's generated edits through the real importer, kills, both rollbacks, commit hold before and after. |
| Contracts, specs, skill scaffold guidance, Cloud identity export | S | 0.5 | |

**Result.** 6.5 to 10 weeks at that pace with generated pages, 5.5 to 8 without them. If stage 2 keys relationships by unit id, subtract about half a week.

**Parallel schedule.** With three workstreams, as the store change planned:

1. Week 1: the tables, the upgrade pass and the specs, which fix the contract that the others share.
2. Weeks 2 and 3: the split and match with the import changes; the unit operations with the readers; the split pass with embeds.
3. Week 4: acceptance on a real-vault copy.

That is about four weeks of elapsed time without generated pages, five at the upper ends. Generated pages add one to two weeks after it. Stage 1 reached its gate-lift release 13 days after its first code. The five rows it covered measure 6 to 7.5 weeks at this pace, so building may run faster. Its acceptance on a real-vault copy was still open 16 days after its first code, so acceptance should not be planned shorter.

**What drives the spread.** Generated pages (0 to 2 weeks). The real-byte edge cases of the match, which the spike's round trip does not cover. The acceptance tail on a real-vault copy. The final shape of stage 2's relationship keys and review item.

## Risks / Trade-offs

- **Two identities for one unit.** The unit id and the unit reference both exist. Readers that only read derived rows keep the reference; readers that hold a reference over time resolve it through the history. The agent sees one id. A reader that compares references across revisions without the history sees a new reference after an edit of an unanchored unit, as it does today.
- **Commit hold.** The match runs under the boundary. The split runs before it, so the added hold is a lookup per changed unit. Acceptance measures the hold before and after on the same instrument.
- **Typing in an editor.** Text typed after an anchor ends the anchor (4 of 100 generated edits). The review item names the unit in the slot that holds the old anchor text, so the fix is one choice. The settle window keeps half-typed states out.
- **Registry drift.** A registry change can turn segments into units or units into segments. The split pass settles it in the background and opens review items only for referenced units.
- **Downgrade.** A build without this change must not write a store that has it. The compatibility id refuses it. Rollback to stage 2 is an explicit command.
- **Generated pages.** A selector that grows with the vault needs its bound. A page at the bound says so.
- **Split and merge by hand.** The light rule keeps the id on the piece that keeps the anchor. The other piece is new. This keeps 10 of 100 generated edits out of review. The design accepts that a split's other half starts a new identity.

## Migration Plan

1. Stage 2 ships and the owner's vault runs it.
2. This change ships behind its release capability. File mode and stage 2 stay the defaults.
3. `exomem store-mode upgrade-units --plan` reports the pages and units that the pass would assign. `--apply` runs the upgrade pass with the service up, in batches, and records `vault-store-v2` when it ends.
4. Verification checks U10 on the whole store before unit operations serve.
5. Rollback to stage 2 or to file mode follows U11.

## Owner decisions (2026-11-24)

- A shared unit appears in other pages as an embed of its one home (U7), not as copied text.
- Generated pages ship only after the outcome evaluation shows that composed pages help agents (task 8.5).
- A hand split or merge keeps the anchored piece's unit id; the other piece is a new unit.
- The order is: the owner's switch to store mode, then stage 2 of `make-vault-store-canonical`, then this change.

## Design sources

The roster row "The model keeps misusing a tool/MCP surface" ("Writing effective tools for agents") shapes U5. No row of the primary-source roster's pick-by-problem table covers a storage data model, so none applies to U1 to U4 and U9 to U11. The motivation for composed pages is the subject-page result in [The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory), which the store change's D6 cites. That study did not isolate links or embeds, so this change measures before it ships generated pages.
