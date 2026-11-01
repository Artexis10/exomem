## ADDED Requirements

### Requirement: Plain-Text Artifacts Are Read In The Encoding Their Bytes Show

The system SHALL decode the bytes of a plain-text, RTF or calendar artifact with one rule. A byte-order mark (UTF-8, UTF-16 or UTF-32) SHALL name the codec. Without one, valid UTF-8 SHALL be read as UTF-8. Otherwise a detected encoding SHALL be used only when the detector's confidence is at least 0.9, and Windows-1252 SHALL win among equally confident single-byte guesses. Bytes that no rule explains SHALL be read as UTF-8 with replacement characters. The original bytes of the artifact SHALL NOT change. The sidecar's `extracted_by` SHALL name a non-UTF-8 encoding as `text:<codec>`.

#### Scenario: A UTF-16 file with a byte-order mark is searchable

- **WHEN** a `.txt` artifact holds UTF-16 text that starts with a byte-order mark
- **THEN** the extracted text holds the file's own characters, not characters separated by NUL
- **AND** `extracted_by` is `text:utf-16`

#### Scenario: A Latin-1 file keeps its accented letters

- **WHEN** a `.txt` artifact holds Latin-1 text that is not valid UTF-8
- **THEN** the extracted text holds the accented letters as written
- **AND** the artifact's bytes are unchanged

#### Scenario: Text that no codec explains reads as before

- **WHEN** a `.txt` artifact holds bytes that are not valid UTF-8 and no detected encoding reaches the confidence bound
- **THEN** the extracted text is the UTF-8 reading with replacement characters
- **AND** `extracted_by` is `text`

#### Scenario: UTF-8 with a stray byte keeps its valid characters

- **WHEN** a `.txt` artifact holds mostly valid UTF-8 text with one invalid byte
- **THEN** every valid character is kept and only the invalid byte becomes a replacement character
