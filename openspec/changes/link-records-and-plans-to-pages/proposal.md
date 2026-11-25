# Link Records and Planning to pages

## Why

Pages hold what a person knows and why. Records hold what is or was. Planning holds what to do. Each fact should have one home, and the other surfaces should point to it. Today they do not connect:

- A Records `link` field accepts any string. A row names its page as free text, such as a title, and no write checks it.
- No page can show the rows or plan items that cite it. `read_page(links=true)` reports only Markdown wikilinks.
- When recall finds a page, it does not show the state that lives in Records about the same thing.
- An agent that seeds a collection is not shown the pages that already know the row's subject. An incident on 2026-11-24 recorded an agent that wrote "unknown" three times in one session over facts that existing pages already held.
- A new collection can duplicate an existing one, and nothing points to the overlap.
- When the same fact lives on a page and in a row, nothing finds it.

The building blocks exist:

- link projection resolves titles and memory refs at read time under the reader's visibility;
- the derived `item_references` index serves inverse lookups for row-to-row references;
- the relation registry has a symmetric `duplicates` relation;
- #3131 adds collection restructure.

This change connects these blocks. It adds no second link store.

## What Changes

- **Typed links.**
  - A write canonicalises a `link` value that names exactly one visible page to that page's `exomem://memory/<uuid>` ref. Such a value is a title, a path or a memory ref.
  - A value that names no page, or more than one, is stored as given. The write commits and warns, naming the field and at most three candidates.
  - Reads keep today's link projection.
- **Plan items link to their results.** Planning items take an optional `results` list of at most 16 links to the record or page where the outcome lives. Completing an item without `results` succeeds, and the reply says that no result is linked.
- **One read seam for citations.**
  - `inbound_citations(ref, caller)` answers "which rows and plan items cite this page" in one indexed query, bounded and paged, from the derived `item_references` index.
  - The function applies two-ended visibility: the caller sees a citation only when it may see both the row and the page.
  - Only canonical refs are indexed. A title or path value is counted and reported as not indexed.
- **Backlinks.** `read_page` returns `cited_by`, which holds at most 20 citing rows and plan items with an exact count and a continuation.
- **Cross-surface recall.**
  - Page hits in `recall_context` and `search_memory` carry `cited_by_count` and at most three citing items. One batched query per request serves them.
  - Plan items stay out of semantic recall and out of the relation graph.
- **Related pages on Records writes.** A write that creates a row returns `related_pages`, at most three per row. These are the visible pages whose entity or title matches the row's natural key or label. A bulk write resolves its rows in one batch.
- **Overlapping collections.**
  - Collection creation for Records and Planning returns `similar_collections`, at most three, compared by field-name overlap and by the semantic similarity of title and description.
  - Each entry points to the existing `reshape_route` for a merge.
  - The check is advice only and never refuses a create.
- **Duplicate-fact detector.**
  - A maintenance job compares each row field value with the units of the page that the row links to.
  - A match opens one review item that proposes the registry's `duplicates` relation.
  - Moving the fact to its single home stays the owner's decision, made per page in that review item.
- **Link retype.** `restructure.retype_field` accepts `link` with a target. It stays lossless: the step refuses unless every value resolves, and its inverse restores the prior values.

## Capabilities

### New Capabilities

- `page-citations`: typed links from collection rows and plan items to pages, one inverse read seam, page backlinks, citation context on recall hits, and the duplicate-fact detector.

### Modified Capabilities

- `planning`: adds `results`, and a requirement that a page may list the plan items that cite it as derived pointers. Plan items stay out of recall and the graph, and the motivation requirement is unchanged.
- `records`: row creation returns `related_pages`.
- `structured-collections`: collection creation returns `similar_collections`, and link values are canonicalised on write.

## Impact

- Code:
  - `structured_collections.py` (link write path);
  - `collection_store/reference_index.py` and `schema.py` (link entries in `item_references`);
  - `planning.py` (`results`);
  - `commands.py` (`read_page` `cited_by`);
  - recall and search hit assembly;
  - `records.py` (create reply);
  - collection creation;
  - `restructure.py` (link retype, after #3131 merges);
  - a maintenance job and its review item.
- Store: no new table. `item_references` gains link rows. `item_reference_state` tracks their backfill, which reads as warming, never as empty.
- Vocabulary: no list in code. Citation relations and `duplicates` come from the relation registry.
- Load target: the Records bounds in `records`, "Records records its load target", up to 100,000 rows. Warm public reads keep p95 under 1 s.
- Coordination: the single-store owner accepted the read seam on 2026-11-24. When D10 stage 2 lands, only `inbound_citations` changes.
