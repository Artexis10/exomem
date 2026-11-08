output "recovery_bucket_name" {
  value = b2_bucket.recovery.bucket_name
}

output "user_export_bucket_name" {
  value = b2_bucket.user_export.bucket_name
}

output "database_backup_bucket_name" {
  value = b2_bucket.database_backup.bucket_name
}

output "etcd_snapshot_bucket_name" {
  value = b2_bucket.etcd_snapshot.bucket_name
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

output "cloud_backup_bucket_name" {
  value = one(b2_bucket.cloud_backups[*].bucket_name)
}

output "cloud_media_bucket_name" {
  value = one(b2_bucket.cloud_media[*].bucket_name)
}

output "cloud_media_bucket_id" {
  value = one(b2_bucket.cloud_media[*].bucket_id)
}

output "cloud_media_edge_application_key_id" {
  value     = one(b2_application_key.cloud_media_edge[*].application_key_id)
  sensitive = true
}

output "cloud_media_edge_application_key" {
  value     = one(b2_application_key.cloud_media_edge[*].application_key)
  sensitive = true
}

output "cloud_media_list_application_key_id" {
  value     = one(b2_application_key.cellctl_media_list[*].application_key_id)
  sensitive = true
}

output "cloud_media_list_application_key" {
  value     = one(b2_application_key.cellctl_media_list[*].application_key)
  sensitive = true
}

output "cloud_backup_bucket_id" {
  value = one(b2_bucket.cloud_backups[*].bucket_id)
}

output "cloud_backup_account_id" {
  value = one(b2_bucket.cloud_backups[*].account_id)
}

output "access_record_bucket" {
  value = var.access_record_enabled ? {
    id   = b2_bucket.access_record[0].bucket_id
    name = b2_bucket.access_record[0].bucket_name
  } : null
}
output "access_record_node_credentials" {
  value = { for node, key in b2_application_key.access_record_node : node => {
    id = key.application_key_id, key = key.application_key
  } }
  sensitive = true
}
output "access_record_reader_credential" {
  value = var.access_record_enabled ? {
    id  = b2_application_key.access_record_reader[0].application_key_id
    key = b2_application_key.access_record_reader[0].application_key
  } : null
  sensitive = true
}
