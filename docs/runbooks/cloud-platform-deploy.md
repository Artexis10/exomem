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
- the reviewed Cloud values: `cellctl`, `cellData`, `cloudGateway`, `cloudIngress`,
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

A routine gate run restarts no K3s node for the flannel MTU: the k3s role
leaves `k3s_flannel_mtu` unset, and then installs no flannel configuration. Its
rollout is the reviewed commit that sets the value, merged and run in an
operator window
([cloud-dedicated-host.md](cloud-dedicated-host.md#roll-out-the-flannel-mtu)).
Once the value is set, a gate run whose nodes already run at it changes
nothing. Do not use the gate for the rollout itself: its output hides the
MTU check's message.

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

### Upgrade with the command

`infra/scripts/platform_upgrade.py` runs the review, the upgrade, the verify, the platform
Deployment record and the `cloud-deploy` dispatch as one command (automate-cloud-releases D7).
Each review step stops the command before anything changes. `EXOMEM_UPGRADE_DIR` is a private
directory outside the repository; the command keeps the tag's checkout, the render and the
dry run there. A render holds the private values, so keep it out of tickets and chat.

```bash
: "${EXOMEM_PLATFORM_VALUES:?set the reviewed private platform values path}"
: "${EXOMEM_UPGRADE_DIR:?set a private upgrade directory outside the repository}"
: "${EXOMEM_CELLCTL_DSN:?set the control database DSN for the schema check}"
umask 077
uv run --frozen python infra/scripts/platform_upgrade.py "$TAG" \
  --crossed <barrier-id>   # repeat for each barrier the upgrade crosses, after its runbook step
```

- The command reads the live release's tag from the Helm release description it writes on every
  upgrade (`platform-v<tag>`). A platform it has not upgraded yet needs `--from <tag>` once.
- A barrier is open when it lies after the live release and at or below the tag, or when it is
  at or below the tag and the newest successful platform Deployment does not record it. An open `platform` barrier needs its runbook step done first,
  then `--crossed <id>`; a `one-way` barrier removes `--atomic` for that upgrade.
- An open `cell` barrier accepts `--crossed <id>` too: only a platform Deployment's
  `barriers_crossed` records a crossing, and `cloud-deploy` stops on an unrecorded entry of either
  scope. Record it here once its runbook step is done; the command prints a note while one is open.
- A secret-contract change with no barrier entry stops the command and names the omission.
- On success it records the platform Deployment and dispatches `cloud-deploy`; on a failed
  upgrade it records `failure` and dispatches nothing.

Expected result: the command prints `recorded the platform Deployment for <tag>` and
`dispatched cloud-deploy.yml on main`, and the next `cloud-deploy` run ends with
`cloud-deploy: CANARY_ON_TARGET` or `cloud-deploy: CELL_IMAGE_CURRENT`.

### Cross the backup key conversion

**Warning:** an upgrade that moves cellctl from before 0.147.0 to 0.147.0 or later starts the conversion of each
cell's backup key. Neither `helm rollback` nor `--atomic` undoes a converted cell.

The conversion runs on a cell's first manifest apply after the upgrade. cellctl makes a per-cell root key and wraps
it under the backup master key. It then wraps the cell's backup key under that root and sets `backup_key_version`
to 1. The code is `_resolve_secret_material` in `infra/cellctl/src/cellctl/reconcile.py`. The backup key does not
change, so the backup repositories stay readable. A cellctl before 0.147.0 unwraps `backup_key_wrapped` with the
backup master key, so it cannot unwrap a converted cell. A rollback restores that older cellctl but not the
control database. The safe undo is to fix forward: correct the cause and run the upgrade again.

A deleted cell does not convert. On its first pass, cellctl 0.147.0 or later erases the wrapped keys of each
deleted cell (`retire_content_keys` in `infra/cellctl/src/cellctl/db.py`). A stopped cell converts when it next starts.

If the upgrade crosses 0.147.0, do "Before the upgrade", then run the upgrade — the command with
`--crossed backup-key-conversion`, or "Manual fallback" — then "After the backup key conversion".
Do "If the upgrade fails" only after a failure.

#### Before the upgrade

1. Run this read-only query as the cellctl database role.

   ```bash
   psql "$EXOMEM_CELLCTL_DSN" --command "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'exomem_cloud_cells' ORDER BY column_name"
   ```

   Expect every name in `CELL_COLUMNS` in `infra/cellctl/src/cellctl/db.py`. The information schema lists only the columns
   that the role can read. A missing name means that a Substrate migration is pending: stop.
2. Save a temporary copy of the backup key columns of each cell that is not deleted.

   ```bash
   psql "$EXOMEM_CELLCTL_DSN" --csv --command "SELECT cell_id, backup_key_wrapped, backup_key_version, cell_root_key_wrapped, cell_root_key_version FROM exomem_cloud_cells WHERE desired_state <> 'deleted' ORDER BY cell_id" > "$EXOMEM_UPGRADE_DIR/cell-keys-before.csv"
   ```

   Expect one row for each cell that is not deleted, with `cell_root_key_wrapped` empty on every row.
   The copy holds wrapped keys: keep it private, and never print, commit or send it.
   It is evidence for a repair, not an undo. "After the backup key conversion" deletes it.
3. Note the number of the current revision. It is the rollback target.

   ```bash
   helm history exomem-platform --namespace exomem-platform --max 1
   ```

   Expect one row, with the status `deployed`.
4. Note the time of the copy as `date -u +%FT%TZ` prints it. "After the backup key conversion" uses it.
5. The upgrade must run without `--atomic`: pass `--crossed backup-key-conversion` to the command and it
   drops `--atomic` itself, or drop it in the manual fallback's command. A failed wait then leaves the new
   cellctl running.

#### If the upgrade fails

The upgrade can fail at its apply or at its wait. Either way, the new cellctl can still run and convert cells.

1. Stop cellctl, so that no cell converts during the count.

   ```bash
   kubectl --namespace exomem-cloud scale deployment/cellctl --replicas=0
   ```

2. List the cellctl pods. Expect `No resources found`. If a pod remains, run this step again.

   ```bash
   kubectl --namespace exomem-cloud get pods --selector app.kubernetes.io/name=cellctl
   ```

3. Count the converted cells.

   ```bash
   psql "$EXOMEM_CELLCTL_DSN" --command "SELECT count(*) FROM exomem_cloud_cells WHERE cell_root_key_wrapped IS NOT NULL"
   ```

4. If the count is 0, roll back to the revision that you noted in step 3 of "Before the upgrade".

   ```bash
   helm rollback exomem-platform <revision> --namespace exomem-platform
   ```

   Expect `Rollback was a success!`
5. After the rollback, run the count of step 3 again. Expect 0. If it is above 0, do step 8.
6. If the count is 0, delete the temporary copy. Nothing converted, so the copy has no use.

   ```bash
   rm "$EXOMEM_UPGRADE_DIR/cell-keys-before.csv"
   ```

7. After the rollback, list the cellctl Deployment.

   ```bash
   kubectl --namespace exomem-cloud get deployment/cellctl
   ```

   If it shows 0 replicas, scale it to 1.

   ```bash
   kubectl --namespace exomem-cloud scale deployment/cellctl --replicas=1
   ```

8. If a count is above 0, fix forward and never roll back. Correct the cause, then run the upgrade again
   without `--atomic` (`--crossed backup-key-conversion` on the command, or the manual fallback's variant).

Expected result: after a rollback, the count is 0, the copy is gone and the old cellctl runs. Stop the
change there, find the cause and start again from "Before the upgrade".
After a fix forward, the upgrade ends without an error.

### Manual fallback

Use these steps when the command cannot run or a review step needs a hand check. An upgrade
that crosses 0.147.0 also needs "Cross the backup key conversion" above, whichever path runs it.

Helm loads the chart's own `values.yaml` as the defaults, so pass only the private values file.
Do not add `--values infra/helm/platform/values.yaml`. That second pass turns the default
`cellStorage.local.maxCellGib: null` into a deleted key, and the schema then refuses the render with
`missing property 'maxCellGib'`.

#### Review the release

Run these steps before every upgrade. `EXOMEM_UPGRADE_DIR` is a private directory outside the repository.
A render holds the private values, so keep it out of tickets and chat.

```bash
: "${EXOMEM_PLATFORM_VALUES:?set the reviewed private platform values path}"
: "${EXOMEM_UPGRADE_DIR:?set a private upgrade directory outside the repository}"
umask 077
```

1. Build the chart dependencies. The script registers each chart repository, then runs `helm dependency build`.

   ```bash
   uv run --frozen python infra/scripts/helm_dependencies.py infra/helm/platform
   ```

   Expect one archive in `infra/helm/platform/charts/` for each dependency in `Chart.yaml`.
   A missing archive stops every render, even for a dependency that is off.
2. Render the new release without the cluster. `--no-hooks` leaves out the hook objects, which `helm get manifest` never holds.

   ```bash
   helm template exomem-platform infra/helm/platform --namespace exomem-platform --no-hooks \
     --values "$EXOMEM_PLATFORM_VALUES" > "$EXOMEM_UPGRADE_DIR/new.yaml"
   ```

   Expect no error. A schema error names the key to change.
3. Save the manifest of the live release.

   ```bash
   helm get manifest exomem-platform --namespace exomem-platform > "$EXOMEM_UPGRADE_DIR/live.yaml"
   ```

4. Compare the two manifests. Name each added, removed or changed object, and each workload whose pod template changes.

   ```bash
   diff -u "$EXOMEM_UPGRADE_DIR/live.yaml" "$EXOMEM_UPGRADE_DIR/new.yaml"
   ```

   Expect only changes that the release should make. `helm template` cannot read the cluster, so its render can add
   the scheduler's state ConfigMaps that the live release omits. Accept only those extra objects.
5. Run a server-side dry run of the upgrade. It prints the whole render, so write the output to the private directory.

   ```bash
   helm upgrade --install exomem-platform infra/helm/platform --namespace exomem-platform \
     --values "$EXOMEM_PLATFORM_VALUES" --dry-run=server > "$EXOMEM_UPGRADE_DIR/dry-run.yaml"
   ```

   Expect no error. A `lookup` check or an ownership conflict names the object.

Expected result: the render has no error, the diff holds only the changes that the release should make,
and the dry run ends without an error.

#### Upgrade the release

1. Apply the namespaces.

   ```bash
   helm template exomem-platform infra/helm/platform --namespace exomem-platform \
     --values "$EXOMEM_PLATFORM_VALUES" --show-only templates/namespaces.yaml | kubectl apply -f -
   ```

2. If the upgrade does not cross the conversion, upgrade the release.

   ```bash
   helm upgrade --install exomem-platform infra/helm/platform --namespace exomem-platform \
     --values "$EXOMEM_PLATFORM_VALUES" \
     --atomic --wait --timeout 10m
   ```

3. If the upgrade crosses the conversion, run this command instead. It has no `--atomic`.

   ```bash
   helm upgrade --install exomem-platform infra/helm/platform --namespace exomem-platform \
     --values "$EXOMEM_PLATFORM_VALUES" \
     --wait --timeout 10m
   ```

Expected result: Helm prints `Release "exomem-platform" has been upgraded.` Then run "Verify" below.

Do not run this upgrade while a cell restore runs
([cloud-operator-restore.md](cloud-operator-restore.md)).

A cellctl release can change the pod template it renders for a backup Job.
The release that lets a local cell's backup Job tolerate its node's cordon is
one. A backup Job that is still running cannot take the new template, because
Kubernetes refuses any change to a Job's template. cellctl then parks that
cell's apply once and retries it after its backoff, and the cell keeps
serving. With local storage enabled, every local cell whose hourly backup runs
during the upgrade parks once. Expect it; it needs no action.

The cellctl release that splits an export into a pause, a pack and an upload
runs the pause with the cell's own image. Until the rollout reaches a cell,
each export of that cell fails with `EXPORT_FAILED`. The owner retries it
after the rollout.

#### Verify

```bash
kubectl -n exomem-cloud wait --for=condition=Available deployment/cellctl --timeout=180s
kubectl -n exomem-cloud wait --for=condition=Available deployment/exomem-cloud-gateway --timeout=180s
kubectl -n exomem-platform get cronjob \
  exomem-hosted-scheduler-exomem-access-delivery exomem-hosted-scheduler-exomem-reconcile \
  -o jsonpath='{range .items[*]}{.metadata.name} suspend={.spec.suspend}{"\n"}{end}'
kubectl get storageclass exomem-cloud-encrypted
```

Both scheduler CronJobs must print `suspend=false` while Cloud is enabled.

#### Record the platform Deployment

The `cloud-deploy` workflow sets a release's cell image only when the platform runs that release's chart and
controller. A GitHub Deployment in the `cloud-platform` environment records which release the platform runs.
Record one after each successful upgrade.

The workflow reads only the newest successful platform Deployment. So each Deployment lists every barrier entry
crossed so far, not only the entries that its own upgrade crossed.

1. Set the release tag that you upgraded to, for example `TAG=v0.171.0`.
2. If this is the first platform Deployment, list every barrier entry at or below the live release as crossed:

   ```bash
   CROSSED=$(jq -c --arg v "${TAG#v}" '[.barriers[] | select((.version | split(".") | map(tonumber)) <= ($v | split(".") | map(tonumber))) | .id]' infra/contracts/upgrade-barriers-v1.json)
   ```

3. Otherwise, read the list from the newest successful platform Deployment:

   ```bash
   PREVIOUS=$(for id in $(gh api "repos/Artexis10/exomem/deployments?environment=cloud-platform&per_page=20" --jq '.[].id'); do if [ "$(gh api "repos/Artexis10/exomem/deployments/$id/statuses?per_page=1" --jq '.[0].state')" = success ]; then gh api "repos/Artexis10/exomem/deployments/$id" --jq '.payload | if type == "string" then fromjson else . end | .barriers_crossed // [] | tojson'; break; fi; done)
   ```

4. After you did each entry's runbook step, add the entries that this upgrade crossed. With none, use `'[]'`:

   ```bash
   CROSSED=$(jq -cn --argjson previous "$PREVIOUS" --argjson new '["secret-matrix-0.172.0"]' '$previous + $new | unique')
   ```

   The command prints an error when `PREVIOUS` is empty. Then no successful Deployment exists, and step 2 applies.
5. Create the Deployment and mark it successful:

   ```bash
   id=$(jq -n --arg ref "$TAG" --argjson crossed "$CROSSED" '{ref: $ref, environment: "cloud-platform", auto_merge: false, required_contexts: [], payload: {barriers_crossed: $crossed}, description: "Platform upgrade"}' | gh api repos/Artexis10/exomem/deployments --input - --jq .id) && gh api "repos/Artexis10/exomem/deployments/$id/statuses" -f state=success --jq .state
   ```

6. Start the workflow on `main`, so that it sets the release's cell image:

   ```bash
   gh workflow run cloud-deploy.yml --ref main -R Artexis10/exomem
   ```

Expected result: step 5 prints `success`, and the next `cloud-deploy` run ends with `cloud-deploy: CANARY_ON_TARGET`
or `cloud-deploy: CELL_IMAGE_CURRENT`.

If the upgrade failed, record nothing: the newest successful Deployment keeps naming the release the platform runs.

## Cell releases

Each release that succeeds on its tag starts the `cloud-deploy` workflow
(`.github/workflows/cloud-deploy.yml`). The workflow verifies the cell image's attestation and sets it as
Substrate's `cell_image` with the deploy token. cellctl then rolls the canary at once, and every other cell in its
own upgrade window. The workflow never clears a pause and never sends an older release. Its log prints codes only.

### Enable the workflow

Do these steps once, after Substrate has shipped the image history and you have run its seed again
(Substrate's Cloud operations runbook, "Seed the image history"). These are live settings of the repository.

1. Create the `cloud-deploy` environment, with a branch policy:

   ```bash
   echo '{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}' | gh api -X PUT repos/Artexis10/exomem/environments/cloud-deploy --input -
   ```

2. Admit only `main`:

   ```bash
   gh api repos/Artexis10/exomem/environments/cloud-deploy/deployment-branch-policies -f name=main -f type=branch
   ```

3. Add the deploy token. Paste it at the prompt:

   ```bash
   gh secret set EXOMEM_DEPLOY_TOKEN --env cloud-deploy -R Artexis10/exomem
   ```

4. Add the alert receiver URL that carries the `cloud-deploy` capability. Paste it at the prompt:

   ```bash
   gh secret set EXOMEM_CLOUD_DEPLOY_ALERT_URL --env cloud-deploy -R Artexis10/exomem
   ```

5. Set Substrate's public origin. Load it into `EXOMEM_SUBSTRATE_URL` first, as an `https://` URL:

   ```bash
   gh variable set EXOMEM_SUBSTRATE_URL --env cloud-deploy --body "$EXOMEM_SUBSTRATE_URL" -R Artexis10/exomem
   ```

6. Optional: set the poll interval and timeout in seconds. The defaults are 30 and 3600.

   ```bash
   gh variable set CLOUD_DEPLOY_POLL_INTERVAL_SECONDS --body 30 -R Artexis10/exomem && gh variable set CLOUD_DEPLOY_POLL_TIMEOUT_SECONDS --body 3600 -R Artexis10/exomem
   ```

7. Check the branch policy:

   ```bash
   gh api repos/Artexis10/exomem/environments/cloud-deploy/deployment-branch-policies --jq '.branch_policies[].name'
   ```

Expected result: step 7 prints only `main`.

To stop the automation, delete `EXOMEM_DEPLOY_TOKEN` from the environment. The workflow then prints
`cloud-deploy: CLOUD_DEPLOY_NOT_CONFIGURED` and sets nothing.

### Read and clear a paused rollout

Load `EXOMEM_ADMIN_TOKEN` and `EXOMEM_SUBSTRATE_URL` into the shell for these steps only.

1. Read the rollout view:

   ```bash
   curl --fail-with-body -sS -H "Authorization: Bearer $EXOMEM_ADMIN_TOKEN" "$EXOMEM_SUBSTRATE_URL/api/exomem/admin/cloud-release/rollout" | jq .view
   ```

2. Find the held cell and its error in cellctl's log:

   ```bash
   kubectl -n exomem-cloud logs deployment/cellctl --since=24h | grep -E 'cloud-rollout-paused|failed with|rollout pauses'
   ```

3. Fix the cause. For a broken release, fix forward: set the fixed release's image and tag with the admin token.

   ```bash
   jq -n --arg image "$FIXED_IMAGE" --arg release "$FIXED_TAG" '{cellImage: $image, release: $release}' | curl --fail-with-body -sS -X PUT -H "Authorization: Bearer $EXOMEM_ADMIN_TOKEN" -H 'Content-Type: application/json' --data @- "$EXOMEM_SUBSTRATE_URL/api/exomem/admin/cloud-release" | jq .success
   ```

4. Clear the pause:

   ```bash
   curl --fail-with-body -sS -X PUT -H "Authorization: Bearer $EXOMEM_ADMIN_TOKEN" -H 'Content-Type: application/json' --data '{"clearRolloutPause":true}' "$EXOMEM_SUBSTRATE_URL/api/exomem/admin/cloud-release" | jq .success
   ```

Expected result: step 4 prints `true`, step 1 then shows `"paused": false`, and `cloud-rollout-paused` resolves on
cellctl's next pass.

### End a forward-only hold

The error `UPGRADE_FORWARD_ONLY` means that an attempt across a one-way `cell` barrier missed readiness. The target
may have changed state that the previous image cannot read, so cellctl did not restore. The cell stays stopped under
its upgrade hold, with the attempt's restore point kept.

Do one of these:

- Fix forward: set the held cell's own image to the fixed release with the admin token. Then do steps 3 and 4 of
  "Read and clear a paused rollout". Without the cell's own image, cellctl retries the cell's pinned target, not the
  fixed release. A second miss holds the cell again. After the cell is Ready on the fixed release, clear its own
  image with the admin token, so that later releases reach it.
  1. Load `EXOMEM_ADMIN_TOKEN` and `EXOMEM_SUBSTRATE_URL` into the shell, as in "Read and clear a paused rollout". Set `CELL_ID` to the held cell, and `FIXED_IMAGE` to the fixed release's image. Choose only a release the canary already runs: a cell's own image bypasses the canary-first rule.
  2. Set the cell's own image. The token goes in through a header file that the shell builds from the variable, so it never appears in curl's argument list:

     ```bash
     jq -n --arg cell "$CELL_ID" --arg image "$FIXED_IMAGE" '{cellId: $cell, cellDesiredImage: $image}' | curl --fail-with-body -sS -X PUT -H @<(printf 'Authorization: Bearer %s\n' "$EXOMEM_ADMIN_TOKEN") -H 'Content-Type: application/json' --data @- "$EXOMEM_SUBSTRATE_URL/api/exomem/admin/cloud-release" | jq .success
     ```

  3. Clear the pause (step 4 of "Read and clear a paused rollout"). Wait until the cell is Ready on the fixed release.
  4. Clear the cell's own image. A `null` value returns the cell to the fleet's `cell_image`:

     ```bash
     jq -n --arg cell "$CELL_ID" '{cellId: $cell, cellDesiredImage: null}' | curl --fail-with-body -sS -X PUT -H @<(printf 'Authorization: Bearer %s\n' "$EXOMEM_ADMIN_TOKEN") -H 'Content-Type: application/json' --data @- "$EXOMEM_SUBSTRATE_URL/api/exomem/admin/cloud-release" | jq .success
     ```

  Expected result: steps 2 and 4 each print `true`.
- Restore by hand with [cloud-operator-restore.md](cloud-operator-restore.md), from the attempt's own backup
  (`exomem.io/pre-upgrade-snapshot` on the cell's StatefulSet). Then set the cell's own image with the admin token,
  and do step 4.

### Never withdraw across a one-way barrier

Do not set `cell_image` back to a release before a one-way `cell` barrier that cells already run past. cellctl
refuses that move: it starts no attempt, keeps each cell on its image, and pauses the rollout with
`UPGRADE_BACKWARD_CROSSING`. Fix forward with a new release instead, then clear the pause.

### Respond to a release or rollout alert

| Alert | Meaning | Step |
|---|---|---|
| `cloud-release-platform-needed` | The release changes the chart or cellctl since the platform's release, or no platform Deployment exists | Upgrade the platform to the release ("Render and upgrade the release"), then "Record the platform Deployment". The next run resolves the alert |
| `cloud-release-barrier` | A barrier entry at or below the release is not crossed. The workflow log names its ID | Do the entry's runbook step (`runbook` in `infra/contracts/upgrade-barriers-v1.json`), upgrade the platform, and record the Deployment with the ID in `CROSSED` |
| `cloud-rollout-paused` | The rollout is paused, for any error code | "Read and clear a paused rollout". For `UPGRADE_FORWARD_ONLY`, "End a forward-only hold" |
| `cloud-rollout-held` | A cell's upgrade or restore hold is older than `alerts.rollout_held_warn_seconds` | Find the cell in cellctl's log (`grep cloud-rollout-held`). Check its pod and its backup or restore Job with `kubectl -n exo-cell-<cell id> get pods,jobs` |
| `cloud-rollout-stalled` | A cell has stayed behind its target for longer than `alerts.rollout_stalled_warn_seconds` | Find the cell in cellctl's log. A busy cell rolls only in the last `cellctl.upgradeWindow.forceMinutes` of its window: raise that value, or set `cellctl.upgradeWindow.enabled: false` for one platform upgrade |
| `cloud-rollout-canary-waiting` | The canary has stayed behind its target for longer than `alerts.canary_wait_warn_seconds` | Check the canary's pod and readiness. A canary that is stopped or refused holds every tenant |

### Rotate a key under upgrade windows

A rotation that changes the render digest, such as a new cell-token key version, restarts each cell in its own
upgrade window. A step that retires the previous key version must wait until every cell runs the new render.

1. Note the time when the rotation's platform upgrade finished, in UTC.
2. List each cell pod's start time:

   ```bash
   kubectl get pods -A -l app.kubernetes.io/name=exomem-cell -o custom-columns=NAMESPACE:.metadata.namespace,STARTED:.status.startTime
   ```

3. Retire the previous version only when every cell pod started after the time from step 1.

For an urgent re-render, set `cellctl.upgradeWindow.enabled: false` for the rotation's platform upgrade. cellctl then
re-applies cells one at a time as soon as each is eligible. Set it back to `true` after the retire step.

## After the backup key conversion

Do this section once, after the upgrade's verify step (the command runs it, or "Manual fallback › Verify").
When a follow-up release is planned, such as the second release of a key custody cutover, do it after that
release instead.

The temporary copy must not outlive the upgrade. cellctl erases a deleted cell's wrapped keys on its first pass.
A copy would keep the key of a cell after its deletion, and defeat the key expiry that the platform promises.

A cell with local storage backs up each hour. Another cell backs up once a day, in the window that
`cells.backupWindow` sets. Its default is 02:00 to 05:00 UTC.

1. Note the time when the last cell pod is Ready, as `date -u +%FT%TZ` prints it. Call it the rollout end.
2. Note the time of the temporary copy from "Before the upgrade", in UTC. Call it the copy time.
3. Put the copy time in this query. Run it until each `last_backup_at` is after the rollout end.

   ```bash
   psql "$EXOMEM_CELLCTL_DSN" --command "SELECT cell_id, last_backup_at FROM exomem_cloud_cells WHERE cell_root_key_wrapped IS NOT NULL AND created_at < '<copy time>+00' AND desired_state IN ('running','read_only') ORDER BY cell_id"
   ```

   The query leaves out a cell created after the copy, because the copy does not hold it.
4. After 26 hours, read the cellctl log since the rollout end for each cell that still has no such backup.

   ```bash
   kubectl --namespace exomem-cloud logs deployment/cellctl --since-time=<rollout end> | grep -F '<cell id>'
   ```

5. If the cellctl pod restarted since the rollout end, run the command of step 4 again with `--previous`.

   A cell with nightly backups logs a failure only inside its backup window. Finding no line is not a cause.

   cellctl unwraps the backup key before it makes the backup Job. A failed unwrap makes no Job, only this log line.
6. If a `reconcile failed for cell` line names `unwrap_secret` or `_resolve_secret_material`, keep the copy. Stop here.
7. After a stop at step 6, fix forward as "If the upgrade fails" step 8 says. Then repeat this section.
8. For any other cause that you found, fix it so that the cell can back up.
9. After three windows with no backup and no log line, find why cellctl skips the cell, such as a park.
10. Keep the copy while a listed cell has no backup after the rollout end. Repeat step 3 after its next window.
11. When each listed cell has a backup after the rollout end, delete the temporary copy of the key columns.

    ```bash
    rm "$EXOMEM_UPGRADE_DIR/cell-keys-before.csv"
    ```

Expected result: `cell-keys-before.csv` is gone, and each listed cell has a backup that is newer than the rollout end.
A cell that is deleted meanwhile leaves the query, so it does not hold the copy.
