## Why

Hosted Exomem must preserve documents, images, and audio and make their contents useful without tying interactive performance to heavy media processing. The current code already preserves binaries and has durable media jobs, but the hosted defaults omit media processing and the infrastructure leaves gaps in transport encryption, temporary storage, and tenant key isolation. A small paid deployment needs these privacy controls and bounded OCR, visual search, and transcription costs before adding more infrastructure.

## What Changes

- Require authenticated encryption across hosted network boundaries and encrypted persistent storage for tenant data, temporary processing, recovery, and sensitive platform state.
- Separate tenant volume-unlock credentials and application encryption keys; preserve existing custody, recovery, and migration authorities.
- Keep Backblaze server-side encryption and add application encryption before every sensitive object upload, including export delivery and platform recovery data.
- Extend the existing media ledger and guarded result commit with bounded CPU execution, tenant-scoped worker capabilities, fair scheduling, and enforceable compute admission budgets.
- Make document extraction and OCR the first hosted processing profile, reusing the Python PDF/office parsers with a CPU installation independent of ASR/CUDA. Add CPU CLIP image indexing and a warm text-query encoder; add timestamped ASR through an optional, separately budgeted worker profile. Defer diarization.
- Keep original upload/download available independently of extraction, subject to existing transfer, governance, and storage limits.
- Prepare a 10 GB logical storage tier with explicit physical headroom and a tenant-scoped incremental backup design before considering 100 GB or 1 TB tiers. Do not change current limits or provision those capacities in this proposal.
- Evaluate the owner-accepted EUR 5–10 friends-tier range for three expected paying users and a possible fourth, plus the operator's cell. Keep the current four-user-cell gate and actual price changes separate from that demand forecast.
- Design a disabled-by-default direct HTTPS content endpoint on the existing node, with a restricted public edge, automatic certificates and explicit OAuth resource migration. Keep its implementation and activation behind the bounded readiness stages in this design, separate from launch activation.
- Reconcile the four-user signed capacity policy with provisioner reservations before evaluating a fifth cell. A permissive code path is not capacity evidence and must not silently raise the supported cohort.
- Stage implementation after the launch-owned gateway and custody changes settle. This proposal changes specifications only and does not authorize a deployment, key rotation, purchase, or launch cutover.

## Capabilities

### New Capabilities

- `hosted-data-protection`: Defines the hosted plaintext trust boundary, encrypted storage and scratch requirements, tenant key custody, recovery/export protection, retention, and deployment evidence.
- `hosted-media-execution`: Defines hosted preservation independent of processing, reuse of the media ledger, constrained workers, CPU/GPU profiles, tenant fairness, cost admission, and storage growth gates.

### Modified Capabilities

- `hosted-gateway-contract`: Requires authenticated, certificate-verified encrypted forwarding across hosts and pods, including direct transfer and recovery paths, without changing principal routing or transfer authority.

## Impact

Implementation will touch media orchestration and extraction under `src/exomem/`, hosted transfer and durability services, the provisioner, Helm network/storage/workload configuration, Terraform recovery configuration, and the existing hosted status surface. Control-plane changes in the sibling website repository must consume the same contracts rather than introduce another job or entitlement authority.

The active `simplify-hosted-launch-boundaries` change owns the gateway move, public URL and OAuth compatibility, binding migration, custody enrollment, and release activation. This change depends on its final contracts and strengthens its currently trusted internal HTTP boundary in a later coordinated rollout. It does not edit that change or its implementation. Existing tenant isolation, media processing reliability, secret custody, database budget, portability, and mutation safety specifications remain authoritative.
