locals {
  agent_labels = merge(var.labels, { role = "k3s-agent" })
}

# Public exposure of an agent is exactly 443 and administrator SSH. Cluster
# traffic (K3s API, Flannel VXLAN, kubelet) uses the private network, which
# Hetzner firewalls do not filter; the k3s role's host firewall admits it only
# from the other K3s nodes' declared private addresses. SSH stays direct
# because the base role forbids TCP forwarding, so the server cannot act as a
# jump host.
resource "hcloud_firewall" "agents" {
  name   = "${var.name_prefix}s"
  labels = local.agent_labels

  rule {
    description = "Restricted administrator SSH"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = var.admin_ssh_cidrs
  }

  rule {
    description = "Public TLS ingress"
    direction   = "in"
    protocol    = "tcp"
    port        = "443"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }
}

# Agents are disposable: they hold no state of their own (each cell volume is
# a separate Hetzner volume that detaches with the server), so removing a map
# entry is one plan. The guard against an accidental removal is the saved-plan
# inspector, which refuses any destroy without a per-address approval.
resource "hcloud_server" "agent" {
  for_each = var.nodes

  name                     = "${var.name_prefix}-${each.key}"
  server_type              = each.value.server_type
  image                    = var.image
  location                 = var.location
  ssh_keys                 = var.ssh_key_ids
  firewall_ids             = [hcloud_firewall.agents.id]
  backups                  = false
  delete_protection        = false
  rebuild_protection       = false
  keep_disk                = true
  shutdown_before_deletion = true
  labels                   = merge(local.agent_labels, { node = each.key })

  # Auto-assigned IPv4 for image, package and object-storage egress; nothing
  # public resolves to it while ingress stays pinned to the server.
  public_net {
    ipv4_enabled = true
    ipv6_enabled = false
  }

  network {
    subnet_id = var.subnet_id
    ip        = each.value.private_ip
    alias_ips = []
  }
}
