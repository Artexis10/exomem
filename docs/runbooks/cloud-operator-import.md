<!-- authority:non-specification -->

# Cloud owner vault restore

**Status:** rehearsed on 2026-09-28 against the production cell image, on a volume that already held runtime state:

- the unpack, swap and start steps, using this runbook's own scripts;
- after the swap, the restored vault's Knowledge Base marker was recalled, and a note only the replaced vault held no longer answered;
- a governed write was committed, read back and recalled;
- the rollback script restored the prior vault, and the cell served it again.

The owner's source vault was checked on 2026-09-28: its governance store is at schema v3, and it has no links, special files or hardlinks. Not yet run against a production cell.

This procedure replaces the Exomem Cloud owner's cell vault with the owner's existing Exomem vault from another machine. It applies to the **owner's own vault only**. The operator mints the key that decrypts the archive, which is acceptable only when the operator and the vault's owner are the same person. Any other user's restore uses the key their own cell mints (`add-exomem-cloud-vault-import`, design D1), never this procedure. To restore a cell from one of its own backups, use [cloud operator restore](cloud-operator-restore.md).

What moves is the vault directory, as a point-in-time copy. The source machine's machine-local state (indexes, the governance store) does not move; the cell rebuilds its own. The source keeps running and stays the owner's primary vault. Writes made after the copy are not in Cloud.

Three machines take part:

- **Source:** where the vault lives. It needs `tar`, `age` and `exomem`.
- **Operator machine:** reaches the source and the node over SSH. It only ever carries ciphertext.
- **Node:** holds the one-time key in `/dev/shm` and the ciphertext on disk, decrypts, and runs `kubectl` as break-glass.

The cell is down from step 5 to step 7, which takes minutes for a vault of a few gigabytes. Everything that can refuse the archive runs before step 5, so a refusal never costs downtime. Run the node steps in an interactive shell **without** `set -e`: a failed gate must leave the shell and its variables in place so that [recovery](#if-a-step-fails-after-step-5) can run. Don't start between 02:00 and 05:00 UTC, when cellctl runs backups.

The unpacker is the reviewed commit's `src/exomem/cloud_import.py`. It uses only the standard library. Copy it to the source machine and to the node before starting, and record its SHA-256 on both.

## What the cell's volume holds

Measured on the cell image by running `cell-init` and the server on an empty volume, then writing and recalling a note. `/data/host` is the runtime's home directory.

| Path | What it is | In a restore |
|---|---|---|
| `/data/vault` | the vault | replaced |
| `/data/host/.local/state/exomem/state/` | derived state per vault: search indexes, graph, receipts, due state | set aside with the prior vault; rebuilt on start |
| `/data/host/.cache/exomem/` | locks, idempotency records, generation counters | set aside with the prior vault; recreated on start |
| `/data/host/.local/state/exomem/standalone-host-control-v1/` | standalone custody, present only once a vault enrols in it | left in place |
| `/data/host/.cache/Microsoft/` | the ONNX runtime's own telemetry ID | left in place |

The derived-state key is a hash of the vault's path, and the path stays `/data/vault`. A restore that left the old derived state in place would serve the replaced vault's indexes for the new notes. Setting it aside is what stops that.

## 1. Check the source

On the source machine. Nothing here prints note names or content.

```bash
set -o pipefail
: "${VAULT:?absolute path of the source vault}"
test -d "$VAULT/Knowledge Base"
exomem --version
exomem governance-schema status --vault "$VAULT" --json
```

Record both version lines and the image digest of the owner's cell (step 5 prints it). Stop if `schema_version` is 4: a standalone cell cannot serve it. Take a copy of the vault back with `exomem governance-schema downmigrate` and import the copy instead.

Decide what stays out. `.git` history and a sync tool's trash are not notes. Attachments come in. Then count what the archive will hold. The pruned paths here must match the stream's `--exclude` options in step 4:

```bash
PRUNE=(-path ./.git -prune -o -path ./.trash -prune -o)
cd "$VAULT" || echo 'no such vault; stop before counting' >&2
find . "${PRUNE[@]}" \( -type l -o ! -type f ! -type d \) -printf '%y\n' | sort | uniq -c
EXPECT_FILES=$(find . "${PRUNE[@]}" -type f -printf '.' | wc -c)
EXPECT_BYTES=$(find . "${PRUNE[@]}" -type f -printf '%s\n' | awk '{s += $1} END {print s + 0}')
echo "files=$EXPECT_FILES bytes=$EXPECT_BYTES"
```

The first command must print nothing. A link, device or FIFO refuses the whole import, so resolve each one on the source first; list them yourself with `find . -type l`, and keep the names out of any shared record. Then run the archive through the unpacker's `verify` mode on the source itself. It reads every member exactly as the restore will and writes nothing, so a hardlink, a name the cell cannot hold or a count mismatch shows up here:

```bash
: "${UNPACKER:?path of cloud_import.py on the source}"
tar -C "$VAULT" --exclude=./.git --exclude=./.trash -cf - . \
  | python3 -I "$UNPACKER" verify --max-bytes "$EXPECT_BYTES" --expect-files "$EXPECT_FILES" --expect-bytes "$EXPECT_BYTES"
```

It must print `"ok": true`. The source keeps running, so a note written between this check and step 4 changes the counts; step 4's node check catches that, and you recount here. Keep `EXPECT_FILES` and `EXPECT_BYTES` for step 4.

## 2. Size the cell

The cell's volume holds the new vault and the prior one until step 9, plus rebuilt indexes. It needs `EXPECT_BYTES`, plus what the current vault and its indexes use, plus 2 GiB free space. If the cell's `storage_gib` is smaller, raise it as the control database owner:

```sql
UPDATE exomem_cloud_cells SET storage_gib = :new_gib WHERE cell_id = :'cell_id';
```

cellctl renders the new size into the PVC and the quota, and the storage class expands the volume online. Confirm before going on:

```bash
kubectl -n "exo-cell-$CELL_ID" get pvc cell-data -o jsonpath='{.status.capacity.storage}{"\n"}'
```

## 3. Mint the one-time key

On the node, as root. The identity never leaves memory-backed storage. Only the public recipient is printed.

```bash
umask 077
set -o pipefail
: "${CELL_ID:?the owner cell ID}"
[[ "$CELL_ID" =~ ^[a-z2-7]{16}$ ]] || echo 'not a cell ID; stop' >&2
IMPORT_ID=$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
KEY_DIR="/dev/shm/exomem-import-$IMPORT_ID"
WORK="/var/lib/exomem-import/$IMPORT_ID"
mkdir -m 700 "$KEY_DIR"
install -d -m 700 /var/lib/exomem-import "$WORK"
age-keygen -o "$KEY_DIR/identity" 2>/dev/null
age-keygen -y "$KEY_DIR/identity"
echo "import_id=$IMPORT_ID"
```

`age` comes from the node's base packages; on a node not yet converged, `apt-get install age` first. Keep this shell open for steps 4 to 9.

## 4. Stream the ciphertext

On the operator machine. The source archives and encrypts, and the node stores the ciphertext. The operator machine sees only ciphertext.

```bash
set -o pipefail
: "${SOURCE:?ssh destination of the source machine}"
: "${NODE:?ssh destination of the node, as root}"
: "${VAULT:?absolute path of the source vault on the source machine}"
: "${RECIPIENT:?the recipient step 3 printed}"
: "${IMPORT_ID:?the import_id step 3 printed}"
[[ "$RECIPIENT" =~ ^age1[02-9ac-hj-np-z]{58}$ ]] || echo 'not an age recipient; stop' >&2
[[ "$IMPORT_ID" =~ ^[0-9a-f]{8}$ ]] || echo 'not an import ID; stop' >&2
# Both commands are built here on purpose (SC2029): every value in them was checked above.
inner="tar -C $(printf '%q' "$VAULT") --exclude=./.git --exclude=./.trash -cf - . | age -r $RECIPIENT | tee >(sha256sum >&2)"
# shellcheck disable=SC2029
ssh "$SOURCE" "bash -o pipefail -c $(printf '%q' "$inner")" \
  | ssh "$NODE" "cat > /var/lib/exomem-import/$IMPORT_ID/vault.tar.age && sha256sum /var/lib/exomem-import/$IMPORT_ID/vault.tar.age && stat -c '%s bytes' /var/lib/exomem-import/$IMPORT_ID/vault.tar.age"
```

The source prints the ciphertext's SHA-256 on stderr, and the node prints its own. They must match. A failed or interrupted stream leaves a short file; rerun this step to replace it.

Back on the node, prove the ciphertext before anything stops. This decrypts into the unpacker's `verify` mode, so the plaintext only passes through a pipe:

```bash
: "${EXPECT_FILES:?from step 1}"
: "${EXPECT_BYTES:?from step 1}"
test -s "$WORK/cloud_import.py" && sha256sum "$WORK/cloud_import.py"
age -d -i "$KEY_DIR/identity" "$WORK/vault.tar.age" \
  | python3 -I "$WORK/cloud_import.py" verify --max-bytes "$EXPECT_BYTES" --expect-files "$EXPECT_FILES" --expect-bytes "$EXPECT_BYTES"
echo "decrypt and verify exit: ${PIPESTATUS[*]}"
```

Both exit codes must be 0 and the line must say `"ok": true`. Anything else is fixed here, with the cell still serving.

## 5. Stop the cell

Back on the node. Mint a break-glass identity first ([cloud operator access](cloud-operator-access.md#break-glass-mint-a-one-hour-identity)), then set up `kubectl` the way the export runbook does:

```bash
: "${BREAK_GLASS_KUBECONFIG:?mint a break-glass identity first}"
export KUBECONFIG="$BREAK_GLASS_KUBECONFIG"
IMPORT_BIN=$(mktemp -d /dev/shm/exomem-import-bin.XXXXXX)
cat > "$IMPORT_BIN/kubectl" <<'SHIM'
#!/bin/sh
exec k3s kubectl "$@"
SHIM
chmod 700 "$IMPORT_BIN/kubectl"
export PATH="$IMPORT_BIN:$PATH"
kubectl auth whoami | grep -qF 'exomem:break-glass'
NS="exo-cell-$CELL_ID"
test "$(kubectl get namespace "$NS" -o jsonpath='{.metadata.labels.exomem\.io/cloud-cell}')" = "$CELL_ID"
IMAGE=$(kubectl -n "$NS" get statefulset cell -o jsonpath='{.spec.template.spec.containers[0].image}')
[[ "$IMAGE" =~ @sha256:[a-f0-9]{64}$ ]] || echo 'image is not pinned by digest; stop' >&2
echo "image=$IMAGE"
# Gates print why they fail, and every change below runs only when its gate passes.
no_runtime_pod() {
  test -z "$(kubectl -n "$NS" get pods -l app.kubernetes.io/name=exomem-cell -o name)" \
    || { echo 'a runtime pod exists; stop' >&2; return 1; }
}
# wait_gone NAMESPACE SELECTOR: until no pod matches, for up to five minutes.
wait_gone() {
  local tries=0
  while [ -n "$(kubectl -n "$1" get pods -l "$2" -o name)" ]; do
    tries=$((tries + 1))
    [ "$tries" -le 60 ] || { echo "pods matching $2 are still running; stop" >&2; return 1; }
    sleep 5
  done
}
```

A `desired_state` write would not hold: Substrate recomputes it from the entitlement, and cellctl would then start the runtime in the middle of the swap. Pause cellctl instead, then stop the runtime. First make sure the cell is not in the middle of a backup, upgrade or restore:

```bash
kubectl -n "$NS" get jobs --no-headers | grep -v ' Complete ' || echo 'no running jobs'
kubectl -n "$NS" get statefulset cell -o jsonpath='{.metadata.annotations}{"\n"}' | grep -o '"[^"]*hold[^"]*":"[^"]*"' || echo 'no hold'
```

Both must print their "no" line. Then:

```bash
kubectl -n exomem-cloud scale deployment cellctl --replicas=0 \
  && wait_gone exomem-cloud app.kubernetes.io/name=cellctl \
  && kubectl -n "$NS" scale statefulset cell --replicas=0 \
  && wait_gone "$NS" app.kubernetes.io/name=exomem-cell \
  && no_runtime_pod && echo 'stopped; continue'
```

Go on only after `stopped; continue`; otherwise follow [recovery](#if-a-step-fails-after-step-5).

While cellctl is paused, no cell reconciles, backs up or upgrades. Keep the pause to this procedure. Only a deploy of the platform chart would bring cellctl back early, so run no platform deploy until step 7. A running cellctl restores the cell's replica at once, because the row still reads `running`, and the runtime would start over a half-swapped vault.

## 6. Unpack and swap

The helper pod runs the cell image with the cell's volume mounted. It has no API token, and no job-egress label, so the namespace's default-deny policy leaves it no network.

```bash
if no_runtime_pod; then kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: owner-restore
  namespace: ${NS}
  labels: {app.kubernetes.io/name: cloud-owner-restore}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001
    fsGroupChangePolicy: OnRootMismatch
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: restore
      image: ${IMAGE}
      imagePullPolicy: IfNotPresent
      command: [python3, -c, 'import time; time.sleep(7200)']
      resources:
        requests: {cpu: 100m, memory: 256Mi}
        limits: {cpu: "1", memory: 1Gi}
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: {drop: [ALL]}
      volumeMounts:
        - {name: data, mountPath: /data}
        - {name: tmp, mountPath: /tmp}
  volumes:
    - name: data
      persistentVolumeClaim: {claimName: cell-data}
    - name: tmp
      emptyDir: {medium: Memory, sizeLimit: 64Mi}
EOF
fi
kubectl -n "$NS" wait --for=condition=Ready pod/owner-restore --timeout=180s
helper() { kubectl -n "$NS" exec -i owner-restore -- "$@"; }
helper sh -c 'cat > /tmp/cloud_import.py' < "$WORK/cloud_import.py"
helper df -B1 --output=avail /data | tail -1
```

The free space printed must exceed `EXPECT_BYTES` plus 2 GiB. Now decrypt on the node and unpack into a new staging directory. The unpacker refuses links, special files, path escapes and a stream that stops early, and removes the staging directory when it refuses. `pipefail` makes a decryption failure fail the step even though the unpacker finished.

```bash
: "${EXPECT_FILES:?from step 1}"
: "${EXPECT_BYTES:?from step 1}"
STAGING="/data/.import-$IMPORT_ID"
set -o pipefail
if no_runtime_pod && age -d -i "$KEY_DIR/identity" "$WORK/vault.tar.age" \
  | helper python3 -I /tmp/cloud_import.py unpack --staging "$STAGING" \
      --max-bytes "$EXPECT_BYTES" --expect-files "$EXPECT_FILES" --expect-bytes "$EXPECT_BYTES"; then
  helper test -d "$STAGING/Knowledge Base" && echo 'unpacked; continue'
else
  helper rm -rf -- "$STAGING"
  echo 'decryption or unpacking failed; the vault is unchanged; go to recovery' >&2
fi
```

Before the swap, compare the Knowledge Base notes. The count of notes that only the current Cloud vault holds is printed. Their names go to a memory-only file on the node, for the owner to read before anything is deleted:

```bash
helper python3 -I - "$STAGING" > "$KEY_DIR/only-in-prior.txt" <<'PY'
import sys
from pathlib import Path
old, new = Path("/data/vault/Knowledge Base"), Path(sys.argv[1]) / "Knowledge Base"
names = lambda root: {p.relative_to(root).as_posix() for p in root.rglob("*.md")} if root.is_dir() else set()
for name in sorted(names(old) - names(new)):
    print(name)
PY
echo "notes only in the current Cloud vault: $(wc -l < "$KEY_DIR/only-in-prior.txt")"
```

Then swap. The prior vault and its derived state move into one directory; the custody directory beside the derived state stays where it is. The cell rebuilds its indexes on start.

```bash
# The script runs in the pod; $1 is the import ID passed after it. It checks
# its own preconditions, so a paste after a failed unpack changes nothing.
# shellcheck disable=SC2016
no_runtime_pod && helper sh -euc '
  prior=/data/.restore-prior-$1
  [ -d "/data/.import-$1/Knowledge Base" ] || { echo "no unpacked import; nothing moved" >&2; exit 1; }
  [ -d /data/vault ] && [ ! -e "$prior" ] || { echo "vault missing or swap already run; nothing moved" >&2; exit 1; }
  mkdir -m 700 "$prior"
  mv /data/vault "$prior/vault"
  if [ -d /data/host/.local/state/exomem/state ]; then mv /data/host/.local/state/exomem/state "$prior/state"; fi
  if [ -d /data/host/.cache/exomem ]; then mv /data/host/.cache/exomem "$prior/cache"; fi
  mv "/data/.import-$1" /data/vault
  ls -A /data /data/host/.local/state/exomem
' swap "$IMPORT_ID" && kubectl -n "$NS" delete pod owner-restore --wait=true
```

## 7. Start the cell

```bash
kubectl -n "$NS" scale statefulset cell --replicas=1
kubectl -n "$NS" rollout status statefulset/cell --timeout=900s
kubectl -n exomem-cloud scale deployment cellctl --replicas=1
kubectl -n exomem-cloud rollout status deployment/cellctl --timeout=300s
```

Scale the StatefulSet back by hand, as above, before you resume cellctl, so the cell starts while you watch it. cellctl would also restore the replica when it resumes: its row still reads `running`, and it applies a served cell that runs no replica again. `cell-init` migrates state for the new vault, and the runtime serves while its indexes build in the background. A large vault takes minutes to become fully searchable.

## 8. Verify

Through the owner's own Exomem Cloud connector:

1. Ask for a note you know is in the source vault's Knowledge Base. Recall covers the Knowledge Base; files outside it are reachable by path and through adoption. Allow a few minutes for indexing.
2. Ask for a note that only the replaced Cloud vault held. It must not answer.
3. Make one governed write, such as a short note, then read it back. A vault the cell cannot serve refuses writes even when recall works.

The owner reads `$KEY_DIR/only-in-prior.txt`. Anything worth keeping is still in `/data/.restore-prior-$IMPORT_ID/vault` until step 9.

## 9. Clean up

As soon as step 8 passes, delete the key and the ciphertext. The prior vault is the rollback, not the ciphertext.

```bash
shred -u "$KEY_DIR/identity"
rm -rf -- "$KEY_DIR" "$IMPORT_BIN"
shred -u "$WORK/vault.tar.age"
rm -rf -- "$WORK"
unset BREAK_GLASS_KUBECONFIG
```

Then end the break-glass session as the access runbook describes.

Delete the prior directory only after cellctl's next scheduled backup of the cell succeeds. It runs in the 02:00–05:00 UTC window; `last_backup_at` on the cell's row moves past the swap time. Check that backup's duration against the backup Job's 900-second deadline. The owner's cell is the rollout canary, so a backup that cannot finish would stall every upgrade. Then, under a fresh break-glass identity and with the runtime stopped as in step 5, run the helper pod again and remove `/data/.restore-prior-$IMPORT_ID`.

## If a step fails after step 5

A failed gate prints why and leaves the shell as it was. While `/data/.restore-prior-$IMPORT_ID` does not exist, the swap has not run and the vault is unchanged; recovery just brings everything back. If it does exist, run the [rollback](#rollback) first.

```bash
kubectl -n "$NS" delete pod owner-restore --ignore-not-found --wait=true
kubectl -n "$NS" scale statefulset cell --replicas=1
kubectl -n "$NS" rollout status statefulset/cell --timeout=900s
kubectl -n exomem-cloud scale deployment cellctl --replicas=1
kubectl -n exomem-cloud rollout status deployment/cellctl --timeout=300s
```

If the helper pod is still there, check with `helper test ! -e "/data/.restore-prior-$IMPORT_ID"` and remove any `/data/.import-$IMPORT_ID` through it before deleting it.

## Rollback

Before step 9, a restore that does not start or does not answer is reversed with the same stop and the same helper pod. Once the restored cell has started, it has written its own derived state, so that is removed before the prior vault and state move back:

```bash
# The script runs in the pod; $1 is the import ID passed after it.
# shellcheck disable=SC2016
no_runtime_pod && helper sh -euc '
  prior=/data/.restore-prior-$1
  [ -d "$prior/vault" ] || { echo "no prior vault to restore; nothing moved" >&2; exit 1; }
  if [ -e /data/vault ]; then mv /data/vault "/data/.rejected-$1"; fi
  mv "$prior/vault" /data/vault
  rm -rf /data/host/.local/state/exomem/state /data/host/.cache/exomem
  if [ -d "$prior/state" ]; then mv "$prior/state" /data/host/.local/state/exomem/state; fi
  if [ -d "$prior/cache" ]; then mv "$prior/cache" /data/host/.cache/exomem; fi
  rmdir "$prior"
' rollback "$IMPORT_ID"
```

Then start the cell as in step 7, and remove `/data/.rejected-$IMPORT_ID` once the cell serves again.

## Record

Record:

- the date, the cell ID and the import ID;
- the source and image versions and the governance schema;
- the file and byte counts;
- the ciphertext digest and size;
- the unpacker's SHA-256 and its JSON result line;
- the number of notes only the prior vault held;
- the recall check;
- the first backup's time and duration;
- when the prior directory was removed.

Never record note names, paths or content.
