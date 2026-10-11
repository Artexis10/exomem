## MODIFIED Requirements

### Requirement: Release checkout and attested source identities remain truthful

Hosted runtime publication SHALL run only from the exact release tag ref and SHALL prove that the tag commit, checked-out `HEAD`, and `GITHUB_SHA` are identical before building. A publication run on another ref that names a release tag SHALL hand publication to a run on that tag. A dry run MAY build the tagged source from another ref, but SHALL NOT attest or publish it. The candidate SHALL record the attested workflow source ref separately from the checkout tag. Manual provisioner publication SHALL be accepted only from the default branch.

#### Scenario: Automatic release is built from its tag commit

- **WHEN** a release's runtime image is published, automatically or by hand
- **THEN** the hosted build runs in a run on the release tag, proceeds only if the tag resolves to that run's commit, and the candidate records the tag as both the attested source ref and the checkout ref

#### Scenario: Manual dispatch names a different tag

- **WHEN** a user dispatches publication from main or another ref while naming an existing release tag
- **THEN** that run builds nothing and hands the publish to a run on the exact tag ref

#### Scenario: Manual provisioner dispatch uses a feature branch

- **WHEN** the provisioner workflow is dispatched from a ref other than the default branch
- **THEN** candidate publication fails before image build or attestation

#### Scenario: A branch dry run checks tagged source

- **WHEN** a user dispatches a dry run from another ref while naming an existing release tag
- **THEN** the workflow verifies the checkout against the resolved tag commit and builds without creating candidate attestations or publishing
