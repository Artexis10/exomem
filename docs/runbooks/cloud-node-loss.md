<!-- authority:non-specification -->

# Cloud node loss: relocate cells, reconcile volumes

**Status:** written with `move-cloud-cells-to-local-storage` phase 2. Not yet rehearsed: the node-loss drill (task 4.1) runs the relocation, and its etcd restore from an older snapshot runs the restore and the reconciliation. Until then, treat every step as unproven.

This applies to cells on local storage (the `exomem-cloud-local` class, TopoLVM). A cell on a Hetzner Cloud Volume never needs it: its volume survives its node.

Run the `kubectl` steps as break-glass ([cloud operator access](cloud-operator-access.md#break-glass-mint-a-one-hour-identity)) and the host steps as root on the agent.

## Relocate the cells of a lost agent

Relocation restores each cell on the lost agent from its last hourly backup onto other capacity. It loses the writes since that backup, at most an hour's. Never start it for a node that is only unreachable: a cell still running there would keep writing to a volume nobody reads again.

1. Make sure replacement capacity exists: a surviving agent with free slots, or a new or recovery agent that has joined.
2. Confirm the agent stopped, by the rule node removal uses: the host is reachable and shows no container process, no pod or CSI mount and no open storage mapping, or you have confirmed the server is destroyed.
3. Tell the cluster, with Kubernetes' own out-of-service taint. cellctl acts on it only while the node is not Ready:

   ```bash
   kubectl taint node "$NODE" node.kubernetes.io/out-of-service=nodeshutdown:NoExecute --overwrite
   ```

   The taint also makes Kubernetes delete the node's pods. A Node already deleted from the cluster is the other trigger: only `remove-agent.yml` deletes one, after the same confirmation.
4. cellctl then relocates each cell whose volume is on that node. The owner's cell goes alone first. Once its restore has ended, the rest follow by rollout priority, at most four at a time. For each cell, cellctl sets the old volume's reclaim policy to Retain, deletes the claim, restores the last backup into a new claim on another node, and starts the cell only after the restore succeeds. The row shows hold `restore` until the cell is back. Accept each cell with recall, governance status and a governed write.
5. A cell whose row shows `RELOCATION_NO_BACKUP` has no backup to restore from. It stays as it is; decide with its owner.
6. A cell whose row shows `RESTORE_FAILED` stays stopped. Its failed restore Job expires five minutes after it fails and the next pass runs the restore again, so a restore that keeps failing needs its cause fixed (`kubectl -n exo-cell-<cell id> logs job/<job>` while it exists). The other cells do not wait for it.
7. Once every cell is accepted, remove the agent with `remove-agent.yml` ([node pool](hosted/node-pool.md#remove-a-node)). Its retained volumes do not block the removal. A deleted cell's volume on that node counts as gone only once the Node is gone from the cluster, so the removal also completes those deletions.
8. Never let the agent rejoin with its `cells` volume group. If the server comes back, keep it out of the cluster and erase its cells device (below) before it is reused. A rejoined node would bring back volumes whose cells now run elsewhere, and volumes of deleted cells.
9. Delete the lost node's retained PersistentVolume objects once the Node is gone from the cluster.

## Erase a removed agent's cells device

Removing a reachable agent ends with its cells device erased. Its disk still holds the retained volumes of relocated cells, and cellctl already counts a deleted cell's volume there as gone.

1. Check that `remove-agent.yml` finished: `kubectl get node "$NODE"` reports `NotFound`.
2. On the host, as root, deactivate the volume group: `vgchange --activate n cells`.
3. Close the LUKS mapping under it. `lsblk` shows its name: `cryptsetup close "$MAPPING"`.
4. Erase every key slot of the LUKS device, the escrowed recovery passphrase included: `cryptsetup erase --batch-mode "$DEVICE"`.
5. Check that `cryptsetup luksDump "$DEVICE"` lists no key slots. Expect the data to be unreadable from now on.

Then destroy the server, or reinstall it before it joins again.

## Restore etcd from an older snapshot

The server uploads an etcd snapshot every 30 minutes to the etcd snapshot bucket, under `etcd-snapshot/`. Restore with the escrowed restore key: `etcd_snapshot_restore_key_id` and `etcd_snapshot_restore_key`, in `infra/secrets/escrow/`. The server's own key cannot read snapshots.

Never list snapshots through K3s, `k3s etcd-snapshot ls --s3` included. K3s lists with the prefix `etcd-snapshot` without the slash. Both keys allow only `etcd-snapshot/`, so B2 answers "not entitled". A read of one exact object name works.

1. On the operator machine, list the snapshots with an S3 client, the restore key and the prefix `etcd-snapshot/`:

   ```bash
   AWS_ACCESS_KEY_ID="$KEY_ID" AWS_SECRET_ACCESS_KEY="$KEY" aws s3api list-objects-v2 --endpoint-url "$ENDPOINT" --bucket "$BUCKET" --prefix etcd-snapshot/ --query 'Contents[].[Key,LastModified]' --output text
   ```

   Expect one line per snapshot, each key starting with `etcd-snapshot/`.
2. Choose the newest snapshot taken before the fault. Set `SNAPSHOT` to its key without the `etcd-snapshot/` prefix.
3. On the server, stop K3s: `systemctl stop k3s`.
4. Restore that exact snapshot with the restore key. The endpoint, bucket and folder come from `/etc/rancher/k3s/config.yaml`:

   ```bash
   k3s server --cluster-reset --etcd-s3 --etcd-s3-access-key="$KEY_ID" --etcd-s3-secret-key="$KEY" --cluster-reset-restore-path="$SNAPSHOT"
   ```

   Expect K3s to report the reset complete and exit.
5. Start K3s: `systemctl start k3s`. Expect the agents to rejoin within a few minutes.
6. As soon as the API answers, pause cellctl. It restarts with the snapshot's state:

   ```bash
   kubectl -n exomem-cloud scale deployment cellctl --replicas=0
   ```

   Until then, the identity check and the empty-vault guard keep cells off empty volumes.

Then reconcile the volumes, below.

## After restoring etcd from an older snapshot

etcd snapshots are taken every 30 minutes. A restore loses the cluster objects of every volume created or relocated since, while the volumes themselves, and the only copy of those writes, are still on the agents' disks. Nothing here may delete a TopoLVM `LogicalVolume` object: on a node whose TopoLVM plugin runs, that destroys the volume, even with its finalizer removed.

1. Check that cellctl is paused, so it neither recreates claims nor records volumes while you work:

   ```bash
   kubectl -n exomem-cloud get deployment cellctl --output=jsonpath='{.spec.replicas}'
   ```

   Expect `0`.

2. Collect the three inputs on the operator machine:

   ```bash
   ansible-playbook infra/ansible/list-cell-volumes.yml -e cell_volumes_dir="$PWD/volumes"
   kubectl get logicalvolumes.topolvm.io -o json > logicalvolumes.json
   ```

   and, as the control database owner, the rows of cells on local storage (TopoLVM volume IDs are UUIDs; Hetzner's are numbers):

   ```sql
   \copy (SELECT cell_id, volume_id, node FROM exomem_cloud_cells WHERE desired_state <> 'deleted' AND volume_id ~ '^[0-9a-f-]{36}$') TO 'rows.csv' WITH CSV
   ```

3. Match them. This reads and prints only:

   ```bash
   uv run --project infra/cellctl python -m cellctl.readopt --lvs-dir volumes --rows rows.csv --logical-volumes logicalvolumes.json
   ```

   - `in_place`: nothing to do.
   - `readopt`: re-adopt each with step 4.
   - `relocate`: the row's own node was listed and does not hold its volume. Use step 5.
   - `unverified`: the row's node wrote no listing, so nothing is known about its volume. Reach that node and list it again. If the node is lost, relocate its cells as in the first section.
   - `conflict`: one volume name on more than one disk. Act on none of those copies. Compare them on the hosts and keep the one the row's node holds; ask before releasing any.
   - `unclaimed`: on a disk, but claimed by no row and no LogicalVolume. Leave it. Only an operator on that host releases it (`lvremove cells/<name>`), after confirming nothing claims it.

4. Re-adopt a volume. TopoLVM names the volume on disk after its object's volume ID, and a recreated object gets a new ID and a new, empty volume. The old volume takes over that name:

   1. Create the LogicalVolume, copying the spec of a surviving one: `name` (the PV name you will use), `nodeName` (the node from the report), `size` (the volume's size in bytes, from its listing) and `deviceClass: thin`. Wait for its `status.volumeID`; call it `NEW_ID`.
   2. On that node, replace the new empty volume with the old one:

      ```bash
      lvremove --yes "cells/$NEW_ID" && lvrename cells "$OLD_ID" "$NEW_ID"
      ```

   3. If the namespace `exo-cell-<cell id>` still has a `cell-data` claim bound to another volume, that claim is from before the snapshot. Retire it without deleting its volume. Stop the cell's pod first, or the claim stays in use; cellctl starts the pod again when it resumes:

      ```bash
      kubectl -n "exo-cell-$CELL_ID" scale statefulset --all --replicas=0
      kubectl patch persistentvolume "$STALE_PV" --type=merge --patch '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
      kubectl -n "exo-cell-$CELL_ID" delete persistentvolumeclaim cell-data
      ```

      Expect the delete to return once the pod is gone.

      Then create the PersistentVolume, copying the spec of a surviving cell's PV: the PV name from step 1, `capacity`, `csi.volumeHandle: $NEW_ID`, the node in its node affinity, and `claimRef` set to namespace `exo-cell-<cell id>`, name `cell-data`.
   4. Release the row's old identity, so cellctl records the new one under its usual class and claim checks:

      ```sql
      UPDATE exomem_cloud_cells SET volume_id = NULL WHERE cell_id = :'cell_id' AND volume_id = :'old_id';
      ```

      It must report `UPDATE 1`. Never clear an identity any other way; cellctl's identity check is what stops a wrong volume serving a cell.

5. Relocate a cell whose volume cannot be re-adopted, without touching its node:
   1. If its namespace has a `cell-data` claim, retire it as in step 4.3.
   2. Mark the volume lost, with the row's `volume_id`:

      ```bash
      kubectl annotate namespace "exo-cell-$CELL_ID" "exomem.io/volume-lost=$VOLUME_ID"
      ```

   3. Leave the row's `volume_id` as it is. After cellctl resumes, it relocates the cell from its backup.
6. Resume cellctl:

   ```bash
   kubectl -n exomem-cloud scale deployment cellctl --replicas=1
   ```

   A re-adopted cell starts on its own volume, and a cell with a recorded backup never initialises an empty one. Accept each cell with recall, governance status and a governed write. Then remove the `exomem.io/volume-lost` marks: `kubectl annotate namespace "exo-cell-$CELL_ID" exomem.io/volume-lost-`.

## Row error codes

`VOLUME_MISSING`: the row records a volume, but the cell's claim is gone. cellctl never gives such a cell a fresh, empty claim, so it stays stopped.

1. Find out where the volume went. Check `kubectl get persistentvolumes` for a PV with that `volumeHandle`, and list the volume's node as in "After restoring etcd" step 2.
2. If the volume is on its node, re-adopt it as in step 4 of that section.
3. If it is lost, mark it lost as in step 5, and cellctl relocates the cell from its backup.

`CELL_INIT_EMPTY_VOLUME_REFUSED`: the cell has a recorded backup, and it started on an empty volume. It refused to create an empty vault, and stays not ready.

1. Find the claim's volume: `kubectl -n "exo-cell-$CELL_ID" get persistentvolumeclaim cell-data`.
2. Check whether the row's `volume_id` matches it. If not, the cell's real volume may still exist: look for it as for `VOLUME_MISSING`, and re-adopt it.
3. If the cell's data is only in its backup, pause cellctl as in "Restore etcd" step 6. Retire the empty claim as in step 4.3, mark the volume lost as in step 5, then resume cellctl.

Never clear the backup record to get past this refusal: the cell would then serve an empty vault as if it were the tenant's.
