## Why

A first provision mints the cell's authorization window when its storage initializes, and that window lasts at most one attestation lifetime. Only the generation's own running replica can renew it, and a generation that has reached the governance migration is fenced and has no replica. Every phase before enrollment then refused the closed window, so any provision whose recovery outlived one attestation lifetime was stranded permanently: its cell, volume, reservation and invite stayed allocated and no supported recovery could finish them.

That is the failure that ended the 2026-09-13 hosted alpha provision. The cell initialized at 17:45Z, a defect in an unrelated proof stopped the run, and by the time the repaired release was published, merged, deployed and the operation requeued, the window had closed. The requeue then refused with `authorization-control-is-invalid`, having nothing left to renew.

The window exists to bound what a *serving* replica may authorize. A fenced, issuance-stopped, never-enrolled generation authorizes nothing, so the refusal protected nothing while costing an entire cell. The commit phase already migrates a generation whose window closed and deliberately preserves it rather than renewing.

## What Changes

- The governance migration's inspect, prepare and enroll phases proceed on a fenced generation whose attestation window has closed, extending the tolerance that the commit phase already had.
- Enrollment and the schema-successor repair accept a closed window in the same fenced state, including the self-check that reads back the window enrollment itself preserved.
- Every other proof is unchanged and still refuses: a valid signing keyring, an authentic MAC, the exact expected custody revision, a control not issued in the future, and a replica proven draining, issuance-stopped and with nothing in flight.
- No path renews or extends a window. A closed window stays closed, and making a drained generation serving again still requires a separately authorized resume.

## Capabilities

### Modified Capabilities

- `hosted-tenant-cell`: Fenced governance migration no longer depends on an attestation window that no component can renew.

## Impact

- Affected code: `governance_migration_coordinator.py`, `governance_migration_membership.py`, `authorization_membership.py`.
- Affected tests: the three suites that asserted the refusal now assert the fenced-state acceptance and the refusals that remain.
- Operational: cells stranded by a closed window become recoverable through the existing same-operation governance requeue, with no new operator mechanism and no change to invites, volumes or fences.
