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
A request SHALL admit each page before enriching its lifecycle facts and SHALL NOT publish those facts into the shared cache.
A stored manifest exemption SHALL require admitted target eligibility and complete identity uniqueness evidence.
Explicit request paths SHALL NOT infer an activation census from missing identity evidence.
An absent activation manifest SHALL require existing complete-view aggregate authority and independent status-registry admission before its complete census is built.
A restricted first write SHALL refuse uniformly until ordinary canonical or administrative preparation establishes the manifest.
The refusal SHALL add no human queue and SHALL NOT disable otherwise valid writes against an existing manifest.
Validity-token reuse SHALL require fresh complete-view admission and current status dependencies; otherwise existing preflight revalidation SHALL run.

#### Scenario: Hidden status cannot poison a shared semantic cache
- **WHEN** an owner warms the structural cache and a restricted writer validates an admitted page beside a hidden unfamiliar status
- **THEN** the hidden label is not classified and no caller-derived lifecycle fact enters the shared cache
- **AND** the complete identity census retains its existing uniqueness obligations

#### Scenario: Restricted first write cannot create a partial activation boundary
- **WHEN** a restricted writer validates a page while the activation manifest is absent
- **THEN** the request reports the same unavailable outcome with or without hidden pages
- **AND** canonical or administrative preparation can establish the complete manifest for later permitted writes

#### Scenario: Hidden duplicate identity cannot inherit grandfathering
- **WHEN** an admitted target shares its stable identity with another canonical page in the complete identity census
- **THEN** the target cannot claim the stored manifest's stable-identity exemption
