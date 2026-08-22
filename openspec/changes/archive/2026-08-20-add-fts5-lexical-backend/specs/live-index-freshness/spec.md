## ADDED Requirements

### Requirement: Lexical Index Synchronization

The system SHALL keep the lexical sidecar synchronized with the vault's markdown
through the same freshness seams that maintain the embedding sidecars — in-process
writer hooks, the live file watcher, and reconcile — on lean installs as well as
full ones (lexical maintenance MUST NOT be gated behind the embeddings extra).
When the sidecar is missing, stale, or was written past by a non-aware process,
the next use MUST detect the mismatch (page count and max mtime against the
markdown walk) and rebuild the affected state from the markdown source of truth
without user action. The `find` hot-cache freshness key MUST incorporate the
lexical sidecar's freshness so cached results cannot outlive a lexical reindex.

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
