## ADDED Requirements

### Requirement: The Cloud CLI needs no endpoint setup and reports its phases

`exomem cloud import` and `exomem cloud export` SHALL default their MCP and API endpoints to the production Exomem Cloud endpoints, defined once in the product. The `--mcp-url` and `--api-base-url` flags and the `EXOMEM_CLOUD_MCP_URL` and `EXOMEM_CLOUD_API_BASE_URL` variables SHALL override the defaults. Before it builds the archive, `exomem cloud import` SHALL report the selection's file count and bytes, and SHALL refuse with the measured values when `age` is not available or when the free space where it writes the ciphertext is smaller than the ciphertext's expected size. It SHALL then report progress for each phase: archive and encryption (bytes written), upload (parts and bytes sent), and server import (state). Every failure SHALL name its phase, a content-free reason code and the import id when one exists. Resume SHALL keep using the existing checkpoint.

#### Scenario: An import runs with no environment set

- **WHEN** a user runs `exomem cloud import ~/Notes` with neither endpoint variable set
- **THEN** the command uses the production endpoints and reports the preflight count and bytes before encrypting

#### Scenario: Too little disk for the ciphertext is refused before encrypting

- **WHEN** the selection's bytes exceed the free space where the ciphertext is written
- **THEN** the command refuses with the expected size and the free space, and writes no ciphertext

#### Scenario: A failed upload says where it stopped

- **WHEN** the upload phase fails after some parts are sent
- **THEN** the command reports the upload phase, a reason code and the import id, and a rerun resumes from the checkpoint
