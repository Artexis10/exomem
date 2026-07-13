## Why

Exomem's hosted tenant runtime and Substrate control plane are substantial but cannot yet provision or operate a real isolated cell. A private alpha for non-technical users now needs a reproducible cloud substrate, the missing durable provisioner, safe external routing, and proven off-cluster recovery rather than manual server setup.

## What Changes

- Add split-state Terraform for a dedicated Hetzner/Cloudflare foundation and B2 durability resources, with remote locking, saved-plan enforcement, and destruction guards.
- Add idempotent Ansible host hardening and a pinned single-node K3s bootstrap designed for declarative recovery.
- Add pinned platform and cell Helm releases with encrypted Hetzner volumes, fixed private mount contracts, quotas, Pod Security, admission checks, RBAC, and default-deny tenant networking.
- Add the exact durable `exomem-cell-provisioner.v1` service expected by Substrate, including idempotency, tenant fencing, restart-safe asynchronous work, lifecycle actions, and proof-bearing destruction.
- Add Cloudflare Access/Tunnel and Traefik control routing plus a separate one-time-grant direct-transfer lane that bypasses Vercel's 4.5 MB Function body limit.
- Add centralized portable backups every 30 minutes, envelope AES-256-GCM encryption, protected B2 retention, clean candidate restore, and explicit private-alpha RPO/RTO targets.
- Add lightweight external/internal monitoring, cost and capacity gates, recovery/deletion runbooks, and owner-first proof drills.
- Reconcile the existing Exomem and Substrate hosted branches as prerequisites: one immutable release contract, supported init/restore helpers, overlapping service-credential rotation, Cloudflare service-token headers, and direct-transfer tickets.

## Capabilities

### New Capabilities

- `hosted-infrastructure-foundation`: Reproducible, hardened Hetzner, Cloudflare, K3s, storage, secret, and deployment foundations for the private alpha.
- `hosted-cell-orchestration`: Durable provisioner protocol, tenant fencing, isolated cell reconciliation, lifecycle control, and destruction proof.
- `hosted-private-routing`: Access-protected control routing and bounded direct browser transfers without exposing long-lived cell credentials.
- `hosted-durability`: Portable encrypted backups, exports, candidate restore, retention, deletion, and measurable recovery objectives.
- `hosted-alpha-operations`: Capacity, cost, observability, disaster-recovery, and owner/invite proof gates for opening the private alpha.

### Modified Capabilities

None. The hosted runtime capabilities are currently carried by Exomem PR #227 and receive their prerequisite contract changes on that branch before this infrastructure binds to them.

## Impact

- New `infra/terraform`, `infra/ansible`, `infra/helm`, `infra/provisioner`, validation, and runbook surfaces in Exomem.
- Targeted prerequisite changes to Exomem PR #227 and Substrate PR #32.
- New dependencies on Terraform, Ansible, K3s/Kubernetes, Helm, Hetzner Cloud CSI, Cloudflare Access/Tunnel, B2, SOPS/age, FastAPI, SQLAlchemy/Alembic, and a dedicated Neon role/schema.
- New operational credentials and recovery material, all outside the open-source runtime's personal-vault path.
- No server-side reasoning model, GPU path, or shared canonical-knowledge database is introduced; hosted cells remain the same pure-substrate, single-vault runtime boundary.
