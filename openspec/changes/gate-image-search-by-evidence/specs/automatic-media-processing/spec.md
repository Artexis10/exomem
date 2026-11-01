## ADDED Requirements

### Requirement: Image OCR Keeps Only Confident Words Searchable

Image OCR, including OCR of video scene frames, SHALL keep in `## Extracted text` only the words whose Tesseract confidence is at or above a configured floor. The default floor SHALL be 70 on Tesseract's 0-100 scale. Words under the floor SHALL be kept in a `## Low-confidence OCR` section of the same sidecar, behind a marker comment that proves the section is the sidecar's own and not OCR text. Every search field SHALL omit that section, and so SHALL any page the sidecar's text is carried into, including the preserved notes of a re-rendered sidecar. Kept words SHALL keep the spacing of Tesseract's own text output. The sidecar SHALL record the mean word confidence of the chosen pass as `ocr_confidence`. A sidecar without `ocr_confidence` SHALL stay searchable as before. A floor of 0 SHALL keep every word searchable. PDF page OCR SHALL be unchanged.

#### Scenario: Misread words leave the search fields

- **WHEN** an image is OCR'd and some words score under the floor
- **THEN** `## Extracted text` holds only the words at or above the floor, in reading order
- **AND** the other words are in `## Low-confidence OCR`
- **AND** a search for a word that is only in `## Low-confidence OCR` does not match the sidecar

#### Scenario: A photo with no legible text is not indexed by noise

- **WHEN** every word Tesseract reads from a photo scores under the floor
- **THEN** `## Extracted text` is empty, with no placeholder text, and the sidecar records its `ocr_confidence`
- **AND** the sidecar counts as extracted and is not queued again
- **AND** the sidecar stays findable by its title, path and CLIP image vector

#### Scenario: A re-rendered sidecar does not return the withheld words to search

- **WHEN** a completed image sidecar with a `## Low-confidence OCR` section is rendered again and its old body is kept as preserved notes
- **THEN** the withheld words are not in the preserved notes
- **AND** a search for a withheld word does not match the sidecar

#### Scenario: OCR text that holds the section heading stays searchable

- **WHEN** the recognised text of an image contains a line equal to `## Low-confidence OCR`
- **THEN** the text around that line stays in `## Extracted text` and in search

#### Scenario: Existing sidecars are unchanged

- **WHEN** a sidecar extracted before this change has no `ocr_confidence`
- **THEN** its extracted text stays searchable
