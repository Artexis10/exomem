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
