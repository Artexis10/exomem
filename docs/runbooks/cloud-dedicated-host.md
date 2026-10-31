<!-- authority:non-specification -->

# Dedicated K3s agents (servers Terraform does not create)

Add, unlock, repair or remove an Exomem Cloud K3s agent on a machine Terraform
does not create: a Hetzner dedicated or auction server, or a server at another
provider (openspec `move-cloud-cells-to-local-storage` D2 and D8). You install
the OS. Ansible does the rest:

- a private link to the cluster: a vSwitch VLAN on Hetzner, WireGuard elsewhere;
- md RAID1 over two data disks or partitions, with LUKS2 on top;
- Clevis bound to Tang on the K3s server, plus an escrowed recovery passphrase;
- the `cells` volume group and its thin pool for TopoLVM;
- a K3s agent that starts only after the storage is unlocked.

Terraform-created agents keep [cloud-node-pool.md](cloud-node-pool.md).

## Preconditions

- The server is bought and reachable in its provider's rescue system or
  installer. Nothing here buys hardware or calls a provider API.
- Exomem Cloud runs TopoLVM with the `thin` device class, or you accept that
  the agent publishes 0 cell slots until it does. The join says which.
- The Tang key set is escrowed, once, before the first dedicated agent. Generate
  it with `jose` and hand it straight to SOPS. It is never written to disk or
  shown:

  ```bash
  repo_root="$(git rev-parse --show-toplevel)"
  matrix="$repo_root/infra/contracts/secret-destinations-v1.json"
  { jose jwk gen -i '{"alg":"ES512"}'; jose jwk gen -i '{"alg":"ECMR"}'; } | jq -cs '{keys: .}' \
    | infra/scripts/secret_handoff.py --matrix "$matrix" --repository-root "$repo_root" \
        --secret dedicated_host_tang_keys --version v1 \
        --destination ansible.hosted-node.tang-keys.active \
        --destination escrow.tang-keys.active --source stdin
  ```

  Escrowing activates nothing. Activate the key set in a reviewed commit that
  adds `"ansible.hosted-node.tang-keys.active": "v1"` to the `destinations` of
  `infra/contracts/active-ansible-selection-v1.json`.

- Each host has its own recovery passphrase, so each host has its own entry in
  `infra/contracts/secret-destinations-v1.json`. Add it in the change that adds
  the host, using the host name with every `-` written as `_` in the variable:

  ```json
  "dedicated_host_recovery_passphrase_exomem_agent_dx1": {
    "sources": [{"kind": "stdin"}],
    "destinations": {
      "ansible.hosted-node.recovery-passphrase-exomem-agent-dx1.active": {
        "kind": "sops_ansible_vars", "slot": "active",
        "target": "infra/secrets/ansible/recovery-passphrase-exomem-agent-dx1.{version}.sops.json",
        "variable": "dedicated_host_recovery_passphrase_exomem_agent_dx1"
      },
      "escrow.recovery-passphrase-exomem-agent-dx1.active": {
        "kind": "sops_escrow", "slot": "active",
        "target": "infra/secrets/escrow/recovery-passphrase-exomem-agent-dx1.{version}.sops.json",
        "secret_key": "passphrase"
      }
    }
  }
  ```

  Then generate and escrow it:

  ```bash
  openssl rand -base64 48 | tr -d '\n' | infra/scripts/secret_handoff.py \
    --matrix "$matrix" --repository-root "$repo_root" \
    --secret dedicated_host_recovery_passphrase_exomem_agent_dx1 --version v1 \
    --destination ansible.hosted-node.recovery-passphrase-exomem-agent-dx1.active \
    --destination escrow.recovery-passphrase-exomem-agent-dx1.active --source stdin
  ```

  In the same change, activate it: add
  `"ansible.hosted-node.recovery-passphrase-exomem-agent-dx1.active": "v1"` to
  `infra/contracts/active-ansible-selection-v1.json`.

## Install the OS

The host runs Ubuntu 24.04 with no swap, and two unformatted data disks or
partitions of equal size.

On Hetzner, boot the rescue system and copy
`infra/ansible/installimage/dedicated-host-autosetup.conf` to `/autosetup`. Fill in
the disks and host name, then run `installimage`. It puts the OS on a small
RAID1 and leaves the rest of both disks unallocated. Still in the rescue
system, add one unformatted partition per disk in the free space:

```bash
sgdisk --new=0:0:0 --typecode=0:fd00 --change-name=0:exomem-cells /dev/nvme0n1
sgdisk --new=0:0:0 --typecode=0:fd00 --change-name=0:exomem-cells /dev/nvme1n1
ls -l /dev/disk/by-id/ | grep -- '-part4'
```

On a server with separate data disks, list only the OS disks in the autosetup
and skip `sgdisk`. At another provider, use its installer the same way: OS on
its own disks or partitions, and two data disks or partitions left
unformatted.

Name data disks by `/dev/disk/by-id/`, because kernel names can change between
boots. The role refuses a data disk that holds a filesystem, a partition table
or another array. It refuses one that something holds or mounts in every case.
If a disk carries old signatures you mean to destroy, list it under `wipe` for
the bootstrap run, then remove that entry.

Enroll the host in NetBird with substrate-infra's `ansible/administration.yml`,
as [cloud-node-pool.md](cloud-node-pool.md#add-a-node) describes. `site.yml` refuses a host
it cannot administer over NetBird.

## Choose the private link

Precondition for either link: every node runs flannel at a `k3s_flannel_mtu`
of 1400, so VXLAN packets are 1400 bytes. A vSwitch carries at most 1400, and
the role's WireGuard interface 1420. Until the MTU is set, flannel takes the
Cloud interface's 1450, and a dedicated agent never receives those packets. The
role refuses a vSwitch or WireGuard host until the MTU is set, so roll it out
first, as in [Roll out the flannel MTU](#roll-out-the-flannel-mtu).

- **Hetzner dedicated: vSwitch.** Create the vSwitch in Robot, attach the
  server, and set the foundation variable. Plan and apply as usual. This adds
  one subnet and changes nothing else:

  ```bash
  # terraform.tfvars (foundation)
  #   vswitch = { id = 54321, vlan_id = 4000, subnet_cidr = "10.50.2.0/24" }
  infra/scripts/plan.sh foundation /run/user/$UID/foundation-vswitch.tfplan
  infra/scripts/apply_saved_plan.sh foundation /run/user/$UID/foundation-vswitch.tfplan
  ```

  Give the host an address in that subnet. It must not be the gateway, which
  is the subnet's first address.
- **Other provider: WireGuard.** Pick a private address outside the Hetzner
  network, for example from `10.51.0.0/24`. Every K3s node peers with the host
  over UDP 51820, and the inter-node firewall admits it on `wgexomem`.

## Roll out the flannel MTU

Run this once per cluster, before the first dedicated join, in an operator
window. The k3s role leaves `k3s_flannel_mtu` unset, and installs no flannel
configuration while it is unset. The rollout is a reviewed commit that sets
`k3s_flannel_mtu: 1400` in `infra/ansible/roles/k3s/defaults/main.yml`. 1400 is
`k3s_vswitch_mtu`, the smallest dedicated link.

A node's first `site.yml` run with the value installs the flannel
configuration. It adds `flannel-conf` to the node's K3s configuration, and
`cluster-cidr` on the server. It then recreates `flannel.1`: it stops K3s,
deletes the device and starts K3s. A plain restart would keep the old MTU. Cell
pods keep running while K3s is stopped. The run fails unless `flannel.1` and
`FLANNEL_MTU` in `/run/flannel/subnet.env` both read 1350, which is 1400 less
VXLAN's 50 bytes.

1. Check that every node's pod range is inside the role's `k3s_cluster_cidr`, `10.42.0.0/16`: `kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name} {.spec.podCIDR}{"\n"}{end}'`. If a range is outside it, stop: the role's value does not match the cluster.
2. Do not start a platform deploy or a cell image release until the last step is done.
3. Merge the reviewed commit only now: after the merge, every `site.yml` run, the deploy gate included, rolls the MTU out on each node it reaches.
4. Converge the server, with the inventory and variables of [Add the host](#add-the-host): `infra/scripts/ansible_with_sops.sh --inventory infra/ansible/inventory.yml "${fleet_vars[@]}" -- --limit exomem-alpha`.
5. If the cluster has agents, converge one canary agent, then each other agent, one at a time. End the same command with `-- --limit 'exomem-alpha,<agent>'`: an agent's run needs the server's facts from the same run, and the server reports no change.
6. On each node, read the MTU: `ssh exomem-admin@<node> 'cat /sys/class/net/flannel.1/mtu; grep FLANNEL_MTU /run/flannel/subnet.env'`.
7. Only if the cluster has agents, and outside the 02:00–05:00 UTC backup window, restart the platform workloads that run on the pod network, so that they take the new MTU. Workloads on the host network keep the host's MTU and are skipped:

   ```bash
   for namespace in kube-system exomem-platform exomem-cloud exomem-edge; do
     kubectl get deployments,daemonsets,statefulsets --namespace "$namespace" -o json \
       | jq -r '.items[] | select(.spec.template.spec.hostNetwork != true) | "\(.kind | ascii_downcase)/\(.metadata.name)"' \
       | xargs -r kubectl rollout restart --namespace "$namespace"
   done
   ```

Expected result: each run reports "Require flannel.1 and new pods at the
configured MTU" as passed, and step 6 prints `1350` and `FLANNEL_MTU=1350` on
every node. A pod that started before its node's run keeps the old MTU until it
restarts, which matters only for traffic to a pod on another node. With the
server as the only node, which is production until an agent token is
selected, no pod traffic crosses nodes, so skip step 7.

Cells take the new MTU when cellctl next recreates their pods, for example at
the next cell image release. Do not delete a cell's pod by hand: that bypasses
cellctl's holds and its backup window.

If a run fails at that check, run the same command again. Every run reads each
node's `flannel.1`, and recreates it while it runs at another MTU.

To undo while no dedicated node is joined, set `k3s_flannel_mtu: 1450`, the
Cloud network's MTU, in a reviewed commit, and repeat steps 4 to 7. Flannel
then sends 1450-byte packets again, as it did with the value unset. The role
refuses 1450 once a vSwitch or WireGuard host is in the inventory, and it
refuses to unset the value once it has applied one.

## Add the host

Describe the host in a private JSON file. It is never committed:

```json
{
  "exomem-agent-dx1": {
    "ipv4": "203.0.113.40",
    "admin_address": "100.64.0.40",
    "private_ip": "10.50.2.40",
    "link": "vswitch",
    "data_disks": [
      "/dev/disk/by-id/nvme-SAMSUNG_A-part4",
      "/dev/disk/by-id/nvme-SAMSUNG_B-part4"
    ]
  }
}
```

Then generate the inventory and converge every node. Never pass `--limit`:
every node must learn the new peer, and the join checks that it did.

```bash
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

`active_ansible_vars.py` passes the version of each hosted-node Ansible
variable that `infra/contracts/active-ansible-selection-v1.json` selects: the
tokens, the etcd keys, the Tang keys and each dedicated host's passphrase. It
refuses when a selected version's file is missing. A version that is escrowed
but not selected is never passed.

The first run fixes the thin pool's geometry: 64 KiB chunks, and 64 bytes of
metadata per chunk computed from the pool's size. Above about 15.8 TiB of pool,
the metadata stops at LVM's 15.81 GiB limit; set a larger
`dedicated_host_thin_chunk_kib` in `group_vars/hosted_nodes.yml` before that run.

Every later `site.yml` run needs the Tang keys and every dedicated host's
passphrase. Each run proves that the escrowed passphrase still opens the array
and that Tang still unlocks it.

If this run is the first with an agent token selected, it restarts the K3s
server with that token. Run steps 4 to 6 of
[Rotate the agent token](#rotate-the-agent-token) straight after it, before
anything else restarts the server, with step 6's first-selection credentials.

A WireGuard host's public key goes in the host list, so that a run limited to
the server can configure the host as a peer without reaching it. After the
host's first join, and after each rotation of its key:

1. Copy the key from the run's output line `Record <key> as exomem-agent-dx1's wireguard_public_key`.
2. Add `"wireguard_public_key": "<key>"` to the host's entry in the host list.
3. Regenerate the inventory as above.

Expected result: the next run prints no `Record` line for the host. If the
host's own key no longer matches its record, the run stops before it configures
any peer, and prints the host's key to record.

## Verify

```bash
ssh exomem-admin@100.64.0.40 'cat /proc/mdstat; sudo cryptsetup status exomem_cells; sudo clevis luks list -d /dev/md/exomem-cells; sudo vgs cells; sudo lvs cells'
ssh exomem-admin@100.64.0.40 'systemctl is-active exomem-cells-ready.service k3s-agent.service'
kubectl get node exomem-agent-dx1 -o jsonpath='{.metadata.annotations.capacity\.topolvm\.io/thin}'
```

The array is a clean or resyncing RAID1, `exomem_cells` is an active LUKS2
mapping, Clevis lists one `tang` binding to the server's private address, and
`cells/pool0` is a thin pool. The node annotation is a positive byte count.
Then reboot the host once and check that it comes back with K3s running and no
operator action.

## Unlock by hand

A rebooting agent waits for Tang. If Tang is down, the host still boots to
NetBird, and K3s waits in `exomem-cells-ready.service`. Unlock with the
escrowed passphrase. It travels from SOPS to the host over SSH and is never
shown:

```bash
sops decrypt --extract '["passphrase"]' \
  infra/secrets/escrow/recovery-passphrase-exomem-agent-dx1.v1.sops.json \
  | ssh exomem-admin@100.64.0.40 'sudo cryptsetup open --key-file=- /dev/md/exomem-cells exomem_cells'
```

`exomem-cells-ready.service` waits for the `exomem_cells` mapping, then
activates the `cells` volume group, and K3s starts after it. A `site.yml` run
does the same unlock. Check the result:

```bash
ssh exomem-admin@100.64.0.40 'systemctl is-active exomem-cells-ready.service k3s-agent.service'
```

Expected result: the command prints `active` twice.

## Tang lost or replaced

If the server is rebuilt, `site.yml` reinstalls the escrowed Tang keys. Every
existing binding keeps working, and nothing is rebound.

If the Tang keys themselves are lost:

1. Escrow a new key set as the next version, as in Preconditions, with `--version v2`.
2. In a reviewed commit, set `ansible.hosted-node.tang-keys.active` to `v2` in `infra/contracts/active-ansible-selection-v1.json`.
3. Run `site.yml` as in Add the host.
4. On each dedicated host, list the bindings: `sudo clevis luks list -d /dev/md/exomem-cells`.
5. Remove the binding the role replaced: `sudo clevis luks unbind -d /dev/md/exomem-cells -s <old slot> -f`.

Expected result: each host lists one `tang` binding, and `site.yml` reports no
change. In step 3, each host's role finds that its binding no longer unlocks,
and binds again with its passphrase.

## Rotate the Tang keys

Tang keeps a key it no longer advertises as a hidden `.jwk` file, and still
answers recovery requests with it. Old bindings therefore keep unlocking until
you delete those files.

1. Escrow a new key set as the next version, as in Preconditions, with `--version v2`.
2. In a reviewed commit, set `ansible.hosted-node.tang-keys.active` to `v2` in `infra/contracts/active-ansible-selection-v1.json`.
3. Run `site.yml` as in Add the host. The server now advertises the v2 keys.
4. Compute the new signing key's thumbprint on your machine. Only the thumbprint is printed:

   ```bash
   thp=$(sops decrypt --extract '["jwks"]' infra/secrets/escrow/tang-keys.v2.sops.json \
     | jq -c '.keys[] | select(.alg == "ES512")' | jose jwk thp -i- -a S256)
   ```

5. On each dedicated host, note the current Tang slot: `sudo clevis luks list -d /dev/md/exomem-cells`.
6. Bind a new slot pinned to the new key. The passphrase travels from SOPS over SSH:

   ```bash
   sops decrypt --extract '["passphrase"]' \
     infra/secrets/escrow/recovery-passphrase-exomem-agent-dx1.v1.sops.json \
     | ssh exomem-admin@100.64.0.40 \
       "sudo clevis luks bind -f -d /dev/md/exomem-cells -k - tang '{\"url\":\"http://10.50.1.10:7500\",\"thp\":\"$thp\"}'"
   ```

7. Remove the slot you noted in step 5: `sudo clevis luks unbind -d /dev/md/exomem-cells -s <old slot> -f`.
8. When every host is rebound, delete the hidden keys on the server: `sudo find /var/lib/tang -name '.*.jwk' -delete`.
9. Run `site.yml` again.

Expected result: step 9 reports no change on any dedicated host. A host you
missed fails its Tang test in step 9, and binds again from its passphrase.
Until then, it needs a manual unlock after a reboot. If the key directory is
not `/var/lib/tang`, `systemctl cat tangd@.service` shows it.

## Replace a failed disk

`site.yml` reports a degraded array but does not repair it. cellctl emails
`cell-array-degraded` while any md array on a dedicated host is inactive, has a
failed member, or has fewer active members than it requires. The email names no
host: `kubectl -n exomem-cloud logs deployment/cellctl | grep cell-array-degraded`
lists the nodes. After the provider swaps the disk, run these steps on the
host. `<new>` and `<surviving>` are whole disks, for example `/dev/nvme1n1` and
`/dev/nvme0n1`.

1. Identify the new disk and the surviving disk: `ls -l /dev/disk/by-id/` and `cat /proc/mdstat`.
2. Copy the surviving disk's partition table onto the new disk. The `--replicate` value is the target:
   `sudo sgdisk --replicate=/dev/<new> /dev/<surviving>`.
3. Give the copy its own GUIDs: `sudo sgdisk --randomize-guids /dev/<new>`.
4. For each OS array in `/proc/mdstat` that lost a member, add the new disk's matching partition:
   `sudo mdadm --manage /dev/<md array> --add /dev/<new partition>`.
5. Add the new data partition to the cell array:
   `sudo mdadm --manage /dev/md/exomem-cells --add /dev/disk/by-id/<new disk>-part4`.
6. If `/proc/mdstat` shows no array for the EFI system partition, copy that partition. The `of=` value is the new one:
   `sudo dd if=/dev/<surviving ESP> of=/dev/<new ESP> bs=4M`.
7. Install the boot loader on the new disk. On UEFI, run `sudo dpkg-reconfigure grub-efi-amd64` and select both ESPs.
   On BIOS boot, run `sudo grub-install /dev/<new>`.
8. Replace the old disk's ID in `data_disks` in the host list, and regenerate the inventory.
9. If the host joins over WireGuard, delete its key with `sudo rm /etc/wireguard/wgexomem.key`. Then run `site.yml` without `--limit`.
10. If the host list records the host's WireGuard key, step 9 stops and prints the new key. Record it as in [Add the host](#add-the-host), and run `site.yml` again.

Expected result: `cat /proc/mdstat` shows every array rebuilding, then `[UU]`.
A `RESOLVED: cell-array-degraded` email follows within a few minutes of the
rebuild finishing. After the last `site.yml` run, every peer holds the host's new public key and the tunnel is up.
The removed disk held the old WireGuard key, the agent token
(`/etc/rancher/k3s/config.yaml`) and the agent's kubelet client certificate
unencrypted. Unless the provider confirms that it destroyed the disk,
[rotate the agent token](#rotate-the-agent-token), then
[the client CA](#rotate-the-client-ca).

## Thin pool at 80%

cellctl emails `thin-pool-high` while the data use or the metadata use of any
dedicated host's thin pool is at or above 80%. The email names no host. cellctl
reads TopoLVM's `topolvm_thinpool_data_percent` and
`topolvm_thinpool_metadata_percent` for the `thin` device class, once a minute.

1. List the hosts that fire: `kubectl -n exomem-cloud logs deployment/cellctl | grep thin-pool-high`.
2. Read the pool on that host: `sudo lvs --options lv_name,lv_size,data_percent,metadata_percent cells`.
3. If `pool0` shows metadata use at or above 80%, grow its metadata: `sudo lvextend --poolmetadatasize +<size>G cells/pool0`.
   The volume group keeps twice the metadata size free for this, and LVM stops at 15.81 GiB.
4. If `pool0` shows data use at or above 80%, delete the cells you no longer need, as in [cloud-cell-deletion.md](cloud-cell-deletion.md).
   The pool cannot grow, because it fills the array.

Expected result: `lvs` shows both percentages below 80 for `pool0`, and a
`RESOLVED: thin-pool-high` email follows within a few minutes.

## Cell unavailable

cellctl emails `cell-unavailable` while any serving cell has been unready for
`alerts.pending_operation_warn_seconds` in `infra/contracts/observability-v1.json`.
A serving cell is wanted `running` or `read_only` and is under no hold: no upgrade,
backup, restore or move. The alert covers every such cell, on a dedicated host or
not. A full volume, a crash loop and a lost node all raise it. cellctl counts from
the first pass that sees the cell unready, so a cellctl restart delays the email
by up to that threshold. The email names no cell.

1. List the cells that fire: `kubectl -n exomem-cloud logs deployment/cellctl | grep cell-unavailable`.
2. Set `CELL_ID` to a listed cell, then read the pod's IP and node: `kubectl -n "exo-cell-$CELL_ID" get pod cell-0 --output wide`.
3. If the pod has no IP, read why: `kubectl -n "exo-cell-$CELL_ID" describe pod cell-0`.
4. On that node, read the readiness reasons: `curl --silent --max-time 10 "http://<pod IP>:8765/health/ready"`.
   The `reasons` array names what blocks readiness, such as `retrieval_unavailable`.
5. If a reason points at the vault or its indexes, read the volume's free space on the node:
   `sudo df -h "/var/lib/kubelet/pods/<pod uid>/volumes/kubernetes.io~csi/<pv name>/mount"`.
   The pod UID is `kubectl -n "exo-cell-$CELL_ID" get pod cell-0 --output jsonpath='{.metadata.uid}'`.
   The PV name is `kubectl -n "exo-cell-$CELL_ID" get pvc cell-data --output jsonpath='{.spec.volumeName}'`.
6. If the volume is full, follow [Cell disk low](#cell-disk-low). Otherwise fix the reason that step 4 shows.

Expected result: the cell answers `200` on `/health/ready`, and a
`RESOLVED: cell-unavailable` email follows within a few minutes.

## Cell disk low

cellctl emails `cell-disk-low` while any serving cell had less than
`alerts.pvc_reserved_free_bytes` free at its last backup report. The key is in
`infra/contracts/observability-v1.json`. The report is the backup Job's own
reading of the cell's volume. It comes hourly for a cell on local storage and
nightly for every other cell, so the email can follow a full disk by up to a day.
A cell that is unready takes no backup, so `cell-unavailable` is the alert that
catches it. cellctl keeps the reading in memory only. After a restart, the alert
cannot resolve until each cell has a new report. The email names no cell.

1. List the cells that fire: `kubectl -n exomem-cloud logs deployment/cellctl | grep cell-disk-low`.
2. For each listed cell, raise `storage_gib` as in step 2 of [cloud-operator-import.md](cloud-operator-import.md#2-size-the-cell).
   The volume cannot shrink, so size for the vault's growth and not only for today's use.
3. Confirm that the claim grew: `kubectl -n "exo-cell-$CELL_ID" get pvc cell-data --output jsonpath='{.status.capacity.storage}'`.
4. On local storage, cellctl already grows a cell past 80% use. A cell that still fires is at its cap, or its node has no room.
   `storage-growth-blocked` fires with it.

Expected result: the claim shows the new size, and a `RESOLVED: cell-disk-low`
email follows the cell's next backup report.

## Cell recall lagging

cellctl emails `cell-recall-lagging` while any serving cell's recall serves a
catalogue that has not advanced for `alerts.recall_lag_warn_seconds` in
`infra/contracts/observability-v1.json`. Writes still work, and recall still
answers, but from the catalogue it last published, so new notes are missing from
search. The cell stays ready, so `cell-unavailable` does not fire. The email names no cell.

cellctl reads each serving cell's readiness once a minute on the cell's
health-only port, 8766. That port answers only `GET /health/ready`, with the
same answer as port 8765. The alert fires when `retrieval.state` is `lagging`
and `retrieval.catalogue_unchanged_seconds` is at or above the threshold. The
cell counts that age from when its own process first saw the catalogue, so a
cell restart starts it again from zero, and a cellctl restart does not.

The alert resolves only when the cell reads `retrieval.state` `ready` again,
which means that its catalogue has caught up. A younger lag, a cell with no ready
pod and a cell that does not answer each leave the alert as it was.

Do not restart the cell to clear the alert. A lagging cell still serves writes
and recall. A restarted cell stays unready until it has warmed again, and its
age starts again from zero, so you lose the measure of the stall.

1. List the cells that fire: `kubectl -n exomem-cloud logs deployment/cellctl | grep cell-recall-lagging`.
2. Set `CELL_ID` to a listed cell, then read the pod's IP and node: `kubectl -n "exo-cell-$CELL_ID" get pod cell-0 --output wide`.
3. On that node, read the cell's readiness: `curl --silent --max-time 10 "http://<pod IP>:8766/health/ready"`.
   `retrieval.lagging_scopes` names the scopes whose catalogue trails, and `retrieval.repair` shows the lexical repair.
4. If `retrieval.repair.phase` is not `idle`, read the answer again in ten minutes, and wait while the repair runs.
5. If `cell-disk-low` also lists this cell, follow [Cell disk low](#cell-disk-low).
6. Otherwise, save the readiness answer and the cell's log, and file the stall as a defect: `kubectl -n "exo-cell-$CELL_ID" logs cell-0 --since=1h`.

Expected result: the cell's readiness shows `retrieval.state` `ready`, and a
`RESOLVED: cell-recall-lagging` email follows within a few minutes.

## Move a cell to a new claim

A planned move takes a serving cell off its volume without losing a write.
cellctl stops the cell and takes a fresh stopped backup of its volume. It then
keeps the old volume as the rollback copy, restores the backup into a new claim
on the local class, and starts the cell only after the restore succeeds. The
cell is down from the stop until it is ready again.

Use it to migrate cells from Hetzner volumes after the cutover (OpenSpec task
7.2): the owner's cell first, then the rest one at a time, each accepted before
the next. `remove-agent.yml` uses the same move for the cells of an agent it
removes.

Preconditions:

- `cellStorage.local.enabled` is true. Without it, cellctl ignores the mark.
- A local node other than the cell's own has room for the cell.
- The cell is not in another hold: no upgrade, backup or restore runs on it.
  A move waits for an hourly backup to end.
- To move a cell off a local node, cordon that node first with
  `kubectl cordon "$NODE"`, or the new claim can land on it again. A cell on a
  Hetzner volume needs no cordon: its node has no local pool.

Steps, with `CELL_ID` set to the cell's ID:

1. Agree the window with the cell's owner. For the owner's cell, agree it
   before the move (OpenSpec task 7.1).
2. Read the volume the cell is on, and record its governance status:

   ```bash
   NS="exo-cell-$CELL_ID"
   VOLUME=$(kubectl get pv "$(kubectl -n "$NS" get persistentvolumeclaim cell-data -o jsonpath='{.spec.volumeName}')" -o jsonpath='{.spec.csi.volumeHandle}')
   kubectl -n "$NS" exec cell-0 -c exomem -- exomem governance-schema status --vault /data/vault --json > "governance-$CELL_ID-before.json"
   ```

   `$VOLUME` must be the row's `volume_id`. A mark naming any other volume moves nothing.
3. Mark the cell:

   ```bash
   kubectl annotate namespace "$NS" "exomem.io/move-volume=$VOLUME"
   ```

4. Watch the row, as the control database owner:

   ```sql
   SELECT hold_kind, observed_state, ready, volume_id, last_backup_at, last_error_code FROM exomem_cloud_cells WHERE cell_id = '<cell id>';
   ```

   Expect `hold_kind` `backup` while the stopped backup runs, then `restore`,
   then no hold, `ready` true and a new `volume_id`. `last_backup_at` moves to
   the time of the backup. Record the time from the mark to `ready`.
5. Accept the cell:
   1. Read its governance status again, as in step 2, into a second file. It must match the first.
   2. Through the owner's Exomem Cloud connector, ask for a note the vault held before the move. It must answer.
   3. Make one governed write, such as a short note, and read it back.
6. Remove the mark:

   ```bash
   kubectl annotate namespace "$NS" exomem.io/move-volume-
   ```

7. Record the retained volume, the rollback copy until the soak ends. It is the PV whose `volumeHandle` is `$VOLUME`, now `Released` with reclaim policy `Retain`.

Expected result: the cell serves from a new claim on the local class, and its
old volume is retained.

For the owner's cell, check the restart against the existing gate, cellctl's
readiness deadline (`cellctl.upgradeReadyDeadlineMinutes` in
`infra/helm/platform/values.yaml`, 30 minutes by default). The next upgrade
rolls back a cell that is not ready within it after its start. Compare it with
the time from the restore's end to `ready`, which `kubectl -n "$NS" get events`
shows. If the restart comes close to the deadline, raise it (the chart accepts
5 to 120) before the next upgrade.
No alert fires while a cell waits out this deadline: `cell-unavailable` skips a
cell under a hold, so a broken image stays down until the restore with only the
rollout pause to show it.

If the move fails:

- `MOVE_BACKUP_FAILED`: the stopped backup failed, and the claim and its
  volume are untouched. The cell stays stopped on its volume until you remove
  the mark (step 6), then starts there as before. If you cordoned its node,
  uncordon it first. cellctl's log names the backup Job's own reason.
- `RESTORE_FAILED` or `RELOCATION_REFUSED`: the move had already retired the
  claim, and its old volume is retained. Follow those codes in
  [cloud-node-loss.md](cloud-node-loss.md#relocate-the-cells-of-a-lost-agent).
- Removing the mark stops a move only until its backup succeeds. After that,
  the move goes on to its restore.

### Roll a moved cell back

Until the soak ends (OpenSpec task 8.1), a moved cell can go back to its
retained volume. It then serves the volume as it was when the move stopped it:
writes it accepted after the move are lost. Run this only when the moved cell
cannot be accepted.

1. Remove the mark. Once the row records the old volume again, a mark still
   naming it would start a new move:

   ```bash
   kubectl annotate namespace "$NS" exomem.io/move-volume-
   ```

2. Uncordon the old volume's node if it is cordoned, as during a host removal. `OLD_PV` is the retained volume you recorded in step 7 of the move. A local volume is pinned to that node, so the cell's pod cannot start there while it is cordoned. A Hetzner volume is pinned to a location, not a node, so `OLD_NODE` stays empty and nothing is uncordoned:

   ```bash
   OLD_NODE=$(kubectl get persistentvolume "$OLD_PV" -o jsonpath='{.spec.nodeAffinity.required.nodeSelectorTerms[*].matchExpressions[?(@.key=="topology.topolvm.io/node")].values[*]}')
   if [ -n "$OLD_NODE" ]; then kubectl uncordon "$OLD_NODE"; fi
   ```

3. Pause cellctl, and stop the cell:

   ```bash
   kubectl -n exomem-cloud scale deployment cellctl --replicas=0
   kubectl -n "$NS" scale statefulset cell --replicas=0
   ```

4. Retain the new volume, and retire its claim. `NEW_PV` is the claim's current volume:

   ```bash
   NEW_PV=$(kubectl -n "$NS" get persistentvolumeclaim cell-data -o jsonpath='{.spec.volumeName}')
   kubectl patch persistentvolume "$NEW_PV" --type=merge --patch '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
   kubectl -n "$NS" delete persistentvolumeclaim cell-data
   ```

5. Free the retained volume, `OLD_PV`, for a new claim: `kubectl patch persistentvolume "$OLD_PV" --type=json --patch '[{"op":"remove","path":"/spec/claimRef"}]'`. Expect its status `Available`.
6. Create claim `cell-data` in `$NS` bound to it: `volumeName: $OLD_PV`, the PV's `storageClassName` and capacity, access mode `ReadWriteOnce`. Expect `Bound`.
7. Release the row's identity by compare-and-set on the new volume's ID, as the control database owner. It must report `UPDATE 1`:

   ```sql
   UPDATE exomem_cloud_cells SET volume_id = NULL WHERE cell_id = '<cell id>' AND volume_id = '<new volume id>';
   ```

8. Resume cellctl: `kubectl -n exomem-cloud scale deployment cellctl --replicas=1`.

Expected result: the cell starts on its old volume, on the uncordoned node
that holds it. Its row records the old volume's ID, its namespace has no
`exomem.io/move-volume` mark, and no new move starts. It passes the acceptance in step 5 of the move. The new volume
stays retained until you delete its PV.

## Remove the host

Move the host's cells off it first, and accept each one while its old volume
is still on the host as the rollback copy. Then run the removal playbook. The
playbook skips a cell already bound to another node. It moves a cell still on
the host itself, as a fallback: it waits until that cell serves, but you can
accept it only after the host is gone, with no rollback copy.

While the host is cordoned, a cell still on it cannot start again: an upgrade
or a re-render of that cell would leave it down. Pause cell upgrades for the
whole removal, and run no platform upgrade until step 6. The host must be
Ready, because the moves take their backups on it. If it is not, relocate its
cells as for a lost node first ([cloud-node-loss.md](cloud-node-loss.md)).

1. Pause cell upgrades, as the control database owner. If the row already reads `paused` with an error code, cellctl paused it: leave it, and skip step 6.

   ```sql
   UPDATE exomem_cloud_rollout SET paused = true WHERE id = 1 AND NOT paused;
   ```

2. Cordon the host: `kubectl cordon exomem-agent-dx1`.
3. List the cells whose live volume is on the host, with each volume's size:

   ```bash
   kubectl get pv -o json | jq -r --arg node exomem-agent-dx1 '.items[] | select(.status.phase == "Bound" and any(.spec.nodeAffinity.required.nodeSelectorTerms[]?.matchExpressions[]?; .key == "topology.topolvm.io/node" and (.values | index($node)))) | "\(.spec.capacity.storage) \(.spec.claimRef.namespace)"'
   ```

   Then check that the other nodes have room for them. Run the removal
   playbook's preflight alone. It changes nothing, and it refuses as step 5
   would, with the room each node has beyond the snapshot reserve:

   ```bash
   cd infra/ansible && ansible-playbook --inventory inventory.yml remove-agent.yml -e k3s_remove_node=exomem-agent-dx1 --tags preflight
   ```

   Expect the run to pass and to print `moving cells fit the remaining nodes' local pools`. If it refuses, add a node or free room first.
4. Move each listed cell, the largest first and one at a time, through [Move a cell to a new claim](#move-a-cell-to-a-new-claim). Accept each one (its step 5) before you move the next. If a cell cannot be accepted, roll it back ([Roll a moved cell back](#roll-a-moved-cell-back)), which uncordons the host, then stop the removal and do step 6.
5. Run the removal playbook, as in [cloud-node-pool.md](cloud-node-pool.md#remove-a-node):

   ```bash
   cd infra/ansible
   ansible-playbook --inventory inventory.yml remove-agent.yml -e k3s_remove_node=exomem-agent-dx1
   ```

   It refuses unless each cell still on the host fits one other general
   node's pool: the node's published free bytes must cover the cell's claim
   and still hold the snapshot reserve, twice the largest cell for each
   backup the node may run at once (32 GiB with 8 GiB cells and two backups).
   The refusal lists the room each node has beyond the reserve. A failed or
   timed-out move stops the playbook with the host still in the cluster and
   cordoned, and its message says what to do. Accept any cell the playbook
   moved itself, as in step 5 of the move.
6. Resume cell upgrades, unless step 1 found them paused by cellctl:

   ```sql
   UPDATE exomem_cloud_rollout SET paused = false WHERE id = 1 AND error_code IS NULL;
   ```

7. Destroy the cell array's LUKS header. Step 5 already closed the storage. This is irreversible, so type the confirmation yourself:

   ```bash
   ssh -t exomem-admin@100.64.0.40 'sudo cryptsetup erase /dev/md/exomem-cells'
   ```

8. Remove the host from the host list, regenerate the inventory, and run `site.yml` without `--limit`.
9. Boot the server into the provider's rescue system.
10. If the disks are NVMe, wipe each whole disk: `nvme format --ses=1 /dev/nvme0n1`, then `/dev/nvme1n1`.
11. If they are not NVMe, wipe each whole disk with `blkdiscard -f /dev/<disk>`.
12. Remove the host's peer from NetBird.
13. In one reviewed commit, remove the host's passphrase entry from the secret matrix and from `infra/contracts/active-ansible-selection-v1.json`, and delete its SOPS files.

To stop the removal before step 5 ends, uncordon the host
(`kubectl uncordon exomem-agent-dx1`) and do step 6. A move that timed out
keeps its mark. Once that cell serves, from its old or its new volume, remove
the mark: `kubectl annotate namespace "$NS" exomem.io/move-volume-`.

Expected result: after step 8, every node has dropped the host's WireGuard
peer, inter-node rules and Tang rule. If it was the last WireGuard host, every
node has also stopped `wg-quick@wgexomem` and removed its configuration and
key. After step 10, `nvme format` has completed on both disks. The WireGuard
key, the agent token and the agent's kubelet client certificate were only on
those disks. Unless the wipe is confirmed,
[rotate the agent token](#rotate-the-agent-token), then
[the client CA](#rotate-the-client-ca): `blkdiscard` and an empty
`blkid --probe` do not prove erasure.

## Rotate the agent token

Rotate `k3s_agent_token` when a disk that held it leaves your custody unwiped.
`k3s token rotate` cannot do it: K3s leaves the agent token alone while
`agent-token` is configured. The rotation changes the configured token and
restarts the server.

That restart leaves etcd with the old copy of `server/cred/passwd`, because
K3s saves its bootstrap data at start only when etcd holds none. The server's
next start would then stop with `cred/passwd newer than datastore`. Step 4 saves
the data again. The `Cloud k3s-link rehearsal` workflow rehearses the K3s side of these steps
on a disposable cluster.

1. Escrow the next version, here v2, to both destinations. Keep v1 for the undo:

   ```bash
   openssl rand -base64 48 | infra/scripts/secret_handoff.py \
     --matrix "$matrix" --repository-root "$repo_root" \
     --secret k3s_agent_token --version v2 \
     --destination ansible.hosted-node.k3s-agent-token.active \
     --destination escrow.k3s-agent-token.active --source stdin
   ```

2. In a reviewed commit, set `ansible.hosted-node.k3s-agent-token.active` to `v2` in `infra/contracts/active-ansible-selection-v1.json`.
3. Run `site.yml` as in [Add the host](#add-the-host). The server restarts with v2, then each agent restarts with it.
4. Save the bootstrap data again on the server. Do not replace the helper with a typed `k3s token rotate`: an empty or wrong `--new-token` rotates the server token. Run `ssh exomem-admin@<server> 'sudo /usr/local/sbin/exomem-k3s-resave-bootstrap'`.
5. Restart the server: `ssh exomem-admin@<server> 'sudo systemctl restart k3s && sudo k3s kubectl get --raw=/readyz'`.
6. Present the old credential, then the new token, each alone, to the server. Each travels from SOPS over SSH, and `curl` reads it from stdin. Set `escrow` to each escrow file in turn, and run the command:
   - for a rotation from v1 to v2, the old credential is `infra/secrets/escrow/k3s-agent-token.v1.sops.json`, and the new token is `k3s-agent-token.v2.sops.json`;
   - for the first selection of an agent token, the old credential is the selected server token's escrow file, for example `k3s-server-token.v1.sops.json`, because agents used the server token until an agent token was set. The new token is `k3s-agent-token.v1.sops.json`.

   ```bash
   escrow=infra/secrets/escrow/k3s-agent-token.v1.sops.json
   sops decrypt --extract '["token"]' "$escrow" \
     | ssh exomem-admin@<server> 'read -r token; printf "user = \"node:%s\"\n" "$token" \
       | sudo curl -sS -o /dev/null -w "%{http_code}\n" -K - \
         --cacert /var/lib/rancher/k3s/server/tls/server-ca.crt https://127.0.0.1:6443/v1-k3s/readyz'
   ```

Expected result: step 4 prints `bootstrap data saved again; the server token is
unchanged`. Step 5 prints `ok`, and `kubectl get nodes` shows every node Ready.
Step 6 prints `401` for the old credential and `200` for the new token. Repeat
it if it prints `503`, which means the server is still starting. A Ready agent
does not prove the new token, because a joined agent also authenticates with
its kubelet client certificate.

If step 4 reports that the server token changed, do not restart K3s. With the
old token in its configuration, the server would no longer find its bootstrap
data in etcd. While the server runs, rotate the token back to the escrowed
server token, then run step 4 again:

```bash
sops decrypt --extract '["token"]' infra/secrets/escrow/k3s-server-token.v1.sops.json \
  | ssh exomem-admin@<server> "sudo sh -c 'read -r t; test -n \"\$t\" && exec k3s token rotate --new-token \"\$t\"'"
```

The token reaches the root shell on stdin, so sudo logs only the script text.

If step 4 reports that `k3s token rotate` failed and the token is unchanged,
run step 4 again.

If the server stops with `newer than datastore` before step 4, remove the file
it names: `sudo rm /var/lib/rancher/k3s/server/cred/passwd`. Then run
`sudo systemctl restart k3s`, and continue with step 4.

To undo, select v1 again in a reviewed commit and repeat steps 3 to 6, with the
expected codes swapped.

### What a lost disk still exposes

The agent-token rotation closes the token, not the other credentials on the
disk. The disk also held the agent's kubelet client certificate and key, under
`/var/lib/rancher/k3s/agent/`, and its node password,
`/etc/rancher/node/password`. K3s keeps accepting that certificate after the
token rotation. With it, a caller can act as that node in the API server, and
get new kube-proxy and k3s-controller client certificates from the server.
While the host's Node still exists, the certificate and the node password also
get a new kubelet client certificate.

Expiry is therefore no remedy: until a removed host's certificate expires, it
can get other new certificates, and a host still in the cluster renews its
own. [Rotate the client CA](#rotate-the-client-ca) after the token to close
this exposure. K3s checks the node password only after a valid token or
certificate, so the password alone gets nothing once both are rotated. Do not
delete a node-password Secret by hand. K3s deletes it with its Node, and
creates it again from the next password that a valid credential presents. The
host firewall's rules admit port 6443 from the private network only for the
other inventoried nodes' addresses. This limits who outside the cluster can
use these credentials.

After both rotations, the disk still holds the agent's kubelet serving
certificate and key. The server CA signed them, and they stay valid until the
certificate expires, at most a year after K3s issued it. Before the rotation,
a holder of the disk's certificate and node password could also get a serving
certificate on their own key. It can name any addresses, and stays valid for a
year from then. Deleting `serving-kubelet.key` revokes neither certificate.
They help only someone who can intercept the API server's traffic to a
kubelet, who could then answer as that host's kubelet.

## Rotate the client CA

Rotate the cluster's client CA when a disk that held an agent's kubelet client
certificate leaves your custody unwiped. That is a failed disk the provider
swapped, or the disks of a removed host whose wipe is not confirmed. Rotate the
agent token first. Afterwards the cluster refuses every client certificate the
old client CA signed, the disk's among them.

The k3s role installs the helper `exomem-k3s-rotate-client-ca` on the server.
It generates a new client CA there and loads it with
`k3s certificate rotate-ca --force`. Without `--force`, K3s accepts a new CA
only if it chains to the current one, so that certificates from both stay
trusted. This rotation must end that trust, so the helper makes an unrelated
CA. `--force` skips K3s's own checks, so the helper first checks that the new
key matches its certificate. The new key never leaves the server. K3s keeps it in
its datastore, encrypted with the escrowed server token, as it keeps the client
CA it replaces. The `Cloud k3s-link rehearsal` workflow rehearses steps 3 to 7
on a disposable cluster. In place of the everyday operator certificate, it
presents a copy of the agent's kubelet client certificate before and after.

Only the client CA changes. The join token's CA hash, the agent token,
ServiceAccount tokens and the CA that pods trust stay the same. These change:

- From the server's restart until an agent restarts, the API server refuses
  that agent's kubelet. Kubernetes marks a refused agent NotReady after about a
  minute, and cellctl then publishes no cell slots for it. `kubectl logs` and
  `kubectl exec` fail for the agent's pods until it restarts.
- A NotReady agent's pods go NotReady too, which interrupts the traffic to its
  cells. Under the default tolerations, Kubernetes evicts them after 300 s, and
  an evicted cell restarts when its agent is back. cellctl does not relocate a
  cell because its node is NotReady.
- K3s rewrites the admin kubeconfig, `/etc/rancher/k3s/k3s.yaml`, when the
  server restarts. A copy taken before the rotation is refused.
- The everyday operator certificate and every break-glass certificate are
  refused from step 4. Step 9 issues a new everyday operator certificate.
- An etcd snapshot taken before the rotation holds the old client CA. A
  restore from one brings the old client CA back: follow the warning in
  [Restore etcd from an older snapshot](cloud-node-loss.md#restore-etcd-from-an-older-snapshot).

Step 5 gives new client keys to a host whose disk left custody and that stays
in the cluster. That disk held its current keys.

Preconditions:

- The agent token is rotated, and step 6 of
  [Rotate the agent token](#rotate-the-agent-token) printed `401` for the old
  token. Otherwise the disk's token gets certificates from the new client CA.
- Every node is Ready, and `ansible k3s_agents -m ansible.builtin.ping`, run in
  `infra/ansible`, reaches every agent.
- The everyday operator certificate on the server is not expired. Step 1 uses
  it as a certificate from before the rotation. If it has expired, issue it
  again as in
  [cloud-operator-access.md](cloud-operator-access.md#issue-the-everyday-operator-certificate).

Steps:

1. On the server, present the everyday operator certificate, which the old client CA signed. Expect the node list:

   ```bash
   ssh exomem-admin@<server> 'sudo k3s kubectl --kubeconfig /root/.kube/exomem-operator.kubeconfig get nodes'
   ```

2. Take an etcd snapshot now, and record the name it prints: `ssh exomem-admin@<server> 'sudo k3s etcd-snapshot save --name pre-client-ca-rotation'`.
3. Load a new client CA on the server, and record the directory it prints: `ssh exomem-admin@<server> 'sudo /usr/local/sbin/exomem-k3s-rotate-client-ca'`.

After step 3 succeeds, continue to step 4; to back out, undo after step 7.
Step 3 changes only K3s's datastore. Any start of K3s on the server loads the
new client CA, a reboot or an upgrade included. Until then, the undo refuses,
because the old pair is still the one on disk.

Steps 4 to 6 restart K3s. From the server's restart until an agent restarts,
the API server refuses that agent's kubelet. Do steps 4 to 6 without a pause.

4. Restart the server: `ssh exomem-admin@<server> 'sudo systemctl restart k3s && sudo k3s kubectl get --raw=/readyz'`.
5. If the host whose disk left custody stays in the cluster, remove its agent's client keys: `ssh exomem-admin@<host> 'sudo sh -c "rm -f /var/lib/rancher/k3s/agent/client-*.key"'`.
6. Restart K3s on every agent at once: `cd infra/ansible && ansible k3s_agents --forks 20 -m ansible.builtin.systemd_service -a 'name=k3s-agent state=restarted'`.
7. Check that every node is Ready, with the admin kubeconfig on the server: `ssh exomem-admin@<server> 'sudo k3s kubectl get nodes'`.

   The everyday operator certificate does not work here: the server refuses it from step 4 until step 9 issues a new one.

8. Present the everyday operator certificate again, as in step 1.
9. Issue a new everyday operator certificate, as in [cloud-operator-access.md](cloud-operator-access.md#issue-the-everyday-operator-certificate).
10. When you accept the rotation, delete the directory that step 3 printed, which holds the old client CA's key, and the local copy of the step 2 snapshot, which scheduled pruning never removes: `ssh exomem-admin@<server> 'sudo rm -rf <directory> && sudo rm -f /var/lib/rancher/k3s/server/db/snapshots/<name>'`. Check that `<name>` is the full name step 2 printed: the second command removes one file only.

Expected result: step 1 lists the nodes. Step 2 logs `Snapshot <name> saved.`
twice with the same name: once for the local copy and once for the S3 copy. One
line means the S3 upload failed and only the local copy exists. Step 3 prints the SHA-256 fingerprints of the old and
the new client CA, and the directory that keeps the old pair. Step 4 prints
`ok`, and step 7 shows every node Ready within a few minutes. Step 8 prints
`error: You must be logged in to the server (Unauthorized)`: the API server
refuses that certificate, as it now refuses the lost disk's. A node that stays
NotReady still runs on certificates from the old client CA: restart K3s on it.

If step 3 refuses or fails, the cluster keeps its client CA. Remove the cause
it prints, and run step 3 again.

If the server does not start in step 4, read its log:
`sudo journalctl -u k3s`. If K3s cannot start on the new client CA, restore
etcd from the snapshot of step 2, as in
[cloud-node-loss.md](cloud-node-loss.md#restore-etcd-from-an-older-snapshot).
Set `SNAPSHOT` to the name that step 2 printed. If step 2 printed its line only
once, restore the local copy instead. The server's `config.yaml` turns S3 on, so
switch it off on the command line, on the stopped server:
`sudo k3s server --cluster-reset --etcd-s3=false --cluster-reset-restore-path=/var/lib/rancher/k3s/server/db/snapshots/<name>`.
The other steps of that procedure still apply.
The restore brings back the old client CA and loses only what changed after
step 2. The agents have not
restarted yet, so they still hold certificates from the old client CA.

To undo the rotation after step 7, load the old pair again, then repeat steps
4 to 7 and step 9. The cluster then accepts the old client CA's certificates
again, the lost disk's among them. It refuses an everyday operator certificate
that step 9 issued on the new client CA, which is why step 9 runs again:

```bash
ssh exomem-admin@<server> 'sudo /usr/local/sbin/exomem-k3s-rotate-client-ca --restore <directory>'
```
