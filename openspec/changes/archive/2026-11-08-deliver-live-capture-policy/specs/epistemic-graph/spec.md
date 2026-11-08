## MODIFIED Requirements

### Requirement: Relation Suggestions Are Propose-Only
The system SHALL expose relation suggestions as proposal output, not durable accepted facts. `suggest_relations` SHALL accept an existing path or draft title/body and return candidate edges with relation type, method, evidence paths/spans, and explanation text. It MUST NOT write Markdown, mutate the graph sidecar as accepted state, change supersession fields, or change `find` ranking.

For an existing path, a candidate that the relation queue holds open SHALL carry
`review` with the queue's `ref`, `fingerprint` and `source_content_hash`,
computed as relation acceptance computes them; any other candidate SHALL carry
none. Computing `review` MUST NOT write. When the queue cannot confirm a
candidate, the response SHALL add a warning and SHALL NOT let the missing
`review` stand for an empty queue.

#### Scenario: Suggestions do not mutate the vault
- **WHEN** `suggest_relations` is called for an existing note
- **THEN** it returns candidate typed relations with evidence
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: Embeddings unavailable still yields deterministic suggestions
- **WHEN** embeddings are disabled or unavailable
- **THEN** `suggest_relations` still returns deterministic candidates from wikilinks, frontmatter, shared sources, and entity mentions when available
- **AND** embedding-based candidates are omitted with an availability indication rather than causing the operation to fail

#### Scenario: A queued candidate carries the fields that accept it
- **WHEN** `suggest_relations` is called for an existing note and the relation queue holds one of its candidates open
- **THEN** that candidate carries `review` with the queue's ref, fingerprint and source content hash
- **AND** `connect_memory(operation="accept-relation")` with those values is not refused for drift

#### Scenario: A candidate the queue has decided carries no review
- **WHEN** the queue has dismissed a candidate or the page already holds its edge
- **THEN** a `suggest_relations` candidate for that pair carries no `review`

#### Scenario: A failed queue lookup is visible
- **WHEN** the relation graph is not current, or the queue cannot confirm a shown candidate
- **THEN** the response adds a `warnings` entry that names the lookup and the class of its failure, not its message
- **AND** the candidates are still returned, without `review` for the unconfirmed ones

#### Scenario: A candidate the queue leaves out on purpose is not a failed lookup
- **WHEN** a suggestion generator proposes a pair that the page already authors
- **THEN** the candidate carries no `review` and the response adds no lookup warning
