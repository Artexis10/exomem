# Exercise D11 with mocked providers: no credentials, API calls or live state.
mock_provider "hcloud" {}
mock_provider "cloudflare" {}
mock_provider "random" {}
mock_provider "local" {}

override_resource {
  target          = hcloud_primary_ip.node
  override_during = plan
  values          = { ip_address = "192.0.2.20" }
}

variables {
  hcloud_token          = "mock-only"
  cloudflare_api_token  = "mock-only"
  cloudflare_account_id = "mock-account"
  cloudflare_zone_id    = "mock-zone"
  control_hostname      = "control.example.com"
  transfer_hostname     = "transfer.example.com"
  database_hostname     = "db.example.com"
  admin_ssh_cidrs       = ["192.0.2.10/32"]
  ssh_public_key        = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleOnlyReplaceMe operator"
}

run "disabled_gateway_has_no_dns_record" {
  command = plan

  assert {
    condition     = length(cloudflare_dns_record.gateway) == 0
    error_message = "An empty gateway hostname must create no gateway DNS record."
  }
}

run "gateway_terminates_tls_on_the_fleet_node" {
  command = plan

  variables {
    gateway_hostname = "mcp.example.com"
  }

  assert {
    condition = (
      length(cloudflare_dns_record.gateway) == 1 &&
      cloudflare_dns_record.gateway[0].name == "mcp.example.com" &&
      cloudflare_dns_record.gateway[0].type == "A" &&
      cloudflare_dns_record.gateway[0].content == "192.0.2.20" &&
      cloudflare_dns_record.gateway[0].proxied == false
    )
    error_message = "Cloud MCP must resolve directly to the fleet node without Cloudflare terminating TLS."
  }

  assert {
    condition = (
      length(cloudflare_zero_trust_tunnel_cloudflared_config.alpha.config.ingress) == 3 &&
      cloudflare_zero_trust_tunnel_cloudflared_config.alpha.config.ingress[0].hostname == var.control_hostname &&
      cloudflare_zero_trust_tunnel_cloudflared_config.alpha.config.ingress[1].hostname == var.transfer_hostname &&
      cloudflare_zero_trust_tunnel_cloudflared_config.alpha.config.ingress[2].service == "http_status:404" &&
      cloudflare_dns_record.control.proxied && cloudflare_dns_record.transfer.proxied
    )
    error_message = "The legacy tunnel must retain only control, transfer and its catch-all; Cloud MCP must never enter it."
  }
}

run "fleet_firewall_admits_only_admin_ssh_and_public_tls" {
  command = plan

  assert {
    condition = length(hcloud_firewall.alpha.rule) == 2 && alltrue([
      for rule in hcloud_firewall.alpha.rule : rule.direction == "in" && rule.protocol == "tcp"
    ])
    error_message = "The fleet firewall must have exactly two inbound TCP rules."
  }

  assert {
    condition = length([
      for rule in hcloud_firewall.alpha.rule : rule
      if rule.port == "22" && toset(rule.source_ips) == toset(["192.0.2.10/32"])
    ]) == 1
    error_message = "SSH must remain restricted to administrator CIDRs."
  }

  assert {
    condition = length([
      for rule in hcloud_firewall.alpha.rule : rule
      if rule.port == "443" && toset(rule.source_ips) == toset(["0.0.0.0/0", "::/0"])
    ]) == 1
    error_message = "Only TLS on port 443 may be public; HTTP and the Kubernetes API stay closed."
  }
}
