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
