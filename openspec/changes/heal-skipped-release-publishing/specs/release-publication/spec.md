## ADDED Requirements

### Requirement: Each release artifact is published once from the release tag

The release workflow SHALL publish a release's artifacts only from a run on that release's tag at the tag's commit. The artifacts are the GitHub release assets, the PyPI distribution, the MCP Registry entry and every supported image. The supported image roles are lean, ml, cuda, cloud and cellctl. Before publishing, the run SHALL check which artifacts are present and SHALL publish only the missing ones, so an artifact that exists is never rebuilt with a new digest. Any other run that finds an artifact of the release missing SHALL hand the publish to a run on the tag. Runs that publish the same tag SHALL NOT overlap.

#### Scenario: The release's own run stops before publishing

- **WHEN** the run that created a release is cancelled or fails before it publishes
- **THEN** the next push to main finds the newest release's missing artifacts and hands the publish to a run on its tag, which publishes only those artifacts

#### Scenario: Some artifacts already exist

- **WHEN** a run verifies matching wheel and sdist filenames and hashes, and complete original evidence for some supported images, while other images are missing
- **THEN** it builds only the missing images and preserves every existing immutable file and tagged digest

#### Scenario: Publishing keeps failing on one tag

- **WHEN** three publish runs on one tag have failed
- **THEN** pushes to main stop handing that tag off and report it as held until a maintainer dispatches it

#### Scenario: A tag that predates the publish path

- **WHEN** the workflow at an older tag refuses the hand-off
- **THEN** the handing-off run warns with the missing artifacts and does not run that tag's earlier publish jobs

#### Scenario: Dry run

- **WHEN** a maintainer dispatches the workflow with `dry_run` for an existing tag from any ref
- **THEN** the run builds the wheel, sdist, Cloud archives, and every supported image from the tag regardless of registry presence or publication settings
- **AND** it creates no package, OCI, release-asset, MCP, attestation, deployment, or live-record publication
- **AND** it may retain temporary Actions artifacts and caches for verification

### Requirement: Recovery preserves original publication evidence

New images SHALL publish by digest before discovery tags. The workflow SHALL
verify and retain original image attestations before attaching tags to that same
digest. Recovery SHALL preserve receipt filenames, producer fields and signed
bytes. Current publication and automatic recovery SHALL exclude Hosted images
and Records rollback claims. Historical Hosted candidate files and both original
Sigstore bundles SHALL remain immutable and verifiable through the standalone
verifier from each receipt's exact source commit. Both immutable aliases SHALL
continue to match the verified image digest.

#### Scenario: A producer stops before tagging

- **WHEN** a producer retains complete evidence but stops before creating a version tag
- **THEN** recovery finds its release assets or Actions artifacts, verifies the original digest and producer evidence, and fills missing tags without rebuilding

#### Scenario: Historical Hosted receipts remain verifiable

- **WHEN** current publication retires Hosted images
- **THEN** historical release assets retain their original filenames, producer fields and signed bytes
- **AND** the receipt's source-commit verifier authenticates the unchanged candidate and both bundles with the maintained gh CLI
- **AND** the version and commit aliases still match the verified image digest

#### Scenario: An immutable discovery tag conflicts

- **WHEN** an existing version or commit tag names a different digest
- **THEN** recovery reports the conflict before its first tagging write and never replaces that tag
- **AND** a surviving immutable alias without recoverable evidence prevents rebuilding even when the version tag is absent

#### Scenario: A legacy image has no retained evidence

- **WHEN** an existing version tag has no recoverable original producer evidence
- **THEN** recovery reports the image as incomplete and does not rebuild or fabricate evidence

#### Scenario: Another release becomes latest during recovery

- **WHEN** recovery is ready to update a floating tag
- **THEN** it serializes image publishers across release versions, checks the latest release inside that serialized job, and updates only the current latest release
- **AND** dry runs use separate concurrency groups

### Requirement: Distribution completeness comes from identity and bytes

The workflow SHALL parse wheel and sdist filenames against the tagged project's
name and version. It SHALL require both formats and compare PyPI filenames and
SHA256 hashes. It SHALL use committed Cloud metadata and one source-to-release
filename mapping for staging and completeness. Retries SHALL reuse retained
release files or producer artifacts and publish only missing matching files.

#### Scenario: PyPI contains only the wheel

- **WHEN** PyPI returns a version with its wheel but no matching sdist
- **THEN** recovery preserves the matching wheel and publishes only the missing sdist

#### Scenario: A filename already exists with different bytes

- **WHEN** an existing release or PyPI file differs from the retained file's SHA256 hash
- **THEN** recovery reports an immutable-file conflict and never replaces or skips over it

#### Scenario: Dispatch follows a skipped Release Please job

- **WHEN** Release Please skips a dispatch but its plan and other direct prerequisites succeed
- **THEN** artifact and image jobs run, while a failed or cancelled direct prerequisite prevents publication

#### Scenario: A raw receipt names another image target or producer

- **WHEN** a receipt filename names a different target, run, or attempt than its authenticated evidence
- **THEN** recovery rejects it using BuildKit provenance for every platform and GitHub's signed producer invocation

#### Scenario: A later dry run produces different package bytes

- **WHEN** original published distributions exist and a later dry run retains different bytes
- **THEN** recovery keeps complete published distributions without merging dry-run artifacts
- **AND** partial recovery selects one matching publication producer through GitHub job records and downloads its exact artifact ID
