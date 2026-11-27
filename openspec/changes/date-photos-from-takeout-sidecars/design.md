# Design: read Google Takeout sidecars, keep their captions and hold their dates

## Context

Google Photos in Takeout holds the media files with one sidecar per item. Each sidecar is a JSON object that carries:
- `title`, the item's name in Google Photos;
- `description`, the caption the owner typed;
- `photoTakenTime.timestamp` and `creationTime.timestamp`, both in epoch seconds UTC;
- `geoData` and `geoDataExif`.

Google publishes no specification of this format. Community tools document the facts below, and each fact names its source. The sources were read on 2026-11-27, at the commits named:
- **README**: the TakeoutExtractor README at commit `3b6c274`, sections "Some Notes on Takeout's Image File Naming", "File Name Uniqueness" and "EXIF Metadata": https://github.com/andyjohnson0/TakeoutExtractor/blob/3b6c274caea5e85d1226f3981689f22e737c80d1/README.md
- **Matcher**: the sidecar matcher of GooglePhotosTakeoutHelper Neo (GPTH Neo) at commit `ab1e56d`: https://github.com/Xentraxx/GooglePhotosTakeoutHelper_Neo/blob/ab1e56d03c51fbc6d12fcf04799bd54ebe2531a7/lib/common/services/json_metadata_services/json_metadata_matcher_service.dart
- **Date extractor**: GPTH Neo's `json_date_extractor.dart`, lines 165-167: https://github.com/Xentraxx/GooglePhotosTakeoutHelper_Neo/blob/ab1e56d03c51fbc6d12fcf04799bd54ebe2531a7/lib/steps/step_04_extract_dates/date_extractors/json_date_extractor.dart#L165-L167
- **Issues 123, 133 and 139** of GPTH Neo: https://github.com/Xentraxx/GooglePhotosTakeoutHelper_Neo/issues/123, https://github.com/Xentraxx/GooglePhotosTakeoutHelper_Neo/issues/133 and https://github.com/Xentraxx/GooglePhotosTakeoutHelper_Neo/issues/139

No exiftool forum thread is cited: the forum could not be fetched from the build environment, so none was read, and no fact here rests on one.

Facts and their sources:
- A sidecar is named `<media name>.json`, or `<media name>.supplemental-metadata.json` in newer exports. Long names are cut, and the cut can fall inside the suffix (`.supplemental-me.json`, `.suppl(1).json`). Sources: Matcher, lines 107-145 (`#L107-L145`); issue 123.
- The media file's own name is cut to 47 characters before the extension, and the sidecar's name to 46 when the original is that long. `&` and `?` in a title become `_` in the file name. An image's extension can differ between archives (`.jpeg` against `.jpg`). Source: README, "Some Notes on Takeout's Image File Naming" and "File Name Uniqueness".
- A duplicate's copy number follows the suffix in the sidecar's name (`IMG.jpg(1).json`, `IMG.jpg.supplemental-metadata(1).json`), while the media file holds it inside its name (`IMG(1).jpg`). The matcher also tries the number inside the sidecar's media name (`IMG(1).jpg.supplemental-metadata.json`), the form that task 1.1 tests. Its `title` keeps the plain name. Sources: README, "File Name Uniqueness"; issue 123; Matcher, lines 252-331.
- `geoDataExif` is the position that Google read from the file. `geoData` may be Google's own inference. Source: Date extractor, lines 165-167.
- An edit that the owner makes on the Google Photos website appears only in the sidecar, not in the file. Source: README, "EXIF Metadata".
- An album folder can hold sidecars with no photo, because Takeout keeps the photo once, in a `Photos from <year>` folder. Source: issue 133.
- A live photo's video has no sidecar of its own: `IMG_1976.HEIC` and `IMG_1976.MP4` share `IMG_1976.HEIC.supplemental-metadata.json`. Source: issue 123.
- A photo and its sidecar can land in different archive parts, and each uploaded archive becomes its own stage. No source above states the first part. Task 3.2 measures it.

No source says what `photoTakenTime` holds for a file that states no time, or that `creationTime` is the upload time. The Matcher's comments tie issue 139 (cross-photo GPS and mis-dated videos) to a sidecar that matched a file of another extension (Matcher, lines 311-345). Pairing by MIME type, one to one, guards against that failure.

The `recover-media-capture-dates` change added the file-name date, which Decision 4 makes read the name that the owner gave. `import-notes-and-mail-exports` gives each parser its own container and adds the append-only expansion record. No entry in the engineering source roster covers deterministic file-format parsing. No model reads this content.

## Held: no sidecar time dates a page

A paired sidecar gives its photo the owner's caption and nothing else. Its `photoTakenTime` is read into the sidecar's row and applied to no page.

The `media-capture-metadata` spec bars a file's modification time as a date source: scanners and editors set it. No source above says that `photoTakenTime` is not that time for a file that states none. A screenshot, a scan or a chat image carries no capture time, so a date that Google derived from the file's modification time would enter the vault as evidence, which the spec forbids.

Task 3.2 measures this on an owner's own Takeout: it compares `photoTakenTime` with the file's modification time inside the archive and with its EXIF. A follow-up change decides dating on that result. That change would also need a rank among the evidence sources, a name for the source and a rule for an owner's edit in Google Photos. None of them is decided here.

## Decision 1: a sidecar parser that pairs in one pass per stage

A `source-formats` entry `google-photos-sidecar` has:
- `parser: photo-sidecar`;
- `file_glob: "*.json"`;
- `item_keys: [title, photoTakenTime]`;
- the vendor's naming facts: the suffix word (`supplemental-metadata`), the sidecar-name limit (46 characters before `.json`), the media-name limit (47), and the characters that become `_` (`&`, `?`).

These are vendor facts that change as Google changes, so they live in the registry. Like every recognition fact, they are fixed in the shipped pack.

The container is one JSON object of at most 1 MiB, read whole. A larger file is not opened. An album's `metadata.json` has no `photoTakenTime`, so it is not recognised.

The parser derives no files. At the start of `file`, it runs one pass over the stage's recognised sidecars. The pass has one state entry, uses the expansion slice budget and resumes from its last row. It appends one row per sidecar, holding the sidecar's path, its name key (Decision 2, steps 1 and 3), and the time and the caption it carries. The row records the time for task 3.2 and nothing applies it. A row never holds a position.

**The row's caption is redacted first.** The rows live in the stage's state folder (`import-expansion/<id>/rows.jsonl`), which is not the Source page. The pass therefore passes each caption through the same redaction as an upload's description (`preserve.redact_capture_text`) before it writes the row. No row, plan, page or log holds a card number or a credential that the owner typed. The plan redacts the caption again, which changes nothing in a caption that is already redacted.

**Photos wait for the pass.** While the pass runs, the stage's images and videos stay out of selection by their media type, not as a list of paths, the same way an export that is still being read stays out today. The hold lifts when the pass ends in any state, done or partial, so a read fault never holds photos back.

**A file that cannot be read.** A JSON file that is gone or that the file system refuses gets its own row, `{"unreadable": path}`, and the pass reads on past it. Every other JSON file of the stage is therefore examined, and none of them files unexamined as a JSON Source with its `geoData` as plain text. The unreadable file stays staged. It never files, and the filing view lists it under `IMPORT_SIDECAR_UNREADABLE` for the person to review: nothing is known of what it holds. A photo whose sidecar was unreadable files without a caption from it. The pass then ends as `partial`, and its fault names the first unreadable file.

When the pass ends, it decides every pair in the stage in one step from its rows (Decision 2, step 4) and records the pairs. Only then do the photos file, each carrying its pair's caption on its planned item. The cost is a delay: photos start filing after the pass, which reads about a kilobyte per sidecar.

The pass runs once per stage. A sidecar that reaches the stage after the pass ended is not read. It files as an ordinary JSON file, with its position data as plain text, and its photo files without its caption. This change builds no second reading, because the pass keeps no list of the JSON files that it found not to be sidecars: telling a new file from an old one would mean opening every JSON file again at each call. The proposal lists the case under "Not included".

A sidecar row is not a derived file and adds nothing to the stage total. Each sidecar counts once, as a staged member that is left out. Its reason is worked out when filing reaches done, from its photo's filing result (Decision 5).

## Decision 2: pair one to one, by name and by title together

Names are compared exactly after NFC normalisation, without their copy numbers, which are compared separately. Candidates are the images and videos in the sidecar's own staged folder.

1. **From the sidecar's name.** Strip `.json`, a copy number `(n)` before it, and the suffix word or any cut form of it. What remains is the base. The base may carry its own copy number before its extension.
2. **By name.** A media file fits when its stem equals the base's stem and both extensions have the same MIME type, or, when the sidecar name is at the registry's sidecar-name limit, when the base is a prefix of the media name. Step 3 confirms a prefix fit. A copy number must agree too: when the sidecar has one, in either place, the media name has the same one, and when the sidecar has none, the media name has none. A `(n)` that is part of the name itself (`photo (1).jpg`) is read both ways, and the reading that the title confirms is used.
3. **By title.** The media name without its copy number must agree with `title`. Apply the replaced characters and the media-name limit to the title first, compare the stems, and require that both extensions have the same MIME type.
   The MIME type comes from the standard library's built-in extension table, never from the host's `mime.types` files, so one Takeout pairs the same way on every machine. `.jpg` and `.jpeg` have one type. `.heic`, a raw file, a PNG and a video each have their own, so a HEIC and a JPG of one shot, or a raw file and its JPG, each pair with their own sidecar, and `S.jpg.json` never pairs with `S.png`. An extension that the table does not know compares as written, in lower case.
4. **One to one.** When the pass ends, a sidecar pairs only when exactly one file fits it and that file fits no other sidecar in the stage. In every other case, each sidecar involved stays unpaired with a reason: `no-photo` or `ambiguous`.

The copy-number and suffix-cut forms are read with string operations on the registry's suffix word, not with a regex. Task 1.1 tests the forms that TakeoutExtractor and GPTH Neo document, not invented ones: the cut suffixes (`.supplemental-me.json`, `.suppl(1).json`), the cut media name, `&` and `?` replaced by `_`, the copy number in either place, a name with its own `(1)`, and a `.jpeg` title against a `.jpg` file.

Four forms are not read. Each leaves its sidecar unpaired with the reason `no-photo`, so the view counts and lists it:
- **An extension that Google added.** The title and the sidecar name carry none, and the file name carries one (`IMG_1` with `IMG_1.jpg`).
- **A Pixel motion photo.** The title ends in `.MP` and the file in `.MP.jpg`.
- **An `-edited` copy.** Its name carries `-edited` where no title does, so no sidecar fits it. It files with no sidecar and no caption. Its original's sidecar still pairs with the original.
- **A copy number between the media name and the suffix word** (`IMG.jpg(1).supplemental-metadata.json`). The Matcher tries this form (its second numbered pattern, lines 266-277), and no source shows Google writing it. The sidecar counts as unpaired; the numbered forms of the sources, after the suffix word or inside the media name, pair.

A live photo's video is not one of these cases: it has no sidecar of its own (issue 123), so the one sidecar pairs with the HEIC or the JPG, and the video files without a caption.

## Decision 3: what a paired sidecar carries

- **Caption.** A non-empty `description` goes into the photo Source's description section. It passes through the same redaction as an upload's description (`preserve`, `capture_redaction`), so one caption has one home and search finds it.
- **Time, recorded and not applied.** The row holds `photoTakenTime` in UTC as `time`. The reader drops a time at or before 1970-01-01 and one after year 9999 as unusable, so such a sidecar has no `time`. The row's `skip` flag marks a time that equals `creationTime` to the second, which the design takes for an upload time. No source above documents `creationTime`, so this is an assumption. No page, count or reason reads either field. Task 3.2 does.
- **Nothing else.** `geoDataExif` is the file's own position, which the camera read already records. `geoData` is not used: it may be Google's inference, not the file's.

## Decision 4: a file-name date reads the name its owner gave, and survival reads the pack

- **File-name date.** The filer stores an original under a name of its own, which states no date. The media worker and the owner backfill therefore read the name recorded as the page's `original_filename`, and fall back to the stored name when none is recorded (`capture_metadata.given_name`). A photo named for its day files with no date and takes it from its name on the media worker's first read. This holds whether or not a sidecar sits beside it.
- **Survival.** `capture_dates.survives_extraction` stops naming `perceptual-match`. It reads a closed pack attribute, `reread: false`, that marks the sources that no re-read can reproduce. Only `matched-original` carries it, so behaviour is unchanged. Every writer (the media worker, the owner backfill and recovery) already goes through it.

## Decision 5: honest counts

When filing reaches done, the filing view counts each recognised sidecar in `left_out`, under one of these reasons, from its photo's filing result:
- `IMPORT_SIDECAR_APPLIED`: the sidecar was paired with its photo, which filed now, and its caption goes to the page;
- `IMPORT_SIDECAR_EMPTY`: the sidecar was paired with its photo, which filed now, but it has no caption to give;
- `IMPORT_SIDECAR_PHOTO_ALREADY_IMPORTED`: the photo was skipped as already imported, so a Source in the vault holds it. The sidecar's caption was not applied, and the reason says so ("{n} sidecar whose caption was not applied, because the vault already held its photo"). The sidecar counts as filed, and the person has nothing to review. The first copy's Source keeps the caption that it received. A person who imports the same Takeout again reads this reason for every sidecar;
- `IMPORT_SIDECAR_PHOTO_NOT_FILED`: the photo did not file (it failed, or adoption did not select it). The person reviews it;
- `IMPORT_SIDECAR_UNPAIRED`: no file, or more than one, fits the sidecar. The view lists these by path;
- `IMPORT_SIDECAR_UNREADABLE`: a JSON file that could be a sidecar and could not be read ("{n} possible sidecar that could not be read"). It stays staged and never files. The person reviews it. The count is of files, not of sidecars: nothing is known of what such a file holds.

The view names what it measures. Over the sidecars of `APPLIED` and `EMPTY`, it counts those that carry a caption (`captioned`). It counts no time, because no time is applied.

## Scale

The load target is 10,000 tenants, and pairing runs per cell.
- **Pairing.** It builds one name index per folder in a single pass over the stage listing. It reads each sidecar once, at most 1 MiB, inside the slice budget, so a 100,000-photo Takeout pairs in O(n) with one state entry.
- **Record.** Each sidecar adds one appended row.
- **Counting.** Each `file` call reads the rows again, because the rows are the record and the state entry only caches the pass. Measured on 2026-11-27 on a laptop, with the stage built in a temporary vault:
  - The pass over 3,000 sidecars and 3,000 photos (3,000 pairs) took 0.93 to 1.01 s in one call, in three runs.
  - A `file` call after the pass ended made 2 reads that returned 6,001 rows in all, so about one full read of 3,000 sidecar rows, 3,000 pair rows and the end row. The first call, which runs the pass, made 13 reads that returned 39,006 rows, or 6.5 full reads.
  - A full read of 25,000 sidecars with 25,000 pairs (11.6 MB) took 0.10 to 0.21 s, in two runs of five reads. A full read of 100,000 of each (46.6 MB) took 0.47 to 0.65 s.
  - A `file` call after the pass ended therefore spends about 0.5 to 0.65 s on the rows at 100,000 sidecars. The first call spends about 6.5 times that if its read count grows with the rows as it did at 3,000, which was not measured at 100,000. Both stay far inside the call's 50 s budget (`request_budget.MCP_REQUEST_BUDGET_SECONDS`). Each cost multiplies two measurements and was not measured at that size.

## Risks

- **Undocumented format.** The pairing rule follows community tools. Task 3.2 counts each sidecar reason on a real Takeout, including duplicates and long names.
- **The time's meaning for files without camera time** is unconfirmed. It is the reason that no time is applied (see "Held"). Task 3.2 compares `photoTakenTime` with the file's modification time inside the archive and with its EXIF, for screenshots, scans and chat images.
- **Split archives.** Each uploaded archive is its own stage, and a sidecar pairs only inside its stage. A photo whose sidecar is in another part files without its caption, and that sidecar counts as unpaired. Owner guidance: in Takeout's export form, choose the largest part size that the form offers, so that fewer pairs straddle a part boundary. GPTH Neo's README names two sizes in that form, 2 GB and 50 GB, and tells the owner to choose 50 GB (README.md, line 139, at commit `ab1e56d`). Google's own page could not be fetched, so the sizes are not confirmed against Google. A larger part cannot rule out a split pair, and the cell cannot join parts. Task 3.2 measures that share on the parts of the owner's own Takeout.
- **`creationTime` as the upload time.** No source above documents it. The design uses it only to flag, in the row, a `photoTakenTime` that equals it to the second. Nothing is applied from either. Task 3.2 counts the flagged rows.
