resource "hcloud_firewall" "alpha" {
  name   = "exomem-alpha"
  labels = local.common_labels

  rule {
    description = "Restricted administrator SSH"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = var.admin_ssh_cidrs
  }

  rule {
    description = "Public Cloud MCP TLS terminated on the fleet node"
    direction   = "in"
    protocol    = "tcp"
    port        = "443"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Only SSH and the PgBouncer TLS listener reach the control database server.
# PgBouncer's own auth_hba_file (D12) admits exomem_gateway and
# exomem_cellctl solely from the private network, so opening this port to
# the internet only ever exposes substrate_app and substrate_owner, behind
# verify-full TLS, SCRAM and nftables connection limits. (Not fail2ban:
# that jail (base role) watches sshd auth failures only -- it has no jail
# for the PgBouncer port and never protects it.) SSH itself is separately
# covered by fail2ban's sshd jail, same as every other host.
resource "hcloud_firewall" "control" {
  name   = "exomem-control-db"
  labels = local.common_labels

  rule {
    description = "Restricted administrator SSH"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = var.admin_ssh_cidrs
  }

  rule {
    description = "Public PgBouncer TLS listener"
    direction   = "in"
    protocol    = "tcp"
    port        = tostring(var.pgbouncer_public_port)
    source_ips  = ["0.0.0.0/0", "::/0"]
  }

  lifecycle {
    prevent_destroy = true
  }
}
