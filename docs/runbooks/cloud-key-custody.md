<!-- authority:non-specification -->

# Exomem Cloud key custody

Each cell reads and writes its backups with one object-store key. The key custodian creates
that key. `cellctl` holds no credential that creates or deletes keys, and no bucket-wide credential in the founder account.
It still holds the stored per-cell key of each cell created before the switch, limited to that cell's prefix, until the cell migrates.

## Credentials

Terraform creates the first three credentials. Their source is `infra/terraform/cell-data/keys.tf`.
Each credential has one holder.

| Credential | Account | Restricted to | Capabilities | Held by |
| --- | --- | --- | --- | --- |
| `exomem-cell-data-key-custodian` | cell-data | None. B2 allows key creation only on an account-level key. | `listKeys`, `writeKeys`, `deleteKeys` | The key custodian, in Secret `exomem-key-custodian-b2` |
| `exomem-cell-data-cellctl-staging` | cell-data | The staging bucket | `listBuckets`, `listFiles`, `readFiles`, `writeFiles`, `deleteFiles` | `cellctl`, for exports, imports and the staging sweep of a deletion |
| `exomem-cell-data-cellctl-backup-list` | cell-data | The locked backup bucket | `listBuckets`, `listFiles` | `cellctl`, to count backup versions for the log |
| One key per cell | cell-data | The locked backup bucket and the prefix `cells/<cell_id>/` | `listBuckets`, `listFiles`, `readFiles`, `writeFiles`, `deleteFiles` | The cell's backup and restore Jobs, in Secret `cell-object-store-key` |
| Operator key | One account per key | None. B2 allows key deletion only on an account-level key. | `listKeys`, `deleteKeys`; for cell-data also `listFiles`, `writeFiles`; for founder also `listBuckets`, `listFiles`, `deleteFiles` | The operator, for the clean-up script only |

The key custodian creates each per-cell key. `cellctl` cannot create that Secret.
Until D3 is in force, `cellctl` can render a Job that reads the Secret, so it can reach a cell's key by that route.
No other credential can create or delete a key.

## Clean up after a cell deletion

`cellctl` cannot delete the deleted cell's key. It cannot delete a new cell's backup files, which the lock keeps.
A cell created before the key custodian keeps its backups in the old founder bucket. `cellctl` holds that
cell's stored key until the cell migrates, but it does not delete the files. When a cell reaches `deleted`, `cellctl` raises the platform alert
`cell-storage-cleanup-pending-<cell_id>`. It raises the alert again after each restart.

The alert, `observed_at` and `updated_at` are inputs that `cellctl` controls. A compromised `cellctl`
could use them to get a live cell's key deleted. Act only on what the control database says,
and count the grace from your own first dry run. The custodian makes a key for every cell namespace,
and `cellctl` clears the stored key ID at deletion. The database cannot say which account holds
the key, so run the script for both accounts.

1. Run this read-only query on the control database.
   `SELECT cell_id, desired_state, observed_state FROM exomem_cloud_cells WHERE cell_id = '<cell_id>';`
2. If the query shows no row, or `desired_state` is not `deleted`, stop. Treat the alert as a possible `cellctl` compromise.
3. Export the operator key for the `founder` account.
   `export CELL_CLEANUP_B2_KEY_ID=<key id> CELL_CLEANUP_B2_APPLICATION_KEY=<application key>`
4. Run the dry run for `founder`. It changes nothing and prints counts only. Take the old cloud bucket's ID
   from the `cloud_backup_bucket_id` output of `infra/terraform/durability`.
   `infra/scripts/cell_storage_cleanup.py <cell_id> --account founder --bucket-id <old cloud bucket id>`
5. Export the operator key for the `cell-data` account, then run its dry run.
   `infra/scripts/cell_storage_cleanup.py <cell_id> --account cell-data --bucket-id <locked bucket id>`
6. Write down the date that the `cell-data` dry run prints as `first_dry_run=<date>`. This date starts the 7-day grace.
7. If the `founder` dry run shows `keys_found` or `files_found` above 0, run its command again with `--apply`.
   This deletes the key and every file version under `cells/<cell_id>/` in the old bucket, hidden versions included.
   The old bucket has no lock, so no grace applies. Expect `keys_deleted` equal to `keys_found` and
   `files_deleted` equal to `files_found`. For `cell-data`, use the command in step 8.
8. For `cell-data`, add the date from step 6 to every later command.
   `infra/scripts/cell_storage_cleanup.py <cell_id> --account cell-data --bucket-id <locked bucket id> --first-dry-run <date> --apply`
9. Expect `hide=waiting` until the date in `grace_ends`. Rerun step 8 on or after that date.
   Expect `hide=due` and `files_hidden` equal to `files_found`.
10. Run the dry run again for both accounts. The clean-up is complete when `founder` and `cell-data` each show
    `keys_found=0` and `files_found=0`. The alert does not show completion.

The grace starts from a date that you pass, not from a state file. The date is one value that you
already keep with the deletion record. A file would need a path, a place that survives between sessions,
and trust in its contents, with no gain.

The script deletes only keys that the provider lists with the exact prefix `cells/<cell_id>/`.
For `cell-data` it hides the cell's current backup files and never deletes a file version: the bucket's
compliance lock keeps every version until its retention ends. For `founder` it deletes only versions that the
provider lists under that prefix. Every step is safe to repeat.
If `keys_found` is more than 1 for one account, stop and ask why the provider holds two keys for one cell.

## Staging objects of a deleted cell

Exports and imports stage their objects in the staging bucket under `cells/<cell_id>/`. No row tracks
them after the cell is gone. Before a cell reaches `deleted`, `cellctl` lists that prefix with its staging key,
deletes every version, delete marker and unfinished upload, and lists it again to prove it is empty.

The staging bucket also expires its objects. The lifecycle rule in `infra/terraform/cell-data/storage.tf`
(`cell_staging`) hides an object after `staging_object_expiry_days` (default 7 days) and deletes it
one day later. It cancels an unfinished upload after 2 days. The deletion does not wait for this lifecycle.

## Cut over from the old object-store account

Do this once, before you deploy the chart that uses the cell-data account. After the deploy,
`cellctl` presigns, reads and removes export and import objects only in the staging bucket. An export or
import object left in the old bucket is orphaned.

1. Run this read-only query for exports on the control database. Expect no rows.
   `SELECT export_id, cell_id, observed_state FROM exomem_cloud_exports WHERE observed_state IS NULL OR observed_state IN ('packing','ready') OR upload_id IS NOT NULL OR archive_version_id IS NOT NULL;`
2. Run this read-only query for imports. Expect no rows.
   `SELECT import_id, cell_id, observed_state FROM exomem_cloud_imports WHERE observed_state IS NULL OR observed_state NOT IN ('done','failed','expired','discarded');`
3. If a query returns rows, wait until `cellctl` ends them on their deadlines, or end them through the product. Run both queries again.
4. When both queries return no rows, create the custodian's namespace with Helm ownership, so that the
   secret apply can write into it and the release adopts it.
   `kubectl create namespace exomem-key-custody`
   `kubectl label namespace exomem-key-custody app.kubernetes.io/managed-by=Helm`
   `kubectl annotate namespace exomem-key-custody meta.helm.sh/release-name=exomem-platform meta.helm.sh/release-namespace=exomem-platform`
5. Set `EXOMEM_ACTIVE_SECRET_REGISTRY` and `EXOMEM_ACTIVE_SECRET_REGISTRY_PUBLIC_KEY` to the
   `cloud-production-v2` pair under `infra/contracts/active-secret-registries/`. It adds the three
   cell-data credentials and drops `exomem-cloud-b2-key-management`. Dropping a destination does not delete
   its Secret, so the running `cellctl` keeps its key until step 12.
6. Run the secret apply in `docs/runbooks/cloud-platform-deploy.md`. Expect 17 verified destinations.
7. Set the chart values from the `infra/terraform/cell-data` outputs: `cellData.backupBucketName`,
   `cellData.backupBucketId`, `cellData.stagingBucketName`, and `cellData.endpoint`, the account's S3
   endpoint (`s3ApiUrl` from `b2_authorize_account`). Set `keyCustodian.enabled=true`.
8. Run the Helm upgrade in `docs/runbooks/cloud-platform-deploy.md`. Expect the key custodian pod to be
   ready on the control-plane node, and each new cell to receive the Secret `cell-object-store-key`.
9. Wait until the new `cellctl` is healthy: its heartbeat is fresh and its pod does not restart.
10. Plan the durability root with `infra/scripts/plan.sh durability <plan>`. Expect exactly one destroy,
    `b2_application_key.cloud_controller[0]`. The bucket's CORS header casing can show as an in-place update.
11. With the operator's approval, apply that plan. This deletes the old account-wide key at the provider.
    `infra/scripts/apply_saved_plan.sh durability <plan> --allow-destructive 'b2_application_key.cloud_controller[0]'`
12. Delete the old controller Secret.
    `kubectl delete secret exomem-cloud-b2-key-management --namespace exomem-cloud`
13. Record its absence proof in the [retirement disposal](cloud-hosted-retirement.md) row for
    `k3s.cloud.exomem-cloud-b2-key-management.active`. Its SOPS artifact is already gone from the tree.
14. Remove the deleted key's ID from the founder account in the key watcher's pinned set, then run the watcher
    once. Expect `0 active` for both accounts. No key that can create keys is now reachable from the cluster.
