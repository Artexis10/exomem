## Why

Cloud needs a service policy that delivers responsive search and prompt semantic visibility within its tenant and node budgets. Workstation Quiet/Normal/Performance modes bundle device use, model lifetime, caches and maintenance around sharing a personal machine; using the same bundle on managed infrastructure obscures the service contract.

## What Changes

- Add an operator-selected `service-v1` Cloud resource profile, independently resolved from workstation modes. Existing images retain legacy behavior until an ordinary digest-pinned rollout selects the new profile.
- Apply the same policy to every supported vault schema/layout through existing vault discovery and indexing contracts. Deployment settings and resource budgets come from operator code; no maintainer-specific folder names or tenant-side performance-mode workaround is required.
- Keep only the core recall encoder resident in a running healthy cell. Preserve lazy, reclaimable corpus caches and optional models; do not equate Cloud with Performance or preload the whole vault.
- Wake existing durable deferred indexing promptly inside its owning service and share existing compute admission fairly with interactive queries and bounded import/recovery work. Preserve successful inline indexing and exact revision publication.
- Expose effective policy, core-model readiness and oldest pending semantic work without loading resources. Canonical commit and semantic freshness remain distinct outcomes.
- Gate rollout on the large owner vault: measured save-to-semantic visibility, query latency, restart recovery and simultaneous-cell capacity at the existing CPU/memory limits. These are release gates, not claims about current performance.

The existing CPU ONNX recall encoder is a pure-substrate ranking model: this changes resource orchestration, not its weights, authority or generated output. Optional reranking, visual and media models remain lazy/default-off where currently optional, and soft-fail without blocking lexical retrieval or canonical writes.

## Capabilities

### New Capabilities

- `cloud-service-resource-policy`: Operator-owned Cloud resource selection, selective core residency, service freshness and capacity acceptance.

### Modified Capabilities

- `resource-governance`: Scope workstation residency requirements independently of an explicit Cloud service profile and report effective policy truthfully.
- `live-index-freshness`: Prompt in-service consumption of existing durable semantic debt under the Cloud profile without weakening publication or recovery fences.

## Impact

Runtime policy resolution (`mode.py`, `cloud_cell.py`), warmup and idle reaping, existing watcher/deferred-index ownership and model admission, no-allocation diagnostics, the Cloud image, resource/freshness tests and operator runbooks. Local modes and Hosted image defaults retain their existing behavior. No tenant configuration authority, authentication, payment, model-space, backup format or fleet resource-limit change is included. Disk-first semantic corpus work remains a separate prerequisite only if measured capacity cannot pass this policy's gate.
