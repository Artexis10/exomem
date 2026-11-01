# Plan only: mocked providers never contact the B2 account or HCP workspace.
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
  bucket_prefix         = "exomem-private-alpha"
}

run "disabled_by_default_preserves_legacy_storage" {
  command = plan

  assert {
    condition = (
      var.cloud_backup_enabled == false &&
      length(b2_bucket.cloud_backups) == 0 &&
      length(b2_application_key.cloud_legacy_delete) == 0 &&
      output.cloud_backup_bucket_name == null &&
      output.cloud_backup_bucket_id == null &&
      output.cloud_backup_account_id == null &&
      output.cloud_legacy_delete_application_key_id == null &&
      output.cloud_legacy_delete_application_key == null
    )
    error_message = "Cloud backup storage and credentials must be absent by default."
  }

  assert {
    condition = (
      b2_bucket.recovery.bucket_name == "exomem-private-alpha-recovery-1a2b3c4d" &&
      b2_bucket.user_export.bucket_name == "exomem-private-alpha-export-1a2b3c4d" &&
      b2_bucket.database_backup.bucket_name == "exomem-private-alpha-database-1a2b3c4d" &&
      b2_bucket.etcd_snapshot.bucket_name == "exomem-private-alpha-etcd-1a2b3c4d"
    )
    error_message = "Existing durability bucket addresses and names must stay unchanged."
  }
}

run "enabled_adds_one_private_unlocked_bucket_and_one_bucket_restricted_delete_key" {
  command = plan

  variables {
    cloud_backup_enabled = true
  }

  override_resource {
    target          = b2_bucket.cloud_backups[0]
    override_during = plan
    values          = { bucket_id = "mock-cloud-bucket" }
  }

  assert {
    condition = (
      length(b2_bucket.cloud_backups) == 1 &&
      b2_bucket.cloud_backups[0].bucket_name == "exomem-private-alpha-cloud-1a2b3c4d" &&
      b2_bucket.cloud_backups[0].bucket_type == "allPrivate" &&
      one(b2_bucket.cloud_backups[0].default_server_side_encryption).mode == "SSE-B2" &&
      one(b2_bucket.cloud_backups[0].default_server_side_encryption).algorithm == "AES256" &&
      one(b2_bucket.cloud_backups[0].file_lock_configuration).is_file_lock_enabled == false &&
      length(b2_bucket.cloud_backups[0].cors_rules) == 0
    )
    error_message = "The Cloud bucket must be private, encrypted, unlocked and closed to browser origins by default."
  }

  assert {
    condition = (
      length(b2_bucket.cloud_backups[0].lifecycle_rules) == 1 &&
      one(b2_bucket.cloud_backups[0].lifecycle_rules).file_name_prefix == "cells/" &&
      one(b2_bucket.cloud_backups[0].lifecycle_rules).days_from_starting_to_canceling_unfinished_large_files == 2 &&
      coalesce(one(b2_bucket.cloud_backups[0].lifecycle_rules).days_from_uploading_to_hiding, 0) == 0 &&
      coalesce(one(b2_bucket.cloud_backups[0].lifecycle_rules).days_from_hiding_to_deleting, 0) == 0
    )
    error_message = "Only unfinished multipart uploads under cells/ may expire; live backup packs must keep their versions."
  }

  # The account-wide key that minted tenant keys is not in this root any more.
  assert {
    condition = (
      length(b2_application_key.cloud_legacy_delete) == 1 &&
      b2_application_key.cloud_legacy_delete[0].key_name == "exomem-cloud-legacy-delete" &&
      toset(b2_application_key.cloud_legacy_delete[0].capabilities) == toset([
        "listBuckets", "listFiles", "deleteFiles"
      ]) &&
      b2_application_key.cloud_legacy_delete[0].bucket_id == null &&
      b2_application_key.cloud_legacy_delete[0].bucket_ids == toset(["mock-cloud-bucket"]) &&
      b2_application_key.cloud_legacy_delete[0].name_prefix == null
    )
    error_message = "The legacy delete key must be limited to the Cloud bucket and hold only listing and file-delete capabilities."
  }

  assert {
    condition     = output.cloud_backup_bucket_name == b2_bucket.cloud_backups[0].bucket_name
    error_message = "The enabled bucket name output must identify the Cloud bucket."
  }
}

run "browser_import_uses_only_configured_origins" {
  command = plan

  variables {
    cloud_backup_enabled         = true
    cloud_import_allowed_origins = ["https://app.example.test", "https://preview.example.test"]
  }

  assert {
    condition = (
      length(b2_bucket.cloud_backups[0].cors_rules) == 1 &&
      toset(one(b2_bucket.cloud_backups[0].cors_rules).allowed_origins) == toset(var.cloud_import_allowed_origins) &&
      toset(one(b2_bucket.cloud_backups[0].cors_rules).allowed_operations) == toset(["s3_put"]) &&
      toset(one(b2_bucket.cloud_backups[0].cors_rules).expose_headers) == toset(["ETag"]) &&
      toset(one(b2_bucket.cloud_backups[0].cors_rules).allowed_headers) == toset(["*"])
    )
    error_message = "Browser imports must allow only configured origins to PUT parts and read ETag."
  }
}
