## MODIFIED Requirements

### Requirement: Published Container Runtime Families

The project SHALL publish distinct container runtime families for lean lexical
search, CPU hybrid search, and NVIDIA CUDA-capable hybrid search. The lean family
SHALL remain the default `latest` and `X.Y.Z` tag. The CPU hybrid family SHALL use
`ml` and `X.Y.Z-ml` tags. The CUDA-capable family SHALL use `cuda` and `X.Y.Z-cuda`
tags. The CUDA family SHALL package deterministic measurement dependencies only
and MUST NOT add any server-side reasoning model.

#### Scenario: Release publishes all runtime tags

- **WHEN** a release publishes container images
- **THEN** lean tags include `latest` and the immutable version tag
- **AND** CPU hybrid tags include `ml` and the immutable `X.Y.Z-ml` tag
- **AND** CUDA tags include `cuda` and the immutable `X.Y.Z-cuda` tag

#### Scenario: Default pull remains resource-light

- **WHEN** a user pulls `ghcr.io/substrate-systems/evermatter:latest`
- **THEN** the image is the lean runtime family
- **AND** it does not include torch, CUDA runtime libraries, or model downloads

