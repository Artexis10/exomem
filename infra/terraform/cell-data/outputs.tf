output "cell_backup_bucket_name" {
  value = b2_bucket.cell_backups.bucket_name
}

output "cell_backup_bucket_id" {
  value = b2_bucket.cell_backups.bucket_id
}

output "cell_staging_bucket_name" {
  value = b2_bucket.cell_staging.bucket_name
}

output "cell_staging_bucket_id" {
  value = b2_bucket.cell_staging.bucket_id
}

output "cell_data_account_id" {
  value = b2_bucket.cell_backups.account_id
}

output "key_custodian_application_key_id" {
  value     = b2_application_key.key_custodian.application_key_id
  sensitive = true
}

output "key_custodian_application_key" {
  value     = b2_application_key.key_custodian.application_key
  sensitive = true
}

output "cellctl_staging_application_key_id" {
  value     = b2_application_key.cellctl_staging.application_key_id
  sensitive = true
}

output "cellctl_staging_application_key" {
  value     = b2_application_key.cellctl_staging.application_key
  sensitive = true
}

output "cellctl_backup_list_application_key_id" {
  value     = b2_application_key.cellctl_backup_list.application_key_id
  sensitive = true
}

output "cellctl_backup_list_application_key" {
  value     = b2_application_key.cellctl_backup_list.application_key
  sensitive = true
}
