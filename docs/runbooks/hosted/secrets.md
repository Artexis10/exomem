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
  destination is used.
- Use a monotonically increasing `vN` for every rotation. Never reuse a version
  after a partial or failed handoff.

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

Use the matrix's exact durability output/destination pair for each B2 key. The
upload, restore, delete, and database-backup identities are separate on purpose;
never combine or substitute them.

## Generated shared credentials

Read once and deliver the initial hosted-scheduler bearer to both named peers:

```bash
openssl rand -base64 48 | infra/scripts/secret_handoff.py \
  --matrix "$matrix" \
  --repository-root "$repo_root" \
  --secret hosted_scheduler_secret \
  --version v1 \
  --destination vercel.substrate.production.scheduler.active \
  --destination k3s.scheduler.active \
  --source stdin \
  --vercel-project "$substrate_root"
```

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
`k3s_server_token` and the database-backup B2 key have exact SOPS Ansible-var
destinations and are never installed as general cluster Secrets.

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
  --destination vercel.substrate.production.scheduler.active \
  --destination k3s.scheduler.active \
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

If any destination fails, keep the last working receiver/sender pair, record
the partial version, and retry idempotently. Never retire an old value until
acceptance, cross-route denial, and cadence health are all proven.

## Break glass

The offline age identity may decrypt only the specific SOPS artifact needed for
recovery. Work on a tmpfs, keep tracing disabled, use `sops exec-file`, and
destroy the recovery environment when the operation ends. Do not copy the age
identity onto the K3s node, into Vercel, or into Terraform state. Every
break-glass use must record operator, reason, ciphertext path/version, start/end
time, and the content-free verification result.
