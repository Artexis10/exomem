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

Both tools run on the node, as root, from a checkout of the reviewed release. They use break-glass ([cloud operator access](cloud-operator-access.md#break-glass-mint-a-one-hour-identity)), because they read the cell's Secret and exec into cell and scratch pods, which the everyday identity cannot do. The script mints its own one-hour identity, and mints a new one when less than 20 minutes remain.

The script copies its release files to `/var/lib/exomem-restore/<run ID>` and runs that copy as a transient systemd unit, `exomem-restore-<run ID>`. A dropped SSH session cannot interrupt it, and a later change to the checkout cannot reach it. The run log is `/var/lib/exomem-restore/<run ID>/run.log`. It holds counts, durations and names of Kubernetes objects, never vault file names or content. The hash lists name vault files, so they stay in `/dev/shm/exomem-restore-<run ID>`, and the script removes them after a pass.

The renderer is `infra/scripts/cloud_restore_manifests.py`, which the [export runbook](cloud-operator-export.md) also uses. It calls cellctl's own `render_cell_manifests` and `render_restore_job` from the same checkout. The hasher is `infra/scripts/cloud_vault_digest.py`. It hashes a regular file by content, a symlink by its target text without following it, and stops on any unreadable directory.

Both tools refuse to start in these cases:

- the time is inside the nightly backup window that the live cellctl Deployment is configured with (`cells.backupWindow`, 02:00-05:00 UTC by default);
- the node has less than 2 GiB of memory available;
- the cell's volume is not in the `exomem-cloud-encrypted` class.

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

1. To see how far the snapshot is from the live vault, run a drill against `live` first and read its counts.
2. Start the restore: `infra/scripts/cloud_restore.sh restore "$CELL_ID" "$SNAPSHOT"`.
3. Note the `run_id` it prints.
4. Show progress with `infra/scripts/cloud_restore.sh follow "$RUN_ID"`. Repeat until `ActiveState=inactive`.
5. Read the last log line. Expect `phase=finished verdict=pass scratch_cleanup=yes rc=0`.
6. Through the cell's connector, recall a note that the snapshot holds. Expect an answer.
7. Make one governed write, then read it back. Expect the write to succeed.

The run does these steps. Every check that can refuse the snapshot runs before the cell stops, so a refusal costs no downtime.

1. It restores the snapshot into a scratch namespace and hashes the restored vault. It refuses a Job that fails or an empty vault. It measures the restored size, then removes the scratch namespace and verifies that the namespace and volume are gone.
2. If the runtime runs, it checks that the volume has the restored size plus 2 GiB free.
3. It refuses when cellctl is already paused, when the cell is not scaled to one, when the cell carries a hold, or when a Job runs in its namespace.
4. It pauses cellctl and checks for a hold or Job again. Then it scales the cell to zero and waits until no pod runs in the namespace.
5. A helper pod checks the free space again, then moves the backed-up paths into `/data/.restore-prior-<run ID>`.
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

To abort a run, stop its unit: `systemctl stop exomem-restore-<run ID>`. The script then runs the recovery for its phase. A run also stops this way when it outlasts its time limit: 50 minutes for a drill, 150 minutes for a restore.

A failed run keeps its hash lists in `/dev/shm/exomem-restore-<run ID>` for diagnosis. Delete that directory when you are done. The restore Job's own reason is in the log line that starts with `!! restore Job did not succeed`.

### Recover by hand

The script leaves the cell stopped and cellctl paused when it cannot roll back, because a partly restored vault must not serve. The log then says `rollback did not run`. A run that was killed outright, by SIGKILL or a node restart, ran no recovery at all. The last `==` line in its log shows the step it reached.

1. Mint a break-glass identity and set up `kubectl` as the import runbook's step 5 does.
2. If `/data/.restore-prior-<run ID>` exists, start the helper pod as the import runbook's step 6 does.
3. Run the rollback below in the helper pod, with `RUN_ID` set to the run ID.
4. Delete the helper pod, then start the cell and cellctl as the import runbook's step 7 does.
5. Check that `cellctl` runs again with its usual replica count. Expect every cell to reconcile again.

```bash
# The script runs in the pod; $1 is the run ID passed after it.
# shellcheck disable=SC2016
no_runtime_pod && helper sh -euc '
  prior=/data/.restore-prior-$1
  [ -d "$prior" ] || { echo "no prior directory; nothing was moved" >&2; exit 0; }
  for part in vault host; do
    if [ -e "$prior/$part" ]; then rm -rf -- "/data/$part"; mv "$prior/$part" "/data/$part"; fi
  done
  rmdir "$prior"
' rollback "$RUN_ID"
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
