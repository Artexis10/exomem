## Context

See proposal.md for the failure and scope. The current deployment has a worker with Kubernetes and runtime-control capabilities, a separate repository-only admission API, and a native cell custody publisher. The worker has no HTTP listener. Public TLS terminates outside the cluster; it does not provide an internal authenticated channel for this repair.

The authoritative authorization Secret already supports revision CAS. Membership renewal preserves activation identity, and the control attestation basis intentionally excludes mutable activation fields. Runtime custody is a read-only projection. A committed catalog publication already proves an exact predecessor/successor, but its event identity is not currently sufficient to recover the outer mutation terminal: an acknowledgement exception can unwind the leaf before the exact graph commit receipt exists.

## Goals / Non-Goals

**Goals:** complete the healthy write within the existing command deadline; retain one mutation across interruption; keep the Secret authoritative across refresh, renewal and pod replacement; preserve cell, attachment, enrollment, lifetime and key boundaries.

**Non-Goals:** replace the provisioner, create a second durable activation authority, expose an operator bearer or signing key to a new caller, alter standalone custody, or advance friends/public launch.

## Decisions

### 1. One durable authority and exact successor proof

Keep the provisioner-owned authorization Secret as the external activation authority. The acknowledgement handler authenticates a current accepted cell credential version from the owned cell-credential Secret, validates its provider recovery identity, and derives tenant/cell/attachment and callback destination from trusted records. A global provisioner bearer is never installed in a cell. Caller-provided paths, callback addresses, tuple values or old lifecycle-request credentials cannot establish authority.

The handler issues a fresh single-use challenge to a new private runtime control proof route. That route returns bounded content-free proof of the committed publication and its exact predecessor, successor, activation store and attachment. It verifies the immutable publication and receipt evidence against the committed store; it does not trust the request to describe the target. It remains available during external/store mismatch while ordinary content serving remains blocked.

The handler revalidates current authority immediately before expected-revision CAS. It may advance only the exact next activation epoch in the same store and attachment. The identical target is a successful replay. A renewal conflict causes reread and rebuild from the authenticated latest bundle, preserving its lifetime, keyring and membership fields. Competing successors, skipped epochs, changed attachment, stale proofs and expired authority are refused.

### 2. Exact mutation recovery at the publication cut

The existing outer graph receipt is too late for this cut. Implementation must bind the original scoped mutation identity, command/request digest, attempt and commit token to recoverable exact canonical outcome evidence before a committed publication can lose its acknowledgement. A publication tuple alone is not a successful capture receipt. Recovery must verify the original committed effects and return the original terminal without rerunning effectful leaf code; it must retain fail-closed behavior for historical attempts without sufficient evidence.

Use a typed PreparedCanonicalMutationRecovery carried by the existing execution attempt. Before effects, retain its deterministic result recipe in private writer state, bound to scoped idempotency digest, command/request digest, attempt ID, commit token and payload digest. The existing commit secret remains outside the vault. Prepared state is not proof of success.

In the same SQLite transaction that commits the final governance tuple publication, insert an authenticated, versioned hosted-mutation-commit/v1 component in the existing governance_operation_journals/governance_operation_components tables. Bind the writer identity, recovery-payload digest, exact publication/predecessor/successor and required canonical child-effect identities with the attempt commit secret. Reuse these tables without changing the exact schema-v4 table definition; its downmigration terminal component already demonstrates transactional journal publication.

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

### 5. Deadline and lock ordering

Budget the complete exchange within the existing hosted command timeout, leaving time to persist and return its terminal. Normal success cannot depend on kubelet Secret projection or the 15-second refresh interval. A missing acknowledgement capability is a precommit refusal. Transport loss after commit retains an explicit exact recovery identity and blocked content serving until acknowledgement completes.

The waiting mutation must not hold a fence required by the proof callback. Read the already committed, bounded publication proof without acquiring the outer writer, receipt or lifecycle mutation lock; do not use generic store-open helpers that acquire those locks. Give proof handling reserved bounded execution capacity separate from the shared command thread pool, plus bounded admission and a short proof deadline. Commands waiting on writer/idempotency fences must not consume the capacity needed to acknowledge their owner. Saturating ordinary command workers must still allow a valid proof to complete; excess proof requests receive a bounded refusal rather than an unbounded queue. Test callback progress while the real capture holds its normal fences. Background recovery reuses the exact pending publication and current authority; it neither retries canonical effects nor renews expired authority.

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
