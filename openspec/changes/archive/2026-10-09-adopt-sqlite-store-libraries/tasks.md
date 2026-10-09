## 1. Implementation

- [x] 1.1 Add the dependencies and packaged revisions; verify the pinned lock and a fresh wheel installation.
- [x] 1.2 Integrate the connection owner; verify raw reads, physical rollback, errors, and accounting with the existing transaction workflows.
- [x] 1.3 Replace SQL construction with Core; verify canonical writes, exact typed values, projection maintenance, and raw TEMP queries.
- [x] 1.4 Integrate Alembic revision tracking; verify historical upgrades, ceilings, atomic failure, concurrent vault isolation, and metadata validation for readers and snapshots.

## 2. Verification and Delivery

- [x] 2.1 Prove the actual old-version-8 read/write/snapshot/restore round trip, preserving metadata, history, sources, values, and subsequent writes.
- [x] 2.2 Verify the installed S1 import/query/correction/recovery/snapshot workflow on the integrated candidate.
- [x] 2.3 Obtain exact independent review, a clean completion corpus, and green required CI for the integrated batch.
  PR #1624 record (issue comment 6062704248): round 18 independent review APPROVE on the exact merge head.
  The serial completion corpus (29,340 items on 726c3a87a) had 39 failures, none attributable to the batch: 8 fail identically on base 704875f05 and 30 pass in isolation on base and HEAD.
  The last one is a wall-clock bound in code that S1 does not change, and it passes 15/15 in isolation on HEAD.
  Required CI 37897615695 ran the full sharded suite green on the merged tree.
- [x] 2.4 Merge the reviewed batch through its existing PR; verify main and notify the dependent owners.
  PR #1624 merged as 1f178c90f. Main push CI 37900531213 and the scheduled full CI 37909339839 passed on that commit.
  The dispatched full CI 37900637883 failed one py3.11 shard on a 200 ms query timeout and passed on rerun (attempt 2).
  That query takes about 4 ms locally on 3.11 and 3.13, so the failure is attributed to a runner stall.
  The RAW and release owners were notified at that SHA.
- [x] 2.5 Coordinate the ordinary release; verify publication and close this change through OpenSpec without overlapping aliases.
  Released as v0.114.0 at a5e8a033a (release PR #1638; release workflow 37917792398 green; full CI 37909339839 green at the base).
  PyPI wheel sha256 11748ec6fd17a90b55b98d2d28a1b03ccd2a108266f22081de06a09fca42ccca; a fresh venv reports 0.114.0, an empty `RELEASED` and 8 Alembic revisions.
  The cloud and cellctl image digests in the release notes match their GHCR manifests. `openspec archive` closes this change under its own name.
