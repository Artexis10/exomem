<!-- authority:non-specification -->

# Cloud operator vault export

**Status:** exercised against a real Cloud cell on 2026-09-28, where the
restore, the age export to a tenant-only recipient, recipient verification and
identity-checked scratch cleanup all passed. That run used the admin identity,
before break-glass existed. This procedure exports one
tenant's point-in-time `vault/` only. It never mounts, stops or changes the
live cell volume; it exports no `/data/host`, OAuth state, Kubernetes Secret or
credential. No runtime HTTP service is required. Only the tenant can read the
archive: it is encrypted to recipients the tenant supplies, and the operator
never holds a key that decrypts it or sees its plaintext.

The procedure reads the source cell's Secret and execs into the scratch
namespace, which the everyday operator identity cannot do and cell admission
admits only for break-glass. So it runs on the node, as root, in the shell
where you minted a break-glass identity for this export
([cloud operator access](cloud-operator-access.md)). No kubeconfig, break-glass
or admin, ever leaves the node; only the ciphertext and its digest do. If the
one-hour certificate expires mid-procedure, mint a fresh one and rerun this
setup.

`kubectl` is not on the node's PATH. The setup puts a private `kubectl` shim
in front of `k3s kubectl`, so the shell and the render script below both use
the break-glass kubeconfig:

```bash
set -euo pipefail
umask 077
: "${BREAK_GLASS_KUBECONFIG:?mint a break-glass identity first}"
export KUBECONFIG="$BREAK_GLASS_KUBECONFIG" KUBE_CONTEXT=break-glass
EXPORT_BIN=$(mktemp -d /dev/shm/exomem-export-bin.XXXXXX)
cat > "$EXPORT_BIN/kubectl" <<'SHIM'
#!/bin/sh
exec k3s kubectl "$@"
SHIM
chmod 700 "$EXPORT_BIN/kubectl"
export PATH="$EXPORT_BIN:$PATH"
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
test "$(command kubectl config current-context)" = "$KUBE_CONTEXT"
kubectl auth whoami | grep -qF 'exomem:break-glass'
```

## Select the source and snapshot

Record the authorized tenant, exact cell ID and requested recovery point in
the approved operator channel. Use an approved **read-only** control
database service. The following query must return exactly one row with that
tenant/cell pair; a namespace name alone does not establish ownership.

```bash
: "${PGSERVICE:?approved read-only control-db service required}"
: "${TENANT_ID:?authorized tenant ID required}"
: "${CELL_ID:?authorized cell ID required}"
[[ "$CELL_ID" =~ ^[a-z2-7]{16}$ ]] || exit 1
: "${KUBE_CONTEXT:?run the break-glass setup above first}"
psql -X -v ON_ERROR_STOP=1 -v tenant_id="$TENANT_ID" -v cell_id="$CELL_ID" <<'SQL'
SELECT cell_id, tenant_id, desired_state, observed_state, ready,
       hold_kind, last_backup_at, last_backup_snapshot
FROM exomem_cloud_cells
WHERE tenant_id = :'tenant_id' AND cell_id = :'cell_id';
SQL
```

Require the expected pair, `ready = true`, no active hold, a non-deleting
state, and a completed backup after the writes the tenant expects. Copy that
row's `last_backup_snapshot` into `SNAPSHOT`. Stop if the backup is too old for
the request. Record the break-glass CSR name with the authorization. Writes after it are
absent; wait for cellctl's next normal backup
if a newer point is needed. Repeat the query immediately before the restore
and require the same pair and selected backup ID. The export does not initiate
a backup or quiesce a writer itself. Verified deletion removes the per-cell
key and backups; this procedure cannot recover a deleted cell.

The recipients file comes from the tenant through the approved channel. The
operator never writes or edits it. The tenant also sends the file's SHA-256
(`sha256sum <file>` on their side) through a separate channel, and says how
many recipients it holds. The next block checks the file before any restore
and prints its fingerprint and recipient count; record both with the
authorization. It stops when:

- the file's SHA-256 differs from the one the tenant sent;
- a line is an SSH key (`ssh-*`): SSH recipients cannot be matched against the
  registered keys, so only age X25519 recipients (`age1…`) are accepted;
- a line is anything else that is not an age X25519 recipient;
- any recipient is one the operator or escrow holds a key for: the age
  recipients of the SOPS artifacts under the reviewed release's
  `infra/secrets/`.

If it stops, nothing has been restored. Ask the tenant for a file with only
their own age keys. Run it from a checkout of the reviewed release on the node.

```bash
: "${SNAPSHOT:?selected backup ID required}"
[[ "$SNAPSHOT" =~ ^[0-9a-f]{64}$ ]] || exit 1
: "${RECIPIENTS:?tenant-supplied age recipients file required}"
: "${RECIPIENTS_SHA256:?the SHA-256 the tenant sent separately}"
[[ "$RECIPIENTS_SHA256" =~ ^[0-9a-f]{64}$ ]] || exit 1
test -r "$RECIPIENTS"
if [ "$(sha256sum < "$RECIPIENTS" | cut -d' ' -f1)" != "$RECIPIENTS_SHA256" ]; then
  echo 'recipients file does not match the SHA-256 the tenant sent; stop' >&2
  exit 1
fi
tenant_recipients=$(grep -Ev '^[[:space:]]*(#|$)' "$RECIPIENTS" || true)
test -n "$tenant_recipients"
if printf '%s\n' "$tenant_recipients" | grep -q '^[[:space:]]*ssh-'; then
  echo 'SSH recipients cannot be checked against the registered keys; ask for age X25519 recipients; stop' >&2
  exit 1
fi
if printf '%s\n' "$tenant_recipients" | grep -Evq '^age1[02-9ac-hj-np-z]{58}$'; then
  echo 'recipients file has a line that is not an age X25519 recipient; stop' >&2
  exit 1
fi
RECIPIENT_COUNT=$(printf '%s\n' "$tenant_recipients" | wc -l)
registered=$(python3 -c '
import json, pathlib
for path in sorted(pathlib.Path("infra/secrets").rglob("*.sops.json")):
    for entry in json.loads(path.read_text(encoding="utf-8")).get("sops", {}).get("age") or []:
        print(entry["recipient"])
')
test -n "$registered"
if printf '%s\n' "$tenant_recipients" | grep -Fxq -f <(printf '%s\n' "$registered"); then
  echo 'a recipient is a registered operator or escrow key; stop before any restore' >&2
  exit 1
fi
echo "recipients sha256=$RECIPIENTS_SHA256 count=$RECIPIENT_COUNT"
: "${OUT_DIR:?private output directory required}"
test -d "$OUT_DIR" && test "$(stat -c %a "$OUT_DIR")" = 700
SCRATCH="exo-scratch-${CELL_ID}-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
export CELL_ID SNAPSHOT SCRATCH
```

Use the reviewed release's `infra/cellctl/src` and its pinned project-local uv
environment (see `CONTRIBUTING.md`); set `CELLCTL_PYTHON` to that environment's
Python. Keep shell tracing off. The recipients file holds only the tenant's
public keys. Do not put the source Secret, archive plaintext or a private age
identity in an argument, file or transcript.

## Render and apply only scratch resources

This script uses the existing `render_cell_manifests` and `render_restore_job`
functions, as rehearsal step 11 does. It takes the source `cell-credentials`
Secret into process memory and never prints it or writes a manifest file. It
reads the source Pod's digest image and PVC size, and the live cellctl
Deployment's bucket, S3 endpoint and job-egress exceptions. A unique scratch
namespace must not already exist.

```bash
: "${CELLCTL_PYTHON:?reviewed cellctl Python required}"
PYTHONPATH="$PWD/infra/cellctl/src" "$CELLCTL_PYTHON" - <<'PY'
import base64, datetime as dt, ipaddress, json, os, re, secrets, subprocess
from urllib.parse import urlparse
from cellctl.manifests import CellManifestSpec, namespace_name, render_cell_manifests, render_restore_job

cell_id, snapshot, scratch = (os.environ[k] for k in ("CELL_ID", "SNAPSHOT", "SCRATCH"))
assert re.fullmatch(r"[a-z2-7]{16}", cell_id)
assert re.fullmatch(r"[0-9a-f]{64}", snapshot)
assert re.fullmatch(rf"exo-scratch-{cell_id}-[0-9a-f]{{8}}", scratch)
source = namespace_name(cell_id)

def kubectl(*args, payload=None):
    result = subprocess.run(["kubectl", "--context", os.environ["KUBE_CONTEXT"], *args], input=payload, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"kubectl {args[0]} failed; inspect the named resource without dumping Secrets")
    return result.stdout

def get(kind, name, namespace=None):
    args = (["-n", namespace] if namespace else []) + ["get", kind, name, "-o", "json"]
    return json.loads(kubectl(*args))

assert not kubectl("get", "namespace", scratch, "--ignore-not-found", "-o", "name").strip()
assert get("namespace", source)["metadata"]["labels"]["exomem.io/cloud-cell"] == cell_id
pvc = get("pvc", "cell-data", source)
assert pvc["spec"]["storageClassName"] == "exomem-cloud-encrypted"
size = pvc["spec"]["resources"]["requests"]["storage"]
assert re.fullmatch(r"[1-9][0-9]*Gi", size)
pod = get("pod", "cell-0", source)
assert pod["status"]["phase"] == "Running"
image = next(c["image"] for c in pod["spec"]["containers"] if c["name"] == "exomem")
assert re.search(r"@sha256:[a-f0-9]{64}$", image)
data = get("secret", "cell-credentials", source)["data"]
keys = ("backup-password", "b2-key-id", "b2-key-secret")
credential = {k: base64.b64decode(data[k], validate=True).decode() for k in keys}
ctl = get("deployment", "cellctl", "exomem-cloud")
env = {e["name"]: e["value"] for e in ctl["spec"]["template"]["spec"]["containers"][0]["env"] if "value" in e}
bucket, endpoint = env["CELLCTL_B2_BUCKET_NAME"], env["CELLCTL_B2_ENDPOINT"]
assert bucket and urlparse(endpoint).scheme == "https" and urlparse(endpoint).hostname
except_cidrs = tuple(env["CELLCTL_JOB_EGRESS_EXCEPT"].split(","))
assert except_cidrs and all(ipaddress.ip_network(c) for c in except_cidrs)
spec = CellManifestSpec(
    cell_id=cell_id, image=image, replicas=0, read_only=False,
    storage_gib=int(size[:-2]), bearer_current=secrets.token_urlsafe(32),
    backup_password=credential["backup-password"],
    b2_key_id=credential["b2-key-id"], b2_key_secret=credential["b2-key-secret"],
    hold_kind="restore", hold_started_at=dt.datetime.now(dt.UTC).isoformat(),
    job_egress_except=except_cidrs,
)
allowed = {("Namespace", scratch), ("NetworkPolicy", "default-deny"),
           ("NetworkPolicy", "job-egress"), ("Secret", "cell-credentials"),
           ("PersistentVolumeClaim", "cell-data")}
documents = []
for doc in render_cell_manifests(spec):
    if doc["kind"] == "Namespace":
        doc["metadata"]["name"] = scratch
        labels = doc["metadata"]["labels"]
        labels.pop("exomem.io/cloud-cell")
        labels["exomem.io/scratch-of"] = cell_id
    else:
        doc["metadata"]["namespace"] = scratch
    if (doc["kind"], doc["metadata"]["name"]) in allowed:
        if doc["kind"] == "Secret": doc["data"].pop("cell-token")
        documents.append(doc)
job = render_restore_job(spec, bucket_name=bucket, endpoint=endpoint, snapshot_id=snapshot)
job["metadata"]["namespace"] = scratch
assert len(documents) == 5 and documents[0]["kind"] == "Namespace"
for doc in [*documents, job]:
    kubectl("apply", "--server-side", "--field-manager=cloud-export-operator",
            "-f", "-", payload=json.dumps(doc))
print(f"scratch={scratch} restore_job={job['metadata']['name']} image={image}")
PY
```

Only Namespace, default-deny/job-egress NetworkPolicies, Secret, PVC and Job
are applied. The scratch Namespace has no `exomem.io/cloud-cell` label, so
cellctl does not own it. No StatefulSet, Service or runtime ingress is created.
The renderer validates the snapshot ID and scopes restore to `/data/vault` and
`/data/host` on the **scratch** PVC. The Job has UID/GID 10001, no API token,
object-store TCP 443/DNS egress and no other allowed network access.

```bash
RESTORE_JOB=$(kubectl -n "$SCRATCH" get job -l exomem.io/cell-job=restore -o jsonpath='{.items[0].metadata.name}')
test -n "$RESTORE_JOB"
kubectl -n "$SCRATCH" wait --for=jsonpath='{.status.phase}'=Bound pvc/cell-data --timeout=300s
kubectl -n "$SCRATCH" wait --for=condition=complete "job/$RESTORE_JOB" --timeout=900s
IMAGE=$(kubectl -n "$SCRATCH" get job "$RESTORE_JOB" -o jsonpath='{.spec.template.spec.containers[0].image}')
[[ "$IMAGE" =~ @sha256:[a-f0-9]{64}$ ]] || exit 1
```

A failed Job is not an export. Inspect status/events without dumping the
Secret, volume or full Job environment. Do not start the archive Pod before
the restore completes.

## Encrypt and hand off `vault/`

Create a short-lived archive Pod after the restore. It mounts only the scratch
PVC read-only, has no API token and no job-egress label. Default-deny therefore
blocks all its network egress.

```bash
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: vault-archive
  namespace: ${SCRATCH}
  labels: {app.kubernetes.io/name: cloud-export-archive}
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
    - name: archive
      image: ${IMAGE}
      imagePullPolicy: IfNotPresent
      command: [python3, -c, 'import time; time.sleep(3600)']
      resources:
        requests: {cpu: 100m, memory: 128Mi}
        limits: {cpu: 500m, memory: 512Mi}
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: {drop: [ALL]}
      volumeMounts:
        - {name: data, mountPath: /data, readOnly: true}
  volumes:
    - name: data
      persistentVolumeClaim: {claimName: cell-data, readOnly: true}
EOF
kubectl -n "$SCRATCH" wait --for=condition=Ready pod/vault-archive --timeout=180s
```

Stream only `/data/vault` to age; no plaintext archive lands on the operator
host. The tar filter rejects symlinks, hardlinks and special files instead of
following them or passing unsafe extraction entries. `pipefail` propagates
both `kubectl exec` and age failures. Keep only the completed ciphertext:

```bash
archive="$OUT_DIR/${CELL_ID}-${SNAPSHOT}.vault.tar.age"
test ! -e "$archive"
temporary=$(mktemp "$OUT_DIR/.vault-XXXXXX.age")
if [ "$(sha256sum < "$RECIPIENTS" | cut -d' ' -f1)" != "$RECIPIENTS_SHA256" ]; then
  rm -f -- "$temporary"
  echo 'recipients file changed since it was checked; stop' >&2
  exit 1
fi
if kubectl -n "$SCRATCH" exec pod/vault-archive -- python3 -c '
import os, sys, tarfile
p = "/data/vault"
if not os.path.isdir(p): raise SystemExit("vault directory absent")
def safe(member):
    if not (member.isfile() or member.isdir()): raise ValueError("unsupported vault entry")
    return member
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as stream:
    stream.add(p, arcname="vault", filter=safe)
' | age -R "$RECIPIENTS" -o "$temporary"; then
  test -s "$temporary"
  mv -- "$temporary" "$archive"
else
  rm -f -- "$temporary"
  echo 'archive stream failed; no handoff artifact' >&2
  exit 1
fi
test "$(stat -c %a "$archive")" = 600
sha256sum "$archive"
stat -c '%s bytes' "$archive"
```

Transfer ciphertext and digest/size through the approved protected channel.
The authorized recipient compares the digest, then counts the recipient
stanzas in the archive header. The count of `-> X25519` stanzas must equal the
number of recipients they supplied, and no other recipient type may appear.
The count alone catches an added key but not a swapped one, so every identity
behind the supplied recipients must then decrypt the archive. Together these
prove it is encrypted to exactly the recipients the tenant supplied. Grease
stanzas, which some age implementations add and which carry no key, are
ignored. Then the recipient verifies archive members without extracting or
printing contents:

```bash
set -euo pipefail
: "${SUPPLIED_RECIPIENTS:?how many recipients the file you sent holds}"
sha256sum "$RECEIVED_ARCHIVE" # compare with the recorded sender digest
x25519=$(LC_ALL=C awk '/^--- /{exit} /^-> X25519 /{n++} END{print n+0}' "$RECEIVED_ARCHIVE")
others=$(LC_ALL=C awk '/^--- /{exit} /^-> / && $2 != "X25519" && $2 !~ /-grease$/ {n++} END{print n+0}' "$RECEIVED_ARCHIVE")
echo "header: x25519=$x25519 other=$others"
if [ "$x25519" != "$SUPPLIED_RECIPIENTS" ] || [ "$others" != 0 ]; then
  echo 'the archive is encrypted to a recipient you did not supply; do not use it' >&2
  exit 1
fi
# One identity file per supplied recipient; each must open the archive.
read -r -a identities <<< "${RECIPIENT_IDENTITIES:-$RECIPIENT_IDENTITY}"
[ "${#identities[@]}" = "$SUPPLIED_RECIPIENTS" ]
for identity in "${identities[@]}"; do
  age -d -i "$identity" "$RECEIVED_ARCHIVE" > /dev/null
done
age -d -i "${identities[0]}" "$RECEIVED_ARCHIVE" | python3 -c '
import sys, tarfile
count = 0
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as stream:
    for member in stream:
        parts = member.name.split("/")
        if parts[0] != "vault" or any(part in ("", ".", "..") for part in parts[1:]) or "\\" in member.name:
            raise SystemExit("non-vault path")
        if not (member.isfile() or member.isdir()):
            raise SystemExit("link or special entry")
        count += 1
if not count: raise SystemExit("empty archive")
while sys.stdin.buffer.read(1024 * 1024): pass # authenticate the full stream
'
```

The recipient also uses `set -o pipefail`, so decryption failure cannot look
like a successful tar check. Record the recipient's verification, never vault
filenames or contents. Only then remove **this** scratch namespace, checking
its audit label immediately before deletion:

```bash
SCRATCH_CELL=$(kubectl get namespace "$SCRATCH" -o jsonpath='{.metadata.labels.exomem\.io/scratch-of}')
test "$SCRATCH_CELL" = "$CELL_ID"
kubectl delete namespace "$SCRATCH" --wait=true --timeout=600s
test -z "$(kubectl get namespace "$SCRATCH" --ignore-not-found -o name)"
rm -rf -- "$EXPORT_BIN"
```

Then end the break-glass session as the access runbook describes.

If an earlier step fails, retain scratch for bounded diagnosis. For an
abandoned export, record that no artifact was handed off, then perform the
same identity-checked, exact-name cleanup; never target the source namespace
or a broad label selector. Record authorization, tenant/cell, snapshot ID/time,
the recipients file's fingerprint, scratch name, image digest, ciphertext
digest/size and the tenant's verification and cleanup outcome, without
recording credentials or vault content.

**Status:** this procedure was run for the owner's cell on 2026-09-28, with
the admin identity, before break-glass existed. It restored from B2 into a
scratch namespace and encrypted the archive to a tenant-only recipient. The recipient verified it (member count and marker
present), and the scratch namespace and its volume were removed.
