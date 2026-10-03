# A disposable physical-headroom experiment, not a K3s fleet agent. No network
# block, fleet join inputs, tenant credentials, or production inventory entry.
variable "cloud_acceptance_enabled" {
  description = "Explicitly provision one disposable, isolated Cloud acceptance host; disable and destroy it after collecting evidence."
  type        = bool
  default     = false
}

locals {
  acceptance_labels = merge(local.common_labels, {
    environment = "isolated-acceptance"
    purpose     = "cloud-resource-acceptance"
  })
}

resource "hcloud_firewall" "acceptance" {
  count  = var.cloud_acceptance_enabled ? 1 : 0
  name   = "exomem-cloud-acceptance"
  labels = local.acceptance_labels

  rule {
    description = "Restricted acceptance administrator SSH"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = var.admin_ssh_cidrs
  }
}

resource "hcloud_primary_ip" "acceptance" {
  count       = var.cloud_acceptance_enabled ? 1 : 0
  name        = "exomem-cloud-acceptance-ipv4"
  type        = "ipv4"
  location    = var.server_location
  auto_delete = false
  labels      = local.acceptance_labels
}

resource "hcloud_server" "acceptance" {
  count                    = var.cloud_acceptance_enabled ? 1 : 0
  name                     = "exomem-cloud-acceptance"
  server_type              = "ccx23"
  image                    = var.server_image
  location                 = var.server_location
  ssh_keys                 = [hcloud_ssh_key.admin.id]
  firewall_ids             = [hcloud_firewall.acceptance[0].id]
  backups                  = false
  delete_protection        = false
  rebuild_protection       = false
  shutdown_before_deletion = true
  labels                   = local.acceptance_labels

  public_net {
    ipv4_enabled = true
    ipv4         = hcloud_primary_ip.acceptance[0].id
    ipv6_enabled = false
  }
}

output "cloud_acceptance_host" {
  description = "Non-sensitive disposable acceptance coordinates, intentionally absent from K3s agent inventory."
  value = var.cloud_acceptance_enabled ? {
    id   = hcloud_server.acceptance[0].id
    name = hcloud_server.acceptance[0].name
    ipv4 = hcloud_primary_ip.acceptance[0].ip_address
  } : null
}
