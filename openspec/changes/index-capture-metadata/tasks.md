# Tasks

This change is proposed and not authorized for implementation. Its implementation starts only under an approved plan. The backfill on a live vault needs the owner's separate approval.

## 1. Extraction

- [ ] 1.1 Record capture time with offset, GPS position, camera and dimensions in image and video extraction. Prove that absent values stay absent on a scanned image with no metadata.

## 2. Filters and disclosure

- [ ] 2.1 Index capture time and position, and accept them as structured filters with the widened window for an unknown offset. Prove indexed cost on a 500,000-sidecar vault.
- [ ] 2.2 Return a position only to a caller who passes the original's release check, and refuse a bounding-box filter from any other caller. Prove both, including no result count on refusal.

## 3. Backfill

- [ ] 3.1 Add the owner metadata-only backfill on the media job ledger. Prove byte-identical extracted text, notes and history pages, and a skipped count for ambiguous sidecars.

## 4. Acceptance

- [ ] 4.1 On the image-search evaluation set, compare hit@8 for event-dated questions with and without a capture window, per case with a 95% confidence interval.
