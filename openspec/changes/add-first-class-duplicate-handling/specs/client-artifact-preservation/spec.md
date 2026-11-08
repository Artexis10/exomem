## MODIFIED Requirements

### Requirement: Governed append-only persistence and truthful outcomes
Each successfully staged file SHALL be committed through the existing `preserve_stream` Evidence path under the active vault mutation boundary, preserving append-only collision refusal, sanitization, sidecars, hashing, index updates, and media reconciliation behavior. The command SHALL return exactly one ordered outcome per input `file_id`, and the batch SHALL carry the mutation's `request_id` and, when available, `receipt_id`. Each outcome SHALL carry a terminal `state` in `stored`, `already_stored`, or `failed`. A stored outcome SHALL include stored path, stable ref, size, SHA-256, hash algorithm, media ID, and content type; an `already_stored` outcome SHALL name the existing path and ref of the artifact, anywhere in the vault in the same lane and protection class, whose SHA-256 equals the staged bytes, and SHALL write no new original, sidecar or Evidence page, except for the family addition that `Identical bytes are stored once across the vault` defines; a failed outcome SHALL include a stable code and sanitized reason. The batch summary SHALL count all three states. One item failure SHALL NOT erase or misreport another item's successful commit. The index and graph fan-out that media reconciliation triggers SHALL NOT run while a preservation mutation guard is held, and SHALL NOT run before the batch terminal is persisted; it SHALL take the deferred derived path that note writes use, or run after terminal persistence when no derived path is available, and the batch's `derived_sync` SHALL report it.

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

#### Scenario: Duplicate bytes were filed under another destination

- **WHEN** a client preserves a file whose SHA-256 already exists under a different `Evidence/<scope>/<category>/` destination
- **THEN** that file's outcome is `already_stored` with the existing path and ref, and no new artifact or sidecar is written
- **AND** the existing page's family list gains the requested destination

## ADDED Requirements

### Requirement: Identical bytes are stored once across the vault and one Evidence page lists every family

`preserve_evidence`, `preserve_artifacts` and the file-handle path of `capture_source` SHALL store identical original bytes once in the vault, per lane and protection class, whatever destination each call names. Before it commits a staged file, the call SHALL look up the file's SHA-256 through the duplicate-handling index, inside the mutation boundary, and SHALL re-hash the stored original. When a live original that the caller can read in full holds identical bytes, the call SHALL write no new original, no sidecar and no Evidence page. It SHALL return the existing path and ref with `state: "already_stored"`.

Each Evidence page SHALL carry `families`, a list of `{scope, category}` pairs naming every evidence family the original belongs to. The first pair is the folder the page lives in, and the page never moves. A page written before this capability carries no list, and its folder family stands for it until the first addition writes both. When the requested `scope` and `category` are not yet listed, the call SHALL add that one pair through a frontmatter-only write that the page's content hash guards, inside the mutation boundary. That write SHALL keep the original bytes, the companion binding and the raw-protection marker unchanged, and the outcome SHALL carry `family_added: true`. Adding a pair that is already listed SHALL write nothing. An `already_stored` row SHALL carry `families`, the full list after the call. When the caller cannot write the existing page, the call SHALL store its own copy in the requested family instead of adding the pair.

A listing or filter by scope or category SHALL read each page's `families` as well as its folder, so that a page appears under every family that lists it. Archive member pools stay per family and keep their own requirement.

#### Scenario: One photo filed in two families

- **WHEN** a client preserves a photo into family A and later preserves the same bytes into family B
- **THEN** one original and one Evidence page exist
- **AND** the page lists both families and the second outcome is `already_stored` with `family_added: true`
- **AND** browsing family A and browsing family B each find the page

#### Scenario: The same family again

- **WHEN** a client preserves bytes into a family that the page already lists
- **THEN** the outcome is `already_stored` with `family_added: false` and no file changes
- **AND** the audit chain does not advance

#### Scenario: A page without a family list gains its second family

- **WHEN** bytes stored before this capability are preserved into a second family
- **THEN** the page's list names its folder family first and the new family second

#### Scenario: A restricted caller cannot write the page

- **WHEN** the caller can read the existing page but cannot write it
- **THEN** the call stores its own copy under the requested family

### Requirement: A hand-over with an adoption envelope finds an existing copy through the index

A `capture_source` or `preserve_artifacts` call that carries an adoption envelope SHALL find an existing copy of the selected bytes through the duplicate-handling index. It SHALL NOT scan a Sources kind folder per call. The copy MAY lie in any Sources kind folder or Evidence family of the same lane. The outcome SHALL remain `already_stored` with the existing path and ref.

#### Scenario: The copy sits in another Sources kind folder

- **WHEN** an adoption hand-over repeats bytes that a Source stores under a different kind folder
- **THEN** the outcome is `already_stored` naming that Source
- **AND** no second Source or artifact is written
