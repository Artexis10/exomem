# client-artifact-preservation Specification

## Purpose
TBD - created by archiving change preserve-client-artifacts. Update Purpose after archive.

## Requirements

### Requirement: Canonical batch artifact preservation command

The system SHALL expose a client-neutral `preserve_artifacts` mutation command across MCP, REST, OpenAPI, and CLI. The command SHALL require `scope`, `category`, and a non-empty ordered `files` array supporting at least eight items. Each file object SHALL declare string properties `download_url`, `file_id`, `mime_type`, and `file_name`, require only `download_url` and `file_id`, and permit the two latter metadata values to be omitted. The same command leaf SHALL serve every generated surface.

#### Scenario: ChatGPT supplies eight attachments in one call

- **WHEN** ChatGPT invokes `preserve_artifacts` with eight attached PNG file handles, one scope, and one category
- **THEN** the command accepts all eight file objects in one MCP invocation
- **AND** the MCP descriptor identifies `files` as an OpenAI file-parameter field
- **AND** no binary bytes pass through a model-visible base64 argument

#### Scenario: Another client supplies equivalent handles

- **WHEN** a non-ChatGPT client supplies the documented file objects through MCP, REST, or CLI
- **THEN** the same command leaf processes them without provider-specific branching

### Requirement: Safe bounded server-side retrieval

The system SHALL treat every `download_url` as hostile input. It MUST accept HTTPS only; reject userinfo, fragments, and any initial or redirected destination that resolves to a non-global IPv4 or IPv6 address; connect only to a validated resolved address while preserving the original hostname for TLS verification; send no Exomem credentials, cookies, or caller headers; and keep full URLs and query strings out of logs and results. It SHALL enforce bounded redirects, timeouts, item count, per-file bytes, and aggregate bytes while streaming to private temporary storage, with the streaming byte count authoritative over response headers.

#### Scenario: Public HTTPS attachment is staged

- **WHEN** an authenticated caller supplies a temporary HTTPS URL whose validated destination remains public and whose body stays within all limits
- **THEN** Exomem streams the body to private temporary storage without buffering the full artifact in memory
- **AND** retrieval occurs before the canonical vault mutation boundary is acquired

#### Scenario: Private or redirecting destination is refused

- **WHEN** a supplied URL or any redirect resolves to loopback, private, link-local, multicast, unspecified, reserved, or metadata address space
- **THEN** that file returns a stable safe-fetch failure without making a request to the forbidden destination or writing vault content

#### Scenario: Stream exceeds a bound

- **WHEN** the response header declares an oversized body or the streamed bytes exceed a per-file or aggregate cap
- **THEN** retrieval stops, temporary bytes are removed, the file reports a stable too-large failure, and no partial final artifact is published

### Requirement: Governed append-only persistence and truthful outcomes

Each successfully staged file SHALL be committed through the existing `preserve_stream` Evidence path under the active vault mutation boundary, preserving append-only collision refusal, sanitization, sidecars, hashing, index updates, and media reconciliation behavior. The command SHALL return exactly one ordered outcome per input `file_id`. A stored outcome SHALL include stored path, size, SHA-256, hash algorithm, media ID, and content type; a failed outcome SHALL include a stable code and sanitized reason. One item failure SHALL NOT erase or misreport another item's successful commit.

#### Scenario: Mixed batch outcome

- **WHEN** two files are valid and a third has an expired temporary URL
- **THEN** the valid files are preserved and reported as stored
- **AND** the expired file is reported as failed with no claimed stored path
- **AND** the batch summary reports both stored and failed counts

#### Scenario: Missing filename uses deterministic fallback

- **WHEN** a valid file object omits `file_name`
- **THEN** Exomem derives a sanitized deterministic filename from the staged SHA-256 and available MIME extension before preservation

#### Scenario: Identical mutation is retried

- **WHEN** an MCP client retries the byte-identical mutating call within the implicit replay window
- **THEN** Exomem returns the cached terminal batch result without fetching or writing the files again

### Requirement: Capability-driven fallback remains available

The system SHALL retain `transfer_artifact(operation="upload")` and `/upload` for clients that cannot expose attachment handles. Bootstrap, tool descriptions, and scaffold guidance SHALL select the destination lane before the transport: raw material to the Sources capture command and proof-bearing material to the Evidence preservation commands. Having selected the lane, guidance SHALL route capable clients to the file-handle path for that lane and other clients to the existing out-of-band upload path, which SHALL carry the selected lane on the minted capability. Guidance MUST NOT claim token minting proves byte transfer and MUST name `operation`, not the nonexistent `mode` parameter.

#### Scenario: Claude lacks a direct file handle

- **WHEN** a Claude runtime can access an attachment locally but cannot populate the file-handle parameter of the command for the selected lane
- **THEN** guidance directs it to mint an upload capability with `transfer_artifact(operation="upload")` naming that lane, and POST the bytes to `/upload`
- **AND** preservation is considered successful only after the response includes stored path, size, and digest

#### Scenario: ChatGPT sandbox lacks egress

- **WHEN** ChatGPT can populate file parameters but Code Interpreter cannot resolve the Exomem host
- **THEN** guidance directs ChatGPT to call the file-handle command for the selected lane directly
- **AND** no client-side `curl` step is attempted

#### Scenario: An attachment is raw material rather than proof

- **WHEN** an attached transcript, article, or screenshot is being kept as material for later compilation
- **THEN** guidance selects the Sources lane before considering which transport the client supports
- **AND** the artifact is not routed to Evidence merely because the file-handle path was convenient
