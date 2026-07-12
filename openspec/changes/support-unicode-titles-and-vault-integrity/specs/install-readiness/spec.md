## ADDED Requirements

### Requirement: Doctor Infers The Available Capability Profile

When neither `--profile` nor `EXOMEM_PROFILE` selects a profile, doctor SHALL infer the highest locally installed capability profile without importing or loading models and without downloading assets. `EXOMEM_DISABLE_EMBEDDINGS` SHALL force lean inference.

#### Scenario: Embeddings install defaults to hybrid

- **WHEN** the embeddings dependency set is discoverable, media dependencies are not complete, and no profile is configured
- **THEN** doctor runs and labels the report as `hybrid`

#### Scenario: Lean install remains lean

- **WHEN** embeddings dependencies are unavailable or explicitly disabled and no profile is configured
- **THEN** doctor runs and labels the report as `lean`

#### Scenario: Explicit profile wins

- **WHEN** a valid profile is supplied by CLI or environment
- **THEN** doctor uses that profile regardless of inferred installed capabilities
