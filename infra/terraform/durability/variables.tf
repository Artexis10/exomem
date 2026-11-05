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

variable "access_record_enabled" {
  description = "Prepare the retained access record. Does not establish live privacy claims."
  type        = bool
  default     = false
}
variable "access_record_retention_days" {
  description = "Compliance retention for each received version, at least one year."
  type        = number
  default     = 365
  validation {
    condition     = var.access_record_retention_days >= 365 && floor(var.access_record_retention_days) == var.access_record_retention_days
    error_message = "Access records require at least 365 whole days of compliance retention."
  }
}
variable "access_record_node_ids" {
  description = "Content-free node identities, each with a separate write-only prefix key."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for node in var.access_record_node_ids : can(regex("^[a-z0-9][a-z0-9-]{0,62}$", node))])
    error_message = "Node identities must be DNS labels."
  }
}
variable "access_record_notifications_enabled" {
  description = "Enable receipts after account enablement and isolated integration proof."
  type        = bool
  default     = false
}
variable "access_record_notification_url" {
  description = "Existing control-plane HTTPS receiver."
  type        = string
  default     = ""
  validation {
    condition     = !var.access_record_notifications_enabled || startswith(var.access_record_notification_url, "https://")
    error_message = "Enabled notifications require an HTTPS receiver."
  }
}
variable "access_record_notification_secret" {
  description = "B2 notification HMAC secret, delivered separately to the control plane."
  type        = string
  sensitive   = true
  default     = ""
  validation {
    condition     = !var.access_record_notifications_enabled || can(regex("^[a-zA-Z0-9]{32}$", var.access_record_notification_secret))
    error_message = "Enabled notifications require B2's 32-character alphanumeric signing secret."
  }
}
