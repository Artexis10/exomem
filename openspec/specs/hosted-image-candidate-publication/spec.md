# hosted-image-candidate-publication Specification

## Purpose

Define independent, source-bound, digest-authoritative, provenance-verifiable hosted runtime and provisioner image candidates for later release composition.

## Requirements

### Requirement: Image candidates are digest-authoritative and strict

The system SHALL produce a closed, versioned candidate record for each hosted runtime or provisioner image. The record SHALL identify one tag-free image repository, the exact `sha256:` digest returned by that image's build-and-push step, the derived immutable repository-at-digest reference, component kind, source identity, workflow identity, and signed image-attestation policy. Server-assigned attestation IDs and UI URLs MUST NOT be treated as authoritative candidate fields. Unknown, missing, partial, malformed, mutable-only, or cross-field-inconsistent values MUST be rejected, and no tag SHALL be treated as release authority.

#### Scenario: Build output becomes the candidate subject

- **WHEN** the hosted image build returns an OCI digest and the candidate is recorded
- **THEN** the candidate subject and immutable reference use that exact digest without substitution or derivation from a tag, contract, manifest, or file hash

#### Scenario: Mutable reference is supplied

- **WHEN** a candidate contains only a version, `latest`, `hosted`, source-SHA, or other tag reference
- **THEN** candidate recording or verification fails before the candidate can be consumed

#### Scenario: Candidate schema is extended implicitly

- **WHEN** a candidate contains an unknown field or an unsupported schema version or component kind
- **THEN** verification fails closed instead of ignoring the field

### Requirement: Provenance is bound to certificate-controlled source and signer identity

Each candidate SHALL carry one SLSA provenance attestation created for the exact OCI subject and a second attestation created for the exact canonical candidate file by the allowed GitHub repository and component workflow. Acceptance MUST cryptographically verify both attestations' predicate type, repository, source commit, source ref, signer workflow, and signer workflow digest; the image statement's exact subject name/digest; and the candidate statement's exact basename/SHA-256. It MUST reject self-hosted-runner provenance. Workflow-supplied predicate values alone MUST NOT satisfy source or signer identity.

#### Scenario: Exact reviewed producer verifies

- **WHEN** the image and exact candidate bytes are signed by the allowed component workflow at its recorded digest for the recorded main or release source ref
- **THEN** both certificates, transparency timestamps, predicates, source identities, signer identities, and exact image/file subjects verify

#### Scenario: One lineage field differs

- **WHEN** the repository, source commit/ref, signer workflow/digest, predicate type, subject name, or OCI digest differs from the candidate
- **THEN** attestation verification fails and the image is not admitted as a candidate

#### Scenario: Release metadata is rewritten after image signing

- **WHEN** a runtime candidate's tag, version, storage URI, workflow metadata, or other canonical byte changes after the image attestation was issued
- **THEN** candidate-file attestation verification fails even if the unchanged image bundle still verifies

#### Scenario: Unreviewed branch produces a valid signature

- **WHEN** a workflow run on an unapproved branch signs an otherwise well-formed image
- **THEN** candidate policy rejects its source ref even though the signature is cryptographically valid

### Requirement: Attestation publication is durable and immediately verified

Each producer SHALL use a full-commit-pinned `actions/attest@v4` with `packages: write`, `id-token: write`, and `attestations: write`; provisioner publication SHALL retain `contents: read`, while runtime publication MAY use `contents: write` only to retain release assets. It SHALL push the image attestation to the image registry, persist both image and candidate-file attestations through GitHub's attestations service, disable organization-only artifact storage records, hash the returned image Sigstore bundle into the candidate, and verify both attestations before declaring publication successful. Candidate JSON, image bundle, and candidate bundle SHALL be uploaded together as bounded workflow evidence. Runtime evidence SHALL also be uploaded to the matching GitHub release under unique non-overwriting names. Provisioner candidate JSON and candidate bundle SHALL be attached together to the exact image subject with versioned media types, resolved by descriptor digest, pulled back, and byte-compared before success.

#### Scenario: Candidate survives workflow-artifact expiry

- **WHEN** the convenience workflow artifact is no longer available
- **THEN** the recorded image and exact candidate bytes remain verifiable through GitHub, the matching release, or the OCI registry rather than depending on a mutable tag or expired build log

#### Scenario: Provisioner candidate attachment drifts

- **WHEN** the provisioner candidate or candidate bundle pulled by its returned OCI descriptor digest differs from the locally verified bytes
- **THEN** publication fails and the attachment is not accepted as durable candidate evidence

#### Scenario: Bundle bytes are changed

- **WHEN** a local or downloaded image bundle does not match the candidate's bundle SHA-256, or a candidate bundle does not attest the exact candidate bytes
- **THEN** verification fails before the candidate can be accepted as provenance evidence

#### Scenario: Attestation cannot be verified after publication

- **WHEN** the newly created attestation cannot be retrieved or verified against the candidate policy
- **THEN** the producer job fails and does not report a usable candidate

### Requirement: Release checkout and attested source identities remain truthful

Automatic hosted runtime publication SHALL prove that the created release tag resolves to the workflow source commit before building. Manual runtime republishing SHALL run only from the exact input tag ref and SHALL prove that the tag commit, checked-out `HEAD`, and `GITHUB_SHA` are identical. The candidate SHALL record the attested workflow source ref separately from the checkout tag. Manual provisioner publication SHALL be accepted only from the default branch.

#### Scenario: Automatic release is built from its tag commit

- **WHEN** Release Please creates a release during a main-branch push
- **THEN** the hosted build proceeds only if the release tag resolves to that push commit, while the candidate records `refs/heads/main` as attested source ref and the tag as checkout ref

#### Scenario: Manual dispatch names a different tag

- **WHEN** a user dispatches the runtime workflow from main or another ref while naming an existing release tag
- **THEN** the job fails before publishing because the workflow ref is not that exact tag ref

#### Scenario: Manual provisioner dispatch uses a feature branch

- **WHEN** the provisioner workflow is dispatched from a ref other than the default branch
- **THEN** candidate publication fails before image build or attestation

### Requirement: Runtime and provisioner producers are independent

The runtime and provisioner workflows SHALL build, attest, verify, and emit their own candidates without reading the other image candidate or any final aggregate hosted-release manifest. The provisioner producer MUST NOT trigger from or verify runtime manifests, Substrate fixture selections, final release composition, or unrelated runtime gates. Candidate records and attestation bundles MUST NOT be copied into either image build context.

#### Scenario: Provisioner source changes

- **WHEN** an owned provisioner build input changes on the default branch
- **THEN** the provisioner workflow can publish and verify a candidate without a runtime candidate, Substrate selection, or aggregate manifest

#### Scenario: Aggregate release metadata changes

- **WHEN** only a final hosted-release manifest or Substrate selection changes
- **THEN** neither image producer rebuilds or republishes an image

#### Scenario: Candidate evidence is inspected in an image

- **WHEN** either published image filesystem and build context are audited
- **THEN** no candidate JSON, attestation bundle, or final aggregate manifest is embedded as an image input

### Requirement: Published images pass immutable identity smoke

Before emitting a usable candidate, each workflow SHALL probe the repository-at-digest image and SHALL require its OCI revision identity to equal the recorded source commit. The provisioner SHALL retain its packaged migration and command smoke checks; the runtime SHALL retain its hosted-image contract smoke without consulting aggregate release metadata. Failures MUST leave the pushed image unselected and the candidate unusable.

#### Scenario: Revision label differs from the candidate

- **WHEN** the immutable image reports an OCI revision other than the recorded source commit
- **THEN** candidate verification fails even if the image digest and signature are otherwise valid

#### Scenario: Provisioner package is incomplete

- **WHEN** the immutable provisioner image lacks required packaged migrations, commands, or exact runtime head identity
- **THEN** its existing image smoke fails and no usable provisioner candidate is emitted
