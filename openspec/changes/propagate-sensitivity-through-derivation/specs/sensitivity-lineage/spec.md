## ADDED Requirements

### Requirement: Effective classes are own, floor and lineage

A page's effective classes SHALL be the union of its own `sensitivity` field, its `inherited_sensitivity` field and its live lineage classes. A row's effective classes SHALL be the union of its own classes, its collection floor and the effective classes of the pages its source refs name. Every governance decision that reads a class SHALL read the effective classes.

#### Scenario: A note cites a classified Source

- **WHEN** a Source carries `sensitivity: [health]` and a note names it in `sources`
- **THEN** the note's effective classes include `health`

#### Scenario: A row names a classified page as its source

- **WHEN** a row's source ref names a page with effective class `financial`
- **THEN** the row's effective classes include `financial`

### Requirement: A write carries the classes of what it drew on

Every write surface SHALL call one pre-plan step that sets `inherited_sensitivity` to the union of the effective classes of the pages the write names in `sources`, the page it edits and, for an episode, its `about` refs. The step SHALL merge those refs into the page's `sources` field or the row's source refs. The plan function SHALL refuse a write whose `inherited_sensitivity` holds fewer classes than the step computes. A write SHALL NOT take a class only because its session read a page.

#### Scenario: An observation drew on a classified page

- **WHEN** an agent adds an observation to an entity page with `sources` naming a page of class `identity`
- **THEN** the entity page's `inherited_sensitivity` includes `identity`, and its `sources` field names that page

#### Scenario: A surface skips the pre-plan step

- **WHEN** a write reaches the plan function with an `inherited_sensitivity` that lacks a class of a page it names in `sources`
- **THEN** the plan function refuses the write and names the missing class

#### Scenario: A session read a classified page the write did not use

- **WHEN** a session was shown a `health` page and then writes a note that does not name it
- **THEN** the note takes no `health` class, and the write result lists that page as shown but not used

### Requirement: Every compiled write surface accepts sources

`edit_note`, `add_observation`, `add_record` and `edit_record` SHALL accept an optional `sources` list, as `create_note` and `supersede_note` do.

#### Scenario: An edit names its sources

- **WHEN** an agent edits a note with `sources` naming a page
- **THEN** the note's `sources` field names that page after the write

### Requirement: Only classify lowers a floor

`inherited_sensitivity` SHALL be protocol-owned. An edit SHALL NOT remove a class from it. The classify operation SHALL clear or lower it with a required reason and a reason record that allows a revert.

#### Scenario: An edit drops the field

- **WHEN** an agent edits a page's frontmatter and leaves out `inherited_sensitivity`
- **THEN** the write keeps the field's classes

### Requirement: Lineage follows derivation edges and stays current

The vault store SHALL hold a derived `page_lineage` object with each page's lineage classes and a generation. Lineage SHALL follow the graph edges whose relation carries the `inherits_sensitivity` behaviour, transitively. The store thread SHALL recompute it in the same derivation transaction that changes a page's own classes, its floor or one of those edges. The generation SHALL increase whenever the lineage classes change.

#### Scenario: A Source is classified after a note cites it

- **WHEN** a note cites a Source, and later the owner classifies the Source as `identity`
- **THEN** after the next derivation the note's effective classes include `identity` without any change to the note's bytes

#### Scenario: A cycle of derivation edges

- **WHEN** two pages cite each other and one gains a class
- **THEN** both carry the class and the recompute terminates

### Requirement: Governance caches key on the lineage generation

Every cache of a governance decision or scope set SHALL key on the page's lineage generation as well as its content identity.

#### Scenario: A cached decision after a raise

- **WHEN** a restricted caller's decision for a note is cached and the note's Source then gains a class
- **THEN** the next read decides the note again under its new generation

### Requirement: Removals flow down only to pages written before the class

Clearing a class on a page SHALL remove it from dependents that hold it only through lineage. A dependent whose `inherited_sensitivity` holds the class SHALL keep it.

#### Scenario: A class is cleared on a Source

- **WHEN** note A cited a Source before the Source was classified `health`, note B cited it after, and the owner then clears `health` on the Source
- **THEN** note A no longer carries `health` and note B still does

### Requirement: A class needs a page the graph indexes

Capture, classify and a plain edit SHALL refuse a class on a page the graph does not index, with `SENSITIVITY_PAGE_UNTRACKED` and a reason that names the rule.

#### Scenario: Classifying a page in an excluded folder

- **WHEN** the owner classifies a Markdown page in a scan-excluded folder
- **THEN** the classify refuses with `SENSITIVITY_PAGE_UNTRACKED` and the page's bytes are unchanged

### Requirement: Unproven lineage answers as missing

When the active policy has a scope that selects by `sensitivity` and the caller is not the owner, a page SHALL be released only with a proof that no derivation is pending at the read's pinned revision for the page or any page in its lineage ancestry, graph catch-up included. Without the proof, the page SHALL answer as a page that does not exist, in response shape and in timing. Owner reads SHALL NOT pay this check.

#### Scenario: A raise is still deriving

- **WHEN** a hub Source gains `health` and a restricted caller searches while a chunk of its dependents is still pending
- **THEN** the pending dependents answer as missing, and the answer cannot be told apart from one where those pages do not exist

### Requirement: The session read record is bounded evidence

The governance sidecar SHALL record, for every principal, each classified page a top-level read boundary released and the classes it released, keyed by session id. The row SHALL commit before egress releases the bytes. Rows older than a retention setting, 30 days by default, SHALL be deleted. A write result SHALL list the classified pages its session was shown that the write did not name in `sources`, edit or name in `about`. The record SHALL NOT be an input to any class or disclosure decision.

#### Scenario: The sidecar cannot commit the row

- **WHEN** the read record cannot commit for a read that would release a classified page
- **THEN** the read releases nothing for that page and reports the failure
