## 1. Early checks

- [ ] 1.1 On a disposable locked bucket, check the provider's semantics. Evidence: the command outputs and the version listings.
  - Restic `init`, `backup`, `forget --prune`, `check`, `key add` and `key remove` succeed in compliance mode.
  - A delete hides a version without removing it.
  - A lock can be extended but not shortened.
  - The lock time is anchored at creation.
  - A key minted from the parent cannot carry `writeBuckets` or a bypass capability.
- [ ] 1.2 Measure the cost on a real cell's repository. Evidence: the bytes, the call counts and the monthly cost at the provider's published prices.
  - 30 days of hidden-version storage from its churn.
  - Lock extensions at fleet scale, for versions within 7 days of expiry.
  - The age distribution of its packs.

## 2. Bucket and keys

- [ ] 2.1 Add the locked backup bucket, its noncurrent-only lifecycle rule, and a parent key with no `deleteFiles` in Terraform.
- [ ] 2.2 Scope new per-cell keys to the locked bucket, and point new cells' repositories at it.
- [ ] 2.3 Add the lock-extension CronJob in its own namespace, with its own extend-only key and service account, the 31-day guard and an alert on a missed run.

## 3. Migration and recovery

- [ ] 3.1 Copy each existing cell's repository with `restic copy`, one cell at a time, owner first, and switch it.
- [ ] 3.2 Write the recovery procedure, and rehearse it on a disposable cell whose repository was deleted.
- [ ] 3.3 State the backup expiry date in the deletion record of a deleted cell.
