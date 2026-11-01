resource "random_id" "bucket_suffix" {
  byte_length = 4
}

locals {
  # Both buckets cancel an abandoned multipart upload after the same two days; restic and the browser import own live parts.
  unfinished_upload_days = 2
}

# The locked cell backup bucket (lock-cloud-backups-against-deletion D1-D3).
resource "b2_bucket" "cell_backups" {
  bucket_name = "${var.bucket_prefix}-cell-backup-${random_id.bucket_suffix.hex}"
  bucket_type = "allPrivate"

  default_server_side_encryption {
    mode      = "SSE-B2"
    algorithm = "AES256"
  }

  # Compliance mode: no key, the account owner's included, can shorten or remove a version's lock.
  file_lock_configuration {
    is_file_lock_enabled = true
    default_retention {
      mode = "compliance"
      period {
        duration = var.backup_retention_days
        unit     = "days"
      }
    }
  }

  # D3: only noncurrent (hidden or overwritten) versions expire, once their lock has lapsed.
  # days_from_uploading_to_hiding stays unset, so a current version never expires and live restic packs stay.
  lifecycle_rules {
    file_name_prefix                                       = ""
    days_from_hiding_to_deleting                           = 1
    days_from_starting_to_canceling_unfinished_large_files = local.unfinished_upload_days
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Export and import staging objects: short-lived, so no Object Lock (lock-cloud-backups-against-deletion D1).
resource "b2_bucket" "cell_staging" {
  bucket_name = "${var.bucket_prefix}-cell-staging-${random_id.bucket_suffix.hex}"
  bucket_type = "allPrivate"

  default_server_side_encryption {
    mode      = "SSE-B2"
    algorithm = "AES256"
  }

  file_lock_configuration {
    is_file_lock_enabled = false
  }

  lifecycle_rules {
    file_name_prefix                                       = ""
    days_from_uploading_to_hiding                          = var.staging_object_expiry_days
    days_from_hiding_to_deleting                           = 1
    days_from_starting_to_canceling_unfinished_large_files = local.unfinished_upload_days
  }

  dynamic "cors_rules" {
    for_each = length(var.staging_import_allowed_origins) == 0 ? [] : [var.staging_import_allowed_origins]
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
