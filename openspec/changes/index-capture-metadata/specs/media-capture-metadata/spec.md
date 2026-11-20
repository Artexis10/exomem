# Spec Delta

## Purpose

Record when and where a photo or video was captured, make capture date and place searchable at indexed cost, and withhold a location from every caller who may not read the original.

## ADDED Requirements

### Requirement: Extraction records capture metadata as the file states it

Image and video extraction SHALL record each of these values in the sidecar frontmatter when the file states it:

- capture time as the camera recorded it, with its UTC offset when the file records one;
- capture date, the ISO calendar date of that recorded local time;
- GPS latitude and longitude;
- camera make and model;
- pixel dimensions.

For images, the capture time SHALL come only from the EXIF original capture time and its offset field. It SHALL NOT come from the file's modification time, which scanners and editors set. A GPS block that the file marks void SHALL count as no position. A value that the file does not state SHALL be absent, never zero, empty or a default date.

#### Scenario: A phone photo with an offset

- **WHEN** a JPEG records an original capture time of 2012-07-14 18:05:00 with offset +03:00 and a valid GPS position
- **THEN** the sidecar records that time with offset +03:00, the capture date 2012-07-14, and that position

#### Scenario: A scanned print

- **WHEN** a scanned image records a modification time but no original capture time and no position
- **THEN** the sidecar has no capture time, no capture date and no position
- **AND** a capture date filter does not match it

### Requirement: Search filters by capture date and by place

A search SHALL accept the capture date as a structured filter with the existing ordered operators on ISO dates. It SHALL accept a geographic bounding box as a pair of ordered latitude and longitude filters. A box that crosses the antimeridian SHALL be expressed as two longitude ranges joined by `$or`. A filtered search SHALL use an index, and its cost SHALL NOT grow with the number of media files outside the filter.

#### Scenario: A summer window

- **WHEN** a search filters the capture date between 2012-06-01 and 2012-08-31
- **THEN** results include the photos whose recorded local date falls in that window, with or without an offset

### Requirement: A location is withheld from a caller who may not read the original

A caller who fails an original's release check SHALL see that original's position as absent on every surface, including:

- page reads, excerpts and exports;
- lexical and vector text;
- every filter, including generic frontmatter-pointer filters and `$exists`.

The release check SHALL be evaluated per original. A location filter SHALL match only originals that the caller passes the release check for.

#### Scenario: A reader without original access

- **WHEN** a caller who may read a photo's page but not download its original reads that page
- **THEN** the page shows the capture date and camera but no position

#### Scenario: A probing filter

- **WHEN** that caller searches with a latitude range, or a frontmatter-pointer filter on the position, that the photo's position would satisfy
- **THEN** the photo does not match, exactly as if it had no position

### Requirement: An owner operation adds capture metadata to existing sidecars without changing their text

One owner operation SHALL add capture metadata to existing image and video sidecars through the media job ledger. It SHALL rewrite only the capture block. It SHALL keep the extracted text, the preserved notes and the prior-extraction pages byte for byte. A sidecar that the boundary guard cannot classify SHALL be skipped and counted. An original that is not local SHALL be skipped and counted, never hydrated by this operation. The operation SHALL run only when the owner starts it.

#### Scenario: A sidecar with owner notes

- **WHEN** the operation reaches a sidecar that carries owner notes after its extracted text
- **THEN** the notes and the extracted text are byte-identical after the write
- **AND** the capture block is added

#### Scenario: An ambiguous legacy sidecar

- **WHEN** the boundary guard cannot tell extracted text from notes in a sidecar
- **THEN** the sidecar is unchanged and counted as skipped
