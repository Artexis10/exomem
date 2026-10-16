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

Precondition for either link: OpenSpec task 6.5 has set the cluster's flannel
MTU. Cloud nodes run flannel at 1400 over the 1450-byte Cloud network, so their
VXLAN packets are 1450 bytes. A vSwitch carries at most 1400, and a default
WireGuard interface 1420. Until 6.5 is done, pod traffic to a dedicated agent
is unproven.

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

Expected result: `cat /proc/mdstat` shows every array rebuilding, then `[UU]`.
A `RESOLVED: cell-array-degraded` email follows within a few minutes of the
rebuild finishing. After step 9, every peer holds the host's new public key and the tunnel is up.
The removed disk held the old WireGuard key and the agent token
(`/etc/rancher/k3s/config.yaml`) unencrypted. Unless the provider confirms
that it destroyed the disk, the agent token must be rotated. That procedure is
pending (OpenSpec task 5.7); until it exists, record the exposure.

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
10-minute readiness deadline (`upgrade_ready_deadline` in
`infra/cellctl/src/cellctl/decide.py`). The next upgrade rolls back a cell
that is not ready within it after its start. Compare it with the time from the
restore's end to `ready`, which `kubectl -n "$NS" get events` shows. If the
restart comes close to the deadline, raise it before the next upgrade.

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

1. Pause cellctl, and stop the cell:

   ```bash
   kubectl -n exomem-cloud scale deployment cellctl --replicas=0
   kubectl -n "$NS" scale statefulset cell --replicas=0
   ```

2. Retain the new volume, and retire its claim. `NEW_PV` is the claim's current volume:

   ```bash
   NEW_PV=$(kubectl -n "$NS" get persistentvolumeclaim cell-data -o jsonpath='{.spec.volumeName}')
   kubectl patch persistentvolume "$NEW_PV" --type=merge --patch '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
   kubectl -n "$NS" delete persistentvolumeclaim cell-data
   ```

3. Free the retained volume, `OLD_PV`, for a new claim: `kubectl patch persistentvolume "$OLD_PV" --type=json --patch '[{"op":"remove","path":"/spec/claimRef"}]'`. Expect its status `Available`.
4. Create claim `cell-data` in `$NS` bound to it: `volumeName: $OLD_PV`, the PV's `storageClassName` and capacity, access mode `ReadWriteOnce`. Expect `Bound`.
5. Release the row's identity by compare-and-set on the new volume's ID, as the control database owner. It must report `UPDATE 1`:

   ```sql
   UPDATE exomem_cloud_cells SET volume_id = NULL WHERE cell_id = '<cell id>' AND volume_id = '<new volume id>';
   ```

6. Resume cellctl: `kubectl -n exomem-cloud scale deployment cellctl --replicas=1`.

Expected result: the cell starts on its old volume, its row records the old
volume's ID, and it passes the acceptance in step 5. The new volume stays
retained until you delete its PV.

## Remove the host

`remove-agent.yml` cordons the host, then moves each cell whose live volume is
on it to a new claim on another node, one at a time, as in
[Move a cell to a new claim](#move-a-cell-to-a-new-claim). Before the cordon,
it refuses unless each of those cells fits one other general node's pool: the
node's published free bytes must cover the cell's claim and still hold the
snapshot reserve, twice the largest cell for each backup the node may run at
once (32 GiB with 8 GiB cells and two backups). The refusal lists the room each
node has beyond the reserve. A failed move stops the playbook with the host
still in the cluster, and its message says what to do. The host must be Ready: the
moves take their backups on it. If it is not, relocate its cells as for a lost
node first ([cloud-node-loss.md](cloud-node-loss.md)).

1. Run the removal playbook, as in [cloud-node-pool.md](cloud-node-pool.md#remove-a-node):

   ```bash
   cd infra/ansible
   ansible-playbook --inventory inventory.yml remove-agent.yml -e k3s_remove_node=exomem-agent-dx1
   ```

2. Accept each moved cell with recall, governance status and a governed write, as in step 5 of the move. The host's disk still holds each moved cell's old volume until step 3.
3. Destroy the cell array's LUKS header. Step 1 already closed the storage. This is irreversible, so type the confirmation yourself:

   ```bash
   ssh -t exomem-admin@100.64.0.40 'sudo cryptsetup erase /dev/md/exomem-cells'
   ```

4. Remove the host from the host list, regenerate the inventory, and run `site.yml` without `--limit`.
5. Boot the server into the provider's rescue system.
6. If the disks are NVMe, wipe each whole disk: `nvme format --ses=1 /dev/nvme0n1`, then `/dev/nvme1n1`.
7. If they are not NVMe, wipe each whole disk with `blkdiscard -f /dev/<disk>`.
8. Remove the host's peer from NetBird.
9. In one reviewed commit, remove the host's passphrase entry from the secret matrix and from `infra/contracts/active-ansible-selection-v1.json`, and delete its SOPS files.

Expected result: after step 4, every node has dropped the host's WireGuard
peer, inter-node rules and Tang rule. If it was the last WireGuard host, every
node has also stopped `wg-quick@wgexomem` and removed its configuration and
key. After step 6, `nvme format` has completed on both disks. The WireGuard
key and the agent token were only on those disks. Rotate the agent token
unless the wipe is confirmed: `blkdiscard` and an empty `blkid --probe` do not
prove erasure. That rotation is pending (OpenSpec task 5.7); until it exists,
record the exposure.
