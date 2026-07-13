## ADDED Requirements

### Requirement: Provisioner v1 authenticates and bounds every call
The provisioner SHALL expose the 14 `exomem-cell-provisioner.v1` actions expected by Substrate at `/cells/<action>`. Every request MUST use HTTPS, the independent provisioner bearer, exact protocol header, JSON content type, and an idempotency key. It SHALL reject redirects, oversized request/response bodies, unsupported fields, invalid identifiers, and unauthenticated calls without provider side effects.

#### Scenario: Invalid protocol is rejected before mutation
- **WHEN** a provision request has valid JSON and bearer credentials but the wrong provisioner protocol header
- **THEN** the request receives a terminal contract error and no namespace, operation, or provider resource is created

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

### Requirement: Provision creates one fixed isolated cell
Provision SHALL create a namespace and versioned Helm release with one single-replica StatefulSet, one 10 GiB encrypted PVC, one ClusterIP Service, one no-API ServiceAccount, private Secrets, ResourceQuota, LimitRange, restricted Pod Security labels, default-deny policies, and restricted routes. Kubernetes quota SHALL permit exactly that one 10 GiB claim and deny a second claim; the separate 5 GiB application entitlement MUST NOT be encoded as a 5 GiB PVC storage quota. It SHALL use only an opaque immutable cell identifier in resource names/labels, preserve the original cell ID in runtime configuration, and SHALL NOT store a person's name or email.

The cell SHALL use invariant absolute vault/state/log paths, no symlink components, mode `0700`, a non-root UID, a read-only root filesystem where validated, bounded temporary space, 128 MiB rotating logs, 5 GiB storage entitlement, 90 MiB upload payload, worker count zero, and no semantic/media/vision/diarization/file-watcher grants.

Provision SHALL NOT create a route or return final success until the bound PV `volumeHandle` and location are durably recorded and the HCloud tenant/cell/operation/fence labels are independently verified. Replay after a crash in that interval SHALL adopt the original volume.

#### Scenario: Fresh cell initializes once
- **WHEN** a new provision action binds an empty PVC
- **THEN** the supported runtime initializer creates matching binding markers and repeated initialization is a no-op

#### Scenario: Binding mismatch fails closed
- **WHEN** a replacement pod changes the cell ID or any bound absolute root path
- **THEN** the runtime remains unready and reports a content-free binding error rather than adopting the volume

#### Scenario: Second PVC is denied without blocking the cell PVC
- **WHEN** the chart requests its declared 10 GiB PVC and a later workload requests another claim
- **THEN** the declared cell PVC is admitted, the second claim is denied by quota, and the 5 GiB application entitlement remains enforced inside Exomem

### Requirement: Provisioner health proves the exact runtime admission contract
Health SHALL call the authenticated private cell live, ready, and contract routes and SHALL return the exact flattened identity, protocol, release, service-authentication, mutation-authority, read/write admission, worker-policy, and reason fields parsed by Substrate. It MUST NOT substitute TCP success, OAuth metadata, or Helm status for runtime readiness.

#### Scenario: Contract drift blocks binding
- **WHEN** the cell image reports a release, protocol, command semantics, or digest different from the frozen Substrate fixture
- **THEN** health/binding fails closed and the cell is not routed

### Requirement: Lifecycle actions preserve runtime ordering
Quiesce SHALL reject new mutations and drain active work. Stop SHALL quiesce before scaling compute to zero without deleting storage. Resume SHALL start compute if needed, resume the runtime, and require later health admission. Rotate-credential SHALL stage active/pending overlap, prove pending-token health, promote, finalize, and prove the former token rejects. Seal SHALL be terminal and SHALL run only after routing stop and drain.

#### Scenario: Stop preserves storage and admission ordering
- **WHEN** an active cell is stopped
- **THEN** new writes are rejected and active work drains before replicas reach zero, while the PVC and provider volume remain

#### Scenario: Credential overlap is proven
- **WHEN** credential rotation completes
- **THEN** the pending token was accepted during overlap, the promoted token remains accepted, and the previous token is independently rejected

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

### Requirement: Provisioner privilege is bounded by platform policy
Routine provisioner RBAC SHALL NOT permit node, CRD, PV mutation, cluster-role/binding, admission-policy, or unrelated platform-namespace changes. A separate volume lifecycle worker SHALL receive HCloud and narrowly scoped PV recovery privileges only for its job. Platform-owned validation SHALL reject unapproved images, privileged/host namespace settings, hostPath, and cross-cell Secret/PVC references.

#### Scenario: Compromised chart values cannot mount another cell
- **WHEN** a provisioner request or rendered cell release references another namespace's Secret/PVC or a privileged hostPath workload
- **THEN** admission rejects it even if provisioner-side validation is bypassed
