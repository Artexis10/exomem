## 1. Readiness state

- [x] 1.1 Map a managed process that proved its catalogue in this warm, and whose ready bit is clear, to `unavailable` before the warm-active branch, through one state helper shared by `retrieval_admission` and its locked reader.
- [x] 1.2 Allow a re-proof and its publication once this process proved its catalogue in this warm, as after the required warm stages, through one helper used by the probe gate and the publish branch.
- [x] 1.4 Record the proof flag through one helper, and only for a managed process that has started a warm, so a proof made before the warm grants no standing.
- [x] 1.3 Update the `_served_catalogue_lag` documentation: it follows `unavailable` and the proof flag, which now hold during the warm.

## 2. Proof

- [x] 2.1 Through `/health/ready` and `find` on a real vault: a warm that proved its catalogue answers 200 with `lagging` after a later write, recall serves the published catalogue with the lag disclosed, and readiness returns to `admitted=true` once the catalogue catches up.
- [x] 2.2 A warm that has not proved its catalogue answers 503 with `warming` after a write, and recall refuses with its warming outcome.
- [x] 2.3 A lost live projection during a warm that proved its catalogue answers 503: `_served_catalogue_lag` has no warm branch, so the existing lost-projection tests and the shared identity check cover it.
- [x] 2.4 Unit: a catalogue proved in this warm is re-proved before the required stages finish; one that was not proved still waits, and is re-proved once they finish; a proof made before the warm grants no standing.
- [x] 2.5 Replace the tests that encoded "a running warm withholds the served lag".

## 3. Verification and delivery

- [x] 3.1 Run the scoped readiness, health, recall, standby and managed-restart suites and compare failure names with the base commit; run Ruff, targeted mypy, the privacy gate, `openspec validate --all --strict` and the archive-discipline check.
- [x] 3.2 Live: on the next promotion under writes, `/health/ready` stays ready from the promoted worker's first catalogue proof to the end of its warm.
  Verified 2026-11-03 on release 0.158.1, in an online upgrade of a fresh copy of the production vault under steady reads and writes: `/health/ready` sampled every 10 s answered 200 from the promoted worker's first sample through the end of its warm (about 80 s), with no 503. The production promotion to 0.161.1 on 2026-11-05 answered no 503 after the promoted worker turned ready; its samples after promotion were sparse.
- [x] 3.3 At archive, if `keep-recall-available-during-pending-recovery` archived first, merge this recall-read-path block with the canonical requirement and keep that change's scenarios (see design.md, "Archive order").
  That change had not archived, so this change archives first. Its task 5.3 now carries the merge obligation.
