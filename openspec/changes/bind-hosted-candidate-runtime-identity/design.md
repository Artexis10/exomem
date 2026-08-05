## Context

The runtime and provisioner producers now publish strict, independently attested image-candidate records. The v0.35.1 runtime candidate is fixed at source `f1472c297d9256a28c9706bb666e249b64cfd804` and image `ghcr.io/artexis10/exomem@sha256:3264271d7292c713e1f6ba6ae4a11a4b8e8c52a58a1a06e1d13726a515175ca3`. The existing provisioner candidate is fixed at source `6cc0718fc6baf294ed313662a889462aad56f164` and image `ghcr.io/artexis10/exomem-provisioner@sha256:b3f2f12691207200a57dd193f3669a8f2cd2f7c058105b0d4af691f3057097df`.

Those records prove what was built, by which reviewed workflow, from which source. They do not choose one deployable pair, prove that later source still represents each image, or bind a Substrate lifecycle target to the runtime contract observed inside a cell. The committed schema-v1 Hosted release manifest is also stale: it describes release 0.22.0 and a 21-command surface, while v0.35.1 exposes 25 REST commands. It cannot be silently reused as v0.35.1 contract evidence.

Three version axes must remain independent:

- Provisioner wire protocol: `exomem-cell-provisioner.v1` or `exomem-cell-provisioner.v2`.
- Hosted runtime protocol: the value `"1"` in the runtime contract and lifecycle target.
- Private runtime routes: `/private/exomem/v1/...`.

Substrate already owns the candidate catalog, compatibility digest, package/archive locks, canary assignment, and immutable lifecycle target columns. Exomem deployment infrastructure owns runtime/provisioner image selection and authenticated observation of the running runtime. The design must not create a second authority for either side.

## Goals / Non-Goals

**Goals:**

- Compose one independently verified runtime candidate, one independently verified provisioner candidate, matching runtime-contract evidence, and the exact bounded legacy-v1 runtime catalog into strict expand and contract deployment locks.
- Add a dual-serving provisioner whose v2 request names the exact runtime target and whose v2 health response reports the exact observed runtime identity.
- Preserve v1 schemas and replay while making protocol selection durable and safe across retries, restarts, feature-gate changes, and rolling deployments.
- Gate readiness, routing, binding, activation, and promotion on the complete observed identity without blocking offline restore, salvage, discard, or destruction when a cell is unhealthy.
- Keep compatibility and client-package lineage in Substrate and outside the provisioner wire contract and deployment lock.
- Deploy through an explicit expand/contract sequence with an exact, hash-pinned rollback tuple.

**Non-Goals:**

- Changing the Hosted runtime protocol, private route versions, MCP protocol, or user-facing Exomem behavior.
- Rebuilding the runtime image unless the source-closure guard proves that v0.35.1 no longer represents the composition source.
- Copying candidate IDs, source commits, image references, compatibility digests, package locks, plugin provenance, or OAuth metadata into `runtimeTarget` or `runtimeIdentity`.
- Adding a second Substrate target/observation JSON blob or duplicating migration 0036 identity columns.
- Treating a repository merge, image publication, or rendered Helm chart as proof of a live deployment.
- Requiring a running cell contract for offline recovery or destructive cleanup.

## Decisions

### Provisioner v2 uses the existing routes and an exact header

All 14 actions stay at `/cells/<action>`. The exact `X-Exomem-Provisioner-Protocol` header selects a closed v1 or v2 schema and response model. Unsupported or mixed-version requests fail before operation creation or provider effects. Adding `/v2/cells/*` would duplicate routing and still require an independent replay discriminator.

V1 request bodies, pending responses, final responses, canonical hashing, and failure envelopes remain unchanged. Internal code reads the legacy top-level release/protocol fields through a pure accessor; it never rewrites persisted v1 dictionaries into v2-shaped data.

For every cell-scoped v2 action, the runtime identity is exactly:

```json
{
  "runtimeTarget": {
    "releaseVersion": "0.35.1",
    "protocolVersion": "1",
    "agentProfile": "hosted-alpha-agent-v1",
    "gatewayContractDigest": "<64 lowercase hex>",
    "commandFingerprint": "<64 lowercase hex>",
    "schemaDigest": "<64 lowercase hex>"
  }
}
```

Successful v2 health returns the same six fields under `runtimeIdentity`. The v2 model forbids candidate IDs, source commits, image references, compatibility fields, package/archive locks, plugin/OAuth provenance, and generic `contractIdentity` objects.

V2 does not discard the established action contract around that identity. Every cell-scoped request retains the existing action-specific context, credentials, worker policy, provider references, and bounded specialized fields, replacing only the legacy top-level release/protocol identity with `runtimeTarget`. The context-only `export-delete`, `export-download`, and tenant `destroy` actions have explicit target-free v2 models whose bodies remain otherwise unchanged. V2 health retains `live`, `ready`, `cellId`, service-authentication, mutation-authority, read/write-admission, worker-policy, and reason fields, replacing only the flattened release/protocol identity with `runtimeIdentity`. Other final and pending response shapes remain unchanged.

### Runtime identity is measurement; compatibility remains catalog authority

The provisioner observes the existing authenticated live, ready, and full gateway contract routes plus `/private/exomem/v1/agent/hosted-alpha-agent-v1/contract`. It derives release and Hosted protocol from the runtime responses, the gateway digest from the full gateway contract, and the profile, command fingerprint, and schema digest from the selected agent contract. It returns `runtimeIdentity` only after all six values match the requested target.

Substrate snapshots and builds `runtimeTarget` for every cell-scoped lifecycle operation from the authoritative selected candidate and cell binding, not only provision and restore. It compares the returned runtime identity to that target. Candidate compatibility and Claude/OpenAI package/archive lineage remain resolved locally through the immutable candidate ID. If the existing all-or-none observation constraint requires an observed compatibility value, Substrate derives it from the selected local candidate and documents it as catalog binding, never as cell evidence.

### Wire protocol is persisted on both sides

The provisioner adds an additive `wire_protocol` column to durable operations. Existing rows are backfilled as v1 and the database keeps a permanent v1 server default so the pinned D0 binary can still insert rows after migration. Operation submission includes wire protocol in the transactional exactness check.

Substrate adds only an immutable provisioner-wire-protocol discriminator to lifecycle operations, also backfilled/defaulted to v1. A missing or malformed issuance flag is off; only normalized `"true"` creates new v2 operations. The selected discriminator and, for every cell-scoped action, the complete runtime target are stored before the first request. Every retry uses stored state rather than re-evaluating the feature flag or process release configuration.

Migration 0036 remains authoritative for target and observed runtime identity. No second identity JSON or duplicate target columns are added.

### Expansion and contraction have different v1 admission rules

The forward provisioner has two explicit admission modes and one bounded legacy-v1 runtime catalog. The catalog contains the exact hash-pinned runtime manifest and digest for every release/protocol pair that is routable, assigned, or referenced by an unfinished v1 operation at expansion preflight. D1 selects fresh v1 and non-final v1 replay behavior only from that catalog; an exact already-final v1 replay may return its persisted, v1-model-validated final result without a runtime catalog entry because it performs no runtime or provider effect. The forward six-field v2 target remains bound to R. An absent, duplicate, unverified, or unreferenced legacy entry fails preflight rather than being guessed from request text.

The modes are:

- `expand`: fresh v1 operations are accepted only for exact cataloged legacy runtime units and fresh v2 operations are accepted for the locked target, allowing D1 to deploy before Substrate changes issuance without breaking current v1 releases.
- `contract`: fresh v2 operations are accepted; an exact already-final v1 operation may return its persisted final result, while non-final v1 succeeds only when action, idempotency key, stored wire protocol, canonical request hash, existing fence semantics, and a retained legacy catalog unit match.

The replay-only decision is atomic inside repository submission. A separate read followed by create/reject would race. Replays continue to obey the existing monotonic tenant-fence rules; this change does not make stale operations replayable after a newer destructive fence.

Cross-version reuse of an action/idempotency key conflicts. Admission errors use the existing bounded content-free envelope and create no operation or provider resource.

### Static target validation and live probing are separate gates

Before installing or changing runtime bytes, the requested v2 target must match the immutable deployment lock. That static validation happens before provider effects.

Live authenticated observation is required before declaring a cell ready, binding routing, activating an assignment, or promoting a candidate. Restore-candidate, backup recovery, salvage, discard, delete, and destruction carry and fence the stored target for audit but do not require a working runtime probe. This preserves recovery precisely when runtime identity is broken.

### Composition verifies candidates, contract evidence, and source closure

A dedicated composer verifies both candidate records through the existing cryptographic candidate verifier; parsing candidate JSON is insufficient. It accepts the forward and every legacy v1 runtime-contract artifact only with exact caller-supplied SHA-256 values, bounded regular-file checks, duplicate-key rejection, immutable image cross-checks, and agreement with the authoritative preflight release set. The stale committed 0.22.0 manifest must fail unless it is independently proven to be an actually referenced legacy unit. A current v0.35.1 compatibility manifest/receipt is generated and independently verified from the immutable runtime image before final composition.

The composer then enforces Git ancestry and fixed no-shell source closures:

- Runtime: `Dockerfile`, `.dockerignore`, `pyproject.toml`, `uv.lock`, `README.md`, `LICENSE`, and `src/**` from runtime candidate source to the composition source.
- Provisioner: `infra/provisioner/Dockerfile`, `infra/provisioner/pyproject.toml`, `infra/provisioner/uv.lock`, `infra/provisioner/README.md`, `infra/provisioner/alembic.ini`, `infra/provisioner/src/**`, `infra/provisioner/alembic/**`, `infra/helm/cell/**`, and `.dockerignore` from provisioner candidate source to the composition source.

Missing/shallow commits, non-ancestry, additions, deletions, renames, or diffs inside a closure fail closed. A valid signature never waives source drift. An unrelated documentation or deployment-only diff does not force an image rebuild.

The composer emits a strict canonical lock pair with identical component/catalog lineage and different immutable admission modes: one expand lock and one contract lock. Each lock contains:

- Runtime candidate image, source, canonical candidate-byte hash, and verified contract identity.
- Forward provisioner candidate image, source, and canonical candidate-byte hash.
- The exact six-field runtime target.
- Provisioner wire protocol v2 and the lock's exact admission mode.
- Composition evidence, including guarded commits/path sets, the forward contract hash, the complete bounded legacy-v1 runtime catalog, and the canonical authoritative legacy release-set digest it was built from.
- An exact rollback block containing D0 image/source, canonical v1 corpus hash, the actual pre-D1 legacy manifest hash, and the pinned last-known-good v1 Substrate consumer commit.

Neither lock contains a compatibility digest or client-package lineage. Helm derives both image digests and runtime configuration from exactly one selected lock; the provisioner image and admission mode are not free overrides. Moving from expand to contract deploys the reviewed contract-lock bytes after drain evidence instead of mutating or overriding the expand lock.

### Candidate producers remain independent

Runtime and provisioner workflows continue producing independent candidates. Composition is a consumer workflow and does not make one producer read the other's candidate. Because v2 changes provisioner build inputs, the current provisioner candidate D0 is rollback-only and implementation must produce a new independently attested D1 before a final lock can be committed.

The runtime candidate R may be reused only if its source closure remains clean. If root runtime inputs change, work stops for a new runtime release/candidate instead of composing stale bytes.

### The cross-language corpus freezes both protocols

V1 fixture bytes and behavior stay frozen at the independently recomputed SHA-256 `ced714a5aa204a837e22cab831262cc0ae4766e44720b2896e61b8c157ddd3b5`. A separate v2 corpus covers every request, pending/final response, protocol header, mismatch, and replay failure. Python validates the corpus against strict Pydantic models; TypeScript consumes the same canonical bytes or an exact hash-pinned copy and proves header/body agreement. The canonical v1 corpus SHA-256 becomes part of rollback authority.

### Rollback is one exact rehearsed tuple

“Roll back to v1” is not an allowed instruction. Rollback requires the exact D0 image and source, the canonical v1 corpus hash, the actual pre-D1 legacy runtime-manifest hash, the exact last-known-good Substrate v1 consumer commit, admission stopped, proof that neither database contains a non-final v2 operation, and proof that every remaining cell/operation is compatible with that one legacy unit. Before the tuple is accepted, an executable rehearsal SHALL run the exact D0 image, manifest, and historical consumer against both upgraded database schemas and the frozen replay corpus. If any precondition or rehearsal fails, D1 stays running while the system rolls forward or resolves operations.

## Risks / Trade-offs

- [Runtime candidate R becomes stale while implementation lands] -> Run the source-closure guard before accepting composition; publish a new runtime release if any guarded path changed.
- [V1 replay behavior drifts through internal normalization] -> Keep v1 classes and fixtures exact, use a read-only accessor, and regression-test pending and final replay across upgrade.
- [A feature flag changes an in-flight request] -> Persist the selected wire protocol before issuance and retry only from stored state.
- [Compatibility is mistaken for observed runtime evidence] -> Remove it from the wire and derive it only from the Substrate candidate catalog.
- [Live identity gates make broken cells impossible to recover] -> Require probes only for readiness/binding/activation/promotion; use static target fencing for offline recovery/destruction.
- [Two signed candidates are composed even though contract metadata is stale] -> Require hash-pinned, candidate-matching runtime-contract evidence and reject the existing stale manifest.
- [D0 is deployed against unfinished v2 operations] -> Make the no-non-final-v2 preflight mandatory and fail closed.
- [D1 expansion rejects the currently live v1 release] -> Generate the bounded legacy catalog from authoritative routable/assigned/in-flight state and prove each exact runtime unit before deployment.
- [The authoritative legacy set changes after lock review] -> Recompute and compare its canonical digest under the cohort/admission lock immediately before D1 takes traffic, freeze assignment/promotion changes through cutover, and regenerate/review the pair on mismatch.
- [A cell-scoped maintenance action lacks a target] -> Snapshot the selected candidate/runtime target for every cell-scoped operation; use explicit target-free v2 only for export-reference and tenant-destroy actions.
- [Mixed deployments create fresh v1 work after contraction] -> Deploy the expand lock, switch new issuance to stored v2, prove the drain, then deploy the immutable contract lock.
- [D0 metadata exists but rollback is not executable] -> Rehearse the exact old binaries and manifest against upgraded schemas and corpus before accepting the tuple.
- [A generated lock contains a placeholder or mutable tag] -> The composer accepts only verified digest-authoritative candidates and writes atomically after every check; no placeholder lock is committed.

## Migration Plan

1. Land and validate this OpenSpec plus the paired Substrate delta before implementation.
2. Implement Exomem dual protocol, durable protocol storage, legacy runtime catalog, runtime observation, cross-language corpus, composition tooling, and Helm exact-lock consumption without changing guarded runtime inputs.
3. Implement the Substrate dual-protocol client, operation discriminator, and complete per-cell-action target snapshotting with v2 issuance default-off.
4. Merge Exomem implementation and let the reviewed producer create D1. Verify D1 image and candidate attestations independently.
5. Generate and verify current v0.35.1 runtime-contract evidence from R plus every authoritative legacy v1 unit. Compose R + D1 only after both source-closure checks pass, then commit the exact expand/contract lock pair.
6. Rehearse the exact D0 rollback unit against both upgraded schemas and the frozen v1 corpus. Immediately before cutover, acquire the cohort/admission lock, freeze assignment and promotion changes, recompute the authoritative legacy release-set digest, and compare it to the expand lock. On mismatch, stop and regenerate/review the lock pair; on match, move traffic to D1, then release the freeze. Prove every cataloged v1 unit plus synthetic v2 behavior and full runtime identity.
7. Deploy the Substrate consumer, enable v2 for new operations, and prove retries preserve stored v1/v2 selection.
8. Prove no new v1 operations are created, drain or finish legacy operations, and deploy the reviewed contract lock.
9. Canary a fresh v2 lifecycle through provision, health identity, binding, routing, and promotion. Record live deployment evidence separately from merge and artifact evidence.
10. Archive the change only after repository verification, D1/lock proof, and the explicitly scoped deployment gates are complete.

Rollback stops admission, checks both databases for unfinished v2 operations and one-unit compatibility, and applies only the rehearsed exact rollback tuple. If the preflight fails, keep D1 and roll forward.

## Open Questions

- The final D1 image digest, source commit, candidate-byte hash, authoritative legacy-v1 catalog, actual pre-D1 manifest hash, and rollback Substrate commit are intentionally unknown until their reviewed implementations and live-state preflight exist. They MUST NOT be represented by placeholders.
- Live K3s apply and canary require operator credentials and cost/capacity authority. Repository delivery can prepare and verify the rollout, but cannot turn missing external authority into deployment evidence.
