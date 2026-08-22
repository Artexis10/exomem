## MODIFIED Requirements

### Requirement: Profile-Specific Readiness

The doctor command SHALL validate the requested capability profile. `lean` SHALL
check Python/package/vault/registry basics. `hybrid` SHALL additionally check
embeddings dependencies and embedding sidecar state. `media` SHALL additionally
check media extraction dependencies and Tesseract discovery. `remote` SHALL
additionally check public URL and OAuth-related environment variables. `hybrid`
and `media` SHALL additionally include a read-only `models.cache` check that
inspects the local Hugging Face hub cache directories for the embedding model,
reranker, and (for `media`, when enabled) the CLIP model, without making any
network request and without downloading anything.

#### Scenario: Optional capability profile is requested

- **WHEN** `doctor --profile media` is run without media extraction dependencies
- **THEN** the report marks the missing media components as failures
- **AND** the remediation names `uv sync --extra media` and any required system
  tool such as Tesseract

#### Scenario: Models are already cached locally

- **WHEN** `doctor --profile hybrid` is run and the embedding model and reranker are already
  present in the local Hugging Face hub cache
- **THEN** the `models.cache` check reports a passing status
- **AND** no network request is made and no model is downloaded as part of the check

#### Scenario: Models are not yet cached locally

- **WHEN** `doctor --profile hybrid` or `doctor --profile media` is run and one or more of the
  embedding model, reranker, or (when CLIP is enabled) CLIP model are not present in the local
  Hugging Face hub cache
- **THEN** the `models.cache` check reports a warn-level status naming the missing model(s)
- **AND** the remediation is to run `exomem warm`
- **AND** no vault file, cache directory, or model file is created, modified, or downloaded by the
  check itself
