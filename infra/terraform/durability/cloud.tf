resource "b2_bucket" "cloud_backups" {
  count       = var.cloud_backup_enabled ? 1 : 0
  bucket_name = "${var.bucket_prefix}-cloud-${random_id.bucket_suffix.hex}"
  bucket_type = "allPrivate"

  default_server_side_encryption {
    mode      = "SSE-B2"
    algorithm = "AES256"
  }

  file_lock_configuration {
    is_file_lock_enabled = false
  }

  # D3 cancels abandoned uploads under cells/ after two days; restic owns live packs.
  lifecycle_rules {
    file_name_prefix                                       = "cells/"
    days_from_starting_to_canceling_unfinished_large_files = 2
  }

  dynamic "cors_rules" {
    for_each = length(var.cloud_import_allowed_origins) == 0 ? [] : [var.cloud_import_allowed_origins]
    content {
      cors_rule_name  = "browser-vault-import"
      allowed_origins = cors_rules.value
      # B2 fixes the S3 operation token and the upload response header.
      allowed_operations = ["s3_put"]
      expose_headers     = ["ETag"]
      # The signed URL authorizes the PUT; browsers can send upload headers.
      allowed_headers = ["*"]
      # Check the configured origin on each preflight after a policy change.
      max_age_seconds = 0
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Cells created before the cell-data account keep their backups in this bucket. Deleting one removes
# its object versions with this key. It is limited to this bucket and cannot create, list or delete
# keys: the account-wide key that minted every tenant key is gone, and key creation now belongs to
# the key custodian in the cell-data account.
resource "b2_application_key" "cloud_legacy_delete" {
  count      = var.cloud_backup_enabled ? 1 : 0
  key_name   = "exomem-cloud-legacy-delete"
  bucket_ids = [b2_bucket.cloud_backups[0].bucket_id]
  # B2 capability names are protocol tokens. listBuckets lets an S3 or native client resolve the bucket.
  capabilities = ["listBuckets", "listFiles", "deleteFiles"]
}
