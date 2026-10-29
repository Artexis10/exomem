# Mocked plans prove the storage boundary without provider credentials or writes.
mock_provider "b2" {}
mock_provider "random" {}

override_resource {
  target          = random_id.bucket_suffix
  override_during = plan
  values          = { hex = "1a2b3c4d" }
}

variables {
  b2_application_key_id = "mock-only"
  b2_application_key    = "mock-only"
  cloud_backup_enabled  = true
}

run "media_storage_is_absent_until_selected" {
  command = plan

  assert {
    condition = (
      length(b2_bucket.cloud_media) == 0 &&
      length(b2_application_key.cloud_media_edge) == 0 &&
      output.cloud_media_bucket_name == null &&
      output.cloud_media_bucket_id == null &&
      output.cloud_media_edge_application_key_id == null &&
      output.cloud_media_edge_application_key == null &&
      length(b2_bucket.cloud_backups) == 1
    )
    error_message = "Enabling Cloud backups must not create media storage or an edge credential."
  }
}

run "media_storage_preserves_versions_and_isolates_the_edge" {
  command = plan

  variables {
    cloud_media_enabled                = true
    cloud_media_unfinished_upload_days = 7
  }

  override_resource {
    target          = b2_bucket.cloud_media[0]
    override_during = plan
    values          = { bucket_id = "mock-media-bucket" }
  }

  assert {
    condition = (
      length(b2_bucket.cloud_media) == 1 &&
      b2_bucket.cloud_media[0].bucket_name != b2_bucket.cloud_backups[0].bucket_name &&
      b2_bucket.cloud_media[0].bucket_type == "allPrivate" &&
      one(b2_bucket.cloud_media[0].file_lock_configuration).is_file_lock_enabled == false &&
      length(b2_bucket.cloud_media[0].cors_rules) == 0 &&
      length(b2_bucket.cloud_media[0].lifecycle_rules) == 1 &&
      one(b2_bucket.cloud_media[0].lifecycle_rules).file_name_prefix == "" &&
      one(b2_bucket.cloud_media[0].lifecycle_rules).days_from_starting_to_canceling_unfinished_large_files == 7 &&
      coalesce(one(b2_bucket.cloud_media[0].lifecycle_rules).days_from_uploading_to_hiding, 0) == 0 &&
      coalesce(one(b2_bucket.cloud_media[0].lifecycle_rules).days_from_hiding_to_deleting, 0) == 0
    )
    error_message = "Media must use a separate private bucket that expires only unfinished uploads, without hiding or deleting completed versions."
  }

  assert {
    condition = (
      length(b2_application_key.cloud_media_edge) == 1 &&
      b2_application_key.cloud_media_edge[0].bucket_id == null &&
      b2_application_key.cloud_media_edge[0].bucket_ids == toset(["mock-media-bucket"]) &&
      toset(b2_application_key.cloud_media_edge[0].capabilities) == toset(["readFiles", "writeFiles"])
    )
    error_message = "The edge must access only the media bucket, with read/write access and no listing, key management or permanent deletion."
  }
}
