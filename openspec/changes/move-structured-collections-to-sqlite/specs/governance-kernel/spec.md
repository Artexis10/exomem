## MODIFIED Requirements

### Requirement: Governance granularity follows canonical representation
Collection manifest, view and template paths SHALL be governed vault-relative paths resolved without symlink escape. Each Records and Planning item SHALL be its own governance subject, evaluated by the existing pure evaluator against one policy resolved once per operation. Its subject SHALL comprise its view path (for a log-layout collection, the log view path), its stable `exomem://record/` or `exomem://plan/` reference, its type, the values of a schema-declared `tags` field, and its manifest's project. Existing scope selectors, standing rules, grants and org caps SHALL therefore express row-level release without a new policy language or a per-row audience value.

The query layer SHALL authorize each candidate item at L6 from identity-only columns before any value is decoded or can affect counts, caps, ordering, diagnostics, identity ambiguity, source versions, continuation snapshots, or reductions. It SHALL then compute filters, sort, pagination, totals and aggregates only over the authorized set. When the resolved policy cannot distinguish a collection's items for the audience, one collection-level decision MAY stand for every item. Authorized-only snapshots SHALL mean a hidden-only change does not invalidate a caller's continuation. A dataset file SHALL remain the governance boundary for all of its rows. Egress of a view file SHALL follow its items: an item view is released as its item, and a log view only when every item it renders is released to the caller.

#### Scenario: Row-level policy withholds one item
- **WHEN** a scope selects one item's stable reference and a rule withholds it from an audience
- **THEN** queries for that audience return every other item, and counts, aggregates, pagination and continuations are identical to that item being absent

#### Scenario: Log is authorized as one canonical artifact
- **WHEN** a log-layout collection is queried, or its log view file is requested
- **THEN** Exomem authorizes the manifest before any item, authorizes structured results per item, and treats the log view file as one artifact that is released only when every item it renders is released

#### Scenario: Item files can have mixed release decisions
- **WHEN** a collection's items fall under different governed scopes
- **THEN** only authorized items reach filter, sort, pagination, aggregate, or view computation

#### Scenario: Hidden malformed item is not parsed
- **WHEN** a below-L6 item would fail a filter's type coercion, sort first, hold an extreme value, or exceed a public cap
- **THEN** its values are never decoded and the structured result is identical to that item being absent

#### Scenario: Log view with a withheld item is withheld as a whole
- **WHEN** a log-layout collection has one item withheld from an audience and that audience requests the log view file
- **THEN** the file is withheld as if absent, while structured queries return the released items

#### Scenario: Mixed-release collection mutation refuses
- **WHEN** a caller can read only a subset of a collection's items and requests append, update or bulk upsert
- **THEN** mutation refuses as if the collection were absent, because a uniqueness conflict or generation change could otherwise reveal a hidden item

### Requirement: Planning item granularity and mutation require complete authorized state
Planning items SHALL be governance subjects exactly as Records items are. Each candidate SHALL be authorized at L6 from identity-only columns before it can affect public caps, ordering, diagnostics, identity ambiguity, relationship validation, source versions, continuation snapshots, or reductions. Authorized-only snapshots SHALL mean a hidden-only change does not invalidate another caller's continuation. Planning mutation, including an applied view edit made by a non-owner principal, SHALL refuse when the caller cannot receive the complete collection state required for safe hierarchy and guard validation.

#### Scenario: Hidden malformed item is never parsed
- **WHEN** a below-L6 Planning item would duplicate a title, exceed a public candidate cap, or lead a horizon group
- **THEN** its values are never decoded and the structured result is identical to that item being absent

#### Scenario: Hidden-only edit preserves released continuation
- **WHEN** only a withheld Planning item changes after a released-only first page
- **THEN** the authorized continuation identity remains stable and reveals no hidden change

#### Scenario: Partial-view mutation refuses
- **WHEN** a caller can read only a subset of a Planning collection and requests add, update, or triage
- **THEN** mutation refuses before commit, because an authorized subset cannot substitute for the complete collection and relationship graph
