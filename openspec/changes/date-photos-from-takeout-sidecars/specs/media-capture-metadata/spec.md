## ADDED Requirements

### Requirement: An export sidecar dates a photo that states no time

When a photo or video states no capture time, its capture time SHALL come from its Source page's `stated_capture_time`, with `capture_date_source: export-sidecar` and `capture_date_basis: utc`. The `export-sidecar` source SHALL be evidence, ranked after `camera-exif` and before `matched-original` and `file-name`. A time that the file states SHALL always win.

#### Scenario: A received photo with a Takeout sidecar

- **WHEN** a JPEG with no EXIF capture time files with a paired sidecar whose `photoTakenTime` is 2019-05-04 10:00:00 UTC and whose `creationTime` is a later day
- **THEN** the photo's capture date is 2019-05-04 with source `export-sidecar`

#### Scenario: The camera time wins

- **WHEN** a JPEG states an EXIF capture time and its paired sidecar states another time
- **THEN** the photo keeps the EXIF time with source `camera-exif`

### Requirement: An upload time is never a capture time

A sidecar whose `photoTakenTime` equals its `creationTime` to the second SHALL give no capture time.

#### Scenario: Google copied the upload time

- **WHEN** a paired sidecar's `photoTakenTime` and `creationTime` are the same second, and the photo states no time
- **THEN** the photo has no evidence capture date, and a date search finds it only under `source-date`

### Requirement: A sidecar date survives re-extraction

An `export-sidecar` date SHALL survive re-extraction while the Source page keeps `stated_capture_time`. A `matched-original` or `file-name` date SHALL NOT replace it.

#### Scenario: Re-extraction of a dated photo

- **WHEN** extraction runs again on a photo dated `export-sidecar`
- **THEN** the photo keeps its date and source
