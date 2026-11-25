## Why

A photo sent through a chat client usually arrives as a display-sized copy that the client re-encoded without its EXIF block. Its capture date and position are gone before Exomem sees it. The vault then holds pictures that no date search can find. A backfill on 2026-11-24 added capture metadata to the sidecars whose original still states it, and it skipped every page whose bytes state nothing. Of those pages, some have a file name that states the capture time (a phone camera or screenshot name), and some have an original elsewhere that does state it: another image in the vault or a folder of originals the owner keeps. Today none of that is used, and a date search never finds the photos that lack a stated date.

## What Changes

- A sidecar records where its capture date came from, as `capture_date_source`, a key of a new `capture-date-sources` vocabulary pack: `camera-exif`, `matched-original`, `file-name` (all evidence) and `source-date` (a fallback that is never stored). A matched date also records `capture_date_matched_from`: a page id, or a path inside the owner's folder of originals.
- `manage_memory` `maintenance.capture_metadata` gains a recovery mode (`recover`, and `originals_root` on a personal server). It dates a photo from, in order, the camera, an original that matches it, or its file name. A match is accepted only when exactly one capture time explains the picture. The preview reports the number of dates by source, the photos left undated, and each skip with its reason.
- Ingest reads the file name of a photo or video that states no capture time. The upload receipt of an image without camera metadata warns that the client sent a copy and names the routes that keep the original's metadata.
- Search treats a photo or video with no evidence date as captured on the day it was filed, labelled `source-date`, and stores nothing. A new filter field `page.capture_date_source` lets a caller keep or drop those. Search hits and the filter trace show the source.
- File-name formats are registry data (`capture-date-name-formats`), extendable by the vault overlay.

Out of scope: dating from the file's modification time (never evidence), dating a video by matching, reading a date from the picture's content, and any write under the owner's folder of originals.

## Capabilities

### Modified Capabilities

- `media-capture-metadata`: a capture date has a recorded source; the owner operation can recover dates; ingest reads file names; a photo with no evidence date answers a date search under a fallback label.
- `structured-retrieval-filters`: `page.capture_date_source` is an indexed scalar page field.

## Impact

- Code: `capture_dates.py`, `capture_match.py`, `perceptual_hash.py` (promoted from `embeddings.py`), `capture_metadata.py`, `capture_backfill.py`, `preserve.py`, `media_worker.py`, `structured_filters.py`, `lexstore.py` (catalogue schema 19), `find.py`, `find_types.py`, `commands.py`, `operation_entries.py`, `maintenance_jobs.py`, `retrieval_models.py`, `governance/egress.py`, two vocabulary packs, the skill scaffold.
- Public surface: two optional `manage_memory` maintenance arguments, one filter field and one search-hit field. The tool-surface fingerprint is unchanged by this change (the schema baseline did not change); the capabilities document lists the two arguments.
- Data: an existing store reads as not current until the owner pass adds the two new index columns; a sidecar changes when the owner runs recovery, and a plain capture-metadata run or a re-extraction also stamps `camera-exif` on an older sidecar that already states a camera date (a one-time change).
- Behaviour: a date search now also returns photos and videos with no evidence date, labelled `source-date`. A caller who wants evidence only filters `page.capture_date_source`.
