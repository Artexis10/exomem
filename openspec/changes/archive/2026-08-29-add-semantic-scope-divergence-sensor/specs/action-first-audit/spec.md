## ADDED Requirements

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
