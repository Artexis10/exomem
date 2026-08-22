## Why

Exomem Hosted has no reusable, end-to-end runtime-upgrade contract: adopting a new release changes the platform default, while existing tenant cells remain on their birth release unless they are changed out of band. Releases need one governed process that verifies immutable artifacts, preserves every tenant, rolls cells forward under control-plane authority, and contracts only after the whole fleet is proven current.

## What Changes

- Define one version-neutral Hosted runtime-upgrade workflow spanning exact release selection, cross-repository contract adoption, deployment-lock composition, expand deployment, fleet inventory, per-cell rollforward, contract cutover, promotion, acceptance, rollback, and evidence.
- Require release adoption to change only the birth runtime for future cells; it MUST NOT implicitly restart, migrate, replace, delete, relabel, or otherwise mutate an existing tenant.
- Compose the separately governed `hosted-cell-rollforward` capability for every existing legacy cell. Each rollforward is operator-authorized, fenced, sequential, data-preserving, forward-only, and confirmed against the exact authorized runtime before control-plane identity moves.
- Treat an empty fleet as an explicit, recorded no-op for the per-cell phase rather than as an assumption made before inventory.
- Keep the prior runtime trusted in expand mode until no routable, assigned, or unfinished operation still references it; refuse contract mode while any legacy dependency remains.
- Make reviewer promotion and personal-account end-to-end acceptance required release gates, including capacity-safe explicit reuse of an eligible pinned reviewer client when the bounded partition is full.
- Record each concrete runtime release as an execution of this generic contract rather than creating a new specification per version.

## Capabilities

### New Capabilities

- `hosted-runtime-upgrade`: Defines the reusable operator workflow and gates for adopting an immutable runtime release, rolling a mixed-version fleet forward without tenant-data loss, contracting admission, promoting clients, accepting the release, and stopping or recovering safely.

### Modified Capabilities

- `hosted-tenant-cell`: Adds the explicit in-place rollforward lifecycle contract for an existing cell, including identity and vault preservation, declared privileged migrations, exact-runtime confirmation, replay safety, bounded unavailability, and fail-closed recovery.

## Impact

- Exomem: release verification, deployment-lock composition and verification, provisioner lifecycle driver, cell Helm chart migration mode, reviewer-bootstrap harness, upgrade evidence, tests, and operator runbook.
- Substrate companion work: trusted release fixtures and mappings, the `hosted-cell-rollforward` control-plane operation, rollout assignment activation, routable observation updates, destroy-path cleanup, reviewer client reuse, promotion gates, database migrations, and integration coverage.
- Production: additive control-plane deployment, expand-mode platform deployment, deterministic fleet inventory, zero or more sequential cell rollforwards, guarded contract cutover, reviewer promotion, and personal-account acceptance.
- Tenants: no automatic mutation during release adoption; when a cell is explicitly rolled forward, its cell identity, binding, volume, canonical vault bytes, security state, credentials, grants, and entitlement remain intact. A failed cell rollforward stops the fleet and leaves expand mode active.
