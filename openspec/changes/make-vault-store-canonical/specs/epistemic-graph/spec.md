## MODIFIED Requirements

### Requirement: Derived Epistemic Graph Sidecar
The system SHALL maintain rebuildable SQLite graph rows derived from page bytes, frontmatter, wikilinks, source/evidence references, supersession fields, semantic blocks, and existing media metadata. In file mode, the rows SHALL live in a graph sidecar and Markdown files SHALL remain the canonical source of truth. In store mode, the rows SHALL live in the vault's canonical store and SHALL be written in the same transaction as the page bytes they derive from (`canonical-vault-store`). In both modes the rows SHALL store typed nodes, typed edges, provenance, schema version, and source freshness metadata, and MUST NOT require a graph database service.

#### Scenario: Graph builds from governed Markdown
- **WHEN** the graph index runs over a governed KB containing notes with `sources`, `supersedes`, wikilinks, and semantic sections
- **THEN** the graph rows contain nodes for files and semantic blocks
- **AND** they contain typed edges such as `derived_from`, `supersedes`, `links_to`, and `evidenced_by` where those relations are present in the source pages

#### Scenario: Sidecar can be rebuilt from files
- **WHEN** a file-mode graph sidecar is deleted and the graph index is rebuilt from the same unchanged Markdown files
- **THEN** graph context over the same seed returns equivalent nodes, edges, and provenance
- **AND** no Markdown file needs to be modified to recover the graph

#### Scenario: Store-mode graph rows commit with their page
- **WHEN** a store-mode governed write changes a page's links
- **THEN** the page bytes and the page's graph nodes and edges commit in one transaction
- **AND** graph context returns the new edges at acknowledgement without a drain, watcher event or repair

#### Scenario: Store-mode graph rows can be re-derived from the store
- **WHEN** an operator rebuilds the graph rows of a store-mode vault
- **THEN** the rebuild derives them from the store's page bytes
- **AND** the result equals the rows the writes committed, apart from write-time and write-order columns
