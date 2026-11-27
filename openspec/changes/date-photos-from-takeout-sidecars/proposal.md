# Date photos from Google Takeout sidecars

## Why

Google Takeout exports Google Photos as the media files plus one JSON sidecar per photo or video. The sidecar holds what Google Photos knows about the item, including a capture time and the caption that the owner typed. Many of the media files carry no camera metadata of their own, such as photos received in chats, screenshots and scans. Today the cell files every sidecar as its own JSON Source. A Takeout of 20,000 photos therefore adds 20,000 JSON Sources that nobody wants. Each of them holds Google's position data as plain text, outside the location withholding that a photo's own position gets. The photos that state no time of their own stay undated.

## What changes

- **Sidecars are read, never filed.** The cell pairs a recognised Takeout sidecar with the one photo or video in its folder that it describes, and only on a one-to-one match. No recognised sidecar files as a Source, paired or not. The filing view counts each sidecar by its outcome: applied, nothing to apply, photo not filed, or unpaired.
- **A photo that states no time gets the sidecar's time.** The time is recorded with a new source `export-sidecar` in the `capture-date-sources` vocabulary. That source ranks below every source that reads the photo itself or a matching original. The capture record names the sidecar it came from, and the date survives later re-reads and owner maintenance runs.
- **The owner's caption is kept.** A non-empty sidecar description goes into the photo Source's description section, through the same redaction as an upload's description, so search finds it.

## Capabilities

### Modified Capabilities

- `media-capture-metadata`: a new evidence source, its rank, and survival of dates that no re-read can reproduce.
- `app-export-import`: a JSON-object container, sidecar pairing, outcome counts, and a sidecar that files nothing.

## Impact

- **Code:**
  - `source_formats.py` and `export_expansion.py`: a `photo-sidecar` parser that pairs instead of expanding, with one budgeted pass per stage.
  - `adoption_run.py`, `adopt.py` and `preserve.py`: the capture record and the description.
  - `capture_dates.py`: survival by a pack attribute.
  - `import_filing.py`: the counts.
  - Vocabulary packs: `source-formats.yaml` and `capture-date-sources.yaml`.
  - The skill scaffold's `vault-care.md`.
- **Order:** this change builds on `import-notes-and-mail-exports` (parser-owned containers and the append-only expansion record). It archives after that change and after `recover-media-capture-dates`.
- **Not included:**
  - Pairing across stages. A sidecar whose photo arrives in another archive part stays unpaired, and the filing view counts and lists it.
  - Applying a sidecar to a photo that the vault already holds.
  - `geoData`, which is Google's inference, not the file's.
  - Album `metadata.json` files, people tags and Google links.
  - Leaving Takeout's trash or locked folders out.
