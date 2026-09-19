## 1. Freeze the proof and compatibility boundaries

- [ ] 1.1 Add the three-second mounted-image read-only-custody reproduction and its writable control to the maintained test suite. Preserve the after-failure note/DB/control evidence and the same-key replay that currently returns outcome unknown. Keep this distinct from the full cluster rehearsal.
- [ ] 1.2 Inventory every supported hosted mutator/selector and direct catalog/policy publisher. Record each canonical effect boundary, prepared result source, required child set and derived work in this change's design. Refuse to dispatch a remember-only implementation as complete coverage.
- [ ] 1.3 Freeze the versioned pending-proof, acknowledgement/current response, Unix-socket and private recovery-descriptor schemas. Specify field bounds, identity source, replay checks, full deadline allocation, reserved proof executor/admission capacity, writer-journal variant/phase ownership, recovery dispatch and retention pins, and stable refusal/pending codes. Test wire fixtures and callback lock ordering before dependent implementation.
- [ ] 1.4 Obtain independent adversarial/security review of the authority, secret-disclosure, recovery and rollout design. Resolve findings before changing those boundaries.

## 2. Bind exact mutation recovery to canonical publication

- [ ] 2.1 Extend writer_lease.py with attempt-bound PreparedCanonicalMutationRecovery and durable private prepared payloads. Test tampering, substituted attempts, stale payloads, missing evidence and precommit crash without marking preparation as success.
- [ ] 2.2 Extend catalog_publication.py and schema_v4.py to add the authenticated hosted-mutation-commit/v1 component in the same transaction as the final tuple publication using existing journal/component tables. Preserve exact v4 schema compatibility and bind all required child effects. Test generic governance recovery dispatch, unknown-variant refusal, projection/catalog garbage-collection pins, retention and downmigration.
- [ ] 2.3 Extract prepare/commit/result seams at the shared semantic creation, edit, move and recovery boundaries and each inventoried direct publication caller. Preserve existing public result shapes and current authorization/egress; no effectful leaf replay is a recovery renderer.
- [ ] 2.4 Extend exact_commit_evidence and canonical resume to verify the actual publication plus original private payload, recover its acknowledgement, render the same canonical outcome and resume derived work. Preserve uncertainty for legacy incomplete attempts.
- [ ] 2.5 Prove the crash cuts before commit, after publication/before acknowledgement, after authoritative acknowledgement/before response, and before outer terminal persistence. Include composite mutations with an incomplete child set and fresh-process recovery, not only caught exceptions.

## 3. Add control-plane-owned acknowledgement

- [ ] 3.1 Add a bounded private pending-publication proof route in server_hosted.py and its transport descriptor. Verify exact committed publication/receipt and challenge, cell, attachment, store and tuple bindings while content parity is blocked. Prove it does not acquire a fence held by the waiting capture or return paths/content. Saturate the shared command worker pool and prove reserved bounded callback capacity remains available.
- [ ] 3.2 Add provisioner acknowledgement/current services using current owned cell credentials, provider recovery identity, trusted callback selection and existing authorization Secret revision CAS. Preserve renewal fields; test replay, stale/foreign proof, expiry, wrong credentials and concurrent acknowledgement/renewal.
- [ ] 3.3 Add activation_ack_api.py to the existing worker composition with a narrow injected capability set and coordinated listener/worker shutdown. Keep the general admission API's privileges and global bearer boundary unchanged. Return signed control/membership and keyring digest/revision only, never keyring bytes.

## 4. Deliver custody promptly and monotonically

- [ ] 4.1 Extend the native custody helper with a bounded Unix-socket acknowledgement client/server path and fixed authenticated TLS control-plane client. Verify identities, signatures and installed parity before completing the healthy request.
- [ ] 4.2 Serialize fast delivery and projected refresh. Test stale projected predecessors, conflicting equal epochs, incomparable renewal generations, sidecar restart, missing rotated verification key and interrupted multi-file publication. Reconcile from authority without locally combining generations.
- [ ] 4.3 Wire hosted acknowledge_activation_tuple to the helper while preserving standalone behavior. Refuse missing capability before mutation and retain exact committed pending recovery after transport loss. Prove the healthy path fits the existing command deadline with the actual held mutation fences.

## 5. Wire deployment and credentials

- [ ] 5.1 Add the worker's internal 8443 TLS listener and ClusterIP Service, trusted fixed endpoint configuration, dedicated certificate Secret and public trust ConfigMap distribution. Add the socket/credential/trust mounts without making runtime custody writable or sharing a Kubernetes/global bearer token.
- [ ] 5.2 Update cell/platform values schemas, exact-shape tenant and provisioner admission policies, Secret/ConfigMap ownership verification, fixed destination egress/DNS and callback ingress. Render and exercise acceptance/refusal against a real API server.
- [ ] 5.3 Document and preflight production certificate creation, encrypted custody, SAN/expiry validation and overlap rotation. Generate a separate disposable CA for tests. Production transport credentials and target deployment remain separately verified actions.
- [ ] 5.4 Add and test preflight classification of clean, protocol-qualified and legacy pending/uncertain targets. Preserve legacy unknown mutation outcomes even when exact activation-only reconciliation restores parity; never invent their missing receipts or replace their identity. Bind the capability to compatible runtime/provisioner/chart releases and deployment locks. Describe forward recovery after an activation advance and refuse incompatible rollback; regenerate signed target and final consumer evidence only for the released exact source.

## 6. Verify and deliver the repair

- [ ] 6.1 Run scoped runtime, mutation, governance, custody, provisioner and chart tests during development. At completion run the required full corpora, build/static/privacy gates and strict OpenSpec validation, comparing any known baseline failures by name.
- [ ] 6.2 Obtain independent review of the implemented authority and recovery cut, including real race and forged-evidence probes. Deliver committed, pushed ready PRs and merge/publish only within the operator's authorized scope.
- [ ] 6.3 Run the real mounted-image/worker/sidecar flow for first capture, cited recall, all acknowledgement loss cuts, renewal races, stale Secret refresh, sidecar restart and pod replacement. Retain bounded private diagnostic evidence before fixture cleanup; do not repeat an unchanged failed cluster run.
- [ ] 6.4 Re-run the connected ordinary empty-installation journey using the released signed image and final consumer commit. Complete owner consent/usefulness acceptance before friends; retain the original invite, tenant and durable attempt. This repair does not complete the later gateway/self-hosting/public redesign.

## Execution ownership

The root owns contract adjudication and integration. After section 1 freezes interfaces, one runtime lane owns sections 2 and 4 plus the runtime proof route; one provisioner lane owns section 3's control-plane handler and section 5. Their file ownership must be explicit before dispatch. Shared descriptors/schema fixtures stay root-owned until frozen; do not concurrently edit server_hosted.py or chart files. An independent review lane attacks the trust/recovery boundary; final verification owns the actual mounted/connected behavior. Model and effort are selected from live harness capabilities at dispatch, with consequential judgment on the strongest appropriate allowed model.
