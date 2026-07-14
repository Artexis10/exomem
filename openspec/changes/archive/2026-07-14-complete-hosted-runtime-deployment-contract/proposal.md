## Why

The hosted cell runtime has strong private HTTP, isolation, and portability primitives, but Kubernetes and the provisioner still lack a supported, versioned operator interface for creating, restoring, rotating, probing, and streaming a cell. The private alpha cannot safely automate lifecycle work while those steps depend on Python internals or a long-lived cell bearer in the browser.

## What Changes

- Add a normative JSON operator contract and an idempotent hosted-cell initializer bound to the exact cell/vault identity, canonical roots, runtime UID/GID, release, and protocol expected by the deployment.
- Add an exclusively locked offline restore-candidate command that validates a control-plane-pinned archive digest, rejects source-cell runtime state, crash-recovers through a durable journal, rebuilds derived state, and atomically publishes canonical bytes under a new cell identity while preserving logical vault identity.
- Add durable active/pending service-credential overlap with explicit bootstrap, stage, authenticated health proof, promote, abort, and finalize transitions; persist only token digests and content-free version/proof metadata.
- Add a content-free authenticated exec probe against a fixed literal-loopback endpoint with proxy use disabled and a bounded response contract.
- Replace browser use of the long-lived cell bearer with signed, short-lived, one-time upload/download capabilities on two dedicated public routes. Cap upload request bodies at 90 MiB; download limits remain explicit per grant.
- Add a process-safe cell security authority for credential state and restart-safe JTI consumption, separate from canonical vault mutation serialization but integrated with lifecycle transfer admission and draining.
- Add a bounded writable transfer-temp directory beneath the state root so the hosted image can retain a read-only root filesystem.
- Preserve ordinary local Exomem behavior unchanged; every new operator/runtime surface is hosted-only and fail-closed.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `hosted-tenant-cell`: Define the supported initializer, root ownership, security authority, credential-overlap lifecycle, authenticated probe, and sole public capability-ingress exception used by the orchestrator and Kubernetes.
- `hosted-vault-portability`: Distinguish quiesced source export from exclusively locked offline target restore, and define restore-candidate identity scrubbing, crash recovery, canonical publication, and derived-state rebuild behavior.
- `hosted-gateway-contract`: Strengthen hosted transfer authority from short-lived tenant binding to browser-safe, one-time, origin/path/operation-bound grants that never expose the cell bearer.

## Impact

Affected areas include `hosted_runtime.py`, `hosted_portability.py`, `hosted_gateway.py`, `server_hosted.py`, the shared service authenticator, the hosted CLI/image entrypoints, persistent cell security state, transfer routes/temp wiring, contract fixtures, container checks, and their security/isolation tests. The provisioner and Substrate gateway gain stable commands and response schemas to call; local CLI/MCP/REST behavior and canonical vault formats do not change.
