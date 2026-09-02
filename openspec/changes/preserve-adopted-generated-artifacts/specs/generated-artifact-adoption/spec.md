## ADDED Requirements

### Requirement: Explicit adoption gate keeps drafts ephemeral
The active agent SHALL keep generated candidates ephemeral unless the user explicitly selects or approves one offered artifact. Generation completion, preview, revision, rejection, filename, MIME type, apparent quality, anticipated delivery, and abandonment SHALL NOT constitute adoption. A definite sent or published report about one uniquely identified offered artifact SHALL require local exact-byte adoption before any delivery observation is recorded. These facts establish eligibility, not write confirmation; agent-initiated adoption and delivery SHALL still obey the `proactive_capture` disposition.

#### Scenario: Unadopted variants cause no writes
- **WHEN** several variants are generated and none is selected, approved, sent, or published
- **THEN** the agent invokes no Source, Evidence, Record, or adoption-receipt write for those variants

#### Scenario: Only the selected variant is eligible
- **WHEN** the user selects variant B from offered variants A, B, and C
- **THEN** only B is eligible for canonical preservation and A and C remain absent

#### Scenario: Rejected variant stays absent
- **WHEN** the user rejects B and later selects C
- **THEN** B remains absent even if it was rendered or downloaded locally

#### Scenario: Delivery report preserves before recording
- **WHEN** the user reports that one uniquely identified offered artifact was sent but no local adoption receipt exists
- **THEN** the agent first attempts exact-byte preservation of that artifact and does not record delivery until a committed local receipt exists

### Requirement: Adoption commits exact original bytes and a bound receipt
Successful adoption SHALL atomically preserve the exact offered bytes and a durable receipt in the lane's canonical page, bound to the explicit trigger, scoped adoption-key digest, selected `file_id`, Source-or-Evidence lane, destination, stored path, SHA-256 algorithm and digest, byte size, content type, and media identity. The success projection SHALL set `committed=true`. A textual description, transcription, OCR result, re-encoding, base64 reconstruction through the model, failed outcome, token-only response, or receipt missing any required identity field MUST NOT count as adoption success.

#### Scenario: Cross-media bytes round-trip exactly
- **WHEN** synthetic PNG, PDF, audio, video, slide, spreadsheet, and text outputs are adopted
- **THEN** each stored file is byte-for-byte equal to its offered bytes and its receipt hash and size match

#### Scenario: Textual substitute is rejected
- **WHEN** a caller attempts to substitute a description, OCR text, or re-encoded content for the selected bytes
- **THEN** the identity check fails and the agent does not claim adoption success

#### Scenario: Artifact and receipt are atomic
- **WHEN** a fault is injected before publication of the adoption write set
- **THEN** neither canonical artifact nor adoption receipt becomes visible

#### Scenario: Media warning does not erase custody
- **WHEN** exact bytes and receipt commit but media reconciliation warns
- **THEN** adoption remains successful with the warning and the original-byte receipt remains authoritative

### Requirement: Durable adoption identity is idempotent and variant-safe
Within the transport request-replay window, the system SHALL replay a matching cached terminal request without refetch or canonical rewrite. After that window, the system SHALL use the durable scoped adoption key to locate the vault-owned receipt, re-stage and hash the presented handle, and compare trigger, selected identifier, lane, destination, byte identity, content type, and media identity. Exact identity SHALL return the original receipt without canonical rewrite. Any mismatch SHALL fail closed with `ADOPTION_KEY_REUSED`. When the offered bytes are unavailable or expired and identity cannot be reproved, the request SHALL fail with `ADOPTION_REPLAY_UNVERIFIABLE`, claim no replay, and write nothing. Request idempotency and durable adoption identity SHALL remain distinct.

#### Scenario: Lost acknowledgement replays one commit
- **WHEN** a caller repeats an outcome-unknown request with the same transport and adoption keys
- **THEN** the original receipt is returned with no second fetch or canonical write

#### Scenario: Later session replays durable adoption
- **WHEN** the request replay window has expired but a later session presents the same adoption identity
- **THEN** the handle is re-staged and hashed and the vault-owned receipt is returned only when every committed identity field matches
- **AND** no second canonical write occurs

#### Scenario: Same key cannot select a sibling
- **WHEN** an adoption key previously committed variant B and is reused for variant C
- **THEN** the request fails with `ADOPTION_KEY_REUSED` and C is not written

#### Scenario: Same apparent handle with changed bytes fails
- **WHEN** a previously committed file identifier resolves to different bytes under the same adoption key
- **THEN** the request fails `ADOPTION_KEY_REUSED` and the original receipt remains unchanged

#### Scenario: Expired durable replay is unverifiable
- **WHEN** the request replay window has expired and the presented handle bytes can no longer be retrieved
- **THEN** the request fails `ADOPTION_REPLAY_UNVERIFIABLE` without claiming replay or writing data

### Requirement: Semantic destination precedes byte transport
The active agent SHALL decide Source versus Evidence from the artifact's role before selecting command and transport. Original material intended for later reasoning SHALL use `capture_source`; an approved final output, deliverable, correspondence, or proof-bearing result SHALL use `preserve_artifacts`. Both commands SHALL use the same adoption identity and exact-byte receipt fields. MIME type and extension MUST NOT determine that choice.

#### Scenario: Same MIME type has different semantic destinations
- **WHEN** one PDF is raw research input and another PDF is an approved final deliverable
- **THEN** the first follows Source preservation and the second follows Evidence adoption despite identical media types

#### Scenario: Missing direct handle is reported honestly
- **WHEN** the active client cannot supply the selected artifact bytes directly
- **THEN** the agent reports an unavailable or non-committing handoff state and does not claim that the artifact was saved

### Requirement: External delivery is a separate observed event
A definite sent, published, or delivered event SHALL be represented separately from local adoption. After a local receipt exists, the active agent MAY write the observation only to a compatible existing Records collection and SHALL link it through the collection's declared link field to the preserved artifact companion. Remote byte identity SHALL remain unverified unless a platform receipt or export proves a matching digest.

#### Scenario: Reported delivery links local Evidence
- **WHEN** the user reports sending an adopted PDF and a compatible Records collection exists
- **THEN** the Record links the Evidence companion and states that delivery was reported without asserting remote byte equality

#### Scenario: Platform proof verifies remote bytes
- **WHEN** a platform receipt or export supplies a digest matching the local adoption receipt
- **THEN** the Record may mark external byte identity as verified and retain the platform reference

#### Scenario: No compatible collection does not block adoption
- **WHEN** no compatible Records collection exists
- **THEN** adoption remains successful and the agent does not silently create a collection or delivery Record

#### Scenario: Tentative delivery stays unwritten
- **WHEN** the only statement is that an artifact was probably published
- **THEN** no observed delivery Record is written
