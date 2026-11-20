# Tasks

The backfill on a live vault needs the owner's separate approval. This change ships the operation and no run on a real vault.

## 1. Extraction

- [x] 1.1 Record capture time with offset, local capture date, GPS position, camera and dimensions in image and video extraction. Prove that a scanned image with only a modification time gets no capture date, and that a void GPS block gives no position.

## 2. Filters and disclosure

- [x] 2.1 Index capture date and position as structured filter fields, including an antimeridian box as two longitude ranges. Prove indexed cost on a 500,000-sidecar vault.
- [x] 2.2 Withhold the position per original from every caller who fails its release check, on reads, excerpts, exports, text indexes and every filter including frontmatter pointers. Prove that a probing filter matches as if the photo had no position.

## 3. Backfill

- [x] 3.1 Add the owner metadata-only backfill as a `maintain_memory` mode on the maintenance job ledger. Prove byte-identical extracted text, notes and history pages, and skipped counts for ambiguous sidecars and originals whose bytes are not on the volume.

## 4. Acceptance

The retrieval gain of a capture window is not measured here. The later contextual-retrieval change measures it with an operator-keyed benchmark: its answer keys come from source truth that the operator supplies, never from the EXIF fields this change records.
