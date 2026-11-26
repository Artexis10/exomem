# Bound Records Query Link Projection

## Why

A Records or Planning query on a collection with link fields decides whether the reader may see each
link's target. When no published corpus generation could be borrowed, the link projector walked the
vault on the query thread. On a vault with about 1,000 pages per folder that walk took 750 to 1,070 ms
of query CPU. Pages and aggregates then missed the 1 s Records query budget, and some timed out, even
when the query returned no link field.

## What Changes

- A Records or Planning query in the flat query grammar does not walk the vault while a published
  corpus generation is coming. A generation is coming when the corpus cache is on, the vault's change
  events are live, the borrow found no generation, one behind the event checkpoint, or its lock held,
  and a background warm that publishes one runs. One function, `semantic_contract.link_corpus_coming`,
  makes this decision from the borrow's decline reason, and starts or joins that warm.
- While a generation is coming, a returned link field leaves out its bare titles and memory references.
  The reply names each field that lost such a value in `unresolved_links`, so a value left out never
  reads as an empty field. It names a link column of an expanded child table as the reply's columns do.
  Planning query replies carry the same key, null when the query could check every returned link field.
- While a generation is coming, a filter, sort, date or aggregate column on a link field, and a saved
  view that filters on one, refuse with the retryable code `LINK_INDEX_WARMING` and `retry_after_ms`.
  Without the index such a query would match no row or count the value as null, with no flag.
- On each of those decline reasons the query starts one single-flight background warm that publishes a
  current generation, or joins the one that runs. The query does not wait for the warm, and a later query
  borrows the published generation. A page write that leaves the generation behind therefore leaves a
  link selection refused only until that warm is done.
- When no generation is coming, as with the corpus cache off, background warms off, or a one-shot process
  whose vault change events are not live, the query runs the bounded walk as before. If that walk stops
  at its cap, the query names each returned link field that lost a value in `unresolved_links` and
  refuses a selection on such a field with `LINK_INDEX_INCOMPLETE`, instead of answering without those
  values. A link field whose values all name a page by path loses none, so a selection on it answers.
- A query with explicit columns on a store-backed collection, when the store selects its rows, projects
  only the fields of those columns, so it resolves no link when it returns no link field. A collection
  kept in vault files still projects whole rows.

Visible change: an agent that reads a Records or Planning query reply can receive `unresolved_links`.
It then knows that the named fields are incomplete. A query that selects on a link field can receive
`LINK_INDEX_WARMING`, which tells it to retry after the wait, or `LINK_INDEX_INCOMPLETE`, which tells
it to query without selecting on those fields.

## Impact

- Affected capabilities: `structured-collections` (link projection), `planning` (query reply keys).
- Affected code: `record_governance` (query link projector, refusal and reply), `semantic_contract`
  (the corpus-coming decision, which schedules the warm), `find` (the single-flight warm),
  `query_data` and `record_formats` (column-scoped projection, and the one reader of a query's
  selection columns that the link refusal shares), `planning` (reply key),
  `plan_memory` (the refusal keeps its retry delay), `cli_ops` (the refusal maps to HTTP 503).
- Not changed: governance decides visibility as before. A version-1 query keeps building its link
  index inside its own deadline. Page citations never walk, as before.
