variable "b2_application_key_id" {
  description = "Bootstrap B2 identity used only by reviewed durability Terraform applies."
  type        = string
  sensitive   = true
}

variable "b2_application_key" {
  description = "Bootstrap B2 secret used only by reviewed durability Terraform applies."
  type        = string
  sensitive   = true
}

variable "bucket_prefix" {
  description = "Globally unique, non-PII prefix for private-alpha durability buckets."
  type        = string
  default     = "exomem-private-alpha"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{4,39}$", var.bucket_prefix))
    error_message = "bucket_prefix must be 5-40 lowercase DNS-safe characters."
  }
}
