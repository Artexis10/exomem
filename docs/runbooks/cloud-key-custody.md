<!-- authority:non-specification -->

# Exomem Cloud key custody

Each cell reads and writes its backups with one object-store key. The key custodian creates
that key. `cellctl` holds no credential that creates or deletes keys. In the founder account it holds no credential that
reads, writes or deletes files; its one key there can only list the media bucket.
It still holds the stored per-cell key of each cell created before the switch, limited to that cell's prefix, until the cell migrates.

## Credentials

Terraform creates the credentials below except the per-cell keys, the purge keys and the operator keys. The cell-data
keys come from `infra/terraform/cell-data/keys.tf`. The four media keys come from `infra/terraform/durability/media.tf`.
No media key can delete a file version. A captured edge key can still hide a name, and the media bucket's
lifecycle rule deletes a hidden version when the grace period ends. The key watcher's hide budget and the
grace period bound that exposure: see "Triage a burst of hides" in `cloud-media-edge.md`.
Each credential has one holder.

| Credential | Account | Restricted to | Capabilities | Held by |
| --- | --- | --- | --- | --- |
| `exomem-cell-data-key-custodian` | cell-data | None. B2 allows key creation only on an account-level key. | `listKeys`, `writeKeys`, `deleteKeys` | The key custodian, in Secret `exomem-key-custodian-b2` |
| `exomem-cell-data-cellctl-staging` | cell-data | The staging bucket | `listBuckets`, `listFiles`, `readFiles`, `writeFiles`, `deleteFiles` | `cellctl`, for exports, imports and the staging sweep of a deletion |
| `exomem-cell-data-cellctl-backup-list` | cell-data | The locked backup bucket | `listBuckets`, `listFiles` | `cellctl`, to count backup versions for the log and to relay deletion grants |
| `exomem-cell-data-key-watcher-backup-list` | cell-data | The locked backup bucket | `listFiles` | The key watcher, to list each deleted cell's backup prefix, in secrets `B2_WATCHER_BACKUP_LIST_KEY_ID` and `B2_WATCHER_BACKUP_LIST_KEY` of environment `key-watcher` |
| `<prefix>-media-edge` | founder | The media bucket | `readFiles`, `writeFiles` | The media edge, in Secret `exomem-media-edge-b2` |
| `<prefix>-media-edge-list` | founder | The media bucket | `listFiles` | The media edge, to find a deleted cell's names for its purge, in Secret `exomem-media-edge-list` |
| `<prefix>-cellctl-media-list` | founder | The media bucket | `listBuckets`, `listFiles` | `cellctl`, to meter each cell's stored bytes and to relay deletion grants. Source: `infra/terraform/durability/media.tf` |
| `<prefix>-key-watcher-media-list` | founder | The media bucket | `listFiles` | The key watcher, to count hide markers, in secrets `B2_WATCHER_MEDIA_KEY_ID` and `B2_WATCHER_MEDIA_KEY` of environment `key-watcher` |
| One key per cell | cell-data | The locked backup bucket and the prefix `cells/<cell_id>/` | `listBuckets`, `listFiles`, `readFiles`, `writeFiles`, `deleteFiles` | The cell's backup and restore Jobs, in Secret `cell-object-store-key` |
| One purge key per deleted cell | cell-data | The locked backup bucket and the prefix `cells/<cell_id>/` | `listFiles`, `writeFiles` | The key custodian, for one pass of at most one hour. It deletes the key after the pass. |
| Operator key | founder | None. B2 allows key deletion only on an account-level key. | `listKeys`, `deleteKeys`, `listBuckets`, `listFiles`, `deleteFiles` | The operator, for the old bucket's clean-up only |
| Operator rescue key | cell-data or founder | One bucket: the locked backup bucket or the media bucket | `listFiles`, `deleteFiles` | The operator, for `--rescue` only. Create it for the rescue and delete it after. |

The key custodian creates each per-cell key and each purge key. `cellctl` cannot create that Secret.
Until D3 is in force, `cellctl` can render a Job that reads the Secret, so it can reach a cell's key by that route.
No other credential can create or delete a key.

## Pin the media keys

The key watcher reports a key or a bucket setting that its pinned set does not list. The set is the GitHub
Actions secret `KEY_WATCHER_PINNED_SET` in environment `key-watcher`. GitHub never returns a secret's value,
so the current document is the copy that you keep from the last time you wrote it. The set is live state.
Never replace it from a template; read the current value, change it and write it back.

1. Apply the durability root with the operator's approval. Expect one new key, `cellctl_media_list`, and
   the outputs `cloud_media_list_application_key_id` and `cloud_media_edge_application_key_id`.
2. Open your copy of the current pinned set. In the `founder` account, add both key IDs to `pinned_key_ids`.
   Skip an ID that the list already holds.
3. In that account's `buckets`, add the media bucket under its ID from `cloud_media_bucket_id`, if it is
   missing. Copy each setting from the provider's `b2_list_buckets` response for that bucket.
   The fields are `bucketType`, `fileLockEnabled`, `defaultRetention`, `lifecycleRules`,
   `defaultServerSideEncryption`, `replicationConfiguration` and `corsRules`.
4. Write the value back: `gh secret set KEY_WATCHER_PINNED_SET --env key-watcher < <your file>`
5. Run the watcher once: `gh workflow run b2-key-watcher.yml`. Expect the run to end with `0 active` for both accounts.

Until step 4, the watcher reports the new key as unpinned. The alert is expected for that window.
A bucket setting that differs from the copy shows as a reason token that names the field and no value.

## Pin the media watch

Do this after the durability apply of "Turn on collection" in `cloud-media-edge.md`. That apply adds the
watcher's media key and the grace period to the media bucket's lifecycle rule. Agree the change with the
owner of `contain-cloud-controller-compromise` first, because that change owns the pinned set.

1. Open your copy of the current pinned set.
2. In the `founder` account, add the ID from `key_watcher_media_list_application_key_id` to `pinned_key_ids`.
3. In that account's `buckets`, replace the media bucket's `lifecycleRules` with the provider's current value
   from `b2_list_buckets`. Expect `daysFromHidingToDeleting` equal to the grace period.
4. Add the media watch to the `founder` account, with the budget and window that you chose:
   `"media": {"bucket_id": "<media bucket id>", "key_id_env": "B2_WATCHER_MEDIA_KEY_ID", "key_env": "B2_WATCHER_MEDIA_KEY", "hide_budget": <budget>, "window_hours": <hours>}`
5. Write the value back: `gh secret set KEY_WATCHER_PINNED_SET --env key-watcher < <your file>`
6. Run the watcher once: `gh workflow run b2-key-watcher.yml`.
   Expect `0 active` for both accounts and a line `account founder: <n> media hides in the window`.

`window_hours` is the period that `hide_budget` covers. Keep it at 2 or more, because a scheduled run can
start late. Until step 5, the watcher reports the new key as unpinned and the lifecycle rule as drift.
A pinned set without `media` still parses, and its watcher counts no hides.

## Pin the deletion watch

The deletion watch lets the key watcher report each deleted cell's purge (`automate-cell-storage-deletion` D4).
Do this before the purge is turned on, after "Pin the media watch". Agree the change with the owner of
`contain-cloud-controller-compromise` first, because that change owns the pinned set.

1. Run steps 2 to 6 with the HCP Terraform credentials, as [Cloud Terraform backend](cloud-terraform-backend.md) says.
   Map the BWS entries `EXOMEM_CELL_DATA_B2_MASTER_KEY_ID` and `EXOMEM_CELL_DATA_B2_MASTER_KEY` to `TF_VAR_cell_data_b2_application_key_id` and `TF_VAR_cell_data_b2_application_key`, through `bwsx-run`.
   For step 3, also map the durability root's bootstrap key to `TF_VAR_b2_application_key_id` and `TF_VAR_b2_application_key`.
2. Plan the cell-data root: `infra/scripts/plan.sh cell-data <plan file>`
   Expect one add, `key_watcher_backup_list`, and no other change.
3. If the platform has the media watch, plan the durability root for the one key only:
   `infra/scripts/plan.sh durability <plan file> -target='b2_application_key.cloud_media_edge_list[0]'`
   Expect one add. A full durability plan can hold other changes.
4. With the operator's approval, apply each saved plan: `infra/scripts/apply_saved_plan.sh <root> <plan file>`
5. Store the watcher's backup list key ID in BWS prod:
   `terraform -chdir=infra/terraform/cell-data output -raw key_watcher_backup_list_application_key_id | bwsx-secret set --project prod EXOMEM_KEY_WATCHER_BACKUP_LIST_B2_KEY_ID --format opaque-line`
6. Store the key the same way: output `key_watcher_backup_list_application_key`, BWS name `EXOMEM_KEY_WATCHER_BACKUP_LIST_B2_KEY`.
7. Pipe each value from BWS into its GitHub secret:
   `bwsx-run EXOMEM_KEY_WATCHER_BACKUP_LIST_B2_KEY_ID -- sh -c 'printf "%s" "$EXOMEM_KEY_WATCHER_BACKUP_LIST_B2_KEY_ID"' | gh secret set B2_WATCHER_BACKUP_LIST_KEY_ID --env key-watcher`
   Do the same for `EXOMEM_KEY_WATCHER_BACKUP_LIST_B2_KEY` into `B2_WATCHER_BACKUP_LIST_KEY`.
8. Set the address of Substrate's deletion set. It has the same capability as `KEY_WATCHER_ALERT_URL`:
   `bwsx-run EXOMEM_KEY_WATCHER_ALERT_URL -- sh -c 'printf "%s" "$EXOMEM_KEY_WATCHER_ALERT_URL"' | sed 's#/api/exomem/alerts/#/api/exomem/cell-deletions/#' | gh secret set KEY_WATCHER_DELETIONS_URL --env key-watcher`
9. Open your copy of the current pinned set.
10. In the `cell-data` account, add the ID from `key_watcher_backup_list_application_key_id` to `pinned_key_ids`.
11. If you planned step 3, add the ID from `cloud_media_edge_list_application_key_id` to `pinned_key_ids` in the `founder` account.
12. Add the deletion watch at the top level, with the skew margin that you chose (the design proposes 60 minutes):
   `"deletions": {"account": "cell-data", "media_account": "founder", "backup_key_id_env": "B2_WATCHER_BACKUP_LIST_KEY_ID", "backup_key_env": "B2_WATCHER_BACKUP_LIST_KEY", "purge_capabilities": ["listFiles", "writeFiles"], "skew_margin_minutes": <minutes>}`
   On a platform without the media watch, set `media_account` to `null`.
13. Parse the file with the watcher before you write it. Run this in `infra/scripts`:
   `python3 -c 'import sys, b2_key_watcher as w; w.parse_pinned_set(open(sys.argv[1]).read())' <your file>`
   Expect no output.
14. Write the value back: `gh secret set KEY_WATCHER_PINNED_SET --env key-watcher < <your file>`
15. Keep the written file as your current copy, and the copy before it for undo.
   Keep both private, outside any scratch folder.
16. Run the watcher once: `gh workflow run b2-key-watcher.yml`.
   Expect `0 active` for both accounts and a line `deletions: <n> in the set, <w> watched: ...`.

The watcher reads the backup retention from the backup bucket's pinned `defaultRetention`, and the media
grace from the media bucket's pinned lifecycle rule. It refuses a `purge_capabilities` equal to the per-cell
shape. Until step 14, the watcher reports the new keys as unpinned. Until Substrate serves the deletion set,
expect `deletions: unknown (DELETIONS_UNKNOWN)` and that alert. A pinned set without `deletions` still
parses, and its watcher watches no deletion.

## Clean up after a cell deletion

The key custodian and the media edge purge every deleted cell's prefixes in the locked backup bucket and the
media bucket, on the cell's deletion grant. The custodian also deletes the cell's key in the cell-data account.
See "Purge a deleted cell's storage" in `cloud-cell-deletion.md`. The script has no apply mode for those buckets.

This section covers only the old founder bucket. A cell created before the key custodian keeps its backups
there, which has no lock and no executor. `cellctl` holds that cell's stored key until the cell migrates, but it
deletes neither the key nor the files. When such a cell reaches `deleted`, `cellctl` raises the platform alert
`cell-storage-cleanup-pending-<cell_id>`. It raises the alert again after each restart. The row keeps the cell's
B2 key ID as the mark of that clean-up.

The alert, `observed_at` and `updated_at` are inputs that `cellctl` controls. A compromised `cellctl`
could use them to get a live cell's key deleted. Act only on what the control database says.

1. Run this read-only query on the control database.
   `SELECT cell_id, desired_state, observed_state FROM exomem_cloud_cells WHERE cell_id = '<cell_id>';`
2. If the query shows no row, or `desired_state` is not `deleted`, stop. Treat the alert as a possible `cellctl` compromise.
3. Export the operator key for the `founder` account.
   `export CELL_CLEANUP_B2_KEY_ID=<key id> CELL_CLEANUP_B2_APPLICATION_KEY=<application key>`
4. Run the dry run for `founder`. It changes nothing and prints counts only. Take the old cloud bucket's ID
   from the `cloud_backup_bucket_id` output of `infra/terraform/durability`.
   Check the corresponding `cloud_backup_bucket_name` output to confirm the intended bucket; pass its ID to `--bucket-id`.
   `infra/scripts/cell_storage_cleanup.py <cell_id> --account founder --bucket-id <old cloud bucket id>`
5. If the dry run shows `keys_found` or `files_found` above 0, run its command again with `--apply`.
   This deletes the key and every file version under `cells/<cell_id>/` in the old bucket, hidden versions included.
   The old bucket has no lock, so no grace applies. Expect `keys_deleted` equal to `keys_found` and
   `files_deleted` equal to `files_found`.
6. Run the dry run again. The clean-up is complete when it shows `keys_found=0` and `files_found=0`.
   The alert does not show completion.

The script deletes only keys that the provider lists with the exact prefix `cells/<cell_id>/`, and only
versions that the provider lists under that prefix. Every step is safe to repeat.
The listing includes unfinished large files, and the script sends the same
[delete-file-version call](https://www.backblaze.com/apidocs/b2-delete-file-version) for each of them.
This runbook does not rely on that call to cancel an unfinished upload, because that provider behaviour
is unverified here. The final dry run counts unfinished files in `files_found`, so it shows any that remain.
If `keys_found` is more than 1, stop and ask why the provider holds two keys for one cell.

## Media bucket of a deleted cell

The media bucket stays in the founder account. Its only writer is the media edge, and no service holds
`deleteFiles` on it. `cellctl` reports a cell `deleted` only after the edge shows a set without the cell
(`add-cloud-media-store` D6, D9). After the cell's `purge_after`, the edge's purge pass hides each current name
under `cells/<cell_id>/`. The bucket's lifecycle rule deletes each hidden version when the media grace ends,
and cancels each unfinished large file. The key watcher reports each step with `CELL_DELETION_PENDING`,
`CELL_DELETION_HIDDEN` and `CELL_DELETION_ANOMALY`. Run no apply mode here: the script has none for this bucket.

Within the media grace, you can rescue a wrong deletion. Add the cell to the hold list `cellDeletion.holdCells`
first, then run the script with `--rescue`. A rescued file is ciphertext. "Hold a cell", "Release a hold" and
"Rescue hidden files" in `cloud-cell-deletion.md` give the steps, with the key and the dry run below.

1. Export an operator key for the founder account that is limited to the media bucket, with `listFiles` and
   `deleteFiles`. Deleting a hide marker deletes a file version. It needs no key capability.
   `export CELL_CLEANUP_B2_KEY_ID=<key id> CELL_CLEANUP_B2_APPLICATION_KEY=<application key>`
2. Run the dry run. Take the media bucket's ID from the `cloud_media_bucket_id` output of `infra/terraform/durability`; check `cloud_media_bucket_name` for the intended bucket.
   `infra/scripts/cell_storage_cleanup.py <cell_id> --account media --bucket-id <media bucket id>`
   Expect `uploads_found`, `hides_found` and `unfinished_found`. It changes nothing.

After the media grace, the dry run shows `uploads_found=0` and `unfinished_found=0`. If a current name remains after `purge_after` plus 1 day, the watcher
raises `CELL_DELETION_ANOMALY:<cell_id>`: follow "Triage an anomaly" in `cloud-cell-deletion.md`.
Delete the operator key when you finish.

## Staging objects of a deleted cell

Exports and imports stage their objects in the staging bucket under `cells/<cell_id>/`. No row tracks
them after the cell is gone. Before a cell reaches `deleted`, `cellctl` lists that prefix with its staging key,
deletes every version, delete marker and unfinished upload, and lists it again to prove it is empty.

The staging bucket also expires its objects. The lifecycle rule in `infra/terraform/cell-data/storage.tf`
(`cell_staging`) hides an object after `staging_object_expiry_days` (default 7 days) and deletes it
one day later. It cancels an unfinished upload after 2 days. The deletion does not wait for this lifecycle.

## Cut over from the old object-store account

Do this once. It moves object-store key creation from `cellctl` to the key custodian. It takes two Helm
releases, then one later step that cannot be undone: "Destroy the old controller key".

After release A, `cellctl` presigns, reads and removes export and import objects only in the staging bucket.
An export or import object left in the old bucket is orphaned.

### Choose the releases

The key custodian is in release 0.157.0 and later. The cell-deletion purge is in release 0.171.0 and later.
Take the `cellctl.image` digest from the cellctl attestation asset of the release that you choose.
The asset is named `exomem-image-cellctl-<commit>-<digest>-<run>-<attempt>.sigstore.json`. Check the digest with
`gh attestation verify oci://ghcr.io/artexis10/exomem-cellctl@sha256:<digest> --owner artexis10 --source-ref refs/tags/v<version>`.
Use that one digest in both releases, because the key custodian runs the `cellctl` image.

| Release | Content | `keyCustodian.enabled` |
| --- | --- | --- |
| A | The chart at the chosen release, the new `cellctl.image` digest, `cellData` and the cell-data Secrets | `false` |
| B | Release A, plus the key custodian and, to turn on the purge, `cellDeletion.grantPublicKeys` | `true` |

Release A starts the backup key conversion: see "Cross the backup key conversion" in
[Cloud platform deploy](cloud-platform-deploy.md#cross-the-backup-key-conversion). A failed wait in one combined release
would roll `cellctl` back after the conversion. In release B, only key custodian objects can roll back.
The chart keeps the key custodian namespace.

A cell that is created between the two releases has no object-store key, so its backup Jobs cannot start until
the key custodian's first sweep. This repository has no switch that pauses sign-ups, because Substrate admits
each new cell. Run release B right after release A.

### Prepare release A

1. Set `EXOMEM_PLATFORM_VALUES` and `EXOMEM_UPGRADE_DIR`, as "Review the release" in [Cloud platform deploy](cloud-platform-deploy.md#render-and-upgrade-the-release) says.
2. Run this read-only query for exports on the control database. Expect no rows.
   `SELECT export_id, cell_id, observed_state FROM exomem_cloud_exports WHERE observed_state IS NULL OR observed_state IN ('packing','ready') OR upload_id IS NOT NULL OR archive_version_id IS NOT NULL;`
3. Run this read-only query for imports. Expect no rows.
   `SELECT import_id, cell_id, observed_state FROM exomem_cloud_imports WHERE observed_state IS NULL OR observed_state NOT IN ('done','failed','expired','discarded');`
4. If a query returns rows, wait until `cellctl` ends them on their deadlines, or end them through the product. Run both queries again.
5. Run the key watcher once. Expect `0 active` for both accounts. If it reports a key, resolve that first.
   `gh workflow run b2-key-watcher.yml`
6. Create the key custodian's namespace, so that the secret apply can write into it and release B adopts it.
   `kubectl create namespace exomem-key-custody`
7. Label the namespace as owned by Helm.
   `kubectl label namespace exomem-key-custody app.kubernetes.io/managed-by=Helm`
8. Annotate the namespace with the release name and the release namespace.
   `kubectl annotate namespace exomem-key-custody meta.helm.sh/release-name=exomem-platform meta.helm.sh/release-namespace=exomem-platform`
9. Set `EXOMEM_ACTIVE_SECRET_REGISTRY` and `EXOMEM_ACTIVE_SECRET_REGISTRY_PUBLIC_KEY` to the
   `cloud-production-v2` pair under `infra/contracts/active-secret-registries/`.
   The pair adds the three cell-data credentials and drops `exomem-cloud-b2-key-management`.
   Dropping a destination does not delete its Secret. The old controller Secret stays until "Destroy the old controller key".
10. Run the secret apply in [Cloud platform deploy](cloud-platform-deploy.md). Expect 17 verified destinations.
11. List the two Secrets of `cellctl`. A missing Secret keeps `cellctl` down until the wait times out.
    `kubectl --namespace exomem-cloud get secret exomem-cloud-cell-data-staging exomem-cloud-cell-data-backup-list`
12. List the Secret of the key custodian. Release B needs it.
    `kubectl --namespace exomem-key-custody get secret exomem-key-custodian-b2`
13. Save the live values of the release as a reference. They are private: keep them out of tickets and chat.
    `helm get values exomem-platform --namespace exomem-platform > "$EXOMEM_UPGRADE_DIR/live-values.yaml"`
14. Remove `cellctl.b2AccountId` and `cellctl.b2KeyManagementSecretName` from the private values file. The chart schema refuses both keys.
15. Set `cellData.backupBucketName`, `cellData.backupBucketId` and `cellData.stagingBucketName` from the `infra/terraform/cell-data` outputs.
    The staging bucket must differ from the backup bucket.
16. Set `cellData.endpoint` to the account's S3 endpoint, the value `s3ApiUrl` in the `b2_authorize_account` response.
17. Set `cellctl.image` to `ghcr.io/artexis10/exomem-cellctl@sha256:<digest>`, the digest from "Choose the releases". The schema refuses a tag while `cellctl` is on.
18. Keep `cellctl.b2BucketName`, `cellctl.b2BucketId` and `cellctl.b2Endpoint`. A cell made before the switch still backs up to the old bucket with its stored key.
19. If the file sets `cellStorage.local.maxCellGib`, set a whole number, never `null`. Helm deletes a `null` key, and the schema then refuses the render.
    If `cellStorage.maxCellGib` is also set, use the same number, or the render fails with `must agree`.
20. Set `keyCustodian.enabled` to `false`.

Expected result: the private values file holds the keys above. Its render has no `key-custod` object.
`helm template exomem-platform infra/helm/platform --namespace exomem-platform --no-hooks --values "$EXOMEM_PLATFORM_VALUES" | grep -c key-custod`
prints `0`.

### Run release A

1. Do "Review the release" in [Cloud platform deploy](cloud-platform-deploy.md#render-and-upgrade-the-release). Expect no error, and no object of the key custodian in the diff.
2. Do "Before the upgrade" under "Cross the backup key conversion" in the same runbook.
3. Run the queries of steps 2 and 3 of "Prepare release A" again. Expect no rows.
4. Do steps 1 and 3 of "Upgrade the release" in the same runbook. Step 3 has no `--atomic`.
5. If the upgrade fails, do "If the upgrade fails" in the same runbook.
6. After a rollback, stop. Do not run release B.
7. After a rollback, find the cause of the failure and start again at step 1.
8. Wait until `cellctl` is Ready. Its liveness probe restarts the pod when its heartbeat file is older than 120 seconds.
   `kubectl --namespace exomem-cloud get pods --selector app.kubernetes.io/name=cellctl`
9. Wait until each cell pod is replaced once. Allow up to 10 minutes for each cell.
   `kubectl get pods --all-namespaces --selector app.kubernetes.io/name=exomem-cell`
10. Note the time when the last cell pod is Ready, as `date -u +%FT%TZ` prints it. It is the rollout end.
11. Do "Verify" in the same runbook.
12. Read the `cellctl` pod again. If `RESTARTS` is above 0, find the cause before release B.
13. Compare the converted cells with all cells, for each desired state.
   `psql "$EXOMEM_CELLCTL_DSN" --command "SELECT desired_state, count(*) AS cells, count(cell_root_key_wrapped) AS converted FROM exomem_cloud_cells GROUP BY desired_state ORDER BY desired_state"`

Expected result: the `cellctl` pod is Ready and has 0 restarts. Every cell pod is Ready and was replaced once.
In the query of step 13, `converted` equals `cells` for each `running` and `read_only` row, and it is 0 for `deleted`.
A `stopped` cell converts when it next starts. The temporary copy of the key columns stays until release B ends.

### Run release B

1. If you turn on the purge, do steps 1 to 5 of "Turn on the purge" in [Cloud cell deletion](cloud-cell-deletion.md#turn-on-the-purge). Skip its step 6: step 4 below is that upgrade.
2. Set `keyCustodian.enabled` to `true` in the private values file.
3. Do "Review the release" in [Cloud platform deploy](cloud-platform-deploy.md#render-and-upgrade-the-release). Expect the diff to add only objects of the key custodian. If it changes `cellctl`, stop.
4. Do steps 1 and 2 of "Upgrade the release" in the same runbook. Step 2 has `--atomic`. Then do "Verify".
5. If the purge is on and the platform has the media edge, do step 7 of "Turn on the purge".
6. List the pod of the key custodian. Expect `key-custodian` Ready on the control-plane node.
   `kubectl --namespace exomem-key-custody get pods --output wide`
7. Read its log. Expect `key custodian:` lines, and no line with `not served`, `not read`, `cannot be read` or `sweep failed`.
   `kubectl --namespace exomem-key-custody logs deployment/key-custodian`
8. Count the cell namespaces.
   `kubectl get namespaces --output name | grep -c '^namespace/exo-cell-'`
9. Count the cell key Secrets.
   `kubectl get secrets --all-namespaces --field-selector metadata.name=cell-object-store-key --output name | wc -l`
10. Compare the two counts. Expect them equal. A deleting cell, or a cell that a deletion grant names, has no key.
11. Run the key watcher once. Expect `0 active` for both accounts.
    `gh workflow run b2-key-watcher.yml`
12. Do "After the backup key conversion" in [Cloud platform deploy](cloud-platform-deploy.md#after-the-backup-key-conversion). Use the rollout end of release A.

Expected result: the key custodian runs, and each live cell has the Secret `cell-object-store-key`.
The temporary copy of the key columns is deleted.

The key custodian also makes a key for each cell that was created before the switch. Such a cell ignores that key
and keeps its stored key until it migrates. The key watcher counts each such key as a per-cell key.

If the wait of release B fails, `--atomic` rolls the release back to release A. It removes the key custodian objects
and keeps the key custodian namespace. `cellctl` does not change.

## Destroy the old controller key

**Warning:** this section destroys the old account-wide controller key at the provider. The provider restores no
deleted key. The Terraform source of that key, `cloud_controller`, is no longer in `infra/terraform`, so no apply
can create it again. A `cellctl` from before release A reads that key as the non-optional `keyId` and
`applicationKey` of the old controller Secret. After step 3, that `cellctl` does not start, so a rollback to a
release before release A fails. Fix forward only.

Do this section only when all of these are true:

- Release B runs and the key custodian is Ready.
- The `cellctl` pod has 0 restarts over a soak that the operator chose.
- The key watcher reports `0 active` for both accounts.
- The operator approves the destroy.

1. Plan the durability root. Expect exactly one destroy, `b2_application_key.cloud_controller[0]`. If the plan holds another destroy, stop.
   `infra/scripts/plan.sh durability <plan>`
   The bucket's CORS header casing can show as an in-place update.
2. Apply that plan. This deletes the old account-wide key at the provider. Expect the apply to end without an error.
   `infra/scripts/apply_saved_plan.sh durability <plan> --allow-destructive 'b2_application_key.cloud_controller[0]'`
3. Delete the old controller Secret.
   `kubectl delete secret exomem-cloud-b2-key-management --namespace exomem-cloud`
4. Record its absence proof in the [retirement disposal](cloud-hosted-retirement.md) row for
   `k3s.cloud.exomem-cloud-b2-key-management.active`. Its SOPS artifact is already gone from the tree.
5. Open your copy of the current pinned set. Never write it from a template: it is live state.
6. In the `founder` account, remove the deleted key's ID from `pinned_key_ids`.
7. Write the pinned set back.
   `gh secret set KEY_WATCHER_PINNED_SET --env key-watcher < <your file>`
8. Run the key watcher once. Expect `0 active` for both accounts.
   `gh workflow run b2-key-watcher.yml`

Until step 7, the watcher reports the pinned key as missing. The alert is expected for that window.

Expected result: the key watcher reports `0 active` for both accounts. Only the key custodian holds a credential that can
create keys: see "Credentials".
