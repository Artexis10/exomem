## Context

`make-vault-store-canonical` makes one SQLite store canonical for a vault's page bytes. Its stage 2 (D10) adds one store row per semantic unit and stores typed relationships as rows. A unit's reference (`unit_ref`) is its anchor when it carries one, or else a reference bound to a fingerprint of its text. A fingerprint-bound reference changes with every edit of the unit. So when a unit becomes either end of its first stored relationship, stage 2 writes an anchor into it. A stored relationship keys on a store-assigned unit id, and a table maps each anchored unit to that id.

This change extends anchored identity to the other ways that something names a unit. A soft reference that names an unanchored unit gets the unit's anchor, written off the reader's path. Agents change, move and embed a unit by its reference, and the unit carries an anchor afterwards. A page can embed a unit of another page. A reference that the store replaces keeps resolving through an alias.

### Words used here

- **Unit**: a semantic unit as the parser finds it: a one-line compact unit or a rich section block.
- **Unit reference**: today's `unit_ref`, derived from the page bytes: `exomem://memory/<exomem_id>#<anchor>` for an anchored unit, else a fingerprint-bound `#unit-<hex>` reference.
- **Anchor**: the unit's own anchor form, as stage 2 defines it: a block anchor (`^a`) for a compact unit, its `- id:` metadata row for a rich unit.
- **Block id**: an Obsidian block identifier, `^a` at the end of a block. It is what an Obsidian embed names.
- **Unit id**: the id that stage 2 assigns to an anchored unit in its anchor-to-id table. Stored relationships and aliases key on it.
- **Referenced unit**: a unit that a stored relationship (at either end), an embed or a unit-level link names, from its own page or another.
- **Soft reference**: a unit reference that recall state saves: a working-set entry, a memory reference, a correction or a conversation reference. It does not make a unit referenced.
- **Alias**: an owner-table row that maps a unit reference that the store replaced to the unit id of its unit.
- **Anchor request**: an owner-table row that asks the store to anchor a unit that a soft reference named.
- **Embed**: an Obsidian block embed, `![[Page#^a]]`.

### What the data shows

A spike on 2026-11-24 used a copy of a real vault of 17,329 pages. 3,211 pages (18.5%) hold units. 83.5% of units already carry an anchor, because `observe_memory` writes one into every unit that it adds or updates. 0.38% of units carry a relationship. In generated hand edits, text typed after an anchor ended it in 4 of 100 edits.

Task 0.2 then counted soft references on a read-only copy of the same vault, on 2026-11-24, with the product's own parsers:

- **Working-set entries.** 30 days of served-context rows: 5,997 rows with 10,537 slice references, 29,403 stance references, 125 position references and 21 overflow references. 4 references named an unanchored unit, all of them position references to one unit. No slice reference carries a fragment.
- **Memory references in page text.** 5,369, 32 with a fragment, none to an unanchored unit.
- **Corrections.** 8 and 7,875 in the two correction stores, none to an unanchored unit.
- **Conversation references.** Not stored; episode pages hold 3, none to an unanchored unit.
- **Write history.** Compiled-history entries hold 372 unit references, of which 98 are stale today. The semantic writer saved them as part of retained before-images, not a read path.

So a read path saves about one reference a month to a unit without an anchor.

**Owner decision (2026-11-24).** On these counts the owner chose anchors over full unit records. This design is that option.

## Goals / Non-Goals

**Goals**
- A soft reference to an unanchored unit keeps resolving after the unit's text changes.
- An agent changes, moves and embeds a unit by its reference, and the reference survives later edits.
- A page can embed a unit of another page, compact or rich, and Obsidian renders it.
- A semantic write cannot drop a referenced unit by accident.
- Every cross-page answer applies the visibility rules that stages 1 and 2 already use.
- Every addition lives in owner tables, and an earlier build serves the store unchanged.

**Non-Goals**
- No identity for a unit that nothing names beyond today's fingerprint-bound reference.
- No unit records, placements, slot rule, reference history, version log or registry-job unit step. Full records are the rejected option (below).
- No inline copies of one unit's text in several pages (owner decision).
- No generated pages. They wait for an outcome test, in a later change (owner decision).
- No similarity or model-based matching.
- No anchors in file mode. A soft reference saved in file mode stays fingerprint-bound, as today.
- No change to compiled-history entries. Their unit references describe a past before-image, which each entry keeps.

## Decisions

### A1. Identity is the anchor

A unit keeps its identity while it keeps its anchor on its page. Its reference is `exomem://memory/<exomem_id>#<anchor>`, and stage 2's anchor-to-id table gives its unit id. Readers keep resolving references with the existing rule, plus the alias lookup of A3.

A hand split or join keeps the identity on the piece that keeps the anchor; the other piece is a new unit (owner decision). The anchor travels with its text, so this needs no rule of its own.

A page without an `exomem_id` has a path-based page part in its references, as today. An anchor makes the fragment stable, and `backfill-ids` makes the page part stable.

### A2. Which anchors the store itself writes

A caller's governed write may author anchors, as today: `observe_memory` writes one into every unit that it adds or updates, and supersession's audit fix converts items to compact units with new anchors. A unit operation of A4 that names an unanchored unit authors its anchor the same way, in its own write.

The store itself writes an anchor into a unit that carries none in three cases only, each in the unit's own anchor form:

1. The unit becomes either end of its first stored relationship, in the governed write that adds the relationship. This is stage 2's case, unchanged.
2. The unit gains its first embed, in the governed write that adds the embed.
3. A saved soft reference names the unit. This write is deferred, and A3 runs it.

Cases 1 and 2 write into another page and need write admission on it, under stage 2's rule. This change splits stage 2's requirement "Semantic units are first-class store rows" into three requirements, each under 500 characters. They keep its text word for word, including the one-transaction write of page bytes and unit rows, apart from the list of these three cases.

### A3. A saved soft reference anchors its unit, off the reader's path

**The hand-off.** When a reader or a writer saves a soft reference to an unanchored unit on a store-mode page, it hands one request to the store thread: the page's reference, the unit's fingerprint-bound reference and the saving principal. The hand-off goes through a bounded in-memory queue of 256 requests, so the save neither waits nor fails. A full queue drops the request, and a process without a store thread drops it too. Status counts the drops. A dropped request leaves today's behaviour, a fingerprint-bound reference.

**The request.** The store thread records each request in its next transaction, in the anchor-request table, one row per unit and saving principal, with a next-attempt time. A request from a working-set entry expires after 30 days, the working set's retention. A request from a durable reference, a memory reference or a correction, does not expire; it ends when its anchor lands or its unit changes.

**The anchor write.** Between requests, the store thread takes up to 32 requests whose next attempt is due, oldest first. For each one, it parses the page's current bytes.

- When no unit carries that fingerprint-bound reference any more, the unit changed before the anchor landed. The request ends, and the saved reference is stale, as it would be today.
- When no requesting principal may write the page, the request ends. This follows stage 2's rule for a store-written anchor in another page.
- When the file on disk changed within the settle window (`edit_back.SETTLE_SECONDS`), its hash differs from the manifest's, or the page is not projected (`write.Expect.projected`), the request waits. Its next attempt backs off, doubling from the settle window up to 300 s, as stage 1's derivation retries do. A waiting request therefore never holds a slot that a due request needs.
- Otherwise the store commits one governed write that adds only the anchor, binding the page revision and `Expect.projected`. A commit that races an import is refused and retried. The same transaction gives the unit a stage-2 unit id if it has none, records the alias and ends the request.

**What the reference holds.** The saved reference stays the fingerprint-bound one. Until the anchor lands, it resolves as it does today. When the anchor lands, the alias maps that reference to the unit's id. A reference that has an alias resolves through it alone, with one indexed lookup, and never falls back to the exact match. The alias answers with the unit it names, or stale when that unit has since lost its anchor or been retired. It goes first because identical unanchored units are numbered by occurrence: once the first one is anchored, the second takes its old reference. For a caller who may not see the alias's unit, the reference answers stale on a read and `STALE_UNIT_REFERENCE` on a write, as a reference that matches nothing does, so it names no hidden page. Stage 2's table then gives the unit's current page and anchor, so the soft reference keeps resolving through every later edit that keeps the anchor, and through moves.

**Why this is safe.** The saving read never writes, waits or fails. The anchor write is a governed write like any other: it binds what it read, needs write admission, and changes nothing but the anchor. It waits for a quiet file whose bytes match the manifest, so it does not write a file that a person is typing into or has saved without an import. A person's save can still land between the commit and the projection. The projector then holds the item, as it does for any governed write, and keep-mine loses only the anchor. At the measured rate of about one request a month, that costs at most one extra page write a month, and its worst outcome is one held item. A request that never lands leaves today's behaviour. An outside edit that later removes the anchor ends the soft reference, as an edit ends a fingerprint-bound reference today. It opens no review item, because no stored relationship depends on it.

### A4. Agents change a unit by its reference

A unit operation is a page write that names one unit by its reference and leaves it anchored:

- **Update** splices the unit's new bytes at its current span. A promotion of a compact unit to a rich one follows supersession's decision 9: it appends the rich block at the end of the body with the same anchor and removes the compact line. It also keeps the compact line's block id (A5).
- **Insert** adds a unit with a new anchor after a named unit or at the end of a section. Its reply names the new reference.
- **Retire** removes the unit and retires its stored relationships with a reason. Embeds and links stay as text that names nothing, as file mode leaves a link to a deleted page.
- **Move** removes the unit from one page and inserts it in another in one commit. It keeps the anchor, or writes a new one when the destination already uses it. It records an alias from the old reference to the unit id and moves stage 2's anchor-to-id row, so stored relationships and earlier aliases stay attached and no alias is rewritten. It rewrites the unit's inbound embeds and unit-level links in the same commit, as a page move rewrites inbound links, within the mover's scope (A8) and up to the move bound (A10). Beyond the bound it refuses with `UNIT_MOVE_TOO_WIDE`, which names the count and the way forward: embed the unit from the destination instead of moving it. The preflight of A6 treats the moved unit as kept, so it does not refuse the move.
- **Embed** writes an embed of a named unit into a page (A5).

An operation that names an unanchored unit writes its anchor in the same commit, gives it a unit id and records an alias from the fingerprint-bound reference. Its reply names the anchored reference.

A caller's page revision is optional, as supersede's `expected_hash` is. The operation binds the revision that it read itself, so a race refuses and retries. A write follows an alias as a read does. A fingerprint-bound reference that matches nothing and has no alias refuses with `STALE_UNIT_REFERENCE`, supersession's code, and changes nothing. An operation that needs an anchor in a visible page that the caller may not write, such as an embed's home page, refuses with `UNIT_PAGE_NOT_WRITABLE` and names that page.

`observe_memory` add, update and remove use these operations, and so do the stage-2 claim operations. The duplicate fold trashes a copy whose bytes equal a canonical page's. It moves the copy's stored relationships to the canonical page's unit with the same anchor and records an alias for each copy reference.

*Design source.* The primary-source roster row "The model keeps misusing a tool/MCP surface" points to "Writing effective tools for agents": return stable identifiers, and offer one obvious operation. A reference that survives edits, and one operation per intent, follow it.

### A5. A page embeds a unit; the unit keeps one home

A unit lives in one page, its home. Another page shows it with an embed, which Obsidian renders, so the person sees the unit's current text without a second copy of its bytes (owner decision). A hand edit of the unit can happen only at its home. A hand edit of an embed line changes the embedding page and nothing else. An embed is a unit-level link, which stage 2 resolves (its task 5.2), so the graph sees it.

**Rich units.** An Obsidian embed names a block id. A compact unit's anchor already is one. A rich unit's anchor is its `- id:` row, which Obsidian does not read. So a rich unit that carries an `id` gets a block id at exactly one place: the end of the last line of the first paragraph of its body, which the Markdown block parser finds. The parser reads a block id only at that place, and only for such a unit, as the unit's block name rather than as text, in file mode and store mode alike.

- When that paragraph already ends with a block id, the unit reuses it and the store writes none, so a person's `[[Page#^xyz]]` links keep working and now reach the unit.
- Otherwise the first embed writes one in the same commit: the unit's `id` when it is a valid block id (letters, digits and hyphens), else a new one generated as `observe_memory` generates anchors and unique on the page.
- A body with no paragraph, such as one that holds only a table or a code fence, refuses the first embed with `UNIT_EMBED_NO_PARAGRAPH`, which names a unit-level link as the way forward.

The embed names the block id, and the link resolver resolves `#^<block id>` to the unit. Writing the block id keeps the unit's identity, content and fingerprint; its span end and `source_hash` move. A promotion puts the compact line's block id at the same place, so an embed that rendered before a promotion still renders after it. Obsidian shows the paragraph that carries the block id; an expanded page read returns the whole unit.

The read rule changes the parser, so this change bumps the lexical parser version, and the registry pass that the owner-schema bump runs derives every page again under it. A rich unit with an `id` whose first paragraph already ends with a block id loses that token from its derived content then. Task 3.4 counts such units on the real-vault copy, and among them those whose block id equals their own `id`, which is expected to be zero.

**Expansion.** A page read can expand embeds one level deep; an embed inside an embedded unit stays an embed line. It returns each embedded unit's bytes from its home in one batched read, up to 64 embeds and 256 KiB per page, and says when it stopped at the bound.

*Alternative rejected: inline copies.* A hand edit of one copy would either have to change every copy, which writes and projects every page and turns a concurrent edit of any of them into a held correction, or fork the unit. A rebuild in file mode would also see several units with one anchor.

*Alternative rejected: compact units only.* Refusing embeds of rich units would break the owner's decision for every rich claim, and a promotion would silently stop an embed that rendered.

### A6. A semantic write refuses to drop a referenced unit

A semantic write checks its change in its preflight, which already parses each page before and after. A referenced unit whose anchor is gone afterwards, whether the write removed the unit or only its anchor, makes the write refuse with `REFERENCED_UNIT_CHANGED`. The refusal names the unit and the two ways forward: change it with `observe_memory` update, which keeps its anchor, or name it in the page-edit tools' `retire_units` argument. A unit that the write moves (A4) counts as kept. The check reads inbound unit links by stage 1's rule that link reads do not trust rows that lag their page. It parses both versions at the current registry, so a registry change never blames the writer. Its lookup of references is one batched, indexed query per write.

A same-page link counts. Today nothing refuses a write that removes the target of a same-page `supersedes: [[#a]]` edge, which then names nothing. Under this rule such a write refuses unless it names the target in `retire_units`.

This control prevents a detachment that a review item would surface only later, after the agent's context is gone. When it fires wrongly, the agent pays one retry. A soft reference does not make a unit referenced, so it never refuses a write.

### A7. A removed referenced unit keeps its references

Stage 2 keeps the stored relationships of a unit that an edit removes, and opens a review item. This change applies the same rule to embeds and unit-level links, and to an edit that keeps the unit but removes its anchor, such as text typed after it. The review item names the unit, what references it, and the pages that embed it, 50 at a time with their total count. It resolves through the repair tool with one of three choices:

- **Restore the anchor**: write the old anchor back at the end of the unit.
- **Attach**: give a unit on the same page the old anchor, in one page write, so embeds and links resolve again. A unit on another page is a move (A4).
- **Retire**: retire the stored relationships with a reason. Embeds and links stay as text.

The importer looks up the references of a changed file's removed units and anchors with one batched, indexed query per file. It imports a changed file of a page that holds referenced units only after its bytes stay the same for the settle window. An editor saves while a person types, and the window keeps a half-typed state from opening a review item.

### A8. Cross-page answers apply the existing visibility rules

This change adds no visibility rule of its own. Every answer and write that crosses pages applies the rules that stages 1 and 2 already use:

- **Two-ended visibility.** A page that the caller may not see is answered exactly as an absent one and is not counted. An embed whose home is invisible expands to nothing, as a missing unit does. An alias whose unit the caller may not see answers as a stale reference (A3). A reference from an invisible page does not make A6 refuse. If the write drops the unit, A7's review item opens for the owner, and the writer's reply does not mention it. A review item lists and counts only the referring pages that its reader may see, and a reader never sees an item whose referrers are all hidden from that reader.
- **The mover's scope (`move_file.MoverView.scope`).** A unit move rewrites referring pages only within the scope that filters every restricted caller, as the curation link rewrite does. A restricted mover, connector-limited or not, therefore never reads, rewrites or counts a page that it cannot see, and the move bound and `UNIT_MOVE_TOO_WIDE` count only pages in scope. An unrestricted mover's scope is every page. A page outside the scope keeps its old embed or link, which Exomem resolves through the alias.
- **Pages the mover may not write.** A visible referring page that the mover may not write also keeps its old embed or link and the move commits, as a page move keeps a link that it may not rewrite. The reply names those visible pages. A move that leaves any page on the alias opens an owner review item that lists them all, so the owner can rewrite them, and rollback to file mode rewrites them first (A9).

For a link that a move could not rewrite, the link resolver follows the alias when a unit fragment no longer resolves on its page, with one batched lookup per page that it derives.

### A9. Owner tables, compatibility and rollback

This change adds two tables, the anchor requests and the aliases. Each is an owner object under `vault_store_owner_schema`, named in `store_mode_objects`. The aliases are indexed by the replaced reference and by unit id; the requests by unit and by next-attempt time. No collection-schema table gains a column or an index. The anchors themselves are page bytes, and stage 2's anchor-to-id table gains rows, not columns.

This change's build moves the owner-schema version, so the registry pass creates the two tables in its first batch. That pass also derives every page once, in bounded batches with its cursor and the service up, as every owner-schema bump does. There is no other upgrade step.

An earlier build can serve a store that this change has run on. It ignores the two tables, so it follows no alias and anchors no request until this change's build returns. No compatibility id is needed, because an earlier build cannot corrupt either table.

Rollback to file mode drops every owner object, as D8 does. Before it projects, it rewrites every embed and unit-level link that resolves only through an alias to the unit's current reference, so file mode's search and graph answers match the store's, as main's rollback scenario requires. It exports the aliases beside the vault with D8's exports, and a later migration loads them. File mode does not follow aliases, so a soft reference to a unit that the anchor job anchored is stale there, as an edited unit's reference is today.

### A10. Scale

The load target stays the store's: 20,000 pages, about 100,000 units and 120,000 edges, with a hub of 10,000 links.

- **Ordinary writes.** A semantic write adds A6's one batched, indexed reference query, and the importer adds A7's per changed file. Nothing else changes in an ordinary commit.
- **Unit operations.** A unit operation on one page parses that page once under the boundary, as `observe_memory` does. Its budget is a store-mode page write's: task 5.3 measures it against that write on the same instrument and allows no growth beyond the run-to-run spread.
- **Moves.** A unit move's hold must stay under the mutation boundary's 5 s acquire timeout (`mutation_lock`). The move bound is a configured count of referring pages in scope. Stage 1's one loaded run, 7.0 to 8.7 s for a page move with 200 linking pages, suggests about 100, which leaves only about 0.6 s of margin. Task 5.3 therefore measures unit moves on the same instrument and sets the bound from that measurement.
- **Expanded reads.** An expanded page read adds one batched query for up to 64 embeds, budgeted at 50 ms at p95 on the load target and measured by task 5.3.
- **The anchor job.** Up to 32 due requests per store-thread step, each a keyed parse of one page and at most one governed write. Requests and aliases grow with soft references to unanchored units, about one a month on the measured vault.
- **Resolution.** A fingerprint-bound reference adds one indexed alias lookup, and the link resolver adds one batched lookup per derived page only for unit fragments that no longer resolve.
- **Lists.** A review item lists embedding pages 50 at a time. Every list is bounded or paged.

## The rejected option: full unit records

The earlier design gave every unit a store record: a unit id independent of its anchor, placements, a reference history, a version log, a light rule with a slot match, and a unit step in the registry job. It would have kept an identity for every unanchored unit through hand edits and renames, without writing any byte. It cost about 7 to 10.5 weeks and added work to every commit. Task 0.2 found that soft references almost never name an unanchored unit: four in 30 days, all to one unit, and none in page text, corrections or episodes. Agent writes already anchor their units. So records would pay their cost for about one reference a month, and the owner rejected them.

## What this reuses

- From stage 2: the anchor write for a relationship end and its write-admission rule, the anchor-to-id table and store-assigned unit id, unit-level links (task 5.2), the stored relationship table and claim operations (task 5.5), the review item for a removed unit (task 5.6), and two-ended visibility.
- From stage 1: the one commit function with `Expect.projected`, the store thread and its retry backoff, the projector, the importer and held flow, the registry pass that creates owner objects, and the mover's scope that the page move and the curation link rewrite use.
- From `add-unit-level-supersession`: its promotion of a compact unit, `STALE_UNIT_REFERENCE` and the optional revision of a supersede.
- From the existing code: `observe_memory`'s anchor generation, the semantic writer's preflight parses, the edit-back settle window and the reference resolver.

No maintained tool does this. The custom work is the anchor job, the alias lookup, the rich unit's block id and the unit operations around existing writers.

## Estimate

Sizes use the store change's pace, the Records and Planning move's, which landed mostly one PR at a time.

| Package | Size | Weeks | Why |
| --- | --- | ---: | --- |
| Anchor job: the bounded hand-off, the request table with backoff, the quiet-file and hash check, the governed anchor write, aliases by unit id and their resolution, status | S–M | 0.5–0.75 | One store-thread step around the existing commit. |
| Unit operations by reference: update, insert, retire, move and embed through `observe_memory` and the claim operations; anchoring on first operation; the move's rewrites within the mover's scope and its owner review item; the fold | M | 0.5–1 | `observe_memory` already splices units and writes anchors; the page move already rewrites links. |
| Embeds: first-embed anchor, the rich unit's block id in the parser and in promotion, one-level expansion with its bound | S | 0.25–0.5 | Stage 2 resolves unit-level links. |
| Preflight check with `retire_units`; review choices for embeds, links and lost anchors; settle window | S | 0.25–0.5 | The preflight already parses before and after; stage 2 has the review item. |
| Acceptance on a real-vault copy, the latency budgets, rollback export, specs and scaffold guidance | S–M | 0.5–0.75 | Outside-editor races need a real editor and a real-vault copy. |

**Result.** 2 to 3.5 weeks at that pace. Plan for the upper end. Generated pages are a later change after the outcome test.

**What drives the spread.** The writer-side changes to the semantic preflight and the page-edit tools, which couple to every semantic write. The anchor job's races with a person's editor, which only acceptance with a real editor measures. The parser change for a rich unit's block id. The shape in which stage 2 ships its anchor-to-id table (task 0.1).

## Risks / Trade-offs

- **An anchor lands in a file a person has open.** The job waits for a quiet file whose bytes match the manifest, and the race left is the projection latency. Its worst outcome is one held item, which keep-mine resolves by dropping the anchor.
- **An unanchored unit that nothing names keeps a fragile reference.** That is today's behaviour, and the count shows nothing names such units.
- **Pages without an `exomem_id`.** Their references stay path-based across page moves until `backfill-ids` runs.
- **Typing after an anchor.** It ends the anchor in 4 of 100 generated edits. For a referenced unit, a review item offers to restore it; for a soft reference, the reference ends.
- **Embeds at hub scale.** A move rewrites at most the measured bound of referring pages, and a retirement leaves embeds as text.
- **Pages left on an alias.** A restricted move leaves hidden referring pages on the alias, and Obsidian shows their embeds as broken until the owner's review item is resolved.
- **A rich unit's embed shows one paragraph in Obsidian.** The expanded page read returns the whole unit.

## Migration Plan

1. The owner's vault switches to store mode, then stage 2 ships and the vault runs it.
2. This change ships behind its release capability. Its build's registry pass creates the two owner tables and derives every page once.
3. Soft references start anchoring their units, and unit operations and embeds become available.
4. A return to an earlier build needs no command. Rollback to file mode follows A9.

## Owner decisions (2026-11-24)

- Anchors, not full unit records, on task 0.2's counts.
- A shared unit appears in other pages as an embed of its one home (A5), not as copied text.
- Generated pages wait for an outcome test and go to a later change.
- A hand split or join keeps the anchored piece's identity; the other piece is a new unit.
- The order is: the owner's switch to store mode, then stage 2 of `make-vault-store-canonical`, then this change.

## Open Questions

None that blocks the build.

## Design sources

The roster row "The model keeps misusing a tool/MCP surface" ("Writing effective tools for agents") shapes A4. No row of the primary-source roster's pick-by-problem table covers a storage data model. The motivation for embeds is the subject-page result in [The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory), which the store change's D6 cites. That study did not isolate links or embeds.
