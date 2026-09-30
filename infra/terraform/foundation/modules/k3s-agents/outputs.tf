output "nodes" {
  description = "Non-sensitive agent coordinates consumed by the generated Ansible inventory."
  value = {
    for key, server in hcloud_server.agent : key => {
      name        = server.name
      server_id   = server.id
      ipv4        = server.ipv4_address
      private_ip  = var.nodes[key].private_ip
      server_type = server.server_type
    }
  }
}

output "firewall_id" {
  description = "Opaque Hetzner firewall identifier shared by every agent."
  value       = hcloud_firewall.agents.id
}
