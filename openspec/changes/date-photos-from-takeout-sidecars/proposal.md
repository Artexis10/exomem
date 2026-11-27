# Date photos from Google Takeout sidecars

## Why

Google Takeout exports Google Photos as the media files plus one JSON sidecar per photo or video. The sidecar holds what Google Photos knows about the item, including a capture time and the caption that the owner typed. Many of the media files carry no camera metadata of their own, such as photos received in chats, screenshots and scans. Today the cell files every sidecar as its own JSON Source. A Takeout of 20,000 photos therefore adds 20,000 JSON Sources that nobody wants. Each of them holds Google's position data as plain text, outside the location withholding that a photo's own position gets. The photos that state no time of their own stay undated.

## What changes

- **Sidecars are read, never filed.** The cell reads every recognised sidecar in a stage before any photo in it files, then pairs a recognised Takeout sidecar with the one photo or video in its folder that it describes, and only on a one-to-one match. No recognised sidecar files as a Source, paired or not. The filing view counts each sidecar by its outcome: applied, nothing to apply, photo already imported (the sidecar's time and caption not applied, because the vault already held the photo), photo not filed, unpaired, or unreadable. A JSON file that cannot be read stays staged and never files, and the pass reads on past it.
- **A photo that states no time gets the sidecar's time.** The time is recorded with a new source `export-sidecar` in the `capture-date-sources` vocabulary. That source ranks below every source that reads the photo itself or a matching original, because `photoTakenTime` is the time that Google Photos showed the owner, who could edit it. The capture record names the sidecar it came from, and the date survives later re-reads and owner maintenance runs.
- **The owner's caption is kept.** A non-empty sidecar description goes into the photo Source's description section, through the same redaction as an upload's description, so search finds it. The redaction runs before the sidecar's row is written to the stage's state folder, so no row holds a card number or a credential.

## Capabilities

### Modified Capabilities

- `media-capture-metadata`: a new evidence source, its rank, survival of dates that no re-read can reproduce, and the name that a file-name date reads.
- `app-export-import`: a JSON-object container, sidecar pairing, photos that wait for their sidecars, outcome counts, and a sidecar that files nothing.
- `structured-retrieval-filters`: the evidence-only filter example includes `export-sidecar`.

## Impact

- **Code:**
  - `source_formats.py` and `export_expansion.py`: a `photo-sidecar` parser that pairs instead of expanding, with one budgeted pass per stage.
  - `adoption_run.py`, `adopt.py` and `preserve.py`: the capture record and the description.
  - `capture_dates.py`: survival by a pack attribute.
  - `capture_metadata.py`, `media_worker.py` and `capture_backfill.py`: a file-name date reads the name the owner gave.
  - `import_filing.py`: the counts.
  - Vocabulary packs: `source-formats.yaml` and `capture-date-sources.yaml`.
  - The skill scaffold's `vault-care.md`.
- **Order:** this change builds on `import-notes-and-mail-exports` (parser-owned containers and the append-only expansion record). It archives after that change and after `recover-media-capture-dates`.
- **Not included:**
  - Pairing across stages. A sidecar whose photo arrives in another archive part stays unpaired, and the filing view counts and lists it. Owner guidance: in Takeout's export form, choose the largest part size that the form offers, so that fewer pairs straddle a part boundary (the design names a community source for the sizes). Each uploaded archive is its own stage, and a larger part cannot rule out a split pair.
  - Applying a sidecar to a photo that the vault already holds.
  - `geoData`, which is Google's inference, not the file's.
  - Album `metadata.json` files, people tags and Google links.
  - Leaving Takeout's trash or locked folders out.
  - A sidecar that reaches a stage after its pass ended. It files as an ordinary JSON file, with its position data as plain text. The pass keeps no list of the JSON files that it found not to be sidecars, so a second reading would open every JSON file again at each call.
  - A time at or before 1970-01-01. It gives no time.
  - Name forms that stay unpaired and are counted as `no-photo`: an extension that Google added where the sidecar and the title have none, a Pixel `.MP` title against a `.MP.jpg` file, and an `-edited` copy, which files undated with no sidecar.
