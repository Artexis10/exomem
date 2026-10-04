<!-- authority:non-specification -->

# Cloud operator access

**Status:** the identity and audit procedures are rehearsed on the disposable
K3s cluster of the cellctl live suite (`infra/cellctl/tests/test_k3s_integration.py`),
which runs their marked blocks as written. The log-canary blocks carry the same
markers but need a real cell with search, recall and review, so they run only
on the Cloud node. On 2026-09-28 the operator certificate, the operator
checks, a break-glass mint, exec and audit trail, and the log canary with its
negative control all ran on the Cloud node as written.

Exomem Cloud has three cluster identities. Use the least one that does the job.

- **Everyday operator (`exomem-operator`).** The default for every procedure.
  It reads workload status, events, endpoint and isolation-policy metadata,
  and the content-free logs cells and controllers emit. It cannot read any Secret, and it cannot exec, attach,
  port-forward, proxy or add an ephemeral container. The platform chart's
  `exomem-operator-read` ClusterRole grants this to group `exomem:operators`.
  The certificate lasts 30 days.
- **Deploy (the K3s admin kubeconfig, `/etc/rancher/k3s/k3s.yaml`).** Root
  only. Use it for scripted Helm and apply procedures, and for approving the
  CSRs below. Those procedures name it explicitly; nothing uses it by default.
- **Break-glass (`exomem-break-glass`).** Mint it for one task and let it
  expire. It is in group `exomem:break-glass`, which the chart binds to
  `cluster-admin`, and cell admission admits connect subresources only for this
  group. No file for it outlives the task, and client certificates cannot be
  revoked, so the one-hour expiry is the control.

Every block runs on the node as root with shell tracing off. `kubectl` is not
on the node's PATH, so the blocks use `k3s kubectl`. A private key never
leaves the node, and nothing below prints one.

## Issue the everyday operator certificate

Run this at first install, and again before the current certificate expires.
It writes a fresh key and certificate, then replaces the kubeconfig in one
rename.

<!-- rehearsed: issue-operator -->
```bash
(
set -euo pipefail
umask 077
ADMIN_KUBECONFIG="${ADMIN_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
OPERATOR_KUBECONFIG="${OPERATOR_KUBECONFIG:-/root/.kube/exomem-operator.kubeconfig}"
admin() { k3s kubectl --kubeconfig "$ADMIN_KUBECONFIG" "$@"; }
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$(dirname "$OPERATOR_KUBECONFIG")"
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$work/key.pem"
openssl req -new -key "$work/key.pem" -subj "/O=exomem:operators/CN=exomem-operator" -out "$work/csr.pem"
csr="exomem-operator-$(date -u +%Y%m%dt%H%M%S)"
admin apply -f - <<EOF
apiVersion: certificates.k8s.io/v1
kind: CertificateSigningRequest
metadata:
  name: ${csr}
spec:
  request: $(base64 -w0 < "$work/csr.pem")
  signerName: kubernetes.io/kube-apiserver-client
  expirationSeconds: 2592000
  usages: [digital signature, client auth]
EOF
admin certificate approve "$csr"
certificate=""
for _ in $(seq 30); do
  certificate=$(admin get csr "$csr" -o jsonpath='{.status.certificate}')
  [ -n "$certificate" ] && break
  sleep 1
done
test -n "$certificate"
printf '%s' "$certificate" | base64 -d > "$work/cert.pem"
admin config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d > "$work/ca.pem"
server=$(admin config view --raw -o jsonpath='{.clusters[0].cluster.server}')
next="$OPERATOR_KUBECONFIG.next"
rm -f -- "$next"
k3s kubectl --kubeconfig "$next" config set-cluster exomem --server="$server" \
  --certificate-authority="$work/ca.pem" --embed-certs=true >/dev/null
k3s kubectl --kubeconfig "$next" config set-credentials exomem-operator \
  --client-certificate="$work/cert.pem" --client-key="$work/key.pem" --embed-certs=true >/dev/null
k3s kubectl --kubeconfig "$next" config set-context exomem-operator --cluster=exomem --user=exomem-operator >/dev/null
k3s kubectl --kubeconfig "$next" config use-context exomem-operator >/dev/null
chmod 600 "$next"
mv -f -- "$next" "$OPERATOR_KUBECONFIG"
openssl x509 -in "$work/cert.pem" -noout -subject -enddate
)
```

Check the new identity before relying on it. Each `can-i` must answer `no`:

<!-- rehearsed: check-operator -->
```bash
(
set -euo pipefail
OPERATOR_KUBECONFIG="${OPERATOR_KUBECONFIG:-/root/.kube/exomem-operator.kubeconfig}"
op() { k3s kubectl --kubeconfig "$OPERATOR_KUBECONFIG" "$@"; }
test "$(stat -c '%a %U' "$OPERATOR_KUBECONFIG")" = "600 $(id -un)"
op auth whoami
op get nodes >/dev/null
# A subresource needs --subresource: `can-i create pods/exec` asks about a pod
# named "exec".
while read -r -a check; do
  if op auth can-i "${check[@]}" --all-namespaces >/dev/null; then
    echo "operator identity can ${check[*]}" >&2
    exit 1
  fi
done <<'CHECKS'
get secrets
list secrets
get pods --subresource=exec
create pods --subresource=exec
get pods --subresource=attach
create pods --subresource=attach
get pods --subresource=portforward
create pods --subresource=portforward
get pods --subresource=proxy
create pods --subresource=proxy
update pods --subresource=ephemeralcontainers
patch pods --subresource=ephemeralcontainers
create certificatesigningrequests
update certificatesigningrequests --subresource=approval
impersonate users
impersonate groups
CHECKS
)
```

## Make the operator identity the default

`k3s kubectl` otherwise falls back to the admin kubeconfig. Point root's
shells at the operator one:

```bash
OPERATOR_KUBECONFIG=/root/.kube/exomem-operator.kubeconfig
line="export KUBECONFIG=$OPERATOR_KUBECONFIG"
grep -qxF "$line" /root/.bashrc || printf '%s\n' "$line" >> /root/.bashrc
```

Deploy procedures keep passing `--kubeconfig /etc/rancher/k3s/k3s.yaml` (or
`KUBECONFIG=/etc/rancher/k3s/k3s.yaml`) explicitly. Keep both kubeconfigs mode
600 and owned by root; never copy either off the node.

## Re-issue before expiry

Read the current expiry, and rerun the issue block with at least a week to
spare. An expired certificate fails closed to no access, not to admin.

```bash
k3s kubectl --kubeconfig /root/.kube/exomem-operator.kubeconfig config view --raw \
  -o jsonpath='{.users[0].user.client-certificate-data}' | base64 -d | openssl x509 -noout -enddate
```

## Break-glass: mint a one-hour identity

Use break-glass only when the everyday identity cannot do the task, for
example a shell in a cell or reading a Secret. First record the reason and the
cell in the operator channel. The certificate and kubeconfig live in a private
directory in memory (`/dev/shm`) and are removed when the task ends. Nothing
standing is written.

<!-- rehearsed: mint-break-glass -->
```bash
BREAK_GLASS_CSR="exomem-break-glass-$(date -u +%Y%m%dt%H%M%S)"
# The work runs in a subshell, so set -e and the cleanup trap stay out of your
# shell and a failure leaves no key behind. Only the directory comes back.
BREAK_GLASS_DIR=$(
  set -euo pipefail
  umask 077
  exec 3>&1 1>&2
  ADMIN_KUBECONFIG="${ADMIN_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
  admin() { k3s kubectl --kubeconfig "$ADMIN_KUBECONFIG" "$@"; }
  dir=$(mktemp -d "${BREAK_GLASS_TMP:-/dev/shm}/exomem-break-glass.XXXXXX")
  trap 'rm -rf -- "$dir"' EXIT
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$dir/key.pem"
  openssl req -new -key "$dir/key.pem" -subj "/O=exomem:break-glass/CN=exomem-break-glass" -out "$dir/csr.pem"
  admin apply -f - <<EOF
apiVersion: certificates.k8s.io/v1
kind: CertificateSigningRequest
metadata:
  name: ${BREAK_GLASS_CSR}
spec:
  request: $(base64 -w0 < "$dir/csr.pem")
  signerName: kubernetes.io/kube-apiserver-client
  expirationSeconds: 3600
  usages: [digital signature, client auth]
EOF
  admin certificate approve "$BREAK_GLASS_CSR"
  certificate=""
  for _ in $(seq 30); do
    certificate=$(admin get csr "$BREAK_GLASS_CSR" -o jsonpath='{.status.certificate}')
    [ -n "$certificate" ] && break
    sleep 1
  done
  test -n "$certificate"
  printf '%s' "$certificate" | base64 -d > "$dir/cert.pem"
  # The signer must have honoured expirationSeconds: at most an hour from now
  # (a minute's slack for clock granularity), never the signer's default year.
  not_after=$(openssl x509 -in "$dir/cert.pem" -noout -enddate | cut -d= -f2)
  echo "notAfter=$not_after"
  test "$(date -u -d "$not_after" +%s)" -le "$(( $(date -u +%s) + 3600 + 60 ))"
  admin config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d > "$dir/ca.pem"
  server=$(admin config view --raw -o jsonpath='{.clusters[0].cluster.server}')
  k3s kubectl --kubeconfig "$dir/kubeconfig" config set-cluster exomem --server="$server" \
    --certificate-authority="$dir/ca.pem" --embed-certs=true >/dev/null
  k3s kubectl --kubeconfig "$dir/kubeconfig" config set-credentials exomem-break-glass \
    --client-certificate="$dir/cert.pem" --client-key="$dir/key.pem" --embed-certs=true >/dev/null
  k3s kubectl --kubeconfig "$dir/kubeconfig" config set-context break-glass --cluster=exomem \
    --user=exomem-break-glass >/dev/null
  k3s kubectl --kubeconfig "$dir/kubeconfig" config use-context break-glass >/dev/null
  rm -f -- "$dir/key.pem" "$dir/csr.pem" "$dir/cert.pem" "$dir/ca.pem"
  trap - EXIT
  printf '%s\n' "$dir" >&3
)
BREAK_GLASS_KUBECONFIG="$BREAK_GLASS_DIR/kubeconfig"
if [ -n "$BREAK_GLASS_DIR" ] && [ -s "$BREAK_GLASS_KUBECONFIG" ]; then
  echo "csr=$BREAK_GLASS_CSR kubeconfig=$BREAK_GLASS_KUBECONFIG"
else
  echo 'break-glass mint failed; nothing was kept' >&2
fi
```

Use it for the task alone, in the same shell, naming the kubeconfig on every
command, for example:

```bash
k3s kubectl --kubeconfig "$BREAK_GLASS_KUBECONFIG" -n "exo-cell-<cell id>" exec -it cell-0 -- sh
```

End the task by removing the directory. The certificate stays valid until it
expires, so do not copy it anywhere:

<!-- rehearsed: end-break-glass -->
```bash
rm -rf -- "$BREAK_GLASS_DIR"
unset BREAK_GLASS_KUBECONFIG
```

## Find break-glass use in the audit log

The node's audit policy logs every request at Metadata level: user, groups,
verb, resource, subresource, namespace, name and time. It records bodies only
for namespaces, PersistentVolumeClaims and StatefulSets, never for Secrets, and
none of those holds vault content. Logs stay on the node for 7 days (`audit.log`, plus rotated
`audit-*.log`). Root can rewrite them, so treat them as a trail, not
tamper-proof evidence. Record in the operator channel the CSR name and the
lines below:

<!-- rehearsed: audit-break-glass -->
```bash
(
set -euo pipefail
AUDIT_LOG="${AUDIT_LOG:-/var/lib/rancher/k3s/server/logs/audit.log}"
: "${BREAK_GLASS_CSR:?the break-glass CSR name the mint step printed}"
# The CSR's creation and its approval.
grep -F '"resource":"certificatesigningrequests"' "$AUDIT_LOG" | grep -F "\"name\":\"$BREAK_GLASS_CSR\"" \
  | grep -F '"verb":"create"'
grep -F '"resource":"certificatesigningrequests"' "$AUDIT_LOG" | grep -F "\"name\":\"$BREAK_GLASS_CSR\"" \
  | grep -F '"subresource":"approval"'
# Every request made as break-glass, exec sessions included: by the minted
# identity, by anyone carrying the group, or by an admin impersonating either
# (impersonatedUser).
grep -F -e '"username":"exomem-break-glass"' -e '"exomem:break-glass"' "$AUDIT_LOG"
)
```

Everyday-identity requests appear the same way under
`"username":"exomem-operator"`.

## Proving logs are content-free

The everyday identity reads `pods/log` in every namespace, because RBAC cannot
exclude cell namespaces created at runtime. So the promise that the operator
cannot see content by accident rests on what cells, cellctl and the gateway
write to their logs. This procedure checks it with a unique canary string.
Run it after any change to the cell image, cellctl or the gateway.

The search covers:

- every pod in the cell namespaces and export scratch namespaces, and the
  cellctl and gateway pods, through `kubectl logs` as the operator identity;
- the kubelet's own log files for those namespaces under `/var/log/pods/`, as
  root on the node, rotated and gzipped files included, because
  `kubectl logs` returns only a container's current file;
- the canary itself and its three base64 alignments, so an encoded copy is
  found too.

The edge is not scanned. Traefik's access log is off
(`traefik.accessLog.enabled: false` in `infra/helm/platform/values.yaml`),
and its own log carries no request content.

Use the owner's own Cloud account; never write a canary into a friend's vault.
Run the write and the scan outside the nightly backup window, 02:00–05:00 UTC
(`cells.backupWindow` in the same file), and when no cell upgrade or restore is
due. A finished backup or restore Job's logs go when its pod does, so the scan
lists, from the audit log, every cell or scratch Job created after the write.
For each one listed, its logs were not searched unless its pod was still there.

Make the canary on the node:

<!-- rehearsed: canary-generate -->
```bash
if [ "$(date -u +%-H)" -ge 2 ] && [ "$(date -u +%-H)" -lt 5 ]; then
  echo 'inside the nightly backup window (02:00-05:00 UTC); wait until it ends' >&2
else
  CANARY="exomem-canary-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
  CANARY_WRITTEN_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  export CANARY CANARY_WRITTEN_AT
  echo "$CANARY"
fi
```

Then, through the Cloud connector, in one session:

1. Write a note containing the canary, for example with `remember`: "Canary
   note: `<canary>` marks the content-free log check."
2. Search for it: `ask_memory` with the canary itself.
3. Recall it: `ask_memory` with a paraphrase ("the note that marks the log
   check"), then `read_memory` on the note it cites.
4. Review it: `review_memory` over the note.

Every step must find the note, or the canary never reached the code paths that
matter. Then scan straight away, within the hour. A pod replaced in the
meantime takes its `kubectl logs` with it, so if any cell, cellctl or gateway
pod restarted or was replaced since step 1, start again with a new canary.

Define the search. `canary_hits` reads every container's current log, and its
previous log when the container has restarted. `canary_file_hits` reads the
kubelet's files for the namespaces it is given. Both keep logs in memory and
print only counts:

<!-- rehearsed: canary-scan-define -->
```bash
OPERATOR_KUBECONFIG="${OPERATOR_KUBECONFIG:-/root/.kube/exomem-operator.kubeconfig}"
op() { k3s kubectl --kubeconfig "$OPERATOR_KUBECONFIG" "$@"; }
CANARY_PODS=0 CANARY_STREAMS=0 CANARY_UNREADABLE=0 CANARY_HITS=0 CANARY_FILES=0 CANARY_FILE_HITS=0
# The canary and its base64 alignments: for 0, 1 or 2 bytes before it, the
# encoded characters that depend on the canary alone.
canary_patterns() {
  local skip encoded from to groups
  local -a start=(0 2 3)
  CANARY_PATTERNS=(-e "$CANARY")
  for skip in 0 1 2; do
    encoded=$({ head -c "$skip" /dev/zero; printf '%s' "$CANARY"; } | base64 -w0)
    # Only whole 3-byte groups: the last partial one depends on what follows.
    groups=$(( (skip + ${#CANARY}) / 3 ))
    from=${start[$skip]} to=$(( groups * 4 ))
    CANARY_PATTERNS+=(-e "${encoded:from:to-from}")
  done
}
canary_hits() {
  local namespace=$1 selector=${2:-} pods pod statuses container restarts stream text count
  local -a scope=(--namespace "$namespace") previous
  if [ -n "$selector" ]; then scope+=(--selector "$selector"); fi
  if ! pods=$(op get pods "${scope[@]}" -o jsonpath='{.items[*].metadata.name}'); then
    CANARY_UNREADABLE=$((CANARY_UNREADABLE + 1))
    printf '%s pods unreadable\n' "$namespace"
    return 0
  fi
  for pod in $pods; do
    CANARY_PODS=$((CANARY_PODS + 1))
    if ! statuses=$(op get pod "$pod" --namespace "$namespace" -o jsonpath='{range .status.initContainerStatuses[*]}{.name}{" "}{.restartCount}{"\n"}{end}{range .status.containerStatuses[*]}{.name}{" "}{.restartCount}{"\n"}{end}') \
        || [ -z "$statuses" ]; then
      CANARY_UNREADABLE=$((CANARY_UNREADABLE + 1))
      printf '%s/%s no readable container status\n' "$namespace" "$pod"
      continue
    fi
    while read -r container restarts; do
      for stream in current previous; do
        previous=()
        if [ "$stream" = previous ]; then
          [ "$restarts" -gt 0 ] || continue
          previous=(--previous)
        fi
        CANARY_STREAMS=$((CANARY_STREAMS + 1))
        if ! text=$(op logs "$pod" --namespace "$namespace" --container "$container" "${previous[@]}" 2>/dev/null); then
          CANARY_UNREADABLE=$((CANARY_UNREADABLE + 1))
          printf '%s/%s %s %s unreadable\n' "$namespace" "$pod" "$container" "$stream"
          continue
        fi
        count=$(grep -cF "${CANARY_PATTERNS[@]}" <<<"$text" || true)
        CANARY_HITS=$((CANARY_HITS + count))
        printf '%s/%s %s %s hits=%s\n' "$namespace" "$pod" "$container" "$stream" "$count"
      done
    done <<<"$statuses"
  done
}
# canary_file_hits NAME...: the kubelet's files under the pod directories
# matching these find -name patterns (<namespace>_<pod>_<uid>). As root.
canary_file_hits() {
  local root=${CANARY_POD_LOGS:-/var/log/pods} name file count
  local -a names=()
  for name in "$@"; do names+=(-o -name "$name"); done
  while IFS= read -r -d '' file; do
    CANARY_FILES=$((CANARY_FILES + 1))
    # containerd splits a long line into partial (P) records at 16 KiB;
    # rejoin them per stream (stdout and stderr interleave) so a canary
    # straddling a split is still one line.
    count=$(zcat -f -- "$file" \
      | awk '{ s = $2; tag = $3; sub(/^[^ ]+ [^ ]+ [^ ]+ /, ""); buf[s] = buf[s] $0; if (tag != "P") { print buf[s]; buf[s] = "" } } END { for (s in buf) if (buf[s] != "") print buf[s] }' \
      | grep -cF "${CANARY_PATTERNS[@]}" || true)
    CANARY_FILE_HITS=$((CANARY_FILE_HITS + count))
    if [ "$count" -gt 0 ]; then printf '%s hits=%s\n' "$file" "$count"; fi
  done < <(find "$root" -mindepth 1 -maxdepth 1 -type d \( "${names[@]:1}" \) -exec find {} -type f -print0 \;)
}
```

Scan every pod in every cell and scratch namespace, cellctl and the gateway,
then the kubelet's files for those namespaces. The scan passes only with at
least one cell, at least one cellctl pod and one gateway pod, every stream
readable, some kubelet files read, and no hit anywhere. It runs in a subshell,
so a failed check does not end your shell:

<!-- rehearsed: canary-scan -->
```bash
(
set -euo pipefail
: "${CANARY:?the canary written through the connector}"
: "${CANARY_WRITTEN_AT:?the time the canary was made}"
[[ "$CANARY" =~ ^exomem-canary-[0-9a-f]{16}$ ]] || exit 1
hour=$(date -u +%-H)
if [ "$hour" -ge 2 ] && [ "$hour" -lt 5 ]; then
  echo 'inside the nightly backup window; start again with a new canary after 05:00 UTC' >&2
  exit 1
fi
elapsed=$(( $(date -u +%s) - $(date -u -d "$CANARY_WRITTEN_AT" +%s) ))
if [ "$elapsed" -ge 3600 ]; then
  echo 'more than an hour since the write; start again with a new canary' >&2
  exit 1
fi
canary_patterns
CANARY_PODS=0 CANARY_STREAMS=0 CANARY_UNREADABLE=0 CANARY_HITS=0 CANARY_FILES=0 CANARY_FILE_HITS=0
cells=0
while read -r namespace; do
  case "$namespace" in exo-cell-*) cells=$((cells + 1)) ;; esac
  canary_hits "$namespace"
done < <(op get namespaces -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' \
  | grep -E '^exo-(cell-[a-z2-7]{16}|scratch-[a-z2-7]{16}-[0-9a-f]{8})$')
before=$CANARY_PODS
canary_hits exomem-cloud app.kubernetes.io/name=cellctl
cellctl=$((CANARY_PODS - before))
before=$CANARY_PODS
canary_hits exomem-cloud app.kubernetes.io/name=exomem-cloud-gateway
gateway=$((CANARY_PODS - before))
canary_file_hits 'exo-cell-*' 'exo-scratch-*' 'exomem-cloud_*'
# Jobs are deleted with their pods 300 s after they finish, so the cluster
# cannot list the ones whose logs are gone. The audit log records every Job
# create at Metadata level; list those in cell and scratch namespaces since
# the write, including rotated audit files. A failure to read them stops
# the scan rather than reading as no Jobs.
audit=${AUDIT_LOG:-/var/lib/rancher/k3s/server/logs/audit.log}
test -r "$audit"
jobs_listing=$(python3 - "$CANARY_WRITTEN_AT" "$audit" <<'PY'
import glob, json, os, re, sys
since, path = sys.argv[1][:19], sys.argv[2]
scope = re.compile(r"^exo-(cell-[a-z2-7]{16}|scratch-[a-z2-7]{16}-[0-9a-f]{8})$")
stem, ext = os.path.splitext(path)
for name in sorted(glob.glob(f"{stem}-*{ext}")) + [path]:
  with open(name, encoding="utf-8", errors="replace") as log:
    for line in log:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # a line still being written, or a damaged one
        ref = event.get("objectRef") or {}
        if event.get("verb") != "create" or ref.get("resource") != "jobs":
            continue
        if not scope.match(ref.get("namespace") or "") or event.get("stage") != "ResponseComplete":
            continue
        created = event.get("requestReceivedTimestamp", "")
        if created[:19] >= since:
            print(ref["namespace"], ref.get("name", "?"), created)
PY
)
jobs_after=0
while read -r namespace job created; do
  [ -n "$namespace" ] || continue
  jobs_after=$((jobs_after + 1))
  echo "job created after the write, logs searched only while its pod exists: $namespace/$job at $created"
done <<< "$jobs_listing"
echo "cells=$cells cellctl=$cellctl gateway=$gateway pods=$CANARY_PODS streams=$CANARY_STREAMS unreadable=$CANARY_UNREADABLE hits=$CANARY_HITS files=$CANARY_FILES file_hits=$CANARY_FILE_HITS jobs_after_write=$jobs_after"
test "$cells" -gt 0
test "$cellctl" -ge 1
test "$gateway" -ge 1
test "$CANARY_UNREADABLE" -eq 0
test "$CANARY_FILES" -gt 0
test "$CANARY_HITS" -eq 0
test "$CANARY_FILE_HITS" -eq 0
)
```

A clean scan proves nothing unless the same search can find the canary. As a
negative control, the deploy identity runs a throwaway pod that prints the
canary, and one base64 alignment of it, on two lines. The same two searches
must each find both lines. The pod runs in its own namespace,
`exomem-canary-control`, created and deleted here. `exomem-cloud` has no quota
and would also work, but it holds the Cloud keys, and a throwaway pod does not
belong beside them. Deleting a dedicated namespace also cannot touch a real
workload. The pod reuses cellctl's digest-pinned image, which is already on
the node:

<!-- rehearsed: canary-control -->
```bash
(
set -euo pipefail
: "${CANARY:?the canary written through the connector}"
[[ "$CANARY" =~ ^exomem-canary-[0-9a-f]{16}$ ]] || exit 1
ADMIN_KUBECONFIG="${ADMIN_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
admin() { k3s kubectl --kubeconfig "$ADMIN_KUBECONFIG" "$@"; }
CONTROL_NAMESPACE=exomem-canary-control
image=$(op get deployment cellctl --namespace exomem-cloud -o jsonpath='{.spec.template.spec.containers[0].image}')
[[ "$image" =~ @sha256:[a-f0-9]{64}$ ]] || exit 1
test -z "$(admin get namespace "$CONTROL_NAMESPACE" --ignore-not-found -o name)"
trap 'admin delete namespace "$CONTROL_NAMESPACE" --ignore-not-found --wait=false >/dev/null' EXIT
admin create namespace "$CONTROL_NAMESPACE"
admin apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: canary-echo
  namespace: ${CONTROL_NAMESPACE}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: echo
      image: ${image}
      command: [python3, -c, 'import base64, sys, time; c = sys.argv[1]; print(c); print(base64.b64encode(b"x" + c.encode()).decode(), flush=True); time.sleep(600)', '${CANARY}']
      resources:
        requests: {cpu: 10m, memory: 32Mi}
        limits: {cpu: 100m, memory: 64Mi}
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: {drop: [ALL]}
EOF
admin wait --namespace "$CONTROL_NAMESPACE" --for=condition=Ready pod/canary-echo --timeout=120s
canary_patterns
for _ in $(seq 10); do
  CANARY_PODS=0 CANARY_STREAMS=0 CANARY_UNREADABLE=0 CANARY_HITS=0 CANARY_FILES=0 CANARY_FILE_HITS=0
  canary_hits "$CONTROL_NAMESPACE"
  canary_file_hits "${CONTROL_NAMESPACE}_*"
  [ "$CANARY_HITS" -ge 2 ] && [ "$CANARY_FILE_HITS" -ge 2 ] && break
  sleep 2
done
echo "control: pods=$CANARY_PODS streams=$CANARY_STREAMS unreadable=$CANARY_UNREADABLE hits=$CANARY_HITS files=$CANARY_FILES file_hits=$CANARY_FILE_HITS"
admin delete namespace "$CONTROL_NAMESPACE" --wait=true --timeout=300s
test "$CANARY_HITS" -ge 2
test "$CANARY_FILE_HITS" -ge 2
)
```

Record the canary's scan line (`cells=… hits=0 … file_hits=0`), any Job it
listed, and the control line (both hit counts at least 2) in the operator
channel, with the date and the image digests of the cell, cellctl and the
gateway. Remove the canary note through the connector if you do not want it
kept.

## Cloud compute policy and capacity

The `cloud` image selects `EXOMEM_CLOUD_RESOURCE_POLICY=service-v1`; local and
Hosted images retain their existing policy. This profile governs CPU residency,
thread and recovery budgets. A vault's stored mode and engagement preference
remain user settings and must match their pre-roll values after deployment.
It supports the ordinary vault/schema resolvers; no operator-vault path belongs
in the image or chart.

Inspect the existing resource status before and after a roll. Its policy reports
`resource_profile`, the actual stored `mode`, core residency/readiness, preparation
budgets and durable semantic debt. `semantic_execution` reports whether its single
recovery owner is running and whether a small or bulk parent is active. Unknown
counts or ages remain unknown. A resource refusal retains canonical bytes and
exact queued custody; unchanged refused input is not repeatedly prepared.

Service overrides accept `EXOMEM_EMBED_BATCH` from 1 to 8, `EXOMEM_CPU_THREADS`
from 1 to 2 and `EXOMEM_SYNC_WORKERS` from 4 to 8. Device overrides require CPU.
The core recall encoder stays resident; optional models and large caches retain
idle reclamation. Whole-model preload and unsafe native-thread overrides are
rejected. Keep the `model_env` forbidden-prefix checks: that tenant-facing map
must not select `EXOMEM_CLOUD_*` policy or change state placement. Offline jobs
using the Cloud image retain `EXOMEM_CLOUD_CELL=1`, as the existing init job does.

At the unchanged 2 CPU / 3 GiB cell limit, measure cgroup peak during warmup,
saves, import competition and restart; require at most 80% of the limit and no
OOM. For node capacity, charge each cell the greater of its memory request and
measured warm peak, add measured platform usage, and retain at least 20% warm
headroom before admitting more cells. Pod readiness alone does not prove capacity.

Kernel-cache control is a separate node adoption. The existing K3s role defaults
`k3s_memory_qos_enabled` to false; enable it only for a dedicated staged node
after exact-source acceptance and sibling-capacity verification. On the pinned
K3s 1.35.6, the owned kubelet drop-in enables MemoryQoS with a 0.625 throttling
factor. A 1 GiB request / 3 GiB limit produces `memory.min=1 GiB` and
`memory.high=2.25 GiB`, with the same hard limit. This protects requested memory
as well as throttling allocations; it affects every pod on that node. It requires
cgroup v2 and kernel 5.9 or later. Image selection does not enable the setting.

Record effective leaf and pod controls, lifetime peak, host reclaim/swap, actual
save/query/shutdown outcomes and warmed sibling capacity. A result materially
assisted by unrelated global reclaim does not prove the required headroom;
nonzero hierarchical PSI alone is not a failure. Use the role's canonical
drop-in in isolated acceptance rather than copying its policy values.

To undo node adoption, disable the inventory option and apply the same role to
remove only its owned drop-in and restart the selected K3s service. Recreate
affected containers through the controlled node/cell lifecycle, preserving
canonical data and queued work, and verify effective `memory.min/high/max`
and readiness: existing containers may retain old settings. Do not restart or
recreate QA/reviewer workloads outside their coordinated windows. A legacy
image rollback alone does not undo node memory policy.

Canary the owner vault first with verified identity, current backup and preserved
source/preferences. Hold fleet and friends on a latency, freshness, memory or
preference miss. Roll back through the existing image-pin procedure to a verified
legacy Cloud image; additive queue metadata remains compatible and custody stays
on disk. Do not delete derived state or drain beside the running service. A
rollback does not close an unresolved preference-preservation incident.
