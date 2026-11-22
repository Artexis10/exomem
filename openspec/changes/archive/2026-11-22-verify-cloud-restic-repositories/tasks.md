# Tasks

## 1. Run and observe the check

- [x] 1.1 Render an admitted, bounded restic verification Job with temporary scratch and existing repository credentials.
- [x] 1.2 Observe the check Job and its matching Pod in the existing fleet-wide reads.

## 2. Schedule and report verification

- [x] 2.1 Persist the exact attempt, result, and next due slice. Test success, failure, interruption, and restart.
- [x] 2.2 Configure the default-off allowlist and give repository operations priority. Verify the existing backup and serving state.

## 3. Verify delivery

- [x] 3.1 Rehearse a successful backup, corrupted slice, exact retry, and advance on isolated k3s and MinIO.
- [x] 3.2 Document verification status, the measured rehearsal, and the activation and rollback procedure.
