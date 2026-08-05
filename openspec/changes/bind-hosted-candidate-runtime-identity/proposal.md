## Why

Exomem now publishes independently attested runtime and provisioner image candidates, but nothing yet composes those candidates into immutable phase-specific deployment units or proves that a provisioned cell is running the selected runtime contract. A merge can therefore produce trustworthy artifacts without making the Hosted control plane safe to deploy, retry, contract, or roll back.

## What Changes

- Add provisioner wire protocol v2 on the existing `/cells/<action>` routes. V2 requests carry one strict `runtimeTarget`; health returns the matching observed `runtimeIdentity`.
- Preserve provisioner v1 request and response behavior exactly for expansion and persisted-operation replay, then reject fresh v1 operations after contraction.
- Persist the outer provisioner wire protocol independently from the Hosted runtime protocol so retries remain byte- and version-stable across restarts and deployments.
- Compose separately verified runtime and provisioner candidates with verified runtime-contract evidence into deterministic expand and contract locks, with one bounded legacy-v1 runtime catalog, component-specific source-closure checks, and exact rollback pins.
- Keep candidate, compatibility, and client-package lineage in Substrate; none of those fields cross the provisioner API or become cell-reported identity.
- Add the paired Substrate v2 consumer and an expand/contract rollout in which issuance and every cell-scoped runtime target are stored per operation before the v2 feature gate is enabled; context-only export-reference and tenant-destroy actions use explicit target-free v2 schemas.
- Correct the active private-alpha specifications that currently describe provisioner v1 and flattened or compatibility-bearing health as the final contract.
- Produce and attest a new provisioner candidate after implementation. The existing signed provisioner is rollback-only because it cannot serve v2.

## Capabilities

### New Capabilities

- `hosted-candidate-runtime-binding`: Candidate composition, provisioner v2 runtime targeting and observation, durable versioned replay, expand/contract rollout, and exact rollback authority.

### Modified Capabilities

- `hosted-image-candidate-publication`: Candidate consumption additionally requires ancestry and component-specific source-closure proof; signatures cannot waive source drift.

## Impact

- Exomem provisioner schemas, admission, durable operation storage, lifecycle targeting, authenticated runtime probes, release composition, Helm inputs, deployment verification, fixtures, tests, and hosted runbooks.
- A paired Substrate change to its provisioner client, lifecycle-operation schema, reconciler, candidate catalog binding, tests, and active Hosted OAuth OpenSpec.
- Provisioner wire v2 is additive during expansion but becomes the only protocol for fresh operations after contraction. The Hosted runtime protocol remains `1`, private runtime paths remain `/private/exomem/v1/...`, and recovery/destruction paths remain operable when live runtime probes fail.
- Runtime candidate `ghcr.io/artexis10/exomem@sha256:3264271d7292c713e1f6ba6ae4a11a4b8e8c52a58a1a06e1d13726a515175ca3` may be reused only if the source-closure guard from `f1472c297d9256a28c9706bb666e249b64cfd804` passes. The v1 provisioner candidate at `sha256:b3f2f12691207200a57dd193f3669a8f2cd2f7c058105b0d4af691f3057097df` is retained only in the exact rollback tuple.
