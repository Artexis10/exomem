## MODIFIED Requirements

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

## ADDED Requirements

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
