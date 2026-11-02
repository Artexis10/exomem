## 1. Readiness state

- [x] 1.1 Map a managed process that proved its catalogue in this warm, and whose ready bit is clear, to `unavailable` before the warm-active branch, through one state helper shared by `retrieval_admission` and its locked reader.
- [x] 1.2 Allow a re-proof and its publication once this process proved its catalogue in this warm, as after the required warm stages, through one helper used by the probe gate and the publish branch.
- [x] 1.3 Update the `_served_catalogue_lag` documentation: it follows `unavailable` and the proof flag, which now hold during the warm.

## 2. Proof

- [x] 2.1 Through `/health/ready` and `find` on a real vault: a warm that proved its catalogue answers 200 with `lagging` after a later write, recall serves the published catalogue with the lag disclosed, and readiness returns to `admitted=true` once the catalogue catches up.
- [x] 2.2 A warm that has not proved its catalogue answers 503 with `warming` after a write, and recall refuses with its warming outcome.
- [x] 2.3 A lost live projection or a changed access policy during a warm that proved its catalogue answers 503.
- [x] 2.4 Unit: a catalogue proved in this warm is re-proved before the required stages finish; one that was not proved still waits.
- [x] 2.5 Replace the tests that encoded "a running warm withholds the served lag".

## 3. Verification and delivery

- [x] 3.1 Run the scoped readiness, health, recall, standby and managed-restart suites and compare failure names with the base commit; run Ruff, targeted mypy, the privacy gate, `openspec validate --all --strict` and the archive-discipline check.
- [ ] 3.2 Live: on the next promotion under writes, `/health/ready` stays ready from the promoted worker's first catalogue proof to the end of its warm.
