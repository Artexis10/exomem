## Why

A Records collection for product failure incidents stayed nearly empty although the vault held months of matching notes. Measured on a live vault (counts only): of 192 failure-type notes about one product, replaying `collection_claims.route()` over title and tags sent 29 to the incident collection, 126 nowhere (114 sharing exactly one term with its claims), and 36 to unrelated collections, 27 of them to a collection whose prose claims contribute "not", "because", "after" and "first" as terms. Notes tagged `failure`/`dogfooding` never met claims spelled `failures`/`dogfood`. Routing never saw a note's `type` or `project`, which is the actual membership signal for an incident log. Creating the collection never looked back at the pages it covered. The advisory channel was flooded: 602 open due items, 355 of them `collection_candidate` on generic words such as result, technique and workflow. Agents ignored the passive `records_routing` advisory, so every incident had to be filed by hand.

## What Changes

- **Claim hygiene.** Claim and observation terms both drop closed-class function words (one consolidated set in `structure_promotion.FUNCTION_WORDS`) and are compared through a conservative inflection fold (`vocabulary_fold.fold_term`). Authored storage is unchanged.
- **Structured claims.** A manifest's `claims` block may declare `match:` frontmatter predicates over `type`, `category`, `project` and `tags`. Every listed key must hold and any listed value may match. A page satisfying them routes to the collection as `strong` regardless of word overlap; several satisfied collections rank by predicates held, then word coverage, and a full tie stays silent. A page silent about a declared key may still route by shared words, while a page whose value contradicts it never routes there. Routing now also sees a page's type, categories and projects, as predicates rather than words.
- **Backfill.** At recompute, off the request path and bounded per collection with a resumable cursor, existing pages older than the discovery lookback that a collection now covers strongly or by predicate become ONE grouped `unreflected_observations` item per collection, asked about once at every prominence. Its signal version is the collection plus its declared claims, so a decision holds until the author changes the claims.
- **Act, don't advise.** A strong route of a failure-shaped observation carries a `disposition` chosen by effective capture prominence. `maximal` gives `file`, with a ready `record_memory` append payload and an instruction to perform it without asking. `balanced` gives `ask`, with one domain-language question asked once per signal version. `light` and `off` give `hold`. A recurrence of an existing item yields `append_occurrence` with a ready update payload. Editing an already-filed note raises none of these again. The runtime still never appends.
- **Noise budget.** `collection_candidate` excludes function words and terms carried by more than a provisional share of the units table. It merges terms that sit on identical units into one candidate, and serves at most a provisional number of candidates per response.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `structured-collections`: the `claims` block gains `match` predicates, and effective claims are compared in folded, function-word-free form.
- `command-surface`: the `records_routing` advisory routes by predicates and page facets, and may carry a prominence-driven disposition through the compact terminal.
- `attention-queue`: `unreflected_observations` gains predicate routes and a grouped backfill kind; `collection_candidate` gains a noise budget.
- `agent-bootstrap-contract`: bootstrap points agents at the disposition and the grouped backfill.
- `delegation-envelope`: the disposition is classified as advisory material directing the agent's own `proactive_capture`.

## Impact

Code touched: `collection_claims`, `vocabulary_fold` (new, shared fold contract), `structure_promotion`, `structured_collections`, `record_governance`, `due_state`, `audit`, `semantic_writes`, `records_disposition` (new), `mutation_terminal`, `collection_candidate`, bootstrap text in `commands`, and the scaffold reference `planning-records.md`. Existing manifests stay valid with no schema-version bump, because `match` is optional and was previously rejected as an unknown claims list. Matched terms are now reported in folded form. No new MCP tool or parameter is added.
