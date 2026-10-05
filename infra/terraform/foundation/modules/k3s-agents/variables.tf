variable "nodes" {
  description = "K3s agent nodes keyed by a short DNS-label suffix; each entry is one server."
  type = map(object({
    private_ip        = string
    server_type       = string
    dedicated_cell_id = optional(string, "")
    shared_profile    = optional(string, "")
  }))
  default = {}
  validation {
    condition = alltrue([
      for node in values(var.nodes) :
      node.shared_profile == "" ||
      (node.dedicated_cell_id == "" && length(node.shared_profile) <= 63 &&
      can(regex("^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", node.shared_profile)))
    ])
    error_message = "shared_profile must be an empty value or a DNS label on a non-dedicated agent."
  }


  validation {
    condition = alltrue([
      for key in keys(var.nodes) : can(regex("^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$", key))
    ])
    error_message = "Each agent key must be a lowercase DNS label of 1-40 characters."
  }

  validation {
    # General nodes retain the attachment-based 16 GiB allow-list. A CX33
    # is restricted to one reserved cell and contributes no general slots.
    condition = alltrue([
      for node in values(var.nodes) :
      contains(["cpx42", "ccx23", "ccx33", "ccx43"], node.server_type) ||
      (node.server_type == "cx33" && node.dedicated_cell_id != "")
    ])
    error_message = "General agents require cpx42, ccx23, ccx33 or ccx43; CX33 requires a dedicated cell reservation."
  }

  validation {
    condition = alltrue([
      for node in values(var.nodes) :
      can(cidrnetmask("${node.private_ip}/32")) &&
      cidrhost("${node.private_ip}/${split("/", var.subnet_cidr)[1]}", 0) == cidrhost(var.subnet_cidr, 0) &&
      node.private_ip != cidrhost(var.subnet_cidr, 0) &&
      node.private_ip != cidrhost(var.subnet_cidr, 1) &&
      node.private_ip != cidrhost(var.subnet_cidr, -1)
    ])
    error_message = "Each agent private_ip must be a usable host address inside the private subnet (not its network, gateway or broadcast address)."
  }

  validation {
    condition     = length(distinct([for node in values(var.nodes) : node.private_ip])) == length(var.nodes)
    error_message = "Agent private_ip values must be unique."
  }

  validation {
    condition = alltrue([
      for node in values(var.nodes) : !contains(var.reserved_private_ips, node.private_ip)
    ])
    error_message = "An agent private_ip must not reuse the fleet server's or the control database's address."
  }
  validation {
    condition = alltrue([
      for node in values(var.nodes) :
      node.dedicated_cell_id == "" ||
      (length(node.dedicated_cell_id) == 16 && can(regex("^[a-z2-7]{16}$", node.dedicated_cell_id)))
    ])
    error_message = "dedicated_cell_id must be empty or an exact sixteen-character lowercase base32 cell identifier."
  }

}

variable "subnet_id" {
  description = "Existing private subnet every agent attaches to."
  type        = string
}

variable "subnet_cidr" {
  description = "CIDR of that subnet, used to validate agent addresses."
  type        = string

  validation {
    condition     = can(cidrnetmask(var.subnet_cidr))
    error_message = "subnet_cidr must be an IPv4 CIDR."
  }
}

variable "reserved_private_ips" {
  description = "Private addresses already owned by other servers on the subnet."
  type        = list(string)
}

variable "location" {
  description = "Hetzner location shared with the fleet node; volumes attach only within a location."
  type        = string
}

variable "image" {
  description = "Declared base image; Ansible owns all post-boot configuration."
  type        = string
}

variable "ssh_key_ids" {
  description = "Existing administrator SSH key identifiers."
  type        = list(string)
}

variable "admin_ssh_cidrs" {
  description = "Temporary break-glass CIDRs allowed to reach public SSH; empty closes it."
  type        = set(string)

  validation {
    condition = alltrue([
      for cidr in var.admin_ssh_cidrs :
      can(cidrnetmask(cidr)) && cidr != "0.0.0.0/0" && cidr != "::/0"
    ])
    error_message = "Administrator CIDRs must be explicit and cannot expose SSH globally."
  }
}

variable "labels" {
  description = "Common labels applied to every agent resource."
  type        = map(string)
}

variable "name_prefix" {
  description = "Server name prefix; the full name is also the Kubernetes node name."
  type        = string
  default     = "exomem-agent"
}
