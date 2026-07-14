output "recovery_bucket_name" {
  value = b2_bucket.recovery.bucket_name
}

output "database_backup_bucket_name" {
  value = b2_bucket.database_backup.bucket_name
}

output "recovery_upload_application_key_id" {
  value     = b2_application_key.recovery_upload.application_key_id
  sensitive = true
}

output "recovery_upload_application_key" {
  value     = b2_application_key.recovery_upload.application_key
  sensitive = true
}

output "recovery_restore_application_key_id" {
  value     = b2_application_key.recovery_restore.application_key_id
  sensitive = true
}

output "recovery_restore_application_key" {
  value     = b2_application_key.recovery_restore.application_key
  sensitive = true
}

output "recovery_delete_application_key_id" {
  value     = b2_application_key.recovery_delete.application_key_id
  sensitive = true
}

output "recovery_delete_application_key" {
  value     = b2_application_key.recovery_delete.application_key
  sensitive = true
}

output "database_backup_application_key_id" {
  value     = b2_application_key.database_backup.application_key_id
  sensitive = true
}

output "database_backup_application_key" {
  value     = b2_application_key.database_backup.application_key
  sensitive = true
}
