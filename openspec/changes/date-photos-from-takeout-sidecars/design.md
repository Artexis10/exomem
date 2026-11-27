# Design: date photos from Google Takeout sidecars

## Context

Google Photos in Takeout holds the media files with one sidecar per item. The sidecar is named `<media name>.json`, or `<media name>.supplemental-metadata.json` in newer exports. Each sidecar is a JSON object that carries:
- `title`, the media file's name in Google Photos;
- `description`, the caption the owner typed;
- `photoTakenTime.timestamp` and `creationTime.timestamp`, both in epoch seconds UTC;
- `geoData` and `geoDataExif`, each with `latitude` and `longitude`.

Google publishes no specification of this format. The following facts come from community tools and forum reports (exiftool forum, GooglePhotosTakeoutHelper, immich discussions), checked 2026-11-27:
- Sidecar names are cut to 46 characters before `.json`, and the `supplemental-metadata` part is cut further.
- A duplicate's copy number lands in a different place in the sidecar name than in the media name (`IMG.jpg(1).json` for `IMG(1).jpg`).
- `creationTime` is the upload time.
- `geoDataExif` is the position that Google read from the file.
- A photo and its sidecar can land in different archive parts.

The `recover-media-capture-dates` change added `capture_date_source`, the `capture-date-sources` pack and the precedence rule. `import-notes-and-mail-exports` gives each parser its own container and adds the append-only expansion record. No entry in the engineering source roster covers deterministic file-format parsing. No model reads any of this content.

## Decision 1: a sidecar parser that pairs instead of expanding

A `source-formats` entry `google-photos-sidecar` has:
- `parser: photo-sidecar`;
- `file_glob: "*.json"`;
- `item_keys: [title, photoTakenTime]`.

The parser's container is one JSON object, not an array. That object is read whole, and a sidecar over 1 MiB is not recognised. An album's `metadata.json` has no `photoTakenTime`, so it is not recognised.

The parser derives no files. At the start of `file`, it pairs each recognised sidecar with one staged media file, as Decision 2 describes, and records one row per sidecar in the expansion record. The row holds the sidecar path, the media path (or none) and the facts it carries.

## Decision 2: pair by `title`, and only when exactly one file fits

The sidecar's `title`, not its truncated name, names the media file. The candidate files are the images and videos in the sidecar's own staged folder.

1. When the sidecar's name has a copy number `(n)` just before `.json`, the candidate is the file named `title` with `(n)` inserted before its extension. Otherwise, the candidate is the file named `title`.
2. When exactly one file has the candidate name, the sidecar pairs with it.
3. In every other case, the sidecar stays unpaired: no file has the name, or the name is not unique when compared case-insensitively.

The copy-number rule reads a vendor's file-naming format, a closed form, through one regex. Its one-line reason in code names that format. A sidecar never pairs across folders, so a split archive whose parts land in different folders leaves the sidecar unpaired.

## Decision 3: what a paired sidecar carries

- **Capture time.** The time is `photoTakenTime`, in UTC, with `capture_date_basis: utc`. It is skipped when it equals `creationTime` to the second. That equality is the shape of an upload time that Google copied in when it had no capture time. A real capture time almost never equals its upload time to the second. When this rule fires wrongly, a photo stays undated and falls back to `source-date`. It never gets a false date.
- **Caption.** The caption is a non-empty `description`, cleaned of control characters and bounded like other text page fields.

Filing puts these on the planned item of the paired media file:
- The caption becomes a new provenance page field, `caption`, with `source: item-caption`.
- The time goes into a new provenance page field, `stated_capture_time` (ISO 8601, ending in `Z`), with `source: export-sidecar`.
The time sits on the Source page, not only in the stage's record, so a later re-extraction still finds it.

The sidecar's position is not carried. A position on the Source page would escape the location withholding that `media-capture-metadata` requires on every surface (page reads, text, filters). `geoDataExif` is the position that the file itself states, so the camera read already records it when the file has it.

Both fields are entries in `page-fields.yaml`, so a vault overlay may retire them.

## Decision 4: the media worker reads the sidecar's facts below the camera

The media worker's capture step reads in this order:
1. the file's own facts (`camera-exif`);
2. the Source page's `stated_capture_time` (`export-sidecar`);
3. the file name (`file-name`).

`capture-date-sources.yaml` gains `export-sidecar`, with `class: evidence` and `producer: export-sidecar`, placed after `camera-exif`. A recovered `matched-original` date never replaces an `export-sidecar` date. An `export-sidecar` date survives re-extraction for as long as the page keeps the field.

## Decision 5: accounting

- A paired sidecar files nothing. The filing view counts it in `left_out` with reason `IMPORT_SIDECAR_APPLIED`, never as failed.
- An unpaired sidecar files as an ordinary file with no count of its own.
- A sidecar whose time was skipped still pairs, and its caption still carries.

## Scale

The load target is 10,000 tenants, and pairing runs per cell. Pairing builds one name index per staged folder in a single pass over the stage listing, so a 100,000-photo Takeout pairs in O(n). Each sidecar is at most 1 MiB and is read once. Each pairing writes one appended row.

## Risks

- **Undocumented format.** The pairing rule follows community reports. Task 3.2 checks it on a real Takeout, including a duplicate pair and a long name.
- **The time-equals-upload rule** may skip a few real capture times. Task 3.2 counts how many photos it skips on a real export.
