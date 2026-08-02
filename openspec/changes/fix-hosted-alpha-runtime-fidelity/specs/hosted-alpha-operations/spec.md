## ADDED Requirements

### Requirement: Hosted cells deliver the complete product surface
A hosted cell SHALL provide the same retrieval capability as the local runtime. The
rendered cell environment MUST NOT disable embeddings or media extraction, and the cell
worker limit SHALL be greater than zero with the `embeddings` feature granted. Readiness
SHALL report the embeddings worker as ready rather than
`HOSTED_WORKER_LIMIT_ZERO`. A cell that cannot run semantic recall SHALL NOT be presented
as a paid or invited tenant.

#### Scenario: Cell renders with workers disabled
- **WHEN** the cell chart would render a worker limit of zero or omit the embeddings grant
- **THEN** rendering fails rather than producing a cell that silently serves keyword-only recall

#### Scenario: Tenant performs semantic recall
- **WHEN** an invited tenant captures notes and then queries by meaning rather than exact wording
- **THEN** the cell returns semantically ranked results, matching the local runtime's behaviour

### Requirement: Capacity basis reflects the embedding-capable node
The six-cell USER cap SHALL be justified against the resized node's CPU and memory with
embeddings enabled, not against the smaller pre-embedding envelope. The capacity
contract's monthly cost basis SHALL use the node actually in service, and raising the cap
SHALL continue to require a fresh soak and reviewed cost sheet.

#### Scenario: Cost basis names a node that is no longer in service
- **WHEN** the capacity contract's server cost does not match the provisioned server type
- **THEN** the cost evidence is rejected and provisioning admission fails closed
