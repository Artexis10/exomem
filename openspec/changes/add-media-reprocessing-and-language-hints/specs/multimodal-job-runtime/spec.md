## ADDED Requirements

### Requirement: PDF Extracted Text Names Each Page

PDF extraction SHALL write a `[page N]` line, with `N` counted from 1, before the text of each page that has text, including a page read by OCR. A page marker SHALL NOT match the timed-transcript line pattern.

#### Scenario: Pages are marked

- **WHEN** a three-page PDF is extracted and its second page is read by OCR
- **THEN** the extracted text holds `[page 1]`, `[page 2]` and `[page 3]` lines, each before its page's text

#### Scenario: A page marker is not a timed line

- **WHEN** a PDF's extracted text is read as a transcript
- **THEN** no `[page N]` line parses as a timed line
