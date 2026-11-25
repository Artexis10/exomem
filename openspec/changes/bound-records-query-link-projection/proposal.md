# Bound Records Query Link Projection

## Why

A Records or Planning query on a collection with link fields decides whether the reader may see each
link's target. When no published corpus generation could be borrowed, the link projector walked the
vault on the query thread. On a vault with about 1,000 pages per folder that walk took 750 to 1,070 ms
of query CPU. Pages and aggregates then missed the 1 s Records query budget, and some timed out, even
when the query returned no link field.

## What Changes

- A query borrows the published corpus generation or answers without it. A query never walks the
  vault for its link index.
- Without a borrowed generation, a returned link field leaves out its bare titles and memory
  references. The reply names that field in `unresolved_links`, so a value left out never reads as an
  empty field. Planning query replies carry the same key, null when the query could check every returned link
  field.
- When no corpus generation exists at all, the query starts one background warm that publishes it.
  The query does not wait for the warm, and a later query borrows the published generation.
- A query with explicit columns projects only the fields of those columns, so a query that returns no
  link field resolves no link.

Visible change: an agent that reads a Records or Planning query reply can receive `unresolved_links`.
It then knows that the named fields are incomplete, and it can query again after the corpus is
published.

## Impact

- Affected capabilities: `structured-collections` (link projection), `planning` (query reply keys).
- Affected code: `record_governance` (query link projector and reply), `semantic_contract` (the borrow
  schedules the warm), `find` (the single-flight warm), `query_data` and `record_formats` (column-scoped
  projection), `planning` (reply key).
- Not changed: governance decides visibility as before. Other link projection callers keep the bounded
  walk. The other borrow decline states do not start a warm; the watcher still advances a corpus that
  is behind.
