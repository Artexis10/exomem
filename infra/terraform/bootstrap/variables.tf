variable "b2_application_key_id" {
  description = "One-time B2 bootstrap identity with bucket and application-key administration capabilities."
  type        = string
  sensitive   = true
}

variable "b2_application_key" {
  description = "Secret for the one-time B2 bootstrap identity."
  type        = string
  sensitive   = true
}

variable "state_bucket_name" {
  description = "Globally unique, non-PII B2 bucket name used only for Terraform remote state."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{4,48}[a-z0-9]$", var.state_bucket_name))
    error_message = "state_bucket_name must be 6-50 lowercase DNS-safe characters."
  }
}
