# client-artifact-preservation Specification

## Purpose
Let any client preserve attached artifacts into governed Evidence through one batch `preserve_artifacts` command across MCP, REST, OpenAPI and CLI. It treats download URLs and destination segments as hostile input, persists append-only with truthful per-file outcomes, and retains the upload fallback for clients without attachment handles.

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

The system SHALL treat every `download_url` as hostile input. It MUST accept HTTPS only; reject userinfo, fragments, and any initial or redirected destination that resolves to a non-global IPv4 or IPv6 address; connect only to a validated resolved address while preserving the original hostname for TLS verification; send no Exomem credentials, cookies, or caller headers; and keep full URLs and query strings out of logs and results. It SHALL enforce bounded redirects, timeouts, item count, per-file bytes, and aggregate bytes while streaming to private temporary storage, with the streaming byte count authoritative over response headers. Retrieval SHALL complete before the canonical vault mutation boundary is acquired, for every command that stages client file handles and not only for the Evidence lane, so that no remote latency is served while the vault mutation lock is held.

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

#### Scenario: Source-lane retrieval runs outside the mutation lock

- **WHEN** a caller invokes `capture_source` with file handles rather than text
- **THEN** every handle is staged before the vault mutation boundary is acquired
- **AND** each staged file is committed under its own acquisition of that boundary
- **AND** a `capture_source` invocation carrying text instead of handles still runs under the boundary held across its whole leaf

#### Scenario: The wide-boundary kill switch still applies

- **WHEN** `EXOMEM_WIDE_MUTATION_BOUNDARY` is set and a caller stages file handles through either lane
- **THEN** the vault mutation boundary is acquired before retrieval begins

### Requirement: Governed append-only persistence and truthful outcomes
Each successfully staged file SHALL be committed through the existing `preserve_stream` Evidence path under the active vault mutation boundary, preserving append-only collision refusal, sanitization, sidecars, hashing, index updates, and media reconciliation behavior. The command SHALL return exactly one ordered outcome per input `file_id`, and the batch SHALL carry the mutation's `request_id` and, when available, `receipt_id`. Each outcome SHALL carry a terminal `state` in `stored`, `already_stored`, or `failed`. A stored outcome SHALL include stored path, stable ref, size, SHA-256, hash algorithm, media ID, and content type; an `already_stored` outcome SHALL name the existing path and ref of the artifact under the same destination whose SHA-256 equals the staged bytes, and SHALL write nothing; a failed outcome SHALL include a stable code and sanitized reason. The batch summary SHALL count all three states. One item failure SHALL NOT erase or misreport another item's successful commit. The index and graph fan-out that media reconciliation triggers SHALL NOT run while a preservation mutation guard is held, and SHALL NOT run before the batch terminal is persisted; it SHALL take the deferred derived path that note writes use, or run after terminal persistence when no derived path is available, and the batch's `derived_sync` SHALL report it.

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

#### Scenario: Duplicate bytes arrive under a new identity

- **WHEN** a client retries a preservation with a different request identity and one staged file's SHA-256 already exists under the same `Evidence/<scope>/<category>/` destination
- **THEN** that file's outcome is `already_stored` with the existing path and ref, no new artifact or sidecar is written, and the audit chain does not advance for it
- **AND** the other files in the batch are stored normally

#### Scenario: Acknowledgement is lost after the batch commits

- **WHEN** every file in a batch commits and deterministic fault injection drops the response before the client receives it
- **THEN** a same-identity retry returns the persisted batch terminal with the same per-file paths, hashes, `request_id` and `receipt_id`
- **AND** each artifact exists exactly once

#### Scenario: Media fan-out does not run inside the critical section

- **WHEN** a batch preserves an image whose sidecar triggers index and graph work
- **THEN** no index upsert or graph refresh runs while the preservation mutation guard is held or before the batch terminal is persisted
- **AND** the batch response returns with `derived_sync` reporting the deferred work

#### Scenario: Terminal persistence fails after media sidecar commit

- **WHEN** the media sidecar commits without a fast-acknowledgement session and batch terminal persistence fails
- **THEN** no request-owned media fan-out runs before the terminal is durable
- **AND** a durable full-refresh record retains the indexing work for background repair
- **AND** a same-identity retry reports `derived_sync=pending` without restaging or duplicating the artifact

#### Scenario: New refresh demand arrives after another drain

- **WHEN** a background drain retires the media refresh record and another write queues a new refresh for the same path
- **THEN** completion of the earlier request's foreground media fan-out does not retire that newer refresh demand

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

### Requirement: Destination segments are validated before retrieval
The preservation commands SHALL treat `scope` and `category` as single path segments. A value containing a path separator, a reserved filesystem character, a control character, leading or trailing whitespace, or that differs from its sanitised form SHALL be refused with `INVALID_PRESERVE` whose `details` name the offending field, the reason, and the accepted form, before any file handle is fetched or staged. The commands SHALL NOT silently normalise a destination segment.

#### Scenario: Scope carries a path separator
- **WHEN** `preserve_artifacts` is called with `scope="work/studio-equipment-orders"` and a valid category
- **THEN** the call refuses with `INVALID_PRESERVE`, `details.field="scope"`, and a reason that says a segment cannot contain `/`
- **AND** no handle is fetched, no byte is staged, and no Evidence path is created

#### Scenario: Category carries a reserved character
- **WHEN** `preserve_evidence` is called with a category containing `:` or a control character
- **THEN** the call refuses with `INVALID_PRESERVE` naming `category`
- **AND** nothing is written

#### Scenario: Valid segments are unchanged
- **WHEN** `scope` and `category` are each one clean segment
- **THEN** the destination is `Evidence/<scope>/<category>/` byte-for-byte as supplied

### Requirement: The upload handoff advertises resumable sessions

On a server that serves upload sessions, `transfer_artifact(operation="upload")` for the Evidence lane SHALL return a `resumable` object beside `upload_url`. The object SHALL name the session creation URL, the protocol and its version, the header that a client resends on every later request, the most bytes that one request may carry, the most bytes that one session may hold, the `Upload-Metadata` keys that creation requires, and a link to a documented shell recipe. It SHALL NOT carry the recipe itself. The token that the handoff mints SHALL create a session through the same credential check and lane binding that `/upload` applies. Session creation SHALL refuse a request that lacks an advertised key with `INVALID_UPLOAD`, name every missing key, and open no session. A Sources-lane handoff SHALL NOT advertise sessions, because a session preserves Evidence.

#### Scenario: An agent with a shell uploads a file larger than the proxy edge cap
- **WHEN** an agent calls `transfer_artifact(operation="upload")`, creates a session at the advertised URL with the minted token, sends the file in parts and loses the connection once
- **THEN** it continues from the offset that the server reports
- **AND** the receipt names the SHA-256 of the whole file

#### Scenario: Creation lacks a required metadata key
- **WHEN** a client creates a session without one of the advertised `Upload-Metadata` keys
- **THEN** creation is refused with `INVALID_UPLOAD` that names every missing key, and no session opens

#### Scenario: A Sources-lane handoff
- **WHEN** an agent mints an upload capability for the Sources lane
- **THEN** the handoff carries `upload_url` and no `resumable` object

### Requirement: A standalone server pulls file handles up to the upload session maximum

On a standalone server, `preserve_artifacts` and `capture_source` SHALL accept one client file handle, and all the file handles of one call together, up to the upload session maximum that `EXOMEM_UPLOAD_SESSION_MAX_BYTES` sets. A held upload SHALL be held only up to the same cap, so that its redeeming command can fetch it. A pull SHALL stop with a stable timed-out failure and remove its temporary bytes when no byte arrives within its per-read timeout, or when, after a fixed allowance, its average rate falls below the documented minimum. Each byte that a pull receives SHALL extend the deadline of its file and of its call. Hosted and Cloud cells SHALL keep the per-file and per-call cap that the artifact broker's grants carry, and fixed deadlines. A call that carries an adoption key SHALL keep that cell cap on every server, with the server's own deadlines, because replaying it re-verifies the stored artifact in memory.

#### Scenario: A file handle larger than 100 MiB
- **WHEN** a standalone server pulls a client file handle larger than 100 MiB and within the session maximum
- **THEN** it streams the bytes to private temporary storage and preserves them

#### Scenario: A slow but healthy pull
- **WHEN** a pull takes longer than the fixed allowance and its average rate stays above the minimum
- **THEN** the pull completes

#### Scenario: A later file in a call
- **WHEN** one call pulls a long first file and then a second file
- **THEN** the second file starts with the time the first file earned for the call

#### Scenario: A trickling pull
- **WHEN** the bytes of a pull arrive slower than the minimum rate after the fixed allowance
- **THEN** the pull stops with a timed-out failure and its temporary bytes are removed

#### Scenario: A Cloud cell
- **WHEN** a Cloud cell pulls a file handle through the artifact broker
- **THEN** the broker grant's cap and the fixed deadlines apply as before

### Requirement: An archive is preserved as content-addressed members

When a client preserves a zip archive with `archive=members`, Exomem SHALL store each
distinct member once, as a compressed blob named by the SHA-256 of the member's
uncompressed bytes, in a member pool that belongs to one `Evidence/<scope>/<category>/`
family. It SHALL write a manifest that records the archive's SHA-256 and size and each
member's path, SHA-256, size and blob. The manifest and every blob SHALL carry the raw
protection marker, so they are owner-only from their first byte. Exomem SHALL validate the
archive's central directory before it reads any member, and SHALL refuse unsafe paths,
symlink entries, duplicate names, encrypted entries, and member counts or sizes over the
documented limits, writing no manifest. A blob SHALL be written only when the pool lacks
it, and the manifest SHALL be written last, as the commit point. Preserving an archive
whose SHA-256 a manifest in the same family already records SHALL return `already_stored`
and write nothing, except that Exomem SHALL restore each blob that manifest names and the
pool lacks. It SHALL take each such member from the uploaded archive, verify its SHA-256
against the manifest, and report how many blobs it restored.

#### Scenario: A second archive shares most members
- **WHEN** an owner preserves an archive whose members the family already holds, except two
- **THEN** Exomem writes two new blobs and one new manifest that lists every member

#### Scenario: The same archive is preserved twice
- **WHEN** an owner preserves an archive with the same SHA-256 into the same family again
- **THEN** the outcome is `already_stored` and no blob or manifest is written

#### Scenario: Preserving an archive again heals a lost member blob
- **WHEN** a blob that a recorded manifest names is missing and the owner preserves the same archive again
- **THEN** the outcome is `already_stored`, the blob is back with its recorded SHA-256, the outcome reports one restored blob, and nothing else is written

#### Scenario: A hostile archive
- **WHEN** an archive contains a member path that leaves the pool, or a symlink entry
- **THEN** Exomem refuses the archive and writes no manifest

### Requirement: Client artifact preservation supports exact selected-artifact adoption
`preserve_artifacts` SHALL accept an optional validated single-artifact adoption envelope while preserving existing non-adoption behavior. The envelope SHALL name one supplied `file_id`, an explicit trigger, and a scoped durable adoption key. Only the selected artifact SHALL commit, and its artifact and versioned companion receipt SHALL publish atomically through the existing append-only Evidence path. The stable response SHALL distinguish committed preservation, transport replay, durable replay, unverifiable replay, failure, and non-committing handoff.

#### Scenario: Existing calls remain compatible
- **WHEN** an existing client calls `preserve_artifacts` without an adoption envelope
- **THEN** its direct-handle staging, multi-file outcomes, append-only collision behavior, and receipt fields remain unchanged

#### Scenario: Adoption commits only the selected handle
- **WHEN** the envelope selects one of several supplied handles
- **THEN** only that handle is staged for canonical commit and no sibling artifact or companion is written

#### Scenario: Companion carries portable receipt identity
- **WHEN** selected bytes commit
- **THEN** their existing Evidence companion contains a versioned adoption block bound to the lane, destination, and committed byte identity and the response projects the same committed receipt

#### Scenario: Handoff token is not preservation
- **WHEN** a client receives only a transfer capability or prepared handoff
- **THEN** the response cannot mark the artifact committed, stored, saved, uploaded, adopted, or delivered

#### Scenario: Textual degradation is forbidden
- **WHEN** exact selected bytes cannot be obtained
- **THEN** the operation fails or requests handoff instead of silently writing a textual reconstruction or another variant

### Requirement: A held zip upload is preserved as members through preserve_artifacts

`preserve_artifacts` SHALL accept an optional `archive` field whose one value is `members`. With it, Exomem SHALL preserve each supplied held upload whose bytes are a zip archive exactly as `/upload` preserves an upload with `archive=members`, as content-addressed members and one export manifest under `Evidence/<scope>/<category>/`. The manifest and every blob SHALL be owner-only whatever `raw_protection` says. Each handle's outcome SHALL keep the states `stored`, `already_stored` and `failed`. A `stored` or `already_stored` outcome SHALL name the export manifest as its `path`, which a collection import takes as `source_ref`. It SHALL report the archive's SHA-256 and size; a `stored` outcome SHALL add its member count and the blobs it wrote, and an `already_stored` outcome the blobs it restored to the pool. A handle that is not a held upload SHALL fail with `INVALID_FILE`, naming the held-upload route, before any byte is fetched. Held bytes that are not a readable zip SHALL fail with `ARCHIVE_INVALID`, write no manifest and leave the hold redeemable. A call that combines `archive` with `adoption` or `transcriptions` SHALL be refused with `INVALID_PRESERVE` before any handle is staged. A held upload for the Sources lane SHALL fail with `HELD_UPLOAD_LANE`, as it does without `archive`.

#### Scenario: An MCP client preserves a held export as members

- **WHEN** an MCP client holds a zip export through `transfer_artifact(hold=true)` and calls `preserve_artifacts` with its handle and `archive: members`
- **THEN** the handle's outcome is `stored` and its `path` is the export manifest
- **AND** a collection import with that manifest as `source_ref` reads the export's members

#### Scenario: A refreshed export shares most members

- **WHEN** the client preserves a later export that adds one member to the members the family holds
- **THEN** the outcome is `stored`, one new blob is written, and the new manifest lists every member

#### Scenario: The same export is preserved again

- **WHEN** the client preserves an export whose SHA-256 a manifest in the family already records
- **THEN** the outcome is `already_stored`, it names that manifest, and no member is written again

#### Scenario: Held bytes that are not a zip

- **WHEN** the client preserves a held file that is not a zip with `archive: members`
- **THEN** the outcome is `failed` with `ARCHIVE_INVALID`, no manifest is written, and the hold stays redeemable

#### Scenario: A fetched attachment with archive

- **WHEN** the client supplies a chat attachment handle with `archive: members`
- **THEN** the outcome is `failed` with `INVALID_FILE`, which names the held-upload route, and nothing is fetched

### Requirement: The upload handoff has an import lane

`upload_files` SHALL accept the lane `import` on a personal server, which SHALL imply a held upload and require `expected_sha256`. An Exomem Cloud principal SHALL be refused with `UPLOAD_LANE_UNAVAILABLE`, which names `exomem cloud import`. The reply SHALL keep the handoff descriptor and add `next`: the multipart POST that sends the archive, as a ready shell line, and the call that redeems it, `manage_memory` `imports.file` with the descriptor's token as `upload`. The lane's bytes SHALL be redeemable only by `imports.file`, for the session that holds them. They SHALL NOT charge the media allowance, and the bearer upload routes and `exomem attach` SHALL NOT accept the lane. A held import upload SHALL keep the held-upload cap, count and one-hour life.

#### Scenario: An import upload is admitted over a full media allowance

- **WHEN** the media allowance is exhausted and an agent sends an archive with the lane `import`
- **THEN** the upload is held, and an upload with the lane `source` of the same size is refused with `MEDIA_ALLOWANCE_EXCEEDED`

#### Scenario: Exomem Cloud refuses the lane

- **WHEN** a Cloud principal calls `upload_files` with the lane `import`
- **THEN** the call is refused with `UPLOAD_LANE_UNAVAILABLE`, which names `exomem cloud import`, and no ticket is issued

#### Scenario: The reply names both steps

- **WHEN** an agent calls `upload_files` with the lane `import`
- **THEN** the reply carries a `next` that names the send and the `imports.file` call with the descriptor's token
