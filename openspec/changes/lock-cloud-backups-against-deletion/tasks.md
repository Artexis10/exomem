## 1. Early checks

- [x] 1.1 On a disposable locked bucket, check the provider's semantics. Evidence: the command outputs and the version listings, run on 2026-11-01.
  - Restic `init`, `backup`, `forget --prune`, `check`, `key add` and `key remove` succeed in compliance mode. Confirmed with restic 0.19.1 over the S3 API.
  - A delete hides a version without removing it. Confirmed, with and without a bypass flag.
  - A lock can be extended but not shortened. Confirmed.
  - The lock time is anchored at creation. Confirmed: the lock ended 24.0 hours after upload under a 1-day default.
  - A key minted from the parent cannot carry `writeBuckets` or a bypass capability. Refuted: see design D3 and `contain-cloud-controller-compromise` D9.
- [x] 1.2 Measure the cost on a real cell's repository. Evidence: the bytes, the call counts and the monthly cost at the provider's published prices, measured on 2026-11-02 with list calls only (design Risks).
  - 30 days of hidden-version storage from its churn: at most about 5.6 GB per cell of the largest real size, $0.04 a month.
  - Lock extensions at fleet scale: about 3.7 million calls a month for 1,000 such cells, all in a call class the provider does not charge for.
  - The age distribution of its packs: a third of the bytes came from the first backup 34 days earlier, and the rest from daily uploads since.
  - Restic retention had never removed a snapshot: each backup carried its Job pod's random hostname, so each snapshot sat in a retention group of its own. The backup now records the cell ID as its host, and `forget` groups across the whole repository.

## 2. Bucket and keys

- [ ] 2.1 Add the locked backup bucket and its noncurrent-only lifecycle rule in Terraform, in the cell-data member account (`contain-cloud-controller-compromise` task 1.1).
- [ ] 2.2 Scope new per-cell keys to the locked bucket, and point new cells' repositories at it. Alert on each cell's storage growth, because a key that writes the locked bucket adds objects that no one can remove for 30 days.
- [ ] 2.3 Add the lock-extension CronJob in its own namespace, with its own extend-only key and service account, the 31-day guard checked against the provider's time, and an alert on a missed run.

## 3. Migration and recovery

- [ ] 3.1 Copy each existing cell's repository with `restic copy`, one cell at a time, owner first, and switch it.
- [ ] 3.2 Write the recovery procedure, and rehearse it on a disposable cell whose repository was deleted.
- [ ] 3.3 State the backup expiry date in the deletion record of a deleted cell.
