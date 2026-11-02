# Whole extraction delta

## ADDED Requirements

### Requirement: Extracted text is never cut

The system SHALL keep the whole extracted text of a Source, up to the largest result the job store carries. The first part of the text SHALL stay on the Source page, up to one page of text. A cut SHALL fall in the second half of a page, at its last paragraph break, else its last line break, else its last character, so no part but the first and the last is shorter than half a page. Each further part SHALL be a page in a hidden folder beside the stored original, at most one page of text each. The folder SHALL be a dot folder, except beside a page whose name carries the raw-protection mark: there it SHALL keep that mark at the start of its name, without a dot, so the protection floor still applies to it. The parts joined in order SHALL equal the extracted text. The Source page SHALL record how many parts there are and the path of its next part. No page SHALL store where a part starts; a reader SHALL measure positions from the parts as they are. Part pages SHALL name no binary, so no processing pass picks them up. The Source page and its part pages SHALL commit in one atomic batch. An extraction larger than the largest result the job store carries, 128 MiB, SHALL keep its first 128 MiB, end with a visible line that says the text was truncated, and log a warning. The original SHALL stay whole.

#### Scenario: A document longer than one page keeps every character
- **WHEN** a text original of several megabytes finishes extraction
- **THEN** the Source page holds the first part and hidden part pages hold the rest
- **AND** reading the parts in order returns the extracted text exactly

#### Scenario: A part page is never processed
- **WHEN** the extraction worker scans the vault after a long extraction
- **THEN** no part page is queued for processing

#### Scenario: An oversized extraction keeps its start
- **WHEN** an extraction is larger than the largest result the job store carries
- **THEN** the Source keeps the start of the text, ending with a visible truncation line
- **AND** the job completes instead of failing

### Requirement: A re-extraction owns exactly its parts

A re-extraction SHALL leave no part page of an earlier extraction beside the Source page. When the new text needs fewer parts, the surplus part pages SHALL be removed. A reprocess that keeps the old extraction on a prior-extraction page SHALL keep its parts with that page, and the old and new text SHALL never mix. A reprocess SHALL publish the new extraction even when a part of the old one is missing, and the prior-extraction page SHALL show a visible line in place of each missing part. Reconcile SHALL check every Source page that lists parts and every page at the path of a part. It SHALL write missing parts again from the original when the original reads without a model, also when the whole folder is gone. It SHALL report a missing part of a recording, an image or a scanned PDF page without running its engine. Reconcile SHALL remove a part page only when its Source page holds an extraction that lists fewer parts and an extraction wrote that page. It SHALL report every other page at the path of a part and leave it.

#### Scenario: A shorter extraction removes the old tail
- **WHEN** an extraction of several parts is replaced by a short one
- **THEN** the hidden folder is gone and a search for text from the old tail finds nothing

#### Scenario: A reprocess keeps the whole old text
- **WHEN** a recording with a multi-part transcript is processed again
- **THEN** the prior-extraction page and its parts hold the whole old text
- **AND** no old part remains beside the Source page

#### Scenario: A reprocess publishes when an old part is missing
- **WHEN** a part of a recording's transcript is missing and the recording is processed again
- **THEN** the new transcript is published
- **AND** the prior-extraction page marks the missing part with a visible line

#### Scenario: A lost part of a recording is reported
- **WHEN** a part of a recording's transcript is missing
- **THEN** reconcile reports the Source page and does not run the speech engine

#### Scenario: A parts folder that is gone is written again
- **WHEN** the hidden folder of a text document's parts is gone and reconcile runs
- **THEN** the part pages are written again from the original

#### Scenario: A hand-made file is kept
- **WHEN** a file at the path of a part was not written by an extraction
- **THEN** reconcile reports it and leaves it in place
