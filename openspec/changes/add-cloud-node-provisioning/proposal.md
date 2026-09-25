## Why

Exomem Cloud runs on one Hetzner server. Hetzner caps how many volumes one server can attach, and every cell owns one volume, so that server holds a fixed number of cells. cellctl already publishes per-node capacity from what Kubernetes observes (`adopt-exomem-cloud-plain-cells` D9), and admission already counts against the sum. What is missing is the node itself: D9 names a K3s agent role and join configuration as a bounded IaC task and leaves it out of scope. Until it exists, the fleet cannot grow past one server, and a public launch cannot outgrow the first full node.

The existing server is also not ready to accept a second node. Its host firewall admits only administrator SSH and the pod and service networks. That blocks the K3s API, the Flannel VXLAN overlay and the kubelet metrics port between nodes. And a joining node could today only present the server token. That token also encrypts the cluster's bootstrap data, so no machine other than the server should hold it.

## What Changes

- **One variable adds or removes a node.** A new `k3s_agent_nodes` map in the foundation root creates one agent server per entry. Each server gets the declared private address on the existing private subnet, the shared agent firewall and identifying labels. It is placed in the same location as the existing node, because volumes attach only within a location. Removing the entry removes that server and nothing else.
- **Agent firewall.** The agent firewall opens TCP 443 to the internet and SSH only to the administrator CIDRs. It opens no other public port. Cluster traffic uses the private network, which Hetzner firewalls do not filter. The host firewall filters it instead.
- **Ansible agent join.** The `k3s` role gains an agent mode, run by a new play over a `k3s_agents` inventory group, after the server play. An agent gets the same `base` hardening as the existing node, the same pinned and checksum-verified K3s binary, the same kernel settings and kubelet limits, and a node label. It joins over the private network with a dedicated agent token. The play waits until the node is Ready and until its CSINode publishes a volume-attachment limit. That limit is what cellctl reads, so the node is not counted as capacity before it can take cells. Rerunning the play changes nothing.
- **Dedicated agent token.** The server configuration gains `agent-token`. Agents receive only that token, never the server token or the etcd snapshot credentials.
- **Inter-node host firewall.** Every K3s node admits the K3s ports it needs, and only from the other K3s nodes' declared private addresses: 6443/tcp on the server, 8472/udp and 10250/tcp on every node.
- **Ansible agent drain and removal.** A new `remove-agent.yml` playbook removes one agent. It first checks that the other schedulable nodes have enough free volume-attachment slots for the cells on it. Then it cordons the node, drains it without force, stops K3s on it, deletes the Kubernetes node and removes that node's host-firewall rules from the remaining nodes. It is idempotent and refuses to act on the server.
- **Ingress stays on the server.** The platform Traefik is pinned to the control-plane node, the node that carries the public address the ingress DNS record targets. A new agent can then never pull the public entrypoint away from that record.
- **Inventory.** The inventory generator emits every agent from a new non-sensitive Terraform output.

## Capabilities

### New Capabilities

- `cloud-node-pool`: adding and removing Exomem Cloud K3s agent nodes through IaC, with the hardening, join, capacity-publication, drain and firewall guarantees above.

### Modified Capabilities

None. `cloud-cell` belongs to the active change `adopt-exomem-cloud-plain-cells`, and its capacity requirement already covers publication. This change supplies the nodes that requirement counts.

## Impact

- **Terraform:** a new `infra/terraform/foundation/modules/k3s-agents` module, with its own mocked-provider `terraform test` suite and lock file. The foundation root gets the `k3s_agent_nodes` variable, the module call and a `k3s_agent_nodes` output.
- **Ansible:** agent mode in the `k3s` role, `agent-token` and inter-node UFW rules on the server, an agent play in `site.yml`, a new `remove-agent.yml` playbook, and the inventory example.
- **Scripts:** `generate_ansible_inventory.py` emits `k3s_agents`. `validate.sh` runs the module's format, validate and test steps.
- **Helm:** one `traefik.nodeSelector` value in the platform chart.
- **Secrets:** one new SOPS-sourced Ansible variable, `k3s_agent_token`.
- **Tests:** contract tests, the Terraform test suite, and a gated containerised K3s join and drain test.
- **Not changed:** cellctl, which is in review in #1368. Its CSINode-based capacity already covers any number of nodes. Nothing in this change runs Terraform apply or touches real servers.
