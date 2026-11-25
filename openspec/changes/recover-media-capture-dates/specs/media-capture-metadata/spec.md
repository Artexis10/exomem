## MODIFIED Requirements

### Requirement: Extraction records capture metadata as the file states it

Image and video extraction SHALL record each of these values in the sidecar frontmatter when the file states it:

- capture time (`capture_time`) as the camera recorded it, with its UTC offset when the file records one;
- capture date (`capture_date`), the ISO calendar date of that recorded local time;
- GPS latitude and longitude (`capture_lat`, `capture_lon`);
- camera make and model (`camera_make`, `camera_model`);
- pixel dimensions (`pixel_width`, `pixel_height`);
- where the capture time came from (`capture_date_source`), a key of the `capture-date-sources` vocabulary.

For images, the capture time SHALL come only from the EXIF original capture time and its offset field. It SHALL NOT come from the file's modification time, which scanners and editors set, or from the generic EXIF date-time field. For a RAW file, the capture time is the RAW reader's timestamp, which the reader may take from the generic date-time field when the file records no original capture time. For a video, the capture time SHALL be the QuickTime creation date with its local offset when the file has one. Otherwise it SHALL be the container's UTC creation time, ending in `Z`, with the capture date taken in UTC and `capture_date_basis` set to `utc`. A GPS block that the file marks void, or whose references are missing or invalid, SHALL count as no position. A value that the file does not state SHALL be absent, never zero, empty or a default date. Camera make and model SHALL be read as UTF-8 text when the bytes are UTF-8, cleaned of control characters and bounded in length. Every value SHALL be written so that the sidecar's frontmatter stays valid YAML whatever the file contains, and a coordinate SHALL be written as a plain decimal number. When the file states no capture time, the capture time SHALL be the time that the file's name states in a registered file-name format (`capture-date-name-formats`), and the source SHALL be `file-name`; a name that matches no format, or whose digits are not a real moment between 1990 and next year, states nothing. A capture time that the file states SHALL carry the source `camera-exif`. A sidecar that records a capture date without a source SHALL be read as `camera-exif`. The extraction write SHALL record these fields together with the extracted text.

#### Scenario: A phone photo with an offset

- **WHEN** a JPEG records an original capture time of 2012-07-14 18:05:00 with offset +03:00 and a valid GPS position
- **THEN** the sidecar records that time with offset +03:00, the capture date 2012-07-14, and that position

#### Scenario: A video with only a UTC creation time

- **WHEN** a video's container records a UTC creation time and no QuickTime creation date
- **THEN** the sidecar records that time ending in `Z`, the UTC date as the capture date, and `capture_date_basis: utc`

#### Scenario: A scanned print

- **WHEN** a scanned image records a modification time but no original capture time and no position, and its name states no time
- **THEN** the sidecar has no capture time, no capture date and no position
- **AND** a capture date filter matches it only as the fallback day that the search requirement below defines, labelled `source-date`

#### Scenario: A screenshot named with its time

- **WHEN** an image with no camera metadata is named `Screenshot from 2026-03-02 14:05:09.png`
- **THEN** the sidecar records the capture time 2026-03-02T14:05:09, the capture date 2026-03-02 and the source `file-name`

### Requirement: An owner operation adds capture metadata to existing sidecars without changing their text

One owner operation SHALL add capture metadata to existing image and video sidecars as a `maintain_memory` mode on the maintenance job ledger. A preview SHALL report a `plan_id`, and the write SHALL name that `plan_id` and a reason. The operation SHALL rewrite only the capture block. It SHALL keep the extracted text, the preserved notes and the prior-extraction pages byte for byte. A sidecar that the boundary guard cannot classify SHALL be skipped and counted. An original whose bytes are not on the volume SHALL be skipped and counted, never fetched by this operation. A file that states no capture metadata, whose name states no time and that matches no original SHALL leave its sidecar unchanged. A sidecar's recorded date SHALL NOT be replaced by a date of lower precedence, in the order camera, matched original, file name. A vault whose pages live in the page store SHALL receive the write through that store. The operation SHALL run only when the owner starts it.

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

## ADDED Requirements

### Requirement: A capture date records its source

A sidecar that records a capture date SHALL record its source as `capture_date_source`, a key of the `capture-date-sources` vocabulary. The vocabulary SHALL declare each key's class, `evidence` or `fallback`, and the order of the evidence keys (`camera-exif`, `matched-original`, `file-name`) SHALL be their precedence. A fallback date SHALL NOT be stored. A file's modification time SHALL NOT be a source.

#### Scenario: A camera date outranks a matched date

- **WHEN** the original of a sidecar with a `matched-original` date later states a camera capture time
- **THEN** the sidecar records the camera time with the source `camera-exif`

### Requirement: A recovered date survives re-extraction

A `matched-original` date SHALL also record `capture_date_matched_from`, a page id or a path relative to the owner's folder of originals. It SHALL survive re-extraction and a plain backfill unless the read states a source of higher precedence.

#### Scenario: A recovered date survives a re-read

- **WHEN** a sidecar records a `matched-original` date and extraction runs again on its re-encoded copy, which states no capture time
- **THEN** the sidecar keeps the recorded date, its source and its origin

### Requirement: Recovery accepts only a unique match

The owner operation SHALL accept `recover`, which dates an image that states no capture time from a camera-dated image in the vault. A match SHALL be accepted only when one capture time explains the picture: the nearest original is within the match distance, the aspect ratios agree, every original with a different capture time is at least the margin farther, and both pictures agree again at a finer scale. A refused match SHALL leave the sidecar unchanged and be counted under its reason.

#### Scenario: A re-encoded copy without EXIF

- **WHEN** the vault holds a camera-dated photo and a resized, re-encoded copy of it with no EXIF block
- **THEN** the recovery write gives the copy the original's capture time, the source `matched-original` and the original's page id as `capture_date_matched_from`

#### Scenario: Two shots of one scene

- **WHEN** a copy is as close to two originals that state different capture times as to either one
- **THEN** the copy is unmatched with the reason that another original is nearly as close
- **AND** its sidecar is unchanged

#### Scenario: Two copies of one original

- **WHEN** two originals state one capture time and a copy matches both
- **THEN** the match is accepted

#### Scenario: A look-alike of a different picture

- **WHEN** an original is a photo of one printed page and a copy without EXIF is a photo of a different page that passes the hash bound
- **THEN** the copy is unmatched with the reason that the closest original differs at a finer scale
- **AND** its sidecar is unchanged

### Requirement: The owner's folder of originals is read-only and personal-server only

The operation SHALL accept `originals_root`, an absolute folder outside the vault whose camera-dated images join the originals, and it implies `recover`. It SHALL NOT write, move or copy anything under that folder. Evermatter Cloud SHALL refuse it with `ORIGINALS_ROOT_UNAVAILABLE` before it reads anything.

#### Scenario: An original in the owner's folder

- **WHEN** `originals_root` holds the camera-dated original of a copy that the vault lacks
- **THEN** the copy records the original's time, the source `matched-original` and the original's path relative to `originals_root`
- **AND** no file under `originals_root` is changed

#### Scenario: Evermatter Cloud refuses the folder

- **WHEN** the operation runs on Evermatter Cloud with `originals_root`
- **THEN** it fails with `ORIGINALS_ROOT_UNAVAILABLE` before it reads anything

### Requirement: The recovery preview reports what it will date

The preview SHALL report `dated_by_source` (dates by source after the plan applies), `undated`, each unmatched reason with its count and a bounded page list (`unmatched`, `unmatched_listed`), and a bounded list of planned matches with time and origin (`matched_listed`). Each planned page's receipt row SHALL carry its source, time and origin. The `plan_id` SHALL bind `recover` and `originals_root`, so a plan names exactly one recovery.

#### Scenario: A preview before the write

- **WHEN** the owner previews a recovery on a vault with one matchable copy and one unrelated image
- **THEN** `dated_by_source` counts the matched date and `unmatched` counts the unrelated image under its reason
- **AND** `matched_listed` names the copy with the matched time and the original it came from
- **AND** no sidecar changes until the write names the `plan_id`

### Requirement: Upload says when an image arrives without camera metadata

The upload receipt of a JPEG or HEIC photo that carries no camera metadata and whose name states no capture time SHALL warn that the client sent a copy without it, and that the original file from disk keeps it. The warning SHALL name the routes that carry the full bytes. An image whose metadata cannot be judged, and an image of another format, SHALL NOT receive the warning.

#### Scenario: A chat attachment

- **WHEN** a client uploads a re-encoded JPEG with no EXIF block and a name that states no time
- **THEN** the receipt carries the warning

#### Scenario: A screenshot

- **WHEN** a client uploads a PNG diagram that never carried camera metadata
- **THEN** the receipt carries no such warning

### Requirement: A photo or video with no evidence date answers a date search under a fallback day

A search SHALL treat an image or video sidecar that records no capture date as captured on the day in its `captured` field, under the fallback source, and SHALL store nothing. The derived day SHALL answer `page.capture_date`, and `page.capture_date_source` SHALL read as the fallback key. A scene frame and a part page SHALL get none. A search hit SHALL expose `capture_date_source`.

#### Scenario: A filed scan in a date window

- **WHEN** a search filters the capture date to a window that holds the day a scan was filed, and the scan states no date
- **THEN** the scan is a result and its source reads `source-date`

#### Scenario: Evidence only

- **WHEN** the same search also requires `page.capture_date_source` to be `camera-exif`
- **THEN** the scan is not a result
