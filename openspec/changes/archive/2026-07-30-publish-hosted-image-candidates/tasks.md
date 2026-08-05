## 1. Candidate Contract and Verification

- [x] 1.1 Add failing pure-logic tests for valid runtime/provisioner round trips, canonical deterministic JSON, exact immutable image references, and component-specific source/workflow/release rules.
- [x] 1.2 Add failing security tests for duplicate or unknown JSON fields, malformed and mutable references, source/tag/commit drift, unapproved refs, symlinked or oversized bundles, bundle hash drift, and cross-subject evidence.
- [x] 1.3 Implement `infra/scripts/hosted_image_candidate.py` with strict bounded loading, atomic non-symlink output, `record` and `verify` commands, and exact no-shell `gh attestation verify` policy enforcement.
- [x] 1.4 Prove the verifier checks parsed image and candidate-file subjects, requires the candidate-byte attestation, and supports both exact local image bundles and OCI-referrer retrieval without importing aggregate release inputs.
- [x] 1.5 Remove unsigned server-assigned attestation pointers from the authoritative record and require a second trusted attestation over the exact canonical candidate bytes.

## 2. Runtime Candidate Producer

- [x] 2.1 Add failing workflow-contract tests for required permissions, hosted build IDs, build-output digest use, source-SHA discovery tags, release/tag/HEAD binding, distinct checkout and OIDC identities, pinned image/candidate attestation inputs, and unique non-overwriting release evidence.
- [x] 2.2 Update automatic hosted runtime publication to prove the release tag commit, attest the exact build digest and candidate bytes, locally verify both, probe the immutable image identity, and upload uniquely named candidate/image-bundle/candidate-bundle release assets.
- [x] 2.3 Update manual hosted runtime republishing to require dispatch on the exact input tag ref and equality among tag commit, checked-out `HEAD`, and `GITHUB_SHA` before performing the same candidate flow.
- [x] 2.4 Verify the runtime producer never treats mutable aliases as authority and never reads or copies an aggregate hosted-release manifest.

## 3. Provisioner Candidate Producer

- [x] 3.1 Add failing distribution tests for main-only admission, narrowed owned-input triggers, exact build-output subject, pinned image/candidate attestation policy, remote OCI verification, typed candidate attachment, and build-context isolation.
- [x] 3.2 Pin and checksum the ORAS client used to publish and retrieve the provisioner candidate attachment.
- [x] 3.3 Remove aggregate runtime, Substrate, and final-release triggers and verification from the provisioner workflow while retaining immutable provisioner image smoke.
- [x] 3.4 Attest the exact provisioner build digest and candidate bytes, verify both plus the persisted image OCI referrer, attach candidate JSON and its candidate bundle to the exact subject, and pull by returned descriptor digest for byte comparisons.
- [x] 3.5 Prove candidate JSON, attestation bundles, and aggregate contract files cannot enter the provisioner image build context.

## 4. Verification and Delivery

- [x] 4.1 Run the focused candidate, runtime-distribution, and provisioner-distribution suites plus Ruff, targeted mypy, strict OpenSpec validation, and the repository hosted validator.
- [x] 4.2 Obtain an independent workflow/security review and an independent exact-HEAD verifier pass before merge.
- [x] 4.3 Merge the producer change, observe the first eligible main-branch provisioner candidate and OCI verification, and confirm no runtime candidate is produced before the `0.35.1` release.
- [x] 4.4 Allow the `0.35.1` release only after the producer proof is green; retain its exact runtime candidate and release assets before starting aggregate release composition.

Evidence:

- Producer PR #357 merged at `9c2914881341d9444262a90789ba82435c4423ec` after focused tests, Ruff, mypy, strict OpenSpec, hosted validation, independent security review, and exact-head verification passed.
- Canonical-main provisioner run `30512025964` published and verified `ghcr.io/artexis10/exomem-provisioner@sha256:b3f2f12691207200a57dd193f3669a8f2cd2f7c058105b0d4af691f3057097df`; its OCI candidate attachment `sha256:49092a58f970117ac98dacf5a9f803cc77a1aaa34f00f9db0b37e1b8a344acda` was independently pulled and byte-compared.
- Release PR #355 merged only after that proof. Release run `30512612543` published `v0.35.1` from `f1472c297d9256a28c9706bb666e249b64cfd804`, retained all three uniquely named runtime evidence assets, and independently verified `ghcr.io/artexis10/exomem@sha256:3264271d7292c713e1f6ba6ae4a11a4b8e8c52a58a1a06e1d13726a515175ca3` from both the release bundle and GHCR.
