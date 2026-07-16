output "recovery_bucket_name" {
  value = b2_bucket.recovery.bucket_name
}

output "user_export_bucket_name" {
  value = b2_bucket.user_export.bucket_name
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

output "user_export_upload_application_key_id" {
  value     = b2_application_key.user_export_upload.application_key_id
  sensitive = true
}

output "user_export_upload_application_key" {
  value     = b2_application_key.user_export_upload.application_key
  sensitive = true
}

output "user_export_restore_application_key_id" {
  value     = b2_application_key.user_export_restore.application_key_id
  sensitive = true
}

output "user_export_restore_application_key" {
  value     = b2_application_key.user_export_restore.application_key
  sensitive = true
}

output "user_export_delete_application_key_id" {
  value     = b2_application_key.user_export_delete.application_key_id
  sensitive = true
}

output "user_export_delete_application_key" {
  value     = b2_application_key.user_export_delete.application_key
  sensitive = true
}

output "user_export_delivery_application_key_id" {
  value     = b2_application_key.user_export_delivery.application_key_id
  sensitive = true
}

output "user_export_delivery_application_key" {
  value     = b2_application_key.user_export_delivery.application_key
  sensitive = true
}

output "database_backup_upload_application_key_id" {
  value     = b2_application_key.database_backup_upload.application_key_id
  sensitive = true
}

output "database_backup_upload_application_key" {
  value     = b2_application_key.database_backup_upload.application_key
  sensitive = true
}

output "database_backup_restore_application_key_id" {
  value     = b2_application_key.database_backup_restore.application_key_id
  sensitive = true
}

output "database_backup_restore_application_key" {
  value     = b2_application_key.database_backup_restore.application_key
  sensitive = true
}

output "etcd_snapshot_upload_application_key_id" {
  value     = b2_application_key.etcd_snapshot_upload.application_key_id
  sensitive = true
}

output "etcd_snapshot_upload_application_key" {
  value     = b2_application_key.etcd_snapshot_upload.application_key
  sensitive = true
}

output "etcd_snapshot_restore_application_key_id" {
  value     = b2_application_key.etcd_snapshot_restore.application_key_id
  sensitive = true
}

output "etcd_snapshot_restore_application_key" {
  value     = b2_application_key.etcd_snapshot_restore.application_key
  sensitive = true
}
