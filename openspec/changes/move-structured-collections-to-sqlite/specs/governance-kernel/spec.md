## ADDED Requirements

### Requirement: Collection store artifacts and view paths never disclose withheld items
The vault-side collection directory (the replica, mode marker, staging, aside, foreign, exported and sync-conflict copies) SHALL be reserved and denied on every egress path, the hosted gateway, and every index and resolver. Release decisions SHALL be cached per audience, policy fingerprint and collection, and re-evaluated only for changed rows or a changed fingerprint. Every surface that can expose a view path SHALL authorize the item behind it before disclosing the path, its title-bearing filename or its existence: directory listing and counts, inbound links and graph neighbours, the wikilink and title resolver, lexical and reference indexes, page refusal shapes, file-tool refusals, attention and due-state entries, history-page links, held-correction views, inventory counts, and sync-conflict copies of views.

#### Scenario: The replica is never served
- **WHEN** any caller requests, lists, searches or resolves a path under the vault-side collection directory
- **THEN** it is treated as absent

#### Scenario: A withheld item's filename does not leak through a listing
- **WHEN** a withheld item's view has a title-bearing filename and an audience lists its folder, follows backlinks, or resolves the title
- **THEN** the view path, filename and count are absent from every response, exactly as if the item did not exist

### Requirement: Collection type default audience is a subject-level default-deny
Every item of a collection type whose declaration sets `default_audience: owner` SHALL be evaluated with a subject-level default-deny that follows exactly the existing scope `default_deny` rule:
- an audience that no standing rule names for a scope matching the item receives the minimum disclosure;
- the owner is never subject to it;
- authored standing rules and grants that name an audience release the item as they would under a default-deny scope.

A type with `default_audience: policy` SHALL add no default. A type declaration SHALL NOT write, synthesize or remove any policy file, and policy SHALL remain canonical only in `_Governance`. The collection type registry hash SHALL be an input to the governance compile fingerprint, so a changed default is never served from a stale decision. Explain output SHALL name `collection-type:<name>` as the default-deny source. Widening a type's default from `owner` to `policy` SHALL be saved only by the owner principal.

#### Scenario: A declared type is private by default
- **WHEN** a new type is saved with the default audience and no authored rule names a delegated audience for its items
- **THEN** that audience's queries, inventory, counts and link projections treat every item of the type as absent, while the owner sees them

#### Scenario: An authored rule shares a declared type
- **WHEN** the owner authors a standing rule naming an audience over a scope whose paths match the type's placement
- **THEN** that audience receives the items up to the rule's ceiling, and explain names both the rule and the type default

#### Scenario: A non-owner cannot widen the default
- **WHEN** a non-owner principal saves a type change from `owner` to `policy`
- **THEN** the save refuses and the default is unchanged

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
