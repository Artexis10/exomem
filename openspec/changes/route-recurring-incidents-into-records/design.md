## Context

`collection_claims.route()` compared a page's title, page tags and unit tags with each Records collection's effective claims. Both sides were tokenised by `structure_promotion._terms`, whose stop list was title glue only. Routing ran on the write path (`semantic_writes._observation_delta` into `due_state.apply_observation_write_delta`) and in the audit recompute (`audit._check_unreflected_observations`), which skips untracked pages older than `OBSERVATION_LOOKBACK_DAYS`. The advisory reached the agent as a passive `records_routing` field, validated into the compact terminal by `mutation_terminal._records_routing_projection` against an exact five-key shape.

## Goals / Non-Goals

**Goals:** failure notes about a product reach that product's incident collection by themselves; creating a collection looks back once; the advice tells the agent what to do at the user's prominence; `collection_candidate` stops flooding.

**Non-Goals:** the runtime appending records; changing authored tags, titles or claims; a manifest schema-version bump; new MCP tools or parameters.

## Decisions

### 1. One function-word set and one fold, applied symmetrically

`structure_promotion.FUNCTION_WORDS` is the single closed-class set. `_STOPWORDS` there is now that set plus navigation glue, and `collection_claims` and `collection_candidate` use it. `working_set_index.STOPWORDS` stays where it is: it is tuned for turn matching, carries conversational words ("want", "like") that are content words for claims, and this change is not the place to reorganise another subsystem. `collection_claims.normalize_terms` folds each token with `vocabulary_fold.fold_term`, then drops the folded token by the same filter `_terms` applies to raw tokens (function words, navigation glue, short and numeric tokens). Because both effective claims and observation terms pass through it, the fold is symmetric, and normalising stored terms again returns them unchanged, so terms stored in a due-state component recompose identically at serve.

The fold itself is owned by `vocabulary_fold.py`, the shared contract for tags, claims and routing terms. This change uses it and does not restate its rules.

Matched terms are now reported in folded form. Signal versions of plain coverage routes change once for entries whose matched terms fold differently, and those entries are re-offered once.

### 2. `claims.match` as a separate manifest attribute

`CollectionManifest.claims` keeps its `Mapping[str, tuple[str, ...]]` shape. `match` is parsed into a new `claim_match` attribute, so every existing consumer of `claims` (inspection, effective claims, the projection) is untouched. `match` was previously rejected as an unknown claims list, so no existing manifest can carry it: accepting it needs no schema-version bump, and the stop condition does not apply. Keys are closed (`type`, `category`, `project`, `tags`); each value list holds 1 to 24 non-empty strings. `category` reads the page's own `category` and its unit categories. `project` reads both `project` and `projects`: failure notes store their projects under the plural key.

A page's routing words are its title, page tags and unit tags. Its `type`, categories, projects and tags are facets: they decide predicates and never count as coverage words, because a shared `type` word let a note about one product reach another product's collection.

Route order: first the targets whose predicates all hold. Exactly one wins as `strong`. Several rank by the number of predicates held, then by word coverage, and a tie on both stays silent. When no predicate holds, the coverage route runs over every collection the page does not contradict. A page contradicts a collection when it states a value for a key the collection declares and none of its values is allowed (`project: gadget` against `match: {project: [widget]}`). Silence about a key is not a contradiction, so a page with no `project` still routes by coverage. Distinctiveness for the subject signal is still judged against every target. A collection with predicates but fewer than two claim terms is still a routing target.

The observation component stores the page's folded facets and the matched predicates, and serve-time recomposition re-applies them. A predicate route's signal version includes the predicates; a plain route's version is computed exactly as before.

### 3. Backfill as a grouped kind of `unreflected_observations`

Chosen over a new `records_backfill` category. The family already owns "a page a collection covers has no reflecting record", with its disclosure, disposition (quiet/off), attention union, carrier and review-state wiring. A new category would have to repeat all of that in `PROJECTION_CATEGORIES`, the attention registry and review families, and its dispositions would diverge from the per-page items the user already manages. The grouped entry is keyed by the manifest path, carries `kind: "backfill"`, and is composed by one function, `audit.backfill_component`, used by both recompute and serve.

- **Where:** only in `audit._check_unreflected_observations`, which runs in `due_state.recompute` / `reconcile`. That is the background upkeep path, never the request path. The write-time delta keeps only its own page's entry, and `_settle_observations_for_record` leaves the grouped entry for reconcile.
- **What:** untracked pages older than the lookback that route `strong` or by predicate to a collection and that no record reflects. A moderate word overlap is not worth one question about many notes. Pages inside the lookback keep their individual entries, so nothing is counted twice.
- **Bound:** a page is a candidate for a collection only if its title and tags share at least two of that collection's claim words and its frontmatter does not contradict the collection's predicates, or if its frontmatter satisfies the declared predicates (a `category` predicate is assumed to hold until the parse decides it). Each collection parses at most `BACKFILL_MAX_PAGES` (500, provisional) candidates per recompute, in path order. A collection that runs out records a cursor, keyed by its claims signal, in the projection's `backfill_cursors`; the next recompute resumes after it, and pages found by earlier windows are carried forward after a cheap check that they still exist, are still untracked and no record links them. While a cycle has not reached the last page, the entry carries `truncated: true` beside its count and its detail says "at least". The entry names at most `BACKFILL_SAMPLE_REFS` (8, provisional) references and carries the page list, so a narrower audience's serve recomposes the count.
- **Identity:** the signal version is `(collection_id, claims_signal)`, where `claims_signal` hashes the manifest's declared `claims` and `claims.match` only. Derived claims move with every ordinary append, so hashing effective claims re-raised a dismissed backfill whenever a record was added. The finding has no related paths. A dismissal therefore holds while more matching pages accumulate and while records are appended, and changing the declared claims asks again: "create or claims change" is expressed by identity, with no stored "last seen hash".
- **Disposition:** the grouped item is asked about once at every prominence, `maximal` included. Prominence delegates filing one observed incident; it does not delegate filing an unreviewed batch of old notes.

### 4. Disposition is an agent payload, not a server-side append

Chosen over a server-side append. The routing advisory is computed after the page write has committed and released its mutation. A server-side append there would be a second governed mutation issued from inside another's post-commit phase. It would need its own writer lease and journal entry, and on refusal it would need its own held-candidate reply to a caller who never asked for a record. The existing contract also says the runtime SHALL NOT append from the advisory. Returning a ready `record_memory` payload keeps the append on the one governed path (same lease, same validation, same held-candidate recovery), and it is simpler. The end-to-end test performs the returned payload through `op_record_memory` unchanged.

`records_disposition.disposition` applies only to `strong` routes of failure-shaped observations, meaning a folded type or category in `FAILURE_SHAPES` (provisional). Prominence is `prominence.effective_capture_level()`, the one projection of capture authority that honours the unreadable-preference floor.

- `maximal` gives `file`: `{action: append, collection, item, why}`. Fields are filled where the manifest maps: string natural-key/`title` fields from the title, the first required or natural-key date from `created`, and same-named frontmatter strings, enums (only valid values) and string arrays. The note reference goes in `sources`; unfilled required fields are listed as `missing_fields`.
- `balanced` gives `ask`: one question with no system vocabulary. `first_ask` records the signal in a separate `records-asked.json` beside the projection, because projection writes replace the payload with a fixed shape and a reconcile must not re-ask. A repeated signal returns `hold`.
- `light` and `off` give `hold`. The observation stays in review and upkeep through its due item.
- Recurrence: an item whose natural-key values equal the proposed item's, or whose string values share at least 3 folded terms at Jaccard 0.6 or more with the title (provisional), over at most 256 projected items, excluding items the page's own entry already lists as reflecting it. Finding the candidate needs no collection read: the claims projection already holds each item's key and values. The disposition is `append_occurrence` carrying a complete `record_memory` update: `item_key`, `changes` with the sources list extended by the note, `expected_container_hash` and `expected_item_version`. Those guards need one read of the collection, taken only on this path; an item that already cites the note, or whose sources field is not a list, yields no disposition.
- Edits: a disposition is offered when a note is created. On a later edit it is offered only while the page's own unreflected entry is still open, meaning the projection holds it and no record reflects it. Editing a filed note therefore neither files it again, recurs it into its own item, nor asks twice.
- Budget: a disposition the compact terminal could not carry (a question or payload over its bounds) is dropped on its own, before `first_ask` records anything, so the plain advisory still arrives and the question can be asked later. The `records-asked.json` read-modify-write holds a file lock, as `prominence_preferences` does for its record; a lock that cannot be taken asks nothing.
- Filling: a `sources` array is filled with a list whatever its item type, and the page's lifecycle `status` is never copied into a record's `status`.

`mutation_terminal` accepted exactly five keys, so any new key would have dropped the whole advisory from the compact terminal. It now accepts the optional keys with bounds: disposition vocabulary, prose of at most 480 characters, a call payload of at most 4 KiB whose action is `append` or `update` on the advisory's own collection, and a question only on `ask`. A predicate route may carry an empty `matched_terms` list. A malformed disposition drops the advisory whole.

### 5. Candidate noise budget

- Function words and terms that normalise to nothing are excluded.
- Distinctiveness: `common_terms()` names terms carried by more than `MAX_TERM_UNIT_SHARE` (0.05, provisional) of the units table, applied only from `UNIT_SHARE_MIN_POPULATION` (100, provisional) units. In the synthetic fixture, generic words sit at about 43% of units and the real domain at about 1.3%, and 5% separates them with margin on both sides. The table-wide verdict is stored on the component (narrowed to the terms its units carry) and passed back at serve, so recomposition from a subset of rows keeps the full table's answer. `detect(terms=...)` stays exact per term.
- `select()` merges candidates whose supporting units are identical. It keeps the widest-spread term, then the lexicographically first, because terms riding the same units are one domain named from several sides.
- Serve cap: at most `MAX_SERVED_CANDIDATES` (3, provisional) `collection_candidate` rows per served response, strongest then widest. The cap is applied after the audience filter and triage, so a dismissed candidate never holds a slot.

### Bootstrap byte ceiling

Compact bootstrap has a pinned ceiling of 63,300 bytes and a 512-byte headroom floor. Main measured 62,857 bytes; this change measured 62,864 (+7). Rewording the handling line so a grouped backfill is asked once at any prominence kept the integrated branch at 62,643 bytes, unchanged. The disposition vocabulary is taught by the `instruction` each disposition carries in the write response itself, so bootstrap only points at it ("disposition carries its own instruction"). It stays command-free so it survives reduced surfaces.

## Risks / Trade-offs

- Folding changes reported matched terms and, for some existing plain entries, their signal versions. Those entries are re-offered once. Accepted, because the old spellings are exactly what failed to route.
- A contradicted collection is excluded from coverage routing. An Evidence sidecar has `type: source`, so it never coverage-routes to a collection that declares a `type` predicate; such a collection names its Evidence by other keys, such as `tags`.
- `hold` at light/off relies on the due item. If the user has quieted the `unreflected_observations` family, the advisory is suppressed as before.
- The recurrence heuristic compares titles only. A recurrence phrased differently becomes a new item, which a person can merge.

## Measured on the synthetic fixture

192 invented failure notes (type `failure`, project `widget`, tags drawn from `failure`/`dogfooding`/`bug`/`ux`/none), an incident collection claiming `failures`/`dogfood`/... with `match: {type: [failure], project: [widget]}`, a prose-claims collection, and an unrelated collection:

| | main | this change |
|---|---|---|
| routed to the incident collection | 0 | 192 |
| routed to an unrelated collection | 31 | 0 |
| routed nowhere | 161 | 0 |

300 generic-word units plus one 4-unit dated domain:

| | main | this change |
|---|---|---|
| candidates | 10 | 1 |
| strong candidates | 7 | 0 |
| generic-word candidates | 7 | 0 |
| served per response | 10 | 1 (cap 3) |
