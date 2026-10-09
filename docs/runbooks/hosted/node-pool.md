<!-- authority:non-specification -->

# K3s agent node pool

Add or remove an Exomem Cloud K3s agent node (openspec
`add-cloud-node-provisioning`). One `k3s_agent_nodes` entry is one agent
server. cellctl needs no change: it counts a node once the node's CSINode
publishes a volume-attachment limit, and zeroes it once the Node is deleted.
Reserved agents publish zero general cell slots, retaining actual attachment counts.
For an agent Terraform does not create (a dedicated, auction or other-provider
server with encrypted local cell storage), follow
[dedicated-host.md](dedicated-host.md) instead.

## Preconditions

- The agent token exists and is active (once, before the first agent): write
  `k3s_agent_token` with `infra/scripts/secret_handoff.py`, then select its v1
  in `infra/contracts/active-ansible-selection-v1.json`, as described in
  `secrets.md`. It must differ from `k3s_server_token`.
- The first run that introduces the agent token restarts the K3s server once.
  Schedule it in a maintenance window; running cells ride through the restart.
- Pick an unused private address in the subnet (not the fleet server's
  `10.50.1.10`), and a type from the allow-list: `cpx42`, `ccx23`, `ccx33`
  or `ccx43`. A `cx33` (4 CPU / 8 GiB) is permitted only with a nonempty
  exact `dedicated_cell_id`, reserving the node for that one cell and zero
  general slots. Check actual same-location availability and obtain saved-plan
  cost authority before purchase; a list price does not prove stock.
- Before a removal, confirm that no cell row carries a hold. The playbook sees
  holds on StatefulSets, but not a hold recorded only on a row whose
  StatefulSet is gone:
  `psql "$EXOMEM_CELLCTL_DSN" -c "select cell_id, hold_kind from exomem_cloud_cells where hold_kind is not null"`.
- Do not change an existing entry's `private_ip` in place. Remove the entry and
  add a new key instead.

## Add a node

Add one entry to the foundation variables, then plan, apply, regenerate the
inventory and converge every node. Never pass `--limit`: every existing node
must admit the new one, and the join checks that it does.

A new agent has no public SSH, and `site.yml` refuses a host without NetBird
when no administrator CIDR is set. Open a temporary CIDR window (see
[Administration access](deploy.md#administration-access)). Enroll the agent
with substrate-infra's `ansible/administration.yml`, with the agent in its
`administration_servers` group and a one-use setup key injected as
`SUBSTRATE_NETBIRD_SETUP_KEY`. Add its NetBird IP to the private address map
and verify a fresh managed SSH connection over NetBird. Only then revert the
CIDR in Terraform and Ansible.

```bash
# terraform.tfvars (foundation)
#   k3s_agent_nodes = {
#     "01" = { private_ip = "10.50.1.31", server_type = "cpx42" }
#   }
export TF_CLOUD_ORGANIZATION=replace-with-approved-org
export TF_TOKEN_app_terraform_io=read-from-secret-manager
infra/scripts/plan.sh foundation /run/user/$UID/foundation-agents.tfplan
infra/scripts/apply_saved_plan.sh foundation /run/user/$UID/foundation-agents.tfplan
terraform -chdir=infra/terraform/foundation output -json > /run/user/$UID/foundation.json
chmod 0600 /run/user/$UID/foundation.json
infra/scripts/generate_ansible_inventory.py /run/user/$UID/foundation.json \
  infra/ansible/inventory.yml --user exomem-admin \
  --admin-addresses "${EXOMEM_ADMIN_ADDRESSES:?private NetBird address map required}" \
  --dedicated-hosts "${EXOMEM_DEDICATED_HOSTS:?private dedicated host list required}"
fleet_vars_text="$(infra/scripts/active_ansible_vars.py hosted-node)"
mapfile -t fleet_vars <<< "${fleet_vars_text}"
infra/scripts/ansible_with_sops.sh --inventory infra/ansible/inventory.yml "${fleet_vars[@]}"
```

The inventory always carries every node. `EXOMEM_DEDICATED_HOSTS` is the
private host list from [dedicated-host.md](dedicated-host.md), or a file
holding `{}` when there are none. An inventory without a dedicated host makes
`site.yml` remove that host's WireGuard link, firewall rules and Tang access
on every other node. `active_ansible_vars.py` passes the version of each
hosted-node Ansible variable that `infra/contracts/active-ansible-selection-v1.json`
selects, including the Tang keys and each dedicated host's passphrase once they
are selected. Escrowing a new version changes nothing until a reviewed commit
selects it.

## Reserve an agent for one selected cell

Before any reserved agent joins, deploy the signed reservation-aware cellctl
controller and its node-read permissions through the ordinary platform release,
leaving `cellctl.dedicatedCellIds` empty. Verify reserved nodes publish zero
general slots; an older controller counts CSI attachment slots without the
reservation and must not observe a restricted 8 GiB node as general capacity.

Dedicated placement is disabled by default. For an authorized relocation, set
that agent's `k3s_agent_dedicated_cell` inventory variable to the cell's exact
16-character base32 ID before its first join. The role registers and converges
`exomem.io/dedicated-cell=<cell ID>` as both a label and a `NoSchedule` taint,
preserving the node-pool label and unrelated labels/taints. MemoryQoS is a
separate host opt-in. Inspect the existing PV's actual topology and stage the
agent in that location; the encrypted RWO PVC remains the same.

Relocation and rollback both use stop/change/start:

1. Request `desired_state=stopped` through the existing cell lifecycle. Wait for
   stopped state, completed maintenance holds and Jobs, and release of every
   volume user. Checking for no hold while still serving races scheduled backup.
2. Add the cell ID to `cellctl.dedicatedCellIds` through the platform chart's
   source-managed release. Its serving and backup/restore templates select only
   that reservation. Do not change selection during an active hold: Job templates
   are immutable. Unselected cells retain their templates and render digests.
3. Resume the cell and verify readiness, selected node, the original PVC/PV and
   successful backup. No restore or alternate vault is part of this move.
4. To roll back, stop and wait again, remove the chart selection, then resume
   and verify relocation away. Keep the agent taint until that verification;
   only then clear the Terraform entry's `dedicated_cell_id`, regenerate inventory
   and converge the role to remove its owned reservation. A CX33 cannot become
   an unreserved general agent: remove it or use an allowed general type before
   clearing that host reservation. Unrelated node labels and taints remain intact.

A label or taint with the reservation key excludes a node from general admission
and removal-capacity calculations. Selected cells still count against shared
slots: this is deliberately conservative attachment capacity, not evidence of
warm-memory headroom. Before adoption, account for sibling requests and warmed
usage including platform workloads, retaining 20% node headroom. Source defaults
neither create a paid host nor authorize a join or relocation.

The Terraform entry's optional `dedicated_cell_id` supplies the generated
inventory host variable `k3s_agent_dedicated_cell`. Do not maintain a second
manual host reservation that can drift from that declaration.

## Remove a node

Run the removal playbook first, while the entry and its inventory host still
exist. It refuses while a cell whose volume is on the target holds a
maintenance hold, and when the cluster's cell volumes would not fit the
remaining nodes' slots (add a node first). It also refuses while any desired
cell template selects the target reservation, including a stopped cell;
relocate and clear that selection first. On an agent with local storage, it
refuses while a cell's live volume is still on the target; relocate those cells
first ([node loss](../cloud-node-loss.md)). Until the playbook relocates them
itself, that means stopping the agent and relocating as for a lost node, which
loses each cell's writes since its last hourly backup. It cordons, drains without force, stops K3s and every container, deletes the Node,
and revokes the node's inter-node firewall rules. Remove nodes when no
invitations are pending: rows admitted during the drain are not yet visible as
volumes.

The removal reads no secret, so it runs without the SOPS wrapper:

```bash
cd infra/ansible
ansible-playbook --inventory inventory.yml remove-agent.yml \
  -e k3s_remove_node=exomem-agent-01
```

If the agent cannot be reached, the playbook stops before deleting the Node.
Pass `-e k3s_remove_host_gone=true` only after verifying in the Hetzner console
that the server is destroyed. Then remove the entry and apply the destroy,
naming the one approved address:

```bash
infra/scripts/plan.sh foundation /run/user/$UID/foundation-agents.tfplan
infra/scripts/apply_saved_plan.sh foundation /run/user/$UID/foundation-agents.tfplan \
  --allow-destructive 'module.k3s_agents.hcloud_server.agent["01"]'
```

A removed agent with a `cells` volume group ends with that device erased
([erase a removed agent's cells device](../cloud-node-loss.md#erase-a-removed-agents-cells-device)).

Regenerate the inventory afterwards. A removed host carries
`/etc/rancher/k3s/removed`, and `site.yml` skips it, with a warning, rather than
rejoin it. To abandon a half-finished removal instead, delete that marker on the
host, run `kubectl uncordon <node>`, and converge with `site.yml`.

## Verify

```bash
kubectl get nodes -L exomem.io/node-pool
kubectl get csinode -o custom-columns=NODE:.metadata.name,LIMIT:.spec.drivers[0].allocatable.count
psql "$EXOMEM_CELLCTL_DSN" -c 'select node, cell_slots, attachments_used from exomem_cloud_capacity'
fleet_vars_text="$(infra/scripts/active_ansible_vars.py hosted-node)"
mapfile -t fleet_vars <<< "${fleet_vars_text}"
infra/scripts/verify_ansible_convergence.py --inventory infra/ansible/inventory.yml "${fleet_vars[@]}"
```

A new agent is Ready, labelled `exomem.io/node-pool=agent`, and publishes a
CSI limit, and cellctl's next pass shows its `cell_slots`. A removed agent is
absent from `kubectl get nodes`, and its `cell_slots` is 0.
