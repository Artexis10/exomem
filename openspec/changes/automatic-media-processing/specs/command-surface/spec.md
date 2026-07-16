## ADDED Requirements

### Requirement: Process Media Is Exposed On Every Generated Surface
The single command registry SHALL define `process_media` once and expose the same process/status/retry contract through MCP, `/api/process_media`, OpenAPI, and the `kb`/`exomem` CLI. All surfaces SHALL call the same orchestration leaf and return the shared result/error envelope.

#### Scenario: One registry entry exposes process media everywhere
- **WHEN** the command registry is built
- **THEN** `process_media` appears in MCP, REST, OpenAPI, and CLI without per-surface business logic

#### Scenario: Process one artifact
- **WHEN** a caller invokes `process_media` with `operation=process` and a supported governed path
- **THEN** the response reports its canonical sidecar and durable pending/completed state without waiting for ASR

#### Scenario: Inspect actionable status
- **WHEN** a caller invokes `process_media` with `operation=status`
- **THEN** the response includes aggregate counts and bounded per-artifact paths, attempts, errors, retryability, and next actions

#### Scenario: Retry failed media
- **WHEN** a caller invokes `process_media` with `operation=retry` and an optional artifact path
- **THEN** retryable matching work returns to pending and the response reports the number requeued
