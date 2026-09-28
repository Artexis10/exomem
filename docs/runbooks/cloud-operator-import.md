<!-- authority:non-specification -->

# Cloud owner vault restore

**Status:** rehearsed on 2026-09-28 against the production cell image, on a volume that already held runtime state:

- the unpack, swap and start steps, using this runbook's own scripts;
- after the swap, the restored vault's Knowledge Base marker was recalled, and a note only the replaced vault held no longer answered;
- a governed write was committed, read back and recalled;
- the rollback script restored the prior vault, and the cell served it again.

Not yet run against a production cell.

This procedure replaces the Exomem Cloud owner's cell vault with the owner's existing Exomem vault from another machine. It applies to the **owner's own vault only**. The operator mints the key that decrypts the archive, which is acceptable only when the operator and the vault's owner are the same person. Any other user's restore uses the key their own cell mints (`add-exomem-cloud-vault-import`, design D1), never this procedure.

What moves is the vault directory, as a point-in-time copy. The source machine's machine-local state (indexes, the governance store) does not move; the cell rebuilds its own. The source keeps running and stays the owner's primary vault. Writes made after the copy are not in Cloud.

Three machines take part:

- **Source:** where the vault lives. It needs `tar`, `age` and `exomem`.
- **Operator machine:** reaches the source and the node over SSH. It only ever carries ciphertext.
- **Node:** holds the one-time key in `/dev/shm` and the ciphertext on disk, decrypts, and runs `kubectl` as break-glass.

The cell is down from step 5 to step 7, which takes minutes for a vault of a few gigabytes.

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
set -euo pipefail
: "${VAULT:?absolute path of the source vault}"
test -d "$VAULT/Knowledge Base"
exomem --version
exomem governance-schema status --vault "$VAULT" --json
```

Record both version lines and the image digest of the owner's cell (step 5 prints it). Stop if `schema_version` is 4: a standalone cell cannot serve it. Take a copy of the vault back with `exomem governance-schema downmigrate` and import the copy instead.

Decide what stays out. `.git` history and a sync tool's trash are not notes. Attachments come in. Then count what the archive will hold. The pruned paths here must match the stream's `--exclude` options in step 4:

```bash
PRUNE=(-path ./.git -prune -o -path ./.trash -prune -o)
cd "$VAULT"
find . "${PRUNE[@]}" \( -type l -o ! -type f ! -type d \) -printf '%y\n' | sort | uniq -c
EXPECT_FILES=$(find . "${PRUNE[@]}" -type f -printf '.' | wc -c)
EXPECT_BYTES=$(find . "${PRUNE[@]}" -type f -printf '%s\n' | awk '{s += $1} END {print s + 0}')
echo "files=$EXPECT_FILES bytes=$EXPECT_BYTES"
```

The first command must print nothing. A link, device or FIFO refuses the whole import, so resolve each one on the source first; list them yourself with `find . -type l`, and keep the names out of any shared record. Keep `EXPECT_FILES` and `EXPECT_BYTES` for step 6.

## 2. Size the cell

The cell's volume holds the new vault and the prior one until step 9, plus rebuilt indexes. It needs at least twice `EXPECT_BYTES` plus 2 GiB free space. If the cell's `storage_gib` is smaller, raise it as the control database owner:

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
set -euo pipefail
umask 077
: "${CELL_ID:?the owner cell ID}"
[[ "$CELL_ID" =~ ^[a-z2-7]{16}$ ]] || exit 1
IMPORT_ID=$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
KEY_DIR="/dev/shm/exomem-import-$IMPORT_ID"
WORK="/var/lib/exomem-import/$IMPORT_ID"
mkdir -m 700 "$KEY_DIR"
install -d -m 700 /var/lib/exomem-import "$WORK"
age-keygen -o "$KEY_DIR/identity" 2>/dev/null
age-keygen -y "$KEY_DIR/identity"
echo "import_id=$IMPORT_ID"
```

`age` comes from the node's base packages. Keep this shell open for steps 5 to 9.

## 4. Stream the ciphertext

On the operator machine. The source archives and encrypts, and the node stores the ciphertext. The operator machine sees only ciphertext.

```bash
set -euo pipefail
: "${SOURCE:?ssh destination of the source machine}"
: "${NODE:?ssh destination of the node, as root}"
: "${VAULT:?absolute path of the source vault on the source machine}"
: "${RECIPIENT:?the recipient step 3 printed}"
: "${IMPORT_ID:?the import_id step 3 printed}"
[[ "$RECIPIENT" =~ ^age1[02-9ac-hj-np-z]{58}$ ]] || exit 1
[[ "$IMPORT_ID" =~ ^[0-9a-f]{8}$ ]] || exit 1
# Both commands are built here on purpose (SC2029): every value in them was checked above.
# shellcheck disable=SC2029
ssh "$SOURCE" "set -o pipefail; tar -C $(printf '%q' "$VAULT") --exclude=./.git --exclude=./.trash -cf - . | age -r $RECIPIENT" \
  | ssh "$NODE" "cat > /var/lib/exomem-import/$IMPORT_ID/vault.tar.age && sha256sum /var/lib/exomem-import/$IMPORT_ID/vault.tar.age && stat -c '%s bytes' /var/lib/exomem-import/$IMPORT_ID/vault.tar.age"
```

A failed or interrupted stream leaves a short file. Step 6 refuses it, because age authenticates its final chunk only at the end. Rerun this step to replace it. Record the digest and size.

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
[[ "$IMAGE" =~ @sha256:[a-f0-9]{64}$ ]] || exit 1
echo "image=$IMAGE"
no_runtime_pod() {
  test -z "$(kubectl -n "$NS" get pods -l app.kubernetes.io/name=exomem-cell -o name)"
}
# wait_gone NAMESPACE SELECTOR: until no pod matches, for up to five minutes.
wait_gone() {
  local tries=0
  while [ -n "$(kubectl -n "$1" get pods -l "$2" -o name)" ]; do
    tries=$((tries + 1))
    [ "$tries" -le 60 ] || return 1
    sleep 5
  done
}
```

A `desired_state` write would not hold: Substrate recomputes it from the entitlement, and cellctl would then start the runtime in the middle of the swap. Pause cellctl instead, then stop the runtime:

```bash
kubectl -n exomem-cloud scale deployment cellctl --replicas=0
wait_gone exomem-cloud app.kubernetes.io/name=cellctl
kubectl -n "$NS" scale statefulset cell --replicas=0
wait_gone "$NS" app.kubernetes.io/name=exomem-cell
no_runtime_pod
```

While cellctl is paused, no cell reconciles, backs up or upgrades. Keep the pause to this procedure. Only a deploy of the platform chart would bring cellctl back early, so run no platform deploy until step 7.

## 6. Unpack and swap

The helper pod runs the cell image with the cell's volume mounted. It has no API token, and no job-egress label, so the namespace's default-deny policy leaves it no network. The unpacker is the reviewed commit's `src/exomem/cloud_import.py`. Copy it to the node first, as `$WORK/cloud_import.py`, and record its SHA-256.

```bash
test -s "$WORK/cloud_import.py"
sha256sum "$WORK/cloud_import.py"
no_runtime_pod
cat <<EOF | kubectl apply -f -
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
no_runtime_pod
set -o pipefail
if ! age -d -i "$KEY_DIR/identity" "$WORK/vault.tar.age" \
  | helper python3 -I /tmp/cloud_import.py unpack --staging "$STAGING" \
      --max-bytes "$EXPECT_BYTES" --expect-files "$EXPECT_FILES" --expect-bytes "$EXPECT_BYTES"; then
  helper rm -rf -- "$STAGING"
  echo 'decryption or unpacking failed; the vault is unchanged' >&2
  exit 1
fi
helper test -d "$STAGING/Knowledge Base"
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
no_runtime_pod
# The script runs in the pod; $1 is the import ID passed after it.
# shellcheck disable=SC2016
helper sh -euc '
  prior=/data/.restore-prior-$1
  mkdir -m 700 "$prior"
  mv /data/vault "$prior/vault"
  if [ -d /data/host/.local/state/exomem/state ]; then mv /data/host/.local/state/exomem/state "$prior/state"; fi
  if [ -d /data/host/.cache/exomem ]; then mv /data/host/.cache/exomem "$prior/cache"; fi
  mv "/data/.import-$1" /data/vault
  ls -A /data /data/host/.local/state/exomem
' swap "$IMPORT_ID"
kubectl -n "$NS" delete pod owner-restore --wait=true
```

## 7. Start the cell

```bash
kubectl -n "$NS" scale statefulset cell --replicas=1
kubectl -n "$NS" rollout status statefulset/cell --timeout=900s
kubectl -n exomem-cloud scale deployment cellctl --replicas=1
kubectl -n exomem-cloud rollout status deployment/cellctl --timeout=300s
```

Scale the StatefulSet back by hand, as above. cellctl will not do it. Its row still reads `running`, so it only checks readiness and never re-applies the replica count. `cell-init` migrates state for the new vault, and the runtime serves while its indexes build in the background. A large vault takes minutes to become fully searchable.

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

## Rollback

Before step 9, a restore that does not start or does not answer is reversed with the same stop and the same helper pod. Once the restored cell has started, it has written its own derived state, so that is removed before the prior vault and state move back:

```bash
no_runtime_pod
# The script runs in the pod; $1 is the import ID passed after it.
# shellcheck disable=SC2016
helper sh -euc '
  prior=/data/.restore-prior-$1
  mv /data/vault "/data/.rejected-$1"
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
