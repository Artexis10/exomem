## Context

See proposal.md for the failure and scope. The current deployment has a worker with Kubernetes and runtime-control capabilities, a separate repository-only admission API, and a native cell custody publisher. The worker has no HTTP listener. Public TLS terminates outside the cluster; it does not provide an internal authenticated channel for this repair.

The authoritative authorization Secret already supports revision CAS. Membership renewal preserves activation identity, and the control attestation basis intentionally excludes mutable activation fields. Runtime custody is a read-only projection. A committed catalog publication already proves an exact predecessor/successor, but its event identity is not currently sufficient to recover the outer mutation terminal: an acknowledgement exception can unwind the leaf before the exact graph commit receipt exists.

## Goals / Non-Goals

**Goals:** complete the healthy write within the existing command deadline; retain one mutation across interruption; keep the Secret authoritative across refresh, renewal and pod replacement; preserve cell, attachment, enrollment, lifetime and key boundaries.

**Non-Goals:** replace the provisioner, create a second durable activation authority, expose an operator bearer or signing key to a new caller, alter standalone custody, or advance friends/public launch.

## Decisions

### 1. One durable authority and exact successor proof

Keep the provisioner-owned authorization Secret as the external activation authority. The acknowledgement handler authenticates a current accepted cell credential version from the owned cell-credential Secret, validates its provider recovery identity, and derives tenant/cell/attachment and callback destination from trusted records. A global provisioner bearer is never installed in a cell. Caller-provided paths, callback addresses, tuple values or old lifecycle-request credentials cannot establish authority.

The handler issues a fresh single-use challenge to `POST /private/exomem/v1/activation/proof` on the trusted cell destination, using the existing private control authentication. That route returns bounded content-free proof of the committed publication and its exact predecessor, successor, activation store and attachment. It verifies the immutable publication and receipt evidence against the committed store; it does not trust the request to describe the target. It remains available during external/store mismatch while ordinary content serving remains blocked.

The handler revalidates current authority immediately before expected-revision CAS. It may advance only the exact next activation epoch in the same store and attachment. The identical target is a successful replay. A renewal conflict causes reread and rebuild from the authenticated latest bundle, preserving its lifetime, keyring and membership fields. Competing successors, skipped epochs, changed attachment, stale proofs and expired authority are refused.

### 2. Exact mutation recovery at the publication cut

The existing outer graph receipt is too late for this cut. Implementation must bind the original scoped mutation identity, command/request digest, attempt and commit token to recoverable exact canonical outcome evidence before a committed publication can lose its acknowledgement. A publication tuple alone is not a successful capture receipt. Recovery must verify the original committed effects and return the original terminal without rerunning effectful leaf code; it must retain fail-closed behavior for historical attempts without sufficient evidence.

Use a typed PreparedCanonicalMutationRecovery carried by the existing execution attempt. Before effects, retain its deterministic result recipe in private writer state, bound to scoped idempotency digest, command/request digest, attempt ID, commit token and payload digest. The existing commit secret remains outside the vault. Prepared state is not proof of success.

In each SQLite transaction that commits a governance tuple publication, insert an authenticated hosted-mutation-child/v1 component in the existing governance_operation_journals/governance_operation_components tables. Bind writer identity, private child-payload digest, exact publication/predecessor/successor and its manifest identity with the attempt secret. Insert the whole-command hosted-mutation-commit/v1 component in that transaction only when all required children are already proven. Otherwise a later evidence-only SQLite transaction verifies and binds the complete child set and immutable final publication, without advancing activation again. Reuse these tables without changing the exact schema-v4 table definition; its downmigration terminal component already demonstrates transactional journal publication.

Extend writer_lease.exact_commit_evidence to verify the committed component, actual publication and private prepared payload together. Recovery acknowledges the exact pending successor, renders the original canonical result under current authorization/egress rules, and resumes derived work without reinvoking an effectful leaf. Paths and content-bearing result recipes remain private rather than widening portable graph receipts. Retain all existing legacy fail-closed recovery behavior when the new proof is absent.

Extract deterministic creation outcome preparation from the existing prepared creation plan before catalog publication. Apply the same prepare-outcome / commit-effects / acknowledge / render-terminal seam to shared edit/move/recovery paths and direct publication callers including delete, preservation, media scene frames and governance tools. Inventory every supported hosted mutator and selector before dispatch. For multi-step commands, one child catalog is never whole-command completion: bind the required child set, preserve explicit incomplete state, and prove every canonical effect before rendering success. Reordering or resuming a child uses its exact prepared identity and guards; canonical effects are not classified as optional derived work.

Journal reuse includes ownership and retention, not just insertion. Freeze a versioned writer-owned journal variant and its permitted phases in task 1.3. Generic governance recovery currently refuses unknown allocating/pending variants; it must recognize this variant and route it to its exact writer recovery owner without attempting an unrelated repair or declaring a partial mutation complete. Unknown variants remain blocked. Extend catalog/projection pin discovery to this variant's complete dependency set, including required child publications, and retain those artifacts through the supported replay/recovery lifetime. Neither cleanup nor downmigration may discard incomplete recovery evidence. Tests must cover generic recovery scans and garbage collection, not only writer replay.

The current pinned v4 write-capable entrypoints are remember, edit_memory, observe_memory, replace_memory, capture_source, preserve_evidence, preserve_artifacts, triage_memory, connect_memory, adoption_studio, maintain_memory, schema_memory, govern_memory, manage_memory_file, record_memory and plan_memory. Some selectors are read-only or never publish governance; the coverage matrix must classify them rather than treating the command-level flag as an effect inventory.

The directly located catalog callers are semantic_writes.py, move_file.py, recover_from_trash.py, delete_file.py, delete_directory.py, preserve.py, scene_frames.py, governance/tool.py and governance/recovery.py, plus the catalog helper's own publication wrapper. governance/tool.py also publishes policy directly. This is a call-site map, not proof that every selector's child-effect/result recipe has been completed; task 1.2 closes that inventory before implementation dispatch.

### 3. Fast delivery through the single custody publisher

The runtime calls a bounded Unix socket at /run/exomem/activation-ack/ack.sock after canonical commit. Its shared socket directory is read-only in the runtime and writable in the native sidecar. The request carries fixed-schema expected/target identities, never paths or replacement control bytes.

The sidecar calls fixed TLS POST /cell-runtime/v1/activation/ack. The worker verifies the committed proof and durably CAS-publishes before returning signed control and membership records, expected keyring digest and bundle revision. It never returns keyring bytes. The sidecar verifies these records with its already projected keyring, publishes through one serialized custody writer, rereads the installed successor, and only then replies. The runtime revalidates parity before reporting usable success.

GET /cell-runtime/v1/activation/current has the same cell-scoped authorization and nonsecret response shape for lost-response recovery. An unavailable rotated keyring waits for normal Secret propagation; the service credential never gains key distribution authority.

Projected refresh and fast delivery are serialized. An older activation or membership generation cannot overwrite a newer installed generation. Equal epoch with different digest is a conflict. Incomparable generations require a fresh authoritative fetch; the sidecar never splices authority fields locally. Sidecar restart retains installed custody; pod replacement reconstructs from the current authoritative Secret and exact pending publication.

### 4. Internal transport on the existing worker

Add activation_ack_api.py as a narrowly injected ASGI handler hosted alongside production.py's existing worker loop. It shares only the required repository, ownership verifier, Secret adapter and private runtime client. Listener failure closes acknowledgement capability; shutdown coordinates both tasks. The general admission API gains no Kubernetes authority.

Expose port 8443 through the internal exomem-activation-ack ClusterIP Service selecting the existing worker. The trusted default endpoint is https://exomem-activation-ack.exomem-platform.svc:8443; namespace-specific rendering must produce the corresponding fixed service DNS identity. Request data cannot change it, and redirects are disabled.

Provision a dedicated certificate with the exact service DNS SAN through the existing operator-managed encrypted-secret workflow. Mount exomem-activation-ack-tls certificate/private key only in the worker. Copy a versioned public trust ConfigMap into each cell namespace and mount it only in the sidecar. Verify hostname, chain and expiry; provide no insecure fallback. Rotate by distributing overlapping public trust, replacing the server certificate, then retiring old trust. The disposable rehearsal owns its own test CA and certificate; that is not production credential provisioning.

Cell egress permits only the selected platform worker pods on 8443 and required cluster DNS. Listener ingress admits only managed cell pods. Preserve the existing worker-to-cell private proof callback path. Update chart values/schemas, immutable target/deployment configuration, exact workload/admission policies and operation authenticity checks together; a chart-only change is incomplete.

#### Deployment capability and retained transport binding

Keep the six-field runtime target, candidate formats and existing private API protocol version unchanged. Deployment-lock v2/v3 accept one optional closed `activationAcknowledgement` object containing exactly `protocol`, `platformNamespace` and `trustBundleSha256`. The protocol is `exomem.hosted-activation-ack/v1`, namespace is a valid Kubernetes DNS label, and digest is full lowercase SHA-256. Null, partial and unknown fields refuse. The object binds the lock's forward runtime and provisioner components; active selection exposes it, rollback/legacy selection does not. Expand and contract members retain identical acknowledgement configuration. The worker listener stays enabled for an extended lock even during rollback selection because previously deployed forward cells can still need acknowledgement.

Both signed candidate source commits declare support in `src/exomem/hosted_deployment_capabilities.json` and `infra/provisioner/src/exomem_provisioner/hosted_deployment_capabilities.json`, respectively. Each declaration is the exact bounded duplicate-free JSON object `{"activationAcknowledgement":"exomem.hosted-activation-ack/v1"}`. Composer and verifier read committed bytes from the authenticated candidate's source commit, never the working tree or only a later composition commit. These paths are already covered by source closure. A declaring forward runtime requires the lock extension; an extension requires both declarations. A capable provisioner with an old undeclared runtime may use a legacy lock. A codec's presence or a chart switch is not a capability assertion. Runtime mutation entry refuses missing acknowledgement configuration in a hosted cell; standalone behavior remains unchanged. Offline migration/maintenance commands do not acquire an unrelated blanket listener requirement.

Derive the immutable public ConfigMap name as `exomem-ack-ca-` plus the first 40 characters of the digest, and always verify the full digest over exact UTF-8 `data["ca.pem"]` bytes, including a trailing newline. Require a bounded nonempty CA certificate bundle and refuse private-key material. The operator supplies the platform bundle and worker-only `exomem-activation-ack-tls` Secret through the existing encrypted-secret workflow. Release preparation preserves the exact lock; it accepts no independent protocol override. The worker verifies the named platform ConfigMap, then renders a provider-owned immutable copy in the cell. Only the native sidecar mounts its CA at `/run/exomem/activation-ack-trust/ca.pem`. Cell values use a closed `activationAcknowledgement` object with `protocol`, `platformNamespace`, `trustBundleSha256`, and `trustBundlePem`: all empty for legacy, otherwise all required. The endpoint and paths are derived. The existing `python -m exomem.governance.authorization_hosted_mount --watch` entrypoint selects the enabled Unix service. Runtime mounts the shared socket directory read-only; the helper mounts it writable. Set the private custody memory volume to 2 MiB to cover three installed 64 KiB records, the bounded before/after delivery journal, and staged replacements.

After public request validation, admission copies the trusted lock object into the server-owned `_activationAcknowledgement` field of the existing authenticated encrypted durable request, alongside `_providerRecoveryEnvelopes`. Public schemas never accept these fields. A bound request requires today's exact recovery-envelope object set plus `activationAckTrustConfigMap` for the derived ConfigMap and `activationAckEgressNetworkPolicy` for `{resource}-activation-ack-egress`; neither addition is independently optional. An unbound request retains precisely the old set. Signer and verifier derive this mode from trusted admission/stored request binding, not dictionary presence or current mutable worker settings. The existing envelope format authenticates ownership; full content-hash verification additionally authenticates the copied public bundle.

Retain the original namespace, digest, envelopes and ciphertext on replay. `repository.submit` currently hashes the enriched request, so rotating trust must not generate an idempotency conflict for an unchanged public request. Under the existing transaction/row lock, when complete hashes differ, decrypt the original request and compare canonical public bytes after removing only `_providerRecoveryEnvelopes` and `_activationAcknowledgement` from both. Different public bytes still conflict. Identical public bytes reuse the original operation and private bindings without rewriting historical hashes or ciphertext. Do not discard arbitrary underscore-prefixed fields. Worker reconciliation uses the owning operation's binding and envelopes together, including the original immutable trust bundle. Missing original trust is unavailable, not permission to substitute current trust.

Rotation distributes an immutable overlap bundle through a new composition lock, confirms consuming cells have rolled over, replaces the worker certificate, then removes old roots only after old consumers and retained operations retire. Keep referenced old ConfigMaps. Operations using old-only trust must finish or be explicitly replaced before switching to a certificate they cannot verify. Capable historical/rollback target support is deferred; this alpha does not invent a multi-version capability catalog.

### 5. Deadline and lock ordering

Budget the complete exchange within the existing hosted command timeout, leaving time to persist and return its terminal. Normal success cannot depend on kubelet Secret projection or the 15-second refresh interval. A missing acknowledgement capability is a precommit refusal. Transport loss after commit retains an explicit exact recovery identity and blocked content serving until acknowledgement completes.

The waiting mutation must not hold a fence required by the proof callback. Read the already committed, bounded publication proof without acquiring the outer writer, receipt or lifecycle mutation lock; do not use generic store-open helpers that acquire those locks. Give proof handling reserved bounded execution capacity separate from the shared command thread pool, plus bounded admission and a short proof deadline. Commands waiting on writer/idempotency fences must not consume the capacity needed to acknowledge their owner. Saturating ordinary command workers must still allow a valid proof to complete; excess proof requests receive a bounded refusal rather than an unbounded queue. Test callback progress while the real capture holds its normal fences. Background recovery reuses the exact pending publication and current authority; it neither retries canonical effects nor renews expired authority.

### 6. Protocol preparation for task 1.3

The following is the proposed v1 wire contract. Task 1.3 remains open until executable codec fixtures, stable error codes and private result-descriptor fields close it, with concrete callback/saturation and composite-crash cases specified. Tasks 2.5 and 3.1 must execute those cases against the implementation before their completion; an unexecuted planned test is never described as red or passing. These bounds are engineering choices to test, not measured capacity or completed implementation.

Use protocol `exomem.hosted-activation-ack/v1`, closed JSON field sets and strict UTF-8. Reject duplicate/unknown fields, floats and booleans in integer fields. Digests/MACs are 64 lowercase hexadecimal characters; request/challenge IDs use 32 random bytes in that encoding. Wire identities retain the provisioner's existing ASCII identifier grammar; private recovery metadata retains the runtime custody validator's broader UTF-8, 512-byte identity bound. These are distinct existing validators, not interchangeable contracts. Retain the credential-version grammar and writer attempt/commit-token formats. Timestamps are integer Unix seconds; epochs retain the existing positive SQLite integer bounds. Signed proof envelopes use sorted compact JSON, omit the MAC field from the signed body and use domain-separated HMAC-SHA256.

**Proof MAC bytes:** sign `b"exomem.hosted-activation-proof/v1\x00" + canonical_body_utf8`. The body is the complete validated proof response with only its top-level `mac` omitted, serialized with `ensure_ascii=False`, `sort_keys=True`, `separators=(",", ":")` and `allow_nan=False`. Do not add a length prefix, trailing newline, hexadecimal intermediate digest or base64 wrapper. The resulting MAC is the lowercase hexadecimal HMAC-SHA256 digest. The runtime signs with its authenticated installed custody keyring's active key and includes that key's ID in the signed body. The worker selects the identical `signing_key_id` only from the authenticated current source bundle's accepted keys; no key supplied in the request, proof or service credential is accepted. The selected key must be valid now and throughout the challenge's issued/expires interval. A rotated unavailable key requires a fresh challenge after normal custody propagation. Existing custody record MACs retain their own framing and are not this proof protocol. Shared known-answer fixtures use the public 32-byte test key `bytes(range(32))`; they establish interoperability, not trusted publication evidence.

Bound requests to 8 KiB, proof responses to 16 KiB, public-bundle responses to 192 KiB and total headers to 8 KiB, checking size before parsing. Individual custody records retain their existing 64 KiB bound. An activation tuple contains activation_store_id, activation_epoch and activation_state_digest. A publication selector contains publication_event_id, predecessor and successor tuples. A selector locates evidence and never authorizes advancement.

**Runtime proof:** add `POST /private/exomem/v1/activation/proof` next to private membership attestation, with its existing service authentication, identity and trusted routing machinery. Request fields are protocol, challenge_id, issued_at, expires_at, cell_id, logical_vault_id, registry_attachment_id, attachment_epoch, expected_bundle_revision and publication. The response echoes these bindings and adds publication_evidence, signing_key_id and mac. The closed publication_evidence object contains component_kind (hosted-mutation-child/v1 or hosted-mutation-commit/v1) and component_sha256. The MAC domain is exomem.hosted-activation-proof/v1. Sign only after verifying the actual committed publication and authenticated writer binding. A child component must be committed transactionally with its exact child publication and bound to the prepared operation manifest. Its proof authorizes that publication's exact activation advancement so the next child can proceed; it never proves whole-command completion. Only complete required-child evidence permits terminal recovery. Task 1.3 includes an intermediate-child acknowledgement fixture, before a final commit component exists.

The worker issues a three-second challenge, shortened by remaining request budget and authority expiry. Allow at most one second of future clock skew and no expiry grace. Bind the challenge to the request, cell, attachment, selector and source revision; consume it once before a CAS attempt. A CAS retry needs a fresh challenge, and worker restart invalidates outstanding challenges. Lost-response recovery verifies current state rather than replaying a nonce. Proof execution has one dedicated slot per runtime, no waiting queue and a one-second budget; excess requests receive bounded unavailability. A timeout must not release a capacity slot while its underlying task is still running.

**Socket:** frame one bounded JSON exchange per connection with a four-byte unsigned network-order length. Restrict the socket to the intended same-UID peers in its dedicated pod-local directory. Request fields are protocol, request_id, operation (check or ack), budget_ms and publication. Check requires null publication; ack requires a selector. budget_ms is an integer from 1 to 5000 and cannot extend the server deadline. Response fields are protocol, request_id, status (ready, acknowledged, pending, conflict or unavailable), bundle_revision, activation, code and retry_after_ms. Revision and activation may be null. Acknowledged means signed custody was installed, reread and matches the successor. Permit one active exchange per cell with no unbounded queue. Check is a capability preflight, not a promise against subsequent transport loss.

**Worker HTTP:** require Authorization with the current accepted cell credential and X-Exomem-Cell-Id, X-Exomem-Credential-Version, X-Exomem-Activation-Protocol and X-Exomem-Request-Id headers. POST /cell-runtime/v1/activation/ack accepts protocol, request_id, budget_ms and publication; its request ID must equal the header. GET /cell-runtime/v1/activation/current accepts no body or query; its correlation ID comes from the same header. Identity and callback resolution remain exclusively trusted-record operations.

The successful response contains protocol, request_id, outcome (advanced, unchanged or current), cell_id, logical_vault_id, registry_attachment_id, attachment_epoch, bundle_revision, keyring_sha256, control_b64 and serving_membership_b64. Encode original signed bytes with unpadded base64url. Return no keyring bytes. Recompute the existing bundle revision over keyring || control || membership using the sidecar's already projected keyring. POST returns 200 only after durable Secret CAS or verified identical-successor replay; GET returns one verified current snapshot without acknowledging anything. Allow one CAS-conflict retry after reread, revalidation and a fresh proof. Revalidate ownership, credential acceptance, lifecycle and attachment before effects.

Stable codes are MALFORMED_REQUEST, AUTHENTICATION_FAILED, ACTIVATION_CONFLICT, ACK_CAPACITY_EXCEEDED, ACK_UNAVAILABLE, ACK_DEADLINE_EXCEEDED, ACK_PENDING, KEYRING_NOT_AVAILABLE, ACKNOWLEDGED and ACK_READY. retry_after_ms is an integer from 0 through 5000, never null. Error responses contain protocol, request_id, code and retry_after_ms: use 400 for malformed input, 401 for authentication, 409 for authority/tuple conflict, 429 for capacity and 503 for unavailable/deadline. Only MALFORMED_REQUEST may use a null request_id for an invalid/missing correlation ID; never echo arbitrary malformed input. Send Cache-Control: no-store, disable redirects and suppress credential-bearing access logs. Initially permit eight active worker exchanges globally and one per cell, without an application waiting queue. Bind the slot until the underlying work ends, including cancellation/timeout cleanup.

**Deadline:** budget three seconds for preparation/canonical commit, five for acknowledgement/installed parity and two for terminal persistence/response inside the existing ten-second envelope. These are acceptance budgets, not permission to cancel a transaction ambiguously or claims that all existing mutators already meet them. Connection establishment is at most 500 ms; worker processing is at most 3.5 seconds including proof. Sidecar installation and runtime verification share the remaining acknowledgement budget. Every step uses the remaining monotonic deadline. Refuse before effects when required remaining budget/capability is absent. Once committed, timeout retains the exact pending operation. Recovery backs off from 250 ms to five seconds under the same mutation identity, without renewing authority or repeating canonical effects.

**Journal ownership:** register internal operation hosted_mutation_commit_v1 with recovery owner writer_canonical_v1. This is not a new public govern_memory operation. Dispatch it before generic _validated_persisted_journal and composite recovery: those validators require generic prior/prepared/final components and intent-based child terminals that do not describe partial writer children. Add a separate internal variant registry; generic scans classify and delegate, while unknown/malformed variants remain blocked.

Use allocating when private preparation and the complete child manifest exist but no committed child is established; pending after any canonical child commits, including fully committed work awaiting acknowledgement/terminal persistence; closed only after exact terminal persistence or proof that a wholly uncommitted attempt aborted. Only the writer recovery owner advances these phases. Component kinds are hosted-mutation-plan/v1, hosted-mutation-child/v1, hosted-mutation-commit/v1 and hosted-mutation-terminal/v1. The plan binds scoped idempotency digest, command digest, attempt ID, commit token, private payload digest, required-child manifest digest and dependency manifest digest. Child components bind exact canonical evidence. Each tuple transaction binds its exact child component. The commit component binds the complete child set and immutable final publication in the same transaction only if all required children are proven, otherwise in a later evidence-only transaction with no new activation; the terminal component binds the actual persisted terminal digest/disposition. Use existing canonical_json/value_hash semantics with the attempt-secret MAC inside the versioned payload. Preserve opaque child IDs rather than forcing intent + ':committed'. The private descriptor below fixes recipe structure; each mutator's prepared result-field schema must be fixed at its prepare/commit seam before executing effects.

**Retention:** extend store.pinned_component_keys and schema_v4.projection_namespace_pins for the full writer dependency manifest: predecessor/successor publications, policy generations, catalogs, projection namespaces, measurement stores and required-child dependencies. For v1, retain journal evidence and its pins even after closure; do not introduce automatic retirement in this repair. This preserves non-expiring idempotency semantics at a storage cost. Unknown/malformed retained dependencies block collection/downmigration instead of returning empty pins. Reclamation requires a separate contract coordinating private writer-state retirement. No schema-v4 table-definition change is needed.

### 7. The pending acknowledgement must not consume the attestation window

Serving-membership readiness runs through `_ready_custody`, which calls
`schema_v4.load_active_state` and so asserts activation-tuple equality between the
store and `control.json`. A cell whose store is ahead therefore cannot sign a
readiness attestation. The provisioner's hourly renewal of the attestation window
needs exactly that signature, so a pending acknowledgement stops the window from
advancing; `live.py` records that once the window expires the cell cannot recover,
because minting is fenced off for a cell that has served and the drain that would
renew it needs an attestation the cell can no longer sign.

That renewal exists precisely to close this deadlock once before. This repair
reopens it through a different door: a pending acknowledgement becomes a one-hour
countdown to an unrecoverable cell rather than a recoverable stall. The healthy
path is seconds, so the exposure is a control-plane outage that outlasts the
window while a cell holds a committed publication awaiting acknowledgement.

`GovernanceReadinessProof.store_agreement` is not the seam. It is hardcoded `True`,
and both the runtime validator and `governance_readiness.py` reject a proof whose
value is not `True`, so reporting the truth through it only changes which side
refuses.

Decision: readiness must distinguish "the store and the registry disagree for an
unknown reason" from "this cell holds an exact pending successor that the control
plane has not yet acknowledged". Only the second may renew the window, it must be
proven by the same committed-publication evidence the acknowledgement protocol
already requires, and it must not report the cell as fully serving or unblock
content parity. The provisioner is the acknowledging authority and can verify that
evidence itself rather than trusting the cell's claim.

This widens the readiness proof and its provisioner validator, so it is an
authority change and belongs in task 1.4's adversarial review before implementation.
Preflight in task 5.4 must additionally classify a cell by remaining window, and
refuse to begin work that cannot complete inside it.

**Amendment, 2026-09-20: the premise above is contradicted by the code.** The
core claim is settled by enumeration rather than by reading one path:
`schema_v4.load_active_state` has exactly five callers in `src/exomem/`, and
the only one on any readiness or attestation path is
`authorization_session_lifecycle.py:153`, inside `_ready_custody`. The other
four are an attachment-transfer authority check, the down-migration and the
governance tool. So no activation-tuple comparison against the store exists
anywhere on the minting path.

The hourly renewal does not pass through `_ready_custody`. `renew-authorization` reaches
`live.py::_transition_authorization_session_membership` with
`require_runtime_attestation=True`, which calls the adapter's
`attest_authorization_session_membership`; that POSTs
`authorization-membership/attest` directly and never calls `health()` or
`verify_governance_readiness`. On the cell, the only gate is
`hosted_runtime.attest_authorization_membership`, which refuses when
`phase == "active" and not _core_ready_locked()` -- vault readiness, service
auth and `_mutation_authority_ready`, the last set once at startup by
`probe_hosted_mutation_authority`. `mint_hosted_replica_readiness_attestation`
then reads custody, checks identity, epoch, digest and the key window, derives
the state from the lifecycle phase, and signs. It never calls
`schema_v4.load_active_state` and never compares the store's activation tuple
to `control.json`.

What `_ready_custody` does gate is `/ready`, session issuance via
`_resolve_session`, and content serving -- which is exactly the observed
defect, and which this repair addresses through the acknowledgement protocol
itself.

If the trace holds, the fuse is not "every acknowledgement failure is a
one-hour countdown". It is at most "an acknowledgement failure plus a pod
restart", because a restart re-runs the startup probe against the diverged
state. That is a narrower risk with a different shape, and it would not
justify widening the readiness proof and its provisioner validator.

What is still open is only the restart variant: `_mutation_authority_ready` is
set once at startup by `probe_hosted_mutation_authority`, so a pod that
restarts while an acknowledgement is pending re-runs that probe against the
diverged state. The probe calls `require_mutation_admission` and
`hosted_mutation_guard`, neither of which obviously reaches the activation
tuple on a hosted cell, but that has not been settled the same way.

Task 3.4 therefore begins with the experiment, not the implementation: a real
hosted vault whose store leads `control.json`, restarted, asking whether the
attestation still mints. Decisions 7's window arithmetic in the preflight classifier stands
either way -- refusing work that cannot finish inside the remaining window
costs nothing and is right whatever renews it.

### 8. Activation epoch is an authorization generation for vocabulary authority v2

`vocabulary_authority` pins a persisted activation generation to
`control.activation_epoch` when it activates, and requires exact equality on every
later check, with grants and reservations stamped by that generation. Hosted
activation epochs are effectively static after migration today, so the coupling has
never been exercised. This repair advances the epoch on every governed write, which
would invalidate every outstanding grant and reservation on each write.

It is gated on `control.version == 2` with `vocabulary_authority_floor == 2`. The
schema default is `1` and no provisioner code raises the floor, so alpha cells are
minted at floor 1 and this is latent rather than an alpha blocker. It must not stay
undocumented: raising the floor without decoupling the generation would break
vocabulary authority silently. Either the generation must stop tracking the
activation epoch, or floor 2 must be refused on a cell with acknowledgement
capability, and whichever is chosen needs a test that fails if the two are ever
combined.

### 9. Certificate custody for the acknowledgement listener

The trust half is already governed and needs no new mechanism.
`hosted_composition_lock.py --activation-ack-trust-bundle` reads a CA-only PEM,
digests it into `activationAcknowledgement.trustBundleSha256`, and the cell chart
projects the same bytes into the immutable ConfigMap `exomem-ack-ca-<digest[:40]>`.
The CA public half is not secret and never enters the destination matrix.

The server half does need a governed route. `load_matrix` refuses two
`sops_k8s_secret` destinations that share a `(namespace, kubernetes_secret)` pair,
deliberately, so two independent handoffs cannot fight over one object under
server-side apply. A `kubernetes.io/tls` Secret is only valid with `tls.crt` and
`tls.key` present together, so the answer is a destination kind
`sops_k8s_tls_secret` that seals both values in one artifact under one lock, not a
relaxation of the uniqueness guard. The CA private key takes an escrow-only
destination, so the next server certificate can be issued under the same trust root
without any cell-visible change. Issuance is a dedicated script in the shape of
`provider_recovery_keypair_handoff.py`; the source kind is generated, and the
generic stdin path refuses it. The subject alternative name is exactly
`exomem-activation-ack.<platformNamespace>.svc.cluster.local`, which is the
ClusterIP Service the cell's sidecar dials.

**Expiry is not a render-time check.** `validate_activation_ack_trust_pem` stays
structural: bounded bytes, CA-only, no private-key material. It runs at chart render
and at release verification, so a not-expired assertion there would turn an expired
CA into a cluster that cannot render its own charts — precisely when an operator is
trying to roll a new one. Time-dependent validation belongs in issuance and in the
5.4 preflight, where the failure reads "rotate now" rather than "you cannot deploy".

**Rotation has two tiers, and only one of them is expensive.** Renewing the server
certificate under the same CA changes no digest, no lock and no cell; this is the
routine expiry path and should be the normal answer. Rotating the CA changes the
bundle digest, therefore the ConfigMap name, therefore the lock, therefore every
cell. It runs as a three-bundle overlap: publish a bundle carrying both CAs, let
cells adopt it, switch the server certificate to the new CA, then publish a bundle
carrying only the new CA. The bundle validator already admits up to
`MAX_ACTIVATION_ACK_CA_CERTIFICATES` certificates, which is what makes the overlap
expressible at all.

**A test certificate must not be able to pass as live readiness.** Tests generate a
disposable CA per run and never read `infra/secrets`. Issuance marks a test CA in
its subject and the preflight refuses a live certificate carrying that marker. What
it prevents is a disposable CA quietly serving production; what it costs when it
fires wrongly is one refused issuance with an explicit reason, paid by the operator
at issuance time rather than by a tenant in production.

Measured while implementing this: `x509.verification.PolicyBuilder().build_server_verifier()`
refuses a leaf that carries no Authority Key Identifier (`2.5.29.35`), and refuses the
chain when the CA itself carries no Key Usage (`2.5.29.15`) -- the second reports as
`candidates exhausted: invalid extension: 2.5.29.15`, which reads like a leaf problem
and is not. Internal issuance must therefore give the CA `keyCertSign`/`crlSign` key
usage and a subject key identifier, and the leaf an authority key identifier, so the
library verification path can be used. Hand-rolled signature checking is the worse
option here, and `validate_activation_ack_server_certificate` now takes the library
path with these requirements encoded in its tests.

### 10. One listener name, derived in one place

Independent review on 2026-09-20 found that the certificate this change issues
could not be verified by the client it is issued for. The cell's HTTP client
dialled `exomem-activation-ack.<namespace>.svc`; the issuer and validator
produced the fully-qualified `exomem-activation-ack.<namespace>.svc.cluster.local`.
Reproduced with a real TLS handshake: the short name fails hostname
verification against the issued certificate.

The consequence is total for a capability-bound cell. The handshake failure
becomes `ActivationAckHttpError`, the custody acknowledgement service cannot
serve, `authorization_custody` raises `AuthorizationCustodyUnavailable`, and no
governed registry CAS can commit — which is the exact defect this change
exists to repair, reintroduced by its own transport.

Nothing caught it because each side had a test that pinned its own spelling.
`tests/test_hosted_activation_ack_http.py` asserted the short form the client
built; the issuer and configuration suites asserted the fully-qualified form
the certificate carried. Both suites were green and agreed with nothing.

The name now lives in `hosted_activation_ack_protocol.py`, the module that is
byte-mirrored between the runtime and the provisioner and already holds the
wire contract. Both sides call `listener_dns_name`, so they cannot spell it
differently again, and the existing copy test keeps the two files identical.
The fully-qualified form is the one kept: every other in-tree service reference
uses it, and the short form appeared exactly once outside tests.

`infra/provisioner/tests/test_activation_ack_listener_identity.py` is the test
that was missing. It asks the real client factory for its hostname, feeds that
to the validator, and completes a real TLS handshake against a certificate the
issuer minted. It can only pass if the two sides agree, which is the property
that matters and the one neither suite was asserting.

### 11. The listener's certificate is checked where it is loaded, not only where it is issued

The same review found that nothing read the certificate again after issuance.
`validate_activation_ack_server_certificate` ran only inside the issuing
script, so the disposable-test-CA marker and the remaining-lifetime floor were
inert against the certificate the worker actually mounts. A Secret replaced by
hand, rolled back to an older pair, or simply left until it expired would be
served without complaint.

The consequence is the same fleet-wide outage as Decision 10, arriving later.
A cell whose handshake fails cannot acknowledge, so `_ready_custody` refuses
and its readiness, session issuance and content serving all stop. Every
capability-bound cell fails the same way at once, silently, and stays failed
until an operator notices and rotates the certificate.

An earlier version of this decision said the cell also loses its attestation
window within the hour and is then unrecoverable. That is the Decision 7
mechanism, and the amendment there retracts it: the hourly renewal does not
pass through `_ready_custody`. A fleet-wide outage is the honest consequence,
and it is enough.

`activation_ack_startup.preflight_activation_ack_listener` therefore runs in
`production.py` before anything answers on 8443. It reads the mounted leaf,
fetches the immutable trust ConfigMap through the same adapter the lifecycle
path uses -- so the lock's pinned digest decides which bundle counts -- and
validates one against the other. What it prevents is that loss; what it costs
when it fires wrongly is a worker that will not start, pausing routine
lifecycle work until the Secret is fixed or rolled back; and the operator pays
it at deploy time with the reason in the first log line and no cell yet harmed.
A certificate that does not chain to the bundle pinned in its own deployment
lock is a fault, not an eventually-consistent state.

The rotation floor is deliberately **not** fatal here, and this is the part
worth keeping. Refusing to serve a valid certificate because it has nine days
left would strand the fleet now to prevent a handshake failure nine days away:
the wrong-firing cost far exceeds what it prevents. `minimum_remaining` is
therefore a parameter. Issuance keeps the fourteen-day floor, because minting a
longer leaf is free and a short one is simply a mistake; startup passes zero
and logs `activation-ack-certificate-rotation-due` instead.

### 12. A legacy-uncertain verdict is recorded, not recomputed

`classify_activation_ack_target` returned `legacy-uncertain` for a cell whose
committed mutation has no acknowledgement and no protocol to recover one. The
review's point was that the verdict could not survive its own inputs. Both
routes back to looking healthy are live: deploying the capability makes
`capability_bound` true and reclassifies the cell as recoverable, and one later
governed write brings the tuples back into parity and reclassifies it as clean.
Neither produces the receipt the cell never wrote. The tuple comparison is
direction-blind precisely because parity is not evidence about what happened,
and reconciling to parity and then reading parity as health is that error with
an extra step.

The classifier now takes `legacy_uncertain_mark` and returns
`mark_legacy_uncertain`. It asks its caller to write the verdict down and
honours a written one whatever the live inputs later say; it cannot persist
anything itself, and clearing a mark is an operator adjudication rather than a
computation. A recorded mark does not mask `stranded`, which is the harder fact
and the one an operator pages on.

The cost of a mark that fires wrongly is bounded: acknowledgement work stays
refused on that one cell until it is adjudicated. That is not a tax, it is
accuracy -- there is nothing for the protocol to recover there. The mark does
not block ordinary lifecycle work.

### 13. The vocabulary-authority incompatibility keeps one refusal site

The review's last deferred finding was that the floor-2 refusal in
`VocabularyAuthority._custody_floor` is sited at runtime while it reads as a
configuration-time rule, so a tenant meets it as a failed capture rather than
an operator meeting it at bind time.

The direction that matters is already covered. `_custody_floor` is called by
`activate()`, so turning the authority on while the capability is deployed is
refused at the deliberate, operator-initiated action with an explicit message
naming both halves. The gap is only the reverse order: a cell already at floor
2 when the capability is later deployed to it.

Three sites could catch that -- the cell's projected-custody mount, the
provisioner as it binds `_activationAcknowledgement` to an operation, and lock
composition. Each costs something real. The mount refusal is content-free by
design, so it surfaces as a pod that will not go ready without saying why. The
provisioner site adds an authorization-Secret read to every driver step of
every capable cell, permanently, to catch a mistake that can be made once per
cell. Lock composition cannot see any cell's custody floor at all.

So this stays at one site. Alpha cells mint at floor 1, no provisioner code
sets the floor, and the combination is reachable only by deliberately
activating floor 2 first -- which is the direction `activate()` already
refuses. Adding a second control to a latent state in a single-operator alpha
is governance that costs more than it protects. The real fix remains decoupling
the persisted generation from the activation epoch, which retires the
incompatibility rather than guarding it twice.

## Risks / Trade-offs

- Two stores can temporarily disagree after canonical commit. Exact recoverable outcome evidence, predecessor-bound external CAS and blocked content serving cover that cut; a success-shaped response does not.
- Credential rotation can outpace projected keyring delivery. Keep the interrupted mutation recoverable and wait for authenticated propagation rather than returning signing keys through the fast endpoint.
- A new internal TLS listener introduces certificate provisioning and rotation. Read-only preflight must verify this dependency before launch; the test certificate does not count as live readiness.
- The callback can deadlock with the waiting mutation. Exercise real fences and committed-proof reads, including concurrent publication and renewal.
- Additional protocol work precedes alpha. Release adoption remains exact and reviewed; no rollout is inferred from a green unit suite.

## Migration Plan

1. Preflight classifies the target as a clean installation, a protocol-qualified recoverable attempt, or legacy pending/uncertain state. The failures motivating this repair were disposable; they establish no live owner state. Existing exact-publication reconciliation may restore legacy activation parity, but it cannot create missing writer outcome evidence. Keep the original legacy mutation uncertain and stop its acceptance stage for explicit reconciliation; do not automatically use a new key, replay effects, replace a tenant or treat note existence as an original success receipt.
2. Land additive protocol, recovery and configuration support behind explicit capability/version validation. Old standalone behavior remains valid; hosted writes requiring unavailable acknowledgement are refused before mutation.
3. Provision internal TLS and public trust, deploy the compatible worker/listener and admission policies, then deploy the paired runtime/sidecar/chart. Verify exact signed runtime and provisioner identities and regenerate final consumer trust/deployment evidence.
4. Rehearse first capture and citation readback, loss at each commit/ack/delivery cut, renewal races, projected refresh, sidecar restart and pod replacement with the actual mounted image and worker handler. No seeded successful activation or direct production row edits.
5. Run the original connected empty-installation journey and owner acceptance before friends. Preserve the existing invitation and tenant identity.
6. After any activation advance, never roll back to stale custody or an implementation unable to recover the recorded publication. Close serving and repair forward under the same identity; compatible rollback requires exact current authority and recovery support.

## Mutator coverage and canonical result preparation

The pinned v4 command registry and its selector-level read-only classifier, rather than a command-name heuristic, define this inventory. C means catalog publication, P policy publication, O other durable canonical effects, and R read-only. O-only commands retain their existing recovery contracts; they do not acquire fictitious catalog evidence. O effects inside a composite that also publishes a catalog remain required children of that composite.

| Command / selectors | Effects and prepared result source | Required children and postcommit work |
|---|---|---|
| remember: validate / commit | R / C; note._PreparedNote plus relation_review's in-fence creation commit plan | One creation/catalog child, including canonical auxiliaries. NoteResult canonical path/ref/slug/warnings come from preparation. Suggestions, due state, routing and sweep are derived. |
| edit_memory: validation / edits | R / C; ExistingPreflight and planned auxiliary writes | One existing-page catalog child. Retain exact byte hashes, lifecycle and transition binding; index/advisory result is derived. |
| observe_memory: validate / add,update,remove | R / C; ExistingPreflight, proposed/removed semantic unit and log plan | One existing-page catalog child. Parse the exact proposed source before effects to prepare final unit identity/fingerprint; do not rely on a postcommit index reread for the canonical result. |
| replace_memory: validation / commit | R / C; _PreparedNote plus predecessor hash, supersession and log plans | One catalog child covering all canonical new/old page writes. Prepare old/new paths, refs and canonical warning/result data before effects. |
| capture_source: text/files | O; add plan or ordered staged-file plan | No tuple publication. Existing source-write recovery stays in force; file batches distinguish stored, failed and unselected outcomes. |
| preserve_evidence | C; prepared binary artifact, sidecar, indexes and destination | One catalog child. Artifact path/ref/hash/size are prepared from the staged source and destination. Media derivation is separate. |
| preserve_artifacts | C per stored nonduplicate artifact; staged ordered batch | Freeze input disposition and required child manifest before the first effect. Validate duplicate/invalid dispositions under the same guards used for commit; later changes cannot silently remove a required child. A subsequent scene-frame publication is its own derived operation with its own identity, never folded into the parent's already complete canonical set. |
| triage_memory | O; namespace-specific review/family/advisory/adoption/relation/vocabulary decision plan | No tuple publisher. Keep exact reviewed fingerprints and existing decision-store receipts. |
| connect_memory: read selectors / create-entity,accept-relation | R / C; link creation or relation existing-page plan | One catalog child in either mutation route. Suggestions, graph/context and resolution selectors remain R. |
| adoption_studio | R for status/work-item; O for run-state/apply; apply-proposal wraps C plus run-state O | Freeze proposal kind/fingerprint before effects. Compilation and entity delegate one creation child; relation and reconciliation-relate delegate one edit child; supersession and reconciliation-supersede delegate one replacement child. Include the proposal/run-state completion receipt as a required O child; one child note never proves the outer proposal completed. |
| maintain_memory | audit R; fix/backfill-ids R when dry_run; reconcile R only when explicitly dry_run; structured-files R unless apply | O, no direct tuple publisher in the pinned selectors. Reconcile/index rebuild is a requested outcome, not an optional advisory. Preserve its existing handoff and completion semantics. |
| schema_memory | preview/inspect/resolve/validate/diff/inventory R; infer O only with save; saves/refresh O | Registry/contract/workflow atomic writers have no direct tuple publication. Preserve their existing canonical result/receipt contracts. |
| govern_memory | list/explain/simulate R; propose/session/grant/revoke/declare O; commit/suspend/resume/undo P; backfill_companion preview R, commit C | Policy operation binds one tuple publication (including its catalog when present) plus required existing proposal/receipt transitions. Session issuance remains excluded from generic result retention because its one-time bearer must never be cached. Nested session_action, scope and backfill_action remain part of the selector identity. Companion commit has one catalog child. Workspace mirror is derived. |
| manage_memory_file | list/trash-list/propose-reclassification and validation R; create/append/move/delete/recover/reclassify conditional C/O | Freeze path kind, membership changes, link rewrites and exact destination before effects. Governed Markdown routes through creation/existing commits. Membership-affecting moves/deletions/recovery each have one catalog child, with their required filesystem effects. In-place source reclassification without catalog is O. Prepare returned metadata from the planned source, not an effectful rerun. |
| record_memory | describe/validate/inspect/query R; create/append/update/revise/rebaseline O | Structured-collection guarded batches, no direct tuple publisher. Retain collection/item IDs, versions, hashes and held-candidate disposition. Pinned v4 excludes discard. |
| plan_memory | inspect/validate/query R; create/add/update/triage/revise/rebaseline O | Shared structured-collection writers; no direct tuple publisher. Preserve action/container/item outcome and original guards. |

Located direct publication seams are semantic_writes commit_existing/_commit_creation/move/recover; move_file.py preparation/publication; recover_from_trash.py preparation/publication; delete_file.py and delete_directory.py membership removal; preserve.py ordinary/adoption publication; scene_frames.py whole frame/sidecar/measurement batch; governance/tool.py policy publication and companion backfill; governance/recovery.py exact companion republish. Catalog helpers also wrap their own single-upsert call. Implementation must exercise each direct caller and composite branch; this matrix is an implementation map, not completed behavior coverage.

### Private recovery descriptor

Use a closed, data-only `exomem.prepared-canonical-mutation/v1` descriptor. Bind it to the existing scoped idempotency digest, normalized command digest, attempt ID, commit token, command name/selector digest, cell/vault/attachment identity and activation store. Do not persist a callable, module import path, raw authorization bearer or signing key. Keep the attempt secret in existing private writer custody. A keyed MAC authenticates the canonical descriptor digest and each child's private result payload under a distinct v1 domain; portable journal components contain digests and opaque identities, not paths/content.

The descriptor fixes an ordered required-child manifest before the first canonical effect. Each child has an opaque ID, canonical kind, immutable plan digest and prerequisite child IDs. A plan describes exact guarded effects and their before/after identities. Child preparation may refine the expected current catalog tuple after a preceding child's acknowledgement, but cannot change the canonical effect set, remove a failed child, replace the outer command/attempt or treat an already committed child as unexecuted. Unknown kinds and dependency cycles refuse before effects.

The outer result recipe is a bounded tree with four explicit node forms: literal JSON, object members, list items, and a reference to a named field of one required child's prepared canonical result. There is no executable expression language or user-selected renderer. Private JSON values follow existing governance canonical JSON semantics, including finite fractional facts such as artifact frame timestamps; NaN and infinity refuse. Identity epochs and wire protocol fields retain their strict integer rules. Every child reference must resolve against its frozen result-field schema before effects; runtime values must match that schema. The canonical result may include public operation facts already present in the ordinary response (paths, refs, exact hashes, IDs and dispositions) but remains privately stored until current authorization/egress permits delivery. Optional advisory/due/index fields are not represented as if already computed.

Before a child changes canonical state, persist its guarded plan and deterministic canonical result payload privately, then bind that payload digest to the child's transactionally published component. That payload is prepared evidence until its exact canonical effect/publication binding verifies. The whole-command commit component binds the complete required child set and immutable final publication. When an O child follows the final tuple, finalization uses a later evidence-only SQLite transaction after authenticating that child; it does not publish another tuple. Before this finalization, even the final tuple proves only its child and the command remains pending. If all required children are already proven, finalization may share the final tuple transaction.

For Adoption Studio, freeze the applied-proposal JSON transition, timestamp and guarded identity before its O child. Include an internal attempt-authenticated completion receipt in the same atomic proposals.json replacement as that transition (adoption_run.AdoptionRunStore.save_proposals). Keep it out of public proposal views. Then register the verified receipt in SQLite. A crash after JSON replacement but before SQLite registration recovers from that exact receipt; it neither repeats _route_apply nor rewrites the completed O child. The existing unconditional exception reset to proposed in adoption_proposals.apply_proposal must retain applying/pending after any qualified child effect or uncertain outcome; reset only on exact evidence of no committed child. Missing or substituted receipts cannot prove completion. This is an explicit filesystem/SQLite recovery seam, not an atomicity claim across both stores.

For v1, cap the canonical JSON descriptor plus prepared result metadata at 4 MiB, the recipe at depth 32 and 16384 nodes, and the required-child manifest at 256 entries. These are precommit engineering bounds to verify against existing batch limits. Oversized/unknown recipes refuse before effects; no silent truncation. Literal payload content is never logged or returned by the activation-proof endpoint. Existing output filtering runs again at recovery; a previously authorized write does not grant perpetual read access to its result.

Implementation must preserve incomplete-child recovery as pending, with an exact next child and its original guards. It may acknowledge already committed child publications and resume only uncommitted prepared children. It never calls the original effectful outer leaf to reconstruct a terminal. The maintained loss test keeps historical attempts without this binding unknown.

### Independently deliverable preflight protection

Before the complete protocol lands, catalog preparation can safely refuse a known-unwritable local custody parent. This narrow protection uses existing custody validation plus effective POSIX access/read-only-filesystem checks and creates no new authority. It does not reserve future permissions, prove transport readiness, repair a committed legacy attempt, or make the hosted alpha usable. It is independently implementable before the remaining phase-1 protocol closure; the post-preparation loss tests must continue to preserve the uncertain result. Windows retains its existing native custody path. Policy and other direct publication paths remain explicit downstream coverage rather than being claimed by this catalog-only guard.

### Integration acceptance cases

These are required cases for tasks 2.5 and 3.1, not executed evidence. Use a disposable enrolled v4 cell and the actual writer, SQLite publication, private proof route, worker and custody helper. Fault injection may pause or kill at a named boundary; it must not replace committed evidence with a success stub.

| Case | Trigger and required observations |
|---|---|
| Callback while capture waits | Pause the real capture after SQLite commit while it holds its normal writer/receipt fences and awaits acknowledgement. Send the worker's challenged proof request over the private route. It completes within its one-second proof budget, returns only the exact committed child evidence, and allows acknowledgement and terminal persistence without releasing or bypassing the capture's guards. |
| Reserved callback capacity | Occupy every ordinary command worker with commands waiting on the same writer fence. The admitted proof still completes. While the single reserved proof slot is occupied, a second proof is refused immediately; cancelling the first request does not release its slot until underlying work ends. Repeat after timeout to prove no leaked slot or unbounded executor queue. |
| Preparation without commitment | Kill a fresh process after private descriptor/results are durable but before any effect. Reopen with the same key and original guards. No successful terminal or committed component exists merely because preparation authenticates. Changed guarded inputs refuse instead of silently replacing the plan. |
| Filesystem/publication cut | Measured 2026-09-20: this cut blocks every later ordinary write to the same page with `GOVERNANCE_CATALOG_PUBLICATION_BLOCKED`, because the reviewed predecessor the catalog holds is no longer what is on disk. Recovery is therefore not an optimization; without it the page is unwritable. Kill after the canonical filesystem batch but before its catalog transaction. Recover using the existing exact guarded filesystem evidence and prepared child identity. Publish only the outstanding original catalog transition; do not rerun the outer leaf or classify an authentic prepared payload alone as proof that the files committed. |
| Committed child without acknowledgement | Kill after the real tuple transaction, before the authority CAS. A fresh process verifies the exact publication and child component, restores acknowledgement under the same key, and never repeats canonical bytes. A substituted descriptor, child payload, event, store or attachment leaves the attempt pending/refused. |
| Lost authoritative reply | Complete Secret CAS, then drop the HTTP or Unix-socket reply. Fetch current authority and recover the same successor. Exact replay changes no activation, membership lifetime or note bytes. |
| Incomplete composite | With two required catalog children, acknowledge the first and terminate before the second. The first proof is valid for acknowledgement but cannot produce whole-command success. Resume only the second prepared child under its original guards; substituted or missing first-child evidence prevents finalization. |
| Adoption completion cut | Exercise all proposal kinds at three boundaries: after catalog before proposal completion; after the atomic proposal/completion-receipt replacement before SQLite registration; and after aggregate commitment before terminal persistence. Preserve applying/pending state, recover the exact required O child, and never repeat `_route_apply`, duplicate the note or advance activation solely to record completion. |
| Lost outer terminal | Kill after all canonical children and the aggregate commit component, before the outer terminal is persisted. A fresh process returns the original canonical outcome under current authorization/egress, resumes only derived work, and retains uncertainty for a legacy attempt lacking the new evidence. |
| Evidence lifetime | Run generic governance recovery, catalog/projection garbage collection and downmigration checks with both incomplete and closed writer journals. Every required dependency remains pinned; malformed or unknown writer variants refuse rather than becoming empty pin sets. |

Pass these process-level cases before the mounted image/worker/sidecar acceptance and the connected ordinary launch. Codec fixtures and authenticated preparation do not substitute for them.
