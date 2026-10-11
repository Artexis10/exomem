## MODIFIED Requirements

### Requirement: Release checkout and attested source identities remain truthful

Hosted runtime publication SHALL run only from the exact release tag ref and SHALL prove that the tag commit, checked-out `HEAD`, and `GITHUB_SHA` are identical before building. A run on another ref that names a release tag SHALL hand the publish to a run on that tag instead of building. The candidate SHALL record the attested workflow source ref separately from the checkout tag. Manual provisioner publication SHALL be accepted only from the default branch.

#### Scenario: Automatic release is built from its tag commit

- **WHEN** a release's runtime image is published, automatically or by hand
- **THEN** the hosted build runs in a run on the release tag, proceeds only if the tag resolves to that run's commit, and the candidate records the tag as both the attested source ref and the checkout ref

#### Scenario: Manual dispatch names a different tag

- **WHEN** a user dispatches the runtime workflow from main or another ref while naming an existing release tag
- **THEN** that run builds nothing and hands the publish to a run on the exact tag ref

#### Scenario: Manual provisioner dispatch uses a feature branch

- **WHEN** the provisioner workflow is dispatched from a ref other than the default branch
- **THEN** candidate publication fails before image build or attestation
