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

  # Nothing hides a version by age. collect-released-media D2: a version that a hold condemned, or that a
  # newer upload superseded, is deleted after the grace; until then a rescue restores it.
  lifecycle_rules {
    file_name_prefix                                       = ""
    days_from_starting_to_canceling_unfinished_large_files = var.cloud_media_unfinished_upload_days
    days_from_hiding_to_deleting                           = var.cloud_media_condemned_days
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

# cellctl's physical meter lists the media prefixes with this key (add-cloud-media-store D10). It
# cannot read, write or delete a file, and no service holds deleteFiles on this bucket: the operator
# deletes with a short-lived key (docs/runbooks/cloud-key-custody.md).
resource "b2_application_key" "cellctl_media_list" {
  count      = var.cloud_media_enabled ? 1 : 0
  key_name   = "${var.bucket_prefix}-cellctl-media-list"
  bucket_ids = [b2_bucket.cloud_media[0].bucket_id]
  # B2 capability names are protocol tokens: listBuckets resolves the bucket, listFiles lists versions.
  capabilities = ["listBuckets", "listFiles"]
}

# The out-of-cluster key watcher counts the media bucket's hide markers with this key (collect-released-media
# D4). It can only list, and no in-cluster component holds it, so a compromised cluster cannot blind the count.
resource "b2_application_key" "key_watcher_media_list" {
  count      = var.cloud_media_enabled ? 1 : 0
  key_name   = "${var.bucket_prefix}-key-watcher-media-list"
  bucket_ids = [b2_bucket.cloud_media[0].bucket_id]
  # B2 capability names are protocol tokens: listFiles lists versions and hide markers, nothing else.
  capabilities = ["listFiles"]
}
