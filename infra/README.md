# Exomem hosted infrastructure

This tree owns the dedicated private-alpha deployment. The shared Substrate
application remains in its own repository; tenant knowledge remains inside one
isolated Exomem cell and its retained encrypted volume.

The lifecycle domains are deliberately split:

- `terraform/foundation`: Hetzner compute/network/firewall and Cloudflare
  Tunnel/DNS/Access. It cannot manage recovery buckets or backup credentials.
- `terraform/durability`: versioned B2 state/recovery/database-backup storage
  and least-privilege identities. It cannot manage the node or ingress.
- `terraform/bootstrap`: one-time creation of the versioned remote-state bucket
  and identities; never run from the normal apply path.
- `ansible`: idempotent host hardening and pinned K3s bootstrap.
- `helm/platform`: cluster-wide CSI, Tunnel, policy, scheduler, and provisioner.
- `helm/cell`: the fixed one-vault StatefulSet/PVC/Secret/Service contract.
- `provisioner`: durable external control-plane worker and API.
- `scripts`: non-printing validation, saved-plan, and secret handoff tooling.

No production apply is implicit. Planning produces a mode `0600` saved plan,
the JSON policy inspector rejects unapproved replacement/destruction, and apply
accepts only that reviewed saved plan.

## Validation and apply

Install the exact versions from `tool-versions.env`, then run:

```bash
infra/scripts/validate.sh
SOPS_AGE_RECIPIENTS=age1... \
  infra/scripts/bootstrap_backend.sh plan \
  /run/user/$UID/exomem-bootstrap.tfplan \
  /secure/operator/exomem-bootstrap-state.sops.json
SOPS_AGE_RECIPIENTS=age1... \
  infra/scripts/bootstrap_backend.sh apply \
  /run/user/$UID/exomem-bootstrap.tfplan \
  /secure/operator/exomem-bootstrap-state.sops.json
TF_BACKEND_CONFIG_FILE=/run/user/$UID/exomem-foundation.tfbackend \
  infra/scripts/plan.sh foundation infra/terraform/foundation/foundation.tfplan
TF_BACKEND_CONFIG_FILE=/run/user/$UID/exomem-durability.tfbackend \
  infra/scripts/plan.sh durability infra/terraform/durability/durability.tfplan
infra/scripts/apply_saved_plan.sh foundation infra/terraform/foundation/foundation.tfplan
```

The backend config must be mode `0600` and contains only the B2 state bucket
and S3 endpoint. Supply its prefix-scoped key through `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY`; do not write those secrets into the backend config.

Destruction or replacement is rejected unless the apply command receives one
`--allow-destructive <exact Terraform address>` flag for every affected
resource. The plan is never recomputed during apply. Terraform state, saved
plans, plan JSON, generated inventory, `.tfvars`, decrypted SOPS material, and
age keys are ignored by Git.
