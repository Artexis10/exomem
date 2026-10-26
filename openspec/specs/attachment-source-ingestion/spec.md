# attachment-source-ingestion Specification

## Purpose
Let an attached file be captured losslessly into Sources without its bytes passing through the model. Every stored artifact gets exactly one addressable page, citations to an artifact or its page resolve to the same page, the lane is determined only by the command and explicit parameters, and Evidence semantics are unchanged.

## Requirements

### Requirement: Lossless attachment capture into Sources

The system SHALL allow an attached file to be captured as raw source material
without its bytes passing through the model. `capture_source` SHALL accept the
same ordered client file handles as `preserve_artifacts`, requiring only
`download_url` and `file_id` per file and permitting `mime_type` and `file_name`
to be omitted. Handles SHALL be staged through the same bounded, safe server-side
retrieval used by `preserve_artifacts`, with identical treatment of hostile URLs,
redirect limits, timeouts, item counts, and byte caps. Each staged file SHALL be
persisted under `Sources/` with the source taxonomy applied, SHALL be written
once, and SHALL NOT be duplicated into a second tree. The command SHALL return
exactly one ordered outcome per input `file_id`.

#### Scenario: An attached transcript is captured as a Source

- **WHEN** a client invokes `capture_source` with a text file handle, a title, and a source kind
- **THEN** the file's bytes are stored under `Sources/` and a source page is created for it
- **AND** no binary or base64 payload appears in a model-visible argument
- **AND** the stored source carries the supplied kind, domain, and project associations

#### Scenario: An attached image is captured as a Source

- **WHEN** a client invokes `capture_source` with an image file handle
- **THEN** the image is stored under `Sources/` with its page marked as media awaiting extraction
- **AND** the existing extraction path converges on that page without lane-specific handling

#### Scenario: A client without file handles reaches the same lane

- **WHEN** a client that cannot expose file handles requests an upload capability for source material
- **THEN** the minted capability names the Sources destination
- **AND** bytes posted against that capability are stored as a Source rather than as Evidence

#### Scenario: A retried identical capture does not store twice

- **WHEN** a client retries the byte-identical attachment capture within the implicit replay window
- **THEN** the cached terminal result is returned without retrieving or writing the files again

### Requirement: Every ingested artifact is addressable

The system SHALL create exactly one markdown page for every artifact it stores,
in both the Sources and Evidence lanes, regardless of media type and regardless
of whether a description or extracted text was supplied. That page SHALL carry
`type: source`, a stable `exomem_id`, an `ingested_into` list, a vault-relative
pointer to the stored bytes, the artifact's original filename, its SHA-256, and
its byte count. The page body SHALL describe the artifact rather than be empty.
An artifact SHALL NOT be stored without such a page.

#### Scenario: A text artifact receives a page

- **WHEN** a text artifact is stored with no description and no extracted text
- **THEN** a page is written alongside it carrying a stable identifier, an empty `ingested_into`, and the artifact pointer
- **AND** the artifact is discoverable through ordinary corpus retrieval by that page

#### Scenario: A media artifact keeps its extraction contract

- **WHEN** a media artifact is stored without supplied text
- **THEN** its page declares the media type, marks extraction as pending, and carries the anchor the extraction worker fills
- **AND** the completed extraction is written into that same page

#### Scenario: No empty page is written

- **WHEN** a page would carry no description, no extracted text, and no artifact metadata
- **THEN** no such page is written

### Requirement: A cited artifact path resolves to its page

The system SHALL resolve a source citation that names either a stored artifact
or its page to the same page, and SHALL append the citing note's back-reference
to that page's `ingested_into`. Resolution SHALL try the page naming convention
used when artifacts are stored before falling back to extension replacement, so
that citations which resolve today continue to resolve.

#### Scenario: A compiled note cites the artifact

- **WHEN** a note is created citing the stored artifact's path in `sources`
- **THEN** the artifact's page receives the note's back-reference in `ingested_into`
- **AND** no source-not-found warning is emitted

#### Scenario: A compiled note cites the page

- **WHEN** a note is created citing the artifact's page path instead
- **THEN** the same page receives the same back-reference

#### Scenario: An existing citation keeps working

- **WHEN** a note cites an ordinary source page whose name already ends in `.md`
- **THEN** it resolves exactly as before

### Requirement: The lane is stated, never inferred

The system SHALL determine an artifact's lane from the command invoked and from
explicit destination parameters only. It SHALL NOT infer the lane from MIME type,
filename, extension, or content. `capture_source` SHALL persist to Sources and
`preserve_artifacts` and `preserve_evidence` SHALL persist to Evidence, on every
transport that carries them.

#### Scenario: Identical bytes reach different lanes by command

- **WHEN** the same file is captured once through `capture_source` and once through `preserve_artifacts`
- **THEN** the first is stored under `Sources/` and the second under `Evidence/`
- **AND** neither outcome depends on the file's type or name

#### Scenario: Guidance routes by intent before capability

- **WHEN** routing guidance describes capturing an attached file
- **THEN** it selects the lane from whether the artifact is raw material or proof-bearing
- **AND** only then selects the transport from what the client can supply

### Requirement: Existing Evidence semantics are preserved

The system SHALL keep `preserve_artifacts` and `preserve_evidence` persisting to
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

`transfer_artifact` SHALL accept upload with `hold=true`, a destination lane and `expected_sha256`.
It SHALL issue a single-use capability bound to the current authorized client and login generation, deployment, vault and lane.
Issuance SHALL NOT commit a Source. Upload SHALL revalidate authority before receiving bytes.

#### Scenario: A managed HTTP agent uploads without a local credential

- **WHEN** an authenticated HTTP MCP client prepares a Source upload and consumes it with `exomem attach --handoff-file`
- **THEN** the CLI sends the file bytes through that capability without reading another credential
- **AND** the response supplies a held file handle redeemable by the originating authorization binding
- **AND** `capture_source` preserves the original bytes through the existing artifact writer

#### Scenario: An upload capability cannot expand authority

- **WHEN** a capability expires, is replayed, has revoked authority, or names another lane
- **THEN** the service refuses it before admitting uploaded bytes
- **AND** another session cannot redeem the resulting held handle

### Requirement: Upload handoffs state their limits and completion

A handoff SHALL return `token`, `upload_url`, `ttl_seconds`, `hold=true`, `lane`, `expected_sha256`, `max_bytes`, `handoff_status=handoff_prepared` and `committed=false`.
Its lifetime SHALL NOT exceed 900 seconds or its upload cap 100 MiB. A smaller deployment cap SHALL apply.
The upload SHALL enforce the byte cap while receiving, including requests without Content-Length.

#### Scenario: Uploading bytes does not claim Source custody

- **WHEN** an upload finishes with matching bytes
- **THEN** it returns a held file handle with the verified SHA-256 and byte count
- **AND** only the later capture receipt proves durable Source custody

#### Scenario: The published tool returns a usable capability

- **WHEN** normal response filtering projects an issued handoff
- **THEN** the client receives its usable opaque token unchanged
- **AND** the implementation retains mandatory response filtering

#### Scenario: Cloud implements the same handoff

- **WHEN** the Cloud gateway implements upload handoffs
- **THEN** it binds the capability to the authenticated tenant, originating authorization and lane
- **AND** it applies the same lifetime, byte cap, hash assertion and completion semantics
- **AND** it exposes no cell filesystem path or unauthenticated cell listener
- **AND** Cloud retains its transfer exclusion until the gateway workflow is verified

### Requirement: Source capture verifies the caller's intended bytes

`capture_source` SHALL accept optional `expected_sha256` as 64 lowercase hexadecimal characters.
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

### Requirement: Exact-byte guidance states client capabilities

Tool guidance SHALL direct agents to original handles or upload handoffs for material they did not author.
The CLI SHALL refuse traversal, symlinks and special files outside its selected read root.
The service SHALL receive bytes rather than a caller-selected server path.
Standalone stdio without a verified HTTP session and Cloud without a gateway bridge SHALL report that limitation explicitly.

#### Scenario: A file path escapes the selected root

- **WHEN** the CLI receives a traversal path or symlink through handoff mode
- **THEN** it refuses before sending bytes
- **AND** no server path is opened on the caller's behalf
