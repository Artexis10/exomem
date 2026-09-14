## Why

A first provision mints the cell's authorization window when its storage initializes, and that window lasts at most one attestation lifetime. Only the generation's own running replica can renew it, and a generation that has reached the governance migration is fenced and has no replica. Every phase before enrollment then refused the closed window, so any provision whose recovery outlived one attestation lifetime was stranded permanently: its cell, volume, reservation and invite stayed allocated and no supported recovery could finish them.

That is the failure that ended the 2026-09-13 hosted alpha provision. The cell initialized at 17:45Z, a defect in an unrelated proof stopped the run, and by the time the repaired release was published, merged, deployed and the operation requeued, the window had closed. The requeue then refused with `authorization-control-is-invalid`, having nothing left to renew.

The first repair made the provisioner accept the closed window, but the target-image migration Job reads custody itself and refuses to inspect or prepare under a closed window. The requeued operation then failed every migration Job with the content-free `HOSTED_GOVERNANCE_JOB_FAILED`. The coordinator suite's Job double never modelled that refusal, so the first repair's tests passed against behaviour production does not have.

The window exists to bound what a *serving* replica may authorize. A fenced, issuance-stopped, never-enrolled generation authorizes nothing, so the refusal protected nothing while costing an entire cell. The commit phase already migrates a generation whose window closed and deliberately preserves it rather than renewing.

## What Changes

- The governance migration's inspect, prepare and enroll phases proceed on a fenced generation whose attestation window has closed, extending the tolerance that the commit phase already had.
- Enrollment and the schema-successor repair accept a closed window in the same fenced state, including the self-check that reads back the window enrollment itself preserved.
- Every other proof is unchanged and still refuses: a valid signing keyring, an authentic MAC, the exact expected custody revision, a control not issued in the future, and a replica proven draining, issuance-stopped and with nothing in flight.
- Before an inspect or prepare Job, the coordinator reissues the window of a drained, never-enrolled source whose window has closed or has under fifty minutes left, as its own custody publication, reusing the signer the genesis drain and target recovery already use. It never reissues once a plan exists, never outlasts the signing key, and the generation stays draining with issuance stopped; making it serving again still requires a separately authorized resume.

## Capabilities

### Modified Capabilities

- `hosted-tenant-cell`: Fenced governance migration no longer depends on an attestation window that no component can renew.

## Impact

- Affected code: `governance_migration_coordinator.py`, `governance_migration_membership.py`, `governance_provision_membership.py`, `governance_migration_job.py`, `authorization_membership.py`.
- Affected tests: the three suites that asserted the refusal now assert the fenced-state acceptance and the refusals that remain; the coordinator's Job double refuses a closed window as the runtime Job does; the runtime Job suite runs the provisioner's reissued bytes through the real custody reader; the K3s governance drill migrates a cell whose window closed before migration.
- Known residual: a window that closes after prepare and before enrollment still stalls, because enrollment re-runs the prepare Job under the plan's own window and the runtime refuses it. Closing that needs the runtime Job to accept the plan-bound window, a runtime release outside this change.
- Replay window: a rolled-back custody Secret is now accepted for as long as its signing keyring is valid (366 days) rather than one attestation lifetime. Rolling one back needs write access to the tenant's authorization Secret, and read access to that Secret already yields the recovery envelope, which is enough to mint an authentic bundle outright; replaying a pre-migration bundle against an already-migrated store is refused independently by the Job's own `actualSchema` evidence.
- Operational: cells stranded by a closed window become recoverable through the existing same-operation governance requeue, with no new operator mechanism and no change to invites, volumes or fences.
