## Purpose

Let authorized service callers retire one independent captured Source incarnation with explicit proof of managed logical cleanup and recoverable progress.

## ADDED Requirements

### Requirement: Explicit policy and content authority govern erasure

The system SHALL expose one erasure operation across the shared governed surfaces. Host policy SHALL enable identity systems explicitly and default to none. Existing whole-namespace content authority SHALL precede lookup, replay and disclosure, and SHALL be rechecked before effects and terminal release.

#### Scenario: Erasure policy is disabled
- **WHEN** a caller requests erasure while host policy enables no matching identity system
- **THEN** the request refuses before disclosing whether the target exists or changing stored data

#### Scenario: Caller loses content authority
- **WHEN** an earlier authorized caller repeats an erasure after its namespace authority narrows
- **THEN** the forbidden result precedes target lookup and saved-result disclosure

#### Scenario: Discovery advertises an implemented operation
- **WHEN** a consumer qualifies one identity system for serving readiness
- **THEN** an authenticated policy read proves its current enabled state separately from static operation discovery
- **AND** unavailable configuration remains unknown rather than enabled or disabled

### Requirement: Every erasure targets one immutable incarnation

The operation SHALL accept an operation identifier, Source identity, immutable Source reference and expected hash. Every retry and deferred effect SHALL retain that target. Hash drift SHALL return a stale precondition. A missing page SHALL be eligible only when retained custody proves its allocation; missing proof SHALL remain unknown.

#### Scenario: Delayed erase arrives after fresh capture
- **WHEN** the same logical identity now belongs to a fresh Source reference
- **THEN** the earlier erase can retire only its original reference and cannot remove the fresh Source or its derived rows

#### Scenario: Allocated Source never became canonical
- **WHEN** a closed capture attempt proves an allocated reference and retained hash but the canonical page is absent
- **THEN** cleanup can retire that allocation's managed artifacts without treating canonical absence as completion

### Requirement: Erasure preserves dependent knowledge

The system MUST refuse known dependent compiled knowledge, historical versions, origin-bound interpretations, collection versions and either endpoint of a correction chain. Incomplete dependency coverage or unresolved publication SHALL remain unknown or pending. Removing a current citation MUST NOT erase retained historical dependence.

#### Scenario: Restricted writer skipped a Source back-reference
- **WHEN** retained compiled history cites a Source whose back-reference the writer could not update
- **THEN** erasure refuses that Source using retained dependency evidence

#### Scenario: Dependency write fails before publication
- **WHEN** a reserved dependency publication proves no effect or complete rollback
- **THEN** its reservation does not become a permanent invented dependency

#### Scenario: Another replica publishes restricted knowledge
- **WHEN** a different replica commits dependent knowledge while skipping the Source back-reference
- **THEN** canonical custody records that dependence in the same publication and an older replica cannot reuse a clear local latch

#### Scenario: Dependence races retirement admission
- **WHEN** a dependent publication races the independence check
- **THEN** one writer-boundary cutoff orders dependency publication before refusal or target retirement before any auxiliary destruction

### Requirement: Complete retirement includes every managed owner

Complete retirement SHALL include canonical data, owned originals and staging, identifying generated entries, private replay and recovery copies, derived stores, reusable caches and retained projection state. Each required owner SHALL acknowledge its target-qualified cleanup. Deferred or registered cleanup SHALL remain pending.

#### Scenario: Canonical deletion finishes before derived cleanup
- **WHEN** the page is gone but an owner still retains its managed bytes or unacknowledged work
- **THEN** the result remains pending and target_retired is false

#### Scenario: Shared log contains another Source
- **WHEN** cleanup removes an owned entry from shared or rotated activity data
- **THEN** it removes only the proved target's entries and preserves unrelated entries

#### Scenario: Trimmed activity copied an older Source
- **WHEN** another Source's entry contains an owned excerpt from the target's earlier activity
- **THEN** cleanup removes that target fragment from every managed version while preserving the other Source's record

#### Scenario: A tracked Source follows unattributed legacy activity
- **WHEN** a fresh Source has a qualified producer birth after a retained image's uncertainty horizon
- **THEN** exact image and copy evidence can prove that Source's ownership while preserving unattributed legacy bytes
- **AND** the older Sources retain their explicit unknown coverage

#### Scenario: An ownership map disappears after publication
- **WHEN** a retained image loses its ownership evidence or its bytes no longer match the bound map
- **THEN** its uncertainty horizon includes existing Source births and its earlier positive associations remain unresolved
- **AND** lookup includes that image without relying only on surviving fragment membership

#### Scenario: A local ownership index is lost
- **WHEN** a writer retains valid canonical ownership records but its derived index is unavailable
- **THEN** it reads the current generated-file binding directly and preserves its ownership map and uncertainty horizon
- **AND** ordinary capture does not require a whole-catalogue rebuild

#### Scenario: A binary original outlives its private native binding
- **WHEN** canonical whole-Source image and copy records retain the original but its private binding disappears
- **THEN** lookup retains the obligation and retirement remains unknown until the native owner resolves it
- **AND** the binary original needs no UTF-8 fragment map

#### Scenario: Birth coverage is unavailable for a known copy
- **WHEN** an explicit canonical Source association retains a copy but its birth evidence cannot qualify historical completeness
- **THEN** lookup returns the known copy with unknown completeness instead of hiding the obligation

#### Scenario: A prior generated publisher resumes after takeover
- **WHEN** another native publication has taken over the current slot with a new revision
- **THEN** the old forward publication and rollback cannot reuse their cancelled publication right
- **AND** displaced physical copies remain outstanding until their owners acknowledge them

#### Scenario: Rollback creates a fresh copy of the before-image
- **WHEN** an inverse publication restores earlier bytes through a newly created native file
- **THEN** it binds a fresh copy and publication right instead of reusing the earlier file's identity
- **AND** an interrupted handoff retains the observed current binding and every unresolved physical obligation

#### Scenario: Store audit retains the target's earlier paths
- **WHEN** target projection and vector cleanup have finished for an incarnation with exact move lineage
- **THEN** cleanup redacts its identifying audit fields while retaining sequence, revision and operation facts
- **AND** it preserves a replacement incarnation's audit facts at a reused path
- **AND** path fields use non-path opaque tokens, preserving existing null move origins without producing a null path

#### Scenario: Managed store replica predates retirement
- **WHEN** a managed reusable snapshot or published replica contains state older than the purge revision
- **THEN** retirement remains pending until its owner replaces or retires that copy
- **AND** an unrelated collection transaction sequence cannot substitute for the store revision acknowledgement

### Requirement: Retirement exposes honest resumable progress

The result SHALL name the target reference, status and target_retired. Status SHALL be complete, pending, failed or unknown. Only complete acknowledged cleanup SHALL set target_retired true. Progress SHALL survive restart and remain bounded per invocation. Missing reads and partial owner evidence MUST NOT become empty success.

#### Scenario: Cleanup response is lost
- **WHEN** the caller retries its retained operation after restart
- **THEN** cleanup resumes for the same incarnation and reports actual component progress

#### Scenario: Custody moves to another replica
- **WHEN** canonical Source custody moves through supported replica handoff
- **THEN** the next owner can reconcile that incarnation without relying on an old local clear latch
- **AND** unavailable attempt-local residue ownership remains unknown rather than absent

### Requirement: Snapshot readers cannot republish retired data

Reads overlapping retirement SHALL be allowed to finish from their existing snapshots. After complete retirement, new reads MUST NOT reach the retired incarnation through managed storage or reusable caches. An obsolete builder MUST NOT publish its captured bytes for reuse after invalidation.

#### Scenario: Builder finishes after cache invalidation
- **WHEN** a builder read the Source before retirement but reaches cache insertion after invalidation
- **THEN** its overlapping invocation can finish while the reusable cache rejects that obsolete publication

### Requirement: Attempt cleanup covers pre-reference residue

Capture custody SHALL identify owned artifacts before surviving writes and distinguish new allocations from reused Sources. Settlement SHALL report custody separately from capture outcome. Explicit residue cleanup SHALL require closed admission. Collection MUST preserve the last recovery link while owned residue remains unresolved.

#### Scenario: Protected staging fails before Source allocation
- **WHEN** a capture leaves owned staging bytes without allocating a Source reference
- **THEN** settlement reports that residue and explicit cleanup continues until its producer acknowledges removal

#### Scenario: Attempt reused another successful capture
- **WHEN** an attempt observed a reused Source rather than allocating it
- **THEN** cleaning that attempt's residue does not erase the observed Source

### Requirement: Erasure scope excludes previously exported copies

The operation SHALL describe logical managed erasure. Earlier backups, downloaded exports, caller-held responses, external synchronization history and forensic remnants SHALL remain outside its guarantee. Restoring excluded copies SHALL NOT be described as fresh capture or permanent nonexistence.

#### Scenario: Earlier backup is restored
- **WHEN** a separately retained backup reintroduces an erased Source
- **THEN** the prior receipt describes its completed managed retirement and does not claim that the restored material cannot exist
