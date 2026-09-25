# Offline contract for the K3s agent pool. The hcloud provider is mocked, so
# this suite needs no credentials and never reaches Hetzner.

mock_provider "hcloud" {}

variables {
  subnet_id            = "1234-10.50.1.0/24"
  subnet_cidr          = "10.50.1.0/24"
  location             = "fsn1"
  image                = "ubuntu-24.04"
  ssh_key_ids          = ["4242"]
  admin_ssh_cidrs      = ["192.0.2.10/32"]
  reserved_private_ips = ["10.50.1.10", "10.50.1.20"]
  labels = {
    application = "exomem-hosted"
    environment = "private-alpha"
    managed_by  = "terraform"
  }
  nodes = {}
}

run "empty_pool_creates_no_servers_but_keeps_one_firewall" {
  command = plan

  assert {
    condition     = length(hcloud_server.agent) == 0
    error_message = "An empty agent map must create no servers."
  }

  assert {
    condition     = length(output.nodes) == 0
    error_message = "An empty agent map must output no nodes."
  }
}

run "two_agents_get_declared_addresses_location_firewall_and_labels" {
  command = plan

  variables {
    nodes = {
      "01" = { private_ip = "10.50.1.31", server_type = "cpx42" }
      "02" = { private_ip = "10.50.1.32", server_type = "ccx23" }
    }
  }

  assert {
    condition     = length(hcloud_server.agent) == 2
    error_message = "Each map entry must create exactly one server."
  }

  assert {
    condition     = hcloud_server.agent["01"].name == "exomem-agent-01" && hcloud_server.agent["02"].name == "exomem-agent-02"
    error_message = "Server names must be the fixed prefix plus the map key."
  }

  assert {
    condition = alltrue([
      for key, server in hcloud_server.agent :
      server.location == "fsn1" && server.image == "ubuntu-24.04"
    ])
    error_message = "Every agent must share the fleet location and pinned image."
  }

  assert {
    condition     = one(hcloud_server.agent["01"].network).ip == "10.50.1.31" && one(hcloud_server.agent["02"].network).ip == "10.50.1.32"
    error_message = "Each agent must sit at its declared private address."
  }

  assert {
    condition = alltrue([
      for key, server in hcloud_server.agent :
      one(server.network).subnet_id == "1234-10.50.1.0/24"
    ])
    error_message = "Every agent must attach to the existing private subnet."
  }

  assert {
    condition     = hcloud_server.agent["01"].server_type == "cpx42" && hcloud_server.agent["02"].server_type == "ccx23"
    error_message = "Each agent must use its declared server type."
  }

  assert {
    condition = alltrue([
      for key, server in hcloud_server.agent :
      server.labels["role"] == "k3s-agent" &&
      server.labels["node"] == key &&
      server.labels["application"] == "exomem-hosted" &&
      server.labels["managed_by"] == "terraform"
    ])
    error_message = "Agents must carry the common labels plus role and node."
  }

  assert {
    condition = alltrue([
      for key, server in hcloud_server.agent :
      server.delete_protection == false &&
      server.rebuild_protection == false &&
      server.shutdown_before_deletion == true &&
      server.backups == false
    ])
    error_message = "Agents are disposable: removal is one plan, guarded by the saved-plan inspector."
  }

  assert {
    condition = alltrue([
      for key, server in hcloud_server.agent :
      one(server.public_net).ipv4_enabled == true && one(server.public_net).ipv6_enabled == false
    ])
    error_message = "Agents need public IPv4 egress and no IPv6."
  }

  assert {
    condition = alltrue([
      for key, server in hcloud_server.agent :
      length(server.ssh_keys) == 1 && contains(server.ssh_keys, "4242")
    ])
    error_message = "Agents must use the existing administrator key."
  }

  assert {
    condition     = output.nodes["01"].private_ip == "10.50.1.31" && output.nodes["01"].name == "exomem-agent-01"
    error_message = "The node output must carry the name and private address."
  }
}

run "agent_firewall_admits_exactly_admin_ssh_and_public_443" {
  command = plan

  variables {
    nodes = {
      "01" = { private_ip = "10.50.1.31", server_type = "cpx42" }
    }
  }

  assert {
    condition     = length(hcloud_firewall.agents.rule) == 2
    error_message = "The agent firewall must have exactly two rules."
  }

  assert {
    condition = alltrue([
      for rule in hcloud_firewall.agents.rule : rule.direction == "in" && rule.protocol == "tcp"
    ])
    error_message = "Only inbound TCP rules are allowed."
  }

  assert {
    condition = length([
      for rule in hcloud_firewall.agents.rule : rule
      if rule.port == "22" && toset(rule.source_ips) == toset(["192.0.2.10/32"])
    ]) == 1
    error_message = "SSH must be admitted only from the administrator CIDRs."
  }

  assert {
    condition = length([
      for rule in hcloud_firewall.agents.rule : rule
      if rule.port == "443" && toset(rule.source_ips) == toset(["0.0.0.0/0", "::/0"])
    ]) == 1
    error_message = "443 must be the only public port."
  }

  assert {
    condition     = hcloud_firewall.agents.labels["role"] == "k3s-agent"
    error_message = "The agent firewall must be labelled for the agent pool."
  }
}

run "removing_one_entry_keeps_the_other_keyed_address" {
  command = plan

  variables {
    nodes = {
      "02" = { private_ip = "10.50.1.32", server_type = "ccx23" }
    }
  }

  assert {
    condition     = keys(hcloud_server.agent) == ["02"]
    error_message = "Servers must be keyed by map key so removing one never re-indexes another."
  }

  assert {
    condition     = one(hcloud_server.agent["02"].network).ip == "10.50.1.32"
    error_message = "The remaining agent must keep its address."
  }
}

run "rejects_an_address_outside_the_subnet" {
  command = plan

  variables {
    nodes = {
      "01" = { private_ip = "10.50.2.31", server_type = "cpx42" }
    }
  }

  expect_failures = [var.nodes]
}

run "rejects_the_subnet_network_gateway_and_broadcast_addresses" {
  command = plan

  variables {
    nodes = {
      "01" = { private_ip = "10.50.1.0", server_type = "cpx42" }
      "02" = { private_ip = "10.50.1.1", server_type = "cpx42" }
      "03" = { private_ip = "10.50.1.255", server_type = "cpx42" }
    }
  }

  expect_failures = [var.nodes]
}

run "rejects_a_duplicate_address" {
  command = plan

  variables {
    nodes = {
      "01" = { private_ip = "10.50.1.31", server_type = "cpx42" }
      "02" = { private_ip = "10.50.1.31", server_type = "cpx42" }
    }
  }

  expect_failures = [var.nodes]
}

run "rejects_the_fleet_server_address" {
  command = plan

  variables {
    nodes = {
      "01" = { private_ip = "10.50.1.10", server_type = "cpx42" }
    }
  }

  expect_failures = [var.nodes]
}

run "rejects_the_control_database_address" {
  command = plan

  variables {
    nodes = {
      "01" = { private_ip = "10.50.1.20", server_type = "cpx42" }
    }
  }

  expect_failures = [var.nodes]
}

run "rejects_an_arm_server_type" {
  command = plan

  variables {
    nodes = {
      "01" = { private_ip = "10.50.1.31", server_type = "cax21" }
    }
  }

  expect_failures = [var.nodes]
}

run "rejects_a_server_type_too_small_for_its_slots" {
  command = plan

  variables {
    nodes = {
      "01" = { private_ip = "10.50.1.31", server_type = "cpx11" }
    }
  }

  expect_failures = [var.nodes]
}

run "rejects_a_key_that_is_not_a_dns_label" {
  command = plan

  variables {
    nodes = {
      "Agent_01" = { private_ip = "10.50.1.31", server_type = "cpx42" }
    }
  }

  expect_failures = [var.nodes]
}

run "rejects_a_global_admin_ssh_cidr" {
  command = plan

  variables {
    admin_ssh_cidrs = ["0.0.0.0/0"]
  }

  expect_failures = [var.admin_ssh_cidrs]
}
