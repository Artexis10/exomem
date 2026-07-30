## Context

The release workflow publishes the hosted runtime alongside the ordinary lean, ML, and CUDA images. The provisioner has a separate main-branch workflow. Both workflows push useful images, but neither produces a reusable trust object from the exact OCI digest returned by its build. The provisioner workflow is also coupled to an aggregate hosted-release manifest, Substrate fixture selection, and runtime gate, so it cannot publish independently.

That coupling already produced an invalid release unit: the manifest could claim a provisioner digest unrelated to the built image, and a manual runtime republish could label one checked-out tag with `github.sha` from another ref. The next runtime contract is `0.35.1`; its runtime image must be published only after this producer contract lands.

GitHub artifact attestations are available for this public repository. `actions/attest@v4` can attest both the exact image repository/build-output digest and the completed candidate file, persist both attestations in GitHub, push the image attestation to GHCR, and return each Sigstore bundle. The repository is user-owned rather than organization-owned, so linked-artifact storage records are unavailable and must be disabled.

## Goals / Non-Goals

**Goals:**

- Produce runtime and provisioner candidates independently from the workflow that built each image.
- Make the OCI digest returned by `docker/build-push-action` the only image authority.
- Bind both the image and the exact candidate bytes to the attestation certificate's repository, source commit/ref, signer workflow and signer digest, not merely to workflow-supplied predicate fields.
- Make automatic release publication and deliberate republishing identify the checked-out tag commit truthfully.
- Retain re-verifiable attestation evidence in GitHub and GHCR, with a canonical candidate record for later release composition.
- Fail before candidate publication or acceptance on malformed, mutable, mismatched, or unreviewed lineage.

**Non-Goals:**

- Assemble or commit the final multi-component hosted release manifest.
- Change the provisioner protocol, runtime health contract, Substrate candidate catalog, or deployment chart selection.
- Promote a candidate, deploy a cell, or submit either marketplace listing.
- Attest the non-hosted lean, ML, or CUDA images in this change.

## Decisions

### 1. Use one strict candidate schema for two component kinds

`hosted_image_candidate.py record` will write canonical JSON with `schemaVersion: 1` and a closed set of fields:

- `kind`: `runtime` or `provisioner`;
- `image`: tag-free repository, `sha256:` digest, and derived immutable reference;
- `source`: exact GitHub repository plus the checked-out artifact ref and 40-character commit (`refs/tags/v...` for runtime, `refs/heads/main` for provisioner);
- `release`: required runtime tag/version and absent for provisioner candidates;
- `workflow`: workflow path, signer-workflow digest, certificate/OIDC source ref and commit, event, run ID and run attempt;
- `attestation`: SLSA predicate type, image subject name/digest, and SHA-256 of the returned image Sigstore bundle. Server-assigned IDs and UI URLs are deliberately excluded because they are not part of the signed image statement.

The recorder receives the build digest and image bundle as data, validates all shapes and cross-field equalities, hashes the bundle bytes, and writes sorted UTF-8 JSON with a trailing newline. The workflow then attests that completed file. It does not invent, resolve, or substitute a digest. The verifier rejects extra fields so future schema changes are explicit.

Alternatives considered:

- Treat the aggregate release manifest as the candidate: rejected because composition happens after both independent images exist and must not participate in either build.
- Treat a source-SHA tag as immutable identity: rejected because GHCR tags can move; the tag remains a discovery alias only.

### 2. Separate attested source ref from checked-out release ref

Automatic Release Please publication runs in a `push` workflow on `refs/heads/main` while checking out the newly created release tag. Its attestation certificate therefore truthfully names the main ref. The candidate records both that attested source ref and the checkout tag, and the workflow proves the tag commit equals `GITHUB_SHA` before building.

Manual republishing is accepted only when the workflow itself is dispatched on the exact input tag ref and `git rev-parse <tag>^{commit}` equals both checked-out `HEAD` and `GITHUB_SHA`. A dispatch from main that merely names a tag fails before publishing. Provisioner manual dispatch is accepted only from `refs/heads/main`.

The verifier admits only the exact repository, the runtime release workflow or provisioner workflow, an allowed main/tag source ref for that mode, the recorded signer workflow digest, and the exact source commit. This distinguishes the certificate-controlled GitHub identity from user-controlled provenance predicate content.

Alternative considered: require a tag source ref for automatic releases. Rejected because the signer runs in the main push event even though checkout selects the tag; claiming otherwise would make correct attestations unverifiable.

### 3. Attest and verify the real OCI subject and candidate bytes

Each hosted build step receives an ID. The attestation step uses the tag-free fully qualified image name and `steps.<build>.outputs.digest`, with:

- `actions/attest@v4` pinned to a reviewed full commit;
- `push-to-registry: true`;
- `create-storage-record: false`;
- job permissions `packages: write`, `id-token: write`, and `attestations: write`, with `contents: read` for the provisioner and narrowly required `contents: write` for runtime release-asset upload.

After recording, the same pinned action separately attests the candidate file without registry push. `hosted_image_candidate.py verify` validates the closed record and exact image-bundle hash, then invokes `gh attestation verify` twice: once on the immutable OCI reference and once on the candidate file. Both calls fix the repository, predicate type, signer workflow, signer digest, source digest and source ref and deny self-hosted runners. Image verification can use either the recorded bundle or the OCI registry copy. Parsed statements must contain exactly the recorded image subject and exactly the candidate basename plus SHA-256 of the candidate bytes. Release tag/version, storage, workflow metadata, and image identity therefore cannot be rewritten independently.

The workflows then probe the immutable image and require the OCI revision label to equal the recorded source commit. Existing provisioner smoke validation remains, but aggregate hosted-release verification is removed from the producer job.

Alternative considered: trust BuildKit's generated provenance alone. Rejected because current release acceptance needs a GitHub-verifiable signer/source policy and a durable, independently addressable attestation.

### 4. Keep evidence durable without making workflow artifacts authoritative

Both attestations are persisted through GitHub's attestations API, and the image attestation is also pushed to GHCR. Both workflows upload the candidate JSON, image bundle, and candidate bundle as a bounded workflow artifact for immediate audit.

Runtime evidence is uploaded to the matching GitHub release under unique, non-overwriting names containing the source commit, image digest, run ID, and attempt. Provisioner evidence has no product release to attach to, so its candidate JSON and candidate-attestation bundle are attached together to the exact image subject as a typed OCI artifact and pulled back by returned descriptor digest for byte comparisons. Its image Sigstore attestation remains a separate OCI referrer. Workflow-artifact retention is therefore convenient transport, not the sole trust anchor.

The candidate JSON is signed evidence metadata, not an image input. Neither candidate records, bundles, nor the final aggregate manifest enter a Docker build context. The OCI candidate attachment is a sibling artifact of the already-built image subject, never a Docker layer or build input.

Alternative considered: publish another container image containing the candidate record. Rejected because a typed OCI attachment provides durable subject binding without creating a second runnable image or a digest/self-reference loop.

### 5. Make producer ownership explicit

The provisioner workflow trigger is narrowed to provisioner source, its cell-chart build input, its image verifier, the candidate tooling, and the workflow itself. Aggregate release manifests, runtime gates, Substrate selection, and `verify_hosted_release.py` are removed from its triggers and steps.

The runtime candidate remains release-driven. A source-commit discovery tag is added alongside the existing version/hosted aliases, but all downstream consumers must use the candidate's digest reference.

## Risks / Trade-offs

- **A compromised producer workflow can sign malicious bytes** -> Pin the attestation action, verify the signer workflow path and digest, admit only reviewed main/release refs, and keep branch PRs unable to publish accepted candidates.
- **A tag is moved after release** -> Prove tag/HEAD/`GITHUB_SHA` equality before build and retain the source commit in the attestation and record; consume only the OCI digest.
- **Candidate metadata is rewritten after image signing** -> Separately attest the exact canonical candidate bytes and verify their basename and SHA-256 before accepting any release, storage, or workflow field.
- **Workflow artifacts expire** -> Persist attestations through both GitHub and GHCR, upload runtime evidence to the release, and attach the provisioner candidate to its exact OCI subject.
- **A later workflow version changes policy** -> Record and verify `github.workflow_sha`; candidate acceptance remains tied to the exact reviewed workflow bytes.
- **GHCR or GitHub attestation lookup is unavailable** -> Preserve the returned bundle and support local-bundle verification; publication fails if the immediate verification cannot complete.
- **The provisioner image publishes before its later protocol change** -> This change only establishes the producer. A later provisioner change publishes a new digest/candidate; no candidate is deployed merely because it exists.

## Migration Plan

1. Land the candidate schema, verifier, tests, and workflow changes.
2. Let the main-branch provisioner workflow publish and verify a first independent candidate; do not promote it.
3. Merge the `0.35.1` release PR. The release workflow builds, attests, verifies, and retains the runtime candidate from that exact release commit.
4. Use the runtime candidate and the later provisioner candidate as inputs to the separate runtime-identity/release-composition change.
5. If producer publication fails, fix or revert only these workflow/tooling changes. Previously pushed images remain unselected and cannot become deploy authority without a valid candidate and later composition.

## Open Questions

None. Final release composition, the provisioner v2 bridge, and deployment promotion are intentionally resolved in the next OpenSpec change.
