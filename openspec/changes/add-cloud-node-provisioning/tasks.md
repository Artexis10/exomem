## 1. Specification

- [ ] 1.1 Get an independent critic review of the proposal, design and spec, and resolve every blocking finding

## 2. Terraform (N1–N3)

- [ ] 2.1 Write red-first `terraform test` cases for `modules/k3s-agents`, with `mock_provider "hcloud"`:
  - an empty map creates nothing;
  - two entries create two servers with the declared addresses, the location, the firewall and the labels;
  - the firewall has exactly the 22 (administrator CIDRs) and 443 inbound rules;
  - each invalid input is rejected: an address outside the subnet, a duplicate, a reserved address, a non-x86 type and a bad key.
- [ ] 2.2 Implement the module and its lock file, and wire `k3s_agent_nodes` (default `{}`), the module call and a non-sensitive `k3s_agent_nodes` output into the foundation root
- [ ] 2.3 Add the module to `validate.sh`, covering `fmt -check`, `init -backend=false`, `validate` and `test`

## 3. Ansible (N4–N6)

- [ ] 3.1 Write red-first contract tests:
  - the agent template carries no server token, `cluster-init` or etcd option;
  - the server template gains `agent-token`;
  - the inter-node UFW rules name node addresses, never the subnet;
  - `site.yml` runs the server play before the agent play, and the agent play applies `base`;
  - the removal playbook refuses servers, preflights capacity, cordons, drains without `--force`, stops the agent, deletes the node and revokes the rules.
- [ ] 3.2 Implement the `k3s` role's agent mode, the server `agent-token`, and the inter-node UFW rules
- [ ] 3.3 Add the agent play to `site.yml`, `remove-agent.yml`, and the inventory and group-vars examples
- [ ] 3.4 Emit `k3s_agents` from `generate_ansible_inventory.py`, and test it
- [ ] 3.5 Register the `k3s_agent_token` secret destination beside the existing K3s token

## 4. Ingress (N7)

- [ ] 4.1 Pin `traefik.nodeSelector` to the control-plane label, with a chart contract test

## 5. Verification (N8)

- [ ] 5.1 Add a gated containerised K3s test (`RUN_K3S_AGENT_JOIN_TEST=1`) on the pinned `rancher/k3s` image:
  - the role's rendered server and agent templates join an agent with the agent token alone;
  - the label is set;
  - a wrong token is refused;
  - the removal playbook's drain and delete argv remove the node.
- [ ] 5.2 Pass `terraform fmt -check`, `validate` and `test`, `ansible-lint --profile production`, `ansible-playbook --syntax-check` on both playbooks, the hosted contract tests, the privacy gate and strict OpenSpec validation
- [ ] 5.3 Add an operator runbook covering adding a node, removing a node and the one-time server restart for `agent-token`
- [ ] 5.4 Get an independent code review of the diff, and resolve every blocking finding
