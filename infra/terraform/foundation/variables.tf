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

variable "gateway_hostname" {
  description = "Optional Cloud MCP hostname for DNS-only direct TLS to the fleet node; empty disables its DNS record. Use a dedicated hostname, not an existing desktop connector."
  type        = string
  default     = ""

  validation {
    condition = var.gateway_hostname == "" || (
      can(regex("^[a-z0-9](?:[a-z0-9-]{0,62}\\.)+[a-z]{2,63}$", var.gateway_hostname)) &&
      var.gateway_hostname != var.control_hostname &&
      var.gateway_hostname != var.transfer_hostname
    )
    error_message = "gateway_hostname must be empty or a distinct lowercase ASCII DNS name."
  }
}

variable "admin_ssh_cidrs" {
  description = "Temporary break-glass CIDRs allowed to reach public SSH; empty (administration over NetBird) closes it."
  type        = set(string)
  default     = []

  validation {
    condition = alltrue([
      for cidr in var.admin_ssh_cidrs :
      can(cidrhost(cidr, 0)) && cidr != "0.0.0.0/0" && cidr != "::/0"
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
  # The existing alpha remains pinned. Historical same-location CX stock
  # failures do not establish permanent retirement; verify current availability
  # and an approved saved plan before any new server purchase or resize.
  # Capacity acceptance uses measured warm platform/cell usage, not old cold
  # memory estimates or volume-attachment counts alone.
  description = "Existing shared-x86 alpha instance, intentionally pinned to CX33."
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

variable "shared_control" {
  description = "Required non-secret dependency published by substrate-infra. Exomem never reads shared Terraform state."
  nullable    = false
  type = object({
    schema_version = number
    owner          = string
    server_id      = string
    public_ipv4    = string
    hostname       = string
    direct_port    = number
    pooled_port    = number
    sslmode        = string
  })
  validation {
    # Version, owner and TLS mode are fixed by the shared-control dependency contract.
    condition = (
      var.shared_control.schema_version == 2 &&
      var.shared_control.owner == "substrate-infra" &&
      var.shared_control.sslmode == "verify-full" &&
      var.shared_control.direct_port > 0 && var.shared_control.direct_port < 65536 &&
      var.shared_control.pooled_port > 0 && var.shared_control.pooled_port < 65536 &&
      can(cidrnetmask("${var.shared_control.public_ipv4}/32")) &&
      length(var.shared_control.hostname) > 0 &&
      length(var.shared_control.server_id) > 0
    )
    error_message = "The shared-control dependency must be complete version 2 from substrate-infra with verify-full TLS."
  }
}

variable "k3s_agent_nodes" {
  # One entry is one K3s agent server (add-cloud-node-provisioning N1). Adding
  # or removing an entry is the whole Terraform change; the module validates
  # addresses against the subnet and the reserved fleet-server address. Run
  # infra/ansible/remove-agent.yml BEFORE removing an entry.
  description = "K3s agent nodes keyed by a short DNS-label suffix: { private_ip, server_type, optional dedicated_cell_id, optional shared_profile }."
  type = map(object({
    private_ip        = string
    server_type       = string
    dedicated_cell_id = optional(string, "")
    shared_profile    = optional(string, "")
  }))
  default = {}
  validation {
    condition = alltrue([
      for node in values(var.k3s_agent_nodes) :
      node.shared_profile == "" ||
      (node.dedicated_cell_id == "" && length(node.shared_profile) <= 63 &&
      can(regex("^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", node.shared_profile)))
    ])
    error_message = "shared_profile must be an empty value or a DNS label on a non-dedicated agent."
  }

  validation {
    condition = alltrue([
      for node in values(var.k3s_agent_nodes) :
      node.dedicated_cell_id == "" ||
      (length(node.dedicated_cell_id) == 16 && can(regex("^[a-z2-7]{16}$", node.dedicated_cell_id)))
    ])
    error_message = "dedicated_cell_id must be empty or an exact sixteen-character lowercase base32 cell identifier."
  }

}

variable "vswitch" {
  # Set only once the dedicated server is bought and its vSwitch exists in
  # Robot. The dedicated server configures its own address in this subnet on
  # the VLAN (the k3s role does, from the generated inventory).
  description = "Optional Robot vSwitch coupled to the private network: { id, vlan_id, subnet_cidr }. Null creates nothing."
  type = object({
    id          = number
    vlan_id     = number
    subnet_cidr = string
  })
  default = null

  validation {
    condition     = var.vswitch == null || try(var.vswitch.vlan_id >= 4000 && var.vswitch.vlan_id <= 4091, false)
    error_message = "A Hetzner vSwitch VLAN ID is between 4000 and 4091."
  }

  validation {
    condition = var.vswitch == null || try(
      cidrhost(format("%s/%s", cidrhost(var.vswitch.subnet_cidr, 0), split("/", var.private_network_cidr)[1]), 0)
      == cidrhost(var.private_network_cidr, 0)
      && tonumber(split("/", var.vswitch.subnet_cidr)[1]) > tonumber(split("/", var.private_network_cidr)[1]),
      false
    )
    error_message = "The vSwitch subnet must sit inside the private network."
  }
}
