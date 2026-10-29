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

variable "cloud_backup_enabled" {
  description = "Create separate Cloud cell backup storage and its controller key."
  type        = bool
  default     = false
}

variable "cloud_import_allowed_origins" {
  description = "Browser origins allowed to PUT encrypted import parts. Empty disables browser CORS."
  type        = list(string)
  default     = []
}

variable "cloud_media_enabled" {
  description = "Create the separate encrypted-media bucket and edge key. This does not enable media offload."
  type        = bool
  default     = false
}

variable "cloud_media_unfinished_upload_days" {
  description = "Days before the media bucket cancels an unfinished multipart upload; completed versions never expire."
  type        = number
  default     = 2

  # Reject invalid day counts before provisioning; a false refusal delays the operator's optional bucket creation.
  validation {
    condition = (
      var.cloud_media_unfinished_upload_days >= 1 &&
      floor(var.cloud_media_unfinished_upload_days) == var.cloud_media_unfinished_upload_days
    )
    error_message = "cloud_media_unfinished_upload_days must be a positive whole number of days."
  }
}
