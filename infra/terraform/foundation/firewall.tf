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

  lifecycle {
    prevent_destroy = true
  }
}
