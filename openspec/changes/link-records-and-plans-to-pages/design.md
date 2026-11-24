# Design: link Records and Planning to pages

## Context

Three surfaces hold a person's knowledge: pages, Records collections and Planning collections. The owner's rule:

- a fact that would be filtered, sorted, counted or tracked across items goes in Records;
- changing state goes in Records, even for one item;
- intentions go in Planning;
- a single item's static spec stays on its page;
- when a plan item completes, its outcome lands in a record or a page, and the plan item links to it;
- the same fact stored in two places is a defect to detect and flag.

This change builds the connections that rule needs. It reuses what exists:

| Need | Existing mechanism | Gap |
|---|---|---|
| A row names a page | `link` field type (`structured_collections.py` `_SUPPORTED_FIELD_TYPES`) | The write checks only that the value is a string |
| Resolve a page name under the reader's visibility | Link projection (`structured-collections`, "Link projection decides from the reader's visible pages") | None for reads |
| Inverse lookup | `item_references` + `item_reference_state` in `collections.sqlite` (schema 17), derived, with warming state | Indexes row-to-row declarations only |
| Canonical row and plan refs | `exomem://record/…`, `exomem://plan/…` (`structured_collections.py`) | None |
| Duplicates relation | `duplicates` in `vocabulary/packs/core/relations.yaml` (family `duplication`, symmetric) | Nothing proposes it between a row and a page |
| Collection merge | `restructure` with `merge_into` and `reshape_route` (#3131) | Nothing suggests it at create time |

Primary-source basis. The roster rows used are "The model keeps misusing a tool/MCP surface" (Writing effective tools for agents) and "Context is wrong, bloated, stale, or missing" (Effective context engineering). Both sources favour three properties: tool replies that carry the next useful context, refusals that name what to change, and bounded paged results over unbounded dumps. D4 and D5 follow them, and so does the bound on every list.

## Goals / Non-Goals

**Goals.**
- One home per fact, with links that a write can type and that a reader can follow in both directions.
- A seeding agent sees what the knowledge base already holds about a row's subject.
- Overlap and duplication become visible items that the owner decides, not silent drift.

**Non-Goals.**
- No second relationship store. D10 relationship rows (`make-vault-store-canonical` stage 2) are out of scope. When stage 2 lands, the single-store owner decides whether D10 rows take row endpoints, and only `inbound_citations` changes.
- No automatic move of a fact between a page and a row. No page rewrite.
- No server-side judgement of meaning, such as whether a value "means unknown". That would be a word list (C6).
- No change to semantic recall eligibility: plan items and rows do not become recall hits.

## Decisions

### D1. Links are canonicalised on write and resolved on read

- On create, update, bulk upsert and import, each `link` value goes through the read-side link projector with the writer's visibility.
- When the value names exactly one visible page, the stored value becomes that page's `exomem://memory/<uuid>`. A value that is already a canonical memory, record or plan ref is kept.
- A value that names no page, or more than one, is stored unchanged. The write commits and returns a warning that names the field, the value and at most three visible candidates.
- Writes never refuse on an unresolved link. This is the same rule as row references ("A write warns … and never refuses for an unresolved value").
- Reads keep today's projection, extended so that a canonical record or plan ref resolves under the reader's visibility of that row.
- A link field that is part of the natural key is stored as supplied, because the key gives the row its identity.
- Link values inside child tables or object properties are not canonicalised or indexed in this change; a follow-up covers them.
- A withheld page reads exactly like a missing one, both on write and on read.

Alternative rejected: a new `page` field type. `link` already exists, renders through `link_kind`, and is what manifests use (for example the Dogfood Incidents `related_notes`). A second type would split the meaning.

### D2. Plan items carry `results`

- `results` is an optional list of at most 16 link values, validated and canonicalised as in D1, on any Planning collection whose manifest declares it, as `motivation` requires today.
- The update that sets `status` to a `class: done` value reports `no_result_linked` when `results` is empty. That status class is read from the `planning-values` registry, never from a status key.
- The update still commits.
- `motivation` gets the same read-time resolution. Its write behaviour is unchanged (shape-checked), so existing items stay valid.

### D3. One derived index, one read seam

- Each link value that is a canonical ref adds an `item_references` row. The row holds the source collection, the field path, `match` (the target ref, as `memory:<uuid>`, `record:<collection>/<key>` or `plan:<collection>/<key>`) and the source row id.
- The index is derived:
  - the canonical item transaction writes it;
  - the existing backfill rebuilds it;
  - `item_reference_state` reports it as warming until it is built.
- Title and path values are not indexed. The resolution that would index them can change with every page rename, and a derived index must be rebuildable from the rows alone.
- `inbound_citations(ref, caller, limit, more)` is the only reader. It probes the index once for the link fields that cite the asked refs, admits only those collections, and reads their entries in one indexed range read on `match`. Rows that need a row decision are decided in one batch per collection. It filters by two-ended visibility, and returns at most `limit` citations (default 20, maximum 100) with an exact visible count and a continuation.
  - Two-ended visibility: the caller must be able to read the row's collection and field, and the page.
  - Each citation carries the profile (`records` or `planning`), the collection title, the row's canonical ref and label, and the field.
  - It also carries the field's declared `relation`, when the manifest names one.
- `relation` is an optional manifest attribute on a `link` field. It names a registry relation whose `origins` include `collection_link`. The shipped pack adds `collection_link` to the origins of `cites`, `evidenced_by`, `derived_from` and `links_to`, so the choice is data. Code never names a relation key.
- A manifest `inspect` reports, per link field, the indexed count and the count of non-canonical values ("not indexed: 7 values are titles or paths"). A partial index is never shown as complete.

The single-store owner accepted this seam on 2026-11-24 with four conditions, each met above:
1. registry relation types;
2. two-ended visibility, with a withheld page that answers like an absent one;
3. refs by stable page id, where a unit ref uses its anchor ref until stage 2 unit ids exist;
4. a derived index with no write path of its own, read in one indexed query that is bounded and paged.

### D4. Backlinks and recall context

- `read_page` returns `cited_by: {items, count, more, state}` from `inbound_citations`. `state` is `ready`, `warming` or `unavailable`. While the index warms, it reports `warming`, never an empty list. The state comes from the vault alone, never from the page asked about.
- A citation read never walks the vault to find a page. It borrows the published corpus generation, and with none to borrow it reports `unavailable`.
- Page hits in `recall_context` and `search_memory` carry `cited_by_count` and at most three citing items. One `inbound_citations_batch(refs, caller)` call serves them, with one query for the whole hit list, never one per hit. The hits involved are the top 10 page hits of the response.
- Rows and plan items reach their pages through the resolved link projection (`related` expansion and resolved titles).
- A new Planning requirement clarifies the existing "Motivation is queryable and never becomes a relation or recall edge". It leaves that requirement unchanged and says:
  - plan items still never become recall hits or graph edges;
  - a page MAY list the plan items that cite it as derived pointers.

### D5. Row creation returns related pages

- A write that creates rows resolves each new row's natural-key values and its presentation title field against visible entities and page titles. It reuses the entity resolution behind `connections.resolve_entity`. A bulk write resolves all its new rows in one batch.
- The reply carries `related_pages`: at most three per row and at most 30 per reply, each with its title and ref.
- The capture skill's Records guidance says: before writing an unknown or a placeholder, read the related pages.
- The server does not decide what counts as "unknown".
- A real-model round replays the 2026-11-24 seeding incident (`eval-loop`): agent-side behaviour is the outcome, and a stubbed model proves only the wiring.

### D6. Creation suggests overlapping collections

- Records and Planning `create_collection` compare the new manifest with the admitted collections of the same profile, on two scores:
  - the Jaccard overlap of field names;
  - the cosine similarity of title and description embeddings, read from the existing embedding runtime.
- `similar_collections` lists at most three collections above both thresholds. Each entry carries the existing `reshape_route` (`{tool, arguments, say}`) with `merge_into` as the step.
- The thresholds are configuration with shipped defaults, not constants.
- The create still commits, so the check never refuses. A refusal would block a legitimate new collection, and the user would pay for it.
- The agent tells the user in plain words and never names collections, records or planning. This is the #3131 rule, and it reuses that PR's cue text.

### D7. Duplicate-fact detector

- A maintenance job walks resolved row-to-page links only. Its work is bounded by the number of links, not by the size of the vault. It runs incrementally when a row or a page changes.
- For each pair, it compares the row's string field values, normalised for Unicode and whitespace, with the units of the linked page. It addresses units as #3143 does.
- A unit that contains a field value and mentions the row's label is a candidate.
- Each candidate opens one review item that proposes `duplicates` between the row and the unit. The item names the field, the unit and both values.
- The owner decides per page: keep both, move the fact to the row, or move it to the page. Only an accepted decision changes anything.
- Matching is literal value matching, not a word list. It finds the same value written twice, which is the defect the owner described.

### D8. Retype to `link` stays lossless

- `restructure.retype_field` (#3131) accepts `type: link` together with `relation`.
- The step canonicalises each value as in D1. It refuses unless every value resolves to exactly one visible page, naming the unresolved values.
- Its inverse restores the prior stored values from shape history.
- It works within #3131's bounds: at most 4,000 rows per collection and 256 rewritten rows per call.
- This lands after #3131 merges.

## Risks / Trade-offs

- **Recall gets heavier.** Each recall adds one index probe and one indexed range read. The proof is a query-count test (the same two citation statements per response, however many hits) and a before/after latency run on a generated vault of 100,000 rows and 10,000 pages. The bounds are the Records load target and warm public p95 under 1 s.
- **Canonicalising on write changes stored bytes.** A title the agent wrote becomes a ref. The reply shows the resolved title, and history keeps the value as written.
- **A page that many rows cite under a policy that varies by row costs one decision per citing row.** The rows are decided in one batch per collection, so the statement count stays fixed, but each decision costs what governance's decision costs. At 20,000 citing rows on one page the read takes about 1.3 s (tasks 1.9).
- **Legacy title values stay unindexed until someone converts them.** Inspect reports how many there are, and D8 converts them on request.
- **The literal detector misses paraphrases.** The candidate rule is precise enough that its review items stay small. A semantic pass would need a model call per pair, and an evaluation has to show it pays before it is added.

## Migration

- Store: no schema change for `item_references`. A link field's backfill state rows are seeded per manifest, as declarations are today.
- Packs: `collection_link` is added to the origins of four relations, which is an additive registry change.
- Existing rows are untouched until a write or an explicit D8 retype.
- Rollback: revert the code. The index rows are derived, and dropping them through the existing derived-table reset restores the prior state.
