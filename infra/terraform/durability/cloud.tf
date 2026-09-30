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

  # Restic owns retention of live backup packs; no upload-age lifecycle expiry.
  lifecycle {
    prevent_destroy = true
  }
}

# Key creation requires account scope. cellctl uses this parent only to mint
# tenant keys; tenant prefix restrictions are enforced by the runtime.
resource "b2_application_key" "cloud_controller" {
  count        = var.cloud_backup_enabled ? 1 : 0
  key_name     = "exomem-cloud-controller"
  capabilities = ["listKeys", "writeKeys", "deleteKeys", "listFiles", "deleteFiles"]
}
