variable "nodes" {
  description = "K3s agent nodes keyed by a short DNS-label suffix; each entry is one server."
  type = map(object({
    private_ip  = string
    server_type = string
  }))
  default = {}

  validation {
    condition = alltrue([
      for key in keys(var.nodes) : can(regex("^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$", key))
    ])
    error_message = "Each agent key must be a lowercase DNS label of 1-40 characters."
  }

  validation {
    # Capacity is counted in volume attachments, not memory: 16 attachments
    # minus the controller's headroom of 5 leaves 11 cell slots at a 1 GiB
    # request each, so only x86 types with at least 16 GB are allowed. The
    # k3s role pins the amd64 binary, which also rules out ARM (cax).
    condition = alltrue([
      for node in values(var.nodes) : contains(["cpx42", "ccx23", "ccx33", "ccx43"], node.server_type)
    ])
    error_message = "Agent server_type must be one of cpx42, ccx23, ccx33 or ccx43 (x86, at least 16 GB)."
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
  description = "Explicit operator CIDRs allowed to reach SSH."
  type        = set(string)

  validation {
    condition = length(var.admin_ssh_cidrs) > 0 && alltrue([
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
