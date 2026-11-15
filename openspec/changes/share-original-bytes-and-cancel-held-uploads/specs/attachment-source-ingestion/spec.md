# Spec Delta

## MODIFIED Requirements

### Requirement: Lossless attachment capture into Sources

The system SHALL allow an attached file to be captured as raw source material
without its bytes passing through the model. `capture_source` SHALL accept the
same ordered client file handles as `preserve_artifacts`, requiring only
`download_url` and `file_id` per file and permitting `mime_type` and `file_name`
to be omitted. Handles SHALL be staged through the same bounded, safe server-side
retrieval used by `preserve_artifacts`, with identical treatment of hostile URLs,
redirect limits, timeouts, item counts, and byte caps. Each staged file SHALL create its intended filing at a logical `Sources/`
address with the source taxonomy applied. Legacy layouts SHALL keep ordinary
original files there. Pooled layouts SHALL resolve the address through their
canonical original store. The bytes SHALL remain exact, and physical sharing
SHALL NOT create another content-lane filing. The command SHALL return
exactly one ordered outcome per input `file_id`.

#### Scenario: An attached transcript is captured as a Source

- **WHEN** a client invokes `capture_source` with a text file handle, a title, and a source kind
- **THEN** a Source filing resolves the exact file bytes at its logical address
- **AND** no binary or base64 payload appears in a model-visible argument
- **AND** the stored source carries the supplied kind, domain, and project associations

#### Scenario: An attached image is captured as a Source

- **WHEN** a client invokes `capture_source` with an image file handle
- **THEN** the image has a logical Source address with its page marked as media awaiting extraction
- **AND** the existing extraction path converges on that page without lane-specific handling

#### Scenario: A client without file handles reaches the same lane

- **WHEN** a client that cannot expose file handles requests an upload capability for source material
- **THEN** the minted capability names the Sources destination
- **AND** bytes posted against that capability are stored as a Source rather than as Evidence

#### Scenario: A retried identical capture does not store twice

- **WHEN** a client retries the byte-identical attachment capture within the implicit replay window
- **THEN** the cached terminal result is returned without retrieving or writing the files again

### Requirement: Every ingested artifact is addressable

The system SHALL create exactly one markdown page for every artifact filing it creates,
in both the Sources and Evidence lanes, regardless of media type and regardless
of whether a description or extracted text was supplied. That page SHALL carry
`type: source`, a stable `exomem_id`, an `ingested_into` list, a vault-relative
logical pointer that resolves the stored bytes, the artifact's original filename, its SHA-256, and
its byte count. The page body SHALL describe the artifact rather than be empty.
An artifact filing SHALL NOT be stored without such a page. Separate filings
SHALL retain separate stable identifiers even when they share physical bytes.

#### Scenario: A text artifact receives a page

- **WHEN** a text artifact is stored with no description and no extracted text
- **THEN** a page is written at its logical companion address with a stable identifier, an empty `ingested_into`, and the artifact pointer
- **AND** the artifact is discoverable through ordinary corpus retrieval by that page

#### Scenario: A media artifact keeps its extraction contract

- **WHEN** a media artifact is stored without supplied text
- **THEN** its page declares the media type, marks extraction as pending, and carries the anchor the extraction worker fills
- **AND** the completed extraction is written into that same page

#### Scenario: No empty page is written

- **WHEN** a page would carry no description, no extracted text, and no artifact metadata
- **THEN** no such page is written
