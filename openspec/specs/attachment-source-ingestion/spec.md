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

### Requirement: A Captured Markdown Original Has Searchable Text

A captured original with the suffix `.md` SHALL be stored with its exact bytes under the suffix `.md.txt`, outside the graph input set. Its Source page SHALL declare the `text` media type and mark extraction as pending, so that its text is extracted in the encoding its bytes show.

#### Scenario: A captured UTF-16 Markdown file

- **WHEN** a client captures a `.md` original that holds UTF-16 text with a byte-order mark
- **THEN** the stored artifact's bytes equal the original and its name ends with `.md.txt`
- **AND** its Source page declares the `text` media type with extraction pending
- **AND** the graph does not read the artifact as a page

### Requirement: Hidden parts are text of the one Source page

An artifact SHALL keep exactly one addressable Source page when its extracted text continues on part pages. Part pages SHALL be text of that page, not pages of the artifact. Browse, overview, the working set and the vault listing SHALL NOT show part pages. Audit counts and the viewer map SHALL NOT count a part page as a memory. A move of a Source page, with or without its original, SHALL move its part pages in the same transaction. A part page SHALL be readable exactly where its Source page is readable. Its scope membership, and with it the audiences that may read it, SHALL be its Source page's, derived from the part's path. A governed projection catalog SHALL NOT hold a part page as an item of its own. It SHALL hold a part's text only as content of its Source's row, so the text of a part never surfaces there as a separate document.

#### Scenario: The folder lists the original and its page
- **WHEN** a long document is stored and its folder is listed
- **THEN** the listing shows the original and its Source page only

#### Scenario: A move carries the parts
- **WHEN** a Source page with part pages is moved to another folder, with or without its original
- **THEN** a search finds the Source at its new path with the same matched part
- **AND** no part page remains at the old path

#### Scenario: A part belongs to the scopes of its Source
- **WHEN** a governance scope selects a Source page by its stable identifier
- **THEN** each part page of that Source belongs to that scope

#### Scenario: Parts are not unprocessed sources
- **WHEN** audit checks for unprocessed sources
- **THEN** it reports the Source page and none of its part pages

### Requirement: Source attachment capture supports exact selected-artifact adoption
`capture_source` SHALL accept the same optional validated single-artifact adoption envelope as `preserve_artifacts` while preserving ordinary Source attachment behavior. The envelope SHALL name one supplied `file_id`, an explicit trigger, and a scoped durable adoption key. Only that handle SHALL commit. The exact bytes and versioned adoption block SHALL publish atomically in the Source lane, with the Source artifact page as the canonical receipt owner. The block and stable response SHALL bind the trigger, key digest, selected identifier, Source lane, destination, path, SHA-256, size, content type, and media identity.

Transport-window and durable adoption replay SHALL use the same semantics and stable failures as Evidence adoption: cached transport replay performs no second fetch; replay after expiry re-stages and hashes bytes; mismatch fails `ADOPTION_KEY_REUSED`; unavailable bytes fail `ADOPTION_REPLAY_UNVERIFIABLE`; no failure writes or claims replay.

#### Scenario: Existing Source handle capture remains compatible
- **WHEN** an existing client calls `capture_source` with file handles and no adoption envelope
- **THEN** its ordered outcomes, exact-byte Source storage, source taxonomy, source page, and replay behavior remain unchanged

#### Scenario: Selected reasoning artifact commits to Source only
- **WHEN** an adoption envelope selects one reasoning-input artifact from several handles supplied to `capture_source`
- **THEN** only that handle is stored under Sources and its Source artifact page carries the committed adoption block
- **AND** no sibling or Evidence artifact is written

#### Scenario: Source receipt matches Evidence vocabulary
- **WHEN** the same synthetic bytes are separately adopted once as Source and once as Evidence under distinct keys
- **THEN** both receipts expose the same trigger, key-digest, selected-id, hash, size, content-type, and media-identity fields
- **AND** their lane, destination, path, and canonical companion/page correctly differ

#### Scenario: Durable Source replay reproves bytes
- **WHEN** a Source adoption key is retried after transport replay expiry
- **THEN** `capture_source` re-stages and hashes the selected handle before returning the original receipt
- **AND** changed or unavailable bytes fail closed without another Source write

### Requirement: Receiving a file makes it eligible for custody

A file the user hands the agent to read SHALL be eligible for capture as a Source by its exact bytes, whether or not it holds a durable conclusion. Eligibility SHALL NOT confirm the write: an agent-initiated capture SHALL obey the `proactive_capture` disposition. Files the agent writes or edits in the user's own workspace or repository, and files the user only mentions, SHALL NOT be eligible. A file that is already stored SHALL return `already_stored` and write nothing.

#### Scenario: A repository file is not a handed file
- **WHEN** the user asks the agent to fix a tracked file in the user's repository
- **THEN** the agent writes no Source for that file

#### Scenario: A file the user says to ignore is not kept
- **WHEN** the user names a file in the folder and says to ignore it
- **THEN** the agent writes no Source for that file

### Requirement: A text copy is reported as a text copy

When `save_source` stores supplied text rather than an uploaded original, its outcome line SHALL say that Exomem kept the text, not the original file.

#### Scenario: An agent pastes a transcript's text
- **WHEN** an agent saves a transcript's text through `content` instead of uploading the file
- **THEN** the reply's outcome line says the text was kept and the original file was not

### Requirement: A kept-for-custody Source waits in its own quiet count

`save_source` SHALL accept `options.custody_only: true`, which records `custody_only: true` on the new Source and adds "kept for custody only" to the reply's outcome line. The `unprocessed_source` audit family SHALL skip a Source that records `custody_only: true`. The `custody_source` audit family SHALL list each such Source that no live page compiles, at `info` severity whatever its age, oldest first. The family SHALL NOT be in the default attention set. A Source that a live page compiles SHALL appear in neither family.

#### Scenario: A screenshot kept only for safekeeping
- **WHEN** an agent saves a handed screenshot with `options.custody_only: true`
- **THEN** the reply's outcome line says the file was kept for custody only
- **AND** the Source appears in `custody_source` at `info` severity and not in `unprocessed_source`

#### Scenario: An old custody Source never becomes a warning
- **WHEN** a Source marked `custody_only: true` has waited more than 90 days with no note citing it
- **THEN** its `custody_source` finding stays at `info` severity

#### Scenario: Compiling a custody Source clears it
- **WHEN** a live note cites a Source marked `custody_only: true`
- **THEN** the Source appears in neither `custody_source` nor `unprocessed_source`

### Requirement: A Source upload step names the held send

When `upload_files` with lane `source` issues an unheld upload step, its reply SHALL carry a `next_action` that tells an agent holding the file to call again with `expected_sha256`, so the send is held and `save_source` takes the document's title, kind and url. It SHALL say that a POST to the unheld step stores the file at once under its file name, with no url and no duplicate check.

#### Scenario: An agent asks for an upload without a digest
- **WHEN** an agent calls `upload_files` with lane `source` and no `expected_sha256` on a server with an upload token
- **THEN** the reply carries the upload step and a `next_action` that names `expected_sha256` and `save_source`

### Requirement: A captured document reports its own identity evidence

When `capture_source` stores an original that is a PDF, it SHALL read the document's first two pages without OCR, its document information and its XMP metadata before it renders the Source page.
It SHALL compare the DOIs in the metadata and on the first page with the DOIs in the claimed `url` and record the result on the Source as `identity_check`; a DOI on the second page SHALL feed only the opening text.
The state SHALL be one of `unavailable`, `no_claim`, `declared`, `printed`, `conflict` or `not_shown`, and `unavailable` SHALL name its reason.
The file row of the reply SHALL carry an `identity` block with the state, the first 600 characters of the pages read, the document's own title and the DOIs found.
On `conflict` or `not_shown`, the outcome sentence SHALL state the finding, and `next_action` SHALL name a metadata-only correction of that Source with its reference, taking the page's content hash from a page read, when the caller may write the correction receipt; otherwise it SHALL ask the agent to tell the person which value is wrong. The DOIs the document shows SHALL be the distinct DOIs of its metadata and its first page together. On `conflict` where the document shows exactly one DOI, the correction call SHALL name that DOI as the `url` and the document's own title, and SHALL ask for the correction then when the opening text shows the document is the work that DOI names; otherwise it SHALL ask the agent to tell the person which value is wrong. Where the document shows more than one DOI, `next_action` SHALL ask for a comparison with the opening text and SHALL NOT name a DOI as the `url`. A comparison SHALL say that a correction keeps every field it omits and that an empty `url` removes the url. A correction the caller may not record SHALL be refused before any work with `SOURCE_CORRECTION_NOT_PERMITTED`. The reply SHALL NOT carry the page's hash, because processing rewrites the page after capture.
No state SHALL claim that the document is the work its metadata names.
A failure to read the document, or a read that passes its memory cap or its deadline, SHALL yield `unavailable` with its reason; it SHALL NOT fail the custody of the original or delay it beyond that deadline.
Media processing SHALL compute the check again after OCR only for a Source whose recorded state is `unavailable` because the PDF has no text layer.

#### Scenario: The claimed DOI differs from the document's own

- **WHEN** an agent saves a PDF whose first page prints `10.5555/aaa` with a `url` naming `10.5555/bbb`
- **THEN** the Source records `identity_check.state: conflict` with both DOIs
- **AND** the reply's outcome sentence names both DOIs and `next_action` names the correction call with `https://doi.org/10.5555/aaa` as its `url`

#### Scenario: The metadata and the first page show different DOIs

- **WHEN** an agent saves a PDF whose XMP declares `10.5555/ccc` and whose first page prints `10.5555/aaa`, with a `url` naming `10.5555/aaa`
- **THEN** the state is `conflict`, because the metadata declares only another DOI
- **AND** `next_action` asks for a comparison with the opening text and names neither DOI as the corrected `url`

#### Scenario: A citing work prints the cited DOI

- **WHEN** an agent saves a thesis summary whose first page prints the DOI of the article it discusses, with that DOI as its `url`
- **THEN** the state is `printed`, never a verified identity
- **AND** the reply returns the opening text, so the agent can compare the title and authors

#### Scenario: The document shows no DOI

- **WHEN** an agent saves a PDF that shows no DOI in its metadata or on its first page, with a `url` naming a DOI
- **THEN** the state is `not_shown` and `next_action` asks for a comparison with the opening text

#### Scenario: The document cannot be read

- **WHEN** the PDF is encrypted, malformed or has no text layer
- **THEN** the original is stored and the state is `unavailable` with its reason
- **AND** a PDF without a text layer is checked again after OCR
