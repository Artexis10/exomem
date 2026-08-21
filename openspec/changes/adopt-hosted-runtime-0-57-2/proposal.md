## Why

Exomem Hosted is still pinned to runtime `0.54.1` even though `0.57.2` is the newest stable release with a successfully published, signed Hosted image. The alpha should start on the current stable runtime, but release adoption must remain non-destructive: changing the platform default must not mutate, restart, migrate, replace, delete, or otherwise alter an existing tenant cell or its vault.

## What Changes

- Adopt the exact signed `0.57.2` Hosted runtime and its additive `hosted-alpha-agent-v1` contract across Exomem's deployment lock and Substrate's trusted-release catalog.
- Retain `0.54.1` as a legacy/rollback release and require expand-mode coexistence whenever a routable cell still reports a legacy release.
- Make release adoption select the birth runtime for future cells only. Existing cells remain pinned to their assigned runtime until a separately authorized rollforward proves vault preservation and exact runtime identity.
- Add a guarded reviewer-bootstrap path that reuses an eligible disabled, pinned loopback OAuth client when the bounded operator-client partition is full; never broaden client admission or silently reuse an incompatible client.
- Verify the complete promotion path against the deployed `0.57.2` cell: preflight, prepare, timed reviewer bootstrap, Claude and OpenAI evidence, cohort promotion, personal-account OAuth, read/write round-trip, reconnect, and cleanup/leak checks.
- Refuse the contract-mode cutover or promotion when runtime identity, contract digests, routable-cell census, capacity, reusable-client eligibility, or evidence differs from the reviewed release.

## Capabilities

### New Capabilities

- `hosted-release-adoption`: Defines exact stable-runtime selection, cross-repository contract adoption, expand/contract rollout gates, reviewer-client reuse, promotion, rollback, and end-to-end acceptance.

### Modified Capabilities

- `hosted-tenant-cell`: Requires a platform release adoption to leave every existing tenant cell, persistent volume, vault byte set, security state, binding, entitlement, and routing assignment unchanged unless a separate explicit cell-rollforward operation is authorized.

## Impact

- Exomem: deployment-lock v2 pair and evidence, Hosted release verification, reviewer bootstrap harness, focused tests, and operator documentation.
- Substrate companion change: the five release-pinned trusted-contract sites, gateway fixture/catalog, bootstrap release authority, database/integration fixtures, and Hosted alpha runbook.
- Production: Substrate control-plane deploy, Exomem platform lock expand/contract deploy, fresh `0.57.2` candidate, reviewer promotion, and personal-account acceptance.
- Existing tenants: no automatic cell rollout, vault mutation, storage replacement, entitlement change, client revocation, or global maintenance window. Any future existing-cell upgrade remains a separate governed change.
