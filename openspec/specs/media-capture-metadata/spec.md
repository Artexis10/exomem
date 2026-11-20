# media-capture-metadata Specification

## Purpose
Record when and where a photo or video was captured, make capture date and place searchable at indexed cost, and withhold a location from every caller who may not download the original.

## Requirements

### Requirement: Extraction records capture metadata as the file states it

Image and video extraction SHALL record each of these values in the sidecar frontmatter when the file states it:

- capture time (`capture_time`) as the camera recorded it, with its UTC offset when the file records one;
- capture date (`capture_date`), the ISO calendar date of that recorded local time;
- GPS latitude and longitude (`capture_lat`, `capture_lon`);
- camera make and model (`camera_make`, `camera_model`);
- pixel dimensions (`pixel_width`, `pixel_height`).

For images, the capture time SHALL come only from the EXIF original capture time and its offset field. It SHALL NOT come from the file's modification time, which scanners and editors set, or from the generic EXIF date-time field. For a RAW file, the capture time is the RAW reader's timestamp, which the reader may take from the generic date-time field when the file records no original capture time. For a video, the capture time SHALL be the QuickTime creation date with its local offset when the file has one. Otherwise it SHALL be the container's UTC creation time, ending in `Z`, with the capture date taken in UTC and `capture_date_basis` set to `utc`. A GPS block that the file marks void, or whose references are missing or invalid, SHALL count as no position. A value that the file does not state SHALL be absent, never zero, empty or a default date. Camera make and model SHALL be read as UTF-8 text when the bytes are UTF-8, cleaned of control characters and bounded in length. Every value SHALL be written so that the sidecar's frontmatter stays valid YAML whatever the file contains, and a coordinate SHALL be written as a plain decimal number. The extraction write SHALL record these fields together with the extracted text.

#### Scenario: A phone photo with an offset

- **WHEN** a JPEG records an original capture time of 2012-07-14 18:05:00 with offset +03:00 and a valid GPS position
- **THEN** the sidecar records that time with offset +03:00, the capture date 2012-07-14, and that position

#### Scenario: A video with only a UTC creation time

- **WHEN** a video's container records a UTC creation time and no QuickTime creation date
- **THEN** the sidecar records that time ending in `Z`, the UTC date as the capture date, and `capture_date_basis: utc`

#### Scenario: A scanned print

- **WHEN** a scanned image records a modification time but no original capture time and no position
- **THEN** the sidecar has no capture time, no capture date and no position
- **AND** a capture date filter does not match it

### Requirement: Search filters by capture date and by place

A search SHALL accept the capture date as a structured filter (`page.capture_date`) with the existing ordered operators on ISO dates. It SHALL accept a geographic bounding box as a pair of ordered numeric filters on `page.capture_lat` and `page.capture_lon`. A box that crosses the antimeridian SHALL be expressed as two longitude ranges joined by `$or`. A filtered search SHALL use an index, and its cost SHALL NOT grow with the number of media files outside the filter.

#### Scenario: A summer window

- **WHEN** a search filters the capture date between 2012-06-01 and 2012-08-31
- **THEN** results include the photos whose recorded local date falls in that window, with or without an offset

### Requirement: A location is withheld from a caller who may not download the original

A caller who fails an original's download check SHALL see that original's position as absent on every surface, including:

- page reads, excerpts and exports;
- lexical and vector text;
- every filter, including generic frontmatter-pointer filters and `$exists`.

The release check SHALL be evaluated per original, from the original that the page is bound to. A location filter SHALL match only originals that the caller passes the release check for. The UTC offset of the capture time is not a position and is not withheld.

#### Scenario: A reader without original access

- **WHEN** a caller who may read a photo's page but not download its original reads that page
- **THEN** the page shows the capture date and camera but no position

#### Scenario: Raw text that cannot be shown without its position

- **WHEN** that caller reads a sidecar's raw text, and removing the position fields leaves a position in it or the frontmatter does not parse
- **THEN** the raw text is withheld whole, and no position appears on any surface

#### Scenario: A probing filter

- **WHEN** that caller searches with a latitude range, or a frontmatter-pointer filter on the position, that the photo's position would satisfy
- **THEN** the photo does not match, exactly as if it had no position

### Requirement: An owner operation adds capture metadata to existing sidecars without changing their text

One owner operation SHALL add capture metadata to existing image and video sidecars as a `maintain_memory` mode on the maintenance job ledger. A preview SHALL report a `plan_id`, and the write SHALL name that `plan_id` and a reason. The operation SHALL rewrite only the capture block. It SHALL keep the extracted text, the preserved notes and the prior-extraction pages byte for byte. A sidecar that the boundary guard cannot classify SHALL be skipped and counted. An original whose bytes are not on the volume SHALL be skipped and counted, never fetched by this operation. A file that states no capture metadata SHALL leave its sidecar unchanged. A vault whose pages live in the page store SHALL receive the write through that store. The operation SHALL run only when the owner starts it.

#### Scenario: A sidecar with owner notes

- **WHEN** the operation reaches a sidecar that carries owner notes after its extracted text
- **THEN** the notes and the extracted text are byte-identical after the write
- **AND** the capture block is added

#### Scenario: An original that is not on the volume

- **WHEN** the operation reaches a sidecar whose original has no bytes on the volume
- **THEN** the sidecar is unchanged and counted as skipped with the reason `bytes not on the volume`
- **AND** the original is not fetched

#### Scenario: An ambiguous legacy sidecar

- **WHEN** the boundary guard cannot tell extracted text from notes in a sidecar
- **THEN** the sidecar is unchanged and counted as skipped
