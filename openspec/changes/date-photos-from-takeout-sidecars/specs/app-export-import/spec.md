## ADDED Requirements

### Requirement: A Takeout photo sidecar is read and left out

A staged JSON object of at most 1 MiB that matches the `google-photos-sidecar` entry SHALL pair with the one image or video in its own folder named by its `title`, taking a copy number from the sidecar's name. A paired sidecar SHALL file nothing. The filing view SHALL count it in `left_out` with reason `IMPORT_SIDECAR_APPLIED`. A sidecar that pairs with no file, or with more than one file, SHALL file as an ordinary file.

#### Scenario: A Takeout album

- **WHEN** a staged folder holds 50 photos and their 50 sidecars, one of them named with a 46-character cut
- **THEN** 50 photo Sources file and no JSON Source does, and the filing view counts 50 left out with reason `IMPORT_SIDECAR_APPLIED`

#### Scenario: A duplicate name

- **WHEN** a folder holds `IMG.jpg`, `IMG(1).jpg`, `IMG.jpg.json` and `IMG.jpg(1).json`
- **THEN** each sidecar pairs with its own photo

#### Scenario: A sidecar without its photo

- **WHEN** a sidecar's photo is in another archive part that was not staged
- **THEN** the sidecar files as an ordinary file

### Requirement: The owner's caption is kept on the photo

A paired sidecar's non-empty `description` SHALL become the photo Source's `caption` page field, so that a search for its words finds the photo.

#### Scenario: A captioned photo

- **WHEN** a paired sidecar's description is "first day at the lake house"
- **THEN** the photo's Source page has that caption, and a search for "lake house" finds the photo
