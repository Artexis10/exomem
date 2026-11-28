# Read Google Takeout sidecars: keep their captions, hold their dates

## Why

Google Takeout exports Google Photos as the media files plus one JSON sidecar per photo or video. The sidecar holds what Google Photos knows about the item, including a capture time and the caption that the owner typed. Today the cell files every sidecar as its own JSON Source. A Takeout of 20,000 photos therefore adds 20,000 JSON Sources that nobody wants. Each of them holds Google's position data as plain text, outside the location withholding that a photo's own position gets. The owner's captions, which are the useful part, never reach the photo's page.

## What changes

- **Sidecars are read, never filed.** The cell reads every recognised sidecar in a stage before any photo in it files, then pairs a recognised Takeout sidecar with the one photo or video in its folder that it describes, and only on a one-to-one match. No recognised sidecar files as a Source, paired or not. The filing view counts each sidecar by its outcome: applied (the photo's page carries its caption), nothing to apply (no caption), photo already imported (the sidecar's caption not applied, because the vault already held the photo), photo not filed, album entry (an album's sidecar whose photo filed from another folder), unpaired, or unreadable. The view counts resolved sidecars under `sidecars` and lists the unresolved ones under `left_out`. A JSON file that cannot be read stays staged and never files, and the pass reads on past it.
- **The owner's caption is kept.** A non-empty sidecar description goes into the photo Source's description section, through the same redaction as an upload's description, so search finds it. The redaction runs before the sidecar's row is written to the stage's state folder, so no row holds a card number or a credential.
- **Dating from a sidecar is held.** The photo Source keeps Google's original `photoTakenTime` and `creationTime` timestamp strings as labelled provenance. The import gives these values no role in capture dating. Task 3.2 compares them with the dates in the owner's Takeout zip and photos. A follow-up can use the provenance if that check supports it.
- **A window keeps a sidecar with its photo.** When the CLI sends a folder in windows, a file whose name reads as a sidecar goes in the window of the photo that its name fits, in its own folder or, for an album's sidecar with no photo beside it, in any folder. Without it, a large folder can send its photos and sidecars in separate windows. If the group cannot fit a window, the sender can send its members separately. The filing view reports any sidecar that cannot pair.
- **A file-name date reads the name the owner gave.** The filer stores an original under a name of its own. The media worker and the owner backfill now read the original's recorded file name, so a photo named for its day takes that date.

## Capabilities

### Modified Capabilities

- `media-capture-metadata`: the name that a file-name date reads.
- `app-export-import`: a JSON-object container, sidecar pairing, photos that wait for their sidecars, outcome counts, captions, and labelled timestamp provenance, and the capture-date hold.

## Impact

- **Code:**
  - `source_formats.py` and `export_expansion.py`: a `photo-sidecar` parser that pairs instead of expanding, with one budgeted pass per stage.
  - `adoption_run.py` and `adopt.py`: the description and labelled Google timestamp provenance.
  - `capture_metadata.py`, `media_worker.py` and `capture_backfill.py`: a file-name date reads the name the owner gave.
  - `import_filing.py`: the counts.
  - `cloud_import_windows.py` and `export_pairing.py`: a likely sidecar travels in its photo's window, decided from names alone.
  - Vocabulary packs: `source-formats.yaml`.
- **Order:** this change builds on `import-app-exports`, whose delta alone holds the `app-export-import` spec (no canonical copy exists yet), then on `import-notes-and-mail-exports` (parser-owned containers and the append-only expansion record) and `recover-media-capture-dates`. It archives after those three, in that order, so that its `MODIFIED` blocks meet the canonical text.
- **Not included:**
  - Dating a photo from a sidecar's time. It is held until task 3.2 reports.
  - Pairing across stages. A sidecar whose photo is in another stage stays unpaired. The filing view counts and lists it.
  - A vault that already imported a Takeout keeps its old JSON Sources. This change removes none of them. Importing the same Takeout again skips its photos as already imported, so their captions are not applied, and the view says so.
  - Applying a sidecar to a photo that the vault already holds.
  - `geoData`, which is Google's inference, not the file's.
  - Album `metadata.json` files, people tags and Google links.
  - Leaving Takeout's trash or locked folders out.
  - A sidecar that reaches a stage after its pass ended. It files as an ordinary JSON file, with its position data as plain text. The pass keeps no list of the JSON files that it found not to be sidecars, so a second reading would open every JSON file again at each call.
  - Name forms that stay unpaired and are counted as `no-photo`: an extension that Google added where the sidecar and the title have none, a Pixel `.MP` title against a `.MP.jpg` file, a copy number between the media name and the suffix word, and an `-edited` copy, which files with no sidecar and no caption.
