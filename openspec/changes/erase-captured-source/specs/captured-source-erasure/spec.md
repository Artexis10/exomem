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

#### Scenario: Vault storage mode lacks erasure support
- **WHEN** a caller requests erasure on a vault whose storage mode has no erasure implementation
- **THEN** the request refuses with an unsupported-mode result before any target lookup or stored-data change
- **AND** the authenticated policy read reports erasure disabled for that vault

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

#### Scenario: Another page links to the Source
- **WHEN** another page holds a wikilink that resolves to the target's canonical page
- **THEN** erasure refuses before its cutoff and the Source remains
- **AND** an unreadable link index answers unknown instead of admitting the cutoff

#### Scenario: Restricted writer skipped a Source back-reference
- **WHEN** retained compiled history cites a Source whose back-reference the writer could not update
- **THEN** erasure refuses that Source using retained dependency evidence

#### Scenario: Another replica publishes restricted knowledge
- **WHEN** a different replica commits dependent knowledge while skipping the Source back-reference
- **THEN** canonical custody records that dependence in the same publication and an older replica cannot reuse a clear local latch

#### Scenario: Dependence races retirement admission
- **WHEN** a dependent publication races the independence check
- **THEN** one writer-boundary cutoff orders dependency publication before refusal or target retirement before any auxiliary destruction

### Requirement: Complete retirement includes every managed owner

Complete retirement SHALL include canonical data, owned originals and staging, the Source's own managed copies, private replay and recovery copies, derived stores, reusable caches and retained projection state. Each required owner SHALL acknowledge its target-qualified cleanup. Deferred or registered cleanup SHALL remain pending.

#### Scenario: Canonical deletion finishes before derived cleanup
- **WHEN** the page is gone but an owner still retains its managed bytes or unacknowledged work
- **THEN** the result remains pending and target_retired is false

#### Scenario: Generic lifecycle operations meet an enrolled Source
- **WHEN** a caller deletes or moves an enrolled Source through the generic file and directory operations
- **THEN** the request refuses and names the erasure operation
- **AND** no trash or moved copy exists outside the copy census

#### Scenario: A cell that cannot erase meets an enrolled Source
- **WHEN** a caller deletes or moves an enrolled Source and host policy no longer admits erasure for its identity system
- **THEN** the request refuses, reports the Source as held under capture custody and does not name the erasure operation

#### Scenario: An enrolled capture left no shared activity
- **WHEN** the target was captured under custody by a build that keeps enrolled Sources off the shared pages
- **THEN** cleanup finds no shared navigation or activity entry for the target and needs no shared-page removal

#### Scenario: A tracked Source follows an unattributed legacy image
- **WHEN** a fresh Source has a qualified producer birth after a retained image's uncertainty horizon
- **THEN** exact image and copy evidence can prove that Source's ownership while preserving unattributed legacy bytes
- **AND** the older Sources retain their explicit unknown coverage

#### Scenario: An image header disappears after publication
- **WHEN** a retained image loses its ownership evidence or its bytes no longer match its header
- **THEN** its uncertainty horizon includes existing Source births and its earlier positive associations remain unresolved
- **AND** lookup includes that image without relying only on surviving recorded membership

#### Scenario: A local ownership index is lost
- **WHEN** a writer retains valid canonical ownership records but its derived index is unavailable
- **THEN** it reads image headers and copy records directly and preserves their membership and uncertainty horizon
- **AND** ordinary capture does not require a whole-catalogue rebuild

#### Scenario: A binary original outlives its private native binding
- **WHEN** canonical whole-Source image and copy records retain the original but its private binding disappears
- **THEN** lookup retains the obligation and retirement remains unknown until the native owner resolves it

#### Scenario: Birth coverage is unavailable for a known copy
- **WHEN** an explicit canonical Source association retains a copy but its birth evidence cannot qualify historical completeness
- **THEN** lookup returns the known copy with unknown completeness instead of hiding the obligation

#### Scenario: A native file identity is reused after installation
- **WHEN** an unrelated allocation reuses a retired allocation's file identity and contains identical bytes
- **THEN** a different destination version remains unknown instead of inheriting the earlier copy's ownership
- **AND** ordinary destination observations use the persisted installation version without hashing the content again

#### Scenario: Partial allocation continuity is lost
- **WHEN** cleanup lacks the original live handle for a partially written allocation
- **THEN** it preserves the current file and reports unresolved custody instead of adopting its identity or content
- **AND** this uncertainty blocks only that copy's cleanup

#### Scenario: Canonical uncertainty survives loss of its derived index
- **WHEN** a writer reads an image after canonical uncertainty was recorded and its local index became unavailable
- **THEN** it carries the effective image and catalogue horizons from direct canonical records
- **AND** an absent legacy summary qualifies no zero horizon and requires no history scan before a later writer

### Requirement: Retirement exposes honest resumable progress

The result SHALL name the target reference, status, settled and target_retired. Status SHALL be complete, pending, failed or unknown. Settled SHALL be false while status is pending or a failed read leaves the answer open, and true otherwise. A result that is not settled SHALL carry retry_after_ms, the wait before a repeat can observe progress, derived from what the operation waits on. A caller SHALL repeat the operation after that wait until settled is true, then read status. Only complete acknowledged cleanup SHALL set target_retired true. Progress SHALL survive restart and remain bounded per invocation. Missing reads and partial owner evidence MUST NOT become empty success. An operation admitted before host policy disables erasure SHALL finish its cleanup; the policy SHALL refuse only new operations.

#### Scenario: Cleanup finishes
- **WHEN** every owner acknowledges its cleanup
- **THEN** the result is settled with status complete and target_retired true

#### Scenario: An owner cannot answer
- **WHEN** the other owners acknowledge their cleanup but one owner cannot prove its state
- **THEN** the result is settled with status unknown and target_retired false
- **AND** repeating the operation does not change that answer

#### Scenario: A store cannot be read this time
- **WHEN** a store that holds the Source's derived state cannot be read during one call
- **THEN** that component reports unknown with a reason that names the failed read
- **AND** the result is not settled and carries retry_after_ms
- **AND** a repeat after the read recovers continues the cleanup to complete

#### Scenario: Host policy is disabled during cleanup
- **WHEN** an operation already holds its cutoff and host policy then stops enabling its identity system
- **THEN** repeating that operation continues its cleanup to a settled result
- **AND** a new operation for any Source of that system refuses

#### Scenario: Cleanup response is lost
- **WHEN** the caller retries its retained operation after restart
- **THEN** cleanup resumes for the same incarnation and reports actual component progress

#### Scenario: A copy reservation never received its native binding
- **WHEN** the catalogue reserved a copy whose native binding was never written
- **THEN** cleanup closes that copy as having no effect and can still report complete

#### Scenario: Earlier copies cannot close
- **WHEN** more copies than one invocation closes precede the target's own copy and no owner can close them
- **THEN** later invocations still reach and close the remaining copies
- **AND** the unclosable copies stay reported unknown and the result is never complete

### Requirement: Snapshot readers cannot republish retired data

Reads overlapping retirement SHALL be allowed to finish from their existing snapshots. After complete retirement, new reads MUST NOT reach the retired incarnation through managed storage or reusable caches. An obsolete builder MUST NOT publish its captured bytes for reuse after invalidation.

#### Scenario: Builder finishes after cache invalidation
- **WHEN** a builder read the Source before retirement but reaches cache insertion after invalidation
- **THEN** its overlapping invocation can finish while the reusable cache rejects that obsolete publication

### Requirement: Attempt cleanup covers pre-reference residue

Capture custody SHALL identify owned artifacts before surviving writes and distinguish new allocations from reused Sources. Settlement SHALL report custody separately from capture outcome. Explicit residue cleanup SHALL require closed admission. Collection MUST preserve the last recovery link while owned residue remains unresolved.

#### Scenario: Attempt reused another successful capture
- **WHEN** an attempt observed a reused Source rather than allocating it
- **THEN** cleaning that attempt's residue does not erase the observed Source

### Requirement: Erasure scope excludes previously exported copies

The operation SHALL describe logical managed erasure. Earlier backups, downloaded exports, caller-held responses, external synchronization history and forensic remnants SHALL remain outside its guarantee. Restoring excluded copies SHALL NOT be described as fresh capture or permanent nonexistence.

#### Scenario: Earlier backup is restored
- **WHEN** a separately retained backup reintroduces an erased Source
- **THEN** the prior receipt describes its completed managed retirement and does not claim that the restored material cannot exist
