# Proposal

## Why

The production PostgreSQL host serves Substrate authentication, billing and multiple products, but its infrastructure and host configuration remain owned by Exomem. Give shared control infrastructure an organisation-owned repository and state boundary so a product deployment cannot change or destroy the shared database.

## What Changes

- Establish `substrate-systems/substrate-infra` as the single source for the shared control host, PostgreSQL/PgBouncer, database TLS and backup operations.
- Transfer management of existing control-host and dedicated backup resources into separately locked HCP Terraform workspaces using reviewed state-only handover; preserve the running resources and their identities.
- Make Exomem consume a versioned, non-secret infrastructure dependency contract instead of managing the host. Keep the current Hetzner project and shared private network for this ownership split.
- Preserve canonical Bitwarden bindings, existing encrypted database credentials, backup lineage and key-only managed SSH. Separate host administration from application-owned migrations.
- Establish a shared, code-managed Tailscale administration standard for our own infrastructure: restricted server tags, ordinary managed OpenSSH over the tailnet and verified recovery access. Begin with the control host; product repositories consume the shared policy rather than copying it.
- Keep migration execution disabled until access, recovery, independent review and exact saved-plan gates pass. No optional runtime capability or model is introduced.

## Capabilities

### New Capabilities

- `shared-control-infrastructure-ownership`: Single-owner management and reversible state handover for shared control infrastructure, with preserved consumers and secret custody.
- `tailnet-server-administration`: One shared enrollment and access policy, preserving managed SSH identities and preventing firewall lockout during adoption.

### Modified Capabilities

None. The existing private-alpha infrastructure change remains evidence for the deployment contracts inherited by this extraction; its unarchived requirements are not silently rewritten here.

## Impact

Exomem `infra/terraform/foundation`, its control-database outputs, `infra/ansible/site.yml`, the base/PostgreSQL roles, inventory generation and secret-destination contracts; the new Substrate infrastructure repository and workspace; shared database consumers. Product schemas, Cloud cell images, gateway flags and provider-project transfer are outside this ownership handover.
