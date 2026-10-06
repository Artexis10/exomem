<!-- authority:non-specification -->

# Cloud node loss: relocate cells, reconcile volumes

**Status:** written with `move-cloud-cells-to-local-storage` phase 2. Not yet rehearsed: the node-loss drill (task 4.1) runs the relocation, and its etcd restore from an older snapshot runs the restore and the reconciliation. Until then, treat every step as unproven.

This applies to cells on local storage (the `exomem-cloud-local` class, TopoLVM). A cell on a Hetzner Cloud Volume never needs it: its volume survives its node.

Run the `kubectl` steps as break-glass ([cloud operator access](cloud-operator-access.md#break-glass-mint-a-one-hour-identity)) and the host steps as root on the agent.

## Relocate the cells of a lost agent

Relocation restores each cell on the lost agent from its last hourly backup onto other capacity. It loses the writes since that backup, at most an hour's. Never start it for a node that is only unreachable: a cell still running there would keep writing to a volume nobody reads again.

1. Make sure replacement capacity exists: a surviving agent with free slots, or a new or recovery agent that has joined.
2. Confirm the agent stopped, by the rule node removal uses: the host is reachable and shows no container process, no pod or CSI mount and no open storage mapping, or you have confirmed the server is destroyed.
3. Tell the cluster, with Kubernetes' own out-of-service taint. This is the only trigger cellctl acts on, and it does so only while the node is not Ready:

   ```bash
   kubectl taint node "$NODE" node.kubernetes.io/out-of-service=nodeshutdown:NoExecute --overwrite
   ```

4. cellctl then relocates each cell whose volume is on that node, starting one per pass, the owner's cell first, then by rollout priority. For each one it sets the old volume's reclaim policy to Retain, deletes the claim, restores the last backup into a new claim on another node, and starts the cell only after the restore succeeds. The row shows hold `restore` until the cell is back. Accept each cell with recall, governance status and a governed write.
5. A cell whose row shows `RELOCATION_NO_BACKUP` has no backup to restore from. It stays as it is; decide with its owner.
6. A cell whose row shows `RESTORE_FAILED` stays stopped. Its failed restore Job expires five minutes after it fails and the next pass runs the restore again, so a restore that keeps failing needs its cause fixed (`kubectl -n exo-cell-<cell id> logs job/<job>` while it exists).
7. Only once every cell is accepted, remove the agent with `remove-agent.yml`. The retained volumes of the lost node no longer hold reachable data; delete their PersistentVolume objects once the server is destroyed.

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
   \copy (SELECT cell_id, volume_id FROM exomem_cloud_cells WHERE desired_state <> 'deleted' AND volume_id ~ '^[0-9a-f-]{36}$') TO 'rows.csv' WITH CSV
   ```

3. Match them. This reads and prints only:

   ```bash
   uv run --project infra/cellctl python -m cellctl.readopt --lvs-dir volumes --rows rows.csv --logical-volumes logicalvolumes.json
   ```

   - `in_place`: nothing to do.
   - `readopt`: re-adopt each with step 4.
   - `relocate`: the row's volume is on no listed disk. Use step 5.
   - `unclaimed`: on a disk, but claimed by no row and no LogicalVolume. Leave it. Only an operator on that host releases it (`lvremove cells/<name>`), after confirming nothing claims it.

4. Re-adopt a volume. TopoLVM names the volume on disk after its object's volume ID, and a recreated object gets a new ID and a new, empty volume. The old volume takes over that name:

   1. Create the LogicalVolume, copying the spec of a surviving one: `name` (the PV name you will use), `nodeName` (the node from the report), `size` (the volume's size in bytes, from its listing) and `deviceClass: thin`. Wait for its `status.volumeID`; call it `NEW_ID`.
   2. On that node, replace the new empty volume with the old one:

      ```bash
      lvremove --yes "cells/$NEW_ID" && lvrename cells "$OLD_ID" "$NEW_ID"
      ```

   3. Create the PersistentVolume, copying the spec of a surviving cell's PV: the PV name from step 1, `capacity`, `csi.volumeHandle: $NEW_ID`, the node in its node affinity, and `claimRef` set to namespace `exo-cell-<cell id>`, name `cell-data`. If that namespace still has a `cell-data` claim bound to another volume, stop: that claim is from before the snapshot, and the cell is relocated from its backup instead (step 5).
   4. Release the row's old identity, so cellctl records the new one under its usual class and claim checks:

      ```sql
      UPDATE exomem_cloud_cells SET volume_id = NULL WHERE cell_id = :'cell_id' AND volume_id = :'old_id';
      ```

      It must report `UPDATE 1`. Never clear an identity any other way; cellctl's identity check is what stops a wrong volume serving a cell.

5. Relocate a cell whose volume cannot be re-adopted. Create its PV as in step 4.3, but with the old volume ID as `volumeHandle` and the lost or unusable node in its node affinity, then taint that node out of service as in the first section. cellctl relocates the cell from its backup. Leave the row's `volume_id` as it is.
6. Resume cellctl:

   ```bash
   kubectl -n exomem-cloud scale deployment cellctl --replicas=1
   ```

   A re-adopted cell starts on its own volume, and a cell with a recorded backup never initialises an empty one. Accept each cell with recall, governance status and a governed write.
