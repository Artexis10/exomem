resource "b2_bucket" "cloud_media" {
  count       = var.cloud_media_enabled ? 1 : 0
  bucket_name = "${var.bucket_prefix}-media-${random_id.bucket_suffix.hex}"
  bucket_type = "allPrivate"

  default_server_side_encryption {
    mode      = "SSE-B2"
    algorithm = "AES256"
  }

  file_lock_configuration {
    is_file_lock_enabled = false
  }

  # D7 keeps every completed version; only abandoned multipart uploads expire.
  lifecycle_rules {
    file_name_prefix                                       = ""
    days_from_starting_to_canceling_unfinished_large_files = var.cloud_media_unfinished_upload_days
  }

  lifecycle {
    # Originals outlive cells; per-cell cleanup must not remove the shared bucket.
    prevent_destroy = true
  }
}

resource "b2_application_key" "cloud_media_edge" {
  count      = var.cloud_media_enabled ? 1 : 0
  key_name   = "${var.bucket_prefix}-media-edge"
  bucket_ids = [b2_bucket.cloud_media[0].bucket_id]
  # B2 capability names are protocol tokens. The edge cannot destroy versions.
  capabilities = ["readFiles", "writeFiles"]
}
