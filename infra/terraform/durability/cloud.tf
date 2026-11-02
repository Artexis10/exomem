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
