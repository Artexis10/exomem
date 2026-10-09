#!/usr/bin/env bash
# Exomem Cloud restore tools. Run on the node, as root, from a checkout of the
# reviewed release. docs/runbooks/cloud-operator-restore.md says when to use
# each one and what to record.
#
#   cloud_restore.sh drill CELL_ID SNAPSHOT REFERENCE
#     Restore SNAPSHOT into a scratch namespace, hash every restored vault file
#     and compare with REFERENCE: "live" (the running cell's vault) or
#     "prior:<8 hex>" (the vault an earlier restore kept). The cell is never
#     stopped, mounted by a new pod or written.
#   cloud_restore.sh restore CELL_ID SNAPSHOT
#     Replace the cell's backed-up paths (/data/vault and /data/host) with
#     SNAPSHOT. The prior contents stay in /data/.restore-prior-<run ID>. The
#     cell is down from the stop to the start.
#   cloud_restore.sh follow RUN_ID
#     Show a run's unit state and the end of its log.
#
# drill and restore copy the release's restore files to
# /var/lib/exomem-restore/<run ID>, start them there as a transient systemd
# unit, so a dropped SSH session cannot interrupt a run, and print the run ID.
# The run's last log line gives its verdict; rc=0 means it passed and its
# cleanup was verified. The log holds counts only. The hash lists name vault
# files, so they stay in /dev/shm/exomem-restore-<run ID>, removed after a pass.
set -euo pipefail
umask 077

K3S=/usr/local/bin/k3s
STATE=/var/lib/exomem-restore
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
MANIFESTS="$HERE/cloud_restore_manifests.py"
DIGEST="$HERE/cloud_vault_digest.py"
# The import runbook's sizing margin: room for the cell's own writes after the start.
MARGIN_BYTES=$((2 * 1024 * 1024 * 1024))
HELPER=cell-restore-helper

die() { echo "!! $*" >&2; exit 1; }
py() { python3 -I "$@"; }
admin() { "$K3S" kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml "$@"; }
# kubectl is the break-glass shim mint_break_glass writes; see run().
k() { kubectl --context break-glass "$@"; }

# Shape checks, before the values reach names and paths. The manifests module
# checks the cell and snapshot again.
check_arguments() {
  local mode=$1
  shift
  case "$mode" in
    drill) [ "$#" -eq 3 ] || die 'drill takes CELL_ID SNAPSHOT REFERENCE' ;;
    restore) [ "$#" -eq 2 ] || die 'restore takes CELL_ID SNAPSHOT' ;;
    *) die "unknown mode $mode" ;;
  esac
  [[ "$1" =~ ^[a-z2-7]{16}$ ]] || die 'CELL_ID is not a cell ID'
  [[ "$2" =~ ^[0-9a-f]{64}$ ]] || die 'SNAPSHOT is not a restic snapshot ID'
  if [ "$mode" = drill ]; then
    [[ "$3" = live || "$3" =~ ^prior:[0-9a-f]{8}$ ]] || die 'REFERENCE must be live or prior:<8 hex>'
  fi
}

launch() {
  local mode=$1 release run_id work
  shift
  check_arguments "$mode" "$@"
  [ "$(id -u)" = 0 ] || die 'run this on the node as root'
  release=$(cd -- "$HERE/../.." && pwd -P)
  run_id=$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
  work="$STATE/$run_id"
  install -d -m 700 "$STATE" "$work"
  # A frozen copy: a later change to the checkout cannot reach a running unit.
  (cd -- "$release" && cp --parents -r -- infra/scripts/cloud_restore.sh infra/scripts/cloud_restore_manifests.py \
    infra/scripts/cloud_vault_digest.py infra/cellctl/src/cellctl "$work/")
  (cd -- "$work" && sha256sum infra/scripts/cloud_restore.sh infra/scripts/cloud_restore_manifests.py \
    infra/scripts/cloud_vault_digest.py infra/cellctl/src/cellctl/*.py) > "$work/release.sha256"
  local limits=(--property=RuntimeMaxSec=3000 --property=TimeoutStopSec=1200)
  # A restore's worst case: about 40 minutes of scratch work, then 70 of
  # downtime. Stopping it runs the recovery, which may start the cell again.
  [ "$mode" = drill ] || limits=(--property=RuntimeMaxSec=9000 --property=TimeoutStopSec=4200)
  systemd-run --unit="exomem-restore-$run_id" --collect "${limits[@]}" \
    --property=CPUWeight=50 --property=IOWeight=50 \
    --property=StandardOutput="file:$work/run.log" --property=StandardError="file:$work/run.log" \
    bash "$work/infra/scripts/cloud_restore.sh" run "$mode" "$run_id" "$@"
  echo "run_id=$run_id; follow with: $0 follow $run_id"
}

follow() {
  [[ "${1:-}" =~ ^[0-9a-f]{8}$ ]] || die 'follow takes a run ID'
  systemctl show -p ActiveState,Result,ExecMainStatus "exomem-restore-$1.service"
  tail -n 40 "$STATE/$1/run.log"
}

# --- break-glass (cloud-operator-access.md): one hour, in memory, minted again
# whenever less than 20 minutes remain, because a restore can outlast one hour.
# A failed step returns at once or leaves a kubeconfig the final check refuses.
mint_break_glass() {
  local csr cert not_after server kc="$BG_DIR/kubeconfig"
  rm -rf -- "$BG_DIR"
  mkdir -m 700 "$BG_DIR" "$BG_DIR/bin" || return 1
  csr="exomem-break-glass-$(date -u +%Y%m%dt%H%M%S)"
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$BG_DIR/key.pem" 2>/dev/null || return 1
  openssl req -new -key "$BG_DIR/key.pem" -subj "/O=exomem:break-glass/CN=exomem-break-glass" \
    -out "$BG_DIR/csr.pem" || return 1
  admin apply -f - >/dev/null <<CSR || return 1
apiVersion: certificates.k8s.io/v1
kind: CertificateSigningRequest
metadata:
  name: ${csr}
spec:
  request: $(base64 -w0 < "$BG_DIR/csr.pem")
  signerName: kubernetes.io/kube-apiserver-client
  expirationSeconds: 3600
  usages: [digital signature, client auth]
CSR
  admin certificate approve "$csr" >/dev/null || return 1
  cert=""
  for _ in $(seq 30); do
    cert=$(admin get csr "$csr" -o jsonpath='{.status.certificate}') && [ -n "$cert" ] && break
    sleep 1
  done
  [ -n "$cert" ] || return 1
  printf '%s' "$cert" | base64 -d > "$BG_DIR/cert.pem"
  # The signer must have honoured expirationSeconds, never its default year.
  not_after=$(date -u -d "$(openssl x509 -in "$BG_DIR/cert.pem" -noout -enddate | cut -d= -f2)" +%s) || return 1
  [ "$not_after" -le $(($(date -u +%s) + 3660)) ] || return 1
  admin config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d > "$BG_DIR/ca.pem"
  server=$(admin config view --raw -o jsonpath='{.clusters[0].cluster.server}') || return 1
  "$K3S" kubectl --kubeconfig "$kc" config set-cluster exomem --server="$server" \
    --certificate-authority="$BG_DIR/ca.pem" --embed-certs=true >/dev/null
  "$K3S" kubectl --kubeconfig "$kc" config set-credentials exomem-break-glass \
    --client-certificate="$BG_DIR/cert.pem" --client-key="$BG_DIR/key.pem" --embed-certs=true >/dev/null
  "$K3S" kubectl --kubeconfig "$kc" config set-context break-glass --cluster=exomem --user=exomem-break-glass >/dev/null
  rm -f -- "$BG_DIR/key.pem" "$BG_DIR/csr.pem" "$BG_DIR/cert.pem" "$BG_DIR/ca.pem"
  # The export runbook's shim: the manifests module calls `kubectl --context break-glass`.
  printf '#!/bin/sh\nexec %s kubectl "$@"\n' "$K3S" > "$BG_DIR/bin/kubectl"
  chmod 700 "$BG_DIR/bin/kubectl"
  k auth whoami | grep -qF 'exomem:break-glass' || return 1
  BG_EXPIRES=$not_after
  echo "break-glass csr=$csr"
}

fresh_break_glass() {
  [ $((BG_EXPIRES - $(date -u +%s))) -gt 1200 ] || mint_break_glass
}

check_window() {
  local hour
  hour=$(date -u +%H)
  if [ "$hour" -ge 2 ] && [ "$hour" -lt 5 ]; then die 'inside the 02:00-05:00 UTC backup window; stop'; fi
}

# The restore pods share the node with live cells; refuse without headroom.
check_memory() {
  local available
  available=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
  [ "$available" -ge $((2 * 1024 * 1024)) ] || die 'less than 2 GiB available on the node; stop'
}

# wait_empty COMMAND...: until COMMAND succeeds with no output, for up to five
# minutes. An API error counts as "not yet", since the cluster converges.
wait_empty() {
  local out tries=0
  while [ "$tries" -lt 60 ]; do
    if out=$("$@") && [ -z "$out" ]; then return 0; fi
    tries=$((tries + 1))
    sleep 5
  done
  return 1
}

# Poll rather than `kubectl wait`, so a failure's reason is read before the
# Job's 300 s TTL removes it. 196 polls of 5 s outlast its 900 s deadline.
wait_job() {
  local namespace=$1 job=$2 state="" reason pod_reason
  for _ in $(seq 196); do
    state=$(k -n "$namespace" get job "$job" -o jsonpath='{.status.succeeded}/{.status.failed}') || state=""
    case "$state" in 1/*) return 0 ;; */1) break ;; esac
    sleep 5
  done
  reason=$(k -n "$namespace" get job "$job" -o jsonpath='{.status.conditions[*].reason}' 2>/dev/null) || reason=""
  pod_reason=$(k -n "$namespace" get pods -l job-name="$job" \
    -o jsonpath='{.items[*].status.containerStatuses[*].state.terminated.reason}' 2>/dev/null) || pod_reason=""
  echo "!! restore Job did not succeed (succeeded/failed=$state) job=$reason pod=$pod_reason" >&2
  return 1
}

# The root of the running cell runtime's filesystem, as the node sees it.
runtime_root() {
  local cid pid
  cid=$(k -n "$NS" get pod cell-0 -o jsonpath='{.status.containerStatuses[?(@.name=="exomem")].containerID}') || return 1
  [ -n "$cid" ] || return 1
  pid=$("$K3S" crictl inspect "${cid#*://}" | py -c 'import json, sys; print(json.load(sys.stdin)["info"]["pid"])') || return 1
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] && [ -d "/proc/$pid/root/data" ] || return 1
  printf '%s' "/proc/$pid/root/data"
}

# --- scratch restore, shared by drill and restore -------------------------

scratch_restore() {
  local started job
  echo "== restore the snapshot into $SCRATCH"
  started=$(date -u +%s)
  py "$MANIFESTS" scratch --context break-glass --cell-id "$CELL_ID" --snapshot "$SNAPSHOT" \
    --scratch "$SCRATCH" --field-manager "$FIELD_MANAGER"
  job=$(k -n "$SCRATCH" get jobs -o jsonpath='{.items[*].metadata.name}')
  [[ "$job" =~ ^[a-z0-9-]+$ ]] || die "expected one restore Job in $SCRATCH"
  k -n "$SCRATCH" wait --for=jsonpath='{.status.phase}'=Bound pvc/cell-data --timeout=300s >/dev/null
  SCRATCH_PV=$(k -n "$SCRATCH" get pvc cell-data -o jsonpath='{.spec.volumeName}')
  wait_job "$SCRATCH" "$job"
  echo "restore completed in $(($(date -u +%s) - started)) s (job deadline 900 s)"
  IMAGE=$(k -n "$SCRATCH" get job "$job" -o jsonpath='{.spec.template.spec.containers[0].image}')
  [[ "$IMAGE" =~ @sha256:[a-f0-9]{64}$ ]] || die 'the restore image is not pinned by digest'

  echo "== hash the restored vault"
  k apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: restore-verify
  namespace: ${SCRATCH}
  labels: {app.kubernetes.io/name: cloud-restore-verify}
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
    - name: verify
      image: ${IMAGE}
      imagePullPolicy: IfNotPresent
      command: [python3, -c, 'import time; time.sleep(3600)']
      resources:
        requests: {cpu: 100m, memory: 128Mi}
        limits: {cpu: "1", memory: 512Mi}
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
  k -n "$SCRATCH" wait --for=condition=Ready pod/restore-verify --timeout=180s >/dev/null
  fresh_break_glass
  k -n "$SCRATCH" exec restore-verify -- python3 -I -c "$DIGEST_SOURCE" hash /data/vault \
    < /dev/null > "$MEM/snapshot.hashes" || die 'hashing the restored vault failed'
  echo "snapshot vault files=$(wc -l < "$MEM/snapshot.hashes")"
  RESTORED_BYTES=$(k -n "$SCRATCH" exec restore-verify -- df -B1 --output=used /data < /dev/null | tail -n 1 | tr -d ' ')
  [[ "$RESTORED_BYTES" =~ ^[0-9]+$ ]] || die 'could not measure the restored size'
  echo "restored bytes=$RESTORED_BYTES"
}

# Deletes only this run's namespace (its name, the scratch label for this cell
# and no cell label), through the node's admin kubeconfig, so an expired
# break-glass certificate cannot block it. It fails closed when it cannot
# verify that the namespace and its volume are gone.
cleanup_scratch() {
  local ns label owner
  [ -n "$SCRATCH" ] && [ "$SCRATCH_CLEAN" != yes ] || return 0
  SCRATCH_CLEAN=no
  if ! ns=$(admin get namespace "$SCRATCH" --ignore-not-found -o name); then
    echo "!! cannot verify whether $SCRATCH exists" >&2
    return 1
  fi
  if [ -n "$ns" ]; then
    label=$(admin get namespace "$SCRATCH" -o jsonpath='{.metadata.labels.exomem\.io/scratch-of}') || label=""
    owner=$(admin get namespace "$SCRATCH" -o jsonpath='{.metadata.labels.exomem\.io/cloud-cell}') || owner="?"
    if [ -z "$SCRATCH_PV" ]; then
      SCRATCH_PV=$(admin -n "$SCRATCH" get pvc cell-data -o jsonpath='{.spec.volumeName}' 2>/dev/null) || SCRATCH_PV=""
    fi
    if [ "$label" != "$CELL_ID" ] || [ -n "$owner" ]; then
      echo "!! $SCRATCH does not carry this run's scratch identity; left in place, inspect it" >&2
      return 1
    fi
    admin delete namespace "$SCRATCH" --wait=true --timeout=600s >/dev/null || true
    if ! ns=$(admin get namespace "$SCRATCH" --ignore-not-found -o name) || [ -n "$ns" ]; then
      echo "!! scratch namespace $SCRATCH still present or unverifiable; delete it by hand" >&2
      return 1
    fi
  fi
  if [ -n "$SCRATCH_PV" ] && ! admin wait --for=delete "pv/$SCRATCH_PV" --timeout=300s >/dev/null 2>&1; then
    if ! ns=$(admin get pv "$SCRATCH_PV" --ignore-not-found -o name) || [ -n "$ns" ]; then
      echo "!! volume $SCRATCH_PV still present or unverifiable; delete it by hand" >&2
      return 1
    fi
  fi
  SCRATCH_CLEAN=yes
  echo "scratch namespace and volume removed"
}

# --- drill -----------------------------------------------------------------

run_drill() {
  local reference=$1 ref_path root
  case "$reference" in
    live) ref_path=vault ;;
    *) ref_path=".restore-prior-${reference#prior:}/vault" ;;
  esac
  check_window
  check_memory
  mint_break_glass
  # The reference is read through the live runtime's root, never through a
  # new pod on the cell's volume. File reads avoid atime updates.
  root=$(runtime_root) && [ -d "$root/$ref_path" ] || die 'reference vault not found through the live runtime'
  scratch_restore
  # Resolved again: the restore can take many minutes.
  root=$(runtime_root) && [ -d "$root/$ref_path" ] || die 'reference vault vanished before hashing'
  py "$DIGEST" hash "$root/$ref_path" > "$MEM/reference.hashes" || die 'hashing the reference vault failed'
  echo "== compare"
  py "$DIGEST" compare "$MEM/snapshot.hashes" "$MEM/reference.hashes" "$MEM"
  VERDICT=pass
  echo "drill PASS"
}

# --- in-place restore ------------------------------------------------------

helper() { k -n "$NS" exec "$HELPER" -- "$@" < /dev/null; }
delete_helper() { k -n "$NS" delete pod "$HELPER" --ignore-not-found --wait=true >/dev/null; }

start_helper() {
  k apply -f - >/dev/null <<EOF || return 1
apiVersion: v1
kind: Pod
metadata:
  name: ${HELPER}
  namespace: ${NS}
  labels: {app.kubernetes.io/name: cloud-restore-helper}
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
    - name: helper
      image: ${IMAGE}
      imagePullPolicy: IfNotPresent
      command: [python3, -c, 'import time; time.sleep(3600)']
      resources:
        requests: {cpu: 100m, memory: 256Mi}
        limits: {cpu: "1", memory: 1Gi}
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: {drop: [ALL]}
      volumeMounts:
        - {name: data, mountPath: /data}
  volumes:
    - name: data
      persistentVolumeClaim: {claimName: cell-data}
EOF
  k -n "$NS" wait --for=condition=Ready "pod/$HELPER" --timeout=180s >/dev/null
}

# Pods in the cell namespace that may still use the volume: every pod not yet
# Succeeded or Failed (Kubernetes' terminal phases), except this run's helper.
running_pods() {
  k -n "$NS" get pods -o jsonpath='{range .items[*]}{.metadata.name} {.status.phase}{"\n"}{end}' \
    | awk -v helper="$HELPER" '$1 != helper && $2 != "Succeeded" && $2 != "Failed" {print $1}'
}

only_helper_runs() {
  local pods
  pods=$(running_pods) || return 1
  [ -z "$pods" ]
}

cellctl_pods() {
  local selector
  # shellcheck disable=SC2016 # a Go template, not a shell expansion
  selector=$(k -n exomem-cloud get deployment cellctl \
    -o go-template='{{range $k, $v := .spec.selector.matchLabels}}{{$k}}={{$v}},{{end}}') || return 1
  [ -n "$selector" ] || return 1
  k -n exomem-cloud get pods -l "${selector%,}" -o name
}

# wait_ready MINUTES: rollout status in 5-minute rounds, so break-glass can be
# minted again between them.
wait_ready() {
  local round
  for ((round = 0; round < $1 / 5; round++)); do
    fresh_break_glass || return 1
    k -n "$NS" rollout status statefulset/cell --timeout=300s >/dev/null && return 0
  done
  return 1
}

resume_cellctl() {
  k -n exomem-cloud scale deployment cellctl --replicas="$CELLCTL_REPLICAS" >/dev/null \
    && k -n exomem-cloud rollout status deployment/cellctl --timeout=300s >/dev/null
}

# These run inside the helper pod. $1 is the run ID; the rest are cellctl's
# backed-up paths, which are exactly what its restore Job rewrites.
# shellcheck disable=SC2016 # expanded by sh in the pod, not here
MOVE_ASIDE='
  prior=/data/.restore-prior-$1
  shift
  [ ! -e "$prior" ] || { echo "$prior exists; nothing moved" >&2; exit 1; }
  mkdir -m 700 "$prior"
  for path in "$@"; do
    if [ -e "$path" ]; then mv "$path" "$prior/${path##*/}"; fi
  done'
# shellcheck disable=SC2016 # expanded by sh in the pod, not here
ROLL_BACK='
  prior=/data/.restore-prior-$1
  shift
  [ -d "$prior" ] || { echo "no prior directory; nothing was moved" >&2; exit 0; }
  for path in "$@"; do
    if [ -e "$prior/${path##*/}" ]; then
      rm -rf -- "$path"
      mv "$prior/${path##*/}" "$path"
    fi
  done
  rmdir "$prior" || echo "$prior is not empty; inspect it" >&2'

run_restore() {
  local root available paths path
  check_window
  check_memory
  mint_break_glass
  paths=$(py "$MANIFESTS" backup-paths)
  read -r -a BACKUP_PATHS <<< "$paths"
  for path in "${BACKUP_PATHS[@]}"; do [[ "$path" =~ ^/data/[a-z]+$ ]] || die "unexpected backup path $path"; done
  [ "${#BACKUP_PATHS[@]}" -gt 0 ] || die 'cellctl names no backup paths'

  # 1. Restore and hash the snapshot beside the cell, so a snapshot that cannot
  # be restored is refused before any downtime.
  scratch_restore
  [ -s "$MEM/snapshot.hashes" ] || die 'the snapshot holds an empty vault; stop'
  cleanup_scratch || die 'the scratch namespace could not be removed; stop before any downtime'
  if root=$(runtime_root); then
    available=$(df -B1 --output=avail -- "$root" | tail -n 1 | tr -d ' ')
    [[ "$available" =~ ^[0-9]+$ ]] && [ "$available" -gt $((RESTORED_BYTES + MARGIN_BYTES)) ] \
      || die "free bytes=$available; the prior and restored contents need $RESTORED_BYTES more plus 2 GiB"
  else
    echo 'no running runtime; the free space is checked after the stop'
  fi

  # 2. Stop the cell. Paused, cellctl cannot start a backup, upgrade or hold,
  # or bring the runtime back mid-restore.
  echo "== stop"
  check_window
  check_memory
  fresh_break_glass
  CELLCTL_REPLICAS=$(k -n exomem-cloud get deployment cellctl -o jsonpath='{.spec.replicas}')
  [[ "$CELLCTL_REPLICAS" =~ ^[1-9][0-9]*$ ]] || die 'cellctl is already paused; another procedure may own the pause'
  [ "$(k -n "$NS" get statefulset cell -o jsonpath='{.spec.replicas}')" = 1 ] || die 'the cell is not scaled to one'
  py "$MANIFESTS" idle --context break-glass --cell-id "$CELL_ID"
  PHASE=stopping
  k -n exomem-cloud scale deployment cellctl --replicas=0 >/dev/null
  wait_empty cellctl_pods || die 'cellctl pods are still running'
  py "$MANIFESTS" idle --context break-glass --cell-id "$CELL_ID"
  k -n "$NS" scale statefulset cell --replicas=0 >/dev/null
  wait_empty running_pods || die "a pod still runs in $NS"
  date -u +"stopped at %FT%TZ"

  # 3. Move the backed-up paths aside, after the space check.
  echo "== move aside"
  start_helper
  available=$(helper df -B1 --output=avail /data | tail -n 1 | tr -d ' ')
  [[ "$available" =~ ^[0-9]+$ ]] && [ "$available" -gt $((RESTORED_BYTES + MARGIN_BYTES)) ] \
    || die "free bytes=$available; the prior and restored contents need $RESTORED_BYTES more plus 2 GiB"
  only_helper_runs || die "a pod besides the helper runs in $NS"
  PHASE=moved
  helper sh -euc "$MOVE_ASIDE" move "$RUN_ID" "${BACKUP_PATHS[@]}"
  delete_helper

  # 4. Restore the snapshot onto the cell's own volume.
  echo "== restore in place"
  fresh_break_glass
  IN_PLACE_JOB=$(py "$MANIFESTS" in-place --context break-glass --cell-id "$CELL_ID" --snapshot "$SNAPSHOT" \
    --field-manager cloud-operator-restore)
  wait_job "$NS" "$IN_PLACE_JOB"

  # 5. The restored vault must equal what the scratch restore produced.
  echo "== verify"
  start_helper
  helper python3 -I -c "$DIGEST_SOURCE" hash /data/vault > "$MEM/in-place.hashes" \
    || die 'hashing the restored vault failed'
  delete_helper
  py "$DIGEST" compare "$MEM/in-place.hashes" "$MEM/snapshot.hashes" "$MEM"

  # 6. Start the cell, then resume cellctl. Once the cell serves, recovery
  # never rolls back.
  echo "== start"
  PHASE=starting
  k -n "$NS" scale statefulset cell --replicas=1 >/dev/null
  wait_ready 30 || die 'the cell did not become Ready'
  PHASE=served
  date -u +"serving again at %FT%TZ"
  resume_cellctl || die 'cellctl did not come back'
  PHASE=finished
  VERDICT=pass
  echo "restore PASS; the prior contents stay in /data/.restore-prior-$RUN_ID"
}

# 7. Recovery by phase. Before the move it brings the cell and cellctl back.
# After it, it rolls the prior contents back first. If the volume cannot be
# reached alone, it leaves the cell stopped and cellctl paused, because a
# partly restored vault must not serve.
recover() {
  local ok=0
  case "$PHASE" in - | checks | finished) return 0 ;; esac
  echo "!! the restore failed during $PHASE; recovering" >&2
  if ! fresh_break_glass; then
    echo "!! break-glass re-mint failed; recover by hand (cloud-operator-restore.md, 'Recover by hand')" >&2
    return 1
  fi
  if [ "$PHASE" = served ]; then
    resume_cellctl || { echo "!! cellctl did not come back; scale it to $CELLCTL_REPLICAS by hand" >&2; ok=1; }
    return "$ok"
  fi
  if [ "$PHASE" = moved ] || [ "$PHASE" = starting ]; then
    k -n "$NS" scale statefulset cell --replicas=0 >/dev/null
    # A stop during the restore leaves its Job writing to the volume.
    if [ -n "$IN_PLACE_JOB" ]; then
      k -n "$NS" delete job "$IN_PLACE_JOB" --ignore-not-found --cascade=foreground --wait=true >/dev/null
    fi
    if wait_empty running_pods && { k -n "$NS" get pod "$HELPER" >/dev/null 2>&1 || start_helper; } \
      && only_helper_runs && helper sh -euc "$ROLL_BACK" rollback "$RUN_ID" "${BACKUP_PATHS[@]}"; then
      echo "rolled back to the prior contents" >&2
    else
      echo "!! rollback did not run; the cell stays stopped and cellctl paused." >&2
      echo "!! The prior contents stay in /data/.restore-prior-$RUN_ID; recover by hand." >&2
      return 1
    fi
  fi
  delete_helper || ok=1
  # The prior contents are back, so cellctl may resume before the cell is
  # Ready; that keeps the pause, which stops every cell's reconcile, short.
  k -n "$NS" scale statefulset cell --replicas=1 >/dev/null || ok=1
  resume_cellctl || { echo "!! cellctl did not come back; scale it to $CELLCTL_REPLICAS by hand" >&2; ok=1; }
  wait_ready 15 || { echo "!! the cell did not become Ready; inspect it" >&2; ok=1; }
  return "$ok"
}

on_exit() {
  local rc=$?
  set +e
  trap - EXIT INT TERM
  if [ "$MODE" = restore ]; then recover || rc=1; fi
  cleanup_scratch || rc=1
  rm -rf -- "$BG_DIR"
  if [ "$rc" -eq 0 ] && [ "$VERDICT" != pass ]; then rc=1; fi
  if [ "$rc" -eq 0 ]; then rm -rf -- "$MEM"; fi
  echo "run=$RUN_ID mode=$MODE phase=$PHASE verdict=$VERDICT scratch_cleanup=$SCRATCH_CLEAN rc=$rc"
  exit "$rc"
}

run() {
  MODE=$1 RUN_ID=$2
  shift 2
  [[ "$RUN_ID" =~ ^[0-9a-f]{8}$ ]] || die 'bad run ID'
  check_arguments "$MODE" "$@"
  CELL_ID=$1 SNAPSHOT=$2
  NS="exo-cell-$CELL_ID"
  SCRATCH="exo-scratch-$CELL_ID-$RUN_ID"
  SCRATCH_PV="" SCRATCH_CLEAN=no PHASE=checks VERDICT=fail BG_EXPIRES=0 IMAGE="" RESTORED_BYTES=0
  CELLCTL_REPLICAS="" BACKUP_PATHS=() IN_PLACE_JOB=""
  MEM="/dev/shm/exomem-restore-$RUN_ID"
  BG_DIR="/dev/shm/exomem-break-glass-$RUN_ID"
  DIGEST_SOURCE=$(cat -- "$DIGEST")
  echo "run=$RUN_ID mode=$MODE started at $(date -u +%FT%TZ)"
  sha256sum "$HERE/cloud_restore.sh" "$MANIFESTS" "$DIGEST" "$HERE/../cellctl/src/cellctl/manifests.py"
  mkdir -m 700 "$MEM"
  export KUBECONFIG="$BG_DIR/kubeconfig" PATH="$BG_DIR/bin:$PATH"
  trap on_exit EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  if [ "$MODE" = drill ]; then
    FIELD_MANAGER=cloud-restore-drill PHASE=-
    run_drill "$3"
  else
    FIELD_MANAGER=cloud-operator-restore
    run_restore
  fi
}

case "${1:-}" in
  drill | restore) launch "$@" ;;
  follow) follow "${2:-}" ;;
  run) shift && run "$@" ;;
  *) die 'usage: cloud_restore.sh drill CELL_ID SNAPSHOT REFERENCE | restore CELL_ID SNAPSHOT | follow RUN_ID' ;;
esac
