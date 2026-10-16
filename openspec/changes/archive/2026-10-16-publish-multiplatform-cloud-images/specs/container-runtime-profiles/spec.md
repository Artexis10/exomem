## ADDED Requirements

### Requirement: Exomem Cloud Images Publish One Multi-Platform Index

A release that publishes container images SHALL publish the Exomem Cloud cell image and the cellctl image for `linux/amd64` and `linux/arm64` as one image index per tag. Each platform SHALL be built on a runner of its own architecture. The release SHALL attest the index digest and retain its signed receipt as a release asset; a node pulls the platform manifest that matches its architecture. Every binary that the Cloud cell or cellctl image build downloads SHALL be selected by the target architecture and checked against a sha256 pinned for that architecture, and a target architecture without a pin SHALL fail the build. The CUDA image MAY remain `linux/amd64` only. Retired image roles SHALL remain retired.

#### Scenario: A release publishes both platforms under one digest

- **WHEN** a release publishes the Cloud images
- **THEN** `X.Y.Z-cloud` and `exomem-cellctl:X.Y.Z` each resolve to one index that lists `linux/amd64` and `linux/arm64`
- **AND** the image attestation and retained release receipt name that index digest

#### Scenario: A binary for the wrong architecture fails the build

- **WHEN** an image build targets an architecture that has no pinned download, or the downloaded restic binary cannot run on the build platform
- **THEN** the build fails before the image is published

#### Scenario: Each architecture is smoke-tested before merge

- **WHEN** a pull request changes a Cloud image's Dockerfile, its dependency manifest or lock, or the source it packages
- **THEN** CI builds the cell and cellctl images natively on `linux/amd64` and `linux/arm64`
- **AND** on each, restic runs, the CLI reports its version, and a cell saves a note and finds it

### Requirement: Native publication preserves release recovery

The release plan SHALL remain authoritative for lean, ml, cuda, cloud and cellctl. Native jobs SHALL build only missing roles, push only by digest, and pass successful child digests through Actions artifacts. One serialized publisher SHALL recover authentic existing receipts before joining new native digests. New Cloud and cellctl indexes SHALL contain exactly `linux/amd64` and `linux/arm64` runtime descriptors, preserve BuildKit provenance, and be inspected by digest before attestation. Historical authentic receipts SHALL NOT require both platforms or trigger a rebuild. Only the existing image finalizer SHALL write release aliases after evidence and conflict checks.

#### Scenario: A retry finds an authentic historical receipt

- **WHEN** original evidence exists for a role, including a historical single-platform image
- **THEN** the release recovers that digest without rebuilding or replacing its receipt
- **AND** it completes missing aliases only after the existing conflict checks

#### Scenario: One native role fails

- **WHEN** a native role lacks a complete platform pair
- **THEN** the serialized publisher completes unaffected roles with valid evidence
- **AND** its finalizer reports the missing role as incomplete
- **AND** it creates no attestation or alias for the incomplete role

#### Scenario: A dry run builds the current release plan

- **WHEN** a release runs in dry-run mode
- **THEN** both native Cloud and cellctl platforms and the other three roles build
- **AND** the run performs no login, push, attestation, receipt upload or alias write
