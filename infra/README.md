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
infra/scripts/plan.sh foundation infra/terraform/foundation/foundation.tfplan
infra/scripts/plan.sh durability infra/terraform/durability/durability.tfplan
infra/scripts/apply_saved_plan.sh foundation infra/terraform/foundation/foundation.tfplan
```

Destruction or replacement is rejected unless the apply command receives one
`--allow-destructive <exact Terraform address>` flag for every affected
resource. The plan is never recomputed during apply. Terraform state, saved
plans, plan JSON, generated inventory, `.tfvars`, decrypted SOPS material, and
age keys are ignored by Git.
