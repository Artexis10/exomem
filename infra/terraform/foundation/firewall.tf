resource "hcloud_firewall" "alpha" {
  name   = "exomem-alpha"
  labels = local.common_labels

  # Administration runs over the company NetBird, so public SSH is normally
  # closed. A non-empty set opens a temporary break-glass window.
  dynamic "rule" {
    for_each = length(var.admin_ssh_cidrs) > 0 ? [var.admin_ssh_cidrs] : []

    content {
      description = "Restricted administrator SSH"
      direction   = "in"
      protocol    = "tcp"
      port        = "22"
      source_ips  = rule.value
    }
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

# Only the PgBouncer TLS listener, and SSH during a break-glass window, reach
# the control database server.
# PgBouncer's own auth_hba_file (D12) admits exomem_gateway and
# exomem_cellctl solely from the private network, so opening this port to
# the internet only ever exposes substrate_app and substrate_owner, behind
# verify-full TLS, SCRAM and nftables connection limits. (Not fail2ban:
# that jail (base role) watches sshd auth failures only -- it has no jail
# for the PgBouncer port and never protects it.) SSH itself is separately
# covered by fail2ban's sshd jail, same as every other host.
resource "hcloud_firewall" "control" {
  name   = var.control_db_server_name
  labels = local.common_labels

  # Administration runs over the company NetBird, so public SSH is normally
  # closed. A non-empty set opens a temporary break-glass window.
  dynamic "rule" {
    for_each = length(var.admin_ssh_cidrs) > 0 ? [var.admin_ssh_cidrs] : []

    content {
      description = "Restricted administrator SSH"
      direction   = "in"
      protocol    = "tcp"
      port        = "22"
      source_ips  = rule.value
    }
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
