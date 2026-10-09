## 1. Implementation

- [x] 1.1 Add the dependencies and packaged revisions; verify the pinned lock and a fresh wheel installation.
- [x] 1.2 Integrate the connection owner; verify raw reads, physical rollback, errors, and accounting with the existing transaction workflows.
- [x] 1.3 Replace SQL construction with Core; verify canonical writes, exact typed values, projection maintenance, and raw TEMP queries.
- [x] 1.4 Integrate Alembic revision tracking; verify historical upgrades, ceilings, atomic failure, concurrent vault isolation, and metadata validation for readers and snapshots.

## 2. Verification and Delivery

- [x] 2.1 Prove the actual old-version-8 read/write/snapshot/restore round trip, preserving metadata, history, sources, values, and subsequent writes.
- [x] 2.2 Verify the installed S1 import/query/correction/recovery/snapshot workflow on the integrated candidate.
- [x] 2.3 Obtain exact independent review, a clean completion corpus, and green required CI for the integrated batch.
  PR #1624 record (issue comment 6062704248): round 18 independent review APPROVE on the exact merge head; required CI 37897615695 green on that tree.
  The serial completion corpus (29,340 items on 726c3a87a) found no failure in the batch: each failure was identical on base 704875f05 or passed in isolation, including one wall-clock bound.
- [x] 2.4 Merge the reviewed batch through its existing PR; verify main and notify the dependent owners.
  PR #1624 merged as 1f178c90f; main push CI 37900531213 and the dispatched full CI 37900637883 ran on that commit; the RAW and release owners were notified at that SHA.
- [ ] 2.5 Coordinate the ordinary release; verify publication and close this change through OpenSpec without overlapping aliases.
