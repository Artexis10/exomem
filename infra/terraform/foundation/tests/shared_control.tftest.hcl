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
  shared_control = {
    schema_version = 1
    owner          = "substrate-infra"
    server_id      = "4242"
    public_ipv4    = "192.0.2.20"
    private_ipv4   = "10.50.1.20"
    hostname       = "db.example.test"
    direct_port    = 5432
    pooled_port    = 6432
    sslmode        = "verify-full"
  }
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

run "incompatible_version_refuses_deployment" {
  command = plan
  variables {
    shared_control = merge(var.shared_control, { schema_version = 2 })
  }
  expect_failures = [var.shared_control]
}
