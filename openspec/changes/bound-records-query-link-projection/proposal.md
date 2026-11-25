# Bound Records Query Link Projection

## Why

A Records or Planning query on a collection with link fields decides whether the reader may see each
link's target. When no published corpus generation could be borrowed, the link projector walked the
vault on the query thread. On a vault with about 1,000 pages per folder that walk took 750 to 1,070 ms
of query CPU. Pages and aggregates then missed the 1 s Records query budget, and some timed out, even
when the query returned no link field.

## What Changes

- A Records or Planning query in the flat query grammar does not walk the vault while a published
  corpus generation is coming. A generation is coming when the corpus cache is on, the watcher keeps
  the vault live, and the borrow found no generation, found one behind the vault, or found its build
  lock held. One function, `semantic_contract.link_corpus_coming`, makes this decision from the borrow's
  decline reason.
- While a generation is coming, a returned link field leaves out its bare titles and memory references.
  The reply names that field in `unresolved_links`, so a value left out never reads as an empty field.
  Planning query replies carry the same key, null when the query could check every returned link field.
- While a generation is coming, a filter, sort, date or aggregate column on a link field, and a saved
  view that filters on one, refuse with the retryable code `LINK_INDEX_WARMING` and `retry_after_ms`.
  Without the index such a query would match no row or count the value as null, with no flag.
- When the borrow found no generation at all, the query starts one background warm that publishes it.
  The query does not wait for the warm, and a later query borrows the published generation.
- When no generation is coming, as with the corpus cache off or in a one-shot process that the watcher
  does not keep live, the query runs the bounded walk as before.
- A query with explicit columns projects only the fields of those columns, so a query that returns no
  link field resolves no link.

Visible change: an agent that reads a Records or Planning query reply can receive `unresolved_links`.
It then knows that the named fields are incomplete. A query that selects on a link field can receive
`LINK_INDEX_WARMING`, which tells it to retry after the wait.

## Impact

- Affected capabilities: `structured-collections` (link projection), `planning` (query reply keys).
- Affected code: `record_governance` (query link projector, refusal and reply), `semantic_contract`
  (the corpus-coming decision, which schedules the warm), `find` (the single-flight warm),
  `query_data` and `record_formats` (column-scoped projection), `planning` (reply key),
  `plan_memory` (the refusal keeps its retry delay), `cli_ops` (the refusal maps to HTTP 503).
- Not changed: governance decides visibility as before. A version-1 query keeps building its link
  index inside its own deadline. Page citations never walk, as before. A borrow that found a
  generation behind the vault or a held build lock starts no warm; the watcher advances that corpus.
