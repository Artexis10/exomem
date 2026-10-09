<!-- authority:non-specification -->

# Cloud operator restore: drill and in-place restore

**Status:** the restore drill ran on 2026-10-09 against two production cells. Both restored byte-identical, and each scratch namespace and its volume were removed and verified:

- a cell with 45 vault files, restored in 45 s and compared with its live vault;
- a cell with 9,324 vault files, restored in 97 s and compared with the vault that snapshot captured.

The restore Job's deadline is 900 s. That drill ran as a reviewed private script, which `infra/scripts/cloud_restore.sh` packages; the packaged script has not yet run against a cell. The in-place restore has not run against a cell either. Its stop, move-aside and start steps follow the owner vault restore that ran on 2026-10-09 ([cloud owner vault restore](cloud-operator-import.md)).

This runbook covers two operator tools for cells on Hetzner network volumes (storage class `exomem-cloud-encrypted`). One script, `infra/scripts/cloud_restore.sh`, runs both:

- **Restore drill.** It restores one of a cell's restic snapshots from object storage into a scratch namespace. It hashes every restored vault file and compares the hashes with a reference vault. It proves that a backup restores byte-identical. The cell keeps serving: the drill never stops it, never mounts its volume in a new pod and never writes it.
- **In-place restore.** It replaces a live cell's contents with a chosen snapshot, after a corrupted volume or a bad change. The cell is down from the stop to the start.

cellctl itself restores a cell only after an upgrade readiness timeout or a local-storage relocation. A cell on local storage (`exomem-cloud-local`) is out of scope here; see [cloud node loss](cloud-node-loss.md). To bring the owner's own vault from another machine, use [cloud owner vault restore](cloud-operator-import.md).

## What a restore covers

A snapshot holds what cellctl's backup Job covers: `BACKUP_PATHS` in `infra/cellctl/src/cellctl/manifests.py`, which are `/data/vault` and `/data/host`. The in-place restore rewrites exactly those paths, as cellctl's own restore Job does. So the derived state, custody and runtime caches in `/data/host` come back as the snapshot captured them, in step with the vault. Writes after the snapshot are not in it.

The prior contents of both paths stay in `/data/.restore-prior-<run ID>` on the cell's volume. A backup never includes that directory.

## How the script runs

Both tools run on the K3s server, the node that holds `/etc/rancher/k3s/k3s.yaml`, as root, from a checkout of the reviewed release. The script refuses to start on any other node. It mints break-glass with that admin kubeconfig, and it removes the scratch namespace with it even after break-glass expires. They use break-glass ([cloud operator access](cloud-operator-access.md#break-glass-mint-a-one-hour-identity)), because they read the cell's Secret and exec into cell and scratch pods, which the everyday identity cannot do. The script mints its own one-hour identity, and mints a new one when less than 20 minutes remain.

The script copies its release files to `/var/lib/exomem-restore/<run ID>` and runs that copy as a transient systemd unit, `exomem-restore-<run ID>`. A dropped SSH session cannot interrupt it, and a later change to the checkout cannot reach it. The run log is `/var/lib/exomem-restore/<run ID>/run.log`. It holds counts, durations and names of Kubernetes objects, never vault file names or content. The hash lists name vault files, so they stay in `/dev/shm/exomem-restore-<run ID>`, and the script removes them after a pass.

The cell can run on another node than the server:

- A drill reads the live or prior vault through the container runtime of the node it runs on. So it supports only a cell that runs on the K3s server. For any other cell it stops before the scratch restore, with `the cell runs on node <name>`.
- An in-place restore works for a cell on any node. Its helper pod and its restore Job take the cell's own placement, so the cell's volume stays on the cell's node during the downtime.
- The scratch restore and its verify pod run on a node with no taint, as the export does, so they stay off a dedicated cell's node.

The renderer is `infra/scripts/cloud_restore_manifests.py`, which the [export runbook](cloud-operator-export.md) also uses. It calls cellctl's own `render_cell_manifests` and `render_restore_job` from the same checkout, and it renders the helper pods. The hasher is `infra/scripts/cloud_vault_digest.py`. It hashes a regular file by content, a symlink by its target text without following it, and stops on any unreadable directory. The helper pod runs `infra/scripts/cloud_restore_volume.sh` to move the backed-up paths aside and to move them back.

Both tools refuse to start in these cases:

- the script does not run on the K3s server;
- the time is inside the nightly backup window that the live cellctl Deployment is configured with (`cells.backupWindow`, 02:00-05:00 UTC by default);
- the node has less than 2 GiB of memory available;
- the cell's volume is not in the `exomem-cloud-encrypted` class.

The backup window check runs at the start and again just before the stop. It does not stop a run that goes on into the window. A restore that runs into the window keeps cellctl paused, so no cell backs up until cellctl resumes. Start a restore early enough to finish before the window opens.

## Before you start

1. Record the reason, the cell ID and the snapshot in the operator channel.
2. Select the snapshot with the read-only query in the export runbook's "Select the source and snapshot".
3. Put the reviewed release on the node, as below, with `COMMIT` set to the reviewed commit.
4. Note the release digests that the run log prints first. Compare them with `sha256sum` of the same files at `COMMIT`.

The control database records only the latest snapshot, `last_backup_snapshot`. This runbook does not cover listing older snapshots.

On the operator machine, from a checkout that has the reviewed commit:

```bash
set -o pipefail
: "${NODE:?ssh destination of the node, as root}"
: "${COMMIT:?the reviewed commit}"
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || echo 'not a full commit ID; stop' >&2
# shellcheck disable=SC2029 # the remote command is built here on purpose; COMMIT was checked above
git archive --format=tar "$COMMIT" infra/scripts infra/cellctl/src \
  | ssh "$NODE" "install -d -m 700 /root/exomem-release-$COMMIT && tar -C /root/exomem-release-$COMMIT -xf -"
```

Run the steps below on the node, as root, in `/root/exomem-release-$COMMIT`.

## Run a drill

1. Set `CELL_ID` and `SNAPSHOT` from the authorization record.
2. Start the drill against the live vault: `infra/scripts/cloud_restore.sh drill "$CELL_ID" "$SNAPSHOT" live`.
3. Note the `run_id` it prints.
4. Show progress with `infra/scripts/cloud_restore.sh follow "$RUN_ID"`. Repeat until `ActiveState=inactive`.
5. Read the last log line. Expect `verdict=pass scratch_cleanup=yes rc=0`.

A drill against `live` passes only when no vault file changed after the snapshot. On a cell in use, expect `differ`, `only_restored` or `only_reference` counts: they count the changes since the snapshot. A drill against `prior:<id>` compares with a vault that an earlier restore or owner import kept in `/data/.restore-prior-<id>`. Use it when that vault is what the snapshot captured.

`scratch_cleanup=no` means the script could not verify that the scratch namespace and its volume are gone. The log names what remains. Delete it by hand only after you check that it carries the `exomem.io/scratch-of` label for this cell.

## Restore a cell in place

**Warning:** do not deploy the platform chart (the `helm upgrade` in [deploy](hosted/deploy.md)) while a restore runs. The chart sets cellctl back to one replica, and cellctl then starts the cell on a half-restored volume. The script cannot prevent this.

1. To see how far the snapshot is from the live vault, run a drill against `live` first and read its counts.
2. Start the restore: `infra/scripts/cloud_restore.sh restore "$CELL_ID" "$SNAPSHOT"`.
3. Note the `run_id` it prints.
4. Show progress with `infra/scripts/cloud_restore.sh follow "$RUN_ID"`. Repeat until `ActiveState=inactive`.
5. Read the last log line. Expect `phase=finished verdict=pass scratch_cleanup=yes rc=0`.
6. Through the cell's connector, recall a note that the snapshot holds. Expect an answer.
7. Make one governed write, then read it back. Expect the write to succeed.

The run does these steps. Every check that can refuse the snapshot runs before the cell stops, so a refusal costs no downtime.

1. It restores the snapshot into a scratch namespace and hashes the restored vault. It refuses a Job that fails or an empty vault. It measures the restored size, then removes the scratch namespace and verifies that the namespace and volume are gone.
2. If the runtime runs, it checks through the running cell that the volume has the restored size plus 2 GiB free.
3. It refuses when cellctl is already paused, when the cell is not scaled to one, when the cell carries a hold, or when a Job runs in its namespace.
4. It pauses cellctl and checks for a hold or Job again. Then it scales the cell to zero and waits until no pod runs in the namespace.
5. A helper pod, placed where the cell runs, checks the free space again. Then it moves the backed-up paths into `/data/.restore-prior-<run ID>`.
6. It applies cellctl's restore Job to the cell's own namespace, with the cell's placement, and waits for the Job to succeed or fail.
7. A helper pod hashes the restored vault. The hashes must equal the scratch restore's hashes.
8. It scales the cell to one and waits up to 30 minutes for Ready. Then it resumes cellctl.

The cell is down from the `stopped at` line to the `serving again at` line in the log.

## If a run fails

The last log line names the phase where the run stopped. The script recovers by phase before it exits:

| Phase | What changed | What the script does |
|---|---|---|
| `checks` | nothing on the cell | removes the scratch namespace |
| `stopping` | cellctl paused, cell maybe stopped | starts the cell and resumes cellctl |
| `moved`, `starting` | prior contents moved aside, restore maybe partial | stops the cell, moves the prior contents back, starts the cell, resumes cellctl |
| `served` | the restored cell serves | resumes cellctl only, and never rolls back |

To abort a run, stop its unit: `systemctl stop exomem-restore-<run ID>`. The script starts the recovery for its phase when its current command returns, within five minutes. A second stop does not interrupt the recovery. A run also stops this way when it outlasts its time limit: 50 minutes for a drill, 150 minutes for a restore.

A failed run keeps its hash lists in `/dev/shm/exomem-restore-<run ID>` for diagnosis. Delete that directory when you are done. The restore Job's own reason is in the log line that starts with `!! restore Job did not succeed`.

### Recover by hand

The script leaves the cell stopped and cellctl paused when it cannot roll back, because a partly restored vault must not serve. The log then says `rollback did not run`. A run that was killed outright, by SIGKILL or a node restart, ran no recovery at all. The last `==` line in its log shows the step it reached. Such a run also leaves behind:

- its break-glass kubeconfig in `/dev/shm/exomem-break-glass-<run ID>`, valid for up to an hour;
- its restore Job and its helper pod `cell-restore-helper`, which the steps below delete;
- its scratch namespace, if it stopped before the downtime.

Run these steps on the K3s server, as root, in `/var/lib/exomem-restore/<run ID>`, the frozen copy of the release that the run used. The rollback takes the backed-up paths from cellctl's `BACKUP_PATHS` and runs the same `cloud_restore_volume.sh` that the script runs.

1. If the log has a `serving again at` line, do not roll back: the restored cell already served. Resume cellctl with `kubectl -n exomem-cloud scale deployment cellctl --replicas=1`, then go to step 7.
2. Mint a break-glass identity and set up `kubectl` as the import runbook's step 5 does.
3. Set `CELL_ID` and `RUN_ID`, the failed run's ID, then run the first block below. It stops the cell and deletes the run's restore Job and helper pod.
4. Read the pod list that the block prints. Expect only pods in phase `Succeeded` or `Failed`. If another pod remains, wait and list again with `kubectl -n "$NS" get pods`.
5. Run the second block. It starts a helper pod where the cell runs and moves the prior contents back. Expect `rolled back; continue`.
6. Start the cell and cellctl as the import runbook's step 7 does. Expect the cell Ready and cellctl running.
7. Run the third block, which removes the run's break-glass directories. If it prints the scratch namespace, remove that namespace as the export runbook's last block does.

```bash
: "${CELL_ID:?the cell ID}"
: "${RUN_ID:?the ID of the failed run}"
cd "/var/lib/exomem-restore/$RUN_ID" || echo 'no such run directory; stop' >&2
NS="exo-cell-$CELL_ID"
manifests() { python3 -I infra/scripts/cloud_restore_manifests.py "$@"; }
kubectl -n "$NS" scale statefulset cell --replicas=0
kubectl -n "$NS" delete job -l "$(manifests restore-job-selector)" --cascade=foreground --wait=true
kubectl -n "$NS" delete pod cell-restore-helper --ignore-not-found --wait=true
kubectl -n "$NS" get pods
```

```bash
manifests helper-pod --context break-glass --cell-id "$CELL_ID" --name cell-restore-helper \
    --field-manager cloud-operator-restore \
  && kubectl -n "$NS" wait --for=condition=Ready pod/cell-restore-helper --timeout=180s \
  && read -r -a paths <<< "$(manifests backup-paths)" \
  && kubectl -n "$NS" exec -i cell-restore-helper -- sh -s rollback "$RUN_ID" "${paths[@]}" \
    < infra/scripts/cloud_restore_volume.sh \
  && echo 'rolled back; continue'
kubectl -n "$NS" delete pod cell-restore-helper --ignore-not-found --wait=true
```

```bash
rm -rf -- "/dev/shm/exomem-break-glass-$RUN_ID" "/dev/shm/exomem-break-glass-$RUN_ID.new"
kubectl get namespace "exo-scratch-$CELL_ID-$RUN_ID" --ignore-not-found -o name
```

## After a restore

Keep `/data/.restore-prior-<run ID>` until cellctl's next scheduled backup of the cell succeeds and the owner accepts the restore. Then remove it as the import runbook's step 9 removes its prior directory. Remove `/var/lib/exomem-restore/<run ID>` after you record the run.

## Record

Record:

- the date, the cell ID, the run ID, and the snapshot ID and time;
- the release commit and the digests the log prints first;
- the break-glass CSR names the log prints;
- the file counts, the restore duration and the restored bytes;
- for an in-place restore, the `stopped at` and `serving again at` times;
- the last log line.

Never record vault file names or content.
