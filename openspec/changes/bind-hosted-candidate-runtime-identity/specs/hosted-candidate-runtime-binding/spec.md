## ADDED Requirements

### Requirement: Provisioner v2 binds every cell operation to one runtime target

The provisioner SHALL dual-serve the existing `/cells/<action>` routes under exact `exomem-cell-provisioner.v1` and `exomem-cell-provisioner.v2` headers. Every cell-scoped v2 request MUST contain exactly one strict `runtimeTarget` with `releaseVersion`, Hosted `protocolVersion`, `agentProfile`, `gatewayContractDigest`, `commandFingerprint`, and `schemaDigest`. Digests MUST be lowercase 64-character hexadecimal values. The v2 target MUST NOT contain candidate IDs, source commits, image references, compatibility digests, client package/archive locks, plugin or OAuth provenance, or a generic contract-identity object. The context-only `export-delete`, `export-download`, and tenant `destroy` actions SHALL use explicit target-free v2 models and MUST NOT invent a runtime target.

#### Scenario: Complete v2 target is admitted

- **WHEN** an authenticated bounded v2 request carries the six-field target matching the deployment lock
- **THEN** the request is validated under the v2 action schema before operation or provider mutation

#### Scenario: Cross-authority field is supplied

- **WHEN** a v2 request adds a candidate ID, image reference, compatibility value, package lock, or unknown field
- **THEN** admission fails closed before an operation or provider resource is created

#### Scenario: Runtime protocol is confused with wire protocol

- **WHEN** the outer protocol header is v2 and the runtime target names the supported Hosted runtime protocol `1`
- **THEN** the provisioner treats those as independent version axes and does not rewrite private `/private/exomem/v1/...` routes or Helm runtime protocol values

#### Scenario: Context-only action uses v2

- **WHEN** an export-reference or tenant-destroy operation uses the v2 header
- **THEN** its closed action body remains target-free while its stored wire discriminator still prevents cross-version replay

### Requirement: Health reports a fully observed matching runtime identity

V2 health SHALL authenticate and inspect the cell live, ready, full gateway-contract, and selected agent-contract routes. It SHALL preserve the existing `live`, `ready`, `cellId`, service-authentication, mutation-authority, read/write-admission, worker-policy, and reason fields while replacing the flattened release/protocol identity with `runtimeIdentity`. It SHALL derive that object with exactly the same six fields as `runtimeTarget` only after every observed value matches. TCP reachability, Helm status, OAuth metadata, a partial contract, or a locally assumed value MUST NOT substitute for runtime observation. V1 health responses SHALL retain their existing flat schema, and non-health pending/final response shapes SHALL remain unchanged.

#### Scenario: All six observations match

- **WHEN** live and ready admission succeeds and the observed release, Hosted protocol, profile, gateway digest, command fingerprint, and schema digest match the stored target
- **THEN** v2 health returns that exact six-field `runtimeIdentity`

#### Scenario: One identity field drifts

- **WHEN** any one observed gateway, release, protocol, profile, command, or schema value differs from the target
- **THEN** health fails closed and the cell cannot become ready, routed, bound, active, or promoted

#### Scenario: Agent contract cannot be observed

- **WHEN** the generic gateway contract succeeds but the selected agent-contract route is absent, unauthenticated, malformed, or incomplete
- **THEN** v2 health fails rather than inferring profile, command, or schema identity

### Requirement: Provisioner wire selection is durable and replay-safe

The provisioner and Substrate SHALL persist the outer wire protocol independently from the Hosted runtime protocol. Existing rows SHALL be backfilled as v1, and the provisioner database SHALL retain a server-side v1 default compatible with the pinned legacy provisioner. Substrate SHALL persist the selected wire protocol before first issuance and SHALL reuse it for every retry regardless of feature-gate or deployment changes. Cross-version reuse of the same action and idempotency key MUST conflict.

#### Scenario: Process restarts during a legacy operation

- **WHEN** a persisted v1 operation is retried after either service restarts or v2 issuance is enabled
- **THEN** the retry uses the exact v1 header and body and converges on the existing operation

#### Scenario: Feature gate changes during a v2 operation

- **WHEN** a v2 lifecycle operation is persisted and the issuance flag later becomes false
- **THEN** every retry remains v2 because stored operation state outranks the current flag

#### Scenario: Same idempotency key crosses protocols

- **WHEN** a caller reuses an existing action and idempotency key under a different wire protocol
- **THEN** the request receives a terminal conflict and the original operation is unchanged

### Requirement: Contract mode permits only exact persisted v1 replay

The forward provisioner SHALL support explicit `expand` and `contract` admission modes plus one bounded legacy-v1 runtime catalog containing every exact release/protocol unit that authoritative preflight finds routable, assigned, or referenced by unfinished v1 work. Expand SHALL admit fresh strict v1 only when its release/protocol selects one unique verified catalog unit and SHALL admit fresh v2 for the locked forward target. Contract SHALL admit fresh v2 operations and SHALL process v1 through one atomic repository decision: an exact already-final v1 operation MAY return its persisted v1-model-validated final result without a runtime catalog entry, while a non-final v1 replay MUST additionally match a retained catalog unit and existing monotonic-fence rules. Missing, duplicate, unverified, or unreferenced legacy units and fresh v1 rejection MUST create no operation and perform no provider effect.

#### Scenario: Fresh v1 is used during expansion

- **WHEN** D1 runs in expand mode before Substrate v2 issuance is enabled and a fresh v1 request names one exact cataloged legacy release
- **THEN** the operation retains the exact legacy behavior for that runtime unit

#### Scenario: Fresh v1 is used after contraction

- **WHEN** contract mode receives a well-formed v1 request without an exact persisted operation
- **THEN** it returns a bounded terminal error before operation creation or provider mutation

#### Scenario: Exact legacy replay occurs after contraction

- **WHEN** contract mode receives a v1 action/key/body matching a persisted v1 operation and current fence semantics
- **THEN** the operation resumes or returns its existing final result under the unchanged v1 response schema

#### Scenario: Final legacy unit is no longer cataloged

- **WHEN** an exact v1 replay matches an already-final stored operation whose old runtime release is no longer routable or retained in the runtime catalog
- **THEN** the provisioner returns the persisted result after v1 model validation without contacting a runtime or provider

#### Scenario: Stale legacy replay follows existing fencing

- **WHEN** a body-exact legacy request is older than the tenant's already-observed destructive fence
- **THEN** replay remains rejected under the existing monotonic-fence contract

### Requirement: Runtime target and package lineage have separate authorities

The deployment lock and provisioner wire SHALL contain runtime-observable release, Hosted protocol, profile, gateway, command, and schema identity only. Substrate SHALL remain authoritative for candidate identity, compatibility, Claude/OpenAI packages and archives, plugin artifacts, OAuth bindings, and staged-client evidence through its immutable candidate catalog and lifecycle snapshot. A cell response MUST NOT be accepted as package or compatibility evidence.

#### Scenario: Client package changes without runtime changes

- **WHEN** a Claude or OpenAI package/archive lock changes while the runtime six-field identity is unchanged
- **THEN** Substrate updates its candidate or staged-client lineage without changing a provisioner request hash or forcing a cell rollout

#### Scenario: Observation storage requires compatibility

- **WHEN** an existing all-or-none database constraint requires an observed compatibility value
- **THEN** Substrate derives it from the immutable local candidate and does not claim it was observed from health

### Requirement: Static target validation and live admission gates remain distinct

Before installing or changing runtime bytes, the provisioner SHALL validate the requested runtime target against the immutable deployment lock before provider effects. It SHALL require matching live runtime identity before readiness, route binding, assignment activation, or candidate promotion. Offline restore, backup recovery, salvage, discard, delete, and destruction SHALL retain target fencing and audit identity but MUST remain possible without a successful live runtime probe.

#### Scenario: Target does not match the deployment lock

- **WHEN** a v2 provision or runtime-changing resume names an untrusted release or contract target
- **THEN** the operation fails before namespace, image, Helm, compute, storage, or routing effects that would install or change that runtime

#### Scenario: Restore candidate is offline

- **WHEN** a valid restore-candidate operation reconstructs stopped storage while the runtime cannot answer health probes
- **THEN** static target and durability fencing are enforced but the restore is not blocked by missing live identity

#### Scenario: Broken cell must be destroyed

- **WHEN** a fenced discard or destroy request targets a cell whose runtime contract is unavailable or mismatched
- **THEN** the authenticated destructive workflow can complete without first making that runtime healthy

### Requirement: Immutable phase locks compose verified component lineage

Production preparation SHALL consume exactly one member of a deterministic strict expand/contract lock pair produced only after cryptographically verifying the exact runtime and provisioner candidate bytes and their image attestations, verifying the forward and every authoritative legacy runtime-contract artifact by caller-supplied SHA-256, and passing source-closure checks. Both locks SHALL carry identical component/catalog lineage and the canonical digest of the authoritative routable, assigned, and unfinished-v1 release set, and SHALL differ only in their immutable provisioner admission mode. Each SHALL derive both digest-authoritative images and runtime configuration for Helm, record component sources and candidate-byte hashes, include the six-field forward runtime target, the bounded legacy-v1 runtime catalog, and exact composition and rollback evidence. Neither a provisioner image nor admission mode MAY be supplied as a free override, and a lock MUST NOT contain mutable image tags as authority, placeholders, compatibility values, or client-package lineage.

#### Scenario: Two candidates and contract evidence verify

- **WHEN** exact signed runtime and provisioner candidates, matching hash-pinned forward and legacy runtime-contract evidence, and both source closures pass
- **THEN** the composer atomically writes deterministic expand and contract locks from which Helm derives both images and runtime settings

#### Scenario: Existing stale v1 manifest is supplied

- **WHEN** the committed 0.22.0/21-command v1 manifest is supplied for the v0.35.1 runtime candidate
- **THEN** composition fails on release, source, image, or contract mismatch rather than carrying stale metadata forward

#### Scenario: Free provisioner image is supplied at preparation time

- **WHEN** an operator attempts to override the provisioner digest separately from the reviewed lock
- **THEN** production preparation fails instead of composing an unreviewed image pair

#### Scenario: Admission mode is overridden

- **WHEN** an operator tries to mutate the expand lock into contract mode through a Helm value or environment override
- **THEN** preparation fails and requires the exact reviewed contract-lock bytes

### Requirement: Rollout follows an explicit expand and contract sequence

Immediately before D1 takes traffic, deployment SHALL hold the cohort/admission lock, freeze assignment and promotion changes, recompute the canonical authoritative legacy release-set digest, and require it to match the reviewed expand lock. A mismatch SHALL stop cutover and require regenerated/reviewed phase locks. With the freeze held, deployment SHALL move traffic to D1 and the reviewed expand lock, then release the freeze, prove every cataloged v1 unit and synthetic v2 behavior, and deploy the Substrate v2-capable consumer with v2 issuance initially disabled. Enabling v2 SHALL affect only newly persisted operations, and every cell-scoped operation SHALL already snapshot a complete runtime target. Contraction SHALL deploy the reviewed contract lock only after no new v1 operations are observed and existing v1 operations are drained or proven replayable. Live canary evidence SHALL be recorded separately from code merge and artifact publication.

#### Scenario: D1 is deployed before Substrate v2 issuance

- **WHEN** the dual-serving provisioner is rolled out in expand mode
- **THEN** the existing Substrate consumer continues operating under v1 while a synthetic v2 target/identity round trip is verified

#### Scenario: Legacy set changes after lock review

- **WHEN** the under-lock pre-cutover release-set digest differs from the digest in the reviewed expand lock
- **THEN** traffic remains on the old provisioner and the lock pair is regenerated and reviewed before another attempt

#### Scenario: Issuance is enabled

- **WHEN** the v2 feature gate is enabled after the consumer deployment
- **THEN** new operations persist v2 while already persisted operations retain their stored protocol

#### Scenario: Contraction preconditions are incomplete

- **WHEN** fresh v1 operations are still being created or legacy operations have not been drained or audited
- **THEN** the provisioner remains in expand mode and the rollout cannot claim contraction complete

### Requirement: Rollback is exact, rehearsed, and rejects unfinished v2 work

Rollback SHALL name the exact D0 provisioner image digest and source, canonical v1 corpus SHA-256, actual pre-D1 runtime-manifest SHA-256, and last-known-good Substrate v1 consumer commit. Before the tuple is accepted, an executable rehearsal MUST run those exact binaries and manifest against both upgraded database schemas and the frozen replay corpus. Before rollback, admission SHALL stop, both databases SHALL prove that no v2 operation remains non-final, and all remaining cells/operations SHALL match the one legacy runtime unit. Generic schema-v1 or tag-based downgrade authority MUST NOT be accepted.

#### Scenario: Exact rollback tuple is clean

- **WHEN** admission is stopped, all v2 operations are final, and every retained rollback pin matches
- **THEN** the reviewed rollback procedure may deploy the exact legacy unit

#### Scenario: Exact rollback unit is not rehearsed

- **WHEN** the pinned D0 image, manifest, historical consumer, upgraded schemas, or replay corpus has not passed the executable rehearsal
- **THEN** the tuple is not accepted as rollback authority

#### Scenario: An unfinished v2 operation exists

- **WHEN** either database contains a non-final v2 operation
- **THEN** D0 rollback is forbidden and D1 remains running while the operation is resolved or rolled forward

#### Scenario: Generic v1 image is proposed

- **WHEN** an operator supplies a v1 tag, schema version, or image other than the exact retained tuple
- **THEN** rollback validation fails closed

### Requirement: Both language implementations share canonical protocol corpora

The repository SHALL retain canonical v1 and v2 corpora covering all 14 request, pending, final, and failure shapes plus their exact protocol headers. Python SHALL validate them against the provisioner models and TypeScript SHALL consume the same bytes or an exact hash-pinned copy. Unknown fields, mixed envelopes, header/body disagreement, and unbounded responses MUST be rejected consistently. The v1 corpus hash SHALL be retained as rollback evidence.

#### Scenario: V1 corpus is replayed after implementation

- **WHEN** the post-v2 Python and TypeScript implementations load the canonical v1 corpus
- **THEN** every header, request, pending response, final proof, and error remains compatible with the frozen v1 hash

#### Scenario: V2 response uses a v1 envelope

- **WHEN** the client issued a v2 header but receives a flat v1 health response or another mixed-version envelope
- **THEN** parsing fails closed rather than auto-detecting or downgrading

### Requirement: Admission failures remain bounded and content-free

Unknown protocol headers, invalid runtime targets, cross-version replay, fresh v1 in contract mode, identity mismatch, and composition validation failures SHALL expose only bounded reason codes and retryability appropriate to the existing contract. They MUST NOT echo credentials, tenant data, request bodies, contract payloads, image metadata, or provider details, and MUST NOT create partial side effects.

#### Scenario: Runtime target contains a secret and is rejected

- **WHEN** a malformed request supplies a forbidden or invalid field containing sensitive text
- **THEN** the response and logs use a content-free code without reflecting that value
