locals {
  common_labels = {
    application = "exomem-hosted"
    environment = "private-alpha"
    managed_by  = "terraform"
  }
}

resource "hcloud_ssh_key" "admin" {
  name       = "exomem-alpha-admin"
  public_key = var.ssh_public_key
  labels     = local.common_labels

  lifecycle {
    prevent_destroy = true
  }
}

resource "hcloud_primary_ip" "node" {
  name              = "exomem-alpha-ipv4"
  type              = "ipv4"
  location          = var.server_location
  auto_delete       = false
  delete_protection = true
  labels            = local.common_labels

  lifecycle {
    prevent_destroy = true
  }
}

resource "hcloud_network" "alpha" {
  name              = "exomem-alpha"
  ip_range          = var.private_network_cidr
  delete_protection = true
  labels            = local.common_labels

  lifecycle {
    prevent_destroy = true
  }
}

resource "hcloud_network_subnet" "alpha" {
  network_id   = hcloud_network.alpha.id
  type         = "cloud"
  network_zone = "eu-central"
  ip_range     = var.private_subnet_cidr
}

resource "hcloud_server" "alpha" {
  name                     = var.server_name
  server_type              = var.server_type
  image                    = var.server_image
  location                 = var.server_location
  ssh_keys                 = [hcloud_ssh_key.admin.id]
  firewall_ids             = [hcloud_firewall.alpha.id]
  backups                  = false
  delete_protection        = true
  rebuild_protection       = true
  keep_disk                = true
  shutdown_before_deletion = true
  labels                   = local.common_labels

  public_net {
    ipv4_enabled = true
    ipv4         = hcloud_primary_ip.node.id
    ipv6_enabled = false
  }

  network {
    subnet_id = hcloud_network_subnet.alpha.id
    ip        = var.private_node_ip
    alias_ips = []
  }

  lifecycle {
    prevent_destroy = true
  }
}

# The control database keeps its own failure domain (D12): a separate
# Hetzner server, on the same private network as the fleet node so cellctl
# and the gateway reach it without a public hop, but with its own primary
# IP, firewall, and DNS-only record so losing the fleet node never touches
# tenant or cell rows.
resource "hcloud_primary_ip" "control_db" {
  name              = "exomem-control-db-ipv4"
  type              = "ipv4"
  location          = var.server_location
  auto_delete       = false
  delete_protection = true
  labels            = local.common_labels

  lifecycle {
    prevent_destroy = true
  }
}

resource "hcloud_server" "control" {
  name                     = var.control_db_server_name
  server_type              = var.control_db_server_type
  image                    = var.server_image
  location                 = var.server_location
  ssh_keys                 = [hcloud_ssh_key.admin.id]
  firewall_ids             = [hcloud_firewall.control.id]
  backups                  = false
  delete_protection        = true
  rebuild_protection       = true
  keep_disk                = true
  shutdown_before_deletion = true
  labels                   = local.common_labels

  public_net {
    ipv4_enabled = true
    ipv4         = hcloud_primary_ip.control_db.id
    ipv6_enabled = false
  }

  network {
    subnet_id = hcloud_network_subnet.alpha.id
    ip        = var.control_db_private_ip
    alias_ips = []
  }

  lifecycle {
    prevent_destroy = true
  }
}

# K3s agent pool (add-cloud-node-provisioning). Same location, image, key and
# subnet as the fleet node; its own firewall admits only 443 and administrator
# SSH. The existing server stays the only K3s server.
module "k3s_agents" {
  source = "./modules/k3s-agents"

  nodes                = var.k3s_agent_nodes
  subnet_id            = hcloud_network_subnet.alpha.id
  subnet_cidr          = var.private_subnet_cidr
  reserved_private_ips = [var.private_node_ip, var.control_db_private_ip]
  location             = var.server_location
  image                = var.server_image
  ssh_key_ids          = [hcloud_ssh_key.admin.id]
  admin_ssh_cidrs      = var.admin_ssh_cidrs
  labels               = local.common_labels
}
