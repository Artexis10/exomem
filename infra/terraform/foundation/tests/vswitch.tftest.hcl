# Offline contract for the optional vSwitch subnet. Every provider is mocked,
# so this suite needs no credentials and reaches no API.

mock_provider "hcloud" {}
mock_provider "cloudflare" {}
mock_provider "random" {}
mock_provider "local" {}

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

run "default_creates_no_vswitch_subnet" {
  command = plan

  assert {
    condition     = length(hcloud_network_subnet.vswitch) == 0 && output.vswitch == null
    error_message = "The foundation must create no vSwitch subnet until a vSwitch is named."
  }
}

run "a_named_vswitch_adds_one_coupled_subnet" {
  command = plan

  variables {
    vswitch = { id = 54321, vlan_id = 4000, subnet_cidr = "10.50.2.0/24" }
  }

  assert {
    condition = (
      length(hcloud_network_subnet.vswitch) == 1 &&
      hcloud_network_subnet.vswitch[0].type == "vswitch" &&
      hcloud_network_subnet.vswitch[0].vswitch_id == 54321 &&
      hcloud_network_subnet.vswitch[0].ip_range == "10.50.2.0/24"
    )
    error_message = "A named vSwitch must add exactly one vswitch subnet on the existing network."
  }
}

run "a_subnet_outside_the_network_is_refused" {
  command = plan

  variables {
    vswitch = { id = 54321, vlan_id = 4000, subnet_cidr = "10.60.2.0/24" }
  }

  expect_failures = [var.vswitch]
}
