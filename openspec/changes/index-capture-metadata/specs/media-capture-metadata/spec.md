# Spec Delta

## Purpose

Record when and where a photo or video was captured, make those facts searchable at indexed cost, and disclose a location only to a caller who may read the original.

## ADDED Requirements

### Requirement: Extraction records capture metadata as the file states it

Image and video extraction SHALL record each of these values in the sidecar frontmatter when the file states it:

- capture time, with the file's UTC offset;
- GPS latitude and longitude;
- camera make and model;
- pixel dimensions.

A capture time without an offset SHALL be recorded as local time with an unknown offset. A value that the file does not state SHALL be absent, never zero, empty or a default date.

#### Scenario: A phone photo with an offset

- **WHEN** a JPEG records an original capture time of 2012-07-14 18:05:00 with offset +03:00 and a GPS position
- **THEN** the sidecar records that time with offset +03:00 and that position

#### Scenario: A scanned print with no metadata

- **WHEN** a scanned image records no capture time and no position
- **THEN** the sidecar has no capture time and no position
- **AND** a time window filter does not match it

### Requirement: Search filters by capture time window and by place

A search SHALL accept a capture time window and a geographic bounding box as structured filters, using the existing ordered operators. A time without an offset SHALL match a window that is widened by the largest real UTC offset range. A filtered search SHALL use an index, and its cost SHALL not grow with the number of media files outside the filter.

#### Scenario: A summer window

- **WHEN** a search filters capture time between 2012-06-01 and 2012-08-31
- **THEN** results include photos taken in that window with a known offset
- **AND** results include photos with an unknown offset whose local time falls in the widened window

### Requirement: A location is disclosed only to a caller who may read the original

A search result or page read SHALL include a GPS position only when the caller passes the original's release check. A bounding-box filter from any other caller SHALL be refused, with no result count.

#### Scenario: A reader without original access

- **WHEN** a caller who may read a photo's page but not download its original reads that page
- **THEN** the page shows the capture time and camera but no position

#### Scenario: A probing bounding box

- **WHEN** that caller searches with a bounding-box filter
- **THEN** the search is refused and reports no count of matching photos

### Requirement: The owner adds capture metadata to existing sidecars without changing their text

One owner operation SHALL add capture metadata to existing image and video sidecars through the media job ledger. It SHALL rewrite only the capture block. It SHALL keep the extracted text, the preserved notes and the prior-extraction pages byte for byte. A sidecar that the boundary guard cannot classify SHALL be skipped and counted. The operation SHALL run only when the owner starts it.

#### Scenario: A sidecar with owner notes

- **WHEN** the operation reaches a sidecar that carries owner notes after its extracted text
- **THEN** the notes and the extracted text are byte-identical after the write
- **AND** the capture block is added

#### Scenario: An ambiguous legacy sidecar

- **WHEN** the boundary guard cannot tell extracted text from notes in a sidecar
- **THEN** the sidecar is unchanged and counted as skipped
