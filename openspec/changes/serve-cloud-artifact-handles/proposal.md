# Proposal

## Why

Cloud exposes file-handle preservation but its runtime cannot resolve or retrieve those files because tenant cells have no egress. Native directory acceptance reproduced the failure. Restore the advertised capability without giving a tenant runtime general network access.

## What Changes

- Add an internal artifact broker that uses the existing safe-fetch implementation and streams only exact gateway-authorized HTTPS file handles.
- Carry a short-lived, cell-bound fetch grant in a private gateway header, never in tool arguments or public responses.
- Permit runtime egress only to the broker's fixed port. Keep cell DNS, internet, metadata and cross-cell access denied; leave gateway public egress denied.
- Stage broker responses through the current artifact loops and commit through the existing Sources/Evidence writers and canonical receipts.
- Keep transport configuration default-off. Missing broker configuration or unavailable transport reports a bounded artifact failure, never a successful preservation or direct-network fallback. Ordinary tools and desktop retrieval are unchanged.

## Capabilities

### New Capabilities

- `cloud-artifact-transport`: authorized, bounded file retrieval across the Cloud isolation boundary, including grant ownership, ephemeral staging and deployment confinement.

### Modified Capabilities

- `client-artifact-preservation`: Cloud file-handle commands use the broker while retaining their canonical writers, ordered outcomes and failure semantics.

## Impact

Exomem artifact staging, a small internal broker entry point, cellctl manifests/configuration, platform Helm network/admission rules and operational deployment instructions. Substrate's Cloud gateway adds bounded grant extraction and signing. No database or stored-vault migration, model execution, compiler, ranking, engagement or preference change. The existing Cloud-cell specification's absolute egress prohibition is narrowed to the named broker alone; no upload or REST route is added to cells.
