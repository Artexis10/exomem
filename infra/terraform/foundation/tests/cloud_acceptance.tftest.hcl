# No live state or credentials: protect the experiment's isolation and cleanup.
mock_provider "hcloud" {
  mock_resource "hcloud_server" {
    defaults = { network = [] }
  }
}
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

run "ordinary_foundation_has_no_acceptance_host" {
  command = plan

  assert {
    condition = (
      length(hcloud_server.acceptance) == 0 &&
      length(hcloud_primary_ip.acceptance) == 0 &&
      length(hcloud_firewall.acceptance) == 0
    )
    error_message = "Ordinary foundation deployment must not provision paid acceptance resources."
  }
}

run "acceptance_host_is_disposable_and_outside_the_fleet" {
  command = plan

  variables {
    cloud_acceptance_enabled = true
  }

  assert {
    condition = (
      length(hcloud_server.acceptance[0].network) == 0 &&
      length(output.k3s_agent_nodes) == 0 &&
      hcloud_server.acceptance[0].server_type == "ccx23"
    )
    error_message = "The dedicated acceptance host must never join a production private network or agent inventory."
  }

  assert {
    condition = length(hcloud_firewall.acceptance[0].rule) == 1 && alltrue([
      for rule in hcloud_firewall.acceptance[0].rule :
      rule.direction == "in" && rule.protocol == "tcp" && rule.port == "22" &&
      toset(rule.source_ips) == toset(var.admin_ssh_cidrs)
    ])
    error_message = "The acceptance host must admit only restricted administrator SSH."
  }

  assert {
    condition = (
      !hcloud_server.acceptance[0].backups &&
      !hcloud_server.acceptance[0].delete_protection &&
      !hcloud_server.acceptance[0].rebuild_protection &&
      !hcloud_primary_ip.acceptance[0].auto_delete
    )
    error_message = "Disposable resources must allow reviewed cleanup without implicit IP deletion corrupting Terraform state."
  }
}
