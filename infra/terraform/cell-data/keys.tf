# The key custodian is the only key that creates object-store keys (contain-cloud-controller-compromise D9).
# The provider allows key creation only on an account-level key, so it cannot be limited to a bucket.
resource "b2_application_key" "key_custodian" {
  key_name = "exomem-cell-data-key-custodian"
  # B2 capability names are protocol tokens.
  capabilities = ["listKeys", "writeKeys", "deleteKeys"]
}

# cellctl presigns and removes export and import objects; it cannot create keys or reach a backup.
resource "b2_application_key" "cellctl_staging" {
  key_name     = "exomem-cell-data-cellctl-staging"
  bucket_ids   = [b2_bucket.cell_staging.bucket_id]
  capabilities = ["listBuckets", "listFiles", "readFiles", "writeFiles", "deleteFiles"]
}

# cellctl lists backup versions for bookkeeping only. listBuckets stays because S3-compatible
# tools need it even on a restricted key (infra/cellctl/src/cellctl/storage/interface.py).
resource "b2_application_key" "cellctl_backup_list" {
  key_name     = "exomem-cell-data-cellctl-backup-list"
  bucket_ids   = [b2_bucket.cell_backups.bucket_id]
  capabilities = ["listBuckets", "listFiles"]
}

# The out-of-cluster key watcher lists each deleted cell's backup prefix with this key
# (automate-cell-storage-deletion D4). No in-cluster component holds it, so a compromised cluster
# cannot blind the watcher's view of a purge.
resource "b2_application_key" "key_watcher_backup_list" {
  key_name   = "exomem-cell-data-key-watcher-backup-list"
  bucket_ids = [b2_bucket.cell_backups.bucket_id]
  # B2 capability names are protocol tokens: listFiles lists versions, start entries and hide markers.
  capabilities = ["listFiles"]
}
