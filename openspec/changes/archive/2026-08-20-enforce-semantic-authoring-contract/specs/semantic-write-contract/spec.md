## ADDED Requirements

### Requirement: Active Compiled Notes Carry A Usable Semantic Unit

Every newly created, replaced, or activated governed active compiled note SHALL
contain at least one valid, non-empty normalized semantic unit. Either a compact
observation or a rich unit SHALL satisfy the minimum. Compact form SHALL remain
the preferred lightweight form, SHALL use
`- [category] content #tags (context) ^anchor`, and SHALL be authored canonically
under `## Observations`; category SHALL remain open vocabulary and compact kind
SHALL remain `observation`. A rich unit SHALL carry its governed kind and its
existing default or explicitly authored category without requiring a duplicate
compact restatement.

#### Scenario: One valid open compact category satisfies the minimum
- **WHEN** an active insight draft contains `## Observations` followed by `- [operating constraint] Keep retries bounded #reliability`
- **THEN** minimum-unit validation passes without registering that category or inferring a non-observation kind

#### Scenario: One non-empty rich unit satisfies the minimum
- **WHEN** an active compiled draft contains a valid non-empty `## Decision` rich unit and no compact observation
- **THEN** minimum-unit validation passes and the writer neither requires nor generates duplicate compact content

#### Scenario: Prose-only active note is rejected
- **WHEN** an active compiled-note create contains only ordinary structural prose and no valid semantic unit
- **THEN** precommit emits stable error finding `missing_semantic_unit`, performs no mutation, and returns compact and rich remediation choices

#### Scenario: Empty rich unit does not satisfy the minimum
- **WHEN** an active compiled draft contains only a recognized block excluded by `empty_rich_unit`
- **THEN** precommit returns both the empty-unit finding and `missing_semantic_unit` without writing or indexing the page

#### Scenario: Inactive draft is checked on activation
- **WHEN** an inactive compiled note without semantic units is created and later transitions to active
- **THEN** inactive creation follows the existing lifecycle rules with a warning and activation is refused until the minimum is satisfied

#### Scenario: Replacement is a new active successor
- **WHEN** a grandfathered predecessor without semantic units is superseded by a new active page
- **THEN** the successor must satisfy the minimum even though the predecessor remains readable unchanged

### Requirement: Compiled Intent And Minimum-Unit Applicability Are Exact

The shared semantic write boundary SHALL define
`compiled_intent(after_state)` as exactly
`canonical_compiled_destination(path) OR normalized_type in COMPILED_TYPES`.
`COMPILED_TYPES` SHALL contain `research-note`, `insight`, `failure`, `pattern`,
`experiment`, and `production-log`. The canonical destination resolver SHALL map
those types to `Notes/Research`, `Notes/Insights`, `Notes/Failures`,
`Notes/Patterns`, `Notes/Experiments`, and `Notes/Productions`, respectively,
while applying existing index, log, schema/admin, template, dataset-card, hub,
snapshot, and activation exclusions. Structural validation SHALL reject a
canonical compiled destination with missing/wrong compiled type and SHALL reject
a recognized compiled type at a noncanonical destination before applicability is
evaluated.

The boundary SHALL then expose one deterministic
`requires_semantic_unit(after_state)` predicate, separate from relation
disposition. It SHALL be true only when compiled intent has passed that
path/type match; the result is writable Markdown inside the managed governed
subtree and outside Sources, Evidence, and trash; no existing activation
exclusion applies; and its resolved lifecycle is active rather than `draft`,
`planned`, `dropped`, `archived`, or `superseded`.

#### Scenario: Typed and Tier-2 writes agree
- **WHEN** equivalent active compiled Markdown is submitted through `remember` and through Tier-2 create at its governed compiled destination
- **THEN** both use `requires_semantic_unit` and return the same semantic-authoring findings

#### Scenario: Tier-2 overwrite and append evaluate the result
- **WHEN** Tier-2 overwrite or append would leave an applicable active compiled page with no valid unit
- **THEN** precommit evaluates the complete resulting document, refuses it, and leaves Markdown and derived state unchanged

#### Scenario: Compiled path cannot bypass with bad frontmatter
- **WHEN** Tier 2 targets a canonical compiled-note route with missing, invalid, or mismatched compiled frontmatter
- **THEN** structural validation fails before commit instead of classifying the page as arbitrary Markdown

#### Scenario: Non-compiled Tier-2 documents are exempt
- **WHEN** Tier 2 writes an index, log, schema/admin artifact, template, dataset card, hub, snapshot, Source, Evidence artifact, non-Markdown file, or arbitrary non-compiled Markdown
- **THEN** existing structural and safety rules apply and the minimum-unit predicate is false

#### Scenario: Validation is non-mutating
- **WHEN** an applicable draft with no valid unit is submitted through a creation path with `validate_only=true`
- **THEN** the response contains `missing_semantic_unit` and no page, index, log, project registration, review state, or auxiliary artifact is written

#### Scenario: Unit and relation obligations stay separate
- **WHEN** a page has a valid semantic unit but no current relation-review disposition, or has a qualifying relation but no valid unit
- **THEN** each independent obligation reports its own finding and neither satisfies the other

### Requirement: All Compiled Commit Paths Use The Shared Predicate

New active typed creation, replacement successors, Tier-2 create/overwrite/append,
adoption compilation at its commit boundary, edits that remove units, and
inactive-to-active edit transitions SHALL evaluate the same normalized after-state and
`requires_semantic_unit(after_state)`. `validate_only` SHALL return the same
findings without mutation. MCP, REST, CLI, OpenAPI-described inputs, and generated
product routes SHALL NOT implement weaker local checks.

#### Scenario: Adoption commit cannot bypass coverage
- **WHEN** an adoption proposal attempts to commit an active compiled page with no valid semantic unit
- **THEN** the shared precommit boundary refuses it with the same finding and leaves the preserved source and proposal state intact

#### Scenario: Public facades preserve the same refusal
- **WHEN** the same invalid compiled draft reaches the shared writer through MCP, REST, and CLI JSON
- **THEN** every facade preserves the same finding codes, remediation, validation state, and non-mutation result

### Requirement: Legacy And Out-Of-Band Content Is Preserved

Pages recorded as pre-existing by the semantic-contract activation boundary SHALL
remain grandfathered under the existing before/after non-worsening rules. A
guarded edit that does not worsen existing missing-unit debt MAY proceed with a
visible warning, but a post-activation compliant page SHALL NOT lose its final
valid semantic unit. A move SHALL preserve grandfathering and SHALL NOT create a
new-unit obligation solely because the path changed. Watcher and reconcile SHALL
never rewrite or delete direct-editor Markdown solely for violating the minimum
or rich-unit validity; they SHALL surface current debt and index only valid units.

#### Scenario: Grandfathered unrelated edit remains possible
- **WHEN** an unrelated guarded edit is applied to a grandfathered active page that already lacks semantic units
- **THEN** the edit may commit with visible legacy debt and does not fabricate a unit

#### Scenario: Compliant page cannot remove its final unit
- **WHEN** an in-process edit would remove the only valid semantic unit from a post-activation active compiled page
- **THEN** precommit refuses the edit with `missing_semantic_unit` and leaves Markdown and indexes unchanged

#### Scenario: Move preserves the compatibility boundary
- **WHEN** a grandfathered page is moved without semantic content change
- **THEN** it remains grandfathered and the move does not become a new active creation

#### Scenario: Direct invalid edit is non-destructive
- **WHEN** a direct editor removes the final unit or creates an empty rich unit and watcher or reconcile observes it
- **THEN** the Markdown remains byte-for-byte authored, valid remaining units are indexed, and actionable posthoc findings are surfaced

#### Scenario: Repair clears debt
- **WHEN** the page is later repaired with either a valid compact observation or a valid non-empty rich unit
- **THEN** repeated reconcile clears the corresponding findings and is otherwise idempotent

### Requirement: Compiled Templates Default To Canonical Observations

Every scaffolded documentation template for an active compiled-note type SHALL
show canonical `## Observations` and compact syntax inside a fenced example that
cannot be parsed as a unit in the template itself. Every generated active-note
candidate SHALL include `## Observations` with a deliberately non-parseable
fill-in row. Structural prose sections and rich blocks MAY remain. Examples SHALL
distinguish page tags, open observation categories, and governed rich kinds, and
SHALL explain that a valid non-empty rich unit is the alternative when rich
semantics are intended.

#### Scenario: Research-note template teaches compact authoring
- **WHEN** a client reads the shipped research-note content shape
- **THEN** it sees `## Observations` and a valid fenced `- [category] content` example before write-time relation guidance, and the example is not an indexable unit in the template source

#### Scenario: Proposal cannot look commit-ready without a unit
- **WHEN** compilation proposal generation returns an active compiled-note scaffold
- **THEN** the scaffold contains a non-parseable observation fill-in row, its handoff states that unresolved placeholders cannot be committed, and submitting it untouched fails `missing_semantic_unit`
