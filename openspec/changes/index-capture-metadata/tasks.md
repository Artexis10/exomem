# Tasks

This change is proposed and not authorized for implementation. Its implementation starts only under an approved plan. The backfill on a live vault needs the owner's separate approval.

## 1. Extraction

- [ ] 1.1 Record capture time with offset, local capture date, GPS position, camera and dimensions in image and video extraction. Prove that a scanned image with only a modification time gets no capture date, and that a void GPS block gives no position.

## 2. Filters and disclosure

- [ ] 2.1 Index capture date and position as structured filter fields, including an antimeridian box as two longitude ranges. Prove indexed cost on a 500,000-sidecar vault.
- [ ] 2.2 Withhold the position per original from every caller who fails its release check, on reads, excerpts, exports, text indexes and every filter including frontmatter pointers. Prove that a probing filter matches as if the photo had no position.

## 3. Backfill

- [ ] 3.1 Add the owner metadata-only backfill on the media job ledger. Prove byte-identical extracted text, notes and history pages, and skipped counts for ambiguous sidecars and originals that are not local.

## 4. Acceptance

- [ ] 4.1 On the image-search evaluation set, compare hit@8 for event-dated questions with and without a capture window, per case with a 95% confidence interval.
