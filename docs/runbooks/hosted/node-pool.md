<!-- authority:non-specification -->

# K3s agent node pool

Add or remove an Exomem Cloud K3s agent node (openspec
`add-cloud-node-provisioning`). One `k3s_agent_nodes` entry is one agent
server. cellctl needs no change: it counts a node once the node's CSINode
publishes a volume-attachment limit, and zeroes it once the Node is deleted.

## Preconditions

- The agent token exists (once, before the first agent): write
  `k3s_agent_token` with `infra/scripts/secret_handoff.py` as described in
  `secrets.md`. It must differ from `k3s_server_token`.
- The first run that introduces the agent token restarts the K3s server once.
  Schedule it in a maintenance window; running cells ride through the restart.
- Pick an unused private address in the subnet (not `10.50.1.10` or
  `10.50.1.20`), and a type from the allow-list: `cpx42`, `ccx23`, `ccx33`
  or `ccx43`.
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
  infra/ansible/inventory.yml --user exomem-admin
infra/scripts/ansible_with_sops.sh \
  --inventory infra/ansible/inventory.yml \
  --vars infra/secrets/ansible/k3s-server-token.v1.sops.json \
  --vars infra/secrets/ansible/k3s-agent-token.v1.sops.json \
  --vars infra/secrets/ansible/etcd-s3-access-key.v1.sops.json \
  --vars infra/secrets/ansible/etcd-s3-secret-key.v1.sops.json
```

## Remove a node

Run the removal playbook first, while the entry and its inventory host still
exist. It refuses while any cell holds a maintenance hold, and when the cluster's
cell volumes would not fit the remaining nodes' slots (add a node first). It
cordons, drains without force, stops K3s and every container, deletes the Node,
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

Regenerate the inventory afterwards. A removed host carries
`/etc/rancher/k3s/removed`, and `site.yml` skips it, with a warning, rather than
rejoin it. To abandon a half-finished removal instead, delete that marker on the
host, run `kubectl uncordon <node>`, and converge with `site.yml`.

## Verify

```bash
kubectl get nodes -L exomem.io/node-pool
kubectl get csinode -o custom-columns=NODE:.metadata.name,LIMIT:.spec.drivers[0].allocatable.count
psql "$EXOMEM_CELLCTL_DSN" -c 'select node, cell_slots, attachments_used from exomem_cloud_capacity'
infra/scripts/verify_ansible_convergence.py --inventory infra/ansible/inventory.yml \
  --vars infra/secrets/ansible/k3s-server-token.v1.sops.json \
  --vars infra/secrets/ansible/k3s-agent-token.v1.sops.json \
  --vars infra/secrets/ansible/etcd-s3-access-key.v1.sops.json \
  --vars infra/secrets/ansible/etcd-s3-secret-key.v1.sops.json
```

A new agent is Ready, labelled `exomem.io/node-pool=agent`, and publishes a
CSI limit, and cellctl's next pass shows its `cell_slots`. A removed agent is
absent from `kubectl get nodes`, and its `cell_slots` is 0.
