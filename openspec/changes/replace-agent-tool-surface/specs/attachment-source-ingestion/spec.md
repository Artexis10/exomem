## MODIFIED Requirements

### Requirement: Lossless attachment capture into Sources

The system SHALL allow an attached file to be captured as raw source material
without its bytes passing through the model. `save_source` SHALL accept the
same ordered client file handles as `save_evidence`, requiring only
`download_url` and `file_id` per file and permitting `mime_type` and `file_name`
to be omitted. Handles SHALL be staged through the same bounded, safe server-side
retrieval used by `save_evidence`, with identical treatment of hostile URLs,
redirect limits, timeouts, item counts, and byte caps. Each staged file SHALL be
persisted under `Sources/` with the source taxonomy applied, SHALL be written
once, and SHALL NOT be duplicated into a second tree. The command SHALL return
exactly one ordered outcome per input `file_id`.

#### Scenario: An attached transcript is captured as a Source

- **WHEN** a client invokes `save_source` with a text file handle, a title, and a source kind
- **THEN** the file's bytes are stored under `Sources/` and a source page is created for it
- **AND** no binary or base64 payload appears in a model-visible argument
- **AND** the stored source carries the supplied kind, domain, and project associations

#### Scenario: An attached image is captured as a Source

- **WHEN** a client invokes `save_source` with an image file handle
- **THEN** the image is stored under `Sources/` with its page marked as media awaiting extraction
- **AND** the existing extraction path converges on that page without lane-specific handling

#### Scenario: A client without file handles reaches the same lane

- **WHEN** a client that cannot expose file handles requests an upload capability for source material
- **THEN** the minted capability names the Sources destination
- **AND** bytes posted against that capability are stored as a Source rather than as Evidence

#### Scenario: A retried identical capture does not store twice

- **WHEN** a client retries the byte-identical attachment capture within the implicit replay window
- **THEN** the cached terminal result is returned without retrieving or writing the files again

### Requirement: The lane is stated, never inferred

The system SHALL determine an artifact's lane from the command invoked and from
explicit destination parameters only. It SHALL NOT infer the lane from MIME type,
filename, extension, or content. `save_source` SHALL persist to Sources and
`save_evidence` SHALL persist to Evidence, on every
transport that carries them.

#### Scenario: Identical bytes reach different lanes by command

- **WHEN** the same file is captured once through `save_source` and once through `save_evidence`
- **THEN** the first is stored under `Sources/` and the second under `Evidence/`
- **AND** neither outcome depends on the file's type or name

#### Scenario: Guidance routes by intent before capability

- **WHEN** routing guidance describes capturing an attached file
- **THEN** it selects the lane from whether the artifact is raw material or proof-bearing
- **AND** only then selects the transport from what the client can supply

### Requirement: Existing Evidence semantics are preserved

The system SHALL keep `save_evidence` persisting to
Evidence with their existing safe-fetch, append-only collision refusal, per-file
outcome, and replay behavior unchanged. Sources-to-Evidence promotion SHALL
remain the only reclassification between the two lanes, SHALL remain one-way, and
SHALL continue to require a stated reason. Artifacts already stored under
Evidence SHALL NOT be moved by this capability.

#### Scenario: Explicit Evidence capture is unaffected

- **WHEN** a caller preserves proof-bearing material through the Evidence commands
- **THEN** the artifact is stored under `Evidence/` exactly as before
- **AND** its per-file outcomes and failure codes are unchanged

#### Scenario: Promotion still requires a reason and one direction

- **WHEN** a stored Source is promoted into Evidence
- **THEN** the promotion is refused unless a reason is supplied
- **AND** the reverse move remains refused

#### Scenario: Previously stored artifacts become citable in place

- **WHEN** an artifact stored before this capability is back-filled
- **THEN** it gains a page and becomes citable
- **AND** it remains in the lane it was originally stored in

### Requirement: MCP authorization admits an exact-byte upload handoff

`upload_files` SHALL accept upload with `hold=true`, a destination lane and `expected_sha256`.
It SHALL issue a single-use capability bound to the current authorized client and login generation, deployment, vault and lane.
Issuance SHALL NOT commit a Source. Upload SHALL revalidate authority before receiving bytes.

#### Scenario: A managed HTTP agent uploads without a local credential

- **WHEN** an authenticated HTTP MCP client prepares a Source upload and consumes it with `exomem attach --handoff-file`
- **THEN** the CLI sends the file bytes through that capability without reading another credential
- **AND** the response supplies a held file handle redeemable by the originating authorization binding
- **AND** `save_source` preserves the original bytes through the existing artifact writer

#### Scenario: An upload capability cannot expand authority

- **WHEN** a capability expires, is replayed, has revoked authority, or names another lane
- **THEN** the service refuses it before admitting uploaded bytes
- **AND** another session cannot redeem the resulting held handle

### Requirement: Source capture verifies the caller's intended bytes

`save_source` SHALL accept optional `expected_sha256` as 64 lowercase hexadecimal characters.
For text it SHALL hash the supplied UTF-8 bytes; for one file it SHALL hash the original bytes.
A mismatch SHALL return `SOURCE_HASH_MISMATCH` before capture commits.
The assertion SHALL NOT describe the rendered Source page. Omitting it SHALL preserve ordinary capture behavior.

#### Scenario: A retyped tool result changes a timestamp

- **WHEN** supplied text differs from the caller's original SHA-256
- **THEN** capture refuses with a clear integrity error and stores no Source

#### Scenario: A Cloud attachment verifies its original

- **WHEN** a Cloud client supplies one file handle and its intended SHA-256
- **THEN** capture checks the retrieved original before publishing its Source
- **AND** a mismatched original commits nothing

### Requirement: Source attachment capture supports exact selected-artifact adoption
`save_source` SHALL accept the same optional validated single-artifact adoption envelope as `save_evidence` while preserving ordinary Source attachment behavior. The envelope SHALL name one supplied `file_id`, an explicit trigger, and a scoped durable adoption key. Only that handle SHALL commit. The exact bytes and versioned adoption block SHALL publish atomically in the Source lane, with the Source artifact page as the canonical receipt owner. The block and stable response SHALL bind the trigger, key digest, selected identifier, Source lane, destination, path, SHA-256, size, content type, and media identity.

Transport-window and durable adoption replay SHALL use the same semantics and stable failures as Evidence adoption: cached transport replay performs no second fetch; replay after expiry re-stages and hashes bytes; mismatch fails `ADOPTION_KEY_REUSED`; unavailable bytes fail `ADOPTION_REPLAY_UNVERIFIABLE`; no failure writes or claims replay.

#### Scenario: Existing Source handle capture remains compatible
- **WHEN** an existing client calls `save_source` with file handles and no adoption envelope
- **THEN** its ordered outcomes, exact-byte Source storage, source taxonomy, source page, and replay behavior remain unchanged

#### Scenario: Selected reasoning artifact commits to Source only
- **WHEN** an adoption envelope selects one reasoning-input artifact from several handles supplied to `save_source`
- **THEN** only that handle is stored under Sources and its Source artifact page carries the committed adoption block
- **AND** no sibling or Evidence artifact is written

#### Scenario: Source receipt matches Evidence vocabulary
- **WHEN** the same synthetic bytes are separately adopted once as Source and once as Evidence under distinct keys
- **THEN** both receipts expose the same trigger, key-digest, selected-id, hash, size, content-type, and media-identity fields
- **AND** their lane, destination, path, and canonical companion/page correctly differ

#### Scenario: Durable Source replay reproves bytes
- **WHEN** a Source adoption key is retried after transport replay expiry
- **THEN** `save_source` re-stages and hashes the selected handle before returning the original receipt
- **AND** changed or unavailable bytes fail closed without another Source write
