## ADDED Requirements

### Requirement: A captured document reports its own identity evidence

When `capture_source` stores an original that is a PDF, it SHALL read the document's first two pages without OCR, its document information and its XMP metadata before it renders the Source page.
It SHALL compare the DOIs in the metadata and on the first page with the DOIs in the claimed `url` and record the result on the Source as `identity_check`; a DOI on the second page SHALL feed only the opening text.
The state SHALL be one of `unavailable`, `no_claim`, `declared`, `printed`, `conflict` or `not_shown`, and `unavailable` SHALL name its reason.
The file row of the reply SHALL carry an `identity` block with the state, the first 600 characters of the pages read, the document's own title and the DOIs found.
On `conflict` or `not_shown`, the outcome sentence SHALL state the finding, and `next_action` SHALL name a metadata-only correction of that Source with its reference, taking the page's content hash from a page read, when the caller may write the correction receipt; otherwise it SHALL ask the agent to tell the person which value is wrong. The DOIs the document shows SHALL be the distinct DOIs of its metadata and its first page together. On `conflict` where the document shows exactly one DOI, the correction call SHALL name that DOI as the `url` and the document's own title, and SHALL ask for the correction then when the opening text shows the document is the work that DOI names; otherwise it SHALL ask the agent to tell the person which value is wrong. Where the document shows more than one DOI, `next_action` SHALL ask for a comparison with the opening text and SHALL NOT name a DOI as the `url`. A correction the caller may not record SHALL be refused before any work with `SOURCE_CORRECTION_NOT_PERMITTED`. The reply SHALL NOT carry the page's hash, because processing rewrites the page after capture.
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
