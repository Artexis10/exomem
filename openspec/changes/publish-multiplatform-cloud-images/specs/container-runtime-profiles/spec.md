## ADDED Requirements

### Requirement: Exomem Cloud Images Publish One Multi-Platform Index

A release that publishes container images SHALL publish the Exomem Cloud cell image and the cellctl image for `linux/amd64` and `linux/arm64` as one image index per tag. Each platform SHALL be built on a runner of its own architecture. The release SHALL attest the index digest and record it in the release notes; a node pulls the platform manifest that matches its architecture. Every binary that the Cloud cell or cellctl image build downloads SHALL be selected by the target architecture and checked against a sha256 pinned for that architecture, and a target architecture without a pin SHALL fail the build. The hosted runtime, provisioner and CUDA images MAY remain `linux/amd64` only.

#### Scenario: A release publishes both platforms under one digest

- **WHEN** a release publishes the Cloud images
- **THEN** `X.Y.Z-cloud` and `exomem-cellctl:X.Y.Z` each resolve to one index that lists `linux/amd64` and `linux/arm64`
- **AND** the image attestation and the release notes name that index digest

#### Scenario: A binary for the wrong architecture fails the build

- **WHEN** an image build targets an architecture that has no pinned download, or the downloaded restic binary cannot run on the build platform
- **THEN** the build fails before the image is published

#### Scenario: Each architecture is smoke-tested before merge

- **WHEN** a pull request changes a Cloud image's Dockerfile, its dependency manifest or lock, or the source it packages
- **THEN** CI builds the cell and cellctl images natively on `linux/amd64` and `linux/arm64`
- **AND** on each, restic runs, the CLI reports its version, and a cell saves a note and finds it
