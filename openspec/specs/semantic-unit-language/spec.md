# semantic-unit-language Specification

## Purpose
TBD - created by archiving change enforce-semantic-authoring-contract. Update Purpose after archive.
## Requirements
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

### Requirement: Prediction Is A Governed Core Semantic Kind
The semantic-unit language SHALL recognize `prediction` as a portable, code-owned core rich kind with the heading alias `Predictions`. It SHALL resolve through the same core kind ring as every other built-in kind, so registry resolution, kind filters, schema profiling, and governed writers accept it without any per-vault registry entry. A vault-owned registry entry that shadows the built-in `prediction` kind SHALL report the existing canonical-collision finding rather than silently overriding product vocabulary.

#### Scenario: A prediction heading parses as a governed rich unit
- **WHEN** a compiled page contains `## Prediction` followed by a substantive body
- **THEN** the parsed document contains one rich semantic unit whose kind is `prediction`

#### Scenario: The plural heading resolves to the singular kind
- **WHEN** a compiled page contains `## Predictions` followed by a substantive body
- **THEN** the parsed unit's kind is `prediction`

#### Scenario: Prediction needs no registry entry
- **WHEN** the core semantic-language registry resolves the kind label `prediction` against a vault with no semantic-language registry file
- **THEN** resolution succeeds with core status and a definition rather than an unregistered status

#### Scenario: A registry extension may not shadow the built-in kind
- **WHEN** a semantic-language registry proposal declares a `prediction` kind
- **THEN** validation reports a `canonical_collision` error finding naming that key

### Requirement: Verdict Is A Governed Categorical Unit-Metadata Key
The semantic-unit language SHALL recognize `verdict` as a governed rich unit-metadata key whose value is exactly one of `abandoned`, `confirmed`, `inconclusive`, `qualified`, or `refuted`, matched after Unicode NFKC normalization and casefolding. Any other value, including any numeric value, SHALL produce a deterministic source-addressed error whose remediation names the closed set and states that a numeric confidence is not a stored field. The normalized value SHALL be projected onto the parsed unit and included in its serialized form. A verdict SHALL NOT change a unit's lifecycle, standing, or rank.

#### Scenario: A valid verdict is normalized and projected
- **WHEN** a rich unit carries the metadata row `- verdict: Refuted`
- **THEN** the parsed unit's verdict is `refuted` and the document reports no error

#### Scenario: An unknown verdict value is rejected
- **WHEN** a rich unit carries the metadata row `- verdict: probably-wrong`
- **THEN** the document reports an `invalid_rich_verdict` error bound to that source line

#### Scenario: A numeric verdict is refused with the no-confidence reason
- **WHEN** a rich unit carries the metadata row `- verdict: 0.7`
- **THEN** the document reports an `invalid_rich_verdict` error whose remediation states that confidence is not a stored field

#### Scenario: A refuted unit keeps active standing
- **WHEN** a rich unit on an active page carries `- verdict: refuted`
- **THEN** the unit remains an ordinary active unit and its parent page status is unchanged

### Requirement: Check By Is A Governed Date Unit-Metadata Key
The semantic-unit language SHALL recognize `check_by` as a governed rich unit-metadata key whose value is a strict ISO calendar date spelled `YYYY-MM-DD`. A value that is not an exact ISO calendar date, including a timestamp or an abbreviated date, SHALL produce a deterministic source-addressed error. The validated value SHALL be projected onto the parsed unit and included in its serialized form.

#### Scenario: A valid check-by date is projected
- **WHEN** a rich unit carries the metadata row `- check_by: 2026-11-01`
- **THEN** the parsed unit's check-by value is `2026-11-01` and the document reports no error

#### Scenario: A non-canonical date is rejected
- **WHEN** a rich unit carries the metadata row `- check_by: 2026-1-1`
- **THEN** the document reports an `invalid_rich_check_by` error bound to that source line

#### Scenario: A timestamp is rejected
- **WHEN** a rich unit carries the metadata row `- check_by: 2026-11-01T09:00:00Z`
- **THEN** the document reports an `invalid_rich_check_by` error

### Requirement: Governed Unit Metadata Is Rich-Form Only And Reserved
Governed unit-metadata keys SHALL be available only in the rich authoring form, because compact observations carry no metadata rows. `verdict` and `check_by` SHALL be treated as reserved metadata rows, so a rich unit whose only content is governed metadata SHALL still be reported as an empty rich unit.

#### Scenario: A metadata-only prediction is still empty
- **WHEN** a page contains `## Prediction` followed only by `- verdict: refuted` and `- check_by: 2026-11-01`
- **THEN** the document reports an `empty_rich_unit` error for that heading

#### Scenario: Compact observations carry no governed metadata
- **WHEN** a compact observation line is parsed
- **THEN** the resulting unit has no verdict and no check-by value
