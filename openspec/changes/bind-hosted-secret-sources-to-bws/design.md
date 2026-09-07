## Context

See proposal.md. The existing matrix already owns destination selection and source allowlists. BWS identity/shape checks belong to the shared yadm tool, not a second SDK implementation here.

## Goals / Non-Goals

Make the normal control-key handoff use its recorded source. Do not rotate credentials, infer a database source, change existing ciphertexts/receipts or add gateway destinations without the governed deployment inputs.

## Decisions

- Add source fields `bindings` (repository-relative JSON under `infra/contracts`) and `binding` (one alias). Resolve the path inside the repository before invoking `bwsx-secret get --bindings FILE NAME`; capture output and discard upstream errors. Do not fall back to stdin, prompt, inventory or a provider export if BWS fails.
- Keep destination validation and dry-run ahead of source access. A dry-run performs no BWS call. Existing source kinds stay compatible; the documented control-key path chooses BWS.
- The production binding records the existing verified BWS entry and canonical base64url-32 format. Authenticated decryptability remains the consumer's responsibility. No Python SDK or secret-manager credential is added to the runtime service.
- The signed active-secret registry binds the full matrix digest, including source metadata. Adoption therefore requires a newly signed immutable registry pair and verify-only proof before the next K3s apply; existing ciphertexts, key material and selection do not change. Retain the previous pair. No cluster apply is part of adding the source adapter.

## Risks / Trade-offs

- Shared helper absent → explicit source failure before destination mutation; install the shared tooling rather than bypassing it.
- Bound key has valid shape but cannot decrypt existing data → require the consumer proof before recovery/rotation acceptance; this adapter does not certify that proof.
- Other secrets still use legacy sources → name the adoption boundary; do not claim a complete inventory migration.
