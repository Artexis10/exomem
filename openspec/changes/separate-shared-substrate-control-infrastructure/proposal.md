# Proposal

## Why

The production PostgreSQL host serves Substrate authentication, billing and multiple products, but its infrastructure and host configuration remain owned by Exomem. Give shared control infrastructure an organisation-owned repository and state boundary so a product deployment cannot change or destroy the shared database.

## What Changes

- Establish `substrate-systems/substrate-infra` as the single source for the shared control host, PostgreSQL/PgBouncer, database TLS and backup operations.
- Transfer management of existing control-host and dedicated backup resources into separately locked HCP Terraform workspaces using reviewed state-only handover; preserve the running resources and their identities.
- Make Exomem consume a versioned, non-secret infrastructure dependency contract instead of managing the host. Keep the current Hetzner project and shared private network for this ownership split.
- Preserve canonical Bitwarden bindings, existing encrypted database credentials, backup lineage and key-only managed SSH. Separate host administration from application-owned migrations.
- Establish a shared, code-managed NetBird Cloud administration standard for our own infrastructure: restricted peer groups, ordinary managed OpenSSH over the private mesh and verified recovery access. Begin with the control host; Q, Exomem and Substrate consume the shared policy rather than copying it. Extend adoption to the owner phone, workstations and Moshi connections. Retain Tailscale only during staged migration, retiring each dependency after its NetBird replacement is verified.
- After the handover, move the control host to its own Substrate Hetzner project. Exomem's cellctl and gateway then reach the database over `verify-full` TLS from one allowlisted address instead of the private network (design decision 10).
- Keep migration execution disabled until access, recovery, independent review and exact saved-plan gates pass. No optional runtime capability or model is introduced.

## Capabilities

### Shared Capabilities

The shared owner now holds `shared-control-infrastructure-ownership` and
`tailnet-server-administration` in [Substrate infrastructure OpenSpec](https://github.com/substrate-systems/substrate-infra/tree/main/openspec/specs).
The requirements moved intact. This change retains the coordinated migration
plan and Exomem’s product deltas until the remaining work closes.

### Modified Capabilities

- `cloud-node-pool`: an agent node's firewall admits SSH only during a declared break-glass window; routine administration uses the company NetBird.
- `cloud-cell`: the gateway and cellctl roles reach the control database on 5432 only from the K3s server node's public /32 (decision 10). The requirement is not canonical yet, so the amendment is made in place in the active change `adopt-exomem-cloud-plain-cells`; this change carries no `cloud-cell` delta.

The existing private-alpha infrastructure change remains evidence for the deployment contracts inherited by this extraction; its unarchived requirements are not silently rewritten here.

## Impact

Exomem `infra/terraform/foundation`, its control-database outputs, `infra/ansible/site.yml`, the base/PostgreSQL roles, inventory generation and secret-destination contracts; the new Substrate infrastructure repository and workspace; shared database consumers. Product schemas, Cloud cell images, gateway flags and provider-project transfer are outside this ownership handover.
