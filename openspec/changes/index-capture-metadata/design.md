# Design

## Context

See `proposal.md` for the outcome. The image viewer already reads the EXIF original capture time (`image_view._taken_at`) for display. Search lanes read the sidecar page, and the structured filter grammar already supports ordered comparisons (`$gt`, `$gte`, `$lt`, `$lte`, `$between`).

The repository records no load target. This design sizes for one vault of 500,000 photos and videos.

## Goals / Non-Goals

**Goals:**
- A search can narrow to a capture time window or a place, at indexed cost.
- Existing photos can gain the metadata without re-running OCR, transcription or CLIP.

**Non-Goals:**
- Place names and reverse geocoding.
- Ranking changes.
- Face or person linkage.
- Events inferred from photo clusters.
- Contextual retrieval itself. That is the next change, which consumes these fields.

## Decisions

### D1: Record what the file says, with its precision

The capture time keeps the file's recorded offset. Without an offset, it is stored as local time with `offset: unknown`.

A time window filter treats an unknown offset as a window widened by the largest real UTC offset range. A photo near the edge of the window is then never lost. The cost is a few extra photos at the edges.

GPS is stored as recorded. A missing value stays absent; it is never zero or the epoch, so "unknown" stays visible.

### D2: One extraction path, metadata-only backfill

New media gets the metadata in the normal extraction write.

Existing sidecars get it through an owner-started job on the media job ledger. The job rewrites only the capture block, under the same sidecar boundary guard that protects preserved notes. It keeps the extraction text and history pages byte for byte.

A sidecar that the guard cannot classify is skipped and counted, never rewritten.

### D3: Location follows the original's release

A precise GPS position can identify a home. Search results and page reads therefore return coordinates only to a caller whose release check would let them download the original.

A bounding-box filter from a caller who may not read the coordinates is refused, with no result count. A count could otherwise reveal where photos were taken.

### D4: Filters are indexed

Capture time and position go into the existing metadata index used by structured filters. A time window or bounding box is then a range query, not a scan of sidecars.

## Risks / Trade-offs

- **Camera clocks are often wrong.** A filter can miss photos from a camera set to the wrong time. Mitigation: none in this change. Contextual retrieval can widen windows when an event match is weak.
- **The backfill rewrites stored sidecars.** Mitigation: it runs only when the owner starts it, it touches only the capture block, and it reports skipped sidecars.

## Migration Plan

New extractions write the fields at once. Existing sidecars change only through the owner-started backfill.

Rollback leaves the fields in place. Older readers ignore unknown frontmatter.
