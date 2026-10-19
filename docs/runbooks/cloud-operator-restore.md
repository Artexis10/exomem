<!-- authority:non-specification -->

# Cloud operator restore: drill and in-place restore

**Status:** the restore drill ran on 2026-10-09 against two production cells. Both restored byte-identical, and each scratch namespace and its volume were removed and verified:

- a cell with 45 vault files, restored in 45 s and compared with its live vault;
- a cell with 9,324 vault files, restored in 97 s and compared with the vault that snapshot captured.

The restore Job's deadline is 900 s. The packaged in-place helper from PR #1641 ran successfully on 2026-10-09.
The retained release files matched that merged implementation. Its restore Job took 44 seconds; service downtime was 93 seconds.
The separate scratch drill took 49 seconds. Each proof compared 45 vault files, with zero differences and verified scratch cleanup.
These runs did not compare `/data/host` runtime state. They do not close a full vault-and-host recovery proof.
On 2026-10-13, maintained helpers restored a snapshot correlated with stopped-source references for both backup roots.
All 45 vault and 116 host entries matched; custody, service authentication and scratch cleanup passed.
The exercise did not invoke the current top-level `drill` wrapper.
The reviewed helper, its tests and the deployed-image recovery support repeatable comparison of both roots.
This proof does not close in-place failure recovery, historical rollback or acceptance of a new runtime image.

This runbook covers two operator tools for cells on Hetzner network volumes (storage class `exomem-cloud-encrypted`). One script, `infra/scripts/cloud_restore.sh`, runs both:

- **Restore drill.** It restores a cell's restic snapshot from object storage into a scratch namespace.
  It hashes each backup root and compares the hashes with an independently captured reference.
  The cell keeps serving: the drill never stops it, mounts its volume in a new pod or writes it.
- **In-place restore.** It replaces a live cell's contents with a chosen snapshot, after a corrupted volume or a bad change. The cell is down from the stop to the start.

cellctl itself restores a cell only after an upgrade readiness timeout or a local-storage relocation. A cell on local storage (`exomem-cloud-local`) is out of scope here; see [cloud node loss](cloud-node-loss.md). To bring the owner's own vault from another machine, use [cloud owner vault restore](cloud-operator-import.md).

## Repeat the restore drill

Use `infra/scripts/cloud_restore.sh drill` with the exact cell, selected B2 snapshot and reference documented below.
The helper already uses `cloud_restore_manifests.py`, `cloud_vault_digest.py` and `cloud_restore_volume.sh`.
Do not create another renderer or private drill script.
A reference must represent the selected snapshot's canonical version.
For canonical host proof, use a frozen reference from the selected snapshot's stopped backup interval.
Record how that reference corresponds to the snapshot; a prior run ID alone does not prove correspondence.
Capture both roots before restoring anything. The drill captures its reference before it starts the scratch restore.
Record the snapshot time, each root's entry counts, exact matches, downtime and verified scratch-resource absence.
Keep the result scoped to the paths and properties actually compared.
Repeat after each relevant release and backup-policy change and at the agreed recovery-check cadence.
Today's successful backup row alone does not satisfy this drill.

Use a disk scratch root for run receipts and manifests.
The helper keeps hash lists in its private run directory. Only break-glass credentials stay in memory scratch.

## What a restore covers

A snapshot holds what cellctl's backup Job covers: `BACKUP_PATHS` in `infra/cellctl/src/cellctl/manifests.py`, which are `/data/vault` and `/data/host`. The in-place restore rewrites exactly those paths, as cellctl's own restore Job does. So the derived state, custody and runtime caches in `/data/host` come back as the snapshot captured them, in step with the vault. Writes after the snapshot are not in it.

The prior contents of both paths stay in `/data/.restore-prior-<run ID>` on the cell's volume. A backup never includes that directory.

## How the script runs

Both tools run on the K3s server, the node that holds `/etc/rancher/k3s/k3s.yaml`, as root, from a checkout of the reviewed release. The script refuses to start on any other node. It mints break-glass with that admin kubeconfig, and it removes the scratch namespace with it even after break-glass expires. They use break-glass ([cloud operator access](cloud-operator-access.md#break-glass-mint-a-one-hour-identity)), because they read the cell's Secret and exec into cell and scratch pods, which the everyday identity cannot do. The script mints its own one-hour identity, and mints a new one when less than 20 minutes remain.

The script copies its release files to `/var/lib/exomem-restore/<run ID>` and runs that copy as a transient systemd unit, `exomem-restore-<run ID>`.
A dropped SSH session cannot interrupt it, and a later change to the checkout cannot reach it.
The run log is `/var/lib/exomem-restore/<run ID>/run.log`.
It holds counts, durations and names of Kubernetes objects, never private file names or content.
Hash lists stay in `/var/lib/exomem-restore/<run ID>/hashes`, with access restricted to root.
The script removes them after a pass.

The cell can run on another node than the server:

- A drill reads the live or prior backup roots through the node's container runtime.
  It supports only a cell that runs on the K3s server.
  For any other cell it stops before the scratch restore, with `the cell runs on node <name>`.
- An in-place restore works for a cell on any node. Its helper pod and its restore Job take the cell's own placement, so the cell's volume stays on the cell's node during the downtime.
- The scratch restore and its verify pod run on a node with no taint, as the export does, so they stay off a dedicated cell's node.

The renderer is `infra/scripts/cloud_restore_manifests.py`, which the [export runbook](cloud-operator-export.md) also uses. It calls cellctl's own `render_cell_manifests` and `render_restore_job` from the same checkout, and it renders the helper pods. The hasher is `infra/scripts/cloud_vault_digest.py`. It hashes a regular file by content, a symlink by its target text without following it, and stops on any unreadable directory. The helper pod runs `infra/scripts/cloud_restore_volume.sh` to move the backed-up paths aside and to move them back.

Each backup root must exist as a directory itself. A missing, unreadable or linked root fails hashing.
An empty host directory can pass; an empty vault cannot.
Hashes cover regular file bytes, symlink targets and special entry types.
They do not compare directory metadata, permissions or ownership. Verify custody and writability separately after recovery.

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
2. Start the drill: `infra/scripts/cloud_restore.sh drill "$CELL_ID" "$SNAPSHOT" live`. Expect a run ID.
3. Note the `run_id` it prints.
4. Show progress with `infra/scripts/cloud_restore.sh follow "$RUN_ID"`. Repeat until `ActiveState=inactive`.
5. Read the last log line. Expect `verdict=pass scratch_cleanup=yes rc=0`.

A drill against `live` passes only when both roots match the selected snapshot.
Background runtime work can change host state while the cell serves, even without a governed write.
The capture timestamps describe a sequential scan; they do not prove an atomic view of a running cell.
A mismatch reports divergence from the captured reference. It does not alone prove that the backup or restore is defective.

A drill against `prior:<id>` reads both roots that an earlier restore kept in `/data/.restore-prior-<id>`.
Use it when independent capture evidence shows that those roots correspond to the selected snapshot.
An owner import that kept only a vault cannot provide the missing host reference.

`scratch_cleanup=no` means the script could not verify that the scratch namespace and its volume are gone. The log names what remains. Delete it by hand only after you check that it carries the `exomem.io/scratch-of` label for this cell.

## Restore a cell in place

**Warning:** do not deploy the platform chart (the `helm upgrade` in [platform deploy](cloud-platform-deploy.md)) while a restore runs. The chart sets cellctl back to one replica, and cellctl then starts the cell on a half-restored volume. The script cannot prevent this.

1. Run a drill against `live` first. Expect separate vault and host divergence counts.
2. Start the restore: `infra/scripts/cloud_restore.sh restore "$CELL_ID" "$SNAPSHOT"`.
3. Note the `run_id` it prints.
4. Show progress with `infra/scripts/cloud_restore.sh follow "$RUN_ID"`. Repeat until `ActiveState=inactive`.
5. Read the last log line. Expect `phase=finished verdict=pass scratch_cleanup=yes rc=0`.
6. Through the cell's connector, recall a note that the snapshot holds. Expect an answer.
7. Make one governed write, then read it back. Expect the write to succeed.

The run checks the scratch restore before the cell stops.
It compares the in-place restore before startup; a failure then triggers recovery and costs downtime.

1. It restores the snapshot into scratch and hashes each backup root.
   A failed Job, failed hash or empty vault stops the run before downtime.
   It measures the restored size and verifies that the scratch namespace and volume are removed.
2. If the runtime runs, it checks through the running cell that the volume has the restored size plus 2 GiB free.
3. It refuses when cellctl is already paused, when the cell is not scaled to one, when the cell carries a hold, or when a Job runs in its namespace.
4. It pauses cellctl and checks for a hold or Job again. Then it scales the cell to zero and waits until no pod runs in the namespace.
5. A helper pod, placed where the cell runs, checks the free space again. Then it moves the backed-up paths into `/data/.restore-prior-<run ID>`.
6. It applies cellctl's restore Job to the cell's own namespace, with the cell's placement, and waits for the Job to succeed or fail.
7. A helper pod hashes each restored root. Each root must match the scratch restore before startup.
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

A failed run keeps its hash lists in `/var/lib/exomem-restore/<run ID>/hashes` for diagnosis.
Delete that directory after recording the evidence. The restore Job's reason appears after `!! restore Job did not succeed`.

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
- the reference source, capture timestamps and evidence of correspondence with the snapshot;
- each root's restored, reference, identical, differing and extra entry counts;
- the restore duration and the restored bytes;
- for an in-place restore, the `stopped at` and `serving again at` times;
- the last log line.

Never record vault file names or content.
