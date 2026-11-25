## Why

Agents name people, organisations, products and places in the notes they write, and many of those things already have an Entity page. A read-only census of a working vault on 2026-11-25 sampled 18 notes that agents wrote in ordinary use over ten days. The notes named things that had an Entity page 58 times and linked 30 of them; 11 of the 18 notes left at least one such name as plain text. An unlinked name is no connection: the graph, recall and the Entity's inbound links do not see it, in file mode or store mode, because both derive links from the page's bytes.

The write receipt already helps with the opposite case. `undeclared_referents` names a wikilinked name that has no page. Nothing names an existing Entity that a note mentions without a link, because that block reads wikilink markup only.

## What Changes

- A committed durable page write carries an `unlinked_mentions` block when its new text names an active Entity, visible to the writer, by its exact title or alias without linking it.
- The block lists at most 5 Entities, each with the name as written and the Entity's link target, and one ready `edit_note` call that turns the listed names into links. The agent decides which to keep; the server links nothing by itself.
- A name that matches two or more Entities, the page's own Entity, a name the page already links, and text inside code, links and frontmatter are never offered.
- The block uses the same gates, dismissal and batch rules as `undeclared_referents`, reads the Entity registry entries of the write's preflight corpus, and walks no vault.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `write-time-identity-candidates`: add the `unlinked_mentions` block.

## Impact

- Code: `capture_sweep.py` (a name index over `entity_registry` and the block), the post-batch carrier in `commands.py`, the compact response projection, and one scaffold line.
- Every durable page write pays one pass over the writer's Entity names to index them, and one dictionary lookup per word run of its new text. The registry it reads is already built for the write's other advisories.
- No new tool, argument or stored state beyond the existing write-advisory dismissal records.
