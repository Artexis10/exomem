# Verify Cloud backup repositories

The Cloud controller allows one restic verification check at a time. It starts with an empty allowlist.
An enabled cell can keep serving while its repository is checked.

Each enabled cell is due for its next slice after one day. The shared slot can extend that interval when cells queue for it. The Job and Pod have a five-minute deadline. The Job limits downloads to 8 MiB/s and checks one of 24 slices. A failed attempt leaves the same slice due after an hour.

The controller schedules verification in idle windows. The pinned restic check reads without locking the repository, so backup, prune, restore, and maintenance can continue. An attempt interrupted by repository activity can fail and retry. A slice that consistently exceeds the deadline stays visibly unverified.

## Allowlist and rollback

1. Set `cellctl.resticVerificationCellIds` to the approved cell IDs in platform configuration.
2. Apply the platform configuration through the ordinary deployment workflow.
3. Read the first verification receipt from an approved cell's StatefulSet.

Clear the allowlist to stop new verification. Before downgrading, wait for the reservation to clear or expire and for all verifier Pods and Jobs to finish or disappear.
An observed nonterminal Pod keeps the verification slot even when its Job has disappeared.
The platform chart keeps `exomem-restic-verification-slot` in the controller namespace across upgrades. Keep that ConfigMap in place when replacing cell or controller workloads.
Before it submits a Job, cellctl writes a reservation there. After an uncertain create, the reservation covers the latest possible start, Pod runtime deadline, and shutdown grace.
For a five-minute Job, that window ends ten minutes and thirty seconds after the recorded start. An unavailable reservation defers new verification.
Keep an uncertain running process in the operator's recovery check after force deletion or kubelet loss.

## Read the result

The StatefulSet's `exomem.io/restic-verification` annotation shows the latest attempt. It records its configured repository fingerprint, slice, originating StatefulSet, Job and Pod identity when known, outcome, and timestamps.
`last_success_part` and `last_success_at` retain the last verified slice. A failed or unknown attempt keeps that slice due.
Success requires the admitted scratch-only mounts, runtime digest, and container times for the exact verifier Pod.
A runtime that reports a child digest for the configured OCI index yields an unknown receipt.

The fingerprint identifies the configured repository address. A successful check covers the packs present in its slice when restic read them. Backup freshness and restore evidence remain in their respective records.
In-place restore allows the exact read-only verifier to continue while it waits for other active Jobs and Pods.

## Rehearsal

The isolated k3s/MinIO rehearsal used the pinned restic 0.19.1 binary. It verified a real snapshot from a synthetic cell and detected a one-byte change in a 4,194,791-byte data pack.
After the original pack bytes returned, a new controller client used the durable receipt to retry the same slice and advance.

The cell continued serving with its original successful backup record. The controller observed the two initial successes after 6.50 and 6.84 seconds. It observed the failed check after 6.11 seconds and the retry success after 6.97 seconds. Total network transfer and production provider latency were unmeasured.

A separate pinned-binary probe checked a repository during a real backup. The default check exited with a lock conflict. The read-only check succeeded, left only the backup's existing lock, and the backup completed.

A separate pinned k3s check verified that a Helm upgrade preserves the reservation. The API rejected a stale write and writes outside the reserved key.
