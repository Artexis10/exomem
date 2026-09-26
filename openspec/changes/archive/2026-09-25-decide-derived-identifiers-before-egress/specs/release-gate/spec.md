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

For a caller other than the owner, a derived structure SHALL decide each candidate page before it assembles, caps, ranks or counts it, so that its answer reads as if the pages withheld from the caller were absent. This SHALL hold for `connect_memory` `context` and `graph-context` (seeds, packed pages, the graph walk, edge endpoints and edge authors, contradictions), evolution timelines (anchors, heads and chain members), entity identity (`resolve-entity` status and `create-entity` duplicate refusal), and directory listings and overview folder totals. A reference that names no file SHALL be kept, since the absent case keeps it too. A requested page the caller may not see SHALL be refused exactly as a missing page. Under a governed policy, relation proposals, the relation queue, and triage or acceptance of a relation candidate SHALL be served to the owner only: another audience SHALL receive `available: false` with `reason: "audience_restricted"`, or the `AUDIENCE_RESTRICTED` refusal for an action, decided before any page or candidate is read. The owner's assembly SHALL be unchanged.

#### Scenario: Twin vaults give the same restricted answer

- **WHEN** one vault holds no withheld page, a twin holds one withheld page that collides with or links to visible pages, and a third holds one unrelated withheld page
- **THEN** a restricted caller's context, graph context, evolution timelines, entity lookups and listings are byte-identical across the three
- **AND** this holds for the `external` audience and for a verified principal named by the rule

#### Scenario: Relation review under a governed policy

- **WHEN** a caller other than the owner asks for the relation queue or relation proposals, or triages or accepts a relation candidate, under a governed policy
- **THEN** it receives the `audience_restricted` refusal whatever page or reference it names, and the owner's queue, proposals and decisions are unchanged

#### Scenario: An identity only a withheld entity holds

- **WHEN** a restricted caller resolves or creates an entity whose name or alias only a withheld entity holds
- **THEN** resolution answers `no_match` and creation proceeds, exactly as with no such entity

#### Scenario: A folder that holds only withheld files

- **WHEN** a restricted caller lists a folder whose every file is withheld, or its parent
- **THEN** the folder is not an entry, asking for it answers as a missing path, and overview totals do not count it

### Requirement: A restricted writer's links resolve over the pages it may see

For a writer other than the owner under a governed policy, write-time wikilink resolution SHALL consider only pages the writer may see: stem, title and path matches in body links, `sources`, bridge sources, entity connections, draft relation suggestions, capture-sweep mentions, and the semantic contract's resolution of the written page's own relation targets. A link that matches only withheld pages SHALL resolve, warn and be reported exactly as it would if those pages were absent, and an ambiguity SHALL list only visible pages. A path that names no file SHALL be unaffected. The owner's resolution SHALL be unchanged.

#### Scenario: A guess matches a withheld page

- **WHEN** a restricted writer's page links a bare stem, a title, or a full path that only a withheld page matches
- **THEN** the stored page, the warnings, the capture-sweep mentions and the contract's relation disposition are identical to the twin without that page

### Requirement: Write doors decide their target before acting

For a writer other than the owner under a governed policy, a write door SHALL decide its target page before resolving, reading or changing it. `edit_memory`, `observe_memory` by path or memory reference, `replace_memory`, and `manage_memory_file` `append`, `move` source, `delete` of a file, and `reclassify` SHALL answer a target the writer may not see exactly as a missing target, and SHALL leave it unchanged. A folder delete by such a writer SHALL be refused with one `AUDIENCE_RESTRICTED` answer whatever the folder holds, and a declared recursive delete SHALL be refused before anything is read. A creation door whose destination a withheld page occupies SHALL answer with its ordinary occupied-destination refusal, naming no path the writer did not supply, and SHALL leave that page unchanged, including under `overwrite`. A move that updates wikilinks SHALL rewrite every linking page, and SHALL report counts and paths for the linking pages the writer may see. The owner's writes SHALL be unchanged.

#### Scenario: A write door names a withheld page

- **WHEN** a restricted writer edits, observes, replaces, appends to, moves, deletes or reclassifies a withheld page
- **THEN** the answer is byte-identical to the answer for an absent page, and the withheld page's bytes are unchanged

#### Scenario: A restricted writer deletes a folder

- **WHEN** a restricted writer deletes a folder that holds visible pages, withheld pages, both, or nothing it may see
- **THEN** it receives the same `AUDIENCE_RESTRICTED` refusal each time and nothing is trashed

#### Scenario: A destination a withheld page occupies

- **WHEN** a restricted writer creates, overwrites, or moves a page to a path a withheld page occupies
- **THEN** it receives the door's ordinary occupied-destination refusal and the withheld page's bytes are unchanged

#### Scenario: A move rewrites a withheld page's link

- **WHEN** a restricted writer moves a page that a withheld page links
- **THEN** the withheld page's link is rewritten and the move's reported counts and touched paths match the twin without that page

### Requirement: Link resolution for restricted readers follows their view

For a reader other than the owner, a derived answer that depends on how a visible page's link resolves SHALL use the resolution a vault without the withheld pages would give, for the links whose candidate set includes a withheld page. The owner's resolution and its cost SHALL be unchanged.

#### Scenario: A withheld page shares a visible link's stem

- **WHEN** a withheld page's stem or title collides with a visible link's target
- **THEN** a restricted reader's graph context, context pack, links and inbound counts are those of the twin without the withheld page

#### Scenario: A bare link only a withheld page answers

- **WHEN** a visible page links a bare name that only a withheld page answers
- **THEN** a restricted reader's link list and provenance list it as an unresolved link, exactly as when no page answers it

### Requirement: Counts follow filtering and whole-vault aggregates go to the owner

A count or rank a restricted caller receives SHALL be computed over the entries it receives, and retrieval diagnostics computed before release decisions SHALL NOT be returned to it. Under a governed policy such a caller's recall SHALL run without the graph lane and graph enrichment, decided before the shared recall cache is consulted. A whole-vault aggregate that a restricted view cannot recompute SHALL be served to the owner only under a governed policy, and SHALL answer other audiences with `available: false` and `reason: "audience_restricted"`, decided before anything is read.

#### Scenario: A count beside a filtered list

- **WHEN** a restricted caller receives a filtered list with a count or ranks
- **THEN** the count equals the list's length and the ranks are consecutive

#### Scenario: A whole-vault aggregate asked for by a restricted caller

- **WHEN** a caller other than the owner asks for an audit, a registry inferred from the corpus, or a coverage block under a governed policy
- **THEN** it receives `available: false` with `reason: "audience_restricted"`, and the owner receives the aggregate unchanged

#### Scenario: A restricted caller recalls without the graph lane

- **WHEN** a caller other than the owner asks for recall with graph enrichment under a governed policy
- **THEN** its hits carry no graph hop, in-degree or enrichment, and match the answer from a vault without the withheld pages; the owner's recall is unchanged
