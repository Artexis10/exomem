# Exomem hosted infrastructure

This tree owns the dedicated private-alpha deployment. The shared Substrate
application remains in its own repository; tenant knowledge remains inside one
isolated Exomem cell and its retained encrypted volume.

The lifecycle domains are deliberately split:

- `terraform/foundation`: Hetzner compute/network/firewall and Cloudflare
  Tunnel/DNS/Access. It cannot manage recovery buckets or backup credentials.
- `terraform/durability`: private B2 recovery, short-lived user-export, and
  complete-database backup storage with least-privilege identities. It cannot
  manage the node, ingress, Kubernetes objects, or tenant application records.
- `terraform/cell-data`: the B2 cell-data member account's compliance-locked cell
  backup bucket, export and import staging bucket, and the key custodian and
  `cellctl` keys. It cannot manage the platform account's buckets.
- `terraform/bootstrap`: already-applied legacy B2 state bucket and identities;
  quarantined until a separately reviewed post-HCP cleanup.
- `terraform/hcp-bootstrap`: one-time HCP project plus explicit local/state-only
  foundation, durability, cell-data, and disposable proof workspaces; never run from the
  normal apply path.
- `ansible`: idempotent host hardening and pinned K3s bootstrap.
- `helm/platform`: cluster-wide CSI, ingress, policy, scheduler, cellctl and the
  Exomem Cloud gateway.
- `cellctl`: the Exomem Cloud cell controller.
- `scripts`: non-printing validation, saved-plan, and secret handoff tooling.

Cell controller PostgreSQL tests read the canonical paid-storage migration and grants from the revision in
`cellctl/tests/fixtures/substrate-contract.json`. CI checks out that revision through the shared
`substrate-test-contract` action. For local PostgreSQL tests, set `CELLCTL_SUBSTRATE_CONTRACT_ROOT` to a
Substrate Git checkout that contains the pinned commit. The fixture reads committed SQL blobs; it does
not read dirty files or fetch the contract while collecting pure tests. Update the manifest when adopting
a new companion SQL contract.

No production apply is implicit. Planning produces a mode `0600` saved plan,
the JSON policy inspector rejects unapproved replacement/destruction, and apply
accepts only that reviewed saved plan.

## Validation and apply

Install the exact versions from `tool-versions.env`, then run:

```bash
infra/scripts/validate.sh
export SOPS_AGE_RECIPIENTS=age1...
export TF_CLOUD_ORGANIZATION=replace-with-approved-org
export TFE_TOKEN=read-from-secret-manager
export TF_TOKEN_app_terraform_io="$TFE_TOKEN"
infra/scripts/bootstrap_hcp_backend.sh plan \
  /run/user/$UID/exomem-hcp-bootstrap.tfplan \
  /secure/operator/exomem-hcp-bootstrap-state.sops.json
infra/scripts/bootstrap_hcp_backend.sh apply \
  /run/user/$UID/exomem-hcp-bootstrap.tfplan \
  /secure/operator/exomem-hcp-bootstrap-state.sops.json
infra/scripts/plan.sh foundation infra/terraform/foundation/foundation.tfplan
infra/scripts/plan.sh durability infra/terraform/durability/durability.tfplan
infra/scripts/plan.sh cell-data infra/terraform/cell-data/cell-data.tfplan
infra/scripts/apply_saved_plan.sh foundation infra/terraform/foundation/foundation.tfplan
```

The fixed workspace names are committed in each root. Supply only the approved
organization and HCP user/team token through the environment; never write the
token into Terraform configuration, variables, plans, or HCP workspace
variables. The wrappers verify explicit local execution and the complete
state-only workspace contract before `terraform init`.

Destruction or replacement is rejected unless the apply command receives one
`--allow-destructive <exact Terraform address>` flag for every affected
resource. The plan is never recomputed during apply. Terraform state, saved
plans, plan JSON, generated inventory, `.tfvars`, decrypted SOPS material, and
age keys are ignored by Git.

The versioned non-printing secret workflow is documented in
`docs/runbooks/cloud-secrets.md`. Its destination matrix keeps the hosted
scheduler in its exact two peers and global `CRON_SECRET` out of K3s.
