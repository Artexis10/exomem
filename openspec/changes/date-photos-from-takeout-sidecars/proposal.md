# Date photos from Google Takeout sidecars

## Why

Google Takeout exports Google Photos as the media files plus one JSON sidecar per photo or video. The sidecar holds what Google Photos knows about the item, including the capture time and the caption the owner typed. Many of these files carry no camera metadata of their own. Examples are photos received in chats, screenshots, scans and edited copies. Today the cell files every sidecar as its own JSON Source. A Takeout of 20,000 photos therefore adds 20,000 JSON Sources that nobody wants, and the photos that state no time of their own stay undated.

## What changes

- **Sidecars are read, not filed.** A recognised Takeout sidecar is paired with the one photo or video in its folder that it describes. A paired sidecar files nothing. The filing view counts it as left out with its own reason. An unpaired sidecar files as an ordinary file, so no bytes are lost.
- **The photo gets the sidecar's capture time.** It is used only when the file itself states none, under a new evidence source `export-sidecar` in the `capture-date-sources` vocabulary. Its place in the order: after `camera-exif`, before `matched-original` and `file-name`.
- **The owner's caption is kept.** A non-empty sidecar description becomes the photo Source's `caption` page field.
- **A guessed time is never used.** When the sidecar's capture time equals its upload time to the second, the time is not treated as evidence.

## Capabilities

### Modified Capabilities

- `media-capture-metadata`: a capture date may come from an export sidecar.
- `app-export-import`: a sidecar is recognised, paired with its media file and left out of filing.

## Impact

- **Code:**
  - `source_formats.py` and `export_expansion.py`: a `photo-sidecar` parser that pairs instead of expanding.
  - `adoption_run.py` and `adopt.py`: the planned item and its page fields.
  - `capture_metadata.py`, `capture_dates.py` and `media_worker.py`: the read order.
  - Vocabulary packs: `source-formats.yaml`, `capture-date-sources.yaml` and `page-fields.yaml`.
- **Order:** the change builds on `import-notes-and-mail-exports` (parser-owned containers and the append-only expansion record) and archives after it and after `recover-media-capture-dates`.
- **Not included:**
  - Album folders' `metadata.json`, which keeps filing as an ordinary file.
  - The sidecar's position: on the Source page it would escape the location withholding. A position that the file itself states is already read.
  - The sidecar's people tags, view count and Google link.
  - Edited copies (`-edited` and its translations) that have no sidecar of their own.
