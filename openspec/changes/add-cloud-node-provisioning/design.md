## Context

- **Topology today.** Terraform declares one fleet server, `hcloud_server.alpha`. It is a `cluster-init` K3s server at `10.50.1.10` on `hcloud_subnet.alpha` (`10.50.1.0/24`). The control database server sits on the same subnet at `10.50.1.20`.
- **Hardening.** The `base` role installs SSH hardening, fail2ban, bounded journald, unattended upgrades and UFW. UFW defaults to deny inbound. It allows SSH from the administrator CIDRs and all traffic from the pod and service CIDRs, and nothing else. The `k3s` role installs a pinned, checksum-verified binary, kernel settings with `protect-kernel-defaults`, kubelet image-GC and log limits, the audit policy and the admission configuration.
- **Capacity.** cellctl (`adopt-exomem-cloud-plain-cells` D9, PR #1368) reads each node's `CSINode` allocatable count for `csi.hetzner.cloud` and its attached VolumeAttachments. From those it publishes `cell_slots` per node. It also zeroes the row of any node that is no longer present. So a node publishes capacity once the hcloud CSI node plugin registers on it, and stops once its Node object is deleted. cellctl needs no change.
- **Placement.** Cells carry no node selector. The scheduler places them, and the Hetzner CSI topology keeps a volume within its location.
- **Ingress.** On `main`, Traefik is ClusterIP behind `cloudflared`, which dials it from inside the cluster, so its placement does not matter. D11 (PR #1368) exposes `websecure` on `hostPort: 443`. After that, the node running Traefik is the public entrypoint.
- **Facts from the K3s documentation** (`k3s-io/docs`, `docs/cli/token.md`, `docs/installation/requirements.md`, `docs/cli/agent.md`):
  - The agent token "can be set before or after the cluster has been started, by changing the CLI option … on all servers". When no agent token is set, it defaults to the server token.
  - The server token is also the PBKDF2 passphrase for the bootstrap data.
  - Nodes need 6443/tcp to the server and 8472/udp between them for Flannel VXLAN. Kubelet traffic otherwise runs through the agent's reverse tunnel. 10250/tcp between nodes is needed for the metrics server, which this cluster keeps enabled.
  - `--node-label` applies only at registration.

## Goals / Non-Goals

**Goals**

- Add or remove an agent node by editing one Terraform variable, then running one idempotent Ansible playbook.
- An agent gets exactly the hardening of the existing node, plus nothing it does not need.
- A new agent is counted as capacity only once it can attach volumes. A removed agent is no longer counted.
- No public port beyond 443 and restricted SSH, and no server-grade secret on an agent.

**Non-Goals**

- A highly available control plane with more K3s servers and embedded-etcd peers.
- Autoscaling, or placing agents in another location or on ARM.
- Moving public ingress onto agents, or load-balancing it across nodes.
- Any cellctl or Substrate change.
- Terraform apply or any contact with a real server in this change. The operator applies through the existing saved-plan workflow.

## Decisions

### N1. Agents live in a child module, keyed by a map

`infra/terraform/foundation/modules/k3s-agents` owns the agent firewall and one `hcloud_server` per entry of its `nodes` map. The root passes `var.k3s_agent_nodes`, which defaults to `{}`, together with values the root already owns:

- the subnet;
- `server_location` and `server_image`;
- the SSH key;
- `admin_ssh_cidrs`;
- the common labels;
- the reserved addresses `private_node_ip` and `control_db_private_ip`.

`for_each` over a map, not `count`. Removing one entry must not re-index, and so must not replace the others. The map key becomes the server name suffix and therefore the Kubernetes node name, `exomem-agent-<key>`. Each value is `{ private_ip, server_type }`.

The module validates its inputs:

- the key is a lowercase DNS label of 1 to 40 characters;
- `private_ip` is inside the subnet, is not a network, gateway or broadcast address, is unique across entries, and is neither reserved address;
- `server_type` is a shared or dedicated x86 type (`cpx*`/`ccx*`), because the role pins the amd64 binary.

A separate module keeps the new resources in their own mocked-provider test suite. That suite needs only the `hcloud` provider. The module adds only new addresses, so no state moves.

### N2. Agent servers are disposable; their protection is the plan gate

An agent server sets:

- `delete_protection = false`, `rebuild_protection = false` and no `prevent_destroy`, so removing its map entry is a single plan;
- `shutdown_before_deletion = true` and `backups = false`;
- the location from the root, never per entry;
- an auto-assigned public IPv4, needed for image, package and object-storage egress, and no IPv6;
- `firewall_ids` set to the shared agent firewall;
- labels: the common labels plus `role = "k3s-agent"` and `node = <key>`.

An agent holds no state of its own. Each cell volume is a separate Hetzner volume that detaches when the server goes. The only guard against an accidental removal is the existing saved-plan inspector (`inspect_terraform_plan.py`), which refuses a destroy without a per-address approval. The removal runbook states that approval explicitly.

### N3. Agent firewall: 443 public, SSH restricted, nothing else

One `hcloud_firewall.agents` with exactly two inbound rules:

- TCP 22 from `admin_ssh_cidrs`, because Ansible reaches the agent directly and `base` forbids TCP forwarding, so the server cannot act as a jump host;
- TCP 443 from anywhere.

While ingress stays pinned to the server (N7), 443 on an agent reaches no listener. It is open so that moving ingress later needs no firewall change. Hetzner firewalls do not filter private-network traffic, so cluster ports get no cloud rule. The host firewall (N5) covers them.

### N4. The `k3s` role gains an agent mode

- **Mode.** `k3s_node_role` is `server` (the default, and today's behaviour) or `agent`. `tasks/main.yml` keeps what both modes share: interface discovery, directories, kernel settings, the binary and the host firewall. It then includes `server.yml` or `agent.yml`.
- **Agent configuration.** The agent writes `/etc/rancher/k3s/config.yaml` at mode `0600`, containing:
  - `server: https://<server private IP>:6443`;
  - `token: <k3s_agent_token>`;
  - `node-ip` and `flannel-iface` from the declared private address;
  - `node-label: exomem.io/node-pool=agent`;
  - `protect-kernel-defaults: true`;
  - the server's kubelet arguments.
- **What an agent never receives.** No `cluster-init`, server token, etcd options, audit or admission file, and no kubeconfig.
- **Unit and waits.** A `k3s-agent.service` unit runs `k3s agent --config …`. After start, the play asks the server until the node reports `Ready`, then until its CSINode lists `csi.hetzner.cloud` with an allocatable count. The CSI wait can be switched off (`k3s_agent_require_csi_capacity`) for a cluster that does not run the Hetzner CSI driver, such as the containerised test.
- **Convergent label.** A `kubectl label --overwrite` issued from the server keeps the node-pool label convergent, because `node-label` applies only at registration.
- **Server address.** The server address comes from the inventory: the first host of `hosted_nodes` outside `k3s_agents`. The play asserts there is exactly one.

### N5. The server gains `agent-token` and both modes gain inter-node rules

- **Agent token.** The server configuration adds `agent-token: <k3s_agent_token>`. The role asserts that the value is at least 32 characters and differs from the server token. Adding it restarts the server once, through the existing handler. Nodes already joined keep their credentials, and later runs change nothing.
- **Inter-node rules.** UFW admits, from each other K3s node's declared `private_node_ip` only:
  - 6443/tcp on the server;
  - 8472/udp on every node;
  - 10250/tcp on every node.

  The addresses come from the inventory, so the control database, which shares the subnet, gets no rule. Because rules name addresses rather than the subnet, a removed agent's address is revoked by the removal playbook (N6), not by a later run.

### N6. Removal is an ordered, idempotent playbook

`remove-agent.yml -e k3s_remove_node=<inventory name>`:

1. **Target check.** Asserts that the target is in `k3s_agents` and is not the server.
2. **Preflight.** From the server, reads the CSINodes, the attached VolumeAttachments and the Nodes. It fails when the target's attached volumes exceed the free slots (allocatable minus attached) on the other Ready, schedulable nodes. The same arithmetic cellctl publishes decides whether the cells have somewhere to go.
3. **Cordon, then drain.** Drains with `--ignore-daemonsets --delete-emptydir-data --timeout`, and never `--force`. A pod with no controller stops the drain for a human instead of being deleted.
4. **Stop the agent.** Stops and disables `k3s-agent` on the target, tolerating an unreachable host.
5. **Delete the node.** Deletes the Node object. Its CSINode goes with it, and cellctl zeroes its capacity row.
6. **Revoke the firewall rules.** Removes the target's inter-node UFW rules from the remaining K3s nodes.

A node that is already absent skips steps 2, 3 and 5. The operator then removes the map entry and plans the destroy.

### N7. Traefik is pinned to the control-plane node

The platform chart sets `traefik.nodeSelector: {node-role.kubernetes.io/control-plane: "true"}`. K3s applies that label to server nodes itself. The public address the ingress DNS record will target is the server's stable primary IP, so pinning keeps D11's hostPort entrypoint on that address whatever the scheduler would otherwise choose. The value is inert on `main`, where Traefik is still ClusterIP behind the tunnel.

### N8. Verification without a provider

- **Terraform.** `terraform test` in the module runs with `mock_provider "hcloud"`, covering plan shapes, rules, labels and every validation rejection. `fmt -check` and `validate` run for the module and the root. The root validation needs the Cloudflare provider, which CI installs.
- **Ansible.** `ansible-playbook --syntax-check` covers both playbooks, and `ansible-lint --profile production` covers the tree.
- **Contract tests.** Python contract tests assert that the rendered agent configuration carries no server token and that the firewall rule set is exact.
- **Containerised K3s test.** A test gated on `RUN_K3S_AGENT_JOIN_TEST=1` renders the role's own server and agent templates, then runs them in the pinned `rancher/k3s` image:
  - the agent joins with the agent token alone;
  - the node label is set;
  - a join with a wrong token fails;
  - the removal playbook's own drain and delete argv remove the node.

## Risks / Trade-offs

- **Drain is a short outage per cell.** An evicted cell restarts on another node after its volume detaches and reattaches, in about a minute. This is accepted, consistent with the plain-cells non-goal of zero-downtime maintenance.
- **Unencrypted overlay.** Flannel VXLAN is not encrypted. It runs only on the Hetzner private network, which carries only our servers.
- **A cordoned node still publishes slots.** cellctl ignores cordoning, so during a drain the node still publishes `cell_slots`, and admission can briefly over-count until the Node is deleted. The window is the drain itself. The preflight also guarantees that the existing cells fit.
- **The first agent restarts the server once.** Adding `agent-token` restarts the K3s server, and running cells ride through a server restart. The runbook places this first run in a maintenance window.
