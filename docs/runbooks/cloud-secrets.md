<!-- authority:non-specification -->

# Secret handoff and rotation

This runbook is the supported path from a bound BWS entry, Terraform, an operator
prompt, or a pipe into Vercel and the static K3s Secret set. The command validates the
versioned destination matrix before reading a value. Values never appear in
arguments or successful output; provider CLI output is captured and discarded.

Provider exports are not a recovery store. The handoff rejects whole-value
`[SENSITIVE]`, `<sensitive>`, `[REDACTED]`, and `<redacted>` display markers
(case-insensitively, allowing surrounding whitespace) before any artifact,
receipt reservation, or destination call. Real secret content is not trimmed
or rewritten beyond the existing single trailing-newline handling. This guard
does not validate an arbitrary token or prove its custody.

Use the authoritative secret-manager value, not an environment-variable listing
or a write-only provider export. A Vercel delivery receipt proves that a CLI
write succeeded; it does not prove that a usable source value is recoverable.
For an existing encryption key, check the consumer's format and authenticated
decryption of existing ciphertext before accepting recovery. A generated
replacement is a rotation, not recovery, and must follow the rotation procedure.

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

## BWS-owned control-plane key

The existing production wrapping key is recorded in
`infra/contracts/bws-production-v1.json`. The normal handoff uses this exact
entry through the shared yadm `bwsx-secret` command; no provider export or
name-based secret search is required. Install the shared tooling and use
`harness bws-auth` in an operator terminal if this machine needs authentication.

Check custody identity and format without printing the value:

```bash
bwsx-secret check \
  --bindings "$repo_root/infra/contracts/bws-production-v1.json" control-plane-key
```

For an authorized delivery, choose the next unused destination version from
the retained receipts and set `next_version` before running:

```bash
infra/scripts/secret_handoff.py \
  --matrix "$matrix" \
  --repository-root "$repo_root" \
  --secret control_plane_key \
  --version "${next_version:?set the next unused destination version}" \
  --destination vercel.substrate.production.control-plane.active \
  --source bws \
  --vercel-project "$substrate_root"
```

The BWS helper verifies entry ID, project, expected key and canonical 32-byte
base64url shape. The handoff captures its value in memory and fails without a
fallback if retrieval fails. Dry-run does not contact BWS. Stdin/prompt remain
deliberate compatibility inputs, not recovery from a failed BWS source.

Shape is not decryptability: accepting recovery or rotation still requires
the Substrate consumer's authenticated decryption and digest checks against
existing envelopes. The binding records the existing key; it does not authorize
generating another one. Other credentials, including the production database
DSN, are not certified or migrated by this first binding. Do not substitute a
generic `DATABASE_URL` entry from another product. The dormant gateway's new
destinations still require their governed deployment artifacts.

Adopting this matrix changes its full-file digest, even though the existing
ciphertexts and destination selection are unchanged. Before the next K3s
secret apply, publish a new immutable signed registry/public-key pair using
the active-registry procedure below and pass `--verify-only` against this
matrix. Retain the previous pair; do not re-encrypt secrets or rotate keys
just to refresh the registry signature. This source binding does not apply
anything to a running cluster.

## Terraform-owned credentials and Vercel receipts

Terraform output is captured in memory with `terraform output -raw`. The etcd
snapshot keys are the Terraform-sourced secrets; use the matrix's exact output
and destination pair for each one.

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

## Generated shared credentials

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

The global `CRON_SECRET` has only the
`vercel.substrate.production.global-cron.active` destination. The matrix has no
K3s route for it.

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

K3s bootstrap material is different:
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

`k3s_agent_token` (openspec `add-cloud-node-provisioning`) follows the same
pattern. It is mandatory once any K3s agent is inventoried, and it must differ
from the server token. Agents present only this token, CA-pinned, and never
see the server token:

```bash
openssl rand -base64 48 | infra/scripts/secret_handoff.py \
  --matrix "$matrix" \
  --repository-root "$repo_root" \
  --secret k3s_agent_token \
  --version v1 \
  --destination ansible.hosted-node.k3s-agent-token.active \
  --destination escrow.k3s-agent-token.active \
  --source stdin
```

Escrowing it activates nothing. Activate v1 in a reviewed commit that adds
`"ansible.hosted-node.k3s-agent-token.active": "v1"` to
`infra/contracts/active-ansible-selection-v1.json`. From then on,
`active_ansible_vars.py hosted-node` passes it to every wrapper invocation
below (see `cloud-node-pool.md`). The first run with it restarts the K3s
server with a new agent token. Save the bootstrap data again straight after
that run, as in steps 4 to 6 of
[Rotate the agent token](cloud-dedicated-host.md#rotate-the-agent-token);
otherwise the server's next start stops with `cred/passwd newer than datastore`.

The database-backup B2 key also has an exact SOPS Ansible-var destination. None
of these host-bootstrap values becomes a general cluster Secret.

## Run Ansible with SOPS vars on tmpfs

Keep the non-secret generated host variables in the normal ignored
`group_vars/hosted_nodes.yml`. Pass the encrypted values through the
executable wrapper; it refuses a non-tmpfs workspace, writes mode `0600`
plaintext only inside a private tmpfs directory, and removes it on exit.
`active_ansible_vars.py` passes the version of each hosted-node Ansible
variable that `infra/contracts/active-ansible-selection-v1.json` selects. It
refuses when that file names a destination the secret matrix does not declare,
or a version whose SOPS file is missing. Escrowing a new version activates
nothing; only a reviewed commit to the selection file does. Rotate a token only
through its own procedure, which owns that commit. The agent token's procedure
is [Rotate the agent token](cloud-dedicated-host.md#rotate-the-agent-token). Run
the wrapper with the selected versions:

```bash
export EXOMEM_SECRET_TMPFS_DIR="${XDG_RUNTIME_DIR:?XDG_RUNTIME_DIR is required}"
export SOPS_AGE_KEY_FILE=/secure/operator/exomem-hosted.agekey

fleet_vars_text="$(infra/scripts/active_ansible_vars.py hosted-node)"
mapfile -t fleet_vars <<< "${fleet_vars_text}"
infra/scripts/ansible_with_sops.sh --inventory infra/ansible/inventory.yml "${fleet_vars[@]}"
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
registry/public-key pair for every active K3s destination; it
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
  bwsx-run EXOMEM_CLOUD_ACTIVE_SECRET_REGISTRY_SIGNING_KEY -- sh -c \
  'printf "%s\\n" "$EXOMEM_CLOUD_ACTIVE_SECRET_REGISTRY_SIGNING_KEY"' \
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

## Break glass

The offline age identity may decrypt only the specific SOPS artifact needed for
recovery. Work on a tmpfs, keep tracing disabled, use `sops exec-file`, and
destroy the recovery environment when the operation ends. Do not copy the age
identity onto the K3s node, into Vercel, or into Terraform state. Every
break-glass use must record operator, reason, ciphertext path/version, start/end
time, and the content-free verification result.

## Verify

Verify the signed registry against the current matrix and active selection with
`infra/scripts/apply_active_sops_secrets.py --verify-only`. Record the command's
content-free result before applying any ciphertext.

For a rotation, prove the new receiver accepts the staged sender during overlap.
Prove the new sender accepts after cutover and the retired sender rejects.
Preserve the exact destination/version evidence and each live acceptance result.
The retired Hosted receipt collector does not authorize Cloud rotation or deployment.

Removing a matrix entry does not revoke a provider credential or delete a Secret.
Use [Hosted retirement disposal](cloud-hosted-retirement.md) for the pending inventory.
