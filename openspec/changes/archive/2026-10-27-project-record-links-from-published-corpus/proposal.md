# Project Record Links From The Published Corpus

## Why

A Records or Planning query that returns a bare `[[Title]]` link or an `exomem://memory/` value must decide
whether the reader may see the link's target. To do this, the projector builds a candidate index. Today it
walks every Markdown file in the vault, once per query. That walk costs time in proportion to the vault, and
it breaks the goal that each SQLite-backed read answers within 100 ms at the MCP boundary.

The walk also has a cap, and the cap has a defect. When a vault holds more Markdown files than the cap, the
walk stops and the projector withholds every bare title and every memory reference, including links to pages
that the reader may see. The cap bounds the cost of the walk. It was never meant to decide disclosure.

The server already holds the same data: the published corpus generation keeps a name index and an identity
index for every page.

## What Changes

- The link projector borrows the last published corpus generation. The borrow never builds a corpus and never
  waits for a build. When no generation can be borrowed, the projector runs today's bounded synchronous walk,
  with today's caps.
- Visibility stays per request. The projector removes every page that the reader may not see before it tests
  an index entry for emptiness, uniqueness or ambiguity. The published corpus holds pages for every reader, so
  it is never a per-reader cache.
- A memory reference resolves from the owners of its id in the borrowed generation, with pending identity
  custody applied. When an outside edit that the watcher has seen is not yet published, the projector re-reads
  each visible owner and withholds the reference if that page no longer holds the id.
- `normalize_wikilink` matches a Knowledge-Base-relative target only when that Knowledge Base page exists. The
  stripped-prefix name index also holds the names of pages outside the Knowledge Base. Before, a withheld
  vault-root page could send a restricted writer's `[[Target]]` to a Knowledge Base path that does not exist.
- The reference spelling walk in `filter_withheld_entries` keeps its directory listings as entries and reads a
  leaf's file type from its entry. It builds a casefold map of a directory only when an exact name misses.
  Its decisions do not change.

Visible change: in a vault that holds more Markdown files than the walk cap and that has a published corpus,
bare titles and memory references now resolve for readers who may see their targets. Before, the projector
withheld them. No response shape, tool description or output schema changes.

## Impact

- Affected capability: `structured-collections` (one requirement added).
- Affected code: `record_governance` (link projector), `semantic_contract` (the borrow), `memory_refs` (the
  owner lookup with pending custody), `vault.normalize_wikilink`, `governance/egress` (spelling walk).
- Not changed: the authorization function, final-target release receipts, path-shaped link decisions, the cold
  walk and its caps, and the order of eager projection.
