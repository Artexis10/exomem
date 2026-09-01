<!-- authority:non-specification -->

# Secret handoff and rotation

This runbook is the only supported path from Terraform, an operator prompt, or
a pipe into Vercel and the static K3s Secret set. The command validates the
versioned destination matrix before reading a value. Values never appear in
arguments or successful output; provider CLI output is captured and discarded.

## Preconditions

- Work from a clean infrastructure checkout with `terraform`, `sops`, `age`,
  `kubectl`, and the Vercel CLI at the pinned versions.
- Keep shell tracing disabled: `set +x`.
- Set `SOPS_AGE_RECIPIENTS` to the off-node operator/escrow recipients. Age
  recipients are public; private keys remain offline.
- Link the Substrate checkout to the correct Vercel project before a Vercel
  destination is used. The handoff reads `.vercel/project.json` and requires
  the exact organization, project ID, and project name recorded in the matrix
  before it reads the secret source.
- Use a monotonically increasing `vN` at each destination. Never reuse a
  destination version after a partial or failed handoff.

Set local paths once:

```bash
repo_root="$(git rev-parse --show-toplevel)"
matrix="$repo_root/infra/contracts/secret-destinations-v1.json"
substrate_root=/absolute/path/to/substrate
```

Validate any route without reading stdin or contacting a provider:

```bash
infra/scripts/secret_handoff.py \
  --matrix "$matrix" \
  --repository-root "$repo_root" \
  --secret hosted_scheduler_secret \
  --version v1 \
  --destination k3s.scheduler.active \
  --source stdin \
  --dry-run
```

## Terraform-owned credentials

Terraform output is captured in memory with `terraform output -raw`. The
Cloudflare Tunnel token has exactly one destination:

```bash
infra/scripts/secret_handoff.py \
  --matrix "$matrix" \
  --repository-root "$repo_root" \
  --secret cloudflare_tunnel_token \
  --version v1 \
  --destination k3s.cloudflared.active \
  --source terraform
```

The Cloudflare Access client ID and secret go only to Substrate/Vercel:

```bash
infra/scripts/secret_handoff.py \
  --matrix "$matrix" \
  --repository-root "$repo_root" \
  --secret cloudflare_access_client_id \
  --version v1 \
  --destination vercel.substrate.production.access.active.client-id \
  --source terraform \
  --vercel-project "$substrate_root"

infra/scripts/secret_handoff.py \
  --matrix "$matrix" \
  --repository-root "$repo_root" \
  --secret cloudflare_access_client_secret \
  --version v1 \
  --destination vercel.substrate.production.access.active.client-secret \
  --source terraform \
  --vercel-project "$substrate_root"
```

Every Vercel attempt first creates an immutable content-free reservation under
`infra/secrets/receipts/vercel/`. A successful CLI write replaces that evidence
with a `.receipt.json` whose fields bind the secret name/version, destination,
slot, variable, environment, and exact Vercel project identity. A
`.receipt.pending.json` means the write was not confirmed. Receipts contain no
secret value or secret digest. Retain them with release evidence; they are one
input to later retirement proof, alongside live acceptance/rejection checks.
The command rejects an existing or lower version for that destination and
holds a per-destination lock through the provider write and receipt finalization,
so concurrent processes cannot regress the mutable Vercel value.

Version numbers are destination-scoped; they do not claim that equal numbers at
different destinations contain equal plaintext. Step 1 of scheduler rotation
may therefore write the K3s `v1` value to a previously unused Vercel-previous
`v1` slot, but only through the shown direct pipe. A single multi-destination
handoff reads once and therefore does guarantee the same value for that command.

Use the matrix's exact durability output/destination pair for each B2 key.
Recovery and user-export upload, restore, and delete identities are separate on
purpose; database backup has upload and restore identities only. There is no
database-backup delete output or K3s Secret. Never combine or substitute them.

## Generated shared credentials

Create the capacity receipt keypair atomically; never copy its private seed into
a worker Secret. The matrix sends the private half only to
`exomem-capacity-receipt-signer/private-key`, sends the public half to
`exomem-capacity-receipt-verifier/public-key` for the routine and
volume-registration workers, and retains a separately encrypted public escrow
copy for operator verification:

```bash
SOPS_AGE_RECIPIENTS=age1... \
  infra/scripts/provider_recovery_keypair_handoff.py \
  --matrix "$matrix" \
  --repository-root "$repo_root" \
  --version v1 \
  --pair capacity-receipt
```

Include both K3s ciphertext destinations in the signed active-secret registry.
The receipt public key is unpadded base64url Ed25519 material; it is not secret,
but its exact destination and trust-root binding are governed. The collector's
HCloud read token and signing seed must remain absent from both worker
Deployments. The privileged volume worker's separate HCloud mutation token does
not authorize receipt signing.

Read once and deliver the initial hosted-scheduler bearer to both named peers:

```bash
openssl rand -base64 48 | infra/scripts/secret_handoff.py \
  --matrix "$matrix" \
  --repository-root "$repo_root" \
  --secret hosted_scheduler_secret \
  --version v1 \
  --destination k3s.scheduler.active \
  --destination vercel.substrate.production.scheduler.active \
  --source stdin \
  --vercel-project "$substrate_root"
```

Caller order does not control mutation order: the command reserves all remote
receipts, encrypts, decrypt-verifies, and durably publishes every local SOPS
target first, then performs Vercel writes. Existing `vN` SOPS targets are never
overwritten, and every ciphertext is checked for the expected destination shape
and exact in-memory round trip before publication.

Generate the provisioner bearer the same way, using
`vercel.substrate.production.provisioner.active` and
`k3s.provisioner.active`. The global `CRON_SECRET` has only the
`vercel.substrate.production.global-cron.active` destination. The matrix has no
K3s route for it. Dynamic cell credentials have no static handoff route at all.

### Scheduler alert receiver capability

The scheduler alert sender is a fixed contract that carries no `Authorization`
header, so its capability has to travel inside `ALERT_WEBHOOK_URL`. That splits
the secret across two matrix entries, and each destination receives only what it
needs: a leak of either side alone must not yield a forgeable alert.

Generate one token, then hand off the URL and its digest separately. Derive the
digest locally; never send the token itself to Vercel:

```bash
token="$(openssl rand -hex 32)"
printf '%s' "https://substratesystems.io/api/exomem/alerts/${token}" \
  | infra/scripts/secret_handoff.py \
    --matrix "$matrix" \
    --repository-root "$repo_root" \
    --secret alert_delivery_webhook_url \
    --version v1 \
    --destination k3s.alert-delivery.active \
    --source stdin
```

```bash
printf '%s' "$token" | sha256sum | cut -d' ' -f1 \
  | infra/scripts/secret_handoff.py \
    --matrix "$matrix" \
    --repository-root "$repo_root" \
    --secret alert_receiver_token_digest \
    --version v1 \
    --destination vercel.substrate.production.alert-receiver.active \
    --source stdin \
    --vercel-project "$substrate_root"
```

Clear `token` from the shell afterwards.

**Accepted residual.** The capability is a URL path segment, so it appears in
Vercel's own request logs for every legitimate delivery, at whatever retention
the plan carries, to anyone with project log access. Storing only the digest
keeps it out of the receiver's environment and out of the application's event
stream, and both matrix destinations are separated — but neither control covers
the platform request log, which holds the forgeable form. Treat project log
access as equivalent to holding the capability, and rotate if that access
changes hands. The sender contract permits no `Authorization` header, so there
is no variant of this design that avoids the exposure.

Rotation uses a two-version receiver overlap against the single-version sender,
exactly as the hosted-scheduler bearer does. The sender URL can only carry one
capability, so the receiver is the side that overlaps:

1. Copy the current digest into
   `vercel.substrate.production.alert-receiver.previous`.
2. Publish the new digest to
   `vercel.substrate.production.alert-receiver.active`. Both capabilities are
   now accepted, so no transition is lost.
3. Hand the new URL to `k3s.alert-delivery.active` under a new version and let
   the evaluator pick it up.
4. Prove the new capability is accepted and confirm a delivered transition.
5. Only then clear the previous digest, and prove the retired capability now
   answers `404`.

Retire the previous slot only after that acceptance proof. A malformed value in
the previous slot is ignored rather than widening the accepted set.

Generate the root wrapping key once and seal the same version for both the
provisioner workload and offline escrow:

```bash
openssl rand -base64 48 | infra/scripts/secret_handoff.py \
  --matrix "$matrix" \
  --repository-root "$repo_root" \
  --secret provisioner_wrapping_key \
  --version v1 \
  --destination k3s.provisioner.wrapping-key.active \
  --destination escrow.provisioner-wrapping-key.active \
  --source stdin
```

The provisioner database URL and its separately scoped HCloud token have only
provisioner-workload destinations. K3s bootstrap material is different again:
`k3s_server_token` is written once to both its exact SOPS Ansible-var destination
and its separately versioned offline escrow destination. It is never installed
as a general cluster Secret:

```bash
openssl rand -base64 48 | infra/scripts/secret_handoff.py \
  --matrix "$matrix" \
  --repository-root "$repo_root" \
  --secret k3s_server_token \
  --version v1 \
  --destination ansible.hosted-node.k3s-server-token.active \
  --destination escrow.k3s-server-token.active \
  --source stdin
```

The database-backup B2 key also has an exact SOPS Ansible-var destination. None
of these host-bootstrap values becomes a general cluster Secret.

## Ephemeral provisioner database bootstrap authority

The destination matrix contains only the dedicated runtime database URL. It
deliberately has no admin URL destination. An admin URL may exist in K3s only as
`exomem-provisioner-database-bootstrap-admin` for the one-shot bootstrap Job in
the deployment runbook. It must be read through a non-printing prompt, FIFO, or
provider helper, streamed to `kubectl` over stdin, and removed on both success
and failure. Stable hooks, Deployments, CronJobs, SOPS artifacts, receipts, and
the active-secret registry must never contain or reference it.

After every bootstrap attempt, verify the Job and Secret are absent, then rotate
or revoke the provider-side admin credential before Helm may continue. Retain a
content-free provider receipt out of band and set its path as
`EXOMEM_DATABASE_ADMIN_ROTATION_RECEIPT` for the deployment gate. Set a stable,
private path outside the ephemeral deploy workspace as
`EXOMEM_DATABASE_BOOTSTRAP_ATTEMPT_STATE`; it binds a failed, timed-out, or
interrupted attempt to the exact receipt required before another attempt.
Repository
automation cannot perform this provider mutation, so an absent receipt blocks a
live install. A crash that leaves either ephemeral resource behind is not a
retry signal: delete it, rotate/revoke the exposed admin credential, obtain a
new one-use URL, and start the whole bootstrap boundary again.

The runtime and admin URLs must be direct or backed by a reviewed
session-affinity guarantee. For Neon, use the direct
`postgresql+asyncpg://ROLE:PASSWORD@ep-<endpoint-id>.<region>.aws.neon.tech/DATABASE?ssl=require`
shape; the `ep-<endpoint-id>-pooler...neon.tech` transaction pool is refused.
For a separately reviewed session-mode proxy, append the local
`pool_mode=session` contract marker; that marker never converts a transaction
pool into a supported endpoint.

## Run Ansible with SOPS vars on tmpfs

Keep the non-secret generated host variables in the normal ignored
`group_vars/hosted_nodes.yml`. Pass the three encrypted bootstrap values through
the executable wrapper; it refuses a non-tmpfs workspace, writes mode `0600`
plaintext only inside a private tmpfs directory, and removes it on exit:

```bash
export EXOMEM_SECRET_TMPFS_DIR="${XDG_RUNTIME_DIR:?XDG_RUNTIME_DIR is required}"
export SOPS_AGE_KEY_FILE=/secure/operator/exomem-hosted.agekey

infra/scripts/ansible_with_sops.sh \
  --inventory infra/ansible/inventory.yml \
  --vars infra/secrets/ansible/k3s-server-token.v1.sops.json \
  --vars infra/secrets/ansible/etcd-s3-access-key.v1.sops.json \
  --vars infra/secrets/ansible/etcd-s3-secret-key.v1.sops.json
```

The wrapper validates `tmpfs`/`ramfs` with `findmnt`, suppresses SOPS output,
and supplies each decrypted document as an Ansible extra-vars file. The K3s
role's secret assertions and configuration render use `no_log: true`. Do not
replace the wrapper with a regular `/tmp` decryption.

## Apply one SOPS artifact

Inspect only identity and version metadata before applying. Do not decrypt to a
regular file:

```bash
sops exec-file infra/secrets/platform/hosted-scheduler.v1.sops.json \
  'kubectl apply --server-side --field-manager=exomem-secret-handoff -f {}'

kubectl -n exomem-platform get secret exomem-hosted-scheduler \
  -o jsonpath='{.metadata.labels.exomem\.io/secret-version}{"\n"}'
```

The verification command intentionally reads no Secret data.

## Hosted-scheduler rotation

The Vercel receiver accepts at most active plus previous; K3s carries only the
active sender. Rotate without a cadence gap:

1. Copy the current K3s ciphertext value into the Vercel previous slot through
   a pipe, then redeploy Substrate. Prove the old K3s sender still receives 200
   from all three hosted scheduler routes.
2. Generate the new value once. In one handoff, replace the Vercel active slot
   and create the new-version K3s ciphertext, but do not apply the K3s artifact
   yet. Redeploy Substrate and prove both versions are accepted only by the
   three hosted routes; both must fail on global-cron routes.
3. Apply the new K3s artifact. Prove a scheduled success for all three jobs and
   no 180-second missed-run or two-failure alert.
4. Remove `EXOMEM_HOSTED_SCHEDULER_SECRET_PREVIOUS` from Vercel, redeploy, and
   prove the old value now returns 401 while the new sender succeeds. Do not
   change `CRON_SECRET` during this drill.

Example for step 2:

```bash
openssl rand -base64 48 | infra/scripts/secret_handoff.py \
  --matrix "$matrix" \
  --repository-root "$repo_root" \
  --secret hosted_scheduler_secret \
  --version v2 \
  --destination k3s.scheduler.active \
  --destination vercel.substrate.production.scheduler.active \
  --source stdin \
  --vercel-project "$substrate_root"
```

Step 1 can also stay entirely in pipes/FIFOs:

```bash
sops decrypt \
  --extract '["stringData"]["secret"]' \
  infra/secrets/platform/hosted-scheduler.v1.sops.json \
  | infra/scripts/secret_handoff.py \
      --matrix "$matrix" \
      --repository-root "$repo_root" \
      --secret hosted_scheduler_secret \
      --version v1 \
      --destination vercel.substrate.production.scheduler.previous \
      --source stdin \
      --vercel-project "$substrate_root"
```

## Partial-handoff recovery

The workflow is deliberately non-transactional across SOPS files and Vercel.
If any destination fails, keep the last proven receiver/sender pair and inspect
only ciphertext paths plus content-free receipts. A final receipt confirms that
the Vercel CLI accepted the write; a pending receipt is uncertain. Local SOPS
artifacts may already exist even when no Vercel call ran.

Never retry or overwrite an affected destination's partial `vN`. Preserve its
artifacts as evidence and choose a higher destination version. For a coordinated
recovery across several peers, use a number higher than every selected peer's
current version, generate or read the intended value again, and hand it to every
destination required for the recovered state. An unaffected destination keeps
its independent version sequence. Then redeploy and repeat acceptance,
old-version rejection, cross-route denial, and cadence checks. Never retire an
old value on receipt evidence alone.

## Signed active-secret registry

The registry signer is a release-custodian operation. It produces an immutable
registry/public-key pair for every one of the 34 active K3s destinations; it
does not decrypt an artifact or apply anything. Disable tracing and use the
pre-created private directory `/secure/operator/exomem-hosted/active-secret-registry/`.
It must be owned by the current operator, mode `0700`, and contain neither a
symlink nor mutable `latest` pointer. Do not use a temporary directory for the
pair.

```bash
set +x
registry_dir=/secure/operator/exomem-hosted/active-secret-registry/
test -d "$registry_dir" && test ! -L "$registry_dir"
test "$(stat -c %U "$registry_dir")" = "$(id -un)"
test "$(stat -c %a "$registry_dir")" = 700
pair_id="$(date -u +%Y%m%dT%H%M%SZ)-reviewed-change"
registry="$registry_dir/active-secret-registry-$pair_id.json"
public_key="$registry_dir/active-secret-registry-$pair_id.public.pem"
test ! -e "$registry" && test ! -e "$public_key"

BWS_PROJECT_ID=69843186-5161-40a2-951f-b487011122ce \
  bwsx-run EXOMEM_HOSTED_ACTIVE_SECRET_REGISTRY_SIGNING_KEY -- sh -c \
  'printf "%s\\n" "$EXOMEM_HOSTED_ACTIVE_SECRET_REGISTRY_SIGNING_KEY"' \
  | infra/scripts/sign_active_secret_registry.py \
      --matrix infra/contracts/secret-destinations-v1.json \
      --selection infra/contracts/active-secret-selection-v1.json \
      --trust-contract infra/contracts/active-secret-registry-v1.json \
      --private-key-stdin \
      --registry-output "$registry" \
      --public-key-output "$public_key"
```

The shell builtin `printf` is the only key transport in this command. The BWS
project is used only for that named item: do not enumerate its contents. Keep
the retained all-v1 pair when making a later v2 pair. Before any application,
verify the pair without a subprocess that can mutate K3s:

```bash
infra/scripts/apply_active_sops_secrets.py \
  --matrix infra/contracts/secret-destinations-v1.json \
  --registry "$registry" \
  --registry-public-key "$public_key" \
  --trust-contract infra/contracts/active-secret-registry-v1.json \
  --verify-only
```

## Future provisioner database rotation

This procedure is deliberately future-only. Do not start it until an
authenticated operation is `succeeded/bound` and its tenant/cell remains active
and ready. Preserve the old role password, v1 ciphertext, signed v1 registry
pair, exact controller snapshot, and the authenticated pre-change SQL session
until final health is proven.

Capture content-free controller state before stopping work; this snapshot is the
only restoration input and is retained with the rotation receipts:

```bash
set -euo pipefail
set +x
namespace=exomem-platform
rotation_root=/secure/operator/exomem-hosted/rotation-runs
rotation_run="$rotation_root/$(date -u +%Y%m%dT%H%M%SZ)-database-v2"
rotation_snapshot="$rotation_run/controller-snapshot.json"
rotation_phase=pre_password_cutover
rollback() {
  set +x
  if test "$rotation_phase" = post_password_cutover && ! test "${DATABASE_PASSWORD_ROLLBACK_VERIFIED:-}" = yes; then
    echo 'leave consumers stopped; perform the verified database-password rollback, set DATABASE_PASSWORD_ROLLBACK_VERIFIED=yes, then rerun recovery' >&2
    trap - ERR
    return 1
  fi
  if test -f "$rotation_snapshot"; then
    infra/scripts/apply_active_sops_secrets.py --matrix infra/contracts/secret-destinations-v1.json --registry "$registry_v1" --registry-public-key "$public_key_v1" --trust-contract infra/contracts/active-secret-registry-v1.json --verify-only
    infra/scripts/apply_active_sops_secrets.py --matrix infra/contracts/secret-destinations-v1.json --registry "$registry_v1" --registry-public-key "$public_key_v1" --trust-contract infra/contracts/active-secret-registry-v1.json
    jq -r '.deployments[] | [.name, .replicas] | @tsv' "$rotation_snapshot" | while IFS=$'\t' read -r name replicas; do kubectl -n exomem-platform scale deployment "$name" --replicas="$replicas"; done
    jq -r '.cronjobs[] | [.name, .suspend] | @tsv' "$rotation_snapshot" | while IFS=$'\t' read -r name suspend; do kubectl -n exomem-platform patch cronjob "$name" --type merge -p "{\"spec\":{\"suspend\":$suspend}}"; done
  fi
}
trap 'rollback' ERR
umask 077
install -d -m 0700 "$rotation_root"
mkdir -m 0700 "$rotation_run"
set -o noclobber
kubectl -n exomem-platform get deployment -o json | jq '[.items[] | select(.metadata.name | IN("exomem-provisioner-api","exomem-provisioner-worker","exomem-volume-worker")) | {kind:"Deployment",name:.metadata.name,replicas:(.spec.replicas // 1)}]' > "$rotation_run/deployments.json"
kubectl -n exomem-platform get cronjob -o json | jq --slurpfile deployments "$rotation_run/deployments.json" '{schema_version:1,deployments:$deployments[0],cronjobs:[.items[] | select(.metadata.name | IN("exomem-durability-actions","exomem-export-gc","exomem-durability-backup","exomem-database-backup","exomem-deletion-dispatcher","exomem-hosted-scheduler-exomem-reconcile")) | {name:.metadata.name,suspend:(.spec.suspend // false)}]}' > "$rotation_snapshot"
rm "$rotation_run/deployments.json"
set +o noclobber
jq -e '(.deployments | length == 3 and ([.deployments[].name] | sort) == ["exomem-provisioner-api","exomem-provisioner-worker","exomem-volume-worker"]) and (.cronjobs | length == 6 and ([.cronjobs[].name] | sort) == ["exomem-database-backup","exomem-deletion-dispatcher","exomem-durability-actions","exomem-durability-backup","exomem-export-gc","exomem-hosted-scheduler-exomem-reconcile"])' "$rotation_snapshot" >/dev/null
chmod 0600 "$rotation_snapshot"
for name in exomem-durability-actions exomem-export-gc exomem-durability-backup exomem-database-backup exomem-deletion-dispatcher exomem-hosted-scheduler-exomem-reconcile; do kubectl -n exomem-platform patch cronjob "$name" --type merge -p '{"spec":{"suspend":true}}'; done
for name in exomem-provisioner-api exomem-provisioner-worker exomem-volume-worker; do kubectl -n exomem-platform scale deployment "$name" --replicas=0; done
kubectl -n exomem-platform get jobs -o json
kubectl -n exomem-platform get jobs -o json | jq '[.items[] | select(any(.metadata.ownerReferences[]?; .controller == true and .kind == "CronJob" and (.name | IN("exomem-durability-actions","exomem-export-gc","exomem-durability-backup","exomem-database-backup","exomem-deletion-dispatcher")))) | {name:.metadata.name,uid:.metadata.uid}]' > "$rotation_run/cronjob-jobs.json"
kubectl -n exomem-platform get jobs -l exomem.io/deletion-job=true -o json | jq '[.items[] | {name:.metadata.name,uid:.metadata.uid}]' > "$rotation_run/deletion-jobs.json"
jq -r '.[].name' "$rotation_run/cronjob-jobs.json" | while read -r name; do kubectl -n exomem-platform delete job "$name" --wait=false; done
jq -r '.[].name' "$rotation_run/deletion-jobs.json" | while read -r name; do kubectl -n exomem-platform delete job "$name" --wait=false; done
drain_attempt=0
while :; do
  drain_attempt=$((drain_attempt + 1))
  test "$drain_attempt" -le 60 || { echo 'captured Jobs did not drain; leave consumers stopped' >&2; exit 1; }
  kubectl -n exomem-platform get jobs -o json > "$rotation_run/jobs-current.json"
  remaining_jobs="$(jq -r --slurpfile cron "$rotation_run/cronjob-jobs.json" --slurpfile deletion "$rotation_run/deletion-jobs.json" '[.items[].metadata.uid] as $live | [($cron[0][]?, $deletion[0][]?) | select(.uid as $uid | $live | index($uid))] | length' "$rotation_run/jobs-current.json")"
  test "$remaining_jobs" -eq 0 && break
  sleep 2
done
! kubectl -n exomem-platform get job exomem-provisioner-database-migration 2>/dev/null
! kubectl -n exomem-platform get job exomem-provisioner-database-bootstrap 2>/dev/null
! kubectl -n exomem-platform get secret exomem-provisioner-database-bootstrap-admin 2>/dev/null
kubectl -n exomem-platform get pods -o json | jq -e '[.items[] | select(.status.phase == "Pending" or .status.phase == "Running") | select([((.spec.initContainers // []) + (.spec.containers // []))[] | .env[]? | select(.valueFrom.secretKeyRef.name == "exomem-provisioner-database")] | length > 0)] | length == 0'
```

Apply the reviewed complete v2 registry with PostgreSQL still accepting v1;
the command reads no Secret data. Use an interactive, non-echoing provider SQL
session for the password change and authentication probes—never put either DSN
or password in argv or output. The old probe must fail specifically with
`password authentication failed`.

```bash
infra/scripts/apply_active_sops_secrets.py --matrix infra/contracts/secret-destinations-v1.json --registry "$registry_v2" --registry-public-key "$public_key_v2" --trust-contract infra/contracts/active-secret-registry-v1.json --verify-only
infra/scripts/apply_active_sops_secrets.py --matrix infra/contracts/secret-destinations-v1.json --registry "$registry_v2" --registry-public-key "$public_key_v2" --trust-contract infra/contracts/active-secret-registry-v1.json
# The governed applier invokes kubectl -n exomem-platform apply -f only after registry verification.
kubectl -n exomem-platform get secret exomem-provisioner-database -o jsonpath='{.metadata.labels.exomem\.io/secret-version}{"\n"}'
echo 'rotation SQL session remains open; consumers stay stopped until v2 acceptance and v1 password authentication failed proof are recorded' >&2
rotation_phase=post_password_cutover
: "${ROTATION_AUTH_PROOFS_RECORDED:?record v2 acceptance and v1 password-authentication rejection before restore}"
for name in exomem-durability-actions exomem-export-gc exomem-durability-backup exomem-database-backup exomem-deletion-dispatcher; do kubectl -n exomem-platform patch cronjob "$name" --type merge -p "{\"spec\":{\"jobTemplate\":{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"exomem.io/restarted-at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}}}}}}}"; done
jq -r '.deployments[] | [.name, .replicas] | @tsv' "$rotation_snapshot" | while IFS=$'\t' read -r name replicas; do kubectl -n exomem-platform scale deployment "$name" --replicas="$replicas"; done
jq -r '.cronjobs[] | select(.name != "exomem-hosted-scheduler-exomem-reconcile") | [.name, .suspend] | @tsv' "$rotation_snapshot" | while IFS=$'\t' read -r name suspend; do kubectl -n exomem-platform patch cronjob "$name" --type merge -p "{\"spec\":{\"suspend\":$suspend}}"; done
kubectl -n exomem-platform rollout status deployment/exomem-provisioner-api --timeout=180s
kubectl -n exomem-platform rollout status deployment/exomem-provisioner-worker --timeout=180s
kubectl -n exomem-platform rollout status deployment/exomem-volume-worker --timeout=180s
reconcile_suspend="$(jq -r '.cronjobs[] | select(.name == "exomem-hosted-scheduler-exomem-reconcile") | .suspend' "$rotation_snapshot")"
kubectl -n exomem-platform patch cronjob "exomem-hosted-scheduler-exomem-reconcile" --type merge -p "{\"spec\":{\"suspend\":$reconcile_suspend}}"
jq -r '.cronjobs[] | [.name, .suspend] | @tsv' "$rotation_snapshot" | while IFS=$'\t' read -r name suspend; do test "$(kubectl -n exomem-platform get cronjob "$name" -o jsonpath='{.spec.suspend}')" = "$suspend"; done
trap - ERR
```

1. Record `ready_cell_baseline_verified`: authenticate the existing ready cell
   and retain a content-free operation/cell health receipt. Block concurrent
   Helm work and snapshot the exact Deployment replicas, CronJob suspend states,
   and exact `exomem-hosted-scheduler-exomem-reconcile` suspend state.
2. Only after that baseline, generate the v2 password and
   `database-url.v2.sops.json`. Review, merge, and verify its ciphertext plus
   the one-line selection promotion. Produce and retain immutable signed all-v1
   and v2 registry/public-key pairs, proving the other 33 entries are unchanged.
3. Record `three_deployments_and_five_cronjobs_quiesced`: preserve and suspend
   `exomem-durability-actions`, `exomem-export-gc`,
   `exomem-durability-backup`, `exomem-database-backup`, and
   `exomem-deletion-dispatcher`; scale `exomem-provisioner-api`,
   `exomem-provisioner-worker`, and `exomem-volume-worker` to zero; suspend the external reconcile CronJob `exomem-hosted-scheduler-exomem-reconcile`.
4. Drain CronJob Jobs and dynamic `exomem-deletion-*` Jobs. Require migration
   and bootstrap Jobs and the bootstrap Secret to be absent. Discover every
   Pending or Running Pod whose normal or init container references
   `exomem-provisioner-database`; drain it and require zero before continuing.
   Record `transient_database_consumers_drained`.
5. Apply the complete 34-entry v2 registry while PostgreSQL still accepts v1.
   Verify only `exomem-provisioner-database` metadata changed to
   `exomem.io/secret-version=v2`, then record
   `new_sops_ciphertext_and_exact_registry_verified` and
   `new_database_secret_applied_before_provider_cutover`.
6. Keep the pre-change authenticated SQL session open, change the role password,
   and prove v2 authenticates with the exact role, schema, and revision. Keep
   DSNs and passwords out of argv and output. Record
   `new_database_credential_accepts`; prove v1 fails specifically due to
   password authentication and record `old_database_credential_rejected`.
7. Patch all five CronJob JobTemplate restart annotations. Restore the exact
   CronJob suspend states and Deployment replicas, wait for all three rollouts,
   and restore the external reconcile state. Record
   `three_deployments_and_five_cronjobs_restored`.
8. Re-run authenticated baseline-cell health and record
   `ready_cell_health_reverified`. Produce every rotation receipt and pass
   `rotation_gate.py` before retiring v1.

On any failure, keep consumers stopped, restore the old password through the
still-authenticated pre-change SQL session, reapply the complete v1 registry,
verify its version label, then restore the exact controller state. If production
rolls back, a follow-up PR reconciles the committed selection.

## Break glass

The offline age identity may decrypt only the specific SOPS artifact needed for
recovery. Work on a tmpfs, keep tracing disabled, use `sops exec-file`, and
destroy the recovery environment when the operation ends. Do not copy the age
identity onto the K3s node, into Vercel, or into Terraform state. Every
break-glass use must record operator, reason, ciphertext path/version, start/end
time, and the content-free verification result.

## Verify

The isolated drill collector turns one completed, mode-`0600` observation into
one 24-hour domain-separated receipt. The observation must name one exact
contracted requirement and use `passed: true`; unknown or failed observations
are refused. Repeat for every requirement, keeping the collector private key
outside the operator, provisioner, and K3s environments:

```bash
infra/helm/platform/files/operational_receipt_collector.py rotation \
  --contract infra/contracts/rotation-drills-v1.json \
  --observation /secure/collector/rotation-observation.json \
  --private-key-file /secure/collector/rotation-receipt.private \
  --output /secure/collector/receipt-01.json
```

Validate retirement proof before those receipts expire, without placing a
secret in arguments or evidence:

```bash
receipt_root=/secure/operator/rotation-receipts/drill-opaque-id
receipt_public_key=/secure/operator/rotation-receipt-collector.public.pem
test -f "$receipt_public_key" && test ! -L "$receipt_public_key"
find "$receipt_root" -type f \
  -exec sh -c 'test "$(stat -c %a "$1")" = 600' _ {} \;
infra/scripts/rotation_gate.py \
  --contract infra/contracts/rotation-drills-v1.json \
  --evidence /secure/operator/content-free-rotation-evidence.json \
  --receipt-root "$receipt_root" \
  --receipt-public-key-file "$receipt_public_key"
```

Each required condition resolves to a distinct receipt file below
`receipt_root`. The drill collector signs the exact drill UUID, rotation,
requirement, old/new versions, observation time, and pass result with the
collector-held Ed25519 private key. The operator and retirement gate receive
only its public key; the private key is never present on the operator
workstation, K3s node, or provisioner. The evidence file carries only the relative path and
SHA-256 for each receipt. The gate rejects missing, reused, escaping, changed,
stale, mismatched, or unauthenticated receipts; an operator-authored boolean or
reference string cannot authorize retirement.

Then inspect only identity/version metadata for each applied Kubernetes Secret.
No verification command may read `.data` or `.stringData`.
