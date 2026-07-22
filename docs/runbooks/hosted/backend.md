# Hosted Terraform backend

## Preconditions

Use pinned tools, an offline SOPS/age recipient, an existing HCP Terraform
organization, and a user or team token with project/workspace plus state/lock
permissions. Keep shell tracing disabled. HCP stores state/history and locking;
Terraform execution remains local or in the reviewed GitHub Actions wrapper.

```bash
set +x
source infra/tool-versions.env
export SOPS_AGE_RECIPIENTS=age1replace
export TF_CLOUD_ORGANIZATION=replace-with-approved-org
export TFE_TOKEN=read-from-secret-manager
export TF_TOKEN_app_terraform_io="$TFE_TOKEN"
```

The already-applied `terraform/bootstrap` B2 state MUST NOT be repurposed or
destroyed here. Plan and apply the separate HCP bootstrap state only through its
versioned escrow wrapper:

```bash
infra/scripts/bootstrap_hcp_backend.sh plan /run/user/$UID/hcp-bootstrap.tfplan /secure/escrow/hcp-bootstrap.sops.json
infra/scripts/bootstrap_hcp_backend.sh apply /run/user/$UID/hcp-bootstrap.tfplan /secure/escrow/hcp-bootstrap.sops.json
```

Verify the exact state-only contract and run the disposable real-account proof:

```bash
infra/scripts/verify_hcp_backend.py preflight --workspace exomem-hosted-foundation
infra/scripts/verify_hcp_backend.py preflight --workspace exomem-hosted-durability
infra/scripts/verify_hcp_backend.py prove \
  --evidence /secure/evidence/exomem-hcp-backend-proof.json
```

The proof requires one apply to hold HCP's state lock while a second mutating
apply is rejected, then rolls the disposable workspace back to a prior HCP state
version and verifies its recorded revision. It never tests rollback against
production state. If rollback verification or its refresh-only plan fails, the
proof exits with the workspace intentionally still operator-locked; inspect it
before an explicit unlock.

For a production recovery, first lock the exact HCP workspace and retain its
current state-version ID. Select the prior version only after comparing lineage,
serial, resource addresses, and provider reality. Rollback duplicates that
historical version as current; it does not roll back cloud resources. Keep the
workspace locked until a refresh-only plan and provider inspection are reviewed,
then unlock. Never fall back to B2 or local production state, and discard every
saved plan created before a state migration or rollback.

## Verify

```bash
infra/scripts/verify_hcp_backend.py preflight --workspace exomem-hosted-foundation
terraform -chdir=infra/terraform/foundation init -input=false
terraform -chdir=infra/terraform/foundation plan -refresh-only -detailed-exitcode -input=false
```

Exit `0` is required. Exit `2` is drift and blocks deployment.
