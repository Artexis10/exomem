# Design

## Context

See `proposal.md` for the outcome.

- The image viewer already reads the EXIF capture time (`image_view._taken_at`) for display. It falls back to the file's modification time, which a scanner sets, so this change reads the original capture time only.
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

- This fits the canonical filter grammar as it stands, so the change needs no filter-contract delta.
- It loses no photo at a window edge to an offset conversion.
- The full time and its offset are stored for display and for a later change that needs hours.

### D2: Record what the file says

Only the EXIF original capture time and its offset field count for images. A void GPS block counts as no position. A missing value is absent, so "unknown" stays visible.

### D3: One extraction path, metadata-only backfill

New media gets the metadata in the normal extraction write.

Existing sidecars get it through an owner-started job on the media job ledger. The job rewrites only the capture block, under the same sidecar boundary guard that protects preserved notes. It skips and counts a sidecar the guard cannot classify, and an original that is not local. It never hydrates an evicted original.

### D4: Location follows the original's release on every surface

A precise position can identify a home.

- Withholding coordinates from page reads alone is not enough. A generic frontmatter filter would still let a caller probe them with `$between`.
- Position fields are therefore classified at the field level and resolved per original. Every read, excerpt, export, text index and filter applies the original's release check before it sees the value.
- `add-collection-query-engine` defines the same rule for collection fields (`classification: location`, withheld from non-owners by default). The implementation reuses that classification and release basis when it is canonical, so the vault keeps one location rule.

### D5: Filters are indexed

Capture date and position go into the metadata index that structured filters use. A date window or bounding box is then a range query, not a scan of sidecars.

## Risks / Trade-offs

- **Camera clocks are often wrong.** A filter can miss photos from a camera set to the wrong date. Mitigation: none in this change. Contextual retrieval can widen windows when an event match is weak.
- **The backfill rewrites stored sidecars.** Mitigation: it runs only when the owner starts it, touches only the capture block, and reports what it skipped.
- **Video metadata can sit at the end of a file.** Mitigation: the probe reads the container's metadata atom where it is. Only a local original is read.

## Migration Plan

New extractions write the fields at once. Existing sidecars change only through the owner-started backfill.

Rollback leaves the fields in place, and older readers ignore unknown frontmatter. An older release would not withhold positions from non-owner callers. A downgrade on a vault with non-owner readers therefore needs the backfill fields removed first, and the implementation plan names that step.
