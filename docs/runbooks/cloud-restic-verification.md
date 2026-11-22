# Verify Cloud backup repositories

The Cloud controller runs one restic verification Job at a time. It starts with an empty allowlist.
An enabled cell can keep serving while its repository is checked.

The default schedule checks one of 24 slices each day. The Job has a five-minute deadline and an 8 MiB/s download limit. A failed check retries the same slice after an hour.
Backup, prune, restore, and cell maintenance take priority. The controller waits until a conflicting verifier finishes releasing its repository lock.

## Allowlist and rollback

1. Set `cellctl.resticVerificationCellIds` to the approved cell IDs in platform configuration.
2. Apply the platform configuration through the ordinary deployment workflow.
3. Read the first verification receipt from an approved cell's StatefulSet.

Clear the allowlist to stop verification. Before downgrading the controller, wait until every verification Job and Pod has finished or disappeared.

## Read the result

The StatefulSet's `exomem.io/restic-verification` annotation shows the latest attempt. It records its configured repository fingerprint, slice, Job UID when observed, outcome, and timestamps.
`last_success_part` and `last_success_at` retain the last verified slice. A failed, busy, or unknown attempt keeps that slice due.

The fingerprint identifies the configured repository address. A successful check covers the packs present in its slice when restic read them. Backup freshness and restore evidence remain in their respective records.

## Rehearsal

The isolated k3s/MinIO rehearsal used the pinned restic 0.19.1 binary. It verified a real snapshot from a synthetic cell, corrupted one byte in a 4,194,795-byte data pack, and detected the damage.
After the original pack bytes returned, a new controller client used the durable receipt to retry the same slice and advance.

The cell continued serving with its original successful backup record. The test observed the three results after 8.64, 11.26, and 7.89 seconds respectively. These are controller observation times from the disposable rehearsal. Total network transfer and production provider latency were unmeasured.
