resource "random_id" "bucket_suffix" {
  byte_length = 4
}

resource "b2_bucket" "recovery" {
  bucket_name = "${var.bucket_prefix}-recovery-${random_id.bucket_suffix.hex}"
  bucket_type = "allPrivate"

  default_server_side_encryption {
    mode      = "SSE-B2"
    algorithm = "AES256"
  }

  file_lock_configuration {
    is_file_lock_enabled = true
    default_retention {
      mode = "governance"
      period {
        duration = 7
        unit     = "days"
      }
    }
  }

  lifecycle_rules {
    file_name_prefix              = ""
    days_from_uploading_to_hiding = 30
    days_from_hiding_to_deleting  = 1
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "b2_bucket" "user_export" {
  bucket_name = "${var.bucket_prefix}-export-${random_id.bucket_suffix.hex}"
  bucket_type = "allPrivate"

  default_server_side_encryption {
    mode      = "SSE-B2"
    algorithm = "AES256"
  }

  lifecycle_rules {
    file_name_prefix              = ""
    days_from_uploading_to_hiding = 31
    days_from_hiding_to_deleting  = 1
  }

  lifecycle_rules {
    file_name_prefix              = "user-export-delivery/"
    days_from_uploading_to_hiding = 1
    days_from_hiding_to_deleting  = 1
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "b2_bucket" "database_backup" {
  bucket_name = "${var.bucket_prefix}-database-${random_id.bucket_suffix.hex}"
  bucket_type = "allPrivate"

  default_server_side_encryption {
    mode      = "SSE-B2"
    algorithm = "AES256"
  }

  file_lock_configuration {
    is_file_lock_enabled = true
    default_retention {
      mode = "governance"
      period {
        duration = 7
        unit     = "days"
      }
    }
  }

  lifecycle_rules {
    file_name_prefix              = ""
    days_from_uploading_to_hiding = 30
    days_from_hiding_to_deleting  = 1
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "b2_application_key" "recovery_upload" {
  key_name     = "exomem-recovery-upload"
  bucket_ids   = [b2_bucket.recovery.bucket_id]
  capabilities = ["listBuckets", "listFiles", "readFiles", "readFileRetentions", "writeFiles", "writeFileRetentions"]
}

resource "b2_application_key" "recovery_restore" {
  key_name     = "exomem-recovery-restore"
  bucket_ids   = [b2_bucket.recovery.bucket_id]
  capabilities = ["listBuckets", "listFiles", "readFiles", "readFileRetentions"]
}

resource "b2_application_key" "recovery_delete" {
  key_name     = "exomem-recovery-delete"
  bucket_ids   = [b2_bucket.recovery.bucket_id]
  capabilities = ["bypassGovernance", "deleteFiles", "listBuckets", "listFiles", "readFiles", "readFileRetentions"]
}

resource "b2_application_key" "user_export_upload" {
  key_name     = "exomem-user-export-upload"
  bucket_ids   = [b2_bucket.user_export.bucket_id]
  capabilities = ["listBuckets", "listFiles", "readFiles", "writeFiles"]
}

resource "b2_application_key" "user_export_restore" {
  key_name     = "exomem-user-export-restore"
  bucket_ids   = [b2_bucket.user_export.bucket_id]
  capabilities = ["listBuckets", "listFiles", "readFiles"]
}

resource "b2_application_key" "user_export_delete" {
  key_name     = "exomem-user-export-delete"
  bucket_ids   = [b2_bucket.user_export.bucket_id]
  capabilities = ["deleteFiles", "listBuckets", "listFiles", "readFiles"]
}

resource "b2_application_key" "user_export_delivery" {
  key_name     = "exomem-user-export-delivery-jit"
  bucket_ids   = [b2_bucket.user_export.bucket_id]
  name_prefix  = "user-export-delivery/"
  capabilities = ["listBuckets", "listFiles", "readFiles", "writeFiles"]
}

resource "b2_application_key" "database_backup_upload" {
  key_name     = "exomem-database-backup-upload"
  bucket_ids   = [b2_bucket.database_backup.bucket_id]
  name_prefix  = "database-backup/"
  capabilities = ["listBuckets", "listFiles", "readFiles", "readFileRetentions", "writeFiles", "writeFileRetentions"]
}

resource "b2_application_key" "database_backup_restore" {
  key_name     = "exomem-database-backup-restore-jit"
  bucket_ids   = [b2_bucket.database_backup.bucket_id]
  name_prefix  = "database-backup/"
  capabilities = ["listBuckets", "listFiles", "readFiles", "readFileRetentions"]
}

resource "b2_application_key" "database_backup_delete" {
  key_name     = "exomem-database-backup-delete-jit"
  bucket_ids   = [b2_bucket.database_backup.bucket_id]
  name_prefix  = "database-backup/"
  capabilities = ["bypassGovernance", "deleteFiles", "listBuckets", "listFiles", "readFiles", "readFileRetentions"]
}

resource "b2_application_key" "etcd_snapshot_upload" {
  key_name     = "exomem-etcd-snapshot-upload"
  bucket_ids   = [b2_bucket.database_backup.bucket_id]
  name_prefix  = "etcd-snapshot/"
  capabilities = ["listBuckets", "listFiles", "writeFiles"]
}

resource "b2_application_key" "etcd_snapshot_restore" {
  key_name     = "exomem-etcd-snapshot-restore-jit"
  bucket_ids   = [b2_bucket.database_backup.bucket_id]
  name_prefix  = "etcd-snapshot/"
  capabilities = ["listBuckets", "listFiles", "readFiles"]
}
