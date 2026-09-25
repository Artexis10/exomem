## 1. Specification

- [x] 1.1 Get an independent critic review of the proposal, design and spec, and resolve every blocking finding (three rounds: REVISE with 5 blocking, REVISE with 2 blocking, APPROVE with 2 should-fixes, all applied)

## 2. Terraform (N1–N3)

- [ ] 2.1 Write red-first `terraform test` cases for `modules/k3s-agents`, with `mock_provider "hcloud"`:
  - an empty map creates no servers;
  - two entries create two servers with the declared addresses, the location, the firewall and the labels;
  - the firewall has exactly the 22 (administrator CIDRs) and 443 inbound rules;
  - removing one key keeps the other;
  - each invalid input is rejected: an address outside the subnet, the network, gateway or broadcast address, a duplicate, a reserved address, a type off the allow-list, a bad key, and a global administrator CIDR.
- [ ] 2.2 Implement the module and its lock file, and wire `k3s_agent_nodes` (default `{}`), the module call and a non-sensitive `k3s_agent_nodes` output into the foundation root, with a mocked root wiring test
- [ ] 2.3 Add the module to `validate.sh`, covering `init -backend=false`, `validate`, TFLint and `test`, plus the foundation `test`

## 3. Ansible (N4–N6)

- [ ] 3.1 Write red-first contract tests:
  - the server play never matches `k3s_agents`, and agent task and template files never reference the server token or etcd variables;
  - the agent token is presented in the secure format;
  - the server template gains `agent-token` only when set;
  - the inter-node UFW rules name peer addresses on the private interface, never the subnet, and stale rules are pruned;
  - the join verifies peer admission and CSI capacity;
  - the removal playbook guards the target, refuses during holds, preflights with the controller's formula, cordons, drains without `--force`, stops every container, unmounts pod and CSI volume mounts, closes crypt mappings, confirms the stop before deleting, writes the removal marker, taints out-of-service, deletes, re-checks, and converges the firewall.
- [ ] 3.2 Implement the `k3s` role's agent mode, the server `agent-token`, the secure token, and converging inter-node UFW rules
- [ ] 3.3 Add the agent play to `site.yml`, `remove-agent.yml`, and the inventory and group-vars examples
- [ ] 3.4 Emit `k3s_agents` from `generate_ansible_inventory.py`, validated, and test it
- [ ] 3.5 Register the `k3s_agent_token` secret destinations beside the existing K3s token, and document the variable in the secrets runbook

## 4. Ingress (N7)

- [ ] 4.1 Pin `traefik.nodeSelector` to the control-plane label and set a no-surge `updateStrategy`, with a contract test and a render test

## 5. Verification (N8)

- [ ] 5.1 Add the gated role test (`RUN_K3S_NODE_POOL_ROLE_TEST=1`) on systemd Ubuntu 24.04 containers:
  - `site.yml` converges, and a second run reports `changed=0`;
  - a second agent added in a later run joins, and the first agent's peer check still passes;
  - the join uses the secure agent token, the node is Ready and labelled;
  - a wrong token is refused;
  - the preflight refuses;
  - removal completes, and a rerun changes nothing.
- [ ] 5.2 Pass `terraform fmt -check`, `validate` and `test`, `ansible-lint --profile production`, `ansible-playbook --syntax-check` on both playbooks, the hosted contract tests, the privacy gate and strict OpenSpec validation
- [ ] 5.3 Add an operator runbook covering adding a node, removing a node, the one-time server restart for `agent-token`, and the destroy approval
- [ ] 5.4 Raise the removal of the plain-cells D8 node-affinity clause (N9) for PR #1368 in this PR's description
- [ ] 5.5 Get an independent code review of the diff, and resolve every blocking finding
