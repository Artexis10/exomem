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
- the vendor's naming facts: the suffix word (`supplemental-metadata`), the sidecar-name limit (46 characters before `.json`), the media-name limit (47), and the characters that become `_` (`&`, `?`).

These are vendor facts that change as Google changes, so they live in the registry. Like every recognition fact, they are fixed in the shipped pack.

The container is one JSON object of at most 1 MiB, read whole. A larger file is not opened. An album's `metadata.json` has no `photoTakenTime`, so it is not recognised.

The parser derives no files. At the start of `file`, it runs one pass over the stage's recognised sidecars. The pass has one state entry, uses the expansion slice budget and resumes from its last row. It appends one row per sidecar, holding the sidecar's path, its name key (Decision 2, steps 1 and 3), and the time and the caption it carries. A row never holds a position.

**Photos wait for the pass.** While the pass runs, the stage's images and videos stay out of selection by their media type, not as a list of paths, the same way an export that is still being read stays out today. The hold lifts when the pass ends in any state, done or partial, so a read fault never holds photos back. A read fault ends the pass as partial: the sidecars read before it keep their pairs, and the sidecars after it are not read and file as ordinary files. When the pass ends, it decides every pair in the stage in one step from its rows (Decision 2, step 4) and records the pairs. Only then do the photos file, each carrying its pair's time and caption on its planned item. The cost is a delay: photos start filing after the pass, which reads about a kilobyte per sidecar.

The pass runs once per stage. A sidecar that reaches the stage after the pass ended is not read. It files as an ordinary file, and its photo files undated. This change builds no second reading for it.

A sidecar row is not a derived file and adds nothing to the stage total. Each sidecar counts once, as a staged member that is left out. Its reason is worked out when filing reaches done, from its photo's filing result (Decision 5).

## Decision 2: pair one to one, by name and by title together

Names are compared exactly after NFC normalisation, without their copy numbers, which are compared separately. Candidates are the images and videos in the sidecar's own staged folder.

1. **From the sidecar's name.** Strip `.json`, a copy number `(n)` before it, and the suffix word or any cut form of it. What remains is the base. The base may carry its own copy number before its extension.
2. **By name.** A media file fits when its stem equals the base's stem and both extensions have the same MIME type, or, when the sidecar name is at the registry's sidecar-name limit, when the base is a prefix of the media name. Step 3 confirms a prefix fit. A copy number must agree too: when the sidecar has one, in either place, the media name has the same one, and when the sidecar has none, the media name has none. A `(n)` that is part of the name itself (`photo (1).jpg`) is read both ways, and the reading that the title confirms is used.
3. **By title.** The media name without its copy number must agree with `title`. Apply the replaced characters and the media-name limit to the title first, compare the stems, and require that both extensions have the same MIME type.
   The MIME type comes from the standard library's built-in extension table, never from the host's `mime.types` files, so one Takeout pairs the same way on every machine. `.jpg` and `.jpeg` have one type. `.heic`, a raw file, a PNG and a video each have their own, so a HEIC and a JPG of one shot, or a raw file and its JPG, each pair with their own sidecar, and `S.jpg.json` never pairs with `S.png`. An extension that the table does not know compares as written, in lower case.
4. **One to one.** When the pass ends, a sidecar pairs only when exactly one file fits it and that file fits no other sidecar in the stage. In every other case, each sidecar involved stays unpaired with a reason: `no-photo` or `ambiguous`.

The copy-number and suffix-cut forms are read with string operations on the registry's suffix word, not with a regex. Task 1.1 tests the forms that TakeoutExtractor and GPTH Neo document, not invented ones: the cut suffixes (`.supplemental-me.json`, `.suppl(1).json`), the cut media name, `&` and `?` replaced by `_`, the copy number in either place, a name with its own `(1)`, and a `.jpeg` title against a `.jpg` file.

Three forms are not read. Each leaves its sidecar unpaired with the reason `no-photo`, so the view counts and lists it:
- **An extension that Google added.** The title and the sidecar name carry none, and the file name carries one (`IMG_1` with `IMG_1.jpg`).
- **A Pixel motion photo.** The title ends in `.MP` and the file in `.MP.jpg`.
- **An `-edited` copy.** Its name carries `-edited` where no title does, so no sidecar fits it. It files with no sidecar and undated. Its original's sidecar still pairs with the original.

## Decision 3: what a paired sidecar carries

- **Time.** The time is `photoTakenTime` in UTC, and the capture date is its UTC day (`capture_date_basis: utc`). A photo taken on an evening west of UTC therefore carries the next day's date, as a video's UTC container time does today, and a date-window search places it on that day. The reader drops a time at or before 1970-01-01 and one after year 9999 as unusable, so a sidecar that states one gives no time. A time is skipped when it equals `creationTime` to the second, the one shape known to be an upload time. Other guesses may pass that check, which is why Decision 4 ranks this source low. Task 3.2 measures it.
- **Caption.** A non-empty `description` goes into the photo Source's description section. It passes through the same redaction as an upload's description (`preserve`, `capture_redaction`), so one caption has one home and search finds it.
- **Nothing else.** `geoDataExif` is the file's own position, which the camera read already records. `geoData` is not used: it may be Google's inference, not the file's.

## Decision 4: rank below every read of the picture, and keep the date

- **Rank.** `capture-date-sources.yaml` gains `export-sidecar`, with `class: evidence` and `producer: export-sidecar`, placed last among the evidence entries, after `file-name`. A camera time, a matched original or a file name therefore always wins. Recovery still tries to match these photos. An owner who changed a date in Google Photos loses that edit when the file states its own time. This is a decision: the file's own statement wins.
- **What the source states.** `photoTakenTime` is the time that Google Photos showed the owner, and the owner could edit it. The source `export-sidecar` therefore says where the date came from, not that the camera took the photo then. It stays `class: evidence`, last among the evidence entries, because any time that the file itself states is a better witness. Task 3.2 measures how often the sidecar time agrees with known truth, and the rank stays until that evidence supports a change.
- **Write.** Filing writes the photo's Source page, which is also its media sidecar, so it writes the time into that page's capture record. It sets `capture_date_source: export-sidecar`, `capture_date_basis: utc`, and `capture_date_matched_from` set to the sidecar's path in the export, so a wrong pair can be audited. The value is the path as a recorded string. The sidecar file stays only as long as the staged copy does, which the owner may discard, so a later audit rests on that string and the photo's own name. No separate page field holds the time. The owner backfill's list of matches (`matched_listed`) is chosen by `capture_date_source: matched-original`, not by the presence of `capture_date_matched_from`.
- **Survival.** `capture_dates.survives_extraction` stops naming `perceptual-match`. It reads a closed pack attribute, `reread: false`, that marks the sources that no re-read can reproduce: `matched-original` and `export-sidecar`. Every writer (the media worker, the owner backfill and recovery) already goes through it, so each keeps the date unless it reads a source of higher precedence.

## Decision 5: honest counts

When filing reaches done, the filing view counts each recognised sidecar in `left_out`, under one of these reasons, from its photo's filing result:
- `IMPORT_SIDECAR_APPLIED`: the sidecar was paired with its photo, which filed now. The sidecar's time applies only where the photo states none, and its caption goes to the page;
- `IMPORT_SIDECAR_EMPTY`: the sidecar was paired with its photo, which filed now, but the sidecar carried nothing (no usable time and no caption);
- `IMPORT_SIDECAR_PHOTO_ALREADY_IMPORTED`: the photo was skipped as already imported, so a Source in the vault holds it. The sidecar counts as filed, and the person has nothing to review. The first copy's Source keeps the time and the caption that it received;
- `IMPORT_SIDECAR_PHOTO_NOT_FILED`: the photo did not file (it failed, or adoption did not select it). The person reviews it;
- `IMPORT_SIDECAR_UNPAIRED`: no file, or more than one, fits the sidecar. The view lists these by path.

The view names what it measures. Over the sidecars of `APPLIED` and `EMPTY`, it counts those that carry a usable time (`with_time`), those that carry a caption (`captioned`) and those whose time was skipped as an upload time (`time_skipped`). `with_time` is not the number of photos that received a date: a photo that states its own time keeps it.

## Scale

The load target is 10,000 tenants, and pairing runs per cell.
- **Pairing.** It builds one name index per folder in a single pass over the stage listing. It reads each sidecar once, at most 1 MiB, inside the slice budget, so a 100,000-photo Takeout pairs in O(n) with one state entry.
- **Record.** Each sidecar adds one appended row.
- **Counting.** Each `file` call reads the sidecar rows two or three times. A measured full read of 25,000 rows (10 MB) takes 0.12 s, against the 2 s budget.

## Risks

- **Undocumented format.** The pairing rule follows community tools. Task 3.2 checks it on a real Takeout, including duplicates and long names.
- **The time's meaning for files without camera time** is unconfirmed. Task 3.2 compares sidecar times with known truth on such files: file-name times on screenshots, and matched originals from the recovery preview. The rank stays last until that measurement supports a change.
- **Split archives.** A photo whose sidecar is in another part stays undated. Task 3.2 measures that share at the default 2 GB part size.

## Spec layout

The owner operation's requirement repeated the precedence order. This change moves its skip rules into their own requirement, which refers to the precedence that "A capture date records its source" states, so the order lives in one place. The operation's behaviour is unchanged.
