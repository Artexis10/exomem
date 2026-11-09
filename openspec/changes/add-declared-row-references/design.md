## Context

This design is the architecture assessment for first-class relationships and cross-collection queries. It reads the code at `3e3d20ee3` and the active changes `add-collection-query-engine`, `add-graph-traversal-queries` and `make-vault-store-canonical`.

Design sources: Anthropic, "Writing effective tools for agents". An agent should make one obvious call that returns an answer, not compose internal steps such as key lookups and joins. Anthropic, "Code execution with MCP". Large intermediate results stay in the server, which computes the totals and returns them bounded.

## What exists

| Mechanism | State | Evidence |
| --- | --- | --- |
| Planning `parent` and `area` | Works, within one collection. The only write-time foreign-key check in the repository. Reverse is an index range read. | `planning.py` hierarchy validation; `index_migrations.py` reference projections |
| Canonical row refs `exomem://record|plan/<cid>/<key>` | Works as a format. Resolved only inside the same collection, for rendering. | `structured_collections.py`, `record_formats.py` |
| Link fields and arrays of links | Partial. Any string is accepted. Values are projected per caller against pages, so a row ref resolves to no page and is omitted. | `record_governance.py` link projection |
| `links.plans[]` Records-to-Planning binding | Partial. Value equality on unchecked plan text. Consumed only by an audit check. Refused on summary collections. | `structured_collections.py`, `audit.py` |
| Join IR and validator | Dead code. At most two forward joins over a declared `relations` block that nothing produces. Every executor refuses `query.joins`. | `query_engine/ir.py`, `validation.py`, `typed_rows.py`, `route.py` |
| Cross-collection read snapshot | Works. One connection, one transaction, admission and field plans kept per collection; the link projector is still one per session. | `collection_store/runtime.py` |
| Aggregates | Partial. Per-group count, sum, avg, min, max and latest, with exact totals. No aggregate names its rows. Reductions cannot filter. | `query_engine/reductions.py` |
| Graph, entities, `memory_refs` | Works for pages and entities. Rows are never graph nodes. | `vocabulary/packs/core/relations.yaml` |

## Gaps

1. No collection can say that a field refers to another collection. So an agent must guess, and an owner copies facts instead.
2. Planned joins resolve only by `item_key`, a value owners never write. Rows actually hold natural keys, such as a site id.
3. A many-valued reference, such as the skills listed on a diary entry, has no shape. Planned joins allow only many-to-one.
4. Reverse lookups are refused, so a back-link list kept by hand cannot be derived.
5. No aggregate shows its working. Reductions cannot filter. No read reports a reference that does not resolve.

## Decisions

### D1. Declare references on the source field, in the manifest

Add `references: {collection: <stable collection ref>}` to a string field, an array of strings, or a string property of objects in an array. Cardinality follows from the field's shape: a scalar is single-valued and an array is many-valued. Create and revise validate a new or changed declaration. They refuse a target that is missing, in another vault or not admitted to the author with one generic error. It checks that before it names anything about the target. It then requires:

- a single-field natural key on the target, of type string, date or datetime;
- a string source field;
- store-backed source and target collections. File-mode and derived collections are refused in phase 1.

A type-level declaration, as in the earlier CQ4 text, is rejected, because a relation binds two collections that one owner keeps, not two shapes. This change amends CQ4 to match. Relations are per-vault schema data in the manifest. The vocabulary registry keeps graph relation types. Code never lists relations or branches on a relation key. Planning `parent` and `area` and the `links.plans[]` binding stay separate in this change. Whether `links.plans[]` folds into declared references is a follow-up question.

### D2. Resolve by the target's natural key, or by a canonical row ref

A value that parses as a canonical `exomem://record/<cid>/<key>` or `exomem://plan/<cid>/<key>` ref is read only as that ref; a ref to another collection does not resolve. Any other value matches the target row whose natural-key field equals it, under the target's own natural-key normalisation: Unicode NFC for strings and ISO form for dates, case-sensitive, no trimming. A malformed date reads `unresolved` and never raises an error. A null, absent or empty value is no reference. A value repeated in one array counts once. Revise forbids changing a collection's natural key or schema version, and the store keeps one row per natural key, so a value matches at most one row. Fuzzy matching is excluded, and so is matching on an undeclared field.

### D3. Phase 1 needs no new table

Forward resolution is a lookup on the target's existing unique natural-key index, or on `(collection, item_key)` for a canonical ref. A filter through a single-valued reference first resolves the released target keys, then applies them to the source field. Declaring `references` marks the source field filterable, as named-validator references already are. If that exceeds the collection's index budget, the filter falls back to a bounded scan, as named-validator references do. Resolution happens at read time, so renaming a target rewrites nothing: the old values stop resolving and stay visible.

Phase 2 adds `item_references(source collection, row, field path, ordinal, raw value)`, indexed on `(source collection, field path, value)`, for inverse expansion. It is maintained in the canonical item transaction, added by a new collection-store schema revision (never by editing a shipped one), kept out of the in-vault replica and rebuilt from items. Until it is built, inverse reads report warming, never an empty list. Materialising resolved targets was rejected. It would need a write cascade across collections, which the store does not have.

### D4. Every reference has a visible state

Each reference that a query names reads `resolved` or `unresolved` on every returned row. References the query does not name are not resolved, so existing reads stay unchanged. A reference is `unresolved` in any of these cases:

- no released target row matches;
- the target row is withheld;
- the target's natural-key field is withheld from the caller by field release.

A withheld target is indistinguishable from a missing one, the same rule as CQ4. A forward reference whose target key field is withheld reads `unresolved`; the query engine's location rule carries this as its one exception, because a refusal would reveal that the key exists. A filter, grouping or inverse expansion that depends on a withheld target key refuses with `QUERY_FIELD_UNAVAILABLE`, because listing the rows that name a target would reveal its key. A query names a reference when it lists it in `related` or uses a dotted path through it. Selecting the raw source value is not naming it, so existing reads stay unchanged. A query that names a reference to a withheld or missing target collection is refused with the generic `COLLECTION_NOT_FOUND`, as CQ4 requires. Any path, `related` expansion, filter or inverse expansion over a source field the caller cannot read is refused with `QUERY_FIELD_UNAVAILABLE`, because expanding it would reveal the value through the target row. `inspect` computes resolution counts and unresolved values under the caller's view, and omits inbound declarations whose source field is withheld from the caller.

### D5. One query: expand, filter and total

A row query gains two things. `related` names declared references to expand into each row. A dotted path through a single-valued reference, such as `site.name`, works in select, filter and grouping, as the amended CQ4 states. Expansion is bounded per row and never multiplies base rows. One filter feeds both the row page and the totals, on the same snapshot. The cursor binds the release-qualified visible-state basis of the source and of each target collection, and refuses once any of them changed. A change to rows hidden from the caller never invalidates that caller's cursor.

Totals need three things that do not exist today: reductions with a filter, a per-aggregate `where` so that one query can sum one field and count the rows matching a condition, and a result that carries rows and totals together. The amended query engine adds the per-aggregate `where`. `combined` forces the base-row path, never the rollup. It reports four values:

- `count`: the rows combined;
- `unresolved_rows`: combined rows where a reference the query names did not resolve;
- `excluded_unresolved`: base rows that a reference filter excluded only because their reference did not resolve, whatever the cause;
- `unresolved_values`: a bounded list of the distinct values that did not resolve, with their counts, so an agent can recognise an old key after a rename. Like every bounded list in this design (import warnings and `inspect` included), it carries `truncated` and the total distinct count.

A filter through a path uses three-valued logic. A row whose reference does not resolve is excluded, and counted in `excluded_unresolved`, only when the whole filter is unknown for it. In `any: [site.name = X, hours > 5]`, a row with an unresolved site and 6 hours still matches.

The row page and its cursor name the combined rows. So after a site rename, "hours at site X" is not a silent zero: `excluded_unresolved` counts the rows that no longer resolve, and `unresolved_values` shows the old key among them.

The motivating question becomes one call on the diary:

1. Filter on `site.name`.
2. Expand `site` and `skills.skill_id`.
3. Sum `hours`, and count the entries with an aggregate whose own `where` keeps only rows whose leave-day field is true.

### D6. Warn on write, never refuse

A write that stores a value which does not resolve for the writer succeeds and returns a warning naming the field and value. The check runs under the writer's own admission and field plan, so the warning tells a writer nothing about rows they cannot read. An import reports a bounded warning: the count and a bounded list of distinct values. A refusal would block imports, and any entry written before its target exists, while the read already shows the unresolved state.

### D7. Reverse expansion is bounded and comes second

`related: [{inverse: {collection: <source ref>, field: "skills.skill_id"}, limit}]` on the target lists the source rows that refer to each returned row, capped per row, with an exact count. Only declarations of collections admitted to the caller are consulted, and `inspect` lists those inbound declarations so that an agent can discover them. This is expansion, not a reverse join, so grouped SQL results never multiply. It retires hand-kept back-link fields.

### D8. References never change Planning state

A resolved reference between a Records row and a plan item is evidence, never progress. No status, health or completion is derived from it.

### D9. Declarations change safely

A saved view in the same manifest that uses a declaration blocks its removal. The revise names the view, as revise refuses broken views today and as CQ1 refuses removing a required lookup. A view in another collection that depends on the declaration, such as an inverse view on the target, reports a stale state at read time. Refusing the revise there would reveal a collection the author may not be able to read. Removing a declaration drops its phase 2 index rows. A target that is later deleted, or withheld from the author, does not block revises that leave the declaration unchanged.

## Alternatives

- **Q3 joins as first specified.** The governance design is reusable and this change keeps it. But resolving by `item_key` cannot bind the owner's strings, and it cannot reach many-valued skills or reverse lists.
- **Rows as graph nodes, through virtual edges.** This gives one model for rows, pages and entities. But it waits on store mode, and path aggregates cannot sum row fields. `add-graph-traversal-queries` wants registry-typed edges over resolved refs. A declaration here carries no relation type yet. Adding an optional relation type from the vocabulary registry is the step that would let the graph change read these declarations as edges.
- **Keep copies by hand.** The status quo. Copies drift, and the owner does the work.

## Governance

Each target collection passes its own admission and field plan before any target value is read. The route needs one link projector per referenced collection. Results carry their egress receipts. The 64 KiB result cap and the interactive visit budget cover expansion. A truncated expansion says so and gives its reason.

## Migration

1. The owner adds `references` to an existing string field. No row changes.
2. `inspect` reports how many values resolve, and a bounded list of the distinct values that do not.
3. The owner fixes values, or creates missing targets, through governed writes. An agent may propose a mapping. Nothing matches fuzzily.
4. Once inverse expansion ships, the owner removes hand-kept back-link fields.

## Single store

`make-vault-store-canonical` keeps pages, graph and identity rows in the same per-vault store, committed on the collection-store writer thread. The phase 2 index fits that model: it uses a new name, a new schema revision taken in landing order, and the canonical item transaction. The graph change can read it from the same database once declarations carry a relation type.

## Risks

- **Expansion cost at real sizes.** Bound it, measure it in the acceptance run, and materialise only if the run misses its budget.
- **Derived collections read from a separate attached file.** That file is not atomic with the main store, so they stay out of the first phase.
- **Renames orphan references.** `unresolved_rows` and `excluded_unresolved` make them visible. Canonical row refs avoid the problem for owners who want rename safety.
