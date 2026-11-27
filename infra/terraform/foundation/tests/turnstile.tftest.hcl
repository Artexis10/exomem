# The sign-in widget accepts only its listed hosts. Dropping the sign-in host breaks sign-in.
mock_provider "hcloud" {}
mock_provider "cloudflare" {}
mock_provider "random" {}
mock_provider "local" {}

override_resource {
  target          = hcloud_primary_ip.node
  override_during = plan
  values          = { ip_address = "192.0.2.20" }
}

# A mock provider cannot import; the override stands in for the adopted widget.
override_resource {
  target          = cloudflare_turnstile_widget.signin
  override_during = plan
  values          = { sitekey = "mock-sitekey" }
}

variables {
  shared_control        = jsondecode(file("tests/shared_control.fixture.json"))
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

run "signin_widget_keeps_the_app_hosts" {
  command = plan

  assert {
    condition     = contains(cloudflare_turnstile_widget.signin.domains, "substratesystems.io")
    error_message = "The sign-in Turnstile widget must keep substratesystems.io, or sign-in fails there."
  }
}
