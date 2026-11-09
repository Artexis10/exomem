## ADDED Requirements

### Requirement: A collection field declares a reference to one target collection

A Records or Planning manifest SHALL let a string field, an array of strings, or a string property of objects in an array declare `references: {collection: <collection ref>}`. A scalar field SHALL be single-valued and an array SHALL be many-valued. Create and revise SHALL refuse a declaration whose target is in another vault, is not admitted to the author, has no single-field natural key, or whose source field is not a string, naming what to change. A value SHALL match the target row whose natural-key field equals it, or the row a canonical row ref names. No other key SHALL be matched, and code SHALL NOT list relations or branch on a relation name.

#### Scenario: A diary field refers to a sites collection

- **WHEN** an owner revises a diary manifest so that `site` declares `references: {collection: <sites ref>}` and Sites has the natural key `site_id`
- **THEN** the revise commits, and a diary row whose `site` equals a site's `site_id` reads that site as resolved

#### Scenario: A target without a single-field natural key is refused

- **WHEN** the declared target's natural key has two fields
- **THEN** the revise is refused with an error that names the target's natural key and asks for a single-field key or canonical row refs

### Requirement: A write keeps an unresolved reference and warns

A create, append, update or import that stores a value no target row matches SHALL commit the value and return a warning naming the field and the value. It SHALL NOT be refused for that reason.

#### Scenario: An entry written before its site exists

- **WHEN** an owner appends a diary row whose `site` names a site not yet recorded
- **THEN** the row commits, and the reply warns that `site` does not resolve

### Requirement: Each reference read has a visible state

Every declared reference on a returned row SHALL read as `resolved` (one released target row), `unresolved` (no released target row, including a withheld one) or `ambiguous` (more than one target row). A withheld target row or collection SHALL read exactly as a missing one. A query SHALL count each state and SHALL NOT drop a row, or count it as zero, because its reference did not resolve.

#### Scenario: Withheld and missing targets read the same

- **WHEN** one fixture's diary row names a site that the caller cannot read and its twin names a site that does not exist
- **THEN** both rows return with `site` unresolved, and no field, count or diagnostic tells them apart

### Requirement: A row query expands references and filters through them

A row query SHALL accept `related`, naming declared references to expand into each returned row with the target's released fields, bounded per row with an exact count of the related rows. A filter SHALL be able to address a released field of a single-valued reference's target. Expansion SHALL NOT multiply base rows. Each target collection SHALL pass its own admission and field plan before any of its values are read. A truncated expansion SHALL say so and give its reason.

#### Scenario: One call answers a question across three collections

- **WHEN** an agent queries the diary with a filter on `site.name`, `related: [site, skills.skill_id]` and aggregates over `hours` and the leave-day field
- **THEN** the reply lists each matching entry with its site and skills expanded, and totals over exactly those entries

### Requirement: An aggregate names the rows it combined

An aggregate SHALL report `combined` with the exact count of the rows it combined, their row refs (paged when they exceed one page), and `unresolved_rows`, the combined rows that carry an unresolved or ambiguous reference. These rows SHALL be counted from the same snapshot and admitted set as the row page.

#### Scenario: Total hours with one entry whose site does not resolve

- **WHEN** an agent sums `hours` over diary rows filtered on a site's id, and one of those rows holds an unresolved skill reference
- **THEN** the total combines every matching row, `combined.count` equals the row count, and `unresolved_rows` is 1

### Requirement: Reverse expansion is bounded and never reveals a hidden collection

A row query on a target collection SHALL accept `related: [{inverse: "<source collection>.<field>", limit}]`, listing the admitted source rows that refer to each returned row, capped per row, with an exact count. Only declarations of collections admitted to the caller SHALL be consulted.

#### Scenario: The entries that practised a skill

- **WHEN** an agent queries a skill with the inverse of `diary.skills.skill_id`
- **THEN** each skill row lists the diary entries that name it and how many there are

#### Scenario: A hidden source collection stays hidden

- **WHEN** a collection the caller cannot read declares a reference to the queried collection
- **THEN** the inverse expansion of that collection is refused as an unknown relation, exactly as for one that does not exist

### Requirement: A reference never changes Planning state

A resolved reference between a Records row and a plan item SHALL be reported as evidence only. No status, health, progress or completion SHALL be derived from it.

#### Scenario: Records that name a plan item

- **WHEN** diary rows refer to a planned placement item
- **THEN** the plan item's status and health are unchanged, and the diary rows are reported only as related evidence
