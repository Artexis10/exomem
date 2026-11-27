## MODIFIED Requirements

### Requirement: A capture date records its source

A sidecar that records a capture date SHALL record its source as `capture_date_source`, a key of the `capture-date-sources` vocabulary. The vocabulary SHALL declare each key's class, `evidence` or `fallback`, and the order of the evidence keys (`camera-exif`, `matched-original`, `file-name`, `export-sidecar`) SHALL be their precedence. A fallback date SHALL NOT be stored. A file's modification time SHALL NOT be a source.

#### Scenario: A camera date outranks a matched date

- **WHEN** the original of a sidecar with a `matched-original` date later states a camera capture time
- **THEN** the sidecar records the camera time with the source `camera-exif`

#### Scenario: A file-name date outranks an export sidecar date

- **WHEN** a screenshot whose name states a time also has a paired Takeout sidecar with another time
- **THEN** the sidecar page records the file-name time with the source `file-name`

### Requirement: A recovered date survives re-extraction

A `matched-original` date SHALL also record `capture_date_matched_from`, a page id or a path relative to the owner's folder of originals. An `export-sidecar` date SHALL record there the sidecar's path in the export. The vocabulary SHALL mark each source that no re-read of the file can reproduce, and a date from such a source SHALL survive re-extraction, a plain backfill and recovery unless the read states a source of higher precedence.

#### Scenario: A recovered date survives a re-read

- **WHEN** a sidecar records a `matched-original` date and extraction runs again on its re-encoded copy, which states no capture time
- **THEN** the sidecar keeps the recorded date, its source and its origin

#### Scenario: A sidecar date survives the owner backfill

- **WHEN** a photo dated `export-sidecar` states no time of its own and the owner runs the capture-metadata operation
- **THEN** the photo keeps its date, its source and the sidecar path

### Requirement: An owner operation adds capture metadata to existing sidecars without changing their text

One owner operation SHALL add capture metadata to existing image and video sidecars as a `maintain_memory` mode on the maintenance job ledger, run only when the owner starts it. A preview SHALL report a `plan_id`, and the write SHALL name that `plan_id` and a reason. The operation SHALL rewrite only the capture block, keeping the extracted text, the preserved notes and the prior-extraction pages byte for byte. A vault whose pages live in the page store SHALL receive the write through that store.

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

### Requirement: The owner operation skips what it cannot read safely

The capture-metadata operation SHALL skip and count a sidecar that the boundary guard cannot classify, and an original whose bytes are not on the volume, which it SHALL never fetch. A file that states no capture metadata, whose name states no time and that matches no original SHALL leave its sidecar unchanged. A recorded date SHALL NOT be replaced by a date of lower precedence.

#### Scenario: A photo that states nothing

- **WHEN** the operation reaches a photo that states no capture metadata, whose name states no time and that matches no original
- **THEN** its sidecar is unchanged

### Requirement: A Takeout sidecar dates a photo that states no time

When a paired photo or video states no capture time and its name states none, its capture date SHALL come from the paired Takeout sidecar's `photoTakenTime`, with `capture_date_source: export-sidecar` and `capture_date_basis: utc`. A sidecar whose `photoTakenTime` equals its `creationTime` to the second SHALL give no time.

#### Scenario: A received photo

- **WHEN** a JPEG with no EXIF capture time and an undated name files with a paired sidecar whose `photoTakenTime` is 2019-05-04 10:00:00 UTC and whose `creationTime` is a later day
- **THEN** the photo's capture date is 2019-05-04 with source `export-sidecar`

#### Scenario: Google copied the upload time

- **WHEN** a paired sidecar's `photoTakenTime` and `creationTime` are the same second, and the photo states no time
- **THEN** the photo has no evidence capture date
