# Hosted Terraform backend

## Preconditions

Use pinned tools, an offline SOPS/age recipient, and B2 bootstrap credentials in
the environment. Keep shell tracing disabled. The real-account mutual-exclusion
and prior-version recovery proof remains a deployment gate.

```bash
set +x
source infra/tool-versions.env
export SOPS_AGE_RECIPIENTS=age1replace
```

Plan and apply bootstrap state only through the versioned escrow wrapper:

```bash
infra/scripts/bootstrap_backend.sh plan /run/user/$UID/bootstrap.tfplan /secure/escrow/bootstrap.sops.json
infra/scripts/bootstrap_backend.sh apply /run/user/$UID/bootstrap.tfplan /secure/escrow/bootstrap.sops.json
```

For recovery, restore a prior B2 object version into a disposable backend key,
run a refresh-only plan, and compare addresses before touching either production
key. Never fall back to local production state.

## Verify

```bash
terraform -chdir=infra/terraform/foundation init -backend-config="$TF_BACKEND_CONFIG_FILE" -input=false
terraform -chdir=infra/terraform/foundation plan -refresh-only -detailed-exitcode -input=false
```

Exit `0` is required. Exit `2` is drift and blocks deployment.
