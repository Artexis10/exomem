## ADDED Requirements

### Requirement: Each release artifact is published once from the release tag

The release workflow SHALL publish a release's artifacts only from a run on that release's tag at the tag's commit. The artifacts are the GitHub release assets, the PyPI distribution, the MCP Registry entry and every image the release builds. Before publishing, the run SHALL check which artifacts are present and SHALL publish only the missing ones, so an artifact that exists is never rebuilt with a new digest. Any other run that finds an artifact of the release missing SHALL hand the publish to a run on the tag. Runs that publish the same tag SHALL NOT overlap.

#### Scenario: The release's own run stops before publishing

- **WHEN** the run that created a release is cancelled or fails before it publishes
- **THEN** the next push to main finds the newest release's missing artifacts and hands the publish to a run on its tag, which publishes only those artifacts

#### Scenario: Some artifacts already exist

- **WHEN** a run on the tag finds the PyPI version and four images present and two images missing
- **THEN** it builds and publishes only the two missing images and leaves the present artifacts unchanged

#### Scenario: Publishing keeps failing on one tag

- **WHEN** three publish runs on one tag have failed
- **THEN** pushes to main stop handing that tag off and report it as held until a maintainer dispatches it

#### Scenario: A tag that predates the publish path

- **WHEN** the workflow at an older tag refuses the hand-off
- **THEN** the handing-off run warns with the missing artifacts and does not run that tag's earlier publish jobs

#### Scenario: Dry run

- **WHEN** a maintainer dispatches the workflow with `dry_run` for an existing tag from any ref
- **THEN** the run builds every artifact from the tag and pushes, uploads, attests and records nothing
