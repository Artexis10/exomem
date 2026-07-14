output "state_bucket_name" {
  description = "B2 bucket used by the foundation and durability S3 backends."
  value       = b2_bucket.terraform_state.bucket_name
}

output "foundation_backend_application_key_id" {
  description = "B2 key ID restricted to the foundation state prefix."
  value       = b2_application_key.foundation.application_key_id
  sensitive   = true
}

output "foundation_backend_application_key" {
  description = "B2 secret restricted to the foundation state prefix."
  value       = b2_application_key.foundation.application_key
  sensitive   = true
}

output "durability_backend_application_key_id" {
  description = "B2 key ID restricted to the durability state prefix."
  value       = b2_application_key.durability.application_key_id
  sensitive   = true
}

output "durability_backend_application_key" {
  description = "B2 secret restricted to the durability state prefix."
  value       = b2_application_key.durability.application_key
  sensitive   = true
}
