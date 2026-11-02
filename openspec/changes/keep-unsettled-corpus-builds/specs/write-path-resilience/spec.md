## MODIFIED Requirements

### Requirement: Corpus Context Reuse Is Sync-Safe And Bounded

The governed write path SHALL reuse a previously built corpus context only when an exact census of every filesystem input read by that context still matches. A build MUST be captioned as current only when its census is identical before and after the build. A build whose census never settles MAY be kept with the last census it was reconciled to, without a caption, and a later caller MUST confirm it with its own census walk before reusing it. Cached contexts MUST be immutable and bounded, and candidate-bearing or disk-divergent registry builds MUST bypass the cache.

#### Scenario: Synced content carries an older timestamp
- **WHEN** Syncthing materializes changed same-size content at an existing path with an older `mtime_ns`
- **THEN** the per-path census differs from the cached census and the context is rebuilt
- **AND** the system does not rely on a vault-wide maximum timestamp

#### Scenario: Corpus changes while context is building
- **WHEN** any censused input changes between the pre-build and post-build census
- **THEN** the raced context is not captioned as current for reuse
- **AND** a later call must establish a stable census before receiving a cached result

#### Scenario: Writes outrun every census confirmation
- **WHEN** a writer changes a Markdown page before each census confirmation of a cold build
- **THEN** the build is kept with its last census and no caption, and corpus warm-up succeeds
- **AND** the next caller's census walk reparses only the pages changed since that census, without a cold build
- **AND** an edit that no event reported is still read by that caller
