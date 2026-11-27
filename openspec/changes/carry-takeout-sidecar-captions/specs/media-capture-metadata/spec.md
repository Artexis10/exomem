## ADDED Requirements

### Requirement: A file-name date reads the name its owner gave

The capture date that a file name states SHALL be read from the name recorded as the page's `original_filename`, falling back to the stored name when none is recorded, for every filed photo or video, in the media worker and in the owner backfill. The cell's filer stores an original under a name of its own, so the stored name SHALL NOT be the only one read.

#### Scenario: A photo filed under a name of its own

- **WHEN** a photo whose original name is `20200314_101112.jpg`, with no capture time in its bytes, is filed under a stored name that states no time
- **THEN** the media worker records `2020-03-14T10:11:12` with source `file-name`
- **AND** the owner backfill records the same for such a photo that the media worker has not read
