# Named cell_data_b2_*, unlike durability's b2_*, so a TF_VAR_b2_* export meant for durability cannot feed this root.
variable "cell_data_b2_application_key_id" {
  description = "Bootstrap B2 identity of the cell-data member account, used only by reviewed cell-data Terraform applies."
  type        = string
  sensitive   = true
}

variable "cell_data_b2_application_key" {
  description = "Bootstrap B2 secret of the cell-data member account, used only by reviewed cell-data Terraform applies."
  type        = string
  sensitive   = true
}

variable "bucket_prefix" {
  description = "Globally unique, non-PII prefix for the cell-data buckets."
  type        = string
  default     = "exomem-private-alpha"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{4,39}$", var.bucket_prefix))
    error_message = "bucket_prefix must be 5-40 lowercase DNS-safe characters."
  }
}

variable "backup_retention_days" {
  description = "Compliance-mode default retention for every new cell backup version (lock-cloud-backups-against-deletion D2: R)."
  type        = number
  default     = 30

  # Reject an invalid period at plan time; a compliance lock cannot be shortened once a version carries it,
  # so the 60-day cap stops a typo from fixing months of storage cost in place.
  validation {
    condition     = var.backup_retention_days >= 1 && var.backup_retention_days <= 60 && floor(var.backup_retention_days) == var.backup_retention_days
    error_message = "backup_retention_days must be a whole number of days from 1 to 60."
  }
}

variable "staging_object_expiry_days" {
  description = "Days before an export or import staging object is hidden; the lifecycle deletes it one day later."
  type        = number
  default     = 7

  validation {
    condition     = var.staging_object_expiry_days >= 1 && floor(var.staging_object_expiry_days) == var.staging_object_expiry_days
    error_message = "staging_object_expiry_days must be a positive whole number of days."
  }
}

variable "staging_import_allowed_origins" {
  description = "Browser origins allowed to PUT encrypted import parts to the staging bucket. Empty disables browser CORS."
  type        = list(string)
  default     = []
}
