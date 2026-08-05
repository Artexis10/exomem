## Why

Hosted runtime and provisioner publication currently stops at mutable tags, build logs, and an aggregate release manifest that may describe different bytes from the image actually pushed. The `0.35.1` hosted rollout needs independently verifiable image candidates built from the real OCI digest before any final release composition or deployment can be trusted.

## What Changes

- Publish separate runtime and provisioner image-candidate records from the workflow that builds each image, using the OCI digest returned by the build as the sole image authority.
- Attest each immutable image subject and then attest the exact candidate JSON bytes with GitHub artifact attestations; retain both verifiable bundles and reject candidates whose release metadata, repository, source commit/ref, signer workflow, subject digest, or candidate digest is outside the allowed release policy.
- Give hosted runtime images an exact checked-out source-commit tag for discovery while keeping all tags explicitly non-authoritative.
- Make manual runtime republishing prove that the selected release tag resolves to the checked-out commit instead of labelling tag bytes with the workflow-dispatch commit.
- Decouple provisioner publication from aggregate runtime, Substrate, and final-release manifests so each component can be produced and verified before composition.
- Add deterministic record/verification tooling and workflow contract tests that fail closed on malformed identity, synthetic digests, mutable image references, expired-only evidence, or unreviewed source refs.
- Keep final hosted release-manifest assembly and provisioner protocol changes out of this change; they consume these independently published candidates in a subsequent change.

## Capabilities

### New Capabilities

- `hosted-image-candidate-publication`: Defines independent, source-bound, digest-authoritative, provenance-verifiable runtime and provisioner image candidates suitable for later hosted release composition.

### Modified Capabilities

None.

## Impact

- GitHub Actions release and provisioner image workflows.
- New strict candidate record and verification scripts under `infra/scripts/`.
- Hosted workflow and platform-distribution tests.
- GHCR image publication and GitHub artifact-attestation evidence.
- No runtime API, MCP command, marketplace package, provisioner wire protocol, or deployed cell behavior changes in this change.
