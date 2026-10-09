# Mocked providers prove consumer validation without credentials or live writes.
mock_provider "hcloud" {}
mock_provider "cloudflare" {}
mock_provider "random" {}
mock_provider "local" {}

variables {
  hcloud_token          = "synthetic-provider-token"
  cloudflare_api_token  = "synthetic-provider-token"
  cloudflare_account_id = "synthetic-account"
  cloudflare_zone_id    = "synthetic-zone"
  control_hostname      = "control.example.test"
  transfer_hostname     = "transfer.example.test"
  ssh_public_key        = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGenericSyntheticKey"
  shared_control        = jsondecode(file("tests/shared_control.fixture.json"))
}

run "published_dependency_preserves_consumer_coordinates" {
  command = plan
  assert {
    condition = (
      output.control_db_server_id == "4242" &&
      output.control_db_server_ipv4 == "192.0.2.20" &&
      output.control_db_private_ip == "10.50.1.20" &&
      output.database_hostname == "db.example.test"
    )
    error_message = "Exomem must use the published shared database coordinates."
  }
}

run "published_fleet_dependency_carries_the_database_client_address" {
  command = plan
  override_resource {
    target          = hcloud_primary_ip.node
    override_during = plan
    values          = { ip_address = "198.51.100.7" }
  }
  assert {
    condition = (
      output.fleet_dependency.schema_version == 2 &&
      output.fleet_dependency.database_client_ipv4_cidr == "198.51.100.7/32"
    )
    error_message = "Substrate-infra admits the database only from the published schema 2 server-node /32."
  }
}

run "incompatible_version_refuses_deployment" {
  command = plan
  variables {
    shared_control = merge(var.shared_control, { schema_version = 2 })
  }
  expect_failures = [var.shared_control]
}
