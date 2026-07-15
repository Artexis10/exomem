variable "hcloud_token" {
  description = "Hetzner API token supplied through a non-printing TF_VAR handoff."
  type        = string
  sensitive   = true
}

variable "cloudflare_api_token" {
  description = "Cloudflare API token scoped to Tunnel, DNS, and Access resources."
  type        = string
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Opaque Cloudflare account identifier."
  type        = string
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone containing the control and transfer hostnames."
  type        = string
}

variable "control_hostname" {
  description = "Access-protected control hostname, without a scheme or path."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9](?:[a-z0-9-]{0,62}\\.)+[a-z]{2,63}$", var.control_hostname))
    error_message = "control_hostname must be a lowercase ASCII DNS name."
  }
}

variable "transfer_hostname" {
  description = "Direct browser-transfer hostname, without a scheme or path."
  type        = string

  validation {
    condition = (
      can(regex("^[a-z0-9](?:[a-z0-9-]{0,62}\\.)+[a-z]{2,63}$", var.transfer_hostname)) &&
      var.transfer_hostname != var.control_hostname
    )
    error_message = "transfer_hostname must be a distinct lowercase ASCII DNS name."
  }
}

variable "admin_ssh_cidrs" {
  description = "Explicit operator IPv4/IPv6 CIDRs allowed to reach SSH."
  type        = set(string)

  validation {
    condition     = length(var.admin_ssh_cidrs) > 0
    error_message = "At least one explicit administrator CIDR is required."
  }

  validation {
    condition = alltrue([
      for cidr in var.admin_ssh_cidrs :
      can(cidrnetmask(cidr)) && cidr != "0.0.0.0/0" && cidr != "::/0"
    ])
    error_message = "Administrator CIDRs must be valid and cannot expose SSH globally."
  }
}

variable "ssh_public_key" {
  description = "Public administrator key uploaded to Hetzner."
  type        = string

  validation {
    condition     = can(regex("^ssh-(?:ed25519|rsa) [A-Za-z0-9+/=]+(?: .*)?$", var.ssh_public_key))
    error_message = "ssh_public_key must be an OpenSSH Ed25519 or RSA public key."
  }
}

variable "server_name" {
  description = "Opaque host name for the dedicated private-alpha node."
  type        = string
  default     = "exomem-alpha-01"
}

variable "server_type" {
  description = "Cost-safe shared-x86 Hetzner instance used until owner soak proves otherwise."
  type        = string
  default     = "cx33"

  validation {
    condition     = var.server_type == "cx33"
    error_message = "The private alpha is intentionally pinned to cx33."
  }
}

variable "server_location" {
  description = "Hetzner location for compute, primary IP, and encrypted volumes."
  type        = string
  default     = "fsn1"

  validation {
    condition     = contains(["fsn1", "nbg1", "hel1"], var.server_location)
    error_message = "The private alpha must remain in an approved EU Hetzner location."
  }
}

variable "server_image" {
  description = "Declared base image; Ansible owns all post-boot configuration."
  type        = string
  default     = "ubuntu-24.04"

  validation {
    condition     = var.server_image == "ubuntu-24.04"
    error_message = "The base image is pinned to Ubuntu 24.04."
  }
}

variable "private_network_cidr" {
  description = "Dedicated private network CIDR."
  type        = string
  default     = "10.50.0.0/16"
}

variable "private_subnet_cidr" {
  description = "Dedicated private subnet CIDR."
  type        = string
  default     = "10.50.1.0/24"
}

variable "private_node_ip" {
  description = "Stable node address inside the private subnet."
  type        = string
  default     = "10.50.1.10"
}
