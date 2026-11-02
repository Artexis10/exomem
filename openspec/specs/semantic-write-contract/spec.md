# semantic-write-contract Specification

## Purpose
Require every newly created, replaced or activated active compiled note to contain at least one valid, non-empty semantic unit, enforced by one shared predicate at every compiled commit path. Legacy and out-of-band content is grandfathered, templates default to canonical observations, and relation guidance on writes is advisory.

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
exclusion applies; and its admitted effective page status has class `live`. A canonical shipped
label resolves independently of private extensions. An unfamiliar label requires
admitted registry classification; unknown labels remain live with debt.
Absent or empty status retains the public live default without overlay admission or debt.
Unavailable classification SHALL refuse dependent validation and SHALL NOT be
interpreted as an inactive page or a false minimum-unit obligation.

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

#### Scenario: Registered pending labels share the inactive minimum rule
- **WHEN** a normalized compiled after-state uses an admitted status label with class `pending`
- **THEN** its minimum-unit applicability is false under the shared predicate
- **AND** existing structural validation and route-specific field rules remain in effect

#### Scenario: Unavailable extension classification cannot approve a dependent write
- **WHEN** an unfamiliar label requires private status definitions that the caller cannot admit
- **THEN** dependent semantic validation reports classification unavailable and cannot approve the mutation
- **AND** independent operations without that dependency remain usable

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

### Requirement: Observe-Memory Refusals Distinguish Legacy Schema From Policy

When `observe_memory` refuses a target because it is not a writable compiled
page, it SHALL keep the existing refusal code
(`OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE`, `FRONTMATTER_REQUIRED` for a
frontmatterless target, or `OUTSIDE_GOVERNED_ROOT` for a target whose real
page lives outside the governed Knowledge Base root) and SHALL distinguish
the cause in the accompanying `remediation` and `resolution` fields rather
than leaving the caller to improvise:

- An untyped page (frontmatter present, `type` missing or not in
  `COMPILED_TYPES`), a frontmatterless page, or a page outside the governed
  Knowledge Base root SHALL receive a `remediation` sentence that names the
  concrete safe move — the exact `suggested_path`, the `part_of` target, the
  in-place alternative, and the migration pointer, in that order — plus a
  structured `resolution` of shape `{"action": "create-dated-child",
  "suggested_path": <string>, "link": {"relation": "part_of", "target":
  <legacy page path>}, "in_place": <string>, "migration": <string>}`.
  `suggested_path` SHALL colocate in the legacy page's own directory when
  that page is under the governed Knowledge Base root, and SHALL otherwise
  resolve under `Notes/Research/<parent-dir-slug>/` inside the Knowledge
  Base. `suggested_path` SHALL name a path that does not already exist,
  de-collided the same way page creation de-collides a filename.
- A target whose access tier is not read-write (readonly, excluded, or any
  other non-read-write tier) SHALL receive the same code, a `remediation` that
  names the tier and states the page is policy-protected, and no `resolution`.
- `_COMPILED_PAGE_TYPES` SHALL NOT be widened by this requirement. `entity`
  remains structural, not compiled.
- No existing error code SHALL be renamed, and no new error code SHALL be
  introduced to carry this distinction.

#### Scenario: Untyped page inside the Knowledge Base offers a dated-child resolution
- **WHEN** `observe_memory` targets a page with frontmatter but no compiled
  `type`, under `Knowledge Base/`
- **THEN** it refuses with `OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE`
- **AND** the raised error's `remediation` sentence names the exact
  `suggested_path` and the `part_of` target, and its `resolution.suggested_path`
  is a dated child in the same directory with `link` targeting the legacy
  page at relation `part_of`

#### Scenario: Frontmatterless page gets the same shape of answer
- **WHEN** `observe_memory` targets a page with no frontmatter delimiters
- **THEN** it refuses with `FRONTMATTER_REQUIRED`
- **AND** the error carries the same shape of `remediation` and `resolution`
  as an untyped page, without synthesizing frontmatter on the page itself

#### Scenario: A page outside the Knowledge Base root gets the same shape of answer
- **WHEN** `observe_memory` targets a page whose real file lives outside the
  governed Knowledge Base root
- **THEN** it refuses with `OUTSIDE_GOVERNED_ROOT`
- **AND** the error carries the same shape of `remediation` and `resolution`
  as an untyped page, with `resolution.suggested_path` under
  `Notes/Research/<parent-dir-slug>/` inside the Knowledge Base and `link`
  targeting the outside page at relation `part_of`

#### Scenario: Policy-protected tier gets tier remediation and no resolution
- **WHEN** `observe_memory` targets a page whose access tier is `readonly` or
  `excluded`
- **THEN** it refuses with `OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE`
- **AND** the `remediation` names the tier and states the page is
  policy-protected
- **AND** no `resolution` is present

#### Scenario: A pre-existing same-dated child shifts the suggested path
- **WHEN** a page already exists at the path `suggested_path` would otherwise
  name
- **THEN** the offered `suggested_path` is de-collided (the `-2` form, or
  further, exactly as ordinary page creation de-collides a filename) rather
  than naming a path that already exists

#### Scenario: Compiled-page type set is not widened
- **WHEN** this requirement's resolution logic runs
- **THEN** `_COMPILED_PAGE_TYPES` (`semantic-write-contract`'s
  `COMPILED_TYPES`) is unchanged and `entity` remains excluded from it

### Requirement: Lifecycle Classification Does Not Pollute Shared Structural Evidence

The shared semantic cache SHALL retain neutral parsed structure and complete stable identity evidence.
A request SHALL admit each page before enriching its lifecycle facts and SHALL NOT publish those facts into the shared structural cache.
A caller who admits every page MAY reuse its enrichment through a memo bound to the structural entry and to the status dependency it consulted; a restricted caller SHALL NOT read or write that memo.
A stored manifest exemption SHALL require admitted target eligibility and complete identity uniqueness evidence.
Explicit request paths SHALL NOT infer an activation census from missing identity evidence.
The activation census SHALL come from the complete structural corpus, independent of the caller, and SHALL record each eligible page's raw status label without classifying it. It SHALL NOT be served.
A write against an absent manifest SHALL NOT be refused for lack of complete-view authority; its result SHALL depend only on the admitted target.
A grandfathering exemption SHALL classify the target's recorded label with the caller's admitted basis.
Manifest preparation SHALL record the owner's class at activation for each recorded label, classified by the owner-local producer independent of the caller. A grandfathering check SHALL classify the recorded label with the caller's basis before it reads that class, and SHALL refuse the exemption when the class at activation is not live. A label with no available class at preparation SHALL be classified at check time.
Validity-token reuse SHALL require fresh complete-view admission and current status dependencies; otherwise existing preflight revalidation SHALL run.

#### Scenario: Hidden status cannot poison a shared semantic cache
- **WHEN** an owner warms the structural cache and a restricted writer validates an admitted page beside a hidden unfamiliar status
- **THEN** the hidden label is not classified and no caller-derived lifecycle fact enters the shared cache
- **AND** the complete identity census retains its existing uniqueness obligations

#### Scenario: Restricted first write uses the complete neutral census
- **WHEN** a restricted writer validates an admitted page while the activation manifest is absent
- **THEN** the write's outcome is the same with or without hidden pages
- **AND** the prepared manifest describes the complete corpus, not the writer's view

#### Scenario: A later label redirect cannot grandfather a page that was pending at activation
- **WHEN** a page carries a pending label when the manifest is prepared, and the owner later redirects that label to a live one
- **THEN** the page is not grandfathered, and its next active edit needs a valid semantic unit

#### Scenario: A denied caller cannot learn the class the owner recorded at activation
- **WHEN** a caller without the owner's status definitions edits a page whose recorded label the owner's overlay classed pending, classed live, or did not define at activation
- **THEN** the caller gets the same unavailable result in each case

#### Scenario: Hidden duplicate identity cannot inherit grandfathering
- **WHEN** an admitted target shares its stable identity with another canonical page in the complete identity census
- **THEN** the target cannot claim the stored manifest's stable-identity exemption

### Requirement: Shared Semantic Contract Boundary
One pure semantic-contract evaluator SHALL accept parsed before/after state plus operation, lifecycle, registry, resolved contracts, and review state. In-process mutation paths SHALL invoke it in `precommit` mode and block on applicable errors; watcher/reconcile SHALL invoke it in `posthoc` mode, preserve externally authored Markdown, and surface drift. Every post-commit index consumer SHALL use the same parsed result. No surface or writer SHALL implement an independent weaker parser or validator.

#### Scenario: Equivalent writes receive equivalent findings
- **WHEN** the same resulting Markdown is proposed through two different in-process write paths
- **THEN** both receive the same semantic-unit, category, relation, and schema findings

#### Scenario: In-process error prevents mutation
- **WHEN** shared contract evaluation returns an error for an in-process write
- **THEN** no target Markdown or derived index is mutated

#### Scenario: Posthoc error preserves external Markdown
- **WHEN** the watcher evaluates an externally edited file with contract errors
- **THEN** it records the same error keys without reverting, deleting, or rewriting the file

### Requirement: Contract Applicability Is Lifecycle-Specific
Active governed compiled create/replacement, active Tier-2 create/overwrite/append under compiled-memory paths, and active adoption compile SHALL receive full precommit syntax, saved-schema, and relation-disposition enforcement. An inactive governed compiled create SHALL receive structural and saved-schema precommit only, SHALL NOT join the active relation corpus, and SHALL NOT receive reviewed-none or bootstrap state. Activation of that page SHALL re-enter the full current contract before the primary update. `draft`, `planned`, `dropped`, and `archived` SHALL be inactive; `planned` is the production-log outline phase. Replacements SHALL produce active successors. Governed compiled edit/observe SHALL receive precommit evaluation with grandfathering where applicable. Other governed-KB Tier-2 documents SHALL receive structural/safety checks only. Move SHALL build the final corpus and reevaluate the deterministic dependency-affected closure, including unchanged pages whose relation resolution or disposition changes, before refreshing identity/index state. Delete/trash SHALL skip departing-content validation while applying existing inbound/lifecycle guards and derived cleanup. Sources/Evidence SHALL never receive mutation-time semantic enforcement, though read-only parsing/indexing MAY expose valid units as raw-parent observations. Watcher/reconcile SHALL be posthoc and nonblocking.

#### Scenario: Inactive draft defers relation disposition
- **WHEN** a newly created compiled page has an inactive lifecycle status
- **THEN** structural and saved-schema precommit run without reviewed-none or bootstrap state
- **AND** a later transition to an active status requires the full current relation disposition before the active primary update

#### Scenario: Planned production log is inactive
- **WHEN** a production log is created in its `planned` outline phase
- **THEN** it is excluded from the active compiled corpus until it transitions to `recorded`, `edited`, `published`, or `reflected`

#### Scenario: Source observations remain read-only evidence
- **WHEN** a Source contains valid compact observation syntax
- **THEN** read-only parsing may expose it with raw-parent/provenance status but `observe_memory` and compiled-page enforcement do not mutate it

#### Scenario: Delete does not deadlock on departing debt
- **WHEN** a governed page with schema debt is deleted or trashed through an allowed lifecycle path
- **THEN** content validation does not block departure, while inbound guards and complete index cleanup still apply

### Requirement: Current Relation Review Disposition

Every newly created active governed compiled page SHALL have a current relation-review
disposition satisfied by a qualifying typed inbound/outbound relation, a qualifying
outbound connectivity signal, an explicit reviewed-none decision bound to current page
identity/content, or an automatic no-candidates bootstrap disposition only for a genuinely
empty governed corpus.

A relation qualifies as a **typed edge** only when it is authored or explicitly
reviewer-accepted, its target resolves unambiguously to an eligible governed page at
evaluation time, its canonical registry entry is active and scope-valid, is not marked `generic: true`, its registry
family is not `link`, `citation`, `derivation`, `evidence`, `mention`, `observation`, or
`provenance`, and either (a) its origin is `markdown_relation`, `semantic_relation`, or
`semantic_block`, or (b) its origin is `frontmatter` and its registered family is exactly
`supersession`. The sole inactive-target exception is a canonical supersession edge to the
exact governed predecessor now marked `superseded` whose `superseded_by` resolves back to
the active successor.

When no typed edge qualifies, the disposition MAY be satisfied by an **outbound
connectivity signal**: an authored outbound connection whose target resolves unambiguously
to a connectable governed page, whose canonical registry entry is active and scope-valid,
and whose origin is `wikilink`, `markdown_relation`, `semantic_relation`, or
`semantic_block`, irrespective of registry family. A resolved outbound body wikilink SHALL
also satisfy this lane by normalized membership in the connectable target set without
requiring a relation fact. The connectable set SHALL be the eligible governed set widened
to admit append-only `Sources/` material, and SHALL be computed separately from the
eligible governed set so that the empty-corpus bootstrap disposition is unaffected by
captured sources.

Frontmatter origin SHALL NOT satisfy connectivity. This keeps the disposition aligned with
the relation-debt measurement, which clears a page on authored relation rows or body
wikilinks and ignores `sources:`. Provenance is a vertical edge to raw material and carries
no claim about how a conclusion relates to other conclusions; because every
adoption-compiled note cites a source by construction, counting it would make the
disposition a no-op for exactly the bulk-import case that most needs review.

Inbound edges MUST NOT satisfy connectivity. Unresolved or ambiguous forward targets MUST
NOT satisfy either lane. A disposition satisfied by connectivity SHALL report that signal
distinctly from a typed edge and SHALL emit a non-blocking typed-edge-absent warning, except when the authored-all-generic advisory already describes that pattern. The
disposition SHALL carry any reviewed-none reason and reference it was satisfied by, so
review decisions are observable rather than write-only. No minimum relation count SHALL be
imposed in either lane.

#### Scenario: Superseded predecessor remains a qualifying target
- **WHEN** an active successor canonically supersedes a governed predecessor and the predecessor is marked `superseded` with an exact backlink to that successor
- **THEN** the supersession edge remains qualifying after commit and during exact prepared recovery
- **AND** no other inactive target receives this exception

#### Scenario: Typed relation satisfies disposition
- **WHEN** a new compiled page has a registered non-generic canonical relation to an existing page
- **THEN** its relation disposition is satisfied and reports the typed edge
- **AND** no typed-edge-absent warning is emitted

#### Scenario: Reviewed none is fingerprint-bound
- **WHEN** a reviewer records that a page has no qualifying relation candidates
- **THEN** the disposition is satisfied only while the page identity and relevant content fingerprint remain current
- **AND** a material change resurfaces relation review
- **AND** the recorded reason is reported on the disposition rather than discarded

#### Scenario: Empty vault bootstraps without fake edge
- **WHEN** the first compiled page is created in a genuinely empty governed corpus and no target can exist
- **THEN** the writer records an automatic bootstrap disposition without fabricating a relation or placeholder

#### Scenario: Captured source does not consume the bootstrap exception
- **WHEN** a vault contains only captured `Sources/` material and the first compiled page is created
- **THEN** the page still receives the automatic bootstrap disposition
- **AND** the connectable target set does not widen the eligible governed set used to detect an empty corpus

#### Scenario: Bootstrap exception expires when a candidate can exist
- **WHEN** a second eligible compiled page is added after the first page received an automatic bootstrap disposition
- **THEN** the first page's bootstrap disposition becomes stale and enters ordinary relation review

#### Scenario: Excluded-family relation satisfies connectivity without a typed edge
- **WHEN** an active compiled page carries no qualifying typed relation but one or more authored relation rows of an excluded family resolving to connectable governed pages
- **THEN** its relation disposition is satisfied and reports the connectivity signal rather than a typed edge
- **AND** a typed-edge-absent warning is emitted at warning severity
- **AND** the write is not blocked

#### Scenario: Resolved body wikilink satisfies connectivity
- **WHEN** an active compiled page carries only an outbound body wikilink that resolves unambiguously to a connectable governed page
- **THEN** the disposition is satisfied and reports the connectivity signal rather than a typed edge
- **AND** a typed-edge-absent warning is emitted at warning severity
- **AND** the write is not blocked

#### Scenario: Cited provenance alone does not satisfy the disposition
- **WHEN** an active compiled page carries no typed relation and no body wikilink, but names existing `Sources/` pages in its `sources:` frontmatter
- **THEN** the disposition remains unsatisfied and enters ordinary relation review
- **AND** an adoption-compiled page, which always cites a source, still receives relation review

#### Scenario: A body wikilink to a Source does satisfy connectivity
- **WHEN** an active compiled page links a `Sources/` page from its body rather than only from frontmatter
- **THEN** the disposition is satisfied by the connectivity signal
- **AND** the target is not required to be an eligible governed page

#### Scenario: Inbound links and provenance back-references never satisfy connectivity
- **WHEN** a compiled page has no outbound connection of any kind, but a `Sources/` page links to it and its own `ingested_into:` back-reference names it
- **THEN** the disposition remains unsatisfied and enters ordinary relation review
- **AND** automatically written back-references cannot make the disposition vacuous

#### Scenario: Excluded-family relation does not qualify as a typed edge but may connect
- **WHEN** a page's only relation is in the `citation`, `evidence`, or `link` family and resolves to a connectable governed page
- **THEN** it does not qualify as a typed edge
- **AND** it satisfies the disposition as connectivity, reported distinctly, with a typed-edge-absent warning

#### Scenario: Inactive relation does not qualify in either lane
- **WHEN** a page has only a relation whose canonical registry entry is inactive or out of scope
- **THEN** the edge remains visible in its own semantics and the disposition remains unsatisfied

#### Scenario: Unresolved forward target does not qualify
- **WHEN** a registered authored relation points to a missing or ambiguous target
- **THEN** the relation remains visible with its resolution finding but does not satisfy relation review
- **AND** it does not satisfy connectivity

#### Scenario: Self wikilink does not qualify
- **WHEN** a page's only outbound body wikilink resolves back to that same page
- **THEN** the disposition remains unsatisfied and enters ordinary relation review
- **AND** the self-link does not satisfy connectivity

#### Scenario: Reviewed-none is not applicable to a connected page
- **WHEN** a caller submits a reviewed-none decision for a page that satisfies the disposition through connectivity
- **THEN** the write reports the decision as not applicable and names the connectivity satisfaction
- **AND** the page commits without a review artifact

#### Scenario: Generic links do not satisfy typed relation review
- **WHEN** a page contains only inline wikilinks, `links_to`, or source/evidence provenance
- **THEN** those connections do not qualify as typed epistemic edges
- **AND** an eligible resolved outbound connection may satisfy the separate connectivity lane, while frontmatter provenance alone does not

#### Scenario: Inactive or excluded-family relation does not qualify
- **WHEN** a page has only an inactive relation or a relation in the `citation`, `evidence`, or `link` family
- **THEN** the edge remains visible in its own semantics but does not qualify as a typed edge
- **AND** an active scope-valid excluded-family relation to a connectable target may satisfy connectivity, while an inactive relation satisfies neither lane

#### Scenario: Supersession frontmatter qualifies narrowly
- **WHEN** a registered active scope-valid supersession edge is authored through the supported frontmatter lifecycle field
- **THEN** it qualifies without making other frontmatter links relation-review eligible

#### Scenario: Inactive body wikilink target does not connect
- **WHEN** a compiled page's only outbound body wikilink resolves to an inactive page
- **THEN** that link does not satisfy connectivity or qualify as a typed edge

#### Scenario: A registry-marked generic relation is connectivity only
- **WHEN** a page authors an active scope-valid relation marked generic in the registry to a connectable governed page
- **THEN** that relation does not qualify as a typed edge, regardless of its registry family
- **AND** it remains writable connectivity with non-blocking guidance

#### Scenario: A specific extension remains typed
- **WHEN** an authored relation resolves to an active non-generic extension and meets the other typed-edge conditions
- **THEN** it qualifies as a typed edge even when its registry parent is generic

### Requirement: Authored all-generic relations receive accurate non-blocking feedback

When admitted authored explicit relations are present and all are registry-marked generic, semantic evaluation SHALL emit one bounded non-blocking advisory.
It SHALL ask the active agent to review specific registered meaning while allowing honest generic relations or no edge.
It SHALL describe only that authored pattern, even when a qualifying incoming specific relation satisfies disposition.
It SHALL suppress the duplicate typed-edge-absent warning for that pattern without changing incoming-specific qualification.
It SHALL NOT infer meaning, force a predicate, impose an output count or refuse a write.
Default compact mutation feedback SHALL retain the advisory and an actionable relation-resolution route.

#### Scenario: Incoming specificity does not conceal generic authorship
- **WHEN** a page authors only admitted registered generic relations and also has a qualifying incoming specific relation
- **THEN** disposition remains satisfied by that incoming relation and the authored-all-generic advisory is visible
- **AND** the advisory does not claim that no typed relation exists

#### Scenario: Specific authorship does not receive the all-generic advisory
- **WHEN** an admitted authored explicit relation is specific rather than registry-marked generic
- **THEN** the all-generic advisory is absent

#### Scenario: Honest generic edges remain writable
- **WHEN** an otherwise valid write authors only admitted generic relations
- **THEN** the write may commit with the advisory and no specificity refusal

### Requirement: Reviewed-None Creation Is Logically Atomic, Crash-Recoverable, And Content-Bound
Creation-capable governed writers SHALL support `validate_only=true`. Validation SHALL preassign a candidate page UUID and return `draft_id`, `draft_hash`, a bounded opaque `draft_token` for server-derived render inputs and ordered project auto-registration intent, deterministic relation findings/candidates, and no filesystem mutation. A reviewed-none commit SHALL repeat identical proposed content and supply the unused `draft_id`, echoed `draft_token`, `relation_disposition="reviewed_none"`, matching `relation_review_hash`, and a non-empty `relation_review_reason`. The writer SHALL recalculate the hash over candidate identity plus normalized final content/destination and reject mismatch/reuse. A bounded portable creation receipt SHALL be prepared for every full active-compiled commit before its deterministic auxiliary writes, and the primary Markdown SHALL be replaced last as the logical commit marker, so a visible page never lacks required review state. Reviewed-none/bootstrap receipts SHALL also carry their disposition; a qualifying-relation receipt SHALL be transaction/recovery state only. Portable state SHALL live in governed Markdown/frontmatter or a vault-managed governed artifact in the same creation batch, not solely in a derived database, and SHALL survive index rebuild or vault transfer. Exact recovery SHALL reconstruct the same ordered auxiliary target/content digest, include already-applied expected targets unchanged, bind every existing leaf to its exact safely read content, and reject unrelated drift rather than overwrite it. An exact existing qualifying primary without an artifact SHALL remain an idempotent already-committed case after upgrade, while a page-less no-artifact attempt has no retroactive prepared-recovery guarantee.

The reviewed-none creation batch SHALL use the shared descriptor-owned atomic
batch primitive and SHALL preserve auxiliary-first, primary-last logical commit
ordering. Abrupt process death MAY leave page-less prepared state or private
transaction-workspace residue. Workspace residue SHALL never be recovery
authority. An exact unchanged retry SHALL reconstruct fresh stages from the
current guarded inputs and SHALL finish the same prepared commit when every
bound primary and auxiliary target is in its exact expected pre-write or
already-applied state. Any mismatch SHALL fail closed and remain reserved for
explicit audit/cleanup. This protocol SHALL NOT claim cross-file all-or-none
power-loss atomicity. A qualifying typed relation or genuinely empty-corpus
bootstrap SHALL not require a reviewed-none decision from the caller.

Review fields SHALL be checked against whether the page needs a relation review, with one check shared by validation and commit. When the page needs no review (a qualifying relation, connectivity signal or empty-corpus bootstrap), a commit that carries `relation_disposition`, `relation_review_hash` or `relation_review_reason` SHALL drop them, commit without a review artifact, and return one warning that names the review fields it dropped When the page needs a review, a commit that omits any of the three fields SHALL be refused with `INVALID_RELATION_REVIEW`, whose `missing` list names each absent field and whose message says to copy `relation_review_hash` from the `validate_only` result. Validation SHALL NOT compare the hash. When the caller supplied any review field, validation SHALL report the fields a commit would find missing or ignored under `relation_review_fields`.

#### Scenario: Second disconnected page can be created after review
- **WHEN** validation for a second page returns no qualifying candidate and the commit repeats the draft with matching reviewed-none fields
- **THEN** portable review state is prepared before the page commit marker, and the returned visible page has a current disposition

#### Scenario: Interrupted prepared commit resumes exactly
- **WHEN** a process stops after portable review state or auxiliaries are replaced but before the primary page commit marker, and the caller retries the exact unchanged draft, reviewed disposition/reason, and auxiliary content
- **THEN** the writer reuses the matching prepared state, completes the primary page last, and rejects any non-exact draft from reusing that identity

#### Scenario: Changed draft cannot reuse review
- **WHEN** content, identity, or relation-relevant metadata differs after validation
- **THEN** the reviewed-none commit fails before mutation and requires a fresh validation hash

#### Scenario: Validation does not reserve a conflicting file
- **WHEN** `validate_only=true` returns a draft identity
- **THEN** no Markdown is created, and commit rejects the draft if that identity or destination became occupied

#### Scenario: Review disposition survives derived-state loss
- **WHEN** all rebuildable indexes/databases are removed and the vault is rebuilt
- **THEN** a still-current portable reviewed-none disposition is recovered from governed vault content

#### Scenario: A page that needs no review ignores the review fields
- **WHEN** a commit for a page with a qualifying connectivity signal carries `relation_disposition="reviewed_none"` and a reason but no hash
- **THEN** the page commits with a receipt of kind `qualifying` and no review artifact
- **AND** the result carries one warning that the review fields were ignored

#### Scenario: A retry that re-sends dropped review fields reports the page as already committed
- **WHEN** a caller repeats the commit of a page that needed no review, with the same review fields it sent the first time
- **THEN** the write is refused with `DRAFT_ALREADY_COMMITTED`, as a repeat without the fields is

#### Scenario: A page that needs review names the missing hash
- **WHEN** a commit for a page that needs review carries the disposition and reason but no `relation_review_hash`
- **THEN** the write is refused with `INVALID_RELATION_REVIEW` and `missing=["relation_review_hash"]`
- **AND** the message tells the caller to copy the hash from the `validate_only` result

#### Scenario: Validation names fields a commit would find missing or ignored
- **WHEN** `validate_only` carries a review field for a page that needs review but omits the hash
- **THEN** `relation_review_fields.missing` lists `relation_review_hash`
- **AND** the same call for a page that needs no review lists the supplied fields under `relation_review_fields.ignored`

### Requirement: Existing-Page Review Decisions And Transition Recovery Are Separate
For an existing page with a unique valid stable memory UUID, a reviewed-none lifecycle decision SHALL be an immutable portable artifact at `Knowledge Base/_Schema/relation-reviews/lifecycle/<uuid>/<after-fingerprint>.json`. Its canonical bounded schema SHALL contain the schema/kind, normalized UUID, exact resulting review-content fingerprint, bounded non-empty reason, and a decision hash derived only from those stable decision fields. Its identity SHALL NOT depend on operation, prior bytes/path, transition token, log/index plan, or auxiliary digest. Transition-specific crash recovery SHALL use one separately guarded replaceable slot at `Knowledge Base/_Schema/relation-reviews/lifecycle/<uuid>/prepared.json`. Its canonical bounded schema SHALL contain the schema/kind, canonical transition UUID, operation, normalized page UUID, exact before/after paths and source hashes, after fingerprint, decision reference plus exact decision-bytes hash or JSON null when no decision is needed, transition-token hash, ordered auxiliary digest, and optional `carried_from` decision reference. The prepared slot SHALL NOT itself satisfy relation review. A current reviewed-none disposition SHALL require an exact unique UUID plus current-fingerprint decision match.

Lifecycle decisions and prepared slots SHALL NOT be created or loaded for path-only legacy identities. A legacy page requiring reviewed-none SHALL first acquire a stable UUID through the explicit backfill workflow. Decision lookup SHALL be direct by UUID/current fingerprint, alias/symlink/reparse safe, and bounded to 256 conforming decision files per UUID; overflow or unsafe/malformed exact lifecycle state SHALL fail closed with governed cleanup guidance. The immutable 6D v1/v2 receipt at `relation-reviews/<uuid>.json` SHALL NOT be overwritten, deleted, reinterpreted, or migrated. Any conforming lifecycle artifact SHALL reserve its UUID against later creation even when the page is absent and no creation receipt exists; unsafe or malformed lifecycle state at that exact UUID SHALL likewise fail creation closed as identity-in-use. Trash/delete SHALL preserve that history. Recovery MAY replace the last committed prepared slot only when the trash sidecar, original path, UUID, and trashed bytes exactly prove its committed after state and no live page owns the UUID. Reconcile SHALL preserve and report that `trashed_committed` proof and SHALL remove only a neither-side prepared slot with no exact trash proof.

Lifecycle decisions and prepared slots SHALL inherit operational admission only from their exact admitted primary UUID and transition. Both current and proposed primary images SHALL remain admitted. Auxiliary path and reference restrictions and access exclusions SHALL still apply. Publication SHALL bind exact generated lifecycle paths, bytes, hashes, and guards through the existing sealed auxiliary manifest. Creation receipts, operation logs, history, indexes, and backreferences SHALL retain independent admission.

The writer SHALL establish admission before inspecting required prepared slots, requested decisions, or decisions referenced by admitted prepared slots. Denied absent and present targets SHALL produce the same content-free refusal. Directory enumeration SHALL filter denied names before leaf inspection, alias checks, encoding, residue classification, and capacity counts. Forward rechecks SHALL preserve the captured admitted census and detect source, permission, directory, or leaf changes.

For a restricted caller, the 256-decision bound SHALL count only admitted lifecycle decisions. Denied siblings SHALL affect neither advice nor transition outcomes, including when physical directories exceed the admitted decision or residue bounds. Unrestricted callers SHALL retain the 256-decision bound and all unsafe-state checks. Publication entry, publication completion, and committed replay SHALL recheck primary admission and source bindings.

After an ordinary publication failure, rollback SHALL restore only exact unchanged installed artifacts through retained full ancestry and directory identities. Rollback SHALL NOT invoke live caller admission or enumerate unrelated lifecycle siblings. Each inverse attempt and retry SHALL prove installed content at the maintained operation boundary. An intervening owned edit or namespace substitution SHALL remain untouched and produce an explicit incomplete rollback. An unrelated new sibling SHALL remain untouched without preventing restoration of unchanged owned artifacts.

#### Scenario: Restricted stable-identity edit retains operational state
- **WHEN** a restricted caller edits an admitted compiled page with a stable UUID
- **THEN** exact bound lifecycle artifacts are admitted without granting the enclosing schema directory or unrelated auxiliary writes

#### Scenario: Denied lifecycle siblings do not change an admitted transition
- **WHEN** two otherwise identical edits differ only in denied lifecycle siblings, including siblings beyond physical history or residue bounds
- **THEN** both edits have the same advice and transition outcome while admitted history still obeys its bound

#### Scenario: Required lifecycle denial precedes existence
- **WHEN** a required prepared slot or decision is denied, whether absent or present
- **THEN** the writer returns the same content-free refusal before inspecting that target

#### Scenario: Revocation after publication restores owned lifecycle state
- **WHEN** permission is revoked after the primary replacement and completion validation refuses the transition
- **THEN** rollback restores unchanged primary bytes and removes newly installed lifecycle artifacts without inspecting revoked sibling state

#### Scenario: Concurrent owned edit survives rollback
- **WHEN** an outside writer changes installed bytes or ancestry before an inverse operation or retry
- **THEN** rollback preserves that state and reports an explicit incomplete outcome

#### Scenario: Inactive page activates with reviewed-none
- **WHEN** an inactive stable-ID page is validated and committed into an active state with an exact reviewed-none decision
- **THEN** the immutable decision and prepared transition are installed before the active primary becomes visible

#### Scenario: Pending transition does not review the before state
- **WHEN** an `A -> B` prepared transition exists but the current primary still exactly matches `A`
- **THEN** the decision for `B` does not satisfy `A`, and a different transition is refused until the exact pending transition is retried or reconciled

#### Scenario: Interrupted existing-page transition resumes exactly
- **WHEN** a process stops after preparing the decision, transition slot, or deterministic auxiliaries and an exact retry repeats the same bound transition
- **THEN** the writer adopts the prepared slot, commits the resulting primary last, and rejects token, reason, content, path, or auxiliary drift

#### Scenario: Committed replay is mutation-free
- **WHEN** an exact retry finds that the current primary already matches the prepared transition's after state
- **THEN** it returns the existing committed outcome without writing the primary, decision, slot, log, or indexes again

#### Scenario: Prior reviewed fingerprint is reusable across transitions
- **WHEN** a page follows `A -> B`, later changes to `C`, and then returns exactly to reviewed fingerprint `B`
- **THEN** the immutable `B` decision is reused while a distinct `C -> B` prepared transition binds the new before state and auxiliaries

#### Scenario: Changed content cannot reuse lifecycle review
- **WHEN** the proposed resulting fingerprint differs from every exact immutable decision for the page UUID
- **THEN** a prior reviewed-none decision cannot satisfy it and fresh review is required before mutation

#### Scenario: Stale prepared slot is cleaned only without recovery proof
- **WHEN** current live bytes match neither side of a prepared slot
- **THEN** dry-run reconcile reports the state, and write-mode reconcile may guarded-remove it only when no exact trash-sidecar/UUID/bytes proof establishes `trashed_committed`

#### Scenario: Deleted identity cannot be stolen
- **WHEN** a stable-ID page is trashed or deleted while conforming lifecycle state remains and a new creation attempts to reuse that UUID
- **THEN** creation fails as identity-in-use, while exact guarded recovery of the original trashed bytes remains allowed

### Requirement: Canonical Relation Authoring And Validation
New note-level typed relations SHALL be authored as one registered lower-snake-case relation and one wikilink per bullet under `## Relations`. An empty section SHALL be structurally valid only when another current disposition satisfies relation review. Placeholder bullets such as `- (none yet)`, malformed bullets, and unregistered semantically inert labels MUST NOT satisfy the disposition.

#### Scenario: Placeholder relation is rejected
- **WHEN** a write proposes `## Relations` containing `- (none yet)`
- **THEN** the contract returns a malformed-relation error and no typed edge

#### Scenario: Empty section with reviewed-none is valid
- **WHEN** a page has an empty canonical Relations section and a current reviewed-none disposition
- **THEN** relation structure and disposition both pass

### Requirement: Existing Corpus Is Grandfathered Without Silent Worsening
Existing compiled pages lacking the new relation/category contract SHALL remain readable and editable under a migration warning policy and SHALL enter activation/review queues instead of being bulk rewritten. Each error finding SHALL have stable key `(code, governed_element_identity, resolved_rule)`. An in-process edit MAY preserve pre-existing error debt only when its after-error key set is a subset of its before-error key set and it does not invalidate a current accepted disposition. Any new error key SHALL block. Replacements SHALL be treated as new compiled conclusions.

On first capability activation, the system SHALL create one portable vault-managed activation manifest that records the current contract version and identities/source hashes of pre-existing governed compiled pages without rewriting those pages. Stable memory IDs SHALL be preferred; legacy pages SHALL use an explicitly move-unstable path+source-hash fallback. Pages first seen after that activation boundary SHALL be new pages, including out-of-band creations, and SHALL receive current-contract posthoc findings when they bypass an in-process writer.

Validate-only and preliminary blockers SHALL NOT create the activation manifest. When the manifest is missing, a real first commit SHALL atomically install or observe the immutable activation-boundary winner after preliminary validation and SHALL then repeat evaluation before any other mutation. If the installed race winner changes membership and repeated evaluation blocks, that immutable manifest SHALL be the sole side effect.

#### Scenario: Legacy debt does not block unrelated safe edit
- **WHEN** an existing grandfathered page lacks typed relations and receives an unrelated edit that preserves its prior semantic state
- **THEN** the write succeeds with migration/review warnings

#### Scenario: Edit cannot introduce malformed observation
- **WHEN** an edit to a grandfathered page introduces malformed observation syntax
- **THEN** the in-process edit is rejected before mutation

#### Scenario: Mechanical worsening is set-based
- **WHEN** a grandfathered edit retains all old error keys but adds one new schema error key
- **THEN** the edit is rejected even if the total error count is unchanged

#### Scenario: Repair may reduce legacy debt incrementally
- **WHEN** a grandfathered edit removes one old error and adds none
- **THEN** the edit may proceed with warnings for the remaining before-existing errors

#### Scenario: Existing-vault activation is portable and nonrewriting
- **WHEN** the capability first opens a non-empty vault
- **THEN** it records existing-page baselines in one governed activation manifest, does not edit the pages, and consistently distinguishes later creations after rebuild/transfer

#### Scenario: Empty vault has no grandfathered page
- **WHEN** activation occurs with no governed compiled pages
- **THEN** the manifest records an empty baseline and the first later page uses the narrow bootstrap disposition

#### Scenario: Replacement follows current contract
- **WHEN** a legacy page is superseded by a newly written compiled conclusion
- **THEN** the successor must satisfy the current semantic and relation contract

### Requirement: Saved Schema Resolution And Writer Modes
Saved memory contracts SHALL cover frontmatter fields, rich kinds, categories, and typed relations and SHALL declare `validation: off|warn|strict`, default `warn`. Resolution SHALL consider all project keys on a page and operate per governed rule. Highest specificity wins in order: exact project+page type, project, page type, global. At equal highest specificity, identical rules collapse and compatible set constraints apply conjunctively; incompatible scalar values, unequal validation modes, or an empty allowed-set intersection SHALL produce an explicit named conflict before an in-process write.

#### Scenario: Warn mode returns findings and writes
- **WHEN** a proposed write violates a resolved warn-mode contract
- **THEN** the write succeeds and returns span/path-addressed findings

#### Scenario: Strict mode blocks an in-process writer
- **WHEN** a proposed in-process create, edit, replace, or observe operation violates a resolved strict contract
- **THEN** the operation fails before filesystem mutation with shared structured errors

#### Scenario: Conflicting specific contracts fail closed
- **WHEN** two incompatible contracts resolve at equal highest specificity
- **THEN** an in-process strict write fails with the conflicting contract names and remediation

#### Scenario: Multiple project contracts resolve per rule
- **WHEN** a page belongs to two projects whose equal-specificity category constraints have a non-empty compatible intersection
- **THEN** both constraints apply conjunctively for that rule while unrelated lower-specificity rules remain available

### Requirement: Out-Of-Band Edits Are Preserved And Surfaced
Watcher and reconcile paths MUST NOT delete, revert, or rewrite externally authored Markdown solely because it violates the semantic contract. They SHALL index all valid parseable units, remove stale derived rows, and surface malformed syntax, strict-schema drift, stale relation disposition, and registry findings through audit/review.

#### Scenario: Direct invalid edit survives reconcile
- **WHEN** a user creates a strict-contract violation in an editor and reconcile runs
- **THEN** the file remains unchanged, valid units are indexed, invalid fragments are excluded, and actionable findings are surfaced

#### Scenario: Fix clears drift idempotently
- **WHEN** the user repairs the Markdown and reconcile runs again
- **THEN** stale findings and stale derived rows disappear and repeated reconcile is a no-op

### Requirement: Structured Semantic Unit Mutation
The system SHALL expose `observe_memory` with `add`, `update`, `remove`, and `validate` operations for writable compiled pages. It SHALL accept parent path/reference, category, content, optional kind/tags/context, expected parent hash, and current unit reference/fingerprint where required. Update and remove SHALL require both the current expected parent hash and current unit fingerprint. Compact observation SHALL be the default authoring form. Typed `relations` SHALL be rejected unless an explicit governed non-observation kind selects rich form; remediation SHALL tell the caller to select rich form or author a canonical note-level relation.

#### Scenario: Add compact observation
- **WHEN** `observe_memory(operation="add")` receives a parent, category, and content
- **THEN** it writes one canonical compact observation under `## Observations`, returns its unit reference, and refreshes derived indexes

#### Scenario: Update uses drift guards
- **WHEN** an update supplies a stale parent hash or stale anonymous unit fingerprint
- **THEN** the operation fails without changing Markdown or indexes

#### Scenario: Validate performs no write
- **WHEN** `observe_memory(operation="validate")` receives a proposed unit
- **THEN** it returns normalized unit and contract findings without modifying the vault or sidecars

#### Scenario: Compact relation input is rejected clearly
- **WHEN** `observe_memory` receives `relations` without an explicit non-observation governed kind
- **THEN** it performs no write and returns remediation for rich-unit or note-level relation authoring

#### Scenario: Immutable trees refuse observation mutation
- **WHEN** `observe_memory` targets Sources, Evidence, a read-only/excluded subtree, or a path outside the governed KB
- **THEN** it refuses with the existing write-boundary error contract

### Requirement: Index Failure Does Not Destroy Committed Markdown
If Markdown commits successfully but a derived semantic-unit sidecar update fails, the operation SHALL preserve the committed Markdown, record deterministic index drift, return degraded/index-reconcile guidance, and allow reconcile to rebuild the missing records. Immediate feedback SHALL distinguish configured disable/no-op, process-local warmup deferral, durable deferred-index work, proved completion, observed degradation, and accepted-unverified legacy leaves. It SHALL require reconcile only for observed degradation and SHALL NOT duplicate the index fan-out to manufacture feedback.

#### Scenario: Sidecar failure after write is recoverable
- **WHEN** a valid Markdown write commits and the semantic-unit index update then fails
- **THEN** the Markdown remains, the response reports index drift, and reconcile restores parity without content loss

### Requirement: Structural creations may commit as one batch

The semantic writer SHALL commit several structural creations as one canonical batch when each creation carries its own draft token and needs no relation review. A structural creation is one whose status class makes the shared semantic-unit predicate false. The batch SHALL refuse a creation that requires the full relation review, that names a predecessor, or that binds a vocabulary entry, and SHALL write nothing when it refuses.

Before the mutation guard, the batch SHALL plan each creation's Source back-references, SHALL refuse two creations that plan the same file, and SHALL judge each creation's contract against the corpus that its preflight read together with every candidate of the batch. Inside one mutation guard, the batch SHALL re-check each creation's Source closure and SHALL admit those verdicts only while every validity stamp is current; otherwise it SHALL judge every candidate again against one fresh corpus. It SHALL write every page, back-reference and shared index or log update in one atomic batch, and a failure that the write catches SHALL leave none of them. The vocabulary writer gate SHALL validate one sealed derived-write manifest per page against the complete batch, and SHALL refuse a derived write that two manifests claim.

The batch SHALL raise only while nothing of it is on disk. Once the batch is written it SHALL NOT raise: each creation SHALL receive its own result, or the error of a step after the write that failed for that creation. A failure after the write that belongs to the whole batch SHALL be the outcome of every creation. A write that fails after its commit point, while the batch's pages hold the bytes that the batch planned, SHALL count as written.

#### Scenario: A chunk of draft notes commits once

- **WHEN** the semantic writer commits three structural creations as one batch
- **THEN** each creation reports its own written paths and the index and log hold one entry for each note

#### Scenario: Two creations plan one file

- **WHEN** a batch holds two creations that plan the same file
- **THEN** the batch refuses before it writes and the caller commits each creation alone

#### Scenario: A commit lands between validation and the batch

- **WHEN** another governed commit lands after the creations were validated and before the batch commits
- **THEN** the batch judges every creation again against one fresh corpus and commits when no verdict blocks

#### Scenario: A step fails after the batch is written

- **WHEN** a step after the write fails for one creation of the batch
- **THEN** every other creation receives its result and that creation receives the error, and the batch does not raise

#### Scenario: A failure while installing the batch

- **WHEN** the atomic write fails after it installed some pages of the batch
- **THEN** no page of the batch remains

#### Scenario: Two manifests claim one derived write

- **WHEN** two sealed manifests of one batch claim the same derived path
- **THEN** the vocabulary gate refuses the batch

### Requirement: Retained Interpretations Record The Writing Release

Each compiled-history entry that the semantic writer retains for a changed
interpretation SHALL record `exomem_version`, the release of the Exomem server
that planned the transition, carried in the transition token so that an exact
retry re-plans identical history bytes. A history read SHALL return that field with the revision.
Entries retained before this field existed SHALL keep their stored bytes and
SHALL remain admissible without it; the system SHALL NOT backfill them. The
system SHALL NOT record a model identity, because the server cannot know it.

#### Scenario: A correction names its writing release

- **WHEN** a governed writer changes an interpretation and retains the prior
  interpretation in compiled history
- **THEN** the retained entry records the Exomem release that planned the change
- **AND** a history read of that page returns the release with the revision

#### Scenario: An entry from before the field still reads

- **WHEN** a history read meets an entry retained before `exomem_version`
  existed
- **THEN** the entry is admitted on its existing checks and returned without
  the field
- **AND** no stored entry is rewritten to add it
