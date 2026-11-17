## ADDED Requirements

### Requirement: The contract teaches research-source custody and identity

The served capture guidance and the shipped skill scaffold SHALL tell agents:

- to keep user-supplied files, and the external documents that an answer relies on, as Sources without waiting to be asked;
- to take a document's title, authors, year, venue and DOI from the document itself, never from a search result or memory;
- to state the document form, and to save an abstract or citation record when the full text is unavailable;
- to decide whether the Source warrants compilation into an existing or new note, and otherwise to report it as kept for custody only;
- to correct a wrong Source with a metadata-only correction.

Each line SHALL ship only when a real-model comparison shows that the arm with it outperforms the arm without it on the incident cases.

#### Scenario: An agent receives a misleading repository match

- **WHEN** a search result names a PDF as an article and the PDF's opening shows a different work
- **THEN** the agent saves it under the document's own identity, or does not save it as that article
- **AND** the user does not have to point out the mismatch
