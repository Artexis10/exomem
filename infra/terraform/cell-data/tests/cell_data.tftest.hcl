# Plan only: mocked providers never contact a B2 account or HCP workspace.
mock_provider "b2" {}
mock_provider "random" {}

override_resource {
  target          = random_id.bucket_suffix
  override_during = plan
  values          = { hex = "1a2b3c4d" }
}

override_resource {
  target          = b2_bucket.cell_backups
  override_during = plan
  values          = { bucket_id = "mock-backup-bucket", account_id = "mock-account" }
}

override_resource {
  target          = b2_bucket.cell_staging
  override_during = plan
  values          = { bucket_id = "mock-staging-bucket" }
}

variables {
  b2_application_key_id = "mock-only"
  b2_application_key    = "mock-only"
  bucket_prefix         = "exomem-private-alpha"
}

run "backup_bucket_is_private_encrypted_and_compliance_locked" {
  command = plan

  assert {
    condition = (
      b2_bucket.cell_backups.bucket_name == "exomem-private-alpha-cell-backup-1a2b3c4d" &&
      b2_bucket.cell_backups.bucket_type == "allPrivate" &&
      one(b2_bucket.cell_backups.default_server_side_encryption).mode == "SSE-B2" &&
      length(b2_bucket.cell_backups.cors_rules) == 0
    )
    error_message = "The backup bucket must be private, encrypted and closed to browser origins."
  }

  assert {
    condition = (
      one(b2_bucket.cell_backups.file_lock_configuration).is_file_lock_enabled == true &&
      one(one(b2_bucket.cell_backups.file_lock_configuration).default_retention).mode == "compliance" &&
      one(one(one(b2_bucket.cell_backups.file_lock_configuration).default_retention).period).duration == 30 &&
      one(one(one(b2_bucket.cell_backups.file_lock_configuration).default_retention).period).unit == "days"
    )
    error_message = "Every new backup version must carry a 30-day compliance-mode lock."
  }
}

run "backup_lifecycle_expires_only_noncurrent_versions" {
  command = plan

  assert {
    condition = (
      length(b2_bucket.cell_backups.lifecycle_rules) == 1 &&
      one(b2_bucket.cell_backups.lifecycle_rules).file_name_prefix == "" &&
      one(b2_bucket.cell_backups.lifecycle_rules).days_from_hiding_to_deleting == 1 &&
      one(b2_bucket.cell_backups.lifecycle_rules).days_from_starting_to_canceling_unfinished_large_files == 2 &&
      one(b2_bucket.cell_backups.lifecycle_rules).days_from_uploading_to_hiding == null
    )
    error_message = "Only noncurrent versions and unfinished uploads may expire; a current backup pack must never be hidden by the lifecycle."
  }
}

run "staging_bucket_is_private_unlocked_and_expires_objects" {
  command = plan

  assert {
    condition = (
      b2_bucket.cell_staging.bucket_name == "exomem-private-alpha-cell-staging-1a2b3c4d" &&
      b2_bucket.cell_staging.bucket_type == "allPrivate" &&
      one(b2_bucket.cell_staging.file_lock_configuration).is_file_lock_enabled == false &&
      length(b2_bucket.cell_staging.cors_rules) == 0
    )
    error_message = "The staging bucket must be private, unlocked and closed to browser origins by default."
  }

  assert {
    condition = (
      length(b2_bucket.cell_staging.lifecycle_rules) == 1 &&
      one(b2_bucket.cell_staging.lifecycle_rules).file_name_prefix == "" &&
      one(b2_bucket.cell_staging.lifecycle_rules).days_from_uploading_to_hiding == 7 &&
      one(b2_bucket.cell_staging.lifecycle_rules).days_from_hiding_to_deleting == 1 &&
      one(b2_bucket.cell_staging.lifecycle_rules).days_from_starting_to_canceling_unfinished_large_files == 2
    )
    error_message = "Staging objects must be hidden after seven days, deleted a day later, and abandoned uploads cancelled."
  }
}

run "staging_import_cors_allows_only_configured_origins" {
  command = plan

  variables {
    staging_import_allowed_origins = ["https://app.example.test"]
  }

  assert {
    condition = (
      length(b2_bucket.cell_staging.cors_rules) == 1 &&
      toset(one(b2_bucket.cell_staging.cors_rules).allowed_origins) == toset(["https://app.example.test"]) &&
      toset(one(b2_bucket.cell_staging.cors_rules).allowed_operations) == toset(["s3_put"]) &&
      length(b2_bucket.cell_backups.cors_rules) == 0
    )
    error_message = "Only the staging bucket may allow browser PUTs, and only from configured origins."
  }
}

run "key_custodian_creates_keys_and_nothing_else" {
  command = plan

  assert {
    condition = (
      toset(b2_application_key.key_custodian.capabilities) == toset(["listKeys", "writeKeys", "deleteKeys"]) &&
      b2_application_key.key_custodian.bucket_id == null &&
      b2_application_key.key_custodian.bucket_ids == null &&
      b2_application_key.key_custodian.name_prefix == null
    )
    error_message = "The custodian key must have exactly the account-wide key-management capabilities and no file access."
  }
}

run "cellctl_staging_key_reaches_only_the_staging_bucket" {
  command = plan

  assert {
    condition = (
      b2_application_key.cellctl_staging.bucket_ids == toset(["mock-staging-bucket"]) &&
      toset(b2_application_key.cellctl_staging.capabilities) == toset([
        "listBuckets", "listFiles", "readFiles", "writeFiles", "deleteFiles"
      ])
    )
    error_message = "The cellctl staging key must be limited to the staging bucket with exactly its five capabilities."
  }
}

run "cellctl_backup_key_can_only_list_the_backup_bucket" {
  command = plan

  assert {
    condition = (
      b2_application_key.cellctl_backup_list.bucket_ids == toset(["mock-backup-bucket"]) &&
      toset(b2_application_key.cellctl_backup_list.capabilities) == toset(["listBuckets", "listFiles"])
    )
    error_message = "The cellctl backup key must be limited to the backup bucket and hold only listBuckets and listFiles."
  }
}
