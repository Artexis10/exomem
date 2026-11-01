<!-- authority:non-specification -->

# Exomem Cloud key custody

Each cell reads and writes its backups with one object-store key. The key custodian creates
that key. `cellctl` holds no credential that creates or deletes keys.

## Credentials

Terraform creates the first four credentials. Their sources are `infra/terraform/cell-data/keys.tf`
and `infra/terraform/durability/cloud.tf`. Each credential has one holder.

| Credential | Account | Restricted to | Capabilities | Held by |
| --- | --- | --- | --- | --- |
| `exomem-cell-data-key-custodian` | cell-data | None. B2 allows key creation only on an account-level key. | `listKeys`, `writeKeys`, `deleteKeys` | The key custodian, in Secret `exomem-key-custodian-b2` |
| `exomem-cell-data-cellctl-staging` | cell-data | The staging bucket | `listBuckets`, `listFiles`, `readFiles`, `writeFiles`, `deleteFiles` | `cellctl`, for exports, imports and the staging sweep of a deletion |
| `exomem-cell-data-cellctl-backup-list` | cell-data | The locked backup bucket | `listBuckets`, `listFiles` | `cellctl`, to count backup versions for the log |
| `exomem-cloud-legacy-delete` | founder | The old cloud backups bucket | `listBuckets`, `listFiles`, `deleteFiles` | `cellctl`, to empty an old cell's prefix at deletion |
| One key per cell | cell-data | The locked backup bucket and the prefix `cells/<cell_id>/` | `listBuckets`, `listFiles`, `readFiles`, `writeFiles`, `deleteFiles` | The cell's backup and restore Jobs, in Secret `cell-object-store-key` |
| Operator key | One account per key | None | `listKeys`, `deleteKeys`; for cell-data also `listFiles`, `writeFiles` | The operator, for the clean-up script only |

The key custodian creates each per-cell key. `cellctl` cannot create that Secret.
Until D3 is in force, `cellctl` can render a Job that reads the Secret, so it can reach a cell's key by that route.
No other credential can create or delete a key.

## Clean up after a cell deletion

`cellctl` cannot delete the deleted cell's key. When a cell reaches `deleted`, `cellctl` raises the
platform alert `cell-storage-cleanup-pending-<cell_id>`. It raises the alert again after each restart.

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
4. Run the dry run for `founder`. It changes nothing and prints counts only.
   `infra/scripts/cell_storage_cleanup.py <cell_id> --account founder`
5. Export the operator key for the `cell-data` account, then run its dry run.
   `infra/scripts/cell_storage_cleanup.py <cell_id> --account cell-data --bucket-id <locked bucket id>`
6. Write down the date that the `cell-data` dry run prints as `first_dry_run=<date>`. This date starts the 7-day grace.
7. If a dry run shows `keys_found` above 0, run the same command with `--apply`. Expect `keys_deleted` equal to `keys_found`.
8. For `cell-data`, add the date from step 6 to every later command.
   `infra/scripts/cell_storage_cleanup.py <cell_id> --account cell-data --bucket-id <locked bucket id> --first-dry-run <date> --apply`
9. Expect `hide=waiting` until the date in `grace_ends`. Rerun step 8 on or after that date.
   Expect `hide=due` and `files_hidden` equal to `files_found`.
10. Run the dry run again for both accounts. The clean-up is complete when `founder` shows `keys_found=0`
    and `cell-data` shows `keys_found=0` and `files_found=0`. The alert does not show completion.

The grace starts from a date that you pass, not from a state file. The date is one value that you
already keep with the deletion record. A file would need a path, a place that survives between sessions,
and trust in its contents, with no gain.

The script deletes only keys that the provider lists with the exact prefix `cells/<cell_id>/`.
It hides the cell's current backup files and never deletes a file version: the bucket's
compliance lock keeps every version until its retention ends. Every step is safe to repeat.
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
`cellctl` reads and removes export and import objects only in the staging bucket. It cannot reach
an object in the old bucket.

1. Run this read-only query for exports on the control database. Expect no rows.
   `SELECT export_id, cell_id, observed_state FROM exomem_cloud_exports WHERE observed_state IS NULL OR observed_state IN ('packing','ready') OR upload_id IS NOT NULL OR archive_version_id IS NOT NULL;`
2. Run this read-only query for imports. Expect no rows.
   `SELECT import_id, cell_id, observed_state FROM exomem_cloud_imports WHERE observed_state IS NULL OR observed_state NOT IN ('done','failed','expired','discarded');`
3. If a query returns rows, wait until `cellctl` ends them on their deadlines, or end them through the product. Run both queries again.
4. Deploy the chart when both queries return no rows.
