## MODIFIED Requirements

### Requirement: Lexical Index Synchronization

In file mode, the system SHALL keep the lexical sidecar synchronized with the vault's markdown
through the same freshness seams that maintain the embedding sidecars — in-process
writer hooks, the live file watcher, and reconcile — on lean installs as well as
full ones (lexical maintenance MUST NOT be gated behind the embeddings extra).
When the sidecar is missing, stale, or was written past by a non-aware process,
the next use MUST detect the mismatch (page count and max mtime against the
markdown walk) and rebuild the affected state from the markdown source of truth
without user action. The `find` hot-cache freshness key MUST incorporate the
lexical sidecar's freshness so cached results cannot outlive a lexical reindex.
In store mode, the lexical catalogue rows SHALL live in the vault's canonical
store and SHALL commit in the same transaction as the page bytes they derive
from, so no freshness seam, mismatch detection or rebuild from markdown is
needed for writes that Exomem commits; outside edits reach the catalogue only
through the store's outside-edit import (`canonical-vault-store`).

#### Scenario: A write keeps the lexical index current

- **WHEN** a markdown page is created, edited, or deleted through a writer path
  or observed by the watcher
- **THEN** the lexical sidecar reflects the change through the same seam that
  refreshes the embedding sidecars
- **AND** a subsequent bm25- or keyword-lane query observes the change

#### Scenario: A pre-existing vault is indexed on first use

- **WHEN** a vault that predates the lexical sidecar is first used by an aware
  version
- **THEN** the sidecar is created and populated from the markdown walk
- **AND** no user action is required

#### Scenario: Lean installs maintain the lexical index

- **WHEN** the server runs a lean install (no embeddings extra)
- **THEN** writer and watcher events still keep the lexical sidecar current

#### Scenario: Out-of-band drift self-heals

- **WHEN** markdown changed without the lexical sidecar being updated
- **THEN** the next use detects the count/mtime mismatch and rebuilds the
  affected state from markdown

#### Scenario: Store-mode lexical rows commit with their page

- **WHEN** a store-mode governed write changes a page
- **THEN** the page bytes and its lexical catalogue rows commit in one transaction
- **AND** a bm25- or keyword-lane query observes the change at acknowledgement

### Requirement: Graph Drift Is Auditable And Reconciled
The system SHALL detect graph sidecar drift caused by missing rows, stale source hashes, schema mismatch, or a missing sidecar. Audit/reconcile SHALL surface and repair graph drift without mutating canonical Markdown content. When graph indexing is disabled, graph drift checks SHALL short-circuit cleanly. In store mode, an interrupted write cannot leave graph drift, because graph rows commit with their page; audit SHALL instead compare the stored graph rows with a fresh derivation from the store's page bytes and report any difference as a defect.

#### Scenario: Reconcile rebuilds stale graph rows
- **WHEN** graph audit detects that graph rows for a Markdown file are stale relative to the file's current content
- **THEN** reconcile refreshes the graph rows for that file or rebuilds the graph sidecar
- **AND** the source Markdown file remains unchanged

#### Scenario: Disabled graph indexing is a no-op
- **WHEN** graph indexing is disabled
- **THEN** graph drift checks return no actionable findings
- **AND** no optional graph dependency or sidecar is required

#### Scenario: Store-mode audit re-derives instead of repairing
- **WHEN** graph audit runs on a store-mode vault
- **THEN** it compares the stored graph rows with a derivation from the store's page bytes
- **AND** it reports any difference as a defect without rebuilding from Markdown
