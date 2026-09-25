## Context

- **Topology today.** Terraform declares one fleet server, `hcloud_server.alpha`. It is the `cluster-init` K3s server at `10.50.1.10` on `hcloud_network_subnet.alpha` (`10.50.1.0/24`). The control database server sits on the same subnet at `10.50.1.20`.
- **Hardening.** The `base` role installs SSH hardening (`AllowTcpForwarding no`), fail2ban, bounded journald, unattended upgrades and UFW. UFW denies inbound by default. It allows SSH from the administrator CIDRs and everything from the pod and service CIDRs, and nothing else. The `k3s` role installs:
  - a pinned, checksum-verified binary;
  - kernel settings with `protect-kernel-defaults`;
  - kubelet image-GC and log limits;
  - the audit policy and the admission configuration.
- **Capacity** (cellctl, `adopt-exomem-cloud-plain-cells` D9, PR #1368).
  - Per node, cellctl reads the `CSINode` allocatable count for `csi.hetzner.cloud` and the attached VolumeAttachments. It publishes `cell_slots = allocatable − headroom − non-cell attachments`, where a non-cell attachment is one whose PV is claimed outside an `exo-cell-` namespace, and headroom is the chart value `capacity.headroom` (5).
  - It zeroes the row of any node that is no longer present.
  - A node whose CSINode publishes no count gets 0 slots.
  - Admission counts every non-deleted cell row against the sum of slots.
- **Placement.** Cells carry no node selector. The scheduler places them, and the Hetzner CSI topology keeps a volume within its location.
- **Ingress.** On `main`, Traefik is ClusterIP behind `cloudflared`, which dials it from inside the cluster. D11 (PR #1368) exposes `websecure` on `hostPort: 443`, and after that the node running Traefik is the public entrypoint. `hcloud_firewall.alpha` does not yet admit 443. That is outstanding D11 work and stays out of this change.
- **Facts from the K3s documentation** (`k3s-io/docs`: `docs/cli/token.md`, `docs/installation/requirements.md`, `docs/cli/agent.md`):
  - **Agent token.** It "can be set before or after the cluster has been started", and defaults to the server token.
  - **Server token.** It is also the PBKDF2 passphrase for the bootstrap data.
  - **Short-format tokens.** A short-format token makes the joining node trust whatever CA the server presents. The secure format `K10<CA hash>::<user>:<password>` pins the CA.
  - **Ports.** Nodes need 6443/tcp to the server and 8472/udp between them (Flannel VXLAN). 10250/tcp between nodes is needed for the metrics server, which this cluster runs.
  - **Labels.** `--node-label` applies only at registration.

## Goals / Non-Goals

**Goals**

- Add an agent node by editing one Terraform variable and running `site.yml`. Remove one by running `remove-agent.yml` and then removing the entry. Both playbooks are idempotent.
- An agent gets exactly the hardening of the existing node, and nothing it does not need.
- A new agent is counted as capacity only once it can attach volumes. A removed agent stops being counted, and removal refuses to start when the cluster's cell volumes would not fit the remaining slots.
- No public port beyond 443 and restricted SSH, and no server-grade secret on an agent.

**Non-Goals**

- A highly available control plane: more K3s servers and embedded-etcd peers.
- Autoscaling, or agents in another location or on ARM.
- Moving public ingress onto agents, or load-balancing it.
- Any cellctl or Substrate change.
- Opening 443 on the existing server's cloud firewall. That is D11's step.
- Terraform apply, or any contact with a real server, in this change.

## Decisions

### N1. Agents live in a child module, keyed by a map

`infra/terraform/foundation/modules/k3s-agents` owns the agent firewall and one `hcloud_server` per entry of its `nodes` map. The root passes `var.k3s_agent_nodes`, which defaults to `{}`, together with values the root already owns:

- the subnet;
- `server_location` and `server_image`;
- the SSH key;
- `admin_ssh_cidrs`;
- the common labels;
- the reserved addresses `private_node_ip` and `control_db_private_ip`.

`for_each` over a map, not `count`, so removing one entry never re-indexes or replaces another. The server name is `exomem-agent-<key>`. It is also the inventory name and, through an explicit `node-name`, the Kubernetes node name (N4).

The module validates:

- the key is a lowercase DNS label of 1 to 40 characters;
- `private_ip` is a usable host address inside the subnet, unique across entries, and neither reserved address;
- `server_type` is on an allow-list of x86 types with at least 16 GB of memory: `cpx42`, `ccx23`, `ccx33`, `ccx43`.

Capacity is counted in volume attachments, not memory: 16 attachments minus 5 headroom leaves 11 slots, at a 1 GiB request per cell. A smaller type would publish slots it cannot host. The role pins the amd64 binary, which rules out ARM.

The module has its own mocked-provider test suite, which needs only `hcloud`. It adds only new addresses, so no state moves.

Changing an entry's `private_ip` in place is not a supported operation. The runbook removes the entry, then adds a new key. The firewall converges either way (N5).

### N2. Agent servers are disposable; the plan gate protects them

- **Protection off.** `delete_protection = false`, `rebuild_protection = false`, and no `prevent_destroy`.
- **Shape.** `shutdown_before_deletion = true` and `backups = false`. The location comes from the root, never from an entry.
- **Addresses.** An auto-assigned public IPv4, for image, package and object-storage egress, and no IPv6.
- **Firewall and labels.** `firewall_ids` names the shared agent firewall. Labels are the common labels plus `role = "k3s-agent"` and `node = <key>`.

An agent holds no state of its own, because each cell volume is a separate Hetzner volume. The only guard against an accidental removal is the saved-plan inspector, which refuses a destroy without per-address approval. The shared firewall exists even with an empty map, so removing the last agent destroys only that server.

### N3. Agent firewall: 443 public, SSH restricted, nothing else

`hcloud_firewall.agents` has exactly two inbound rules:

- **TCP 443 from anywhere.** The operator asked for 443. With ingress pinned to the server (N7), no listener answers on it until ingress is deliberately moved.
- **TCP 22 from `admin_ssh_cidrs`.** This deliberately goes beyond "443 only". Ansible reaches each agent directly, and `base` forbids TCP forwarding, so the server cannot act as a jump host.

Hetzner firewalls do not filter private-network traffic, so cluster ports get no cloud rule. The host firewall filters them instead (N5).

### N4. The `k3s` role gains an agent mode

**Groups.** `k3s_agents` is a child group of `hosted_nodes`, so agents inherit the hosted group variables: administrator CIDRs, keys and the agent token. `site.yml` runs three K3s plays, in order:
1. over all of `hosted_nodes`: `base`, then the role's inter-node firewall;
2. the server play, on `hosted_nodes:!k3s_agents`, which never matches an agent;
3. the agent play, on `k3s_agents`, with `serial: 1`.

Every node's firewall has therefore converged before any agent joins, so a join can require its peers to admit it without racing a peer that has not run yet. `k3s_node_role` is derived from group membership, and the agent play also sets it explicitly. The role asserts there is exactly one server host.

**Structure.**
- `tasks/main.yml` keeps what both modes share: interface discovery, validation, the configuration directory, kernel settings, the binary and the host firewall.
- It then imports `server.yml` or `agent.yml`. `server.yml` receives the audit log directory, the audit and admission files and the server configuration. The server assertions (server token, etcd credentials) apply only in server mode.
- The `Restart K3s` handler restarts `k3s_service_name`, which is `k3s` on the server and `k3s-agent` on an agent.

**Agent configuration.** Agent files never reference `k3s_server_token` or `k3s_etcd_*`. The agent writes `/etc/rancher/k3s/config.yaml` at mode `0600`, containing:
- `server: https://<server private IP>:6443`;
- `token`: the secure agent token (below);
- `node-name`: the inventory name;
- `node-ip` and `flannel-iface`: the declared private address and its interface;
- `node-label`: `exomem.io/node-pool=agent`;
- `protect-kernel-defaults: true`;
- the server's kubelet arguments, verbatim.

It carries no `cluster-init`, server token, etcd options, apiserver arguments, audit or admission file, or kubeconfig.

**Secure token.** The agent's token is `K10<sha256 of server-ca.crt>::node:<k3s_agent_token>`. The hash comes from `stat` with a SHA-256 checksum, delegated to the server, so no file content leaves the server. This pins the cluster CA, and a machine on the subnet cannot impersonate the server.

**Removed hosts do not rejoin.** `remove-agent.yml` writes the removal marker `/etc/rancher/k3s/removed`. `site.yml` then skips a host carrying it, with a warning, in both the hardening play and the agent play. A run between removal and the destroy therefore neither changes that host nor re-registers it, and it still converges every other node. The remaining nodes may re-admit the removed address, which is inert because that host runs nothing. The first run after the destroy and inventory regeneration prunes it.

**Join postconditions.** A `k3s-agent.service` unit runs `k3s agent --config …`. After it starts, the play asks the server, until each holds:
1. the node reports `Ready`;
2. the node-pool label is set, converged with `kubectl label --overwrite` and reported changed only when the label actually changed;
3. every other K3s node's UFW admits this node's address on the inter-node ports. This is read with `ufw show added`, delegated to each peer. A `--limit` run, or a partial one, therefore fails loudly instead of leaving the overlay silently broken;
4. if the `csi.hetzner.cloud` CSIDriver exists, the node's CSINode lists it with an allocatable count.

**Before the CSI driver exists.** On a fresh rebuild the platform chart, which installs the driver, runs after `site.yml`. The play then prints that capacity will be published once the platform is installed, and cellctl publishes 0 slots for the node until then.

### N5. The server gains `agent-token`, and every node converges inter-node rules

**Agent token.**
- The server configuration adds `agent-token: <k3s_agent_token>` whenever it is set. It is mandatory once any host is in `k3s_agents`, must be at least 32 characters, and must differ from the server token.
- Adding it restarts the server once, through the existing handler. Joined nodes keep their credentials, and later runs change nothing.
- A server-only inventory does not need it, so current runs are unaffected.

**Inter-node rules.** UFW admits the following, on the private interface only, from each other K3s node's declared `private_node_ip`:
- 6443/tcp on the server;
- 8472/udp on every node;
- 10250/tcp on every node.

Each rule carries the fixed comment `Exomem K3s inter-node`. Every run reads the rules carrying that comment and deletes any whose source is not a current peer. The rule set therefore converges to the inventory. That covers a removed node, a changed address, and a re-run of `site.yml` between removal and apply. The control database, which shares the subnet, never gets a rule.

### N6. Removal is an ordered, idempotent playbook

`remove-agent.yml -e k3s_remove_node=<inventory name>` performs these steps.

1. **Target.** The target must be in `k3s_agents` and must not be the server. If its Node exists, the Node must carry `exomem.io/node-pool=agent` and must not carry `node-role.kubernetes.io/control-plane`.
2. **No maintenance in flight.** It refuses while any StatefulSet in an `exo-cell-` namespace carries `exomem.io/hold`. A backup, restore or upgrade would lose its Job to eviction.
3. **Capacity preflight.** It uses cellctl's own formula, over the remaining nodes that are Ready, schedulable and have a known CSI limit:

   `cell PVCs in exo-cell- namespaces ≤ Σ (allocatable − k3s_remove_headroom − non-cell attachments) − non-cell attachments on the target`

   `k3s_remove_headroom` defaults to 5, the chart's `capacity.headroom`. As in cellctl, a node's slots are not clamped at zero, so the sum equals what cellctl publishes. PVCs are counted rather than attachments, because stopped cells still own their volumes. The preflight runs immediately before cordoning, and refusal changes nothing.
4. **Cordon, then drain.** A present node is always cordoned. If it is also Ready, it is drained with `--ignore-daemonsets --delete-emptydir-data --timeout`, never `--force`. A pod with no controller stops the drain for a human.
5. **Stop the agent completely.** The unit uses `KillMode=process`, as the server's does, so stopping it leaves pod containers running. The step therefore does what K3s's `k3s-killall.sh` does:
   - stop and disable `k3s-agent`;
   - kill every `containerd-shim`, then every process still in a `kubepods` cgroup (a container outlives its shim), and wait until none remain;
   - delete the agent configuration, which carries the agent token;
   - unmount everything under `/var/lib/kubelet/pods`, then every CSI `globalmount` under `/var/lib/kubelet/plugins/kubernetes.io/csi`;
   - close each dm-crypt mapping that the encrypted storage class opened;
   - write the removal marker.

   The stop counts as confirmed only on a reachable host showing no shim process, no process in a pod cgroup, no pod or CSI volume mount, and no open crypt mapping except ones backing a mount outside kubelet, or when the operator passes `k3s_remove_host_gone=true` after verifying that the server is destroyed. The target is pinged first. An unreachable host simply stays unconfirmed, so a lone dead agent never aborts the run. Without the operator's confirmation, the delete play then fails before anything is deleted. A live agent is therefore never deleted and left to re-register, and a volume is never force-detached from a running writer.
6. **Force the stragglers off.** With the agent confirmed stopped, it applies the `node.kubernetes.io/out-of-service=nodeshutdown:NoExecute` taint. It then waits until no VolumeAttachment names the node. That covers the NotReady rerun case, where evicted pods on a dead kubelet would otherwise stay Terminating.
7. **Delete.** It deletes the Node with `--ignore-not-found`, waits, and re-checks that the Node stays absent.
8. **Revoke.** It converges the inter-node UFW rules on the remaining K3s nodes, excluding the target's address.

If the Node is already absent, the playbook skips step 1's label check and steps 3, 4, 6 and 7. It still stops the agent (5) and converges the firewall (8). The operator then removes the map entry and plans the destroy. A rerun is meaningful only until that destroy: afterwards the regenerated inventory no longer holds the target, and step 1 refuses it.

### N7. Traefik is pinned to the control-plane node

The platform chart sets `traefik.nodeSelector: {node-role.kubernetes.io/control-plane: "true"}`. K3s applies that label to server nodes. The DNS record for D11's hostPort entrypoint will target the server's stable primary IP, so the pin keeps ingress on that address.

The pin also constrains scheduling on `main`, harmlessly: with one node, Traefik already runs there. The line sits outside the `ports.websecure` hunk PR #1368 edits.

**Rollout strategy belongs to D11.** A single `hostPort: 443` replica on one node cannot roll with a surge pod, so D11 needs `maxSurge: 0`. On `main`, Traefik is still one ClusterIP replica with no `hostPort`, and a no-surge strategy would only add an ingress outage to every rollout. The strategy therefore lands with D11's `hostPort` in PR #1368, not here.

### N8. Verification without a provider or real hosts

- **Terraform.**
  - `terraform test` in the module, with `mock_provider "hcloud"`, covers plan shapes, rules, labels and every validation rejection.
  - A root `terraform test` with all four providers mocked covers the wiring.
  - `fmt -check` and `validate` run for both.
- **Ansible.** `ansible-playbook --syntax-check` runs on both playbooks, and `ansible-lint --profile production` on the tree.
- **Contract tests.** Python contract tests check what the rendered files and task files may contain.
- **Role test.** A gated role test (`RUN_K3S_NODE_POOL_ROLE_TEST=1`) runs the real `site.yml` and `remove-agent.yml` over the Docker connection, against two privileged systemd Ubuntu 24.04 containers acting as the server and an agent. It proves:
  - `site.yml` converges both nodes, and a second run reports `changed=0`;
  - with two agents, one existing and one added in a later run, every join passes the peer-admission check;
  - the agent joins with the secure agent token, is Ready and carries the label;
  - a node presenting a wrong token is refused;
  - the removal preflight refuses when cell PVCs exceed slots, and changes nothing;
  - removal deletes the node and revokes its rules, and a rerun changes nothing in the cluster.

  The test documents its deviations from a real host:
  - the pinned binary is served from a `file://` copy of the `rancher/k3s-upgrade` image's `/opt/k3s`, whose SHA-256 equals the role's pin, so the checksum is unchanged;
  - a `config.yaml.d` drop-in appends `fail-cgroupv1=false`, because the sandbox kernel runs cgroup v1;
  - `/var/lib/rancher` sits on a Docker volume, because containerd cannot use overlayfs on overlayfs;
  - the etcd-s3 endpoint is unreachable, which K3s tolerates at start;
  - no Hetzner CSI driver is present, so the capacity wait takes the "driver absent" path.

  The test also writes the role's kernel settings to the Docker host kernel, so it runs only on disposable hosts.

### N9. D8's multi-node affinity clause is not needed

D8 says that "once there is more than one node, backup and restore Jobs carry node affinity to the volume's node". cellctl in PR #1368 has no such affinity, and this change does not need it:

- every backup and restore runs with the cell scaled to 0, on a ReadWriteOnce volume;
- once the cell pod is gone, the volume detaches, and the Job's pod attaches it wherever it schedules within the location;
- the scheduler's volume-limit check already respects each CSINode's allocatable count.

Pinning a Job to the node where the volume was last attached would add a failure mode: that node might be full, cordoned or gone. This change therefore records D8's clause as superseded. It asks for the clause to be removed in `adopt-exomem-cloud-plain-cells` (PR #1368 edits the adjacent lines, so the edit belongs there) rather than implemented.

## Risks / Trade-offs

- **A drain is a short outage per cell.** An evicted cell restarts elsewhere once its volume moves, in about a minute. This is consistent with the plain-cells non-goal.
- **Unencrypted overlay.** Flannel VXLAN is unencrypted, and it runs only on the Hetzner private network.
- **Over-count during removal.** The preflight counts cell PVCs, but admission counts non-deleted rows. A row admitted but not yet provisioned has no PVC, so it is invisible to the preflight. And cellctl ignores cordoning and NotReady, so it keeps publishing the target's slots until step 7 deletes the Node. Admissions in that window can over-commit capacity. The preflight runs immediately before cordoning, which keeps the window to the drain itself. The runbook removes nodes when no invitations are pending.
- **Holds only on the row.** The preflight reads holds from StatefulSet annotations. cellctl also treats a row's `hold_kind` as a hold when the StatefulSet is gone, and the preflight cannot see that case. The runbook checks the row as well.
- **Stale placement column.** cellctl writes a row's `node` only at first placement, so after a drain the column names the old node. Nothing in this change reads it. Fixing it belongs to cellctl.
- **Memory-blind capacity.** Slots count attachments, not memory. N1's type allow-list keeps an agent's slots within its memory at the current cell request. A larger cell request needs the list revisited.
- **Pod-CIDR rules on any interface.** `base` admits the pod and service CIDRs on every interface, so a host that spoofs a pod-CIDR source address on the private network bypasses N5's peer rules. N5 narrows the node ports, not that pre-existing allowance, and the spec states the guarantee accordingly.
- **The first agent restarts the server once.** Adding `agent-token` restarts the K3s server, and running cells ride through it. The runbook places this first run in a maintenance window.
