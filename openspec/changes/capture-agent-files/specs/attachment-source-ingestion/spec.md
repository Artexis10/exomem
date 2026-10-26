## ADDED Requirements

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
