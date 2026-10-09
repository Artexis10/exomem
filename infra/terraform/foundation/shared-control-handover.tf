# Preserve deployed resources while the coordinated native state transfer changes their owner.
removed {
  from = hcloud_server.control
  lifecycle { destroy = false }
}

removed {
  from = hcloud_primary_ip.control_db
  lifecycle { destroy = false }
}

removed {
  from = hcloud_firewall.control
  lifecycle { destroy = false }
}

removed {
  from = cloudflare_dns_record.database
  lifecycle { destroy = false }
}
