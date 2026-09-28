<!-- authority:non-specification -->

# Cloud operator access

**Status:** the identity and audit procedures are rehearsed on the disposable
K3s cluster of the cellctl live suite (`infra/cellctl/tests/test_k3s_integration.py`),
which runs their marked blocks as written. The log-canary blocks carry the same
markers but need a real cell with search, recall and review, so they run only
on the Cloud node. Nothing here is yet exercised on the Cloud node.

Exomem Cloud has three cluster identities. Use the least one that does the job.

- **Everyday operator (`exomem-operator`).** The default for every procedure.
  It reads workload status, events and the content-free logs cells and
  controllers emit. It cannot read any Secret, and it cannot exec, attach,
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
```

Check the new identity before relying on it. Each `can-i` must answer `no`:

<!-- rehearsed: check-operator -->
```bash
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
CHECKS
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
set -euo pipefail
umask 077
ADMIN_KUBECONFIG="${ADMIN_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
admin() { k3s kubectl --kubeconfig "$ADMIN_KUBECONFIG" "$@"; }
BREAK_GLASS_DIR=$(mktemp -d "${BREAK_GLASS_TMP:-/dev/shm}/exomem-break-glass.XXXXXX")
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$BREAK_GLASS_DIR/key.pem"
openssl req -new -key "$BREAK_GLASS_DIR/key.pem" -subj "/O=exomem:break-glass/CN=exomem-break-glass" \
  -out "$BREAK_GLASS_DIR/csr.pem"
BREAK_GLASS_CSR="exomem-break-glass-$(date -u +%Y%m%dt%H%M%S)"
admin apply -f - <<EOF
apiVersion: certificates.k8s.io/v1
kind: CertificateSigningRequest
metadata:
  name: ${BREAK_GLASS_CSR}
spec:
  request: $(base64 -w0 < "$BREAK_GLASS_DIR/csr.pem")
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
printf '%s' "$certificate" | base64 -d > "$BREAK_GLASS_DIR/cert.pem"
admin config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d \
  > "$BREAK_GLASS_DIR/ca.pem"
server=$(admin config view --raw -o jsonpath='{.clusters[0].cluster.server}')
BREAK_GLASS_KUBECONFIG="$BREAK_GLASS_DIR/kubeconfig"
k3s kubectl --kubeconfig "$BREAK_GLASS_KUBECONFIG" config set-cluster exomem --server="$server" \
  --certificate-authority="$BREAK_GLASS_DIR/ca.pem" --embed-certs=true >/dev/null
k3s kubectl --kubeconfig "$BREAK_GLASS_KUBECONFIG" config set-credentials exomem-break-glass \
  --client-certificate="$BREAK_GLASS_DIR/cert.pem" --client-key="$BREAK_GLASS_DIR/key.pem" --embed-certs=true >/dev/null
k3s kubectl --kubeconfig "$BREAK_GLASS_KUBECONFIG" config set-context break-glass --cluster=exomem \
  --user=exomem-break-glass >/dev/null
k3s kubectl --kubeconfig "$BREAK_GLASS_KUBECONFIG" config use-context break-glass >/dev/null
rm -f -- "$BREAK_GLASS_DIR/key.pem" "$BREAK_GLASS_DIR/csr.pem" "$BREAK_GLASS_DIR/cert.pem" "$BREAK_GLASS_DIR/ca.pem"
openssl x509 -in <(k3s kubectl --kubeconfig "$BREAK_GLASS_KUBECONFIG" config view --raw \
  -o jsonpath='{.users[0].user.client-certificate-data}' | base64 -d) -noout -enddate
echo "csr=$BREAK_GLASS_CSR kubeconfig=$BREAK_GLASS_KUBECONFIG"
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
verb, resource, subresource, namespace, name and time, with no request or
response bodies. Logs stay on the node for 7 days (`audit.log`, plus rotated
`audit-*.log`). Root can rewrite them, so treat them as a trail, not
tamper-proof evidence. Record in the operator channel the CSR name and the
lines below:

<!-- rehearsed: audit-break-glass -->
```bash
set -euo pipefail
AUDIT_LOG="${AUDIT_LOG:-/var/lib/rancher/k3s/server/logs/audit.log}"
: "${BREAK_GLASS_CSR:?the break-glass CSR name the mint step printed}"
# The CSR's creation and its approval.
grep -F '"resource":"certificatesigningrequests"' "$AUDIT_LOG" | grep -F "\"name\":\"$BREAK_GLASS_CSR\"" \
  | grep -F '"verb":"create"'
grep -F '"resource":"certificatesigningrequests"' "$AUDIT_LOG" | grep -F "\"name\":\"$BREAK_GLASS_CSR\"" \
  | grep -F '"subresource":"approval"'
# Every request the break-glass identity made, exec sessions included.
grep -F '"username":"exomem-break-glass"' "$AUDIT_LOG"
```

Everyday-identity requests appear the same way under
`"username":"exomem-operator"`.

## Proving logs are content-free

The everyday identity reads `pods/log` in every namespace, because RBAC cannot
exclude cell namespaces created at runtime. So the promise that the operator
cannot see content by accident rests on what cells, cellctl and the gateway
write to their logs. This procedure checks it with a unique canary string.
Run it after any change to the cell image, cellctl or the gateway.

Use the owner's own Cloud account; never write a canary into a friend's vault.

Make the canary on the node:

<!-- rehearsed: canary-generate -->
```bash
CANARY="exomem-canary-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
export CANARY
echo "$CANARY"
```

Then, through the Cloud connector, in one session:

1. Write a note containing the canary, for example with `remember`: "Canary
   note: `<canary>` marks the content-free log check."
2. Search for it: `ask_memory` with the canary itself.
3. Recall it: `ask_memory` with a paraphrase ("the note that marks the log
   check"), then `read_memory` on the note it cites.
4. Review it: `review_memory` over the note.

Every step must find the note, or the canary never reached the code paths that
matter. Then scan the logs straight away with the operator identity. A pod
replaced in the meantime takes its logs with it, so if any cell, cellctl or
gateway pod restarted or was replaced since step 1, start again with a new
canary.

Define the search. It reads every container's current log, and its previous
log when the container has restarted, and counts lines holding the canary. It
keeps logs in memory and prints only counts:

<!-- rehearsed: canary-scan-define -->
```bash
OPERATOR_KUBECONFIG="${OPERATOR_KUBECONFIG:-/root/.kube/exomem-operator.kubeconfig}"
op() { k3s kubectl --kubeconfig "$OPERATOR_KUBECONFIG" "$@"; }
CANARY_PODS=0 CANARY_STREAMS=0 CANARY_UNREADABLE=0 CANARY_HITS=0
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
        count=$(grep -cF -- "$CANARY" <<<"$text" || true)
        CANARY_HITS=$((CANARY_HITS + count))
        printf '%s/%s %s %s hits=%s\n' "$namespace" "$pod" "$container" "$stream" "$count"
      done
    done <<<"$statuses"
  done
}
```

Scan every pod in every cell namespace, plus cellctl and the gateway. The scan
passes only with at least one cell, both controllers, every stream readable
and no hit:

<!-- rehearsed: canary-scan -->
```bash
set -euo pipefail
: "${CANARY:?the canary written through the connector}"
[[ "$CANARY" =~ ^exomem-canary-[0-9a-f]{16}$ ]] || exit 1
CANARY_PODS=0 CANARY_STREAMS=0 CANARY_UNREADABLE=0 CANARY_HITS=0
cells=0
while read -r namespace; do
  cells=$((cells + 1))
  canary_hits "$namespace"
done < <(op get namespaces -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep -E '^exo-cell-[a-z2-7]{16}$')
before=$CANARY_PODS
canary_hits exomem-cloud 'app.kubernetes.io/name in (cellctl,exomem-cloud-gateway)'
controllers=$((CANARY_PODS - before))
echo "cells=$cells controllers=$controllers pods=$CANARY_PODS streams=$CANARY_STREAMS unreadable=$CANARY_UNREADABLE hits=$CANARY_HITS"
test "$cells" -gt 0
test "$controllers" -ge 2
test "$CANARY_UNREADABLE" -eq 0
test "$CANARY_HITS" -eq 0
```

A clean scan proves nothing unless the same search can find the canary. As a
negative control, the deploy identity runs a throwaway pod that prints the
canary, and the operator identity runs the same search over it. The pod runs
in its own namespace, `exomem-canary-control`, created and deleted here.
`exomem-cloud` has no quota and would also work, but it holds the Cloud keys,
and a throwaway pod does not belong beside them. Deleting a dedicated
namespace also cannot touch a real workload. The pod reuses cellctl's
digest-pinned image, which is already on the node:

<!-- rehearsed: canary-control -->
```bash
set -euo pipefail
: "${CANARY:?the canary written through the connector}"
[[ "$CANARY" =~ ^exomem-canary-[0-9a-f]{16}$ ]] || exit 1
ADMIN_KUBECONFIG="${ADMIN_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
admin() { k3s kubectl --kubeconfig "$ADMIN_KUBECONFIG" "$@"; }
CONTROL_NAMESPACE=exomem-canary-control
image=$(op get deployment cellctl --namespace exomem-cloud -o jsonpath='{.spec.template.spec.containers[0].image}')
[[ "$image" =~ @sha256:[a-f0-9]{64}$ ]] || exit 1
test -z "$(admin get namespace "$CONTROL_NAMESPACE" --ignore-not-found -o name)"
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
      command: [python3, -c, 'import sys, time; print(sys.argv[1], flush=True); time.sleep(600)', '${CANARY}']
      resources:
        requests: {cpu: 10m, memory: 32Mi}
        limits: {cpu: 100m, memory: 64Mi}
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: {drop: [ALL]}
EOF
admin wait --namespace "$CONTROL_NAMESPACE" --for=condition=Ready pod/canary-echo --timeout=120s
for _ in $(seq 10); do
  CANARY_PODS=0 CANARY_STREAMS=0 CANARY_UNREADABLE=0 CANARY_HITS=0
  canary_hits "$CONTROL_NAMESPACE"
  [ "$CANARY_HITS" -gt 0 ] && break
  sleep 2
done
echo "control: pods=$CANARY_PODS streams=$CANARY_STREAMS unreadable=$CANARY_UNREADABLE hits=$CANARY_HITS"
admin delete namespace "$CONTROL_NAMESPACE" --wait=true --timeout=300s
test "$CANARY_HITS" -gt 0
```

Record the canary's scan line (`cells=… hits=0`) and the control line
(`hits` above zero) in the operator channel, with the date and the image
digests of the cell, cellctl and the gateway. Remove the canary note through
the connector if you do not want it kept.
