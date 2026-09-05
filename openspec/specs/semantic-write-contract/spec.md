# semantic-write-contract Specification

## Purpose
TBD - created by archiving change enforce-semantic-authoring-contract. Update Purpose after archive.
## Requirements
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

### Requirement: The Prediction Kind Satisfies The Shared Write Contract
The shared semantic write contract SHALL recognize a valid `prediction` rich unit exactly as it recognizes every other governed kind. A compiled active page whose only semantic unit is a substantive `## Prediction` block SHALL satisfy the minimum-semantic-unit rule, and a governed writer SHALL accept `prediction` as an explicit kind without a registry entry.

#### Scenario: A prediction alone satisfies the minimum unit rule
- **WHEN** an active compiled page is written whose only semantic unit is a substantive `## Prediction` rich block
- **THEN** the precommit contract reports no `missing_semantic_unit` finding

#### Scenario: A governed writer accepts the prediction kind
- **WHEN** a caller adds a semantic unit through the single-unit mutation route with kind `prediction`
- **THEN** the unit is written as a rich `## Prediction` block rather than refused as an unsupported kind

### Requirement: Loop Primitives Are Exempt From Quota Logic
Neither the `prediction` kind nor the `verdict` and `check_by` metadata keys SHALL introduce, satisfy, or alter any count-based obligation beyond the existing one-unit minimum. No contract SHALL require a page to contain a prediction, require a prediction to carry a verdict, or treat a verdict as changing how many units a page needs.

#### Scenario: A page without a prediction is not penalized
- **WHEN** an active compiled page satisfies the minimum-unit rule with a non-prediction unit
- **THEN** the precommit contract reports no finding that references predictions

#### Scenario: A prediction without a verdict is valid
- **WHEN** an active compiled page contains a substantive `## Prediction` rich block carrying no `verdict` metadata row
- **THEN** the precommit contract reports no error for the missing verdict

#### Scenario: Adding a verdict does not change the unit obligation
- **WHEN** the same page is written once without and once with a `- verdict: refuted` row on its only unit
- **THEN** both writes report the same minimum-semantic-unit outcome

### Requirement: Compact mutation terminals surface unknown relation guidance

Every semantic mutation route that commits one or more explicit unregistered relation observations SHALL carry one bounded `relation_advisory` through the shared mutation terminal at default compact detail. The advisory SHALL name the normalized raw labels, registry hash, bounded occurrence evidence available without a hot-path corpus scan, and the read-only relation-resolution route. It SHALL state that the observation remains preserved but untraversed. The advisory MUST NOT block or rewrite the committed note, infer a parent or meaning, trigger a registry save, run an embedding search, or scan the vault synchronously.

#### Scenario: Unknown relation is visible after an ordinary write
- **WHEN** `remember`, `observe_memory`, `edit_memory`, or `replace_memory` commits an explicit `applies_to` observation that is not registered
- **THEN** the successful compact terminal contains a bounded relation advisory and resolution next action
- **AND** the raw edge remains unregistered until a separate reviewed registry save

#### Scenario: Portable and honest relations do not create bureaucracy
- **WHEN** a committed write uses an active registered relation, the legitimate `relates_to` fallback, or authors no edge
- **THEN** no unknown-relation advisory is added solely to encourage greater specificity

#### Scenario: Advisory projection is uniform across public surfaces
- **WHEN** equivalent semantic mutations are committed through MCP, REST, and CLI
- **THEN** the same shared terminal projection exposes or omits the relation advisory under the same conditions
- **AND** no facade performs local relation reasoning

### Requirement: Unregistered relation observations are advisory but non-qualifying

When an authored relation row resolves with registry status `unregistered`, the
semantic write evaluator SHALL emit an `unregistered_relation` warning rather
than an independent blocking error. The unresolved fact SHALL remain ineligible
for typed-edge and connectivity qualification and SHALL NOT satisfy relation
disposition. Deprecated relations and registry scope violations SHALL remain
blocking errors under their existing rules. Successful writes SHALL expose
unknown-relation guidance through the shared compact mutation terminal.

#### Scenario: A separate governed connection permits advisory feedback
- **WHEN** a compiled write contains an unregistered relation row and another
  outbound relation independently satisfies the connectivity lane
- **THEN** the evaluator reports `unregistered_relation` with warning severity
- **AND** disposition is satisfied only by the independently qualifying relation
- **AND** the unknown observation does not independently block the write

#### Scenario: An unknown-only relation remains blocked by disposition
- **WHEN** the only authored relation row uses an unregistered label
- **THEN** the evaluator reports the warning but neither typed-edge nor
  connectivity qualification accepts that fact
- **AND** unsatisfied relation disposition still blocks the write

#### Scenario: Governed registry violations remain errors
- **WHEN** a relation resolves as deprecated or outside its permitted scope
- **THEN** its existing registry finding remains an error
