<!-- authority:non-specification -->

# Exomem Cloud platform deployment

This runbook converges the K3s fleet and upgrades the `exomem-platform` Helm
release: cellctl, the Cloud gateway and ingress, storage, the scheduler and its
observability. Cell images roll out through cellctl, not through this release.

## Preconditions

Production mutation always uses a saved plan; a second plan is never computed
during apply.

Use `infra/scripts/plan.sh` for plan preparation. It deletes its temporary raw
JSON and emits a separate `<saved-plan>.review.json` containing resource addresses
and actions only. Review configuration in the owning IaC source; this summary
does not contain configuration values. Saved binary plans and raw `terraform
show -json` output can contain plaintext credentials despite Terraform's
`sensitive` display markers. Never print, search or pass them to an agent or
reviewer; only the value-free summary is review material.

Prepare one private values file for the release. It must set:

- `platformJobs.image` to the current Cloud cell image,
  `ghcr.io/artexis10/exomem@sha256:<digest>`. The scheduler, its collector and
  alert evaluator run their mounted scripts on it.
- the reviewed Cloud values: `cellctl`, `cloudGateway`, `cloudIngress`,
  `artifactBroker`, `edge`, `capacity` and `cells`.


Keep its mode `0600` outside the repository and set its path in
`EXOMEM_PLATFORM_VALUES`.

## Administration access

Hosts have no public SSH. Operators and Ansible reach them over the company
NetBird with the existing key and host pin; the shared NetBird role admits
22/tcp on `wt0`. Keep a private JSON file outside the repository that maps each
inventory host name (`exomem-alpha`, `exomem-agent-<key>`) to its NetBird IP,
and pass it to the inventory generator with `--admin-addresses`. Hosts it omits
keep their public IPv4, which is closed. The shared control host is not in
this inventory; `substrate-systems/substrate-infra` administers it. The base
role refuses to run with no public SSH CIDR unless the host has `wt0` and UFW
admits 22/tcp on it.

Ansible uses plain OpenSSH, not `harness ssh`. Route only the NetBird
addresses through the workstation's NetBird SOCKS listener (the workstation
default is `127.0.0.1:21080`), with one `~/.ssh/config` block per host that also
reuses the existing host pin, as the managed desktop aliases do:

```
Host <NetBird IP>
  HostKeyAlias <public IPv4>
  ProxyCommand nc -X 5 -x 127.0.0.1:21080 %h %p
```

Do not set the proxy globally (`ANSIBLE_SSH_ARGS` or group-level
`ansible_ssh_common_args`): break-glass and new-agent runs connect to a public
IPv4, which the SOCKS listener cannot reach. `host_key_checking` stays on.

Break-glass is the Hetzner console or rescue system. A disposable host with
public inbound closed at both the Hetzner firewall and UFW returned over
NetBird after a soft reboot and a hard reset, and stayed publicly unreachable,
with console access confirmed (2026-10-05).

- A fresh host has UFW inactive, so a Terraform window is enough: set one CIDR
  in `admin_ssh_cidrs` and the same CIDR in `base_admin_ssh_cidrs`, so a base
  role run keeps admitting it once UFW is enabled.
- An existing host with NetBird down admits 22/tcp only on `wt0`, so neither
  Terraform nor Ansible can reach it. From the Hetzner console first run
  `ufw allow from <ip> to any port 22 proto tcp comment 'Exomem administrator SSH'`,
  then open the same CIDR in Terraform. The managed comment lets the next
  Ansible run retire the rule.

Close the window once NetBird SSH works again: revert both lists to `[]`, apply
Terraform, and run the targeted Ansible command below. It deletes only rules
carrying the managed comment, never NetBird's or any other.

### Cutover to NetBird-only SSH

Confirm a fresh managed SSH connection over NetBird and console access for each
host first. Then, with `base_admin_ssh_cidrs: []` in the private group
variables, converge SSH access alone, one host at a time:

```bash
cd infra/ansible
ansible-playbook --inventory inventory.yml site.yml --tags admin_ssh --limit <host>
```

The tag runs fact gathering and the SSH access tasks only; no package upgrades
or K3s changes. Next, set `admin_ssh_cidrs = []`, review the saved foundation
plan (in-place rule removal on the alpha and agent firewalls only), and apply
it. `substrate-systems/substrate-infra` administers the shared control host.

Finally, run `sudo ufw status numbered` on every host and remove any public
22/tcp rule that remains with `sudo ufw delete <n>`, listing again after each
deletion. The role deliberately leaves uncommented and hand-written rules
alone, so these are the operator's to remove. Confirm port 22 is unreachable
from outside NetBird and a fresh managed connection still works.

## Converge the fleet

```bash
infra/scripts/validate.sh
```

Generate non-sensitive inventory and run the governed two-pass convergence gate.
The gate converges the whole K3s fleet: the server, every Terraform agent and
every dedicated host. `site.yml` converges the inter-node firewall, the private
link and Tang to the inventory, so a run over part of the fleet would remove
the missing nodes' rules on every node it reaches. The shared control database
is not in this inventory; substrate-infra converges it.

```bash
set -euo pipefail
umask 077
command -v jq >/dev/null
deploy_work_dir="$(mktemp -d)"
trap 'rm -rf -- "${deploy_work_dir}"' EXIT
terraform -chdir=infra/terraform/foundation output -json \
  | jq '{server_ipv4, private_node_ip, k3s_agent_nodes, vswitch} | with_entries(select(.value != null))' \
  > "${deploy_work_dir}/foundation-output.json"
infra/scripts/generate_ansible_inventory.py \
  "${deploy_work_dir}/foundation-output.json" "${deploy_work_dir}/inventory.json" \
  --admin-addresses "${EXOMEM_ADMIN_ADDRESSES:?private NetBird address map required}" \
  --dedicated-hosts "${EXOMEM_DEDICATED_HOSTS:?private dedicated host list required}"
# The version of each hosted-node Ansible variable that
# infra/contracts/active-ansible-selection-v1.json selects: server and agent
# tokens, etcd keys, Tang keys and passphrases.
fleet_vars_text="$(infra/scripts/active_ansible_vars.py hosted-node)"
mapfile -t fleet_vars <<< "${fleet_vars_text}"
infra/scripts/verify_ansible_convergence.py --inventory "${deploy_work_dir}/inventory.json" \
  "${fleet_vars[@]}"
```

The gate needs every inventoried K3s node reachable, and fails closed if one
is not. It removes nothing on the way: the firewall, WireGuard and Tang read
their peers from the inventory's groups, not from the hosts a run reaches.

- For a node that is down on purpose, append `-- --limit '!<node>'` to the
  gate. This passes only when the excluded node is the sole K3s agent and no
  WireGuard host is inventoried. The excluded node keeps its firewall rules,
  WireGuard peer and Tang access on every other node.
- Every agent's run reads the inter-node rules on every other K3s node ("Read
  every other K3s node's inter-node rules" in
  `infra/ansible/roles/k3s/tasks/agent.yml`). With a second agent, or with the
  server excluded, that read needs the excluded node, and the run stops with
  nothing removed.
- With any WireGuard host in the inventory, every node's private-link check
  needs each WireGuard host's public key. A run on that host reads it, or the
  host list records it as `wireguard_public_key`
  ([cloud-dedicated-host.md](cloud-dedicated-host.md#add-the-host)). A key that
  is neither stops the run in the same way.
- A gate run with `--limit` set to the server needs no agent to be reachable,
  once every WireGuard host's key is recorded. It is unproven until OpenSpec
  `move-cloud-cells-to-local-storage` task 6.6 runs it.
- A node that stays down for longer leaves the inventory through
  [cloud-node-pool.md](cloud-node-pool.md#remove-a-node) or
  [cloud-dedicated-host.md](cloud-dedicated-host.md#remove-the-host) before
  the next gate.

## Apply the active secrets

The registry must be re-signed against the kept matrix and active selection before
the next deployment. Removing a destination does not revoke its provider key or
delete its Kubernetes Secret. Complete the pending
[retirement disposal](cloud-hosted-retirement.md) separately.

Apply every declared active K3s ciphertext before installing the immutable
platform release. The registry is an Ed25519-signed, matrix-bound artifact from
the secret-release custodian and names one explicit version and ciphertext
digest for every active K3s destination. The deployment operator holds only the
registry public key. Missing, extra, stale, changed, or unsigned entries stop
deployment before Helm runs.

```bash
: "${EXOMEM_ACTIVE_SECRET_REGISTRY:?set the signed active-secret registry path}"
: "${EXOMEM_ACTIVE_SECRET_REGISTRY_PUBLIC_KEY:?set its trusted Ed25519 public-key path}"
infra/scripts/apply_active_sops_secrets.py \
  --matrix infra/contracts/secret-destinations-v1.json \
  --registry "$EXOMEM_ACTIVE_SECRET_REGISTRY" \
  --registry-public-key "$EXOMEM_ACTIVE_SECRET_REGISTRY_PUBLIC_KEY" \
  --trust-contract infra/contracts/active-secret-registry-v1.json \
  --verify-only

infra/scripts/apply_active_sops_secrets.py \
  --matrix infra/contracts/secret-destinations-v1.json \
  --registry "$EXOMEM_ACTIVE_SECRET_REGISTRY" \
  --registry-public-key "$EXOMEM_ACTIVE_SECRET_REGISTRY_PUBLIC_KEY" \
  --trust-contract infra/contracts/active-secret-registry-v1.json
```

## Render and upgrade the release

Apply the namespaces and upgrade the reviewed release:

```bash
: "${EXOMEM_PLATFORM_VALUES:?set the reviewed private platform values path}"
helm template exomem-platform infra/helm/platform --namespace exomem-platform \
  --values infra/helm/platform/values.yaml \
  --values "$EXOMEM_PLATFORM_VALUES" \
  --show-only templates/namespaces.yaml | kubectl apply -f -

helm upgrade --install exomem-platform infra/helm/platform --namespace exomem-platform \
  --values infra/helm/platform/values.yaml \
  --values "$EXOMEM_PLATFORM_VALUES" \
  --atomic --wait --timeout 10m
```

Do not run this upgrade while a cell restore runs
([cloud-operator-restore.md](cloud-operator-restore.md)).

## Verify

```bash
kubectl -n exomem-cloud wait --for=condition=Available deployment/cellctl --timeout=180s
kubectl -n exomem-cloud wait --for=condition=Available deployment/exomem-cloud-gateway --timeout=180s
kubectl -n exomem-platform get cronjob \
  exomem-hosted-scheduler-exomem-access-delivery exomem-hosted-scheduler-exomem-reconcile \
  -o jsonpath='{range .items[*]}{.metadata.name} suspend={.spec.suspend}{"\n"}{end}'
kubectl get storageclass exomem-cloud-encrypted
```

Both scheduler CronJobs must print `suspend=false` while Cloud is enabled.
