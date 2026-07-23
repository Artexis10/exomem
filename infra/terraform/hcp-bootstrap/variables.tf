variable "hcp_hostname" {
  description = "HCP Terraform application hostname without a URL scheme."
  type        = string
  default     = "app.terraform.io"

  validation {
    condition     = var.hcp_hostname == "app.terraform.io"
    error_message = "The private beta uses the pinned HCP Terraform hostname app.terraform.io."
  }
}

variable "hcp_organization" {
  description = "Existing HCP Terraform organization that owns Exomem Hosted state."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_-]{2,39}$", var.hcp_organization))
    error_message = "hcp_organization must be a valid 3-40 character HCP organization name."
  }
}

variable "terraform_version" {
  description = "Exact Terraform version accepted by every state workspace."
  type        = string
  default     = "1.15.8"

  validation {
    condition     = var.terraform_version == "1.15.8"
    error_message = "terraform_version must match infra/tool-versions.env."
  }
}
