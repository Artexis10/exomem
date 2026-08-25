## MODIFIED Requirements

### Requirement: Lexical Index Synchronization

The system SHALL keep the lexical sidecar synchronized with the vault's Markdown through the same freshness seams that maintain the embedding sidecars—in-process writer hooks, the live file watcher, and reconcile—on lean installs as well as full ones. Lexical maintenance MUST NOT be gated behind the embeddings extra. When the sidecar is missing, stale, or was written past by a non-aware process, the system SHALL detect the mismatch without treating the catalog as authoritative empty state. A complete freshness delta containing no more than the foreground cap MAY be applied synchronously. Unknown, incomplete, or larger drift MUST schedule single-flight background repair and return the bounded typed `RETRIEVAL_INDEX_WARMING` outcome for ordinary maintained-catalog retrieval; it MUST NOT rebuild, heal, or scan the corpus on that request. The `find` hot-cache freshness key MUST incorporate lexical sidecar freshness so cached results cannot outlive a lexical reindex.

#### Scenario: A write keeps the lexical index current

- **WHEN** a Markdown page is created, edited, or deleted through a writer path or observed by the watcher
- **THEN** the lexical sidecar reflects the change through the same seam that refreshes the embedding sidecars
- **AND** a subsequent BM25- or keyword-lane query observes the change

#### Scenario: A pre-existing vault is indexed without request-time scanning

- **WHEN** a production-sized vault that predates the lexical sidecar is first used by an aware version
- **THEN** startup or single-flight background repair creates and populates the sidecar from Markdown source of truth
- **AND** ordinary requests return typed warming until publication completes
- **AND** no ordinary request performs the full Markdown walk

#### Scenario: Lean installs maintain the lexical index

- **WHEN** the server runs a lean install without the embeddings extra
- **THEN** writer and watcher events still keep the lexical sidecar current

#### Scenario: Small complete drift heals in proportion to change

- **WHEN** the catalog is stale by a complete retained delta within the foreground cap
- **THEN** the request may apply only those changed and deleted paths atomically
- **AND** it does not walk or rebuild the corpus

#### Scenario: Unknown out-of-band drift self-heals in background

- **WHEN** Markdown changed without a complete retained lexical delta
- **THEN** the next use detects incomplete readiness and schedules single-flight background repair
- **AND** the request returns `RETRIEVAL_INDEX_WARMING` instead of rebuilding or scanning the corpus

#### Scenario: Failed background repair is retried by later demand

- **WHEN** startup catalog repair failed and warm-up finished without admission
- **THEN** each later refused ordinary request may re-arm the single-flight repair before returning its typed retry outcome
- **AND** the request does not walk or parse the corpus to trigger that retry

#### Scenario: A pre-existing vault is indexed on first use

- **WHEN** a vault that predates the lexical sidecar is first used by an aware version
- **THEN** background warm-up creates and populates the sidecar from the Markdown source of truth
- **AND** a large-corpus request remains bounded while that repair is incomplete
- **AND** no user action is required

#### Scenario: Out-of-band drift self-heals

- **WHEN** Markdown changed without the lexical sidecar being updated and the resulting drift is unknown or incomplete
- **THEN** freshness verification detects the mismatch and schedules background repair
- **AND** retrieval admission remains withheld until the affected state is current again
