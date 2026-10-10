output "server_id" {
  description = "Opaque Hetzner server identifier."
  value       = hcloud_server.alpha.id
}

output "server_ipv4" {
  description = "Stable primary IPv4 of the K3s server: public ingress, break-glass SSH and the shared database's allowlisted client address."
  value       = hcloud_primary_ip.node.ip_address
}

output "private_node_ip" {
  description = "Stable private-network node address used by generated Ansible inventory."
  value       = var.private_node_ip
}

output "gateway_hostname" {
  description = "Optional gateway origin consumed by the Substrate-owned exact-path rewrite."
  value       = var.gateway_hostname
}

output "control_hostname" {
  description = "Access-protected public control hostname used by the platform release."
  value       = var.control_hostname
}

output "transfer_hostname" {
  description = "Direct public transfer hostname used by the platform release."
  value       = var.transfer_hostname
}

output "tunnel_id" {
  description = "Opaque Cloudflare Tunnel identifier."
  value       = cloudflare_zero_trust_tunnel_cloudflared.alpha.id
}

output "control_access_audience" {
  description = "Audience expected on Access-authenticated control requests."
  value       = cloudflare_zero_trust_access_application.control.aud
}

output "cloudflare_tunnel_token" {
  description = "Sensitive one-destination handoff value for the K3s cloudflared Secret."
  value       = data.cloudflare_zero_trust_tunnel_cloudflared_token.alpha.token
  sensitive   = true
}

output "access_service_token_client_id" {
  description = "Sensitive Access client identifier handed only to Substrate/Vercel."
  value       = cloudflare_zero_trust_access_service_token.substrate.client_id
  sensitive   = true
}

output "access_service_token_client_secret" {
  description = "Sensitive Access client secret handed only to Substrate/Vercel."
  value       = cloudflare_zero_trust_access_service_token.substrate.client_secret
  sensitive   = true
}

output "estimated_fixed_monthly_eur_ex_vat" {
  # 8.49 (cx33, fsn1) + 0.50 (primary IPv4), from the Hetzner pricing API.
  # Hetzner reverse-charges VAT to the Estonian entity, so net equals gross.
  # Null for any other server type: this output carries no price for it.
  description = "CX33 plus primary IPv4 estimate, null for other server types; excludes usage-priced B2 and tenant volumes."
  value       = var.server_type == "cx33" ? 8.99 : null
}

output "control_db_server_id" {
  description = "Opaque Hetzner control database server identifier."
  value       = var.shared_control.server_id
}

output "control_db_server_ipv4" {
  description = "Stable primary IPv4 of the public PgBouncer listener and of direct PostgreSQL for the K3s server's /32."
  value       = var.shared_control.public_ipv4
}

output "database_hostname" {
  description = "DNS-only public hostname carrying the control database's PgBouncer TLS listener."
  value       = var.shared_control.hostname
}

output "fleet_dependency" {
  description = "Non-secret versioned database-client coordinates consumed by the shared infrastructure owner."
  value = {
    schema_version = 3
    # The shared database admits direct PostgreSQL only from this address (decision 10).
    database_client_ipv4_cidr = "${hcloud_primary_ip.node.ip_address}/32"
  }
}

output "k3s_agent_nodes" {
  description = "Non-sensitive K3s agent coordinates (name, IPv4, private IP) consumed by the generated Ansible inventory."
  value = {
    for key, node in module.k3s_agents.nodes : key => {
      name              = node.name
      ipv4              = node.ipv4
      private_ip        = node.private_ip
      dedicated_cell_id = node.dedicated_cell_id
    }
  }
}

output "vswitch" {
  description = "The optional vSwitch subnet dedicated Hetzner servers join on, consumed by the generated Ansible inventory; null when off."
  value = var.vswitch == null ? null : {
    vlan_id      = var.vswitch.vlan_id
    subnet_cidr  = hcloud_network_subnet.vswitch[0].ip_range
    gateway      = hcloud_network_subnet.vswitch[0].gateway
    network_cidr = var.private_network_cidr
  }
}
