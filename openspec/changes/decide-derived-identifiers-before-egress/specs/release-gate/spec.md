## ADDED Requirements

### Requirement: Derived identifier fields are decided before egress

The terminal entry filter SHALL decide every field of a result entry that names a vault item, not only a path field. That includes relation endpoints (`to`, `from`), pair members (`a`, `b`), timeline anchors and heads (`topic_anchor`, `chain_id`), graph node keys (`src_key`, `dst_key`), and any key whose name marks an identifier (`*_path`, `*_key`, `*_anchor`, `*_source`, `*_target`, `*_ref`). A `file:` node key, a vault URI, and an extensionless page reference SHALL be decided as the page they name. A proposed relation bullet SHALL be scanned for wikilinks. An entry naming a page the caller may not see SHALL be dropped whole rather than rewritten. A value that names no vault item SHALL be kept.

#### Scenario: A relation endpoint names a withheld page

- **WHEN** a result entry carries a withheld page in `to`, `from`, `a`, `b`, `topic_anchor`, `chain_id`, a `file:` node key, or an identifier-suffixed field, with or without its extension
- **THEN** a restricted caller does not receive that entry, and receives the entries that name visible pages unchanged

#### Scenario: A proposed bullet links a withheld page

- **WHEN** a relation proposal's bullet text links a withheld page
- **THEN** the proposal is dropped whole for a restricted caller

#### Scenario: The owner keeps every identifier

- **WHEN** the owner receives the same entries
- **THEN** none is dropped

### Requirement: Derived structures decide their candidates before assembly

For a caller other than the owner, a derived structure SHALL decide each candidate page before it assembles, caps, ranks or counts it, so that its answer reads as if the pages withheld from the caller were absent. This SHALL hold for `connect_memory` `context` and `graph-context` (seeds, packed pages, the graph walk, edge endpoints and edge authors, contradictions), relation proposals and the relation queue (targets and the pages their evidence rests on, queue source pages), evolution timelines (anchors, heads and chain members), entity identity (`resolve-entity` status and `create-entity` duplicate refusal), and directory listings and overview folder totals. A reference that names no file SHALL be kept, since the absent case keeps it too. A requested page the caller may not see SHALL be refused exactly as a missing page. The owner's assembly SHALL be unchanged.

#### Scenario: Twin vaults give the same restricted answer

- **WHEN** one vault holds no withheld page, a twin holds one withheld page that collides with or links to visible pages, and a third holds one unrelated withheld page
- **THEN** a restricted caller's context, graph context, relation proposals, relation queue, evolution timelines, entity lookups and listings are byte-identical across the three
- **AND** this holds for the `external` audience and for a verified principal named by the rule

#### Scenario: An identity only a withheld entity holds

- **WHEN** a restricted caller resolves or creates an entity whose name or alias only a withheld entity holds
- **THEN** resolution answers `no_match` and creation proceeds, exactly as with no such entity

#### Scenario: A folder that holds only withheld files

- **WHEN** a restricted caller lists a folder whose every file is withheld, or its parent
- **THEN** the folder is not an entry, asking for it answers as a missing path, and overview totals do not count it

### Requirement: A restricted writer's links resolve over the pages it may see

For a writer other than the owner under a governed policy, write-time wikilink resolution SHALL consider only pages the writer may see: stem, title and path matches in body links, `sources`, bridge sources, entity connections, draft relation suggestions and capture-sweep mentions. A link that matches only withheld pages SHALL resolve, warn and be reported exactly as it would if those pages were absent, and an ambiguity SHALL list only visible pages. A path that names no file SHALL be unaffected. The owner's resolution SHALL be unchanged.

#### Scenario: A guess matches a withheld page

- **WHEN** a restricted writer's page links a bare stem, a title, or a full path that only a withheld page matches
- **THEN** the stored page, the warnings and the capture-sweep mentions are identical to the twin without that page

### Requirement: Write doors decide their target before acting

For a writer other than the owner under a governed policy, a write door SHALL decide its target page before resolving, reading or changing it. `edit_memory`, `observe_memory` by path or memory reference, `replace_memory`, and `manage_memory_file` `append`, `move` source, `delete` of a file or of a folder holding only withheld files, and `reclassify` SHALL answer a target the writer may not see exactly as a missing target, and SHALL leave it unchanged. The owner's writes SHALL be unchanged.

#### Scenario: A write door names a withheld page

- **WHEN** a restricted writer edits, observes, replaces, appends to, moves, deletes or reclassifies a withheld page
- **THEN** the answer is byte-identical to the answer for an absent page, and the withheld page's bytes are unchanged

### Requirement: Link resolution for restricted readers follows their view

For a reader other than the owner, a derived answer that depends on how a visible page's link resolves SHALL use the resolution a vault without the withheld pages would give, for the links whose candidate set includes a withheld page. The owner's resolution and its cost SHALL be unchanged.

#### Scenario: A withheld page shares a visible link's stem

- **WHEN** a withheld page's stem or title collides with a visible link's target
- **THEN** a restricted reader's graph context, context pack, links, inbound counts, relation proposals and relation queue are those of the twin without the withheld page

### Requirement: Counts follow filtering and whole-vault aggregates go to the owner

A count or rank a restricted caller receives SHALL be computed over the entries it receives, and retrieval diagnostics computed before release decisions SHALL NOT be returned to it. A whole-vault aggregate that a restricted view cannot recompute SHALL be served to the owner only under a governed policy, and SHALL answer other audiences with `available: false` and `reason: "audience_restricted"`, decided before anything is read.

#### Scenario: A count beside a filtered list

- **WHEN** a restricted caller receives a filtered list with a count or ranks
- **THEN** the count equals the list's length and the ranks are consecutive

#### Scenario: A whole-vault aggregate asked for by a restricted caller

- **WHEN** a caller other than the owner asks for an audit, a registry inferred from the corpus, or a coverage block under a governed policy
- **THEN** it receives `available: false` with `reason: "audience_restricted"`, and the owner receives the aggregate unchanged
