## Why

A person who keeps related Records collections cannot ask one question across them. Take a practice diary whose `site` field names a row in a Sites collection, and whose entries list `skills[].skill_id` values from a Skills collection. A question such as which days were spent at site X, which skills were practised there, what was confirmed, the total hours and the leave days used needs rows from all three collections. Today an agent must find the site's key, filter the diary, then look up each skill separately and add the hours itself. Nothing tells the agent that `site` refers to Sites. So owners copy facts by hand instead, for example a list of diary entries kept on each skill row, and those copies drift.

The query engine already has a join IR and validator (`query_engine/ir.py`, `validation.py`), but nothing executes them, and nothing lets a collection declare a relation. The planned Q3 joins in `add-collection-query-engine` resolve only by an opaque `item_key`, declare relations on types, and refuse every reverse lookup. That design cannot bind the string references owners already write, and it cannot answer the question above without rewriting data first.

## What Changes

- A collection manifest can mark a string field, an array of strings, or a string property of objects in an array as a reference to one target collection, by stable collection id. A value resolves through the target's single-field natural key, or as a canonical row ref. The declaration is manifest data, never a code list.
- Resolution happens at read time, through the target's existing natural-key index. Renaming a target never rewrites source rows; a rename shows up as unresolved references and as rows a filter excluded.
- Every reference read is `resolved` or `unresolved`. A withheld target reads exactly like a missing one, and a write warning is computed under the writer's own view.
- A row query can expand declared references into each returned row, and filter through a forward single-valued reference. Base rows are never multiplied.
- An aggregate reports which rows it combined, how many carry an unresolved reference, and how many rows a reference filter excluded because their reference did not resolve. Nothing unresolved is dropped silently or counted as zero.
- A later phase adds a reference index, by a new collection-store schema revision, and bounded reverse expansion, such as "the diary entries that name this skill", with an exact count. That makes hand-kept back-link fields derivable.
- A write keeps an unresolved reference and reports it as a warning. It is never refused, so an entry written before its target exists still saves.
- This change amends `add-collection-query-engine`: CQ4 and Q3 tasks 3.5–3.6 take their relation declarations from this capability, not type-level declarations, and many-valued and reverse relations are expansion, not joins.

Out of scope: time-window joins between collections, for example glucose readings joined to sleep nights by time. That is a separate relation kind for a later change. Multi-hop graph paths stay in `add-graph-traversal-queries`, which can consume the reference index as typed virtual edges. Write-time cascades are out of scope too.

## Capabilities

### New Capabilities

- `collection-row-references`: declared references between Records and Planning rows, their resolution states, related-row expansion, filtering through a reference, and aggregates that name their rows.

### Modified Capabilities

- `collection-query-engine`: no canonical spec exists yet, so this change edits the active change `add-collection-query-engine` directly. CQ4 joins follow references declared by `collection-row-references`, resolve through the declared key and stay forward-only with at most two hops.

## Impact

- Code: the manifest parser and revise validation (`structured_collections.py`), filtered base-row reductions and the read path with per-target governance (`query_engine`, `record_governance`), `read_records` and `read_plans` query arguments and their tool text, and the generated tool schemas and capabilities. Phase 2 adds a collection-store schema revision for the reference index.
- Data: no rewrite of existing rows. Owners add `references` to existing string fields; `inspect` reports how many values resolve.
- Undo: remove the `references` declarations. The phase 2 index is derived, kept out of the in-vault replica and rebuilt from items.
