## Purpose

Make hosted documents, images and audio useful through bounded asynchronous processing while preserving tenant privacy, durable originals and predictable interactive service.

## ADDED Requirements

### Requirement: Preservation is independent of media processing availability

Authorized hosted document, image and audio upload/download SHALL remain available within existing governance, transfer and storage limits independently of OCR, visual indexing or transcription availability. Preservation SHALL retain original bytes and their provenance. Status MUST distinguish preservation from completed indexing and give the owner an actionable processing state.

#### Scenario: Audio upload arrives with no available processing budget

- **WHEN** an authorized audio upload fits storage and transfer limits but ASR admission is unavailable
- **THEN** the original is preserved and remains downloadable
- **AND** its processing state reports the budget or capacity block without claiming that a transcript exists

### Requirement: Hosted execution extends the existing durable media authority

Hosted local and remote processing SHALL use the existing tenant-bound job and governed sidecar lifecycle. Scheduling projections MUST NOT become a second source of job completion truth. Attempts SHALL be fenced, attributable to an immutable input and pinned processing profile, and recoverable without duplicate canonical publication.

#### Scenario: Worker acknowledgment is lost

- **WHEN** a worker retries a completed attempt after losing its acknowledgment
- **THEN** the cell resolves the existing completion without publishing duplicate sidecars or repeating extraction solely because derived-index fanout failed

#### Scenario: Scheduling projection is lost

- **WHEN** a shared scheduler loses its reconstructible hints
- **THEN** pending work can be recovered from cell authorities
- **AND** loss of the projection cannot create a completed job or bypass resource admission

### Requirement: Worker authority covers only one admitted artifact operation

Workers SHALL receive only the data and bounded operation authority required for their current admitted job. Access SHALL bind worker identity, tenant, attempt, fenced lease, immutable artifact identity, operation, byte limit and expiry. Workers MUST NOT receive whole-vault access, general service credentials, wrapping roots, arbitrary fetch authority or canonical mutation permission.

#### Scenario: Worker requests another artifact or tenant

- **WHEN** a worker changes the artifact, operation, tenant or destination of a job capability
- **THEN** the request fails before content or existence is disclosed

#### Scenario: Signed capability outlives its job authorization

- **WHEN** the lease is revoked, replaced or deletion-sealed before retrieval or result admission
- **THEN** access fails even if the capability's signature and original expiry remain valid

#### Scenario: Active stream is revoked

- **WHEN** deletion, cancellation or lease replacement revokes a capability during artifact streaming
- **THEN** the service checks revocation before further bounded reads, stops the stream within its documented cancellation bound and refuses retries under the revoked attempt
- **AND** partial worker plaintext enters tracked cleanup, with quarantine on uncertain cleanup and no claim that previously delivered bytes can be recalled

#### Scenario: Broker tries to expand a valid job grant

- **WHEN** a content-bearing broker attempts to enumerate tenants or reuse a job grant for another artifact, tenant or operation
- **THEN** the cell refuses disclosure and the broker has no independent key or service authority to bypass that refusal
- **AND** the broker remains disclosed as a plaintext processor for the authorized bytes it proxies

### Requirement: Processing is sandboxed and result publication rechecks authority

Each execution sandbox SHALL isolate one tenant job, enforce decoded-input, output, CPU, memory and time limits, and restrict network and filesystem access. Returned data SHALL be treated as untrusted. Only the cell SHALL publish results after rechecking input identity, governance, lease and lifecycle state through existing mutation and recovery boundaries. Unverified cleanup MUST prevent sandbox reuse.

#### Scenario: Small input expands beyond its processing limit

- **WHEN** a compressed document, image or audio artifact exceeds its admitted decoded size or runtime limit
- **THEN** the worker stops with a bounded actionable failure
- **AND** original bytes remain intact and other tenants retain service

#### Scenario: Late result arrives after replacement or deletion

- **WHEN** a result refers to an artifact or attempt that is no longer current
- **THEN** the cell rejects canonical publication
- **AND** cleanup proceeds without resurrecting deleted content or derived indexes

#### Scenario: Result publication races deletion or artifact replacement

- **WHEN** deletion or replacement races a worker result after preliminary validation
- **THEN** the decisive checks and publication share a serialized, fenced mutation boundary with that operation
- **AND** an earlier deletion/replacement rejects the stale result, while a later deletion/replacement removes or supersedes the completed derived content
- **AND** restart recovers consistent job completion, canonical sidecars and durable index receipts without resurrecting the old artifact

### Requirement: Hosted processing profiles preserve modality and model boundaries

The hosted media offering SHALL include document extraction and OCR, with separately admitted CLIP visual indexing and timestamped ASR profiles. Prose-emitting model transducers SHALL require explicit profile selection and pinned model identity. Processing MUST preserve the pure-substrate rules and MUST NOT add instruction-following or reasoning models. Diarization SHALL remain disabled and outside this change's acceptance criteria.

#### Scenario: Selected OCR profile processes a scanned document

- **WHEN** a supported scanned document is preserved and its selected OCR profile obtains admission
- **THEN** extracted text and supported page provenance become searchable through the existing governed result path
- **AND** the original document remains unchanged

#### Scenario: CPU document profile runs independently of heavy media inference

- **WHEN** the hosted document/OCR profile is installed without ASR, CLIP or CUDA dependencies
- **THEN** supported PDF, DOCX, XLSX and PPTX parsing and configured OCR remain available through the existing extraction authority
- **AND** conversion makes no external inference request or macro/external-link execution, while original bytes remain available independently of conversion success

#### Scenario: Spreadsheet or office structure informs retrieval

- **WHEN** a supported multi-sheet workbook, table-containing document or presentation is extracted
- **THEN** fixture-verified text and available sheet/table/slide context enter the governed search representation
- **AND** the system does not claim that extraction reproduces full layout, executes macros or recalculates formulas

#### Scenario: Visual search queries an indexed image

- **WHEN** an enabled CLIP profile receives a supported visual query
- **THEN** it uses the encoder identity compatible with the stored image vectors
- **AND** interactive query processing does not wait for remote GPU provisioning

#### Scenario: ASR completes without diarization

- **WHEN** an admitted audio job produces a transcript
- **THEN** it preserves timestamped segments and accurate processing provenance
- **AND** absent speaker attribution does not prevent successful transcription

### Requirement: Tenant fairness preserves interactive and recovery capacity

Heavy jobs SHALL obey both tenant and global concurrency, memory and runtime admission limits. Initial admission SHALL permit at most one heavy job globally and one per tenant, with bounded fair scheduling. Exhausted capacity SHALL queue work rather than borrow reserved interactive/recovery resources or bypass the existing user-cell cap.

User-cell and recovery admission SHALL enforce the same approved policy across signed contracts, upstream gates and provisioner reservations, including the union of observed resources and outstanding reservations. A permissive implementation MUST NOT establish a larger supported cohort. Evaluating five cells SHALL require distinct five-cell resource and recovery evidence before a separately reviewed policy increase.

#### Scenario: Outstanding reservations exceed the observed cell count

- **WHEN** concurrent requests would exceed the approved user, recovery or attachment limit after combining observations with retained reservations
- **THEN** admission rejects the excess before provisioning resources
- **AND** a fresh signed observation with fewer materialized cells does not reset reserved capacity

#### Scenario: A five-client benchmark uses only two cells

- **WHEN** latency acceptance exercises five clients across two synthetic cells
- **THEN** the receipt identifies that scope and does not establish five-cell capacity
- **AND** admitting a fifth cell requires whole-node demand, recovery and attachment evidence under the revised policy

#### Scenario: One tenant submits a large backlog

- **WHEN** another tenant has an eligible queued job
- **THEN** bounded scheduling gives the other tenant an admission opportunity after the running bounded job
- **AND** the first tenant cannot occupy all later slots merely by queue length

#### Scenario: Background processing harms the accepted latency envelope

- **WHEN** the configured processing profile fails concurrent interactive or recovery acceptance
- **THEN** that profile remains disabled or receives tighter admission
- **AND** the system does not raise capacity claims based only on a model benchmark

### Requirement: Paid compute reserves cost before allocation

Paid compute SHALL default to a zero budget and require explicit funded configuration. Admission SHALL atomically reserve conservative total allocation cost against tenant and global limits before provisioning. Reservations SHALL include finite runtime, startup, billing granularity, ancillary resources, retries and cleanup. Unknown prices or unresolved allocations MUST block further unsafe admissions.

#### Scenario: Concurrent jobs compete for the remaining budget

- **WHEN** admitting both jobs would exceed either budget
- **THEN** only the affordable reservation succeeds
- **AND** the rejected job remains preserved and visibly blocked without allocating a paid resource

#### Scenario: Provider deletion cannot be confirmed

- **WHEN** a worker expires or crashes and provider resource deletion is unconfirmed
- **THEN** the independent cleanup mechanism retries, costs remain reserved across budget rollover, and the operator is alerted
- **AND** admission does not treat a stopped process as proof that billing ended

### Requirement: Storage growth accounts for physical and historical copies

Logical storage entitlements SHALL be distinct from physical provisioning, transfer ceilings, derived indexes and retained backup versions. The future 10 GB tier SHALL mean 10,000,000,000 logical canonical bytes and MUST NOT activate until physical and recovery headroom is proved. Larger tiers SHALL require tenant-scoped incremental encrypted backup and verified cost/capacity evidence; this change MUST NOT provision 100 GB or 1 TB per tenant.

#### Scenario: Logical limit would consume the entire physical volume

- **WHEN** a proposed entitlement leaves insufficient space for indexes, temporary writes or safe recovery
- **THEN** activation fails before the larger entitlement is published

#### Scenario: Unchanged media appears in multiple snapshots

- **WHEN** incremental backup creates another retained snapshot referencing unchanged tenant media
- **THEN** it reuses protected objects within that tenant while retaining independently verifiable manifests
- **AND** garbage collection cannot remove an object still needed by a retained snapshot or reveal another tenant's possession of identical content

#### Scenario: Snapshot publication races interrupted garbage collection

- **WHEN** a new snapshot reuses objects while garbage collection runs or restarts after a crash
- **THEN** durable object publication, manifest publication and reachability revalidation are fenced so no retained published snapshot references a deleted object
- **AND** Object Lock failures and unconfirmed deletions remain safely replayable, with restore proof before old full archives retire
