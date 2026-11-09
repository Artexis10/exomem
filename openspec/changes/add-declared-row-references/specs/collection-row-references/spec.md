## ADDED Requirements

### Requirement: A collection field declares a reference to one target collection

A Records or Planning manifest SHALL let a string field, an array of strings, or a string property of objects in an array declare `references: {collection: <collection ref>}`. A scalar field SHALL be single-valued and an array SHALL be many-valued. For a new or changed declaration, create and revise SHALL refuse a target that is missing, in another vault or not admitted to the author with one generic error, before naming anything about the target. They SHALL then refuse, naming what to change, a target without a single-field natural key of type string, date or datetime, a source field that is not a string, and a source or target collection that is not store-backed. Code SHALL NOT list relations or branch on a relation name.

#### Scenario: A diary field refers to a sites collection

- **WHEN** an owner revises a diary manifest so that `site` declares `references: {collection: <sites ref>}` and Sites has the natural key `site_id`
- **THEN** the revise commits, and a diary row whose `site` equals a released site's `site_id` reads that site as resolved

#### Scenario: Withheld and missing targets are refused alike

- **WHEN** one revise names a target collection the author cannot read and its twin names one that does not exist
- **THEN** both are refused with the same error, which names nothing about either target

### Requirement: A value resolves only through its declared key

A value that parses as a canonical record or plan row ref SHALL be read only as that ref, and a ref to another collection SHALL NOT resolve. Any other value SHALL match the target row whose natural-key field equals it under the target's natural-key normalisation: Unicode NFC for strings and ISO form for dates, case-sensitive, untrimmed. A malformed date SHALL read `unresolved`, never an error. A null, absent or empty value SHALL be no reference. A value repeated in one array SHALL count once.

#### Scenario: Case differs

- **WHEN** a diary row's `site` holds `north-field` and the site's key is `North-Field`
- **THEN** the reference does not resolve

### Requirement: Each reference read is resolved or unresolved

Every reference a query names SHALL read `resolved` or `unresolved` on each returned row; references the query does not name SHALL NOT be resolved. It SHALL be `unresolved` when no released target row matches, when the target row is withheld, and when the target's natural-key field is withheld from the caller. A withheld target row SHALL read exactly as a missing one. A query names a reference when it lists it in `related` or uses a dotted path through it; selecting the raw source value SHALL NOT count as naming it. A query that names a reference to a withheld or missing target collection SHALL be refused with `COLLECTION_NOT_FOUND`. A filter, grouping or inverse expansion that depends on a target's withheld natural-key field SHALL be refused with `QUERY_FIELD_UNAVAILABLE`. A path, `related` expansion, filter or inverse expansion over a source field the caller cannot read SHALL be refused with `QUERY_FIELD_UNAVAILABLE`. `inspect` SHALL compute resolution counts and unresolved values under the caller's view, and SHALL omit inbound declarations whose source field is withheld from the caller; its output for a withheld target collection SHALL equal its output for a missing one.

#### Scenario: Withheld and missing target rows read the same

- **WHEN** one fixture's diary row names a site row the caller cannot read and its twin names a site that does not exist
- **THEN** both rows return with `site` unresolved, and no field, count or diagnostic tells them apart

### Requirement: A row query expands references, filters through them and totals honestly

A row query SHALL accept `related`, naming declared references to expand into each returned row with the target's released fields, bounded per row with an exact count. A dotted path through a single-valued reference SHALL work in select, filter and grouping. Expansion SHALL NOT multiply base rows. Each target collection SHALL pass its own admission and field plan before any of its values are read, and a truncated expansion SHALL say so with its reason. One filter SHALL feed the row page and any aggregate on the same snapshot. The aggregate SHALL report `combined` with `count`, `unresolved_rows` (combined rows where a named reference did not resolve), `excluded_unresolved` (base rows a reference filter excluded only because their reference did not resolve, whatever the cause) and `unresolved_values` (a bounded list of distinct unresolved values with their counts, marked `truncated` with the total distinct count). Every bounded list of unresolved values, including import warnings and `inspect`, SHALL carry `truncated` and the total distinct count. An aggregate SHALL accept its own `where` over the same admitted rows. A filter through a path SHALL use three-valued logic: a row SHALL be excluded and counted in `excluded_unresolved` only when the whole filter is unknown because a reference did not resolve. The row page and its cursor SHALL name the combined rows. A cursor SHALL bind the release-qualified visible-state basis of the source and each target collection, and SHALL refuse once any of them changed; a change to rows hidden from the caller SHALL NOT invalidate it. Grouping by a path SHALL report `unresolved` as a typed state, distinct from any value and from a resolved null. On a two-hop path, a failure at either hop SHALL count as unresolved, and `unresolved_values` SHALL list the source row's own values. A grouped result SHALL report `combined` per group and for the whole result.

#### Scenario: One call answers a question across three collections

- **WHEN** an agent queries the diary with a filter on `site.name`, `related: [site, skills.skill_id]`, a sum of `hours`, and a count whose own `where` keeps entries whose leave-day field is true
- **THEN** the reply lists each matching entry with its site and skills expanded, and the totals combine exactly those entries

#### Scenario: A row with an unresolved site still matches another branch

- **WHEN** an agent filters the diary on `any: [site.name = X, hours > 5]`, and one row with 6 hours names a site that does not exist
- **THEN** that row matches, and it is not counted in `excluded_unresolved`

#### Scenario: A renamed site is not a silent zero

- **WHEN** a site's key was renamed so that three diary rows still hold the old key, no other diary row holds an unresolved site, and an agent totals hours filtered on that site's name
- **THEN** `combined.count` counts only the rows that resolve, `excluded_unresolved` is 3, and `unresolved_values` lists the old key with count 3

#### Scenario: A combined row with an unresolved skill

- **WHEN** an agent totals hours over diary rows filtered on a site, expands `skills.skill_id`, and one matching row names a skill that does not exist
- **THEN** that row is combined, and `unresolved_rows` is 1

### Requirement: A write warns, under the writer's own view, and never refuses for an unresolved value

A create, append, update or import that stores a value which does not resolve for the writer SHALL commit and return a warning naming the field and value, computed under the writer's admission and field plan. An import SHALL report the count and a bounded list of distinct unresolved values.

#### Scenario: An entry written before its site exists

- **WHEN** an owner appends a diary row whose `site` names a site not yet recorded
- **THEN** the row commits, and the reply warns that `site` does not resolve

#### Scenario: Withheld and missing sites warn alike

- **WHEN** a writer who cannot read one site appends a row naming it, and its twin appends a row naming a site that does not exist
- **THEN** both rows commit with the same warning

### Requirement: Reverse expansion is bounded and never reveals a hidden collection

A row query on a target collection SHALL accept `related: [{inverse: {collection: <source ref>, field: <field path>}, limit}]`, listing the admitted source rows that refer to each returned row, capped per row, with an exact count. Only declarations of collections admitted to the caller SHALL be consulted, and `inspect` SHALL list those inbound declarations. Until the reference index is built, an inverse read SHALL report warming, never an empty list.

#### Scenario: The entries that practised a skill

- **WHEN** an agent queries a skill with the inverse of the diary's `skills.skill_id`
- **THEN** each skill row lists the diary entries that name it and how many there are

#### Scenario: A hidden source collection stays hidden

- **WHEN** a collection the caller cannot read declares a reference to the queried collection
- **THEN** an inverse expansion naming it is refused exactly as for a collection that does not exist, and `inspect` does not list it

### Requirement: Declarations change without breaking unrelated work

A revise that removes a declaration still used by a saved view in the same manifest SHALL be refused, naming the view. A saved view in another collection that depends on a removed declaration SHALL report a stale state at read time. Removing a declaration SHALL drop its index rows. A target that is later deleted, or withheld from the author, SHALL NOT block revises that leave the declaration unchanged.

#### Scenario: A declaration is removed under its own saved view

- **WHEN** an owner removes `references` from `site` while a saved view in the same manifest filters on `site.name`
- **THEN** the revise is refused, naming that view

#### Scenario: A target disappears

- **WHEN** the target of `site` is deleted and the owner revises an unrelated field of the diary
- **THEN** the revise commits, and a query naming `site` is refused with `COLLECTION_NOT_FOUND`

### Requirement: A reference never changes Planning state

A resolved reference between a Records row and a plan item SHALL be reported as evidence only. No status, health, progress or completion SHALL be derived from it.

#### Scenario: Records that name a plan item

- **WHEN** diary rows refer to a planned placement item
- **THEN** the plan item's status and health are unchanged, and the diary rows are reported only as related evidence
