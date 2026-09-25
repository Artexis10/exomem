## Why

Exomem Cloud runs on one Hetzner server. Hetzner caps how many volumes one server can attach, and every cell owns one volume, so that server holds a fixed number of cells. cellctl already publishes per-node capacity from what Kubernetes observes (`adopt-exomem-cloud-plain-cells` D9), and admission already counts against the sum. What is missing is the node itself: D9 names a K3s agent role and join configuration as a bounded IaC task and leaves it out of scope. Until it exists, the fleet cannot grow past one server, and a public launch cannot outgrow the first full node.

The existing server is also not ready to accept a second node. Its host firewall admits only administrator SSH and the pod and service networks. That blocks the K3s API, the Flannel VXLAN overlay and the kubelet metrics port between nodes. And a joining node could today only present the server token. That token also encrypts the cluster's bootstrap data, so no machine other than the server should hold it.

## What Changes

- **One variable adds or removes a node.** A new `k3s_agent_nodes` map in the foundation root creates one agent server per entry. Each server gets the declared private address on the existing private subnet, the shared agent firewall and identifying labels. It is placed in the same location as the existing node, because volumes attach only within a location. Removing the entry removes that server and nothing else.
- **Agent firewall.** The agent firewall opens TCP 443 to the internet, as requested, and SSH only to the administrator CIDRs. It opens no other public port.
  - SSH is a deliberate addition to "443 only": Ansible reaches each agent directly, and the base hardening forbids TCP forwarding, so the server cannot act as a jump host.
  - Cluster traffic uses the private network, which Hetzner firewalls do not filter. The host firewall filters it instead.
- **Ansible agent join.** The `k3s` role gains an agent mode. It runs in a new play over a `k3s_agents` inventory group, a child of `hosted_nodes`, after the server play, which now excludes agents.
  - An agent gets the same `base` hardening as the existing node, the same pinned and checksum-verified K3s binary, the same kernel settings and kubelet limits, and a node label.
  - It joins over the private network with a dedicated, CA-pinned agent token.
  - The play waits until the node is Ready and every peer's host firewall admits it.
  - When the Hetzner CSI driver is installed, the play also waits until the node's CSINode publishes a volume-attachment limit. That limit is what cellctl reads, so the node is not counted before it can take cells.
  - Rerunning the play changes nothing.
- **Dedicated agent token.** The server configuration gains `agent-token` once any agent is inventoried. Agents receive only that token, in the secure format that pins the cluster CA. They never receive the server token or the etcd snapshot credentials.
- **Inter-node host firewall.** Every K3s node admits the K3s ports it needs, only from the other K3s nodes' declared private addresses and only on the private interface: 6443/tcp on the server, 8472/udp and 10250/tcp on every node. The rules converge to the inventory on every run.
- **Ansible agent drain and removal.** A new `remove-agent.yml` playbook removes one agent. It is idempotent and refuses to act on the server.
  - **Refusals.** It refuses while any cell maintenance hold is present. It also refuses unless every cell volume fits the remaining nodes' slots, counted by cellctl's own formula.
  - **Drain and stop.** It cordons the node, drains it without force and stops K3s on it.
  - **Delete only after a confirmed stop.** Only once the stop is confirmed does it taint the node out of service, delete the Kubernetes node and check that the node stays gone.
  - **Firewall.** It converges the remaining nodes' host-firewall rules without the removed node.
- **Ingress stays on the server.** The platform Traefik is pinned to the control-plane node, the node that carries the public address the ingress DNS record targets, and rolls without a surge pod. A new agent can then never pull the public entrypoint away from that record, and a single `hostPort` replica can always roll.
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
- **Helm:** `traefik.nodeSelector` and a no-surge `traefik.updateStrategy` in the platform chart.
- **Secrets:** one new SOPS-sourced Ansible variable, `k3s_agent_token`.
- **Tests:** contract tests, the Terraform test suites, and a gated role test that runs the real playbooks against systemd containers.
- **Not changed:** cellctl, which is in review in #1368. Its CSINode-based capacity already covers any number of nodes.
  - The plain-cells D8 clause asking backup Jobs for node affinity is unnecessary; design N9 explains why. Its removal belongs to that change, because #1368 edits the adjacent lines.
  - Nothing in this change runs Terraform apply or touches real servers.
