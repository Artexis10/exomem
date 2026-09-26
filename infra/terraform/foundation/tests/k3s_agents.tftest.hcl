# Offline wiring contract for the foundation root's K3s agent pool. Every
# provider is mocked, so this suite needs no credentials and reaches no API.

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

run "default_has_no_agents" {
  command = plan

  assert {
    condition     = length(module.k3s_agents.nodes) == 0
    error_message = "The foundation must create no agents by default."
  }
}

run "one_variable_entry_adds_one_agent_in_the_fleet_location" {
  command = plan

  variables {
    k3s_agent_nodes = {
      "01" = { private_ip = "10.50.1.31", server_type = "cpx42" }
    }
  }

  assert {
    condition     = output.k3s_agent_nodes["01"].private_ip == "10.50.1.31" && output.k3s_agent_nodes["01"].name == "exomem-agent-01"
    error_message = "The root output must carry each agent's name and private address."
  }
}

