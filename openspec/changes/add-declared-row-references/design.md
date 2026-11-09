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
| Cross-collection read snapshot | Works. One connection, one transaction, admission cached per collection. | `collection_store/runtime.py` |
| Aggregates | Partial. Per-group count, sum, avg, min, max and latest, with exact totals. No aggregate names its rows. Reductions cannot filter. | `query_engine/reductions.py` |
| Graph, entities, `memory_refs` | Works for pages and entities. Rows are never graph nodes. | `vocabulary/packs/core/relations.yaml` |

## Gaps

1. No collection can say that a field refers to another collection. So an agent must guess, and an owner copies facts instead.
2. Planned joins resolve only by `item_key`, a value owners never write. Natural keys such as a site id are what rows actually hold.
3. A many-valued reference, such as the skills listed on a diary entry, has no shape. Planned joins allow only many-to-one.
4. Reverse lookups are refused, so a back-link list kept by hand cannot be derived.
5. No aggregate shows its working, and no read reports a reference that does not resolve.

## Decisions

### D1. Declare references on the source field, in the manifest

Add `references: {collection: <stable collection ref>}` to a string field, an array of strings, or a string property of objects in an array. Cardinality follows from the field's shape: a scalar is single-valued and an array is many-valued. Revise validates the declaration:

- the target is in the same vault and admitted to the author;
- the target has a single-field natural key;
- the source field is a string.

A type-level declaration, as in CQ4, is rejected, because a relation binds two collections that one owner keeps, not two shapes. Relations are per-vault schema data in the manifest. The vocabulary registry keeps graph relation types. Code never lists relations or branches on a relation key.

### D2. Resolve by the target's natural key, or by a canonical row ref

A value matches the target row whose single natural-key field equals it, or the row a canonical `exomem://record/<cid>/<key>` ref names. This is declared, not inferred: the manifest names the target, and the target names its own key. Fuzzy matching is excluded, and so is matching on an undeclared field.

### D3. A reference index, with resolution at read time

The store keeps `item_references(source collection, row, field path, ordinal, raw value)`, indexed on `(source collection, field path, value)`. It is maintained in the canonical item transaction, beside the rollups, and rebuilt like other projections. Resolution happens at read time. Renaming a target rewrites nothing; the old values become `unresolved` and stay visible. Materialising resolved targets was rejected for now. It would need a write cascade across collections, which the store does not have, and it would turn a rename into a background fan-out. If expansion misses its budget at real sizes, that choice can be revisited.

### D4. Every reference has a visible state

Each declared reference on a returned row reads as one of three states:

- `resolved`: one released target row matches.
- `unresolved`: no released target row matches, or the target is withheld.
- `ambiguous`: more than one row matches, for example across schema versions.

A withheld target is indistinguishable from a missing one, the same rule as CQ4 §6. A query counts each state; it never drops a row or treats it as zero.

### D5. One query: expand, filter and total

A row query gains two things. `related` names declared references to expand into each row. A filter can address a single-valued reference's target field, such as `site.name`. Expansion is bounded per row and never multiplies base rows. Aggregates run on the base rows in the same snapshot and report `combined`: the exact row count, the row refs (paged when there are more than a page holds) and `unresolved_rows`. The motivating question then becomes one call on the diary: filter on `site.name`, expand `site` and `skills.skill_id`, and sum `hours` and leave days.

### D6. Warn on write, never refuse

A write that stores an unresolved value succeeds and returns a warning that names the field and value. A refusal would block imports, and any entry written before its target exists. The owner would pay for it with a failed write and nothing in return, because the read already shows the unresolved state.

### D7. Reverse expansion is bounded and comes second

`related: [{inverse: "<source collection>.<field>", limit}]` on the target lists the source rows that refer to each returned row, with an exact count, capped per row. Only declarations of collections admitted to the caller are consulted, so a hidden collection is never revealed by its references. This retires hand-kept back-link fields. It is the reverse traversal that CQ4 left to the owner, delivered as expansion rather than as a join, so grouped SQL results never multiply.

### D8. References never change Planning state

A resolved reference from a Records row to a plan item is evidence, never progress. No status, health or completion is derived from it. This follows the existing rule that time and related rows never prove completion.

## Alternatives

- **Q3 joins as specified.** Its governance design is reusable. But it resolves by `item_key`, it cannot reach many-valued skills, and it refuses reverse lookups. It also needs the owner's strings rewritten first.
- **Rows as graph nodes, through virtual edges.** This gives one model for rows, pages and entities. But it waits on store mode, and path aggregates cannot sum row fields. `add-graph-traversal-queries` can consume this change's index as its row-link edges later, so nothing is built twice.
- **Keep copies by hand.** The status quo. Copies drift, and the owner does the work.

## Governance

Each target collection passes its own admission and field plan before any target value is read. Today the route wires one link projector and one field plan per session. Each referenced collection needs its own. Results carry their egress receipts. The 64 KiB result cap and the interactive visit budget cover expansion. A truncated expansion says so and gives its reason.

## Migration

1. The owner adds `references` to an existing string field. No row changes.
2. `inspect` reports resolved, unresolved and ambiguous counts, and a bounded list of the distinct unresolved values.
3. The owner fixes values, or creates missing targets, through governed writes. An agent may propose a mapping. Nothing matches fuzzily.
4. Once reverse expansion ships, the owner removes the hand-kept back-link fields.

## Single store

The index lives in the collection store, which store mode extends to pages. `make-vault-store-canonical` puts pages and graph tables in the same database and transaction. `add-graph-traversal-queries` plans declared row links as virtual edges maintained in the canonical transaction. This change supplies those declarations and that index.

## Risks

- **Expansion cost at real sizes.** Bound it, measure it in the acceptance run, and materialise only if the run misses its budget.
- **Derived collections read from a separate attached file.** That file is not atomic with the main store, so they stay out of the first phase.
- **Renames orphan references.** The unresolved state makes them visible, and canonical row refs avoid the problem for owners who want rename safety.
