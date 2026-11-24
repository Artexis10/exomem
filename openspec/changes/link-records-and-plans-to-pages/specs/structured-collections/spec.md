# structured-collections

## ADDED Requirements

### Requirement: Link values are canonicalised on write

A create, append, update, bulk upsert or import that stores a `link` value SHALL resolve it with the writer's visibility, through the projection that reads use. A value naming exactly one visible page SHALL be stored as that page's `exomem://memory/<uuid>`. A canonical memory, record or plan ref SHALL be kept as given, and so SHALL a link field that is part of the natural key. The write reply SHALL report each changed value as supplied and as stored; the row SHALL keep only the stored value.

#### Scenario: A title becomes a stable ref

- **WHEN** an agent appends a row whose `page` link names a visible page by its title
- **THEN** the row stores that page's memory ref, and the row still resolves after the page is renamed

### Requirement: An unresolved link value commits with a warning

A link value that names no visible page, or more than one, SHALL be stored unchanged, and the write SHALL commit with a warning naming the field, the value and at most three visible candidates. A withheld page SHALL produce the same result as a missing one. A value that names a heading or a block of a page SHALL also be stored unchanged with a warning, because a page ref names the whole page.

#### Scenario: An ambiguous title is kept and warned

- **WHEN** a link value matches two visible pages
- **THEN** the row commits with the value unchanged and a warning listing both candidates

#### Scenario: A link to a heading keeps its anchor

- **WHEN** an agent appends a row whose `page` link names a heading of a visible page
- **THEN** the row stores the value as written, the write warns, and the citation index holds no entry for it

### Requirement: Collection creation suggests extending an overlapping collection

Records and Planning collection creation SHALL compare the new manifest with the admitted collections of the same profile, by field-name overlap and by title and description similarity. It SHALL return `similar_collections`: at most three collections passing both configured thresholds, each with the existing reshape route and `merge_into` as its step.

#### Scenario: A near copy is pointed at the original

- **WHEN** an agent creates a collection whose fields and title match an existing visible collection
- **THEN** the create commits, and its reply names the existing collection with a merge route

### Requirement: The overlap check never refuses and never reveals

The overlap comparison SHALL NOT refuse a create. Collections that the creator may not read SHALL NOT be compared or named.

#### Scenario: A hidden overlapping collection stays hidden

- **WHEN** the only overlapping collection is one that the creator may not read
- **THEN** the create commits, and its reply carries no `similar_collections` entry

### Requirement: A field retype to link is lossless

A restructure step that retypes a field to `link` SHALL canonicalise every stored value as a link write does. It SHALL refuse, naming the unresolved values, unless every value resolves to exactly one visible target. Its inverse SHALL restore the prior stored values. It SHALL keep the restructure bounds.

#### Scenario: One unresolved value refuses the retype

- **WHEN** an owner retypes a ten-row `page` field to `link` and one value names no page
- **THEN** the step refuses, names that value and changes no row

#### Scenario: Undo restores the titles

- **WHEN** an owner undoes a committed retype to `link`
- **THEN** each row holds the title it held before the retype
