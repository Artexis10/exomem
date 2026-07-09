## ADDED Requirements

### Requirement: Derived Epistemic Graph Sidecar
The system SHALL maintain a rebuildable SQLite graph sidecar derived from Markdown, frontmatter, wikilinks, source/evidence references, supersession fields, semantic blocks, and existing media metadata. Markdown files SHALL remain the canonical source of truth. The sidecar SHALL store typed nodes, typed edges, provenance, schema version, and source freshness metadata, and MUST NOT require a graph database service.

#### Scenario: Graph builds from governed Markdown
- **WHEN** the graph index runs over a governed KB containing notes with `sources`, `supersedes`, wikilinks, and semantic sections
- **THEN** the sidecar contains nodes for files and semantic blocks
- **AND** it contains typed edges such as `derived_from`, `supersedes`, `links_to`, and `evidenced_by` where those relations are present in the source files

#### Scenario: Sidecar can be rebuilt from files
- **WHEN** the graph sidecar is deleted and the graph index is rebuilt from the same unchanged Markdown files
- **THEN** graph context over the same seed returns equivalent nodes, edges, and provenance
- **AND** no Markdown file needs to be modified to recover the graph

### Requirement: Explicit Relationship Vocabulary
The graph SHALL support a typed relationship vocabulary including `supports`, `contradicts`, `refines`, `duplicates`, `supersedes`, `derived_from`, `evidenced_by`, `depends_on`, `implements`, `mitigates`, `caused_by`, `blocks`, `answers`, `raises_question`, `used_for`, `observed_in`, `mentions`, `about_entity`, and `links_to`. Persisted graph edges SHALL include their origin and source provenance, and SHALL NOT store authority or confidence floats on notes.

#### Scenario: Relationship provenance is returned
- **WHEN** a graph edge is derived from a note's frontmatter or body
- **THEN** graph lookups return the relation type, source path, origin method, and source anchor or span when available
- **AND** the relation can be traced back to the file content that produced it

#### Scenario: Unsupported relation label is not persisted
- **WHEN** a note contains an unrecognized optional relation label
- **THEN** the graph index does not persist that label as an accepted typed edge
- **AND** indexing continues for other supported relations in the same file

### Requirement: Read-Only Graph Context Surface
The system SHALL expose a read-only `graph_context` operation through the single command registry on MCP, REST, and CLI. The operation SHALL accept a path or query seed plus bounded traversal controls such as depth, relation-type filters, node-type filters, and caps. The response SHALL include seed nodes, related nodes, edges, provenance, graph availability, and explicit truncation entries whenever caps omit content.

#### Scenario: Graph context returns a bounded neighborhood
- **WHEN** `graph_context` is called for a note with depth `1`
- **THEN** it returns the seed note, directly related nodes, typed edges, and provenance
- **AND** no files under the vault are created, modified, moved, or deleted

#### Scenario: Graph context is exposed consistently
- **WHEN** the command registry is inspected
- **THEN** `graph_context` is available as an MCP tool, `/api/graph_context` REST route, and CLI subcommand
- **AND** all three surfaces call the same leaf function

### Requirement: Relation Suggestions Are Propose-Only
The system SHALL expose relation suggestions as proposal output, not durable accepted facts. `suggest_relations` SHALL accept an existing path or draft title/body and return candidate edges with relation type, method, evidence paths/spans, and explanation text. It MUST NOT write Markdown, mutate the graph sidecar as accepted state, change supersession fields, or change `find` ranking.

#### Scenario: Suggestions do not mutate the vault
- **WHEN** `suggest_relations` is called for an existing note
- **THEN** it returns candidate typed relations with evidence
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: Embeddings unavailable still yields deterministic suggestions
- **WHEN** embeddings are disabled or unavailable
- **THEN** `suggest_relations` still returns deterministic candidates from wikilinks, frontmatter, shared sources, and entity mentions when available
- **AND** embedding-based candidates are omitted with an availability indication rather than causing the operation to fail

### Requirement: Model-Backed Graph Suggestions Respect Pure Substrate
Any model-backed relation, contradiction, polarity, or claim-classification path SHALL be optional, default-off, and soft-failing. Such paths SHALL be labelled as measurement and SHALL only propose candidate edges for review; they MUST NOT author note text, auto-accept relations, auto-supersede notes, or run as a server-side reasoning agent.

#### Scenario: Default graph indexing uses no reasoning model
- **WHEN** the graph sidecar is built with default configuration
- **THEN** graph indexing uses deterministic extraction and available measurement sidecars only
- **AND** no generative or reasoning model is invoked

#### Scenario: Optional model failure does not break graph context
- **WHEN** optional model-backed relation suggestion is enabled but the model cannot load
- **THEN** graph context remains available from deterministic graph data
- **AND** the response reports the optional suggestion path as unavailable
