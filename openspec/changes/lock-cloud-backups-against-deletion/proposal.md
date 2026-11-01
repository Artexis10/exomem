## Why

Any credential that writes a Cloud cell's backups can also delete them:
- each cell's object-store key has `deleteFiles`;
- the controller's parent key has `deleteFiles`, and it can create more keys;
- the Cloud bucket has no Object Lock.

So one compromised controller, or one leaked key, can destroy every tenant's backups, and no one can recover them. The recovery and database buckets already use Object Lock. The cluster snapshot bucket already uses a write-only upload key. The Cloud backups have neither.

## What Changes

- **Backups move to a dedicated bucket with Object Lock in compliance mode.** The import and export staging objects stay in the existing bucket, so the lock does not hold them.
- **Locks stay at least 23 days ahead.** A lock runs from an object's creation, so a long-lived restic pack could be close to expiry when an attacker hides it. A daily job therefore extends each current version's lock to today plus 30 days once it is within 24 days of expiry. Compliance mode allows extension, never shortening. After any deletion, every hidden version stays at least 23 days. The job fails closed on any date more than 31 days ahead, because an extension cannot be undone.
- **A delete hides a backup; it does not remove it.** Restic's `forget` and `prune` keep their behaviour: they hide the packs they no longer need. Every hidden version stays until its lock expires. A bucket lifecycle rule removes it after that, and no running service holds a credential that can do so sooner.
- **No running service can shorten the lock.** Compliance mode cannot be shortened or removed by any key, the account owner's included, until each version's retention ends.
- **Recovery from a mass deletion is a runbook procedure.** The procedure restores a cell's repository from its hidden versions.
- **Cell deletion follows the lock.** A deleted cell's backup versions expire when their retention ends. The deletion record states that date, as `make-cloud-cells-operator-resistant` D2 requires.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `cloud-platform-operations`: a new requirement that cell backups survive a compromised credential.

## Impact

- **Terraform:** the locked backup bucket and its lifecycle rule, in the cell-data account. Key creation moves to the key custodian of `contain-cloud-controller-compromise` D9.
- **cellctl:** per-cell keys scoped to the new bucket; the repository location; the migration of existing repositories.
- **Runbooks:** recovery from a mass deletion, and the stated expiry of deleted cells' backups.
- **Cost:** storage of hidden versions for the retention period (task 1.2 measures it).

**Not changed:** the backup format, the backup schedule and restic's retention policy.
