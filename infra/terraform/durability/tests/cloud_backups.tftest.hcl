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
      length(b2_application_key.cloud_controller) == 0 &&
      output.cloud_backup_bucket_name == null &&
      output.cloud_backup_bucket_id == null &&
      output.cloud_backup_account_id == null &&
      output.cloud_controller_application_key_id == null &&
      output.cloud_controller_application_key == null
    )
    error_message = "Cloud backup storage and credentials must be absent by default."
  }

  assert {
    condition = (
      b2_bucket.recovery.bucket_name == "exomem-private-alpha-recovery-1a2b3c4d" &&
      b2_bucket.user_export.bucket_name == "exomem-private-alpha-export-1a2b3c4d" &&
      b2_bucket.database_backup.bucket_name == "exomem-private-alpha-database-1a2b3c4d" &&
      b2_bucket.etcd_snapshot.bucket_name == "exomem-private-alpha-etcd-1a2b3c4d" &&
      b2_bucket.control_db_pgbackrest.bucket_name == "exomem-private-alpha-control-db-1a2b3c4d"
    )
    error_message = "Existing durability bucket addresses and names must stay unchanged."
  }
}

run "enabled_adds_one_private_unlocked_bucket_and_one_unrestricted_controller_key" {
  command = plan

  variables {
    cloud_backup_enabled = true
  }

  assert {
    condition = (
      length(b2_bucket.cloud_backups) == 1 &&
      b2_bucket.cloud_backups[0].bucket_name == "exomem-private-alpha-cloud-1a2b3c4d" &&
      b2_bucket.cloud_backups[0].bucket_type == "allPrivate" &&
      one(b2_bucket.cloud_backups[0].default_server_side_encryption).mode == "SSE-B2" &&
      one(b2_bucket.cloud_backups[0].default_server_side_encryption).algorithm == "AES256" &&
      one(b2_bucket.cloud_backups[0].file_lock_configuration).is_file_lock_enabled == false &&
      length(b2_bucket.cloud_backups[0].lifecycle_rules) == 0
    )
    error_message = "The Cloud bucket must be private, encrypted, unlocked and free of age-based expiry."
  }

  assert {
    condition = (
      length(b2_application_key.cloud_controller) == 1 &&
      b2_application_key.cloud_controller[0].key_name == "exomem-cloud-controller" &&
      toset(b2_application_key.cloud_controller[0].capabilities) == toset([
        "listKeys", "writeKeys", "deleteKeys", "listFiles", "deleteFiles"
      ]) &&
      b2_application_key.cloud_controller[0].bucket_id == null &&
      b2_application_key.cloud_controller[0].bucket_ids == null &&
      b2_application_key.cloud_controller[0].name_prefix == null
    )
    error_message = "The controller key must have exactly the requested account-wide key and cleanup capabilities."
  }

  assert {
    condition     = output.cloud_backup_bucket_name == b2_bucket.cloud_backups[0].bucket_name
    error_message = "The enabled bucket name output must identify the Cloud bucket."
  }
}
