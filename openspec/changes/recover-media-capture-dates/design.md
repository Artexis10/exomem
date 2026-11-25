## Context

A capture date is evidence only when a file states it. Exomem never reads the file's modification time for a capture time (scanners and editors set it). The 2026-11-24 backfill kept that rule and left every photo without EXIF undated. Two other facts can date such a photo honestly: a file name that a camera or screenshot tool wrote, and another copy of the same picture that still has its EXIF block. A third is weak and always available: the day the source entered the vault (`captured`).

Design rules applied: one obvious operation for the owner, and from the existing `media-capture-metadata` spec, a value the file does not state is absent, never a default.

Closest existing options: PIL's `imagehash` package and CLIP vectors. `imagehash` adds a dependency for 20 lines the repository already has (`embeddings._avg_hash`, used by scene detection). CLIP similarity ranks "similar scene" and cannot tell two shots of one scene a second apart, which a date needs to tell apart. The gap is a unique-match rule over a perceptual hash; the added maintenance is one module of about 160 lines.

## Decisions

### D1. The source is a registry key; code branches on the class

`capture-date-sources.yaml` is a shipped pack whose entries carry two closed attributes: `class` (`evidence` or `fallback`) and `producer` (the code path that writes the key). Code looks a key up by producer and branches only on class and pack order, never on a key. The overlay cannot add a source, because a source is a producer that code implements. Pack order is the precedence among evidence sources: camera, matched original, file name.

### D2. File-name shapes are a second registry that the overlay extends

New phone and screenshot tools add naming shapes over time, so they are data: each entry has a `pattern` that names the groups `year`, `month`, `day`, `hour`, `minute` and `second`, a `maker`, and a one-line description of why the name states a time. A shape that states less than a time to the second is refused, because `capture_time` carries a time. A leading set number such as `01-` is stripped by its shape before the match, and the raw-protected capture marker is stripped first. A year before 1990 or after next year states nothing.

### D3. The fallback is derived at read time and stored nowhere

`structured_filters.page_view` is the one place that turns a sidecar into filter fields, for the in-memory evaluator and for the index columns alike. For an image or video sidecar with no `capture_date`, it sets `capture_date` from `captured` and `capture_date_source` to the fallback key. A scene frame or a part page is no media sidecar and gets none. Nothing is written to the sidecar, so a stored page never carries a fallback day that could be mistaken for evidence, and the day follows `captured` if that is corrected. The lexstore catalogue gains the two columns (schema 19); an older store reads as not current until the owner pass fills them.

### D4. A match is a date, not a file

The recovery pool holds the vault's camera-dated images and, on a personal server, the images under `originals_root`. A copy is matched by a 16x16 average hash (256 bits, box resample, EXIF-transposed) and its aspect ratio. A match is accepted only when the nearest original is within 24 bits, the aspect ratios agree within 2%, and every original that states a different capture time is at least 24 bits farther. Two copies of one original state one time, so they never make a match ambiguous. A picture with fewer than 64 set or clear bits has no structure and is never matched. The thresholds are set from the design reasoning above and are not yet calibrated on a real archive; the preview lists every unmatched page with its reason so the owner sees what the bound refuses.

### D5. Recovery lives inside the existing capture-metadata job

`maintenance.capture_metadata` already has preview, `plan_id`, chunked commit, the boundary guard and skip reasons. Recovery adds `recover` and `originals_root` to its arguments and reports through the same preview. `originals_root` implies `recover`, must be an absolute folder outside the vault, and is refused on Evermatter Cloud, which has no such folder. The job opens files under it read-only and never writes, moves or copies anything there. The `plan_id` binds both flags, so a plan names exactly one recovery.

### D6. A recovered date survives re-extraction

`set_capture_fields` replaces the capture block with what the file states. A matched-original date is a fact about another file that no re-read can reproduce, so the writer keeps it unless the new read states a source of higher precedence. Camera and file-name dates follow the file, because a re-read reproduces them. A page already dated from a source that a match cannot outrank is not matched again, so a rerun is stable.

### D7. The notice lives on the receipt

Upload of an image that carries no camera metadata and whose name states no time adds one warning to the receipt: the client sent a copy without it, and the original must come from disk. It names the routes that carry full bytes. The sidecar records nothing about it, because the sidecar's absence of a date already says so.

## Risks

- A wrong match dates a photo wrongly. The unique-match rule and the margin refuse doubtful cases, the source label and `capture_date_matched_from` let the owner audit and filter, and the preview shows what would change before the write. Thresholds may need tuning on a real archive.
- A date search now returns photos filed on that day by arrival, not by capture. The `source-date` label and the `page.capture_date_source` filter make that visible and removable.
- Walking a large `originals_root` costs one decode per image. The job reports its phase (`indexing originals`, `matching`) through the maintenance checkpoint, and RAW files are left out of the walk.
