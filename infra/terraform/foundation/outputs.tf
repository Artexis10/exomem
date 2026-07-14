output "server_id" {
  description = "Opaque Hetzner server identifier."
  value       = hcloud_server.alpha.id
}

output "server_ipv4" {
  description = "Stable primary IPv4 used only for restricted SSH administration."
  value       = hcloud_primary_ip.node.ip_address
}

output "private_node_ip" {
  description = "Stable private-network node address used by generated Ansible inventory."
  value       = var.private_node_ip
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
  description = "CX33 plus primary IPv4 estimate; excludes usage-priced B2 and tenant volumes."
  value       = 8.99
}
