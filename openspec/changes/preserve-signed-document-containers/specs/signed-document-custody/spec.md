# Spec Delta

## Purpose

Keep signed document containers and their unchanged payloads recoverable, attributable and searchable under the same access boundary.

## ADDED Requirements

### Requirement: Preserve container and payload bytes through ordinary custody

Source and Evidence capture SHALL preserve an ASiC-E container unchanged. For a supported safe container, capture SHALL publish unchanged DOCX and PDF payloads with their companions in the same custody batch. Each payload SHALL record its parent path, parent SHA-256, exact member path, payload SHA-256 and size. Member names SHALL not overwrite another artifact. Original downloads SHALL return each artifact's exact bytes.

#### Scenario: Capture and retrieve a supported signed container
- **WHEN** a caller captures a safe ASiC-E container with document payloads
- **THEN** the original and payloads have separate custody identities with connected provenance
- **AND** downloading each original returns bytes whose hashes match the input

#### Scenario: Repeat capture after a lost reply
- **WHEN** a caller repeats a completed capture with the same mutation identity
- **THEN** the existing terminal is replayed without fetching or publishing another artifact

### Requirement: Bound inspection and keep failed inspection explicit

Inspection SHALL validate archive paths, aliases, duplicate entries, entry types, encryption, central-directory size, member count and expanded byte limits before publishing payloads. Streaming reads SHALL enforce byte limits and detect incomplete or corrupt members. Inspection SHALL not execute members or fetch external resources. Invalid archives, unsupported payloads and exceeded limits SHALL retain exact original custody and expose an actionable inspection outcome. They SHALL not claim complete payload preservation.

#### Scenario: Unsafe or corrupt archive
- **WHEN** a container has an unsafe path, a corrupt member or an exceeded extraction limit
- **THEN** the unchanged container remains recoverable with failed inspection and no partial child publication

#### Scenario: Unsupported payload
- **WHEN** a structurally safe container includes an unsupported document type
- **THEN** its inspection result identifies the unsupported payload and the supported next action
- **AND** it does not present its metadata as extracted document text

### Requirement: Payload access inherits current parent restrictions

Payload pages, extracted text parts and original downloads SHALL retain every applicable parent restriction, including raw protection and later policy changes. Exomem SHALL validate the child location and exact parent identity before trusting the binding. An unresolved or forged binding SHALL not grant access.

Exomem SHALL check current parent access before selected payload text reaches a scorer or changes visible graph results. Held Markdown and path-only snapshots SHALL enforce the same parent access.

The reserved payload subtree SHALL require a valid binding. Outside it, Exomem SHALL strictly validate every parsed container_member claim. Malformed ordinary notes without a parsed claim SHALL retain their existing path classification.

#### Scenario: Parent access changes after extraction
- **WHEN** a principal loses access to the preserved container
- **THEN** that principal cannot read or find its payload text or download its payload originals

#### Scenario: A payload claims a different parent
- **WHEN** a payload binding does not match its parent identity or deterministic child location
- **THEN** Exomem refuses disclosure through that binding

#### Scenario: A desktop move retains payload provenance
- **WHEN** a moved payload or companion retains its parsed binding outside the reserved subtree
- **THEN** the invalid location refuses original downloads, page reads and projected scoring

#### Scenario: A projected candidate loses parent access
- **WHEN** stored projected scopes still admit a payload whose current parent denies access
- **THEN** the payload text does not reach a scorer or appear in results
- **AND** the payload does not contribute to visible graph ranking or counts

### Requirement: Share byte identity proofs within a request

Read requests SHALL reuse unchanged byte identity proofs through the existing companion proof scope. Each reuse SHALL validate the proof's root, path and current file version. Proof reuse SHALL not cache parent metadata or access decisions. A restricted final recheck SHALL not capture another proof.

#### Scenario: Several payloads share a container
- **WHEN** a read request admits several unchanged payloads from the same container
- **THEN** their checks reuse the container's byte proof within that request
- **AND** each admission still checks current parent restrictions

#### Scenario: Original bytes change during a request
- **WHEN** an original changes after a request captures its byte proof
- **THEN** Exomem refuses reuse of that proof
- **AND** it captures a fresh proof only where the existing scope permits capture

### Requirement: Reuse complete document processing and honest signature states

Preserved payloads SHALL use existing document processing, searchable companions and extraction parts. Missing engines and failed processing SHALL retain preserved bytes and expose existing actionable retry states. A processing retry SHALL not duplicate preserved originals. Structural inspection SHALL report signatures as not verified unless a supported cryptographic verifier actually verifies them under a declared trust policy.

#### Scenario: Extract complete text and retry processing
- **WHEN** document processing is interrupted after custody commits and the caller retries it
- **THEN** processing resumes through the existing jobs without another container or payload
- **AND** the complete extracted text, including its tail, becomes readable and searchable

#### Scenario: Signature material is malformed or untrusted
- **WHEN** a container holds signature material without a successful supported verification
- **THEN** Exomem reports that signatures are not verified and makes no validity or signer-trust claim

### Requirement: Existing opaque containers use explicit processing

Duplicate preservation SHALL keep its existing no-write contract. A caller SHALL be able to request supported inspection and payload processing for an existing opaque container through the existing media command surface. The operation SHALL validate current original bytes and preserve custody and provenance idempotently. No bulk migration SHALL run automatically.

#### Scenario: Process an older opaque container
- **WHEN** an authorized caller explicitly processes an existing ASiC-E original
- **THEN** Exomem verifies its current identity, preserves missing payloads through ordinary custody and queues their extraction
- **AND** repeating that processing request does not duplicate artifacts or replace immutable originals
