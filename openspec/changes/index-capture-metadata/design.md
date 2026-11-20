# Design

## Context

See `proposal.md` for the outcome.

- The image viewer already reads the EXIF capture time (`image_view._taken_at`) for display. It falls back to the file's modification time, which a scanner sets, so extraction does not reuse it: one capture reader reads the original capture time only. The viewer keeps its own display read, because it serves a person looking at one picture and a fallback is acceptable there.
- Search lanes read the sidecar page.
- The canonical structured filter grammar orders scalar numbers, ISO dates and timezone-qualified date-times. A comparison against an incompatible type is false.
- Any caller can filter frontmatter through a generic pointer.

The repository records no load target. This design sizes for one vault of 500,000 photos and videos.

## Goals / Non-Goals

**Goals:**

- A search can narrow to a capture date window or a place, at indexed cost.
- Existing photos can gain the metadata without running OCR, transcription or CLIP again.

**Non-Goals:**

- Place names and reverse geocoding.
- Ranking changes.
- Face or person linkage.
- Events inferred from photo clusters.
- Contextual retrieval itself. The next change consumes these fields.

## Decisions

### D1: Filter on the recorded local calendar date

Most camera times carry no offset. The question people ask is about the local calendar ("that summer", "Christmas 2012"). The filter field is therefore the ISO date of the recorded local time.

- The date fits the canonical filter grammar: an ISO date takes the existing ordered operators. The change still amends the structured-retrieval-filters contract, in two ways. It adds the reserved fields `page.capture_date`, `page.capture_lat` and `page.capture_lon`, and it adds the rule that a withheld field reads as missing in every filter (D4).
- It loses no photo at a window edge to an offset conversion.
- The full time and its offset are stored for display and for a later change that needs hours.
- A video whose container records only a UTC creation time has no local clock. Its `capture_time` ends in `Z`, its `capture_date` is the UTC date, and `capture_date_basis: utc` says so. That date can differ by one day from the date where the video was shot, and a window edge can then miss or catch it.

### D2: Record what the file says

A missing value is absent, so "unknown" stays visible. The sidecar fields are `capture_time`, `capture_date`, `capture_lat`, `capture_lon`, `camera_make`, `camera_model`, `pixel_width` and `pixel_height`, plus `capture_date_basis` for the UTC-only video case.

- **Images.** Only the EXIF original capture time (`DateTimeOriginal`) and its offset field (`OffsetTimeOriginal`) count. The generic `DateTime` field and the modification time never do. A GPS block whose status is void, or whose references are missing or invalid, counts as no position. A coordinate outside its range counts as none.
- **Dimensions.** The pixel dimensions are the stored ones. The reader does not apply the EXIF orientation, so a portrait photo that a camera stores sideways reports its stored width and height.
- **RAW files.** The RAW reader's timestamp is the capture time for RAW files, on purpose: it is the only time that reader exposes. It can come from a field other than `DateTimeOriginal`. Make, model and position are not read from RAW files.
- **Videos.** The QuickTime creation date with its local offset (`com.apple.quicktime.creationdate`) wins when present. Otherwise the container's `creation_time`, which is UTC, gives the time and the UTC date described in D1.
- **Untrusted input.** Every value comes from an untrusted file. The reader bounds each value (text length, dimension range, coordinate range) and a malformed block loses only its own fields.

### D3: One extraction path, metadata-only backfill

New media gets the metadata in the normal extraction write.

Existing sidecars get it through an owner-started `maintain_memory` mode, `capture-metadata`, on the maintenance job ledger. It follows `convert-originals`: a preview job returns a `plan_id`, and the write names that `plan_id` and a `why`. Its own writer replaces only the capture block, under the same sidecar boundary guard that protects preserved notes. The existing extraction writers are not reused, because they rewrite the extracted text.

- It skips and counts a sidecar the guard cannot classify (`AMBIGUOUS_SIDECAR_BOUNDARY`) and an original whose bytes are not on the volume. It never fetches an evicted original back.
- A file that states nothing is counted and left as it is. The job never removes an existing block on that evidence.
- It commits through the sidecar's own writer, so a vault whose pages live in the page store gets them through that store.
- It lists at most 200 skipped pages by path. The counts per reason are always exact.

### D4: Location follows the original's release on every surface

A precise position can identify a home.

- Withholding coordinates from page reads alone is not enough. A generic frontmatter filter would still let a caller probe them with `$between`.
- Position fields are therefore classified at the field level, once, and resolved per original. Every read, excerpt, export, text index and filter applies the original's release check before it sees the value. "May not see" means that the caller fails the download check (`release_allows_download`) for the original that the page is bound to. A caller who may read the page text but not download the original sees no position.
- The UTC offset in `capture_time` is not withheld. It reveals a timezone band, which is far coarser than a position, and the time it qualifies is already released with the page.
- A media position follows the release of its original. This is a different basis from the collection rule in `add-collection-query-engine`, where a field with `classification: location` stays withheld from non-owners until an explicit release. The two rules stay separate: a media sidecar carries no per-field release, and a collection field carries no original.

### D5: Filters are indexed

Capture date and position go into the metadata index that structured filters use, as four columns on the page table (the day, its typed value and the two coordinates), with a partial index on the day, the latitude and the longitude. A date window or bounding box is then a range query, not a scan of sidecars. SQLite's cost estimate prefers the scope index over a range index, so the query marks the scope term as not usable for index selection when a capture range can drive the scan.

- A box that crosses the antimeridian is two longitude ranges joined by `$or`. A bounding box may use either its latitude or its longitude index, whichever the planner prefers.
- For a caller who may not see every position, the index returns a superset, and the in-memory filter then decides each page after the per-original release check (D4). A withheld page therefore matches as one without a position: same count, same ranking, same errors.

## Risks / Trade-offs

- **Camera clocks are often wrong.** A filter can miss photos from a camera set to the wrong date. Mitigation: none in this change. Contextual retrieval can widen windows when an event match is weak.
- **The backfill rewrites stored sidecars.** Mitigation: it runs only when the owner starts it, touches only the capture block, and reports what it skipped.
- **Video metadata can sit at the end of a file.** Mitigation: the probe reads the container's metadata atom where it is. Only an original with bytes on the volume is read.
- **A UTC-only video can land one day off.** Mitigation: `capture_date_basis: utc` marks the date, and contextual retrieval can widen windows (see the camera-clock risk).
- **A page store built before the capture columns.** The catalogue schema stamp is 18. `catalogue_schema_current` reads a store with an older stamp, or without the capture columns, as not current. `create_catalogue_schema` adds the columns and their indexes at the owner's next pass, so such a store heals without a manual step. Until then its reads answer "unchecked", never a wrong result.

## Migration Plan

New extractions write the fields at once. Existing sidecars change only through the owner-started backfill.

Rollback leaves the fields in place, and older readers ignore unknown frontmatter. An older release would not withhold positions from non-owner callers. A downgrade on a vault with non-owner readers therefore needs the backfill fields removed first, and the implementation plan names that step.
