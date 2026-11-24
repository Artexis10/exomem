## Context

`make-vault-store-canonical` makes one SQLite store canonical for a vault's page bytes. Its stage 2 (D10) adds one derived row per semantic unit and stores typed relationships as rows. A unit's reference (`unit_ref`) is its anchor when it carries one, or else a reference bound to a fingerprint of its text. A fingerprint-bound reference changes with every edit of the unit. So when a unit becomes either end of its first stored relationship, stage 2 writes an anchor into it. A stored relationship keys on a store-assigned unit id, and a table maps each anchored unit to that id.

This change extends anchored identity to every other way that something names a unit. A soft reference that names an unanchored unit gets the unit's anchor, written off the reader's path. Agents change, move and embed a unit by its reference, and the unit carries an anchor afterwards. A page can embed a unit of another page. A reference that the store replaces keeps resolving through an alias.

### Words used here

- **Unit**: a semantic unit as the parser finds it: a one-line compact unit or a rich section block.
- **Unit reference**: today's `unit_ref`, derived from the page bytes: `exomem://memory/<exomem_id>#<anchor>` for an anchored unit, else a fingerprint-bound `#unit-<hex>` reference.
- **Anchor**: the unit's own anchor form, as stage 2 defines it: a block anchor for a compact unit, its `id` metadata for a rich unit.
- **Unit id**: the id that stage 2 assigns to an anchored unit in its anchor-to-id table. Stored relationships key on it.
- **Referenced unit**: a unit that a stored relationship (at either end), an embed or a unit-level link in another page names.
- **Soft reference**: a unit reference that recall state saves: a working-set entry, a memory reference, a correction or a conversation reference. It does not make a unit referenced.
- **Alias**: an owner-table row that maps a reference that the store replaced to the unit's current reference.
- **Anchor request**: an owner-table row that asks the store to anchor a unit that a soft reference named.
- **Embed**: an Obsidian block embed, `![[Page#^anchor]]`.

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
- A page can embed a unit of another page.
- A semantic write cannot drop a referenced unit by accident.
- Every addition lives in owner tables. A stage-2 build serves the store unchanged.

**Non-Goals**
- No identity for a unit that nothing references beyond today's fingerprint-bound reference.
- No unit records, placements, slot rule, reference history, version log or registry-job unit step. Full records are the rejected option (below).
- No inline copies of one unit's text in several pages (owner decision).
- No generated pages. They wait for an outcome test, in a later change (owner decision).
- No similarity or model-based matching.
- No anchors in file mode. A soft reference saved in file mode stays fingerprint-bound, as today.
- No change to compiled-history entries. Their unit references describe a past before-image, which each entry keeps.

## Decisions

### A1. Identity is the anchor

A unit keeps its identity while it keeps its anchor on its page. Its reference is `exomem://memory/<exomem_id>#<anchor>`, and stage 2's anchor-to-id table gives its unit id where a stored relationship needs one. Readers keep resolving references with the existing rule, plus one alias lookup (A3).

A hand split or join keeps the identity on the piece that keeps the anchor; the other piece is a new unit (owner decision). The anchor travels with its text, so this needs no rule of its own.

A page without an `exomem_id` has a path-based page part in its references, as today. An anchor makes the fragment stable, and `backfill-ids` makes the page part stable.

### A2. The store writes an anchor only for a unit that is named

The store writes an anchor into a unit that has none in four cases, each in the unit's own anchor form:

1. The unit becomes either end of its first stored relationship. This is stage 2's case, unchanged.
2. The unit gains its first embed. The embedding write writes the anchor in the home page, in the same commit.
3. A unit operation names the unit (A4). The operation's own commit writes the anchor; `observe_memory` already does this.
4. A soft reference names the unit. The anchor job writes it later (A3).

A unit that already carries an anchor keeps it. Each case that writes into another page needs write admission on that page, as stage 2's relationship case does. A unit that nothing names keeps its bytes.

### A3. A saved soft reference anchors its unit, off the reader's path

**The request.** When a reader or a writer saves a soft reference to an unanchored unit on a store-mode page, it hands one request to the store thread: the page's reference, the unit's fingerprint-bound reference and the saving principal. The hand-off is in memory, so the save neither waits nor fails. The store thread records the request in its next transaction, in the anchor-request table, one row per unit. A request lost to a crash before that leaves today's behaviour, a fingerprint-bound reference.

**The anchor write.** Between requests, the store thread takes up to 32 requests per step. For each one, it parses the page's current bytes.

- When no unit carries that fingerprint-bound reference any more, the unit changed before the anchor landed. The request is dropped, and the saved reference is stale, as it would be today.
- When the saving principal may not write the page, the request is dropped. This follows stage 2's rule for a store-written anchor in another page.
- When the file on disk changed within the settle window (`edit_back.SETTLE_SECONDS`), or the page is not projected (`write.Expect.projected`: the manifest records the page's revision, no projection is queued and nothing is held), the request waits for a later step.
- Otherwise the store commits one governed write that adds only the anchor. It binds the page revision and `Expect.projected`, so a commit that races an import is refused and retried. The same transaction records the alias and deletes the request.

A request expires after 30 days, the working set's retention.

**What the reference holds.** The saved reference stays the fingerprint-bound one. Until the anchor lands, it resolves as it does today. The anchor changes the unit's reference, so the anchor write records an alias from the fingerprint-bound reference to the anchored one. Reference resolution follows an alias when the exact match is stale or missing, with one indexed lookup. The soft reference therefore keeps resolving through every later edit that keeps the anchor.

**Why this is safe.** The saving read never writes, waits or fails. The anchor write is a governed write like any other: it binds what it read, needs write admission, and changes nothing but the anchor. It waits for a quiet, projected file, so it does not write a file that a person is typing into. A person's save can still land between the commit and the projection. The projector then holds the item, as it does for any governed write, and keep-mine loses only the anchor. At the measured rate of about one request a month, that costs at most one extra page write a month, and its worst outcome is one held item. A request that never lands leaves today's behaviour. An outside edit that later removes the anchor ends the soft reference, as an edit ends a fingerprint-bound reference today. It opens no review item, because no stored relationship depends on it.

### A4. Agents change a unit by its reference

A unit operation is a page write that names one unit by its reference, binds the page revision, and leaves the unit anchored:

- **Update** splices the unit's new bytes at its current span.
- **Insert** adds a unit with a new anchor after a named unit or at the end of a section. Its reply names the new reference.
- **Retire** removes the unit and retires its stored relationships with a reason. Embeds and links stay as text that names nothing, as file mode leaves a link to a deleted page.
- **Move** removes the unit from one page and inserts it in another in one commit. It keeps the anchor, or writes a new one when the destination already uses it. It moves stage 2's anchor-to-id row, so stored relationships stay attached. It records an alias from the old reference to the new one. It rewrites the unit's embeds in other pages in the same commit, up to 256 embedding pages; beyond that it refuses with `UNIT_MOVE_TOO_WIDE`, which names the count.
- **Embed** writes an embed of a named unit into a page (A5).

An operation that names an unanchored unit writes its anchor in the same commit and records an alias from the fingerprint-bound reference. Its reply names the anchored reference. An operation that must write an anchor into a page that the caller may see but not write refuses with `UNIT_PAGE_NOT_WRITABLE`, and changes nothing. An operation with a stale page revision refuses and names the current one.

`observe_memory` add, update and remove use these operations, and so do the stage-2 claim operations. The duplicate fold trashes a copy whose bytes equal a canonical page's. It moves the copy's stored relationships to the canonical page's unit with the same anchor and records an alias for each copy reference.

When a later move replaces a reference that an alias points at, the move updates that alias too. Every alias therefore names the unit's current reference, and resolution takes one lookup.

*Design source.* The primary-source roster row "The model keeps misusing a tool/MCP surface" points to "Writing effective tools for agents": return stable identifiers, and offer one obvious operation. A reference that survives edits, and one operation per intent, follow it.

### A5. A page embeds a unit; the unit keeps one home

A unit lives in one page, its home. Another page shows it with an embed, which Obsidian renders, so the person sees the unit's current text without a second copy of its bytes (owner decision). A hand edit of the unit can happen only at its home. A hand edit of an embed line changes the embedding page and nothing else. An embed is a unit-level link, which stage 2 resolves (its task 5.2), so the graph sees it.

A page read can expand embeds. It returns each embedded unit's bytes from its home in one batched read, up to 64 embeds and 256 KiB per page, and says when it stopped at the bound.

*Alternative rejected: inline copies.* A hand edit of one copy would either have to change every copy, which writes and projects every page and turns a concurrent edit of any of them into a held correction, or fork the unit. A rebuild in file mode would also see several units with one anchor.

### A6. A semantic write refuses to drop a referenced unit

A semantic write checks its change in its preflight, which already parses each page before and after. A referenced unit whose anchor is gone afterwards, whether the write removed the unit or only its anchor, makes the write refuse with `REFERENCED_UNIT_CHANGED`. The refusal names the unit and the two ways forward: change it with `observe_memory` update, which keeps its anchor, or name it in the page-edit tools' `retire_units` argument. The check reads inbound unit links by stage 1's rule that link reads do not trust rows that lag their page. It parses both versions at the current registry, so a registry change never blames the writer.

This control prevents a detachment that a review item would surface only later, after the agent's context is gone. When it fires wrongly, the agent pays one retry. A soft reference does not make a unit referenced, so it never refuses a write.

### A7. A removed referenced unit keeps its references

Stage 2 keeps the stored relationships of a unit that an edit removes, and opens a review item. This change applies the same rule to embeds and unit-level links, and to an edit that keeps the unit but removes its anchor, such as text typed after it. The review item names the unit, what references it, and the pages that embed it, 50 at a time with their total count. It resolves through the repair tool with one of three choices:

- **Restore the anchor**: write the old anchor back at the end of the unit.
- **Attach**: give a unit on the same page the old anchor, in one page write, so embeds and links resolve again. A unit on another page is a move (A4).
- **Retire**: retire the stored relationships with a reason. Embeds and links stay as text.

The importer imports a changed file of a page that holds referenced units only after its bytes stay the same for the settle window. An editor saves while a person types, and the window keeps a half-typed state from opening a review item.

### A8. Owner tables, compatibility and rollback

This change adds two tables, the anchor requests and the aliases. Each is an owner object under `vault_store_owner_schema`, named in `store_mode_objects`, and created with the other owner objects. No collection-schema table gains a column or an index. The anchors themselves are page bytes, and stage 2's anchor-to-id table gains rows, not columns.

A stage-2 build can serve a store that this change has run on. It ignores the two tables, so it follows no alias and anchors no request until this change's build returns. No compatibility id is needed, because a stage-2 build cannot corrupt either table.

Rollback to file mode drops every owner object, as D8 does. It therefore exports the aliases beside the vault with D8's exports, and a later migration loads them. File mode does not follow aliases, so a soft reference to a unit that the anchor job anchored is stale there, as an edited unit's reference is today.

### A9. Scale

The load target stays the store's: 20,000 pages, about 100,000 units and 120,000 edges, with a hub of 10,000 links. This change adds no work to an ordinary commit.

- The anchor job takes up to 32 requests per store-thread step, each a keyed parse of one page and at most one governed write. Requests and aliases grow with soft references to unanchored units, about one a month on the measured vault. Both tables are indexed by reference.
- Reference resolution adds one indexed alias lookup, only when the exact match is stale or missing.
- A unit operation parses its pages once under the boundary, as `observe_memory` does today.
- An embed expansion reads all its units in one batched query, within its bound.
- A move rewrites at most 256 embedding pages. A review item lists embedding pages 50 at a time.

## The rejected option: full unit records

The earlier design gave every unit a store record: a unit id independent of its anchor, placements, a reference history, a version log, a light rule with a slot match, and a unit step in the registry job. It would have kept an identity for every unanchored unit through hand edits and renames, without writing any byte. It cost about 7 to 10.5 weeks and added work to every commit. Task 0.2 found that soft references almost never name an unanchored unit: four in 30 days, all to one unit, and none in page text, corrections or episodes. Agent writes already anchor their units. So records would pay their cost for about one reference a month, and the owner rejected them.

## What this reuses

- From stage 2: the anchor write for a relationship end and its write-admission rule, the anchor-to-id table and store-assigned unit id, unit-level links (task 5.2), the stored relationship table and claim operations (task 5.5), and the review item for a removed unit (task 5.6).
- From stage 1: the one commit function with `Expect.projected`, the store thread, the projector, the importer and held flow, and the owner-object list.
- From the existing code: `observe_memory`'s anchor generation, the semantic writer's preflight parses, the edit-back settle window and the reference resolver.

No maintained tool does this; the custom work is the anchor job, the alias lookup and the unit operations around existing writers.

## Estimate

Sizes use the store change's pace, the Records and Planning move's, which landed mostly one PR at a time.

| Package | Size | Weeks | Why |
| --- | --- | ---: | --- |
| Anchor job: the hand-off from soft-reference saves, the request table, the quiet-file check, the governed anchor write, aliases and their resolution, status | S–M | 0.5–0.75 | One store-thread step around the existing commit. |
| Unit operations by reference: update, insert, retire, move and embed through `observe_memory` and the claim operations; anchoring on first operation; alias updates; the fold | M | 0.5–1 | `observe_memory` already splices units and writes anchors. |
| Embeds: first-embed anchor, expansion bound, move rewrite bound | S | 0.25–0.5 | Stage 2 resolves unit-level links. |
| Preflight check with `retire_units`; review choices for embeds, links and lost anchors; settle window | S | 0.25–0.5 | The preflight already parses before and after; stage 2 has the review item. |
| Acceptance on a real-vault copy, rollback export, specs and scaffold guidance | S–M | 0.5–0.75 | Outside-editor races need a real editor and a real-vault copy. |

**Result.** 2 to 3.5 weeks at that pace. Plan for the upper end. Generated pages are a later change after the outcome test.

**What drives the spread.** The writer-side changes to the semantic preflight and the page-edit tools, which couple to every semantic write. The anchor job's races with a person's editor, which only acceptance with a real editor measures. The shape in which stage 2 ships its anchor-to-id table (task 0.1).

## Risks / Trade-offs

- **An anchor lands in a file a person has open.** The job waits for a quiet, projected file, and the race left is the projection latency. Its worst outcome is one held item, which keep-mine resolves by dropping the anchor.
- **An unanchored unit that nothing names keeps a fragile reference.** That is today's behaviour, and the count shows nothing names such units.
- **Pages without an `exomem_id`.** Their references stay path-based across page moves until `backfill-ids` runs.
- **Typing after an anchor.** It ends the anchor in 4 of 100 generated edits. For a referenced unit, a review item offers to restore it; for a soft reference, the reference ends.
- **Embeds at hub scale.** A move rewrites at most 256 embedding pages, and a retirement leaves embeds as text.

## Migration Plan

1. The owner's vault switches to store mode, then stage 2 ships and the vault runs it.
2. This change ships behind its release capability. Its build creates the two owner tables with the other owner objects.
3. Soft references start anchoring their units, and unit operations and embeds become available.
4. A return to a stage-2 build needs no command. Rollback to file mode follows A8.

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
