## ADDED Requirements

### Requirement: Provisioner wire protocols authenticate and bound every call
The provisioner SHALL expose the 14 actions expected by Substrate at `/cells/<action>` and SHALL select the closed v1 or v2 request and response contract from the exact provisioner protocol header. Every request MUST use HTTPS, the independent provisioner bearer, JSON content type, and an idempotency key. It SHALL reject redirects, oversized request/response bodies, unsupported or mixed-version fields, invalid identifiers, unsupported protocol headers, and unauthenticated calls without operation or provider side effects. During expansion it SHALL dual-serve fresh v1 only for exact verified entries in the bounded authoritative legacy runtime catalog and fresh v2 for the locked forward target. After contraction it SHALL accept fresh v2 operations, exact non-final v1 replay only against a retained verified catalog entry, and exact already-final v1 replay without a catalog entry after current-fence and v1-model validation because that replay performs no runtime or provider effect.

#### Scenario: Invalid protocol is rejected before mutation
- **WHEN** a provision request has valid JSON and bearer credentials but the wrong provisioner protocol header
- **THEN** the request receives a terminal contract error and no namespace, operation, or provider resource is created

#### Scenario: Persisted v1 survives contraction
- **WHEN** an exact v1 action, idempotency key, canonical body, stored wire discriminator, current fence, and retained runtime catalog entry match a non-final persisted operation after contraction
- **THEN** the provisioner resumes that operation under the unchanged v1 schema without admitting a fresh v1 operation

#### Scenario: Final v1 replay outlives its catalog entry
- **WHEN** an exact current-fence replay matches an already-final v1 operation whose runtime unit is no longer cataloged
- **THEN** the provisioner validates and returns the persisted v1 result without runtime or provider effects

### Requirement: Operations are durably idempotent and tenant-fenced
Before performing side effects, the provisioner SHALL persist the action, canonical request hash, idempotency key, tenant ID, operation/checkpoint, monotonic fence generation, and progress. Every durable Kubernetes namespace/release/PVC/PV/route, HCloud volume, and B2 export/backup side effect SHALL also carry or authenticate immutable opaque tenant ID, cell/candidate ID, operation ID, and fence generation outside PostgreSQL. Replaying the same key and body SHALL resume or return the same result. Reusing a key with changed input SHALL conflict. A lower fence MUST NOT mutate, recreate, or destroy resources after a higher fence is observed. These guarantees SHALL survive provisioner and database-client restarts.

#### Scenario: Identical provision replay returns one cell
- **WHEN** the same provision action, body, fence, and idempotency key are submitted twice
- **THEN** both observations converge on the same provider reference and endpoint with no duplicate namespace or volume

#### Scenario: Altered idempotency replay conflicts
- **WHEN** an existing idempotency key is reused with a changed cell ID, credential, policy, or action
- **THEN** the provisioner returns a terminal conflict and leaves the original operation unchanged

#### Scenario: Stale fence cannot resurrect deleted resources
- **WHEN** a lower-fence provision or resume request arrives after a higher-fence destroy request
- **THEN** it is rejected and no tenant resource is recreated

#### Scenario: Provider metadata outranks a restored database
- **WHEN** PostgreSQL is restored behind a provider resource carrying a higher fence
- **THEN** reconciliation computes that provider-observed maximum before accepting mutations and rejects a lower-fence replay

### Requirement: Long actions use a non-attempt-consuming pending/final contract
Each of the existing 14 action responses SHALL be a strict union of a pending checkpoint or the action's existing final proof. Pending SHALL include the durable operation/checkpoint and retry delay, SHALL be replayed on the same endpoint and idempotency key, and SHALL move Substrate to a waiting state without consuming lifecycle failure attempts. Transport and real action failures SHALL retain bounded retry/terminal behavior.

#### Scenario: Work remains pending beyond six cron runs
- **WHEN** a restore or retained deletion returns pending for more than six reconciliation cycles
- **THEN** Substrate remains in a non-terminal waiting checkpoint and later accepts the exact final proof

#### Scenario: Both sides restart while pending
- **WHEN** Substrate and the provisioner restart during a queued export, restore, or destroy
- **THEN** replay of the original action/key resumes from durable progress without duplicate side effects

### Requirement: Exceptional init-retry recovery is exact, private, and transactional
The provisioner SHALL expose an operator-only helper inside the signed image that can reopen only an exact `PROVISION / ERROR / failed / PROVISIONER_PROVIDER_METADATA_CONFLICT` operation to `PROVISION / PENDING / volume-owned`. It MUST NOT expose a general operation editor or HTTP recovery endpoint, accept caller-selected lifecycle fields, or receive the confidential operation identifier through command-line arguments. Before mutation it SHALL validate the exact PostgreSQL role/schema/revision; old operation state; tenant fence and lock conflicts; decrypted canonical request and request hash; immutable tenant, cell, provider-operation, protocol, runtime-target, and fence identity; exact durable resource set and authenticated references; one unreleased USER reservation; and two stable non-terminating live provider observations. It SHALL preserve the request, identities, fences, resources, reservation, progress, retry interval, claim generation, result fields, and creation timestamp. The single compare-and-swap transition and one append-only content-free recovery receipt SHALL commit in the same PostgreSQL transaction or neither SHALL commit.

#### Scenario: Exact historical false negative resumes in place
- **WHEN** the stored operation has the exact eligible terminal shape, every durable and live identity matches, no conflicting claim or fence exists, and the second observation is stable
- **THEN** one transaction records the immutable content-free receipt and reopens that same operation at `PENDING / volume-owned` without creating or replacing any resource or reservation

#### Scenario: Recovery mismatch is a hard no-op
- **WHEN** any state, request, fence, resource, reservation, runtime-target, live identity, termination, or compare-and-swap invariant differs
- **THEN** the helper returns a fixed refusal and leaves the operation, receipt table, resources, and reservation unchanged

#### Scenario: Repeated recovery cannot reopen again
- **WHEN** the helper is invoked after the transition committed or after another actor progressed the operation
- **THEN** it returns the verified existing receipt or `already-progressed` without another mutation

### Requirement: Provision creates one fixed isolated cell
Provision SHALL create a namespace and versioned Helm release with one single-replica StatefulSet, one 10 GiB encrypted PVC, one ClusterIP Service, one no-API ServiceAccount, private Secrets, ResourceQuota, LimitRange, restricted Pod Security labels, default-deny policies, and restricted routes. Kubernetes quota SHALL permit exactly that one 10 GiB claim and deny a second claim; the separate 5 GiB application entitlement MUST NOT be encoded as a 5 GiB PVC storage quota. It SHALL use only an opaque immutable cell identifier in resource names/labels, preserve the original cell ID in runtime configuration, and SHALL NOT store a person's name or email.

The cell SHALL use invariant absolute vault/state/log paths, no symlink components, mode `0700`, a non-root UID, a read-only root filesystem where validated, bounded temporary space, 128 MiB rotating logs, 5 GiB storage entitlement, 90 MiB upload payload, worker count zero, and no semantic/media/vision/diarization/file-watcher grants.

Provision SHALL NOT create a route or return final success until the bound PV `volumeHandle` and location are durably recorded and the HCloud tenant/cell/operation/fence labels are independently verified. Replay after a crash in that interval SHALL adopt the original volume.

Before either the routine lifecycle driver or the dedicated volume-registration driver performs a PROVISION effect, the worker SHALL revalidate the signed live receipt and fresh local observation and SHALL create or find the exact active reservation for the claimed internal operation, immutable tenant/cell/resource/class/provider-operation/fence identity, and current claim. Every observed active reservation SHALL appear in exactly its reserved USER or RECOVERY class before idempotent return or limit evaluation. Namespace creation and initial Helm installation SHALL independently require that exact active reservation. Reservation release SHALL occur only in the same fenced transaction as final provider-proved DISCARD or DESTROY completion; pending, failure, retry exhaustion, claim expiry, and provision completion SHALL retain it. Older reservations SHALL release normally. An equal-fence release SHALL be allowed only for DISCARD after the final three-true provider proof selects the exact authenticated tenant/cell active reservation, validates its deterministic `cell_resource_name(cell)`, and proves `reserving_provider_operation_id` equals the DISCARD operation's external operation ID; this proves cleanup of the same logical candidate. Equal-fence DESTROY, equal-fence DISCARD for another provider operation, and any newer reservation SHALL roll back completion without release or destructive history. Every proof-valid destructive completion SHALL also write immutable history under the capacity-ledger lock even when no active reservation exists: DISCARD fences its authenticated tenant/cell and DESTROY fences its authenticated tenant. An equal-or-newer destructive fence SHALL block admission, while a genuinely later PROVISION fence remains eligible.

#### Scenario: Fresh cell initializes once
- **WHEN** a new provision action binds an empty PVC
- **THEN** the supported runtime initializer creates matching binding markers and repeated initialization is a no-op

#### Scenario: Binding mismatch fails closed
- **WHEN** a replacement pod changes the cell ID or any bound absolute root path
- **THEN** the runtime remains unready and reports a content-free binding error rather than adopting the volume

#### Scenario: Second PVC is denied without blocking the cell PVC
- **WHEN** the chart requests its declared 10 GiB PVC and a later workload requests another claim
- **THEN** the declared cell PVC is admitted, the second claim is denied by quota, and the 5 GiB application entitlement remains enforced inside Exomem

#### Scenario: Volume registration resumes after a worker crash
- **WHEN** routine provisioning has committed a reservation and reached `volume-registration-required` before the privileged worker restarts
- **THEN** the volume worker revalidates live evidence, finds the same exact active reservation idempotently, and only then resumes its driver effect

#### Scenario: Final destruction releases reserved capacity
- **WHEN** DISCARD proves exactly compute/storage/key destruction at a newer fence or the equal-fence reservation of the same provider operation and deterministic candidate, or DESTROY additionally proves all tenant resources destroyed at a strictly newer fence
- **THEN** operation completion, the targeted reservation release, and immutable destructive history commit atomically while the historical reservation row remains

#### Scenario: Destruction completes before admission
- **WHEN** proof-valid DISCARD or DESTROY at a fence commits before an equal-or-older PROVISION reservation exists
- **THEN** immutable destructive history blocks that admission, while pending, failed, malformed-proof, or rolled-back destruction leaves no such fence

#### Scenario: Reservation wins the equal-fence race
- **WHEN** an equal-fence PROVISION reservation commits before DESTROY, or before DISCARD for a different provider operation, completes
- **THEN** destructive completion rolls back rather than releasing or fencing the equal-or-newer reservation

### Requirement: Provisioner health proves the exact runtime admission contract
V1 health SHALL preserve the existing authenticated live, ready, and gateway-contract probes and flattened response. V2 health SHALL additionally call the selected authenticated agent-contract route and SHALL return a nested `runtimeIdentity` containing only the observed release, Hosted protocol, agent profile, gateway digest, command fingerprint, and schema digest after all six match the stored target. Compatibility and client-package lineage SHALL remain Substrate-owned and MUST NOT be reported as cell observations. Neither version may substitute TCP success, OAuth metadata, Helm status, a partial contract, or locally assumed values for runtime readiness.

#### Scenario: Contract drift blocks binding
- **WHEN** the cell image reports a release, Hosted protocol, profile, gateway digest, command fingerprint, or schema digest different from the stored lifecycle target
- **THEN** health/binding fails closed and the cell is not routed

#### Scenario: Broken runtime is destroyed
- **WHEN** a fenced discard or destroy targets a cell whose live contract cannot be observed
- **THEN** static target fencing remains enforced but the destructive recovery path does not require successful runtime health

### Requirement: Lifecycle actions preserve runtime ordering
Quiesce SHALL reject new mutations and drain active work. Stop SHALL quiesce before scaling compute to zero without deleting storage. Resume SHALL start compute if needed, resume the runtime, and require later health admission. Rotate-credential SHALL stage active/pending overlap, prove pending-token health, promote, finalize, and prove the former token rejects. Seal SHALL be terminal and SHALL run only after routing stop and drain.

#### Scenario: Stop preserves storage and admission ordering
- **WHEN** an active cell is stopped
- **THEN** new writes are rejected and active work drains before replicas reach zero, while the PVC and provider volume remain

#### Scenario: Credential overlap is proven
- **WHEN** credential rotation completes
- **THEN** the pending token was accepted during overlap, the promoted token remains accepted, and the previous token is independently rejected

#### Scenario: Maintenance release preserves Lease ownership
- **WHEN** a worker releases a Kubernetes maintenance Lease after completing its guarded work
- **THEN** deletion carries the unchanged owned Lease UID and resource-version preconditions through the installed client contract, and a replaced or foreign Lease is not deleted

### Requirement: Export and restore use portable runtime contracts
Export SHALL call the quiesced cell export API with truthful routing-stopped assertion, stream and verify archive/manifest/size, envelope-encrypt provider output, persist opaque export/release references, and release the local checkpoint exactly once. Restore SHALL run the supported offline helper against a stopped empty candidate, validate/decrypt/prepare/publish atomically, exclude source hosted state, recreate candidate bindings, rebuild derived state, and require authenticated readiness.

#### Scenario: Restore changes binding but preserves knowledge
- **WHEN** an export from one cell is restored into a different candidate cell
- **THEN** canonical knowledge matches the source while every hosted binding and credential identifies only the candidate

### Requirement: Discard and destroy produce independently verified proofs
Discard SHALL remove only the targeted failed candidate's compute, storage, route, and keys. Destroy SHALL accept tenant ID without relying on one provider reference, enumerate active and orphan resources from external registry plus Kubernetes/HCloud/B2 labels and prefixes, and return `computeDestroyed`, `storageDestroyed`, `keysDestroyed`, and `tenantResourcesDestroyed` only after independent absence checks and retention obligations are complete.

#### Scenario: Candidate discard preserves active cell
- **WHEN** a failed restore candidate is discarded while the tenant has an active cell
- **THEN** only candidate resources are absent and the active cell remains ready

#### Scenario: Tenant destroy discovers orphan resources
- **WHEN** tenant destroy runs with an active cell, orphan candidate, retained volume, route, pending credential, export, and recovery backup
- **THEN** it remains pending through retention and returns all true proofs only after every item is independently absent

### Requirement: Deletion authority selects only the live destructive claim
The deletion worker SHALL acquire authority only from the singleton operation matching the authenticated tenant and external operation ID, an action in the explicit `discard` or `destroy` set, the caller's fence, live `claimed` state, exact claim token, and an unexpired lease. It SHALL fail closed if more than one operation is eligible and SHALL preserve the `TenantFence -> Operation` lock order. Completed maintenance or destructive history rows sharing identifiers MUST NOT grant, shadow, or delay authority.

#### Scenario: Maintenance history cannot shadow live destroy authority
- **WHEN** completed `quiesce`, `seal`, or `discard` rows share tenant and external-operation identity with one live claimed `destroy`
- **THEN** the worker selects the destroy claim and never treats an incidental historical row as authoritative

#### Scenario: Ambiguous destructive claims fail closed
- **WHEN** more than one live destructive row satisfies the complete authority predicate
- **THEN** no claim is selected and no deletion side effect occurs

### Requirement: Provisioner privilege is bounded by platform policy
Routine provisioner RBAC SHALL NOT permit node, CRD, PV mutation, cluster-role/binding, admission-policy, or unrelated platform-namespace changes. A separate volume lifecycle worker SHALL receive HCloud and narrowly scoped PV recovery privileges only for its job. Platform-owned validation SHALL reject unapproved images, privileged/host namespace settings, hostPath, and cross-cell Secret/PVC references.

#### Scenario: Compromised chart values cannot mount another cell
- **WHEN** a provisioner request or rendered cell release references another namespace's Secret/PVC or a privileged hostPath workload
- **THEN** admission rejects it even if provisioner-side validation is bypassed
