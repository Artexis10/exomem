resource "random_bytes" "tunnel_secret" {
  length = 32
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "alpha" {
  account_id    = var.cloudflare_account_id
  name          = "exomem-private-alpha"
  config_src    = "cloudflare"
  tunnel_secret = random_bytes.tunnel_secret.base64

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "alpha" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.alpha.id
  source     = "cloudflare"

  config = {
    ingress = [
      {
        hostname = var.control_hostname
        service  = "http://exomem-platform-traefik.exomem-platform.svc.cluster.local:80"
        origin_request = {
          http_host_header = var.control_hostname
        }
      },
      {
        hostname = var.transfer_hostname
        service  = "http://exomem-platform-traefik.exomem-platform.svc.cluster.local:80"
        origin_request = {
          http_host_header = var.transfer_hostname
        }
      },
      {
        service = "http_status:404"
      }
    ]
  }
}

resource "cloudflare_dns_record" "control" {
  zone_id = var.cloudflare_zone_id
  name    = var.control_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.alpha.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "Exomem hosted Access-protected control ingress"
}

resource "cloudflare_dns_record" "transfer" {
  zone_id = var.cloudflare_zone_id
  name    = var.transfer_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.alpha.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "Exomem hosted direct browser transfer ingress"
}

resource "cloudflare_zero_trust_access_service_token" "substrate" {
  account_id = var.cloudflare_account_id
  name       = "exomem-substrate-control"
  duration   = "8760h"
}

resource "cloudflare_zero_trust_access_policy" "substrate" {
  account_id = var.cloudflare_account_id
  name       = "Exomem Substrate service authentication"
  decision   = "non_identity"
  include = [
    {
      service_token = {
        token_id = cloudflare_zero_trust_access_service_token.substrate.id
      }
    }
  ]
}

resource "cloudflare_zero_trust_access_application" "control" {
  account_id                 = var.cloudflare_account_id
  name                       = "Exomem hosted control"
  domain                     = var.control_hostname
  type                       = "self_hosted"
  session_duration           = "24h"
  app_launcher_visible       = false
  http_only_cookie_attribute = true
  options_preflight_bypass   = false
  service_auth_401_redirect  = true
  policies = [
    {
      id = cloudflare_zero_trust_access_policy.substrate.id
    }
  ]
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "alpha" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.alpha.id
}
