## 1. Freeze And Review The Contract

- [x] 1.1 Validate this change and the corrected private-alpha specifications with strict OpenSpec checks
- [x] 1.2 Add the paired Substrate OpenSpec delta for v2 issuance, migration 0037, runtime-only observation, and expand/contract deployment
- [x] 1.3 Obtain an adversarial architecture/security review of both repository artifacts and address every actionable finding
- [x] 1.4 Record the exact v1 wire corpus hash and the current runtime/provisioner candidate evidence used as implementation baselines

## 2. Add Strict Dual-Protocol Models

- [x] 2.1 Add failing tests for exact v1 regression, strict six-field v2 targets, v2 health identity, mixed envelopes, unknown fields, and bounded content-free failures
- [x] 2.2 Freeze the current v1 request/final maps and add separate header-selected v2 models for all 14 actions, including explicit target-free export-reference and tenant-destroy requests
- [x] 2.3 Add one pure runtime-target accessor that reads untouched v1 top-level identity or required v2 `runtimeTarget` without rewriting persisted request bodies
- [x] 2.4 Route API validation, pending/final replay, outer health metadata, and worker response validation through the selected exact wire protocol

## 3. Persist Protocol And Enforce Replay Admission

- [x] 3.1 Add failing repository and PostgreSQL-upgrade tests for v1 backfill/default, protocol immutability, cross-version conflict, atomic replay-only admission, and D0-compatible inserts
- [x] 3.2 Add the additive provisioner operation wire-protocol migration with permanent server-side v1 default
- [x] 3.3 Include stored wire protocol in operation snapshots and transactional idempotency comparison without changing the canonical v1 request body hash
- [x] 3.4 Implement explicit expand and contract modes plus the verified legacy-v1 runtime catalog so current cataloged v1 releases survive expansion and contract mode accepts exact persisted v1 replay only
- [x] 3.5 Prove existing monotonic-fence behavior still rejects otherwise exact stale v1 replay

## 4. Validate And Observe Runtime Identity

- [x] 4.1 Add failing lifecycle tests proving runtime-changing effects require static lock/target match while restore-candidate, salvage, discard, and destroy do not require a live probe
- [x] 4.2 Replace direct legacy release/protocol field reads with the pure target accessor across lifecycle, live, durability, and provider paths
- [x] 4.3 Extend authenticated private-cell probing to the selected agent-contract route and derive the six observed identity fields without conflating gateway, capability, command, or schema claims
- [x] 4.4 Fail readiness, routing, binding, activation, and promotion on every one-field mismatch while preserving the exact flat v1 health response
- [x] 4.5 Run focused API, repository, lifecycle, durability, live-provider, and PostgreSQL 17 suites

## 5. Freeze The Cross-Language Contract

- [x] 5.1 Preserve the canonical v1 corpus bytes and add a separate canonical v2 corpus covering all requests, headers, pending/final responses, mismatches, replay failures, and error envelopes
- [x] 5.2 Validate both corpora against the Python Pydantic models and assert the canonical v1 SHA-256
- [x] 5.3 Make the paired TypeScript tests consume the exact corpus bytes or a hash-pinned copy and assert header/body agreement plus strict response parsing

## 6. Compose Verified Deployment Lineage

- [x] 6.1 Add failing composition tests for candidate attestation verification, exact candidate-byte hashing, unsafe/stale/duplicate legacy runtime evidence, deterministic expand/contract locks, and atomic private writes
- [x] 6.2 Implement fixed no-shell ancestry and source-closure guards for the runtime and provisioner path sets, including add/delete/rename, missing, shallow, and non-ancestor failures
- [x] 6.3 Add a strict composer that verifies both candidate records, exact hash-pinned v0.35.1 runtime evidence, and the authoritative bounded legacy-v1 runtime catalog plus canonical release-set digest before producing phase locks
- [x] 6.4 Add v2 expand/contract lock verification containing identical component/catalog lineage, the six-field target, immutable admission mode, composition proof, and exact rollback tuple without package or compatibility data
- [x] 6.5 Update release preparation and verification so production accepts one exact member of the v2 lock pair, derives both immutable images and admission mode from it, and has no free overrides
- [x] 6.6 Update platform Helm templates, values, admission expressions, ConfigMap/path, and tests to consume both images and runtime settings from the selected exact phase lock
- [x] 6.7 Rewire hosted-infrastructure release proof to reverify candidate attestations, forward and legacy runtime identity, provisioner smoke, source closure, and the exact phase lock

## 7. Implement The Paired Substrate Consumer

- [x] 7.1 Add failing TypeScript tests for default-off v1 issuance, explicit v2 issuance, stored retry selection, cross-version idempotency, strict health parsing, and runtime-only identity comparison
- [x] 7.2 Add migration 0037 with only the immutable provisioner-wire-protocol discriminator, v1 backfill/default, allowed-value constraint, and no duplicate identity columns
- [x] 7.3 Add strict v1/v2 serializers and response parsers while preserving exact v1 bodies and keeping runtime Hosted protocol separate
- [x] 7.4 Persist the selected protocol before first issuance; make retries use stored state and let only normalized `true` enable v2 for new operations
- [x] 7.5 Snapshot and build v2 `runtimeTarget` for every cell-scoped action from authoritative candidate/cell state, use explicit target-free v2 for export-reference and tenant-destroy actions, and compare only returned `runtimeIdentity`
- [x] 7.6 Keep compatibility and Claude/OpenAI package/archive lineage candidate-owned; derive any constraint-required compatibility observation locally rather than from health
- [x] 7.7 Run focused Node tests, migration/integration tests, lint, TypeScript checking, and strict Substrate OpenSpec validation

## 8. Publish D1 And Finalize The Lock

- [x] 8.1 Integrate and independently review the Exomem implementation without changing guarded root runtime inputs
- [ ] 8.2 Merge the Exomem implementation and verify the resulting provisioner producer publishes a new D1 candidate with valid image and candidate attestations
- [ ] 8.3 Generate and independently verify current v0.35.1 runtime evidence plus every authoritative legacy-v1 runtime unit; reject stale or unreferenced manifests
- [ ] 8.4 Prove R and D1 source closure, prove D0 fails as the forward candidate, and compose the exact expand/contract lock pair without placeholders
- [ ] 8.5 Rehearse the exact D0 image, actual pre-D1 manifest, frozen corpus, and historical Substrate consumer against both upgraded schemas before accepting rollback authority
- [ ] 8.6 Commit, review, merge, and reverify the final deployment lock pair at its exact default-branch commit
- [ ] 8.7 Merge the paired Substrate consumer with v2 issuance still disabled and record its exact rollback/forward commits

## 9. Deploy Through Expand And Contract

- [ ] 9.1 Run deployment preflight for credentials, capacity, cost alarms, database backups, additive migrations, authoritative legacy release-set digest, and rehearsed exact rollback tuple without claiming success from repository evidence
- [ ] 9.2 Under the cohort/admission lock freeze assignment/promotion changes, compare the live legacy-set digest to the reviewed expand lock, abort/regenerate on mismatch, or move traffic to D1 before releasing the freeze; then verify every cataloged v1 unit and synthetic v2 target/identity behavior
- [ ] 9.3 Deploy the paired Substrate consumer, enable v2 only for new operations, and prove restart/retry preserves stored protocol selection
- [ ] 9.4 Prove no new v1 operations are created, drain or audit persisted v1 work, and deploy the exact reviewed contract lock
- [ ] 9.5 Canary a fresh v2 cell through provisioning, complete identity observation, binding, routing, activation, and promotion
- [ ] 9.6 Verify fresh v1 rejection, exact persisted v1 replay, offline recovery/destruction, tenant isolation, one-unit rollback compatibility, and the no-unfinished-v2 preflight
- [ ] 9.7 Record exact live deployment, canary, and rollback evidence separately from merge, release, and marketplace status

## 10. Close The Change

- [ ] 10.1 Run all focused and full repository verification, strict OpenSpec validation, source-closure checks, Helm rendering, and immutable image smokes at exact delivered commits
- [ ] 10.2 Obtain independent code/security review and an exact-HEAD verifier pass across Exomem, Substrate, D1, the lock, and rollout evidence
- [ ] 10.3 Sync the completed delta specs, archive the change only after its scoped gates are genuinely complete, and retain unresolved external marketplace approval as a separate launch gate
