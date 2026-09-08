## 1. Reconcile launch and establish acceptance fixtures

- [ ] 1.1 Reconcile the final `simplify-hosted-launch-boundaries` gateway, binding, transfer and custody contracts before implementation; verify a written dependency map in this design names the owning interfaces and preserves their release/migration fences.
- [ ] 1.2 Add synthetic two-tenant fixtures for privacy, encrypted recovery and media jobs before wiring services; verify cross-tenant failures and inspectable content-free evidence without using live tenant data.
- [ ] 1.3 Record baseline CPU, memory, storage and launch latency acceptance on the chosen synthetic deployment; verify the receipt distinguishes measured results, provider prices and unverified assumptions.
- [ ] 1.4 Reproduce the four-user contract versus six-user reservation mismatch with signed synthetic receipts through `LiveCapacityAdmission`, add red-first observed-plus-reserved and concurrent last-slot tests, then restore consistent enforcement of the existing approved policy; verify recovery/orphan accounting and existing reservation integrity without raising limits or deleting cells.
- [ ] 1.5 Evaluate five synthetic user cells with platform/direct-edge demand, admitted recovery and one heavy-job contender; record whole-node memory, OOM/eviction, latency, provider attachment and cost evidence. Keep fifth-cell admission blocked unless a separate reviewed capacity revision is justified; do not run load against the launch session or buy resources for this evaluation implicitly.

## 2. Encrypt transport and isolate platform services

- [ ] 2.1 Add failing transport-policy tests for HTTP across pods, wrong/expired certificates, redirects and proxy overrides, then implement verified TLS policy for gateway, transfer, lifecycle, recovery and worker clients; verify the scoped hosted transport suite and unchanged literal-loopback probe behavior.
- [ ] 2.2 Wire service certificates and workload identities through `infra/helm/platform` and `infra/helm/cell`; verify every synthetic hop, trust rejection and certificate rotation with actual connections rather than rendered manifests alone.
- [ ] 2.3 Enforce verified database server identity in the control-plane database client; verify missing CA/hostname checks fail before credentials are transmitted and valid production-form connections succeed against a synthetic endpoint.
- [ ] 2.4 Add platform default-deny policies and separate privileged CSI admission/service accounts; verify an allowed-peer matrix with positive and negative network probes and no loss of required DNS, durability or provisioning behavior.
- [ ] 2.5 Bind the transport profile and peer identities to signed deployment admission evidence; verify all old/new gateway, routing-layer and cell combinations and rollback fail closed when they cannot preserve the activated TLS requirement.
- [ ] 2.6 After independent design critique, add a disabled direct edge with an explicit public route allowlist, separate private transfer listener and no Kubernetes discovery/token; prove SNI/Host, encoded/private paths, forwarding identity, method, grant and network isolation with the pinned Traefik binary and synthetic backends before any public exposure.
- [ ] 2.7 After task 1.1 resolves the signed publisher/enrollment interfaces, implement the bounded Exomem artifact-producer change followed by the Substrate import/enrollment change, separate the direct resource identity from the existing OAuth issuer and publish an immutable direct candidate with matching signed client package/archive/OAuth-configuration evidence. Verify exact resource agreement through token admission, candidate lookup and client evidence; retain one live candidate per profile and prove a clean endpoint switch with legacy operator-test grant revocation, fresh direct authorization, metadata and canceled/expired test transfers. Preserve vault data/custody; do not build customer/session migration or legacy rollback publication machinery for the test-only deployment.
- [ ] 2.8 Add automatic TLS-ALPN certificate lifecycle, encrypted platform certificate state and signed direct-profile admission; verify content-disabled bootstrap, renewal, expiry, state loss, restored issuance, rate/body/connection limits and compatible rollback. Keep public activation blocked on actual private-TLS, encrypted-storage, client, resource and deployment-owner evidence.

## 3. Close persistent storage and key-custody gaps

- [ ] 3.1 Add unsafe scratch-placement tests, then route cell and durability/database/export staging to bounded memory or encrypted scratch with swap/dump protection; verify all success, cancellation and failure paths leave no plaintext on unencrypted filesystems.
- [ ] 3.2 Implement encrypted node/runtime storage through a replacement-node deployment path; verify actual root/runtime/log mount encryption, restart recovery and restore into a fresh synthetic cell before scheduling tenant content.
- [ ] 3.3 Implement the dedicated custody-controller operation-grant boundary and external recovery bindings; verify least-privilege create/unwrap/retire cases and clean-cluster recovery of one retained tenant volume and archive without another tenant's keys.
- [ ] 3.4 Verify the pinned CSI release supports tenant-specific StorageClass secret references, then provision distinct tenant unlock credentials through the existing custody source; verify tenant A's credential cannot unlock tenant B's volume and fresh provisioning cannot rotate existing credentials.
- [ ] 3.5 Implement resumable key prepare/prove/promote/finalize migration with retained historical custody; verify interrupted transitions, fresh restore, rollback and old-key retirement without deleting live volumes or bypassing governance enrollment.
- [ ] 3.6 Extend application encryption to delivery objects, private manifests and platform recovery snapshots using separate tenant/platform wrapping scopes; verify provider read credentials alone cannot decrypt fixtures and metadata contains no private filenames or paths.
- [ ] 3.7 Replace native SSE-only platform uploads with application-encrypted etcd/control-plane snapshot staging; verify clean-cluster restore and exact-version inventory, including hidden versions, multipart uploads and obsolete credentials, before activating the stronger object-store claim.
- [ ] 3.8 Replace provider-readable decrypted export staging with authenticated streaming delivery; verify chunk corruption, reordering, truncation, authorization, disconnect cleanup and portable canonical round-trip behavior.
- [ ] 3.9 Inventory historical objects and key versions and implement retention-aware retirement; verify locked snapshots stay restorable until expiry and privacy/deletion status does not claim immediate erasure of retained history.

## 4. Extend media execution authority

- [ ] 4.1 Add pure-logic tests for fenced attempts, immutable inputs and duplicate completion, then extend `media_jobs.py` and supervisor integration; verify restart recovery and reconstructed scheduling hints use the existing ledger as completion authority.
- [ ] 4.2 Implement worker-identity-bound, artifact-scoped transfer/result grants and live lease checks; verify tampering, replay, cross-tenant access, expiry, revocation, replaced input and deletion sealing before bytes or existence are disclosed.
- [ ] 4.3 Add a restricted one-job execution sandbox with pinned images/models, encrypted scratch and decoded input/resource limits; verify archive/pixel/duration exhaustion, denied arbitrary network/path access and no general cell/Kubernetes credentials in the worker.
- [ ] 4.4 Wire bounded result validation into the existing governed commit and durable fanout receipt path; verify stale/duplicate results, malformed provenance, crash around commit and graph fanout failure without extraction reruns or deleted-content resurrection.
- [ ] 4.5 Implement cleanup receipts and quarantine for uncertain worker cleanup; verify deletion and cancellation revoke access promptly and a failed cleanup cannot lead to another tenant using the same unverified sandbox.
- [ ] 4.6 Wire the content-bearing broker with cell-enforced job authority and active-stream cancellation; verify grant expansion/enumeration refusal, bounded stream revocation and partial-plaintext cleanup, then run barrier-controlled deletion/replacement/result races through commit and restart.

## 5. Enable bounded CPU media profiles

- [ ] 5.1 Add global/tenant resource admission and bounded round-robin scheduling, initially one heavy job globally; verify fairness, crash recovery and preservation of interactive/recovery reserves under a multi-tenant backlog.
- [ ] 5.2 Enable explicit hosted document/OCR profiles over existing extraction engines; verify scanned PDFs, office documents and images preserve originals and produce governed searchable text with available page provenance. Split CPU document/OCR dependencies from ASR/CUDA installation while preserving existing desktop/media extras, and prove extraction in the resulting CPU-only image.
- [ ] 5.3 Enable CPU CLIP indexing and a compatible warm text-query encoder behind measured admission; verify retrieval quality, vector identity, cold/warm memory and that queries never depend on GPU boot. Keep the profile disabled if acceptance fails.
- [ ] 5.4 Add bounded timestamped CPU ASR using the pinned selected profile; verify supported audio, missing dependencies, corrupt input and completion without diarization or reasoning-model calls.
- [ ] 5.5 Expose preserved/queued/processing/indexed/budget-blocked/failed status through the existing product surfaces; verify core upload/download and text recall while all media processing is unavailable, and inspect any changed UI through the required browser acceptance procedure.
- [ ] 5.6 Verify document fidelity with multi-sheet XLSX, DOCX tables, PPTX slides and mixed text/scanned PDF fixtures; prove useful structural context, original-byte preservation, truthful unsupported/protected-file status and no macro execution, external-link fetching or inference API calls.

## 6. Add optional paid execution with cost admission

- [ ] 6.1 Implement a durable cost-reservation ledger with a fake provider before real provisioning; verify concurrent tenant/global admission, unknown prices, retries, ancillary charges, budget rollover and unresolved allocation reservations.
- [ ] 6.2 Implement a disabled-by-default ephemeral GPU adapter using the same worker protocol and pinned provider SKU/region terms; verify finite lifetime and no allocation with a zero budget using provider simulations before an explicitly funded synthetic trial.
- [ ] 6.3 Implement an independent allocation reaper and content-free billing alerts; verify worker/controller crashes, lost provider acknowledgments, failed deletion and orphaned disks/IPs retain reservations and stop unsafe new admissions.
- [ ] 6.4 Benchmark end-to-end ASR cost and latency including boot/load/cleanup; verify a recorded comparison with CPU execution and leave paid execution disabled unless the measured benefit and available receipts fund it.

## 7. Make storage growth affordable and recoverable

- [ ] 7.1 Add explicit logical-byte accounting and physical-headroom admission while preserving the current transfer ceiling; verify that a 10,000,000,000-byte entitlement cannot activate on insufficient physical storage and that no 100 GB/1 TB allocation is created by a quota setting.
- [ ] 7.2 Implement tenant-scoped incremental encrypted backup objects and snapshot manifests behind dual-read compatibility; verify unchanged object reuse, tenant isolation, corruption detection and canonical round-trip restoration.
- [ ] 7.3 Add reachability/retention-aware garbage collection and old-archive retirement; verify deleted/current/locked snapshot cases and retained key availability before removing any historical representation.
- [ ] 7.4 Refresh the existing capacity/economics evidence with actual stored versions, net receipts and incremental provider costs; verify the EUR 5–10 price comparison for three paying friends plus the operator, retain the four-user-cell gate for a possible fourth friend, and do not increase capacity, catalog prices or paid budgets implicitly.
- [ ] 7.5 Fence object/manifest publication against reachability-based garbage collection; verify concurrent new snapshots, orphan recovery, interrupted GC and Object Lock failures remain restorable before large-tier admission or full-archive retirement.

## 8. Verify and coordinate activation

- [ ] 8.1 Run the affected module/app suites during implementation and the required full product, provisioner, Helm/Terraform and control-plane checks at delivery boundaries; verify no new failures against recorded baselines and run `openspec validate --all --strict` plus public-artifact validation.
- [ ] 8.2 Perform an independent security review and deployed synthetic privacy/restore audit; verify actual TLS identities, mounts, key separation, redaction, provider-visible ciphertext and negative worker/network cases before making privacy claims or broader onboarding.
- [ ] 8.3 Repeat the launch latency workload with admitted OCR, CLIP and ASR activity; verify thresholds, recovery headroom, tenant fairness and resource receipts before enabling each profile for an authorized adopter. For direct ingress, interleave equivalent authenticated legacy/direct runs and separately record warm, cold-transport, cold-service, streaming, browser-transfer and real-connector outcomes under Decision 8; no speedup claim from unauthenticated probes or changed backend conditions.
- [ ] 8.4 Coordinate live activation and rollback with the launch/deployment owner; verify the final endpoint, OAuth, release, binding and custody evidence remains valid, and that a protection failure disables the affected operation without silently weakening security.
- [ ] 8.5 Update operational/privacy documentation from verified deployment behavior, then synchronize and archive completed OpenSpec scope through the standard workflow; verify required tasks have implementation, test and delivery evidence before checking them complete.
