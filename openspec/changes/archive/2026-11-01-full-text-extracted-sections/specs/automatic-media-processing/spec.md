# Whole extraction delta

## ADDED Requirements

### Requirement: Extracted text is never cut

The system SHALL keep the whole extracted text of a Source. The first part of the text SHALL stay on the Source page, up to one page of text, cut at a paragraph break, else a line break, else a character boundary. Each further part SHALL be a page in a hidden folder beside the stored original, at most one page of text each, cut by the same rule. The parts joined in order SHALL equal the extracted text. The Source page SHALL record how many parts there are, where each starts and the length of the whole text. Part pages SHALL name no binary, so no processing pass picks them up. The Source page and its part pages SHALL commit in one atomic batch. An extraction that exceeds the largest result the job store carries SHALL fail visibly and keep the original.

#### Scenario: A document longer than one page keeps every character
- **WHEN** a text original of several megabytes finishes extraction
- **THEN** the Source page holds the first part and hidden part pages hold the rest
- **AND** reading the parts in order returns the extracted text exactly

#### Scenario: A part page is never processed
- **WHEN** the extraction worker scans the vault after a long extraction
- **THEN** no part page is queued for processing

### Requirement: A re-extraction owns exactly its parts

A re-extraction SHALL leave no part page of an earlier extraction beside the Source page. When the new text needs fewer parts, the surplus part pages SHALL be removed. A reprocess that keeps the old extraction on a prior-extraction page SHALL keep its parts with that page, and the old and new text SHALL never mix. Reconcile SHALL remove part pages whose Source page is gone or lists fewer parts. Reconcile SHALL write a missing part again from the original when extraction is cheap, and SHALL report a missing part of a recording or an image without running its engine.

#### Scenario: A shorter extraction removes the old tail
- **WHEN** an extraction of several parts is replaced by a short one
- **THEN** the hidden folder is gone and a search for text from the old tail finds nothing

#### Scenario: A reprocess keeps the whole old text
- **WHEN** a recording with a multi-part transcript is processed again
- **THEN** the prior-extraction page and its parts hold the whole old text
- **AND** no old part remains beside the Source page

#### Scenario: A lost part of a recording is reported
- **WHEN** a part of a recording's transcript is missing
- **THEN** reconcile reports the Source page and does not run the speech engine
