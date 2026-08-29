# action-first-audit Specification

## Purpose
TBD - created by archiving change clear-agent-facing-friction. Update Purpose after archive.

## Requirements

### Requirement: Default Audit Output Is Action-First

Audit product commands SHALL default to an action-first projection. Current non-grandfathered blockers MUST be ordered first, malformed and unregistered semantic work next, other current findings after that, and grandfathered `RELATION_DISPOSITION_MISSING` debt SHALL be represented as grouped backlog information rather than hundreds of actionable errors.

#### Scenario: Current blockers coexist with legacy backlog
- **WHEN** an audit observes a small number of current blockers and hundreds of grandfathered missing-disposition findings
- **THEN** every current blocker appears before the grouped backlog
- **AND** the backlog reports observed count, omissions, completion state, and deterministic representative samples

#### Scenario: Grandfathered missing disposition does not block current work
- **WHEN** a missing-disposition finding is marked grandfathered and is not a current mutation precondition
- **THEN** its audit presentation severity is info/backlog
- **AND** current semantic write enforcement is unchanged

### Requirement: Audit Prioritizes Before Bounding

For the default bounded semantic posthoc projection, the system SHALL prioritize current actionable findings before applying finding-count or byte bounds. Summary and omission counters MUST describe the complete evaluated batch even when individual findings are omitted.

#### Scenario: Legacy findings exceed the default cap
- **WHEN** grandfathered findings alone exceed the default semantic finding cap and current blockers occur later in path order
- **THEN** current blockers remain in the retained projection
- **AND** omitted legacy findings are reflected in grouped and truncation metadata

### Requirement: Full Audit Enumeration Is Explicit

`detail="full"` SHALL request the full raw finding enumeration for audit, including original categories, metadata, and truncation/omission facts. `review_memory(mode="audit")` and `maintain_memory(mode="audit")` SHALL forward the same detail and sampling controls and remain read-only.

#### Scenario: Caller asks for full detail
- **WHEN** audit is called with `detail="full"`
- **THEN** individual grandfathered findings are returned rather than only representative samples
- **AND** no repair or mutation occurs

#### Scenario: Diagnostics contend with a writer
- **WHEN** a full audit overlaps a live mutation
- **THEN** the audit does not acquire or retain the mutation boundary
- **AND** it cannot cause a post-commit mutation to return `MUTATION_BUSY`

### Requirement: The audit detects semantic scope divergence from existing unit vectors

The audit SHALL provide a `scope_divergence_semantic` category that judges eligible
compiled pages using only the vectors already stored in the embedding sidecar. A page
is flagged when a deterministic grouping of its units' vectors yields a group that is
internally cohesive, separated from the page's identity remainder, reaches child-note
mass, and leaves the page's original scope retained — while pages whose declared
identity announces breadth (hubs, snapshots, navigation and log pages) are exempt.
The category SHALL add no write-time embedding, no model call, and no new index.

#### Scenario: Shared vocabulary no longer hides divergence

- **GIVEN** a compiled page whose units share the parent domain's vocabulary
- **AND** the stored unit vectors form a cohesive group separated from the page's identity remainder at child-note mass, with the original scope retained
- **WHEN** the audit sweeps the category
- **THEN** one `scope_divergence_semantic` finding is produced for the page, carrying reason `semantic_cluster_diverges` and extractive label terms drawn from the group's own units

#### Scenario: The advisory binds to structure, not vocabulary

- **GIVEN** the same vector geometry with every recurring term replaced by a synonym
- **WHEN** the audit sweeps the category
- **THEN** the finding is still produced

#### Scenario: Breadth-declaring and matched twin pages stay quiet

- **GIVEN** a bounded-scope page, a page with one sub-mass tangent group, a declared hub, and a legitimately heterogeneous log page, each frequency- and length-matched to a flagged fixture
- **WHEN** the audit sweeps the category
- **THEN** zero `scope_divergence_semantic` findings are produced for them

#### Scenario: Acting on the advice resolves it by state change

- **GIVEN** a flagged page and eligible compiled destinations whose declared identity covers the group's label terms with at least two terms per contributing destination
- **WHEN** the audit sweeps the category again
- **THEN** no finding is produced for that group
- **AND** deleting a covering destination brings the finding back without any recorded dismissal

#### Scenario: A coherent thread on an undeclared heterogeneous page is found

- **GIVEN** a page declaring no breadth whose units are mutually dissimilar except one internally cohesive group at child-note mass, with the page's scope retained
- **WHEN** the audit sweeps the category
- **THEN** one finding is produced for the cohesive group — undeclared heterogeneity is not an exemption; declared breadth is

#### Scenario: Stale vector generations are not judged

- **GIVEN** a page whose stored unit vectors were written for an earlier parse generation than the current one
- **WHEN** the audit sweeps the category
- **THEN** the page is not judged and no finding is produced for it

#### Scenario: Missing vectors never fail closed into advice

- **GIVEN** a page whose units have no stored vectors
- **WHEN** the audit sweeps the category
- **THEN** the page is not judged and no finding is produced for it

#### Scenario: Findings ride the existing review machinery

- **GIVEN** a `scope_divergence_semantic` finding
- **WHEN** it is delivered
- **THEN** it is a fingerprint-bound review item keyed by page identity and the sorted label terms, honouring family dispositions, dismissal suppression, and material-change reopen when the label set changes

### Requirement: The audit detects recurring unresolved entity identities

The audit SHALL provide an `entity_recurrence` category that counts, per
NFKC-normalised identity, the distinct pages whose bodies carry an unresolved
wikilink to it, and SHALL produce one finding per identity that reaches the
spread gate while resolving against neither an existing vault page nor the
entity registry's titles and aliases. The finding SHALL carry reason
`unresolved_identity_recurs`, the candidate identity, the sorted mentioning
pages, and a bounded deterministic list of registry near-matches. The category
SHALL add no write-time work, no embedding, no model call, and SHALL never
create any page.

#### Scenario: A recurring unresolved identity becomes a candidate

- **GIVEN** three distinct pages whose bodies link an identity that exists neither as a page nor as a registry title or alias
- **WHEN** the audit sweeps the category
- **THEN** one `entity_recurrence` finding is produced carrying reason `unresolved_identity_recurs`, the candidate, the three pages sorted, and the near-match list

#### Scenario: Frequency inside one page is not spread

- **GIVEN** one page linking the same unresolved identity five times and one other page linking it once
- **WHEN** the audit sweeps the category
- **THEN** zero findings are produced for that identity

#### Scenario: Plain-text mentions are out of scope for this stream

- **GIVEN** an identity mentioned in body text of many pages with no wikilink anywhere
- **WHEN** the audit sweeps the category
- **THEN** zero findings are produced for it

#### Scenario: A registry-resolved identity is never a candidate

- **GIVEN** an entity page whose alias NFKC-matches an identity linked from five pages
- **WHEN** the audit sweeps the category
- **THEN** zero findings are produced for that identity

#### Scenario: Retired and excluded pages are not evidence

- **GIVEN** an identity linked from two eligible pages and from one page that is superseded, archived, draft, or in an excluded access tier
- **WHEN** the audit sweeps the category
- **THEN** zero findings are produced for that identity
- **AND** a finding never anchors on a retired or excluded page

#### Scenario: Acting on the advice resolves it by state change

- **GIVEN** a firing candidate
- **WHEN** an entity page whose title or alias resolves the identity is created, or the linked target page itself is created
- **THEN** the finding is not produced on the next sweep
- **AND** deleting that page brings the finding back without any recorded dismissal

#### Scenario: Findings ride the existing review machinery

- **GIVEN** an `entity_recurrence` finding
- **WHEN** it is delivered
- **THEN** it is a fingerprint-bound review item keyed on the identity, honouring family dispositions and dismissal suppression
