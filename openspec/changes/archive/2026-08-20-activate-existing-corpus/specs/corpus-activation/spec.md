## ADDED Requirements

### Requirement: Deterministic Existing-Corpus Coverage

The system SHALL scan active, read-write compiled knowledge and return explicit coverage counts for eligible pages, connected pages, typed-relation pages, generic-only pages, disconnected pages, provenance-candidate pages, provenance-linked pages, and unregistered relation observations. It MUST NOT combine the counts into a confidence, authority, or quality score.

#### Scenario: Mixed corpus produces denominator-backed coverage

- **WHEN** the corpus contains a disconnected compiled page, a generic-link-only compiled page, a typed-relation page, and an assertion-bearing page with provenance
- **THEN** each page is counted in the applicable coverage fields with `eligible_pages` as the page denominator
- **AND** no aggregate quality score is returned

#### Scenario: Protected and inactive material is excluded

- **WHEN** Sources, Evidence, read-only pages, excluded pages, navigation pages, drafts, archived pages, or superseded pages are present
- **THEN** they are not counted as eligible activation targets
- **AND** the scan does not modify them

### Requirement: Structural Activation Signals

The system SHALL surface four explicit structural signals: `relation_debt` for an eligible page with no outbound graph connection, `typed_relation_debt` for one with generic connections but no registered typed semantic relation, `provenance_debt` for one with assertion-bearing semantic blocks but no explicit page-level provenance relation, and `unregistered_relation` for authored relation observations outside the loaded registry. Every signal SHALL carry the page content version and measured counts needed for stable review fingerprinting.

#### Scenario: Generic links do not masquerade as typed epistemic structure

- **WHEN** an eligible page contains ordinary body wikilinks but no registered typed relation
- **THEN** it produces `typed_relation_debt`, not `relation_debt`
- **AND** its metadata reports the observed generic and typed connection counts

#### Scenario: Assertion-bearing page lacks page-level provenance

- **WHEN** an eligible page contains a claim, finding, inference, hypothesis, or result semantic block and has no `derived_from`, `evidenced_by`, or `cites` relation through supported Markdown or frontmatter origins
- **THEN** it produces a `provenance_debt` measurement
- **AND** the guidance states that page-level provenance does not establish support for every block

#### Scenario: Explicit unknown vocabulary remains unassigned

- **WHEN** an authored relation observation cannot be resolved by the loaded relation registry
- **THEN** the page produces an `unregistered_relation` measurement containing the observed labels and anchors
- **AND** the system does not map, register, or rewrite the relation automatically

### Requirement: Governed Activation Actions

Every activation reason SHALL include deterministic next-action routes into existing read, relation-proposal, schema-review, or governed edit operations. Running activation MUST NOT execute those routes, write a relation, change the registry, mutate a note, update graph data, or alter retrieval ranking. Any model-assisted relation suggestion remains explicit, default-off, response-only, and soft-failing under its existing contract.

#### Scenario: Reviewing an activation item is non-mutating

- **WHEN** an activation report is requested
- **THEN** each item includes review guidance and applicable existing-tool routes
- **AND** vault file content, graph sidecars, registry files, and find ordering remain unchanged

### Requirement: Dependency-Light Soft Failure

Corpus activation SHALL require no embedding, reranking, media, or reasoning-model dependency. An unreadable individual page MUST NOT fail the whole scan; it SHALL be skipped using the existing tolerant corpus parsing behavior while the completed measurements are returned.

#### Scenario: Embeddings are disabled

- **WHEN** activation runs with embeddings and optional model features disabled
- **THEN** the same Markdown-derived findings and deterministic ordering are available

