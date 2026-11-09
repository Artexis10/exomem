## Why

A person who keeps related Records collections cannot ask one question across them. Take a practice diary whose `site` field names a row in a Sites collection, and whose entries list `skills[].skill_id` values from a Skills collection. "Every day I worked at site X, the skills I practised, the confirmations, total hours and leave days used" needs rows from all three collections. Today an agent must find the site's key, filter the diary, then look up each skill separately and add the hours itself. Nothing tells the agent that `site` refers to Sites. So owners copy facts by hand instead, for example a list of diary entries kept on each skill row, and those copies drift.

The query engine already has a join IR and validator (`query_engine/ir.py`, `validation.py`), but nothing executes them, and nothing lets a collection declare a relation. The planned Q3 joins in `add-collection-query-engine` resolve only by an opaque `item_key`, declare relations on types, and refuse every reverse lookup. That design cannot bind the string references owners already write, and it cannot answer the question above without rewriting data first.

## What Changes

- A collection manifest can mark a string field, an array of strings, or a string property of objects in an array as a reference to one target collection, by stable collection id. A value resolves through the target's single-field natural key, or as a canonical row ref. The declaration is manifest data, never a code list.
- The store keeps a reference index of raw values, maintained in the canonical item transaction. Resolution happens at read time, so renaming a target never rewrites source rows. A rename shows up as an unresolved reference instead.
- Every reference read has a visible state: `resolved`, `unresolved` or `ambiguous`. A withheld target reads exactly like a missing one.
- A row query can expand declared references into each returned row, and filter through a forward single-valued reference. Base rows are never multiplied.
- An aggregate reports which rows it combined, and how many of them had an unresolved reference. Nothing unresolved is dropped silently or counted as zero.
- A later phase adds bounded reverse expansion, such as "the diary entries that name this skill", with an exact count. That makes hand-kept back-link fields derivable.
- A write keeps an unresolved reference and reports it as a warning. It is never refused, so an entry written before its target exists still saves.
- `add-collection-query-engine` CQ4 and Q3 tasks 3.5–3.8 take their relation declarations from this capability instead of type-level declarations.

Out of scope: time-window joins between collections, for example glucose readings joined to sleep nights by time. That is a separate relation kind for a later change. Multi-hop graph paths stay in `add-graph-traversal-queries`, which can consume the reference index as typed virtual edges. Write-time cascades are out of scope too.

## Capabilities

### New Capabilities

- `collection-row-references`: declared references between Records and Planning rows, their resolution states, related-row expansion, filtering through a reference, and aggregates that name their rows.

### Modified Capabilities

- `collection-query-engine` (in the active change `add-collection-query-engine`): CQ4 joins follow relations declared by `collection-row-references`. They resolve through the declared key and keep their forward-only, at-most-two-hop contract.

## Impact

- Code: the manifest parser and revise validation (`structured_collections.py`), a store migration that adds the reference index (`collection_store`), the read path and governance binding for target collections (`query_engine`, `record_governance`), `read_records` query arguments and their tool text, and the generated tool schemas and capabilities.
- Data: no rewrite of existing rows. Owners add `references` to existing string fields; `inspect` reports how many values resolve.
- Undo: remove the `references` declarations. The index is derived and rebuilt from canonical rows.
