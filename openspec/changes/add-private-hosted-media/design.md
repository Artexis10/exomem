## Context

See `proposal.md` for the problem and scope. This is a design-only change against the hosted contracts at commit `2ec65c0f`; it is not evidence about the deployed cluster.

The existing runtime has a per-vault durable media ledger in `media_jobs.py`, an on-demand child-process supervisor in `media_worker.py`, guarded canonical result commits, Tesseract/PDF/office extraction, faster-whisper ASR, and CLIP indexing. What is missing is hosted admission and remote execution with narrowly scoped data access. A new independent job system would duplicate authority and weaken existing recovery behavior.

The checked-in cell defaults allow embeddings and file watching, with two configured workers, a 5 GiB logical storage limit, a 90 MiB upload ceiling, and a 10 GiB PVC. The two-worker configuration is not evidence that two heavy model processes fit. The private-alpha capacity gate allows four user cells, including the operator's cell, and two recovery cells. CPU is shared; requests and limits are scheduling controls rather than dedicated tenant cores.

Existing controls include private Backblaze buckets with SSE, application-encrypted vault recovery archives, LUKS cell volumes, restricted cell networking, redacted application logs, and encrypted Kubernetes Secrets. Source inspection also found HTTP between cluster components, a shared volume-unlock credential, disk-backed plaintext archive scratch, SSE-only delivery/platform objects, and platform paths without a complete encryption proof. None of these findings establishes whether an uninspected live setting adds protection.

The active `simplify-hosted-launch-boundaries` change owns the gateway placement, public URL/OAuth contract, binding migration and governance enrollment. Its trusted internal HTTP boundary is explicitly strengthened here after its final transport contract is reconciled. This change must preserve its migration fences and release evidence.

## Goals / Non-Goals

**Goals:** preserve original media immediately; deliver useful document/OCR and visual retrieval on bounded CPU; offer asynchronous ASR without an always-on GPU; protect data across storage, transport and worker boundaries; make costs and service readiness independently measurable.

**Non-Goals:** a server-side reasoning model, diarization, unlimited included processing, immediate 100 GB/1 TB provisioning, a second tenant runtime, automatic price changes, or a zero-knowledge claim. This planning change does not activate features or mutate provider resources.

## Decisions

### 1. State the plaintext trust boundary precisely

The service can read authorized tenant content while serving search and processing jobs. The service operator and a compromised compute host remain inside that trust boundary. Separate tenant keys reduce accidental access and credential blast radius; a cluster administrator able to control workloads can still obtain mounted keys and plaintext. This is encrypted hosted processing, not end-to-end encryption against the operator.

Keep Cloudflare Tunnel for the first hardening phase and disclose Cloudflare and any content-bearing Vercel rewrite as processors in the actual request path. Tunnel encryption does not hide content from Cloudflare's TLS termination. A later direct HTTPS data hostname, with TLS terminated at the controlled gateway and Cloudflare DNS-only, can remove both intermediaries from content traffic; retaining a Vercel rewrite would defeat that purpose. That migration changes the public endpoint/OAuth/client compatibility boundary and is deferred to a separate approved change. Authentication, rate limits and origin protection must accompany it.

Direct HTTPS is the preferred direction for reducing content processors after launch, subject to a separately reviewed migration. It can use the existing gateway and server address; it does not inherently require another server. The comparison is:

| Route | Privacy boundary | Operational trade-off |
| --- | --- | --- |
| Existing Tunnel | Cloudflare terminates content TLS; any Vercel content rewrite is also in the path | Outbound origin connectivity and Cloudflare edge protection simplify exposure management |
| Direct HTTPS data endpoint | Content TLS terminates at the controlled gateway; DNS-only Cloudflare does not proxy those bytes | Public gateway exposure, certificate renewal, rate limits, request limits and network/application abuse protection become our responsibility |

The future migration must bypass both intermediaries on the complete content path, use a publicly trusted certificate with tested renewal, and verify real Claude/ChatGPT authentication, streaming, upload/download and revocation. DNS-only on one record is insufficient if its CNAME chain or application rewrite still traverses a proxy. Keep administrative endpoints private. Benchmark latency from client locations; removing a hop does not guarantee better network routing. Website/DNS hosting may remain separate, but a hosted browser UI still trusts its code delivery even when uploads travel directly. These comparisons do not replace the current encrypted storage, worker or operator trust requirements.

True provider-blind operation requires client-held keys and client-side processing/search, or a separately evaluated attested compute design. Neither is assumed compatible with the current generic hosted MCP product or its small budget.

### 2. Encrypt every network boundary carrying content or credentials

Use certificate-verified TLS from the tunnel connector to the gateway, gateway to routing layer, routing layer to cells, and provisioner/durability workers to cells. Include browser transfer paths, callbacks, database connections, object storage and future remote workers. Workload identity and authorization remain required in addition to encryption. Use a private CA with constrained service identities and mTLS for cluster service clients; a trusted hostname and certificate are required even on private IP space. Database connections require CA and hostname verification, not merely a URL convention.

Preserve the existing literal-loopback operator probe contract: it remains confined to the cell network namespace and never traverses a pod or host boundary. It is an explicit local exception, not evidence that the broader network is encrypted. Reject insecure redirects, proxy environment overrides and certificate-verification bypasses on credential-bearing service clients. Test every actual hop; setting an HTTPS public URL or a Cloudflare zone mode is insufficient.

Extend default-deny ingress and egress to gateway, provisioner, durability and processing workloads. Permit only required peers, resolver access and explicit provider endpoints. Place privileged CSI components in a separate namespace with restricted service accounts; ordinary workloads must not inherit privileged admission.

Bind a versioned transport profile and expected peer identities to the existing signed gateway/cell release candidate and deployment admission evidence. `private-tls-v1` is a monotonic activation requirement: a legacy HTTP gateway, routing layer or cell cannot join an activated route. Mixed versions may communicate only when all participants prove that profile. An incompatible rollback disables the affected content route; it cannot revert its requirement to HTTP. Test the old/new gateway, routing-layer and cell matrix, preserving the launch-owned custody and governance fences. The profile augments deployment proof rather than changing persistent vault ownership markers.

### 3. Protect persistent state, scratch, keys and recovery together

Cell canonical files, indexes, job ledgers, upload staging, caches, logs and temporary files remain on that cell's encrypted volume. For small bounded scratch use memory-backed temporary storage with swap disabled; larger backup/restore/database jobs need dedicated encrypted scratch volumes. A 6 GiB tmpfs on an 8 GiB node is not an acceptable blanket fix. Swap and core dumps must not spill tenant plaintext. Sensitive node paths, including K3s state, container logs and recovery staging, must be on verified encrypted storage before the strengthened at-rest guarantee is advertised. Prefer replacement-node rollout with encrypted root/runtime storage over in-place filesystem surgery; preserve the current node until restore and rollback are proved.

Replace the shared LUKS unlock passphrase with a distinct high-entropy unlock credential for each tenant's volume set. LUKS already uses volume keys; the problem is the shared unlocking credential. Start with a per-tenant StorageClass referring to a tenant-scoped CSI secret, a tractable approach at the existing cell cap. Pin and verify CSI support before implementation. Model weights may be shared read-only; tenant caches, decrypted data, vectors and job results may not.

Use separate tenant-scoped application wrapping keys for vault objects, and a separate platform-recovery key for database/control-plane backups. Fresh data keys encrypt payloads with authenticated encryption; keys are wrapped outside the object store. Preserve the matrix-owned BWS custody and existing restore authority. Workers receive neither the wrapping root nor the volume unlock credentials. Compromise of a tenant credential must not decrypt another tenant's objects.

The authority topology is explicit:

| Principal | Permitted key operation |
| --- | --- |
| Dedicated custody controller | Generate, escrow, wrap/unwrap and retire tenant/platform keys through the existing matrix-owned BWS source; validate an authoritative operation grant before releasing any data key |
| Provisioner | Request creation or recovery for an admitted tenant operation; create the cluster-scoped tenant StorageClass and deliver only that tenant's unlock secret to CSI; no export of wrapping roots |
| CSI node component | Unlock the specifically bound volume through its scoped secret reference; no object-store or application wrapping access |
| Cell and recovery/delivery execution | Receive a short-lived data key only for the grant-bound tenant/object/operation; no fleet-wide unwrap or key listing capability |
| Media worker and scheduling broker | No volume or wrapping key access; media plaintext is delivered through the cell's artifact authorization boundary |

The custody controller is a trusted platform principal, isolated from ordinary backup/media execution; its compromise remains inside the disclosed operator boundary. Requests use workload identity plus an operation grant from the existing lifecycle/custody authority, binding tenant, object, key version, purpose, release/generation and expiry. Caller-selected tenant IDs or wrapped blobs do not confer unwrap authority. Escrow includes protected tenant-to-volume identifiers and recovery bindings outside the cluster, so namespace/etcd loss does not strand retained volumes. Acceptance rebuilds a clean cluster, recovers one tenant's escrow only, rebinds its retained volume, and restores its retained objects while another tenant's data stays inaccessible.

Backblaze SSE remains a second layer. Encrypt archive bodies, manifests containing private names, delivery objects and platform snapshots before upload. Use opaque object names and content-free provider metadata. SSE-C alone is insufficient because its key is presented to Backblaze during server processing. Existing plaintext-to-provider delivery objects must be retired through retention-aware migration, not merely a new bucket default.

Replace the native SSE-only K3s snapshot upload path with a snapshot-to-encrypted-staging wrapper that application-encrypts etcd snapshots before object upload. Inventory etcd, control-plane database dumps, recovery manifests and any credentials/configuration needed for restoration. Stop the old uploader once the new path is proved; do not run an untracked second plaintext backup path. Prove restoration on a clean cluster using the encrypted snapshot and separately escrowed platform recovery authority.

Before activating the strengthened object-store privacy claim, inventory exact versions, hidden versions, delete markers and retained multipart uploads; every sensitive body must either already be application-encrypted or have its provider-readable historical version removed after lock/retention permits. Creating an encrypted replacement does not remove the old exposure. Revoke obsolete object-store credentials, prove absence of remaining provider-readable sensitive versions, and only then retire migration status. If a locked old version remains, keep that limitation explicit and do not advertise the stronger historical guarantee.

Downloads authenticate the user, acquire the existing lifecycle admission, and stream authenticated decryption from protected storage through the authorized delivery service. Verify each encrypted chunk before releasing its plaintext; validate ordered framing and completion. Do not persist a provider-readable decrypted export object or issue a presigned URL to one. Preserve portable plaintext exports at the recipient and canonical manifest integrity checks. Abort, error and cancellation paths release admission and remove local scratch.

Key migration uses a recorded prepare/prove/promote/finalize sequence: retain the old key while adding and testing the new key, prove restoration into a fresh isolated cell, then remove old unlock access. Keep historical decryption material under restricted custody for retained backups; backup expiry, object locks and key retirement must agree. Never rotate a shared key implicitly through provisioning, delete a live PVC, or weaken existing custody/enrollment fences.

### 4. Keep the cell as the only job and canonical-write authority

The execution path is:

```text
authorized upload -> original + governed pending sidecar + existing job ledger
                                       |
                              bounded resource admission
                                       |
                       artifact-scoped CPU / optional GPU job
                                       |
                           bounded untrusted result envelope
                                       |
                     cell rechecks authority and source identity
                                       |
                   existing guarded commit + durable index receipt
```

Extend existing job state with an attempt identifier, fenced lease generation, model/configuration digest and execution profile. Do not create a second canonical queue in the control plane. A shared scheduler may hold opaque scheduling hints and a separate durable spending-reservation ledger; job completion and canonical result publication remain authoritative in the cell ledger. Hints are reconstructible from cell state.

The cell issues a short-lived capability bound to the authenticated worker identity, tenant, job, attempt, lease generation, immutable artifact hash, exact processing operation, byte limit and expiry. Worker retrieval uses a dedicated authenticated endpoint and cannot accept arbitrary URLs, filesystem paths or tenant selectors. Every access checks the live lease and lifecycle admission; a signed capability alone is not proof that a deleted or revoked job is still authorized. Bounded retries use fresh transfer grants under the same fenced attempt. Workers fetch only authorized bytes and return only bounded outputs. They cannot list a vault, access other jobs, upload canonical notes or use general cell credentials.

Choose a content-bearing job broker for remote workers: worker -> broker -> cell, with independently verified TLS and worker/grant binding on both legs. The broker is explicitly a plaintext processor. It routes by verified opaque grants and authoritative job bindings, never a user-supplied cell URL; the cell is the final artifact/result authorization authority. The broker has verification material, no general cell bearer, no wrapping keys, and no tenant enumeration/listing API. It cannot mint job grants or authorize a different job. It streams bounded data without persistent plaintext or tenant data caches. Local workers can use the same cell authorization seam without the proxy.

Artifact streaming checks current lease/deletion generation before each bounded read and supports cancellation from lifecycle sealing. Read-ahead is capped at 64 KiB per leg, and cancellation checks must run at least once per second while an admitted stream is active. After revocation is observed no new source reads or retry grants are permitted; buffered bytes already released to the transport cannot be recalled. The stream is aborted within the cancellation bound, and partial worker plaintext enters tracked cleanup/quarantine. Deletion sealing waits for admitted streams to stop or records an explicit cleanup failure, rather than silently treating an admission-time check as perpetual authorization.

Use one tenant/job per sandboxed process or pod with non-root execution, read-only root filesystem, no host mounts, no Kubernetes API credentials, restricted system calls, pinned images/models and denied network egress except its job broker. Fetch model weights during a separate content-free build/cache stage. Bound pages, decoded pixels, archive expansion, audio duration, memory, CPU time, wall time and output bytes. A small compressed upload can otherwise exceed every reasonable runtime limit.

Treat results as untrusted. The cell checks schema, provenance, result limits, model/config digest, artifact identity, current governance, current lease, deletion state and duplicate completion before entering the existing mutation boundary. Expired, superseded or revoked attempts cannot commit. Commit/index ordering and crash recovery reuse `media-processing-reliability`; graph fanout failure does not re-run completed extraction. Deletion cancels running access and prevents late-result resurrection. Cleanup of worker plaintext is recorded separately from job completion; uncertain cleanup quarantines the worker from reuse.

The decisive source/lease/deletion checks happen again inside the same serialized canonical mutation/lifecycle boundary used by replacement and deletion. Reserve the fenced attempt with compare-and-swap and retain a recoverable commit intent; hold the relevant fence through sidecar publication and durable fanout receipt admission. This is a logical serialized commit with existing crash-recovery accounting, not a claim that filesystem and SQLite writes form one physical transaction. If deletion/replacement wins, the old result cannot publish; if result publication wins, subsequent deletion/replacement purges or supersedes its sidecar and vectors. Add barrier-controlled races at validation, publication and restart boundaries and verify ledger, canonical data and retained receipts recover consistently.

### 5. Add media profiles in order of value and cost

| Profile | Execution | User-visible result |
| --- | --- | --- |
| Documents and OCR | Existing PDF/office parsers and Tesseract, bounded CPU jobs | Searchable source text, provenance, and page references where extraction supports them |
| CLIP | CPU image embedding in background; warm CPU text encoder for queries | Text-to-image and image similarity retrieval alongside OCR/text search |
| Timestamped ASR | Bounded CPU jobs first; optional ephemeral GPU when measured throughput justifies it | Searchable transcript with timestamps |

Reuse the actual Python parsing paths: PyMuPDF for PDFs, with Tesseract on scanned pages; MarkItDown with its DOCX/XLSX/PPTX extras for Word, Excel and PowerPoint, using the existing office dependencies including mammoth, openpyxl and python-pptx. HTML and existing plain-text/email/calendar paths remain supported through their current adapters. Ordinary parsing uses no inference API and requires no GPU. Preserve originals because extracted Markdown is a search representation, not a lossless replacement for document layout or spreadsheet behavior.

The current `media` extra groups these parsers with faster-whisper, CTranslate2 and CUDA runtime wheels. Introduce CPU-only document/OCR dependency groups and worker images without requiring the ASR, vision or CUDA stack; preserve existing desktop/media installation compatibility. Keep the single extraction dispatch rather than duplicating converters in the control plane. Verify multi-sheet XLSX values and sheet context, DOCX tables, PPTX slide content and mixed text/scanned PDFs against fixtures. Do not execute document macros, fetch external links or claim formula recalculation; unavailable protected/unsupported formats remain preserved with an actionable status.

OCR is required for the hosted media offering. Explicitly select a processing profile through the existing owner/entitlement configuration; retain the pure-substrate rule that prose-emitting model transducers are off without that selection. Selection authorizes the pinned transducer, not instruction-following models or an external inference provider. Diarization remains disabled and has no delivery dependency.

CLIP complements OCR: OCR finds visible words; CLIP finds visual similarity and concepts. Preserve the existing vector dimensions and encoder identity when enabling the current model. Quantization or an ONNX conversion requires retrieval-quality and numerical compatibility evaluation, not an assumption that an exported model is interchangeable. The text query encoder must be warm within the admitted cell footprint; queries must not wait for a GPU to start. If this cannot fit, leave visual search pending and measure a separately isolated query service before changing the boundary. Do not duplicate full model stacks per cell without measuring total node residency.

Begin with one heavy CPU job globally and at most one running job per tenant. Use round-robin admission with bounded job duration; backlogs cannot monopolize the node. Set scheduler requests and hard container limits from measured peaks while retaining interactive and recovery headroom. Jobs pause or remain queued when headroom is unavailable. The current four-user-cell cap remains in force; embedding runtime savings do not automatically raise it.

Preservation/download and normal text recall do not require media processing to be available. Status distinguishes preserved, queued, processing, indexed, blocked by budget, unsupported and failed, without misrepresenting a queued file as searchable. Owner-facing job details can include the owner's artifact; operational logs cannot.

### 6. Make GPU execution a budgeted option, not base infrastructure

Keep paid GPU provisioning off until an operator sets a nonzero service budget and tenant allowances backed by observed receipts. A first candidate is an operator-controlled ephemeral EU L4 instance running the same worker protocol. Provider APIs offering cheap transcription remain off unless the owner explicitly selects that provider and its data policy; there is no silent fallback when local processing is busy.

Before allocating resources, atomically reserve a conservative worst-case cost across both tenant and global budgets. Include billing granularity, boot time, model loading, execution, teardown allowance, retries, storage, IP and transfer charges. Reject unknown prices and jobs without a finite maximum duration. Reconcile actual costs while retaining reservations for resources whose deletion is unconfirmed. Budget rollover must not release an unresolved resource reservation.

Give every allocation an expiry, a maximum concurrent-instance count of one initially, and an independent reaper that enumerates provider resources by opaque allocation identity. Delete compute, scratch volumes and addresses; stopping compute alone can leave charges. A provider control-plane outage can delay deletion, so admission limits are not an absolute invoice cap. In that state stop new admissions, keep costs reserved, alert the operator and retry cleanup. Provider billing alerts supplement this mechanism; they do not implement it.

### 7. Budget from net receipts and actual stored versions

The checked-in private-alpha model records EUR 3.34 net from a EUR 5 friends payment, EUR 8.49/month for CX33, EUR 0.50 IPv4, EUR 0.572 per active 10 GiB cell volume and EUR 5.80 for B2 storage/operations. These are recorded inputs, not a newly verified invoice or a guarantee about the next tax jurisdiction. It already labels the friends tier subsidized.

| Paying friends plus one operator cell | Recorded monthly infrastructure | Friends' net receipts | Operator-funded remainder |
| --- | ---: | ---: | ---: |
| 2 friends / 3 cells | EUR 16.51 | EUR 6.68 | EUR 9.83 |
| 3 friends / 4 cells | EUR 17.08 | EUR 10.02 | EUR 7.06 |

These totals exclude new compute and any incremental database, website, monitoring or support costs. Equal shares would be about EUR 5.50 and EUR 4.27 per cell respectively, also above a friend's EUR 3.34 net receipt. Charging enough to cover this entire recorded baseline would mean roughly EUR 11.49 with two paying friends or EUR 8.11 with three, under the recorded fee/tax model and before extra costs. This is arithmetic, not an approved price change. The existing capacity gate prevents assuming that more paying tenants can simply be added to the same node. A founder-funded baseline can make EUR 5 useful as an introductory marginal-cost tier, but the subsidy must be explicit.

The owner accepts evaluating EUR 5–10/month where the delivered value justifies it, with three expected paying friends and a potential fourth. For three paying friends plus the operator, the recorded fee/tax formula gives this sensitivity against the EUR 17.078 baseline:

| Gross monthly price per friend | Approximate total net receipts | Remainder after baseline |
| --- | ---: | ---: |
| EUR 5.00 | EUR 10.03 | EUR -7.05 |
| EUR 7.50 | EUR 15.70 | EUR -1.38 |
| EUR 10.00 | EUR 21.37 | EUR 4.30 |

This table computes with unrounded intermediate values; the earlier table uses the recorded rounded EUR 3.34 receipt. EUR 10 is the working recommendation for a cost-covering paid offer with functioning private document/OCR and image retrieval, while processing allowances remain bounded. EUR 4.30 is limited operating headroom, not a profit guarantee or an automatic GPU budget. No billing catalog changes are authorized by this price comparison.

Three friends plus the operator consume all four currently admitted user cells. The potential fourth friend would require a fifth user cell. Do not promise or charge for that slot until the separate capacity gate admits it with recovery headroom intact. The present attachment policy, not just RAM, binds that gate; buying a larger single server does not by itself change its volume-attachment allowance. A separate measured capacity revision can evaluate attachment reserve or multi-node placement before publishing new economics.

Storage entitlement is a ceiling, not pre-provisioned physical consumption. Specify a future 10 GB tier as exactly 10,000,000,000 logical canonical bytes, separately from GiB PVC sizing. Preserve the current 5 GiB limit until quota accounting, indexes, simultaneous upload scratch, free-space reserve and restore headroom support an increase. The 90 MiB transfer ceiling is unchanged; resumable larger uploads need a separate contract change.

Current backups are daily full archives with roughly a month of retained versions. A mostly incompressible 10 GB media vault can therefore account for about 300 GB of backup bodies before additional copies and lifecycle overlap. At the advertised B2 rate that is about USD 2.09/month, whereas one 10 GB copy is about USD 0.07. A 1 TB entitlement does not imply a USD 6.95 total storage bill when active storage and historical copies are included.

Implement tenant-scoped incremental encrypted backup objects before larger tiers: upload each new immutable content object once, reference it from encrypted snapshot manifests, and retain existing objects while any retained snapshot needs them. Use tenant-keyed lookup identifiers and opaque provider names; no cross-tenant deduplication or existence oracle. Garbage collection follows manifest reachability, Object Lock and retention, with restore verification before old full archives retire. This initially changes backup representation, not the canonical filesystem or the active cell storage architecture.

Publish all referenced objects durably before publishing a snapshot manifest. Maintain a tenant-scoped publication/GC generation fence; an object being reused by a new snapshot is pinned before it can become a GC candidate. GC revalidates reachability, generation and retention under that fence before deletion, retaining unconfirmed deletions for idempotent replay. Crashes before manifest publication leave reclaimable orphans rather than published missing objects. Test simultaneous snapshot publication and GC, interrupted deletion, Object Lock refusal and restore after replay before retiring the full-archive fallback.

### 8. Use published prices as estimates and benchmark latency locally

Sources checked 2026-09-08:

- [Hetzner price adjustment](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/): new CX33 pricing EUR 8.49/month excluding VAT/IPv4; older existing orders can differ.
- [Backblaze pricing](https://www.backblaze.com/cloud-storage/pricing): advertised USD 6.95/TB per 30 days. Free allowance is account-wide; API, transfer, retention and active storage remain separate cost inputs.
- [Backblaze SSE](https://www.backblaze.com/docs/cloud-storage-server-side-encryption): encryption covers file data, not file metadata; SSE does not provide operator-side application encryption.
- [Scaleway GPU pricing](https://www.scaleway.com/en/pricing/gpu/): L4-1-24G EUR 0.79/hour. Eight compute hours would be EUR 6.32 before extras, not eight audio hours of promised throughput.
- [Scaleway billing](https://www.scaleway.com/en/docs/instances/faq/): most GPU instances bill by minute including startup/standby; allocated storage and IPs continue billing independently. Pin the selected SKU's current terms before admission uses a price.
- [Cloudflare TLS boundary](https://developers.cloudflare.com/ssl/faq/) and [Tunnel HTTPS origins](https://developers.cloudflare.com/tunnel/troubleshooting/https-origins/): edge termination and origin certificate verification are distinct controls.
- [Cloudflare proxy status](https://developers.cloudflare.com/dns/proxy-status/): DNS-only records expose the origin address and remove Cloudflare's HTTP proxy/security features; a proxied CNAME chain can still route traffic through Cloudflare.

Retain the launch acceptance workload and thresholds from `simplify-hosted-launch-boundaries`: at least 100 warm samples and 20 cold runs from a European vantage, warm p95 initialization/tool listing <=500 ms, small durable capture/recall <=1 second, cold initialization <=2 seconds, with the specified two synthetic cells and five clients. Run the same workload with admitted OCR/CLIP/ASR background activity. These are acceptance targets, not measured results. Measure query-encoder residency, queue wait, processing duration, decoded input size and total node memory separately. GPU cold-start latency is acceptable only for asynchronous jobs.

## Risks / Trade-offs

- Shared host or cluster-admin compromise exposes active plaintext -> make this boundary explicit; use least privilege and per-tenant secrets without claiming protection against the trusted operator.
- New TLS or key configuration can interrupt a cell -> prove overlap, expiry/rotation, restoration and rollback on synthetic cells before any live rollout.
- Heavy CPU models can exhaust the small node -> admission depends on measured residency and interactive latency; preserve core service and leave work queued.
- Provider deletion outages can continue billing -> finite leases, retained reservations, an independent reaper and operator alerts; no claim of an infallible spending cap.
- Locked historical objects cannot be erased immediately -> report the retention horizon, revoke active access promptly and expire historical versions lawfully under the configured retention contract.
- Separate agents may advance launch contracts -> rebase and reconcile this change before implementation; do not bypass new custody or migration state machines.

## Migration Plan

1. Land this specification separately. Implement against the completed launch transport/custody contracts in dedicated task worktrees. No artifact in this change is evidence of live readiness.
2. Build synthetic transport, encrypted storage, key-custody and restore acceptance first. Record all data paths and actual mount/connection proof. Gate broader onboarding and privacy claims on that proof.
3. Roll out privacy changes with the deployment owner: validate certificates and key overlap, restore into a fresh cell, replace the node only after restoration is proven, and retain a compatible rollback path. Never weaken privacy silently to recover availability; disable the affected operation when protection cannot be established.
4. Enable the selected document/OCR profile for a synthetic cell, then one authorized adopter, then the admitted cohort. Add CLIP only after memory, retrieval quality and concurrent latency acceptance. Processing can be disabled independently while preserved files remain accessible through safe routes.
5. Add budgeted ASR and optional GPU execution only after crash/cleanup and cost-reservation tests pass. Keep the paid budget at zero until explicitly configured.
6. Introduce incremental backups with dual-read verification and retention-safe retirement. Raise storage entitlements only after physical capacity, cost and recovery evidence supports them. Do not increase the cell count in this change.

## Open Questions

- Exact production CPU job limits and model warm-idle durations come from the bounded acceptance benchmark; changing them must not relax the isolation, cost or latency contract.
- The final commercial price and included processing allowance remain to be selected within the discussed EUR 5–10 range using measured costs and delivered features. Paid GPU budgets remain zero until funded, and the potential fifth user cell requires a separate capacity decision.
