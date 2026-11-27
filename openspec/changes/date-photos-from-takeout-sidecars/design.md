# Design: date photos from Google Takeout sidecars

## Context

Google Photos in Takeout holds the media files with one sidecar per item. Each sidecar is a JSON object that carries:
- `title`, the item's name in Google Photos;
- `description`, the caption the owner typed;
- `photoTakenTime.timestamp` and `creationTime.timestamp`, both in epoch seconds UTC;
- `geoData` and `geoDataExif`.

Google publishes no specification of this format. Community tools document the facts below. Sources, checked 2026-11-27: the TakeoutExtractor README, the matcher and tests of GooglePhotosTakeoutHelper Neo (GPTH Neo, issues 123, 133 and 139), and exiftool forum threads.
- A sidecar is named `<media name>.json`, or `<media name>.supplemental-metadata.json` in newer exports. Long names are cut, and the cut can fall inside the suffix (`.supplemental-me.json`, `.suppl(1).json`).
- The media file's own name is cut too, to 47 characters before the extension. `&` and `?` in a title become `_` in the file name, and an extension can differ from the title's (`.jpeg` against `.jpg`).
- A duplicate's copy number appears in either of two places: after the suffix (`IMG.jpg(1).json`) or inside the media name (`IMG(1).jpg.supplemental-metadata.json`). Its `title` keeps the plain name in both cases.
- `creationTime` is the upload time.
- `geoDataExif` is the position that Google read from the file. `geoData` may be Google's own inference.
- A photo and its sidecar can land in different archive parts. Each uploaded archive becomes its own stage.

No source confirms what `photoTakenTime` holds for a file that states no time. GPTH Neo's issue 139 shows the main failure of loose pairing: one photo's date and caption land on another photo.

The `recover-media-capture-dates` change added `capture_date_source`, its precedence and `capture_date_matched_from`. `import-notes-and-mail-exports` gives each parser its own container and adds the append-only expansion record. No entry in the engineering source roster covers deterministic file-format parsing. No model reads this content.

## Decision 1: a sidecar parser that pairs in one pass per stage

A `source-formats` entry `google-photos-sidecar` has:
- `parser: photo-sidecar`;
- `file_glob: "*.json"`;
- `item_keys: [title, photoTakenTime]`;
- the vendor's naming facts: the suffix word (`supplemental-metadata`), the media-name limit (47), and the characters that become `_` (`&`, `?`).

These are vendor facts that change as Google changes, so they live in the registry. Like every recognition fact, they are fixed in the shipped pack.

The container is one JSON object of at most 1 MiB, read whole. A larger file is not opened. An album's `metadata.json` has no `photoTakenTime`, so it is not recognised.

The parser derives no files. At the start of `file`, it runs one pass over the stage's recognised sidecars. The pass has one state entry, uses the expansion slice budget, and resumes from its last row. It appends one row per sidecar with:
- the sidecar's path;
- the paired media path, or none;
- the outcome;
- the time and the caption it carries.

A row never holds a position. A sidecar row is not a derived file and adds nothing to the stage total. Each sidecar counts once, as a staged member that is left out.

## Decision 2: pair one to one, by name and by title together

Names are compared exactly after NFC normalisation. Candidates are the images and videos in the sidecar's own staged folder.

1. **From the sidecar's name.** Strip `.json`, a copy number `(n)` before it, and the suffix word or any cut form of it. What remains is the base. The base may carry its own copy number before its extension.
2. **By name.** A media file fits when its name equals the base, or when the base is the media name cut at the point where the sidecar name reached its limit. A copy number must agree too: when the sidecar has one, in either place, the media name has the same one, and when the sidecar has none, the media name has none.
3. **By title.** The media name without its copy number must agree with `title`. Apply the replaced characters and the media-name limit to the title first, compare the stems, and require that both extensions name the same media type.
4. **One to one.** A sidecar pairs only when exactly one file fits it, and that file fits no other sidecar. In every other case, each sidecar involved stays unpaired with a reason: `no-photo` or `ambiguous`.

The copy-number and suffix-cut forms are read with one regex built from the registry's suffix word. Its one-line reason in code names the vendor's naming format. Task 1.1 tests the forms that TakeoutExtractor and GPTH Neo document, not invented ones.

## Decision 3: what a paired sidecar carries

- **Time.** The time is `photoTakenTime` in UTC. It is skipped when it equals `creationTime` to the second, the one shape known to be an upload time. Other guesses may pass that check, which is why Decision 4 ranks this source low. Task 3.2 measures it.
- **Caption.** A non-empty `description` goes into the photo Source's description section. It passes through the same redaction as an upload's description (`preserve`, `capture_redaction`), so one caption has one home and search finds it.
- **Nothing else.** `geoDataExif` is the file's own position, which the camera read already records. `geoData` is not used: it may be Google's inference, not the file's.

## Decision 4: rank below every read of the picture, and keep the date

- **Rank.** `capture-date-sources.yaml` gains `export-sidecar`, with `class: evidence` and `producer: export-sidecar`, placed last among the evidence entries, after `file-name`. A camera time, a matched original or a file name therefore always wins. Recovery still tries to match these photos. An owner who changed a date in Google Photos loses that edit when the file states its own time. This is a decision: the file's own statement wins.
- **Write.** Filing writes the time into the paired photo's capture record. It sets `capture_date_source: export-sidecar`, `capture_date_basis: utc`, and `capture_date_matched_from` set to the sidecar's path in the export, so a wrong pair can be audited. No separate page field holds the time.
- **Survival.** `capture_dates.survives_extraction` stops naming `perceptual-match`. It reads a closed pack attribute, `reread: false`, that marks the sources that no re-read can reproduce: `matched-original` and `export-sidecar`. Every writer (the media worker, the owner backfill and recovery) already goes through it, so each keeps the date unless it reads a source of higher precedence.

## Decision 5: honest counts

The filing view counts each recognised sidecar in `left_out`, under one of these reasons:
- `IMPORT_SIDECAR_APPLIED`: the photo filed, and a time or a caption was applied;
- `IMPORT_SIDECAR_EMPTY`: the photo filed, but the sidecar carried nothing (no usable time and no caption);
- `IMPORT_SIDECAR_PHOTO_NOT_FILED`: the photo was skipped as already imported, or it failed;
- `IMPORT_SIDECAR_UNPAIRED`: no file, or more than one, fits the sidecar. The view lists these by path.

Skipped times are counted within `APPLIED` and `EMPTY`.

## Scale

The load target is 10,000 tenants, and pairing runs per cell.
- **Pairing.** It builds one name index per folder in a single pass over the stage listing. It reads each sidecar once, at most 1 MiB, inside the slice budget, so a 100,000-photo Takeout pairs in O(n) with one state entry.
- **Record.** Each sidecar adds one appended row.

## Risks

- **Undocumented format.** The pairing rule follows community tools. Task 3.2 checks it on a real Takeout, including duplicates and long names.
- **The time's meaning for files without camera time** is unconfirmed. Task 3.2 compares sidecar times with known truth on such files: file-name times on screenshots, and matched originals from the recovery preview. The rank stays last until that measurement supports a change.
- **Split archives.** A photo whose sidecar is in another part stays undated. Task 3.2 measures that share at the default 2 GB part size.

## Spec layout

The owner operation's requirement repeated the precedence order. This change moves its skip rules into their own requirement, which refers to the precedence that "A capture date records its source" states, so the order lives in one place. The operation's behaviour is unchanged.
