# Offline contract for the fleet server type: the existing x86 cx33, or an
# Arm64 (Hetzner CAX) server for a cluster that runs on Arm64. Every provider
# is mocked, so this suite needs no credentials and reaches no API.

mock_provider "hcloud" {}
mock_provider "cloudflare" {}
mock_provider "random" {}
mock_provider "local" {}

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

run "arm64_fleet_server_is_accepted" {
  command = plan

  variables {
    server_type = "cax21"
  }

  assert {
    condition     = hcloud_server.alpha.server_type == "cax21"
    error_message = "An Arm64 fleet server must plan with its CAX type."
  }

  assert {
    condition     = output.estimated_fixed_monthly_eur_ex_vat == null
    error_message = "The cost estimate carries only the cx33 price, so another type must report no estimate."
  }
}

run "x86_type_other_than_cx33_is_rejected" {
  command = plan

  variables {
    server_type = "cpx42"
  }

  expect_failures = [var.server_type]
}
