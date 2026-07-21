## ADDED Requirements

### Requirement: Rich Semantic Blocks Follow Heading Hierarchy

A recognized rich heading at numeric ATX level `N` SHALL own content until the
next non-fenced ATX heading whose numeric level is `<= N`. Headings with numeric
level `> N` SHALL remain inside
the parent body, even when their label is a recognized rich kind. A recognized
rich heading nested beneath an unknown structural heading MAY start a unit when
no recognized ancestor is open. Authors SHALL use sibling heading levels for
sibling rich units.

#### Scenario: Nested subsections remain in the rich body
- **WHEN** `## Finding` contains `### Mechanism` and `### Consequence` subsections before the next level-two heading
- **THEN** one finding unit spans both subsections and the nested headings neither truncate it nor become sibling units

#### Scenario: Sibling heading ends the block
- **WHEN** `## Decision` is followed by `## Risk`
- **THEN** the decision ends before the risk and the parser emits two independently validated rich units

#### Scenario: Unknown structural parent can contain a rich child
- **WHEN** an unknown level-two container contains a recognized level-three rich heading
- **THEN** the recognized child starts a rich unit because no recognized ancestor owns its span

#### Scenario: Fenced headings remain content
- **WHEN** a rich block body contains an ATX-looking line inside a fenced code block
- **THEN** that line does not end the rich block or start a nested semantic unit

### Requirement: Empty Rich Units Are Diagnosed And Excluded

After leading metadata, relation rows, descendant heading markers, and whitespace
are removed, a recognized rich block with no substantive body SHALL emit stable
diagnostic `empty_rich_unit` at the heading span and SHALL NOT produce a normalized
or indexable semantic unit. Metadata, relations, or descendant headings alone
SHALL NOT make a block non-empty.

#### Scenario: Metadata-only rich heading is empty
- **WHEN** a recognized rich heading contains only `id`, `category`, `tags`, `context`, or `relations` metadata before its boundary
- **THEN** parsing emits `empty_rich_unit` and no index, graph, pack, count, or recall unit for that block

#### Scenario: Nested outline without prose is empty
- **WHEN** a recognized rich heading contains descendant heading labels but no substantive body text
- **THEN** descendant labels do not fabricate content and the parent is excluded with `empty_rich_unit`

#### Scenario: Posthoc parsing preserves the source
- **WHEN** watcher or reconcile parses a directly authored empty rich block
- **THEN** the Markdown remains byte-for-byte unchanged while the invalid unit is excluded and the finding is surfaced

### Requirement: Normalized Semantic Unit Spans Do Not Overlap

One source span SHALL contribute to at most one normalized semantic unit. While a
recognized rich block is open, deeper recognized headings and compact-shaped
bullets SHALL remain part of that rich body rather than producing nested units.
Compact observations intended as independent units SHALL be authored outside rich
spans, canonically under `## Observations`.

#### Scenario: Compact-shaped bullet inside rich body is not duplicated
- **WHEN** a non-empty rich block contains a `- [category] content` bullet
- **THEN** the bullet remains rich body content and does not create a second compact index row, graph node, count, fingerprint, or recall hit

#### Scenario: Compact observation under Observations remains independent
- **WHEN** a valid compact bullet is under `## Observations` outside any recognized rich block
- **THEN** it produces one compact semantic unit with governed kind `observation`

#### Scenario: Reparse has non-overlapping stable spans
- **WHEN** the same nested Markdown is parsed repeatedly
- **THEN** unit spans are non-overlapping and normalized units, diagnostics, identities, and fingerprints are byte-stable

### Requirement: Hierarchy Migration Rebuilds Only Derived State

The hierarchy change SHALL increment the semantic parser/index schema version.
Rebuild and reconcile SHALL replace affected derived lexical, vector, graph, pack,
and count state from Markdown without rewriting source pages. Anonymous unit
references and expected fingerprints MAY become stale; stable authored anchors
SHALL retain their parent-qualified identity while still requiring the current
fingerprint for mutation.

#### Scenario: Upgrade rebuilds rich derived records
- **WHEN** a vault indexed under the earlier heading-boundary behavior opens under the new parser version
- **THEN** derived semantic-unit state is rebuilt from Markdown and stale empty or overlapping rows disappear without a source-file write

#### Scenario: Stale anonymous reference fails safely
- **WHEN** hierarchy migration changes an anonymous rich unit span or body and a caller submits its former reference or fingerprint
- **THEN** mutation fails as stale and does not select or edit a different unit
